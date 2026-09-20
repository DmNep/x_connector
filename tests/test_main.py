"""Тесты CLI: открытие аудио-обвязки по --backend (xconn_channel/__main__.py).

Запуск: py -m unittest discover -s tests
"""

from __future__ import annotations

import unittest
from unittest import mock

from xconn_channel.__main__ import _open_transport, build_parser


class DummyDevice:
    """Заглушка AudioDevice: интересуют только переданные open_audio() kwargs."""

    def sink(self, chunk) -> None:
        pass

    def source(self):
        return None

    def close(self) -> None:
        pass


class TestOpenTransportWavBackend(unittest.TestCase):
    """--backend wav: --capture/--playback — пути к файлам, а не устройство."""

    def _args(self, argv: list[str]):
        return build_parser().parse_args(argv)

    def test_wav_backend_wires_capture_playback_to_paths(self) -> None:
        args = self._args(
            [
                "client",
                "--backend",
                "wav",
                "--capture",
                "in.wav",
                "--playback",
                "out.wav",
            ]
        )
        with mock.patch("xconn_channel.__main__.open_audio") as open_audio:
            open_audio.return_value = DummyDevice()
            _open_transport(args, "client")
        open_audio.assert_called_once_with("wav", in_path="in.wav", out_path="out.wav")

    def test_wav_backend_without_flags_passes_none(self) -> None:
        args = self._args(["agent", "--backend", "wav"])
        with mock.patch("xconn_channel.__main__.open_audio") as open_audio:
            open_audio.return_value = DummyDevice()
            _open_transport(args, "agent")
        open_audio.assert_called_once_with("wav", in_path=None, out_path=None)


if __name__ == "__main__":
    unittest.main()
