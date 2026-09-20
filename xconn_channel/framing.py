"""Сборка и разбор кадров, побайтовое кадрирование 8N1, преамбула.

Структура кадра и смысл полей — docs/protocol.md 4. Здесь реализованы три
вещи, которые обязаны совпадать на обеих сторонах канала байт в байт:
раскладка полей, порядок бит в байте (младший первым) и форма преамбулы.

Модуляция сюда не заходит: модулятор получает последовательность бит из
to_bits(), демодулятор отдаёт её в from_bits(). Благодаря этому кадр можно
проверить целиком в памяти, без звука, что и делает selftest.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config
from .crc import crc16


class FrameError(Exception):
    """Кадр принят, но неверен. Причина — в code, она уходит в NAK.

    seq — номер отвергнутого кадра, если заголовок успел прочитаться.
    Нужен для NAK: подтверждать отвержение без seq бессмысленно, мастер
    не узнает, что ретранслировать. None — заголовок не читается, кадр
    не идентифицирован, отвечать нечем (docs/protocol.md 8.5: молча).

    raw — исходные байты, на которых разбор споткнулся, если к этому
    моменту заголовок уже прочитан. AudioTransport отдаёт их вызывающему
    как есть вместо самого исключения: сессия сама вызовет parse_frame()
    на тех же байтах и получит тот же FrameError с тем же seq — так
    испорченный кадр доходит до сессии, а не тонет в демодуляторе.
    """

    def __init__(
        self,
        message: str,
        code: int = config.NAK_CRC,
        seq: int | None = None,
        raw: bytes | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.seq = seq
        self.raw = raw


@dataclass(frozen=True)
class Frame:
    """Разобранный кадр. crc уже проверена, повторять не нужно."""

    type: int
    seq: int
    payload: bytes

    @property
    def type_name(self) -> str:
        return config.FRAME_TYPES.get(self.type, f"UNKNOWN({self.type:#04x})")

    def __str__(self) -> str:
        return (
            f"{self.type_name} seq={self.seq} len={len(self.payload)}"
            + (f" {self.payload!r}" if len(self.payload) <= 24 else "")
        )


# --- Логический кадр --------------------------------------------------------


def build_frame(frame_type: int, seq: int, payload: bytes = b"") -> bytes:
    """Собрать кадр без преамбулы: sync | type | seq | len | payload | crc."""
    if not 0 <= frame_type <= 0xFF:
        raise ValueError(f"тип кадра {frame_type} вне байта")
    if not 0 <= seq <= 0xFF:
        raise ValueError(f"seq {seq} вне байта")
    if len(payload) > config.MAX_PAYLOAD:
        raise ValueError(
            f"payload {len(payload)} байт превышает MAX_PAYLOAD={config.MAX_PAYLOAD}, "
            "нужна сегментация на уровне приложения (docs/protocol.md 7)"
        )

    length = len(payload)
    body = bytes(
        (
            config.SYNC_BYTE,
            frame_type,
            seq,
            (length >> 8) & 0xFF,
            length & 0xFF,
        )
    ) + payload
    crc = crc16(body)
    return body + bytes(((crc >> 8) & 0xFF, crc & 0xFF))


def parse_frame(data: bytes | bytearray) -> Frame:
    """Разобрать кадр, начинающийся с sync. Бросает FrameError с причиной для NAK.

    Проверка строгая по трём причинам. Неизвестный тип обязан давать NAK с
    NAK_TYPE, а не тихое игнорирование: иначе сторона с новой прошивкой
    молча перестанет отвечать, и это выглядит как обрыв кабеля. Длина
    обязана совпадать с фактическим размером буфера, иначе обрезанный на
    границе слота кадр пройдёт CRC и даст неверный payload. И CRC проверяется
    последней, чтобы ошибки длины и типа не маскировались под контрольную
    сумму.
    """
    if len(data) < 1:
        raise FrameError("пустой кадр", config.NAK_LENGTH)

    if data[0] != config.SYNC_BYTE:
        raise FrameError(f"ожидался sync {config.SYNC_BYTE:#04x}, получен {data[0]:#04x}")

    if len(data) < 5:
        raise FrameError(f"усечённый заголовок: {len(data)} байт", config.NAK_LENGTH)

    frame_type = data[1]
    seq = data[2]
    length = (data[3] << 8) | data[4]

    if length > config.MAX_PAYLOAD:
        raise FrameError(
            f"длина payload {length} превышает {config.MAX_PAYLOAD}",
            config.NAK_LENGTH,
            seq,
        )

    expected = 5 + length + 2
    if len(data) != expected:
        raise FrameError(
            f"длина кадра {len(data)} не совпадает с ожидаемой {expected}",
            config.NAK_LENGTH,
            seq,
        )

    payload = bytes(data[5 : 5 + length])
    body = bytes(data[: 5 + length])
    received = (data[5 + length] << 8) | data[6 + length]
    computed = crc16(body)

    if received != computed:
        raise FrameError(
            f"CRC: получено {received:#06x}, вычислено {computed:#06x}",
            config.NAK_CRC,
            seq,
        )

    if frame_type not in config.FRAME_TYPES:
        raise FrameError(f"неизвестный тип кадра {frame_type:#04x}", config.NAK_TYPE)

    return Frame(type=frame_type, seq=seq, payload=payload)


# --- Побитовое представление ------------------------------------------------


def preamble_bits() -> bytearray:
    """Преамбула как строгое чередование битов, вне 8N1.

    Период ровно 2 бита — на него настроен захват тактов autocorrelation.
    Если прогнать преамбулу через 8N1, стартовые нули и стоповые единицы
    добавят фронты с другим периодом, и захват даст кратную тактовую
    частоту. Кадр при этом начнёт декодироваться с устойчивым сдвигом.
    """
    bits = bytearray(config.PREAMBLE_BITS)
    for i in range(config.PREAMBLE_BITS):
        bits[i] = (config.PREAMBLE_BYTE >> (7 - (i % 8))) & 1
    return bits


def byte_to_bits(byte: int) -> bytearray:
    """Байт в 10 бит 8N1: стартовый 0, 8 данных младший бит первым, стоповый 1."""
    bits = bytearray(config.BITS_PER_BYTE)
    bits[0] = 0
    for i in range(config.DATA_BITS):
        bits[1 + i] = (byte >> i) & 1
    bits[9] = 1
    return bits


def bits_to_byte(bits) -> int:
    """Обратная к byte_to_bits операция. Ожидает 8 бит данных, младший первым."""
    byte = 0
    for i in range(config.DATA_BITS):
        if bits[i]:
            byte |= 1 << i
    return byte


def to_bits(frame_bytes: bytes, with_preamble: bool = True) -> bytearray:
    """Кадры в последовательность битов для модулятора."""
    bits = preamble_bits() if with_preamble else bytearray()
    for byte in frame_bytes:
        bits += byte_to_bits(byte)
    return bits


class BitCollector:
    """Собирает биты потока в байты и кадры.

    Возврат add_bit() различает три случая: None — ничего не произошло,
    Frame — кадр собран и проверен, FrameError — кадр похож на кадр, но
    испорчен. Исключение наружу не бросается намеренно: разбор живого
    потока встречает мусор как штатную ситуацию, и исключение на каждом
    мусорном бите сделало бы приёмник непригодным. Вызывающий сам решает,
    когда FrameError стоит отправить как NAK.

    Байт читается как 0 + 8 данных + 1, поэтому проверка обрамления даёт
    отдельный слой защиты: при пропуске или вставке бита обрамление
    ломается раньше, чем это увидит CRC.
    """

    def __init__(self) -> None:
        self._bits: list[int] = []
        self._bytes: bytearray = bytearray()
        self._framing = False
        self._expected = 0

    # Паттерн sync-бита в 8N1 — цель поиска фазы в idle-режиме.
    _SYNC_BITS = tuple(byte_to_bits(config.SYNC_BYTE))

    def reset(self) -> None:
        self._bits.clear()
        self._bytes.clear()
        self._framing = False
        self._expected = 0

    @property
    def sync_seen(self) -> bool:
        return self._framing

    def add_bit(self, bit: int) -> Frame | FrameError | None:
        """Добавить бит. Вернуть Frame или FrameError при событии.

        Поиск sync и разбор кадра — это два разных режима с разным
        отношением к фазе, и разделение принципиально.

        В idle (sync не найден) окно сдвигается на один бит до тех пор, пока
        десять бит не дадут ровно sync-байт. Нельзя здесь же жадно разбирать
        байты: преамбула — это чередование, и при неверной фазе оно даёт
        корректные обрамлённые байты 0x55. Ошибки обрамления нет, приёмник
        считает фазу верной, стабильно читает мусор до конца преамбулы и
        пропускает sync ровно на те биты, на которые фаза была сдвинута
        мусором перед кадром. На железе это выглядит так, будто канал
        работает, но первый кадр после помехи всегда теряется.

        В framing (sync найден) фаза зафиксирована, байты читаются жадно по
        десять, а потеря синхронизации видна по слому обрамления.
        """
        self._bits.append(1 if bit else 0)

        if not self._framing:
            return self._hunt_sync()
        return self._read_frame()

    def _hunt_sync(self) -> Frame | FrameError | None:
        """Сдвигать окно по одному биту, пока не встретится sync-байт."""
        if len(self._bits) < config.BITS_PER_BYTE:
            return None

        window = tuple(self._bits[: config.BITS_PER_BYTE])
        if window == self._SYNC_BITS:
            del self._bits[: config.BITS_PER_BYTE]
            self._framing = True
            self._bytes = bytearray((config.SYNC_BYTE,))
            self._expected = 0
        else:
            del self._bits[0]
        return None

    def _read_frame(self) -> Frame | FrameError | None:
        while len(self._bits) >= config.BITS_PER_BYTE:
            window = self._bits[: config.BITS_PER_BYTE]

            if window[0] != 0 or window[9] != 1:
                # Обрамление сломалось — фаза потеряна в середине кадра.
                # Возвращаемся к поиску sync, окно при этом не выбрасываем
                # целиком, иначе потерялась бы и накопленная в нём информация.
                self._end_frame()
                del self._bits[0]
                return self._hunt_sync()

            del self._bits[: config.BITS_PER_BYTE]
            result = self._accept_byte(bits_to_byte(window[1:9]))
            if result is not None:
                return result

        return None

    def _end_frame(self) -> None:
        """Завершение кадра: состояние разбора кадра сбрасывается, принятые
        биты остаются.

        Полное обнуление здесь отбросило бы до девяти битов следующей
        посылки, а кадры могут идти и без преамбулы между собой.
        """
        self._bytes.clear()
        self._framing = False
        self._expected = 0

    def _accept_byte(self, byte: int) -> Frame | FrameError | None:
        """Обработка байта в режиме framing. Sync здесь уже найден."""
        self._bytes.append(byte)

        # Ранний выход по длине: как только приняты 5 байт заголовка, полная
        # длина известна, и дальше кадр считается по байту без ожидания
        # таймаута по тишине.
        if len(self._bytes) == 5:
            length = (self._bytes[3] << 8) | self._bytes[4]
            if length > config.MAX_PAYLOAD:
                seq = self._bytes[2]
                raw = bytes(self._bytes)
                self._end_frame()
                return FrameError(
                    f"длина payload {length} превышает {config.MAX_PAYLOAD}",
                    config.NAK_LENGTH,
                    seq,
                    raw,
                )
            self._expected = 5 + length + 2
            return None

        if len(self._bytes) < 5 or len(self._bytes) < self._expected:
            return None

        collected = bytes(self._bytes)
        self._end_frame()
        try:
            return parse_frame(collected)
        except FrameError as error:
            error.raw = collected
            return error


# --- Удобные конструкторы -----------------------------------------------------


def ack(seq: int) -> bytes:
    return build_frame(config.ACK, seq, bytes((seq,)))


def nak(seq: int, reason: int) -> bytes:
    return build_frame(config.NAK, seq, bytes((seq, reason)))


def ping(seq: int) -> bytes:
    return build_frame(config.PING, seq)


def pong(seq: int) -> bytes:
    return build_frame(config.PONG, seq)


def cmd(seq: int, text: str) -> bytes:
    """Строка ввода. Перевод в конец не добавляется — его задаёт вызывающий.

    Разделение сознательное: Enter — это обычный байт 0x0D, и ИИ-агенту
    нужно уметь отправлять и строку без Enter (набор текста, ответ на
    запрос подтверждения).
    """
    return build_frame(config.CMD, seq, text.encode("utf-8"))


def key(seq: int, key_code: bytes) -> bytes:
    """Один ключ без текстового представления: Ctrl-C, стрелки, Tab."""
    return build_frame(config.KEY, seq, key_code)
