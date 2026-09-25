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

from . import (
    agent,
    config,
    crc,
    demodulator,
    framing,
    handshake,
    modulator,
    screen,
    session,
    transport,
    vt100,
)
from .agent import AgentCore
from .client import Client
from .host import AgentHost
from .transport import AudioTransport, SampleLink
from .demodulator import Demodulator, decimate
from .framing import (
    BitCollector,
    Frame,
    FrameError,
    build_frame,
    parse_frame,
    to_bits,
)
from .handshake import HandshakeError, Helo, client_handshake, negotiate
from .modulator import Modulator
from .screen import Screen, ScreenError
from .session import AgentSession, MasterSession, SessionError

__all__ = [
    "AgentCore",
    "AgentHost",
    "AgentSession",
    "Client",
    "AudioTransport",
    "BitCollector",
    "Demodulator",
    "Frame",
    "FrameError",
    "HandshakeError",
    "Helo",
    "MasterSession",
    "Modulator",
    "SampleLink",
    "Screen",
    "ScreenError",
    "SessionError",
    "build_frame",
    "client_handshake",
    "config",
    "crc",
    "decimate",
    "demodulator",
    "framing",
    "handshake",
    "modulator",
    "negotiate",
    "parse_frame",
    "screen",
    "session",
    "to_bits",
    "transport",
    "vt100",
]

__version__ = "1.0.0"
