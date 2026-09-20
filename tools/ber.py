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
from xconn_channel.demodulator import demodulate_frame, gate_level_from_noise, rms_level
from xconn_channel.framing import Frame, FrameError
from xconn_channel.modulator import Modulator

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


def snr_db(signal_rms: float, noise_sigma: float) -> float:
    if noise_sigma <= 0:
        return math.inf
    if signal_rms <= 0:
        return float("-inf")
    return 20.0 * math.log10(signal_rms / noise_sigma)


def measure_train(
    mode: str,
    n_frames: int = DEFAULT_FRAMES,
    payload_len: int = PAYLOAD_BYTES,
    noise_sigma: float = 0.0,
    seed: int = 1,
) -> dict:
    """Один прогон: N кадров, BER по полезным битам payload, FER по кадрам."""
    payloads = [prbs_payload(payload_len, seed + seq) for seq in range(n_frames)]
    samples = modulate_train(mode, payloads)
    noisy = add_noise(samples, noise_sigma, seed)
    lead = round(LEAD_MS * config.SAMPLE_RATE / 1000.0)
    noise_floor = rms_level(noisy[: max(1, lead)])
    gate = gate_level_from_noise(noise_floor) if noise_floor else 512.0
    results = demodulate_frame(noisy, mode, gate)

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

    signal_rms = rms_level(samples[lead:]) if len(samples) > lead else 0.0
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
        "snr_db": snr_db(signal_rms, noise_sigma),
        "gate_level": gate,
    }


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
    payload = text if text.endswith("\n") else text + "\n"
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    data = payload.encode(encoding, errors="replace")
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        buf.write(data)
        buf.flush()
    else:
        sys.stdout.write(payload)


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
        type=int,
        default=DEFAULT_FRAMES,
        help=f"кадров в прогоне (по умолчанию {DEFAULT_FRAMES})",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.selftest:
        rows = run_selftest()
        _write_report(format_report(rows))
        return 0 if selftest_ok(rows) else 1
    modes = (args.mode,) if args.mode else (config.PROBE, config.BASE)
    rows = [
        measure_train(mode, n_frames=args.frames, noise_sigma=args.noise)
        for mode in modes
    ]
    _write_report(format_report(rows))
    return 0 if selftest_ok(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
