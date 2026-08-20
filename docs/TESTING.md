# Testing

Run targeted tests while developing and one full suite at a batch gate:

```powershell
cd produce-seeding-video
python -m unittest tests.test_system_manager tests.test_providers -v
python -m unittest discover -s tests
```

Acceptance evidence must distinguish mocked scheduling tests from real runtime
smokes. Claims involving video success require real FFmpeg/ffprobe/decode;
Provider claims require an actual configured Provider where feasible. A forged
`final.mp4`, stale Review, invalid Perception, or report containing secrets must
be rejected.

The full Public Beta clean-machine matrix is maintained in
`qa/v7.5-public-beta/final-acceptance.md`.
