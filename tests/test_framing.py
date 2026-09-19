"""Тесты кадрирования. Запуск: py -m unittest discover -s tests

Только стандартная библиотека: тесты обязаны идти в комплекте с флешкой и
запускаться на машине без pytest и без сети.
"""

from __future__ import annotations

import random
import unittest

from xconn_channel import config, crc, framing


class TestCrc(unittest.TestCase):
    def test_check_vector(self) -> None:
        self.assertEqual(crc.crc16(b"123456789"), 0x29B1)

    def test_empty(self) -> None:
        self.assertEqual(crc.crc16(b""), 0xFFFF)

    def test_incremental_equals_direct(self) -> None:
        data = bytes(range(200))
        direct = crc.crc16(data)
        acc = crc.CRC16_INIT
        for i in range(0, len(data), 7):
            acc = crc.crc16_update(acc, data[i : i + 7])
        self.assertEqual(acc ^ crc.CRC16_XOROUT, direct)

    def test_detects_single_bit_flip(self) -> None:
        base = bytes(range(120))
        reference = crc.crc16(base)
        for i in range(len(base)):
            for bit in range(8):
                flipped = bytearray(base)
                flipped[i] ^= 1 << bit
                if crc.crc16(flipped) == reference:
                    self.fail(f"CRC не заметила бит {bit} в байте {i}")


class TestBits(unittest.TestCase):
    def test_preamble_is_strict_alternation(self) -> None:
        bits = framing.preamble_bits()
        self.assertEqual(len(bits), config.PREAMBLE_BITS)
        for i, bit in enumerate(bits):
            self.assertEqual(bit, i % 2, f"нарушен период 2 бита на позиции {i}")

    def test_byte_8n1_layout(self) -> None:
        bits = framing.byte_to_bits(0b10000001)
        self.assertEqual(len(bits), 10)
        self.assertEqual(bits[0], 0, "стартовый бит обязан быть 0")
        self.assertEqual(bits[9], 1, "стоповый бит обязан быть 1")
        self.assertEqual(bits[1], 1, "младший бит данных идёт первым")
        self.assertEqual(bits[8], 1)

    def test_byte_roundtrip(self) -> None:
        for value in range(256):
            self.assertEqual(framing.bits_to_byte(framing.byte_to_bits(value)[1:9]), value)


class TestBuildParse(unittest.TestCase):
    def test_roundtrip(self) -> None:
        for payload in (b"", b"A", b"\x7e\x7e\x7e", bytes(range(64)), bytes(240)):
            for frame_type in (config.CMD, config.SCREEN_DELTA, config.ACK):
                raw = framing.build_frame(frame_type, 7, payload)
                parsed = framing.parse_frame(raw)
                self.assertEqual(parsed.type, frame_type)
                self.assertEqual(parsed.seq, 7)
                self.assertEqual(parsed.payload, payload)

    def test_payload_may_contain_sync(self) -> None:
        """Sync внутри payload допустим: длина поля явная, фрейминг не по флагам."""
        raw = framing.build_frame(config.CMD, 1, bytes((config.SYNC_BYTE,) * 10))
        self.assertEqual(len(framing.parse_frame(raw).payload), 10)

    def test_crc_covers_sync_through_payload(self) -> None:
        raw = bytearray(framing.build_frame(config.CMD, 1, b"abc"))
        raw[1] ^= 0x01  # портим тип кадра
        with self.assertRaises(framing.FrameError):
            framing.parse_frame(raw)

    def test_truncated_frame_rejected(self) -> None:
        raw = framing.build_frame(config.CMD, 1, b"hello world")
        with self.assertRaises(framing.FrameError) as ctx:
            framing.parse_frame(raw[:-3])
        self.assertEqual(ctx.exception.code, config.NAK_LENGTH)

    def test_extended_frame_rejected(self) -> None:
        raw = framing.build_frame(config.CMD, 1, b"hello") + b"\x00"
        with self.assertRaises(framing.FrameError) as ctx:
            framing.parse_frame(raw)
        self.assertEqual(ctx.exception.code, config.NAK_LENGTH)

    def test_unknown_type_rejected_as_type_error(self) -> None:
        """Неизвестный тип обязан давать NAK_TYPE, а не CRC и не тихий игнор.

        Иначе сторона с новой прошивкой молчит, и это выглядит как обрыв
        кабеля вместо того, чтобы называться своим именем.
        """
        raw = bytearray(framing.build_frame(config.CMD, 1, b"abc"))
        raw[1] = 0xF7
        raw = self._recrc(raw)
        with self.assertRaises(framing.FrameError) as ctx:
            framing.parse_frame(raw)
        self.assertEqual(ctx.exception.code, config.NAK_TYPE)

    def test_oversize_length_rejected(self) -> None:
        raw = bytearray(framing.build_frame(config.CMD, 1, b"abc"))
        raw[3] = 0xFF
        raw[4] = 0xFF
        with self.assertRaises(framing.FrameError) as ctx:
            framing.parse_frame(raw)
        self.assertEqual(ctx.exception.code, config.NAK_LENGTH)

    def test_build_rejects_oversize_payload(self) -> None:
        with self.assertRaises(ValueError):
            framing.build_frame(config.CMD, 0, bytes(config.MAX_PAYLOAD + 1))

    def test_seq_bounds(self) -> None:
        for bad in (-1, 256):
            with self.assertRaises(ValueError):
                framing.build_frame(config.CMD, bad)

    @staticmethod
    def _recrc(raw: bytearray) -> bytearray:
        value = crc.crc16(bytes(raw[:-2]))
        raw[-2] = (value >> 8) & 0xFF
        raw[-1] = value & 0xFF
        return raw


class TestBitCollector(unittest.TestCase):
    @staticmethod
    def _feed(collector: framing.BitCollector, bits) -> tuple[list, list]:
        """Прогнать биты, вернуть (принятые кадры, ошибки). Ничего не бросает."""
        frames: list = []
        errors: list = []
        for bit in bits:
            result = collector.add_bit(bit)
            if isinstance(result, framing.Frame):
                frames.append(result)
            elif isinstance(result, framing.FrameError):
                errors.append(result)
            elif result is not None:
                raise AssertionError(f"неожиданный тип возврата {type(result)}")
        return frames, errors

    def test_single_frame_from_bits(self) -> None:
        raw = framing.build_frame(config.CMD, 3, b"ip a\n")
        frames, errors = self._feed(framing.BitCollector(), framing.to_bits(raw))
        self.assertEqual(errors, [])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].type, config.CMD)
        self.assertEqual(frames[0].seq, 3)
        self.assertEqual(frames[0].payload, b"ip a\n")

    def test_garbage_never_raises(self) -> None:
        """Мусор обязан возвращаться значением, а не бросаться наружу.

        Разбор живого потока встречает мусор штатно, и исключение на мусоре
        означало бы, что приёмник падает от шума в кабеле.
        """
        rng = random.Random(11)
        for _ in range(200):
            noise = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40)))
            collector = framing.BitCollector()
            self._feed(collector, framing.to_bits(noise, with_preamble=False))

    def test_fake_frame_reports_error_not_accept(self) -> None:
        """Мусор, начавшийся с 0x7E и совпавший по длине, выглядит как кадр.

        Он обязан закончиться FrameError (то есть NAK), а не молчаливым
        принятием неверного payload.
        """
        noise = bytes([0x7E, 0x10, 0x00, 0x00, 0x05, 1, 2, 3, 4, 5, 6, 7])
        frames, errors = self._feed(
            framing.BitCollector(), framing.to_bits(noise, with_preamble=False)
        )
        self.assertEqual(frames, [])
        self.assertEqual(len(errors), 1)

    def test_frames_back_to_back(self) -> None:
        collector = framing.BitCollector()
        frames: list = []
        errors: list = []
        for seq in range(4):
            raw = framing.build_frame(config.CMD, seq, bytes((seq,)) * 5)
            got, errs = self._feed(collector, framing.to_bits(raw))
            frames += got
            errors += errs
        self.assertEqual(errors, [])
        self.assertEqual([f.seq for f in frames], [0, 1, 2, 3])

    def test_bit_flip_never_accepts_wrong_payload(self) -> None:
        """Инверсия бита обязана дать FrameError или ничего — но не приём
        неверного payload молча."""
        rng = random.Random(20260920)
        payload = b"ip route show\n"
        bits = framing.to_bits(framing.build_frame(config.CMD, 9, payload))
        accepted_wrong = 0
        for _ in range(500):
            corrupted = bytearray(bits)
            corrupted[rng.randrange(len(corrupted))] ^= 1
            frames, _ = self._feed(framing.BitCollector(), corrupted)
            for frame in frames:
                if frame.payload != payload:
                    accepted_wrong += 1
        self.assertEqual(accepted_wrong, 0)

    def test_resync_after_garbage_bits(self) -> None:
        """После мусорных битов приёмник обязан восстановиться.

        Здесь проверяется сдвиг окна на один бит при неверном обрамлении.
        Если вместо сдвига выбрасывать окно целиком, фаза теряется и первый
        настоящий кадр после мусора не принимается — на железе это выглядит
        как «канал то работает, то нет».
        """
        collector = framing.BitCollector()
        self._feed(collector, [1, 0, 1, 1, 1, 0, 0] * 5)
        frames, errors = self._feed(collector, framing.to_bits(framing.build_frame(config.PONG, 11)))
        self.assertEqual(errors, [])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].seq, 11)

    def test_resync_after_misaligned_start(self) -> None:
        """Приёмник, стартовавший в середине кадра, обязан догнать фазу."""
        raw = framing.build_frame(config.CMD, 4, b"ss -tunap\n")
        bits = framing.to_bits(raw)
        collector = framing.BitCollector()
        self._feed(collector, bits[:23])
        frames, _ = self._feed(collector, framing.to_bits(framing.build_frame(config.PONG, 42)))
        self.assertEqual([f.seq for f in frames], [42])


class TestFrameTiming(unittest.TestCase):
    def test_frame_bits_match_spec_table(self) -> None:
        """Значения из docs/protocol.md 4. Тест держит документ и код вместе."""
        expected = {0: 110, 1: 120, 39: 500, 40: 510, 200: 2110, 240: 2510}
        for payload_size, bits in expected.items():
            self.assertEqual(config.frame_bits(payload_size), bits, f"N={payload_size}")

    def test_frame_seconds_base(self) -> None:
        self.assertAlmostEqual(config.frame_seconds(40, config.BASE), 0.425, places=3)
        self.assertAlmostEqual(config.frame_seconds(240, config.BASE), 2.092, places=3)

    def test_samples_per_bit(self) -> None:
        self.assertAlmostEqual(config.samples_per_bit(config.PROBE), 53.333, places=2)
        self.assertAlmostEqual(config.samples_per_bit(config.BASE), 13.333, places=2)
        self.assertAlmostEqual(config.samples_per_bit(config.STRETCH), 6.667, places=2)

    def test_t_idle_scales_with_mode(self) -> None:
        self.assertAlmostEqual(config.t_idle_ms(config.BASE), 100.0, places=1)
        self.assertAlmostEqual(config.t_idle_ms(config.PROBE), 400.0, places=1)

    def test_stretch_locked_by_default(self) -> None:
        self.assertFalse(config.STRETCH_ENABLED)
        with self.assertRaises(RuntimeError):
            config.check_mode(config.STRETCH)
        config.check_mode(config.BASE)


class TestHelpers(unittest.TestCase):
    def test_ack_echoes_seq(self) -> None:
        frame = framing.parse_frame(framing.ack(17))
        self.assertEqual(frame.type, config.ACK)
        self.assertEqual(frame.seq, 17)
        self.assertEqual(frame.payload, bytes((17,)))

    def test_nak_carries_reason(self) -> None:
        frame = framing.parse_frame(framing.nak(5, config.NAK_CRC))
        self.assertEqual(frame.payload, bytes((5, config.NAK_CRC)))

    def test_cmd_does_not_append_newline(self) -> None:
        frame = framing.parse_frame(framing.cmd(1, "ls"))
        self.assertEqual(frame.payload, b"ls")

    def test_type_name_for_unknown(self) -> None:
        frame = framing.Frame(type=0xEE, seq=0, payload=b"")
        self.assertIn("UNKNOWN", frame.type_name)


if __name__ == "__main__":
    unittest.main()
