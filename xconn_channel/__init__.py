"""Общий пакет звукового канала x_connector.

Копируется на обе стороны канала целиком и без изменений: клиент на
ноутбуке и агент на сервере импортируют один и тот же код. Это требование,
а не удобства сборки — модулятор, демодулятор и кадрирование обязаны
совпадать байт в байт, иначе рассинхрон сторон отлаживается по логам
поведения, которое нигде не описано.

Внешних зависимостей нет: агент ставится автономно с флешки и работает при
выключенной сети (AGENTS.md 3.2, 3.3). Только стандартная библиотека.

Спецификация — docs/protocol.md, константы — config.py.
"""

from . import config, crc, demodulator, framing, modulator
from .demodulator import Demodulator, decimate
from .framing import (
    BitCollector,
    Frame,
    FrameError,
    build_frame,
    parse_frame,
    to_bits,
)
from .modulator import Modulator

__all__ = [
    "BitCollector",
    "Demodulator",
    "Frame",
    "FrameError",
    "Modulator",
    "build_frame",
    "config",
    "crc",
    "decimate",
    "demodulator",
    "framing",
    "modulator",
    "parse_frame",
    "to_bits",
]

__version__ = "0.1.0"
