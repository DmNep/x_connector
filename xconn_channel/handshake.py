"""HELO-рукопожатие: версия, режим и размер экрана (docs/protocol.md 8.5).

Порядок установления соединения:

1. Клиент переходит в probe (300 бод) и шлёт HELO сериями с ретраями —
   ретраи уже встроены в MasterSession.exchange.
2. Агент постоянно слушает; мусор отбрасывается молча, на принятый HELO
   отвечает своим HELO.
3. Обе стороны сверяют PROTO_VERSION. Невыполнение — ошибка, а не тихая
   деградация: сторона с чужой версией молча перестаёт отвечать и выглядит
   как обрыв кабеля, поэтому несовпадение обязано называться своим именем.
4. Клиент запрашивает SCREEN_FULL и работает в согласованном режиме.

Payload HELO — 4 байта:

    version:1  mode:1  rows:1  cols:1

mode — код режима: 0 probe, 1 base, 2 stretch. Имя режима — строка, по
кабелю она не поедет без вариативной длины поля; код фиксирован байтом.

Согласование режима: клиент предлагает, агент подтверждает, если режим
в его списке возможностей, иначе понижает до старшего режима не быстрее
желаемого. Клиент работает в режиме из ответа агента.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from . import config, framing
from .framing import Frame
from .session import MasterSession, SessionError


class HandshakeError(Exception):
    """Рукопожатие не состоялось. Ошибка, а не тихая деградация (8.5)."""


# Коды режимов в payload HELO.
MODE_CODES = {config.PROBE: 0, config.BASE: 1, config.STRETCH: 2}
CODE_MODES = {code: mode for mode, code in MODE_CODES.items()}

# Порядок режимов по скорости: для понижения при отказе агента.
MODE_ORDER = (config.PROBE, config.BASE, config.STRETCH)

HELO_PAYLOAD_SIZE = 4


@dataclass(frozen=True)
class Helo:
    """Параметры стороны в HELO: версия протокола, режим, размер экрана."""

    version: int
    mode: str
    rows: int
    cols: int


def encode_helo(helo: Helo) -> bytes:
    try:
        mode_code = MODE_CODES[helo.mode]
    except KeyError:
        raise HandshakeError(
            f"неизвестный режим {helo.mode!r}, есть {sorted(MODE_CODES)}"
        ) from None
    return bytes((helo.version, mode_code, helo.rows, helo.cols))


def decode_helo(payload: bytes) -> Helo:
    if len(payload) != HELO_PAYLOAD_SIZE:
        raise HandshakeError(
            f"HELO payload {len(payload)} байт, ожидалось {HELO_PAYLOAD_SIZE}"
        )
    version, mode_code, rows, cols = payload
    mode = CODE_MODES.get(mode_code)
    if mode is None:
        raise HandshakeError(f"неизвестный код режима {mode_code:#04x}")
    if not 1 <= rows <= 255 or not 1 <= cols <= 255:
        raise HandshakeError(f"размер экрана {rows}x{cols} вне диапазона")
    return Helo(version, mode, rows, cols)


def negotiate(desired: str, supported: Sequence[str]) -> str:
    """Согласование режима: желаемый, если агент может, иначе понижение.

    Понижение — до старшего режима агента, не быстрее желаемого: клиент
    просил скорость не выше, и быстрее ему навязывать нельзя. Если агент
    не умеет ничего настолько медленного — его старший режим: медленнее
    клиент может отказаться сам, но предложение обязано быть.
    """
    supported = tuple(supported)
    if desired in supported:
        return desired
    slower = [
        mode
        for mode in MODE_ORDER
        if mode in supported and config.MODES[mode] <= config.MODES[desired]
    ]
    if slower:
        return slower[-1]
    any_supported = [mode for mode in MODE_ORDER if mode in supported]
    if not any_supported:
        raise HandshakeError(f"агент не поддерживает ни одного режима: {supported!r}")
    return any_supported[-1]


def client_handshake(
    master: MasterSession,
    desired_mode: str = config.DEFAULT_MODE,
    transport=None,
) -> Helo:
    """Рукопожатие клиента (docs/protocol.md 8.5, шаги 1-4).

    Сессия мастера обязана работать в probe: канал захватывается на
    медленном режиме (разнос тонов 1000 Гц при 300 бод даёт 21 дБ
    запаса, docs/protocol.md 3.2), и поднимается до base после
    согласования. Возвращает HELO агента; рабочий режим — agent.mode,
    размер экрана агента — agent.rows/cols.

    При успехе сама переводит master (и transport, если он передан) в
    согласованный режим — вызывающему не нужно помнить про эти две
    строки после вызова: раньше их пропуск оставлял транспорт слушать
    старым режимом, пока агент уже переключился, и обмен молча
    зависал на таймаутах без диагностики, указывающей на причину.
    """
    if master.mode != config.PROBE:
        raise HandshakeError(
            f"рукопожатие ведётся в {config.PROBE} (docs/protocol.md 8.5), "
            f"сессия мастера в {master.mode}"
        )
    try:
        config.check_mode(desired_mode)
    except RuntimeError as error:
        raise HandshakeError(str(error)) from None

    try:
        return _helo_once(master, desired_mode, transport)
    except SessionError as probe_err:
        # Агент после удачного сеанса остаётся в base и не слышит probe.
        # Новый клиент иначе молчит до restart (железо 2026-09-22).
        if transport is None:
            raise HandshakeError(str(probe_err)) from probe_err
        transport.set_mode(config.BASE)
        master.mode = config.BASE
        try:
            return _helo_once(master, desired_mode, transport)
        except SessionError as base_err:
            raise HandshakeError(
                f"нет ответа в probe и в base: {base_err}"
            ) from base_err


def _helo_once(master: MasterSession, desired_mode: str, transport) -> Helo:
    mine = Helo(
        config.PROTO_VERSION, desired_mode, config.DEFAULT_ROWS, config.DEFAULT_COLS
    )
    reply = master.exchange(config.HELO, encode_helo(mine))
    if reply.type != config.HELO:
        raise HandshakeError(f"ожидался HELO, агент ответил {reply.type_name}")

    agent = decode_helo(reply.payload)
    if agent.version != config.PROTO_VERSION:
        raise HandshakeError(
            f"версия протокола агента {agent.version}, наша {config.PROTO_VERSION}: "
            "невыполнение — ошибка, а не тихая деградация"
        )
    try:
        config.check_mode(agent.mode)
    except RuntimeError as error:
        raise HandshakeError(str(error)) from None
    if transport is not None:
        transport.set_mode(agent.mode)
    master.mode = agent.mode
    return agent


def agent_helo_handler(
    rows: int = config.DEFAULT_ROWS,
    cols: int = config.DEFAULT_COLS,
    supported: Sequence[str] = (config.PROBE, config.BASE),
) -> Callable[[Frame], tuple[int, bytes]]:
    """Handler агента для фазы рукопожатия: HELO -> HELO с согласованием.

    Агент отвечает своим HELO всегда — включая несовпадение версии
    клиента: честный ответ с собственной версией даст клиенту ошибиться
    громко, а молчание выглядело бы как обрыв кабеля.

    Кадры не-HELO в фазе рукопожатия получают NAK с NAK_STATE:
    «состояние не позволяет» — это ровно он, соединение ещё не установлено.
    После рукопожатия агент подменяет handler на рабочий.
    """

    def handler(frame: Frame) -> tuple[int, bytes]:
        if frame.type == config.HELO:
            client = decode_helo(frame.payload)
            mode = negotiate(client.mode, supported)
            return (
                config.HELO,
                encode_helo(Helo(config.PROTO_VERSION, mode, rows, cols)),
            )
        return config.NAK, bytes((frame.seq, config.NAK_STATE))

    return handler
