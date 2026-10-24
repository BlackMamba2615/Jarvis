"""Atomic task metadata journal. Prompts and generated responses never go on disk."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import threading
import time


ACTIVE_STATES = {"queued", "running", "cancel_requested"}
TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}
STATES = ACTIVE_STATES | TERMINAL_STATES
STAGES = {
    "queued", "starting", "working", "editing", "executing", "testing",
    "cancel_requested", "completed", "failed", "cancelled", "interrupted",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_FIELDS = {
    "status", "stage", "project", "created_at", "updated_at", "thread_id",
    "turn_id", "cancel_requested", "change_count", "command_count",
}


def is_redirect(path: Path) -> bool:
    """Reject symlinks and Windows junction/reparse entries, not just symlinks."""
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_record(record: dict) -> dict:
    # A strict positive list prevents accidental persistence of speech/output,
    # SDK exception bodies, credentials, or newly added arbitrary fields.
    clean = {key: value for key, value in record.items() if key in _FIELDS}
    if clean.get("status") not in STATES or clean.get("stage") not in STAGES:
        raise ValueError("invalid task state")
    project = clean.get("project", "")
    if not isinstance(project, str) or not project or len(project) > 255 or any(
        char in project for char in ("/", "\\", "\x00", "\n", "\r")
    ) or project in {".", ".."}:
        raise ValueError("invalid task project")
    for key in ("thread_id", "turn_id"):
        value = clean.get(key, "")
        if not isinstance(value, str) or (value and not _SAFE_ID.fullmatch(value)):
            raise ValueError("invalid task identifier")
        clean[key] = value
    for key in ("created_at", "updated_at"):
        value = clean.get(key, 0)
        if not isinstance(value, (int, float)) or not 0 <= value < 10**12:
            raise ValueError("invalid task timestamp")
        clean[key] = value
    clean["cancel_requested"] = clean.get("cancel_requested") is True
    for key in ("change_count", "command_count"):
        value = clean.get(key, 0)
        if not isinstance(value, int) or not 0 <= value <= 1_000_000:
            raise ValueError("invalid task counter")
        clean[key] = value
    return clean


class TaskStore:
    def __init__(self, path: Path, *, limit: int = 200):
        self.path = Path(path)
        self.limit = limit
        self._lock = threading.RLock()

    def _check_location(self) -> None:
        if is_redirect(self.path) or any(is_redirect(parent) for parent in self.path.parents):
            raise ValueError("task journal cannot use redirected paths")

    def load(self, *, recover: bool = True) -> dict[str, dict]:
        with self._lock:
            self._check_location()
            if not self.path.exists():
                return {}
            if self.path.stat().st_size > 2_000_000:
                raise ValueError("task journal too large")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("unsupported task journal")
            records = payload.get("tasks")
            if not isinstance(records, dict) or len(records) > self.limit:
                raise ValueError("invalid task journal")
            result = {}
            changed = False
            for task_id, record in records.items():
                if not isinstance(task_id, str) or not _SAFE_ID.fullmatch(task_id) or not isinstance(record, dict):
                    raise ValueError("invalid task journal")
                clean = _safe_record(record)
                if recover and clean["status"] in ACTIVE_STATES:
                    clean.update(status="interrupted", stage="interrupted", updated_at=time.time())
                    changed = True
                result[task_id] = clean
            if changed:
                self.save(result)
            return result

    def save(self, tasks: dict[str, dict]) -> None:
        with self._lock:
            self._check_location()
            # Never drop an active task just to keep the history bounded.
            active = [(key, value) for key, value in tasks.items() if value.get("status") in ACTIVE_STATES]
            finished = sorted(
                ((key, value) for key, value in tasks.items() if value.get("status") not in ACTIVE_STATES),
                key=lambda pair: pair[1].get("created_at", 0), reverse=True,
            )
            selected = active + finished[:max(0, self.limit - len(active))]
            if len(selected) > self.limit:
                raise ValueError("too many active tasks")
            records = {}
            for task_id, record in selected:
                if not isinstance(task_id, str) or not _SAFE_ID.fullmatch(task_id):
                    raise ValueError("invalid task identifier")
                records[task_id] = _safe_record(record)
            body = json.dumps({"version": 1, "tasks": records}, ensure_ascii=False, allow_nan=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._check_location()
            temporary = self.path.with_name(self.path.name + "." + str(os.getpid()) + ".tmp")
            # Exclusive creation avoids following a pre-existing temporary link.
            created = False
            try:
                with temporary.open("x", encoding="utf-8") as output:
                    created = True
                    output.write(body)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                # Only remove the temporary file this call actually created.
                if created and temporary.exists() and not is_redirect(temporary):
                    temporary.unlink()
