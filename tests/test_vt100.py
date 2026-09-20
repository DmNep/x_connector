"""Тесты VT100-эмулятора (docs/protocol.md 6.3, AGENTS.md 2.7).

Запуск: py -m unittest discover -s tests

Кормление — кусками на произвольных границах: поток из PTY приходит
разрезанным, и последовательность обязана дособираться, а не теряться.
"""

from __future__ import annotations

import random
import unittest

from xconn_channel import config, vt100
from xconn_channel.screen import FLAG_BEL, FLAG_CURSOR_VISIBLE
from xconn_channel.vt100 import Vt100


def text_of(vt: Vt100) -> list[str]:
    return [
        vt.screen.row_bytes(r).decode(config.SCREEN_CODEPAGE).rstrip()
        for r in range(vt.screen.rows)
    ]


class TestPrintAndControl(unittest.TestCase):
    def test_print_puts_chars(self) -> None:
        vt = Vt100(4, 10)
        vt.feed(b"hi")
        self.assertEqual(text_of(vt)[0], "hi")
        self.assertEqual((vt.screen.cur_row, vt.screen.cur_col), (0, 2))

    def test_cr_lf(self) -> None:
        vt = Vt100(4, 10)
        vt.feed(b"abc\r\ndef")
        self.assertEqual(text_of(vt)[0], "abc")
        self.assertEqual(text_of(vt)[1], "def")
        self.assertEqual((vt.screen.cur_row, vt.screen.cur_col), (1, 3))

    def test_backspace(self) -> None:
        vt = Vt100(4, 10)
        vt.feed(b"abc\x08X")
        self.assertEqual(text_of(vt)[0], "abX")

    def test_tab_stops(self) -> None:
        vt = Vt100(2, 20)
        vt.feed(b"a\tb")
        self.assertEqual(vt.screen.cur_col, 9, "после b на позиции 9 (стоп 8)")
        self.assertEqual(vt.screen.get(0, 8), ord("b"))

    def test_bel_sets_flag(self) -> None:
        vt = Vt100(2, 10)
        vt.feed(b"x\x07")
        self.assertEqual(vt.screen.flags & FLAG_BEL, FLAG_BEL)

    def test_wrap_on_overflow(self) -> None:
        """DECAWM: печать за последней колонкой переносит строку."""
        vt = Vt100(3, 5)
        vt.feed(b"123456")
        self.assertEqual(text_of(vt)[0], "12345")
        self.assertEqual(text_of(vt)[1], "6")
        self.assertEqual((vt.screen.cur_row, vt.screen.cur_col), (1, 1))

    def test_scroll_at_bottom(self) -> None:
        vt = Vt100(3, 5)
        vt.feed(b"one\r\ntwo\r\nthree\r\nfour")
        self.assertEqual(text_of(vt)[0], "two")
        self.assertEqual(text_of(vt)[1], "three")
        self.assertEqual(text_of(vt)[2], "four")

    def test_cp437_box_drawing(self) -> None:
        vt = Vt100(2, 10)
        vt.feed("┌─┐\r\n└─┘".encode("cp437"))
        self.assertEqual(text_of(vt)[0], "┌─┐")
        self.assertEqual(text_of(vt)[1], "└─┘")


class TestCursorMoves(unittest.TestCase):
    def test_cup(self) -> None:
        vt = Vt100(5, 10)
        vt.feed(b"\x1b[3;5H")
        self.assertEqual((vt.screen.cur_row, vt.screen.cur_col), (2, 4))

    def test_cup_defaults_to_home(self) -> None:
        vt = Vt100(5, 10)
        vt.feed(b"abc\x1b[H")
        self.assertEqual((vt.screen.cur_row, vt.screen.cur_col), (0, 0))

    def test_relative_moves(self) -> None:
        vt = Vt100(5, 10)
        vt.feed(b"\x1b[3;3H")  # (2,2)
        vt.feed(b"\x1b[A")  # вверх
        vt.feed(b"\x1b[B")  # вниз
        vt.feed(b"\x1b[C")  # вправо
        vt.feed(b"\x1b[D")  # влево
        self.assertEqual((vt.screen.cur_row, vt.screen.cur_col), (2, 2))
        vt.feed(b"\x1b[2A\x1b[3C")
        self.assertEqual((vt.screen.cur_row, vt.screen.cur_col), (0, 5))

    def test_moves_clamped(self) -> None:
        vt = Vt100(3, 5)
        vt.feed(b"\x1b[99;99H")
        self.assertEqual((vt.screen.cur_row, vt.screen.cur_col), (2, 4))
        vt.feed(b"\x1b[9A\x1b[9D")
        self.assertEqual((vt.screen.cur_row, vt.screen.cur_col), (0, 0))

    def test_save_restore_cursor(self) -> None:
        vt = Vt100(4, 10)
        vt.feed(b"abc\x1b7\x1b[H\x1b8")
        self.assertEqual((vt.screen.cur_row, vt.screen.cur_col), (0, 3))


class TestErase(unittest.TestCase):
    def test_el_to_end(self) -> None:
        vt = Vt100(2, 10)
        vt.feed(b"abcdef\x1b[3G\x1b[K")
        self.assertEqual(text_of(vt)[0], "ab")

    def test_el_to_cursor(self) -> None:
        vt = Vt100(2, 10)
        vt.feed(b"abcdef\x1b[3G\x1b[1K")
        self.assertEqual(text_of(vt)[0], "   def")

    def test_el_whole_line(self) -> None:
        vt = Vt100(2, 10)
        vt.feed(b"abcdef\x1b[2K")
        self.assertEqual(text_of(vt)[0], "")

    def test_ed_below(self) -> None:
        vt = Vt100(4, 10)
        vt.feed(b"a\r\nb\r\nc\r\nd\x1b[2;1H\x1b[J")
        self.assertEqual(text_of(vt)[0], "a")
        self.assertEqual(text_of(vt)[1], "")

    def test_ed_above(self) -> None:
        """ED1: от начала до курсора включительно."""
        vt = Vt100(4, 10)
        vt.feed(b"a\r\nb\r\nc\r\nd\x1b[3;1H\x1b[1J")
        self.assertEqual(text_of(vt)[0], "")
        self.assertEqual(text_of(vt)[2], "")
        self.assertEqual(text_of(vt)[3], "d", "ниже курсора не тронуто")

    def test_ed_all(self) -> None:
        vt = Vt100(4, 10)
        vt.feed(b"a\r\nb\x1b[2J")
        self.assertEqual(text_of(vt), ["", "", "", ""])

    def test_ed_keeps_cursor(self) -> None:
        vt = Vt100(4, 10)
        vt.feed(b"ab\x1b[2J")
        self.assertEqual((vt.screen.cur_row, vt.screen.cur_col), (0, 2))


class TestScrollRegion(unittest.TestCase):
    def test_region_scroll_only_inside(self) -> None:
        """Скролл внутри региона не трогает строки вне него (DECSTBM)."""
        vt = Vt100(5, 10)
        for r, line in enumerate(("one", "two", "three", "four", "five")):
            vt.feed(f"\x1b[{r + 1};1H".encode() + line.encode())
        vt.feed(b"\x1b[2;4r")  # регион строки 2..4 (индексы 1..3)
        vt.feed(b"\x1b[3;1H\n\n")  # скролл региона дважды
        rows = text_of(vt)
        self.assertEqual(rows[0], "one", "вне региона сверху")
        self.assertEqual(rows[1], "three")
        self.assertEqual(rows[2], "four")
        self.assertEqual(rows[3], "")
        self.assertEqual(rows[4], "five", "вне региона снизу")

    def test_il_dl(self) -> None:
        vt = Vt100(4, 10)
        for r, line in enumerate(("a", "b", "c", "d")):
            vt.feed(f"\x1b[{r + 1};1H".encode() + line.encode())
        vt.feed(b"\x1b[2;1H\x1b[L")  # вставить строку на 2-й
        rows = text_of(vt)
        self.assertEqual(rows, ["a", "", "b", "c"])

        vt.feed(b"\x1b[2;1H\x1b[M")  # удалить строку на 2-й
        rows = text_of(vt)
        self.assertEqual(rows, ["a", "b", "c", ""])


class TestModes(unittest.TestCase):
    def test_cursor_visibility(self) -> None:
        vt = Vt100(2, 10)
        self.assertEqual(vt.screen.flags & FLAG_CURSOR_VISIBLE, 0, "по умолчанию скрыт")
        vt.feed(b"\x1b[?25h")
        self.assertEqual(vt.screen.flags & FLAG_CURSOR_VISIBLE, FLAG_CURSOR_VISIBLE)
        vt.feed(b"\x1b[?25l")
        self.assertEqual(vt.screen.flags & FLAG_CURSOR_VISIBLE, 0)

    def test_alt_buffer(self) -> None:
        """?1049: вход сохраняет основной экран, выход восстанавливает."""
        vt = Vt100(3, 10)
        vt.feed(b"main text")
        vt.feed(b"\x1b[?1049h")
        self.assertEqual(text_of(vt)[0], "", "альт-буфер чист")
        vt.feed(b"\x1b[2;2Hvim")
        self.assertEqual(text_of(vt)[1], " vim")
        vt.feed(b"\x1b[?1049l")
        self.assertEqual(text_of(vt)[0], "main text", "основной восстановлен")
        self.assertEqual(text_of(vt)[1], "")

    def test_sgr_parsed_and_ignored(self) -> None:
        """SGR парсится, на сетку не влияет: атрибутов в однобайтовой сетке нет."""
        vt = Vt100(2, 10)
        vt.feed(b"\x1b[1;31mred\x1b[0m plain")
        self.assertEqual(text_of(vt)[0], "red plain")


class TestReplies(unittest.TestCase):
    """Ответы на запросы терминала — без них vim и htop зависают (6.3)."""

    def test_cpr_cursor_position(self) -> None:
        vt = Vt100(5, 10)
        vt.feed(b"ab\x1b[6n")
        self.assertEqual(vt.feed(b""), b"")
        # Ответ приходит на байте 'n' запроса.
        vt2 = Vt100(5, 10)
        reply = vt2.feed(b"ab\x1b[6n")
        self.assertEqual(reply, b"\x1b[1;3R")

    def test_device_attributes(self) -> None:
        vt = Vt100(2, 10)
        self.assertEqual(vt.feed(b"\x1b[c"), b"\x1b[?1;2c")

    def test_window_size_chars(self) -> None:
        vt = Vt100(24, 80)
        self.assertEqual(vt.feed(b"\x1b[18t"), b"\x1b[8;24;80t")

    def test_window_size_pixels(self) -> None:
        vt = Vt100(24, 80)
        self.assertEqual(vt.feed(b"\x1b[14t"), b"\x1b[4;384;640t")


class TestChunkedStream(unittest.TestCase):
    def test_split_escape_sequence(self) -> None:
        """Последовательность, разрезанная между кусками, дособирается."""
        vt = Vt100(3, 10)
        vt.feed(b"\x1b")
        vt.feed(b"[2;3")
        vt.feed(b"H")
        vt.feed(b"X")
        # X напечатан на позиции CUP 2;3 (0-базно (1,2)), курсор после него (1,3).
        self.assertEqual((vt.screen.cur_row, vt.screen.cur_col), (1, 3))
        self.assertEqual(vt.screen.get(1, 2), ord("X"))

    def test_random_split_never_corrupts(self) -> None:
        """Произвольная нарезка потока даёт тот же экран, что и цельный."""
        stream = (
            b"\x1b[2J\x1b[H"
            b"root@srv:~# ip a\r\n"
            b"1: lo: <LOOPBACK>\r\n"
            b"\x1b[?25l\x1b[2;10H\x1b[1K\x1b[7m up \x1b[m\x1b[?25h"
            b"\x1b[6n\x1b[18t"
        )
        whole = Vt100(6, 40)
        whole.feed(stream)
        rng = random.Random(3)
        for _ in range(30):
            chopped = Vt100(6, 40)
            data = stream
            while data:
                cut = rng.randrange(1, len(data) + 1)
                chopped.feed(data[:cut])
                data = data[cut:]
            self.assertEqual(chopped.screen.cells, whole.screen.cells)
            self.assertEqual(
                (chopped.screen.cur_row, chopped.screen.cur_col),
                (whole.screen.cur_row, whole.screen.cur_col),
            )


if __name__ == "__main__":
    unittest.main()
