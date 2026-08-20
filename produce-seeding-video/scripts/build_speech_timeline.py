from __future__ import annotations

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


SPECIAL_TOKEN_RE = re.compile(r"^\[_.*_\]$")
PUNCTUATION_RE = re.compile(r"[\s，。！？；：、,.!?;:（）()\-—…]+")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="gb18030")


def _normalize(text: str) -> str:
    return PUNCTUATION_RE.sub("", text).casefold()


def _flatten_asr_chars(payload: dict[str, Any]) -> tuple[str, list[tuple[float, float]]]:
    characters: list[str] = []
    timings: list[tuple[float, float]] = []
    for segment in payload.get("transcription", []):
        for token in segment.get("tokens", []):
            raw = str(token.get("text") or "")
            if not raw or SPECIAL_TOKEN_RE.match(raw):
                continue
            offsets = token.get("offsets") or {}
            start = float(offsets.get("from", 0)) / 1000.0
            end = float(offsets.get("to", offsets.get("from", 0))) / 1000.0
            normalized = _normalize(raw)
            if not normalized:
                continue
            duration = max(0.02, end - start)
            for index, character in enumerate(normalized):
                char_start = start + duration * index / len(normalized)
                char_end = start + duration * (index + 1) / len(normalized)
                characters.append(character)
                timings.append((char_start, char_end))
    if not characters:
        raise ValueError("Whisper transcript contains no usable timed tokens")
    return "".join(characters), timings


def _script_chars(script: str) -> tuple[str, list[int]]:
    characters: list[str] = []
    source_indices: list[int] = []
    for source_index, character in enumerate(script):
        normalized = _normalize(character)
        for clean_character in normalized:
            characters.append(clean_character)
            source_indices.append(source_index)
    if not characters:
        raise ValueError("Script contains no usable characters")
    return "".join(characters), source_indices


def _map_script_to_asr(script_text: str, asr_text: str) -> tuple[list[float], float]:
    matcher = SequenceMatcher(None, script_text, asr_text, autojunk=False)
    anchors: dict[int, int] = {}
    matched = 0
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            anchors[block.a + offset] = block.b + offset
            matched += 1
    if not anchors:
        raise ValueError("Script and ASR transcript have no alignable text")

    mapped: list[float | None] = [None] * len(script_text)
    for script_index, asr_index in anchors.items():
        mapped[script_index] = float(asr_index)

    known = sorted(anchors)
    first_script, last_script = known[0], known[-1]
    first_asr, last_asr = anchors[first_script], anchors[last_script]
    for index in range(0, first_script):
        mapped[index] = max(0.0, first_asr - (first_script - index))
    for left, right in zip(known, known[1:]):
        if right == left + 1:
            continue
        left_asr = float(anchors[left])
        right_asr = float(anchors[right])
        for index in range(left + 1, right):
            ratio = (index - left) / (right - left)
            mapped[index] = left_asr + ratio * (right_asr - left_asr)
    for index in range(last_script + 1, len(mapped)):
        mapped[index] = min(float(len(asr_text) - 1), last_asr + (index - last_script))

    return [float(value) for value in mapped], matched / max(1, len(script_text))


def _phrase_ranges(script: str, maximum: int) -> list[tuple[int, int, str]]:
    ranges: list[tuple[int, int, str]] = []
    cursor = 0
    for match in re.finditer(r"[^，。！？；：、,.!?;:\n]+[，。！？；：、,.!?;:]?", script):
        raw = match.group(0).strip()
        if not raw:
            continue
        clean = raw.strip("，。！？；：、,.!?;: ")
        if not clean:
            continue
        start = match.start() + raw.find(clean)
        chunks = [clean[index : index + maximum] for index in range(0, len(clean), maximum)]
        if len(chunks) >= 2 and len(chunks[-1]) < 3:
            combined = chunks[-2] + chunks[-1]
            left_size = (len(combined) + 1) // 2
            chunks[-2:] = [combined[:left_size], combined[left_size:]]
        local = start
        for chunk in chunks:
            ranges.append((local, local + len(chunk), chunk))
            local += len(chunk)
        cursor = match.end()
    merged: list[tuple[int, int, str]] = []
    for item in ranges:
        if len(item[2]) < 3 and merged and len(merged[-1][2]) + len(item[2]) <= maximum + 2:
            previous = merged.pop()
            merged.append((previous[0], item[1], previous[2] + "、" + item[2]))
        else:
            merged.append(item)
    ranges = merged

    if not ranges and script.strip():
        text = script.strip()
        start = script.find(text)
        ranges.append((start, start + len(text), text))
    return ranges


def build_timeline(script: str, transcript: dict[str, Any], maximum: int) -> dict[str, Any]:
    asr_text, asr_timings = _flatten_asr_chars(transcript)
    clean_script, source_indices = _script_chars(script)
    mapped, confidence = _map_script_to_asr(clean_script, asr_text)

    source_to_clean: dict[int, list[int]] = {}
    for clean_index, source_index in enumerate(source_indices):
        source_to_clean.setdefault(source_index, []).append(clean_index)

    cues: list[dict[str, Any]] = []
    sentence_index = 0
    for start_source, end_source, text in _phrase_ranges(script, maximum):
        clean_indices = [
            index
            for source_index in range(start_source, end_source)
            for index in source_to_clean.get(source_index, [])
        ]
        if not clean_indices:
            continue
        mapped_indices = [
            max(0, min(len(asr_timings) - 1, round(mapped[index])))
            for index in clean_indices
        ]
        start = asr_timings[min(mapped_indices)][0]
        end = asr_timings[max(mapped_indices)][1]
        while sentence_index < script[:start_source].count("\n"):
            sentence_index += 1
        cues.append(
            {
                "index": len(cues) + 1,
                "start": round(start, 3),
                "end": round(max(start + 0.28, end), 3),
                "text": text,
                "manual_highlights": [],
                "sentence_index": sentence_index,
                "role": "hook" if sentence_index == 0 else "cta" if sentence_index >= 6 else "normal",
            }
        )

    previous_start = -1.0
    for cue in cues:
        cue["start"] = round(max(previous_start + 0.04, float(cue["start"])), 3)
        previous_start = float(cue["start"])

    for index, cue in enumerate(cues):
        if index + 1 < len(cues):
            next_start = float(cues[index + 1]["start"])
            latest_end = max(float(cue["start"]) + 0.12, next_start - 0.03)
            cue["end"] = round(
                min(
                    max(float(cue["start"]) + 0.28, float(cue["end"]) + 0.10),
                    latest_end,
                ),
                3,
            )
    speech_end = max(float(cue["end"]) for cue in cues)
    return {
        "schema_version": 1,
        "timing_mode": "whisper-word-aligned-script",
        "alignment_confidence": round(confidence, 4),
        "speech_end": round(speech_end, 3),
        "cues": cues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Align an exact script to Whisper word timestamps")
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--script", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-chars", type=int, default=11)
    args = parser.parse_args()

    transcript = json.loads(_read_text(args.transcript))
    script = _read_text(args.script).strip()
    timeline = build_timeline(script, transcript, max(4, args.max_chars))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), **{key: timeline[key] for key in ("alignment_confidence", "speech_end")}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
