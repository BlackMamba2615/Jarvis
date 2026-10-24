"""Capture only the WeChat *login* QR from a verified WeChat window.

No full-screen/window image is persisted or sent. QR images are held in memory
briefly and retrieved through the authenticated Jarvis API.
"""

from __future__ import annotations

import io
import ctypes
import os
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np
from PIL import Image, ImageOps


QR_LIFETIME_SECONDS = 60
_qr_lock = threading.Lock()
_pending_qr: dict[str, tuple[float, bytes]] = {}


class LoginQrUnavailable(RuntimeError):
    """WeChat did not show a safely identifiable login QR."""


class OpenWechatUnavailable(RuntimeError):
    """The allowlisted WeChat app could not be shown on the user desktop."""


@contextmanager
def _physical_pixel_coordinates():
    """Avoid 150% DPI virtualization clipping the right side of the QR."""
    user32 = ctypes.windll.user32
    set_context = user32.SetThreadDpiAwarenessContext
    set_context.argtypes = [ctypes.c_void_p]
    set_context.restype = ctypes.c_void_p
    previous = set_context(ctypes.c_void_p(-4))  # per-monitor aware V2
    try:
        yield
    finally:
        if previous:
            set_context(previous)


def _verified_wechat_windows(executable: Path) -> list[int]:
    import win32api
    import win32con
    import win32gui
    import win32process

    expected = os.path.normcase(os.path.abspath(executable))
    matches: list[int] = []

    def inspect(hwnd: int, _unused: object) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        # The desktop login panel is compact. Never image a full chat window.
        if not (250 <= width <= 700 and 280 <= height <= 850):
            return
        if not win32gui.GetClassName(hwnd).startswith("Qt"):
            return
        _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                False,
                pid,
            )
            try:
                actual = win32process.GetModuleFileNameEx(handle, 0)
            finally:
                handle.Close()
        except Exception:
            return
        if os.path.normcase(os.path.abspath(actual)) == expected:
            matches.append(hwnd)

    win32gui.EnumWindows(inspect, None)
    return matches


def _wechat_is_running(executable: Path) -> bool:
    """Avoid starting a second instance over an existing signed-in account."""
    import win32api
    import win32con
    import win32process

    expected = os.path.normcase(os.path.abspath(executable))
    for pid in win32process.EnumProcesses():
        try:
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                False,
                pid,
            )
            try:
                actual = win32process.GetModuleFileNameEx(handle, 0)
            finally:
                handle.Close()
        except Exception:
            continue
        if os.path.normcase(os.path.abspath(actual)) == expected:
            return True
    return False


def _activate_wechat_window(executable: Path) -> bool:
    """Show a verified WeChat window without reading its title or contents."""
    import win32api
    import win32con
    import win32gui
    import win32process

    expected = os.path.normcase(os.path.abspath(executable))
    candidates: list[tuple[int, int, int]] = []

    def inspect(hwnd: int, _unused: object) -> None:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left < 200 or bottom - top < 150:
            return
        _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                False,
                pid,
            )
            try:
                actual = win32process.GetModuleFileNameEx(handle, 0)
            finally:
                handle.Close()
        except Exception:
            return
        if os.path.normcase(os.path.abspath(actual)) == expected:
            visible = int(win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd))
            area = (right - left) * (bottom - top)
            candidates.append((visible, area, hwnd))

    win32gui.EnumWindows(inspect, None)
    if not candidates:
        return False
    # The main window may use a different class than the compact login panel.
    # Prefer a visible, full-size window; never inspect its title or content.
    hwnd = max(candidates)[2]
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # Windows may block focus stealing even when the window was restored.
        pass
    return True


def open_wechat_desktop(executable_path: str) -> str:
    """Open or restore the allowlisted WeChat app in the interactive desktop."""
    executable = Path(executable_path)
    if executable.name.lower() not in {"weixin.exe", "wechat.exe"}:
        raise OpenWechatUnavailable("微信程序路径未通过安全检查")
    if not executable.is_file():
        raise OpenWechatUnavailable("电脑上找不到微信程序")
    if _wechat_is_running(executable):
        if _activate_wechat_window(executable):
            return "微信已打开"
        raise OpenWechatUnavailable("微信已在后台运行，但没有可显示的窗口")

    try:
        os.startfile(str(executable), "open", cwd=str(executable.parent))
    except OSError as error:
        raise OpenWechatUnavailable("电脑微信无法启动") from error
    deadline = time.monotonic() + 10
    saw_process = False
    while time.monotonic() < deadline:
        if _wechat_is_running(executable):
            saw_process = True
            if _activate_wechat_window(executable):
                return "微信已打开"
        time.sleep(0.25)
    if saw_process:
        raise OpenWechatUnavailable("微信已启动，但没有出现可显示的窗口")
    raise OpenWechatUnavailable("电脑微信启动后退出，请检查微信程序")


def _window_image(hwnd: int) -> Image.Image | None:
    import win32gui
    import win32ui

    with _physical_pixel_coordinates():
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        source_handle = win32gui.GetWindowDC(hwnd)
        if not source_handle:
            return None
        source = win32ui.CreateDCFromHandle(source_handle)
        target = source.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source, width, height)
            target.SelectObject(bitmap)
            # PrintWindow captures the selected WeChat window only, even when
            # another application happens to cover it on the user's desktop.
            print_window = ctypes.windll.user32.PrintWindow
            print_window.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
            print_window.restype = ctypes.c_bool
            ok = print_window(hwnd, target.GetSafeHdc(), 2)
            if not ok:
                ok = print_window(hwnd, target.GetSafeHdc(), 0)
            if not ok:
                return None
            pixels = bitmap.GetBitmapBits(True)
            return Image.frombuffer(
                "RGB", (width, height), pixels, "raw", "BGRX", 0, 1
            ).copy()
        finally:
            win32gui.DeleteObject(bitmap.GetHandle())
            target.DeleteDC()
            source.DeleteDC()
            win32gui.ReleaseDC(hwnd, source_handle)


def _extract_login_qr(image: Image.Image) -> bytes | None:
    width, height = image.size
    gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
    payload, corners, _straight = cv2.QRCodeDetector().detectAndDecode(gray)
    if not payload or corners is None or not _is_wechat_login_url(payload):
        return None
    points = corners.reshape(4, 2)
    x0, y0 = points.min(axis=0)
    x1, y1 = points.max(axis=0)
    side_x, side_y = x1 - x0, y1 - y0
    center_x, center_y = (x0 + x1) / 2, (y0 + y1) / 2
    if not (95 <= side_x <= 500 and 95 <= side_y <= 500):
        return None
    if not (0.82 <= side_x / side_y <= 1.18):
        return None
    # A stray code in a corner is not the centered login code.
    if abs(center_x - width / 2) > width * 0.25:
        return None
    if abs(center_y - height / 2) > height * 0.30:
        return None
    margin = max(12, int(max(side_x, side_y) * 0.10))
    bounds = (
        max(0, int(x0) - margin),
        max(0, int(y0) - margin),
        min(width, int(x1) + margin),
        min(height, int(y1) + margin),
    )
    qr = ImageOps.expand(image.crop(bounds), border=16, fill="white")
    output = io.BytesIO()
    qr.save(output, format="PNG")
    return output.getvalue()


def _is_wechat_login_url(payload: str) -> bool:
    """Reject arbitrary QR codes even inside a compact WeChat window."""
    parsed = urlsplit(payload)
    try:
        port = parsed.port
    except ValueError:
        return False
    # The official Windows client currently encodes http://weixin.qq.com/x/...
    # rather than HTTPS in its login QR. We only inspect the URL and never
    # request it; both schemes are accepted for this exact host/path only.
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or port:
        return False
    host = (parsed.hostname or "").lower()
    return (
        (host == "login.weixin.qq.com" and parsed.path.startswith(("/l/", "/qrcode/")))
        or (host == "weixin.qq.com" and parsed.path.startswith("/x/"))
    )


def prepare_login_qr(executable_path: str, timeout_seconds: float = 15) -> str:
    executable = Path(executable_path)
    if executable.name.lower() not in {"weixin.exe", "wechat.exe"}:
        raise LoginQrUnavailable("微信程序路径未通过安全检查")
    if not executable.is_file():
        raise LoginQrUnavailable("电脑上找不到微信程序")

    # Reuse an already visible login panel. Otherwise open the allowlisted app.
    launched = False
    if not _verified_wechat_windows(executable):
        if _wechat_is_running(executable):
            raise LoginQrUnavailable(
                "电脑微信未显示登录二维码。若看到旧账号头像，请在电脑微信手动点“切换账号”后重试；若已进入聊天，请先手动退出登录"
            )
        try:
            os.startfile(str(executable))
            launched = True
        except OSError as error:
            raise LoginQrUnavailable("电脑微信无法启动") from error

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for hwnd in _verified_wechat_windows(executable):
            try:
                image = _window_image(hwnd)
                if image is not None:
                    qr_png = _extract_login_qr(image)
                    if qr_png is not None:
                        token = secrets.token_urlsafe(24)
                        with _qr_lock:
                            _pending_qr.clear()
                            _pending_qr[token] = (
                                time.monotonic() + QR_LIFETIME_SECONDS,
                                qr_png,
                            )
                        return token
            except Exception:
                # A window may close or refresh while being captured.
                continue
        time.sleep(0.5)
    if launched and not _wechat_is_running(executable):
        raise LoginQrUnavailable(
            "电脑微信启动后退出，未出现登录二维码；请先检查电脑微信能否正常打开"
        )
    raise LoginQrUnavailable(
        "未找到微信登录二维码。若看到旧账号头像，请在电脑微信手动点“切换账号”后重试；若已进入聊天，请先手动退出登录"
    )


def consume_login_qr(token: str) -> bytes | None:
    with _qr_lock:
        entry = _pending_qr.pop(token, None)
    if entry is None or time.monotonic() > entry[0]:
        return None
    return entry[1]


def request_login_qr() -> str:
    """Ask the user's interactive desktop Jarvis for a QR without launching here."""
    from wechat_desktop_bridge import request_qr_from_desktop

    qr_png = request_qr_from_desktop()
    token = secrets.token_urlsafe(24)
    with _qr_lock:
        _pending_qr.clear()
        _pending_qr[token] = (time.monotonic() + QR_LIFETIME_SECONDS, qr_png)
    return token
