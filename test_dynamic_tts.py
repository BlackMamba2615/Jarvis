import sys
import threading
import types
import unittest
from unittest import mock

from dynamic_tts import speak_interruptible


class InterruptibleSpeechTests(unittest.TestCase):
    def modules(self, voice):
        pythoncom = types.ModuleType("pythoncom")
        pythoncom.CoInitialize = mock.Mock()
        pythoncom.CoUninitialize = mock.Mock()
        client = types.ModuleType("win32com.client")
        client.Dispatch = mock.Mock(return_value=voice)
        win32com = types.ModuleType("win32com")
        win32com.client = client
        return {"pythoncom": pythoncom, "win32com": win32com, "win32com.client": client}

    def test_sapi_text_is_async_and_never_xml(self):
        voice = mock.Mock()
        voice.GetVoices.return_value = []
        voice.WaitUntilDone.return_value = True
        on_start, on_done = mock.Mock(), mock.Mock()
        with mock.patch.dict(sys.modules, self.modules(voice)):
            completed = speak_interruptible("<speak>作为文字读出</speak>", on_start=on_start, on_done=on_done)
        self.assertTrue(completed)
        voice.Speak.assert_called_once_with("<speak>作为文字读出</speak>", 17)
        on_start.assert_called_once()
        on_done.assert_called_once()

    def test_cancellation_purges_pending_sapi_audio(self):
        cancel = threading.Event()
        voice = mock.Mock()
        voice.GetVoices.return_value = []

        def wait(_milliseconds):
            cancel.set()
            return False

        voice.WaitUntilDone.side_effect = wait
        with mock.patch.dict(sys.modules, self.modules(voice)):
            completed = speak_interruptible("正在讲话", cancel)
        self.assertFalse(completed)
        self.assertEqual(voice.Speak.call_args_list, [mock.call("正在讲话", 17), mock.call("", 3)])

    def test_already_cancelled_speech_does_not_open_com(self):
        cancel = threading.Event()
        cancel.set()
        self.assertFalse(speak_interruptible("不要播放", cancel))


if __name__ == "__main__":
    unittest.main()
