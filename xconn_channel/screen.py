"""Формат экрана: сетка символов, снимки и дельты (docs/protocol.md 6).

Экран — прямоугольная сетка rows × cols, символ — один байт cp437: она
даёт box drawing консольных программ (dialog, установщики) одним байтом
на символ. Агент держит сетку символов, а не поток stdout (AGENTS.md 2.7),
и транслирует её клиенту снимками.

Сериализация SCREEN_FULL (6.1):

    rows:1  cols:1  cur_row:1  cur_col:1  flags:1  data:...

    data — rows строк по cols байт, без разделителей; поле от rows до
    конца data сжимается zlib (level 6).

Сериализация SCREEN_DELTA (6.2):

    base_seq:1  count:1  cur_row:1  cur_col:1  flags:1
    ( row_index:1  zlib(row_bytes [+ inverse_row]) )...

    base_seq — номер кадра SCREEN_FULL, от которого считается дельта.
    Курсор и флаги едут в каждом кадре дельты: иначе клиент видит сетку
    после команды, а курсор — с прошлого FULL.
    Если клиент не может применить дельту (нет базового снимка, base_seq
    не совпал), он отвечает NAK с причиной NAK_STATE, и агент присылает
    SCREEN_FULL.

Дельта обязательна: полный снимок при base идёт ~1.8 с, изменённая
строка — ~0.36 с, без дельты целевая задержка обмена (8.4) недостижима.

Сжатие zlib безопасно, потому что payload целиком защищён CRC кадра и
ретранслируется при ошибке: повреждённый поток zlib до разборщика не
доходит. Собственный RLE не нужен — zlib делает то же сильнее.
"""

from __future__ import annotations

import zlib

from . import config


class ScreenError(Exception):
    """Снимок или дельта не применяются. Причина — в code для NAK."""

    def __init__(self, message: str, code: int = config.NAK_STATE) -> None:
        super().__init__(message)
        self.code = code


# flags (docs/protocol.md 6.1): бит 0 — курсор видим, бит 1 — режим
# вставки, бит 2 — приложение активно, бит 3 — BEL с прошлого снимка.
FLAG_CURSOR_VISIBLE = 0x01
FLAG_INSERT_MODE = 0x02
FLAG_APP_ACTIVE = 0x04
FLAG_BEL = 0x08

_ZLIB_LEVEL = 6


class Screen:
    """Сетка символов терминала с курсором и флагами.

    Сетка — один bytearray без списков строк: экран 24×80 это 1920 байт,
    и копирование/сравнение строк работает срезами, без поэлементных
    циклов на каждый кадр.
    """

    def __init__(
        self,
        rows: int = config.DEFAULT_ROWS,
        cols: int = config.DEFAULT_COLS,
    ) -> None:
        if not 1 <= rows <= 255:
            raise ValueError(f"rows {rows} вне байта")
        if not 1 <= cols <= 255:
            raise ValueError(f"cols {cols} вне байта")
        self.rows = rows
        self.cols = cols
        self.cells = bytearray(b" " * (rows * cols))
        self.inverse = bytearray(rows * cols)  # 0/1, SGR reverse
        self.cur_row = 0
        self.cur_col = 0
        self.flags = 0

    # --- доступ к сетке ---------------------------------------------------

    def row_bytes(self, row: int) -> bytes:
        self._check_row(row)
        start = row * self.cols
        return bytes(self.cells[start : start + self.cols])

    def set_row(self, row: int, data: bytes, inverse: bytes | None = None) -> None:
        self._check_row(row)
        if len(data) != self.cols:
            raise ValueError(f"строка {len(data)} байт, ожидалось {self.cols}")
        start = row * self.cols
        self.cells[start : start + self.cols] = data
        if inverse is None:
            self.inverse[start : start + self.cols] = b"\x00" * self.cols
        else:
            if len(inverse) != self.cols:
                raise ValueError(f"inverse {len(inverse)} байт, ожидалось {self.cols}")
            self.inverse[start : start + self.cols] = inverse

    def row_inverse(self, row: int) -> bytes:
        self._check_row(row)
        start = row * self.cols
        return bytes(self.inverse[start : start + self.cols])

    def put(self, row: int, col: int, ch: int, inverse: int = 0) -> None:
        self._check_row(row)
        if not 0 <= col < self.cols:
            raise ValueError(f"col {col} вне 0..{self.cols - 1}")
        idx = row * self.cols + col
        self.cells[idx] = ch
        self.inverse[idx] = 1 if inverse else 0

    def copy_look(self, other: "Screen") -> None:
        """Курсор, флаги и инверсия. Ячейки — отдельно, через cells[:]."""
        self.cur_row = other.cur_row
        self.cur_col = other.cur_col
        self.flags = other.flags
        if (self.rows, self.cols) == (other.rows, other.cols):
            self.inverse[:] = other.inverse

    def get(self, row: int, col: int) -> int:
        self._check_row(row)
        if not 0 <= col < self.cols:
            raise ValueError(f"col {col} вне 0..{self.cols - 1}")
        return self.cells[row * self.cols + col]

    def text(self) -> str:
        """Сетка как текст — для отладки и тестов, cp437."""
        return "\n".join(
            self.row_bytes(r).decode(config.SCREEN_CODEPAGE) for r in range(self.rows)
        )

    def blank(self) -> None:
        n = self.rows * self.cols
        self.cells[:] = b" " * n
        self.inverse[:] = b"\x00" * n

    def write_span(self, start: int, data: bytes, inverse: bytes | None = None) -> None:
        end = start + len(data)
        self.cells[start:end] = data
        if inverse is None:
            self.inverse[start:end] = b"\x00" * len(data)
        else:
            self.inverse[start:end] = inverse

    def _check_row(self, row: int) -> None:
        if not 0 <= row < self.rows:
            raise ValueError(f"row {row} вне 0..{self.rows - 1}")

    # --- сравнение ----------------------------------------------------------

    def changed_rows(self, other: "Screen") -> list[int]:
        """Индексы строк, отличающихся от other. Форма сетки обязана совпадать."""
        if (self.rows, self.cols) != (other.rows, other.cols):
            raise ValueError(
                f"форма сетки {self.rows}x{self.cols} против {other.rows}x{other.cols}"
            )
        changed = []
        for row in range(self.rows):
            start = row * self.cols
            if (
                self.cells[start : start + self.cols]
                != other.cells[start : start + self.cols]
                or self.inverse[start : start + self.cols]
                != other.inverse[start : start + self.cols]
            ):
                changed.append(row)
        return changed


# --- SCREEN_FULL ---------------------------------------------------------------


def serialize_full(screen: Screen) -> bytes:
    """Снимок целиком: заголовок + сетка, поле сжато zlib (docs/protocol.md 6.1).

    Бросает ValueError, если после сжатия не влезает в MAX_PAYLOAD:
    типичный экран сжимается в 5-10 раз (1920 -> 150-400 байт), но экран
    из несжимаемого мусора режется на SCREEN_PART (docs/protocol.md 6.4).
    """
    packed = pack_full(screen)
    if len(packed) > config.MAX_PAYLOAD:
        raise ValueError(
            f"снимок после zlib {len(packed)} байт превышает MAX_PAYLOAD="
            f"{config.MAX_PAYLOAD}: экран несжимаем, нужна сегментация"
        )
    return packed


def pack_full(screen: Screen) -> bytes:
    """zlib снимка без проверки MAX_PAYLOAD — для нарезки на SCREEN_PART."""
    header = bytes(
        (screen.rows, screen.cols, screen.cur_row, screen.cur_col, screen.flags)
    )
    body = bytes(screen.cells)
    if any(screen.inverse):
        body += bytes(screen.inverse)
    return zlib.compress(header + body, _ZLIB_LEVEL)


def split_packed(blob: bytes, size: int | None = None) -> list[bytes]:
    """Нарезать zlib-снимок на куски SCREEN_CHUNK."""
    chunk = config.SCREEN_CHUNK if size is None else size
    if chunk < 1:
        raise ValueError(f"кусок {chunk} байт")
    if not blob:
        return [b""]
    return [blob[i : i + chunk] for i in range(0, len(blob), chunk)]


def encode_part(kind: int, index: int, total: int, data: bytes) -> bytes:
    """SCREEN_PART: kind, index, total, кусок."""
    if not 0 <= kind <= 1:
        raise ValueError(f"kind {kind}")
    if not 0 <= index < total <= 255:
        raise ValueError(f"часть {index}/{total}")
    if len(data) > config.SCREEN_CHUNK:
        raise ValueError(
            f"кусок {len(data)} байт, лимит {config.SCREEN_CHUNK}"
        )
    return bytes((kind, index, total)) + data


def decode_part(payload: bytes) -> tuple[int, int, int, bytes]:
    """Разбор SCREEN_PART. ScreenError, если заголовок короче трёх байт."""
    if len(payload) < config.SCREEN_PART_HDR:
        raise ScreenError(
            f"SCREEN_PART усечён: {len(payload)} байт", config.NAK_LENGTH
        )
    kind, index, total = payload[:3]
    if total < 1 or index >= total:
        raise ScreenError(
            f"SCREEN_PART {index}/{total}", config.NAK_LENGTH
        )
    return kind, index, total, payload[3:]


def parse_full(payload: bytes) -> Screen:
    """Разбор SCREEN_FULL. Бросает ScreenError, если payload не распаковывается."""
    try:
        raw = zlib.decompress(payload)
    except zlib.error as error:
        raise ScreenError(f"zlib: {error}", config.NAK_CRC) from None

    if len(raw) < 5:
        raise ScreenError(f"снимок усечён: {len(raw)} байт", config.NAK_LENGTH)

    rows, cols, cur_row, cur_col, flags = raw[:5]
    data = raw[5:]
    try:
        parsed = Screen(rows, cols)
    except ValueError as err:
        raise ScreenError(str(err), config.NAK_LENGTH) from None
    need = rows * cols
    if len(data) == need:
        parsed.cells[:] = data
    elif len(data) == need * 2:
        parsed.cells[:] = data[:need]
        parsed.inverse[:] = data[need:]
    else:
        raise ScreenError(
            f"сетка {len(data)} байт, ожидалось {need} или {need * 2}",
            config.NAK_LENGTH,
        )
    parsed.flags = flags
    if cur_row >= rows or cur_col >= cols:
        raise ScreenError(
            f"курсор ({cur_row},{cur_col}) вне сетки {rows}x{cols}", config.NAK_LENGTH
        )
    parsed.cur_row = cur_row
    parsed.cur_col = cur_col
    return parsed


# --- SCREEN_DELTA ---------------------------------------------------------------


def serialize_delta(base_seq: int, screen: Screen, changed: list[int]) -> bytes:
    """Дельта изменённых строк от базового снимка (docs/protocol.md 6.2).

    base_seq — seq кадра SCREEN_FULL, от которого считается дельта.
    changed — индексы изменённых строк (Screen.changed_rows).

    Бросает ValueError при выходе за MAX_PAYLOAD: дельта с большим числом
    строк не влезает в один кадр, уровень выше режет её на несколько
    кадров или посылает полный снимок. Проверка — по фактическому размеру
    после сборки, а не по верхней оценке: zlib на типичной строке жмёт
    в 2-3 раза, и консервативная оценка резала бы рабочие дельты.
    """
    if not 0 <= base_seq <= 0xFF:
        raise ValueError(f"base_seq {base_seq} вне байта")
    if not 0 <= len(changed) <= 0xFF:
        raise ValueError(f"число строк {len(changed)} вне байта")

    parts = [bytes((base_seq, len(changed), screen.cur_row, screen.cur_col, screen.flags))]
    for row in changed:
        row_payload = screen.row_bytes(row)
        inv = screen.row_inverse(row)
        if any(inv):
            row_payload += inv
        packed = zlib.compress(row_payload, _ZLIB_LEVEL)
        parts.append(bytes((row,)) + packed)
    payload = b"".join(parts)
    if len(payload) > config.MAX_PAYLOAD:
        raise ValueError(
            f"дельта из {len(changed)} строк — {len(payload)} байт, превышает "
            f"MAX_PAYLOAD={config.MAX_PAYLOAD}: режьте на кадры или шлите SCREEN_FULL"
        )
    return payload


def parse_delta(payload: bytes, base_seq: int, screen: Screen) -> Screen:
    """Применить SCREEN_DELTA к screen. Возвращает новый снимок.

    Проверка базы — прежде разбора: если base_seq кадра не совпал с
    базой клиента, дельту применять нельзя, и клиент отвечает NAK с
    NAK_STATE, агент присылает SCREEN_FULL (docs/protocol.md 6.2).
    """
    if len(payload) < 5:
        raise ScreenError(f"дельта усечена: {len(payload)} байт", config.NAK_LENGTH)
    if payload[0] != base_seq:
        raise ScreenError(
            f"base_seq {payload[0]} не совпал с базой {base_seq}", config.NAK_STATE
        )

    updated = Screen(screen.rows, screen.cols)
    updated.cells[:] = screen.cells
    updated.inverse[:] = screen.inverse
    updated.cur_row = payload[2]
    updated.cur_col = payload[3]
    updated.flags = payload[4]
    if updated.cur_row >= updated.rows or updated.cur_col >= updated.cols:
        raise ScreenError(
            f"курсор ({updated.cur_row},{updated.cur_col}) вне сетки",
            config.NAK_LENGTH,
        )

    count = payload[1]
    offset = 5
    for _ in range(count):
        if offset + 1 > len(payload):
            raise ScreenError("дельта обрезана в списке строк", config.NAK_LENGTH)
        row = payload[offset]
        offset += 1
        if row >= updated.rows:
            raise ScreenError(
                f"строка {row} вне сетки {updated.rows}", config.NAK_LENGTH
            )
        # Строка сжата zlib: длина куска неизвестна из заголовка, но zlib
        # знает свой конец — decompressobj до eof.
        try:
            dec = zlib.decompressobj()
            row_bytes = dec.decompress(payload[offset:])
            if not dec.eof:
                raise ScreenError("строка дельты без конца zlib-потока", config.NAK_LENGTH)
        except zlib.error as error:
            raise ScreenError(f"zlib строки: {error}", config.NAK_CRC) from None
        if len(row_bytes) == updated.cols:
            updated.set_row(row, row_bytes)
        elif len(row_bytes) == updated.cols * 2:
            updated.set_row(
                row, row_bytes[: updated.cols], row_bytes[updated.cols :]
            )
        else:
            raise ScreenError(
                f"строка {len(row_bytes)} байт, ожидалось {updated.cols} "
                f"или {updated.cols * 2}",
                config.NAK_LENGTH,
            )
        offset = len(payload) - len(dec.unused_data)

    if offset != len(payload):
        raise ScreenError("лишние байты после дельты", config.NAK_LENGTH)
    return updated
