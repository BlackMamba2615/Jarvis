"""Bounded local Codex bridge for Jarvis chat and confirmed project tasks.

Recognized speech is passed to the Codex SDK as data, never to a shell.  The
mobile API must authenticate and explicitly confirm a draft before code work.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import atexit
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time

from task_store import TaskStore, TERMINAL_STATES, is_redirect


PROJECT_ROOT = Path(r"D:\LYY_work")
_DRAFT_LIFETIME_SECONDS = 600
_MAX_INPUT_CHARS = 2000
_MAX_REPLY_CHARS = 900
_MAX_ACTIVE_TASKS = 4
_LOGGER = logging.getLogger("jarvis.codex")
_LOCK = threading.RLock()
_CHAT_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="JarvisCodexTask")
_DRAFTS: dict[str, tuple[float, str]] = {}
_TASKS: dict[str, dict] = {}
_TASK_STORE = TaskStore(Path(__file__).resolve().parent / "runtime" / "codex_tasks.json")
_STORE_AVAILABLE = True
_TASKS_LOADED = False
_ACTIVE_TURNS: dict[str, object] = {}
_TASK_FUTURES: dict[str, object] = {}
_CANCEL_EVENTS: dict[str, threading.Event] = {}
_CHAT_THREAD_ID: str | None = None
_CHAT_CLIENT = None
_CHAT_THREAD = None
_SDK_LAUNCH_LOCK = threading.Lock()


class _NoConsoleCodexSubprocess:
    """Keep the SDK's local app-server from opening a Windows console."""

    def __getattr__(self, name: str):
        return getattr(subprocess, name)

    def Popen(self, args, *pargs, **kwargs):
        if (
            os.name == "nt" and isinstance(args, (list, tuple)) and args
            and Path(str(args[0])).name.casefold() == "codex.exe"
        ):
            kwargs["creationflags"] = (
                kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            )
        return subprocess.Popen(args, *pargs, **kwargs)


def _configure_quiet_codex_launch() -> None:
    # This SDK version launches its app-server through client.subprocess.
    # Swap only that module reference; leave subprocess.Popen elsewhere alone.
    from openai_codex import client as sdk_client

    with _SDK_LAUNCH_LOCK:
        if sdk_client.subprocess is subprocess:
            sdk_client.subprocess = _NoConsoleCodexSubprocess()


class CodexBridgeUnavailable(RuntimeError):
    def __init__(self, message: str, *, reason: str = "service_error") -> None:
        super().__init__(message)
        # Fixed category only: callers must not log SDK exception text, prompts,
        # credentials, or the local account's configuration.
        self.reason = reason


class InvalidProject(ValueError):
    pass


def _root() -> Path:
    if is_redirect(PROJECT_ROOT):
        raise InvalidProject("项目根目录不能是链接或目录联接")
    return PROJECT_ROOT.resolve(strict=True)


def list_projects() -> list[str]:
    try:
        root = _root()
    except (OSError, InvalidProject):
        return []
    projects: list[str] = []
    for child in root.iterdir():
        if not child.is_dir() or is_redirect(child):
            continue
        try:
            if child.resolve(strict=True).parent == root:
                projects.append(child.name)
        except OSError:
            continue
    return sorted(projects, key=str.casefold)


def resolve_project(name: str) -> Path:
    if not isinstance(name, str) or name not in list_projects():
        raise InvalidProject("请选择 D:\\LYY_work 中的有效项目")
    root = _root()
    candidate = root / name
    if is_redirect(candidate):
        raise InvalidProject("项目不能是链接或目录联接")
    project = candidate.resolve(strict=True)
    if project.parent != root:
        raise InvalidProject("项目目录超出允许范围")
    return project


def is_project_task(text: str) -> bool:
    lowered = text.casefold()
    if "codex" in lowered or "交给代码助手" in text:
        return True
    return "项目" in text and any(
        verb in text for verb in ("修复", "修改", "开发", "实现", "优化", "添加", "增加", "编写", "测试", "重构")
    )


def create_draft(text: str) -> str:
    prompt = text.strip()
    if not prompt or len(prompt) > _MAX_INPUT_CHARS:
        raise ValueError("任务描述不能为空且不得超过 2000 字")
    with _LOCK:
        now = time.monotonic()
        for token, (expires, _) in list(_DRAFTS.items()):
            if expires <= now:
                del _DRAFTS[token]
        token = secrets.token_urlsafe(24)
        _DRAFTS[token] = (now + _DRAFT_LIFETIME_SECONDS, prompt)
    return token


def _codex_binary() -> str:
    """Find the installed Codex even when Explorer launches us with a small PATH."""
    found = shutil.which("codex")
    if found and Path(found).is_file():
        return found
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        install_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        try:
            candidates = [
                child / "codex.exe" for child in install_root.iterdir()
                if child.is_dir() and not child.is_symlink() and (child / "codex.exe").is_file()
            ]
        except OSError:
            candidates = []
        if candidates:
            return str(max(candidates, key=lambda path: path.stat().st_mtime))
    raise CodexBridgeUnavailable("本机未找到 Codex 程序", reason="binary_missing")


def _codex_config():
    try:
        from openai_codex import CodexConfig
    except ImportError as error:
        raise CodexBridgeUnavailable(
            "本机 Codex 接口尚未安装", reason="sdk_missing"
        ) from error
    return CodexConfig(codex_bin=_codex_binary(), cwd=str(_root()))


def chat(text: str) -> str:
    """Read-only conversational thread using the already signed-in local Codex."""
    prompt = text.strip()
    if not prompt or len(prompt) > _MAX_INPUT_CHARS:
        raise ValueError("聊天内容不能为空且不得超过 2000 字")
    global _CHAT_THREAD_ID, _CHAT_CLIENT, _CHAT_THREAD
    try:
        from openai_codex import ApprovalMode, Codex, Sandbox
        with _CHAT_LOCK:
            if _CHAT_CLIENT is None:
                _configure_quiet_codex_launch()
                client = Codex(_codex_config())
                try:
                    client.__enter__()
                except Exception:
                    client.close()
                    raise
                _CHAT_CLIENT = client
            if _CHAT_THREAD is None:
                _CHAT_THREAD = _CHAT_CLIENT.thread_start(
                    cwd=str(_root()), sandbox=Sandbox.read_only,
                    approval_mode=ApprovalMode.deny_all,
                    model="gpt-6-luna",
                    developer_instructions=(
                        "你是 Jarvis 的中文语音聊天助手。像沉着、机敏的助理一样自然地交谈。"
                        "通常只说一到两句，口语化，不用 Markdown，不重复用户问题，"
                        "避免模板化开场和冗长解释；复杂问题仍要准确。"
                        "当前会话仅用于聊天，不修改文件、不运行电脑操作、"
                        "不读取密钥或隐私文件。不要声称已执行任何动作。"
                    ),
                )
                _CHAT_THREAD_ID = _CHAT_THREAD.id
            result = _CHAT_THREAD.run(prompt)
            if getattr(result.status, "value", result.status) != "completed":
                _CHAT_THREAD_ID = None
                _CHAT_THREAD = None
                raise CodexBridgeUnavailable("聊天暂时未完成，请稍后再试")
        return (result.final_response or "我暂时没有生成回答，请再说一次。")[:_MAX_REPLY_CHARS]
    except (ValueError, InvalidProject, CodexBridgeUnavailable):
        raise
    except Exception as error:
        _LOGGER.warning("chat_failed type=%s", type(error).__name__)
        close_chat()
        raise CodexBridgeUnavailable("聊天服务暂时不可用，请稍后重试") from error


def warm_chat() -> None:
    """Pay the first-turn model latency before the first mobile conversation."""
    try:
        chat("启动检查。只回复：就绪。")
    except CodexBridgeUnavailable as error:
        _LOGGER.warning("chat_warmup_failed type=%s", type(error).__name__)


def close_chat() -> None:
    """Release the warm SDK connection; the next request can start a fresh one."""
    global _CHAT_CLIENT, _CHAT_THREAD, _CHAT_THREAD_ID
    # A desktop voice-exit must not wait through a lengthy first-turn warmup.
    # If Python is shutting down, closing the transport pipe will stop the
    # child app-server even when this lock is held by the daemon warmup thread.
    if not _CHAT_LOCK.acquire(timeout=2.0):
        _LOGGER.warning("chat_close_busy")
        return
    try:
        client = _CHAT_CLIENT
        _CHAT_CLIENT = None
        _CHAT_THREAD = None
        _CHAT_THREAD_ID = None
        if client is not None:
            try:
                client.close()
            except Exception as error:
                _LOGGER.warning("chat_close_failed type=%s", type(error).__name__)
    finally:
        _CHAT_LOCK.release()


atexit.register(close_chat)


_STAGE_MESSAGES = {
    "queued": "任务已排队",
    "starting": "正在建立项目任务连接",
    "working": "Codex 正在处理；阶段依据实际工具事件更新",
    "editing": "Codex 正在修改项目文件",
    "executing": "Codex 正在运行项目命令",
    "testing": "Codex 正在运行测试",
    "cancel_requested": "已请求停止，等待 Codex 确认；已完成的修改不会自动撤销",
    "completed": "任务已完成；详细输出未持久化，请查看项目或 Codex 会话",
    "failed": "Codex 任务未完成，请检查登录、项目目录或权限",
    "cancelled": "任务已停止；已完成的修改不会自动撤销",
    "interrupted": "服务曾中断，任务不会自动重跑；请检查项目状态后重新描述并确认任务",
}


def _ensure_tasks_loaded() -> None:
    global _TASKS_LOADED, _STORE_AVAILABLE
    with _LOCK:
        if _TASKS_LOADED:
            return
        try:
            loaded = _TASK_STORE.load(recover=True)
        except (OSError, ValueError, TypeError):
            _STORE_AVAILABLE = False
            _TASKS_LOADED = True
            _LOGGER.warning("task_store_unavailable")
            return
        for task_id, record in loaded.items():
            record["message"] = _STAGE_MESSAGES[record["stage"]]
            _TASKS.setdefault(task_id, record)
        _TASKS_LOADED = True


def _persist_tasks() -> None:
    global _STORE_AVAILABLE
    if not _STORE_AVAILABLE:
        raise CodexBridgeUnavailable("任务记录不可用，已禁止启动新任务", reason="task_store_unavailable")
    try:
        _TASK_STORE.save(_TASKS)
    except (OSError, ValueError, TypeError) as error:
        _STORE_AVAILABLE = False
        _LOGGER.warning("task_store_write_failed type=%s", type(error).__name__)
        raise CodexBridgeUnavailable("任务记录无法保存，已停止任务启动", reason="task_store_unavailable") from error


def _update_task(task_id: str, *, persist: bool = True, **updates) -> None:
    with _LOCK:
        task = _TASKS[task_id]
        task.update(updates, updated_at=time.time())
        if "stage" in updates and "message" not in updates:
            task["message"] = _STAGE_MESSAGES[updates["stage"]]
        if persist:
            _persist_tasks()


def _field(value, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _enum_value(value):
    return getattr(value, "value", value)


def _consume_task_turn(task_id: str, turn) -> tuple[str, str]:
    """Read the installed SDK's real notifications; never persist their bodies."""
    seen_items = set()
    final_text = ""
    terminal = None
    stream = turn.stream()
    try:
        for event in stream:
            method = _field(event, "method", "")
            payload = _field(event, "payload", {})
            if method in {"item/started", "item/completed"}:
                item = _field(payload, "item", {})
                item = _field(item, "root", item)
                kind = _enum_value(_field(item, "type", ""))
                item_id = _field(item, "id", "")
                if kind == "agentMessage" and method == "item/completed":
                    phase = _enum_value(_field(item, "phase"))
                    if phase in {None, "final_answer"}:
                        text = _field(item, "text", "")
                        if isinstance(text, str):
                            final_text = text[:_MAX_REPLY_CHARS]
                if kind in {"fileChange", "commandExecution"} and item_id and item_id not in seen_items:
                    seen_items.add(item_id)
                    with _LOCK:
                        task = _TASKS[task_id]
                        cancelling = task["status"] == "cancel_requested"
                        if kind == "fileChange":
                            updates = {"change_count": task.get("change_count", 0) + 1, "stage": "editing"}
                        else:
                            # Command data is inspected in memory solely to label an
                            # actual test event; it is never stored or executed here.
                            import re
                            command = _field(item, "command", "")
                            testing = isinstance(command, str) and re.search(
                                r"\b(pytest|unittest|ctest|jest|vitest|cargo\s+test|go\s+test|(?:npm|pnpm|yarn)\s+(?:run\s+)?test)\b",
                                command, flags=re.IGNORECASE,
                            )
                            updates = {"command_count": task.get("command_count", 0) + 1, "stage": "testing" if testing else "executing"}
                        if cancelling:
                            updates.pop("stage")
                        _update_task(task_id, **updates)
            elif method == "turn/completed":
                completed = _field(payload, "turn", {})
                if _field(completed, "id", "") == turn.id:
                    terminal = _enum_value(_field(completed, "status", ""))
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    if terminal is None:
        raise CodexBridgeUnavailable("Codex 未返回任务终态")
    return terminal, final_text


def _run_task(task_id: str, project_name: str, prompt: str) -> None:
    _ensure_tasks_loaded()
    try:
        with _LOCK:
            cancel = _CANCEL_EVENTS.setdefault(task_id, threading.Event())
            if cancel.is_set() or _TASKS[task_id]["status"] == "cancelled":
                _update_task(task_id, status="cancelled", stage="cancelled")
                return
            _update_task(task_id, status="running", stage="starting")
        from openai_codex import ApprovalMode, Codex, Sandbox
        project = resolve_project(project_name)
        _configure_quiet_codex_launch()
        with Codex(_codex_config()) as codex:
            thread = codex.thread_start(
                cwd=str(project), sandbox=Sandbox.workspace_write,
                approval_mode=ApprovalMode.deny_all,
                developer_instructions=(
                    "只在当前项目内完成用户确认的开发任务。不要读取密钥或隐私文件；"
                    "不要删除项目或向远端推送。先检查，再修改并运行相关测试。"
                    "需要超出工作区的权限时停止并说明，不要绕过限制。"
                ),
            )
            with _LOCK:
                if cancel.is_set():
                    _update_task(task_id, status="cancelled", stage="cancelled")
                    return
                _update_task(task_id, thread_id=thread.id)
            turn = thread.turn(prompt)
            with _LOCK:
                _ACTIVE_TURNS[task_id] = turn
                _update_task(task_id, turn_id=turn.id, stage="cancel_requested" if cancel.is_set() else "working")
            if cancel.is_set():
                turn.interrupt()
            terminal, final_text = _consume_task_turn(task_id, turn)
            if terminal in {"interrupted", "cancelled"}:
                _update_task(task_id, status="cancelled", stage="cancelled")
                return
            if terminal != "completed":
                raise CodexBridgeUnavailable("Codex 任务未完成")
        with _LOCK:
            _update_task(task_id, status="completed", stage="completed", message=(
                final_text or "任务已结束，请在 Codex 中检查结果。"
            ))
    except Exception as error:
        _LOGGER.warning("task_failed task_id=%s type=%s", task_id, type(error).__name__)
        with _LOCK:
            _update_task(task_id, persist=False, status="failed", stage="failed")
            try:
                _persist_tasks()
            except CodexBridgeUnavailable:
                pass
    finally:
        with _LOCK:
            _ACTIVE_TURNS.pop(task_id, None)
            _TASK_FUTURES.pop(task_id, None)
            _CANCEL_EVENTS.pop(task_id, None)


def confirm_draft(token: str, project_name: str) -> str:
    _ensure_tasks_loaded()
    project = resolve_project(project_name)
    with _LOCK:
        if not _STORE_AVAILABLE:
            raise CodexBridgeUnavailable("任务记录不可用，暂不能启动任务", reason="task_store_unavailable")
        if sum(task["status"] not in TERMINAL_STATES for task in _TASKS.values()) >= _MAX_ACTIVE_TASKS:
            raise ValueError("任务队列已满，请等待或取消现有任务")
        draft = _DRAFTS.pop(token, None)
        if draft is None or draft[0] <= time.monotonic():
            raise ValueError("任务确认已过期，请重新说出任务")
        task_id = secrets.token_urlsafe(18)
        # Retain bounded detailed in-memory summaries as well as bounded disk
        # metadata. Older completed history is deliberately not an archive.
        finished = sorted(
            (key for key, value in _TASKS.items() if value["status"] in TERMINAL_STATES),
            key=lambda key: _TASKS[key].get("created_at", 0),
        )
        while len(_TASKS) >= _TASK_STORE.limit and finished:
            _TASKS.pop(finished.pop(0), None)
        _TASKS[task_id] = {
            "status": "queued", "project": project.name,
            "message": "任务已排队", "thread_id": "",
            "turn_id": "", "stage": "queued", "created_at": time.time(),
            "updated_at": time.time(), "cancel_requested": False,
            "change_count": 0, "command_count": 0,
        }
        try:
            _persist_tasks()
        except CodexBridgeUnavailable:
            _TASKS.pop(task_id, None)
            raise
        _CANCEL_EVENTS[task_id] = threading.Event()
        # Submit while holding the lock so the worker cannot race registration.
        try:
            _TASK_FUTURES[task_id] = _EXECUTOR.submit(_run_task, task_id, project.name, draft[1])
        except Exception:
            _update_task(task_id, status="failed", stage="failed")
            raise CodexBridgeUnavailable("无法将任务加入队列", reason="queue_unavailable") from None
    return task_id


def task_status(task_id: str) -> dict:
    _ensure_tasks_loaded()
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return dict(task)


def list_tasks(limit: int = 50) -> list[dict]:
    _ensure_tasks_loaded()
    if not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("任务列表数量必须在 1 到 200 之间")
    with _LOCK:
        return [dict(task, task_id=task_id) for task_id, task in sorted(
            _TASKS.items(), key=lambda pair: pair[1].get("created_at", 0), reverse=True,
        )[:limit]]


def cancel_task(task_id: str) -> dict:
    """Request a real SDK interrupt; never promise cancellation or rollback early."""
    _ensure_tasks_loaded()
    with _LOCK:
        if task_id not in _TASKS:
            raise KeyError(task_id)
        task = _TASKS[task_id]
        if task["status"] in TERMINAL_STATES:
            return dict(task)
        _CANCEL_EVENTS.setdefault(task_id, threading.Event()).set()
        turn = _ACTIVE_TURNS.get(task_id)
        future = _TASK_FUTURES.get(task_id)
        queued_cancelled = task["status"] == "queued" and (future is None or future.cancel())
        if queued_cancelled:
            _update_task(task_id, persist=False, status="cancelled", stage="cancelled", cancel_requested=True)
            _TASK_FUTURES.pop(task_id, None)
            _CANCEL_EVENTS.pop(task_id, None)
        else:
            _update_task(task_id, persist=False, status="cancel_requested", stage="cancel_requested", cancel_requested=True)
        try:
            _persist_tasks()
        except CodexBridgeUnavailable:
            # Stopping an active operation must still be attempted even if the
            # metadata drive is full; no new tasks may start in this condition.
            pass
    if turn is not None:
        try:
            turn.interrupt()
        except Exception as error:
            _LOGGER.warning("task_interrupt_failed task_id=%s type=%s", task_id, type(error).__name__)
            with _LOCK:
                if _TASKS[task_id]["status"] == "cancel_requested":
                    _TASKS[task_id]["message"] = "停止请求未获确认，任务可能仍在运行；请检查 Codex，已完成的修改不会自动撤销"
    return task_status(task_id)
