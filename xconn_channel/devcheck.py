"""Проверка звуковых устройств до открытия карты.

Пользователь должен сразу понять: нет входа, нет выхода или неверный
номер. Трассировка winmm «код 2» для этого не годится.

Список устройств читается через waveInGetNumDevs / waveOutGetNumDevs,
без открытия. Открытие по-прежнему в audioio.py.
"""

from __future__ import annotations

import os
import sys


class DeviceError(Exception):
    """Нет входа, нет выхода или номер вне диапазона. str() — текст для stderr."""


# MMSYSERR_BADDEVICEID: устройства нет или номер не существует.
_MMSYSERR_BADDEVICEID = 2


def missing_capture_text(outputs: list[str]) -> str:
    lines = [
        "нет устройства записи (микрофон или линейный вход).",
        "подключите микрофон, гарнитуру или USB-карту.",
        "проверьте: Параметры Windows -> Система -> Звук -> Ввод.",
    ]
    if outputs:
        lines.append("воспроизведение есть:")
        lines.extend(f"  выход {i}: {name}" for i, name in enumerate(outputs))
    else:
        lines.append("устройства воспроизведения тоже нет.")
    return "\n".join(lines)


def missing_playback_text(inputs: list[str]) -> str:
    lines = [
        "нет устройства воспроизведения (наушники или линейный выход).",
        "подключите наушники, колонки или USB-карту.",
        "проверьте: Параметры Windows -> Система -> Звук -> Вывод.",
    ]
    if inputs:
        lines.append("запись есть:")
        lines.extend(f"  вход {i}: {name}" for i, name in enumerate(inputs))
    else:
        lines.append("устройства записи тоже нет.")
    return "\n".join(lines)


def unknown_index_text(kind: str, index: int, names: list[str]) -> str:
    word = "вход" if kind == "in" else "выход"
    lines = [f"{word} {index} не существует."]
    if names:
        lines.append("доступно:")
        lines.extend(f"  {word} {i}: {name}" for i, name in enumerate(names))
    else:
        lines.append(f"список {word}ов пуст.")
    lines.append("запуск: py -m xconn_channel devices")
    return "\n".join(lines)


def explain_oserror(exc: BaseException) -> str:
    """Запасной текст, если open_audio всё же бросил OSError."""
    raw = str(exc)
    if "waveInOpen" in raw:
        extra = ""
        if f"код {_MMSYSERR_BADDEVICEID}" in raw:
            extra = " обычно это «нет микрофона» или неверный --capture."
        return "не удалось открыть вход (запись)." + extra + f"\n({raw})"
    if "waveOutOpen" in raw:
        return (
            "не удалось открыть выход (воспроизведение). "
            "обычно это «нет наушников/колонок» или неверный --playback.\n"
            f"({raw})"
        )
    if "arecord" in raw.lower() or "aplay" in raw.lower():
        return (
            "не удалось открыть ALSA (arecord/aplay).\n"
            "проверьте устройство: arecord -l && aplay -l\n"
            "нужен hw:N,M, не default.\n"
            f"({raw})"
        )
    return f"звуковое устройство не открылось.\n({raw})"


def _winmm():
    import ctypes

    return ctypes.windll.winmm


def _caps_name(raw: bytes) -> str:
    text = raw.split(b"\x00", 1)[0]
    return text.decode("mbcs", errors="replace") or "(без имени)"


def list_winmm() -> tuple[list[str], list[str]]:
    """Имена входов и выходов winmm. Пустые списки — устройств нет."""
    import ctypes
    from ctypes import wintypes

    winmm = _winmm()

    class InCaps(ctypes.Structure):
        _fields_ = [
            ("wMid", wintypes.WORD),
            ("wPid", wintypes.WORD),
            ("vDriverVersion", wintypes.UINT),
            ("szPname", ctypes.c_char * 32),
            ("dwFormats", wintypes.DWORD),
            ("wChannels", wintypes.WORD),
            ("wReserved1", wintypes.WORD),
        ]

    class OutCaps(ctypes.Structure):
        _fields_ = [
            ("wMid", wintypes.WORD),
            ("wPid", wintypes.WORD),
            ("vDriverVersion", wintypes.UINT),
            ("szPname", ctypes.c_char * 32),
            ("dwFormats", wintypes.DWORD),
            ("wChannels", wintypes.WORD),
            ("wReserved1", wintypes.WORD),
            ("dwSupport", wintypes.DWORD),
        ]

    inputs = []
    for i in range(winmm.waveInGetNumDevs()):
        caps = InCaps()
        if winmm.waveInGetDevCapsA(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            inputs.append(_caps_name(caps.szPname))
        else:
            inputs.append(f"(устройство {i})")
    outputs = []
    for i in range(winmm.waveOutGetNumDevs()):
        caps = OutCaps()
        if winmm.waveOutGetDevCapsA(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            outputs.append(_caps_name(caps.szPname))
        else:
            outputs.append(f"(устройство {i})")
    return inputs, outputs


def check_winmm(in_device: int, out_device: int) -> None:
    """Бросает DeviceError, если входа/выхода нет или номер неверный."""
    inputs, outputs = list_winmm()
    if not inputs:
        raise DeviceError(missing_capture_text(outputs))
    if not outputs:
        raise DeviceError(missing_playback_text(inputs))
    if in_device >= len(inputs):
        raise DeviceError(unknown_index_text("in", in_device, inputs))
    if out_device >= len(outputs):
        raise DeviceError(unknown_index_text("out", out_device, outputs))
    # -1 — WAVE_MAPPER, допустим, если список не пуст.


def format_device_list(inputs: list[str], outputs: list[str]) -> str:
    lines = ["запись (вход, --capture):"]
    if inputs:
        lines.extend(f"  {i}: {name}" for i, name in enumerate(inputs))
    else:
        lines.append("  (нет устройств)")
    lines.append("воспроизведение (выход, --playback):")
    if outputs:
        lines.extend(f"  {i}: {name}" for i, name in enumerate(outputs))
    else:
        lines.append("  (нет устройств)")
    lines.append("пример: py -m xconn_channel client --capture 0 --playback 0 --repl")
    return "\n".join(lines)


def report_devices(stream=None) -> int:
    """Напечатать список. Код 2, если нет входа или выхода."""
    out = stream if stream is not None else sys.stdout
    if os.name == "nt":
        inputs, outputs = list_winmm()
        text = format_device_list(inputs, outputs) + "\n"
        _write(out, text)
        if not inputs or not outputs:
            err = sys.stderr if stream is None else out
            if not inputs:
                _write(err, missing_capture_text(outputs) + "\n")
            if not outputs:
                _write(err, missing_playback_text(inputs) + "\n")
            return 2
        return 0
    _write(out, "на Linux: arecord -l && aplay -l\n")
    return 0


def emit(text: str, stream=None) -> None:
    """Напечатать сообщение в кодировке консоли, без UnicodeEncodeError."""
    out = stream if stream is not None else sys.stderr
    if not text.endswith("\n"):
        text += "\n"
    _write(out, text)


def _write(stream, text: str) -> None:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    data = text.encode(encoding, errors="replace")
    buf = getattr(stream, "buffer", None)
    if buf is not None:
        buf.write(data)
        buf.flush()
    else:
        stream.write(text)
