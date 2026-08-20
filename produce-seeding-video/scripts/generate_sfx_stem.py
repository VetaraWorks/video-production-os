#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import wave
from pathlib import Path
from typing import Callable

import numpy as np


SR = 48_000


def env_decay(length: int, tau: float) -> np.ndarray:
    t = np.arange(length) / SR
    return np.exp(-t / tau)


def fade_edges(signal: np.ndarray, fade_ms: float = 6.0) -> np.ndarray:
    out = signal.copy()
    count = min(len(out) // 2, max(1, int(SR * fade_ms / 1000)))
    ramp = np.linspace(0.0, 1.0, count, endpoint=False)
    if out.ndim == 2:
        ramp = ramp[:, None]
    out[:count] *= ramp
    out[-count:] *= ramp[::-1]
    return out


def stereo(signal: np.ndarray, pan: float = 0.0) -> np.ndarray:
    pan = max(-1.0, min(1.0, pan))
    left = math.cos((pan + 1.0) * math.pi / 4)
    right = math.sin((pan + 1.0) * math.pi / 4)
    return np.column_stack((signal * left, signal * right))


def normalize(signal: np.ndarray, peak: float = 0.72) -> np.ndarray:
    current = float(np.max(np.abs(signal))) if signal.size else 0.0
    return signal if current < 1e-9 else signal * (peak / current)


def tone(freq: float, duration: float, tau: float, *, phase: float = 0.0) -> np.ndarray:
    length = int(SR * duration)
    t = np.arange(length) / SR
    return np.sin(2 * np.pi * freq * t + phase) * env_decay(length, tau)


def chirp(start_hz: float, end_hz: float, duration: float, tau: float) -> np.ndarray:
    length = int(SR * duration)
    t = np.arange(length) / SR
    k = (end_hz - start_hz) / max(duration, 1e-6)
    phase = 2 * np.pi * (start_hz * t + 0.5 * k * t * t)
    return np.sin(phase) * env_decay(length, tau)


def smooth_noise(rng: np.random.Generator, duration: float, window: int = 120) -> np.ndarray:
    length = int(SR * duration)
    noise = rng.normal(0.0, 1.0, length + window - 1)
    kernel = np.ones(window) / window
    return np.convolve(noise, kernel, mode="valid")


def pop(rng: np.random.Generator) -> np.ndarray:
    duration = 0.24
    length = int(SR * duration)
    body = 0.78 * chirp(420, 120, duration, 0.075)
    click = rng.normal(0, 1, length) * env_decay(length, 0.012) * 0.22
    return normalize(stereo(fade_edges(body + click), -0.08))


def sparkle(_: np.random.Generator) -> np.ndarray:
    duration = 0.62
    sig = (
        tone(1320, duration, 0.16)
        + 0.72 * tone(1760, duration, 0.22, phase=0.5)
        + 0.45 * tone(2640, duration, 0.28, phase=1.1)
    )
    return normalize(stereo(fade_edges(sig), 0.18), 0.64)


def whoosh(rng: np.random.Generator) -> np.ndarray:
    duration = 0.46
    length = int(SR * duration)
    t = np.arange(length) / SR
    noise = smooth_noise(rng, duration, 18)
    shape = np.sin(np.pi * np.clip(t / duration, 0, 1)) ** 1.6
    sweep = chirp(260, 3100, duration, 0.45) * 0.32
    mono = noise * shape * 1.8 + sweep * shape
    left = fade_edges(mono)
    right = np.roll(left, 130) * 0.92
    return normalize(np.column_stack((left, right)), 0.66)


def warning_hit(rng: np.random.Generator) -> np.ndarray:
    duration = 0.48
    length = int(SR * duration)
    bass = tone(86, duration, 0.16) + 0.45 * tone(172, duration, 0.11)
    crack = rng.normal(0, 1, length) * env_decay(length, 0.018) * 0.3
    return normalize(stereo(fade_edges(bass + crack), 0.0), 0.78)


def glitch(_: np.random.Generator) -> np.ndarray:
    duration = 0.36
    length = int(SR * duration)
    t = np.arange(length) / SR
    carrier = np.sign(np.sin(2 * np.pi * (110 + 180 * t) * t))
    gate = ((np.floor(t * 34) % 2) == 0).astype(float)
    return normalize(stereo(fade_edges(carrier * gate * env_decay(length, 0.2)), -0.15), 0.58)


def reveal(rng: np.random.Generator) -> np.ndarray:
    base = whoosh(rng)
    bell = sparkle(rng)
    length = max(len(base), int(0.18 * SR) + len(bell))
    out = np.zeros((length, 2))
    out[: len(base)] += base * 0.82
    offset = int(0.18 * SR)
    out[offset : offset + len(bell)] += bell * 0.72
    return normalize(np.tanh(out * 1.05), 0.72)


def stamp(rng: np.random.Generator) -> np.ndarray:
    one = warning_hit(rng)[: int(0.16 * SR)]
    length = int(0.58 * SR)
    out = np.zeros((length, 2))
    out[: len(one)] += one * 0.72
    offset = int(0.30 * SR)
    out[offset : offset + len(one)] += one[:, ::-1] * 0.76
    return normalize(out, 0.70)


def ingredient(rng: np.random.Generator) -> np.ndarray:
    p = pop(rng)
    s = sparkle(rng)
    length = max(len(p), int(0.08 * SR) + len(s))
    out = np.zeros((length, 2))
    out[: len(p)] += p * 0.78
    offset = int(0.08 * SR)
    out[offset : offset + len(s)] += s * 0.55
    return normalize(out, 0.70)


def bottle_shake(rng: np.random.Generator) -> np.ndarray:
    duration = 0.95
    length = int(SR * duration)
    out = np.zeros((length, 2))
    for index, at in enumerate((0.02, 0.16, 0.30, 0.45, 0.60, 0.76)):
        burst_len = int(0.11 * SR)
        burst = rng.normal(0, 1, burst_len) * np.hanning(burst_len)
        burst += chirp(950, 340, 0.11, 0.06) * 0.42
        pan = -0.45 if index % 2 == 0 else 0.45
        stereo_burst = stereo(burst, pan)
        offset = int(at * SR)
        out[offset : offset + burst_len] += stereo_burst
    return normalize(out, 0.68)


def creamy_plop(rng: np.random.Generator) -> np.ndarray:
    duration = 0.42
    length = int(SR * duration)
    plop = chirp(310, 72, duration, 0.12)
    soft = smooth_noise(rng, duration, 90) * env_decay(length, 0.08) * 0.7
    return normalize(stereo(fade_edges(plop + soft), 0.05), 0.62)


def foam_fizz(rng: np.random.Generator) -> np.ndarray:
    duration = 1.05
    length = int(SR * duration)
    t = np.arange(length) / SR
    noise = rng.normal(0, 1, length)
    smooth = np.convolve(noise, np.ones(7) / 7, mode="same")
    shape = np.sin(np.pi * np.clip(t / duration, 0, 1)) ** 0.7
    out = stereo(smooth * shape * 0.34, -0.1)
    for index, at in enumerate((0.13, 0.31, 0.52, 0.73, 0.89)):
        bubble = chirp(780 + index * 90, 1550 + index * 110, 0.11, 0.045)
        offset = int(at * SR)
        out[offset : offset + len(bubble)] += stereo(bubble, -0.35 + index * 0.17) * 0.32
    return normalize(fade_edges(out), 0.50)


def massage_ticks(rng: np.random.Generator) -> np.ndarray:
    length = int(SR * 0.78)
    out = np.zeros((length, 2))
    for index, at in enumerate((0.02, 0.24, 0.46)):
        tick = pop(rng)[: int(0.16 * SR)] * 0.56
        offset = int(at * SR)
        out[offset : offset + len(tick)] += tick[:, ::-1] if index % 2 else tick
    return normalize(out, 0.56)


def shimmer(_: np.random.Generator) -> np.ndarray:
    duration = 0.92
    sig = 0.55 * chirp(620, 2450, duration, 0.43)
    sig += tone(1480, duration, 0.28) + 0.62 * tone(2220, duration, 0.38, phase=0.7)
    return normalize(stereo(fade_edges(sig), 0.16), 0.60)


def result_chime(_: np.random.Generator) -> np.ndarray:
    duration = 0.88
    sig = tone(880, duration, 0.30) + 0.72 * tone(1108.73, duration, 0.36) + 0.5 * tone(1320, duration, 0.42)
    return normalize(stereo(fade_edges(sig), 0.12), 0.62)


def cta_cash(rng: np.random.Generator) -> np.ndarray:
    duration = 0.92
    length = int(SR * duration)
    out = np.zeros((length, 2))
    click_len = int(0.12 * SR)
    click = rng.normal(0, 1, click_len) * env_decay(click_len, 0.015)
    out[:click_len] += stereo(click, -0.28) * 0.28
    ding = tone(1760, 0.74, 0.31) + 0.6 * tone(2640, 0.74, 0.42)
    offset = int(0.09 * SR)
    out[offset : offset + len(ding)] += stereo(ding, 0.22)
    return normalize(out, 0.70)


BUILDERS: dict[str, Callable[[np.random.Generator], np.ndarray]] = {
    "pop": pop,
    "sparkle": sparkle,
    "whoosh": whoosh,
    "warning_hit": warning_hit,
    "glitch": glitch,
    "reveal": reveal,
    "stamp": stamp,
    "ingredient": ingredient,
    "bottle_shake": bottle_shake,
    "creamy_plop": creamy_plop,
    "foam_fizz": foam_fizz,
    "massage_ticks": massage_ticks,
    "shimmer": shimmer,
    "result_chime": result_chime,
    "cta_cash": cta_cash,
}


DEFAULT_CUES = [
    {"start": 0.20, "effect": "pop", "gain": 0.95, "label": "开场集合花字"},
    {"start": 1.60, "effect": "glitch", "gain": 0.42, "label": "莫名其妙情绪点"},
    {"start": 4.90, "effect": "sparkle", "gain": 0.72, "label": "蓬松高颅顶"},
    {"start": 7.40, "effect": "whoosh", "gain": 0.55, "label": "上午洗头下午条形码"},
    {"start": 8.933, "effect": "warning_hit", "gain": 0.72, "label": "堵地漏警告"},
    {"start": 10.367, "effect": "reveal", "gain": 0.78, "label": "产品首次揭示"},
    {"start": 17.133, "effect": "shimmer", "gain": 0.42, "label": "护理头皮发根重点"},
    {"start": 23.067, "effect": "stamp", "gain": 0.60, "label": "资质认证与检测报告"},
    {"start": 29.80, "effect": "glitch", "gain": 0.58, "label": "治标不治本"},
    {"start": 32.467, "effect": "ingredient", "gain": 0.72, "label": "1%二硫化硒"},
    {"start": 35.267, "effect": "whoosh", "gain": 0.46, "label": "咖啡因加姜根"},
    {"start": 42.30, "effect": "bottle_shake", "gain": 0.78, "label": "摇晃瓶身动作"},
    {"start": 44.067, "effect": "creamy_plop", "gain": 0.62, "label": "奶昔质地"},
    {"start": 46.70, "effect": "foam_fizz", "gain": 0.56, "label": "揉搓泡沫"},
    {"start": 49.067, "effect": "massage_ticks", "gain": 0.58, "label": "按摩三到五分钟"},
    {"start": 51.20, "effect": "shimmer", "gain": 0.48, "label": "洗后干净清爽"},
    {"start": 54.567, "effect": "result_chime", "gain": 0.60, "label": "掉发明显减少"},
    {"start": 58.767, "effect": "cta_cash", "gain": 0.76, "label": "活动CTA"},
]


def write_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(SR)
        writer.writeframes(pcm.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic copyright-safe SFX and an aligned stem.")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--duration", type=float, default=60.633333)
    parser.add_argument("--stem-name", default="赛逸77-音效增强轨.wav")
    args = parser.parse_args()

    rng = np.random.default_rng(7701)
    output_dir = args.output_dir.expanduser().resolve()
    individual_dir = output_dir / "individual"
    effects: dict[str, np.ndarray] = {}
    for name, builder in BUILDERS.items():
        effects[name] = builder(rng)
        write_wav(individual_dir / f"{name}.wav", effects[name])

    stem = np.zeros((int(round(args.duration * SR)), 2), dtype=np.float64)
    for cue in DEFAULT_CUES:
        effect = effects[str(cue["effect"])] * float(cue["gain"])
        offset = int(round(float(cue["start"]) * SR))
        available = max(0, min(len(effect), len(stem) - offset))
        if available:
            stem[offset : offset + available] += effect[:available]
    stem = np.tanh(stem * 1.16)
    stem = normalize(stem, 0.52)
    stem_path = output_dir / args.stem_name
    write_wav(stem_path, stem)

    report = {
        "schema_version": 1,
        "sample_rate": SR,
        "channels": 2,
        "duration_seconds": args.duration,
        "peak_dbfs": round(20 * math.log10(float(np.max(np.abs(stem)))), 2),
        "stem": str(stem_path),
        "cue_count": len(DEFAULT_CUES),
        "cues": DEFAULT_CUES,
    }
    (output_dir / "赛逸77-音效时间点.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
