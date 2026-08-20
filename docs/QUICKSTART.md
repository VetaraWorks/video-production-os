# Quickstart

Prepare this project contract:

```text
project/
├── script/script.txt
├── raw_video/
├── material/          # optional
├── reference/         # optional
└── config/config.json # optional overrides
```

Then run:

```powershell
python produce-seeding-video/scripts/video_os.py doctor
python produce-seeding-video/scripts/video_os.py run C:\path\to\project --to PLAN
python produce-seeding-video/scripts/video_os.py status C:\path\to\project
python produce-seeding-video/scripts/video_os.py run C:\path\to\project --to FINAL
```

The Director automatically runs ANALYZE → PERCEPTION → PLAN → RENDER → QA →
REVIEW. `pass` advances to FINAL. `fix` enters bounded REPAIR → RENDER → QA →
REVIEW with a new video signature. Provider failure or invalid output cannot
become success.

For support, create a redacted report without media or credentials:

```powershell
python produce-seeding-video/scripts/video_os.py report C:\path\to\project
```
