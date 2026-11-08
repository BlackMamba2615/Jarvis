"""Allowlisted agent-desktop adapter. UI data stays local; speech never enters argv.

The CLI supplies observations, not instructions. No shell, screenshots, model
tool loop, force flags, coordinate guessing or automatic mutation retries.
"""
from __future__ import annotations

import atexit
from collections import Counter
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time

BASE_DIR = Path(__file__).resolve().parent
OPERATIONS = frozenset({"windows", "inspect", "minimize", "maximize", "restore",
                        "click", "set-value", "check", "uncheck", "expand", "collapse",
                        "scroll-up", "scroll-down", "confirm", "cancel"})
ELEMENT_OPS = frozenset({"click", "set-value", "check", "uncheck", "expand", "collapse",
                         "scroll-up", "scroll-down"})
WINDOW_OPS = frozenset({"minimize", "maximize", "restore"})
TTL = 45
PROTECTED_APPS = frozenset({"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe",
    "codex.exe", "chatgpt.exe", "weixin.exe", "wechat.exe", "wechatappex.exe",
    "keepass.exe", "keepassxc.exe", "1password.exe", "bitwarden.exe", "consent.exe",
    "regedit.exe", "taskmgr.exe", "mmc.exe"})
SENSITIVE = re.compile(r"\.env|password|credential|api.?key|secret|sk-proj-|密码|密钥|口令", re.I)
REF = re.compile(r"@[a-zA-Z0-9_-]+:e\d+\Z")


class DesktopControlUnavailable(RuntimeError):
    pass


def parse_desktop_command(text: str) -> dict | None:
    """Small deterministic grammar. Unknown targets fail locally, never go to CLI."""
    command = text.strip().lower()
    command = re.sub(r"^(?:请|帮我|替我|给我)+", "", command).strip()
    if command in {"查看电脑窗口", "查看桌面窗口", "列出电脑窗口", "列出桌面窗口"}:
        return {"operation": "windows"}
    if command in {"确认桌面操作", "取消桌面操作"}:
        return {"operation": "confirm" if command.startswith("确认") else "cancel"}
    match = re.fullmatch(r"(最小化|最大化|还原|恢复)(.{1,60}?)(?:的?窗口)?", command)
    if match:
        return {"operation": {"最小化":"minimize", "最大化":"maximize", "还原":"restore",
                              "恢复":"restore"}[match[1]], "app": match[2].strip()}
    match = re.fullmatch(r"(?:查看|检查)(.{1,60}?)(?:的)?(?:界面|控件|窗口)", command)
    if match:
        return {"operation": "inspect", "app": match[1].strip()}
    match = re.fullmatch(r"点击(.{1,60}?)(?:里的|中的|的)(.{1,100})", command)
    if match:
        return {"operation": "click", "app": match[1].strip(), "control": match[2].strip()}
    return None


def validate_apps(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict) or not value or len(value) > 30:
        raise ValueError("桌面应用白名单无效")
    result = {}
    for alias, apps in value.items():
        if not isinstance(alias, str) or not 1 <= len(alias) <= 40 or not isinstance(apps, list) or not apps:
            raise ValueError("桌面应用别名无效")
        if any(not isinstance(app, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+\.exe", app)
               or app.casefold() in PROTECTED_APPS for app in apps):
            raise ValueError("桌面应用只允许普通应用的 EXE 名称")
        result[alias.casefold()] = [app.casefold() for app in apps]
    return result


class DesktopAutomation:
    def __init__(self, skills, *, runner=None, clock=time.monotonic):
        self.skills = skills
        self.apps = validate_apps(skills.settings.get("desktop_apps"))
        self.allowed_apps = {app for values in self.apps.values() for app in values}
        self._runner = runner
        self._clock = clock
        self._lock = threading.RLock()
        self._views = {}
        self._pending = {}
        self._home = None

    def close(self):
        with self._lock:
            self._views.clear()
            self._pending.clear()
            self._remove_home()

    def _remove_home(self):
        if self._home:
            target = Path(self._home).resolve()
            if target.parent == Path(tempfile.gettempdir()).resolve() and target.name.startswith("jarvis-desktop-"):
                shutil.rmtree(target, ignore_errors=True)
            self._home = None

    def _private_home(self):
        if os.name != "nt":
            raise DesktopControlUnavailable("此桌面技能仅支持 Windows")
        self._home = tempfile.mkdtemp(prefix="jarvis-desktop-")
        try:
            import win32api
            import win32con
            import win32security
            token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
            try:
                sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            finally:
                token.Close()
            acl = win32security.ACL()
            acl.AddAccessAllowedAceEx(win32security.ACL_REVISION,
                win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE, 0x1F01FF, sid)
            win32security.SetNamedSecurityInfo(self._home, win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, acl, None)
        except Exception:
            self._remove_home()
            raise DesktopControlUnavailable("无法建立私有桌面状态目录，已停止操作") from None
        self._schedule_expiry()

    def _call(self, args: list[str], *, value: str | None = None) -> dict:
        if self._runner:
            return self._runner(args, value=value)
        candidates = (BASE_DIR / "tools" / "agent-desktop.exe",
                      Path(r"D:\agent-desktop\agent-desktop.exe"))
        from jarvis_settings import is_redirect
        binary = next((path for path in candidates if path.is_file()
                       and not any(is_redirect(p) for p in (path, *path.parents))), None)
        if binary is None:
            raise DesktopControlUnavailable("未安装桌面工具，请按桌面控制说明安装 agent-desktop")
        if self._home is None:
            self._private_home()
        # Do not forward Jarvis/cloud credentials or inherited tracing options.
        env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "USERPROFILE",
                "LOCALAPPDATA", "APPDATA", "TEMP", "TMP") if key in os.environ}
        env["AGENT_DESKTOP_HOME"] = self._home
        try:
            reply = subprocess.run([str(binary), *args], input=None if value is None else value.encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False, env=env,
                timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if len(reply.stdout) > 2_000_000:
                raise ValueError("oversized reply")
            envelope = json.loads(reply.stdout.decode("utf-8"))
            if not isinstance(envelope, dict) or not isinstance(envelope.get("ok"), bool):
                raise ValueError("invalid envelope")
            return envelope
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
            # A timed-out mutation may already have landed. Never auto-retry it.
            raise DesktopControlUnavailable("桌面工具未返回可靠结果；请先查看电脑，勿重复执行") from None

    def _read(self, args):
        envelope = self._call(args)
        if not envelope.get("ok"):
            code = envelope.get("error", {}).get("code", "unavailable")
            hints = {"APP_NOT_FOUND":"应用尚未打开", "WINDOW_NOT_FOUND":"窗口已关闭或发生变化",
                     "AMBIGUOUS_TARGET":"存在多个相同目标，请在手机上选择具体窗口或控件",
                     "POLICY_DENIED":"该操作需要额外桌面权限，当前没有执行"}
            raise DesktopControlUnavailable(hints.get(code, "桌面读取失败，请重新查看窗口"))
        return envelope.get("data")

    @staticmethod
    def _fingerprint(window):
        return tuple(window.get(key) for key in ("id", "pid", "process_instance", "app_name", "title"))

    def _inventory(self):
        data = self._read(["list-windows", "--timeout-ms", "5000"])
        if not isinstance(data, list):
            raise DesktopControlUnavailable("窗口清单无效")
        return [w for w in data if isinstance(w, dict)
                and str(w.get("app_name", "")).casefold() in self.allowed_apps
                and re.fullmatch(r"w-\d+", str(w.get("id", "")))
                and isinstance(w.get("pid"), int) and w.get("process_instance")
                and not SENSITIVE.search(str(w.get("title", ""))) and w.get("visible") is True][:50]

    @staticmethod
    def _public_window(window):
        return {"id": window["id"], "app": window["app_name"],
                "title": str(window.get("title", ""))[:160], "minimized": bool(window.get("minimized"))}

    def _target(self, payload):
        windows = self._inventory()
        if payload.get("window_id"):
            matches = [w for w in windows if w["id"] == payload["window_id"]]
        else:
            apps = self.apps.get(str(payload.get("app", "")).casefold(), [])
            matches = [w for w in windows if w["app_name"].casefold() in apps]
        if len(matches) != 1:
            raise DesktopControlUnavailable("未找到唯一的白名单窗口；请先打开应用，或在手机上选择具体窗口")
        return matches[0]

    @staticmethod
    def _identity(node):
        return json.dumps([node.get("role"), node.get("name"), node.get("native_id")], sort_keys=True, ensure_ascii=False)

    def _snapshot(self, window):
        if window.get("minimized"):
            raise DesktopControlUnavailable("窗口已最小化，请先还原窗口再查看控件")
        data = self._read(["snapshot", "--window-id", window["id"], "--compact", "--timeout-ms", "7000"])
        if not isinstance(data, dict) or data.get("complete") is not True or data.get("window", {}).get("id") != window["id"]:
            raise DesktopControlUnavailable("控件读取不完整，请稍后重新查看")
        nodes = []
        def visit(node):
            if not isinstance(node, dict):
                return
            states = node.get("states", [])
            name = node.get("name", "")
            if not isinstance(name, str) or SENSITIVE.search(name) or any(s in states for s in ("protected", "password", "secure")):
                return
            if REF.fullmatch(str(node.get("ref_id", ""))) and name and len(name) <= 140:
                nodes.append({key: node.get(key) for key in ("role", "name", "native_id", "ref_id", "available_actions")})
            for child in node.get("children", []):
                visit(child)
        visit(data.get("tree"))
        counts = Counter(self._identity(n) for n in nodes)
        return [n for n in nodes if counts[self._identity(n)] == 1][:100]

    def _authorize(self, operation):
        for action_id, definition in self.skills.actions.items():
            if definition.get("type") == "desktop_control" and definition.get("operation") == operation:
                return action_id
        raise DesktopControlUnavailable("该桌面操作不在 skills.json 白名单中")

    def _expire(self):
        now = self._clock()
        for mapping in (self._views, self._pending):
            for key in list(mapping):
                if mapping[key]["expires"] <= now:
                    del mapping[key]
        if not self._views and not self._pending and self._home:
            self._remove_home()

    def _schedule_expiry(self):
        def expire():
            with self._lock:
                self._expire()
        timer = threading.Timer(TTL + 0.1, expire)
        timer.daemon = True
        timer.start()

    def _inspect(self, owner, window):
        nodes = self._snapshot(window)
        controls = {secrets.token_urlsafe(12): node for node in nodes}
        self._views[owner] = {"expires": self._clock() + TTL, "window": window, "controls": controls}
        self._schedule_expiry()
        return {"success": True, "message": "已读取窗口控件；仅在本地使用，不会发送给大模型", "local_only": True,
                "window": self._public_window(window), "expires_seconds": TTL,
                "controls": [{"id": key, "name": n["name"], "role": n["role"]} for key, n in controls.items()]}

    def handle(self, payload: dict, *, owner: str) -> dict:
        if not isinstance(payload, dict) or owner not in {"mobile", "desktop"}:
            raise DesktopControlUnavailable("桌面请求无效")
        if set(payload) - {"operation", "window_id", "app", "control", "control_id", "value", "token"}:
            raise DesktopControlUnavailable("不接受任意桌面命令或参数")
        if any(not isinstance(value, str) or len(value) > (2000 if key == "value" else 200)
               or "\0" in value for key, value in payload.items()):
            raise DesktopControlUnavailable("桌面参数无效或过长")
        operation = payload.get("operation")
        action_id = self._authorize(operation)
        with self._lock:
            self._expire()
            if operation == "cancel":
                self._pending.pop(owner, None)
                self._views.pop(owner, None)
                return {"success": True, "message": "已取消桌面操作", "actions": [action_id]}
            if operation == "confirm":
                return self._confirm(payload, owner, action_id)
            self._pending.pop(owner, None)  # A new request invalidates older approval.
            if operation == "windows":
                self._views.pop(owner, None)
                windows = [self._public_window(w) for w in self._inventory()]
                return {"success": True, "message": f"找到 {len(windows)} 个白名单窗口，请在手机控制中心查看",
                        "windows": windows, "actions": [action_id], "local_only": True}
            window = self._target(payload)
            if operation in WINDOW_OPS:
                self._views.pop(owner, None)
                envelope = self._call([operation, "--window-id", window["id"]])
                return self._outcome(envelope, window, action_id)
            if operation == "inspect":
                return {**self._inspect(owner, window), "actions": [action_id]}
            if operation not in ELEMENT_OPS:
                raise DesktopControlUnavailable("不支持此桌面操作")
            view = self._views.get(owner)
            if "control_id" in payload:
                if view is None or self._fingerprint(view["window"]) != self._fingerprint(window):
                    raise DesktopControlUnavailable("控件列表已过期或窗口已变化，请重新查看")
                node = view["controls"].get(payload["control_id"])
                if node is None:
                    raise DesktopControlUnavailable("控件不在当前列表中")
            else:
                nodes = self._snapshot(window)
                matches = [n for n in nodes if n["name"].casefold() == payload.get("control", "").casefold()]
                if len(matches) != 1:
                    raise DesktopControlUnavailable("没有唯一匹配的控件，请在手机上查看并选择")
                node = matches[0]
            if operation == "set-value" and (node.get("role") not in {"textfield", "textarea", "edit", "combobox"}
                    or not payload.get("value") or SENSITIVE.search(payload["value"])):
                raise DesktopControlUnavailable("仅支持向普通文本框填写非敏感文字")
            if operation in {"check", "uncheck"} and node.get("role") not in {"checkbox", "switch"}:
                raise DesktopControlUnavailable("请先选择复选框或开关控件")
            token = secrets.token_urlsafe(24)
            self._pending[owner] = {"expires": self._clock() + TTL, "token": token, "window": window,
                                    "node": node, "operation": operation,
                                    "value": payload.get("value") if operation == "set-value" else None}
            self._schedule_expiry()
            # Do not return entered text; do not embed UI labels in executable syntax.
            return {"success": False, "pending_confirmation": True, "desktop_token": token,
                    "expires_seconds": TTL, "actions": [], "local_only": True,
                    "message": f"目标：{window['app_name']}的{node['name']}。" +
                               ("请说贾维斯确认桌面操作" if owner == "desktop" else "请在手机核对后确认") + "；45 秒内有效",
                    "preview": {"operation": operation, "window": self._public_window(window),
                                "control": node["name"], "role": node["role"]}}

    def _confirm(self, payload, owner, action_id):
        pending = self._pending.get(owner)
        # Phone always confirms the exact displayed token; voice confirms its own single pending action.
        token = payload.get("token", "")
        if pending is None or (owner == "mobile" and not token) or (token and not secrets.compare_digest(token, pending["token"])):
            raise DesktopControlUnavailable("没有有效的待确认操作，请重新选择")
        self._pending.pop(owner, None)
        self._views.pop(owner, None)
        self._authorize(pending["operation"])
        window = self._target({"window_id": pending["window"]["id"]})
        if self._fingerprint(window) != self._fingerprint(pending["window"]):
            raise DesktopControlUnavailable("窗口身份或标题发生变化，请重新查看并确认")
        nodes = self._snapshot(window)
        matches = [n for n in nodes if self._identity(n) == self._identity(pending["node"])]
        if len(matches) != 1:
            raise DesktopControlUnavailable("控件已变化或存在同名目标，请重新查看")
        node = matches[0]
        operation = pending["operation"]
        if operation in {"check", "uncheck"}:
            state = self._read(["is", node["ref_id"], "--property", "checked"])
            if not isinstance(state, dict) or state.get("applicable") is False or not isinstance(state.get("result"), bool):
                raise DesktopControlUnavailable("无法确认该控件的勾选状态，没有执行")
            if state["result"] == (operation == "check"):
                return {"success": True, "recognized": True, "local_only": True, "actions": [action_id],
                        "delivery": "not_needed", "readback": True, "message": "控件已处于所需状态，无需重复操作"}
        args = ["scroll" if operation.startswith("scroll-") else operation, node["ref_id"]]
        if operation.startswith("scroll-"):
            args += ["--direction", operation[7:], "--amount", "2"]
        if operation == "set-value":
            args += ["--stdin"]
        envelope = self._call(args, value=pending["value"] if operation == "set-value" else None)
        return self._outcome(envelope, window, action_id)

    def _outcome(self, envelope, window, action_id):
        data = envelope.get("data", {}) if envelope.get("ok") else envelope.get("error", {})
        delivery = data.get("disposition", {}).get("delivery", "unknown")
        # Independent observation, even after uncertain delivery. Never repeat a mutation.
        observed = False
        try:
            current = self._target({"window_id": window["id"]})
            if not current.get("minimized"):
                self._snapshot(current)
            observed = True
        except DesktopControlUnavailable:
            pass
        verified = envelope.get("ok") is True and delivery == "delivered_verified"
        return {"success": bool(verified), "recognized": True, "local_only": True,
                "actions": [action_id] if envelope.get("ok") else [], "delivery": delivery,
                "readback": observed, "message": "桌面操作已完成并核实" if verified else
                "操作结果尚未完全核实，请查看电脑；没有自动重复执行"}


_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def desktop_engine():
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            from jarvis_skills import SkillSystem
            _ENGINE = DesktopAutomation(SkillSystem.load(BASE_DIR / "skills.json"))
            atexit.register(_ENGINE.close)
        return _ENGINE


def handle_desktop_request(payload, *, owner="desktop"):
    result = desktop_engine().handle(payload, owner=owner)
    return {"recognized": True, **result}


def discard_desktop_pending():
    """Sleep/exit drops only voice approval, without opening a tool or microphone."""
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            with _ENGINE._lock:
                _ENGINE._pending.pop("desktop", None)
                _ENGINE._views.pop("desktop", None)
