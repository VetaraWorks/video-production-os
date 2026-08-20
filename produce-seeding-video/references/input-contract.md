# Input contract

## Required layout

```text
project/
├── script/
│   └── script.txt
├── raw_video/
├── material/
├── reference/
├── perception/
│   └── perception.json
└── config/
    └── config.json
```

`script/script.txt` and at least one video under `raw_video/` or `material/` are required. The other paths are optional.

The default Video OS production flow creates and validates `perception/perception.json` automatically before PLAN. It must follow [perception-contract.md](perception-contract.md), cover current project videos, and carry the current project input signature. Provider failure, missing configuration, invalid data, or stale signatures block at PERCEPTION; they are never silently converted to metadata-only planning. Projects that genuinely do not require material understanding may explicitly disable/relax Perception in project configuration.

Wrap words that must be highlighted with double square brackets:

```text
这支眼线笔[[防水防汗]]，现在买一支还送两支替换芯。
```

The rendered subtitle shows `防水防汗` without brackets and uses the configured highlight color. Numbers, common benefit words, and offer language can also be highlighted automatically.

Supported video extensions: `.mp4`, `.mov`, `.mkv`, `.avi`, `.webm`, `.m4v`.

Supported audio extensions: `.mp3`, `.wav`, `.m4a`, `.aac`, `.flac`, `.ogg`.

## Filename role tags

Use one or more keywords in filenames to improve deterministic selection:

- Hook: `hook`, `开头`, `钩子`
- Talking head: `talking`, `speak`, `host`, `口播`, `真人`, `人物`
- Product: `product`, `item`, `产品`, `商品`
- Detail: `detail`, `closeup`, `macro`, `细节`, `特写`
- Proof/result: `proof`, `result`, `beforeafter`, `效果`, `对比`, `使用`
- CTA: `cta`, `buy`, `order`, `购买`, `下单`, `引导`
- BGM: `bgm`, `music`, `配乐`, `音乐`

Files in `raw_video/` default to `talking`; videos in `material/` default to `product`. Explicit filename tags take precedence.

## Configuration

Create `config/config.json` only for values that differ from `assets/default-config.json`. Configuration is deep-merged, so a small override is sufficient.

Example:

```json
{
  "canvas": {
    "width": 720,
    "height": 1280
  },
  "encoder": {
    "preset": "veryfast"
  },
  "bgm": {
    "enabled": false
  },
  "sound_effects": {
    "enabled": false
  }
}
```

To select a specific BGM, provide a project-relative path:

```json
{
  "bgm": {
    "enabled": true,
    "path": "material/bgm-soft.mp3",
    "volume": 0.12
  }
}
```

To tune the benchmark-derived subtitle preset:

```json
{
  "subtitles": {
    "font_size": 64,
    "hook_font_size": 70,
    "margin_v": 480,
    "highlight_color": "#FFE66D",
    "highlight_keywords": ["防水", "持妆", "买", "送"]
  },
  "audio": {
    "target_lufs": -15,
    "true_peak_db": -1.5
  }
}
```

`margin_v` is measured upward from the bottom of the canvas. The 1080×1920 default of `480` places the baseline near 75% of frame height. Scale it proportionally for smaller canvases.

Sound-effect events name a template segment and a skill-bundled asset:

```json
{
  "sound_effects": {
    "enabled": true,
    "events": [
      {
        "segment": "hook",
        "offset": 0.08,
        "asset": "sfx/pop-soft.wav",
        "volume": 0.22
      },
      {
        "segment": "cta",
        "offset": 0.05,
        "asset": "sfx/ding-soft.wav",
        "volume": 0.18
      }
    ]
  }
}
```

Bundled sound-effect paths must stay under `assets/sfx/`. Use low event volumes; the final audio normalization stage does not make an overused effect tasteful.

## Outputs

The default output directory is `project/output/`:

- `analysis.json`: script, media metadata, role tags, and warnings.
- `edit_plan.json`: executable fixed-template plan.
- `subtitles.ass`: primary styled subtitles with inline highlights.
- `subtitles.srt`: plain compatibility subtitles with the same timing.
- `final.mp4`: rendered vertical video.
- `qa_report.json`: structural metadata, full-decode result, hook/CTA checks, subtitle safe-zone check, and measured loudness/true peak.

Source inputs are never overwritten.

Proxy media and worker state may be written under `preprocess/` and `perception/tasks/`. For large projects, pass a D-drive path to `scripts/prepare_perception.py prepare --work-root` so proxy files do not consume the system drive. Proxy timestamps must remain aligned with their original source files.

## Batch layout

Place independent projects one level below a batch root:

```text
batch-root/
├── project-a/
│   ├── script/script.txt
│   └── raw_video/
└── project-b/
    ├── script/script.txt
    └── material/
```

Default batch execution runs every child project through `python scripts/video_os.py run <child-project-dir> --to FINAL`. Each project writes to its own `output/` directory and retains its Video OS state and logs. `python scripts/run_batch.py <batch-root>` remains available only for legacy callers that require `batch_report.json`; one project failure does not stop later projects, and the process returns a non-zero status if any project failed.
