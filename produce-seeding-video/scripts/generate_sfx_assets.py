from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path
from typing import Callable


SAMPLE_RATE = 48_000
MAX_INT16 = 32_767


def _write_stereo(
    path: Path,
    duration: float,
    generator: Callable[[float, int], tuple[float, float]],
) -> None:
    frame_count = round(duration * SAMPLE_RATE)
    payload = bytearray()
    for index in range(frame_count):
        time = index / SAMPLE_RATE
        left, right = generator(time, index)
        payload.extend(
            struct.pack(
                "<hh",
                round(max(-1.0, min(1.0, left)) * MAX_INT16),
                round(max(-1.0, min(1.0, right)) * MAX_INT16),
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(payload)


def _pop(time: float, _: int) -> tuple[float, float]:
    duration = 0.18
    progress = min(1.0, time / duration)
    envelope = math.sin(math.pi * progress) ** 1.4 * math.exp(-3.2 * progress)
    frequency = 180 + 420 * progress
    value = 0.78 * envelope * math.sin(2 * math.pi * frequency * time)
    click = 0.28 * math.exp(-75 * time) * math.sin(2 * math.pi * 1650 * time)
    sample = value + click
    return sample * 0.97, sample


def _whoosh_factory() -> Callable[[float, int], tuple[float, float]]:
    random_source = random.Random(20260730)
    state = 0.0

    def _whoosh(time: float, _: int) -> tuple[float, float]:
        nonlocal state
        duration = 0.48
        progress = min(1.0, time / duration)
        raw = random_source.uniform(-1.0, 1.0)
        smoothing = 0.035 + 0.12 * progress
        state += smoothing * (raw - state)
        envelope = math.sin(math.pi * progress) ** 1.7
        tone = math.sin(2 * math.pi * (260 + 920 * progress) * time)
        sample = envelope * (state * 1.9 + tone * 0.12) * 0.58
        pan = 0.32 + 0.36 * progress
        return sample * (1.0 - pan * 0.35), sample * (0.72 + pan * 0.35)

    return _whoosh


def _ding(time: float, _: int) -> tuple[float, float]:
    envelope = math.exp(-5.8 * time)
    fundamental = math.sin(2 * math.pi * 880 * time)
    overtone = 0.42 * math.sin(2 * math.pi * 1320 * time + 0.18)
    shimmer = 0.18 * math.sin(2 * math.pi * 1760 * time)
    sample = 0.66 * envelope * (fundamental + overtone + shimmer)
    return sample, sample * 0.96


def main() -> None:
    skill_root = Path(__file__).resolve().parents[1]
    output_dir = skill_root / "assets" / "sfx"
    outputs = [
        ("pop-soft.wav", 0.18, _pop),
        ("whoosh-soft.wav", 0.48, _whoosh_factory()),
        ("ding-soft.wav", 0.62, _ding),
    ]
    for filename, duration, generator in outputs:
        path = output_dir / filename
        _write_stereo(path, duration, generator)
        print(path)


if __name__ == "__main__":
    main()
