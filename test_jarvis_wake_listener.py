import ctypes
import itertools
import queue
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from jarvis_wake_listener import (
    DEFAULT_SKILLS_PATH,
    FasterWhisperTranscriber,
    WakeConfig,
    WakeWordListener,
    WindowsWaveRecorder,
    UtteranceBuffer,
    _CapturedAudio,
    _WaveHeader,
    load_asr_hotwords,
)


class FakeAudioSource:
    def chunks(self, stop_event: threading.Event):
        return iter(())


class FakeTranscriber:
    def __init__(self, texts):
        self.texts = iter(texts)

    def transcribe(self, pcm: bytes) -> str:
        return next(self.texts)


class BufferedWinMM:
    """Driver simulation only: never opens a real input endpoint."""

    def __init__(self, *, prepare_error=0, add_error=0, complete=True):
        self.prepares = self.adds = self.starts = self.resets = self.closes = 0
        self.unprepared = 0
        self.prepare_error, self.add_error = prepare_error, add_error
        self.complete = complete
        self.queued = []

    def waveInOpen(self, *_args):
        return 0

    def waveInPrepareHeader(self, *_args):
        self.prepares += 1
        return 1 if self.prepares == self.prepare_error else 0

    @staticmethod
    def _finish(header):
        header.dwFlags |= WindowsWaveRecorder.WHDR_DONE
        header.dwBytesRecorded = header.dwBufferLength

    def waveInAddBuffer(self, _handle, pointer, _size):
        self.adds += 1
        if self.adds == self.add_error:
            return 1
        header = ctypes.cast(pointer, ctypes.POINTER(_WaveHeader)).contents
        header.dwFlags &= ~WindowsWaveRecorder.WHDR_DONE
        if self.starts and self.complete:
            self._finish(header)
        else:
            self.queued.append(header)
        return 0

    def waveInStart(self, *_args):
        assert len(self.queued) == 3, "All buffers must be queued before capture starts"
        self.starts += 1
        if self.complete:
            for header in self.queued:
                self._finish(header)
        return 0

    def waveInReset(self, *_args):
        self.resets += 1
        return 0

    def waveInUnprepareHeader(self, *_args):
        self.unprepared += 1
        return 0

    def waveInClose(self, *_args):
        self.closes += 1
        return 0


class ContinuousRecorderTests(unittest.TestCase):
    def recorder(self, driver):
        recorder = WindowsWaveRecorder.__new__(WindowsWaveRecorder)
        recorder.config = WakeConfig(capture_session_seconds=0)
        recorder._winmm = driver
        return recorder

    def test_three_buffers_requeue_before_yield_without_stopping_capture(self):
        driver = BufferedWinMM()
        capture = self.recorder(driver).chunks(threading.Event())
        try:
            for number in range(4):
                self.assertEqual(len(next(capture)), 3_200)
                self.assertEqual(driver.adds, 3 + number + 1)
                self.assertEqual(driver.starts, 1)
                self.assertEqual(driver.unprepared, 0)
                self.assertEqual(driver.resets, 0)
        finally:
            capture.close()
        self.assertEqual((driver.resets, driver.unprepared, driver.closes), (1, 3, 1))

    def test_partial_prepare_failure_releases_only_prepared_buffers(self):
        driver = BufferedWinMM(prepare_error=2)
        with self.assertRaises(OSError):
            next(self.recorder(driver).chunks(threading.Event()))
        self.assertEqual(driver.starts, 0)
        self.assertEqual((driver.resets, driver.unprepared, driver.closes), (1, 1, 1))

    def test_partial_queue_failure_releases_all_prepared_buffers(self):
        driver = BufferedWinMM(add_error=2)
        with self.assertRaises(OSError):
            next(self.recorder(driver).chunks(threading.Event()))
        self.assertEqual((driver.resets, driver.unprepared, driver.closes), (1, 2, 1))

    def test_timed_out_driver_still_releases_every_buffer(self):
        driver = BufferedWinMM(complete=False)
        ticks = itertools.count(0, 10)
        with patch("jarvis_wake_listener.time.monotonic", side_effect=lambda: next(ticks)):
            with self.assertRaises(TimeoutError):
                next(self.recorder(driver).chunks(threading.Event()))
        self.assertEqual((driver.resets, driver.unprepared, driver.closes), (1, 3, 1))


class UtteranceBufferTests(unittest.TestCase):
    def setUp(self):
        self.buffer = UtteranceBuffer(WakeConfig())

    def test_name_spanning_old_one_second_boundary_arrives_as_one_utterance(self):
        audio = np.zeros(24_000, dtype=np.int16)
        audio[12_800:19_200] = 150
        packets = []
        for offset in range(0, audio.size, 1600):
            packets.extend(self.buffer.feed(audio[offset:offset + 1600].tobytes()))
        speech = [pcm for pcm in packets if np.any(np.frombuffer(pcm, dtype=np.int16))]
        self.assertEqual(len(speech), 1)
        self.assertEqual(np.count_nonzero(np.frombuffer(speech[0], dtype=np.int16)), 6400)
        self.assertLess(len(speech[0]), 32_000)

    def test_isolated_click_is_discarded_but_soft_name_is_retained(self):
        click = np.zeros(8000, dtype=np.int16)
        click[1600:1920] = 12_000
        self.assertFalse(self.buffer.feed(click.tobytes()))
        voice = np.concatenate((np.full(2400, 150, dtype=np.int16), np.zeros(4800, dtype=np.int16)))
        self.assertEqual(len(self.buffer.feed(voice.tobytes())), 1)

    def test_continuous_speech_is_bounded_without_duplicating_command_audio(self):
        audio = np.full(64_000, 180, dtype=np.int16)
        packets = self.buffer.feed(audio.tobytes())
        packets += self.buffer.feed(bytes(9600))
        self.assertEqual(len(packets), 3)
        self.assertTrue(all(len(pcm) <= 51_200 for pcm in packets))
        self.assertEqual(sum(np.count_nonzero(np.frombuffer(pcm, dtype=np.int16)) for pcm in packets), 64_000)

    def test_reset_discards_partial_name_and_partial_frame(self):
        self.buffer.feed(np.full(1601, 180, dtype=np.int16).tobytes())
        self.buffer.reset()
        packets = self.buffer.feed(bytes(32_000))
        self.assertTrue(all(not any(pcm) for pcm in packets))

    def test_quiet_ticks_can_finish_command_window_without_excessive_queueing(self):
        packets = []
        for _ in range(30):
            packets += self.buffer.feed(bytes(3200))
        self.assertEqual(len(packets), 3)
        self.assertTrue(all(not any(pcm) for pcm in packets))


class WakeListenerTests(unittest.TestCase):
    def setUp(self):
        self.pcm = (np.ones(1600, dtype=np.int16) * 1200).tobytes()

    def listener(self, texts, wakes, commands):
        config = WakeConfig(
            skills_path=DEFAULT_SKILLS_PATH,
            minimum_rms=100,
            cooldown_seconds=1,
            command_timeout_seconds=8,
        )
        listener = WakeWordListener(
            config,
            on_wake=wakes.append,
            on_command=commands.append,
            audio_source=FakeAudioSource(),
            transcriber=FakeTranscriber(texts),
        )
        listener.written_wake_events = []
        listener._write_wake_event = listener.written_wake_events.append
        return listener

    def test_output_interrupt_accepts_exact_stop_but_never_pc_skills(self):
        class PlainTranscriber:
            def __init__(self):
                self.words = iter(["打开计算器", "贾维斯打开计算器", "好的先生", "别说了"])

            def transcribe_plain(self, _pcm):
                return next(self.words)

            def transcribe(self, _pcm):
                raise AssertionError("Biased wake decoder must stay blocked during output")

        wakes, commands, interrupts = [], [], []
        listener = self.listener([], wakes, commands)
        listener.transcriber = PlainTranscriber()
        listener.ignore_wake_audio = lambda: True
        listener.interruption_enabled = lambda: True
        listener.on_interrupt = interrupts.append
        self.assertEqual(listener.process_audio(self.pcm, now=10), "output_audio")
        self.assertEqual(listener.process_audio(self.pcm, now=12), "output_audio")
        self.assertEqual(listener.process_audio(self.pcm, now=14), "output_audio")
        self.assertEqual(listener.process_audio(self.pcm, now=16), "interrupt")
        self.assertEqual(len(interrupts), 1)
        self.assertFalse(wakes)
        self.assertFalse(commands)

    def test_interrupt_phrase_is_not_a_substring_match(self):
        listener = self.listener([], [], [])
        interrupts = []
        listener.on_interrupt = interrupts.append
        for phrase in ("别说了然后打开计算器", "请解释停一下的含义", "贾维斯"):
            self.assertIsNone(listener._handle_interrupt(phrase))
        self.assertFalse(interrupts)
        self.assertEqual(listener._handle_interrupt("贾维斯停止回答"), "interrupt")
        self.assertEqual(len(interrupts), 1)

    def test_sleep_exit_during_output_require_exact_wake_prefix(self):
        listener = self.listener([], [], [])
        sleeps, exits = [], []
        listener.on_sleep, listener.on_exit = sleeps.append, exits.append
        self.assertIsNone(listener._handle_interrupt("退出"))
        self.assertIsNone(listener._handle_interrupt("休眠"))
        self.assertEqual(listener._handle_interrupt("贾维斯休眠"), "sleep")
        self.assertEqual(listener._handle_interrupt("贾维斯退出"), "exit")
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(len(exits), 1)

    def test_microphone_timeout_reopens_capture_instead_of_exiting(self):
        class StopFlag:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, _timeout):
                return self.stopped

        class FlakySource:
            calls = 0

            def chunks(self, stop_event):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("headset paused")
                stop_event.stopped = True
                return iter(())

        source = FlakySource()
        listener = self.listener([], [], [])
        listener.audio_source = source
        listener.prepare = lambda: None
        listener.run_forever(stop_event=StopFlag())
        self.assertEqual(source.calls, 2)

    def test_recorder_periodically_releases_default_device_for_rebinding(self):
        class FakeWinMM:
            opened = 0
            closed = 0

            def waveInOpen(self, *_args):
                self.opened += 1
                return 0

            def waveInPrepareHeader(self, *_args):
                return 0

            def waveInAddBuffer(self, _handle, header_pointer, _header_size):
                header = ctypes.cast(header_pointer, ctypes.POINTER(_WaveHeader)).contents
                header.dwFlags = WindowsWaveRecorder.WHDR_DONE
                header.dwBytesRecorded = header.dwBufferLength
                return 0

            def waveInStart(self, *_args):
                return 0

            def waveInStop(self, *_args):
                return 0

            def waveInUnprepareHeader(self, *_args):
                return 0

            def waveInReset(self, *_args):
                return 0

            def waveInClose(self, *_args):
                self.closed += 1
                return 0

        recorder = WindowsWaveRecorder.__new__(WindowsWaveRecorder)
        recorder.config = WakeConfig(chunk_seconds=0.01, capture_session_seconds=0.1)
        recorder._winmm = FakeWinMM()
        ticks = (item * 0.2 for item in itertools.count())
        with patch("jarvis_wake_listener.time.monotonic", side_effect=ticks):
            chunks = list(recorder.chunks(threading.Event()))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(recorder._winmm.opened, 1)
        self.assertEqual(recorder._winmm.closed, 1)

    def test_repeated_device_errors_back_off_and_report_only_error_type(self):
        class StopFlag:
            stopped = False
            waits = []

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                self.waits.append(timeout)
                if len(self.waits) >= 7:
                    self.stopped = True
                return self.stopped

        class DisconnectedSource:
            def chunks(self, _stop_event):
                raise OSError("private device description")

        stop = StopFlag()
        statuses = []
        listener = self.listener([], [], [])
        listener.audio_source = DisconnectedSource()
        listener.on_microphone_status = statuses.append
        listener.prepare = lambda: None
        listener.run_forever(stop_event=stop)
        self.assertEqual(stop.waits, [0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 15.0])
        self.assertTrue(all(item.state == "reconnecting" for item in statuses))
        self.assertTrue(all(item.reason == "OSError" for item in statuses))

    def test_timeout_after_voice_sleep_reports_recovery_and_keeps_wake_available(self):
        class StopFlag:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, _timeout):
                return self.stopped

        class SleepyHeadset:
            calls = 0

            def chunks(self, stop_event):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("private headset name")
                yield self.pcm
                stop_event.stopped = True

        stop = StopFlag()
        source = SleepyHeadset()
        source.pcm = self.pcm
        statuses, wakes, sleeps, commands = [], [], [], []
        listener = self.listener(["贾维斯", "休眠", "贾维斯"], wakes, commands)
        listener.on_sleep = sleeps.append
        listener.on_microphone_status = statuses.append
        listener.audio_source = source
        listener.prepare = lambda: None
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        self.assertEqual(listener.process_audio(self.pcm, now=12), "sleep")
        self.assertEqual(len(sleeps), 1)
        listener.run_forever(stop_event=stop)
        self.assertEqual(source.calls, 2)
        self.assertEqual([status.state for status in statuses], ["reconnecting", "recovered"])
        self.assertEqual(statuses[0].reason, "TimeoutError")
        listener._reset_capture_context()
        listener.transcriber = FakeTranscriber(["贾维斯"])
        wake_count = len(wakes)
        self.assertEqual(
            listener.process_audio(self.pcm, now=listener._last_wake_at + 2), "wake"
        )
        self.assertEqual(len(wakes), wake_count + 1)

    def test_wake_then_command(self):
        wakes, commands = [], []
        listener = self.listener(["贾维斯", "打开 计算器"], wakes, commands)
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        self.assertEqual(listener.process_audio(self.pcm, now=12), "command")
        self.assertEqual(commands[0].text, "打开计算器")
        self.assertEqual(len(listener.written_wake_events), 1)
        self.assertFalse(wakes[0].inline_command)

    def test_jarvis_speech_cannot_wake_it_again_after_command(self):
        wakes, commands = [], []
        listener = self.listener(["贾维斯", "打开计算器", "贾维斯"], wakes, commands)
        output_active = False
        listener.ignore_wake_audio = lambda: output_active
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        output_active = True
        # Being awake still permits an immediate user command.
        self.assertEqual(listener.process_audio(self.pcm, now=12), "command")
        self.assertEqual(listener.process_audio(self.pcm, now=14), "output_audio")
        self.assertEqual(len(wakes), 1)
        output_active = False
        self.assertEqual(listener.process_audio(self.pcm, now=16), "wake")
        self.assertEqual(len(wakes), 2)

    def test_conversation_is_sent_after_silence_within_thirty_seconds(self):
        wakes, commands = [], []
        listener = self.listener(["贾维斯", "讲个笑话"], wakes, commands)
        listener.config = WakeConfig(
            skills_path=DEFAULT_SKILLS_PATH, minimum_rms=100,
            command_timeout_seconds=30,
        )
        quiet = np.zeros(1600, dtype=np.int16).tobytes()
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        self.assertEqual(listener.process_audio(self.pcm, now=34), "waiting")
        self.assertEqual(listener.process_audio(quiet, now=35), "command")
        self.assertEqual(commands[0].text, "讲个笑话")

    def test_thirty_second_inactivity_sleeps_without_command(self):
        wakes, commands, sleeps = [], [], []
        listener = self.listener(["贾维斯"], wakes, commands)
        listener.config = WakeConfig(
            skills_path=DEFAULT_SKILLS_PATH, minimum_rms=100,
            command_timeout_seconds=30,
        )
        listener.on_sleep = sleeps.append
        quiet = np.zeros(1600, dtype=np.int16).tobytes()
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        self.assertEqual(listener.process_audio(quiet, now=39), "silence")
        self.assertEqual(listener.process_audio(quiet, now=41), "timeout")
        self.assertEqual(sleeps[0].reason, "timeout")
        self.assertFalse(commands)

    def test_lone_wake_is_confirmed_then_followup_uses_one_decode(self):
        class CountingTranscriber:
            primary_calls = 0
            plain_calls = 0

            def transcribe(self, _pcm):
                self.primary_calls += 1
                return "贾维斯"

            def transcribe_plain(self, _pcm):
                self.plain_calls += 1
                return "贾维斯" if self.plain_calls == 1 else "打开计算器"

        commands = []
        listener = self.listener([], [], commands)
        decoder = CountingTranscriber()
        listener.transcriber = decoder
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        self.assertEqual((decoder.primary_calls, decoder.plain_calls), (1, 1))
        self.assertEqual(listener.process_audio(self.pcm, now=12), "command")
        self.assertEqual((decoder.primary_calls, decoder.plain_calls), (1, 2))
        self.assertEqual(commands[0].text, "打开计算器")

    def test_voice_sleep_keeps_listener_available_for_next_wake(self):
        wakes, sleeps, commands = [], [], []
        listener = self.listener(["贾维斯", "休眠", "贾维斯"], wakes, commands)
        listener.on_sleep = sleeps.append
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        self.assertEqual(listener.process_audio(self.pcm, now=12), "sleep")
        self.assertEqual(len(sleeps), 1)
        self.assertFalse(commands)
        self.assertEqual(listener.process_audio(self.pcm, now=15), "wake")
        self.assertEqual(len(wakes), 2)

    def test_voice_exit_is_distinct_from_sleep(self):
        wakes, commands, sleeps, exits = [], [], [], []
        listener = self.listener(["贾维斯", "退出"], wakes, commands)
        listener.on_sleep = sleeps.append
        listener.on_exit = exits.append
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        self.assertEqual(listener.process_audio(self.pcm, now=12), "exit")
        self.assertEqual(len(exits), 1)
        self.assertFalse(sleeps)
        self.assertFalse(commands)
        self.assertEqual(listener._waiting_for_command_until, 0)

    def test_one_phrase_wake_and_exit(self):
        wakes, commands, exits = [], [], []
        listener = self.listener(["贾维斯，退出"], wakes, commands)
        listener.on_exit = exits.append
        self.assertEqual(listener.process_audio(self.pcm, now=10), "exit")
        self.assertEqual(len(wakes), 1)
        self.assertTrue(wakes[0].inline_command)
        self.assertEqual(len(exits), 1)

    def test_exit_phrase_with_wake_word_at_end(self):
        wakes, commands, exits = [], [], []
        listener = self.listener(["退出贾维斯"], wakes, commands)
        listener.on_exit = exits.append
        self.assertEqual(listener.process_audio(self.pcm, now=10), "exit")
        self.assertTrue(wakes[0].inline_command)
        self.assertEqual(len(exits), 1)

    def test_direct_exit_requires_second_decoder_to_confirm(self):
        class DisagreeingDecoder:
            def transcribe(self, _pcm):
                return "贾维斯退出"

            def transcribe_plain(self, _pcm):
                return "听不清"

        wakes, exits = [], []
        listener = self.listener([], wakes, [])
        listener.transcriber = DisagreeingDecoder()
        listener.on_exit = exits.append
        self.assertEqual(listener.process_audio(self.pcm, now=10), "unconfirmed_wake")
        self.assertFalse(wakes)
        self.assertFalse(exits)

    def test_one_decoder_cannot_invent_chat_after_confirmed_wake(self):
        class DisagreeingDecoder:
            def transcribe(self, _pcm):
                return "贾维斯"

            def transcribe_plain(self, _pcm):
                return "贾维斯好的先生"

        wakes, commands = [], []
        listener = self.listener([], wakes, commands)
        listener.transcriber = DisagreeingDecoder()
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        self.assertEqual(len(wakes), 1)
        self.assertFalse(commands)
        self.assertEqual(listener._pending_command, "")

    def test_headset_wake_survives_one_character_error_in_plain_decoder(self):
        class NearWakeDecoder:
            def transcribe(self, _pcm):
                return "贾维斯"

            def transcribe_plain(self, _pcm):
                return "甲维斯"

        wakes, commands = [], []
        listener = self.listener([], wakes, commands)
        listener.transcriber = NearWakeDecoder()
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        self.assertEqual(len(wakes), 1)
        self.assertFalse(commands)

    def test_headset_wake_accepts_same_pronunciation_with_different_characters(self):
        class HomophoneDecoder:
            def transcribe(self, _pcm):
                return "贾维斯"

            def transcribe_plain(self, _pcm):
                return "甲威司"

        wakes, commands = [], []
        listener = self.listener([], wakes, commands)
        listener.transcriber = HomophoneDecoder()
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        self.assertEqual(len(wakes), 1)
        self.assertFalse(commands)

    def test_loud_standalone_wake_accepts_imperfect_phonetics_only(self):
        class ImperfectDecoder:
            def transcribe(self, _pcm):
                return "贾维斯"

            def transcribe_plain(self, _pcm):
                return "甲海石"

        loud_pcm = (np.ones(1600, dtype=np.int16) * 2500).tobytes()
        wakes, commands = [], []
        listener = self.listener([], wakes, commands)
        listener.transcriber = ImperfectDecoder()
        self.assertEqual(listener.process_audio(self.pcm, now=10), "unconfirmed_wake")
        self.assertEqual(listener.process_audio(loud_pcm, now=12), "wake")
        self.assertEqual(len(wakes), 1)
        self.assertFalse(commands)

    def test_loud_unrelated_speech_still_cannot_wake(self):
        class UnrelatedDecoder:
            def transcribe(self, _pcm):
                return "贾维斯"

            def transcribe_plain(self, _pcm):
                return "大家好"

        loud_pcm = (np.ones(1600, dtype=np.int16) * 3500).tobytes()
        wakes, commands = [], []
        listener = self.listener([], wakes, commands)
        listener.transcriber = UnrelatedDecoder()
        self.assertEqual(listener.process_audio(loud_pcm, now=10), "unconfirmed_wake")
        self.assertFalse(wakes)
        self.assertFalse(commands)

    def test_repeated_imperfect_wake_during_command_window_is_not_chat(self):
        class RepeatedWakeDecoder:
            plain_calls = 0

            def transcribe(self, _pcm):
                return "贾维斯"

            def transcribe_plain(self, _pcm):
                self.plain_calls += 1
                return "甲海石"

        loud_pcm = (np.ones(1600, dtype=np.int16) * 2500).tobytes()
        quiet_pcm = np.zeros(1600, dtype=np.int16).tobytes()
        wakes, commands = [], []
        listener = self.listener([], wakes, commands)
        listener.transcriber = RepeatedWakeDecoder()
        self.assertEqual(listener.process_audio(loud_pcm, now=10), "wake")
        self.assertEqual(listener.process_audio(loud_pcm, now=12), "wake")
        self.assertEqual(listener.process_audio(quiet_pcm, now=13), "silence")
        self.assertEqual(len(wakes), 2)
        self.assertFalse(commands)

    def test_negated_exit_does_not_exit(self):
        wakes, commands, exits = [], [], []
        listener = self.listener(["贾维斯", "不要退出"], wakes, commands)
        listener.on_exit = exits.append
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        self.assertEqual(listener.process_audio(self.pcm, now=12), "waiting")
        self.assertFalse(exits)

    def test_one_phrase_wake_and_sleep(self):
        wakes, sleeps, commands = [], [], []
        listener = self.listener(["贾维斯休眠"], wakes, commands)
        listener.on_sleep = sleeps.append
        self.assertEqual(listener.process_audio(self.pcm, now=10), "sleep")
        self.assertEqual(len(wakes), 1)
        self.assertEqual(len(sleeps), 1)

    def test_sleep_word_inside_negation_is_not_an_exit_command(self):
        wakes, sleeps, commands = [], [], []
        listener = self.listener(["贾维斯", "不要休眠"], wakes, commands)
        listener.on_sleep = sleeps.append
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        self.assertEqual(listener.process_audio(self.pcm, now=12), "waiting")
        self.assertFalse(sleeps)

    def test_slow_wake_transcription_does_not_consume_command_timeout(self):
        wakes, commands = [], []
        listener = self.listener(["贾维斯", "打开计算器"], wakes, commands)
        clock = [10]

        class SlowFirstTranscriber:
            def __init__(self):
                self.texts = iter(["贾维斯", "打开计算器"])

            def transcribe(self, pcm):
                clock[0] = 20
                return next(self.texts)

        listener.transcriber = SlowFirstTranscriber()
        with patch("jarvis_wake_listener.time.monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(listener.process_audio(self.pcm), "wake")
        self.assertEqual(listener.process_audio(self.pcm, now=21), "command")
        self.assertEqual(commands[0].text, "打开计算器")

    def test_wake_and_command_in_same_utterance(self):
        wakes, commands = [], []
        listener = self.listener(["Jarvis，打开微信"], wakes, commands)
        self.assertEqual(listener.process_audio(self.pcm, now=20), "command")
        self.assertEqual(commands[0].text, "打开微信")
        self.assertTrue(wakes[0].inline_command)

    def test_wake_ack_audio_is_not_treated_as_command_and_window_is_extended(self):
        wakes, commands = [], []
        listener = self.listener(["贾维斯", "打开计算器"], wakes, commands)
        listener.ignore_command_audio = lambda: len(wakes) == 1 and not commands
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        self.assertEqual(listener.process_audio(self.pcm, now=12), "output_audio")
        self.assertEqual(listener._waiting_for_command_until, 20)
        listener.ignore_command_audio = lambda: False
        self.assertEqual(listener.process_audio(self.pcm, now=14), "command")

    def test_common_chinese_homophone_wakes_listener(self):
        wakes, commands = [], []
        listener = self.listener(["加维斯，打开计算器"], wakes, commands)
        self.assertEqual(listener.process_audio(self.pcm, now=25), "command")
        self.assertEqual(commands[0].text, "打开计算器")

    def test_wake_word_can_span_two_audio_chunks(self):
        wakes, commands = [], []
        listener = self.listener(["贾", "维斯"], wakes, commands)
        self.assertEqual(listener.process_audio(self.pcm, now=26), "ignored")
        self.assertEqual(listener.process_audio(self.pcm, now=27), "wake")
        self.assertEqual(len(wakes), 1)

    def test_audio_overlap_recovers_wake_lost_at_chunk_boundary(self):
        wakes, commands = [], []
        listener = self.listener([], wakes, commands)

        class BoundaryTranscriber:
            def __init__(self):
                self.calls = 0

            def transcribe(self, pcm):
                self.calls += 1
                return ["模糊片段", "打开计算器", "贾维斯打开计算器"][self.calls - 1]

            def transcribe_plain(self, pcm):
                return "贾维斯打开计算器"

        listener.transcriber = BoundaryTranscriber()
        self.assertEqual(listener.process_audio(self.pcm, now=10), "ignored")
        self.assertEqual(listener.process_audio(self.pcm, now=12), "command")
        self.assertEqual(commands[0].text, "打开计算器")

    def test_incomplete_wake_tail_waits_for_allowlisted_command(self):
        wakes, commands = [], []
        listener = self.listener(["贾维斯你好", "打开计算器"], wakes, commands)
        self.assertEqual(listener.process_audio(self.pcm, now=50), "wake")
        self.assertFalse(commands)
        self.assertEqual(listener.process_audio(self.pcm, now=52), "command")
        self.assertEqual(len(commands), 1)
        self.assertIn("打开计算器", commands[0].text)

    def test_command_can_span_two_audio_chunks(self):
        wakes, commands = [], []
        listener = self.listener(["贾维斯打开", "计算器"], wakes, commands)
        self.assertEqual(listener.process_audio(self.pcm, now=60), "wake")
        self.assertEqual(listener.process_audio(self.pcm, now=62), "command")
        self.assertEqual(commands[0].text, "打开计算器")

    def test_direct_command_split_after_wake_skips_wake_reply(self):
        wakes, commands = [], []
        listener = self.listener(["贾维斯", "打开计算器"], wakes, commands)
        first_chunk = np.zeros(24_000, dtype=np.int16)
        first_chunk[-5_000:] = 1200
        self.assertEqual(listener.process_audio(first_chunk.tobytes(), now=10), "wake")
        self.assertTrue(wakes[0].inline_command)
        self.assertEqual(listener.process_audio(self.pcm, now=12), "command")
        self.assertEqual(commands[0].text, "打开计算器")

    def test_unwoken_speech_is_ignored(self):
        wakes, commands = [], []
        listener = self.listener(["打开微信"], wakes, commands)
        self.assertEqual(listener.process_audio(self.pcm, now=30), "ignored")
        self.assertFalse(wakes)
        self.assertFalse(commands)

    def test_quiet_audio_is_not_transcribed(self):
        wakes, commands = [], []
        listener = self.listener([], wakes, commands)
        quiet = np.zeros(1600, dtype=np.int16).tobytes()
        self.assertEqual(listener.process_audio(quiet, now=40), "silence")

    def test_short_soft_wake_is_not_diluted_by_silent_padding(self):
        wakes, commands = [], []
        listener = self.listener(["贾维斯"], wakes, commands)
        listener.config = WakeConfig(skills_path=DEFAULT_SKILLS_PATH)
        soft = np.zeros(16_000, dtype=np.int16)
        soft[6000:10_800] = 180  # 300 ms; whole-buffer RMS is only 98.
        self.assertLess(listener._rms(soft.tobytes()), listener.config.minimum_rms)
        self.assertEqual(listener.process_audio(soft.tobytes(), now=10), "wake")
        self.assertEqual(len(wakes), 1)
        self.assertFalse(commands)

    def test_short_click_and_low_room_noise_do_not_reach_decoder(self):
        listener = self.listener([], [], [])
        listener.config = WakeConfig(skills_path=DEFAULT_SKILLS_PATH)
        click = np.zeros(16_000, dtype=np.int16)
        click[4800:5120] = 12_000  # 20 ms impulse, despite high whole RMS.
        self.assertEqual(listener.process_audio(click.tobytes(), now=10), "silence")
        room = (np.ones(16_000, dtype=np.int16) * 80).tobytes()
        self.assertEqual(listener.process_audio(room, now=12), "silence")

    def test_soft_wake_still_requires_unbiased_confirmation(self):
        class HallucinatingDecoder:
            def transcribe(self, _pcm):
                return "贾维斯打开计算器"

            def transcribe_plain(self, _pcm):
                return "大家好"

        wakes, commands = [], []
        listener = self.listener([], wakes, commands)
        listener.config = WakeConfig(skills_path=DEFAULT_SKILLS_PATH)
        listener.transcriber = HallucinatingDecoder()
        soft = np.zeros(16_000, dtype=np.int16)
        soft[4000:8800] = 260
        self.assertEqual(listener.process_audio(soft.tobytes(), now=10), "unconfirmed_wake")
        self.assertFalse(wakes)
        self.assertFalse(commands)

    def test_text_boundary_match_uses_both_audio_fragments_for_confirmation(self):
        class BoundaryDecoder:
            def __init__(self):
                self.texts = iter(["贾", "维斯"])
                self.confirmed_size = 0

            def transcribe(self, _pcm):
                return next(self.texts)

            def transcribe_plain(self, pcm):
                self.confirmed_size = len(pcm)
                return "贾维斯" if len(pcm) == 2 * len(self.fragment) else "维斯"

        wakes, commands = [], []
        listener = self.listener([], wakes, commands)
        decoder = BoundaryDecoder()
        decoder.fragment = self.pcm
        listener.transcriber = decoder
        self.assertEqual(listener.process_audio(self.pcm, now=10), "ignored")
        self.assertEqual(listener.process_audio(self.pcm, now=12), "wake")
        self.assertEqual(decoder.confirmed_size, 2 * len(self.pcm))
        self.assertEqual(len(wakes), 1)
        self.assertFalse(commands)

    def test_silence_breaks_old_wake_text_carry(self):
        wakes, commands = [], []
        listener = self.listener(["贾", "维斯"], wakes, commands)
        self.assertEqual(listener.process_audio(self.pcm, now=10), "ignored")
        self.assertEqual(listener.process_audio(bytes(len(self.pcm)), now=12), "silence")
        self.assertEqual(listener._previous_audio_tail, b"")
        self.assertEqual(listener._wake_carry, "")
        self.assertEqual(listener.process_audio(self.pcm, now=14), "ignored")
        self.assertFalse(wakes)

    def test_hinted_full_name_still_uses_audio_overlap_after_partial_speech(self):
        class LastSyllableDecoder:
            def __init__(self):
                self.texts = iter(["片段", "贾维斯"])
                self.confirmed_size = 0

            def transcribe(self, _pcm):
                return next(self.texts)

            def transcribe_plain(self, pcm):
                self.confirmed_size = len(pcm)
                return "贾维斯" if len(pcm) == 64_000 else ""

        wakes, commands = [], []
        listener = self.listener([], wakes, commands)
        listener.transcriber = LastSyllableDecoder()
        first = np.zeros(16_000, dtype=np.int16)
        first[-4000:] = 320
        second = np.zeros(16_000, dtype=np.int16)
        second[:4000] = 320
        self.assertEqual(listener.process_audio(first.tobytes(), now=10), "ignored")
        self.assertEqual(listener.process_audio(second.tobytes(), now=12), "wake")
        self.assertEqual(listener.transcriber.confirmed_size, 64_000)
        self.assertEqual(len(wakes), 1)
        self.assertFalse(commands)

    def test_unconfirmed_first_syllable_retains_audio_but_never_rejected_text(self):
        class PartialDecoder:
            def transcribe(self, _pcm):
                return "贾维斯"

            def transcribe_plain(self, pcm):
                return "贾维斯" if len(pcm) == 64_000 else ""

        wakes, commands = [], []
        listener = self.listener([], wakes, commands)
        listener.transcriber = PartialDecoder()
        first = np.zeros(16_000, dtype=np.int16)
        first[-4000:] = 320
        second = np.zeros(16_000, dtype=np.int16)
        second[:4000] = 320
        self.assertEqual(listener.process_audio(first.tobytes(), now=10), "unconfirmed_wake")
        self.assertFalse(wakes)
        self.assertEqual(listener._wake_carry, "")
        self.assertEqual(listener.process_audio(second.tobytes(), now=12), "wake")
        self.assertEqual(len(wakes), 1)
        self.assertFalse(commands)

    def test_quiet_audio_has_bounded_gain_but_silence_is_not_amplified(self):
        class InspectModel:
            def transcribe(self, audio, **_options):
                self.audio = audio.copy()
                return iter(()), None

        model = InspectModel()
        decoder = FasterWhisperTranscriber(WakeConfig(skills_path=DEFAULT_SKILLS_PATH))
        decoder._model = model
        soft = np.zeros(16_000, dtype=np.int16)
        soft[4000:8800] = 260
        decoder.transcribe(soft.tobytes())
        self.assertAlmostEqual(float(model.audio.max()), 1600 / 32768)
        self.assertTrue(np.all(model.audio[:4000] == 0))
        decoder.transcribe(bytes(32_000))
        self.assertTrue(np.all(model.audio == 0))

    def test_whisper_receives_configured_hotwords(self):
        class FakeModel:
            def __init__(self):
                self.options = None

            def transcribe(self, audio, **options):
                self.options = options
                return iter(()), None

        fake_model = FakeModel()
        transcriber = FasterWhisperTranscriber(WakeConfig(skills_path=DEFAULT_SKILLS_PATH))
        transcriber._model = fake_model
        transcriber.transcribe(self.pcm)
        self.assertIn("贾维斯", fake_model.options["hotwords"])
        self.assertIn("打开计算器", fake_model.options["hotwords"])
        self.assertTrue(fake_model.options["vad_filter"])
        self.assertEqual(fake_model.options["temperature"], 0.0)
        self.assertEqual(fake_model.options["best_of"], 1)
        self.assertTrue(fake_model.options["without_timestamps"])
        self.assertEqual(fake_model.options["max_new_tokens"], 32)

    def test_model_prepare_warms_encoder_even_when_vad_would_reject_silence(self):
        class InspectModel:
            def transcribe(self, _audio, **options):
                self.options = options
                return iter(()), None

        decoder = FasterWhisperTranscriber(WakeConfig())
        decoder._model = InspectModel()
        decoder.prepare()
        self.assertFalse(decoder._model.options["vad_filter"])

    def test_strong_sustained_name_is_not_dropped_by_neural_prefilter(self):
        class InspectModel:
            def transcribe(self, _audio, **options):
                self.options = options
                return iter(()), None

        decoder = FasterWhisperTranscriber(WakeConfig())
        decoder._model = InspectModel()
        decoder.transcribe(np.full(4800, 1600, dtype=np.int16).tobytes())
        self.assertFalse(decoder._model.options["vad_filter"])
        decoder.transcribe(np.full(4800, 260, dtype=np.int16).tobytes())
        self.assertTrue(decoder._model.options["vad_filter"])

    def test_cpu_selection_never_initializes_cuda(self):
        decoder = FasterWhisperTranscriber(WakeConfig(asr_device="cpu"))
        with patch("ctranslate2.get_cuda_device_count") as count, \
                patch("jarvis_wake_listener.WhisperModel") as model:
            decoder._get_model()
        count.assert_not_called()
        model.assert_called_once_with("small", device="cpu", compute_type="int8", local_files_only=True)

    def test_missing_gpu_library_falls_back_once_without_logging_library_details(self):
        decoder = FasterWhisperTranscriber(WakeConfig())
        decoder._load_cuda_libraries = Mock(side_effect=OSError("private library path"))
        with patch("ctranslate2.get_cuda_device_count", return_value=1), \
                patch("jarvis_wake_listener.WhisperModel") as model, \
                self.assertLogs("jarvis.wake", level="INFO") as captured:
            self.assertIs(decoder._get_model(), model.return_value)
            decoder._get_model()
        self.assertEqual(decoder._device, "cpu")
        self.assertEqual(decoder._load_cuda_libraries.call_count, 1)
        self.assertNotIn("private library path", " ".join(captured.output))

    def test_gpu_constructor_failure_uses_cpu(self):
        decoder = FasterWhisperTranscriber(WakeConfig())
        decoder._load_cuda_libraries = Mock()
        cpu_model = object()
        with patch("ctranslate2.get_cuda_device_count", return_value=1), \
                patch("jarvis_wake_listener.WhisperModel", side_effect=[RuntimeError(), cpu_model]):
            self.assertIs(decoder._get_model(), cpu_model)
        self.assertEqual(decoder._device, "cpu")

    def test_delayed_gpu_error_retries_the_same_audio_once_on_cpu(self):
        class BrokenGPU:
            def transcribe(self, _audio, **_options):
                def fail_lazily():
                    raise RuntimeError("private GPU failure details")
                    yield
                return fail_lazily(), None

        cpu = Mock()
        cpu.transcribe.side_effect = [(iter(()), None), (iter(()), None)]
        decoder = FasterWhisperTranscriber(WakeConfig())
        decoder._device = "cuda"
        decoder._model = BrokenGPU()
        with patch("jarvis_wake_listener.WhisperModel", return_value=cpu) as model, \
                self.assertLogs("jarvis.wake", level="INFO") as captured:
            self.assertEqual(decoder.transcribe(self.pcm), "")
            self.assertEqual(decoder.transcribe(self.pcm), "")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(cpu.transcribe.call_count, 2)
        self.assertTrue(decoder._cuda_disabled)
        self.assertNotIn("private GPU failure", " ".join(captured.output))

    def test_repeated_name_restarts_wait_and_does_not_complete_pending_chat(self):
        wakes, commands = [], []
        listener = self.listener(["贾维斯", "假维斯啊"], wakes, commands)
        self.assertEqual(listener.process_audio(self.pcm, now=10), "wake")
        listener._pending_command = "旧的不完整句子"
        self.assertEqual(listener.process_audio(self.pcm, now=16), "wake")
        self.assertEqual(len(wakes), 2)
        self.assertEqual(listener._waiting_for_command_until, 24)
        self.assertEqual(listener._pending_command, "")
        self.assertFalse(commands)

    def test_invalid_asr_device_fails_before_starting_listener(self):
        with self.assertRaises(ValueError):
            WakeConfig(asr_device="not-a-device")

    def test_very_soft_input_gain_is_capped_at_eight_not_unbounded(self):
        class InspectModel:
            def transcribe(self, audio, **_options):
                self.audio = audio
                return iter(()), None

        decoder = FasterWhisperTranscriber(WakeConfig())
        decoder._model = InspectModel()
        decoder.transcribe(np.full(1600, 130, dtype=np.int16).tobytes())
        self.assertAlmostEqual(float(decoder._model.audio.max()), 130 * 8 / 32768)

    def test_slow_decoder_does_not_replay_stale_microphone_audio(self):
        stop = threading.Event()
        items = iter([_CapturedAudio(self.pcm, 46.0, 1), _CapturedAudio(self.pcm, 49.5, 2)])

        class SimulatedQueue:
            def get(self, **_options):
                try:
                    return next(items)
                except StopIteration:
                    stop.set()
                    raise queue.Empty

        listener = self.listener([], [], [])
        listener.prepare = lambda: None
        listener._wake_carry = "贾"
        seen = []
        listener.process_audio = lambda pcm: seen.append((pcm, listener._wake_carry)) or "ignored"
        with patch("jarvis_wake_listener.queue.Queue", return_value=SimulatedQueue()), \
                patch("jarvis_wake_listener.threading.Thread"), \
                patch("jarvis_wake_listener.time.monotonic", return_value=50.0):
            listener.run_forever(stop)
        self.assertEqual(seen, [(self.pcm, "")])

    def test_overflow_gap_cannot_join_wake_fragments_or_pending_commands(self):
        stop = threading.Event()
        items = iter([_CapturedAudio(self.pcm, 49.0, 1), _CapturedAudio(self.pcm, 49.5, 4)])

        class SimulatedQueue:
            def get(self, **_options):
                try:
                    return next(items)
                except StopIteration:
                    stop.set()
                    raise queue.Empty

        listener = self.listener([], [], [])
        listener.prepare = lambda: None
        seen = []

        def process(_pcm):
            seen.append((listener._wake_carry, listener._pending_command))
            listener._wake_carry = "贾"
            listener._pending_command = "打开"
            return "ignored"

        listener.process_audio = process
        with patch("jarvis_wake_listener.queue.Queue", return_value=SimulatedQueue()), \
                patch("jarvis_wake_listener.threading.Thread"), \
                patch("jarvis_wake_listener.time.monotonic", return_value=50.0):
            listener.run_forever(stop)
        self.assertEqual(seen, [("", ""), ("", "")])

    def test_neutral_recognition_selects_action_after_biased_wake(self):
        class DualTranscriber:
            def transcribe(self, pcm):
                return "贾维斯打开微信"

            def transcribe_plain(self, pcm):
                return "打开计算器"

        wakes, commands = [], []
        listener = WakeWordListener(
            WakeConfig(skills_path=DEFAULT_SKILLS_PATH, minimum_rms=100),
            on_wake=wakes.append,
            on_command=commands.append,
            audio_source=FakeAudioSource(),
            transcriber=DualTranscriber(),
        )
        listener._write_wake_event = lambda event: None
        self.assertEqual(listener.process_audio(self.pcm, now=100), "unconfirmed_wake")
        self.assertFalse(wakes)
        self.assertFalse(commands)

    def test_hinted_action_without_neutral_confirmation_waits(self):
        class DualTranscriber:
            def transcribe(self, pcm):
                return "贾维斯打开微信"

            def transcribe_plain(self, pcm):
                return ""

        wakes, commands = [], []
        listener = WakeWordListener(
            WakeConfig(skills_path=DEFAULT_SKILLS_PATH, minimum_rms=100),
            on_wake=wakes.append,
            on_command=commands.append,
            audio_source=FakeAudioSource(),
            transcriber=DualTranscriber(),
        )
        listener._write_wake_event = lambda event: None
        self.assertEqual(listener.process_audio(self.pcm, now=110), "unconfirmed_wake")
        self.assertFalse(wakes)
        self.assertFalse(commands)

    def test_hotwords_follow_current_skill_config(self):
        hints = load_asr_hotwords(DEFAULT_SKILLS_PATH)
        self.assertIn("贾维斯", hints)
        self.assertIn("打开计算器", hints)


if __name__ == "__main__":
    unittest.main()
