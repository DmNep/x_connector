"""Тесты живого тракта калибровки без звуковой карты.

Запуск: py -m unittest discover -s tests
"""

from __future__ import annotations

import array
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from xconn_channel import config
from xconn_channel.audioio import BLOCK_SAMPLES_16K

from tools.ber import measure_live
from tools.live import EchoDevice, find_onset, iter_blocks, play_and_capture
from tools.probe import (
    build_live_stimulus,
    generate_tone,
    run_live_probe,
    slice_live_capture,
)


class TestEchoRoundtrip(unittest.TestCase):
    def test_play_returns_padded_copy(self) -> None:
        device = EchoDevice()
        src = array.array("h", [100, -100, 50])
        captured = play_and_capture(device, src, drain_ms=0, sleep=lambda _t: None)
        expected = 0
        for _ in iter_blocks(src):
            expected += BLOCK_SAMPLES_16K
        self.assertEqual(len(captured), expected)
        self.assertEqual(list(captured[:3]), [100, -100, 50])

    def test_onset_skips_leading_silence(self) -> None:
        lead = array.array("h", [0] * 800)
        tone = generate_tone(1200, 40)
        onset = find_onset(lead + tone)
        self.assertGreaterEqual(onset, 700)
        self.assertLess(onset, 800 + 80)

    def test_onset_silence_is_none(self) -> None:
        """Тишина — не «фронт на нулевом отсчёте», а «сигнала нет» (12)."""
        self.assertIsNone(find_onset(array.array("h", [0] * 400)))

    def test_onset_too_short_is_none(self) -> None:
        self.assertIsNone(find_onset(array.array("h", [0] * 10)))


class TestProbeLiveEcho(unittest.TestCase):
    def test_slice_recovers_sweep_freqs(self) -> None:
        stim = build_live_stimulus(tone_ms=40, silence_ms=50, gap_ms=10, tail_ms=20)
        delay = array.array("h", [0] * 320)
        noise, tones = slice_live_capture(delay + stim["samples"], stim)
        self.assertGreater(len(noise), 0)
        self.assertIn(1200, tones)
        self.assertIn(2200, tones)
        self.assertGreater(len(tones[1200]), 0)

    def test_run_live_probe_on_echo(self) -> None:
        text = run_live_probe(EchoDevice())
        self.assertIn("GATE_LEVEL", text)
        self.assertIn("1200", text)

    def test_dead_loopback_raises_not_silent_report(self) -> None:
        """Немая петля (нет тона совсем) обязана явно упасть, не отчитаться (12).

        До фикса find_onset()==0 для «нет сигнала» был неотличим от
        «фронт на нулевом отсчёте», и slice_live_capture тихо строила
        отчёт из тишины/шума вместо явной ошибки.
        """
        stim = build_live_stimulus(tone_ms=40, silence_ms=50, gap_ms=10, tail_ms=20)
        silence = array.array("h", [0] * len(stim["samples"]))
        with self.assertRaises(SystemExit):
            slice_live_capture(silence, stim)


class TestBerLiveEcho(unittest.TestCase):
    def test_echo_base_zero_ber(self) -> None:
        row = measure_live(EchoDevice(), config.BASE, n_frames=3, seed=2)
        self.assertEqual(row["frames_ok"], 3)
        self.assertEqual(row["ber"], 0.0)


if __name__ == "__main__":
    unittest.main()
