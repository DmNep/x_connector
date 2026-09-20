"""Калибровка аудиотракта x_connector.

docs/protocol.md 2.1, 2.2, 11, 12. Запускается до работы канала и после
смены кабелей. Без внешних зависимостей.

Замеры, которые делает этот модуль:

- шум → порог energy gate на GATE_MARGIN_DB выше RMS (раздел 11);
- пик и клиппинг относительно −12 dBFS (2.1);
- АРУ: амплитуда чистого тона не должна плыть;
- АЧХ свипом 300–3400 Гц, «мёртвые» участки;
- завал 2200 Гц относительно 1200 Гц больше 6 дБ — тона смещать вниз.

Железо не обязательно: анализ работает на готовых отсчётах. Живой звук —
опция CLI, `py tools/probe.py`.
"""

from __future__ import annotations

import argparse
import array
import math
import os
import sys

# Пакет канала лежит на уровень выше tools/.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from xconn_channel import config
from xconn_channel.demodulator import gate_level_from_noise, rms_level

from tools.live import add_device_args, find_onset, open_live_device, play_and_capture

SWEEP_START_HZ = 300
SWEEP_STOP_HZ = 3400
SWEEP_STEP_HZ = 100
TONE_MS = 80
FULL_SCALE = 32768.0
# Протокол 12: 2200 Гц завален ниже 1200 Гц больше чем на 6 дБ.
IMBALANCE_LIMIT_DB = 6.0
# АРУ: разброс RMS внутри одного тона. 3 дБ — уже слышимая перекачка.
AGC_WANDER_DB = 3.0
DEAD_ZONE_DB = 12.0  # провал относительно лучшей точки свипа
# Живой прогон: тишина для шума, пауза между тонами, хвост после последнего.
LIVE_SILENCE_MS = 400
LIVE_GAP_MS = 20
LIVE_TAIL_MS = 200


def _fmt_db(value: float) -> str:
    """Число дБ только ASCII: консоль Windows (cp1251) падает на U+2212."""
    if not math.isfinite(value):
        return "-inf" if value < 0 else "inf"
    text = f"{value:.1f}"
    return text.replace("\u2212", "-")


def dbfs(amplitude: float) -> float:
    """Амплитуда шкалы int16 → dBFS. Ноль — −∞."""
    if amplitude <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(amplitude / FULL_SCALE)


def db_ratio(num: float, den: float) -> float:
    if num <= 0.0 or den <= 0.0:
        return float("-inf") if num <= 0.0 else float("inf")
    return 20.0 * math.log10(num / den)


def generate_tone(
    hz: float,
    duration_ms: float,
    sample_rate: int = config.SAMPLE_RATE,
    amplitude: int = config.PEAK_AMPLITUDE,
) -> array.array:
    """Чистый тон пиковой амплитуды, непрерывная фаза внутри вызова."""
    if hz <= 0 or duration_ms <= 0:
        raise ValueError("частота и длительность обязаны быть положительными")
    total = max(1, round(duration_ms * sample_rate / 1000.0))
    step = 2.0 * math.pi * hz / sample_rate
    out = array.array("h", bytes(2 * total))
    phase = 0.0
    for i in range(total):
        out[i] = int(round(amplitude * math.sin(phase)))
        phase += step
        if phase >= 2.0 * math.pi:
            phase -= 2.0 * math.pi
    return out


def peak_abs(samples) -> int:
    if not len(samples):
        return 0
    return max(abs(int(v)) for v in samples)


def is_clipped(samples, limit: int = 32767) -> bool:
    return peak_abs(samples) >= limit


def analyze_level(samples) -> dict:
    """Пик, RMS, dBFS, клиппинг одного фрагмента."""
    peak = peak_abs(samples)
    rms = rms_level(samples)
    return {
        "peak": peak,
        "rms": rms,
        "peak_dbfs": dbfs(peak),
        "rms_dbfs": dbfs(rms),
        "clipped": is_clipped(samples),
    }


def measure_noise(samples) -> dict:
    """Порог gate на GATE_MARGIN_DB выше RMS шума (docs/protocol.md 11)."""
    rms = rms_level(samples)
    gate = gate_level_from_noise(rms) if rms > 0 else 0.0
    return {
        "noise_rms": rms,
        "noise_dbfs": dbfs(rms),
        "gate_level": gate,
        "gate_margin_db": config.GATE_MARGIN_DB,
    }


def agc_wander_db(samples, parts: int = 4) -> float:
    """Разброс RMS внутри фрагмента в дБ. Большой — подозрение на АРУ (2.2)."""
    n = len(samples)
    if n < parts or parts < 2:
        return 0.0
    width = n // parts
    levels = []
    for i in range(parts):
        chunk = samples[i * width : (i + 1) * width]
        rms = rms_level(chunk)
        if rms > 0:
            levels.append(rms)
    if len(levels) < 2:
        return 0.0
    return db_ratio(max(levels), min(levels))


def goertzel_power(samples, hz: float, sample_rate: int = config.SAMPLE_RATE) -> float:
    """Энергия тона Гёрцелем по целому фрагменту."""
    n = len(samples)
    if n < 2 or hz <= 0:
        return 0.0
    k = int(0.5 + n * hz / sample_rate)
    if k <= 0:
        k = 1
    w = 2.0 * math.pi * k / n
    coeff = 2.0 * math.cos(w)
    s0 = s1 = s2 = 0.0
    for x in samples:
        s0 = float(x) + coeff * s1 - s2
        s2 = s1
        s1 = s0
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


def sweep_points(start: int = SWEEP_START_HZ, stop: int = SWEEP_STOP_HZ, step: int = SWEEP_STEP_HZ):
    """Частоты свипа, включая stop, если он кратен шагу."""
    hz = start
    while hz <= stop:
        yield hz
        hz += step
    if (stop - start) % step != 0:
        yield stop


def frequency_response(captures: list[tuple[int, array.array]]) -> list[dict]:
    """АЧХ: для каждой частоты — энергия Гёрцеля и dB к максимуму свипа."""
    rows = []
    powers = []
    for hz, samples in captures:
        power = goertzel_power(samples, hz)
        powers.append(power)
        rows.append({"hz": hz, "power": power, "level": analyze_level(samples)})
    peak_power = max(powers) if powers else 0.0
    for row, power in zip(rows, powers):
        row["db_from_peak"] = db_ratio(power, peak_power) if peak_power > 0 else float("-inf")
        row["dead"] = row["db_from_peak"] <= -DEAD_ZONE_DB
    return rows


def tone_imbalance(samples_1200, samples_2200) -> dict:
    """Завал 2200 относительно 1200. Больше 6 дБ — смещать TONES вниз (12)."""
    p1200 = goertzel_power(samples_1200, 1200)
    p2200 = goertzel_power(samples_2200, 2200)
    delta = db_ratio(p2200, p1200)
    return {
        "db_2200_vs_1200": delta,
        "limit_db": IMBALANCE_LIMIT_DB,
        "shift_tones_down": delta < -IMBALANCE_LIMIT_DB,
    }


def peak_vs_target(samples) -> dict:
    """Сравнение пика с PEAK_AMPLITUDE / −12 dBFS (2.1)."""
    level = analyze_level(samples)
    return {
        **level,
        "target_peak": config.PEAK_AMPLITUDE,
        "target_dbfs": config.PEAK_DBFS,
        "over_target_db": db_ratio(level["peak"], config.PEAK_AMPLITUDE)
        if level["peak"]
        else float("-inf"),
    }


AGC_CHECKLIST = [
    "Windows: выключить улучшения микрофона, АРУ, подавление шума и эха",
    "Linux: захват с hw:, без PipeWire/PulseAudio и module-echo-cancel",
    "Уровни на Linux — amixer, не ползунок микшера",
]


def format_report(
    noise: dict | None = None,
    peak: dict | None = None,
    agc_db: float | None = None,
    response: list[dict] | None = None,
    imbalance: dict | None = None,
) -> str:
    """Текстовый отчёт для приёмки (docs/protocol.md 2.2, 12)."""
    lines = ["# x_connector probe", ""]
    lines.append("## Отключения (проверить руками)")
    for item in AGC_CHECKLIST:
        lines.append(f"- [ ] {item}")
    lines.append("")
    if noise is not None:
        lines.append("## Шум и energy gate")
        lines.append(
            f"- noise RMS {noise['noise_rms']:.1f} ({_fmt_db(noise['noise_dbfs'])} dBFS)"
        )
        lines.append(
            f"- GATE_LEVEL {noise['gate_level']:.1f} "
            f"(+{noise['gate_margin_db']:.0f} дБ к шуму)"
        )
        lines.append("")
    if peak is not None:
        lines.append("## Уровень")
        lines.append(
            f"- пик {peak['peak']} ({_fmt_db(peak['peak_dbfs'])} dBFS), "
            f"цель {peak['target_peak']} ({_fmt_db(peak['target_dbfs'])} dBFS)"
        )
        if peak["clipped"]:
            lines.append("- КЛИППИНГ: гармоники убьют FSK, нужен делитель на mic-входе")
        lines.append("")
    if agc_db is not None:
        lines.append("## АРУ")
        lines.append(f"- разброс RMS тона {_fmt_db(agc_db)} дБ (порог {AGC_WANDER_DB:.0f} дБ)")
        if agc_db >= AGC_WANDER_DB:
            lines.append("- подозрение: АРУ или шумодав включены")
        else:
            lines.append("- амплитуда тона стабильна")
        lines.append("")
    if response is not None:
        lines.append("## АЧХ 300–3400 Гц")
        dead = [row["hz"] for row in response if row["dead"]]
        for row in response:
            mark = " DEAD" if row["dead"] else ""
            lines.append(
                f"- {row['hz']:4d} Гц  {_fmt_db(row['db_from_peak']):>7} дБ к пику{mark}"
            )
        if dead:
            lines.append(f"- мёртвые участки: {', '.join(str(hz) for hz in dead)} Гц")
        else:
            lines.append("- мёртвых участков нет")
        lines.append("")
    if imbalance is not None:
        lines.append("## Тона Bell 202")
        lines.append(
            f"- 2200 Гц к 1200 Гц: {_fmt_db(imbalance['db_2200_vs_1200'])} дБ "
            f"(лимит -{imbalance['limit_db']:.0f} дБ)"
        )
        if imbalance["shift_tones_down"]:
            lines.append(
                "- 2200 Гц завален: сместить TONES вниз и зафиксировать в отчёте"
            )
        else:
            lines.append("- пара 1200/2200 Гц пригодна")
        lines.append("")
    return "\n".join(lines)


def silence(duration_ms: float, sample_rate: int = config.SAMPLE_RATE) -> array.array:
    total = max(0, round(duration_ms * sample_rate / 1000.0))
    return array.array("h", bytes(2 * total))


def build_live_stimulus(
    tone_ms: float = TONE_MS,
    silence_ms: float = LIVE_SILENCE_MS,
    gap_ms: float = LIVE_GAP_MS,
    tail_ms: float = LIVE_TAIL_MS,
) -> dict:
    """Тишина + свип тонов + хвост. Смещения тонов — от начала первого тона."""
    freqs = list(sweep_points())
    gap = silence(gap_ms)
    stream = silence(silence_ms)
    tone_len = 0
    for i, hz in enumerate(freqs):
        tone = generate_tone(hz, tone_ms)
        tone_len = len(tone)
        stream += tone
        if i + 1 < len(freqs):
            stream += gap
    stream += silence(tail_ms)
    return {
        "samples": stream,
        "freqs": freqs,
        "tone_len": tone_len,
        "gap_len": len(gap),
        "silence_len": round(silence_ms * config.SAMPLE_RATE / 1000.0),
    }


def slice_live_capture(captured: array.array, stim: dict) -> tuple[array.array, dict]:
    """Шум до фронта, тона — куски известной длины после onset."""
    onset = find_onset(captured)
    noise = captured[:onset] if onset > 0 else captured[: stim["silence_len"]]
    if not noise:
        noise = array.array("h", bytes(2))
    tones = {}
    pos = onset
    for hz in stim["freqs"]:
        end = pos + stim["tone_len"]
        tones[hz] = captured[pos:end]
        pos = end + stim["gap_len"]
    return noise, tones


def run_live_probe(device) -> str:
    """Проиграть свип, снять петлю, построить тот же отчёт, что --selftest."""
    stim = build_live_stimulus()
    captured = play_and_capture(device, stim["samples"])
    if not captured:
        raise SystemExit("вход пуст: проверьте кабель петли и --capture/--playback")
    noise, tones = slice_live_capture(captured, stim)
    empty = [hz for hz, chunk in tones.items() if not chunk]
    if empty:
        raise SystemExit(
            "запись короче стимула, нет тонов: "
            + ", ".join(str(hz) for hz in empty)
            + " Гц"
        )
    return analyze_loopback_sweep(noise, tones)


def analyze_loopback_sweep(
    noise_samples,
    tone_map: dict[int, array.array],
) -> str:
    """Полный отчёт по заранее снятым отсчётам (тесты и WAV)."""
    noise = measure_noise(noise_samples)
    peak = None
    agc = None
    if 1000 in tone_map:
        peak = peak_vs_target(tone_map[1000])
        agc = agc_wander_db(tone_map[1000])
    elif tone_map:
        first = next(iter(tone_map.values()))
        peak = peak_vs_target(first)
        agc = agc_wander_db(first)
    captures = [(hz, tone_map[hz]) for hz in sorted(tone_map)]
    response = frequency_response(captures) if captures else None
    imb = None
    if 1200 in tone_map and 2200 in tone_map:
        imb = tone_imbalance(tone_map[1200], tone_map[2200])
    return format_report(noise, peak, agc, response, imb)


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


def _cmd_selftest() -> int:
    """Синтетический тракт без железа: тон −12 dBFS, плоская АЧХ."""
    noise = array.array("h", bytes(2 * 1600))
    tones = {hz: generate_tone(hz, TONE_MS) for hz in sweep_points()}
    _write_report(analyze_loopback_sweep(noise, tones))
    return 0


def _cmd_wav(in_path: str) -> int:
    """Разобрать моно WAV 16 кГц или 48 кГц как захват калибровки."""
    import wave

    from xconn_channel.demodulator import decimate

    with wave.open(in_path, "rb") as wav:
        rate = wav.getframerate()
        raw = array.array("h")
        raw.frombytes(wav.readframes(wav.getnframes()))
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit("нужен моно WAV 16 бит")
    if rate == config.DEVICE_SAMPLE_RATE:
        samples = decimate(raw)
    elif rate == config.SAMPLE_RATE:
        samples = raw
    else:
        raise SystemExit(f"частота {rate} Гц, ждут 16000 или 48000")
    # Файл целиком как шум+сигнал: первая десятая — шум, остальное не режем.
    split = max(1, len(samples) // 10)
    noise = samples[:split]
    rest = samples[split:]
    tones = {1000: rest} if rest else {}
    _write_report(analyze_loopback_sweep(noise, tones))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe.py",
        description="Калибровка аудиотракта x_connector (docs/protocol.md 2, 11, 12).",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="синтетический отчёт без звуковой карты",
    )
    parser.add_argument("--wav", help="разобрать записанный моно WAV")
    add_device_args(parser)
    return parser


def _cmd_live(args: argparse.Namespace) -> int:
    device = open_live_device(args)
    try:
        _write_report(run_live_probe(device))
    finally:
        device.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.wav:
        return _cmd_wav(args.wav)
    if args.live:
        return _cmd_live(args)
    return _cmd_selftest()


if __name__ == "__main__":
    sys.exit(main())
