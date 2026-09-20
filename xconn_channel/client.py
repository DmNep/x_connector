"""Клиент канала: рукопожатие, команды, снимок экрана.

Управляемая программа для ИИ-агента на ноутбуке (AGENTS.md 3.7): отправить
команду, получить сетку терминала, нажать ключ, сменить размер окна.
Отрисовка — текст 24×80, не GUI.

Рукопожатие ведётся в probe, работа — в режиме из HELO агента
(docs/protocol.md 8.5). Смена тонов транспорта — после приёма HELO.
"""

from __future__ import annotations

from . import config, handshake, screen
from .framing import Frame
from .screen import Screen, ScreenError
from .session import MasterSession, SessionError


# Имена ключей для CLI и ИИ-агента. Значения — байты, как с клавиатуры VT100.
KEYS: dict[str, bytes] = {
    "ctrl-c": b"\x03",
    "ctrl-d": b"\x04",
    "ctrl-l": b"\x0c",
    "ctrl-z": b"\x1a",
    "tab": b"\t",
    "enter": b"\r",
    "esc": b"\x1b",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "backspace": b"\x7f",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
}


class Client:
    """Сторона мастера: connect / cmd / key / resize / ping."""

    def __init__(self, send, receive, transport=None, clock=None) -> None:
        self.transport = transport
        self.master = MasterSession(send, receive, mode=config.PROBE, clock=clock)
        self.screen: Screen | None = None
        self.base_seq: int | None = None
        self.helo = None

    def connect(self, desired_mode: str = config.DEFAULT_MODE):
        """HELO в probe, переход в согласованный режим, полный снимок."""
        self.helo = handshake.client_handshake(self.master, desired_mode)
        if self.transport is not None:
            self.transport.set_mode(self.helo.mode)
        self.master.mode = self.helo.mode
        self.refresh()
        return self.helo

    def cmd(self, text: str) -> Screen:
        """Строка ввода. Добавляет \\n, если его нет: удобство CLI и ИИ."""
        payload = text.encode("utf-8")
        if not payload.endswith((b"\n", b"\r")):
            payload += b"\n"
        reply = self.master.exchange(config.CMD, payload)
        return self._apply(reply)

    def type_bytes(self, data: bytes) -> Screen:
        """Сырые байты без добавления перевода строки."""
        reply = self.master.exchange(config.CMD, data)
        return self._apply(reply)

    def key(self, name: str) -> Screen:
        """Именованный ключ из KEYS либо сырая строка байт."""
        data = KEYS.get(name.lower())
        if data is None:
            data = name.encode("latin-1")
        reply = self.master.exchange(config.KEY, data)
        return self._apply(reply)

    def resize(self, rows: int, cols: int) -> Screen:
        reply = self.master.exchange(config.RESIZE, bytes((rows, cols)))
        return self._apply(reply)

    def ping(self) -> Frame:
        return self.master.exchange(config.PING)

    def refresh(self) -> Screen:
        """Запросить SCREEN_FULL (PING с флагом, docs/protocol.md 6.2)."""
        reply = self.master.exchange(config.PING, bytes((config.PING_FULL,)))
        return self._apply(reply)

    def render(self) -> str:
        """Сетка как текст для печати. Пустой экран — пустая строка."""
        if self.screen is None:
            return ""
        s = self.screen
        header = f"{s.rows}x{s.cols} cursor={s.cur_row},{s.cur_col}"
        return header + "\n" + s.text()

    def _apply(self, reply: Frame) -> Screen:
        if reply.type == config.SCREEN_FULL:
            self.screen = screen.parse_full(reply.payload)
            self.base_seq = reply.seq
            return self.screen
        if reply.type == config.SCREEN_DELTA:
            if self.screen is None or self.base_seq is None:
                return self.refresh()
            try:
                self.screen = screen.parse_delta(
                    reply.payload, self.base_seq, self.screen
                )
            except ScreenError:
                return self.refresh()
            self.base_seq = reply.seq
            return self.screen
        if reply.type == config.PONG:
            if self.screen is None:
                raise SessionError("PONG без экрана после connect", 1, reply.seq)
            return self.screen
        if reply.type == config.NAK:
            raise SessionError(
                f"агент отверг кадр seq={reply.seq}", 1, reply.seq
            )
        raise SessionError(
            f"неожиданный ответ {reply.type_name} seq={reply.seq}",
            1,
            reply.seq,
        )
