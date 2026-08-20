# Perception worker prompt

Analyze one uploaded proxy video as an objective observer. Return one JSON object only. Do not recommend an edit order, invent product claims, or describe anything not visible or audible.

Use the original source path, duration, and signature supplied in the task. Keep all timestamps aligned to the proxy's unchanged original timebase.

Output:

```json
{
  "provider": {"name": "provider-name", "model": "model-name"},
  "source": {
    "source": "material/example.mp4",
    "segments": [
      {
        "id": "stable-unique-id",
        "start": 0.0,
        "end": 3.0,
        "safe_start": 0.3,
        "safe_end": 2.7,
        "summary": "objective visual and action description",
        "semantic_tags": ["product", "detail"],
        "subjects": ["person"],
        "objects": ["bottle"],
        "actions": ["shake_bottle"],
        "script_alignment": [
          {"sentence_index": 0, "score": 0.0, "reason": "visible evidence"}
        ],
        "quality": {
          "usable": true,
          "score": 0.0,
          "issues": []
        },
        "confidence": 0.0,
        "visual_fingerprint": "same value for duplicate or near-duplicate shots"
      }
    ]
  }
}
```

Identify actions and commercial visual facts that matter to short-form product videos, including bottle shaking, product reveal, product label visibility, texture, foam, massage, drain, hair strands, before/after state, usage result, CTA gesture, and intentional close-ups.

Exclude camera setup, entering or leaving frame, incomplete faces, partial heads, focus hunting, accidental occlusion, unstable reframing, transition remnants, and action tails from safe ranges. A stable intentional scalp or hair close-up is not a face-crop error. If boundaries are uncertain, reduce confidence or mark the segment unusable instead of claiming frame-level precision.
