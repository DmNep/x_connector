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
        self._mod = Modulator(mode)
        self._dem = Demodulator(mode, gate_level)
        self._clock = clock or _Clock()
        # Тишина внутри кадра в секундах: T_IDLE в байт-времени (8.3).
        self._idle_s = config.t_idle_ms(mode) / 1000.0
        self._last_signal = None  # момент последней активности gate

    @property
    def mode(self) -> str:
        return self._mode

    # --- отправка -------------------------------------------------------------

    def send(self, frame_bytes: bytes) -> None:
        """Кадр в эфир с таймингами полудуплекса (docs/protocol.md 8.3)."""
        chunk = self._mod.silence(config.T_LEAD_MS)
        chunk += self._mod.modulate(framing.to_bits(frame_bytes))
        chunk += self._mod.silence(config.GAP_MS)
        self._sink(chunk)

    # --- приём -----------------------------------------------------------------

    def receive(self, timeout: float) -> bytes | None:
        """Ждать кадр до timeout (T_CARRIER у вызывающего).

        Возвращает байты кадра, None — таймаут. timeout <= 0 — одна
        вычитка источника без ожидания: для прокрутки из чужого цикла
        ожидания, данные либо уже накопились, либо их пока нет.

        Обрыв внутри кадра ловится по тишине: gate молчит дольше T_IDLE —
        сброс демодулятора, оборванный кадр не отравляет приём следующего.
        """
        if timeout <= 0:
            chunk = self._source()
            if chunk:
                return self._feed(chunk)
            return None

        deadline = self._clock() + timeout
        while self._clock() < deadline:
            chunk = self._source()
            if chunk:
                frame_bytes = self._feed(chunk)
                if frame_bytes is not None:
                    return frame_bytes
            self._check_idle(self._clock())
        return None

    def _feed(self, chunk) -> bytes | None:
        """Пропустить отсчёты в демодулятор. Байты кадра — при сборке.

        FrameError не возвращается: кадр испорчен, NAK — решение сессии,
        у неё есть seq и причина (docs/protocol.md 8.2). Приём продолжается.
        """
        for event in self._dem.feed(chunk):
            if isinstance(event, Frame):
                # Кадр собран: CRC уже проверена, байты восстанавливаются
                # из проверенных полей — рассинхрон невозможен.
                self._last_signal = None
                return framing.build_frame(event.type, event.seq, event.payload)
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
        self._a_out: list[int] = []
        self._b_out: list[int] = []
        self._a_pending = array.array("h")
        self._b_pending = array.array("h")

    def _deliver(self, chunk: array.array, inbox: array.array) -> None:
        if self.noise:
            inbox.extend(int(v + self._rng.uniform(-self.noise, self.noise)) for v in chunk)
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
        chunk = self._a_pending[:]
        del self._a_pending[:]
        return chunk or None

    def _take_b(self) -> array.array | None:
        chunk = self._b_pending[:]
        del self._b_pending[:]
        return chunk or None
