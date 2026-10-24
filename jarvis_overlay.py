"""Safe, persistent bridge to the borderless Jarvis desktop HUD.

Display events go over a private pipe, never through a shell command line.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


TRANSPARENT_COLOR = "#010101"
ALLOWED_STATUSES = frozenset({"standby", "listening", "processing", "speaking", "success", "error"})
STATUS_STYLE = {
    "standby": ("STANDBY", "#64f6ff"),
    "listening": ("LISTENING", "#64f6ff"),
    "processing": ("PROCESSING", "#50a7ff"),
    "speaking": ("SPEAKING", "#9df5ff"),
    "success": ("ONLINE", "#76ffb1"),
    "error": ("ATTENTION", "#ff6f91"),
}
_BRIDGE_LOCK = threading.Lock()
_BRIDGE_PROCESS: subprocess.Popen[bytes] | None = None


@dataclass(frozen=True)
class OverlayOptions:
    status: str = "listening"
    message: str = "我在听"
    duration_ms: int = 4200
    size: int = 540

    def validated(self) -> "OverlayOptions":
        status = self.status if self.status in ALLOWED_STATUSES else "listening"
        message = sanitize_message(self.message)
        duration_ms = min(30_000, max(800, int(self.duration_ms)))
        size = min(900, max(360, int(self.size)))
        return OverlayOptions(status, message, duration_ms, size)


def sanitize_message(value: object) -> str:
    """Return display-only text with control characters removed and a hard cap."""
    text = str(value or "")
    text = " ".join(text.split())
    text = "".join(character for character in text if character.isprintable())
    return text[:48] or "我在听"


def sanitize_voice_level(value: object) -> float | None:
    """Accept only a finite, bounded display amplitude; never interpret it as a command."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    level = float(value)
    if not math.isfinite(level):
        return None
    return min(1.0, max(0.0, level))


def _pythonw_executable() -> str:
    # The HUD's image renderer needs Pillow and NumPy. A legacy desktop
    # controller may still run under the system Python without those packages.
    bundled = Path(__file__).resolve().parent / ".voice-venv" / "Scripts" / "pythonw.exe"
    if os.name == "nt" and bundled.is_file():
        return str(bundled)
    executable = Path(sys.executable)
    if os.name == "nt" and executable.name.lower() == "python.exe":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return str(executable)


def build_overlay_command(options: OverlayOptions) -> list[str]:
    """Build a fixed argv list; intended for ``shell=False`` only."""
    safe = options.validated()
    return [
        _pythonw_executable(),
        str(Path(__file__).resolve()),
        "--status",
        safe.status,
        "--message",
        safe.message,
        "--duration-ms",
        str(safe.duration_ms),
        "--size",
        str(safe.size),
    ]


def build_server_command() -> list[str]:
    return [_pythonw_executable(), str(Path(__file__).resolve()), "--server"]


def show_overlay(
    status: str = "listening",
    message: str = "我在听",
    duration_ms: int = 4200,
    size: int = 540,
    entrance: bool = False,
    dismiss: bool = False,
    hold_full: bool | None = None,
    voice_level: float | None = None,
) -> subprocess.Popen[bytes]:
    """Update one click-through HUD process, spawning it on first use."""
    del size  # The new HUD sizes itself for full-screen and top mini modes.
    safe = OverlayOptions(status, message, duration_ms).validated()
    payload = {
        "status": safe.status,
        "message": safe.message,
        "duration_ms": safe.duration_ms,
        "entrance": bool(entrance),
        "dismiss": bool(dismiss),
        "voice_level": sanitize_voice_level(voice_level),
    }
    if hold_full is not None:
        payload["hold_full"] = bool(hold_full)
    encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    global _BRIDGE_PROCESS
    with _BRIDGE_LOCK:
        for _attempt in range(2):
            if _BRIDGE_PROCESS is None or _BRIDGE_PROCESS.poll() is not None:
                _BRIDGE_PROCESS = subprocess.Popen(
                    build_server_command(),
                    shell=False,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
            try:
                assert _BRIDGE_PROCESS.stdin is not None
                _BRIDGE_PROCESS.stdin.write(encoded)
                _BRIDGE_PROCESS.stdin.flush()
                return _BRIDGE_PROCESS
            except (BrokenPipeError, OSError):
                _BRIDGE_PROCESS = None
    raise OSError("Jarvis HUD process did not accept a display event")


def close_overlay() -> None:
    """Ask this controller's private HUD process to close, then release its pipe."""
    global _BRIDGE_PROCESS
    with _BRIDGE_LOCK:
        process = _BRIDGE_PROCESS
        _BRIDGE_PROCESS = None
        if process is None:
            return
        if process.poll() is None and process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.write(b'{"control":"exit"}\n')
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)


class JarvisOverlay:
    """A transparent, always-on-top HUD implemented with Python's Tk runtime."""

    def __init__(self, options: OverlayOptions) -> None:
        import tkinter as tk

        self.tk = tk
        self.options = options.validated()
        self.started_at = time.monotonic()
        self.root = tk.Tk()
        self.root.title("Jarvis")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=TRANSPARENT_COLOR)
        try:
            self.root.wm_attributes("-transparentcolor", TRANSPARENT_COLOR)
        except tk.TclError:
            pass
        self.root.attributes("-alpha", 0.01)
        self.root.bind("<Escape>", lambda _event: self.close())
        self.root.bind("<Button-1>", lambda _event: self.close())

        size = self.options.size
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        left = max(0, (screen_width - size) // 2)
        top = max(0, (screen_height - size) // 2)
        self.root.geometry(f"{size}x{size}+{left}+{top}")
        self.canvas = tk.Canvas(
            self.root,
            width=size,
            height=size,
            bg=TRANSPARENT_COLOR,
            highlightthickness=0,
        )
        self.canvas.pack(fill="both", expand=True)

    def close(self) -> None:
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass

    def run(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self._animate()
        self.root.mainloop()

    def _animate(self) -> None:
        elapsed = time.monotonic() - self.started_at
        duration = self.options.duration_ms / 1000
        if elapsed >= duration:
            self.close()
            return

        fade_in = min(1.0, elapsed / 0.32)
        fade_out = min(1.0, max(0.0, duration - elapsed) / 0.5)
        alpha = max(0.01, min(fade_in, fade_out) * 0.96)
        self.root.attributes("-alpha", alpha)
        self._draw(elapsed)
        self.root.after(33, self._animate)

    def _draw(self, seconds: float) -> None:
        canvas = self.canvas
        canvas.delete("all")
        size = self.options.size
        center = size / 2
        scale = size / 540
        _label, accent = STATUS_STYLE[self.options.status]

        def oval(radius: float, **kwargs: object) -> int:
            return canvas.create_oval(
                center - radius,
                center - radius,
                center + radius,
                center + radius,
                **kwargs,
            )

        # Dim halo plus crisp orbital rings gives a neon-like glow without PIL.
        for radius, width, color in (
            (225 * scale, 12 * scale, "#06232b"),
            (205 * scale, 8 * scale, "#092b38"),
            (146 * scale, 10 * scale, "#08293a"),
        ):
            oval(radius, outline=color, width=width)

        spin = seconds * 76
        arc_specs = (
            (225, spin, 78, 3),
            (225, spin + 180, 42, 2),
            (194, -spin * 0.72, 105, 4),
            (168, spin * 1.18, 64, 2),
            (146, -spin * 1.4, 116, 3),
        )
        for radius, start, extent, width in arc_specs:
            radius *= scale
            canvas.create_arc(
                center - radius,
                center - radius,
                center + radius,
                center + radius,
                start=start,
                extent=extent,
                style="arc",
                outline=accent,
                width=max(1, int(width * scale)),
            )

        pulse = 1 + 0.035 * math.sin(seconds * math.tau * 1.4)
        core_radius = 104 * scale * pulse
        oval(core_radius, fill="#071720", outline="#1c829c", width=max(1, int(2 * scale)))
        oval(82 * scale, outline=accent, width=max(1, int(2 * scale)))
        oval(70 * scale, outline="#15566a", width=max(1, int(scale)))

        for index in range(48):
            angle = math.radians(index * 7.5 + spin * 0.36)
            inner = (232 if index % 4 else 228) * scale
            outer = (238 if index % 4 else 244) * scale
            color = accent if index % 4 == 0 else "#17657a"
            canvas.create_line(
                center + math.cos(angle) * inner,
                center + math.sin(angle) * inner,
                center + math.cos(angle) * outer,
                center + math.sin(angle) * outer,
                fill=color,
                width=max(1, int((2 if index % 4 == 0 else 1) * scale)),
            )

        label, _accent = STATUS_STYLE[self.options.status]
        canvas.create_text(
            center,
            center - 12 * scale,
            text="JARVIS",
            fill="#d7fbff",
            font=("Segoe UI Light", max(18, int(31 * scale))),
        )
        canvas.create_text(
            center,
            center + 27 * scale,
            text=label,
            fill=accent,
            font=("Consolas", max(8, int(10 * scale)), "bold"),
        )
        canvas.create_text(
            center,
            center + 126 * scale,
            text=self.options.message,
            fill="#b8edf4",
            font=("Microsoft YaHei UI", max(9, int(13 * scale))),
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show the Jarvis desktop overlay")
    parser.add_argument("--status", choices=sorted(ALLOWED_STATUSES), default="listening")
    parser.add_argument("--message", default="我在听")
    parser.add_argument("--duration-ms", type=int, default=4200)
    parser.add_argument("--size", type=int, default=540)
    parser.add_argument("--server", action="store_true", help="read display events from stdin")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration without opening a window",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    options = OverlayOptions(args.status, args.message, args.duration_ms, args.size).validated()
    if args.check:
        print(
            f"Jarvis overlay OK: status={options.status}, "
            f"duration_ms={options.duration_ms}, size={options.size}"
        )
        return 0
    from jarvis_mask_overlay import MaskOverlay

    MaskOverlay(options.status, options.message, options.duration_ms, server=args.server).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
