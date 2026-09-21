"""Живой аудиотракт для калибровки: play + capture на AudioDevice.

docs/protocol.md 2. Используется tools/probe.py и tools/ber.py.
Тесты подставляют EchoDevice — без звуковой карты.
"""

from __future__ import annotations

import argparse
import array
import os
import time

from xconn_channel import config
from xconn_channel.audioio import BLOCK_SAMPLES_16K, open_audio
from xconn_channel.demodulator import rms_level
from xconn_channel.devcheck import DeviceError, check_winmm, parse_winmm_index

# Хвост тишины: карта доигрывает буфер, последний тон не обрезается.
DRAIN_MS = 400
# Окно поиска фронта сигнала, 10 мс на 16 кГц.
ONSET_WINDOW = 160


class EchoDevice:
    """Петля в памяти: sink сразу доступен через source. Для тестов."""

    def __init__(self) -> None:
        self._queue: list[array.array] = []

    def sink(self, chunk: array.array) -> None:
        self._queue.append(array.array("h", chunk))

    def source(self) -> array.array | None:
        if not self._queue:
            return None
        return self._queue.pop(0)

    def close(self) -> None:
        self._queue.clear()


def add_device_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--live",
        action="store_true",
        help="проиграть и записать через звуковую карту (петля на разъёмах)",
    )
    parser.add_argument(
        "--backend",
        choices=("winmm", "alsa", "wav"),
        default=None,
        help="обвязка; по умолчанию winmm на Windows, alsa на Linux",
    )
    parser.add_argument("--capture", default=None, help="вход: hw:N,M или номер winmm")
    parser.add_argument("--playback", default=None, help="выход: hw:N,M или номер winmm")


def open_live_device(args: argparse.Namespace):
    """Открыть карту так же, как client/agent."""
    kwargs: dict = {}
    name = args.backend
    if name is None:
        name = "winmm" if os.name == "nt" else "alsa"
    if name == "alsa":
        kwargs["capture_device"] = args.capture or "hw:0,0"
        kwargs["playback_device"] = args.playback or "hw:0,0"
    elif name == "winmm":
        kwargs["in_device"] = parse_winmm_index(args.capture, "capture")
        kwargs["out_device"] = parse_winmm_index(args.playback, "playback")
        check_winmm(kwargs["in_device"], kwargs["out_device"])
    elif name == "wav":
        kwargs["in_path"] = args.capture
        kwargs["out_path"] = args.playback
    return open_audio(name, **kwargs)


def iter_blocks(samples: array.array):
    """Нарезать 16 кГц на блоки устройства, хвост дополнить нулями."""
    i = 0
    n = len(samples)
    while i < n:
        chunk = samples[i : i + BLOCK_SAMPLES_16K]
        if len(chunk) < BLOCK_SAMPLES_16K:
            padded = array.array("h", chunk)
            padded.extend([0] * (BLOCK_SAMPLES_16K - len(chunk)))
            yield padded
        else:
            yield chunk
        i += BLOCK_SAMPLES_16K


def play_and_capture(
    device,
    samples: array.array,
    drain_ms: float = DRAIN_MS,
    clock=time.monotonic,
    sleep=time.sleep,
) -> array.array:
    """Проиграть отсчёты 16 кГц и параллельно снять вход.

    На одной машине это аналоговая петля (выход → вход). На winmm sink
    ждёт конца блока, за это время буферы waveIn успевают заполниться.
    """
    captured = array.array("h")

    def take() -> None:
        rec = device.source()
        if rec:
            captured.extend(rec)

    for chunk in iter_blocks(samples):
        take()
        device.sink(chunk)
        take()
    deadline = clock() + drain_ms / 1000.0
    while clock() < deadline:
        before = len(captured)
        take()
        if len(captured) == before:
            sleep(0.001)
    return captured


def find_onset(samples, window: int = ONSET_WINDOW) -> int:
    """Индекс первого окна, где RMS ≥ четверти пика по записи.

    Сдвигает разбор тонов на задержку карты. Если сигнала нет — 0.
    """
    n = len(samples)
    if n < window:
        return 0
    step = max(1, window // 2)
    peak = 0.0
    levels = []
    for i in range(0, n - window + 1, step):
        level = rms_level(samples[i : i + window])
        levels.append((i, level))
        if level > peak:
            peak = level
    if peak <= 0.0:
        return 0
    thresh = peak * 0.25
    for i, level in levels:
        if level >= thresh:
            return i
    return 0
