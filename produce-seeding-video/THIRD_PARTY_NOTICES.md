# Third-party notices

This inventory is informational and is not legal advice. Video OS does not
bundle Python, Node.js, FFmpeg, Chrome, Edge, Playwright, NumPy, ImageIO,
MediaInfo, pymediainfo, or uiautomation binaries.

## Distributed source

- `scripts/vendor/pyJianYingDraft/` is derived from
  [GuanYixuan/pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft).
  Copyright 2024 Gary Guan; Apache License 2.0. The complete retained license is
  `scripts/vendor/pyJianYingDraft-LICENSE`.
- `assets/sfx/*.wav` are deterministic project-generated waveforms. Their
  generator source is `scripts/generate_sfx_assets.py`; they are not third-party
  recordings.

## External runtime dependencies

- [Playwright](https://github.com/microsoft/playwright): Apache-2.0. The exact
  Public Beta dependency is pinned in `package.json` and `package-lock.json`.
- [NumPy](https://github.com/numpy/numpy): BSD-3-Clause and compatible bundled
  component licenses; optional standalone SFX-stem helper only.
- [ImageIO](https://github.com/imageio/imageio): BSD-2-Clause; optional GIF
  metadata support in Jianying draft export.
- [pymediainfo](https://github.com/sbraz/pymediainfo): MIT; optional Jianying
  media metadata. Its separate MediaInfo runtime has its own terms.
- [uiautomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows):
  Apache-2.0; optional Windows Jianying UI controller only.

Video OS is licensed under `AGPL-3.0-or-later`. The entries in this notice do
not alter the separate license terms that apply to third-party components.
