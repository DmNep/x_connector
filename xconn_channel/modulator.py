"""CPFSK-модулятор: последовательность бит в отсчёты рабочей частоты.

docs/protocol.md 3: FSK с непрерывной фазой, семейство тонов Bell 202,
mark = 1. Непрерывность фазы обязательна: разрыв фазы — широкополосный
всплеск, интерферирующий с обоими тонами. Поэтому частота задаётся только
скоростью накопления фазы, а фаза не сбрасывается ни на границе бит, ни
между кадрами.

Границы бит дробные (13.33 отсчёта на бит при base), поэтому бит
принадлежит отсчёту по точной временной сетке, а не по округлённой длине:
округление длины бита дало бы джиттер границ, который PLL приёмника
вынужден потом вылизывать на каждом кадре.

Уровень — PEAK_AMPLITUDE из config (-12 dBFS): запас по пику защищает от
клиппинга на переходах, а клиппинг порождает гармоники в полосу чужого
тона (docs/protocol.md 2.1).
"""

from __future__ import annotations

import array
import math

from . import config

_TWO_PI = 2.0 * math.pi


class Modulator:
    """Модулятор одного режима. Фаза держится между вызовами modulate()."""

    def __init__(self, mode: str = config.DEFAULT_MODE) -> None:
        config.check_mode(mode)
        self._mode = mode
        self._baud = config.baud_of(mode)
        mark_hz, space_hz = config.tones_of(mode)
        self._step_mark = _TWO_PI * mark_hz / config.SAMPLE_RATE
        self._step_space = _TWO_PI * space_hz / config.SAMPLE_RATE
        self._phase = 0.0

    @property
    def mode(self) -> str:
        return self._mode

    def modulate(self, bits) -> array.array:
        """Биты (mark=1) в отсчёты int16 на SAMPLE_RATE."""
        if not bits:
            return array.array("h")
        total = round(len(bits) * config.SAMPLE_RATE / self._baud)
        out = array.array("h", bytes(2 * total))
        phase = self._phase
        amplitude = config.PEAK_AMPLITUDE
        last = len(bits) - 1
        for i in range(total):
            # Принадлежность отсчёта биту — floor(i / samples_per_bit)
            # без промежуточного округления длины бита.
            bit = bits[min(i * self._baud // config.SAMPLE_RATE, last)]
            phase += self._step_mark if bit else self._step_space
            # Приращение фазы всегда меньше 2*pi, одного вычитания достаточно.
            if phase >= _TWO_PI:
                phase -= _TWO_PI
            out[i] = round(amplitude * math.sin(phase))
        self._phase = phase
        return out

    def silence(self, milliseconds: float) -> array.array:
        """Тишина для GAP и T_LEAD (docs/protocol.md 8.3).

        Фаза не трогается: тишина не входит в CPFSK-поток, и следующий
        кадр продолжает накопление фазы с того же значения.
        """
        total = round(milliseconds * config.SAMPLE_RATE / 1000.0)
        return array.array("h", bytes(2 * total))


def upsample(samples: array.array, factor: int = config.DECIMATION) -> array.array:
    """16 кГц -> 48 кГц линейной интерполяцией для устройства (docs/protocol.md 2).

    Симметрично decimate() приёмника: DSP работает на 16 кГц, звуковая
    карта обязана быть на стандартных 48 кГц, иначе драйвер включит свой
    ресемплинг с непредсказуемой фазой.

    Интерполяция, а не повторение отсчётов: ступенька zero-order hold
    даёт образы спектра вокруг 16 кГц полной амплитудой, линейная
    интерполяция их ослабляет. Полоса полезного сигнала до 3400 Гц,
    образы выше 12 кГц приёмник всё равно режет прореживанием, но
    вносить их в тракт с запасом не стоит.
    """
    if factor <= 0:
        raise ValueError(f"фактор подъёма {factor} обязан быть положительным")
    total = len(samples)
    out = array.array("h", bytes(2 * total * factor))
    for i, value in enumerate(samples):
        nxt = samples[i + 1] if i + 1 < total else value
        base = value * factor
        step = nxt - value
        for k in range(factor):
            out[i * factor + k] = (base + step * k) // factor
    return out
