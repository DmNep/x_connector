'''Тесты модема: CPFSK-модулятор, демодулятор, сквозной бит→звук→кадр.

Запуск: py -m unittest discover -s tests

Только стандартная библиотека, как и весь канал (AGENTS.md 3.2, 3.3).

Сквозные тесты гоняют честный путь: to_bits -> Modulator -> Demodulator ->
BitCollector -> Frame, без подмены компонентов. Это единственный способ
поймать рассинхрон тактов и фазы, который по отдельности живёт в каждом
компоненте и не виден в юнит-тестах компонентов.
'''

from __future__ import annotations

import array
import math
import random
import unittest

from xconn_channel import config, framing
from xconn_channel.demodulator import (
    Demodulator,
    EnergyGate,
    PreambleTracker,
    SlideGoertzel,
    decimate,
    demodulate_frame,
)
from xconn_channel.modulator import Modulator


def modulated_frame(
    frame_bytes: bytes, mode: str = config.DEFAULT_MODE, lead_silence_ms: float = 200.0
) -> array.array:
    """Кадр с тишиной спереди и сзади: как на проводе при смене направления.

    Тишина сзади обязательна: сигнал, обрывающийся ровно на последнем бите
    кадра, не даёт детектору выдать решение по последнему биту — окно
    Гёрцеля в бит требует бит отсчётов после границы. GAP между
    направлениями (docs/protocol.md 8.3) эту тишину и обеспечивает.
    """
    mod = Modulator(mode)
    return (
        mod.silence(lead_silence_ms)
        + mod.modulate(framing.to_bits(frame_bytes))
        + mod.silence(config.GAP_MS)
    )


class TestModulator(unittest.TestCase):
    def test_output_length_matches_bit_time(self) -> None:
        mod = Modulator(config.BASE)
        bits = [1, 0] * 50
        out = mod.modulate(bits)
        self.assertEqual(len(out), round(len(bits) * config.SAMPLE_RATE / config.baud_of(config.BASE)))

    def test_amplitude_within_peak(self) -> None:
        '''Пик обязан влезать в PEAK_AMPLITUDE с допуском на синус.

        Ровно 8192*sin() не достигает, но int16-округление и фаза обязаны
        держать пик ниже шкалы — клиппинг порождает гармоники в чужой тон.
        '''
        mod = Modulator(config.BASE)
        rng = random.Random(1)
        bits = [rng.randrange(2) for _ in range(2000)]
        out = mod.modulate(bits)
        self.assertLessEqual(max(abs(v) for v in out), config.PEAK_AMPLITUDE)

    def test_phase_continuous_across_bit_boundary(self) -> None:
        '''Непрерывность фазы: скачок на границе бит запрещён (docs/protocol.md 3).

        Проверяется по разности соседних отсчётов: при непрерывной фазе и
        частоте не выше 2200 Гц соседние отсчёты не могут отличаться больше
        чем на амплитуду * 2 * sin(pi * f / fs), разрыв фазы даёт выброс.
        '''
        mod = Modulator(config.BASE)
        bits = [1, 1, 0, 0, 1, 0, 1, 1, 0, 0] * 20
        out = mod.modulate(bits)
        max_step = 2.0 * config.PEAK_AMPLITUDE * math.sin(
            math.pi * max(config.tones_of(config.BASE)) / config.SAMPLE_RATE
        )
        worst = 0
        for i in range(1, len(out)):
            worst = max(worst, abs(out[i] - out[i - 1]))
        # Допуск на округление int16 и на переходный бит огибающей.
        self.assertLessEqual(worst, max_step + 2.0)

    def test_phase_carried_between_calls(self) -> None:
        '''Фаза не сбрасывается между кадрами: разрыв фазы — всплеск.'''
        mod = Modulator(config.BASE)
        first = mod.modulate([1, 0, 1, 0])
        second = mod.modulate([1, 0, 1, 0])
        # Продолжение фазы: первый отсчёт второго куска не может быть
        # продолжением синуса от нуля при произвольной накопленной фазе.
        # Проверяем косвенно — отсутствие сброса фазы к началу.
        self.assertNotEqual((first[0], second[0]), (first[0], 0))

    def test_silence_length(self) -> None:
        mod = Modulator(config.BASE)
        self.assertEqual(len(mod.silence(120)), round(120 * config.SAMPLE_RATE / 1000.0))

    def test_empty_bits(self) -> None:
        self.assertEqual(len(Modulator(config.BASE).modulate([])), 0)

    def test_probe_mode_tones(self) -> None:
        '''Тон mark по длинной единице обязан попадать в 1200 Гц.'''
        mod = Modulator(config.PROBE)
        out = mod.modulate([1] * 600)  # 2 секунды при 300 бод
        self.assertGreater(len(out), 0)
        # Оценка частоты по переходам через ноль на второй секунде.
        crossings = 0
        start = len(out) // 2
        for i in range(start + 1, len(out)):
            if (out[i - 1] < 0) != (out[i] < 0):
                crossings += 1
        # 2 секунды mark: 2 * 1200 переходов вверх+вниз = 2400 смен знака.
        duration = (len(out) - start) / config.SAMPLE_RATE
        measured_hz = crossings / 2.0 / duration
        self.assertAlmostEqual(measured_hz, config.tones_of(config.PROBE)[0], delta=10.0)


class TestDecimation(unittest.TestCase):
    def test_length(self) -> None:
        out = decimate(array.array("h", bytes(2 * 48000)))
        self.assertEqual(len(out), 16000)

    def test_dc_passthrough(self) -> None:
        out = decimate(array.array("h", [1000] * 300))
        self.assertEqual(set(out), {1000})

    def test_alias_suppression(self) -> None:
        """Среднее по 3 давит зеркальную компоненту выше Никвиста.

        Нуль скользящего среднего по 3 лежит на fs/3 = 16 кГц; 10 кГц
        после выборки каждым третьим зеркалится в 6 кГц — в полосу
        детектора. Среднее обязано давить её до уровня, не мешающего
        тонам: запас фильтра на 10 кГц — около 6 дБ, и это честная
        характеристика фильтра, а не дефект.
        """
        src = array.array(
            "h",
            (
                int(10000 * math.sin(2.0 * math.pi * 10000.0 * i / 48000.0))
                for i in range(4800)
            ),
        )
        out = decimate(src)
        rms = math.sqrt(sum(v * v for v in out) / len(out))
        # 10000 * 10^(-6/20) ≈ 5000, допуск на переходный процесс фильтра.
        self.assertLess(rms, 5500.0)


class TestEnergyGate(unittest.TestCase):
    def test_activates_on_tone(self) -> None:
        gate = EnergyGate(512.0)
        tone = array.array("h", (int(8192 * math.sin(2 * math.pi * 1200 * i / 16000)) for i in range(1600)))
        self.assertTrue(gate.feed(tone))

    def test_stays_off_in_silence(self) -> None:
        gate = EnergyGate(512.0)
        rng = random.Random(2)
        noise = array.array("h", (rng.randrange(-8, 8) for _ in range(3200)))
        self.assertFalse(gate.feed(noise))

    def test_releases_after_signal(self) -> None:
        gate = EnergyGate(512.0)
        tone = array.array("h", (int(8192 * math.sin(2 * math.pi * 1200 * i / 16000)) for i in range(1600)))
        gate.feed(tone)
        self.assertTrue(gate.active)
        gate.feed(array.array("h", bytes(2 * 3200)))
        self.assertFalse(gate.active)


class TestPreambleTracker(unittest.TestCase):
    def test_acquires_on_preamble(self) -> None:
        tracker = PreambleTracker(config.BASE)
        bits = framing.preamble_bits()
        mod = Modulator(config.BASE)
        # Пустая тишина спереди, чтобы история не была пустой.
        samples = mod.silence(50) + mod.modulate(bits)
        self.assertTrue(tracker.feed(samples))

    def test_stays_quiet_on_noise(self) -> None:
        tracker = PreambleTracker(config.BASE)
        rng = random.Random(3)
        noise = array.array("h", (rng.randrange(-2000, 2000) for _ in range(8000)))
        self.assertFalse(tracker.feed(noise))


class TestSlideGoertzel(unittest.TestCase):
    @staticmethod
    def tone(hz: float, count: int) -> array.array:
        return array.array(
            "h", (int(8000 * math.sin(2.0 * math.pi * hz * i / config.SAMPLE_RATE)) for i in range(count))
        )

    def test_mark_tone_dominates_mark_bank(self) -> None:
        window = round(config.samples_per_bit(config.BASE))
        mark_hz, space_hz = config.tones_of(config.BASE)
        tone = self.tone(mark_hz, 4 * window)
        bank_mark = SlideGoertzel(mark_hz, window)
        bank_space = SlideGoertzel(space_hz, window)
        e_mark = e_space = 0.0
        for v in tone:
            e_mark = bank_mark.push(v)
            e_space = bank_space.push(v)
        self.assertGreater(e_mark, 10.0 * e_space)

    def test_space_tone_dominates_space_bank(self) -> None:
        window = round(config.samples_per_bit(config.BASE))
        mark_hz, space_hz = config.tones_of(config.BASE)
        tone = self.tone(space_hz, 4 * window)
        bank_mark = SlideGoertzel(mark_hz, window)
        bank_space = SlideGoertzel(space_hz, window)
        e_mark = e_space = 0.0
        for v in tone:
            e_mark = bank_mark.push(v)
            e_space = bank_space.push(v)
        self.assertGreater(e_space, 10.0 * e_mark)


class TestEndToEnd(unittest.TestCase):
    '''Сквозной путь: кадр -> биты -> звук -> биты -> кадр.'''

    GATE_LEVEL = 512.0

    def _roundtrip(self, frame_bytes: bytes, mode: str = config.BASE) -> tuple[list, list]:
        samples = modulated_frame(frame_bytes, mode)
        return self._split(demodulate_frame(samples, mode, self.GATE_LEVEL))

    @staticmethod
    def _split(results) -> tuple[list, list]:
        frames = [r for r in results if isinstance(r, framing.Frame)]
        errors = [r for r in results if isinstance(r, framing.FrameError)]
        return frames, errors

    def test_single_frame_base(self) -> None:
        raw = framing.build_frame(config.CMD, 7, b"ip a\n")
        frames, errors = self._roundtrip(raw)
        self.assertEqual(errors, [], f"ошибки: {errors}")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].type, config.CMD)
        self.assertEqual(frames[0].seq, 7)
        self.assertEqual(frames[0].payload, b"ip a\n")

    def test_single_frame_probe(self) -> None:
        raw = framing.build_frame(config.PING, 1)
        frames, errors = self._roundtrip(raw, config.PROBE)
        self.assertEqual(errors, [])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].type, config.PING)

    def test_frame_with_noise(self) -> None:
        '''Шум на 20 дБ ниже сигнала не должен рвать кадр.'''
        rng = random.Random(42)
        samples = modulated_frame(framing.build_frame(config.CMD, 3, b"nft list ruleset\n"))
        noisy = array.array("h", (v + rng.randrange(-800, 800) for v in samples))
        frames, errors = self._split(demodulate_frame(noisy, config.BASE, self.GATE_LEVEL))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].payload, b"nft list ruleset\n")

    def test_two_frames_with_gap(self) -> None:
        '''Кадры с GAP между ними: приёмник обязан взять оба.'''
        mod = Modulator(config.BASE)
        stream = mod.silence(200)
        stream += mod.modulate(framing.to_bits(framing.build_frame(config.CMD, 1, b"ls\n")))
        stream += mod.silence(config.GAP_MS)
        stream += mod.modulate(framing.to_bits(framing.build_frame(config.CMD, 2, b"pwd\n")))
        stream += mod.silence(config.GAP_MS)
        frames, errors = self._split(demodulate_frame(stream, config.BASE, self.GATE_LEVEL))
        self.assertEqual(errors, [])
        self.assertEqual([f.seq for f in frames], [1, 2])
        self.assertEqual(frames[0].payload, b"ls\n")
        self.assertEqual(frames[1].payload, b"pwd\n")

    def test_48k_path_through_decimation(self) -> None:
        '''Честный тракт: 16 кГц -> апсемплинг в 48 -> decimate -> приём.

        Апсемплинг линейной интерполяцией — грубее реального кодека, но
        ловит ошибку, если decimate и модулятор разойдутся в шкале времени.
        '''
        samples = modulated_frame(framing.build_frame(config.CMD, 9, b"ip r\n"))
        up = array.array("h")
        for i in range(len(samples) * 3):
            pos = i / 3.0
            lo = samples[int(pos)]
            hi = samples[min(int(pos) + 1, len(samples) - 1)]
            up.append(round(lo + (hi - lo) * (pos - int(pos))))
        down = decimate(up)
        frames, errors = self._split(demodulate_frame(down, config.BASE, self.GATE_LEVEL))
        self.assertEqual(errors, [])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].payload, b"ip r\n")

    def test_receiver_reuse_between_frames(self) -> None:
        '''Один приёмник на поток с несколькими кадрами — как живой тракт.'''
        mod = Modulator(config.BASE)
        dem = Demodulator(config.BASE, self.GATE_LEVEL)
        all_frames: list = []
        all_errors: list = []
        for seq in range(3):
            chunk = mod.silence(config.GAP_MS)
            chunk += mod.modulate(framing.to_bits(framing.build_frame(config.PONG, seq)))
            chunk += mod.silence(config.GAP_MS)
            frames, errors = self._split(self._feed(dem, chunk))
            all_frames += frames
            all_errors += errors
        self.assertEqual(all_errors, [])
        self.assertEqual([f.seq for f in all_frames], [0, 1, 2])

    @staticmethod
    def _feed(dem: Demodulator, chunk: array.array) -> list:
        return dem.feed(chunk)


if __name__ == "__main__":
    unittest.main()
