"""Safe desktop integration for the local Jarvis voice listener.

The listener produces Python events only.  This module maps command text through
the existing allowlisted SkillSystem; speech is never used as a process or shell
argument.  Logs deliberately omit transcript text.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
from typing import Any, Callable

from codex_gateway import is_project_task
from chat_router import chat as assistant_chat
from local_llm import LocalModelUnavailable
from openai_chat import OpenAIAPIUnavailable
from jarvis_skills import SkillSystem
from dynamic_tts import speak_interruptible


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "desktop_config.json"
SKILLS_PATH = BASE_DIR / "skills.json"
LOG_PATH = BASE_DIR / "logs" / "desktop_voice.log"
ERROR_ALREADY_EXISTS = 183
WAKE_ACKNOWLEDGEMENT = "先生，我在。"
COMMAND_ACKNOWLEDGEMENT = "好的，先生。"
SLEEP_ACKNOWLEDGEMENT = "好的，随时待命。"
EXIT_ACKNOWLEDGEMENT = "好的，先生。贾维斯已退出。"
WAKE_REPLY_DELAY_SECONDS = 0.18
SPEECH_PULSE_INTERVAL_SECONDS = 0.16
DESKTOP_CHAT_MAX_CHARS = 200
DESKTOP_REPLY_MAX_CHARS = 600
DESKTOP_HEALTH_PATH = BASE_DIR / "runtime" / "desktop_health.json"
INTERRUPT_PHRASES = frozenset({"停一下", "别说了", "停止播报", "停止回答", "暂停回答", "不要说了"})
DESKTOP_CHAT_BLOCKED_TERMS = (
    "打开", "关闭", "启动", "运行", "执行", "删除", "清空", "卸载", "安装",
    "下载", "上传", "发送", "登录", "登陆", "退出登录", "锁屏", "关机",
    "重启", "修改", "编辑", "创建", "保存", "复制", "移动", "切换账号", "退出",
    "powershell", "cmd", "终端", "命令行", "脚本",
)
DESKTOP_ECHO_PHRASES = (
    "先生我在请吩咐", "先生我在", "我在请吩咐", "我在听", "请吩咐",
    "抱歉聊天暂时不可用请稍后再试", "好的随时待命",
)
DESKTOP_ECHO_FRAGMENTS = frozenset({"先生", "我在", "随时待命"})


def is_desktop_chat_query(normalized: str) -> bool:
    """Only route conversational input to the model; never infer PC actions."""
    compact = "".join(normalized.split())
    if not 2 <= len(compact) <= DESKTOP_CHAT_MAX_CHARS:
        return False
    if compact in DESKTOP_ECHO_FRAGMENTS or any(
        phrase in compact for phrase in DESKTOP_ECHO_PHRASES
    ):
        return False
    if any(term in compact for term in DESKTOP_CHAT_BLOCKED_TERMS):
        return False
    return not is_project_task(compact)


def _speech_phase_level(seconds: float) -> float:
    """A bounded visual speech envelope, not a measurement of microphone audio."""
    # The fixed response has two clauses separated by a short pause.
    if 0.95 <= seconds <= 1.22:
        return 0.12
    wave = abs(math.sin(seconds * 11.0))
    return min(0.88, 0.30 + 0.47 * wave)


def _speak_fixed_reply(
    phrase: str, cancel_event: threading.Event | None = None, delay_seconds: float = 0,
    *, on_start: Callable[[], None] | None = None,
    on_done: Callable[[], None] | None = None,
) -> None:
    """Only pre-approved fixed phrases reach offline SAPI, never speech input."""
    if phrase not in {
        WAKE_ACKNOWLEDGEMENT, COMMAND_ACKNOWLEDGEMENT,
        SLEEP_ACKNOWLEDGEMENT, EXIT_ACKNOWLEDGEMENT,
    }:
        raise ValueError("Jarvis reply is not allowlisted")
    if cancel_event is not None and cancel_event.wait(delay_seconds):
        return
    try:
        speak_interruptible(
            phrase, cancel_event, rate=-1, on_start=on_start, on_done=on_done,
        )
    except Exception:
        # The visual acknowledgement remains usable if Windows speech fails.
        logging.getLogger("jarvis.desktop").warning("wake_voice_unavailable")


def speak_wake_acknowledgement(
    cancel_event: threading.Event | None = None,
    *, on_start: Callable[[], None] | None = None,
    on_done: Callable[[], None] | None = None,
) -> None:
    _speak_fixed_reply(
        WAKE_ACKNOWLEDGEMENT, cancel_event, WAKE_REPLY_DELAY_SECONDS,
        on_start=on_start, on_done=on_done,
    )


def speak_sleep_acknowledgement(cancel_event: threading.Event | None = None) -> None:
    _speak_fixed_reply(SLEEP_ACKNOWLEDGEMENT, cancel_event)


def speak_exit_acknowledgement() -> None:
    _speak_fixed_reply(EXIT_ACKNOWLEDGEMENT)


class DesktopConfigError(RuntimeError):
    """Raised when desktop_config.json is missing or malformed."""


class _SentenceBuffer:
    """Bounded incremental text splitter; no transcript or answer persistence."""

    def __init__(self, limit: int = DESKTOP_REPLY_MAX_CHARS) -> None:
        self.pending = ""
        self.received = 0
        self.limit = limit

    def feed(self, delta: str, *, final: bool = False) -> list[str]:
        remaining = max(0, self.limit - self.received)
        part = delta[:remaining]
        self.received += len(part)
        self.pending += part
        phrases: list[str] = []
        while self.pending:
            boundary = re.search(r"[。！？!?；;\n]", self.pending)
            if boundary is not None and boundary.end() <= 120:
                length = boundary.end()
            elif len(self.pending) >= 120:
                # Commas are a natural breath boundary for long sentences.
                cut = max(self.pending.rfind("，", 0, 120), self.pending.rfind(",", 0, 120))
                length = cut + 1 if cut >= 30 else 120
            elif final:
                length = len(self.pending)
            else:
                break
            phrase, self.pending = self.pending[:length].strip(), self.pending[length:]
            if phrase:
                phrases.append(phrase)
        return phrases


def load_desktop_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as error:
        raise DesktopConfigError(f"Unable to load desktop configuration: {error}") from error

    if not isinstance(config, dict) or config.get("version") != 1:
        raise DesktopConfigError("desktop_config.json must be a version 1 object")
    mutex_name = config.get("mutex_name")
    if not isinstance(mutex_name, str) or not mutex_name.startswith("Local\\"):
        raise DesktopConfigError("mutex_name must be a Local\\ Windows mutex name")

    overlay = config.get("overlay", {})
    logging_config = config.get("logging", {})
    wake_config = config.get("wake_listener", {})
    live_actions = config.get("live_action_allowlist", [])
    if not isinstance(overlay, dict) or not isinstance(logging_config, dict):
        raise DesktopConfigError("overlay and logging must be objects")
    if not isinstance(wake_config, dict):
        raise DesktopConfigError("wake_listener must be an object")
    if not isinstance(live_actions, list) or not all(
        isinstance(action_id, str) and action_id.strip() for action_id in live_actions
    ):
        raise DesktopConfigError("live_action_allowlist must be a list of action ids")

    duration_ms = overlay.get("duration_ms", 4200)
    max_bytes = logging_config.get("max_bytes", 1_048_576)
    backup_count = logging_config.get("backup_count", 5)
    if not isinstance(duration_ms, int) or not 500 <= duration_ms <= 30_000:
        raise DesktopConfigError("overlay.duration_ms must be between 500 and 30000")
    if not isinstance(max_bytes, int) or not 65_536 <= max_bytes <= 10_485_760:
        raise DesktopConfigError("logging.max_bytes is outside the safe range")
    if not isinstance(backup_count, int) or not 1 <= backup_count <= 20:
        raise DesktopConfigError("logging.backup_count must be between 1 and 20")
    return config


def configure_logging(config: dict[str, Any]) -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging_config = config.get("logging", {})
    logger = logging.getLogger("jarvis.desktop")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_PATH,
            maxBytes=logging_config.get("max_bytes", 1_048_576),
            backupCount=logging_config.get("backup_count", 5),
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    wake_logger = logging.getLogger("jarvis.wake")
    wake_logger.setLevel(logging.INFO)
    wake_logger.propagate = False
    for handler in logger.handlers:
        if handler not in wake_logger.handlers:
            wake_logger.addHandler(handler)
    return logger


def normalize_command(text: str, skill_system: SkillSystem) -> str:
    """Normalize locally without importing main.py (which loads Whisper at import)."""

    from opencc import OpenCC

    normalized = OpenCC("t2s").convert(text.strip()).lower()
    for character in skill_system.setting_list("punctuation"):
        normalized = normalized.replace(character, "")
    normalized = " ".join(normalized.split())
    for wake_word in skill_system.setting_list("wake_words"):
        normalized_wake_word = OpenCC("t2s").convert(wake_word).lower().strip()
        if normalized.startswith(normalized_wake_word):
            return normalized[len(normalized_wake_word) :].strip()
    return normalized


class SingleInstance:
    """Process-lifetime Windows mutex; no process enumeration or termination."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.handle: int | None = None

    def acquire(self) -> bool:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError("Unable to create the Jarvis desktop mutex")
        self.handle = int(handle)
        return kernel32.GetLastError() != ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None


class DesktopController:
    def __init__(self, config: dict[str, Any], *, dry_run: bool = False, hologram_startup: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.hologram_startup = hologram_startup
        self.logger = configure_logging(config)
        self.skills = SkillSystem.load(SKILLS_PATH)
        self.live_action_allowlist = frozenset(config.get("live_action_allowlist", []))
        if not self.live_action_allowlist.issubset(self.skills.actions):
            raise DesktopConfigError("live_action_allowlist includes an unknown action")
        self.overlay_config = config.get("overlay", {})
        self._wake_reply_cancel = threading.Event()
        self._chat_reply_cancel = threading.Event()
        self._command_reply_cancel = threading.Event()
        self._sleep_reply_cancel = threading.Event()
        self._chat_active = threading.Event()
        self._wake_state_lock = threading.Lock()
        self._voice_output_lock = threading.Lock()
        self._voice_output_count = 0
        self._voice_output_guard_until = 0.0
        self._voice_command_guard_until = 0.0
        self._stop_event = threading.Event()
        self._health_lock = threading.Lock()
        self._health_stop = threading.Event()
        self._health_started = False
        self._health = {
            "pid": os.getpid(), "timestamp": 0.0, "microphone": "unavailable",
            "model_ready": False, "speaking": False, "last_audio_at": 0.0, "rms": 0,
        }

    def _write_health(self, *, stopped: bool = False) -> None:
        if self.dry_run or not self._health_started:
            return
        with self._health_lock:
            payload = dict(self._health)
            payload["timestamp"] = time.time()
            payload["speaking"] = self._voice_output_count > 0
            if stopped or self._stop_event.is_set() or self._health_stop.is_set():
                payload["microphone"] = "stopped"
                payload["model_ready"] = False
                payload["speaking"] = False
            temporary = DESKTOP_HEALTH_PATH.with_name(
                DESKTOP_HEALTH_PATH.name + f".{os.getpid()}.tmp"
            )
            try:
                DESKTOP_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(json.dumps(payload), encoding="utf-8")
                os.replace(temporary, DESKTOP_HEALTH_PATH)
            except OSError:
                self.logger.warning("desktop_health_write_unavailable")
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _health_worker(self) -> None:
        while not self._health_stop.wait(2.0):
            self._write_health()

    def on_audio_level(self, rms: int) -> None:
        with self._health_lock:
            self._health["last_audio_at"] = time.time()
            self._health["rms"] = max(0, min(32768, int(rms)))

    def _interruption_enabled(self) -> bool:
        with self._voice_output_lock:
            return self._voice_output_count > 0 or self._chat_active.is_set()

    def on_interrupt(self, _event: Any = None) -> None:
        """Cancel speech/chat only; never reinterpret this as a PC operation."""
        with self._wake_state_lock:
            self._wake_reply_cancel.set()
            self._chat_reply_cancel.set()
            self._command_reply_cancel.set()
            self._sleep_reply_cancel.set()
            self._chat_active.clear()
        self.logger.info("desktop_reply_interrupted")
        self._show_overlay("standby", "已停止播报，随时待命", duration_ms=3000, hold_full=False)

    def _begin_voice_output(self) -> None:
        with self._voice_output_lock:
            self._voice_output_count += 1

    def _end_voice_output(self) -> None:
        with self._voice_output_lock:
            self._voice_output_count = max(0, self._voice_output_count - 1)
            now = time.monotonic()
            self._voice_output_guard_until = now + 3.0
            self._voice_command_guard_until = now + 0.7

    def _ignore_wake_audio(self) -> bool:
        with self._voice_output_lock:
            return self._voice_output_count > 0 or time.monotonic() < self._voice_output_guard_until

    def _ignore_command_audio(self) -> bool:
        with self._voice_output_lock:
            return self._voice_output_count > 0 or time.monotonic() < self._voice_command_guard_until

    def _speak_sleep_with_wake_guard(self, cancel: threading.Event | None = None) -> None:
        cancel = cancel if cancel is not None else self._sleep_reply_cancel
        self._begin_voice_output()
        try:
            speak_sleep_acknowledgement(cancel)
        finally:
            self._end_voice_output()
            if not cancel.is_set():
                self._show_overlay("standby", "待命中", duration_ms=1500, dismiss=True)

    def _show_overlay(
        self, status: str, message: str, duration_ms: int | None = None,
        *, entrance: bool = False, dismiss: bool = False,
        hold_full: bool | None = None,
        voice_level: float | None = None,
    ) -> None:
        if not self.overlay_config.get("enabled", True):
            return
        try:
            from jarvis_overlay import show_overlay

            options: dict[str, Any] = dict(
                status=status,
                message=message,
                duration_ms=duration_ms or self.overlay_config.get("duration_ms", 4200),
                entrance=entrance,
                dismiss=dismiss,
            )
            if hold_full is not None:
                options["hold_full"] = hold_full
            if voice_level is not None:
                options["voice_level"] = voice_level
            show_overlay(**options)
        except Exception as error:
            self.logger.warning("overlay_failed type=%s", type(error).__name__)

    def on_wake(self, event: Any) -> None:
        wake_received_at = time.perf_counter()
        event_id = getattr(event, "event_id", "unknown")
        self.logger.info("wake event_id=%s", event_id)
        with self._wake_state_lock:
            self._wake_reply_cancel.set()
            self._chat_reply_cancel.set()
            self._command_reply_cancel.set()
            self._sleep_reply_cancel.set()
            self._chat_active.clear()
            self._chat_reply_cancel = threading.Event()
            self._wake_reply_cancel = threading.Event()
            reply_cancel = self._wake_reply_cancel
            self._show_overlay(
                "listening", "我在听", duration_ms=30000,
                entrance=True, hold_full=True,
            )
        self.logger.info(
            "wake_latency overlay_dispatch_ms=%.1f",
            (time.perf_counter() - wake_received_at) * 1000,
        )
        if getattr(event, "inline_command", False):
            self.logger.info("wake_inline_command")
            return
        speech_done = threading.Event()
        speech_started_at: list[float] = []

        def update_speech_animation() -> None:
            while not speech_done.wait(SPEECH_PULSE_INTERVAL_SECONDS):
                with self._wake_state_lock:
                    if reply_cancel.is_set() or speech_done.is_set():
                        return
                    elapsed = time.perf_counter() - speech_started_at[0]
                    self._show_overlay(
                        "speaking", WAKE_ACKNOWLEDGEMENT,
                        voice_level=_speech_phase_level(elapsed),
                    )

        def on_speech_start() -> None:
            with self._wake_state_lock:
                if reply_cancel.is_set():
                    return
                self._begin_voice_output()
                speech_started_at.append(time.perf_counter())
                self.logger.info(
                    "wake_latency audio_start_ms=%.1f",
                    (speech_started_at[0] - wake_received_at) * 1000,
                )
                self._show_overlay("speaking", WAKE_ACKNOWLEDGEMENT, voice_level=0.30)
            threading.Thread(
                target=update_speech_animation,
                name="JarvisSpeechAnimation",
                daemon=True,
            ).start()

        def on_speech_done() -> None:
            with self._wake_state_lock:
                speech_done.set()
                if speech_started_at:
                    self._end_voice_output()
                    self.logger.info(
                        "wake_latency audio_duration_ms=%.1f",
                        (time.perf_counter() - speech_started_at[0]) * 1000,
                    )
                if not reply_cancel.is_set():
                    self._show_overlay("listening", "我在听", duration_ms=30000)

        threading.Thread(
            target=speak_wake_acknowledgement,
            args=(reply_cancel,),
            kwargs={"on_start": on_speech_start, "on_done": on_speech_done},
            name="JarvisWakeReply",
            daemon=True,
        ).start()

    def on_sleep(self, event: Any) -> None:
        timed_out = getattr(event, "reason", "voice") == "timeout"
        with self._wake_state_lock:
            self._wake_reply_cancel.set()
            self._chat_reply_cancel.set()
            self._command_reply_cancel.set()
            self._sleep_reply_cancel.set()
            self._sleep_reply_cancel = threading.Event()
            sleep_cancel = self._sleep_reply_cancel
            self._chat_active.clear()
            self.logger.info("desktop_sleep")
            if timed_out:
                self._show_overlay("standby", "待命中", duration_ms=1500, dismiss=True)
            else:
                self._show_overlay("speaking", SLEEP_ACKNOWLEDGEMENT, voice_level=0.5)
        if not timed_out:
            threading.Thread(
                target=self._speak_sleep_with_wake_guard,
                args=(sleep_cancel,),
                name="JarvisSleepReply",
                daemon=True,
            ).start()

    def on_exit(self, event: Any) -> None:
        """Stop only the desktop listener and its own HUD; leave phone API alive."""
        del event
        with self._wake_state_lock:
            self._wake_reply_cancel.set()
            self._chat_reply_cancel.set()
            self._command_reply_cancel.set()
            self._sleep_reply_cancel.set()
            self._chat_active.clear()
            self.logger.info("desktop_exit_by_voice")
            if not self.dry_run:
                self._show_overlay("speaking", EXIT_ACKNOWLEDGEMENT, voice_level=0.5)
            self._stop_event.set()
            self._write_health(stopped=True)
        try:
            if not self.dry_run:
                speak_exit_acknowledgement()
        finally:
            self._show_overlay("standby", "正在退出", duration_ms=1500, dismiss=True)
            try:
                from jarvis_overlay import close_overlay

                close_overlay()
            except Exception as error:
                self.logger.warning("overlay_close_failed type=%s", type(error).__name__)

    def _start_command_acknowledgement(self) -> threading.Event:
        done = threading.Event()
        self._command_reply_cancel.set()
        self._command_reply_cancel = threading.Event()
        cancelled = self._command_reply_cancel
        self._begin_voice_output()

        def speak_then_collapse() -> None:
            self._show_overlay(
                "speaking", COMMAND_ACKNOWLEDGEMENT,
                duration_ms=10000, voice_level=0.52,
            )
            try:
                _speak_fixed_reply(COMMAND_ACKNOWLEDGEMENT, cancelled)
            finally:
                self._end_voice_output()
                if not cancelled.is_set():
                    self._show_overlay(
                        "success", "随时待命", duration_ms=3000, hold_full=False,
                    )
                done.set()

        threading.Thread(
            target=speak_then_collapse,
            name="JarvisCommandAcknowledgement",
            daemon=True,
        ).start()
        return done

    def _respond_to_chat(
        self, text: str, cancel: threading.Event,
        after_ack: threading.Event | None = None,
    ) -> None:
        # Generate and speak independently: the first finished sentence need
        # not wait for the rest of a long model answer.  The queue and splitter
        # are bounded and only hold text in memory for this single reply.
        phrases: queue.Queue[str | None] = queue.Queue(maxsize=12)
        splitter = _SentenceBuffer()
        had_delta = False

        def enqueue(phrase: str | None) -> None:
            while not cancel.is_set():
                try:
                    phrases.put(phrase, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def on_delta(delta: str) -> None:
            nonlocal had_delta
            if cancel.is_set() or not isinstance(delta, str):
                return
            had_delta = had_delta or bool(delta)
            for phrase in splitter.feed(delta):
                enqueue(phrase)

        def generate() -> None:
            try:
                answer = assistant_chat(
                    text, session_id="desktop", on_delta=on_delta, cancel_event=cancel,
                )
                if not had_delta and not cancel.is_set():
                    on_delta(answer)
                for phrase in splitter.feed("", final=True):
                    enqueue(phrase)
            except (LocalModelUnavailable, OpenAIAPIUnavailable) as error:
                if not cancel.is_set():
                    self.logger.warning("desktop_chat_unavailable reason=%s", error.reason)
                    enqueue("抱歉，聊天暂时不可用，请稍后再试。")
            except Exception as error:
                if not cancel.is_set():
                    self.logger.warning("desktop_chat_failed type=%s", type(error).__name__)
                    enqueue("抱歉，聊天暂时不可用，请稍后再试。")
            finally:
                enqueue(None)

        worker = threading.Thread(target=generate, name="JarvisChatGenerate", daemon=True)
        worker.start()
        try:
            if after_ack is not None:
                deadline = time.monotonic() + 10.0
                while not cancel.is_set() and not after_ack.wait(0.05):
                    if time.monotonic() >= deadline:
                        break
            while not cancel.is_set():
                try:
                    phrase = phrases.get(timeout=0.1)
                except queue.Empty:
                    if not worker.is_alive():
                        break
                    continue
                if phrase is None:
                    break
                self._show_overlay("speaking", phrase[:80], duration_ms=12000, voice_level=0.45)
                self._begin_voice_output()
                try:
                    speak_interruptible(
                        phrase, cancel, rate=0,
                        on_progress=lambda elapsed: self._show_overlay(
                            "speaking", phrase[:80], duration_ms=12000,
                            voice_level=_speech_phase_level(elapsed),
                        ) if not cancel.is_set() else None,
                    )
                except Exception as error:
                    self.logger.warning("desktop_chat_voice_failed type=%s", type(error).__name__)
                finally:
                    self._end_voice_output()
            if not cancel.is_set():
                self._show_overlay("success", "随时待命", duration_ms=3000)
        finally:
            if self._chat_reply_cancel is cancel:
                self._chat_active.clear()

    def on_microphone_status(self, status: Any) -> None:
        """Show safe device health hints without exposing audio or transcripts."""
        state = getattr(status, "state", "")
        if state not in {"ready", "reconnecting", "recovered", "silent_input"}:
            return
        with self._health_lock:
            self._health["microphone"] = {
                "ready": "ready", "recovered": "ready",
                "reconnecting": "recovering", "silent_input": "unavailable",
            }[state]
        self.logger.info("microphone_status state=%s", state)
        if state == "reconnecting":
            self._show_overlay("error", "麦克风连接中，正在自动恢复", duration_ms=4000)
        elif state == "silent_input":
            self._show_overlay("error", "麦克风没有输入，请检查耳机静音", duration_ms=4000)
        elif state == "recovered" or getattr(status, "reason", "") == "signal_restored":
            self._show_overlay("success", "麦克风已恢复，语音监听正常", duration_ms=2500)

    def on_command(self, event: Any) -> dict[str, Any]:
        text = getattr(event, "text", "")
        if not isinstance(text, str):
            text = ""
        normalized = normalize_command(text, self.skills)
        if normalized in INTERRUPT_PHRASES:
            self.on_interrupt()
            return {"success": True, "recognized": True, "actions": [], "message": "已停止播报"}
        with self._wake_state_lock:
            self._wake_reply_cancel.set()
            self._command_reply_cancel.set()
            self._chat_reply_cancel.set()
            self._chat_active.clear()
            self._show_overlay("processing", "正在处理")
        planned, _ = self.skills.plan(normalized)
        if not planned and is_project_task(normalized):
            self.logger.info("desktop_project_task_requires_phone_confirmation")
            self._show_overlay("error", "项目任务请在手机端确认", hold_full=False)
            return {
                "success": False, "recognized": True, "actions": [],
                "message": "项目任务请在手机端确认",
            }
        if not planned and not self.dry_run and is_desktop_chat_query(normalized):
            with self._wake_state_lock:
                self._chat_reply_cancel.set()
                self._chat_reply_cancel = threading.Event()
                chat_cancel = self._chat_reply_cancel
                self._chat_active.set()
            self.logger.info("desktop_chat_requested")
            ack_done = self._start_command_acknowledgement()
            threading.Thread(
                target=self._respond_to_chat,
                args=(text, chat_cancel, ack_done),
                name="JarvisDesktopChat",
                daemon=True,
            ).start()
            return {"success": True, "recognized": True, "actions": [], "message": "正在回答"}
        if not self.dry_run and planned and any(
            item.action_id not in self.live_action_allowlist for item in planned
        ):
            self.logger.info("command_blocked action_not_enabled")
            result = {
                "success": False,
                "recognized": True,
                "actions": [],
                "message": "该操作尚未启用",
            }
        else:
            result = self.skills.run(
                original_text=text,
                normalized_command=normalized,
                dry_run=self.dry_run,
            )
        actions = result.get("actions", [])
        self.logger.info(
            "command recognized=%s success=%s actions=%s dry_run=%s",
            bool(result.get("recognized")),
            bool(result.get("success")),
            ",".join(actions) if actions else "none",
            self.dry_run,
        )
        if result.get("success"):
            if self.dry_run:
                self._show_overlay("success", str(result.get("message", "完成")), hold_full=False)
            else:
                self._start_command_acknowledgement()
        elif result.get("recognized"):
            self._show_overlay("error", "执行失败", hold_full=False)
        else:
            self._show_overlay("error", "这个指令还不在白名单中", hold_full=False)
        return result

    def run_forever(self) -> None:
        from jarvis_wake_listener import WakeConfig, WakeWordListener

        wake_values = self.config.get("wake_listener", {})
        wake_config = WakeConfig(**wake_values)
        listener = WakeWordListener(
            wake_config,
            on_wake=self.on_wake,
            on_command=self.on_command,
            on_sleep=self.on_sleep,
            on_exit=self.on_exit,
            on_microphone_status=self.on_microphone_status,
            on_audio_level=self.on_audio_level,
            on_interrupt=self.on_interrupt,
            interruption_enabled=self._interruption_enabled,
            ignore_wake_audio=self._ignore_wake_audio,
            ignore_command_audio=self._ignore_command_audio,
        )
        self.logger.info("desktop_controller_loading_model dry_run=%s", self.dry_run)
        listener.prepare()
        with self._health_lock:
            self._health["model_ready"] = True
        self._health_started = True
        self._health_stop.clear()
        self._write_health()
        threading.Thread(target=self._health_worker, name="JarvisHealthHeartbeat", daemon=True).start()
        if not self.dry_run:
            try:
                from wechat_desktop_bridge import start_desktop_bridge

                start_desktop_bridge(
                    self.skills.actions["open_wechat"]["target"], self.logger
                )
            except Exception as error:
                self.logger.warning(
                    "wechat_desktop_bridge_start_failed type=%s", type(error).__name__
                )
        self.logger.info("desktop_controller_started dry_run=%s", self.dry_run)
        self._show_startup_overlay()
        try:
            listener.run_forever(stop_event=self._stop_event)
        finally:
            self._health_stop.set()
            self._write_health(stopped=True)

    def _show_startup_overlay(self) -> None:
        if self.hologram_startup:
            # The EXE owns the text-free assembly animation. Initialize only
            # the tucked-away top HUD so two entrance animations do not overlap.
            self._show_overlay("standby", "", duration_ms=1500, dismiss=True)
        else:
            self._show_overlay("success", "语音监听已就绪", duration_ms=10000)


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis desktop voice controller")
    parser.add_argument("--dry-run", action="store_true", help="Never execute allowlisted actions")
    parser.add_argument("--check", action="store_true", help="Validate configuration and exit")
    parser.add_argument("--hologram-startup", action="store_true", help="Use the EXE's text-free startup animation")
    args = parser.parse_args()

    config = load_desktop_config()
    controller = DesktopController(config, dry_run=args.dry_run, hologram_startup=args.hologram_startup)
    if args.check:
        controller.logger.info("configuration_check_passed")
        return 0

    instance = SingleInstance(config["mutex_name"])
    if not instance.acquire():
        controller.logger.info("duplicate_desktop_start_skipped")
        instance.close()
        return 0
    try:
        controller.run_forever()
    except KeyboardInterrupt:
        controller.logger.info("desktop_controller_stopped")
        return 0
    except Exception as error:
        controller.logger.exception("desktop_controller_failed type=%s", type(error).__name__)
        return 1
    finally:
        instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
