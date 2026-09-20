"""Аудио-транспорт: сессия поверх модема (docs/protocol.md 8.1, 8.3).

Слой между сессией и звуком. Сессия видит два каллбэка: send(bytes) и
receive(timeout) -> bytes | None. Транспорт гонит кадры через модулятор
и демодулятор, соблюдая тайминги полудуплекса:

    отправка:  T_LEAD тишина | преамбула + кадр | GAP тишина

T_LEAD — пауза перед началом передачи после приёма: приёмник обязан
успеть освободить тракт. GAP — тишина между передачами при смене
направления: у звуковых карт буферизация выходит за десятки
миллисекунд, и остатки сигнала наводятся в соседний тракт.

Приём: входные отсчёты кормятся демодулятору до таймаута T_CARRIER.
Обрыв внутри кадра ловится по тишине: gate молчит дольше T_IDLE —
состояние демодулятора сбрасывается, оборванный кадр не должен
отравлять приём следующего (docs/protocol.md 8.3: T_IDLE — тишина
внутри кадра, прерывает приём).

Устройство не открывается здесь: sink и source — каллбэки. Реальный
аудио-ввод-вывод (WASAPI на ноутбуке, ALSA hw: на сервере) — обвязка
платформы, транспорт остаётся проверяемым в памяти.
"""

from __future__ import annotations

import array
import threading
import time

from . import config, framing
from .demodulator import Demodulator
from .framing import Frame, FrameError
from .modulator import Modulator


class _Clock:
    """Инъекция времени: monotonic в бою, управляемая в тестах."""

    def __call__(self) -> float:
        return time.monotonic()


class FakeClock(_Clock):
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class AudioTransport:
    """Один конец аудио-канала: кадры <-> отсчёты.

    sink(chunk: array) — куда писать исходящие отсчёты (выход звуковой
    карты). source() -> array | None — откуда брать входящие (ввод);
    None или пустой массив — данных пока нет.
    """

    def __init__(
        self,
        sink,
        source,
        mode: str = config.DEFAULT_MODE,
        gate_level: float = 512.0,
        clock=None,
    ) -> None:
        config.check_mode(mode)
        self._sink = sink
        self._source = source
        self._mode = mode
        self._gate_level = gate_level
        self._mod = Modulator(mode)
        self._dem = Demodulator(mode, gate_level)
        self._clock = clock or _Clock()
        # Тишина внутри кадра в секундах: T_IDLE в байт-времени (8.3).
        self._idle_s = config.t_idle_ms(mode) / 1000.0
        self._last_signal = None  # момент последней активности gate
        # T_LEAD нужен один раз за обмен — перед ответом на только что
        # принятый кадр (8.1, 8.4), а не перед каждой передачей. Флаг
        # взводится в _feed() при успешной сборке (Frame или FrameError с
        # raw) и гасится ближайшим send().
        self._pending_lead = False

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        """Смена режима после HELO: probe → base (docs/protocol.md 8.5).

        Модулятор и демодулятор создаются заново. Фаза и PLL с предыдущего
        режима на другой скорости не имеют смысла. Вызывать после отправки
        последнего кадра старого режима, иначе ответ HELO уйдёт уже новым
        тоном, а приёмник ещё в probe.
        """
        config.check_mode(mode)
        if mode == self._mode:
            return
        self._mode = mode
        self._mod = Modulator(mode)
        self._dem = Demodulator(mode, self._gate_level)
        self._idle_s = config.t_idle_ms(mode) / 1000.0
        self._last_signal = None
        self._pending_lead = False

    # --- отправка -------------------------------------------------------------

    def send(self, frame_bytes: bytes) -> None:
        """Кадр в эфир с таймингами полудуплекса (docs/protocol.md 8.3, 8.4).

        T_LEAD уходит первым, до расчёта синуса кадра — приёмник должен
        начать T_CARRIER, не дожидаясь, пока чистый Python посчитает все
        отсчёты длинного SCREEN_FULL, — но только когда это ответ на
        только что принятый кадр: агент отвечает RESP на REQ, клиент —
        ACK на RESP, любая сторона — NAK на испорченный кадр. Свежий REQ
        (после собственного предыдущего GAP, а не после приёма) T_LEAD не
        получает — иначе лишняя пауза перед каждой передачей набегает в
        ощутимую задержку сверх целевой (AGENTS.md 3.2, «команда в
        секунду»). Итог не совпадает с ≈1.24 с из таблицы 8.4 (та считает
        T_LEAD один раз, здесь их два — перед RESP и перед ACK, оба
        подпадают под общее определение «после приёма», раздел 10) —
        расхождение на один T_LEAD (60 мс) меньше и безопаснее, чем
        удалять паузу там, где приём только что реально был.
        """
        if self._pending_lead:
            self._sink(self._mod.silence(config.T_LEAD_MS))
            self._pending_lead = False
        self._sink(self._mod.modulate(framing.to_bits(frame_bytes)))
        self._sink(self._mod.silence(config.GAP_MS))

    # --- приём -----------------------------------------------------------------

    def receive(self, timeout: float) -> bytes | None:
        """Ждать кадр. timeout — T_CARRIER: ожидание начала несущей.

        После того как gate увидел тон, ждать до сборки кадра, обрыва по
        T_IDLE или потолка длительности максимального кадра. Нельзя
        отрезать длинный SCREEN_FULL тем же T_CARRIER: 250 мс меньше
        1.76 с снимка (docs/protocol.md 8.3, 8.4).
        """
        if timeout <= 0:
            chunk = self._source()
            if chunk:
                return self._feed(chunk)
            return None

        carrier_deadline = self._clock() + timeout
        max_frame_s = config.frame_seconds(config.MAX_PAYLOAD, self._mode) + 1.0
        frame_deadline = None
        heard = False

        while True:
            now = self._clock()
            if not heard and now >= carrier_deadline:
                return None
            if heard and frame_deadline is not None and now >= frame_deadline:
                self._dem.reset()
                self._last_signal = None
                return None

            chunk = self._source()
            if chunk:
                frame_bytes = self._feed(chunk)
                if frame_bytes is not None:
                    return frame_bytes
                if self._dem._gate.active:
                    if not heard:
                        heard = True
                        frame_deadline = now + max_frame_s
                    self._last_signal = self._clock()
                continue

            if heard:
                self._check_idle(self._clock())
                if self._last_signal is None:
                    return None
            remaining_to = frame_deadline if heard else carrier_deadline
            remaining = remaining_to - self._clock() if remaining_to is not None else 0.0
            if remaining <= 0:
                continue
            time.sleep(min(0.005, remaining))

    def _feed(self, chunk) -> bytes | None:
        """Пропустить отсчёты в демодулятор. Байты кадра — при сборке.

        Кадр с верной CRC восстанавливается из проверенных полей. Кадр,
        испорченный уже после чтения заголовка (CRC, длина, тип), тоже
        не глушится: FrameError несёт raw — те же байты, на которых
        споткнулся разбор, — и они уходят наверх как обычный ответ.
        Сессия сама вызовет framing.parse_frame() на них и получит тот же
        FrameError с seq, чтобы отправить NAK (docs/protocol.md 8.2) —
        без этого NAK-путь недостижим на реальном звуке: молчание после
        порчи кадра неотличимо от полного отсутствия сигнала. Мусор без
        читаемого заголовка (raw нет) по-прежнему отбрасывается молча
        (8.5).
        """
        for event in self._dem.feed(chunk):
            if isinstance(event, Frame):
                # Кадр собран: CRC уже проверена, байты восстанавливаются
                # из проверенных полей — рассинхрон невозможен.
                self._last_signal = None
                self._pending_lead = True
                return framing.build_frame(event.type, event.seq, event.payload)
            if isinstance(event, FrameError) and event.raw is not None:
                self._last_signal = None
                self._pending_lead = True
                return bytes(event.raw)
        if self._dem._gate.active:
            self._last_signal = self._clock()
        return None

    def _check_idle(self, now: float) -> None:
        """Сброс по тишине внутри кадра (docs/protocol.md 8.3, T_IDLE)."""
        if self._last_signal is None:
            return
        if self._dem._gate.active:
            self._last_signal = now
            return
        if now - self._last_signal > self._idle_s:
            # Обрыв: сброс, чтобы мусор оборванного кадра не съел следующий.
            self._dem.reset()
            self._last_signal = None


class SampleLink:
    """Двунаправленная петля отсчётов с шумом: отладка и тесты.

    Каждый конец пишет в свой буфер-очередь, читает из чужого. Шум
    добавляется при пересылке — как тракт между звуковыми картами.
    """

    def __init__(self, noise: float = 0.0, seed: int = 7) -> None:
        import random

        self._rng = random.Random(seed)
        self.noise = noise
        self._lock = threading.Lock()
        self._a_out: list[int] = []
        self._b_out: list[int] = []
        self._a_pending = array.array("h")
        self._b_pending = array.array("h")

    def _deliver(self, chunk: array.array, inbox: array.array) -> None:
        with self._lock:
            if self.noise:
                inbox.extend(
                    int(v + self._rng.uniform(-self.noise, self.noise)) for v in chunk
                )
            else:
                inbox.extend(chunk)

    def end_a(self):
        return (
            lambda chunk: self._deliver(chunk, self._b_pending),
            self._take_a,
        )

    def end_b(self):
        return (
            lambda chunk: self._deliver(chunk, self._a_pending),
            self._take_b,
        )

    def _take_a(self) -> array.array | None:
        with self._lock:
            chunk = self._a_pending[:]
            del self._a_pending[:]
            return chunk or None

    def _take_b(self) -> array.array | None:
        with self._lock:
            chunk = self._b_pending[:]
            del self._b_pending[:]
            return chunk or None
