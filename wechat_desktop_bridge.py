"""Fixed-purpose bridge from the phone API to the user's interactive desktop.

Only a verified WeChat login QR crosses a loopback connection, in memory.
The random bridge secret is protected with Windows DPAPI for this user.
No voice text, full-window image, or QR file is stored.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib import error, request

import win32crypt


AUTH_FILE = Path(__file__).resolve().parent / "runtime" / "wechat_bridge_auth.bin"
BRIDGE_PATH = "/wechat-login-qr"
OPEN_PATH = "/open-wechat"
HEALTH_PATH = "/health"
SCREEN_PATH = "/screen-preview"
DESKTOP_CONTROL_PATH = "/desktop-control"


class DesktopBridgeUnavailable(RuntimeError):
    """The user's interactive Jarvis could not complete a fixed operation."""


class _NoBridgeRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise DesktopBridgeUnavailable("电脑桌面桥不允许重定向")


def start_desktop_bridge(executable_path: str, logger: logging.Logger) -> None:
    """Start one localhost listener in the user's visible Jarvis process."""
    secret = secrets.token_bytes(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:
            from wechat_login_qr import (
                LoginQrUnavailable,
                OpenWechatUnavailable,
                consume_login_qr,
                open_wechat_desktop,
                prepare_login_qr,
            )

            if self.path not in {BRIDGE_PATH, OPEN_PATH, HEALTH_PATH, SCREEN_PATH, DESKTOP_CONTROL_PATH} or not hmac.compare_digest(
                self.headers.get("X-Jarvis-Bridge", ""), secret.hex()
            ):
                self.send_error(403)
                return
            try:
                if self.path == DESKTOP_CONTROL_PATH:
                    from desktop_automation import handle_desktop_request, DesktopControlUnavailable
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= length <= 16_384:
                        self.send_error(400)
                        return
                    try:
                        payload = json.loads(self.rfile.read(length))
                        result = handle_desktop_request(payload, owner="mobile")
                    except (ValueError, DesktopControlUnavailable) as failure:
                        status, kind = 409, "text/plain; charset=utf-8"
                        body = str(failure).encode("utf-8")
                    else:
                        status, kind = 200, "application/json"
                        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                elif self.path == HEALTH_PATH:
                    status, kind = 200, "application/json"
                    body = json.dumps({"connected": True, "pid": os.getpid(), "version": 2}).encode()
                elif self.path == SCREEN_PATH:
                    from screen_vision import capture_foreground_png
                    status, kind, body = 200, "image/png", capture_foreground_png()
                elif self.path == BRIDGE_PATH:
                    token = prepare_login_qr(executable_path)
                    png = consume_login_qr(token)
                    if png is None:
                        raise LoginQrUnavailable("微信二维码已过期，请重试")
                    status, kind, body = 200, "image/png", png
                    logger.info("wechat_desktop_bridge_qr_ready")
                else:
                    message = open_wechat_desktop(executable_path)
                    status, kind = 200, "text/plain; charset=utf-8"
                    body = message.encode("utf-8")
                    logger.info("wechat_desktop_bridge_opened")
            except (LoginQrUnavailable, OpenWechatUnavailable) as failure:
                status, kind = 409, "text/plain; charset=utf-8"
                body = str(failure).encode("utf-8")
                logger.info("wechat_desktop_bridge_operation_unavailable")
            except Exception as failure:
                status, kind = 500, "text/plain; charset=utf-8"
                body = "电脑 Jarvis 无法完成该请求".encode("utf-8")
                logger.warning(
                    "wechat_desktop_bridge_failed type=%s", type(failure).__name__
                )
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = AUTH_FILE.with_name(f"{AUTH_FILE.name}.{os.getpid()}.tmp")
    try:
        protected = win32crypt.CryptProtectData(secret, None, None, None, None, 0)
        temporary.write_bytes(port.to_bytes(2, "big") + protected)
        os.replace(temporary, AUTH_FILE)
    except Exception:
        server.server_close()
        raise
    finally:
        temporary.unlink(missing_ok=True)

    threading.Thread(
        target=server.serve_forever,
        name="wechat-desktop-bridge",
        daemon=True,
    ).start()
    logger.info("wechat_desktop_bridge_ready")


def _request_from_desktop(path: str, timeout: int, limit: int, *, payload: bytes = b"") -> bytes:
    """Call one of the two fixed bridge operations; never send voice text."""
    if path not in {BRIDGE_PATH, OPEN_PATH, HEALTH_PATH, SCREEN_PATH, DESKTOP_CONTROL_PATH}:
        raise ValueError("unknown desktop bridge operation")
    if (payload and path != DESKTOP_CONTROL_PATH) or len(payload) > 16_384:
        raise ValueError("invalid desktop payload")
    try:
        connection = AUTH_FILE.read_bytes()
        port = int.from_bytes(connection[:2], "big")
        if not 1 <= port <= 65535:
            raise ValueError("invalid bridge port")
        secret = win32crypt.CryptUnprotectData(
            connection[2:], None, None, None, 0
        )[1]
        if len(secret) != 32:
            raise ValueError("invalid bridge secret")
        call = request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=payload,
            headers={"X-Jarvis-Bridge": secret.hex(), "Content-Type":"application/json"},
            method="POST",
        )
        opener = request.build_opener(request.ProxyHandler({}), _NoBridgeRedirect())
        with opener.open(call, timeout=timeout) as reply:
            response = reply.read(limit + 1)
    except error.HTTPError as failure:
        if failure.code == 409:
            raise DesktopBridgeUnavailable(
                failure.read(1000).decode("utf-8", errors="replace")
            ) from failure
        raise DesktopBridgeUnavailable("电脑桌面 Jarvis 拒绝了请求") from failure
    except (OSError, ValueError, TypeError) as failure:
        raise DesktopBridgeUnavailable(
            "电脑桌面 Jarvis 未连接，请在电脑上双击 JarvisDesktop.exe 后重试"
        ) from failure
    if len(response) > limit:
        raise DesktopBridgeUnavailable("电脑桌面 Jarvis 返回了过大的响应")
    return response


def request_qr_from_desktop() -> bytes:
    """Request the QR; a stale or missing desktop process fails closed."""
    from wechat_login_qr import LoginQrUnavailable

    try:
        png = _request_from_desktop(BRIDGE_PATH, timeout=22, limit=500_000)
    except DesktopBridgeUnavailable as failure:
        raise LoginQrUnavailable(str(failure)) from failure

    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise LoginQrUnavailable("电脑桌面 Jarvis 返回了无效的二维码")
    return png


def request_open_wechat_from_desktop() -> str:
    """Ask the interactive process to open only the configured WeChat app."""
    response = _request_from_desktop(OPEN_PATH, timeout=13, limit=1000)
    message = response.decode("utf-8", errors="replace")
    if message != "微信已打开":
        raise DesktopBridgeUnavailable("电脑桌面 Jarvis 未确认微信已打开")
    return message


def probe_desktop_bridge() -> bool:
    """Authenticated non-mutating check, never opens WeChat or captures pixels."""
    try:
        value = json.loads(_request_from_desktop(HEALTH_PATH, timeout=1, limit=1000))
        return value.get("connected") is True and value.get("version", 0) >= 2
    except (DesktopBridgeUnavailable, ValueError, AttributeError):
        return False


def request_screen_preview() -> bytes:
    """Only called after a fresh explicit phone request; never runs periodically."""
    png = _request_from_desktop(SCREEN_PATH, timeout=5, limit=1_500_000)
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DesktopBridgeUnavailable("电脑未返回有效的窗口截图")
    return png


def request_desktop_control(payload: dict) -> dict:
    """Typed, authenticated local request; never a command line or model tool call."""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response = _request_from_desktop(DESKTOP_CONTROL_PATH, timeout=55, limit=150_000, payload=raw)
    try:
        value = json.loads(response)
        if not isinstance(value, dict):
            raise ValueError("invalid desktop result")
        return value
    except ValueError:
        raise DesktopBridgeUnavailable("桌面工具返回格式无效") from None
