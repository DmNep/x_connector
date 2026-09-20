"""Тесты передачи файла FILE_* (docs/protocol.md 7).

Запуск: py -m unittest discover -s tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xconn_channel import config, transfer
from xconn_channel.agent import AgentCore
from xconn_channel.transfer import TransferError

from test_agent import FakePty, frame


class TestCodec(unittest.TestCase):
    def test_open_roundtrip(self) -> None:
        payload = transfer.encode_open("netplan.yaml", 12, 0xAABBCCDD)
        name, size, digest = transfer.decode_open(payload)
        self.assertEqual(name, "netplan.yaml")
        self.assertEqual(size, 12)
        self.assertEqual(digest, 0xAABBCCDD)
        self.assertEqual(len(payload), config.FILE_NAME_BYTES + 8)

    def test_path_becomes_basename(self) -> None:
        self.assertEqual(transfer.sanitize_name("../etc/passwd"), "passwd")
        self.assertEqual(transfer.sanitize_name("a/b"), "b")

    def test_rejects_dot_names(self) -> None:
        with self.assertRaises(TransferError):
            transfer.sanitize_name("..")
        with self.assertRaises(TransferError):
            transfer.sanitize_name(".")
        with self.assertRaises(TransferError):
            transfer.sanitize_name("")

    def test_basename_from_windows_path(self) -> None:
        self.assertEqual(transfer.sanitize_name("C:\\\\tmp\\\\x.conf"), "x.conf")

    def test_data_roundtrip(self) -> None:
        payload = transfer.encode_data(234, b"abc")
        offset, chunk = transfer.decode_data(payload)
        self.assertEqual(offset, 234)
        self.assertEqual(chunk, b"abc")

    def test_chunk_limit(self) -> None:
        with self.assertRaises(TransferError):
            transfer.encode_data(0, b"x" * (config.FILE_CHUNK + 1))

    def test_crc32_known(self) -> None:
        self.assertEqual(transfer.crc32(b"123456789"), 0xCBF43926)


class TestAgentFile(unittest.TestCase):
    def test_put_writes_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pty = FakePty([])
            core = AgentCore(pty.write, pty.read, file_root=tmp)
            data = b"iface eth0 inet dhcp\n"
            digest = transfer.crc32(data)
            rt, _ = core.handle(
                frame(config.FILE_OPEN, 0, transfer.encode_open("net.cfg", len(data), digest))
            )
            self.assertEqual(rt, config.NOTE)
            for offset, chunk in transfer.iter_chunks(data, size=8):
                rt, _ = core.handle(
                    frame(config.FILE_DATA, 1, transfer.encode_data(offset, chunk))
                )
                self.assertEqual(rt, config.NOTE)
            rt, payload = core.handle(
                frame(config.FILE_CLOSE, 2, transfer.encode_close(digest))
            )
            self.assertEqual(rt, config.NOTE)
            self.assertEqual(payload, bytes((config.NOTE_OK,)))
            written = Path(tmp, "net.cfg").read_bytes()
            self.assertEqual(written, data)
            self.assertEqual(core.stats["files"], 1)

    def test_close_without_open_is_nak(self) -> None:
        core = AgentCore(FakePty([]).write, FakePty([]).read, file_root=".")
        rt, payload = core.handle(
            frame(config.FILE_CLOSE, 0, transfer.encode_close(0))
        )
        self.assertEqual(rt, config.NAK)
        self.assertEqual(payload[1], config.NAK_STATE)

    def test_no_root_naks(self) -> None:
        core = AgentCore(FakePty([]).write, FakePty([]).read, file_root=None)
        rt, payload = core.handle(
            frame(
                config.FILE_OPEN,
                0,
                transfer.encode_open("a.bin", 0, transfer.crc32(b"")),
            )
        )
        self.assertEqual(rt, config.NAK)
        self.assertEqual(payload[1], config.NAK_STATE)

    def test_bad_crc_naks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core = AgentCore(FakePty([]).write, FakePty([]).read, file_root=tmp)
            data = b"hello"
            digest = transfer.crc32(data)
            core.handle(
                frame(config.FILE_OPEN, 0, transfer.encode_open("a.txt", len(data), digest))
            )
            core.handle(frame(config.FILE_DATA, 1, transfer.encode_data(0, data)))
            rt, payload = core.handle(
                frame(config.FILE_CLOSE, 2, transfer.encode_close(0xDEADBEEF))
            )
            self.assertEqual(rt, config.NAK)
            self.assertFalse(Path(tmp, "a.txt").exists())


class TestReplayFile(unittest.TestCase):
    def test_replay_after_note_stays_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core = AgentCore(FakePty([]).write, FakePty([]).read, file_root=tmp)
            core.handle(
                frame(
                    config.FILE_OPEN,
                    0,
                    transfer.encode_open("z", 0, transfer.crc32(b"")),
                )
            )
            rtype, payload = core.replay()
            self.assertEqual(rtype, config.NOTE)
            self.assertEqual(payload, bytes((config.NOTE_OK,)))


if __name__ == "__main__":
    unittest.main()
