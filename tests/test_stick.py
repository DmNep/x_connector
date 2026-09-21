"""Тесты записи установочной флешки: xconn_channel.stick, AGENTS.md 2.5.

Запуск: py -m unittest discover -s tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xconn_channel import __main__ as cli
from xconn_channel import __version__
from xconn_channel.stick import write_stick


_FORBIDDEN = ("apt-get", "apt ", "pip install", "curl ", "wget ", "http://", "https://")


class TestWriteStick(unittest.TestCase):
    def test_writes_package_and_installer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_stick(tmp)
            self.assertTrue((dest / "xconn_channel" / "__init__.py").is_file())
            self.assertTrue((dest / "xconn_channel" / "agent.py").is_file())
            self.assertTrue((dest / "install.sh").is_file())
            self.assertTrue((dest / "run-agent.sh").is_file())
            self.assertTrue((dest / "xconn-agent.service").is_file())
            self.assertTrue((dest / "README.txt").is_file())
            self.assertEqual((dest / "VERSION").read_text(encoding="utf-8").strip(), __version__)
            self.assertFalse((dest / "xconn_channel" / "__pycache__").exists())

    def test_scripts_are_lf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_stick(tmp)
            raw = (dest / "install.sh").read_bytes()
            self.assertNotIn(b"\r", raw)
            self.assertTrue(raw.startswith(b"#!/bin/sh"))

    def test_installer_stays_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_stick(tmp)
            text = (dest / "install.sh").read_text(encoding="utf-8")
        for token in _FORBIDDEN:
            self.assertNotIn(token, text, token)
        self.assertIn("dpkg -i", text)
        self.assertIn("без apt", text)
        self.assertIn("XCONN_PREFIX", text)

    def test_service_denies_ip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_stick(tmp)
            unit = (dest / "xconn-agent.service").read_text(encoding="utf-8")
            self.assertIn("IPAddressDeny=any", unit)
            self.assertIn("run-agent.sh", unit)

    def test_overwrite_existing_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest / "xconn_channel").mkdir()
            (dest / "xconn_channel" / "old.txt").write_text("x", encoding="utf-8")
            write_stick(dest)
            self.assertFalse((dest / "xconn_channel" / "old.txt").exists())
            self.assertTrue((dest / "xconn_channel" / "agent.py").is_file())


class TestStickCli(unittest.TestCase):
    def test_missing_dest_exits_2(self) -> None:
        self.assertEqual(cli.main(["stick"]), 2)

    def test_cli_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli.main(["stick", tmp]), 0)
            self.assertTrue(Path(tmp, "install.sh").is_file())


if __name__ == "__main__":
    unittest.main()
