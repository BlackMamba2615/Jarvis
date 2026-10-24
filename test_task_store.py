import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from task_store import TaskStore


def task(status="completed", created_at=1):
    return {
        "status": status, "stage": status, "project": "example",
        "created_at": created_at, "updated_at": created_at,
        "thread_id": "thread-1", "turn_id": "turn-1",
        "cancel_requested": False, "change_count": 0, "command_count": 0,
    }


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime" / "tasks.json"
        self.store = TaskStore(self.path)

    def test_empty_store_does_not_create_file(self):
        self.assertEqual(self.store.load(), {})
        self.assertFalse(self.path.exists())

    def test_only_positive_list_metadata_is_saved(self):
        record = task()
        record.update(prompt="SECRET-PROMPT", message="SECRET-OUTPUT", api_key="SECRET-KEY", arbitrary="SECRET")
        self.store.save({"task-1": record})
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET", raw)
        self.assertEqual(self.store.load()["task-1"]["project"], "example")

    def test_restart_recovers_all_nonterminal_states_without_reexecution(self):
        tasks = {"task-" + state: task(state) for state in ("queued", "running", "cancel_requested")}
        for record in tasks.values():
            record["stage"] = "working"
        self.store.save(tasks)
        restored = self.store.load()
        self.assertEqual({value["status"] for value in restored.values()}, {"interrupted"})
        self.assertEqual({value["stage"] for value in restored.values()}, {"interrupted"})
        self.assertEqual({value["status"] for value in json.loads(self.path.read_text(encoding="utf-8"))["tasks"].values()}, {"interrupted"})

    def test_terminal_states_survive_restart(self):
        self.store.save({"task-1": task("completed")})
        self.assertEqual(self.store.load()["task-1"]["status"], "completed")

    def test_retention_keeps_active_and_newest_finished(self):
        self.store.limit = 2
        self.store.save({"task-old": task(created_at=1), "task-new": task(created_at=2), "task-active": task("queued", 0)})
        self.assertEqual(set(self.store.load(recover=False)), {"task-new", "task-active"})

    def test_invalid_metadata_is_rejected(self):
        for key, value in (("project", "../outside"), ("status", "anything"), ("thread_id", "private\ntext"), ("command_count", -1), ("created_at", float("nan"))):
            record = task()
            record[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.save({"task-1": record})
        self.assertFalse(self.path.exists())

    def test_invalid_json_is_not_silently_overwritten(self):
        self.path.parent.mkdir()
        self.path.write_text("not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.load()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "not json")

    def test_reparse_path_is_rejected(self):
        with mock.patch("task_store.is_redirect", return_value=True):
            with self.assertRaises(ValueError):
                self.store.load()
            with self.assertRaises(ValueError):
                self.store.save({"task-1": task()})

    def test_replace_failure_keeps_previous_file_and_cleans_own_temporary(self):
        self.store.save({"task-first": task()})
        with mock.patch("task_store.os.replace", side_effect=OSError("disk problem")):
            with self.assertRaises(OSError):
                self.store.save({"task-second": task()})
        self.assertEqual(set(self.store.load()), {"task-first"})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
