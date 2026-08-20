# Subtitle and audio style

## Benchmark-derived preset

The `social-bold` preset encodes the common traits found in the two supplied beauty-product benchmark videos:

- 9:16 canvas with captions centered around 72–76% of frame height.
- Large bold white captions with a dark outline and small shadow.
- Pale-yellow emphasis inside the same line for product benefits, numbers, shades, prices, and offer language.
- Mostly one-line phrases of roughly 6–12 Chinese characters.
- Faster caption replacement than sentence-level subtitles; motion comes from wording changes, gestures, product handling, and occasional overlays rather than mandatory hard cuts.
- A clear product/deal CTA in the final section.

At 1080×1920, start with font size `64`, hook font size `70`, outline `5`, and bottom margin `480`. Scale these values with the canvas. Validate against actual face, product, and platform UI positions before delivery.

## Highlight priority

1. Explicit `[[text]]` markers in `script.txt`.
2. Numbers and units such as `05号色`, `16克`, `99元`, or `8折`.
3. Configured benefit and offer keywords.

Use no more than two highlighted spans per cue by default. Highlight the claim already present in the script; never introduce a new efficacy, price, guarantee, or promotion.

## Timing

The deterministic fallback splits sentences on punctuation, balances long phrases into short cues, and assigns time by visible character count. This is a planning fallback, not speech alignment.

When trustworthy word-level timestamps are available, replace the heuristic cue times while retaining the ASS styles and inline emphasis. Do not claim lip-synced subtitles when heuristic timing is in use.

## Sound design

Use semantic effects, not decorative noise:

- Hook: `pop-soft.wav`, once near the opening product/gesture beat.
- Product/proof transition: `whoosh-soft.wav`, only at selected changes.
- CTA: `ding-soft.wav`, once near the offer or purchase prompt.

The bundled WAV files are deterministically synthesized by `scripts/generate_sfx_assets.py`; they contain no copied commercial audio. Their configured volumes are deliberately low.

## Mix

- Keep speech at the center of the mix.
- Default BGM gain is `0.10`.
- Duck BGM under speech with side-chain compression.
- Normalize the finished mix toward `-15 LUFS` with a true-peak ceiling of `-1.5 dBTP`.
- Do not copy clipped benchmark peaks literally. A benchmark can describe style without defining a safe delivery ceiling.

Check the final soundtrack by listening when possible. Structural QA and loudness numbers cannot detect an annoying, mistimed, or semantically wrong sound effect.
