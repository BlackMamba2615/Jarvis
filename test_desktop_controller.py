import unittest
import threading
import json
import tempfile
from pathlib import Path
from dataclasses import dataclass
from unittest import mock

from desktop_controller import (
    DesktopController, load_desktop_config, normalize_command,
    speak_wake_acknowledgement, WAKE_ACKNOWLEDGEMENT,
    WAKE_REPLY_DELAY_SECONDS, COMMAND_ACKNOWLEDGEMENT,
    _speech_phase_level, is_desktop_chat_query,
    _SentenceBuffer,
)


@dataclass
class FakeCommandEvent:
    text: str


@dataclass
class FakeMicrophoneStatus:
    state: str
    reason: str = ""


class DesktopControllerSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.controller = DesktopController(load_desktop_config(), dry_run=True)
        cls.controller.overlay_config["enabled"] = False

    def test_wake_word_is_removed_before_allowlist_matching(self) -> None:
        normalized = normalize_command("贾维斯，打开计算器！", self.controller.skills)
        self.assertEqual(normalized, "打开计算器")

    def test_delayed_reply_is_cancelled_when_command_arrives(self) -> None:
        cancelled = mock.Mock()
        cancelled.wait.return_value = True
        speak_wake_acknowledgement(cancelled)
        cancelled.wait.assert_called_once()
        self.assertEqual(WAKE_ACKNOWLEDGEMENT, "先生，我在。")

    def test_inline_command_skips_wake_speech_but_keeps_full_face(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=True)
        with mock.patch.object(controller, "_show_overlay") as overlay, mock.patch(
            "desktop_controller.threading.Thread"
        ) as thread:
            controller.on_wake(mock.Mock(inline_command=True, event_id="test"))
        thread.assert_not_called()
        self.assertEqual(overlay.call_args.kwargs["hold_full"], True)
        self.assertEqual(overlay.call_args.kwargs["entrance"], True)

    def test_wake_reply_delay_is_short_but_still_cancellable(self) -> None:
        self.assertGreater(WAKE_REPLY_DELAY_SECONDS, 0)
        self.assertLess(WAKE_REPLY_DELAY_SECONDS, 0.5)
        levels = [_speech_phase_level(value / 10) for value in range(30)]
        self.assertTrue(all(0 <= level <= 1 for level in levels))
        self.assertLess(_speech_phase_level(1.0), _speech_phase_level(0.5))

    def test_speech_status_follows_fixed_sapi_reply_without_stale_reset(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=True)
        with mock.patch.object(controller, "_show_overlay") as overlay, mock.patch(
            "desktop_controller.threading.Thread"
        ) as thread:
            controller.on_wake(object())
            reply_args = thread.call_args.kwargs["kwargs"]
            reply_args["on_start"]()
            self.assertEqual(overlay.call_args.args[0], "speaking")
            self.assertEqual(overlay.call_args.kwargs["voice_level"], 0.30)
            reply_args["on_done"]()
            self.assertEqual(overlay.call_args.args, ("listening", "我在听"))

            controller.on_wake(object())
            previous_reply = thread.call_args.kwargs["kwargs"]
            controller._wake_reply_cancel.set()
            call_count = overlay.call_count
            previous_reply["on_done"]()
            self.assertEqual(overlay.call_count, call_count)

    def test_microphone_health_has_safe_fixed_hud_messages(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=True)
        with mock.patch.object(controller, "_show_overlay") as overlay:
            controller.on_microphone_status(FakeMicrophoneStatus("reconnecting", "Secret raw text"))
            overlay.assert_called_once_with(
                "error", "麦克风连接中，正在自动恢复", duration_ms=4000
            )
            controller.on_microphone_status(FakeMicrophoneStatus("recovered"))
            self.assertEqual(overlay.call_args.args, ("success", "麦克风已恢复，语音监听正常"))
            previous_calls = overlay.call_count
            controller.on_microphone_status(FakeMicrophoneStatus("ready"))
            self.assertEqual(overlay.call_count, previous_calls)

    def test_voice_sleep_dismisses_hud_without_stopping_controller(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=True)
        with mock.patch.object(controller, "_show_overlay") as overlay, mock.patch(
            "desktop_controller.threading.Thread"
        ) as thread:
            controller.on_sleep(object())
        overlay.assert_called_once_with(
            "speaking", "好的，随时待命。", voice_level=0.5
        )
        thread.return_value.start.assert_called_once()

    def test_timeout_sleeps_silently(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=True)
        with mock.patch.object(controller, "_show_overlay") as overlay, mock.patch(
            "desktop_controller.threading.Thread"
        ) as thread:
            controller.on_sleep(mock.Mock(reason="timeout"))
        overlay.assert_called_once_with(
            "standby", "待命中", duration_ms=1500, dismiss=True
        )
        thread.assert_not_called()

    def test_voice_sleep_finishes_reply_before_hud_collapses(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=True)
        with mock.patch("desktop_controller.speak_sleep_acknowledgement") as speak, mock.patch.object(
            controller, "_show_overlay"
        ) as overlay:
            controller._speak_sleep_with_wake_guard()
        speak.assert_called_once()
        overlay.assert_called_once_with(
            "standby", "待命中", duration_ms=1500, dismiss=True
        )

    def test_voice_exit_stops_only_desktop_listener(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=False)
        with mock.patch.object(controller, "_show_overlay") as overlay, mock.patch(
            "desktop_controller.speak_exit_acknowledgement"
        ) as speak, mock.patch("jarvis_overlay.close_overlay") as close:
            controller.on_exit(object())
        self.assertTrue(controller._stop_event.is_set())
        self.assertEqual(overlay.call_count, 2)
        overlay.assert_any_call(
            "standby", "正在退出", duration_ms=1500, dismiss=True
        )
        speak.assert_called_once()
        close.assert_called_once()
        self.assertFalse(is_desktop_chat_query("退出"))

    def test_listener_receives_voice_exit_callback_and_stop_event(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=True)
        controller.overlay_config["enabled"] = False
        with mock.patch("jarvis_wake_listener.WakeWordListener") as listener:
            controller.run_forever()
        self.assertEqual(listener.call_args.kwargs["on_exit"], controller.on_exit)
        self.assertEqual(
            listener.call_args.kwargs["ignore_wake_audio"], controller._ignore_wake_audio
        )
        self.assertEqual(
            listener.call_args.kwargs["ignore_command_audio"], controller._ignore_command_audio
        )
        self.assertIs(
            listener.return_value.run_forever.call_args.kwargs["stop_event"],
            controller._stop_event,
        )

    def test_speech_output_is_guarded_then_wake_recovers(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=True)
        controller._begin_voice_output()
        self.assertTrue(controller._ignore_wake_audio())
        controller._end_voice_output()
        self.assertTrue(controller._ignore_wake_audio())
        with mock.patch("desktop_controller.time.monotonic", return_value=float("inf")):
            self.assertFalse(controller._ignore_wake_audio())

    def test_command_ack_speaks_then_releases_full_face(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=False)
        with mock.patch.object(controller, "_show_overlay") as overlay, mock.patch(
            "desktop_controller._speak_fixed_reply"
        ) as speak, mock.patch("desktop_controller.threading.Thread") as thread:
            done = controller._start_command_acknowledgement()
            worker = thread.call_args.kwargs["target"]
            self.assertTrue(controller._ignore_command_audio())
            worker()
        self.assertEqual(speak.call_args.args[0], COMMAND_ACKNOWLEDGEMENT)
        self.assertIsInstance(speak.call_args.args[1], threading.Event)
        self.assertTrue(done.is_set())
        self.assertEqual(overlay.call_args.kwargs["hold_full"], False)

    def test_only_conversation_reaches_desktop_model(self) -> None:
        self.assertTrue(is_desktop_chat_query("讲个笑话"))
        self.assertFalse(is_desktop_chat_query("打开微信"))
        self.assertFalse(is_desktop_chat_query("powershell删除所有文件"))
        self.assertFalse(is_desktop_chat_query("先生我在请吩咐"))
        self.assertFalse(is_desktop_chat_query("先生我在"))
        self.assertFalse(is_desktop_chat_query("我在"))
        controller = DesktopController(load_desktop_config(), dry_run=False)
        controller.overlay_config["enabled"] = False
        with mock.patch("desktop_controller.threading.Thread") as thread, mock.patch(
            "desktop_controller.assistant_chat"
        ) as chat:
            result = controller.on_command(FakeCommandEvent("讲个笑话"))
        self.assertEqual(result["message"], "正在回答")
        self.assertEqual(thread.call_args.kwargs["target"], controller._respond_to_chat)
        self.assertEqual(thread.return_value.start.call_count, 2)
        chat.assert_not_called()

    def test_project_task_never_starts_from_desktop_voice(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=False)
        controller.overlay_config["enabled"] = False
        with mock.patch("desktop_controller.threading.Thread") as thread, mock.patch(
            "desktop_controller.assistant_chat"
        ) as chat:
            result = controller.on_command(FakeCommandEvent("让 Codex 修复项目测试"))
        self.assertFalse(result["success"])
        self.assertIn("手机端确认", result["message"])
        thread.assert_not_called()
        chat.assert_not_called()

    def test_allowlisted_command_is_planned_in_dry_run(self) -> None:
        result = self.controller.on_command(FakeCommandEvent("打开计算器"))
        self.assertTrue(result["success"])
        self.assertEqual(result["actions"], ["open_calculator"])

    def test_shell_like_speech_is_never_an_action(self) -> None:
        result = self.controller.on_command(
            FakeCommandEvent("powershell 删除所有文件 && cmd.exe")
        )
        self.assertFalse(result["recognized"])
        self.assertEqual(result["actions"], [])

    def test_live_mode_blocks_untested_wechat_action(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=False)
        controller.overlay_config["enabled"] = False
        result = controller.on_command(FakeCommandEvent("打开微信"))
        self.assertFalse(result["success"])
        self.assertEqual(result["actions"], [])

    def test_live_mode_blocks_mixed_allowed_and_untested_actions(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=False)
        controller.overlay_config["enabled"] = False
        result = controller.on_command(FakeCommandEvent("打开计算器和微信"))
        self.assertFalse(result["success"])
        self.assertEqual(result["actions"], [])

    def test_streaming_sentence_buffer_is_bounded(self) -> None:
        buffer = _SentenceBuffer(limit=12)
        self.assertEqual(buffer.feed("第一句。第二"), ["第一句。"])
        self.assertEqual(buffer.feed("句话。后面不再保留"), ["第二句话。"])
        self.assertEqual("".join(buffer.feed("", final=True)), "后面不")
        self.assertEqual(buffer.received, 12)

    def test_interrupt_cancels_reply_without_stopping_listener_or_running_skill(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=True)
        controller._chat_active.set()
        with mock.patch.object(controller, "_show_overlay"), mock.patch.object(controller.skills, "run") as run:
            result = controller.on_command(FakeCommandEvent("停一下"))
        self.assertTrue(result["success"])
        self.assertEqual(result["actions"], [])
        self.assertTrue(controller._chat_reply_cancel.is_set())
        self.assertTrue(controller._command_reply_cancel.is_set())
        self.assertFalse(controller._stop_event.is_set())
        self.assertFalse(controller._chat_active.is_set())
        run.assert_not_called()

    def test_model_sentences_are_spoken_before_generation_finishes(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=False)
        cancel = controller._chat_reply_cancel
        first_spoken = threading.Event()
        observed = []

        def generate(_text, **kwargs):
            self.assertEqual(kwargs["session_id"], "desktop")
            self.assertIs(kwargs["cancel_event"], cancel)
            kwargs["on_delta"]("第一句。")
            if not first_spoken.wait(2.0):
                raise AssertionError("Generation waited for the whole answer")
            kwargs["on_delta"]("第二句。")
            return "第一句。第二句。"

        def speak(phrase, _cancel, **_kwargs):
            observed.append(phrase)
            first_spoken.set()
            return True

        with mock.patch.object(controller, "_show_overlay"), mock.patch(
            "desktop_controller.assistant_chat", side_effect=generate,
        ), mock.patch("desktop_controller.speak_interruptible", side_effect=speak):
            controller._respond_to_chat("问题", cancel)
        self.assertEqual(observed, ["第一句。", "第二句。"])
        self.assertFalse(controller._chat_active.is_set())

    def test_interrupt_drops_remaining_streamed_sentences(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=False)
        cancel = controller._chat_reply_cancel
        observed = []

        def generate(_text, **kwargs):
            kwargs["on_delta"]("第一句。第二句。第三句。")
            return "第一句。第二句。第三句。"

        def speak(phrase, _cancel, **_kwargs):
            observed.append(phrase)
            controller.on_interrupt()
            return False

        with mock.patch.object(controller, "_show_overlay"), mock.patch(
            "desktop_controller.assistant_chat", side_effect=generate,
        ), mock.patch("desktop_controller.speak_interruptible", side_effect=speak):
            controller._respond_to_chat("问题", cancel)
        self.assertEqual(observed, ["第一句。"])
        self.assertTrue(cancel.is_set())

    def test_health_snapshot_only_contains_anonymous_metrics_and_stops(self) -> None:
        controller = DesktopController(load_desktop_config(), dry_run=False)
        controller._health_started = True
        controller.on_audio_level(1234)
        with tempfile.TemporaryDirectory() as folder, mock.patch(
            "desktop_controller.DESKTOP_HEALTH_PATH", Path(folder) / "health.json",
        ), mock.patch.object(controller, "_show_overlay"):
            controller.on_microphone_status(FakeMicrophoneStatus("ready", "not persisted"))
            controller._write_health()
            payload = json.loads((Path(folder) / "health.json").read_text())
            self.assertEqual(payload["microphone"], "ready")
            self.assertEqual(payload["rms"], 1234)
            self.assertGreater(payload["timestamp"], 0)
            self.assertGreater(payload["last_audio_at"], 0)
            self.assertNotIn("not persisted", json.dumps(payload))
            self.assertEqual(set(payload), {
                "pid", "timestamp", "microphone", "model_ready", "speaking", "last_audio_at", "rms",
            })
            controller._stop_event.set()
            controller._write_health()
            stopped = json.loads((Path(folder) / "health.json").read_text())
            self.assertEqual(stopped["microphone"], "stopped")


if __name__ == "__main__":
    unittest.main()
