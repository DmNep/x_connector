"""Тесты замера BER: tools/ber.py, docs/protocol.md 3.2, 11, 12.

Запуск: py -m unittest discover -s tests
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from xconn_channel import config

from tools.ber import (
    BENIGN_NOISE,
    bit_errors,
    format_report,
    measure_train,
    prbs_payload,
    run_selftest,
    selftest_ok,
)


class TestBitErrors(unittest.TestCase):
    def test_identical_zero(self) -> None:
        data = prbs_payload(40, 1)
        self.assertEqual(bit_errors(data, data), 0)

    def test_one_flipped_bit(self) -> None:
        a = bytes((0x00, 0x00))
        b = bytes((0x01, 0x00))
        self.assertEqual(bit_errors(a, b), 1)

    def test_missing_byte_counts_eight(self) -> None:
        self.assertEqual(bit_errors(b"\xff\xff", b"\xff"), 8)


class TestLoopback(unittest.TestCase):
    def test_clean_base_zero_ber(self) -> None:
        row = measure_train(config.BASE, n_frames=4, noise_sigma=0.0, seed=3)
        self.assertEqual(row["frames_ok"], 4)
        self.assertEqual(row["ber"], 0.0)
        self.assertEqual(row["fer"], 0.0)

    def test_clean_probe_zero_ber(self) -> None:
        row = measure_train(config.PROBE, n_frames=3, noise_sigma=0.0, seed=5)
        self.assertEqual(row["frames_ok"], 3)
        self.assertEqual(row["ber"], 0.0)

    def test_benign_noise_still_zero(self) -> None:
        row = measure_train(
            config.BASE, n_frames=4, noise_sigma=BENIGN_NOISE, seed=42
        )
        self.assertEqual(row["frames_ok"], 4)
        self.assertEqual(row["ber"], 0.0)

    def test_crushing_noise_loses_frames(self) -> None:
        row = measure_train(config.BASE, n_frames=4, noise_sigma=12000.0, seed=7)
        self.assertGreater(row["fer"], 0.0)


class TestReport(unittest.TestCase):
    def test_selftest_rows_pass(self) -> None:
        rows = run_selftest()
        self.assertTrue(selftest_ok(rows))
        text = format_report(rows)
        self.assertIn("BER x_connector", text)
        self.assertIn("stretch", text)
        self.assertNotIn("\u2212", text)


if __name__ == "__main__":
    unittest.main()
