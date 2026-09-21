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
from .transfer import crc32, encode_close, encode_data, encode_open, iter_chunks, sanitize_name


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
        self.helo = handshake.client_handshake(self.master, desired_mode, self.transport)
        self.refresh()
        return self.helo

    def _exchange_screen(self, frame_type: int, payload: bytes = b"") -> Screen:
        reply = self.master.exchange(frame_type, payload, accept=self._apply)
        if reply.type == config.NAK:
            raise SessionError(
                f"агент отверг кадр seq={reply.seq}", 1, reply.seq
            )
        assert self.screen is not None
        return self.screen

    def cmd(self, text: str) -> Screen:
        """Строка ввода. Добавляет \\n, если его нет: удобство CLI и ИИ."""
        data = text.encode("utf-8")
        if not data.endswith((b"\n", b"\r")):
            data += b"\n"
        return self._exchange_screen(config.CMD, data)

    def type_bytes(self, data: bytes) -> Screen:
        """Сырые байты без добавления перевода строки."""
        return self._exchange_screen(config.CMD, data)

    def key(self, name: str) -> Screen:
        """Именованный ключ из KEYS либо сырая строка байт."""
        data = KEYS.get(name.lower())
        if data is None:
            data = name.encode("latin-1")
        return self._exchange_screen(config.KEY, data)

    def resize(self, rows: int, cols: int) -> Screen:
        return self._exchange_screen(config.RESIZE, bytes((rows, cols)))

    def ping(self) -> Frame:
        return self.master.exchange(config.PING)

    def refresh(self) -> Screen:
        """Запросить SCREEN_FULL (PING с флагом, docs/protocol.md 6.2)."""
        self._exchange_screen(config.PING, bytes((config.PING_FULL,)))
        assert self.screen is not None
        return self.screen

    def put(self, local_path: str, remote_name: str | None = None) -> Screen:
        """Файл на агент: OPEN / DATA / CLOSE (docs/protocol.md 7)."""
        from pathlib import Path

        path = Path(local_path)
        data = path.read_bytes()
        name = sanitize_name(remote_name or path.name)
        digest = crc32(data)
        for ftype, payload in (
            (config.FILE_OPEN, encode_open(name, len(data), digest)),
            *[(config.FILE_DATA, encode_data(offset, chunk)) for offset, chunk in iter_chunks(data)],
            (config.FILE_CLOSE, encode_close(digest)),
        ):
            reply = self.master.exchange(ftype, payload, accept=self._apply)
            if reply.type == config.NAK:
                raise SessionError(
                    f"агент отверг {ftype:#04x} seq={reply.seq}", 1, reply.seq
                )
        assert self.screen is not None
        return self.screen

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
                raise ScreenError("нет базы для дельты", config.NAK_STATE)
            self.screen = screen.parse_delta(
                reply.payload, self.base_seq, self.screen
            )
            self.base_seq = reply.seq
            return self.screen
        if reply.type == config.PONG:
            if self.screen is None:
                raise SessionError("PONG без экрана после connect", 1, reply.seq)
            return self.screen
        if reply.type == config.NOTE:
            if self.screen is None:
                raise SessionError("NOTE без экрана после connect", 1, reply.seq)
            if not reply.payload or reply.payload[0] != config.NOTE_OK:
                raise SessionError(
                    f"NOTE без NOTE_OK seq={reply.seq}", 1, reply.seq
                )
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
