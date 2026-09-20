"""Тесты аудио-устройств: ресемплинг и WAV-обвязка (docs/protocol.md 2).

Запуск: py -m unittest discover -s tests

WinmmAudio и AlsaAudio требуют реального железа и здесь не проверяются:
их контракт — sink/source на 16 кГц — совпадает с WavAudio, а корректность
ресемплинга и пути через 48 кГц проверяется на WAV-обвязке.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from xconn_channel import config, framing
from xconn_channel.audioio import WavAudio
from xconn_channel.demodulator import decimate, demodulate_frame
from xconn_channel.modulator import Modulator, upsample
from xconn_channel.transport import AudioTransport


class TestResample(unittest.TestCase):
    def test_upsample_triples_length(self) -> None:
        import array

        samples = array.array("h", [0, 100, -100, 32767, -32768])
        up = upsample(samples, config.DECIMATION)
        self.assertEqual(len(up), len(samples) * config.DECIMATION)

    def test_upsample_interpolates(self) -> None:
        """Линейная интерполяция: между 0 и 300 встают 100 и 200."""
        import array

        up = upsample(array.array("h", [0, 300]), 3)
        self.assertEqual(list(up), [0, 100, 200, 300, 300, 300])

    def test_upsample_bad_factor(self) -> None:
        import array

        with self.assertRaises(ValueError):
            upsample(array.array("h", [0]), 0)

    def test_resample_loop_preserves_frame(self) -> None:
        """16k -> 48k -> 16k: кадр переживает петлю ресемплинга."""
        mod = Modulator(config.BASE)
        raw = framing.build_frame(config.PING, 7)
        sig = (
            mod.silence(200)
            + mod.modulate(framing.to_bits(raw))
            + mod.silence(300)
        )
        back = decimate(upsample(sig))
        self.assertEqual(len(back), len(sig))
        events = demodulate_frame(back, config.BASE)
        frames = [e for e in events if isinstance(e, framing.Frame)]
        self.assertEqual(len(frames), 1)
        self.assertEqual((frames[0].type, frames[0].seq), (config.PING, 7))

    def test_resample_loop_keeps_amplitude(self) -> None:
        """Интерполяция не раздувает пик: клиппинг убивает FSK (2.1)."""
        mod = Modulator(config.BASE)
        sig = mod.modulate(framing.to_bits(framing.build_frame(config.PING, 0)))
        back = decimate(upsample(sig))
        peak = max(abs(v) for v in back)
        self.assertLessEqual(peak, config.PEAK_AMPLITUDE)
        # Ослабление ФНЧ прореживания — не более 3 дБ от исходного пика.
        src_peak = max(abs(v) for v in sig)
        self.assertGreater(peak, src_peak * 0.7)


class TestWavAudio(unittest.TestCase):
    def test_roundtrip_frame_through_wav(self) -> None:
        """Кадр через WAV-файл: транспорт отправляет, транспорт принимает."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "link.wav")

            out = WavAudio(out_path=path)
            ta = AudioTransport(out.sink, lambda: None, config.BASE)
            raw = framing.build_frame(config.CMD, 3, b"ip a\n")
            ta.send(raw)
            out.close()

            inp = WavAudio(in_path=path)
            tb = AudioTransport(lambda c: None, inp.source, config.BASE)
            self.assertEqual(tb.receive(5.0), raw)

    def test_wav_is_device_sample_rate(self) -> None:
        """Файл — на 48 кГц, как реальное устройство (docs/protocol.md 2)."""
        import wave

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.wav")
            out = WavAudio(out_path=path)
            ta = AudioTransport(out.sink, lambda: None, config.BASE)
            ta.send(framing.build_frame(config.PING, 0))
            out.close()
            with wave.open(path, "rb") as wav:
                self.assertEqual(wav.getframerate(), config.DEVICE_SAMPLE_RATE)
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getsampwidth(), 2)

    def test_source_exhausted_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "empty.wav")
            out = WavAudio(out_path=path)
            out.close()
            inp = WavAudio(in_path=path)
            self.assertIsNone(inp.source())

    def test_missing_input_is_silence(self) -> None:
        inp = WavAudio(in_path="nonexistent.wav")
        self.assertIsNone(inp.source())


if __name__ == "__main__":
    unittest.main()
