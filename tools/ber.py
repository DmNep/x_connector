"""Замер BER и FER звукового канала x_connector.

docs/protocol.md 3.2, 11, 12. Эталонная последовательность кадров гоняется
через модулятор и демодулятор. Железо не обязательно: --selftest — чистый
DSP-loopback. На кабеле тот же анализ применяется к записанному тракту,
когда появится живой захват.

Режим stretch здесь не включается: STRETCH_ENABLED остаётся False, пока
замер не сделан на целевом железе (protocol.md 3.2).
"""

from __future__ import annotations

import argparse
import array
import math
import os
import random
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from xconn_channel import config, framing
from xconn_channel.devcheck import DeviceError, emit
from xconn_channel.demodulator import demodulate_frame, gate_level_from_noise, rms_level
from xconn_channel.framing import Frame, FrameError
from xconn_channel.modulator import Modulator

from tools.live import add_device_args, open_live_device, play_and_capture

# Кадр «.CMD с 40 байтами — типичная команда из protocol.md 8.4.
PAYLOAD_BYTES = 40
DEFAULT_FRAMES = 8
LEAD_MS = 200.0
# Шум как в tests/test_modem.py: 20 дБ ниже пика, кадр обязан пройти.
BENIGN_NOISE = 800


def prbs_payload(length: int, seed: int) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(length))


def bit_errors(expected: bytes, got: bytes) -> int:
    """Число несовпавших бит. Короткий got — недостающие байты все ошибочны."""
    err = 0
    n = max(len(expected), len(got))
    for i in range(n):
        a = expected[i] if i < len(expected) else 0
        if i >= len(got):
            err += 8
            continue
        err += (a ^ got[i]).bit_count()
    return err


def add_noise(samples: array.array, sigma: float, seed: int) -> array.array:
    if sigma <= 0:
        return array.array("h", samples)
    rng = random.Random(seed)
    out = array.array("h")
    for value in samples:
        mixed = int(value + rng.gauss(0.0, sigma))
        if mixed > 32767:
            mixed = 32767
        elif mixed < -32768:
            mixed = -32768
        out.append(mixed)
    return out


def modulate_train(
    mode: str,
    payloads: list[bytes],
    lead_ms: float = LEAD_MS,
) -> array.array:
    """Последовательность CMD-кадров с GAP, как на проводе."""
    mod = Modulator(mode)
    stream = mod.silence(lead_ms)
    for seq, payload in enumerate(payloads):
        raw = framing.build_frame(config.CMD, seq, payload)
        stream += mod.modulate(framing.to_bits(raw))
        stream += mod.silence(config.GAP_MS)
    return stream


def snr_db(signal_rms: float, noise_level: float) -> float:
    """SNR по измеренному уровню шума, не по заданной sigma инъекции.

    noise_level обязан быть измерением (RMS тишины на входе), а не
    параметром add_noise(): на живом захвате инъекции нет, sigma всегда
    0, и SNR от неё был бы всегда бесконечен независимо от реального
    шума в линии.
    """
    if noise_level <= 0:
        return math.inf
    if signal_rms <= 0:
        return float("-inf")
    return 20.0 * math.log10(signal_rms / noise_level)


def score_capture(
    mode: str,
    payloads: list[bytes],
    samples: array.array,
    noise_sigma: float = 0.0,
) -> dict:
    """Разобрать уже снятые отсчёты: BER по payload, FER по кадрам."""
    n_frames = len(payloads)
    payload_len = len(payloads[0]) if payloads else 0
    lead = round(LEAD_MS * config.SAMPLE_RATE / 1000.0)
    head = samples[: max(1, min(lead, len(samples)))]
    noise_floor = rms_level(head)
    gate = gate_level_from_noise(noise_floor) if noise_floor else 512.0
    results = demodulate_frame(samples, mode, gate)

    by_seq: dict[int, Frame] = {}
    crc_fail = 0
    for item in results:
        if isinstance(item, FrameError):
            crc_fail += 1
            continue
        if isinstance(item, Frame) and item.type == config.CMD:
            by_seq[item.seq] = item

    payload_bits = n_frames * payload_len * 8
    errors = 0
    ok = 0
    for seq, expected in enumerate(payloads):
        got = by_seq.get(seq)
        if got is None:
            errors += payload_len * 8
            continue
        ok += 1
        errors += bit_errors(expected, got.payload)

    body = samples[lead:] if len(samples) > lead else samples
    signal_rms = rms_level(body)
    fer = 1.0 - (ok / n_frames) if n_frames else 0.0
    ber = errors / payload_bits if payload_bits else 0.0
    return {
        "mode": mode,
        "baud": config.baud_of(mode),
        "frames_sent": n_frames,
        "frames_ok": ok,
        "frames_lost": n_frames - ok,
        "crc_fail": crc_fail,
        "payload_bits": payload_bits,
        "bit_errors": errors,
        "ber": ber,
        "fer": fer,
        "noise_sigma": noise_sigma,
        "snr_db": snr_db(signal_rms, noise_floor),
        "gate_level": gate,
    }


def measure_train(
    mode: str,
    n_frames: int = DEFAULT_FRAMES,
    payload_len: int = PAYLOAD_BYTES,
    noise_sigma: float = 0.0,
    seed: int = 1,
) -> dict:
    """DSP-прогон: N кадров через модулятор, опционально шум."""
    payloads = [prbs_payload(payload_len, seed + seq) for seq in range(n_frames)]
    samples = add_noise(modulate_train(mode, payloads), noise_sigma, seed)
    return score_capture(mode, payloads, samples, noise_sigma)


def measure_live(
    device,
    mode: str,
    n_frames: int = DEFAULT_FRAMES,
    payload_len: int = PAYLOAD_BYTES,
    seed: int = 1,
) -> dict:
    """Проиграть поезд кадров в карту и посчитать BER по снятому входу."""
    payloads = [prbs_payload(payload_len, seed + seq) for seq in range(n_frames)]
    played = modulate_train(mode, payloads)
    captured = play_and_capture(device, played)
    if not captured:
        raise SystemExit("вход пуст: проверьте кабель петли и --capture/--playback")
    return score_capture(mode, payloads, captured, noise_sigma=0.0)


def _fmt_ratio(value: float) -> str:
    if value == 0:
        return "0"
    if value < 1e-4:
        return f"{value:.2e}"
    return f"{value:.6f}"


def _fmt_snr(value: float) -> str:
    if not math.isfinite(value):
        return "inf" if value > 0 else "-inf"
    return f"{value:.1f} dB"


def format_report(rows: list[dict]) -> str:
    lines = ["# BER x_connector", ""]
    for row in rows:
        lines.append(f"## {row['mode']} ({row['baud']} бод)")
        lines.append(f"- кадров: {row['frames_ok']}/{row['frames_sent']} ок")
        lines.append(f"- потеряно: {row['frames_lost']}, CRC fail: {row['crc_fail']}")
        lines.append(f"- FER: {_fmt_ratio(row['fer'])}")
        lines.append(
            f"- BER (биты payload): {_fmt_ratio(row['ber'])} "
            f"({row['bit_errors']}/{row['payload_bits']})"
        )
        lines.append(
            f"- шум sigma: {row['noise_sigma']:.0f}, SNR: {_fmt_snr(row['snr_db'])}"
        )
        lines.append("")
    if not config.STRETCH_ENABLED:
        lines.append(
            "stretch (2400 бод) пропущен: STRETCH_ENABLED=False, пока нет "
            "замера BER на целевом железе (docs/protocol.md 3.2)."
        )
        lines.append("")
    return "\n".join(lines)


def _write_report(text: str) -> None:
    emit(text, sys.stdout)


def run_selftest() -> list[dict]:
    rows = []
    for mode in (config.PROBE, config.BASE):
        rows.append(measure_train(mode, noise_sigma=0.0, seed=1))
    rows.append(
        measure_train(config.BASE, noise_sigma=BENIGN_NOISE, seed=42)
    )
    rows[-1]["mode"] = "base+noise"
    return rows


def selftest_ok(rows: list[dict]) -> bool:
    """Чистый loopback обязан быть без ошибок. Шум 20 дБ ниже пика — тоже."""
    return all(row["ber"] == 0.0 and row["fer"] == 0.0 for row in rows)


def _positive_int(value: str) -> int:
    """argparse type=: --frames <= 0 даёт пустой прогон, не ошибку (12).

    range(0) и range(отрицательное) — пустая последовательность, поэтому
    без этой проверки payloads=[] и ber/fer оба тихо становятся 0.0 —
    selftest_ok() засчитывает это как чистый PASS при нуле реально
    проверенных кадров.
    """
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError(f"--frames обязан быть > 0, получено {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ber.py",
        description="BER/FER loopback x_connector (docs/protocol.md 3.2, 11, 12).",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="DSP loopback probe/base, без звуковой карты",
    )
    parser.add_argument(
        "--frames",
        type=_positive_int,
        default=DEFAULT_FRAMES,
        help=f"кадров в прогоне, > 0 (по умолчанию {DEFAULT_FRAMES})",
    )
    parser.add_argument(
        "--noise",
        type=float,
        default=0.0,
        help="sigma шума в отсчётах int16, 0 — чисто",
    )
    parser.add_argument(
        "--mode",
        choices=(config.PROBE, config.BASE),
        default=None,
        help="один режим; по умолчанию probe и base",
    )
    add_device_args(parser)
    return parser


def _cmd_live(args: argparse.Namespace) -> int:
    modes = (args.mode,) if args.mode else (config.PROBE, config.BASE)
    try:
        device = open_live_device(args)
    except DeviceError as exc:
        emit(str(exc))
        return 2
    try:
        rows = [
            measure_live(device, mode, n_frames=args.frames)
            for mode in modes
        ]
    finally:
        device.close()
    _write_report(format_report(rows))
    # Живой кабель не обязан быть BER=0: критерий selftest_ok только для DSP.
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.selftest:
        rows = run_selftest()
        _write_report(format_report(rows))
        return 0 if selftest_ok(rows) else 1
    if args.live:
        return _cmd_live(args)
    modes = (args.mode,) if args.mode else (config.PROBE, config.BASE)
    rows = [
        measure_train(mode, n_frames=args.frames, noise_sigma=args.noise)
        for mode in modes
    ]
    _write_report(format_report(rows))
    return 0 if selftest_ok(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
