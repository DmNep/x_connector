"""Демодулятор CPFSK: отсчёты в биты и кадры, docs/protocol.md 11.

Порядок обработки, сверху вниз, каждый шаг — отдельная сущность:

1. Energy gate. Скользящая RMS по окну 8 мс; ниже GATE_LEVEL — тишина,
   состояние сбрасывается. Порог не константа: он берётся на GATE_MARGIN_DB
   выше уровня шума, измеренного tools/probe.py на реальном тракте.
2. Захват преамбулы. Autocorrelation на лаге двух бит: паттерн 0x55 имеет
   строгий период 2 бита, пик корреляции даёт грубую тактовую частоту и
   фазу. Реализован в PreambleTracker.
3. Goertzel-детектор. Два скользящих фильтра с окном W = round(samples_per_bit),
   шаг 1 отсчёт, на каждом шаге энергии E_mark и E_space. Это не блочный
   goertzel_power из tools/probe.py: калибровке достаточно одного числа на
   весь захват, модему нужен поток решений.
4. Решение с гистерезисом. mark при E_mark > K * E_space, space при
   обратном, иначе удержание предыдущего решения. K = DETECT_K.
5. Тактовый PLL. Ошибка момента перехода относительно ожидаемого момента
   умножается на PLL_GAIN и добавляется к тактовой частоте. Компенсирует
   расхождение кварцев двух звуковых карт.
6. Разбор кадра — BitCollector из framing, сюда не входит.

Поиск sync перебором фазы живёт в BitCollector._hunt_sync: пока sync не
найден, окно сдвигается на один бит до совпадения с паттерном 0x7E в 8N1.
Жадно разбирать байты в этом режиме нельзя: преамбула — чередование, при
неверной фазе она даёт корректно обрамлённые байты 0x55, приёмник считает
фазу верной и пропускает sync ровно на величину сдвига (docs/protocol.md
11, шаг 3).

Все числа — float и без numpy: внешних зависимостей нет по требованию
автономности сервера (AGENTS.md 3.2, 3.3).
"""

from __future__ import annotations

import array
import collections
import math

from . import config
from .framing import BitCollector, Frame, FrameError


# --- Прореживание ------------------------------------------------------------


def decimate(samples: array.array, factor: int = config.DECIMATION) -> array.array:
    """48 кГц -> 16 кГц скользящим средним по 3 отсчёта (docs/protocol.md 2).

    Среднее, а не прореживание выборкой: среднее — дешёвый ФНЧ, давящий
    зеркальные компоненты выше 8 кГц, которые иначе попали бы в полосу
    тонов. Фазовая характеристика фильтра приёмнику безразлична: PLL
    работает по моментам переходов, а не по форме волны.
    """
    if factor <= 0:
        raise ValueError(f"фактор прореживания {factor} обязан быть положительным")
    out = array.array("h")
    for i in range(0, len(samples) - factor + 1, factor):
        acc = 0
        for j in range(factor):
            acc += samples[i + j]
        out.append(int(acc / factor))
    return out


# --- Energy gate ---------------------------------------------------------------


def rms_level(samples) -> float:
    """RMS по переданному фрагменту, в единицах шкалы int16."""
    if not len(samples):
        return 0.0
    acc = 0
    for v in samples:
        acc += v * v
    return math.sqrt(acc / len(samples))


def gate_level_from_noise(noise_rms: float) -> float:
    """Порог energy gate: на GATE_MARGIN_DB выше измеренного шума."""
    return noise_rms * (10.0 ** (config.GATE_MARGIN_DB / 20.0))


class EnergyGate:
    """Скользящая RMS по окну 8 мс с гистерезисом включения.

    Гистерезис (порог отпускания вдвое ниже порога срабатывания) нужен
    против дребезга на огибающей CPFSK: энергия тона колеблется вокруг
    порога на переходах, и без гистерезиса gate рвёт кадр на каждом
    переходе mark/space.

    Сумма квадратов поддерживается инкрементально: O(1) на отсчёт, иначе
    пересчёт окна на каждом из 16000 отсчётов в секунду тормозил бы
    приёмник на чистом Python. Само окно — deque, а не list: выброс
    самого старого отсчёта через pop(0) сдвигал бы весь список на
    каждом отсчёте, тот же O(W), от которого спасает инкрементальная
    сумма; popleft() — O(1).
    """

    def __init__(self, level: float, sample_rate: int = config.SAMPLE_RATE) -> None:
        if level <= 0:
            raise ValueError(f"порог gate {level} обязан быть положительным")
        self._level = level
        self._release = level / 2.0
        self._window = max(1, round(0.008 * sample_rate))
        self._sum_sq = 0.0
        self._queue: collections.deque[float] = collections.deque()
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def level(self) -> float:
        """Текущая RMS окна."""
        if not self._queue:
            return 0.0
        return math.sqrt(self._sum_sq / len(self._queue))

    def feed(self, samples) -> bool:
        """Пропустить отсчёты, вернуть True — gate активен после фрагмента."""
        for v in samples:
            x = float(v)
            self._queue.append(x)
            self._sum_sq += x * x
            if len(self._queue) > self._window:
                old = self._queue.popleft()
                self._sum_sq -= old * old
            rms = math.sqrt(self._sum_sq / len(self._queue))
            if rms >= (self._release if self._active else self._level):
                self._active = True
            else:
                self._active = False
        return self._active


# --- Захват преамбулы ----------------------------------------------------------


class PreambleTracker:
    """Autocorrelation на лаге двух бит: у паттерна 0x55 период ровно 2 бита.

    Сдвиг окном по сигналу, на каждом сдвиге корреляция знакопеременной
    составляющей с самой собой на лаге 2 бита. Чередование даёт |corr|
    около 0.5 в любом режиме, шум и данные — около 0.1. Захват идёт по
    модулю корреляции: знак зависит от того, как набег фазы за лаг
    раскладывается между тонами, и от режима (в base +0.5, в probe −0.5).

    Даёт грубую фазу бита: позиция пика корреляции кратна половине периода,
    точная фаза доуточняется PLL на sync-байте.
    """

    ACQUIRE_RATIO = 0.3

    def __init__(self, mode: str = config.DEFAULT_MODE) -> None:
        config.check_mode(mode)
        # Дробный лаг, не округлённый: 2 бита при probe это 106.67 отсчёта,
        # округление до 107 уводит фазу space-тона (период 7.27) на 0.7
        # периода, и корреляция на space-бите становится отрицательной —
        # захват срабатывает только на mark-участке, втрое позже.
        self._lag = 2.0 * config.samples_per_bit(mode)
        self._window = 4 * round(self._lag)
        # deque(maxlen=window): _correlate() читает только последние
        # self._window отсчётов, старые вытесняются сами за O(1) на
        # append — раньше здесь держали list вдвое длиннее окна и раз в
        # отсчёт делали del history[:1], который на Python-списке — это
        # O(window) сдвиг всех оставшихся элементов, на каждый отсчёт.
        self._history: collections.deque[float] = collections.deque(maxlen=self._window)
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def feed(self, samples) -> bool:
        """Накопить отсчёты (deque.maxlen сдвигает окно сам), вернуть True при захвате."""
        self._history.extend(float(v) for v in samples)
        if not self._acquired:
            self._acquired = abs(self._correlate()) >= self.ACQUIRE_RATIO
        return self._acquired

    def _correlate(self) -> float:
        """Нормированная корреляция с лагом 2 бита по последнему окну.

        Дробный лаг: отсчёт сравнивается с интерполяцией соседних, иначе
        округление лага даёт фазовую ошибку, зависящую от частоты тона.

        Знак корреляции чередования зависит от режима: набег фазы за лаг
        в 2 бита раскладывается между mark- и space-кусками по-разному, и
        в base выходит +0.5, в probe −0.5. Поэтому захват идёт по модулю:
        чередование даёт |corr| ≈ 0.5 в любом режиме, шум — около 0.1.
        """
        if len(self._history) < self._window:
            return 0.0
        data = list(self._history)  # ровно self._window отсчётов (maxlen)
        lag = self._lag
        lo = int(lag)
        frac = lag - lo
        acc = 0.0
        energy = 0.0
        for i in range(len(data) - lo - 1):
            v = data[i]
            w = data[i + lo] * (1.0 - frac) + data[i + lo + 1] * frac
            acc += v * w
            energy += v * v
        if energy <= 0.0:
            return 0.0
        return acc / energy


# --- Goertzel ------------------------------------------------------------------


class SlideGoertzel:
    """Скользящий Гёрцель за O(1) на отсчёт, окно сдвигается на 1.

    Классический Гёрцель считает энергию блока за O(W) в конце блока.
    Для побитового решения нужен сдвиг окна на каждый отсчёт, и O(W) на
    каждый сдвиг — это 16 кГц * W умножений на тон. Здесь энергия окна
    поддерживается инкрементально:

      E(t) = |sum_{i=t-W+1..t} x_i * e^{-j2pi k i / N}|^2

    поддерживается парой рекуррентных сумм с комплексным поворотом:
    добавляем новый отсчёт с фазой текущего индекса, вычитаем покинувший
    окно с его фазой. Точность не деградирует: вычитаются ровно те же
    числа, что были добавлены, копейки округления не накапливаются.

    Очередь окна — deque: pop(0) на списке сдвигал бы все оставшиеся
    отсчёты на каждый новый отсчёт (O(W) вместо O(1)), а этот детектор
    вызывается дважды на каждый отсчёт (mark и space).
    """

    __slots__ = ("_coeff", "_window", "_index", "_s_re", "_s_im", "_queue")

    def __init__(self, frequency_hz: float, window: int) -> None:
        self._coeff = 2.0 * math.pi * frequency_hz / config.SAMPLE_RATE
        self._window = window
        self._index = 0
        self._s_re = 0.0
        self._s_im = 0.0
        self._queue: collections.deque[tuple[float, float]] = collections.deque()

    def reset(self) -> None:
        self._index = 0
        self._s_re = 0.0
        self._s_im = 0.0
        self._queue.clear()

    def push(self, sample: int) -> float:
        """Добавить отсчёт, вернуть энергию окна, если оно полное."""
        angle = self._coeff * self._index
        self._index += 1
        c = math.cos(angle)
        s = math.sin(angle)
        v = float(sample)
        self._s_re += v * c
        self._s_im += v * s
        self._queue.append((v * c, v * s))
        if len(self._queue) > self._window:
            old_re, old_im = self._queue.popleft()
            self._s_re -= old_re
            self._s_im -= old_im
        if len(self._queue) < self._window:
            return 0.0
        return self._s_re * self._s_re + self._s_im * self._s_im


# --- Приёмник ------------------------------------------------------------------


class Demodulator:
    """Демодулятор одного режима: отсчёты -> биты -> кадры.

    Пайплайн: energy gate -> захват преамбулы -> скользящий Гёрцель ->
    решение с гистерезисом -> тактовый PLL -> BitCollector.

    Тактирование: идеальный момент решения — центр бита. PLL держит фазу
    тактов в отсчётах, на каждом идеальном моменте выдаёт бит по решению
    детектора в этой точке и подстраивает фазу по измеренному моменту
    перехода. Подстройка ограничена: единичный выброс шума не должен
    сдвигать такты на бит.

    Возврат feed() — то же соглашение, что у BitCollector.add_bit: None —
    ничего, Frame — кадр, FrameError — кадр испорчен. Исключения наружу
    не летят: шум в кабеле — штатная ситуация, не сбой.
    """

    def __init__(
        self,
        mode: str = config.DEFAULT_MODE,
        gate_level: float = 512.0,
    ) -> None:
        config.check_mode(mode)
        self._mode = mode
        self._spb = config.SAMPLE_RATE / config.baud_of(mode)
        # Окно Гёрцеля — один бит, сэмпл — на границе бит: окно последних
        # W отсчётов тогда накрывает предыдущий бит целиком, и решение на
        # границе относится к закончившемуся биту. Сэмпл в центре бита
        # давал бы окно из половин двух бит при W в бит или окно в полбита
        # с плохой дискриминацией тонов (1200 Гц за полбита — полпериода).
        self._window = max(2, round(self._spb))
        self._gate = EnergyGate(gate_level)
        self._preamble = PreambleTracker(mode)
        mark_hz, space_hz = config.tones_of(mode)
        self._mark = SlideGoertzel(mark_hz, self._window)
        self._space = SlideGoertzel(space_hz, self._window)
        self._collector = BitCollector()
        self._decision: int | None = None
        self._prev_decision: int | None = None
        self._clock = 0.0  # позиция следующего идеального момента, в отсчётах
        self._clock_active = False
        self._sample_index = 0

    @property
    def mode(self) -> str:
        return self._mode

    def reset(self) -> None:
        """Полный сброс: тишина обнаружена, тракт свободен."""
        self._preamble = PreambleTracker(self._mode)
        self._mark.reset()
        self._space.reset()
        self._collector.reset()
        self._decision = None
        self._prev_decision = None
        self._clock = 0.0
        self._clock_active = False

    def feed(self, samples) -> list[Frame | FrameError]:
        """Пропустить фрагмент отсчётов, вернуть собранные события."""
        results: list[Frame | FrameError] = []
        for sample in samples:
            self._process_sample(sample)
            result = self._tick(sample)
            if result is not None:
                results.append(result)
        return results

    def _process_sample(self, sample: int) -> None:
        """Gate и захват преамбулы."""
        self._gate.feed([sample])
        if not self._clock_active and self._gate.active:
            self._preamble.feed([sample])
            if self._preamble.acquired:
                # Такты стартуют с задержкой в бит от точки захвата: окно
                # Гёрцеля в один бит отдаёт первое чистое решение через бит
                # после того, как сигнал целиком вошёл в окно. Фазу доуточнит
                # _lock_phase по первому же переходу детектора.
                self._clock = self._sample_index + self._spb
                self._clock_active = True
        self._sample_index += 1

    def _lock_phase(self, transition: int) -> None:
        """Выравнивание тактов по переходу: пока sync не найден, фаза
        произвольна и обязана быть принудительно посажена на переход.

        Пик autocorrelation кратен двум битам, и в чередовании 0101 фаза,
        сдвинутая на два бита, неотличима от верной — преамбула совпадает,
        sync ломается. До sync переходы преамбулы — единственный опорный
        сигнал. Граница бита, закончившегося на переходе, — переход минус
        задержка детектора _detector_lag(). Такты держат границу последнего
        бита в (clock - spb), туда и сажем. Поправка не ограничена: это
        захват фазы, не слежение, и ошибка здесь — целые биты.
        """
        boundary = transition - self._detector_lag()
        error = boundary - (self._clock - self._spb)
        error -= self._spb * round(error / self._spb)
        self._clock += error

    def _detector_lag(self) -> float:
        """Задержка флипа детектора от фактической границы бита.

        Энергия Гёрцеля растёт как квадрат числа отсчётов тона в окне,
        поэтому решение переключается при доле окна √K/(1+√K), а не при
        половине: при K = 1.3 это 0.52 бита от границы.
        """
        k = math.sqrt(config.DETECT_K)
        return self._spb * k / (1.0 + k)

    def _tick(self, sample: int) -> Frame | FrameError | None:
        """Детектор, гистерезис, PLL и выборка бита на идеальном моменте."""
        if not self._clock_active:
            return None
        e_mark, e_space = self._mark.push(sample), self._space.push(sample)
        if e_mark <= 0.0 and e_space <= 0.0:
            return None

        decision = self._decide(e_mark, e_space)
        transition = self._note_transition(decision)
        if transition is not None and not self._collector.sync_seen:
            self._lock_phase(transition)
        else:
            self._track_clock(transition)

        if self._sample_index >= self._clock:
            bit = self._decision if self._decision is not None else decision
            self._clock += self._spb
            result = self._collector.add_bit(bit)
            if result is not None:
                # Кадр собран или сломан: такты и детекторы к следующему кадру
                # перезапускаются захватом преамбулы заново.
                self._clock_active = False
                self._preamble = PreambleTracker(self._mode)
                self._mark.reset()
                self._space.reset()
                self._decision = None
                self._prev_decision = None
            return result
        return None

    def _decide(self, e_mark: float, e_space: float) -> int:
        """Гистерезисное решение: mark/space/удержание (docs/protocol.md 11.5)."""
        k = config.DETECT_K
        if e_mark > k * e_space:
            decision = 1
        elif e_space > k * e_mark:
            decision = 0
        else:
            decision = self._decision if self._decision is not None else 1
        self._decision = decision
        return decision

    def _note_transition(self, decision: int) -> int | None:
        """Индекс отсчёта перехода mark<->space, если он только что был."""
        previous = self._prev_decision
        self._prev_decision = decision
        if previous is not None and previous != decision:
            self._last_transition = self._sample_index
            return self._sample_index
        return None

    def _track_clock(self, transition: int | None) -> None:
        """PLL: подтянуть фазу тактов к измеренному моменту перехода.

        Граница бита, закончившегося на переходе, — переход минус
        _detector_lag(). Ошибка — расстояние от неё до границы тактов
        (clock - spb), поправка ограничена четвертью бита: одиночный
        шумовой выброс не должен сдвигать такты на целый бит и рвать кадр.
        """
        if transition is None:
            return
        boundary = transition - self._detector_lag()
        error = boundary - (self._clock - self._spb)
        error -= self._spb * round(error / self._spb)
        error = max(-self._spb / 4.0, min(self._spb / 4.0, error))
        self._clock += config.PLL_GAIN * error


def demodulate_frame(
    samples,
    mode: str = config.DEFAULT_MODE,
    gate_level: float = 512.0,
) -> list[Frame | FrameError]:
    """Разово прогнать фрагмент через Demodulator. Удобно для тестов и probe."""
    return Demodulator(mode, gate_level).feed(samples)
