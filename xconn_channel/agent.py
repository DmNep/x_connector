"""Ядро агента: PTY-цикл над Vt100 и снимки (docs/protocol.md 9).

Агент держит bash в PTY и VT100-эмулятор поверх. Байты из CMD/KEY идут в
master-сторону PTY, вывод PTY разбирается эмулятором, состояние сетки
транслируется клиенту снимками. Размер окна PTY задаётся RESIZE, по
умолчанию 24×80; программы, читающие TIOCGWINSZ, получают корректное
значение.

Ядро не открывает PTY само — оно получает два каллбэка: write_pty(bytes)
и read_pty() -> bytes. Открытие PTY, SIGWINCH и перезапуск упавшего bash —
обвязка уровня ОС (Linux-сторона), ядро остаётся кросс-платформенным и
проверяется целиком в памяти. Требование автономности (AGENTS.md 3.2):
в модуле нет ни одного сетевого импорта.

Логика снимков:

- Первый обмен и обмен после RESIZE — SCREEN_FULL, он становится базой
  (base_seq) для дельт.
- Обычный ответ — SCREEN_DELTA изменённых строк от базы.
- Повтор REQ (ретрансляция мастера) не исполняет команду повторно, но
  ответ пересчитывается: клиент потерял дельту, дельта от старой базы
  могла не подойти, поэтому повтор отдаёт SCREEN_FULL (8.2, 6.2).
"""

from __future__ import annotations

import os
import time

from . import config, screen, transfer
from .framing import Frame
from .screen import Screen
from .transfer import TransferError
from .vt100 import Vt100


class AgentCore:
    """Ядро агента: PTY-цикл и снимки, без аудио и без ОС-специфики."""

    def __init__(
        self,
        write_pty,
        read_pty,
        rows=None,
        cols=None,
        pump_wait_ms: float = 0,
        pump_idle_ms: float = 0,
        file_root=None,
    ) -> None:
        self._write_pty = write_pty
        self._read_pty = read_pty
        helo_rows = config.DEFAULT_ROWS if rows is None else rows
        helo_cols = config.DEFAULT_COLS if cols is None else cols
        self._vt = Vt100(helo_rows, helo_cols)
        self._base: Screen | None = None
        self._base_seq: int = 0
        self._replies = bytearray()
        # На FakePty данные появляются синхронно с записью — ждать нечего.
        # На живом bash вывод приходит с задержкой: wait — до первого байта,
        # idle — тишина после последнего, после которой снимок стабилен.
        self._pump_wait_ms = pump_wait_ms
        self._pump_idle_ms = pump_idle_ms
        self._file_root = file_root
        self._xfer = None
        self._last_reply: tuple[int, bytes] | None = None
        self.stats = {
            "cmds": 0,
            "keys": 0,
            "resizes": 0,
            "fulls": 0,
            "deltas": 0,
            "files": 0,
        }

    @property
    def screen(self) -> Screen:
        return self._vt.screen

    # --- PTY-цикл -------------------------------------------------------------

    def pump(self, wait_ms: float | None = None, idle_ms: float | None = None) -> None:
        """Забрать вывод PTY в эмулятор, ответы терминала — обратно в PTY.

        Чтение идёт до исчерпания: вывод приходит кусками на любых
        границах, и один обмен обязан увидеть его целиком, иначе дельта
        считается по половине вывода. Ответы на запросы (ESC[6n и прочие)
        предназначены программе на slave-стороне PTY и обязаны идти в
        master-сторону — туда же, куда пишет клиент (docs/protocol.md 6.3).

        wait_ms — сколько ждать первого байта (живой bash не отвечает
        мгновенно). idle_ms — тишина после последнего байта, после которой
        считаем вывод законченным. Нули — как раньше: один проход до пустого
        чтения, без пауз. Это сохраняет тесты на FakePty.
        """
        wait = self._pump_wait_ms if wait_ms is None else wait_ms
        idle = self._pump_idle_ms if idle_ms is None else idle_ms
        first_deadline = time.monotonic() + wait / 1000.0
        last_data = time.monotonic()
        got = False
        while True:
            data = self._read_pty()
            if data:
                got = True
                last_data = time.monotonic()
                replies = self._vt.feed(data)
                if replies:
                    self._write_pty(replies)
                continue
            now = time.monotonic()
            if not got:
                if now < first_deadline:
                    time.sleep(min(0.01, first_deadline - now))
                    continue
                return
            if idle and (now - last_data) * 1000.0 < idle:
                time.sleep(0.01)
                continue
            return

    # --- обработка кадров --------------------------------------------------------

    def handle(self, frame: Frame) -> tuple[int, bytes]:
        """Handler для AgentSession: CMD/KEY/RESIZE/FILE_* -> ответ."""
        reply = self._handle(frame)
        self._last_reply = reply
        return reply

    def _handle(self, frame: Frame) -> tuple[int, bytes]:
        if frame.type == config.CMD:
            self.stats["cmds"] += 1
            self._write_pty(frame.payload)
            self.pump()
            return self._snapshot(frame.seq)
        if frame.type == config.KEY:
            self.stats["keys"] += 1
            self._write_pty(frame.payload)
            self.pump()
            return self._snapshot(frame.seq)
        if frame.type == config.RESIZE:
            self.stats["resizes"] += 1
            if len(frame.payload) != 2:
                return config.NAK, bytes((frame.seq, config.NAK_LENGTH))
            rows, cols = frame.payload
            if not 1 <= rows <= 255 or not 1 <= cols <= 255:
                return config.NAK, bytes((frame.seq, config.NAK_LENGTH))
            self._vt.resize(rows, cols)
            # База недействительна: форма сетки изменилась, дельта от
            # старой базы не применима — следующий снимок полный.
            self._base = None
            self.pump()
            return self._snapshot(frame.seq)
        if frame.type == config.PING:
            # Пустой PING — PONG. Первый байт PING_FULL — полный снимок
            # по запросу (docs/protocol.md 6.2).
            if frame.payload[:1] == bytes((config.PING_FULL,)):
                self._base = None
                self.pump()
                return self._snapshot(frame.seq)
            return config.PONG, b""
        if frame.type in (config.FILE_OPEN, config.FILE_DATA, config.FILE_CLOSE):
            return self._handle_file(frame)
        return config.NAK, bytes((frame.seq, config.NAK_TYPE))

    def _handle_file(self, frame: Frame) -> tuple[int, bytes]:
        """FILE_OPEN/DATA/CLOSE → NOTE или NAK (docs/protocol.md 7)."""
        if self._file_root is None:
            return config.NAK, bytes((frame.seq, config.NAK_STATE))
        try:
            if frame.type == config.FILE_OPEN:
                return self._file_open(frame)
            if frame.type == config.FILE_DATA:
                return self._file_data(frame)
            return self._file_close(frame)
        except TransferError:
            return config.NAK, bytes((frame.seq, config.NAK_LENGTH))

    def _file_open(self, frame: Frame) -> tuple[int, bytes]:
        name, size, digest = transfer.decode_open(frame.payload)
        os.makedirs(self._file_root, exist_ok=True)
        self._xfer = {
            "name": name,
            "size": size,
            "digest": digest,
            "buf": bytearray(size),
            "got": bytearray(size),
            "path": transfer.dest_path(self._file_root, name),
        }
        return config.NOTE, bytes((config.NOTE_OK,))

    def _file_data(self, frame: Frame) -> tuple[int, bytes]:
        if self._xfer is None:
            return config.NAK, bytes((frame.seq, config.NAK_STATE))
        offset, chunk = transfer.decode_data(frame.payload)
        end = offset + len(chunk)
        if end > self._xfer["size"]:
            return config.NAK, bytes((frame.seq, config.NAK_LENGTH))
        self._xfer["buf"][offset:end] = chunk
        self._xfer["got"][offset:end] = b"\x01" * len(chunk)
        return config.NOTE, bytes((config.NOTE_OK,))

    def _file_close(self, frame: Frame) -> tuple[int, bytes]:
        if self._xfer is None:
            return config.NAK, bytes((frame.seq, config.NAK_STATE))
        digest = transfer.decode_close(frame.payload)
        xfer = self._xfer
        if xfer["size"] and (0 in xfer["got"]):
            self._xfer = None
            return config.NAK, bytes((frame.seq, config.NAK_STATE))
        actual = transfer.crc32(bytes(xfer["buf"]))
        if digest != xfer["digest"] or actual != digest:
            self._xfer = None
            return config.NAK, bytes((frame.seq, config.NAK_STATE))
        xfer["path"].write_bytes(bytes(xfer["buf"]))
        self._xfer = None
        self.stats["files"] += 1
        return config.NOTE, bytes((config.NOTE_OK,))

    def replay(self) -> tuple[int, bytes]:
        """Ответ на повторный REQ: полный снимок, кроме FILE/PONG/NAK.

        Команда в PTY не повторяется. Клиент потерял дельту — повтор
        экрана обязан быть SCREEN_FULL (8.2, 6.2). Повтор FILE_* и PING
        отдаёт тот же NOTE/PONG/NAK, иначе клиент примет сетку за статус.
        """
        if self._last_reply is not None:
            rtype = self._last_reply[0]
            if rtype not in (config.SCREEN_FULL, config.SCREEN_DELTA):
                return self._last_reply
        return config.SCREEN_FULL, screen.serialize_full(self._vt.screen)

    # --- снимки --------------------------------------------------------------------

    def _snapshot(self, seq: int) -> tuple[int, bytes]:
        """Снимок состояния: полный при отсутствии базы, дельта иначе.

        seq кадра становится base_seq следующей дельты: дельта считается
        от последнего доставленного снимка (6.2).
        """
        current = self._vt.screen
        if self._base is None:
            self._base = Screen(current.rows, current.cols)
            self._base.cells[:] = current.cells
            self._base.flags = current.flags
            self._base_seq = seq
            self.stats["fulls"] += 1
            return config.SCREEN_FULL, screen.serialize_full(current)

        changed = self._base.changed_rows(current)
        try:
            payload = screen.serialize_delta(self._base_seq, current, changed)
        except ValueError:
            # Дельта не влезает в кадр: слишком много изменилось, полный
            # снимок дешевле и надёжнее (docs/protocol.md 6.2).
            self._base = None
            return self._snapshot(seq)

        self._base.cells[:] = current.cells
        self._base.flags = current.flags
        self._base_seq = seq
        self.stats["deltas"] += 1
        return config.SCREEN_DELTA, payload
