"""Upgrades use dummy auth, mock model calls and generated test-only pixels."""
import importlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

import jarvis_health
import screen_vision


class UpgradeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.dict(os.environ, {"JARVIS_API_KEY": "test-only-key"}), mock.patch(
            "dotenv.load_dotenv", return_value=False
        ), mock.patch("faster_whisper.WhisperModel"):
            cls.main = importlib.import_module("main")
        cls.client = TestClient(cls.main.app)
        cls.headers = {"X-Jarvis-Key": "test-only-key"}

    def test_all_private_endpoints_require_auth(self):
        for path in ("/health/details", "/ai/memories", "/screen/preview/test", "/codex/tasks"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
        for path in ("/ai/chat/stream", "/screen/preview", "/screen/analyze", "/ai/memories"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={"text":"hello"}).status_code, 401)
        self.assertEqual(self.client.post('/codex/tasks/test/cancel').status_code, 401)
        self.assertEqual(self.client.post('/ai/cancel/test').status_code, 401)

    def test_streaming_chat_emits_deltas_and_route(self):
        def fake_chat(text, *, session_id, on_delta, cancel_event, route_out):
            self.assertEqual(session_id, "mobile")
            on_delta("你好。"); on_delta("我在。")
            route_out.update({"backend":"local", "model":"test"})
            return "你好。我在。"
        with mock.patch.object(self.main, "assistant_chat", side_effect=fake_chat), mock.patch.object(
            self.main, "get_last_route", return_value={"backend":"local", "model":"test"}
        ):
            response = self.client.post('/ai/chat/stream', json={"text":"你好"}, headers=self.headers)
        rows = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual([x['type'] for x in rows], ['start','delta','delta','result'])
        self.assertEqual(rows[-1]['message'], '你好。我在。')
        self.assertEqual(rows[-1]['route']['backend'], 'local')
        self.assertFalse(self.main._CHAT_REQUESTS)

    def test_screen_requires_two_explicit_confirmations(self):
        with mock.patch.object(self.main, 'prepare_preview') as capture:
            response = self.client.post('/screen/preview', json={}, headers=self.headers)
        self.assertEqual(response.status_code, 400); capture.assert_not_called()
        with mock.patch.object(self.main, 'analyze_preview') as analyze:
            response = self.client.post('/screen/analyze', json={"token":"x"}, headers=self.headers)
        self.assertEqual(response.status_code, 400); analyze.assert_not_called()

    def test_screen_local_analysis_never_calls_chat_router(self):
        with mock.patch.object(self.main, 'analyze_preview', return_value='测试图片'), mock.patch.object(
            self.main, 'assistant_chat'
        ) as cloud:
            response = self.client.post('/screen/analyze', json={"token":"x","confirmed":True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['local_only']); cloud.assert_not_called()

    def test_task_cancel_and_listing(self):
        with mock.patch.object(self.main,'list_codex_tasks',return_value=[{'task_id':'x','status':'interrupted'}]):
            result=self.client.get('/codex/tasks',headers=self.headers).json()
        self.assertEqual(result['tasks'][0]['status'],'interrupted')
        with mock.patch.object(self.main,'cancel_codex_task',return_value={'status':'cancel_requested'}):
            result=self.client.post('/codex/tasks/x/cancel',headers=self.headers).json()
        self.assertEqual(result['status'],'cancel_requested')

    def test_health_is_actual_probe_not_static_ready(self):
        with mock.patch.object(self.main,'collect_health',return_value={
            'phone_voice':'unavailable','desktop_bridge':'unavailable'
        }):
            value=self.client.get('/status').json()
        self.assertEqual(value['voice'],'unavailable');self.assertEqual(value['pc'],'unavailable')

    def test_page_loads_control_center_script(self):
        self.assertIn('/assets/upgrades.js',self.client.get('/control').text)
        script=self.client.get('/assets/upgrades.js')
        self.assertEqual(script.status_code,200)
        for marker in ('/health/details','/codex/tasks','/screen/preview','/ai/memories','/voice-command?stream=true'):
            self.assertIn(marker,script.text)


class WindowPrivacyTests(unittest.TestCase):
    def test_analysis_requires_delivered_preview(self):
        token='unseen-preview'
        screen_vision._FRAMES[token]=(time.monotonic()+30,b'test-only-pixels',False)
        self.addCleanup(screen_vision.discard_preview,token)
        with mock.patch('screen_vision.requests.Session') as connection:
            with self.assertRaises(screen_vision.ScreenUnavailable):
                screen_vision.analyze_preview(token,'解释图片')
        connection.assert_not_called()

    def test_sensitive_process_is_not_captured(self):
        with mock.patch('win32gui.GetForegroundWindow',return_value=1), mock.patch(
            'win32gui.GetWindowText',return_value='普通标题'
        ), mock.patch('win32process.GetWindowThreadProcessId',return_value=(1,2)), mock.patch(
            'win32api.OpenProcess',return_value=123
        ), mock.patch('win32api.CloseHandle'), mock.patch(
            'win32process.GetModuleFileNameEx',return_value=r'C:\Apps\Weixin.exe'
        ), mock.patch('win32gui.IsWindowVisible',return_value=True), mock.patch(
            'win32gui.IsIconic',return_value=False
        ), mock.patch('PIL.ImageGrab.grab') as capture:
            with self.assertRaises(screen_vision.ScreenUnavailable):
                screen_vision.capture_foreground_png()
        capture.assert_not_called()

    def test_window_switch_during_capture_discards_pixels(self):
        with mock.patch('win32gui.GetForegroundWindow',side_effect=[1,2]), mock.patch(
            'win32gui.GetWindowText',return_value='测试普通窗口'
        ), mock.patch('win32process.GetWindowThreadProcessId',return_value=(1,2)), mock.patch(
            'win32api.OpenProcess',return_value=123
        ), mock.patch('win32api.CloseHandle'), mock.patch(
            'win32process.GetModuleFileNameEx',return_value=r'C:\Apps\notepad.exe'
        ), mock.patch('win32gui.IsWindowVisible',return_value=True), mock.patch(
            'win32gui.IsIconic',return_value=False
        ), mock.patch('win32gui.GetWindowRect',return_value=(0,0,300,400)), mock.patch(
            'PIL.ImageGrab.grab',return_value=Image.new('RGB',(300,400),'white')
        ) as capture:
            with self.assertRaises(screen_vision.ScreenUnavailable):
                screen_vision.capture_foreground_png()
        capture.assert_called_once_with(window=1)

    def test_oversized_and_expired_metadata_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'heartbeat.json'
            path.write_text(json.dumps({'timestamp':time.time()-100,'microphone':'ready'}),encoding='utf-8')
            with mock.patch.object(jarvis_health,'DESKTOP_HEALTH',path):
                self.assertEqual(jarvis_health.desktop_health()['state'],'stale')

    def test_preview_is_one_time_and_has_ttl(self):
        png=BytesIO();Image.new('RGB',(20,20),'red').save(png,format='PNG')
        with mock.patch('wechat_desktop_bridge.request_screen_preview',return_value=png.getvalue()):
            token=screen_vision.prepare_preview()
        self.assertEqual(screen_vision.preview_bytes(token),png.getvalue())
        self.assertEqual(screen_vision.preview_bytes(token,consume=True),png.getvalue())
        with self.assertRaises(screen_vision.ScreenUnavailable):screen_vision.preview_bytes(token)

    def test_local_vision_payload_has_no_tools_or_cloud(self):
        token='test-vision';screen_vision._FRAMES[token]=(time.monotonic()+30,b'test-only-pixels',True)
        with mock.patch('screen_vision.requests.Session') as factory:
            client=factory.return_value.__enter__.return_value
            client.post.return_value.json.return_value={'message':{'content':'测试'}}
            self.assertEqual(screen_vision.analyze_preview(token,'解释图片'),'测试')
            args,kwargs=client.post.call_args
        self.assertTrue(args[0].startswith('http://127.0.0.1:11434/'))
        self.assertNotIn('tools',kwargs['json'])
        self.assertNotIn(token,screen_vision._FRAMES)


if __name__ == '__main__':unittest.main()
