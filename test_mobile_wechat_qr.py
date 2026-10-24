"""API regression tests use a dummy key and do not load the real .env."""

import importlib
import os
import sys
import unittest
from unittest import mock

from fastapi.testclient import TestClient


class MobileWechatQrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.dict(os.environ, {"JARVIS_API_KEY": "test-only-key"}), \
             mock.patch("dotenv.load_dotenv", return_value=False), \
             mock.patch("faster_whisper.WhisperModel"):
            cls.main = importlib.import_module("main")
        cls.client = TestClient(cls.main.app)

    def test_login_voice_intent_returns_qr_reference(self):
        with mock.patch.object(self.main, "request_login_qr", return_value="test-token"):
            response = self.client.post(
                "/command",
                json={"command": "帮我在电脑上登陆微信"},
                headers={"X-Jarvis-Key": "test-only-key"},
            )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result["success"])
        self.assertEqual(result["actions"], ["wechat_login_qr"])
        self.assertEqual(result["qr_token"], "test-token")

    def test_login_keyword_order_and_synonyms_return_qr_reference(self):
        for command in (
            "电脑上微信扫码",
            "微信在计算机上用二维码登录",
            "PC 登录微信",
        ):
            with self.subTest(command=command), mock.patch.object(
                self.main, "request_login_qr", return_value="test-token"
            ) as prepare:
                response = self.client.post(
                    "/command",
                    json={"command": command},
                    headers={"X-Jarvis-Key": "test-only-key"},
                )
                self.assertTrue(response.json()["success"])
                self.assertEqual(response.json()["actions"], ["wechat_login_qr"])
                prepare.assert_called_once_with()

    def test_qr_image_requires_header_not_url_key(self):
        self.assertEqual(self.client.get("/wechat/login-qr/test-token").status_code, 401)
        with mock.patch.object(self.main, "consume_login_qr", return_value=b"png-bytes"):
            response = self.client.get(
                "/wechat/login-qr/test-token",
                headers={"X-Jarvis-Key": "test-only-key"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"png-bytes")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_mixed_command_does_not_launch_anything(self):
        with mock.patch.object(self.main, "request_login_qr") as prepare:
            response = self.client.post(
                "/command",
                json={"command": "帮我在电脑上登陆微信并锁定电脑"},
                headers={"X-Jarvis-Key": "test-only-key"},
            )
        self.assertFalse(response.json()["success"])
        prepare.assert_not_called()

    def test_open_wechat_uses_desktop_bridge_not_local_startfile(self):
        with mock.patch.object(
            self.main, "request_open_wechat_from_desktop", return_value="微信已打开"
        ) as desktop_open, mock.patch("jarvis_skills.os.startfile") as local_start:
            response = self.client.post(
                "/command",
                json={"command": "帮我打开微信"},
                headers={"X-Jarvis-Key": "test-only-key"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["actions"], ["open_wechat"])
        desktop_open.assert_called_once_with()
        local_start.assert_not_called()

    def test_mixed_open_keeps_other_allowlisted_action(self):
        with mock.patch.object(
            self.main, "request_open_wechat_from_desktop", return_value="微信已打开"
        ) as desktop_open, mock.patch.object(
            self.main.skill_system, "execute_action", return_value="计算器已打开"
        ) as ordinary_action:
            response = self.client.post(
                "/command",
                json={"command": "打开微信和计算器"},
                headers={"X-Jarvis-Key": "test-only-key"},
            )
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["actions"], ["open_wechat", "open_calculator"])
        desktop_open.assert_called_once_with()
        ordinary_action.assert_called_once_with("open_calculator", dry_run=False)


if __name__ == "__main__":
    unittest.main()
