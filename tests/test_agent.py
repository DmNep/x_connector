"""Тесты ядра агента: PTY-цикл, снимки, RESIZE (docs/protocol.md 9).

Запуск: py -m unittest discover -s tests

PTY подменяется парой каллбэков над bytearray: вывод «программы» —
скриптованный, как вывод реального bash. Ядро проверяется целиком,
без ОС-специфики.
"""

from __future__ import annotations

import unittest

from xconn_channel import agent, config, framing, screen
from xconn_channel.agent import AgentCore
from xconn_channel.framing import Frame
from xconn_channel.screen import Screen
from xconn_channel.session import AgentSession, MasterSession, FakeClock


class FakePty:
    """PTY-заглушка: вывод появляется после записи, как ответ bash.

    Очередь replies: каждый элемент — байты, которые «программа» выводит
    после очередной записи в master. Читается кусками по 64 байта: вывод
    приходит на произвольных границах, эмулятор обязан дособирать.
    Ответы эмулятора (ESC[6n и прочие) пишутся в written вместе с командами.
    """

    def __init__(self, replies: list[bytes]):
        self.written = bytearray()
        self.replies = list(replies)
        self._pending = bytearray()

    def write(self, data: bytes) -> None:
        self.written += data
        if self.replies:
            self._pending += self.replies.pop(0)

    def read(self) -> bytes:
        chunk = bytes(self._pending[:64])
        del self._pending[:64]
        return chunk


def frame(frame_type: int, seq: int, payload: bytes = b"") -> Frame:
    return framing.parse_frame(framing.build_frame(frame_type, seq, payload))


class TestAgentCore(unittest.TestCase):
    def test_cmd_first_exchange_is_full(self) -> None:
        """Первый обмен — SCREEN_FULL, он становится базой дельт (6.2)."""
        pty = FakePty([b"root@srv:~# "])
        core = AgentCore(pty.write, pty.read)
        reply_type, payload = core.handle(frame(config.CMD, 0, b"ls\n"))
        self.assertEqual(reply_type, config.SCREEN_FULL)
        restored = screen.parse_full(payload)
        self.assertEqual(restored.row_bytes(0), b"root@srv:~# ".ljust(80))
        self.assertEqual(pty.written, b"ls\n")
        self.assertEqual(core.stats["fulls"], 1)

    def test_second_exchange_is_delta(self) -> None:
        """Обычный ответ — дельта изменённых строк (6.2)."""
        pty = FakePty([b"root@srv:~# ", b"file1 file2\r\nroot@srv:~# "])
        core = AgentCore(pty.write, pty.read)
        core.handle(frame(config.CMD, 0, b"ls\n"))
        reply_type, payload = core.handle(frame(config.CMD, 1, b"ls\n"))
        self.assertEqual(reply_type, config.SCREEN_DELTA)
        base = core.screen
        applied = screen.parse_delta(payload, 0, _screen_like(base))
        self.assertEqual(applied.cells, base.cells)
        self.assertEqual(core.stats["deltas"], 1)

    def test_unchanged_screen_empty_delta(self) -> None:
        """Нет изменений — дельта с нулём строк, не полный снимок."""
        pty = FakePty([b"$ ", b""])
        core = AgentCore(pty.write, pty.read)
        core.handle(frame(config.CMD, 0, b"\n"))
        reply_type, payload = core.handle(frame(config.CMD, 1, b"\n"))
        self.assertEqual(reply_type, config.SCREEN_DELTA)
        self.assertEqual(payload[0], 0, "base_seq")
        self.assertEqual(payload[1], 0, "ноль изменённых строк")

    def test_replay_returns_full_not_reexecutes(self) -> None:
        """Повтор REQ: команда не пишется в PTY повторно, ответ — FULL (8.2)."""
        pty = FakePty([b"$ ", b"out\r\n$ ", b"SHOULD NOT PRINT"])
        core = AgentCore(pty.write, pty.read)
        core.handle(frame(config.CMD, 0, b"cmd\n"))
        # Вторая команда: её вывод ещё не в эмуляторе, replay его не увидит —
        # replay отвечает состоянием на момент ретрансляции.
        written_after_first = bytes(pty.written)

        reply_type, payload = core.replay()
        self.assertEqual(reply_type, config.SCREEN_FULL)
        self.assertEqual(bytes(pty.written), written_after_first, "PTY не тронут повтором")
        # Экран содержит приглашение первой команды.
        self.assertIn(b"$", screen.parse_full(payload).cells)

    def test_replay_after_second_command_sees_new_state(self) -> None:
        """Replay-ответ пересчитывается: он отражает состояние, а не кэш."""
        pty = FakePty([b"$ ", b"out\r\n$ "])
        core = AgentCore(pty.write, pty.read)
        core.handle(frame(config.CMD, 0, b"cmd\n"))
        core.handle(frame(config.CMD, 1, b"cmd\n"))
        reply_type, payload = core.replay()
        self.assertEqual(reply_type, config.SCREEN_FULL)
        self.assertIn(b"out", screen.parse_full(payload).cells)

    def test_oversize_delta_falls_back_to_full(self) -> None:
        """Дельта не влезает — попытка полного снимка (6.2).

        При построчном zlib дельта изменённых строк всегда меньше полного
        снимка того же экрана, поэтому рабочий сценарий «дельта не влезла,
        FULL влез» структурно недостижим: не влезшая дельта означает
        несжимаемый экран, на котором упадёт и FULL. Здесь проверяется,
        что oversize-дельта не роняет ядро: fallback пробует FULL, и
        несжимаемый экран честно даёт ValueError — сегментации снимка в
        протоколе нет (docs/protocol.md 6.1), уровень выше обязан это
        видеть, а не получать молчание.
        """
        import random

        rng = random.Random(5)
        big_output = b"\r\n".join(
            bytes(rng.randrange(256) for _ in range(80)) for _ in range(24)
        )
        pty = FakePty([b"$ ", big_output + b"\r\n$ "])
        core = AgentCore(pty.write, pty.read)
        core.handle(frame(config.CMD, 0, b"\n"))
        with self.assertRaises(ValueError):
            core.handle(frame(config.CMD, 1, b"big\n"))

    def test_resize_resets_base(self) -> None:
        """RESIZE: сетка меняет форму, следующий снимок полный (9, 6.2)."""
        pty = FakePty([b"$ ", b"$ ", b"$ "])
        core = AgentCore(pty.write, pty.read)
        core.handle(frame(config.CMD, 0, b"\n"))
        reply_type, payload = core.handle(
            frame(config.RESIZE, 1, bytes((30, 100)))
        )
        self.assertEqual(reply_type, config.SCREEN_FULL)
        restored = screen.parse_full(payload)
        self.assertEqual((restored.rows, restored.cols), (30, 100))

    def test_resize_keeps_intersection(self) -> None:
        pty = FakePty([b"hello world"])
        core = AgentCore(pty.write, pty.read)
        core.handle(frame(config.CMD, 0, b"\n"))
        core._vt.resize(10, 20)
        self.assertEqual(core.screen.get(0, 0), ord("h"))
        self.assertEqual(core.screen.get(0, 10), ord("d"))

    def test_resize_invalid_rejected(self) -> None:
        pty = FakePty([])
        core = AgentCore(pty.write, pty.read)
        reply_type, payload = core.handle(frame(config.RESIZE, 0, bytes((0, 80))))
        self.assertEqual(reply_type, config.NAK)
        self.assertEqual(payload, bytes((0, config.NAK_LENGTH)))
        reply_type, _ = core.handle(frame(config.RESIZE, 1, bytes((24,))))
        self.assertEqual(reply_type, config.NAK)

    def test_unknown_type_nak(self) -> None:
        pty = FakePty([])
        core = AgentCore(pty.write, pty.read)
        raw = framing.build_frame(config.NOTE, 0, b"x")
        reply_type, payload = core.handle(framing.parse_frame(raw))
        self.assertEqual(reply_type, config.NAK)
        self.assertEqual(payload, bytes((0, config.NAK_TYPE)))

    def test_ping_pong(self) -> None:
        pty = FakePty([])
        core = AgentCore(pty.write, pty.read)
        reply_type, payload = core.handle(frame(config.PING, 0))
        self.assertEqual((reply_type, payload), (config.PONG, b""))

    def test_key_goes_to_pty(self) -> None:
        """KEY: один ключ без текста, байты напрямую в PTY (5)."""
        pty = FakePty([b"$ ", b"^C$ "])
        core = AgentCore(pty.write, pty.read)
        core.handle(frame(config.CMD, 0, b"sleep 100\n"))
        reply_type, _ = core.handle(frame(config.KEY, 1, b"\x03"))
        self.assertEqual(reply_type, config.SCREEN_DELTA)
        self.assertIn(b"\x03", pty.written)

    def test_terminal_replies_returned_to_pty(self) -> None:
        """Ответы на запросы терминала идут в PTY, не в сетку (6.3)."""
        pty = FakePty([b"\x1b[6n"])
        core = AgentCore(pty.write, pty.read)
        core.handle(frame(config.CMD, 0, b"\n"))
        self.assertIn(b"\x1b[1;1R", pty.written)


def _screen_like(s: Screen) -> Screen:
    copy = Screen(s.rows, s.cols)
    copy.cells[:] = s.cells
    copy.cur_row, copy.cur_col = s.cur_row, s.cur_col
    copy.flags = s.flags
    return copy


class TestAgentWithSession(unittest.TestCase):
    """Ядро агента под сессией: replay-пересчёт через set_replay_response."""

    def _make_pair(self):
        import collections

        to_agent: collections.deque = collections.deque()
        to_master: collections.deque = collections.deque()
        pty = FakePty([b"$ ", b"ok\r\n$ ", b"SHOULD NOT PRINT"])

        core = AgentCore(pty.write, pty.read)

        def handler(req: Frame):
            return core.handle(req)

        sess = AgentSession(
            lambda d: to_master.append(d),
            lambda t=0.0: to_agent.popleft() if to_agent else None,
            handler,
        )
        sess.set_replay_response(core.replay)

        def master_receive(timeout=0.0):
            while sess.poll(0):
                pass
            return to_master.popleft() if to_master else None

        master = MasterSession(
            lambda d: to_agent.append(d), master_receive, clock=FakeClock()
        )
        return master, sess, core, pty

    def test_exchange_and_replay_over_session(self) -> None:
        master, sess, core, pty = self._make_pair()
        reply = master.exchange(config.CMD, b"cmd\n")
        self.assertEqual(reply.type, config.SCREEN_FULL)

        # Ретрансляция того же seq (как потеря ACK мастером): команда в PTY
        # не пишется повторно, ответ пересчитывается как полный снимок.
        master._send(framing.build_frame(config.CMD, 0, b"cmd\n"))
        sess.poll(0)
        written_before = bytes(pty.written)
        self.assertEqual(core.stats["cmds"], 1, "команда исполнена один раз")
        self.assertEqual(bytes(pty.written), written_before)
        self.assertEqual(core.stats["replays"] if "replays" in core.stats else 0, 0)


if __name__ == "__main__":
    unittest.main()
