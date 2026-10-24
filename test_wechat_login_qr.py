import unittest
from unittest import mock

import cv2
from PIL import Image

import wechat_login_qr as qr


def login_panel() -> Image.Image:
    code = cv2.QRCodeEncoder_create().encode(
        "https://weixin.qq.com/x/test-only-login-token"
    )
    code = cv2.resize(code, (220, 220), interpolation=cv2.INTER_NEAREST)
    panel = Image.new("RGB", (330, 440), "white")
    panel.paste(Image.fromarray(code).convert("RGB"), (55, 110))
    return panel


class WechatLoginQrTests(unittest.TestCase):
    def test_only_detected_centered_qr_is_returned(self):
        result = qr._extract_login_qr(login_panel())
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIsNone(qr._extract_login_qr(Image.new("RGB", (330, 440), "white")))

    def test_contact_or_other_qr_is_not_sent(self):
        self.assertFalse(qr._is_wechat_login_url("https://weixin.qq.com/r/contact"))
        self.assertFalse(qr._is_wechat_login_url("https://evil.example/x/fake"))
        self.assertTrue(qr._is_wechat_login_url("http://weixin.qq.com/x/test-login"))

    def test_qr_token_is_one_use(self):
        with mock.patch.object(qr, "_verified_wechat_windows", return_value=[123]), \
             mock.patch.object(qr, "_window_image", return_value=login_panel()):
            token = qr.prepare_login_qr("D:/Program Files/Tencent/Weixin/Weixin.exe")
        image = qr.consume_login_qr(token)
        self.assertIsNotNone(image)
        self.assertIsNone(qr.consume_login_qr(token))

    def test_rejects_non_wechat_program(self):
        with self.assertRaises(qr.LoginQrUnavailable):
            qr.prepare_login_qr("C:/Windows/System32/calc.exe")

    def test_does_not_start_second_instance_over_existing_account(self):
        with mock.patch.object(qr, "_verified_wechat_windows", return_value=[]), \
             mock.patch.object(qr, "_wechat_is_running", return_value=True), \
             mock.patch.object(qr.os, "startfile") as start:
            with self.assertRaisesRegex(qr.LoginQrUnavailable, "切换账号"):
                qr.prepare_login_qr("D:/Program Files/Tencent/Weixin/Weixin.exe")
        start.assert_not_called()

    def test_reports_wechat_exit_after_launch(self):
        with mock.patch.object(qr, "_verified_wechat_windows", return_value=[]), \
             mock.patch.object(qr, "_wechat_is_running", return_value=False), \
             mock.patch.object(qr.os, "startfile") as start:
            with self.assertRaisesRegex(qr.LoginQrUnavailable, "启动后退出"):
                qr.prepare_login_qr(
                    "D:/Program Files/Tencent/Weixin/Weixin.exe",
                    timeout_seconds=0,
                )
        start.assert_called_once()

    def test_open_wechat_restores_running_window_without_second_instance(self):
        with mock.patch.object(qr, "_wechat_is_running", return_value=True), \
             mock.patch.object(qr, "_activate_wechat_window", return_value=True), \
             mock.patch.object(qr.os, "startfile") as start:
            self.assertEqual(
                qr.open_wechat_desktop("D:/Program Files/Tencent/Weixin/Weixin.exe"),
                "微信已打开",
            )
        start.assert_not_called()

    def test_open_wechat_launches_only_when_not_running(self):
        with mock.patch.object(qr, "_wechat_is_running", side_effect=[False, True]), \
             mock.patch.object(qr, "_activate_wechat_window", return_value=True), \
             mock.patch.object(qr.os, "startfile") as start:
            self.assertEqual(
                qr.open_wechat_desktop("D:/Program Files/Tencent/Weixin/Weixin.exe"),
                "微信已打开",
            )
        start.assert_called_once_with(
            "D:\\Program Files\\Tencent\\Weixin\\Weixin.exe",
            "open",
            cwd="D:\\Program Files\\Tencent\\Weixin",
        )


if __name__ == "__main__":
    unittest.main()
