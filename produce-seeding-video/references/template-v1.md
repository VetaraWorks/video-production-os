# Fixed 60-second template V1

## Timeline

1. `hook` — 0–3 seconds
   - Show a person or product immediately.
   - Lead with the first meaningful script sentence.
   - Prefer `hook`, `talking`, then `product` footage.

2. `explain` — 3–15 seconds
   - Establish the problem, audience, and main product premise.
   - Prefer talking-head footage with usable source audio.

3. `product` — 15–30 seconds
   - Show the product, use process, details, or close-ups.
   - Prefer `product` and `detail` footage.

4. `proof` — 30–45 seconds
   - Reinforce benefits with result, comparison, or usage footage.
   - Prefer `proof`, then `product` and `detail`.

5. `cta` — 45–60 seconds
   - Deliver the final script sentence and purchasing guidance.
   - Prefer `cta`, `talking`, then `product`.

## V1 constraints

- Keep a single hard-cut transition model. Add transitions only after the base pipeline passes.
- Use deterministic clip selection and trim offsets.
- Permit clip reuse and looping, but emit warnings.
- Keep heuristic subtitle timing separate from any future ASR alignment mode.
- Do not infer product claims from visuals alone.

## Extension path

Add templates as separate configuration resources rather than branching renderer code. Future templates may specialize for beauty, food reviews, live-stream highlights, or knowledge sharing while preserving the same edit-plan schema.
