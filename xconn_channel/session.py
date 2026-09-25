"""Цикл обмена полудуплекса: ретраи, идемпотентность, кэш ответа.

docs/protocol.md 8. Клиент — единственный мастер: он инициирует каждый
обмен, агент никогда не начинает передачу самостоятельно. Это исключает
коллизии и делает поведение канала предсказуемым при отладке.

Обмен:

    клиент: REQ(seq) ──→ агент
    клиент: ←── агент: RESP(seq)
    клиент: ACK(seq) ──→ агент

Обе стороны не знают ни звука, ни времени: транспорт — это два каллбэка,
send(bytes) и receive(timeout_ms). Слой сессии гоняет по ним кадры,
слой модема (modulator/demodulator) отвечает за звук. Так сессия
проверяется целиком в памяти, без генерации сигнала.

Роли:

- MasterSession — сторона клиента: посылает REQ, ждёт RESP, шлёт ACK,
  ретранслирует REQ по таймауту или NAK, не более MAX_RETRY раз.
- AgentSession — сторона агента: исполняет запрос через handler, кэширует
  последний ответ. Повторный кадр с тем же seq не исполняется повторно,
  а отдаёт закешированный ответ — ретрансмиссия идемпотентна, потеря ACK
  не приводит к двойному исполнению команды (docs/protocol.md 8.2).
"""

from __future__ import annotations

import time

from . import config, framing
from .framing import Frame, FrameError
from .screen import ScreenError


class SessionError(Exception):
    """Обмен не удался после MAX_RETRY ретраев. Счётчики сохранены."""

    def __init__(
        self, message: str, attempts: int, seq: int, nak: int | None = None
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.seq = seq
        self.nak = nak


class _Clock:
    """Инъекция времени: time.monotonic в бою, управляемая в тестах."""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return time.monotonic()

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeClock(_Clock):
    def __call__(self) -> float:
        return self._now


class MasterSession:
    """Сторона мастера: цикл REQ -> RESP -> ACK с ретраями (docs/protocol.md 8.2).

    Таймауты двух видов, и это не избыточность (8.3): T_CARRIER ловит
    «агент вообще молчит» — ответ не начался; T_IDLE ловит «ответ начался
    и оборвался» — кадр прерван, ждать бессмысленно. Единый таймаут обязан
    быть больше самого большого ответа (2.09 с при N=240) и обнаруживал бы
    обрыв недопустимо медленно.
    """

    def __init__(self, send, receive, mode: str = config.DEFAULT_MODE, clock=None) -> None:
        self._send = send
        self._receive = receive
        self._mode = mode
        self._clock = clock or _Clock()
        self._seq = 0
        self.stats = {"retries": 0, "naks": 0, "exchanges": 0, "timeouts": 0}

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        """Смена режима сессии: рукопожатие поднимает probe -> base (8.5).

        check_mode внутри ловит выключенный stretch: согласование не может
        включить режим, запрещённый константой.
        """
        config.check_mode(value)
        self._mode = value

    @property
    def seq(self) -> int:
        return self._seq

    def exchange(self, frame_type: int, payload: bytes = b"", accept=None) -> Frame:
        """Один обмен: REQ(seq) -> RESP(seq) -> ACK(seq).

        accept(frame) — разбор полезной нагрузки до ACK. ScreenError
        превращается в NAK, агент шлёт SCREEN_FULL (docs/protocol.md 6.2).
        """
        attempts = 0
        req_naks = 0
        reason = "нет ответа"
        while attempts <= config.MAX_RETRY and req_naks <= config.MAX_RETRY:
            self._send(framing.build_frame(frame_type, self._seq, payload))
            reply = self._await_reply(self._seq)

            if isinstance(reply, Frame) and reply.type == config.NAK:
                code = (
                    reply.payload[1]
                    if len(reply.payload) >= 2
                    else config.NAK_CRC
                )
                self.stats["naks"] += 1
                if code == config.NAK_CRC:
                    req_naks += 1
                    reason = "REQ отвергнут агентом"
                    continue
                return self._finish(reply)

            if isinstance(reply, FrameError):
                # Ответ пришёл испорченным: NAK — запрос повтора ответа,
                # не подтверждение (docs/protocol.md 8.2). Агент
                # ретранслирует ответ немедленно, ждём повтор.
                self.stats["naks"] += 1
                if reply.seq is not None:
                    self._send(framing.nak(reply.seq, reply.code))
                retry = self._await_reply(self._seq)
                if (
                    isinstance(retry, Frame)
                    and retry.type != config.NAK
                    and retry.seq == self._seq
                ):
                    # NAK в ответ на наш NAK — не ответ: агент отверг наш
                    # RETR-кадр, следующая итерация ретранслирует REQ.
                    return self._complete(retry, accept)
                reason = "ответ испорчен повторно"
                self.stats["retries"] += 1
                attempts += 1
                continue

            if isinstance(reply, Frame) and reply.seq == self._seq:
                return self._complete(reply, accept)

            # Таймаут или чужой seq: ретрансляция того же seq.
            self.stats["timeouts"] += 1
            self.stats["retries"] += 1
            attempts += 1
            reason = "нет ответа (T_CARRIER/T_IDLE)"
            continue

        raise SessionError(
            f"обмен seq={self._seq} не удался: {reason}",
            attempts + req_naks,
            self._seq,
        )

    def _complete(self, reply: Frame, accept) -> Frame:
        """Разбор payload, затем ACK. Битый снимок — NAK, ждём FULL."""
        if accept is None:
            return self._finish(reply)
        try:
            accept(reply)
        except ScreenError as err:
            self.stats["naks"] += 1
            self._send(framing.nak(self._seq, err.code))
            retry = self._await_reply(self._seq)
            if not (
                isinstance(retry, Frame)
                and retry.seq == self._seq
                and retry.type != config.NAK
            ):
                raise SessionError(
                    f"повтор снимка seq={self._seq} не пришёл",
                    1,
                    self._seq,
                )
            try:
                accept(retry)
            except ScreenError:
                raise SessionError(
                    f"снимок seq={self._seq} не разбирается",
                    1,
                    self._seq,
                )
            return self._finish(retry)
        return self._finish(reply)

    def _finish(self, reply: Frame) -> Frame:
        """Завершение успешного обмена: ACK и продвижение seq."""
        self._send(framing.ack(self._seq))
        self._advance_seq()
        self.stats["exchanges"] += 1
        return reply

    def _advance_seq(self) -> None:
        self._seq = (self._seq + 1) % 256

    def _await_reply(self, seq: int) -> Frame | FrameError | None:
        """Ждать ответ до T_CARRIER.

        Обрыв внутри кадра по T_IDLE — работа транспорта (docs/protocol.md
        8.3): сессия видит уже собранный кадр или пусто. Один вызов
        receive с остатком T_CARRIER; пустой возврат — агент молчит.
        """
        deadline = self._clock() + config.T_CARRIER_MS / 1000.0
        remaining = deadline - self._clock()
        if remaining <= 0:
            return None
        chunk = self._receive(remaining)
        if chunk:
            return self._parse_chunk(chunk)
        return None

    def _parse_chunk(self, chunk: bytes) -> Frame | FrameError | None:
        """Разобрать принятые байты как один кадр ответа.

        Транспорт сессии отдаёт кадры целиком: кадрирование по границам
        слота — работа модемного слоя, здесь байты уже собраны.
        """
        try:
            return framing.parse_frame(chunk)
        except FrameError as error:
            return error


class AgentSession:
    """Сторона агента: приём REQ, исполнение, ответ, идемпотентность.

    Агент никогда не начинает передачу самостоятельно (docs/protocol.md 1).
    На каждый принятый кадр — ровно один ответ: результат handler для нового
    seq, закешированный ответ для повторного seq, NAK для испорченного.

    Идемпотентность (8.2): агент хранит последний обработанный seq и кэш
    последнего ответа. Повторный кадр с тем же seq не исполняется повторно,
    а заново отдаёт закешированный ответ. Потеря ACK не приводит к двойному
    исполнению команды — команда уже могла изменить состояние сервера.
    """

    def __init__(self, send, receive, handler) -> None:
        """handler(frame: Frame) -> (frame_type, payload) ответа.

        Повторный REQ с тем же seq не исполняется повторно, но ответ
        пересчитывается через replay_response (если задан): между
        ретрансляциями состояние могло измениться (деградация дельты
        до полного снимка), и закешированный ответ обязан обновиться.
        """
        self._send = send
        self._receive = receive
        self._handler = handler
        self._replay_response = None
        self._clock = _Clock()
        self._last_seq: int | None = None
        self._cached: bytes | None = None
        self.stats = {"frames": 0, "replays": 0, "naks": 0, "rejected": 0}

    def set_replay_response(self, callback) -> None:
        """callback() -> (frame_type, payload) ответа на повторный seq.

        Вызывается вместо handler при повторе seq: исполнение (запись в
        PTY, исполнение команды) не повторяется, ответ пересчитывается.
        Без callback повтор отдаёт закешированный байт в байт.
        """
        self._replay_response = callback

    def poll(self, timeout_ms: float) -> bool:
        """Одна итерация ожидания: принять кадр, ответить. True — был обмен.

        poll() не блокируется дольше timeout_ms и возвращает управление:
        агент между кадрами занят терминалом (docs/protocol.md 9).
        timeout_ms == 0 — вычитка без ожидания: один опрос источника,
        для прокрутки агента из чужого цикла ожидания.
        """
        if timeout_ms <= 0:
            chunk = self._receive(0)
            if not chunk:
                return False
            self.stats["frames"] += 1
            self._answer(chunk)
            return True

        deadline = self._clock() + timeout_ms / 1000.0
        while self._clock() < deadline:
            chunk = self._receive(0)
            if chunk:
                self.stats["frames"] += 1
                self._answer(chunk)
                return True
            time.sleep(0.001)
        return False

    def _answer(self, chunk: bytes) -> None:
        reply = self._parse(chunk)
        if reply is None:
            return
        self._send(reply)

    def _parse(self, chunk: bytes) -> bytes | None:
        try:
            request = framing.parse_frame(chunk)
        except FrameError as error:
            # Кадр испорчен: NAK, если seq читается. Мусор без заголовка —
            # молча (8.5: агент не отвечает на кадр с неверной CRC... но
            # NAK на читаемый заголовок ускоряет ретрансляцию мастера).
            self.stats["naks"] += 1
            if error.seq is None:
                self.stats["rejected"] += 1
                return None
            return framing.nak(error.seq, error.code)

        if request.type == config.ACK:
            # ACK мастера — не запрос, ответа не требует (8.2).
            return None

        if request.type == config.HELO:
            # Новый клиент всегда начинает с seq=0. Старый last_seq=0 иначе
            # отдаёт кэш (SCREEN_PART) вместо рукопожатия — эфир «жив»,
            # а connect видит не-HELO.
            reply_type, reply_payload = self._handler(request)
            reply = framing.build_frame(reply_type, request.seq, reply_payload)
            self._last_seq = request.seq
            self._cached = reply
            return reply

        if request.type == config.NAK:
            # NAK мастера — запрос повтора RESP, не новый REQ (8.2).
            # Исполнение не повторяется: отдаём кэш, а если задан
            # replay_response — пересчитываем (дельта → SCREEN_FULL).
            nak_seq = request.seq
            if self._cached is None or self._last_seq != nak_seq:
                return None
            self.stats["replays"] += 1
            if self._replay_response is not None:
                reply_type, reply_payload = self._replay_response()
                self._cached = framing.build_frame(
                    reply_type, nak_seq, reply_payload
                )
            return self._cached

        if self._last_seq is not None and request.seq == self._last_seq:
            # Повтор REQ с тем же seq: ретрансляция мастера, отдаём кэш.
            # Ответ пересчитывается, если задан replay_response: между
            # ретрансляциями состояние могло измениться, и кэш устарел.
            self.stats["replays"] += 1
            if self._replay_response is not None:
                reply_type, reply_payload = self._replay_response()
                self._cached = framing.build_frame(reply_type, request.seq, reply_payload)
            assert self._cached is not None
            return self._cached

        reply_type, reply_payload = self._handler(request)
        reply = framing.build_frame(reply_type, request.seq, reply_payload)
        self._last_seq = request.seq
        self._cached = reply
        return reply
