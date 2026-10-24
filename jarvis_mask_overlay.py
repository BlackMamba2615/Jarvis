"""Borderless, click-through Iron Man-inspired Jarvis HUD.

The long-lived overlay receives display events over an inherited pipe from the
desktop controller. It never processes voice text as code or opens applications.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageTk


TRANSPARENT = "#010101"
CYAN = "#5cecff"
PALETTE = {
    "standby": ("STANDBY", CYAN),
    "listening": ("LISTENING", CYAN),
    "processing": ("PROCESSING", "#7baaff"),
    "speaking": ("SPEAKING", "#9df5ff"),
    "success": ("ONLINE", "#82ffd2"),
    "error": ("ATTENTION", "#ff748e"),
}
INTRO_SECONDS = 5.6
NANO_LOCK_SECONDS = 3.4
FRAME_MS = 42  # Cinematic 24 fps keeps CPU available for speech recognition.
MINI_WIDTH = 390
MINI_HEIGHT = 116
MINI_VISIBLE_TOP = 14
MINI_HIDDEN_TOP = -MINI_HEIGHT + 17
MINI_REVEAL_SECONDS = 4.0
MASK_ASSET = Path(__file__).resolve().parent / "assets" / "ironman-mask-full.png"


def _remove_baked_shadow(pixels: np.ndarray) -> np.ndarray:
    """Clip the source image's ground shadow to its solid helmet silhouette.

    The supplied PNG contains a dark oval below and beside the chin.  Keep the
    original file untouched; clean one in-memory copy before generating every
    full, mini, eye-lit, and particle frame so the shadow cannot reappear.
    """
    cleaned = pixels.copy()
    if cleaned.ndim != 3 or cleaned.shape[2] != 4:
        return cleaned
    height, width = cleaned.shape[:2]
    # These fractions describe only the lower part of this portrait asset.
    # The upper helmet and its eyes are not modified at all.
    start = round(height * 0.63)
    alpha = cleaned[:, :, 3]
    for y in range(start, height):
        solid = np.flatnonzero(alpha[y] >= 248)
        if solid.size == 0:
            alpha[y, :] = 0
            continue
        left = max(0, int(solid[0]) - 3)
        right = min(width - 1, int(solid[-1]) + 3)
        alpha[y, :left] = 0
        alpha[y, right + 1:] = 0
        # Two-pixel antialiasing avoids a hard vertical cut along the jaw.
        alpha[y, left:min(left + 2, right + 1)] = (
            alpha[y, left:min(left + 2, right + 1)].astype(np.float32)
            * np.array([0.35, 0.7], dtype=np.float32)[:max(0, min(2, right - left + 1))]
        ).astype(np.uint8)
        alpha[y, max(left, right - 1):right + 1] = (
            alpha[y, max(left, right - 1):right + 1].astype(np.float32)
            * np.array([0.7, 0.35], dtype=np.float32)[-min(2, right - left + 1):]
        ).astype(np.uint8)
    return cleaned


def _enable_windows_dpi_awareness(
    user32: Any | None = None,
    shcore: Any | None = None,
) -> str:
    """Select physical-pixel coordinates before Tk creates its first window.

    Windows otherwise virtualizes ``GetSystemMetrics``/Tk geometry for high-DPI
    displays.  Per-monitor V2 is preferred so a future monitor change also keeps
    the HUD centered; the older APIs retain compatibility with Windows 8.1/7.
    """
    if sys.platform != "win32":
        return "not-windows"
    user32 = user32 or ctypes.windll.user32
    try:
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if set_context(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass
    try:
        shcore = shcore or ctypes.windll.shcore
        set_awareness = shcore.SetProcessDpiAwareness
        set_awareness.argtypes = [ctypes.c_int]
        set_awareness.restype = ctypes.c_long
        if set_awareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
            return "per-monitor"
    except (AttributeError, OSError):
        pass
    try:
        set_legacy = user32.SetProcessDPIAware
        set_legacy.argtypes = []
        set_legacy.restype = ctypes.c_bool
        if set_legacy():
            return "system"
    except (AttributeError, OSError):
        pass
    # ERROR_ACCESS_DENIED normally means the host already chose DPI awareness.
    return "already-configured"


def _geometry(width: int, height: int, left: int = 0, top: int = 0) -> str:
    """Return Tk geometry while preserving negative multi-monitor origins."""
    x = f"+{left}" if left >= 0 else str(left)
    y = f"+{top}" if top >= 0 else str(top)
    return f"{width}x{height}{x}{y}"


def _viewport_center(width: int, height: int) -> tuple[float, float]:
    """Return the exact center in the window's current coordinate space."""
    return width / 2, height / 2


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _ease_out(value: float) -> float:
    value = _clamp(value, 0.0, 1.0)
    return 1.0 - (1.0 - value) ** 3


def _safe_message(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return "".join(character for character in text if character.isprintable())[:48]


def _reveal_thresholds(width: int, height: int) -> np.ndarray:
    """Neck-fed microplates climb the shell before sealing the faceplate."""
    y, x = np.indices((height, width), dtype=np.float32)
    rng = np.random.default_rng(20260925)
    center_distance = np.abs(x - (width - 1) / 2) / max(1, width / 2)
    wave = 0.16 * np.sin(x * 0.028 - y * 0.017)
    wave += 0.11 * np.sin(x * 0.057 + y * 0.031)
    wave += 0.045 * np.sin(x * 0.10 - y * 0.085)
    # A shared jitter per 7-pixel cell gives the front the look of many tiny
    # interlocking plates, rather than a single blurred image wipe.
    cells = rng.random(((height + 6) // 7, (width + 6) // 7), dtype=np.float32)
    plate_jitter = np.repeat(np.repeat(cells, 7, axis=0), 7, axis=1)[:height, :width]
    noise = plate_jitter * 0.13 + rng.random((height, width), dtype=np.float32) * 0.035
    rise = 1 - y / max(1, height - 1)
    arrival = (
        0.22 + rise * 2.34 + (1 - center_distance) * rise * 0.23
        + wave + noise
    )
    return np.clip(arrival, 0.14, NANO_LOCK_SECONDS - 0.75)


def _eye_pixels(source: np.ndarray) -> np.ndarray:
    """Locate the image's own white/cyan eye pixels; never draw eye bars."""
    height, width = source.shape[:2]
    y, x = np.indices((height, width))
    red = source[:, :, 0].astype(np.float32)
    green = source[:, :, 1].astype(np.float32)
    blue = source[:, :, 2].astype(np.float32)
    return (
        (y >= 330) & (y < 402)
        & (((x >= 100) & (x < 255)) | ((x >= 318) & (x < 473)))
        & (blue > 165) & (green > 180)
        & (blue >= red * 0.95) & (blue >= green * 0.90)
        & (source[:, :, 3] > 100)
    )


def _eye_intensity(seconds: float) -> float:
    beat = (seconds * 3.3) % 5.0
    closure = max(0.0, 1.0 - abs(beat - 0.29) / 0.29)
    return max(0.06, 1.08 + 0.16 * math.sin(seconds * 9) - closure * 1.15)


def _safe_voice_level(value: Any) -> float | None:
    """Reject malformed IPC data and clamp valid levels to the visual range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    level = float(value)
    if not math.isfinite(level):
        return None
    return _clamp(level, 0.0, 1.0)


def _display_eye_intensity(status: str, seconds: float, voice_level: float | None) -> float:
    """Drive existing image eye pixels from speech amplitude, never draw eye bars."""
    if status == "speaking":
        if voice_level is None:
            return 0.72 + 0.10 * math.sin(seconds * 4.2)
        return 0.32 + 0.91 * voice_level ** 0.7
    if status == "processing":
        return 0.43 + 0.08 * math.sin(seconds * 3.0)
    if status == "standby":
        return 0.58
    if status == "error":
        return 0.22
    return _eye_intensity(seconds)


def _apply_eye_tint(frame: np.ndarray, source: np.ndarray, eyes: np.ndarray, intensity: float) -> None:
    original = source[eyes, :3].astype(np.float32)
    if intensity <= 1:
        frame[eyes, :3] = np.clip(original * intensity, 0, 255)
    else:
        blend = min(0.65, (intensity - 1) * 1.8)
        frame[eyes, :3] = np.clip(
            original * (1 - blend) + np.array([230, 255, 255]) * blend, 0, 255
        )


def _render_mask_pixels(
    source: np.ndarray, thresholds: np.ndarray, eyes: np.ndarray, seconds: float,
    eye_intensity: float | None = None,
) -> np.ndarray:
    """Grow a dark nanobot skin, then solidify its red/gold source armor."""
    frame = source.copy()
    age = seconds - thresholds
    alpha = frame[:, :, 3]
    alpha[age <= -0.025] = 0
    forming = (age > -0.025) & (age < 0.70) & (source[:, :, 3] > 0)
    if np.any(forming):
        phase = age[forming]
        original = source[forming, :3].astype(np.float32)
        # The first visible layer resembles the dark chrome microbots in the
        # reference's arm/neck close-ups, not gold dust around the silhouette.
        liquid = original * np.array([0.12, 0.15, 0.19], dtype=np.float32)
        liquid += np.array([32, 42, 52], dtype=np.float32)
        armor = np.clip((phase - 0.16) / 0.54, 0.0, 1.0)
        metal = liquid * (1.0 - armor[:, None]) + original * armor[:, None]
        # Cyan circuitry glints in the black substrate. A second, warm seam
        # briefly follows when the red/gold armor closes over it.
        circuit = np.clip(1.0 - np.abs(phase - 0.12) / 0.075, 0.0, 1.0)
        seal = np.clip(1.0 - np.abs(phase - 0.47) / 0.09, 0.0, 1.0)
        metal += circuit[:, None] * np.array([8, 55, 75], dtype=np.float32)
        metal += seal[:, None] * np.array([55, 34, 12], dtype=np.float32)
        ys, xs = np.nonzero(forming)
        # Staggered, interrupted seams suggest thousands of tiny scales
        # instead of a regular checkerboard painted over the finished face.
        row = ys // 7
        column = (xs + (row % 2) * 3) // 7
        seam_fragment = (column * 13 + row * 29) % 5 < 3
        micro_seam = (
            (((xs + (row % 2) * 3) % 7 == 0) | (ys % 7 == 0))
            & seam_fragment & (phase < 0.58)
        )
        metal[micro_seam] *= 0.43
        shade = (((column * 19 + row * 23) % 5) - 2) * 5.0
        metal += (shade * (1.0 - armor))[:, None]
        joints = ((xs * 31 + ys * 17) % 89 < 2) & (phase > 0.04) & (phase < 0.33)
        metal[joints] = np.array([64, 165, 190], dtype=np.float32)
        frame[forming, :3] = np.clip(metal, 0, 255).astype(np.uint8)
    nascent = (age > -0.025) & (age < 0.095) & (source[:, :, 3] > 0)
    alpha[nascent] = (
        source[:, :, 3][nascent].astype(np.float32)
        * np.clip((age[nascent] + 0.025) / 0.12, 0.0, 1.0)
    ).astype(np.uint8)
    # Actual eye pixels stay unlit until the armor has nearly finished.
    eye_power = _clamp((seconds - (NANO_LOCK_SECONDS - 0.48)) / 0.38, 0.0, 1.0)
    actual_eye_intensity = _eye_intensity(seconds) if eye_intensity is None else eye_intensity
    _apply_eye_tint(frame, source, eyes, max(0.015, eye_power * actual_eye_intensity))
    return frame


def _pointer_near_top(pointer: tuple[int, int] | None, left: int) -> bool:
    return pointer is not None and left <= pointer[0] < left + MINI_WIDTH and 0 <= pointer[1] <= 36


def _nanobot_color(index: int, is_eye: bool) -> str:
    """Mostly gunmetal microplates, with sparse blue circuit flashes."""
    if is_eye or index % 29 == 0:
        return "#83dbea"
    if index % 7 == 0:
        return "#397b8b"
    return ("#7e8b96", "#a3adb6", "#596875")[index % 3]


def _surface_particle_position(
    target_x: int, target_y: int, arrival: float, seconds: float, index: int,
) -> tuple[float, float, float] | None:
    """Keep a microplate within a few pixels of its final mask surface."""
    travel = 0.43 + (index % 5) * 0.035
    progress = (seconds - arrival + travel) / travel
    if progress <= 0.0 or progress >= 1.0:
        return None
    remaining = 1.0 - _ease_out(progress)
    x = target_x + math.sin(progress * math.tau + index * 0.73) * 3.5 * remaining
    y = target_y + 23.0 * remaining
    return x, y, progress


class MaskOverlay:
    """A single Tk window that transforms from full-screen HUD to top mini HUD."""

    def __init__(
        self,
        status: str = "standby",
        message: str = "JARVIS ONLINE",
        duration_ms: int = 4200,
        *,
        server: bool = False,
    ) -> None:
        self.server = server
        self.diagnostic = os.environ.get("JARVIS_HUD_DIAGNOSTIC") == "1"
        self.status = status if status in PALETTE else "standby"
        self.voice_level: float | None = None
        self.message = _safe_message(message)
        self.status_until = time.monotonic() + max(0.8, duration_ms / 1000)
        self.created_at = time.monotonic()
        self.intro_started: float | None = None if server else self.created_at
        self.hold_full = False
        self._mode = ""
        self._events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._render_errors = 0
        self._geometry_logged = False
        self._mini_top = float(MINI_VISIBLE_TOP)
        self._mini_visible_until = self.created_at + MINI_REVEAL_SECONDS

        self.dpi_awareness = _enable_windows_dpi_awareness()
        self.root = tk.Tk()
        self.root.title("Jarvis HUD")
        if not self.diagnostic:
            self.root.overrideredirect(True)
        self.root.configure(bg=TRANSPARENT)
        self.root.attributes("-topmost", True)
        if not self.diagnostic:
            try:
                self.root.wm_attributes("-transparentcolor", TRANSPARENT)
            except tk.TclError:
                pass
            try:
                self.root.wm_attributes("-toolwindow", True)
            except tk.TclError:
                pass
        self.screen_width = self.root.winfo_screenwidth()
        self.screen_height = self.root.winfo_screenheight()
        self.canvas = tk.Canvas(
            self.root,
            bg=TRANSPARENT,
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.mask_full: tk.PhotoImage | None = None
        self.mask_mini: tk.PhotoImage | None = None
        self._source_pixels: np.ndarray | None = None
        self._reveal_times: np.ndarray | None = None
        self._eye_mask: np.ndarray | None = None
        self._dynamic_mask: ImageTk.PhotoImage | None = None
        self._eye_levels = (0.06, 0.15, 0.3, 0.52, 0.76, 0.98, 1.10, 1.23)
        self._full_eye_frames: list[ImageTk.PhotoImage] = []
        self._mini_eye_frames: list[ImageTk.PhotoImage] = []
        self._nano_targets: list[tuple[int, int, float, str]] = []
        try:
            source_image = Image.open(MASK_ASSET).convert("RGBA")
            self._source_pixels = _remove_baked_shadow(np.asarray(source_image))
            source_image = Image.fromarray(self._source_pixels, "RGBA")
            self._reveal_times = _reveal_thresholds(*source_image.size)
            self._eye_mask = _eye_pixels(self._source_pixels)
            self.mask_full = ImageTk.PhotoImage(source_image)
            mini_size = (source_image.width // 8, source_image.height // 8)
            self.mask_mini = ImageTk.PhotoImage(source_image.resize(mini_size, Image.Resampling.LANCZOS))
            for level in self._eye_levels:
                tinted = self._source_pixels.copy()
                _apply_eye_tint(tinted, self._source_pixels, self._eye_mask, level)
                image = Image.fromarray(tinted, "RGBA")
                self._full_eye_frames.append(ImageTk.PhotoImage(image))
                self._mini_eye_frames.append(ImageTk.PhotoImage(
                    image.resize(mini_size, Image.Resampling.LANCZOS)
                ))
            # Every microplate terminates on a real, nontransparent mask pixel
            # immediately before that part of the armor materializes.
            opaque = np.argwhere(self._source_pixels[:, :, 3] > 150)
            rng = np.random.default_rng(50)
            selected = opaque[rng.choice(len(opaque), size=min(3000, len(opaque)), replace=False)]
            for index, (y, x) in enumerate(selected):
                color = _nanobot_color(index, bool(self._eye_mask[y, x]))
                self._nano_targets.append((int(x), int(y), float(self._reveal_times[y, x]), color))
        except (OSError, ValueError, tk.TclError):
            # Keep the previous vector face as a usable offline fallback.
            pass
        self._set_mode("full" if self.intro_started is not None else "mini")
        self.root.update_idletasks()
        if not self.diagnostic:
            self._enable_click_through()

        if server:
            threading.Thread(
                target=self._read_events,
                name="JarvisHudEvents",
                daemon=True,
            ).start()

    def _enable_click_through(self) -> None:
        if sys.platform != "win32":
            return
        # Layered + transparent passes mouse input to the actual desktop app;
        # tool-window + no-activate keeps the HUD out of Alt-Tab and focus.
        user32 = ctypes.windll.user32
        hwnd = self.root.winfo_id()
        index = -20  # GWL_EXSTYLE
        transparent = 0x20
        toolwindow = 0x80
        noactivate = 0x08000000
        try:
            get_style = user32.GetWindowLongPtrW
            set_style = user32.SetWindowLongPtrW
        except AttributeError:
            get_style = user32.GetWindowLongW
            set_style = user32.SetWindowLongW
        get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        set_style.restype = ctypes.c_ssize_t
        current = get_style(hwnd, index)
        # Tk owns the layered/color-key setup. Do not set WS_EX_LAYERED here:
        # doing so after -transparentcolor can invalidate its painted surface.
        set_style(hwnd, index, current | transparent | toolwindow | noactivate)

    def _set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        if mode == "full":
            left, top = 0, 0
            width, height = self.screen_width, self.screen_height
            self.root.geometry(_geometry(width, height, left, top))
            self.canvas.configure(width=self.screen_width, height=self.screen_height)
        else:
            left = max(0, (self.screen_width - MINI_WIDTH) // 2)
            top = round(self._mini_top)
            width, height = MINI_WIDTH, MINI_HEIGHT
            self.root.geometry(_geometry(width, height, left, top))
            self.canvas.configure(width=MINI_WIDTH, height=MINI_HEIGHT)
        # Realize the new size before the next frame reads canvas dimensions.
        self.root.update_idletasks()
        self._force_window_bounds(left, top, width, height)
        self.root.lift()

    def _force_window_bounds(self, left: int, top: int, width: int, height: int) -> None:
        """Move Tk's actual top-level HWND, not just its drawable child.

        Tk can resize an overrideredirect window without honoring a new origin
        after the mini HUD has been placed at the top center. That leaves the
        full-screen canvas offset by exactly the mini HUD's former left edge.
        """
        if sys.platform != "win32":
            return
        try:
            frame = int(str(self.root.tk.call("wm", "frame", self.root._w)), 0)
            set_pos = ctypes.windll.user32.SetWindowPos
            set_pos.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_uint,
            ]
            set_pos.restype = ctypes.c_bool
            # HWND_TOPMOST, SWP_NOACTIVATE | SWP_SHOWWINDOW.
            set_pos(frame, ctypes.c_void_p(-1), left, top, width, height, 0x50)
        except (AttributeError, OSError, ValueError, tk.TclError):
            pass  # Tk geometry above remains the portable fallback.

    def _read_events(self) -> None:
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        if stream is None:
            self._events.put(None)
            return
        try:
            for line in stream:
                try:
                    payload = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(payload, dict):
                    self._events.put(payload)
        finally:
            self._events.put(None)

    def _apply_event(self, payload: dict[str, Any]) -> None:
        status = payload.get("status")
        if status not in PALETTE:
            return
        self.status = status
        self.voice_level = _safe_voice_level(payload.get("voice_level")) if status == "speaking" else None
        self.message = _safe_message(payload.get("message", ""))
        try:
            duration = int(payload.get("duration_ms", 4200))
        except (TypeError, ValueError):
            duration = 4200
        self.status_until = time.monotonic() + _clamp(duration / 1000, 0.8, 30.0)
        self._mini_visible_until = time.monotonic() + MINI_REVEAL_SECONDS
        if isinstance(payload.get("hold_full"), bool):
            self.hold_full = payload["hold_full"]
        if payload.get("dismiss") is True:
            self.hold_full = False
            self.intro_started = None
            self._mini_visible_until = time.monotonic() - 1.0
            self._set_mode("mini")
            return
        if payload.get("entrance") is True:
            self.intro_started = time.monotonic()
            self._geometry_logged = False
            self._set_mode("full")

    def run(self) -> None:
        self.root.after(16, self._tick)
        self.root.mainloop()

    def _tick(self) -> None:
        try:
            self._frame()
        except Exception as error:
            self._render_errors += 1
            if self._render_errors <= 3:
                location = traceback.extract_tb(error.__traceback__)[-1]
                path = Path(__file__).resolve().parent / "runtime" / "hud_errors.log"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as log:
                    log.write(
                        f"{time.time():.0f} {type(error).__name__} "
                        f"{location.name}:{location.lineno}\n"
                    )
        try:
            if self.root.winfo_exists():
                self.root.after(FRAME_MS, self._tick)
        except tk.TclError:
            pass  # The standalone preview was intentionally closed.

    def _frame(self) -> None:
        try:
            while True:
                payload = self._events.get_nowait()
                if payload is None:
                    self.root.destroy()
                    return
                if isinstance(payload, dict) and payload.get("control") == "exit":
                    self.root.destroy()
                    return
                self._apply_event(payload)
        except queue.Empty:
            pass

        now = time.monotonic()
        if not self.server and now - self.created_at > self.status_until - self.created_at + INTRO_SECONDS:
            self.root.destroy()
            return
        if now > self.status_until and self.status != "standby":
            self.status = "standby"
            self.message = "JARVIS ONLINE"
        if self.intro_started is not None:
            elapsed = now - self.intro_started
            if elapsed < INTRO_SECONDS or self.hold_full:
                self._set_mode("full")
                self._draw_full(elapsed)
            else:
                self.intro_started = None
                self._set_mode("mini")
                self._draw_mini(now)
        else:
            self._set_mode("mini")
            self._draw_mini(now)
        if self._mode == "mini":
            self._update_mini_position(now)

    def _cursor_position(self) -> tuple[int, int] | None:
        if sys.platform != "win32":
            return None
        class Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        point = Point()
        return (point.x, point.y) if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)) else None

    def _update_mini_position(self, now: float) -> None:
        left = max(0, (self.screen_width - MINI_WIDTH) // 2)
        if _pointer_near_top(self._cursor_position(), left):
            self._mini_visible_until = now + 1.0
        target = MINI_VISIBLE_TOP if now < self._mini_visible_until else MINI_HIDDEN_TOP
        next_top = self._mini_top + (target - self._mini_top) * 0.21
        if abs(next_top - target) < 0.7:
            next_top = float(target)
        if round(next_top) != round(self._mini_top):
            self._mini_top = next_top
            self.root.geometry(_geometry(MINI_WIDTH, MINI_HEIGHT, left, round(next_top)))
            self._force_window_bounds(left, round(next_top), MINI_WIDTH, MINI_HEIGHT)

    def _polygon(
        self,
        cx: float,
        cy: float,
        scale: float,
        points: tuple[tuple[float, float], ...],
        *,
        fill: str,
        outline: str = "",
        width: float = 1,
        offset_x: float = 0,
        offset_y: float = 0,
    ) -> None:
        coords: list[float] = []
        for x, y in points:
            coords.extend((cx + (x + offset_x) * scale, cy + (y + offset_y) * scale))
        self.canvas.create_polygon(
            *coords,
            fill=fill,
            outline=outline,
            width=max(1, width * scale),
            joinstyle="round",
        )

    def _helmet(self, cx: float, cy: float, scale: float, seconds: float, assembly: float = 1.0) -> None:
        c = self.canvas
        offset = (1.0 - _ease_out(assembly)) * 150
        pulse = 0.5 + 0.5 * math.sin(seconds * 5.2)

        # Armored rear shell, assembling from either side.
        self._polygon(cx, cy, scale,
            ((-128, -154), (-63, -189), (0, -197), (63, -189), (128, -154),
             (150, -60), (134, 92), (86, 176), (0, 202), (-86, 176), (-134, 92), (-150, -60)),
            fill="#300b13", outline="#ff7956", width=3)
        self._polygon(cx, cy, scale,
            ((-128, -148), (-70, -179), (-110, -55), (-112, 55), (-72, 137), (-125, 99), (-147, 8)),
            fill="#a52b35", outline="#ef6b4b", width=2, offset_x=-offset)
        self._polygon(cx, cy, scale,
            ((128, -148), (70, -179), (110, -55), (112, 55), (72, 137), (125, 99), (147, 8)),
            fill="#a52b35", outline="#ef6b4b", width=2, offset_x=offset)

        # Gold faceplate and angular forehead have a clear helmet silhouette.
        self._polygon(cx, cy, scale,
            ((-89, -128), (-51, -164), (51, -164), (89, -128),
             (108, -43), (95, 87), (56, 158), (0, 184), (-56, 158), (-95, 87), (-108, -43)),
            fill="#bd8749", outline="#ffe0a2", width=3,
            offset_y=-(1 - _ease_out(assembly)) * 100)
        self._polygon(cx, cy, scale,
            ((-72, -135), (-42, -158), (42, -158), (72, -135), (52, -91),
             (0, -77), (-52, -91)),
            fill="#f0c477", outline="#ffe5ad", width=2)
        self._polygon(cx, cy, scale,
            ((-93, -68), (-53, -86), (-15, -61), (-18, 19), (-68, 36), (-98, 13)),
            fill="#e3b771", outline="#ffe1a4", width=2)
        self._polygon(cx, cy, scale,
            ((93, -68), (53, -86), (15, -61), (18, 19), (68, 36), (98, 13)),
            fill="#e3b771", outline="#ffe1a4", width=2)
        self._polygon(cx, cy, scale,
            ((-68, 36), (-18, 19), (18, 19), (68, 36), (62, 132),
             (30, 158), (-30, 158), (-62, 132)),
            fill="#d5a35f", outline="#ffe0a0", width=2)
        self._polygon(cx, cy, scale,
            ((-32, 157), (32, 157), (50, 143), (37, 178), (0, 191), (-37, 178), (-50, 143)),
            fill="#932d31", outline="#f26652", width=2)

        # Dark sockets plus two bright cyan, trapezoid-shaped eyes.
        for direction in (-1, 1):
            eye = ((19, -38), (82, -47), (70, -15), (31, -9))
            socket = ((13, -50), (91, -58), (78, -7), (24, 2))
            transform = lambda shape: tuple((x * direction, y) for x, y in shape)
            self._polygon(cx, cy, scale, transform(socket), fill="#10252d")
            self._polygon(cx, cy, scale, transform(eye), fill="#147e96", outline="#64eaff", width=4)
            inner = ((24, -34), (77, -42), (67, -22), (34, -15))
            self._polygon(cx, cy, scale, transform(inner),
                          fill="#d8ffff" if pulse > 0.15 else "#8aeefd")
        for y, extent in ((86, 25), (101, 34), (116, 27)):
            c.create_line(cx - extent * scale, cy + y * scale,
                          cx + extent * scale, cy + y * scale,
                          fill="#5c3435", width=max(1, 4 * scale))
        c.create_line(cx, cy - 76 * scale, cx, cy + 75 * scale,
                      fill="#fce0a5", width=max(1, 2 * scale))

    def _draw_armor_assembly(self, cx: float, cy: float, seconds: float) -> None:
        if self.mask_full is None:
            return
        if seconds >= NANO_LOCK_SECONDS and self._full_eye_frames:
            index = min(
                range(len(self._eye_levels)),
                key=lambda item: abs(self._eye_levels[item] - _display_eye_intensity(self.status, seconds, self.voice_level)),
            )
            image = self._full_eye_frames[index]
        elif (self._source_pixels is not None and self._reveal_times is not None
              and self._eye_mask is not None):
            pixels = _render_mask_pixels(
                self._source_pixels, self._reveal_times, self._eye_mask, seconds,
                _display_eye_intensity(self.status, seconds, self.voice_level),
            )
            self._dynamic_mask = ImageTk.PhotoImage(Image.fromarray(pixels, "RGBA"))
            image = self._dynamic_mask
        else:
            image = self.mask_full
        self.canvas.create_image(cx, cy, image=image)

    def _draw_nano_stream(self, cx: float, cy: float, seconds: float) -> None:
        """Surface-bound microplates form the face without flying trails."""
        if self.mask_full is None:
            return
        c = self.canvas
        left = cx - self.mask_full.width() / 2
        top = cy - self.mask_full.height() / 2
        for index, (target_x, target_y, arrival, color) in enumerate(self._nano_targets):
            position = _surface_particle_position(target_x, target_y, arrival, seconds, index)
            if position is None:
                continue
            local_x, local_y, progress = position
            pixel_x, pixel_y = round(local_x), round(local_y)
            if (self._source_pixels is None or pixel_x < 0 or pixel_y < 0
                    or pixel_y >= self._source_pixels.shape[0]
                    or pixel_x >= self._source_pixels.shape[1]
                    or self._source_pixels[pixel_y, pixel_x, 3] < 110):
                continue
            x, y = left + local_x, top + local_y
            width = 1.7 + (index % 3) * 0.45
            height = 2.4 + (index % 4) * 0.5
            c.create_polygon(
                x - width, y + height * 0.5,
                x, y - height,
                x + width, y - height * 0.2,
                x + width * 0.2, y + height,
                fill=color, outline="",
            )

    def _draw_full(self, seconds: float) -> None:
        c = self.canvas
        c.delete("all")
        # Use the realized canvas rather than cached screen metrics. This keeps
        # the art centered even if Windows adjusts the borderless window while
        # applying DPI or a display configuration change.
        w = max(1, c.winfo_width())
        h = max(1, c.winfo_height())
        cx, cy = _viewport_center(w, h)
        if seconds >= 0.35 and not self._geometry_logged:
            self._geometry_logged = True
            path = Path(__file__).resolve().parent / "runtime" / "hud_geometry.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as log:
                log.write(
                    f"{time.time():.0f} dpi={self.dpi_awareness} "
                    f"screen={self.screen_width}x{self.screen_height} "
                    f"root={self.root.winfo_rootx()},{self.root.winfo_rooty()} "
                    f"{self.root.winfo_width()}x{self.root.winfo_height()} "
                    f"canvas={w}x{h} center={cx:.0f},{cy:.0f}\n"
                )
        scale = min(1.45, w / 1000, h / 720)
        reveal = _ease_out(seconds / 1.25)
        label, accent = PALETTE[self.status]

        # Keep the desktop unobscured: no enclosing HUD circle behind the mask.
        if self.mask_full is not None:
            # The source pixels appear only as the incoming particles arrive.
            self._draw_armor_assembly(cx, cy, seconds)
        else:
            self._helmet(cx, cy, scale * 1.06, seconds, assembly=reveal)
        self._draw_nano_stream(cx, cy, seconds)
        mask_half_height = self.mask_full.height() / 2 if self.mask_full is not None else 220 * scale
        title_y = min(h - 100, cy + mask_half_height + 26)
        c.create_text(cx, title_y, text="J A R V I S",
                      fill="#c6f9ff", font=("Segoe UI Light", max(22, int(36 * scale))))
        c.create_text(cx, min(h - 60, title_y + 36),
                      text=f"{label}  //  {self.message}", fill=accent,
                      font=("Consolas", max(10, int(13 * scale)), "bold"))

    def _draw_mini(self, seconds: float) -> None:
        c = self.canvas
        c.delete("all")
        label, accent = PALETTE[self.status]
        pulse = 0.5 + 0.5 * math.sin(seconds * 3.7)
        border = "#39c5d9" if pulse > 0.48 else "#216b7b"
        c.create_polygon(12, 7, 378, 7, 389, 19, 389, 95, 378, 108,
                         12, 108, 1, 95, 1, 19,
                         fill="#061921", outline=border, width=2)
        c.create_line(18, 12, 111, 12, fill="#50dff0", width=2)
        c.create_line(281, 103, 372, 103, fill="#50dff0", width=2)
        c.create_arc(17, 14, 111, 108, start=seconds * 90, extent=100,
                     style="arc", outline=accent, width=2)
        if self._mini_eye_frames:
            index = min(
                range(len(self._eye_levels)),
                key=lambda item: abs(self._eye_levels[item] - _display_eye_intensity(self.status, seconds, self.voice_level)),
            )
            c.create_image(64, 59, image=self._mini_eye_frames[index])
        elif self.mask_mini is not None:
            c.create_image(64, 59, image=self.mask_mini)
        else:
            self._helmet(64, 59, 0.20, seconds)
        c.create_line(124, 18, 124, 98, fill="#236376", width=2)
        c.create_text(146, 36, text="JARVIS", anchor="w",
                      fill="#d5fbff", font=("Segoe UI Light", 22))
        c.create_text(147, 65, text=label, anchor="w",
                      fill=accent, font=("Consolas", 11, "bold"))
        c.create_text(147, 88, text=self.message[:24], anchor="w",
                      fill="#8ebcc7", font=("Microsoft YaHei UI", 10))
        for index in range(9):
            height = 5 + (0.5 + 0.5 * math.sin(seconds * 7 + index * 0.8)) * 18
            x = 335 + index * 4
            c.create_line(x, 68 - height / 2, x, 68 + height / 2,
                          fill=accent, width=2)
