import ctypes
import struct
import unittest
from unittest import mock

import jarvis_mask_overlay


class MaskPositioningTests(unittest.TestCase):
    @unittest.skipUnless(jarvis_mask_overlay.MASK_ASSET.is_file(), "Personal-use mask image is intentionally not distributed")
    def test_mask_asset_is_a_transparent_png(self):
        with jarvis_mask_overlay.MASK_ASSET.open("rb") as image:
            header = image.read(33)
        self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(header[12:16], b"IHDR")
        width, height = struct.unpack(">II", header[16:24])
        self.assertGreaterEqual(width, 500)
        self.assertGreaterEqual(height, 700)
        self.assertEqual(header[25], 6)  # RGBA, not a baked-in white background.

    def test_viewport_center_is_exact_for_scaled_resolution(self):
        self.assertEqual(
            jarvis_mask_overlay._viewport_center(2560, 1440),
            (1280.0, 720.0),
        )

    def test_geometry_preserves_negative_monitor_origins(self):
        self.assertEqual(
            jarvis_mask_overlay._geometry(1920, 1080, -1920, 120),
            "1920x1080-1920+120",
        )

    def test_dpi_awareness_prefers_per_monitor_v2(self):
        user32 = mock.Mock()
        user32.SetProcessDpiAwarenessContext.return_value = True
        with mock.patch.object(jarvis_mask_overlay.sys, "platform", "win32"):
            result = jarvis_mask_overlay._enable_windows_dpi_awareness(
                user32=user32,
                shcore=mock.Mock(),
            )
        self.assertEqual(result, "per-monitor-v2")
        context = user32.SetProcessDpiAwarenessContext.call_args.args[0]
        self.assertIsInstance(context, ctypes.c_void_p)
        user32.SetProcessDPIAware.assert_not_called()

    def test_non_windows_does_not_touch_windows_apis(self):
        user32 = mock.Mock()
        with mock.patch.object(jarvis_mask_overlay.sys, "platform", "linux"):
            result = jarvis_mask_overlay._enable_windows_dpi_awareness(user32=user32)
        self.assertEqual(result, "not-windows")
        self.assertEqual(user32.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
