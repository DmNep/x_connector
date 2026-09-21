"""Тесты формата экрана: сетка, снимки, дельты (docs/protocol.md 6).

Запуск: py -m unittest discover -s tests
"""

from __future__ import annotations

import random
import unittest

from xconn_channel import config, screen
from xconn_channel.screen import Screen, ScreenError


def filled_screen(rows: int, cols: int, fill: bytes, cur=(0, 0)) -> Screen:
    s = Screen(rows, cols)
    for r in range(rows):
        s.set_row(r, fill * cols)
    s.cur_row, s.cur_col = cur
    return s


class TestScreenGrid(unittest.TestCase):
    def test_default_shape(self) -> None:
        s = Screen()
        self.assertEqual((s.rows, s.cols), (config.DEFAULT_ROWS, config.DEFAULT_COLS))
        self.assertEqual(len(s.cells), 24 * 80)
        self.assertEqual(s.row_bytes(0), b" " * 80, "сетка начинается пробелами")

    def test_shape_bounds(self) -> None:
        with self.assertRaises(ValueError):
            Screen(0, 80)
        with self.assertRaises(ValueError):
            Screen(24, 0)
        with self.assertRaises(ValueError):
            Screen(256, 80)
        with self.assertRaises(ValueError):
            Screen(24, 256)

    def test_put_get(self) -> None:
        s = Screen(4, 10)
        s.put(2, 3, ord("X"))
        self.assertEqual(s.get(2, 3), ord("X"))
        self.assertEqual(s.get(0, 0), ord(" "))

    def test_row_access_bounds(self) -> None:
        s = Screen(4, 10)
        with self.assertRaises(ValueError):
            s.row_bytes(4)
        with self.assertRaises(ValueError):
            s.set_row(0, b"short")
        with self.assertRaises(ValueError):
            s.put(0, 10, ord("X"))
        with self.assertRaises(ValueError):
            s.get(0, -1)

    def test_cp437_box_drawing(self) -> None:
        """Псевдографика cp437 — один байт на символ (docs/protocol.md 6)."""
        s = Screen(2, 4)
        s.set_row(0, b"\xda\xbf\xc0\xd9")  # ┌┐└┘
        self.assertEqual(s.text().splitlines()[0], "┌┐└┘")

    def test_changed_rows(self) -> None:
        base = filled_screen(4, 10, b" ")
        other = filled_screen(4, 10, b" ")
        other.set_row(1, b"a" * 10)
        other.set_row(3, b"z" * 10)
        self.assertEqual(base.changed_rows(other), [1, 3])
        self.assertEqual(base.changed_rows(base), [])


class TestSerializeFull(unittest.TestCase):
    def test_roundtrip(self) -> None:
        s = filled_screen(24, 80, b" ", cur=(5, 10))
        s.set_row(0, "root@server:~# ".ljust(80).encode())
        s.set_row(23, "┌─status─┐".ljust(80).encode("cp437"))
        s.flags = screen.FLAG_CURSOR_VISIBLE | screen.FLAG_BEL
        packed = screen.serialize_full(s)
        restored = screen.parse_full(packed)
        self.assertEqual(restored.rows, 24)
        self.assertEqual(restored.cols, 80)
        self.assertEqual((restored.cur_row, restored.cur_col), (5, 10))
        self.assertEqual(restored.flags, s.flags)
        self.assertEqual(restored.cells, s.cells)

    def test_compression_ratio_typical(self) -> None:
        """Типичный экран сжимается в 5-10 раз (docs/protocol.md 6.1)."""
        s = filled_screen(24, 80, b" ")
        s.set_row(0, "Linux 6.8.0 kernel boot ok".ljust(80).encode())
        s.set_row(1, "[  OK  ] Started Network Manager.".ljust(80).encode())
        packed = screen.serialize_full(s)
        self.assertLess(len(packed), 400)
        self.assertGreater(len(packed), 50)

    def test_max_payload_limit_enforced(self) -> None:
        """Несжимаемый экран не влезает — ValueError, сегментации нет."""
        rng = random.Random(9)
        s = Screen(24, 80)
        s.cells[:] = bytes(rng.randrange(256) for _ in range(24 * 80))
        with self.assertRaises(ValueError):
            screen.serialize_full(s)

    def test_random_screen_roundtrip(self) -> None:
        """Случайные, но сжимаемые экраны: roundtrip без потерь.

        Чистый случайный шум zlib не жмёт (1920 байт -> ~1000), и в лимит
        кадра он не попадает — это свойство данных, сегментации снимка в
        протоколе нет. Реальный терминал сжимаем: повторы пробелов и слов.
        """
        rng = random.Random(4)
        for _ in range(20):
            s = Screen(24, 80)
            for row in range(24):
                # Строки из повторяющихся слов — как вывод терминала.
                word = bytes(rng.choice(b"lorem ipsum net ok") for _ in range(rng.randrange(3, 9)))
                s.set_row(row, (word * (80 // max(1, len(word)) + 1))[:80])
            s.cur_row = rng.randrange(24)
            s.cur_col = rng.randrange(80)
            s.flags = rng.randrange(256)
            restored = screen.parse_full(screen.serialize_full(s))
            self.assertEqual(restored.cells, s.cells)
            self.assertEqual((restored.cur_row, restored.cur_col), (s.cur_row, s.cur_col))
            self.assertEqual(restored.flags, s.flags)

    def test_corrupt_zlib_rejected(self) -> None:
        s = filled_screen(4, 10, b"x")
        packed = bytearray(screen.serialize_full(s))
        packed[3] ^= 0xFF
        with self.assertRaises(ScreenError) as ctx:
            screen.parse_full(bytes(packed))
        self.assertEqual(ctx.exception.code, config.NAK_CRC)

    def test_truncated_grid_rejected(self) -> None:
        """Сетка не согласована с rows×cols — NAK_LENGTH."""
        import zlib

        raw = bytes((4, 10, 0, 0, 0)) + b"x" * 39  # 39 вместо 40
        with self.assertRaises(ScreenError) as ctx:
            screen.parse_full(zlib.compress(raw, 6))
        self.assertEqual(ctx.exception.code, config.NAK_LENGTH)

    def test_zero_rows_is_screen_error(self) -> None:
        import zlib

        raw = bytes((0, 10, 0, 0, 0))
        with self.assertRaises(ScreenError) as ctx:
            screen.parse_full(zlib.compress(raw, 6))
        self.assertEqual(ctx.exception.code, config.NAK_LENGTH)
        import zlib

        raw = bytes((4, 10, 9, 0, 0)) + b" " * 40  # cur_row 9 при rows 4
        with self.assertRaises(ScreenError):
            screen.parse_full(zlib.compress(raw, 6))


class TestSerializeDelta(unittest.TestCase):
    def test_roundtrip_one_row(self) -> None:
        base = filled_screen(24, 80, b" ")
        other = filled_screen(24, 80, b" ")
        other.set_row(7, "ip a".ljust(80).encode())
        changed = base.changed_rows(other)
        payload = screen.serialize_delta(17, other, changed)
        applied = screen.parse_delta(payload, 17, base)
        self.assertEqual(applied.cells, other.cells)

    def test_roundtrip_many_rows(self) -> None:
        """8 строк по 80 байт не влезают в MAX_PAYLOAD — берём 3."""
        base = filled_screen(24, 80, b" ")
        other = filled_screen(24, 80, b" ")
        rng = random.Random(1)
        changed = sorted(rng.sample(range(24), 3))
        for row in changed:
            other.set_row(row, bytes(rng.choice(b"lorem ipsum 01") for _ in range(80)))
        payload = screen.serialize_delta(3, other, changed)
        applied = screen.parse_delta(payload, 3, base)
        self.assertEqual(applied.cells, other.cells)
        self.assertLess(len(payload), 400)

    def test_base_seq_mismatch_rejected(self) -> None:
        """base_seq не совпал — NAK_STATE, агент присылает SCREEN_FULL (6.2)."""
        base = filled_screen(4, 10, b" ")
        other = filled_screen(4, 10, b" ")
        other.set_row(1, b"a" * 10)
        payload = screen.serialize_delta(17, other, [1])
        with self.assertRaises(ScreenError) as ctx:
            screen.parse_delta(payload, 16, base)
        self.assertEqual(ctx.exception.code, config.NAK_STATE)

    def test_cursor_and_flags_travel_in_delta(self) -> None:
        """Дельта несёт курсор и флаги, не только строки (6.2 / AGENTS.md 2.7)."""
        base = filled_screen(4, 10, b" ")
        base.cur_row, base.cur_col = 3, 7
        base.flags = screen.FLAG_APP_ACTIVE
        other = filled_screen(4, 10, b" ")
        other.set_row(2, b"z" * 10)
        other.cur_row, other.cur_col = 1, 4
        other.flags = screen.FLAG_BEL
        payload = screen.serialize_delta(0, other, [2])
        applied = screen.parse_delta(payload, 0, base)
        self.assertEqual((applied.cur_row, applied.cur_col), (1, 4))
        self.assertEqual(applied.flags, screen.FLAG_BEL)

    def test_delta_size_limit_enforced(self) -> None:
        """Дельта из слишком многих строк не влезает — ValueError."""
        s = Screen(24, 80)
        changed = list(range(24))
        with self.assertRaises(ValueError):
            screen.serialize_delta(0, s, changed)

    def test_empty_delta(self) -> None:
        base = filled_screen(4, 10, b" ")
        payload = screen.serialize_delta(5, base, [])
        applied = screen.parse_delta(payload, 5, base)
        self.assertEqual(applied.cells, base.cells)

    def test_corrupt_row_zlib_rejected(self) -> None:
        base = filled_screen(4, 10, b" ")
        other = filled_screen(4, 10, b" ")
        other.set_row(1, b"a" * 10)
        payload = bytearray(screen.serialize_delta(0, other, [1]))
        payload[-1] ^= 0xFF
        with self.assertRaises(ScreenError):
            screen.parse_delta(bytes(payload), 0, base)

    def test_row_outside_grid_rejected(self) -> None:
        import zlib

        payload = bytes((0, 1, 0, 0, 0, 9)) + zlib.compress(b"a" * 10, 6)
        base = filled_screen(4, 10, b" ")
        with self.assertRaises(ScreenError) as ctx:
            screen.parse_delta(payload, 0, base)
        self.assertEqual(ctx.exception.code, config.NAK_LENGTH)


class TestSizes(unittest.TestCase):
    """Размеры из docs/protocol.md 4 и 6.2: снимок и строка в лимит кадра."""

    def test_typical_full_fits_240(self) -> None:
        s = filled_screen(24, 80, b" ")
        for r in range(1, 24):
            s.set_row(r, "[  OK  ]".ljust(80).encode())
        self.assertLessEqual(len(screen.serialize_full(s)), config.MAX_PAYLOAD)

    def test_typical_delta_fits_240(self) -> None:
        s = filled_screen(24, 80, b" ")
        changed = [3]
        payload = screen.serialize_delta(1, s, changed)
        self.assertLessEqual(len(payload), config.MAX_PAYLOAD)
        self.assertLess(len(payload), 100)


if __name__ == "__main__":
    unittest.main()
