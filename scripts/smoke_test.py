from pathlib import Path
import importlib

ROOT = Path(__file__).resolve().parents[1]
paths = [
    ROOT / "Recordings" / "Not Transcribed",
    ROOT / "Recordings" / "Completed",
    ROOT / "Meeting Minutes" / "Markdown",
    ROOT / "Meeting Minutes" / "PDF",
    ROOT / ".localplaud_state",
]
for p in paths:
    p.mkdir(parents=True, exist_ok=True)

modules = ["faster_whisper", "anthropic", "reportlab", "dotenv"]
for m in modules:
    importlib.import_module(m)

print("Smoke test passed")
