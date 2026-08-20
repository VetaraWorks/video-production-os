# Video perception contract

Use `perception/perception.json` as the stable boundary between video understanding and edit planning. The perception provider reports observable facts and safe source ranges; it must not decide the final edit.

## Required structure

```json
{
  "schema_version": 1,
  "status": "done",
  "input_signature": {
    "algorithm": "video-os-perception-input-v1",
    "digest_sha256": "...",
    "script": {
      "path": "script/script.txt",
      "size_bytes": 123,
      "sha256": "..."
    },
    "sources": [
      {
        "source": "material/IMG_0649.mov",
        "group": "material",
        "duration": 180.2,
        "signature": {"size_bytes": 251900000, "mtime_ns": 1780000000000000000, "sample_sha256": "..."}
      }
    ]
  },
  "provider": {"name": "gemini-web", "model": "selected-in-ui"},
  "sources": [
    {
      "source": "material/IMG_0649.mov",
      "duration": 180.2,
      "signature": {
        "size_bytes": 251900000,
        "mtime_ns": 1780000000000000000,
        "sample_sha256": "..."
      },
      "segments": [
        {
          "id": "img0649-001",
          "start": 12.4,
          "end": 16.8,
          "safe_start": 12.75,
          "safe_end": 16.45,
          "summary": "手持并连续摇晃橙色瓶身，产品正面可见",
          "semantic_tags": ["product", "detail", "shake_bottle"],
          "subjects": ["hand"],
          "objects": ["orange_bottle"],
          "actions": ["shake_bottle"],
          "script_alignment": [
            {"sentence_index": 4, "score": 0.88, "reason": "台词提到使用前摇匀"}
          ],
          "quality": {
            "usable": true,
            "score": 0.92,
            "issues": []
          },
          "confidence": 0.9,
          "visual_fingerprint": "provider-or-local-shot-id"
        }
      ]
    }
  ]
}
```

## Rules

- Keep `source` project-relative and under `raw_video/`, `material/`, or `reference/`.
- Segment `id` is unique across the project. When independent Provider results
  reuse an ID across sources, merge deterministically qualifies the later ID by
  source and records the original value as `provider_segment_id`.
- Keep timestamps on the original media timebase, even when a lower-resolution proxy is analyzed.
- Set `safe_start` and `safe_end` inside the observed segment after excluding setup motion, partial faces, camera handling, focus hunting, and action tails.
- Describe facts in `summary`, `subjects`, `objects`, and `actions`. Do not invent product claims or choose final edit order.
- Use stable lowercase semantic tags. Include role tags such as `hook`, `talking`, `product`, `detail`, `proof`, and `cta` when objectively supported.
- Mark unusable material with `quality.usable=false` and list concrete issues such as `face_crop`, `camera_shake`, `out_of_focus`, `occluded_product`, or `transition_frame`.
- Use `visual_fingerprint` to group the same or near-identical shot. The planner treats repeated fingerprints as duplicate footage.
- The default planner refuses to reuse a visual fingerprint. Set `perception.allow_duplicate_fingerprint=true` only for an explicit creative repeat or when the user accepts insufficient unique footage.
- Keep confidence and quality scores between `0` and `1`.
- Produce one source entry per original video. Never point editing at the proxy file.
- `input_signature` is generated locally and binds the result to the current script, source paths/groups/durations, and source content signatures. A missing or mismatched signature is stale and must be regenerated.
- Every current project video must be represented. Empty sources, partial coverage, incomplete observation fields, or an empty Provider identity fail contract validation.

The pipeline rejects unknown files, path traversal, stale source signatures, invalid durations, unsorted segments, timestamps outside the source, unresolved duplicate IDs, and unsafe ranges shorter than 80 ms.
