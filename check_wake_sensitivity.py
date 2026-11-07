"""Explicit offline smoke test: synthetic speech, no mic, playback or files.

Uses only the already cached small model. Results are anonymous metrics, not
transcripts. This cannot substitute for a user's real headset test.
"""
from __future__ import annotations

import json
import argparse
import time

import numpy as np

from jarvis_wake_listener import (
    FasterWhisperTranscriber, WakeConfig, WakeWordListener, UtteranceBuffer, _frame_levels,
)


class NoMicrophone:
    def chunks(self, _stop_event):
        raise AssertionError("This check must never capture a microphone")


def synthetic_wake(phrase: str = "贾维斯") -> bytes:
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
        voice.Speak(phrase, 16)  # Authored test fixture, not captured speech.
        return bytes(memory.GetData())
    finally:
        pythoncom.CoUninitialize()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu", action="store_true", help="Explicitly test CPU fallback instead of automatic GPU")
    args = parser.parse_args()
    config = WakeConfig(asr_device="cpu" if args.cpu else "auto")
    decoder = FasterWhisperTranscriber(config)
    decoder.prepare()
    raw = synthetic_wake()
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    levels = _frame_levels(raw, config)
    active_level = float(np.percentile(levels[levels >= 50], 75))
    frame_size = int(config.sample_rate * config.chunk_seconds)
    results = []
    for label, level, offset in (
        ("normal", 1200, 0),
        ("soft", 320, 0),
        ("very_soft", 150, 0),
        ("soft_boundary_early", 320, 5_600),
        ("soft_boundary", 320, 10_400),
        ("soft_boundary_late", 320, 13_600),
    ):
        voice = np.clip(samples * level / active_level, -32768, 32767).astype(np.int16)
        audio = np.concatenate((np.zeros(offset, dtype=np.int16), voice, np.zeros(config.sample_rate, dtype=np.int16)))
        wakes, commands = [], []
        listener = WakeWordListener(config, on_wake=wakes.append, on_command=commands.append,
                                    transcriber=decoder, audio_source=NoMicrophone())
        listener._write_wake_event = lambda _event: None
        started = time.monotonic()
        states = []
        buffer = UtteranceBuffer(config)
        for index in range(0, audio.size, frame_size):
            block = audio[index:index + frame_size]
            if block.size < frame_size:
                block = np.pad(block, (0, frame_size - block.size))
            for utterance in buffer.feed(block.tobytes()):
                states.append(listener.process_audio(utterance, now=10 + index / config.sample_rate))
            if wakes:
                break
        results.append({"case": label, "passed": bool(wakes) and not commands,
                        "states": states, "processing_ms": round((time.monotonic() - started) * 1000)})

    rng = np.random.default_rng(23)
    for label, audio in (
        ("silence", np.zeros(config.sample_rate, dtype=np.int16)),
        ("noise", rng.normal(0, 260, config.sample_rate).astype(np.int16)),
        ("tone", (420 * np.sin(2 * np.pi * 440 * np.arange(config.sample_rate) / config.sample_rate)).astype(np.int16)),
        ("ordinary_speech", np.frombuffer(synthetic_wake("你好，今天天气怎么样"), dtype=np.int16)),
        ("command_without_wake", np.frombuffer(synthetic_wake("请打开计算器"), dtype=np.int16)),
    ):
        wakes, commands = [], []
        listener = WakeWordListener(config, on_wake=wakes.append, on_command=commands.append,
                                    transcriber=decoder, audio_source=NoMicrophone())
        listener._write_wake_event = lambda _event: None
        buffer = UtteranceBuffer(config)
        states = []
        padded = np.concatenate((audio, np.zeros(config.sample_rate, dtype=np.int16)))
        for index in range(0, padded.size, frame_size):
            for utterance in buffer.feed(padded[index:index + frame_size].tobytes()):
                states.append(listener.process_audio(utterance, now=10 + index / config.sample_rate))
        results.append({"case": label, "passed": not wakes and not commands, "states": states})
    print(json.dumps({"synthetic_only": True, "backend": decoder._device, "results": results}, ensure_ascii=True))
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
