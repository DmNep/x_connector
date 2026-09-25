"""VT100-эмулятор: поток байт PTY в сетку Screen (docs/protocol.md 6.3, 9).

Агент держит сетку символов, а не поток stdout (AGENTS.md 2.7): вывод bash
и программ под ним разбирается эмулятором, состояние сетки транслируется
клиенту снимками.

Минимальный набор эмуляции (6.3): печатаемые символы, CR/LF/BEL/BS/TAB,
ESC [ с параметрами — CUU/CUD/CUF/CUB, CUP, ED, EL, IL, DL, DECSTBM, SGR,
DECSET/DECRST для ?25 (видимость курсора) и ?1049 (альтернативный буфер).
Обязательны ответы на запросы терминала: ESC [ 6 n (курсор), ESC [ c
(идентификатор), ESC [ 14 t / ESC [ 18 t (размер в пикселях и символах).
Без них vim, htop, less и инсталляторы зависают в ожидании ответа.

Парсер — побайтовый автомат: поток из PTY приходит кусками на любых
границах, последовательность может быть разрезана между кусками, и
эмулятор обязан дособрать её, а не выбросить половину.

SGR 0/7/27 хранит reverse в Screen.inverse: сетка по-прежнему cp437,
но инверсия едет в снимке (docs/protocol.md 6.3).
"""

from __future__ import annotations

from . import config
from .screen import FLAG_BEL, FLAG_CURSOR_VISIBLE, Screen

# Идентификатор терминала для ответа на ESC [ c: VT100 с AVO.
_DEVICE_ATTRIBUTES = b"\x1b[?1;2c"

# Размер ячейки символа в пикселях для ответа на ESC [ 14 t: PTY знает
# размер в символах, пикселей у него нет, отвечаем расчётом от ячейки.
_CELL_WIDTH_PX = 8
_CELL_HEIGHT_PX = 16

_TAB_STOP = 8


class Vt100:
    """Эмулятор над Screen: feed(bytes) -> байты ответов в PTY.

    Ответы на запросы терминала возвращаются из feed(), а не пишутся в
    сетку: они предназначены программе на той стороне PTY (vim, htop), и
    агент обязан записать их обратно в master-сторону PTY.
    """

    def __init__(
        self,
        rows: int = config.DEFAULT_ROWS,
        cols: int = config.DEFAULT_COLS,
    ) -> None:
        self.screen = Screen(rows, cols)
        self._alt = Screen(rows, cols)
        self._alt_saved = Screen(rows, cols)
        self._alt_active = False
        # Границы скролл-региона DECSTBM, включительно.
        self._top = 0
        self._bottom = rows - 1
        self._wrap_pending = False
        self._sgr_reverse = False
        self._state = _GROUND
        self._params = bytearray()
        self._private = b""
        self._osc_len = 0

    # --- приём потока -------------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        """Пропустить кусок вывода PTY. Возврат — байты ответов в PTY."""
        replies = bytearray()
        for byte in data:
            reply = self._step(byte)
            if reply:
                replies += reply
        return bytes(replies)

    def _step(self, byte: int) -> bytes | None:
        if self._state == _GROUND:
            return self._ground(byte)
        if self._state == _ESC:
            return self._escape(byte)
        if self._state == _OSC:
            return self._osc(byte)
        if self._state == _OSC_ESC:
            return self._osc_esc(byte)
        return self._csi(byte)

    # --- основное состояние --------------------------------------------------

    def _ground(self, byte: int) -> bytes | None:
        if byte == 0x1B:
            self._state = _ESC
            return None
        if byte == 0x0D:  # CR
            self.screen.cur_col = 0
            self._wrap_pending = False
        elif byte in (0x0A, 0x0B, 0x0C):  # LF, VT, FF — как перевод строки
            self._linefeed()
        elif byte == 0x07:  # BEL
            self.screen.flags |= FLAG_BEL
        elif byte == 0x08:  # BS
            if self.screen.cur_col > 0:
                self.screen.cur_col -= 1
            self._wrap_pending = False
        elif byte == 0x09:  # TAB
            self._tab()
        elif byte >= 0x20:
            # Печатаемые 0x20..0xFF: один байт cp437, один символ сетки.
            self._print(byte)
        # Управляющие C0 вне списка (SO/SI, XON/XOFF) — пропускаются.
        return None

    def _print(self, byte: int) -> None:
        if self._wrap_pending:
            # DECAWM: печать после последней колонки переносит строку.
            self.screen.cur_col = 0
            self._linefeed()
        idx = self.screen.cur_row * self.screen.cols + self.screen.cur_col
        self.screen.cells[idx] = byte
        self.screen.inverse[idx] = 1 if self._sgr_reverse else 0
        if self.screen.cur_col + 1 < self.screen.cols:
            self.screen.cur_col += 1
            self._wrap_pending = False
        else:
            self._wrap_pending = True

    def _linefeed(self) -> None:
        row = self.screen.cur_row
        if row == self._bottom:
            self._scroll_up(1)
        elif row < self._bottom:
            self.screen.cur_row = row + 1
        self._wrap_pending = False

    def _tab(self) -> None:
        col = self.screen.cur_col
        nxt = (col // _TAB_STOP + 1) * _TAB_STOP
        self.screen.cur_col = min(nxt, self.screen.cols - 1)
        self._wrap_pending = False

    # --- escape-состояния ------------------------------------------------------

    def _escape(self, byte: int) -> bytes | None:
        if byte == ord("["):
            self._state = _CSI
            self._params = bytearray()
            self._private = b""
            return None
        if byte == ord("]"):
            # OSC: ESC ] … BEL или ST. Иначе payload (133;start=…)
            # печатается в сетку — живой bash/systemd так забивает 24×80.
            self._state = _OSC
            self._osc_len = 0
            return None
        # ESC-команды без параметров: сохранение/восстановление курсора
        # (DECSC/DECRC) — единственные из нужных минимому.
        if byte == ord("7"):
            self._save_cursor()
        elif byte == ord("8"):
            self._restore_cursor()
        self._state = _GROUND
        return None

    def _osc(self, byte: int) -> bytes | None:
        if byte == 0x07 or byte == 0x9C:
            self._state = _GROUND
            return None
        if byte == 0x1B:
            self._state = _OSC_ESC
            return None
        self._osc_len += 1
        if self._osc_len > 4096:
            self._state = _GROUND
        return None

    def _osc_esc(self, byte: int) -> bytes | None:
        if byte == ord("\\"):
            self._state = _GROUND
            return None
        self._state = _ESC
        return self._escape(byte)

    def _csi(self, byte: int) -> bytes | None:
        if byte in b"?0123456789;:<=>":
            # Приватный префикс обязан идти первым, параметры после него.
            if not self._params and byte == ord("?"):
                self._private = b"?"
            else:
                self._params.append(byte)
            return None
        self._state = _GROUND
        return self._csi_final(byte)

    def _csi_final(self, final: int) -> bytes | None:
        params = self._parse_params(bytes(self._params))
        private = self._private
        self._params = bytearray()
        self._private = b""
        if private:
            return self._csi_private(final, params)
        return self._csi_dispatch(final, params)

    @staticmethod
    def _parse_params(params: bytes) -> list[int]:
        """Параметры CSI: пустые — нули (ESC [ H это ESC [ 0;0 H)."""
        if not params:
            return [0]
        return [int(p) if p else 0 for p in params.split(b";")]

    # --- публичные CSI -------------------------------------------------------

    def _csi_dispatch(self, final: int, p: list[int]) -> bytes | None:
        s = self.screen
        if final == ord("A"):  # CUU
            limit = self._top if s.cur_row > self._top else 0
            s.cur_row = max(limit, s.cur_row - max(1, p[0]))
            self._wrap_pending = False
        elif final == ord("B"):  # CUD
            s.cur_row = min(self._bottom, s.cur_row + max(1, p[0]))
            self._wrap_pending = False
        elif final == ord("C"):  # CUF
            s.cur_col = min(s.cols - 1, s.cur_col + max(1, p[0]))
            self._wrap_pending = False
        elif final == ord("D"):  # CUB
            s.cur_col = max(0, s.cur_col - max(1, p[0]))
            self._wrap_pending = False
        elif final == ord("G"):  # CHA: колонка абсолютная
            s.cur_col = min(max(0, p[0] - 1), s.cols - 1)
            self._wrap_pending = False
        elif final == ord("H") or final == ord("f"):  # CUP/HVP
            row = max(1, p[0]) - 1
            col = (p[1] if len(p) > 1 else 0) and max(1, p[1]) - 1
            s.cur_row = min(max(row, 0), s.rows - 1)
            s.cur_col = min(max(col, 0), s.cols - 1)
            self._wrap_pending = False
        elif final == ord("J"):  # ED
            self._erase_display(p[0])
        elif final == ord("K"):  # EL
            self._erase_line(p[0])
        elif final == ord("L"):  # IL
            self._insert_lines(max(1, p[0]))
        elif final == ord("M"):  # DL
            self._delete_lines(max(1, p[0]))
        elif final == ord("r"):  # DECSTBM
            self._set_scroll_region(p[0], p[1] if len(p) > 1 else 0)
        elif final == ord("m"):  # SGR
            self._apply_sgr(p)
        elif final == ord("n"):  # DSR
            if p[0] == 6:
                # Позиция курсора, 1-базная — так ждут программы.
                return b"\x1b[%d;%dR" % (s.cur_row + 1, s.cur_col + 1)
        elif final == ord("c"):  # DA: идентификатор терминала.
            return _DEVICE_ATTRIBUTES
        elif final == ord("t"):  # window ops
            if p[0] == 18:
                # Размер в символах.
                return b"\x1b[8;%d;%dt" % (s.rows, s.cols)
            if p[0] == 14:
                # Размер в пикселях: расчёт от ячейки, PTY пикселей не знает.
                return b"\x1b[4;%d;%dt" % (
                    s.rows * _CELL_HEIGHT_PX,
                    s.cols * _CELL_WIDTH_PX,
                )
        return None

    def _apply_sgr(self, params: list[int]) -> None:
        codes = params or [0]
        for code in codes:
            if code in (0, 27):
                self._sgr_reverse = False
            elif code == 7:
                self._sgr_reverse = True

    def _csi_private(self, final: int, p: list[int]) -> bytes | None:
        if final == ord("h"):  # DECSET
            if p[0] == 25:
                self.screen.flags |= FLAG_CURSOR_VISIBLE
            elif p[0] == 1049:
                self._enter_alt()
        elif final == ord("l"):  # DECRST
            if p[0] == 25:
                self.screen.flags &= ~FLAG_CURSOR_VISIBLE
            elif p[0] == 1049:
                self._leave_alt()
        return None

    # --- операции сетки --------------------------------------------------------

    def _erase_display(self, mode: int) -> None:
        s = self.screen
        if mode == 0:  # от курсора до конца
            self._erase_line(0)
            blank = b" " * s.cols
            for row in range(s.cur_row + 1, s.rows):
                s.set_row(row, blank)
        elif mode == 1:  # от начала до курсора
            blank = b" " * s.cols
            for row in range(0, s.cur_row):
                s.set_row(row, blank)
            self._erase_line(1)
        elif mode == 2:  # всё
            s.blank()

    def _erase_line(self, mode: int) -> None:
        s = self.screen
        start = s.cur_row * s.cols
        if mode == 0:  # от курсора до конца строки
            n = s.cols - s.cur_col
            s.write_span(start + s.cur_col, b" " * n)
        elif mode == 1:  # от начала строки до курсора
            s.write_span(start, b" " * (s.cur_col + 1))
        elif mode == 2:  # строка целиком
            s.write_span(start, b" " * s.cols)

    def _scroll_up(self, n: int) -> None:
        """Скролл региона вверх на n строк: верхние уходят, снизу пустые."""
        s = self.screen
        top, bottom = self._top, self._bottom
        height = bottom - top + 1
        n = min(n, height)
        block = s.cells[top * s.cols : (bottom + 1) * s.cols]
        inv = s.inverse[top * s.cols : (bottom + 1) * s.cols]
        s.cells[top * s.cols : (bottom + 1) * s.cols] = (
            block[n * s.cols :] + b" " * (n * s.cols)
        )
        s.inverse[top * s.cols : (bottom + 1) * s.cols] = (
            inv[n * s.cols :] + b"\x00" * (n * s.cols)
        )

    def _insert_lines(self, n: int) -> None:
        """IL: пустые строки на курсоре, нижние уходят за bottom."""
        s = self.screen
        if not self._top <= s.cur_row <= self._bottom:
            return
        height = self._bottom - s.cur_row + 1
        n = min(n, height)
        start = s.cur_row * s.cols
        end = (self._bottom + 1) * s.cols
        block = s.cells[start:end]
        inv = s.inverse[start:end]
        s.cells[start:end] = b" " * (n * s.cols) + block[: (height - n) * s.cols]
        s.inverse[start:end] = b"\x00" * (n * s.cols) + inv[: (height - n) * s.cols]

    def _delete_lines(self, n: int) -> None:
        """DL: строки на курсоре уходят, снизу пустые."""
        s = self.screen
        if not self._top <= s.cur_row <= self._bottom:
            return
        height = self._bottom - s.cur_row + 1
        n = min(n, height)
        start = s.cur_row * s.cols
        end = (self._bottom + 1) * s.cols
        block = s.cells[start:end]
        inv = s.inverse[start:end]
        s.cells[start:end] = block[n * s.cols :] + b" " * (n * s.cols)
        s.inverse[start:end] = inv[n * s.cols :] + b"\x00" * (n * s.cols)

    def _set_scroll_region(self, top: int, bottom: int) -> None:
        s = self.screen
        top = max(1, top) - 1
        bottom = (bottom - 1) if bottom else s.rows - 1
        if 0 <= top < bottom < s.rows:
            self._top = top
            self._bottom = bottom
            # Курсор в home при установке региона — по спецификации DECSTBM.
            s.cur_row, s.cur_col = top, 0
            self._wrap_pending = False

    # --- альтернативный буфер ---------------------------------------------------

    def _enter_alt(self) -> None:
        if self._alt_active:
            return
        self._alt_active = True
        # ?1049: сохранить основной экран и курсор, дать чистый альт.
        self._alt_saved.cells[:] = self.screen.cells
        self._alt_saved.inverse[:] = self.screen.inverse
        self._alt_saved.cur_row = self.screen.cur_row
        self._alt_saved.cur_col = self.screen.cur_col
        self._alt_saved.flags = self.screen.flags
        self.screen.blank()
        self.screen.cur_row = self.screen.cur_col = 0

    def _leave_alt(self) -> None:
        if not self._alt_active:
            return
        self._alt_active = False
        self.screen.cells[:] = self._alt_saved.cells
        self.screen.inverse[:] = self._alt_saved.inverse
        self.screen.cur_row = self._alt_saved.cur_row
        self.screen.cur_col = self._alt_saved.cur_col
        self.screen.flags = self._alt_saved.flags

    # --- смена формы ----------------------------------------------------------

    def resize(self, rows: int, cols: int) -> None:
        """RESIZE (docs/protocol.md 9): смена формы сетки, пересечение сохраняется.

        Скролл-регион и alt-буферы сбрасываются: регион и содержимое
        привязаны к старой форме. Vim и htop после SIGWINCH перерисуются
        целиком, так что потеря альт-буфера незаметна.
        """
        old = self.screen
        new = Screen(rows, cols)
        copy_rows = min(old.rows, rows)
        copy_cols = min(old.cols, cols)
        for r in range(copy_rows):
            new.cells[r * cols : r * cols + copy_cols] = old.cells[
                r * old.cols : r * old.cols + copy_cols
            ]
            new.inverse[r * cols : r * cols + copy_cols] = old.inverse[
                r * old.cols : r * old.cols + copy_cols
            ]
        new.cur_row = min(old.cur_row, rows - 1)
        new.cur_col = min(old.cur_col, cols - 1)
        new.flags = old.flags
        self.screen = new
        self._alt = Screen(rows, cols)
        self._alt_saved = Screen(rows, cols)
        self._top, self._bottom = 0, rows - 1
        self._wrap_pending = False

    # --- сохранение курсора -------------------------------------------------------

    def _save_cursor(self) -> None:
        self._alt.cur_row = self.screen.cur_row
        self._alt.cur_col = self.screen.cur_col

    def _restore_cursor(self) -> None:
        self.screen.cur_row = min(self._alt.cur_row, self.screen.rows - 1)
        self.screen.cur_col = min(self._alt.cur_col, self.screen.cols - 1)
        self._wrap_pending = False


# Состояния парсера.
_GROUND, _ESC, _CSI, _OSC, _OSC_ESC = 0, 1, 2, 3, 4
