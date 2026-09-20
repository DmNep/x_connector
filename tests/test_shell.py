"""Тесты оболочки и накопления PCM для неблокирующего ALSA."""

from __future__ import annotations

import sys
import time
import unittest

from xconn_channel.audioio import take_pcm_block
from xconn_channel.shell import PipeShell
from xconn_channel.transport import AudioTransport, SampleLink
from xconn_channel import config, framing


class TestTakePcmBlock(unittest.TestCase):
    def test_partial_then_full(self) -> None:
        pending = bytearray()
        self.assertIsNone(take_pcm_block(pending, b"\x00\x01", 4))
        self.assertEqual(bytes(pending), b"\x00\x01")
        block = take_pcm_block(pending, b"\x02\x03\x04", 4)
        self.assertEqual(block, b"\x00\x01\x02\x03")
        self.assertEqual(bytes(pending), b"\x04")

    def test_empty_incoming(self) -> None:
        pending = bytearray(b"abc")
        self.assertIsNone(take_pcm_block(pending, b"", 4))


class TestPipeShell(unittest.TestCase):
    def test_echo_roundtrip(self) -> None:
        script = (
            "import sys\n"
            "sys.stdout.write('READY\\n')\n"
            "sys.stdout.flush()\n"
            "line = sys.stdin.readline()\n"
            "sys.stdout.write(line.upper())\n"
            "sys.stdout.flush()\n"
        )
        sh = PipeShell([sys.executable, "-u", "-c", script])
        try:
            got = b""
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and b"READY" not in got:
                got += sh.read()
                time.sleep(0.02)
            self.assertIn(b"READY", got)
            sh.write(b"hello\n")
            got = b""
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and b"HELLO" not in got:
                got += sh.read()
                time.sleep(0.02)
            self.assertIn(b"HELLO", got)
        finally:
            sh.close()


class TestTransportSetMode(unittest.TestCase):
    def test_probe_then_base(self) -> None:
        link = SampleLink()
        sink_a, source_a = link.end_a()
        sink_b, source_b = link.end_b()
        ta = AudioTransport(sink_a, source_a, config.PROBE)
        tb = AudioTransport(sink_b, source_b, config.PROBE)
        raw = framing.build_frame(config.HELO, 0, b"\x01\x01\x18\x50")
        ta.send(raw)
        self.assertEqual(tb.receive(5.0), raw)
        ta.set_mode(config.BASE)
        tb.set_mode(config.BASE)
        ping = framing.build_frame(config.PING, 1)
        ta.send(ping)
        self.assertEqual(tb.receive(2.0), ping)


if __name__ == "__main__":
    unittest.main()
