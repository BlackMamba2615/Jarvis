"""Offline Windows SAPI synthesis for short, authenticated Jarvis replies."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable


MAX_SPEECH_CHARS = 200


def speak_interruptible(
    text: str,
    cancel_event: threading.Event | None = None,
    *,
    rate: int = 0,
    on_start: Callable[[], None] | None = None,
    on_done: Callable[[], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> bool:
    """Speak plain text asynchronously; cancellation purges SAPI's own queue.

    This is a local COM call, not a shell/process invocation.  Use plain-text
    flags so model output cannot be interpreted as SAPI XML or audio markup.
    All COM access stays on this worker thread, including the purge operation.
    """
    utterance = text.strip()
    if not utterance or len(utterance) > 900:
        raise ValueError("语音回复须为 1 至 900 字")
    if cancel_event is not None and cancel_event.is_set():
        return False

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    started = False
    completed = False
    purged = False
    voice = None
    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        for candidate in voice.GetVoices():
            if candidate.GetAttribute("Language").lower() == "804":
                voice.Voice = candidate
                break
        voice.Rate = max(-2, min(2, int(rate)))
        if cancel_event is not None and cancel_event.is_set():
            return False
        if on_start is not None:
            on_start()
        started = True
        # SVSFlagsAsync = 1, SVSFIsNotXML = 16.
        voice.Speak(utterance, 17)
        began = time.monotonic()
        next_progress = began
        while True:
            if cancel_event is not None and cancel_event.is_set():
                # SVSFPurgeBeforeSpeak = 2.  Empty text clears pending audio.
                voice.Speak("", 3)
                purged = True
                return False
            if voice.WaitUntilDone(50):
                completed = True
                return True
            current = time.monotonic()
            if on_progress is not None and current >= next_progress:
                on_progress(current - began)
                next_progress = current + 0.16
    finally:
        try:
            if started and not completed and not purged and voice is not None:
                try:
                    voice.Speak("", 3)
                except Exception:
                    pass
            if started and on_done is not None:
                on_done()
        finally:
            voice = None
            pythoncom.CoUninitialize()


def synthesize_wave(text: str) -> Path:
    utterance = text.strip()
    if not utterance or len(utterance) > MAX_SPEECH_CHARS:
        raise ValueError("语音回复须为 1 至 200 字")

    import pythoncom
    import win32com.client

    descriptor, filename = tempfile.mkstemp(prefix="jarvis-reply-", suffix=".wav")
    os.close(descriptor)
    output = Path(filename)
    pythoncom.CoInitialize()
    stream = None
    voice = None
    try:
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        # SSFMCreateForWrite = 3. SAPI writes a standard RIFF/WAVE header.
        stream.Open(str(output), 3, False)
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        for candidate in voice.GetVoices():
            if candidate.GetAttribute("Language").lower() == "804":
                voice.Voice = candidate
                break
        voice.Rate = -1
        voice.AudioOutputStream = stream
        voice.Speak(utterance)
        stream.Close()
        stream = None
        voice = None
        return output
    except Exception:
        if stream is not None:
            try:
                stream.Close()
            except Exception:
                pass
        output.unlink(missing_ok=True)
        raise
    finally:
        pythoncom.CoUninitialize()
