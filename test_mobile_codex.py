import importlib
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient


class MobileCodexRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.dict(os.environ, {"JARVIS_API_KEY": "test-only-key"}), mock.patch(
            "dotenv.load_dotenv", return_value=False
        ), mock.patch("faster_whisper.WhisperModel"):
            cls.main = importlib.import_module("main")
        cls.client = TestClient(cls.main.app)

    def test_existing_skill_does_not_invoke_chat(self):
        with mock.patch.object(self.main, "execute_command", return_value={"recognized": True, "success": True}) as skill, mock.patch.object(self.main, "assistant_chat") as chat:
            result = self.main.handle_mobile_voice("打开记事本")
        self.assertTrue(result["recognized"])
        skill.assert_called_once_with("打开记事本")
        chat.assert_not_called()

    def test_unknown_speech_uses_read_only_chat(self):
        with mock.patch.object(self.main, "assistant_chat", return_value="你好，我在。"):
            result = self.main.handle_mobile_voice("今天天气怎么样")
        self.assertEqual(result["message"], "你好，我在。")
        self.assertEqual(result["actions"], [])

    def test_coding_task_only_creates_unconfirmed_draft(self):
        with mock.patch.object(self.main, "list_codex_projects", return_value=["example"]), mock.patch.object(
            self.main, "create_codex_draft", return_value="test-token"
        ) as draft, mock.patch.object(self.main, "assistant_chat") as chat:
            result = self.main.handle_mobile_voice("让 Codex 优化项目测试")
        self.assertEqual(result["codex_draft_token"], "test-token")
        self.assertFalse(result["success"])
        draft.assert_called_once()
        chat.assert_not_called()

    def test_codex_endpoints_require_phone_key(self):
        self.assertEqual(self.client.get("/codex/projects").status_code, 401)
        self.assertEqual(self.client.post("/ai/chat", json={"text": "你好"}).status_code, 401)
        self.assertEqual(self.client.post("/speech/dynamic", json={"text": "你好"}).status_code, 401)
        self.assertEqual(
            self.client.post("/codex/confirm", json={"token": "x", "project": "example"}).status_code,
            401,
        )

    def test_phone_confirms_task_and_reads_status(self):
        headers = {"X-Jarvis-Key": "test-only-key"}
        with mock.patch.object(self.main, "list_codex_projects", return_value=["example"]):
            self.assertEqual(
                self.client.get("/codex/projects", headers=headers).json()["projects"],
                ["example"],
            )
        with mock.patch.object(self.main, "confirm_codex_draft", return_value="task-1") as confirm:
            response = self.client.post(
                "/codex/confirm", json={"token": "draft-1", "project": "example"},
                headers=headers,
            )
        self.assertEqual(response.json()["task_id"], "task-1")
        confirm.assert_called_once_with("draft-1", "example")
        with mock.patch.object(self.main, "codex_task_status", return_value={"status": "completed", "message": "完成"}):
            response = self.client.get("/codex/tasks/task-1", headers=headers)
        self.assertEqual(response.json()["status"], "completed")

    def test_direct_chat_endpoint_is_read_only_backend(self):
        with mock.patch.object(self.main, "assistant_chat", return_value="你好") as chat:
            response = self.client.post(
                "/ai/chat", json={"text": "你好"},
                headers={"X-Jarvis-Key": "test-only-key"},
            )
        self.assertEqual(response.json()["message"], "你好")
        self.assertIn("route", response.json())
        chat.assert_called_once_with("你好", session_id="mobile", route_out={})

    def test_dynamic_speech_rejects_oversized_reply(self):
        response = self.client.post(
            "/speech/dynamic", json={"text": "长" * 201},
            headers={"X-Jarvis-Key": "test-only-key"},
        )
        self.assertEqual(response.status_code, 400)

    def test_dynamic_speech_returns_playable_wav(self):
        response = self.client.post(
            "/speech/dynamic", json={"text": "好的，先生。"},
            headers={"X-Jarvis-Key": "test-only-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content[:4], b"RIFF")
        self.assertGreater(len(response.content), 1000)

    def test_mobile_page_uses_offline_speech_first(self):
        page = self.client.get("/control")
        self.assertEqual(page.status_code, 200)
        self.assertIn('function speakReply(message)', page.text)
        self.assertIn('speakOfflineReply(message);', page.text)
        self.assertIn('"/speech/dynamic"', page.text)


if __name__ == "__main__":
    unittest.main()
