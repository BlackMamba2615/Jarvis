import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import chat_sessions


class ChatSessionsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "memory.sqlite3"
        self.patch = mock.patch.object(chat_sessions, "MEMORY_PATH", self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        chat_sessions._SESSIONS.clear()

    def test_history_is_bounded_isolated_and_not_persisted(self):
        for number in range(10):
            chat_sessions.append_turn("mobile", f"question {number}", "answer")
        self.assertEqual(len(chat_sessions.get_history("mobile")), 8)
        self.assertEqual(chat_sessions.get_history("desktop"), [])
        chat_sessions.context_messages("mobile")
        self.assertFalse(self.path.exists())
        chat_sessions.clear_history("mobile")
        self.assertEqual(chat_sessions.get_history("mobile"), [])

    def test_explicit_memory_persists_and_has_session_ownership(self):
        memory = chat_sessions.remember("mobile", "喜欢简短回答")
        self.assertTrue(self.path.is_file())
        self.assertEqual(chat_sessions.list_memories("mobile"), [memory])
        self.assertEqual(chat_sessions.list_memories("desktop"), [])
        self.assertFalse(chat_sessions.delete_memory("desktop", memory["id"]))
        self.assertTrue(chat_sessions.delete_memory("mobile", memory["id"]))
        self.assertEqual(chat_sessions.list_memories("mobile"), [])

    def test_clear_memory_does_not_clear_another_session(self):
        chat_sessions.remember("mobile", "称呼先生")
        chat_sessions.remember("desktop", "说中文")
        chat_sessions.clear_memories("mobile")
        self.assertEqual(chat_sessions.list_memories("mobile"), [])
        self.assertEqual(len(chat_sessions.list_memories("desktop")), 1)

    def test_secret_and_invalid_content_are_rejected(self):
        for value in ("", "x" * 501, "密码：not-real", "API_KEY=test-value", "sk-unit-test-not-a-real-key"):
            with self.assertRaises(ValueError):
                chat_sessions.remember("mobile", value)
        self.assertFalse(self.path.exists())
        for session in ("../other", "", "a" * 65):
            with self.assertRaises(ValueError):
                chat_sessions.get_history(session)

    def test_cancelled_lock_does_not_leave_session_locked(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(chat_sessions.ChatCancelled):
            with chat_sessions.SessionTurn("mobile", event):
                pass
        with chat_sessions.SessionTurn("mobile"):
            chat_sessions.append_turn("mobile", "你好", "您好")
        self.assertEqual(chat_sessions._session("mobile")["busy"], 0)


if __name__ == "__main__":
    unittest.main()
