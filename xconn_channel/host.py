"""Хост агента: HELO, смена режима, PTY, цикл сессии (docs/protocol.md 8.5, 9).

Склеивает то, что ядро намеренно не знает: транспорт, оболочку и факт, что
рукопожатие идёт в probe, а работа — в согласованном режиме. Ядро остаётся
кросс-платформенным; хост — точка входа серверной стороны.

Порядок после HELO: сначала уходит ответ в probe, потом set_mode. Иначе
ответ HELO уедет тонами base, а клиент ещё слушает probe.
"""

from __future__ import annotations

import time

from . import config, handshake
from .agent import AgentCore
from .framing import Frame
from .session import AgentSession


class AgentHost:
    """Рабочий цикл агента над send/receive и оболочкой."""

    def __init__(
        self,
        send,
        receive,
        pty,
        transport=None,
        rows: int = config.DEFAULT_ROWS,
        cols: int = config.DEFAULT_COLS,
        supported=None,
        pump_wait_ms: float = 250,
        pump_idle_ms: float = 80,
    ) -> None:
        self.pty = pty
        self.transport = transport
        self.core = AgentCore(
            pty.write,
            pty.read,
            rows,
            cols,
            pump_wait_ms=pump_wait_ms,
            pump_idle_ms=pump_idle_ms,
        )
        if supported is None:
            supported = (config.PROBE, config.BASE)
        self._helo = handshake.agent_helo_handler(rows, cols, supported)
        self._pending_mode: str | None = None
        self.connected = False
        self.session = AgentSession(send, receive, self.handle)
        self.session.set_replay_response(self.core.replay)

    def handle(self, frame: Frame) -> tuple[int, bytes]:
        """Handler сессии: HELO в любой момент, иначе ядро."""
        if frame.type == config.HELO:
            result = self._helo(frame)
            if result[0] == config.HELO:
                agent = handshake.decode_helo(result[1])
                self._pending_mode = agent.mode
            return result
        if not self.connected:
            return config.NAK, bytes((frame.seq, config.NAK_STATE))
        if frame.type == config.RESIZE and len(frame.payload) == 2:
            rows, cols = frame.payload
            if hasattr(self.pty, "resize"):
                self.pty.resize(rows, cols)
        return self.core.handle(frame)

    def poll(self, timeout_ms: float = 0) -> bool:
        """Одна итерация: принять кадр, ответить, сменить режим, слить PTY."""
        had = self.session.poll(timeout_ms)
        if self._pending_mode is not None:
            if self.transport is not None:
                self.transport.set_mode(self._pending_mode)
            self._pending_mode = None
            self.connected = True
        self.core.pump(wait_ms=0, idle_ms=0)
        if hasattr(self.pty, "child_exited") and self.pty.child_exited():
            if hasattr(self.pty, "restart"):
                self.pty.restart()
        return had

    def serve(self, stop=None) -> None:
        """Крутить poll, пока stop() не станет истинным. stop=None — вечно."""
        while stop is None or not stop():
            if not self.poll(0):
                time.sleep(0.001)
