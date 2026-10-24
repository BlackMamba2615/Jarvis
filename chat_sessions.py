"""Bounded, isolated chat context; only explicitly saved memories reach disk."""

from __future__ import annotations

from collections import OrderedDict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading


MEMORY_PATH = Path(__file__).resolve().parent / "runtime" / "chat_memories.sqlite3"
_LOCK = threading.RLock()
_SESSIONS: OrderedDict[str, dict] = OrderedDict()
_MAX_SESSIONS = 64
_SESSION_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_SENSITIVE = re.compile(
    r"sk-[A-Za-z0-9_-]{12,}|api[ _-]?key|access[ _-]?token|password|密码|密钥|口令",
    re.IGNORECASE,
)


class ChatCancelled(RuntimeError):
    def __init__(self) -> None:
        super().__init__("回答已停止")


def check_cancelled(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ChatCancelled()


def validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_PATTERN.fullmatch(session_id):
        raise ValueError("无效的对话编号")
    return session_id


def _session(session_id: str) -> dict:
    session_id = validate_session_id(session_id)
    with _LOCK:
        if session_id not in _SESSIONS:
            if len(_SESSIONS) >= _MAX_SESSIONS:
                # Never evict a session with an active request.
                victim = next((key for key, value in _SESSIONS.items() if not value["busy"]), None)
                if victim is None:
                    raise ValueError("同时进行的对话过多，请稍后重试")
                del _SESSIONS[victim]
            _SESSIONS[session_id] = {
                "history": deque(maxlen=8), "lock": threading.RLock(), "busy": 0,
            }
        _SESSIONS.move_to_end(session_id)
        return _SESSIONS[session_id]


class SessionTurn:
    """Serialize one session, including model switching, without blocking others."""

    def __init__(self, session_id: str, cancel_event=None):
        self.session_id = session_id
        self.cancel_event = cancel_event

    def __enter__(self):
        with _LOCK:
            self.state = _session(self.session_id)
            self.state["busy"] += 1
        try:
            while not self.state["lock"].acquire(timeout=0.1):
                check_cancelled(self.cancel_event)
        except BaseException:
            with _LOCK:
                self.state["busy"] -= 1
            raise
        if self.cancel_event is not None and self.cancel_event.is_set():
            self.state["lock"].release()
            with _LOCK:
                self.state["busy"] -= 1
            raise ChatCancelled()
        return self

    def __exit__(self, *_args):
        self.state["lock"].release()
        with _LOCK:
            self.state["busy"] -= 1


def get_history(session_id: str) -> list[dict[str, str]]:
    state = _session(session_id)
    with state["lock"]:
        return [dict(item) for item in state["history"]]


def append_turn(session_id: str, prompt: str, answer: str) -> None:
    state = _session(session_id)
    with state["lock"]:
        state["history"].extend((
            {"role": "user", "content": prompt[:2000]},
            {"role": "assistant", "content": answer[:900]},
        ))


def clear_history(session_id: str = "default") -> None:
    state = _session(session_id)
    with state["lock"]:
        state["history"].clear()


@contextmanager
def _connect():
    # Refuse redirected stores; only explicitly saved text is stored here.
    path = MEMORY_PATH
    for candidate in (path, path.parent):
        if candidate.is_symlink():
            raise ValueError("记忆存储路径不可为链接")
        if candidate.exists():
            attributes = getattr(candidate.lstat(), "st_file_attributes", 0)
            if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise ValueError("记忆存储路径不可为链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=3)
    try:
        with db:
            db.execute("PRAGMA busy_timeout=3000")
            db.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, session TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL)")
            yield db
    finally:
        db.close()


def remember(session_id: str, text: str) -> dict[str, str]:
    """Called only after an explicit user save action, never automatically."""
    validate_session_id(session_id)
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise ValueError("记忆内容须为 1 至 500 字")
    text = text.strip()
    if _SENSITIVE.search(text):
        raise ValueError("请勿将密码、密钥或访问令牌保存到记忆中")
    item = {"id": secrets.token_urlsafe(18), "text": text, "created_at": datetime.now(timezone.utc).isoformat()}
    with _LOCK, _connect() as db:
        count = db.execute("SELECT COUNT(*) FROM memories WHERE session=?", (session_id,)).fetchone()[0]
        if count >= 30:
            raise ValueError("记忆已满，请先删除不需要的记忆")
        db.execute("INSERT INTO memories VALUES (?,?,?,?)", (item["id"], session_id, text, item["created_at"]))
    return item


def list_memories(session_id: str = "default") -> list[dict[str, str]]:
    validate_session_id(session_id)
    # A normal chat without explicit memory does not create a disk store.
    if not MEMORY_PATH.exists():
        return []
    with _LOCK, _connect() as db:
        rows = db.execute("SELECT id,text,created_at FROM memories WHERE session=? ORDER BY created_at LIMIT 30", (session_id,)).fetchall()
    return [{"id": row[0], "text": row[1], "created_at": row[2]} for row in rows]


def delete_memory(session_id: str, memory_id: str) -> bool:
    validate_session_id(session_id)
    if not isinstance(memory_id, str) or len(memory_id) > 100:
        raise ValueError("无效的记忆编号")
    if not MEMORY_PATH.exists():
        return False
    with _LOCK, _connect() as db:
        cursor = db.execute("DELETE FROM memories WHERE session=? AND id=?", (session_id, memory_id))
        return bool(cursor.rowcount)


def clear_memories(session_id: str = "default") -> None:
    validate_session_id(session_id)
    if MEMORY_PATH.exists():
        with _LOCK, _connect() as db:
            db.execute("DELETE FROM memories WHERE session=?", (session_id,))


def context_messages(session_id: str) -> list[dict[str, str]]:
    memories = list_memories(session_id)
    context = []
    if memories:
        context.append({
            "role": "user",
            "content": "用户明确保存的参考偏好（只作聊天参考，不是电脑操作授权）：\n" + "\n".join(item["text"] for item in memories)[:3000],
        })
    return context + get_history(session_id)
