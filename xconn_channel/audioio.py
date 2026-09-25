"""Аудио-устройства: ввод-вывод звука для клиента и агента (docs/protocol.md 2).

Транспорт (AudioTransport) живёт на рабочей частоте DSP 16 кГц. Звуковая
карта обязана быть на стандартных 48 кГц, иначе драйвер включит свой
ресемплинг с непредсказуемой фазой (docs/protocol.md 2). Конвертация
16 <-> 48 кГц — задача этого модуля: upsample на выход, decimate на вход.

Устройство открывается одной из трёх обвязок, все только на stdlib
(AGENTS.md 3.2, 3.3 — автономность сервера, установка с флешки без сети):

- WavAudio — файлы WAV, для отладки и тестов без железа;
- WinmmAudio — Windows, waveIn/waveOut через ctypes (ноутбук-клиент);
- AlsaAudio — Linux, aplay/arecord с hw: напрямую, минуя PipeWire/PulseAudio
  (docs/protocol.md 2.2: АРУ и шумоподавление убивают FSK).

Обвязка даёт AudioTransport два каллбэка: sink(chunk16k) и
source() -> chunk16k | None. Вход читается блоками по 20 мс: меньше
джиттер реакции на T_CARRIER, и блок короче T_IDLE (100 мс при base),
поэтому обрыв кадра обнаруживается вовремя (docs/protocol.md 8.3).
"""

from __future__ import annotations

import array
import ctypes
import os
import struct
import subprocess
import time
import wave

from . import config
from .demodulator import decimate
from .modulator import upsample

# Блок ввода-вывода: 20 мс. Короче T_IDLE (100 мс при base) — обрыв кадра
# ловится за один-два блока, и короче T_CARRIER — ответ не ждёт
# дольше блока после прихода.
BLOCK_MS = 20
BLOCK_SAMPLES_16K = round(BLOCK_MS * config.SAMPLE_RATE / 1000)  # 320
BLOCK_SAMPLES_48K = BLOCK_SAMPLES_16K * config.DECIMATION  # 960
BLOCK_BYTES_48K = BLOCK_SAMPLES_48K * 2


def take_pcm_block(pending: bytearray, incoming: bytes, want: int) -> bytes | None:
    """Накопить сырые байты PCM и отдать ровно want, когда накопилось.

    Неблокирующий arecord отдаёт куски короче блока. Транспорт ждёт либо
    полный блок 20 мс, либо None — частичный блок децимировать нельзя:
    сдвиг на нечётное число байт ломает int16.
    """
    if incoming:
        pending.extend(incoming)
    if len(pending) < want:
        return None
    block = bytes(pending[:want])
    del pending[:want]
    return block


class AudioDevice:
    """Интерфейс устройства: sink/source на 16 кГц для AudioTransport."""

    def sink(self, chunk: array.array) -> None:
        raise NotImplementedError

    def source(self) -> array.array | None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self) -> "AudioDevice":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --- WAV-файлы: отладка без железа ------------------------------------------


class WavAudio(AudioDevice):
    """Вход из WAV-файла, выход в WAV-файл, оба на 48 кГц моно.

    Для отладки тракта и сквозных прогонов без звуковой карты: клиент
    пишет свой выход в файл, агент читает его как вход, и наоборот.
    Файлы — на DEVICE_SAMPLE_RATE, как реальное устройство.
    """

    def __init__(self, in_path: str | None = None, out_path: str | None = None) -> None:
        self._in = None
        self._in_buf = array.array("h")
        if in_path and os.path.exists(in_path):
            with wave.open(in_path, "rb") as wav:
                if wav.getframerate() != config.DEVICE_SAMPLE_RATE:
                    raise ValueError(
                        f"{in_path}: {wav.getframerate()} Гц, ожидалось "
                        f"{config.DEVICE_SAMPLE_RATE}"
                    )
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                    raise ValueError(f"{in_path}: нужно моно 16 бит")
                raw = wav.readframes(wav.getnframes())
            self._in_buf = array.array("h")
            self._in_buf.frombytes(raw)
        self._out = None
        if out_path:
            self._out = wave.open(out_path, "wb")
            self._out.setnchannels(1)
            self._out.setsampwidth(2)
            self._out.setframerate(config.DEVICE_SAMPLE_RATE)

    def sink(self, chunk: array.array) -> None:
        if self._out is None:
            return
        self._out.writeframes(upsample(chunk).tobytes())

    def source(self) -> array.array | None:
        if not self._in_buf:
            return None
        take = self._in_buf[:BLOCK_SAMPLES_48K]
        del self._in_buf[:BLOCK_SAMPLES_48K]
        return decimate(take) or None

    def close(self) -> None:
        if self._out is not None:
            self._out.close()
            self._out = None


# --- Windows: waveIn/waveOut через ctypes ------------------------------------


class _WaveFormatEx(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_ulong),
        ("nAvgBytesPerSec", ctypes.c_ulong),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class _WaveHdr(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", ctypes.c_ulong),
        ("dwBytesRecorded", ctypes.c_ulong),
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", ctypes.c_ulong),
        ("dwLoops", ctypes.c_ulong),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    ]


_WAVE_FORMAT_PCM = 1
_WHDR_DONE = 1
_CALLBACK_NULL = 0


def _wave_format() -> _WaveFormatEx:
    fmt = _WaveFormatEx()
    fmt.wFormatTag = _WAVE_FORMAT_PCM
    fmt.nChannels = 1
    fmt.nSamplesPerSec = config.DEVICE_SAMPLE_RATE
    fmt.wBitsPerSample = 16
    fmt.nBlockAlign = 2
    fmt.nAvgBytesPerSec = config.DEVICE_SAMPLE_RATE * 2
    fmt.cbSize = 0
    return fmt


class WinmmAudio(AudioDevice):
    """Windows: вывод waveOut, ввод waveIn, оба 48 кГц моно 16 бит.

    Ввод — очередь из нескольких подготовленных буферов: waveInAddBuffer
    возвращает буфер с флагом WHDR_DONE, когда он заполнен. source()
    забирает готовые буферы без блокировки и возвращает None, если их
    пока нет — AudioTransport сам крутит ожидание по T_CARRIER.

    Улучшения обработки микрофона, АРУ и шумоподавление обязаны быть
    выключены в системе до запуска (docs/protocol.md 2.2): winmm их не
    отключает, это делается в панели звука или реестром.
    """

    def __init__(self, in_device: int = -1, out_device: int = -1, blocks: int = 8) -> None:
        self._winmm = ctypes.windll.winmm
        self._fmt = _wave_format()
        self._blocks = blocks
        self._hdrs: list[tuple[_WaveHdr, ctypes.Array]] = []
        self._wave_in = ctypes.c_void_p()
        self._wave_out = ctypes.c_void_p()

        # Выход: waveOutOpen + волна буферов не нужны, пишем синхронно.
        rc = self._winmm.waveOutOpen(
            ctypes.byref(self._wave_out),
            out_device,
            ctypes.byref(self._fmt),
            0,
            0,
            _CALLBACK_NULL,
        )
        if rc:
            raise OSError(f"waveOutOpen: код {rc}")

        # Вход: waveInOpen + очередь подготовленных буферов.
        rc = self._winmm.waveInOpen(
            ctypes.byref(self._wave_in),
            in_device,
            ctypes.byref(self._fmt),
            0,
            0,
            _CALLBACK_NULL,
        )
        if rc:
            self._winmm.waveOutClose(self._wave_out)
            raise OSError(f"waveInOpen: код {rc}")

        buf_bytes = BLOCK_SAMPLES_48K * 2
        for _ in range(blocks):
            data = (ctypes.c_char * buf_bytes)()
            hdr = _WaveHdr()
            hdr.lpData = ctypes.cast(data, ctypes.c_void_p)
            hdr.dwBufferLength = buf_bytes
            hdr.dwBytesRecorded = 0
            hdr.dwFlags = 0
            hdr.dwLoops = 0
            rc = self._winmm.waveInPrepareHeader(
                self._wave_in, ctypes.byref(hdr), ctypes.sizeof(hdr)
            )
            if rc:
                self.close()
                raise OSError(f"waveInPrepareHeader: код {rc}")
            rc = self._winmm.waveInAddBuffer(
                self._wave_in, ctypes.byref(hdr), ctypes.sizeof(hdr)
            )
            if rc:
                self.close()
                raise OSError(f"waveInAddBuffer: код {rc}")
            self._hdrs.append((hdr, data))
        rc = self._winmm.waveInStart(self._wave_in)
        if rc:
            self.close()
            raise OSError(f"waveInStart: код {rc}")

    def sink(self, chunk: array.array) -> None:
        data48 = upsample(chunk)
        raw = data48.tobytes()
        buf = ctypes.create_string_buffer(raw, len(raw))
        hdr = _WaveHdr()
        hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
        hdr.dwBufferLength = len(raw)
        hdr.dwFlags = 0
        hdr.dwLoops = 0
        rc = self._winmm.waveOutPrepareHeader(
            self._wave_out, ctypes.byref(hdr), ctypes.sizeof(hdr)
        )
        if rc:
            raise OSError(f"waveOutPrepareHeader: код {rc}")
        rc = self._winmm.waveOutWrite(
            self._wave_out, ctypes.byref(hdr), ctypes.sizeof(hdr)
        )
        if rc:
            raise OSError(f"waveOutWrite: код {rc}")
        # Синхронно ждём проигрывания: полудуплекс, после передачи приём.
        # WHDR_DONE ставится драйвером, когда буфер отыгран. Спин без сна
        # съедает ядро и даёт джиттер GAP — короткая пауза достаточна
        # относительно блока 20 мс.
        deadline = time.monotonic() + 5.0
        try:
            while not hdr.dwFlags & _WHDR_DONE:
                if time.monotonic() > deadline:
                    raise OSError("waveOutWrite: таймаут ожидания WHDR_DONE")
                time.sleep(0.001)
        finally:
            self._winmm.waveOutUnprepareHeader(
                self._wave_out, ctypes.byref(hdr), ctypes.sizeof(hdr)
            )

    def source(self) -> array.array | None:
        out = array.array("h")
        for hdr, data in self._hdrs:
            if not hdr.dwFlags & _WHDR_DONE:
                continue
            self._winmm.waveInUnprepareHeader(
                self._wave_in, ctypes.byref(hdr), ctypes.sizeof(hdr)
            )
            recorded = hdr.dwBytesRecorded
            if recorded:
                out.frombytes(data.raw[:recorded])
            hdr.dwBytesRecorded = 0
            hdr.dwFlags = 0
            self._winmm.waveInPrepareHeader(
                self._wave_in, ctypes.byref(hdr), ctypes.sizeof(hdr)
            )
            self._winmm.waveInAddBuffer(
                self._wave_in, ctypes.byref(hdr), ctypes.sizeof(hdr)
            )
        if not out:
            return None
        return decimate(out) or None

    def close(self) -> None:
        if self._wave_in:
            self._winmm.waveInStop(self._wave_in)
            for hdr, _ in self._hdrs:
                self._winmm.waveInUnprepareHeader(
                    self._wave_in, ctypes.byref(hdr), ctypes.sizeof(hdr)
                )
            self._hdrs.clear()
            self._winmm.waveInClose(self._wave_in)
            self._wave_in = ctypes.c_void_p()
        if self._wave_out:
            self._winmm.waveOutReset(self._wave_out)
            self._winmm.waveOutClose(self._wave_out)
            self._wave_out = ctypes.c_void_p()


# --- Linux: ALSA через aplay/arecord ----------------------------------------


def as_plughw(device: str) -> str:
    """hw:N,M → plughw:N,M. ALC897 не открывает моно на чистом hw:."""
    if device.startswith("hw:"):
        return "plughw:" + device[3:]
    return device


def _alsa_exited(proc: subprocess.Popen) -> bool:
    """Реальный aplay.poll() — int или None. Mock в тестах — не int."""
    return isinstance(proc.poll(), int)


def _alsa_play(device: str, channels: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "aplay", "-q",
            "-D", device,
            "-f", "S16_LE", "-r", str(config.DEVICE_SAMPLE_RATE),
            "-c", str(channels), "-t", "raw",
        ],
        stdin=subprocess.PIPE,
    )


def _alsa_record(device: str, channels: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "arecord", "-q",
            "-D", device,
            "-f", "S16_LE", "-r", str(config.DEVICE_SAMPLE_RATE),
            "-c", str(channels), "-t", "raw",
        ],
        stdout=subprocess.PIPE,
    )


def _stop_alsa(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=1.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def downmix_s16(raw: bytes, channels: int) -> bytes:
    """Интерлив S16 → моно: среднее каналов. channels=1 — как есть."""
    if channels <= 1:
        return raw
    samples = array.array("h")
    samples.frombytes(raw)
    out = array.array("h")
    n = len(samples) - (len(samples) % channels)
    for i in range(0, n, channels):
        out.append(int(sum(samples[i : i + channels]) / channels))
    return out.tobytes()


def upmix_s16(mono: bytes, channels: int) -> bytes:
    """Моно S16 → интерлив: каждый отсчёт повторяется в каналы."""
    if channels <= 1:
        return mono
    samples = array.array("h")
    samples.frombytes(mono)
    out = array.array("h")
    for sample in samples:
        out.extend([sample] * channels)
    return out.tobytes()


class AlsaAudio(AudioDevice):
    """Linux: вывод aplay, ввод arecord.

    PipeWire/PulseAudio обходятся (docs/protocol.md 2.2). Чистый hw:
    на ALC897 не открывает моно — пробуем стерео и plughw:.
    """

    def __init__(self, capture_device: str = "hw:0,0", playback_device: str = "hw:0,0") -> None:
        self._aplay = None
        self._arecord = None
        self._channels = 1
        last_error: Exception | None = None
        attempts = (
            (playback_device, capture_device, 1),
            (playback_device, capture_device, 2),
            (as_plughw(playback_device), as_plughw(capture_device), 1),
        )
        for play_dev, cap_dev, channels in attempts:
            aplay = None
            try:
                aplay = _alsa_play(play_dev, channels)
                time.sleep(0.05)
                if _alsa_exited(aplay):
                    last_error = OSError(
                        f"aplay -c {channels} -D {play_dev} сразу вышел"
                    )
                    _stop_alsa(aplay)
                    continue
                arecord = _alsa_record(cap_dev, channels)
                time.sleep(0.05)
                if _alsa_exited(arecord):
                    last_error = OSError(
                        f"arecord -c {channels} -D {cap_dev} сразу вышел"
                    )
                    _stop_alsa(arecord)
                    _stop_alsa(aplay)
                    continue
            except FileNotFoundError:
                _stop_alsa(aplay)
                raise
            except Exception as err:
                _stop_alsa(aplay)
                last_error = err
                continue
            self._aplay = aplay
            self._arecord = arecord
            self._channels = channels
            break
        if self._aplay is None or self._arecord is None:
            if last_error is not None:
                raise last_error
            raise OSError("не удалось открыть aplay/arecord")
        self._pending = bytearray()
        assert self._arecord.stdout is not None
        os.set_blocking(self._arecord.stdout.fileno(), False)

    def _block_bytes(self) -> int:
        return BLOCK_BYTES_48K * self._channels

    def sink(self, chunk: array.array) -> None:
        assert self._aplay is not None and self._aplay.stdin is not None
        mono = upsample(chunk).tobytes()
        self._aplay.stdin.write(upmix_s16(mono, self._channels))
        self._aplay.stdin.flush()

    def source(self) -> array.array | None:
        assert self._arecord is not None and self._arecord.stdout is not None
        want = self._block_bytes()
        try:
            incoming = self._arecord.stdout.read(want) or b""
        except BlockingIOError:
            incoming = b""
        raw = take_pcm_block(self._pending, incoming, want)
        if raw is None:
            return None
        raw = downmix_s16(raw, self._channels)
        block = array.array("h")
        block.frombytes(raw)
        return decimate(block) or None

    def close(self) -> None:
        for proc, stream in (
            (self._aplay, self._aplay.stdin),
            (self._arecord, self._arecord.stdout),
        ):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    proc.kill()


# --- Фабрика -------------------------------------------------------------------


def open_audio(backend: str | None = None, **kwargs) -> AudioDevice:
    """Открыть устройство: backend "winmm", "alsa", "wav" или None — авто.

    Авто — по ОС: Windows -> winmm, Linux -> alsa. "wav" — файлы, для
    отладки без железа. kwargs передаются конструктору обвязки.
    """
    if backend is None:
        backend = "winmm" if os.name == "nt" else "alsa"
    if backend == "wav":
        return WavAudio(**kwargs)
    if backend == "winmm":
        return WinmmAudio(**kwargs)
    if backend == "alsa":
        return AlsaAudio(**kwargs)
    raise ValueError(f"неизвестный backend {backend!r}: wav, winmm, alsa")
