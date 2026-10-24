import unittest
from unittest import mock
import io
import json
import math
from PIL import Image
import numpy as np

import jarvis_overlay
import jarvis_mask_overlay


class OverlayOptionsTests(unittest.TestCase):
    def test_options_are_bounded_and_status_is_allowlisted(self):
        options = jarvis_overlay.OverlayOptions(
            status="not-a-state",
            message=" hello\nworld ",
            duration_ms=99,
            size=5_000,
        ).validated()
        self.assertEqual(options.status, "listening")
        self.assertEqual(options.message, "hello world")
        self.assertEqual(options.duration_ms, 800)
        self.assertEqual(options.size, 900)

    def test_message_is_display_only_and_limited(self):
        message = jarvis_overlay.sanitize_message("x" * 100 + "\x00")
        self.assertEqual(message, "x" * 48)

    def test_command_uses_separate_arguments(self):
        command = jarvis_overlay.build_overlay_command(
            jarvis_overlay.OverlayOptions(message="hello & calc.exe")
        )
        self.assertIn("hello & calc.exe", command)
        self.assertNotIn("shell", command)
        self.assertTrue(command[1].endswith("jarvis_overlay.py"))

    @mock.patch("jarvis_overlay.subprocess.Popen")
    def test_launcher_never_uses_shell(self, popen: mock.Mock):
        fake_process = mock.Mock()
        fake_process.stdin = io.BytesIO()
        fake_process.poll.return_value = None
        popen.return_value = fake_process
        jarvis_overlay._BRIDGE_PROCESS = None
        jarvis_overlay.show_overlay(message="display only & calc.exe", entrance=True)
        args, kwargs = popen.call_args
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["stdin"], jarvis_overlay.subprocess.PIPE)
        self.assertNotIn("startupinfo", kwargs)
        self.assertNotIn("display only", " ".join(args[0]))
        payload = json.loads(fake_process.stdin.getvalue())
        self.assertEqual(payload["message"], "display only & calc.exe")
        self.assertTrue(payload["entrance"])
        jarvis_overlay._BRIDGE_PROCESS = None

    @mock.patch("jarvis_overlay.subprocess.Popen")
    def test_sleep_event_only_sends_display_state(self, popen: mock.Mock):
        fake_process = mock.Mock()
        fake_process.stdin = io.BytesIO()
        fake_process.poll.return_value = None
        popen.return_value = fake_process
        jarvis_overlay._BRIDGE_PROCESS = None
        jarvis_overlay.show_overlay("standby", "待命中", dismiss=True)
        payload = json.loads(fake_process.stdin.getvalue())
        self.assertTrue(payload["dismiss"])
        self.assertFalse(payload["entrance"])
        self.assertEqual(payload["status"], "standby")
        jarvis_overlay._BRIDGE_PROCESS = None

    @mock.patch("jarvis_overlay.subprocess.Popen")
    def test_full_face_hold_is_explicit_and_not_reset_by_status_updates(self, popen: mock.Mock):
        fake_process = mock.Mock()
        fake_process.stdin = io.BytesIO()
        fake_process.poll.return_value = None
        popen.return_value = fake_process
        jarvis_overlay._BRIDGE_PROCESS = None
        jarvis_overlay.show_overlay("listening", entrance=True, hold_full=True)
        jarvis_overlay.show_overlay("speaking", "好的，先生")
        jarvis_overlay.show_overlay("success", hold_full=False)
        payloads = [json.loads(line) for line in fake_process.stdin.getvalue().splitlines()]
        self.assertTrue(payloads[0]["hold_full"])
        self.assertNotIn("hold_full", payloads[1])
        self.assertFalse(payloads[2]["hold_full"])
        jarvis_overlay._BRIDGE_PROCESS = None

    @mock.patch("jarvis_overlay.subprocess.Popen")
    def test_speaking_event_sends_bounded_display_only_level(self, popen: mock.Mock):
        fake_process = mock.Mock()
        fake_process.stdin = io.BytesIO()
        fake_process.poll.return_value = None
        popen.return_value = fake_process
        jarvis_overlay._BRIDGE_PROCESS = None
        jarvis_overlay.show_overlay("speaking", "正在回应", voice_level=1.7)
        payload = json.loads(fake_process.stdin.getvalue())
        self.assertEqual(payload["status"], "speaking")
        self.assertEqual(payload["voice_level"], 1.0)
        self.assertFalse(payload["entrance"])
        jarvis_overlay._BRIDGE_PROCESS = None

    def test_level_rejects_non_finite_or_non_numeric_input(self):
        for invalid in (float("nan"), float("inf"), -float("inf"), True, "0.7", None):
            self.assertIsNone(jarvis_overlay.sanitize_voice_level(invalid))
            self.assertIsNone(jarvis_mask_overlay._safe_voice_level(invalid))
        self.assertEqual(jarvis_overlay.sanitize_voice_level(-0.2), 0.0)
        self.assertEqual(jarvis_mask_overlay._safe_voice_level(1.3), 1.0)

    def test_close_overlay_sends_private_exit_event(self):
        fake_process = mock.Mock()
        fake_process.stdin = io.BytesIO()
        fake_process.poll.return_value = None
        jarvis_overlay._BRIDGE_PROCESS = fake_process
        jarvis_overlay.close_overlay()
        self.assertIsNone(jarvis_overlay._BRIDGE_PROCESS)
        fake_process.wait.assert_called_once_with(timeout=3)

    def test_mask_hud_exit_control_closes_window(self):
        import queue

        overlay = jarvis_mask_overlay.MaskOverlay.__new__(jarvis_mask_overlay.MaskOverlay)
        overlay._events = queue.Queue()
        overlay._events.put({"control": "exit"})
        overlay.root = mock.Mock()
        overlay._apply_event = mock.Mock()
        overlay._frame()
        overlay.root.destroy.assert_called_once()
        overlay._apply_event.assert_not_called()


class MaskHudMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image = Image.open(jarvis_mask_overlay.MASK_ASSET).convert("RGBA")
        cls.original = np.asarray(image).copy()
        cls.source = jarvis_mask_overlay._remove_baked_shadow(cls.original)
        cls.thresholds = jarvis_mask_overlay._reveal_thresholds(*image.size)
        cls.eyes = jarvis_mask_overlay._eye_pixels(cls.source)

    def test_cast_shadow_is_removed_without_changing_helmet_or_eyes(self):
        self.assertGreater(int(self.original[600, 80, 3]), 0)
        self.assertEqual(int(self.source[600, 80, 3]), 0)
        self.assertEqual(int(self.source[760, 286, 3]), 0)
        self.assertEqual(int(self.source[600, 286, 3]), 255)
        np.testing.assert_array_equal(self.source[:495], self.original[:495])

    def test_source_pixels_materialize_from_bottom_up(self):
        self.assertGreater(
            float(self.thresholds[130].mean()),
            float(self.thresholds[-130].mean()),
        )
        early = jarvis_mask_overlay._render_mask_pixels(
            self.source, self.thresholds, self.eyes, 0.7
        )
        midway = jarvis_mask_overlay._render_mask_pixels(
            self.source, self.thresholds, self.eyes, 1.8
        )
        locked = jarvis_mask_overlay._render_mask_pixels(
            self.source, self.thresholds, self.eyes,
            jarvis_mask_overlay.NANO_LOCK_SECONDS,
        )
        self.assertLess(int(early[:, :, 3].sum()), int(midway[:, :, 3].sum()))
        self.assertLess(int(midway[:, :, 3].sum()), int(locked[:, :, 3].sum()))
        np.testing.assert_array_equal(locked[:, :, 3], self.source[:, :, 3])

    def test_eye_glow_changes_actual_eye_pixels_only(self):
        self.assertGreater(int(self.eyes.sum()), 1000)
        ys, xs = np.where(self.eyes)
        self.assertTrue(np.all((ys >= 330) & (ys < 402)))
        self.assertTrue(np.all(((xs >= 100) & (xs < 255)) | ((xs >= 318) & (xs < 473))))
        frame = self.source.copy()
        jarvis_mask_overlay._apply_eye_tint(frame, self.source, self.eyes, 0.12)
        self.assertLess(float(frame[self.eyes, :3].mean()),
                        float(self.source[self.eyes, :3].mean()))
        np.testing.assert_array_equal(frame[~self.eyes], self.source[~self.eyes])

    def test_speech_level_changes_only_real_eye_pixels_and_pauses_dim_them(self):
        quiet = jarvis_mask_overlay._display_eye_intensity("speaking", 4.0, 0.0)
        loud = jarvis_mask_overlay._display_eye_intensity("speaking", 4.0, 1.0)
        fallback = jarvis_mask_overlay._display_eye_intensity("speaking", 4.0, None)
        self.assertLess(quiet, fallback)
        self.assertLess(fallback, loud)
        self.assertTrue(math.isfinite(fallback))
        quiet_frame, loud_frame = self.source.copy(), self.source.copy()
        jarvis_mask_overlay._apply_eye_tint(quiet_frame, self.source, self.eyes, quiet)
        jarvis_mask_overlay._apply_eye_tint(loud_frame, self.source, self.eyes, loud)
        self.assertLess(float(quiet_frame[self.eyes, :3].mean()),
                        float(loud_frame[self.eyes, :3].mean()))
        np.testing.assert_array_equal(quiet_frame[~self.eyes], loud_frame[~self.eyes])

    def test_speaking_update_does_not_restart_full_entrance(self):
        overlay = jarvis_mask_overlay.MaskOverlay.__new__(jarvis_mask_overlay.MaskOverlay)
        overlay.status = "listening"
        overlay.message = ""
        overlay.voice_level = None
        overlay.intro_started = 123.0
        overlay._mini_visible_until = 0.0
        overlay._set_mode = mock.Mock()
        overlay._apply_event({"status": "speaking", "message": "回应中", "voice_level": 0.4})
        self.assertEqual(overlay.intro_started, 123.0)
        self.assertEqual(overlay.voice_level, 0.4)
        overlay._set_mode.assert_not_called()
        overlay._apply_event({"status": "listening", "message": "我在听"})
        self.assertIsNone(overlay.voice_level)

    def test_full_face_stays_held_until_explicit_release(self):
        overlay = jarvis_mask_overlay.MaskOverlay.__new__(jarvis_mask_overlay.MaskOverlay)
        overlay.status = "listening"
        overlay.message = ""
        overlay.voice_level = None
        overlay.hold_full = False
        overlay.intro_started = 123.0
        overlay._mini_visible_until = 0.0
        overlay._set_mode = mock.Mock()
        overlay._apply_event({"status": "listening", "hold_full": True})
        overlay._apply_event({"status": "speaking"})
        self.assertTrue(overlay.hold_full)
        overlay._apply_event({"status": "success", "hold_full": False})
        self.assertFalse(overlay.hold_full)

    def test_held_face_remains_full_after_intro_duration(self):
        import queue
        import time

        overlay = jarvis_mask_overlay.MaskOverlay.__new__(jarvis_mask_overlay.MaskOverlay)
        overlay.server = True
        overlay._events = queue.Queue()
        overlay.status = "listening"
        overlay.message = "我在听"
        overlay.status_until = time.monotonic() + 30
        overlay.intro_started = time.monotonic() - jarvis_mask_overlay.INTRO_SECONDS - 1
        overlay.hold_full = True
        overlay._mode = "full"
        overlay._set_mode = mock.Mock()
        overlay._draw_full = mock.Mock()
        overlay._draw_mini = mock.Mock()
        overlay._frame()
        overlay._draw_full.assert_called_once()
        overlay._draw_mini.assert_not_called()

    def test_dark_nanobot_skin_precedes_metal_and_eye_ignition(self):
        seconds = 1.8
        frame = jarvis_mask_overlay._render_mask_pixels(
            self.source, self.thresholds, self.eyes, seconds
        )
        age = seconds - self.thresholds
        gold_front = (
            (self.source[:, :, 3] > 200)
            & (self.source[:, :, 0] > 100)
            & (self.source[:, :, 1] > 65)
            & ~self.eyes
            & (age > 0.05)
            & (age < 0.24)
        )
        self.assertGreater(int(gold_front.sum()), 1000)
        self.assertLess(float(frame[gold_front, :3].mean()),
                        float(self.source[gold_front, :3].mean()) * 0.6)
        before_ignition = jarvis_mask_overlay._render_mask_pixels(
            self.source, self.thresholds, self.eyes,
            jarvis_mask_overlay.NANO_LOCK_SECONDS - 0.8,
        )
        self.assertLess(float(before_ignition[self.eyes, :3].mean()), 10)

    def test_microplate_palette_is_mostly_metal_with_sparse_cyan(self):
        colors = [jarvis_mask_overlay._nanobot_color(index, False) for index in range(100)]
        self.assertGreater(colors.count("#596875"), 20)
        self.assertLess(colors.count("#83dbea"), 10)
        self.assertEqual(jarvis_mask_overlay._nanobot_color(1, True), "#83dbea")

    def test_particles_crawl_on_surface_instead_of_flying_from_outside(self):
        target_x, target_y, arrival = 280, 370, 1.8
        self.assertIsNone(jarvis_mask_overlay._surface_particle_position(
            target_x, target_y, arrival, 0.8, 1
        ))
        for seconds in (1.38, 1.5, 1.7):
            x, y, progress = jarvis_mask_overlay._surface_particle_position(
                target_x, target_y, arrival, seconds, 1
            )
            self.assertLessEqual(abs(x - target_x), 4)
            self.assertGreaterEqual(y, target_y)
            self.assertLessEqual(y - target_y, 24)
            self.assertGreater(progress, 0)
        self.assertIsNone(jarvis_mask_overlay._surface_particle_position(
            target_x, target_y, arrival, arrival, 1
        ))

    def test_top_edge_reveals_minimized_hud(self):
        self.assertTrue(jarvis_mask_overlay._pointer_near_top((500, 2), 400))
        self.assertFalse(jarvis_mask_overlay._pointer_near_top((500, 80), 400))
        self.assertFalse(jarvis_mask_overlay._pointer_near_top((900, 2), 400))


if __name__ == "__main__":
    unittest.main()
