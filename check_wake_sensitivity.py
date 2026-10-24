"""Explicit offline smoke test: synthetic speech, no mic, playback or files.

Uses only the already cached small model. Results are anonymous metrics, not
transcripts. This cannot substitute for a user's real headset test.
"""
from __future__ import annotations

import json
import time

import numpy as np

from jarvis_wake_listener import (
    FasterWhisperTranscriber, WakeConfig, WakeWordListener, _frame_levels,
)


class NoMicrophone:
    def chunks(self, _stop_event):
        raise AssertionError("This check must never capture a microphone")


def synthetic_wake() -> bytes:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        chinese = [item for item in voice.GetVoices() if item.GetAttribute("Language").lower() == "804"]
        if not chinese:
            raise RuntimeError("No offline Mandarin voice installed")
        voice.Voice = chinese[0]
        memory = win32com.client.Dispatch("SAPI.SpMemoryStream")
        memory.Format.Type = 18  # SAFT16kHz16BitMono
        voice.AudioOutputStream = memory  # Never plays through a speaker.
        voice.Speak("贾维斯", 16)  # Fixed authored fixture, not captured speech.
        return bytes(memory.GetData())
    finally:
        pythoncom.CoUninitialize()


def main() -> int:
    from faster_whisper import WhisperModel

    config = WakeConfig()
    decoder = FasterWhisperTranscriber(config)
    decoder._model = WhisperModel("small", device="cpu", compute_type="int8", local_files_only=True)
    raw = synthetic_wake()
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    levels = _frame_levels(raw, config)
    active_level = float(np.percentile(levels[levels >= 50], 75))
    frame_size = config.sample_rate
    results = []
    for label, level, offset in (
        ("normal", 1200, 0),
        ("soft", 320, 0),
        ("soft_boundary_early", 320, 5_600),
        ("soft_boundary", 320, 10_400),
        ("soft_boundary_late", 320, 13_600),
    ):
        voice = np.clip(samples * level / active_level, -32768, 32767).astype(np.int16)
        audio = np.concatenate((np.zeros(offset, dtype=np.int16), voice, np.zeros(frame_size, dtype=np.int16)))
        wakes, commands = [], []
        listener = WakeWordListener(config, on_wake=wakes.append, on_command=commands.append,
                                    transcriber=decoder, audio_source=NoMicrophone())
        listener._write_wake_event = lambda _event: None
        started = time.monotonic()
        states = []
        for index in range(0, audio.size, frame_size):
            block = audio[index:index + frame_size]
            if block.size < frame_size:
                block = np.pad(block, (0, frame_size - block.size))
            states.append(listener.process_audio(block.tobytes(), now=10 + index / frame_size * 2))
            if wakes:
                break
        results.append({"case": label, "passed": bool(wakes) and not commands,
                        "states": states, "processing_ms": round((time.monotonic() - started) * 1000)})

    rng = np.random.default_rng(23)
    for label, audio in (
        ("silence", np.zeros(frame_size, dtype=np.int16)),
        ("noise", rng.normal(0, 260, frame_size).astype(np.int16)),
        ("tone", (420 * np.sin(2 * np.pi * 440 * np.arange(frame_size) / frame_size)).astype(np.int16)),
    ):
        wakes, commands = [], []
        listener = WakeWordListener(config, on_wake=wakes.append, on_command=commands.append,
                                    transcriber=decoder, audio_source=NoMicrophone())
        listener._write_wake_event = lambda _event: None
        state = listener.process_audio(audio.tobytes(), now=10)
        results.append({"case": label, "passed": not wakes and not commands, "states": [state]})
    print(json.dumps({"synthetic_only": True, "results": results}, ensure_ascii=True))
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
