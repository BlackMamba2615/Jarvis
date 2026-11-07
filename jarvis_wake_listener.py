"""Always-on, offline wake-word listener for the Windows desktop Jarvis.

Audio stays on this computer.  Recognized text is returned only through Python
callbacks; it is never interpolated into a command line or passed to a shell.
"""

from __future__ import annotations

import argparse
import ctypes
from difflib import SequenceMatcher
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Protocol

import numpy as np
from pypinyin import lazy_pinyin
from faster_whisper import WhisperModel
from opencc import OpenCC
from jarvis_skills import SkillSystem


LOGGER = logging.getLogger("jarvis.wake")
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SKILLS_PATH = BASE_DIR / "skills.json"
DEFAULT_EVENT_PATH = BASE_DIR / "runtime" / "wake_event.json"
INTERRUPT_PHRASES = frozenset({"停一下", "别说了", "停止播报", "停止回答", "暂停回答", "不要说了"})


class AudioSource(Protocol):
    def chunks(self, stop_event: threading.Event) -> Iterator[bytes]: ...


class Transcriber(Protocol):
    def transcribe(self, pcm: bytes) -> str: ...


@dataclass(frozen=True)
class WakeConfig:
    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2
    chunk_seconds: float = 0.1
    command_timeout_seconds: float = 30.0
    cooldown_seconds: float = 1.5
    minimum_rms: int = 120
    microphone_device_id: int = -1
    # Reopening WAVE_MAPPER periodically follows a newly selected Windows
    # default input device even if the old endpoint keeps returning silence.
    capture_session_seconds: float = 90.0
    model_name: str = "small"
    asr_device: str = "auto"
    event_path: Path = DEFAULT_EVENT_PATH
    skills_path: Path = DEFAULT_SKILLS_PATH

    def __post_init__(self) -> None:
        if self.asr_device not in {"auto", "cpu", "cuda"}:
            raise ValueError("asr_device must be auto, cpu or cuda")


@dataclass(frozen=True)
class WakeEvent:
    event_id: str
    timestamp: str
    inline_command: bool = False


@dataclass(frozen=True)
class CommandEvent:
    text: str
    timestamp: str


@dataclass(frozen=True)
class SleepEvent:
    timestamp: str
    reason: str = "voice"


@dataclass(frozen=True)
class ExitEvent:
    timestamp: str


@dataclass(frozen=True)
class InterruptEvent:
    timestamp: str


@dataclass(frozen=True)
class MicrophoneStatus:
    state: str
    reason: str = ""
    retry_seconds: float = 0.0


@dataclass(frozen=True)
class _CaptureReset:
    """Discard ASR context recorded before an input-device interruption."""


@dataclass(frozen=True)
class _CapturedAudio:
    pcm: bytes
    captured_at: float
    sequence: int


class UtteranceBuffer:
    """Frame audio by pauses instead of cutting a wake name every second.

    Uses only raw energy, not text. PCM stays bounded in memory. The ASR's
    bundled speech detector subsequently rejects noise before decoding.
    """

    def __init__(self, config: WakeConfig) -> None:
        self.config = config
        self._frame_bytes = max(1, int(config.sample_rate * 0.02)) * config.channels * config.sample_width
        self._pre_roll_bytes = self._frame_bytes * 8  # 160 ms before onset.
        self._max_bytes = self._frame_bytes * 80  # At most 1.6 s per decode.
        self.reset()

    def reset(self) -> None:
        self._remainder = b""
        self._pre_roll = b""
        self._active = bytearray()
        self._voice_frames = 0
        self._silent_frames = 0
        self._quiet_frames = 0

    def feed(self, pcm: bytes) -> list[bytes]:
        output: list[bytes] = []
        data = self._remainder + pcm
        complete = len(data) // self._frame_bytes * self._frame_bytes
        self._remainder = data[complete:]
        for offset in range(0, complete, self._frame_bytes):
            frame = data[offset:offset + self._frame_bytes]
            values = np.frombuffer(frame, dtype=np.int16).astype(np.float64)
            voiced = float(np.sqrt(np.mean(values * values))) >= self.config.minimum_rms
            if voiced:
                if not self._active:
                    self._active.extend(self._pre_roll)
                    self._pre_roll = b""
                self._voice_frames += 1
                self._silent_frames = self._quiet_frames = 0
            elif self._active:
                self._silent_frames += 1
            else:
                self._pre_roll = (self._pre_roll + frame)[-self._pre_roll_bytes:]
                self._quiet_frames += 1
                # Quiet ticks let a pending command/30 s timeout finish. Do
                # not enqueue all 10 capture callbacks per second for ASR.
                if self._quiet_frames >= 50:
                    output.append(bytes(self._frame_bytes * 10))
                    self._quiet_frames = 0
                continue

            self._active.extend(frame)
            if self._silent_frames >= 15 or len(self._active) >= self._max_bytes:
                if self._voice_frames >= 4:
                    output.append(bytes(self._active))
                self._pre_roll = (
                    bytes(self._active[-self._pre_roll_bytes:])
                    if self._silent_frames >= 15 else b""
                )
                self._active.clear()
                self._voice_frames = self._silent_frames = self._quiet_frames = 0
        return output


class _WaveFormat(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_uint),
        ("nAvgBytesPerSec", ctypes.c_uint),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class _WaveHeader(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", ctypes.c_uint),
        ("dwBytesRecorded", ctypes.c_uint),
        ("dwUser", ctypes.c_size_t),
        ("dwFlags", ctypes.c_uint),
        ("dwLoops", ctypes.c_uint),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_size_t),
    ]


def _frame_levels(pcm: bytes, config: WakeConfig) -> np.ndarray:
    """20 ms RMS windows; quiet padding must not hide a short wake name."""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    frame_size = max(1, int(config.sample_rate * config.channels * 0.02))
    count = samples.size // frame_size
    if not count:
        return np.empty(0, dtype=np.float64)
    frames = samples[:count * frame_size].reshape(count, frame_size)
    return np.sqrt(np.mean(frames * frames, axis=1))


class WindowsWaveRecorder:
    """Records adjacent PCM chunks using the Windows winmm API."""

    WAVE_FORMAT_PCM = 1
    WAVE_MAPPER = 0xFFFFFFFF
    CALLBACK_NULL = 0
    WHDR_DONE = 0x00000001

    def __init__(self, config: WakeConfig) -> None:
        if not hasattr(ctypes, "WinDLL"):
            raise RuntimeError("The desktop microphone listener requires Windows")
        self.config = config
        self._winmm = ctypes.WinDLL("winmm")
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        wave_in = ctypes.c_void_p
        self._winmm.waveInOpen.argtypes = [
            ctypes.POINTER(wave_in), ctypes.c_uint, ctypes.POINTER(_WaveFormat),
            ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint,
        ]
        self._winmm.waveInOpen.restype = ctypes.c_uint
        for name in ("waveInPrepareHeader", "waveInUnprepareHeader"):
            fn = getattr(self._winmm, name)
            fn.argtypes = [wave_in, ctypes.POINTER(_WaveHeader), ctypes.c_uint]
            fn.restype = ctypes.c_uint
        self._winmm.waveInAddBuffer.argtypes = [
            wave_in, ctypes.POINTER(_WaveHeader), ctypes.c_uint,
        ]
        self._winmm.waveInAddBuffer.restype = ctypes.c_uint
        for name in ("waveInStart", "waveInStop", "waveInReset", "waveInClose"):
            fn = getattr(self._winmm, name)
            fn.argtypes = [wave_in]
            fn.restype = ctypes.c_uint

    @staticmethod
    def _check(code: int, operation: str) -> None:
        if code:
            raise OSError(f"Windows microphone operation {operation} failed ({code})")

    def chunks(self, stop_event: threading.Event) -> Iterator[bytes]:
        cfg = self.config
        fmt = _WaveFormat(
            self.WAVE_FORMAT_PCM,
            cfg.channels,
            cfg.sample_rate,
            cfg.sample_rate * cfg.channels * cfg.sample_width,
            cfg.channels * cfg.sample_width,
            cfg.sample_width * 8,
            0,
        )
        handle = ctypes.c_void_p()
        device_id = self.WAVE_MAPPER if cfg.microphone_device_id < 0 else cfg.microphone_device_id
        self._check(
            self._winmm.waveInOpen(
                ctypes.byref(handle), device_id, ctypes.byref(fmt), 0, 0, self.CALLBACK_NULL
            ),
            "open",
        )
        size = int(cfg.sample_rate * cfg.channels * cfg.sample_width * cfg.chunk_seconds)
        session_deadline = time.monotonic() + cfg.capture_session_seconds
        # Keep native buffers queued while Python yields/decodes. A single
        # stop/start buffer leaves capture gaps that can cut the wake name.
        prepared: list[tuple[ctypes.Array, _WaveHeader]] = []
        try:
            for _ in range(3):
                audio_buffer = ctypes.create_string_buffer(size)
                header = _WaveHeader(ctypes.addressof(audio_buffer), size, 0, 0, 0, 0, None, 0)
                header_size = ctypes.sizeof(header)
                self._check(
                    self._winmm.waveInPrepareHeader(handle, ctypes.byref(header), header_size),
                    "prepare",
                )
                prepared.append((audio_buffer, header))
                self._check(
                    self._winmm.waveInAddBuffer(handle, ctypes.byref(header), header_size),
                    "queue buffer",
                )
            self._check(self._winmm.waveInStart(handle), "start")
            buffer_index = 0
            while not stop_event.is_set():
                audio_buffer, header = prepared[buffer_index]
                deadline = time.monotonic() + cfg.chunk_seconds + 2.0
                while not (header.dwFlags & self.WHDR_DONE):
                    if stop_event.wait(0.02):
                        return
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Microphone did not return audio in time")
                if stop_event.is_set():
                    return
                if not header.dwBytesRecorded:
                    raise TimeoutError("Microphone returned an empty buffer")
                recorded = audio_buffer.raw[:header.dwBytesRecorded]
                # Requeue before yielding, not after the consumer resumes us.
                header.dwBytesRecorded = 0
                self._check(
                    self._winmm.waveInAddBuffer(handle, ctypes.byref(header), ctypes.sizeof(header)),
                    "requeue buffer",
                )
                buffer_index = (buffer_index + 1) % len(prepared)
                yield recorded
                if cfg.capture_session_seconds > 0 and time.monotonic() >= session_deadline:
                    return
        finally:
            self._winmm.waveInReset(handle)
            for _buffer, header in prepared:
                self._winmm.waveInUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
            self._winmm.waveInClose(handle)


class FasterWhisperTranscriber:
    def __init__(self, config: WakeConfig) -> None:
        self.config = config
        self._model: WhisperModel | None = None
        self._hotwords = load_asr_hotwords(config.skills_path)
        self._device = "cpu"
        self._cuda_disabled = config.asr_device == "cpu"
        self._dll_handles: list[object] = []

    def _load_cuda_libraries(self) -> None:
        """Register only installed libraries inside this Python environment.

        No driver installation or global PATH changes. Keep directory/library
        handles alive for CTranslate2's delayed Windows DLL loading.
        """
        if os.name != "nt":
            return
        from importlib.metadata import PackageNotFoundError, distribution

        packages = (
            ("nvidia-cuda-nvrtc-cu12", "nvidia/cuda_nvrtc/bin"),
            ("nvidia-cublas-cu12", "nvidia/cublas/bin"),
            ("nvidia-cudnn-cu12", "nvidia/cudnn/bin"),
        )
        directories = {}
        for package, relative in packages:
            try:
                directory = Path(distribution(package).locate_file(relative)).resolve()
            except PackageNotFoundError:
                continue  # A system-provided CUDA installation may suffice.
            if not directory.is_relative_to(Path(sys.prefix).resolve()):
                raise OSError("CUDA package is outside the active Python environment")
            if directory.is_dir():
                directories[package] = directory
                self._dll_handles.append(os.add_dll_directory(str(directory)))
        for package, name in (
            ("nvidia-cublas-cu12", "cublasLt64_12.dll"),
            ("nvidia-cublas-cu12", "cublas64_12.dll"),
            ("nvidia-cudnn-cu12", "cudnn64_9.dll"),
        ):
            if package in directories:
                self._dll_handles.append(ctypes.WinDLL(str(directories[package] / name)))

    def _cpu_model(self) -> WhisperModel:
        self._device = "cpu"
        LOGGER.info("asr_backend=cpu compute_type=int8")
        return WhisperModel(self.config.model_name, device="cpu", compute_type="int8", local_files_only=True)

    def _get_model(self) -> WhisperModel:
        if self._model is None:
            LOGGER.info("Loading offline speech model %s", self.config.model_name)
            if not self._cuda_disabled:
                import ctranslate2

                try:
                    if ctranslate2.get_cuda_device_count() > 0:
                        self._load_cuda_libraries()
                        self._model = WhisperModel(
                            self.config.model_name, device="cuda", compute_type="float16", local_files_only=True,
                        )
                        self._device = "cuda"
                        LOGGER.info("asr_backend=cuda compute_type=float16")
                        return self._model
                except (RuntimeError, OSError) as error:
                    self._cuda_disabled = True
                    LOGGER.warning("asr_cuda_fallback reason=%s", type(error).__name__)
            self._model = self._cpu_model()
        return self._model

    def prepare(self) -> None:
        self._get_model()
        # Loading weights does not warm the first inference. Finish that work
        # before announcing the listener as ready and opening the microphone.
        silent_pcm = np.zeros(self.config.sample_rate, dtype=np.int16).tobytes()
        self._transcribe(silent_pcm, use_hotwords=True, detect_speech=False)

    def _transcribe(self, pcm: bytes, *, use_hotwords: bool, detect_speech: bool = True) -> str:
        if not pcm:
            return ""
        # faster-whisper accepts mono float32 PCM directly, which keeps every
        # utterance in memory and avoids leaving temporary recordings on disk.
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        levels = _frame_levels(pcm, self.config)
        voiced = levels[levels >= self.config.minimum_rms]
        # Bounded gain helps low headset input; never amplify digital silence
        # or use the boosted signal to bypass the raw-audio activity gate.
        if voiced.size >= 4:
            level = max(1.0, float(np.percentile(voiced, 75)))
            gain = min(8.0, max(1.0, 1600.0 / level))
            audio = np.clip(audio * gain, -1.0, 1.0)
        # Very short/loud Mandarin names can be rejected by neural VAD. A
        # sustained strong raw signal may bypass that *prefilter*, never the
        # independent unbiased wake/command checks below.
        use_vad = detect_speech and not (voiced.size >= 8 and float(np.percentile(voiced, 75)) >= 600)
        try:
            return self._decode(audio, use_hotwords=use_hotwords, use_vad=use_vad)
        except (RuntimeError, OSError) as error:
            if self._device != "cuda":
                raise
            # Missing delayed DLLs or GPU memory pressure must not disable
            # wake-up. Retry this audio once on CPU, not on every future call.
            LOGGER.warning("asr_cuda_fallback reason=%s", type(error).__name__)
            self._cuda_disabled = True
            self._model = self._cpu_model()
            return self._decode(audio, use_hotwords=use_hotwords, use_vad=use_vad)

    def _decode(self, audio: np.ndarray, *, use_hotwords: bool, use_vad: bool) -> str:
        segments, _ = self._get_model().transcribe(
            audio,
            language="zh",
            beam_size=1,
            best_of=1,
            # A noisy 100 ms fragment must not trigger the default sequence
            # of six temperature retries and block later real wake speech.
            temperature=0.0,
            without_timestamps=True,
            max_new_tokens=32,
            vad_filter=use_vad,
            vad_parameters={
                "threshold": 0.25,
                "min_speech_duration_ms": 64,
                "min_silence_duration_ms": 160,
                "speech_pad_ms": 160,
            },
            condition_on_previous_text=False,
            hotwords=self._hotwords if use_hotwords else None,
        )
        return "".join(segment.text for segment in segments).strip()

    def transcribe(self, pcm: bytes) -> str:
        return self._transcribe(pcm, use_hotwords=True)

    def transcribe_plain(self, pcm: bytes) -> str:
        """Confirm an action without bias from the wake-word hints."""
        return self._transcribe(pcm, use_hotwords=False)


def load_wake_words(skills_path: Path) -> tuple[str, ...]:
    with skills_path.open("r", encoding="utf-8-sig") as config_file:
        config = json.load(config_file)
    words = config.get("settings", {}).get("wake_words")
    if not isinstance(words, list) or not words or not all(isinstance(word, str) for word in words):
        raise ValueError("skills.json settings.wake_words must be a non-empty string list")
    return tuple(words)


def load_asr_hotwords(skills_path: Path) -> str:
    """Hint Whisper with configured wake words and allowlisted app names."""
    with skills_path.open("r", encoding="utf-8-sig") as config_file:
        config = json.load(config_file)
    wake_words = load_wake_words(skills_path)
    actions = config.get("actions", {})
    phrases: list[str] = []
    # Put the short Chinese and English wake forms first.  These are the
    # phrases most likely to be lost at the start of a two-second utterance.
    for preferred in ("贾维斯", "加维斯", "jarvis"):
        phrases.extend(word for word in wake_words if word.lower() == preferred)
    for action_id in sorted(actions):
        action = actions[action_id]
        if not isinstance(action, dict) or action.get("type") != "open_path":
            continue
        aliases = action.get("aliases", [])
        if isinstance(aliases, list) and aliases and isinstance(aliases[0], str):
            phrases.append("打开" + aliases[0])
    phrases.extend(wake_words)
    return " ".join(dict.fromkeys(phrases))


class WakeWordListener:
    """Detects a configured wake word, then captures one command utterance."""

    def __init__(
        self,
        config: WakeConfig | None = None,
        on_wake: Callable[[WakeEvent], None] | None = None,
        on_command: Callable[[CommandEvent], None] | None = None,
        on_sleep: Callable[[SleepEvent], None] | None = None,
        on_exit: Callable[[ExitEvent], None] | None = None,
        on_microphone_status: Callable[[MicrophoneStatus], None] | None = None,
        on_audio_level: Callable[[int], None] | None = None,
        on_interrupt: Callable[[InterruptEvent], None] | None = None,
        interruption_enabled: Callable[[], bool] | None = None,
        ignore_wake_audio: Callable[[], bool] | None = None,
        ignore_command_audio: Callable[[], bool] | None = None,
        audio_source: AudioSource | None = None,
        transcriber: Transcriber | None = None,
    ) -> None:
        self.config = config or WakeConfig()
        self.on_wake = on_wake
        self.on_command = on_command
        self.on_sleep = on_sleep
        self.on_exit = on_exit
        self.on_microphone_status = on_microphone_status
        self.on_audio_level = on_audio_level
        self.on_interrupt = on_interrupt
        self.interruption_enabled = interruption_enabled
        self.ignore_wake_audio = ignore_wake_audio
        self.ignore_command_audio = ignore_command_audio
        self.audio_source = audio_source or WindowsWaveRecorder(self.config)
        self.transcriber = transcriber or FasterWhisperTranscriber(self.config)
        self._converter = OpenCC("t2s")
        self._skills = SkillSystem.load(self.config.skills_path, normalize_term=self._normalize)
        self._wake_words = tuple(self._normalize(item) for item in load_wake_words(self.config.skills_path))
        self._sleep_phrases = frozenset(
            self._normalize(item) for item in self._skills.setting_list("sleep_phrases")
        )
        self._exit_phrases = frozenset(
            self._normalize(item) for item in self._skills.setting_list("exit_phrases")
        )
        self._wake_carry = ""
        self._wake_carry_length = max(len(word) for word in self._wake_words) - 1
        self._pending_command = ""
        self._waiting_for_command_until = 0.0
        self._last_wake_at = 0.0
        self._previous_audio_tail = b""

    def prepare(self) -> None:
        prepare = getattr(self.transcriber, "prepare", None)
        if callable(prepare):
            prepare()

    def _normalize(self, text: str) -> str:
        simplified = self._converter.convert(text).lower()
        return re.sub(r"[\s。！？!?，,、；;：:\"'（）()【】\[\]]+", "", simplified)

    def _find_wake_word(self, normalized: str) -> tuple[int, str] | None:
        matches = ((normalized.find(word), word) for word in self._wake_words if word)
        valid = [match for match in matches if match[0] >= 0]
        return min(valid, key=lambda item: item[0]) if valid else None

    def _find_confirmed_wake(
        self, normalized: str, *, strong_standalone: bool = False
    ) -> tuple[int, int, str] | None:
        """Confirm a wake by text or matching Mandarin pronunciation."""
        exact = self._find_wake_word(normalized)
        if exact:
            return exact[0], len(exact[1]), "exact"
        best_sound_score = 0.0
        for word in self._wake_words:
            if not word or len(normalized) < len(word):
                continue
            # Chinese one-character substitutions are common with a headset.
            # English needs a tighter match to avoid arbitrary short words.
            chinese_word = all("\u4e00" <= char <= "\u9fff" for char in word)
            threshold = 0.66 if chinese_word else 0.83
            target_sound = lazy_pinyin(word) if chinese_word else []
            for index in range(len(normalized) - len(word) + 1):
                segment = normalized[index:index + len(word)]
                if SequenceMatcher(None, word, segment).ratio() >= threshold:
                    return index, len(word), "near_text"
                if not chinese_word or not all("\u4e00" <= char <= "\u9fff" for char in segment):
                    continue
                heard_sound = lazy_pinyin(segment)
                if len(target_sound) != len(heard_sound):
                    continue
                exact_syllables = sum(a == b for a, b in zip(target_sound, heard_sound))
                sound_score = sum(
                    SequenceMatcher(None, a, b).ratio()
                    for a, b in zip(target_sound, heard_sound)
                ) / len(target_sound)
                best_sound_score = max(best_sound_score, sound_score)
                if exact_syllables >= len(target_sound) - 1 and sound_score >= 0.82:
                    return index, len(word), "near_sound"
                # Whisper often writes a short, clearly spoken wake name as a
                # different three-character phrase. Relax only when the hotword
                # pass heard *just* the wake name and the microphone signal is
                # much louder than the normal background. No inline command is
                # allowed through this path.
                if (
                    strong_standalone and len(word) == 3
                    and len(normalized) == 3 and sound_score >= 0.58
                ):
                    return index, len(word), "strong_standalone_sound"
        LOGGER.info("wake_confirm_sound_score=%.2f", best_sound_score)
        return None

    def _has_allowlisted_action(self, normalized: str) -> bool:
        planned, _ = self._skills.plan(normalized)
        return bool(planned)

    def _neutral_command(self, pcm: bytes, hinted_text: str, *, already_plain: bool = False) -> str:
        plain_transcribe = getattr(self.transcriber, "transcribe_plain", None)
        text = (
            hinted_text if already_plain or not callable(plain_transcribe)
            else plain_transcribe(pcm)
        )
        normalized = self._normalize(text)
        wake_match = self._find_wake_word(normalized)
        if wake_match:
            _, word = wake_match
            normalized = normalized.split(word, 1)[1]
        elif len(normalized) == 3 and self._speech_level(pcm) >= 1800:
            # Repeating the wake name while the 30-second command window is
            # open is not a chat request, even when plain ASR spells it with
            # unrelated characters.
            repeated_wake = self._find_confirmed_wake(
                normalized, strong_standalone=True
            )
            if repeated_wake and repeated_wake[0] == 0 and repeated_wake[1] == 3:
                return ""
        return normalized

    def _write_wake_event(self, event: WakeEvent) -> None:
        path = self.config.event_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        payload = {"event_id": event.event_id, "timestamp": event.timestamp, "state": "awake"}
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def _emit_wake(self, *, inline_command: bool = False) -> None:
        event = WakeEvent(
            str(uuid.uuid4()), datetime.now(timezone.utc).isoformat(), inline_command
        )
        self._write_wake_event(event)
        if self.on_wake:
            self.on_wake(event)

    def _emit_command(self, text: str) -> None:
        if self.on_command:
            self.on_command(CommandEvent(text=text, timestamp=datetime.now(timezone.utc).isoformat()))

    def _emit_sleep(self, reason: str = "voice") -> None:
        self._waiting_for_command_until = 0.0
        self._pending_command = ""
        self._wake_carry = ""
        if self.on_sleep:
            self.on_sleep(SleepEvent(
                timestamp=datetime.now(timezone.utc).isoformat(), reason=reason,
            ))

    def _emit_exit(self) -> None:
        self._waiting_for_command_until = 0.0
        self._pending_command = ""
        self._wake_carry = ""
        if self.on_exit:
            self.on_exit(ExitEvent(timestamp=datetime.now(timezone.utc).isoformat()))

    def _handle_interrupt(self, normalized: str) -> str | None:
        """While speaking/thinking, accept exact local controls, never skills.

        This path uses unbiased ASR and no fuzzy wake matching.  Unrecognized
        speaker echo remains blocked by the ordinary output-audio guards.
        """
        control = normalized
        has_wake_prefix = False
        for word in sorted(self._wake_words, key=len, reverse=True):
            if control.startswith(word):
                control = control[len(word):]
                has_wake_prefix = True
                break
        if control in INTERRUPT_PHRASES:
            self._reset_capture_context()
            if self.on_interrupt:
                self.on_interrupt(InterruptEvent(datetime.now(timezone.utc).isoformat()))
            return "interrupt"
        if has_wake_prefix and self._is_exit_command(control):
            self._emit_exit()
            return "exit"
        if has_wake_prefix and self._is_sleep_command(control):
            self._emit_sleep()
            return "sleep"
        return None

    def _is_exit_command(self, normalized: str) -> bool:
        return normalized in self._exit_phrases

    def _is_sleep_command(self, normalized: str) -> bool:
        return normalized in self._sleep_phrases

    def _finish_pending_command(self) -> bool:
        if len(self._pending_command) < 2:
            return False
        command = self._pending_command
        self._waiting_for_command_until = 0.0
        self._pending_command = ""
        self._wake_carry = ""
        self._emit_command(command)
        return True

    def _reset_capture_context(self) -> None:
        self._previous_audio_tail = b""
        self._wake_carry = ""
        self._pending_command = ""
        self._waiting_for_command_until = 0.0

    def _emit_microphone_status(
        self, state: str, reason: str = "", retry_seconds: float = 0.0
    ) -> None:
        # Only fixed states and exception class names are emitted.  Never log
        # microphone samples or any recognized words here.
        LOGGER.info(
            "microphone_state=%s reason=%s retry_seconds=%.1f",
            state, reason or "none", retry_seconds,
        )
        if self.on_microphone_status:
            try:
                self.on_microphone_status(MicrophoneStatus(state, reason, retry_seconds))
            except Exception:
                LOGGER.exception("Microphone status callback failed")

    @staticmethod
    def _rms(pcm: bytes) -> int:
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size == 0:
            return 0
        values = samples.astype(np.float64)
        return int(np.sqrt(np.mean(values * values)))

    def _has_speech(self, pcm: bytes) -> bool:
        # Require at least 80 ms above the raw microphone floor. This admits
        # a quiet short name amid silence, but not a single click/impulse.
        return bool(np.count_nonzero(_frame_levels(pcm, self.config) >= self.config.minimum_rms) >= 4)

    def _speech_level(self, pcm: bytes) -> int:
        levels = _frame_levels(pcm, self.config)
        voiced = levels[levels >= self.config.minimum_rms]
        return int(np.percentile(voiced, 75)) if voiced.size >= 4 else 0

    def _speech_continues_at_boundary(self, pcm: bytes) -> bool:
        """A wake word at a chunk edge may be followed by its command next chunk."""
        minimum_samples = int(self.config.sample_rate * self.config.channels)
        tail_samples = int(self.config.sample_rate * self.config.channels * 0.25)
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size < minimum_samples or tail_samples <= 0:
            return False
        return self._has_speech(samples[-tail_samples:].tobytes())

    def process_audio(self, pcm: bytes, now: float | None = None) -> str:
        """Process one PCM chunk. Return wake, command, silence, or ignored."""
        predecoded: str | None = None
        if (
            self.interruption_enabled is not None
            and self.interruption_enabled()
            and self._has_speech(pcm)
        ):
            # A narrow emergency/control decoder remains available even when
            # ordinary wake/skill detection is suppressed for speaker echo.
            plain = getattr(self.transcriber, "transcribe_plain", None)
            if callable(plain):
                predecoded = plain(pcm)
                interrupted = self._handle_interrupt(self._normalize(predecoded))
                if interrupted is not None:
                    return interrupted
        if (
            self._waiting_for_command_until
            and self.ignore_command_audio is not None
            and self.ignore_command_audio()
        ):
            # The standalone wake acknowledgement is not a user command.
            # Start the full command window after that reply finishes.
            current = time.monotonic() if now is None else now
            self._waiting_for_command_until = max(
                self._waiting_for_command_until,
                current + self.config.command_timeout_seconds,
            )
            self._previous_audio_tail = b""
            return "output_audio"
        # The response speaker can feed the microphone (including audio queued
        # during the previous chunk). Never classify that output as a new wake.
        # Keep the awake command window open so users can speak right away.
        if (
            not self._waiting_for_command_until
            and self.ignore_wake_audio is not None
            and self.ignore_wake_audio()
        ):
            self._previous_audio_tail = b""
            self._wake_carry = ""
            return "output_audio"
        previous_audio_tail = self._previous_audio_tail
        overlap_bytes = int(
            self.config.sample_rate * self.config.channels *
            self.config.sample_width * 1.3
        )
        self._previous_audio_tail = pcm[-overlap_bytes:]
        if not self._has_speech(pcm):
            current = time.monotonic() if now is None else now
            if self._waiting_for_command_until:
                if current > self._waiting_for_command_until:
                    if self._finish_pending_command():
                        return "command"
                    self._emit_sleep("timeout")
                    return "timeout"
                # A conversational utterance has no skill trigger.  Wait for
                # the next quiet chunk before sending its complete phrase.
                if self._finish_pending_command():
                    return "command"
            # Never join a wake fragment to unrelated words after silence.
            self._wake_carry = ""
            self._previous_audio_tail = b""
            return "silence"
        # Once awake, use the unbiased command decoder directly instead of
        # decoding the same audio twice (hotword pass followed by plain pass).
        plain_transcribe = getattr(self.transcriber, "transcribe_plain", None)
        waiting_at_capture = bool(
            self._waiting_for_command_until
            and (time.monotonic() if now is None else now) <= self._waiting_for_command_until
        )
        use_plain = waiting_at_capture and callable(plain_transcribe)
        decode_started = time.monotonic()
        text = (
            predecoded if predecoded is not None
            else plain_transcribe(pcm) if use_plain
            else self.transcriber.transcribe(pcm)
        )
        LOGGER.info(
            "asr_%s_ms=%d", "command" if use_plain else "primary",
            int((time.monotonic() - decode_started) * 1000),
        )
        # CPU transcription can take several seconds.  Start the response
        # window after decoding, or a queued command may already be expired.
        current = time.monotonic() if now is None else now
        normalized = self._normalize(text)

        if self._waiting_for_command_until and current > self._waiting_for_command_until:
            if self._finish_pending_command():
                return "command"
            self._emit_sleep("timeout")
            return "timeout"

        if current <= self._waiting_for_command_until:
            if not normalized:
                if self._finish_pending_command():
                    return "command"
                return "silence"
            # Calling the name again while waiting is a fresh summons, not a
            # chat request. This is already unbiased ASR inside an awake
            # session; it grants no new action permissions.
            repeated = self._find_confirmed_wake(
                normalized,
                strong_standalone=len(normalized) == 3 and self._speech_level(pcm) >= 1800,
            )
            if repeated and normalized[repeated[0] + repeated[1]:] in {"", "啊", "呀", "哦", "呢"}:
                self._pending_command = ""
                self._wake_carry = ""
                self._waiting_for_command_until = current + self.config.command_timeout_seconds
                if current - self._last_wake_at >= self.config.cooldown_seconds:
                    self._last_wake_at = current
                    self._emit_wake()
                return "wake"
            neutral = self._neutral_command(
                pcm, text, already_plain=use_plain
            )
            candidate = self._pending_command + neutral
            if self._is_exit_command(neutral) or self._is_exit_command(candidate):
                self._emit_exit()
                return "exit"
            if self._is_sleep_command(neutral) or self._is_sleep_command(candidate):
                self._emit_sleep()
                return "sleep"
            if self._has_allowlisted_action(candidate):
                self._emit_command(candidate)
                self._waiting_for_command_until = 0.0
                self._pending_command = ""
                self._wake_carry = ""
                return "command"
            self._pending_command = candidate[-80:]
            return "waiting"

        # Whisper chunks are adjacent, so a short wake phrase can straddle the
        # boundary between two chunks.  Keep only the few characters needed to
        # complete a configured wake word; command text still comes exclusively
        # from the current recognized chunk.
        wake_candidate = self._wake_carry + normalized
        wake_match = self._find_wake_word(wake_candidate)
        command_pcm = pcm
        if wake_match and previous_audio_tail and (
            wake_match[0] < len(self._wake_carry)
            or self._speech_continues_at_boundary(previous_audio_tail)
        ):
            # A biased pass can complete the name from its last syllables even
            # without a text carry match. If speech crossed the boundary, the
            # unbiased pass still needs BOTH audio fragments to confirm it.
            command_pcm = previous_audio_tail + pcm
        if (
            not wake_match
            and previous_audio_tail
            and self._has_speech(previous_audio_tail)
        ):
            # A wake phrase near the capture boundary is easy to lose when
            # decoded as two separate snippets. Retry with just the preceding
            # 1.3 seconds; keep audio in memory and never log the transcript.
            overlapped_pcm = previous_audio_tail + pcm
            decode_started = time.monotonic()
            overlapped_text = self.transcriber.transcribe(overlapped_pcm)
            LOGGER.info("asr_overlap_ms=%d", int((time.monotonic() - decode_started) * 1000))
            overlapped_candidate = self._wake_carry + self._normalize(overlapped_text)
            wake_match = self._find_wake_word(overlapped_candidate)
            if wake_match:
                LOGGER.info("wake_overlap_match")
                wake_candidate = overlapped_candidate
                command_pcm = overlapped_pcm
                text = overlapped_text
            if now is None:
                current = time.monotonic()
        if wake_match and current - self._last_wake_at >= self.config.cooldown_seconds:
            _, word = wake_match
            hinted_tail = wake_candidate.split(word, 1)[1]
            plain_transcribe = getattr(self.transcriber, "transcribe_plain", None)
            confirmed_full = wake_candidate
            tail = hinted_tail
            if callable(plain_transcribe):
                # The hotword decoder is deliberately biased toward "Jarvis".
                # A separate unbiased pass must hear the wake word before any
                # animation, acknowledgement, chat, or desktop action begins.
                confirm_started = time.monotonic()
                confirmed_full = self._normalize(plain_transcribe(command_pcm))
                LOGGER.info(
                    "asr_wake_confirm_ms=%d",
                    int((time.monotonic() - confirm_started) * 1000),
                )
                strong_standalone = (
                    not hinted_tail
                    and len(confirmed_full) == 3
                    and self._speech_level(command_pcm) >= 1800
                )
                confirmed_match = self._find_confirmed_wake(
                    confirmed_full, strong_standalone=strong_standalone
                )
                if confirmed_match is None:
                    LOGGER.info("wake_rejected_unconfirmed plain_empty=%s plain_length=%d",
                                not bool(confirmed_full), len(confirmed_full))
                    self._wake_carry = ""
                    # No rejected text is trusted. Retain only this bounded
                    # in-memory audio tail: the first half of a real name can
                    # itself produce a biased full name + empty plain result.
                    # The next adjacent buffer must independently confirm the
                    # complete audio; silence/output/reset discards this tail.
                    return "unconfirmed_wake"
                confirmed_index, confirmed_length, confirm_type = confirmed_match
                if confirm_type != "exact":
                    LOGGER.info("wake_confirmed_%s", confirm_type)
                tail = confirmed_full[confirmed_index + confirmed_length:]
                hinted_actions, _ = self._skills.plan(hinted_tail)
                confirmed_actions, _ = self._skills.plan(tail)
                if (hinted_actions or confirmed_actions) and (
                    {item.action_id for item in hinted_actions}
                    != {item.action_id for item in confirmed_actions}
                ):
                    LOGGER.info("wake_rejected_action_mismatch")
                    self._wake_carry = ""
                    self._previous_audio_tail = b""
                    return "unconfirmed_wake"
                # A single decoder can invent a command after a real or
                # hallucinated wake word.  Keep listening instead of sending
                # that uncorroborated tail to chat or a local control.
                if not hinted_tail or not tail:
                    tail = ""
                if (
                    hinted_tail and tail and not hinted_actions and not confirmed_actions
                    and SequenceMatcher(None, hinted_tail, tail).ratio() < 0.45
                ):
                    LOGGER.info("wake_rejected_tail_mismatch")
                    self._wake_carry = ""
                    self._previous_audio_tail = b""
                    return "unconfirmed_wake"
            if now is None:
                current = time.monotonic()
            self._last_wake_at = current
            self._waiting_for_command_until = current + self.config.command_timeout_seconds
            # Exact local controls remain available once the wake word itself
            # has been independently confirmed.
            control_text = confirmed_full if callable(plain_transcribe) else wake_candidate
            control_confirmed = bool(tail) or control_text.startswith(("退出", "关闭", "休眠"))
            if control_confirmed and (
                self._is_exit_command(tail)
                or self._is_exit_command(control_text)
                or self._is_sleep_command(tail)
                or self._is_sleep_command(control_text)
            ):
                self._emit_wake(inline_command=True)
                if self._is_exit_command(tail) or self._is_exit_command(control_text):
                    self._emit_exit()
                    return "exit"
                self._emit_sleep()
                return "sleep"
            has_inline_command = self._speech_continues_at_boundary(pcm) or any(
                candidate not in {"", "啊", "呀", "哦", "呢"}
                for candidate in (tail,)
            )
            self._emit_wake(inline_command=has_inline_command)
            self._wake_carry = ""
            if self._is_exit_command(tail):
                self._emit_exit()
                return "exit"
            if self._is_sleep_command(tail):
                self._emit_sleep()
                return "sleep"
            if tail and self._has_allowlisted_action(tail):
                self._waiting_for_command_until = 0.0
                self._emit_command(tail)
                return "command"
            self._pending_command = tail[-80:]
            return "wake"
        if self._wake_carry_length > 0:
            self._wake_carry = wake_candidate[-self._wake_carry_length :]
        return "ignored"

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        active_stop = stop_event or threading.Event()
        self.prepare()
        LOGGER.info("Desktop wake listener is ready")
        # Capture and transcription run independently.  This prevents the
        # recognizer's CPU time from creating long periods when the microphone
        # is not listening.
        # Do not execute stale speech or join text across an overflow/device
        # gap. Sequence + capture time survive even if a reset marker drops.
        audio_queue: queue.Queue[_CapturedAudio | _CaptureReset | BaseException] = queue.Queue(maxsize=6)

        def capture_worker() -> None:
            utterances = UtteranceBuffer(self.config)
            sequence = 0
            retry_seconds = 0.5
            had_audio = False
            recovering = False
            near_zero_since: float | None = None
            reported_silent = False
            while not active_stop.is_set():
                try:
                    for recorded_pcm in self.audio_source.chunks(active_stop):
                        if active_stop.is_set():
                            return
                        if not recorded_pcm:
                            raise TimeoutError("Microphone returned no samples")
                        if recovering:
                            self._emit_microphone_status("recovered")
                            recovering = False
                        elif not had_audio:
                            self._emit_microphone_status("ready")
                        had_audio = True
                        retry_seconds = 0.5
                        if self.on_audio_level:
                            try:
                                self.on_audio_level(self._rms(recorded_pcm))
                            except Exception:
                                LOGGER.warning("microphone_level_callback_failed")
                        # A physically muted headset may return valid buffers
                        # of digital zeroes. Surface that separately without
                        # treating ordinary quiet rooms as a disconnect.
                        if self._rms(recorded_pcm) <= 2:
                            if near_zero_since is None:
                                near_zero_since = time.monotonic()
                            elif not reported_silent and time.monotonic() - near_zero_since >= 45:
                                self._emit_microphone_status("silent_input")
                                reported_silent = True
                        else:
                            near_zero_since = None
                            if reported_silent:
                                self._emit_microphone_status("ready", "signal_restored")
                                reported_silent = False
                        for phrase_pcm in utterances.feed(recorded_pcm):
                            sequence += 1
                            packet = _CapturedAudio(phrase_pcm, time.monotonic(), sequence)
                            try:
                                audio_queue.put_nowait(packet)
                            except queue.Full:
                                try:
                                    audio_queue.get_nowait()
                                except queue.Empty:
                                    pass
                                LOGGER.warning("audio_queue_overflow_dropped_oldest")
                                try:
                                    audio_queue.put_nowait(packet)
                                except queue.Full:
                                    pass
                    # Normal generator exhaustion is a planned device refresh
                    # for WAVE_MAPPER; it does not indicate a listening fault.
                    if active_stop.wait(0.05):
                        return
                except (OSError, TimeoutError) as error:
                    # Headsets may sleep, reconnect, or stop delivering samples
                    # after Windows resumes. Reopen the capture device without
                    # killing the desktop controller and its phone bridge.
                    recovering = True
                    utterances.reset()
                    near_zero_since = None
                    reported_silent = False
                    while True:
                        try:
                            audio_queue.get_nowait()
                        except queue.Empty:
                            break
                    audio_queue.put_nowait(_CaptureReset())
                    self._emit_microphone_status(
                        "reconnecting", type(error).__name__, retry_seconds
                    )
                    if active_stop.wait(retry_seconds):
                        return
                    retry_seconds = min(retry_seconds * 2, 15.0)
                except BaseException as error:
                    audio_queue.put(error)
                    return

        capture_thread = threading.Thread(
            target=capture_worker,
            name="JarvisMicrophoneCapture",
            daemon=True,
        )
        capture_thread.start()
        previous_sequence: int | None = None
        while not active_stop.is_set():
            try:
                item = audio_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, _CaptureReset):
                self._reset_capture_context()
                previous_sequence = None
                continue
            if previous_sequence is not None and item.sequence != previous_sequence + 1:
                self._reset_capture_context()
                LOGGER.warning("audio_capture_gap_context_reset")
            previous_sequence = item.sequence
            age = time.monotonic() - item.captured_at
            if age > 3.0:
                self._reset_capture_context()
                LOGGER.warning("audio_stale_dropped age_ms=%d", int(age * 1000))
                continue
            try:
                level = self._rms(item.pcm)
                state = self.process_audio(item.pcm)
                LOGGER.info("audio_chunk rms=%d state=%s", level, state)
            except Exception:
                LOGGER.exception("Could not process an audio chunk")


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline Jarvis desktop wake listener")
    parser.add_argument("--model", default="small", help="Local faster-whisper model name/path")
    parser.add_argument("--device", type=int, default=-1, help="Windows microphone device id")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    listener = WakeWordListener(WakeConfig(model_name=args.model, microphone_device_id=args.device))
    try:
        listener.run_forever()
    except KeyboardInterrupt:
        LOGGER.info("Desktop wake listener stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
