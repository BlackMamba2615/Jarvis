import logging
import os
import unittest
from pathlib import Path
from unittest import mock
from urllib import error, request

import wechat_desktop_bridge as bridge
import wechat_login_qr as qr


class WechatDesktopBridgeTests(unittest.TestCase):
    def test_authenticated_memory_only_qr_roundtrip(self):
        image = b"\x89PNG\r\n\x1a\n" + b"test-only"
        auth_file = Path(__file__).resolve().parent / "runtime" / f"bridge-test-{os.getpid()}.bin"
        self.addCleanup(auth_file.unlink, missing_ok=True)
        with mock.patch.object(bridge, "AUTH_FILE", auth_file), \
             mock.patch.object(qr, "prepare_login_qr", return_value="test-token") as prepare, \
             mock.patch.object(qr, "consume_login_qr", return_value=image):
            bridge.start_desktop_bridge(
                "D:/Program Files/Tencent/Weixin/Weixin.exe",
                logging.getLogger("test-bridge"),
            )
            port = int.from_bytes(bridge.AUTH_FILE.read_bytes()[:2], "big")
            call = request.Request(
                f"http://127.0.0.1:{port}{bridge.BRIDGE_PATH}",
                data=b"",
                method="POST",
            )
            with self.assertRaises(error.HTTPError) as denied:
                request.urlopen(call, timeout=3)
            self.assertEqual(denied.exception.code, 403)
            prepare.assert_not_called()
            self.assertEqual(bridge.request_qr_from_desktop(), image)
            prepare.assert_called_once()
            self.assertTrue(auth_file.is_file())

    def test_open_action_never_requests_qr_or_sends_voice_text(self):
        auth_file = Path(__file__).resolve().parent / "runtime" / f"bridge-open-test-{os.getpid()}.bin"
        self.addCleanup(auth_file.unlink, missing_ok=True)
        with mock.patch.object(bridge, "AUTH_FILE", auth_file), \
             mock.patch.object(qr, "open_wechat_desktop", return_value="微信已打开") as opened, \
             mock.patch.object(qr, "prepare_login_qr") as prepare:
            bridge.start_desktop_bridge(
                "D:/Program Files/Tencent/Weixin/Weixin.exe",
                logging.getLogger("test-bridge-open"),
            )
            self.assertEqual(bridge.request_open_wechat_from_desktop(), "微信已打开")
            opened.assert_called_once_with("D:/Program Files/Tencent/Weixin/Weixin.exe")
            prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
