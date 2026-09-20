"""Тесты калибровки тракта: tools/probe.py, docs/protocol.md 2, 11, 12.

Запуск: py -m unittest discover -s tests
"""

from __future__ import annotations

import array
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from xconn_channel import config

from tools.probe import (
    IMBALANCE_LIMIT_DB,
    agc_wander_db,
    analyze_loopback_sweep,
    dbfs,
    frequency_response,
    generate_tone,
    goertzel_power,
    is_clipped,
    measure_noise,
    peak_vs_target,
    sweep_points,
    tone_imbalance,
)


class TestTone(unittest.TestCase):
    def test_peak_is_minus_12_dbfs(self) -> None:
        tone = generate_tone(1200, 100)
        peak = max(abs(v) for v in tone)
        self.assertAlmostEqual(peak, config.PEAK_AMPLITUDE, delta=1)
        self.assertAlmostEqual(dbfs(peak), config.PEAK_DBFS, delta=0.05)

    def test_not_clipped(self) -> None:
        self.assertFalse(is_clipped(generate_tone(2200, 50)))

    def test_goertzel_peaks_on_own_tone(self) -> None:
        tone = generate_tone(1200, 200)
        own = goertzel_power(tone, 1200)
        other = goertzel_power(tone, 2200)
        self.assertGreater(own, other * 10)


class TestNoiseAndGate(unittest.TestCase):
    def test_silence_gate_is_zero(self) -> None:
        silence = array.array("h", bytes(2 * 1600))
        report = measure_noise(silence)
        self.assertEqual(report["noise_rms"], 0.0)
        self.assertEqual(report["gate_level"], 0.0)

    def test_gate_is_12_db_above_noise(self) -> None:
        noise = array.array("h", [100] * 800)
        report = measure_noise(noise)
        self.assertAlmostEqual(
            report["gate_level"] / report["noise_rms"],
            10 ** (config.GATE_MARGIN_DB / 20),
            places=6,
        )


class TestAgc(unittest.TestCase):
    def test_stable_tone_low_wander(self) -> None:
        tone = generate_tone(1000, 400)
        self.assertLess(agc_wander_db(tone), 1.0)

    def test_ramped_tone_flags_agc(self) -> None:
        n = 4000
        ramp = array.array("h")
        for i in range(n):
            env = 500 + 7000 * i / n
            ramp.append(int(env * math.sin(2 * math.pi * 1000 * i / config.SAMPLE_RATE)))
        self.assertGreater(agc_wander_db(ramp), 3.0)


class TestSweep(unittest.TestCase):
    def test_sweep_covers_protocol_band(self) -> None:
        hz = list(sweep_points())
        self.assertEqual(hz[0], 300)
        self.assertEqual(hz[-1], 3400)
        self.assertIn(1200, hz)
        self.assertIn(2200, hz)

    def test_flat_response_has_no_dead_zones(self) -> None:
        captures = [(h, generate_tone(h, 80)) for h in (300, 1200, 2200, 3400)]
        rows = frequency_response(captures)
        self.assertFalse(any(row["dead"] for row in rows))

    def test_notch_is_dead(self) -> None:
        quiet = array.array("h", [0] * len(generate_tone(2200, 80)))
        captures = [
            (1200, generate_tone(1200, 80)),
            (2200, quiet),
        ]
        rows = frequency_response(captures)
        dead = {row["hz"]: row["dead"] for row in rows}
        self.assertTrue(dead[2200])
        self.assertFalse(dead[1200])


class TestImbalance(unittest.TestCase):
    def test_equal_tones_ok(self) -> None:
        a = generate_tone(1200, 100)
        b = generate_tone(2200, 100)
        report = tone_imbalance(a, b)
        self.assertFalse(report["shift_tones_down"])
        self.assertGreater(report["db_2200_vs_1200"], -IMBALANCE_LIMIT_DB)

    def test_collapsed_2200_recommends_shift(self) -> None:
        a = generate_tone(1200, 100)
        b = array.array("h", [0] * len(a))
        report = tone_imbalance(a, b)
        self.assertTrue(report["shift_tones_down"])


class TestReport(unittest.TestCase):
    def test_selftest_mentions_gate_and_bell(self) -> None:
        noise = array.array("h", bytes(2 * 800))
        tones = {hz: generate_tone(hz, 40) for hz in (300, 1200, 2200, 3400)}
        text = analyze_loopback_sweep(noise, tones)
        self.assertIn("GATE_LEVEL", text)
        self.assertIn("1200", text)
        self.assertIn("пригодна", text)
        self.assertIn("улучшения микрофона", text)

    def test_peak_report_matches_config(self) -> None:
        report = peak_vs_target(generate_tone(1000, 50))
        self.assertEqual(report["target_peak"], config.PEAK_AMPLITUDE)
        self.assertFalse(report["clipped"])


if __name__ == "__main__":
    unittest.main()
