"""Константы канала x_connector.

Единственный источник истины по значениям протокола. Спецификация —
docs/protocol.md, раздел 10. Любое расхождение кода и документации
исправляется в документации, а не в коде: обе стороны канала (клиент на
ноутбуке и агент на сервере) импортируют именно этот модуль, поэтому
разъезд здесь означает рассинхронизацию сторон, а не опечатку.

Внешних зависимостей нет намеренно: сервер устанавливается автономно с
флешки и работает при полностью выключенной сети (AGENTS.md 3.2, 3.3).
"""

from __future__ import annotations

# --- Дискретизация -----------------------------------------------------------

# Частота захвата звуковой карты. Обязана быть стандартной для кодеков,
# иначе драйвер включит свой ресемплинг с непредсказуемой фазой.
DEVICE_SAMPLE_RATE = 48000

# Рабочая частота DSP после прореживания. Полезная полоса до 3400 Гц,
# Никвист 8000 Гц — запас двукратный (docs/protocol.md 2).
SAMPLE_RATE = 16000

# Прореживание 48000 -> 16000 целочисленное, без дробного коэффициента.
DECIMATION = DEVICE_SAMPLE_RATE // SAMPLE_RATE
assert DECIMATION * SAMPLE_RATE == DEVICE_SAMPLE_RATE

# --- Режимы модуляции --------------------------------------------------------

PROBE = "probe"
BASE = "base"
STRETCH = "stretch"

# Бод и пара тонов (mark, space) в герцах. Mark соответствует логической
# единице. Разнесение тонов обязано быть порядка скорости передачи, иначе
# некогерентный детектор не различает тона (docs/protocol.md 3.2).
MODES: dict[str, int] = {
    PROBE: 300,
    BASE: 1200,
    STRETCH: 2400,
}

TONES: dict[str, tuple[int, int]] = {
    PROBE: (1200, 2200),
    BASE: (1200, 2200),
    STRETCH: (1400, 3400),
}

DEFAULT_MODE = BASE

# Экспериментальный режим. На паре 1200/2200 на 2400 бод неработоспособен,
# для него заведена отдельная пара тонов; включается только после замера BER
# на конкретном железе и явного согласия владельца (docs/protocol.md 3.2).
STRETCH_ENABLED = False


def baud_of(mode: str) -> int:
    try:
        return MODES[mode]
    except KeyError:
        raise ValueError(f"неизвестный режим {mode!r}, есть {sorted(MODES)}") from None


def tones_of(mode: str) -> tuple[int, int]:
    try:
        return TONES[mode]
    except KeyError:
        raise ValueError(f"неизвестный режим {mode!r}, есть {sorted(MODES)}") from None


def samples_per_bit(mode: str) -> float:
    """Длительность бита в отсчётах рабочей дискретизации."""
    return SAMPLE_RATE / baud_of(mode)


def byte_time_ms(mode: str) -> float:
    """Время байта 8N1 в миллисекундах: 10 бит на байт."""
    return 10000.0 / baud_of(mode)


def check_mode(mode: str) -> None:
    """Запрет на использование выключенного режима. Зовётся при установке соединения."""
    if mode == STRETCH and not STRETCH_ENABLED:
        raise RuntimeError(
            "режим stretch выключен: включите STRETCH_ENABLED только после "
            "замеров tools/ber.py на целевом тракте (docs/protocol.md 3.2)"
        )
    baud_of(mode)


# --- Кадрирование -----------------------------------------------------------

# Преамбула — побитовое чередование, НЕ через 8N1. Строгий период 2 бита
# нужен для захвата тактов autocorrelation; обрамление каждого байта
# стартовым и стоповым битами этот период исказит (docs/protocol.md 4).
PREAMBLE_BYTE = 0x55
PREAMBLE_BITS = 40

SYNC_BYTE = 0x7E

# Байт 8N1: стартовый, 8 данных (младший первым), стоповый.
BITS_PER_BYTE = 10
DATA_BITS = 8

# Байт mark — все данные единицы. Используется как заполнитель и для
# калибровки: на нем детектор обязан давать устойчивую единицу.
MARK_BYTE = 0xFF

# Фиксированная часть кадра до payload: sync, тип, seq, длина(2), CRC(2).
HEADER_SIZE = 1  # sync
FIELD_SIZE_AFTER_SYNC = 6  # type, seq, len(2), crc(2)
MAX_PAYLOAD = 240

# Пропускная способность кадра в битах, N — размер payload.
FRAME_OVERHEAD_BITS = PREAMBLE_BITS + BITS_PER_BYTE * (1 + FIELD_SIZE_AFTER_SYNC)


def frame_bits(payload_size: int) -> int:
    if not 0 <= payload_size <= MAX_PAYLOAD:
        raise ValueError(f"payload {payload_size} вне диапазона 0..{MAX_PAYLOAD}")
    return FRAME_OVERHEAD_BITS + BITS_PER_BYTE * payload_size


def frame_seconds(payload_size: int, mode: str = DEFAULT_MODE) -> float:
    return frame_bits(payload_size) / baud_of(mode)


# --- Типы кадров ------------------------------------------------------------

ACK = 0x03
NAK = 0x04
HELO = 0x40
PING = 0x41
PONG = 0x42
# Первый байт payload PING: запрос SCREEN_FULL (docs/protocol.md 6.2).
PING_FULL = 0x01
CMD = 0x10
KEY = 0x11
RESIZE = 0x12
SCREEN_MORE = 0x13
SCREEN_FULL = 0x20
SCREEN_DELTA = 0x21
NOTE = 0x22
SCREEN_PART = 0x23
FILE_OPEN = 0x30
FILE_DATA = 0x31
FILE_CLOSE = 0x32
FILE_GET = 0x33
FILE_OFFER = 0x34
FILE_PULL = 0x35

# Имя в FILE_OPEN — 64 байта UTF-8 с NUL-дополнением (docs/protocol.md 7).
FILE_NAME_BYTES = 64
# Заголовок FILE_DATA: offset(4) + length(2). Кусок = MAX_PAYLOAD − 6.
FILE_DATA_HDR = 6
FILE_CHUNK = MAX_PAYLOAD - FILE_DATA_HDR
FILE_MAX_BYTES = 256 * 1024
FILE_GET_NAME_MAX = 200
# NOTE: успешный FILE_* (один байт).
NOTE_OK = 0x00

# Нарезка снимка: kind:1 index:1 total:1 + кусок zlib.
SCREEN_PART_HDR = 3
SCREEN_CHUNK = MAX_PAYLOAD - SCREEN_PART_HDR
SCREEN_PART_FULL = 0
SCREEN_PART_DELTA = 1

FRAME_TYPES: dict[int, str] = {
    ACK: "ACK",
    NAK: "NAK",
    HELO: "HELO",
    PING: "PING",
    PONG: "PONG",
    CMD: "CMD",
    KEY: "KEY",
    RESIZE: "RESIZE",
    SCREEN_MORE: "SCREEN_MORE",
    SCREEN_FULL: "SCREEN_FULL",
    SCREEN_DELTA: "SCREEN_DELTA",
    NOTE: "NOTE",
    SCREEN_PART: "SCREEN_PART",
    FILE_OPEN: "FILE_OPEN",
    FILE_DATA: "FILE_DATA",
    FILE_CLOSE: "FILE_CLOSE",
    FILE_GET: "FILE_GET",
    FILE_OFFER: "FILE_OFFER",
    FILE_PULL: "FILE_PULL",
}

# Причины NAK.
NAK_CRC = 0x01
NAK_TYPE = 0x02
NAK_LENGTH = 0x03
NAK_STATE = 0x04

# --- Протокол ---------------------------------------------------------------

PROTO_VERSION = 1

DEFAULT_ROWS = 24
DEFAULT_COLS = 80

# Живой ALC897: полный 24×80 после motd не влезает в MAX_PAYLOAD (nak=0x03),
# после такого NAK агент часто перестаёт отвечать на HELO. Клиент жмёт
# окно до этих размеров сразу после рукопожатия, до первого снимка.
SAFE_ROWS = 8
SAFE_COLS = 32

# Кодовая страница экрана. cp437 даёт псевграфику консольных программ
# (dialog, установщики, box drawing) одним байтом на символ.
SCREEN_CODEPAGE = "cp437"

# --- Тайминги полудуплекса --------------------------------------------------

GAP_MS = 120  # тишина при смене направления
T_LEAD_MS = 60  # пауза перед передачей после приёма
# Сколько агент ждёт вывод PTY перед снимком (host.pump).
PUMP_WAIT_MS = 250
PUMP_IDLE_MS = 80
# Команда ещё в foreground (TIOCGPGRP ≠ bash) — не снимать экран.
# ping / netplan apply иначе обрезаются через 80 мс тишины.
PUMP_BUSY_MS = 20000
# Ожидание начала несущей ответа. Должно покрыть pump + T_LEAD, иначе
# медленный bash неотличим от мёртвого кабеля. HELO/PING остаются
# короткими; CMD/KEY ждут до PUMP_BUSY (см. T_CMD_CARRIER_MS).
T_CARRIER_MS = PUMP_WAIT_MS + PUMP_IDLE_MS + T_LEAD_MS + 110  # 500
T_CMD_CARRIER_MS = PUMP_BUSY_MS + T_LEAD_MS + 500
T_IDLE_BYTES = 12  # тишина внутри кадра, в байт-времени
MAX_RETRY = 3

assert T_CARRIER_MS >= PUMP_WAIT_MS + PUMP_IDLE_MS + T_LEAD_MS


def t_idle_ms(mode: str = DEFAULT_MODE) -> float:
    return T_IDLE_BYTES * byte_time_ms(mode)


# --- Уровни и детектор ------------------------------------------------------

# Пик сигнала. 8192 — ровно четверть 16-битной шкалы, то есть -12.04 dBFS.
# Запас по пику защищает от клиппинга: огибающая CPFSK на переходах
# выше амплитуды тонов, а клиппинг порождает гармоники в полосу другого тона.
PEAK_AMPLITUDE = 8192
PEAK_DBFS = -12.0

# Порог отношения энергий тонов в детекторе с гистерезисом.
DETECT_K = 1.3

# Коэффициент тактового PLL. Слишком большое значение делает захват
# неустойчивым к шуму, слишком маленькое — не успевает за дрейфом кварцев.
PLL_GAIN = 0.02

# Energy gate берётся на 12 дБ выше измеренного шума (docs/protocol.md 11).
GATE_MARGIN_DB = 12.0
