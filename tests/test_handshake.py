"""Тесты HELO-рукопожатия (docs/protocol.md 8.5).

Запуск: py -m unittest discover -s tests

Два уровня:

1. Логика: кодек HELO, согласование режима, ошибки версии — через
   in-memory тракт, без звука.
2. Сквозной: рукопожатие в probe через CPFSK-модем, смена на base и
   рабочий обмен CMD -> SCREEN_FULL — весь стек канала целиком, от байт
   команды до байт экрана через звук.
"""

from __future__ import annotations

import unittest

from xconn_channel import config, framing, handshake, screen
from xconn_channel.framing import Frame
from xconn_channel.handshake import HandshakeError, Helo, negotiate
from xconn_channel.modulator import Modulator
from xconn_channel.demodulator import Demodulator
from xconn_channel.screen import Screen
from xconn_channel.session import AgentSession, FakeClock, MasterSession


class TestHeloCodec(unittest.TestCase):
    def test_roundtrip(self) -> None:
        helo = Helo(config.PROTO_VERSION, config.BASE, 24, 80)
        self.assertEqual(handshake.decode_helo(handshake.encode_helo(helo)), helo)

    def test_payload_is_four_bytes(self) -> None:
        packed = handshake.encode_helo(Helo(1, config.PROBE, 24, 80))
        self.assertEqual(len(packed), 4)

    def test_unknown_mode_string_rejected(self) -> None:
        with self.assertRaises(HandshakeError):
            handshake.encode_helo(Helo(1, "turbo", 24, 80))

    def test_unknown_mode_code_rejected(self) -> None:
        with self.assertRaises(HandshakeError):
            handshake.decode_helo(bytes((1, 0x7F, 24, 80)))

    def test_wrong_payload_size_rejected(self) -> None:
        with self.assertRaises(HandshakeError):
            handshake.decode_helo(b"\x01\x00\x18\x50\x00")

    def test_zero_screen_rejected(self) -> None:
        with self.assertRaises(HandshakeError):
            handshake.decode_helo(bytes((1, 0, 0, 80)))


class TestNegotiate(unittest.TestCase):
    def test_desired_supported_wins(self) -> None:
        self.assertEqual(negotiate(config.BASE, (config.PROBE, config.BASE)), config.BASE)

    def test_downgrade_to_slower(self) -> None:
        """Агент без base понижает до probe — не быстрее желаемого."""
        self.assertEqual(negotiate(config.BASE, (config.PROBE,)), config.PROBE)

    def test_no_slower_falls_back_to_slowest_supported(self) -> None:
        """Клиент probe, агент умеет только base: предложение — base."""
        self.assertEqual(negotiate(config.PROBE, (config.BASE,)), config.BASE)

    def test_empty_supported_rejected(self) -> None:
        with self.assertRaises(HandshakeError):
            negotiate(config.BASE, ())


class TestHandshakeLogic(unittest.TestCase):
    """Рукопожатие через in-memory тракт (как test_session.Wire)."""

    def _make_pair(self, supported=(config.PROBE, config.BASE)):
        import collections

        to_agent: collections.deque = collections.deque()
        to_master: collections.deque = collections.deque()
        agent = None

        def master_send(data):
            to_agent.append(data)

        def master_receive(timeout=0.0):
            if agent is not None:
                while agent.poll(0):
                    pass
            return to_master.popleft() if to_master else None

        def agent_send(data):
            to_master.append(data)

        def agent_receive(timeout=0.0):
            return to_agent.popleft() if to_agent else None

        master = MasterSession(master_send, master_receive, mode=config.PROBE, clock=FakeClock())
        agent = AgentSession(agent_send, agent_receive, handshake.agent_helo_handler(supported=supported))
        return master, agent

    def test_successful_handshake(self) -> None:
        master, agent = self._make_pair()
        agent_helo = handshake.client_handshake(master, config.BASE)
        self.assertEqual(agent_helo.version, config.PROTO_VERSION)
        self.assertEqual(agent_helo.mode, config.BASE)
        self.assertEqual((agent_helo.rows, agent_helo.cols), (24, 80))

    def test_helo_retries_after_cached_nak(self) -> None:
        """seq=0 отдаёт кэш NAK — следующий seq получает живой HELO."""
        import collections

        to_agent: collections.deque = collections.deque()
        to_master: collections.deque = collections.deque()
        inner = handshake.agent_helo_handler()

        def handler(frame: Frame):
            if frame.seq == 0:
                return config.NAK, bytes((0, config.NAK_STATE))
            return inner(frame)

        def master_receive(timeout=0.0):
            while agent.poll(0):
                pass
            return to_master.popleft() if to_master else None

        agent = AgentSession(
            lambda d: to_master.append(d),
            lambda t=0.0: to_agent.popleft() if to_agent else None,
            handler,
        )
        master = MasterSession(
            lambda d: to_agent.append(d),
            master_receive,
            mode=config.PROBE,
            clock=FakeClock(),
        )
        agent_helo = handshake.client_handshake(master, config.BASE)
        self.assertEqual(agent_helo.mode, config.BASE)
        self.assertGreaterEqual(master.seq, 2)

    def test_handshake_sets_master_mode_without_caller_help(self) -> None:
        """client_handshake сама переводит master.mode (docs/protocol.md 8.5).

        Раньше это было обязанностью вызывающего: пропуск двух строк
        после client_handshake оставлял транспорт слушать старым
        режимом, пока агент уже переключился.
        """
        master, agent = self._make_pair()
        self.assertEqual(master.mode, config.PROBE)
        handshake.client_handshake(master, config.BASE)
        self.assertEqual(master.mode, config.BASE)

    def test_handshake_sets_transport_mode_when_given(self) -> None:
        """Переданный transport тоже переключается client_handshake."""
        master, agent = self._make_pair()

        class FakeTransport:
            def __init__(self) -> None:
                self.mode = config.PROBE

            def set_mode(self, mode: str) -> None:
                self.mode = mode

        transport = FakeTransport()
        handshake.client_handshake(master, config.BASE, transport)
        self.assertEqual(transport.mode, config.BASE)

    def test_version_mismatch_is_error(self) -> None:
        """Несовпадение версии — ошибка, не тихая деградация (8.5).

        Агент с чужой версией: handler отвечает HELO с версией 2, клиент
        на версии 1 обязан громко ошибиться.
        """
        import collections

        to_agent: collections.deque = collections.deque()
        to_master: collections.deque = collections.deque()

        def agent_send(data):
            to_master.append(data)

        def agent_receive(timeout=0.0):
            return to_agent.popleft() if to_agent else None

        def handler(frame: Frame):
            # Агент старой версии: HELO с версией 2.
            return config.HELO, handshake.encode_helo(Helo(2, config.BASE, 24, 80))

        agent = AgentSession(agent_send, agent_receive, handler)
        master = MasterSession(
            lambda d: to_agent.append(d),
            lambda t=0.0: (agent.poll(0) or None) and (to_master.popleft() if to_master else None),
            mode=config.PROBE,
            clock=FakeClock(),
        )
        with self.assertRaises(HandshakeError) as ctx:
            handshake.client_handshake(master, config.BASE)
        self.assertIn("версия", str(ctx.exception))

    def test_handshake_requires_probe(self) -> None:
        """Клиент обязан начинать в probe (8.5, шаг 1)."""
        master, _ = self._make_pair()
        master.mode = config.BASE
        with self.assertRaises(HandshakeError):
            handshake.client_handshake(master, config.BASE)

    def test_non_helo_frame_gets_nak_state(self) -> None:
        """Кадр не-HELO до рукопожатия — NAK_STATE: соединения ещё нет."""
        handler = handshake.agent_helo_handler()
        reply_type, reply_payload = handler(framing.parse_frame(framing.cmd(0, "ls\n")))
        self.assertEqual(reply_type, config.NAK)
        self.assertEqual(reply_payload, bytes((0, config.NAK_STATE)))

    def test_agent_downgrades_to_probe(self) -> None:
        master, _ = self._make_pair(supported=(config.PROBE,))
        agent_helo = handshake.client_handshake(master, config.BASE)
        self.assertEqual(agent_helo.mode, config.PROBE)

    def test_stretch_not_negotiated_when_disabled(self) -> None:
        """Выключенный stretch не согласовывается: check_mode в клиенте.

        STRETCH_ENABLED=False по умолчанию (docs/protocol.md 10): клиент
        обязан отказать в handshake ещё до согласования, а не после.
        """
        master, _ = self._make_pair(supported=(config.PROBE, config.STRETCH))
        with self.assertRaises(HandshakeError):
            handshake.client_handshake(master, config.STRETCH)


class TestEndToEndOverAudio(unittest.TestCase):
    """Сквозной прогон: рукопожатие и рабочий обмен через CPFSK-модем.

    Это проверка всего стека: сессии поверх транспорта, где байты ходят
    звуком. Мастер шлёт кадры, модулятор гонит их через CPFSK, тракт
    (с шумом) отдаёт демодулятору агента, кадры собираются обратно.
    """

    GATE_LEVEL = 512.0

    def _audio_wire(self, noise: bool = True):
        """Транспорт поверх модема: send модулирует, receive демодулирует.

        Полудуплекс честный: каждая сторона держит свой модулятор и
        демодулятор, GAP-тишина окружает каждый кадр (docs/protocol.md 8.3).
        """
        import array
        import random

        rng = random.Random(2026)
        master_mod = Modulator(config.PROBE)
        agent_mod = Modulator(config.PROBE)
        master_dem = Demodulator(config.PROBE, self.GATE_LEVEL)
        agent_dem = Demodulator(config.PROBE, self.GATE_LEVEL)
        master_in: list = []
        agent_in: list = []

        def modulate_into(target: list, mod: Modulator, data: bytes) -> None:
            chunk = mod.silence(config.GAP_MS)
            chunk += mod.modulate(framing.to_bits(data))
            chunk += mod.silence(config.GAP_MS)
            if noise:
                target.extend(
                    v + rng.randrange(-400, 400) for v in chunk
                )
            else:
                target.extend(chunk)

        def master_send(data: bytes) -> None:
            modulate_into(agent_in, master_mod, data)

        def agent_send(data: bytes) -> None:
            modulate_into(master_in, agent_mod, data)

        def drain(dem: Demodulator, inbox: list):
            """Забрать из инбокса всё, что уже накопилось, вернуть кадры."""
            chunk = inbox[:]
            inbox.clear()
            return [r for r in dem.feed(chunk) if isinstance(r, framing.Frame)]

        def master_receive(timeout: float = 0.0):
            frames = drain(master_dem, master_in)
            return framing.build_frame(
                frames[0].type, frames[0].seq, frames[0].payload
            ) if frames else None

        def agent_receive(timeout: float = 0.0):
            frames = drain(agent_dem, agent_in)
            return framing.build_frame(
                frames[0].type, frames[0].seq, frames[0].payload
            ) if frames else None

        return master_send, master_receive, agent_send, agent_receive

    def test_full_stack_handshake_and_exchange(self) -> None:
        """HELO в probe по звуку, затем CMD -> SCREEN_FULL в base."""
        ms, mr, as_, ar = self._audio_wire()
        master = MasterSession(ms, mr, mode=config.PROBE, clock=FakeClock())
        agent = AgentSession(as_, ar, handshake.agent_helo_handler())
        # Агент отвечает синхронно при опросе тракта мастером — как
        # параллельный процесс на сервере (см. test_session.Wire).
        agent_holder = [agent]

        def master_receive_with_agent(timeout: float = 0.0):
            a = agent_holder[0]
            while a.poll(0):
                pass
            return mr(timeout)

        master._receive = master_receive_with_agent

        agent_helo = handshake.client_handshake(master, config.BASE)
        self.assertEqual(agent_helo.version, config.PROTO_VERSION)
        self.assertEqual(agent_helo.mode, config.BASE)
        # client_handshake сам переводит сессию мастера в согласованный
        # режим. Модем в этом тесте остаётся probe-скорости (transport
        # сюда не передан) — это намеренно, проверяется именно то, что
        # сессия принимает новый режим независимо от скорости модема.
        self.assertEqual(master.mode, config.BASE)

        # Рабочий handler агента: CMD -> SCREEN_FULL со снимком.
        terminal = Screen(agent_helo.rows, agent_helo.cols)
        terminal.set_row(0, b"root@server:~# ".ljust(agent_helo.cols))

        def work_handler(frame: Frame):
            if frame.type == config.CMD:
                return config.SCREEN_FULL, screen.serialize_full(terminal)
            return config.NAK, bytes((frame.seq, config.NAK_TYPE))

        agent._handler = work_handler
        agent_holder[0] = agent
        reply = master.exchange(config.CMD, b"ip a\n")
        self.assertEqual(reply.type, config.SCREEN_FULL)
        restored = screen.parse_full(reply.payload)
        self.assertEqual(restored.row_bytes(0), b"root@server:~# ".ljust(agent_helo.cols))


if __name__ == "__main__":
    unittest.main()
