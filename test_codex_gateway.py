import tempfile
import subprocess
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import codex_gateway
from task_store import TaskStore


class CodexGatewaySafetyTests(unittest.TestCase):
    def setUp(self):
        codex_gateway.close_chat()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "example"
        self.project.mkdir()
        self.root_patch = mock.patch.object(codex_gateway, "PROJECT_ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(codex_gateway.close_chat)
        self.store_patch = mock.patch.object(codex_gateway, "_TASK_STORE", TaskStore(self.root / "tasks.json"))
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.available_patch = mock.patch.object(codex_gateway, "_STORE_AVAILABLE", True)
        self.available_patch.start()
        self.addCleanup(self.available_patch.stop)
        self.loaded_patch = mock.patch.object(codex_gateway, "_TASKS_LOADED", True)
        self.loaded_patch.start()
        self.addCleanup(self.loaded_patch.stop)
        with codex_gateway._LOCK:
            codex_gateway._DRAFTS.clear()
            codex_gateway._TASKS.clear()
            codex_gateway._ACTIVE_TURNS.clear()
            codex_gateway._TASK_FUTURES.clear()
            codex_gateway._CANCEL_EVENTS.clear()

    def queue_task(self, prompt="请修复项目中的测试"):
        token = codex_gateway.create_draft(prompt)
        with mock.patch.object(codex_gateway._EXECUTOR, "submit"):
            return codex_gateway.confirm_draft(token, "example")

    def mock_task_client(self, events):
        turn = mock.Mock(id="turn-1")
        turn.stream.return_value = iter(events)
        thread = mock.Mock(id="thread-1")
        thread.turn.return_value = turn
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.thread_start.return_value = thread
        return client, thread, turn

    def test_only_immediate_child_projects_are_listed(self):
        (self.project / "nested").mkdir()
        self.assertEqual(codex_gateway.list_projects(), ["example"])
        with self.assertRaises(codex_gateway.InvalidProject):
            codex_gateway.resolve_project("../outside")
        with self.assertRaises(codex_gateway.InvalidProject):
            codex_gateway.resolve_project("nested")

    def test_project_task_requires_coding_context(self):
        self.assertTrue(codex_gateway.is_project_task("让 Codex 修复页面问题"))
        self.assertTrue(codex_gateway.is_project_task("优化项目的测试"))
        self.assertFalse(codex_gateway.is_project_task("打开微信"))
        self.assertFalse(codex_gateway.is_project_task("聊聊项目管理"))

    def test_draft_requires_separate_confirmation_and_is_one_use(self):
        token = codex_gateway.create_draft("请修复项目中的测试")
        with mock.patch.object(codex_gateway._EXECUTOR, "submit") as submit:
            task_id = codex_gateway.confirm_draft(token, "example")
        submit.assert_called_once()
        self.assertEqual(codex_gateway.task_status(task_id)["status"], "queued")
        with self.assertRaises(ValueError):
            codex_gateway.confirm_draft(token, "example")

    def test_invalid_project_does_not_consume_draft(self):
        token = codex_gateway.create_draft("修复测试")
        with self.assertRaises(codex_gateway.InvalidProject):
            codex_gateway.confirm_draft(token, "outside")
        self.assertIn(token, codex_gateway._DRAFTS)

    def test_expired_draft_is_rejected(self):
        token = codex_gateway.create_draft("修复测试")
        with codex_gateway._LOCK:
            codex_gateway._DRAFTS[token] = (0.0, "修复测试")
        with self.assertRaises(ValueError):
            codex_gateway.confirm_draft(token, "example")

    def test_chat_reuses_one_connection_and_thread(self):
        thread = mock.Mock(id="test-chat")
        thread.run.return_value = SimpleNamespace(
            status="completed", final_response="您好。"
        )
        client = mock.MagicMock()
        client.thread_start.return_value = thread
        with mock.patch("openai_codex.Codex", return_value=client) as codex, mock.patch.object(
            codex_gateway, "_codex_config", return_value=mock.Mock()
        ):
            self.assertEqual(codex_gateway.chat("你好"), "您好。")
            self.assertEqual(codex_gateway.chat("再见"), "您好。")
            codex.assert_called_once()
            client.thread_start.assert_called_once()
            self.assertEqual(thread.run.call_count, 2)
            self.assertEqual(client.thread_start.call_args.kwargs["model"], "gpt-6-luna")

    def test_codex_binary_is_found_when_desktop_path_omits_it(self):
        binary = self.root / "OpenAI" / "Codex" / "bin" / "installed" / "codex.exe"
        binary.parent.mkdir(parents=True)
        binary.touch()
        with mock.patch.object(codex_gateway.shutil, "which", return_value=None), mock.patch.dict(
            codex_gateway.os.environ, {"LOCALAPPDATA": str(self.root)}
        ):
            self.assertEqual(codex_gateway._codex_binary(), str(binary))

    def test_codex_binary_failure_has_safe_reason(self):
        with mock.patch.object(codex_gateway.shutil, "which", return_value=None), mock.patch.dict(
            codex_gateway.os.environ, {"LOCALAPPDATA": str(self.root)}
        ):
            with self.assertRaises(codex_gateway.CodexBridgeUnavailable) as caught:
                codex_gateway._codex_binary()
        self.assertEqual(caught.exception.reason, "binary_missing")

    def test_codex_app_server_launches_without_console_on_windows(self):
        proxy = codex_gateway._NoConsoleCodexSubprocess()
        with mock.patch.object(codex_gateway.os, "name", "nt"), mock.patch.object(
            subprocess, "Popen", return_value=object()
        ) as launch:
            proxy.Popen([r"C:\Program Files\Codex\codex.exe", "app-server"])
            self.assertTrue(launch.call_args.kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)
            proxy.Popen([r"C:\Windows\notepad.exe"])
            self.assertNotIn("creationflags", launch.call_args.kwargs)

    def test_task_journal_does_not_contain_prompt_or_response(self):
        task_id = self.queue_task("PRIVATE-SPOKEN-PROMPT")
        with codex_gateway._LOCK:
            codex_gateway._update_task(task_id, status="completed", stage="completed", message="PRIVATE-RESPONSE")
        raw = (self.root / "tasks.json").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE-SPOKEN-PROMPT", raw)
        self.assertNotIn("PRIVATE-RESPONSE", raw)
        self.assertNotIn('"message"', raw)
        self.assertEqual(json.loads(raw)["tasks"][task_id]["status"], "completed")

    def test_restart_marks_pending_task_interrupted_without_resubmitting(self):
        task_id = self.queue_task()
        with codex_gateway._LOCK:
            codex_gateway._TASKS.clear()
            codex_gateway._TASKS_LOADED = False
        with mock.patch.object(codex_gateway._EXECUTOR, "submit") as submit:
            status = codex_gateway.task_status(task_id)
        self.assertEqual(status["status"], "interrupted")
        self.assertIn("不会自动重跑", status["message"])
        submit.assert_not_called()

    def test_task_listing_is_newest_first_and_bounded(self):
        first = self.queue_task()
        second = self.queue_task()
        result = codex_gateway.list_tasks(limit=1)
        self.assertEqual(result[0]["task_id"], second)
        self.assertNotEqual(result[0]["task_id"], first)
        with self.assertRaises(ValueError):
            codex_gateway.list_tasks(0)

    def test_queued_task_can_cancel_before_it_executes(self):
        task_id = self.queue_task()
        codex_gateway._TASK_FUTURES[task_id].cancel.return_value = True
        self.assertEqual(codex_gateway.cancel_task(task_id)["status"], "cancelled")
        with mock.patch("openai_codex.Codex") as launch:
            codex_gateway._run_task(task_id, "example", "private prompt")
        launch.assert_not_called()

    def test_running_cancel_requests_sdk_interrupt_but_waits_for_confirmation(self):
        task_id = self.queue_task()
        codex_gateway._update_task(task_id, status="running", stage="working")
        turn = mock.Mock()
        codex_gateway._ACTIVE_TURNS[task_id] = turn
        status = codex_gateway.cancel_task(task_id)
        turn.interrupt.assert_called_once_with()
        self.assertEqual(status["status"], "cancel_requested")
        self.assertIn("不会自动撤销", status["message"])

    def test_failed_interrupt_never_claims_task_cancelled(self):
        task_id = self.queue_task()
        codex_gateway._update_task(task_id, status="running", stage="working")
        turn = mock.Mock()
        turn.interrupt.side_effect = RuntimeError("PRIVATE-ERROR")
        codex_gateway._ACTIVE_TURNS[task_id] = turn
        with self.assertLogs("jarvis.codex", level="WARNING") as logs:
            result = codex_gateway.cancel_task(task_id)
        self.assertEqual(result["status"], "cancel_requested")
        self.assertIn("可能仍在运行", result["message"])
        self.assertNotIn("PRIVATE-ERROR", str(logs.output))

    def test_completed_task_cancel_is_noop(self):
        task_id = self.queue_task()
        codex_gateway._update_task(task_id, status="completed", stage="completed")
        self.assertEqual(codex_gateway.cancel_task(task_id)["status"], "completed")

    def test_real_sdk_event_shape_drives_progress_and_completion(self):
        task_id = self.queue_task()
        events = [
            SimpleNamespace(method="item/started", payload=SimpleNamespace(item=SimpleNamespace(root=SimpleNamespace(type="fileChange", id="edit-1")))),
            SimpleNamespace(method="item/completed", payload=SimpleNamespace(item=SimpleNamespace(root=SimpleNamespace(type="fileChange", id="edit-1")))),
            SimpleNamespace(method="item/started", payload=SimpleNamespace(item=SimpleNamespace(root=SimpleNamespace(type="commandExecution", id="cmd-1", command="python -m pytest")))),
            SimpleNamespace(method="item/completed", payload=SimpleNamespace(item=SimpleNamespace(root=SimpleNamespace(type="agentMessage", id="msg-1", phase="final_answer", text="PRIVATE-SDK-OUTPUT")))),
            SimpleNamespace(method="turn/completed", payload=SimpleNamespace(turn=SimpleNamespace(id="turn-1", status="completed"))),
        ]
        client, thread, turn = self.mock_task_client(events)
        with mock.patch("openai_codex.Codex", return_value=client), mock.patch.object(codex_gateway, "_codex_config", return_value=mock.Mock()):
            codex_gateway._run_task(task_id, "example", "PRIVATE-PROMPT")
        status = codex_gateway.task_status(task_id)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["message"], "PRIVATE-SDK-OUTPUT")
        self.assertEqual(status["change_count"], 1)
        self.assertEqual(status["command_count"], 1)
        self.assertEqual(status["thread_id"], "thread-1")
        self.assertEqual(status["turn_id"], "turn-1")
        self.assertNotIn(task_id, codex_gateway._ACTIVE_TURNS)
        thread.run.assert_not_called()
        client.__exit__.assert_called_once()
        from openai_codex import Sandbox, ApprovalMode
        self.assertEqual(client.thread_start.call_args.kwargs["sandbox"], Sandbox.workspace_write)
        self.assertEqual(client.thread_start.call_args.kwargs["approval_mode"], ApprovalMode.deny_all)
        raw = (self.root / "tasks.json").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE-PROMPT", raw)
        self.assertNotIn("PRIVATE-SDK-OUTPUT", raw)
        self.assertNotIn("pytest", raw)

    def test_cancellation_is_terminal_only_after_sdk_interrupted_event(self):
        task_id = self.queue_task()
        client, thread, turn = self.mock_task_client([])
        def event_stream():
            self.assertEqual(codex_gateway.cancel_task(task_id)["status"], "cancel_requested")
            yield SimpleNamespace(method="turn/completed", payload=SimpleNamespace(turn=SimpleNamespace(id="turn-1", status="interrupted")))
        turn.stream.side_effect = event_stream
        with mock.patch("openai_codex.Codex", return_value=client), mock.patch.object(codex_gateway, "_codex_config", return_value=mock.Mock()):
            codex_gateway._run_task(task_id, "example", "PRIVATE-PROMPT")
        self.assertEqual(codex_gateway.task_status(task_id)["status"], "cancelled")
        turn.interrupt.assert_called_once_with()

    def test_task_without_completed_event_is_failed_not_completed(self):
        task_id = self.queue_task()
        client, thread, turn = self.mock_task_client([])
        with mock.patch("openai_codex.Codex", return_value=client), mock.patch.object(codex_gateway, "_codex_config", return_value=mock.Mock()), self.assertLogs("jarvis.codex", level="WARNING"):
            codex_gateway._run_task(task_id, "example", "PRIVATE-PROMPT")
        self.assertEqual(codex_gateway.task_status(task_id)["status"], "failed")

    def test_storage_failure_blocks_task_launch(self):
        token = codex_gateway.create_draft("PRIVATE-PROMPT")
        with mock.patch.object(codex_gateway._TASK_STORE, "save", side_effect=OSError("PRIVATE-ERROR")), mock.patch.object(codex_gateway._EXECUTOR, "submit") as submit, self.assertLogs("jarvis.codex", level="WARNING") as logs:
            with self.assertRaises(codex_gateway.CodexBridgeUnavailable):
                codex_gateway.confirm_draft(token, "example")
        submit.assert_not_called()
        self.assertFalse(codex_gateway._STORE_AVAILABLE)
        self.assertNotIn("PRIVATE-ERROR", str(logs.output))

    def test_full_queue_rejects_without_consuming_confirmation(self):
        for _ in range(codex_gateway._MAX_ACTIVE_TASKS):
            self.queue_task()
        token = codex_gateway.create_draft("private prompt")
        with mock.patch.object(codex_gateway._EXECUTOR, "submit") as submit:
            with self.assertRaisesRegex(ValueError, "队列已满"):
                codex_gateway.confirm_draft(token, "example")
        self.assertIn(token, codex_gateway._DRAFTS)
        submit.assert_not_called()

    def test_history_retention_bounds_in_memory_responses(self):
        codex_gateway._TASK_STORE.limit = 2
        oldest = self.queue_task()
        codex_gateway._update_task(oldest, status="completed", stage="completed")
        second = self.queue_task()
        codex_gateway._update_task(second, status="completed", stage="completed")
        newest = self.queue_task()
        self.assertEqual(set(codex_gateway._TASKS), {second, newest})

    def test_reparse_project_and_root_are_rejected(self):
        with mock.patch.object(codex_gateway, "is_redirect", side_effect=lambda path: path == self.project):
            self.assertEqual(codex_gateway.list_projects(), [])
            with self.assertRaises(codex_gateway.InvalidProject):
                codex_gateway.resolve_project("example")
        with mock.patch.object(codex_gateway, "is_redirect", return_value=True):
            self.assertEqual(codex_gateway.list_projects(), [])


if __name__ == "__main__":
    unittest.main()
