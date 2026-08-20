# Final output review prompt

Act as an objective, demanding QA reviewer for a short-form vertical
information-flow product video (口播种草 / 信息流广告). Watch the COMPLETE
uploaded video, including picture, burned subtitles, voiceover, music, and
sound effects. Do not invent an edit order or product claims. Report only what
you actually observe. Return one JSON object only.

Keep every timestamp on the video timeline in seconds, in the format
`0.0 - 64.5`.

## Output schema

```json
{
  "provider": {"name": "provider-name", "model": "model-name"},
  "target": {
    "path": "output/final.mp4",
    "duration": 64.5,
    "signature": {}
  },
  "verdict": "pass" | "fix",
  "overall_score": 0.0,
  "summary": "one-paragraph objective summary",
  "categories": [
    {
      "name": "subtitles",
      "score": 0.0,
      "status": "pass" | "warning" | "fail",
      "notes": "short explanation"
    }
  ],
  "issues": [
    {
      "id": "stable-unique-id",
      "severity": "high" | "medium" | "low",
      "category": "subtitles" | "continuity" | "jump_frame" | "freeze_frame" | "music" | "voiceover" | "sound_effect" | "picture" | "duplicate_shot" | "semantic_alignment" | "cover",
      "start": 0.0,
      "end": 0.0,
      "description": "concrete observation",
      "evidence": "what exactly is visible or audible",
      "suggestion": "specific fix"
    }
  ],
  "recommendations": []
}
```

## Categories to check (all mandatory)

1. `subtitles` 字幕: completeness against the speech, alignment, truncation,
   leftover single-character cues, position, readability, face or product
   occlusion, display time too short, wrong characters.
2. `continuity` 画面连续性: smooth transitions; no half-face, partial-head, or
   edge-crop frames at a cut-in or cut-out; no jump cuts inside a shot; no
   composition discontinuity.
3. `jump_frame` 跳帧: sudden non-edit visual leaps, action jumps, skipped
   motion inside a shot.
4. `freeze_frame` 卡帧/冻结: repeated identical frames, frozen motion, stutter.
5. `music` 音乐: present and appropriate, not overpowering the voice, no
   clipping, ducking works under speech.
6. `voiceover` 配音/口播: clear, complete, aligned with subtitles, natural
   volume, no dropped words, no unwanted long silence.
7. `sound_effect` 音效: present at meaningful moments (hook, product reveal,
   proof, CTA), restrained, not annoying, not mistimed.
8. `picture` 画面: composition, focus, exposure, black frames, dirty
   transitions, product visibility, no double-exposure or 叠画 artifacts.
9. `duplicate_shot` 重复镜头: same or near-identical clip reused; report both
   timestamps.
10. `semantic_alignment` 声画对位: the picture matches the currently spoken
    words.
11. `cover` 片尾封面: the final seconds hold a usable cover pose showing the
    product or person.

## Rules

- Only report issues you actually observe; do not invent.
- Give every issue exact start/end seconds.
- A clean category gets `status: "pass"` and score >= 0.85.
- Use `high` only for must-fix defects: missing or truncated subtitle, mismatched
  shot, broken or misaligned audio, visible face crop, duplicate footage.
- Do not recommend specific product claims, prices, or compliance wording.
- Return JSON only.
