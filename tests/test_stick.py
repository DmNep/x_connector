"""Тесты записи установочной флешки: xconn_channel.stick, AGENTS.md 2.5.

Запуск: py -m unittest discover -s tests
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from xconn_channel import __main__ as cli
from xconn_channel import __version__
from xconn_channel.stick import (
    LinuxPython,
    StickError,
    cached_python_archives,
    ensure_linux_python,
    write_stick,
)


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
            self.assertTrue((dest / "python-linux" / "README.txt").is_file())

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
        self.assertIn("tar -xzf", text)
        self.assertIn("python/bin/python3", text)
        self.assertIn("alsa-debs", text)
        self.assertIn("aplay", text)

    def test_run_agent_prefers_bundled_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_stick(tmp)
            text = (dest / "run-agent.sh").read_text(encoding="utf-8")
        self.assertIn("$PREFIX/python/bin/python3", text)
        self.assertIn('exec "$PYTHON"', text)

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

    def test_copies_cached_linux_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            name = (
                "cpython-3.12.14+20260901-x86_64-unknown-linux-gnu-"
                "install_only_stripped.tar.gz"
            )
            (cache / name).write_bytes(b"archive")
            dest = write_stick(Path(tmp) / "stick", cache=cache)
            self.assertEqual((dest / "python-linux" / name).read_bytes(), b"archive")


class TestFetchLinuxPython(unittest.TestCase):
    def test_downloads_and_reuses_cache(self) -> None:
        payload = b"python-bytes"
        asset = LinuxPython(
            arch="x86_64",
            filename="tiny.tar.gz",
            url="https://example.test/tiny.tar.gz",
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
        )
        hits = {"n": 0}

        class Resp:
            def __init__(self) -> None:
                self._data = payload
                self._pos = 0

            def read(self, n: int = -1) -> bytes:
                if n < 0:
                    chunk = self._data[self._pos :]
                    self._pos = len(self._data)
                    return chunk
                chunk = self._data[self._pos : self._pos + n]
                self._pos += len(chunk)
                return chunk

            def __enter__(self) -> "Resp":
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

        def opener(_request: object) -> Resp:
            hits["n"] += 1
            return Resp()

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            first = ensure_linux_python(
                cache=cache, opener=opener, assets=(asset,)
            )
            second = ensure_linux_python(
                cache=cache, opener=opener, assets=(asset,)
            )
            self.assertEqual(hits["n"], 1)
            self.assertEqual(first[0].read_bytes(), payload)
            self.assertEqual(second[0], first[0])
            self.assertEqual(
                cached_python_archives(cache, assets=(asset,)),
                first,
            )

    def test_rejects_bad_hash(self) -> None:
        asset = LinuxPython(
            arch="x86_64",
            filename="tiny.tar.gz",
            url="https://example.test/tiny.tar.gz",
            sha256="0" * 64,
            size=4,
        )

        class Resp:
            def __init__(self) -> None:
                self._data = b"xxxx"
                self._pos = 0

            def read(self, n: int = -1) -> bytes:
                if n < 0:
                    chunk = self._data[self._pos :]
                    self._pos = len(self._data)
                    return chunk
                chunk = self._data[self._pos : self._pos + n]
                self._pos += len(chunk)
                return chunk

            def __enter__(self) -> "Resp":
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(StickError):
                ensure_linux_python(
                    cache=Path(tmp),
                    opener=lambda _req: Resp(),
                    assets=(asset,),
                )
            self.assertFalse((Path(tmp) / "tiny.tar.gz").exists())
            self.assertFalse((Path(tmp) / "tiny.tar.gz.part").exists())


class TestStickCli(unittest.TestCase):
    def test_missing_dest_exits_2(self) -> None:
        self.assertEqual(cli.main(["stick"]), 2)

    def test_cli_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli.main(["stick", "--no-fetch", tmp]), 0)
            self.assertTrue(Path(tmp, "install.sh").is_file())


if __name__ == "__main__":
    unittest.main()
