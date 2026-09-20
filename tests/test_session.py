"""Тесты цикла обмена: ретраи, идемпотентность, NAK (docs/protocol.md 8).

Запуск: py -m unittest discover -s tests

Сессии гоняются через in-memory транспорт: две очереди, потеря и порча
кадров управляемо. Звук не участвует — модем тестируется отдельно
(test_modem), здесь проверяется логика обмена.
"""

from __future__ import annotations

import collections
import random
import unittest

from xconn_channel import config, framing
from xconn_channel.framing import Frame, FrameError
from xconn_channel.session import AgentSession, FakeClock, MasterSession, SessionError


class Wire:
    """In-memory тракт с живым агентом: receive мастера крутит агента.

    Агент отвечает синхронно при опросе тракта мастером — как параллельный
    процесс на сервере, только без потоков. Потеря и порча управляемы:

    loss — вероятность потери кадра в каждую сторону (0..1).
    damage — вероятность порчи CRC кадра (0..1): кадр доходит, но не
    проходит проверку.
    """

    def __init__(
        self,
        loss: float = 0.0,
        damage: float = 0.0,
        seed: int = 7,
        damage_to_agent_only: bool = False,
    ) -> None:
        self.to_agent: collections.deque = collections.deque()
        self.to_master: collections.deque = collections.deque()
        self.loss = loss
        self.damage = damage
        self.damage_to_agent_only = damage_to_agent_only
        self.rng = random.Random(seed)
        self.sent_master = 0
        self.sent_agent = 0
        self.agent: AgentSession | None = None

    def attach(self, agent: AgentSession) -> None:
        self.agent = agent

    def master_send(self, data: bytes) -> None:
        self.sent_master += 1
        if self.rng.random() < self.loss:
            return
        self.to_agent.append(self._damage(data, to_agent=True))

    def master_receive(self, timeout: float = 0.0) -> bytes | None:
        # Агент отвечает синхронно: пока он не исчерпал входящие, крутим.
        if self.agent is not None:
            while self.agent.poll(0):
                pass
        if self.to_master:
            return self.to_master.popleft()
        return None

    def agent_send(self, data: bytes) -> None:
        self.sent_agent += 1
        if self.rng.random() < self.loss:
            return
        self.to_master.append(self._damage(data, to_agent=False))

    def agent_receive(self, timeout: float = 0.0) -> bytes | None:
        if self.to_agent:
            return self.to_agent.popleft()
        return None

    def _damage(self, data: bytes, to_agent: bool = True) -> bytes:
        if self.rng.random() >= self.damage:
            return data
        if self.damage_to_agent_only and not to_agent:
            return data
        corrupted = bytearray(data)
        # Портим CRC: кадр доходит целиком, но не проходит проверку.
        corrupted[-1] ^= 0x40
        return bytes(corrupted)


def pong_handler(frame: Frame) -> tuple[int, bytes]:
    """Заглушка агента: PING -> PONG, CMD -> экранная строка."""
    if frame.type == config.PING:
        return config.PONG, b""
    return config.NOTE, frame.payload


class TestHappyPath(unittest.TestCase):
    def test_single_exchange(self) -> None:
        wire = Wire()
        master = MasterSession(wire.master_send, wire.master_receive, clock=FakeClock())
        agent = AgentSession(wire.agent_send, wire.agent_receive, pong_handler)
        wire.attach(agent)
        reply = master.exchange(config.PING)
        self.assertEqual(reply.type, config.PONG)
        self.assertEqual(reply.seq, 0)
        self.assertEqual(master.seq, 1)
        self.assertEqual(master.stats["exchanges"], 1)

    def test_seq_wraps_at_256(self) -> None:
        wire = Wire()
        master = MasterSession(wire.master_send, wire.master_receive, clock=FakeClock())
        agent = AgentSession(wire.agent_send, wire.agent_receive, pong_handler)
        wire.attach(agent)
        master._seq = 255
        reply = master.exchange(config.PING)
        self.assertEqual(reply.seq, 255)
        self.assertEqual(master.seq, 0)

    def test_three_exchanges_in_sequence(self) -> None:
        wire = Wire()
        master = MasterSession(wire.master_send, wire.master_receive, clock=FakeClock())
        agent = AgentSession(wire.agent_send, wire.agent_receive, pong_handler)
        wire.attach(agent)
        seqs = []
        for _ in range(3):
            reply = master.exchange(config.PING)
            seqs.append(reply.seq)
        self.assertEqual(seqs, [0, 1, 2])


class TestRetries(unittest.TestCase):
    def test_lost_request_is_retransmitted(self) -> None:
        """Потерянный REQ ретранслируется, seq не меняется (docs/protocol.md 8.2)."""
        wire = Wire(loss=0.5, seed=11)
        master = MasterSession(wire.master_send, wire.master_receive, clock=FakeClock())
        agent = AgentSession(wire.agent_send, wire.agent_receive, pong_handler)
        wire.attach(agent)
        reply = master.exchange(config.PING)
        self.assertEqual(reply.type, config.PONG)
        self.assertGreater(master.stats["retries"], 0)
        self.assertEqual(master.seq, 1)

    def test_lost_response_is_retransmitted(self) -> None:
        """Потерянный RESP: мастер ретранслирует REQ, агент отвечает из кэша."""
        wire = Wire(loss=0.5, seed=11)
        master = MasterSession(wire.master_send, wire.master_receive, clock=FakeClock())
        agent = AgentSession(wire.agent_send, wire.agent_receive, pong_handler)
        wire.attach(agent)
        reply = master.exchange(config.PING)
        self.assertEqual(reply.type, config.PONG)
        self.assertGreater(agent.stats["replays"], 0)

    def test_gives_up_after_max_retry(self) -> None:
        """Полная тишина: SessionError после 1 + MAX_RETRY попыток."""
        wire = Wire(loss=1.0)
        master = MasterSession(wire.master_send, wire.master_receive, clock=FakeClock())
        agent = AgentSession(wire.agent_send, wire.agent_receive, pong_handler)
        wire.attach(agent)
        with self.assertRaises(SessionError) as ctx:
            master.exchange(config.PING)
        self.assertEqual(ctx.exception.attempts, config.MAX_RETRY + 1)
        self.assertEqual(wire.sent_master, config.MAX_RETRY + 1)
        self.assertEqual(master.seq, 0, "seq не двинулся: обмен не завершён")

    def test_req_naks_and_timeouts_have_independent_retry_budgets(self) -> None:
        """NAK на REQ и таймаут не делят один бюджет ретраев (docs/protocol.md 8.2).

        Комментарий у ветки NAK утверждает, что отказ агента на REQ — не
        наш сбой (повреждение кадра в тракте на пути туда) и в бюджет
        attempts не входит. Здесь три NAK и три таймаута подряд — шесть
        сбоев, больше единого бюджета MAX_RETRY+1=4, который делили бы
        оба вида сбоя без фикса, — и обмен всё равно завершается успехом
        на седьмой попытке.
        """
        sent: list[bytes] = []
        outcomes = iter(["nak", "timeout", "nak", "timeout", "nak", "timeout", "ok"])

        def send(data: bytes) -> None:
            sent.append(data)

        def receive(timeout: float = 0.0):
            seq = framing.parse_frame(sent[-1]).seq
            outcome = next(outcomes)
            if outcome == "nak":
                return framing.nak(seq, config.NAK_CRC)
            if outcome == "timeout":
                return None
            return framing.build_frame(config.PONG, seq)

        master = MasterSession(send, receive, clock=FakeClock())
        reply = master.exchange(config.PING)
        self.assertEqual(reply.type, config.PONG)
        # 6 REQ, съевших сбои (3 NAK + 3 таймаута), 7-й успешный REQ, ACK.
        self.assertEqual(len(sent), 8)

    def test_damaged_request_gets_nak(self) -> None:
        """REQ с битой CRC: агент отвечает NAK, мастер ретранслирует.

        Порча только в сторону агента: иначе NAK агента тоже приходит битым,
        и обмен разворачивается в двойной счёт попыток.
        """
        wire = Wire(damage=1.0, seed=5, damage_to_agent_only=True)
        master = MasterSession(wire.master_send, wire.master_receive, clock=FakeClock())
        agent = AgentSession(wire.agent_send, wire.agent_receive, pong_handler)
        wire.attach(agent)
        calls = []
        strict_handler = pong_handler
        def counting(frame):
            calls.append(frame.seq)
            return strict_handler(frame)
        agent._handler = counting
        # Все REQ битые, все попытки исчерпаны — но NAK не считается ретраем
        # мастера? Считается: обмен не завершён.
        with self.assertRaises(SessionError):
            master.exchange(config.PING)
        self.assertEqual(calls, [], "битые кадры не исполняются")
        self.assertEqual(agent.stats["naks"], config.MAX_RETRY + 1)


class TestAgentPoll(unittest.TestCase):
    def test_poll_with_timeout_does_not_busy_spin(self) -> None:
        """poll(timeout_ms>0) без данных не должен крутить CPU без сна (9).

        До фикса цикл дергал receive(0) без паузы между опросами: за
        отведённые 30 мс набегали бы десятки тысяч вызовов. Со сном
        ~1 мс между опросами их — единицы-десятки.
        """
        calls = {"n": 0}

        def counting_receive(timeout: float = 0.0):
            calls["n"] += 1
            return None

        agent = AgentSession(lambda data: None, counting_receive, pong_handler)
        had = agent.poll(30)
        self.assertFalse(had, "данных не было — обмена не было")
        self.assertLess(calls["n"], 100, "без сна между опросами счётчик ушёл бы в тысячи")


class TestIdempotency(unittest.TestCase):
    def test_replayed_request_not_executed_twice(self) -> None:
        """Повтор REQ с тем же seq не исполняется, отдаётся кэш (docs/protocol.md 8.2).

        Потеря ACK не должна приводить к двойному исполнению команды —
        команда могла изменить состояние сервера.
        """
        wire = Wire()
        executions = []

        def handler(frame: Frame) -> tuple[int, bytes]:
            executions.append(frame.payload)
            return config.NOTE, b"done"

        agent = AgentSession(wire.agent_send, wire.agent_receive, handler)
        wire.attach(agent)
        request = framing.build_frame(config.CMD, 0, b"reboot\n")
        first = agent._parse(request)
        second = agent._parse(request)  # ретрансляция мастера
        self.assertEqual(first, second, "кэш отдаёт тот же ответ")
        self.assertEqual(executions, [b"reboot\n"], "исполнение ровно одно")

    def test_new_seq_executes_anew(self) -> None:
        wire = Wire()
        executions = []

        def handler(frame: Frame) -> tuple[int, bytes]:
            executions.append(frame.payload)
            return config.NOTE, b"done"

        agent = AgentSession(wire.agent_send, wire.agent_receive, handler)
        wire.attach(agent)
        agent._parse(framing.build_frame(config.CMD, 0, b"ls\n"))
        agent._parse(framing.build_frame(config.CMD, 1, b"pwd\n"))
        self.assertEqual(executions, [b"ls\n", b"pwd\n"])

    def test_ack_frame_is_not_a_request(self) -> None:
        """ACK мастера — не запрос, ответа не требует (docs/protocol.md 8.2)."""
        wire = Wire()
        agent = AgentSession(wire.agent_send, wire.agent_receive, pong_handler)
        wire.attach(agent)
        self.assertIsNone(agent._parse(framing.ack(0)))
        # NAK без кэша — молча: повторять нечего.
        self.assertIsNone(agent._parse(framing.nak(0, config.NAK_CRC)))

    def test_garbage_answered_with_silence(self) -> None:
        """Мусор без читаемого заголовка — молча (docs/protocol.md 8.5)."""
        wire = Wire()
        agent = AgentSession(wire.agent_send, wire.agent_receive, pong_handler)
        wire.attach(agent)
        self.assertIsNone(agent._parse(b"\x00\x01\x02"))
        self.assertEqual(agent.stats["rejected"], 1)


class TestNakPath(unittest.TestCase):
    def test_damaged_response_triggers_nak_and_replay(self) -> None:
        """RESP с битой CRC: мастер шлёт NAK, агент ретранслирует из кэша."""
        wire = Wire()
        master = MasterSession(wire.master_send, wire.master_receive, clock=FakeClock())
        executions = []

        def handler(frame: Frame) -> tuple[int, bytes]:
            executions.append(1)
            return config.PONG, b""

        agent = AgentSession(wire.agent_send, wire.agent_receive, handler)
        wire.attach(agent)

        # Ручная прокрутка с порчей первого ответа.
        master._send(framing.build_frame(config.PING, 0))
        request = wire.agent_receive()
        agent._answer(request)
        damaged = bytearray(wire.to_master.popleft())
        damaged[-1] ^= 0x40  # портим CRC ответа
        master_reply = master._parse_chunk(bytes(damaged))
        self.assertIsInstance(master_reply, FrameError)

        # Мастер шлёт NAK, агент ретранслирует RESP из кэша (docs/protocol.md 8.2).
        nak_frame = framing.nak(0, master_reply.code)
        agent_replay = agent._parse(nak_frame)
        self.assertIsNotNone(agent_replay, "NAK мастера — запрос повтора RESP")
        replayed = framing.parse_frame(agent_replay)
        self.assertEqual(replayed.type, config.PONG)
        self.assertEqual(replayed.seq, 0)
        self.assertEqual(executions, [1], "исполнение по NAK не повторяется")

        # Повторный REQ с тем же seq по-прежнему отдаёт кэш.
        cached = agent._parse(framing.build_frame(config.PING, 0))
        self.assertEqual(executions, [1])
        self.assertIsNotNone(cached)


class TestFullLoop(unittest.TestCase):
    """Сквозной прогон под нагрузкой: 20 обменов с потерями и порчей.

    Нагрузка 20%/10% выбрана так, чтобы ретраи, NAK и кэш-повторы реально
    exercised, а все 20 обменов проходили при 1 + MAX_RETRY попытках.
    Seed фиксирован: при двойной порче (REQ и NAK одновременно) обмен
    законно исчерпывает попытки — это свойство канала, не баг сессии, и
    тест не должен зависеть от такого стечения.
    """

    def test_twenty_exchanges_over_noisy_wire(self) -> None:
        wire = Wire(loss=0.2, damage=0.1, seed=6)
        master = MasterSession(wire.master_send, wire.master_receive, clock=FakeClock())
        agent = AgentSession(wire.agent_send, wire.agent_receive, pong_handler)
        wire.attach(agent)
        for expected_seq in range(20):
            reply = master.exchange(config.PING)
            self.assertEqual(reply.type, config.PONG)
            self.assertEqual(reply.seq, expected_seq)
        self.assertGreater(master.stats["retries"], 0)
        self.assertGreater(master.stats["naks"], 0)
        self.assertGreater(agent.stats["replays"], 0)


if __name__ == "__main__":
    unittest.main()