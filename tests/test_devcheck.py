"""Тесты понятных ошибок звуковых устройств (без открытия карты).

Запуск: py -m unittest discover -s tests
"""

from __future__ import annotations

import io
import sys
import unittest
from unittest.mock import patch

from xconn_channel.devcheck import (
    DeviceError,
    explain_oserror,
    format_device_list,
    missing_capture_text,
    missing_playback_text,
    parse_winmm_index,
    unknown_index_text,
)
from xconn_channel import __main__ as cli


class TestMessages(unittest.TestCase):
    def test_no_mic_mentions_settings_and_outputs(self) -> None:
        text = missing_capture_text(["BENQ E2220HD"])
        self.assertIn("нет устройства записи", text)
        self.assertIn("микрофон", text)
        self.assertIn("Звук -> Ввод", text)
        self.assertIn("BENQ E2220HD", text)
        self.assertNotIn("код 2", text)

    def test_no_speaker_mentions_outputs(self) -> None:
        text = missing_playback_text(["HUAWEI FreeBuds"])
        self.assertIn("нет устройства воспроизведения", text)
        self.assertIn("HUAWEI FreeBuds", text)

    def test_both_missing(self) -> None:
        text = missing_capture_text([])
        self.assertIn("тоже нет", text)

    def test_bad_index_lists_devices(self) -> None:
        text = unknown_index_text("in", 5, ["mic0"])
        self.assertIn("вход 5 не существует", text)
        self.assertIn("вход 0: mic0", text)
        self.assertIn("devices", text)

    def test_oserror_wavein_is_plain(self) -> None:
        text = explain_oserror(OSError("waveInOpen: код 2"))
        self.assertIn("не удалось открыть вход", text)
        self.assertIn("нет микрофона", text)

    def test_oserror_waveout_is_plain(self) -> None:
        text = explain_oserror(OSError("waveOutOpen: код 2"))
        self.assertIn("не удалось открыть выход", text)

    def test_device_list_empty_marks_none(self) -> None:
        text = format_device_list([], ["speakers"])
        self.assertIn("(нет устройств)", text)
        self.assertIn("0: speakers", text)

    def test_parse_winmm_index_rejects_non_int(self) -> None:
        with self.assertRaises(DeviceError) as ctx:
            parse_winmm_index("mic", "capture")
        self.assertIn("не номер", str(ctx.exception))
        self.assertEqual(parse_winmm_index(None, "capture"), -1)
        self.assertEqual(parse_winmm_index("0", "capture"), 0)


class TestCliDevices(unittest.TestCase):
    def test_devices_subcommand_exists(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["devices"])
        self.assertTrue(callable(args.func))

    def test_client_prints_plain_error(self) -> None:
        captured = io.StringIO()
        err = DeviceError("нет устройства записи (микрофон или линейный вход).")
        with patch.object(cli, "check_winmm", side_effect=err):
            with patch.object(sys, "stderr", captured):
                code = cli.main(["client", "--repl"])
        self.assertEqual(code, 2)
        self.assertIn("нет устройства записи", captured.getvalue())
        self.assertNotIn("Traceback", captured.getvalue())
        self.assertNotIn("код 2", captured.getvalue())

    def test_parse_winmm_index_rejects_non_int(self) -> None:
        with self.assertRaises(DeviceError) as ctx:
            parse_winmm_index("mic", "capture")
        self.assertIn("не номер", str(ctx.exception))
        self.assertEqual(parse_winmm_index(None, "capture"), -1)
        self.assertEqual(parse_winmm_index("0", "capture"), 0)


if __name__ == "__main__":
    unittest.main()
