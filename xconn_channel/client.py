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
from .transfer import (
    crc32,
    decode_data,
    decode_offer,
    encode_close,
    encode_data,
    encode_get,
    encode_open,
    encode_pull,
    iter_chunks,
    sanitize_name,
)


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
        self._part_buf: bytearray | None = None
        self._part_kind = 0

    def connect(self, desired_mode: str = config.DEFAULT_MODE):
        """HELO (probe, затем base), полный снимок. Старый агент — SAFE resize."""
        self.helo = handshake.client_handshake(self.master, desired_mode, self.transport)
        try:
            self.refresh()
        except SessionError:
            # NAK_LENGTH — старый агент без нарезки. CRC/таймаут —
            # 24×80 после motd тоже не проходит живой канал.
            self.resize(config.SAFE_ROWS, config.SAFE_COLS)
        return self.helo

    def _nak_error(self, reply: Frame) -> SessionError:
        code = reply.payload[1] if len(reply.payload) >= 2 else -1
        return SessionError(
            f"агент отверг кадр seq={reply.seq} nak={code:#04x}",
            1,
            reply.seq,
            nak=code,
        )

    def _exchange_screen(self, frame_type: int, payload: bytes = b"") -> Screen:
        timeout = None
        if frame_type in (config.CMD, config.KEY, config.SCREEN_MORE):
            timeout = config.T_CMD_CARRIER_MS
        reply = self.master.exchange(
            frame_type, payload, accept=self._apply, timeout_ms=timeout
        )
        if reply.type == config.NAK:
            raise self._nak_error(reply)
        while (
            reply.type == config.SCREEN_PART
            and self._part_buf is not None
        ):
            reply = self.master.exchange(
                config.SCREEN_MORE, b"", accept=self._apply
            )
            if reply.type == config.NAK:
                raise self._nak_error(reply)
        assert self.screen is not None
        return self.screen

    def cmd(self, text: str) -> Screen:
        """Строка ввода. Добавляет \\n, если его нет: удобство CLI и ИИ.

        Кадр не длиннее MAX_PAYLOAD: длинную строку режем, перевод строки
        только в последнем куске — bash не исполняет набор по частям.
        """
        data = text.encode("utf-8")
        if not data.endswith((b"\n", b"\r")):
            data += b"\n"
        limit = config.MAX_PAYLOAD
        screen = None
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + limit]
            offset += len(chunk)
            screen = self._exchange_screen(config.CMD, chunk)
        assert screen is not None
        return screen

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
                raise self._nak_error(reply)
        assert self.screen is not None
        return self.screen

    def get(self, remote_name: str, local_path: str) -> None:
        """Файл с агента: GET / OFFER / PULL (docs/protocol.md 7)."""
        from pathlib import Path

        reply = self.master.exchange(config.FILE_GET, encode_get(remote_name))
        if reply.type == config.NAK:
            raise self._nak_error(reply)
        if reply.type != config.FILE_OFFER:
            raise SessionError(
                f"ожидался FILE_OFFER, агент ответил {reply.type_name}",
                1,
                reply.seq,
            )
        size, digest = decode_offer(reply.payload)
        buf = bytearray(size)
        offset = 0
        while offset < size:
            reply = self.master.exchange(config.FILE_PULL, encode_pull(offset))
            if reply.type == config.NAK:
                raise self._nak_error(reply)
            if reply.type != config.FILE_DATA:
                raise SessionError(
                    f"ожидался FILE_DATA, агент ответил {reply.type_name}",
                    1,
                    reply.seq,
                )
            got_off, chunk = decode_data(reply.payload)
            end = got_off + len(chunk)
            if end > size:
                raise SessionError("кусок вышел за размер файла", 1, reply.seq)
            buf[got_off:end] = chunk
            offset = end
        if crc32(bytes(buf)) != digest:
            raise SessionError("crc32 выгрузки не совпал", 1, 0)
        Path(local_path).write_bytes(buf)

    def render(self) -> str:
        """Сетка как текст для печати. Пустой экран — пустая строка."""
        if self.screen is None:
            return ""
        s = self.screen
        header = f"{s.rows}x{s.cols} cursor={s.cur_row},{s.cur_col}"
        return header + "\n" + s.text()

    def _take_part(self, reply: Frame) -> None:
        kind, index, total, data = screen.decode_part(reply.payload)
        if index == 0:
            self._part_buf = bytearray()
            self._part_kind = kind
        if self._part_buf is None:
            raise ScreenError("SCREEN_PART без начала", config.NAK_STATE)
        self._part_buf += data
        if index + 1 < total:
            return
        blob = bytes(self._part_buf)
        self._part_buf = None
        if kind == config.SCREEN_PART_FULL:
            self.screen = screen.parse_full(blob)
            self.base_seq = reply.seq
            return
        if self.screen is None or self.base_seq is None:
            raise ScreenError("нет базы для дельты", config.NAK_STATE)
        self.screen = screen.parse_delta(blob, self.base_seq, self.screen)
        self.base_seq = reply.seq

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
        if reply.type == config.SCREEN_PART:
            self._take_part(reply)
            return self.screen
        if reply.type == config.NAK:
            return self.screen
        raise SessionError(
            f"неожиданный ответ {reply.type_name} seq={reply.seq}",
            1,
            reply.seq,
        )
