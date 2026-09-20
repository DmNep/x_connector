"""Тесты аудио-транспорта: сессия поверх модема (docs/protocol.md 8.1, 8.3).

Запуск: py -m unittest discover -s tests

Канал — SampleLink с шумом: как тракт между звуковыми картами. Время
реальное (monotonic): транспорт живёт таймингами полудуплекса, и
FakeClock здесь скрывал бы именно то, что проверяется — что кадр
успевает пройти за отведённые таймауты.
"""

from __future__ import annotations

import unittest

from xconn_channel import config, framing, screen
from xconn_channel.screen import Screen
from xconn_channel.session import AgentSession, MasterSession
from xconn_channel.transport import AudioTransport, SampleLink


class TestTransportRoundtrip(unittest.TestCase):
    def _pair(self, noise: float = 300.0, mode: str = config.BASE, seed: int = 11):
        link = SampleLink(noise=noise, seed=seed)
        sink_a, source_a = link.end_a()
        sink_b, source_b = link.end_b()
        ta = AudioTransport(sink_a, source_a, mode)
        tb = AudioTransport(sink_b, source_b, mode)
        return ta, tb

    def test_ping_pong_both_directions(self) -> None:
        ta, tb = self._pair()
        raw = framing.build_frame(config.PING, 0)
        ta.send(raw)
        self.assertEqual(tb.receive(2.0), raw)

        raw2 = framing.build_frame(config.PONG, 0)
        tb.send(raw2)
        self.assertEqual(ta.receive(2.0), raw2)

    def test_probe_mode(self) -> None:
        ta, tb = self._pair(mode=config.PROBE)
        raw = framing.build_frame(config.HELO, 0, b"\x01\x00\x18\x50")
        ta.send(raw)
        self.assertEqual(tb.receive(5.0), raw)

    def test_large_frame_screen_full(self) -> None:
        """Кадр с payload у лимита: снимок экрана проходит по звуку."""
        s = Screen(24, 80)
        for r in range(1, 24):
            s.set_row(r, "[  OK  ]".ljust(80).encode())
        packed = screen.serialize_full(s)
        self.assertLessEqual(len(packed), config.MAX_PAYLOAD)
        ta, tb = self._pair()
        raw = framing.build_frame(config.SCREEN_FULL, 5, packed)
        ta.send(raw)
        self.assertEqual(tb.receive(5.0), raw)

    def test_timeout_returns_none(self) -> None:
        ta, tb = self._pair()
        self.assertIsNone(tb.receive(0.3), "тишина — таймаут, не исключение")

    def test_send_timing_lead_and_gap(self) -> None:
        """Кадр окружён тишиной T_LEAD и GAP (docs/protocol.md 8.3)."""
        link = SampleLink()
        sink_a, source_a = link.end_a()
        captured: list = []

        def capturing_sink(chunk) -> None:
            captured.append(chunk)

        ta = AudioTransport(capturing_sink, source_a, config.BASE)
        ta.send(framing.build_frame(config.PING, 0))
        self.assertEqual(len(captured), 3, "T_LEAD, кадр и GAP уходят отдельно")
        chunk = captured[0] + captured[1] + captured[2]
        lead = round(config.T_LEAD_MS * config.SAMPLE_RATE / 1000)
        gap = round(config.GAP_MS * config.SAMPLE_RATE / 1000)
        # Тишина спереди и сзади: сигнал не прилипает к границам слота.
        # Отсчёт int16 — 2 байта, поэтому эталон тишины — пары нулей.
        self.assertEqual(bytes(chunk[:lead]), b"\x00\x00" * lead)
        self.assertEqual(bytes(chunk[-gap:]), b"\x00\x00" * gap)

    def test_idle_reset_recovers_next_frame(self) -> None:
        """Обрыв кадра по тишине T_IDLE не отравляет следующий кадр (8.3)."""
        ta, tb = self._pair(noise=0.0)
        raw = framing.build_frame(config.CMD, 3, b"ip a\n")
        # Оборванный кадр: половина сигнала, потом долгая тишина.
        from xconn_channel.modulator import Modulator

        mod = Modulator(config.BASE)
        half = mod.modulate(framing.to_bits(raw))
        tb._feed(half[: len(half) // 2])
        # Тишина дольше T_IDLE: демодулятор обязан сброситься.
        tb._last_signal = 0.0
        tb._check_idle(tb._idle_s + 1.0)

        # Полный кадр после обрыва принимается.
        ta.send(raw)
        self.assertEqual(tb.receive(2.0), raw)

    def test_corrupted_frame_surfaces_for_nak(self) -> None:
        """CRC-битый, но читаемый по заголовку кадр не тонет молча (8.2).

        NAK-путь без нового REQ работает только если сессия вообще узнаёт
        о порче кадра. Раньше _feed() ронял FrameError демодулятора, и
        приёмник просто молчал — от полного отсутствия сигнала это было
        неотличимо.
        """
        ta, tb = self._pair(noise=0.0)
        raw = framing.build_frame(config.CMD, 7, b"ls\n")
        corrupted = bytearray(raw)
        corrupted[5] ^= 0xFF  # портим payload, не трогая type/seq/len
        from xconn_channel.modulator import Modulator

        samples = Modulator(config.BASE).modulate(framing.to_bits(bytes(corrupted)))
        got = tb._feed(samples)
        self.assertIsNotNone(got, "испорченный кадр не должен тонуть молча")
        with self.assertRaises(framing.FrameError) as ctx:
            framing.parse_frame(got)
        self.assertEqual(ctx.exception.code, config.NAK_CRC)
        self.assertEqual(ctx.exception.seq, 7)


class TestSessionOverAudio(unittest.TestCase):
    """Сессии мастер и агент целиком поверх аудио-транспорта."""

    def _make_stack(self, noise: float = 200.0, seed: int = 42):
        link = SampleLink(noise=noise, seed=seed)
        sink_a, source_a = link.end_a()
        sink_b, source_b = link.end_b()
        tm = AudioTransport(sink_a, source_a, config.BASE)
        ta = AudioTransport(sink_b, source_b, config.BASE)

        def handler(req):
            if req.type == config.CMD:
                s = Screen()
                s.set_row(0, b"$ ok".ljust(80))
                return config.SCREEN_FULL, screen.serialize_full(s)
            return config.PONG, b""

        agent = AgentSession(ta.send, ta.receive, handler)

        def master_receive(timeout: float = 0.0) -> bytes | None:
            # Агент — как параллельный процесс: прокручивается, пока
            # есть входящие кадры, до опроса собственного входа мастера.
            while agent.poll(0):
                pass
            return tm.receive(timeout)

        master = MasterSession(tm.send, master_receive, clock=None)
        return master, agent

    def test_ping_exchange_over_audio(self) -> None:
        master, _ = self._make_stack()
        reply = master.exchange(config.PING)
        self.assertEqual(reply.type, config.PONG)
        self.assertEqual(reply.seq, 0)

    def test_cmd_returns_screen_over_audio(self) -> None:
        master, _ = self._make_stack()
        reply = master.exchange(config.CMD, b"ls -la\n")
        self.assertEqual(reply.type, config.SCREEN_FULL)
        restored = screen.parse_full(reply.payload)
        self.assertEqual(restored.row_bytes(0), b"$ ok".ljust(80))

    def test_several_exchanges_in_sequence(self) -> None:
        master, _ = self._make_stack()
        seqs = [master.exchange(config.PING).seq for _ in range(3)]
        self.assertEqual(seqs, [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
