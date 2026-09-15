"""
LocalPlaud - Private meeting transcription and summarisation.
Audio stays local. Only text is sent to Claude.
"""

import os
import sys
import json
import time
import shutil
import re
import datetime
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: load .env before anything else
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Rich + InquirerPy
# ---------------------------------------------------------------------------
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.text import Text
from rich import box as rbox

console = Console()

try:
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice
    _INQUIRER = True
except ImportError:
    _INQUIRER = False

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
ROOT                = Path(__file__).parent
DIR_NOT_TRANSCRIBED = ROOT / "Recordings" / "Not Transcribed"
DIR_COMPLETED       = ROOT / "Recordings" / "Completed"
DIR_MARKDOWN        = ROOT / "Meeting Minutes" / "Markdown"
DIR_PDF             = ROOT / "Meeting Minutes" / "PDF"
DIR_REVIEW          = ROOT / "Meeting Minutes" / "Review"
DIR_TRANSCRIPTS     = ROOT / "Meeting Minutes" / "Transcripts"
DIR_STATE           = ROOT / ".localplaud_state"
FILE_CONTEXT        = ROOT / "context.md"
FILE_LOG            = ROOT / "processing_log.txt"
FILE_ERROR_LOG      = ROOT / "error_log.jsonl"

AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".ogg", ".flac", ".aac", ".wma", ".webm"}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HF_TOKEN          = os.environ.get("HF_TOKEN", "")
WHISPER_MODEL     = os.environ.get("WHISPER_MODEL", "turbo")
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

HAIKU_COST_IN   = 0.80  / 1_000_000
HAIKU_COST_OUT  = 4.00  / 1_000_000
SONNET_COST_IN  = 3.00  / 1_000_000
SONNET_COST_OUT = 15.00 / 1_000_000

SPEAKER_COLORS = ["cyan", "yellow", "magenta", "green", "blue", "red"]

# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def print_ok(msg: str):
    console.print(f"  [green]✓[/green]  {msg}")

def print_err(msg: str):
    console.print(f"  [bold red]✗[/bold red]  {msg}")

def print_warn(msg: str):
    console.print(f"  [yellow]⚠[/yellow]  {msg}")

def print_info(msg: str):
    console.print(f"  [dim]{msg}[/dim]")

def print_section(title: str, step: int = 0, total: int = 0):
    console.print()
    if step and total:
        console.rule(f"[bold cyan][ {step:02d} / {total:02d} ]  {title}[/bold cyan]", style="dim blue")
    else:
        console.rule(f"[bold cyan]{title}[/bold cyan]", style="dim blue")
    console.print()

class Spinner:
    def __init__(self, message: str):
        self._ctx = console.status(f"[cyan]{message}[/cyan]", spinner="dots")

    def __enter__(self):
        self._ctx.__enter__()
        return self

    def __exit__(self, *args):
        self._ctx.__exit__(*args)

def startup_animation():
    console.print()
    console.print(Panel(
        "[dim]Private Meeting Notes  ·  Audio stays local[/dim]",
        title="[bold blue]LOCALPLAUD[/bold blue]",
        border_style="blue",
        padding=(0, 2),
    ))
    console.print()

# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------
def ask_text(prompt: str, default: str = "") -> str:
    if _INQUIRER:
        try:
            result = inquirer.text(message=prompt, default=default).execute()
            return (result or "").strip()
        except (KeyboardInterrupt, EOFError):
            return ""
        except Exception:
            pass
    try:
        return input(f"  ? {prompt} ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""

def ask_confirm(prompt: str, default: bool = False) -> bool:
    if _INQUIRER:
        try:
            return inquirer.confirm(message=prompt, default=default).execute()
        except (KeyboardInterrupt, EOFError):
            return default
        except Exception:
            pass
    hint = "(Y/n)" if default else "(y/N)"
    try:
        val = input(f"  ? {prompt} {hint} ").strip().upper()
        return (val == "Y") if val else default
    except (EOFError, KeyboardInterrupt):
        return default

def ask_select(prompt: str, choices: list, default_index: int = 0) -> str | None:
    if _INQUIRER:
        try:
            return inquirer.select(
                message=prompt,
                choices=choices,
                default=choices[default_index] if choices else None,
            ).execute()
        except (KeyboardInterrupt, EOFError):
            return None
        except Exception:
            pass
    console.print()
    for i, ch in enumerate(choices, 1):
        label = ch.name if hasattr(ch, "name") else str(ch)
        console.print(f"  [cyan][{i}][/cyan]  {label}")
    console.print()
    try:
        val = input(f"  ? {prompt} ").strip()
        if val.isdigit() and 1 <= int(val) <= len(choices):
            ch = choices[int(val) - 1]
            return ch.value if hasattr(ch, "value") else ch
    except (EOFError, KeyboardInterrupt):
        pass
    return None

def safe_input(prompt: str, buffer: bool = False) -> str:
    if buffer:
        time.sleep(0.1)
    return ask_text(prompt)

# ---------------------------------------------------------------------------
# Ensure folders exist
# ---------------------------------------------------------------------------
for _d in [DIR_NOT_TRANSCRIBED, DIR_COMPLETED, DIR_MARKDOWN, DIR_PDF,
           DIR_REVIEW, DIR_TRANSCRIPTS, DIR_STATE]:
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg: str):
    ts   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"[{ts}] {msg}"
    console.print(f"  [dim]{line}[/dim]")
    try:
        with open(FILE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def log_error(event: str, error: Exception, extra: dict | None = None):
    payload = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "error_type": type(error).__name__,
        "error": str(error),
    }
    if extra:
        payload["extra"] = extra
    try:
        with open(FILE_ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass

def run_doctor() -> int:
    print_section("LOCALPLAUD DOCTOR")
    ok = True
    checks = [
        ("Recordings/Not Transcribed", DIR_NOT_TRANSCRIBED),
        ("Recordings/Completed",       DIR_COMPLETED),
        ("Meeting Minutes/Markdown",   DIR_MARKDOWN),
        ("Meeting Minutes/PDF",        DIR_PDF),
        ("Meeting Minutes/Review",     DIR_REVIEW),
        ("Meeting Minutes/Transcripts", DIR_TRANSCRIPTS),
        (".localplaud_state",          DIR_STATE),
    ]
    for label, path in checks:
        try:
            path.mkdir(parents=True, exist_ok=True)
            test_file = path / ".write_test.tmp"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
            print_ok(f"{label} is writable")
        except Exception as e:
            ok = False
            print_err(f"{label} not writable: {e}")
            log_error("doctor_path_check_failed", e, {"path": str(path)})

    if ANTHROPIC_API_KEY:
        print_ok("ANTHROPIC_API_KEY is set")
    else:
        ok = False
        print_err("ANTHROPIC_API_KEY is missing")

    print_info(f"Whisper model: {WHISPER_MODEL}")
    print_info(f"Claude model:  {CLAUDE_MODEL}")
    if HF_TOKEN:
        print_ok("HF_TOKEN is set (speaker ID enabled)")
    else:
        print_warn("HF_TOKEN missing (speaker ID disabled)")

    for lib in ["faster_whisper", "anthropic", "reportlab", "dotenv"]:
        try:
            __import__(lib)
            print_ok(f"Import OK: {lib}")
        except Exception as e:
            ok = False
            print_err(f"Import failed: {lib} ({e})")
            log_error("doctor_import_failed", e, {"module": lib})

    console.print()
    if ok:
        print_ok("Doctor checks passed.")
        return 0
    print_warn("Doctor found issues. Fix above items and retry.")
    return 1

# ---------------------------------------------------------------------------
# Checkpoint / resume
# ---------------------------------------------------------------------------
def state_path(stem: str) -> Path:
    return DIR_STATE / f"{stem}.json"

def save_state(stem: str, state: dict):
    with open(state_path(stem), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_state(stem: str) -> dict | None:
    p = state_path(stem)
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def clear_state(stem: str):
    p = state_path(stem)
    if p.exists():
        p.unlink()

def list_incomplete_states() -> list[dict]:
    states = []
    for p in DIR_STATE.glob("*.json"):
        if p.stem.endswith("_tx"):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                s = json.load(f)
            s["_state_file"] = str(p)
            s["_stem"]       = p.stem
            states.append(s)
        except Exception:
            pass
    return states

def tx_archive_path(stem: str) -> Path:
    return DIR_STATE / f"{stem}_tx.json"

def save_tx_archive(stem: str, state: dict):
    """Permanent transcript archive — survives completion, enables reprocess from any step."""
    archive = {k: state.get(k) for k in (
        "audio_filename", "meeting_date", "audio_duration",
        "transcript", "user_context", "notes_instructions", "continuation",
    )}
    with open(tx_archive_path(stem), "w", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=2)

def list_tx_archives() -> list[dict]:
    archives = []
    for p in DIR_STATE.glob("*_tx.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                a = json.load(f)
            a["_archive_file"] = str(p)
            a["_stem"] = p.stem[:-3]
            archives.append(a)
        except Exception:
            pass
    return archives

# ---------------------------------------------------------------------------
# File utilities
# ---------------------------------------------------------------------------
def list_audio_files(folder: Path) -> list[Path]:
    return [p for p in sorted(folder.iterdir()) if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS]

def safe_move(src: Path, dst_dir: Path) -> Path:
    dst = dst_dir / src.name
    if dst.exists():
        dst = dst_dir / f"{src.stem}_2{src.suffix}"
    shutil.move(str(src), str(dst))
    return dst

def unique_path(folder: Path, stem: str, ext: str) -> Path:
    p = folder / f"{stem}{ext}"
    n = 2
    while p.exists():
        p = folder / f"{stem}_{n}{ext}"
        n += 1
    return p

# ---------------------------------------------------------------------------
# Date detection
# ---------------------------------------------------------------------------
DATE_PATTERNS = [
    r"(\d{4})[._-](\d{2})[._-](\d{2})",
    r"(\d{4})(\d{2})(\d{2})",
]

def detect_date(audio_path: Path) -> str:
    for pattern in DATE_PATTERNS:
        m = re.search(pattern, audio_path.stem)
        if m:
            try:
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return datetime.datetime.fromtimestamp(audio_path.stat().st_mtime).strftime("%Y-%m-%d")

# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------
def transcribe(audio_path: Path) -> tuple[list[dict], float]:
    print_section("TRANSCRIBE", 3, 8)

    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        device = "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    print_info(f"Model: [white]{WHISPER_MODEL}[/white]  ·  Device: [white]{device.upper()} / {compute_type}[/white]")
    print_info(f"File:  [white]{audio_path.name}[/white]")
    console.print()

    from faster_whisper import WhisperModel

    with Spinner("Loading Whisper model"):
        model = WhisperModel(WHISPER_MODEL, device=device, compute_type=compute_type)
    print_ok("Whisper model loaded")
    console.print()

    segments_iter, info = model.transcribe(
        str(audio_path),
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    duration = info.duration
    print_info(f"Duration: [white]{duration/60:.1f} min[/white]  ·  Language: [white]{info.language}[/white]")
    console.print()

    segments_out = []
    count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[dim]{task.fields[snippet]}[/dim]"),
        BarColumn(bar_width=28),
        TextColumn("[cyan]{task.percentage:>3.0f}%[/cyan]"),
        TextColumn("[dim]{task.fields[time_str]}[/dim]"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("", total=duration, snippet="Waiting for first segment...", time_str="")
        for seg in segments_iter:
            segments_out.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()})
            count += 1
            snip = seg.text.strip()
            if len(snip) > 55:
                snip = snip[:54] + "…"
            progress.update(task, completed=seg.end, snippet=f"› {snip}",
                            time_str=f"{seg.end/60:.1f}/{duration/60:.1f}min · {count}seg")

    console.print()
    print_ok(f"Transcription complete  ·  [white]{count} segments[/white]  ·  [white]{duration/60:.1f} min[/white]")
    return segments_out, duration

# ---------------------------------------------------------------------------
# Diarization helpers
# ---------------------------------------------------------------------------
def _find_contextual_excerpts(labelled: list[dict], target: str,
                              window: int = 10, max_samples: int = 8) -> list[list[dict]]:
    """Return useful, non-overlapping samples for identifying one speaker.

    Longer utterances are generally easier to recognise. After ranking on clarity,
    samples are spread across the recording so "show more" does not repeat the
    same moment of the meeting.
    """
    appearances = [i for i, s in enumerate(labelled) if s["speaker"] == target]
    if not appearances:
        return []

    ranked = sorted(
        appearances,
        key=lambda i: (len(labelled[i].get("text", "")), -i),
        reverse=True,
    )
    chosen_ranges: list[tuple[int, int]] = []
    samples: list[list[dict]] = []
    for idx in ranked:
        start = max(0, idx - window // 2)
        end = min(len(labelled), start + window)
        start = max(0, end - window)
        # Avoid near-duplicate views of the same conversation turn.
        if any(max(start, old_start) < min(end, old_end) for old_start, old_end in chosen_ranges):
            continue
        chosen_ranges.append((start, end))
        samples.append(labelled[start:end])
        if len(samples) >= max_samples:
            break

    if not samples:
        idx = appearances[0]
        start = max(0, idx - window // 2)
        samples.append(labelled[start:min(len(labelled), start + window)])
    return samples

def _render_excerpt(excerpt: list[dict], speaker_color: dict) -> Text:
    text = Text()
    prev_s = None
    for seg in excerpt:
        s_name = seg["speaker"]
        color  = speaker_color.get(s_name, "white")
        ts     = f"[{int(seg['start']//60):02d}:{int(seg['start']%60):02d}]"
        if s_name != prev_s:
            text.append("\n")
            text.append(f"  {ts}  ", style="dim")
            text.append(f"{s_name}\n", style=f"bold {color}")
            prev_s = s_name
        text.append(f"                    {seg['text']}\n")
    return text

# ---------------------------------------------------------------------------
# Diarization
# ---------------------------------------------------------------------------
def diarize(audio_path: Path, segments: list[dict]) -> tuple[list[dict], int]:
    print_section("SPEAKER ID", 4, 8)

    import os, sys
    import torch
    import torchaudio

    # PyTorch 2.6 changed weights_only default to True, breaking pyannote checkpoints.
    # The official override env var (checked at call time by torch.serialization.load).
    os.environ.setdefault("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", "1")

    # ── Patch: huggingface_hub use_auth_token → token ───────────────────────
    # pyannote 3.3.x passes use_auth_token= internally; newer huggingface_hub
    # removed that parameter. Patch hf_hub_download BEFORE importing pyannote.
    import huggingface_hub as _hf
    import huggingface_hub.file_download as _hfd
    _orig_dl = _hfd.hf_hub_download
    if not getattr(_orig_dl, "_lp_patched", False):
        def _compat_dl(*a, use_auth_token=None, **kw):
            if use_auth_token is not None and "token" not in kw:
                kw["token"] = use_auth_token
            return _orig_dl(*a, **kw)
        _compat_dl._lp_patched = True
        _hfd.hf_hub_download = _compat_dl
        _hf.hf_hub_download = _compat_dl

    # ── Import pyannote AFTER both patches are applied ───────────────────────
    from pyannote.audio import Pipeline
    # Sweep any module-level bindings pyannote created during its import
    for _mod in list(sys.modules.values()):
        try:
            if getattr(_mod, "hf_hub_download", None) is _orig_dl:
                _mod.hf_hub_download = _compat_dl
        except Exception:
            pass

    with Spinner("Loading pyannote speaker model  (first run ~1 GB download)"):
        pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1")

    try:
        if torch.cuda.is_available():
            pipeline = pipeline.to(torch.device("cuda"))
    except Exception:
        pass

    print_ok("Speaker model loaded")
    console.print()

    with Spinner("Loading audio waveform"):
        try:
            waveform, sample_rate = torchaudio.load(str(audio_path))
        except Exception:
            import av as _av, numpy as _np
            _con = _av.open(str(audio_path))
            sample_rate = _con.streams.audio[0].codec_context.sample_rate
            _rs = _av.audio.resampler.AudioResampler(format="fltp")
            _chunks = []
            for _f in _con.decode(audio=0):
                _f.pts = None
                for _o in _rs.resample(_f):
                    _chunks.append(_o.to_ndarray())
            _con.close()
            _arr = _np.concatenate(_chunks, axis=1).astype(_np.float32) if _chunks else _np.zeros((1, 0), dtype=_np.float32)
            waveform = torch.from_numpy(_arr)
    print_ok("Waveform loaded")
    console.print()

    with Spinner("Running diarization"):
        diarization = pipeline({"waveform": waveform, "sample_rate": sample_rate})

    annotation = diarization.diarization if hasattr(diarization, "diarization") else diarization
    turns = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        turns.append({"start": turn.start, "end": turn.end, "speaker": speaker})

    def best_speaker(seg_start: float, seg_end: float) -> str:
        best, best_overlap = None, 0.0
        for t in turns:
            overlap = min(seg_end, t["end"]) - max(seg_start, t["start"])
            if overlap > best_overlap:
                best_overlap, best = overlap, t["speaker"]
        return best or "UNKNOWN"

    labelled = []
    for seg in segments:
        speaker_id = best_speaker(seg["start"], seg["end"])
        labelled.append({**seg, "speaker_id": speaker_id, "speaker": speaker_id})
    all_speakers = sorted({s["speaker"] for s in labelled})
    speaker_color = {spk: SPEAKER_COLORS[i % len(SPEAKER_COLORS)] for i, spk in enumerate(all_speakers)}

    spk_tags = "  ".join(f"[{speaker_color[s]}]{s}[/]" for s in all_speakers)
    print_ok(f"Detected [white]{len(all_speakers)} speaker(s)[/white]: {spk_tags}")
    console.print()

    console.print("  [bold cyan]NAME SPEAKERS[/bold cyan]  [dim]Type M to show another sample · Enter to keep the label[/dim]")
    console.print()

    name_map = {}
    for spk in all_speakers:
        color = speaker_color.get(spk, "white")
        samples = _find_contextual_excerpts(labelled, spk, window=10)
        sample_index = 0
        while True:
            ctx = samples[sample_index]
            console.print(Panel(
                _render_excerpt(ctx, speaker_color),
                title=(f"[{color}]▶  {spk}[/]  [dim]— sample "
                       f"{sample_index + 1} of {len(samples)}[/dim]"),
                border_style=color,
                padding=(0, 1),
            ))
            ans = ask_text("Name this speaker, M for more, or Enter to keep:")
            if ans.strip().lower() in {"m", "more"}:
                if sample_index + 1 < len(samples):
                    sample_index += 1
                else:
                    print_info("No more distinct samples are available for this speaker.")
                console.print()
                continue
            name_map[spk] = ans.strip() if ans.strip() else spk
            console.print()
            break

    for seg in labelled:
        seg["speaker"] = name_map.get(seg["speaker"], seg["speaker"])
    speaker_color = {name_map.get(old, old): col for old, col in speaker_color.items()}

    console.print()
    final_speakers = sorted({s["speaker"] for s in labelled})
    final_tags = "  ".join(f"[{speaker_color.get(s,'white')}]{s}[/]" for s in final_speakers)
    print_ok(f"Speaker labels finalised  ·  [white]{len(final_speakers)} speaker(s)[/white]: {final_tags}")
    return labelled, len(final_speakers)

# ---------------------------------------------------------------------------
# Transcript formatting
# ---------------------------------------------------------------------------
def format_transcript(segments: list[dict], has_speakers: bool) -> str:
    lines, prev_speaker = [], None
    for seg in segments:
        if not seg.get("text"):
            continue
        if has_speakers:
            spk = seg.get("speaker", "UNKNOWN")
            if spk != prev_speaker:
                lines.append(f"\n{spk}:")
                prev_speaker = spk
            lines.append(f"  [{_format_hms(seg.get('start', 0))}] {seg['text']}")
        else:
            lines.append(f"[{_format_hms(seg.get('start', 0))}] {seg['text']}")
    return "\n".join(lines)

def _format_hms(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"

def _parse_hms(value: str) -> float:
    parts = [int(part) for part in value.strip().split(":")]
    if len(parts) != 3:
        raise ValueError(f"Invalid timestamp: {value}")
    hours, minutes, seconds = parts
    if minutes > 59 or seconds > 59:
        raise ValueError(f"Invalid timestamp: {value}")
    return float(hours * 3600 + minutes * 60 + seconds)

def generate_review_markdown(state: dict) -> str:
    """Create the human-editable gate between local processing and Claude."""
    segments = state.get("transcript") or []
    speaker_map: dict[str, str] = {}
    for seg in segments:
        speaker_id = seg.get("speaker_id") or seg.get("speaker") or "TRANSCRIPT"
        speaker_map.setdefault(speaker_id, seg.get("speaker") or speaker_id)

    lines = [
        f"# {Path(state['audio_filename']).stem} - Review",
        "",
        "> Review speaker names, transcript text, context, and instructions below.",
        "> LocalPlaud will not contact Claude until you choose Finalize reviewed meeting.",
        "> To swap a speaker everywhere, edit only that entry in Speaker Map.",
        "",
        "## Meeting Date",
        state.get("meeting_date") or "",
        "",
        "## Meeting Context",
        state.get("user_context") or "",
        "",
        "## Notes Instructions",
        state.get("notes_instructions") or "",
        "",
        "## Speaker Map",
    ]
    if state.get("has_speakers"):
        for speaker_id, name in sorted(speaker_map.items()):
            lines.append(f"- {speaker_id}: {name}")
    else:
        lines.append("No speaker labels available.")

    lines.extend([
        "",
        "## Transcript",
        "<!-- Keep each transcript segment on one line and preserve its stable speaker ID. -->",
    ])
    for seg in segments:
        speaker_id = seg.get("speaker_id") or seg.get("speaker") or "TRANSCRIPT"
        speaker = seg.get("speaker") or speaker_id
        text = " ".join((seg.get("text") or "").splitlines()).strip()
        lines.append(
            f"[{_format_hms(seg.get('start', 0))} - {_format_hms(seg.get('end', 0))}] "
            f"{speaker_id} [{speaker}]: {text}"
        )
    lines.append("")
    return "\n".join(lines)

def parse_review_markdown(review_path: Path) -> dict:
    """Read user corrections from a LocalPlaud review Markdown file."""
    raw = review_path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    required = ["## Meeting Date", "## Meeting Context", "## Notes Instructions",
                "## Speaker Map", "## Transcript"]
    headings = {
        line.strip(): i for i, line in enumerate(lines)
        if line.strip() in required
    }
    missing = [heading for heading in required if heading not in headings]
    if missing:
        raise ValueError("Review file is missing section(s): " + ", ".join(missing))

    def section_text(heading: str) -> str:
        start = headings[heading] + 1
        later = [idx for idx in headings.values() if idx > headings[heading]]
        end = min(later) if later else len(lines)
        return "\n".join(lines[start:end]).strip()

    meeting_date = section_text("## Meeting Date").splitlines()[0].strip()
    datetime.date.fromisoformat(meeting_date)
    user_context = section_text("## Meeting Context")
    notes_instructions = section_text("## Notes Instructions")

    speaker_map: dict[str, str] = {}
    for line in section_text("## Speaker Map").splitlines():
        match = re.match(r"^\s*-\s*([^:]+):\s*(.+?)\s*$", line)
        if match:
            speaker_map[match.group(1).strip()] = match.group(2).strip()

    segment_re = re.compile(
        r"^\[(\d{2}:\d{2}:\d{2})\s+-\s+(\d{2}:\d{2}:\d{2})\]\s+"
        r"(\S+)\s+\[(.*?)\]:\s*(.*)$"
    )
    segments = []
    for line in section_text("## Transcript").splitlines():
        if not line.strip() or line.lstrip().startswith("<!--"):
            continue
        match = segment_re.match(line)
        if not match:
            raise ValueError(f"Could not read transcript line: {line[:100]}")
        start, end, speaker_id, inline_name, text = match.groups()
        speaker = speaker_map.get(speaker_id, inline_name.strip() or speaker_id)
        segments.append({
            "start": _parse_hms(start),
            "end": _parse_hms(end),
            "speaker_id": speaker_id,
            "speaker": speaker,
            "text": text.strip(),
        })
    if not segments:
        raise ValueError("No transcript segments found in the review file.")

    has_speakers = any(seg["speaker_id"] != "TRANSCRIPT" for seg in segments)
    speaker_count = len({seg["speaker"] for seg in segments}) if has_speakers else 0
    return {
        "meeting_date": meeting_date,
        "user_context": user_context,
        "notes_instructions": notes_instructions,
        "transcript": segments,
        "has_speakers": has_speakers,
        "speaker_count": speaker_count,
    }

def generate_transcript_markdown(state: dict) -> str:
    """Render a Gemini-style editable transcript with periodic time headings."""
    title = Path(state["audio_filename"]).stem
    duration = state.get("audio_duration", 0)
    segments = state.get("transcript") or []
    lines = [f"# {title} - Transcript", ""]
    last_heading_at = -60.0
    for seg in segments:
        start = float(seg.get("start", 0))
        if not lines or start - last_heading_at >= 60 or last_heading_at < 0:
            lines.extend([f"## {_format_hms(start)}", ""])
            last_heading_at = start
        speaker = seg.get("speaker") if state.get("has_speakers") else "Speaker"
        lines.extend([f"{speaker}: {seg.get('text', '').strip()}", ""])
    lines.extend([
        f"## Transcription ended after {_format_hms(duration)}",
        "",
        "This editable transcript was computer generated and may contain errors.",
        "",
    ])
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Claude summarisation
# ---------------------------------------------------------------------------
CLAUDE_PROMPT = """\
You are an expert business meeting notes editor. Write in the concise,
outcome-led style of Google Meet's "Notes by Gemini", while relying only on the
provided transcript and context.

{context_section}

{user_context_section}

{notes_instructions_section}

CRITICAL RULES:
- Cover every substantive topic, but omit greetings, audio troubleshooting, and
  unrelated small talk unless they materially affected the meeting.
- Use real names, organisations, numbers, and dates from the transcript.
- Preserve proper nouns exactly as provided; do not silently rename them.
- Clearly distinguish discussion, proposals, alignment, decisions, and completed work.
- Never convert a suggestion into a decision or invent an owner, deadline, or fact.
- Use neutral, professional third-person wording.
- Keep bullets concise while retaining the reasoning and specifics that matter.
- Details must progress in meeting order and cite supporting timestamps as (HH:MM:SS).
{speaker_rules}

IMPORTANT: Your response MUST begin with this exact line:
TOPIC: [2-4 word topic in kebab-case, e.g. Strategy-Session, Client-Call-Franke, Weekly-Standup]

Then use EXACTLY these Markdown sections in this order:

## Summary
Begin with one outcome-led sentence that captures the meeting as a whole.
Then add 2-5 thematic subheadings using ###, each followed by 1-3 concise
sentences describing the principal discussion and outcome. Do not use bullets
in this section.

## Decisions
### Aligned
List only explicit decisions or clear alignment:
- **Short decision title:** What was agreed and the material rationale.
If none, write "No explicit decisions recorded."

## Next steps
Use this exact action format:
{action_format}
Include only supported actions. Use [The group] only when responsibility was
genuinely collective. If none, write "No next steps identified."

## Details
Create chronological bullets grouped by meaningful topics:
- **Topic title:** A compact but sufficiently detailed synthesis naming the
  relevant participants and preserving concrete facts. End with one or more
  supporting timestamps such as (00:12:34) or (00:12:34) (00:18:02).

TRANSCRIPT:
{transcript}
"""

def build_prompt(transcript_text: str, has_speakers: bool, user_context: str,
                 notes_instructions: str = "") -> str:
    context_section = ""
    if FILE_CONTEXT.exists():
        ctx = FILE_CONTEXT.read_text(encoding="utf-8").strip()
        if ctx:
            context_section = f"BUSINESS CONTEXT:\n{ctx}"

    user_context_section = ""
    if user_context.strip():
        user_context_section = f"MEETING-SPECIFIC CONTEXT:\n{user_context.strip()}"

    notes_instructions_section = ""
    if notes_instructions.strip():
        notes_instructions_section = f"USER NOTES INSTRUCTIONS:\n{notes_instructions.strip()}"

    if has_speakers:
        speaker_rules = "- Attribute action items and quotes to specific speakers"
        action_format = "- **[Full Name] Action title:** Clear description, including a deadline only when stated."
    else:
        speaker_rules = "- Do NOT assign action items to specific people (speakers not identified)"
        action_format = "- **[Unassigned] Action title:** Clear description, including a deadline only when stated."

    return CLAUDE_PROMPT.format(
        context_section=context_section,
        user_context_section=user_context_section,
        notes_instructions_section=notes_instructions_section,
        speaker_rules=speaker_rules,
        action_format=action_format,
        transcript=transcript_text,
    )

def summarise(segments: list[dict], has_speakers: bool, user_context: str,
              notes_instructions: str = "") -> tuple[str, str, int, int, float]:
    print_section("SUMMARISE", 6, 8)
    print_info(f"Model: [white]{CLAUDE_MODEL}[/white]")

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    transcript_text = format_transcript(segments, has_speakers)
    prompt          = build_prompt(transcript_text, has_speakers, user_context, notes_instructions)

    print_info(f"Transcript: [white]{len(transcript_text):,} chars[/white]  ·  Prompt: [white]{len(prompt):,} chars[/white]")
    console.print()

    with Spinner("Claude is reading the transcript"):
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

    response_text = message.content[0].text
    in_tokens     = message.usage.input_tokens
    out_tokens    = message.usage.output_tokens
    cost = (in_tokens * HAIKU_COST_IN + out_tokens * HAIKU_COST_OUT) if "haiku" in CLAUDE_MODEL \
           else (in_tokens * SONNET_COST_IN + out_tokens * SONNET_COST_OUT)

    print_ok(f"Response received  ·  [white]{in_tokens:,} in / {out_tokens:,} out[/white]  ·  [green]~${cost:.4f}[/green]")

    lines = response_text.strip().splitlines()
    topic_slug, notes = "Meeting-Notes", response_text
    if lines and lines[0].upper().startswith("TOPIC:"):
        raw_topic  = lines[0].split(":", 1)[1].strip()
        topic_slug = re.sub(r"[^a-zA-Z0-9\-]", "", raw_topic.replace(" ", "-")) or "Meeting-Notes"
        notes = "\n".join(lines[1:]).strip()

    return notes, topic_slug, in_tokens, out_tokens, cost

# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------
NAVY  = (26/255,  39/255,  68/255)
BLUE  = (46/255, 109/255, 164/255)
LGREY = (240/255, 244/255, 248/255)
BLACK = (0,       0,       0)
DGREY = (80/255,  80/255,  80/255)

def generate_pdf(notes_md: str, output_path: Path, meeting_date: str, topic: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER

    width, height = A4
    margin = 20 * mm

    doc = SimpleDocTemplate(str(output_path), pagesize=A4,
                            leftMargin=margin, rightMargin=margin,
                            topMargin=margin, bottomMargin=margin)

    def c(rgb):
        return colors.Color(*rgb)

    style_title    = ParagraphStyle("title",    fontSize=20, leading=26, textColor=c(NAVY), spaceAfter=4,   fontName="Helvetica-Bold")
    style_subtitle = ParagraphStyle("subtitle", fontSize=11, leading=14, textColor=c(BLUE), spaceAfter=10,  fontName="Helvetica")
    style_h2       = ParagraphStyle("h2",       fontSize=13, leading=17, textColor=c(NAVY), spaceBefore=14, spaceAfter=4,  fontName="Helvetica-Bold")
    style_h3       = ParagraphStyle("h3",       fontSize=11, leading=14, textColor=c(BLUE), spaceBefore=8,  spaceAfter=3,  fontName="Helvetica-Bold")
    style_body     = ParagraphStyle("body",     fontSize=10, leading=14, textColor=c(BLACK), spaceAfter=3,  fontName="Helvetica")
    style_bullet   = ParagraphStyle("bullet",   fontSize=10, leading=14, textColor=c(BLACK), spaceAfter=2,  leftIndent=14, fontName="Helvetica")
    style_footer   = ParagraphStyle("footer",   fontSize=8,  leading=10, textColor=c(DGREY), spaceAfter=0,  fontName="Helvetica", alignment=TA_CENTER)

    story = []
    story.append(Paragraph(topic.replace("-", " "), style_title))
    story.append(Paragraph(f"Meeting Notes  |  {meeting_date}", style_subtitle))
    story.append(HRFlowable(width="100%", thickness=1.5, color=c(NAVY), spaceAfter=10))

    def md(text: str) -> str:
        import re as _re
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        text = _re.sub(r'__(.+?)__',     r'<b>\1</b>', text)
        text = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
        return text

    in_email_block, email_lines = False, []

    def flush_email():
        nonlocal in_email_block, email_lines
        if email_lines:
            data = [[Paragraph("<br/>".join(email_lines), style_body)]]
            t    = Table(data, colWidths=[width - 2 * margin])
            t.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), c(LGREY)),
                ("BOX",           (0, 0), (-1, -1), 0.5, c(BLUE)),
                ("LEFTPADDING",   (0, 0), (-1, -1), 10),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
                ("TOPPADDING",    (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]))
            story.append(t)
            story.append(Spacer(1, 8))
        in_email_block, email_lines = False, []

    for raw_line in notes_md.splitlines():
        line = raw_line.strip()
        if line.startswith("## Follow-Up Email Draft"):
            flush_email()
            story.append(Paragraph("Follow-Up Email Draft", style_h2))
            in_email_block = True
            continue
        if in_email_block:
            if line.startswith("## "):
                flush_email()
                story.append(Paragraph(line[3:], style_h2))
            else:
                email_lines.append(md(line) if line else "&nbsp;")
            continue
        if line.startswith("## "):
            story.append(Paragraph(md(line[3:]), style_h2))
        elif line.startswith("### "):
            story.append(Paragraph(md(line[4:]), style_h3))
        elif line.startswith("- [ ] ") or line.startswith("- [x] "):
            box_char = "☑" if line.startswith("- [x] ") else "☐"
            story.append(Paragraph(f"{box_char}  {md(line[6:])}", style_bullet))
        elif line.startswith("- "):
            story.append(Paragraph(f"•  {md(line[2:])}", style_bullet))
        elif line.startswith("> "):
            story.append(Paragraph(f'<i>"{md(line[2:])}"</i>', style_body))
        elif line.startswith('"') and line.endswith('"'):
            story.append(Paragraph(f"<i>{md(line)}</i>", style_body))
        elif line in ("", "---"):
            story.append(Spacer(1, 4))
        elif line:
            story.append(Paragraph(md(line), style_body))

    if in_email_block:
        flush_email()

    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=0.5, color=c(BLUE), spaceAfter=4))
    story.append(Paragraph(
        f"Generated by LocalPlaud  |  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}  |  Audio stays local — only text sent to Claude",
        style_footer,
    ))
    doc.build(story)

# ---------------------------------------------------------------------------
# Completion card
# ---------------------------------------------------------------------------
def print_completion_card(state: dict):
    topic    = state.get("topic", "Meeting-Notes")
    date_str = state.get("meeting_date", "")
    dur      = state.get("audio_duration", 0)
    segs     = len(state.get("transcript") or [])
    spks     = state.get("speaker_count", 0)
    tok_in   = state.get("token_in",  0)
    tok_out  = state.get("token_out", 0)
    cost     = state.get("cost", 0.0)
    md_path  = state.get("md_path",  "")
    pdf_path = state.get("pdf_path", "")
    transcript_path = state.get("transcript_path", "")

    dur_str = f"{dur/60:.0f} min  ·  {segs} segments"
    if spks:
        dur_str += f"  ·  {spks} speaker{'s' if spks != 1 else ''}"

    t = Table(box=None, show_header=False, padding=(0, 1))
    t.add_column(style="dim", no_wrap=True)
    t.add_column()
    t.add_row("Audio",   dur_str)
    t.add_row("Tokens",  f"{tok_in:,} in / {tok_out:,} out  ·  [green]~${cost:.4f}[/green]")
    if md_path:
        t.add_row("Markdown", Path(md_path).name)
    if pdf_path:
        t.add_row("PDF",      Path(pdf_path).name)
    if transcript_path:
        t.add_row("Transcript", Path(transcript_path).name)

    console.print()
    console.print(Panel(
        t,
        title=f"[green]✓ COMPLETE[/green]  [bold white]{topic.replace('-', ' ')}[/bold white]  [dim]·  {date_str}[/dim]",
        border_style="green",
        padding=(0, 1),
    ))
    console.print()

# ---------------------------------------------------------------------------
# Processing pipeline
# ---------------------------------------------------------------------------
def process_file(audio_path: Path, state: dict | None = None):
    stem = audio_path.stem

    if state is None:
        state = {
            "current_step":   1,
            "audio_filename": audio_path.name,
            "meeting_date":   None,
            "transcript":     None,
            "audio_duration": 0,
            "has_speakers":   False,
            "speaker_count":  0,
            "notes":          None,
            "topic":          None,
            "token_in":       0,
            "token_out":      0,
            "cost":           0.0,
            "user_context":   "",
            "notes_instructions": "",
            "continuation":   None,
            "awaiting_review": False,
            "review_path":    None,
            "md_path":        None,
            "pdf_path":       None,
            "transcript_path": None,
        }

    # Backwards compatibility for checkpoints created by earlier releases.
    state.setdefault("notes_instructions", "")
    state.setdefault("awaiting_review", False)
    state.setdefault("review_path", None)
    state.setdefault("transcript_path", None)

    if state.get("awaiting_review"):
        print_warn("This meeting is waiting for transcript review.")
        if state.get("review_path"):
            print_info(f"Review file: {state['review_path']}")
        print_info("Edit the Markdown file, then choose 'Finalize reviewed meeting'.")
        return "awaiting_review"

    # Step 1: VALIDATE
    if state["current_step"] <= 1:
        print_section("VALIDATE", 1, 8)
        if not audio_path.exists():
            print_err(f"File not found: {audio_path}")
            return
        if not ANTHROPIC_API_KEY:
            print_err("ANTHROPIC_API_KEY not set. Check your .env file.")
            return
        print_ok(f"File:    {audio_path.name}")
        print_ok("API key present")
        state["current_step"] = 2

    # Step 2: DATE + context
    if state["current_step"] <= 2:
        print_section("PREPARE", 2, 8)

        if state["meeting_date"] is None:
            date = detect_date(audio_path)
            print_info(f"Detected date: [white]{date}[/white]")
            confirm = ask_text(f"Use {date}? (Enter to confirm, or type YYYY-MM-DD):")
            if confirm:
                try:
                    datetime.date.fromisoformat(confirm)
                    date = confirm
                except ValueError:
                    print_warn(f"Invalid format — using detected date: {date}")
            state["meeting_date"] = date
            print_ok(f"Meeting date: [white]{state['meeting_date']}[/white]")

        if not state["user_context"]:
            console.print()
            print_info("Add one-time context for this meeting (e.g. 'Budget review with Franke Ltd')")
            state["user_context"] = ask_text("Context (Enter to skip):")

            console.print()
            if ask_confirm("Continuation of a previous recording?", default=False):
                all_audio = list_audio_files(DIR_COMPLETED) + list_audio_files(DIR_NOT_TRANSCRIBED)
                if all_audio:
                    choices = [Choice(value=str(f), name=f"{f.name}  ({f.stat().st_size/1024/1024:.1f} MB)") for f in all_audio]
                    choices.append(Choice(value="", name="Skip"))
                    picked = ask_select("Select continuation file:", choices=choices)
                    if picked:
                        state["continuation"] = picked
                        print_ok(f"Continuation of: {Path(picked).name}")

        state["current_step"] = 3
        save_state(stem, state)

    # Step 3: TRANSCRIBE
    if state["current_step"] <= 3:
        if state["transcript"] is None:
            state["transcript"], state["audio_duration"] = transcribe(audio_path)
        state["current_step"] = 4
        save_state(stem, state)
        save_tx_archive(stem, state)

    # Step 4: DIARIZATION
    if state["current_step"] <= 4:
        if HF_TOKEN and not state["has_speakers"]:
            try:
                state["transcript"], state["speaker_count"] = diarize(audio_path, state["transcript"])
                state["has_speakers"] = True
            except Exception as e:
                print_warn(f"Speaker ID failed: {e}")
                print_info("Continuing without speaker labels.")
                log_error("diarization_failed", e, {"audio": str(audio_path)})
                state["has_speakers"] = False
        state["current_step"] = 5
        save_state(stem, state)

    # Step 5: WRITE REVIEW FILE AND PAUSE BEFORE ANY CLAUDE REQUEST
    if state["current_step"] <= 5:
        print_section("REVIEW TRANSCRIPT", 5, 8)
        review_base = f"{state['meeting_date']}_{stem}_Review"
        review_path = unique_path(DIR_REVIEW, review_base, ".md")
        review_path.write_text(generate_review_markdown(state), encoding="utf-8")
        state["review_path"] = str(review_path)
        state["awaiting_review"] = True
        state["current_step"] = 6
        save_state(stem, state)
        print_ok(f"Review file → [white]{review_path.name}[/white]")
        print_info("No transcript text has been sent to Claude.")
        print_info("Correct the file, then choose 'Finalize reviewed meeting' from the main menu.")
        return "awaiting_review"

    # Step 6: SUMMARISE THE USER-REVIEWED TRANSCRIPT
    if state["current_step"] <= 6:
        if state["notes"] is None:
            (state["notes"], state["topic"],
             state["token_in"], state["token_out"],
             state["cost"]) = summarise(
                 state["transcript"], state["has_speakers"],
                 state["user_context"], state.get("notes_instructions", ""),
             )
        state["current_step"] = 7
        save_state(stem, state)

    # Step 7: SAVE OUTPUT
    if state["current_step"] <= 7:
        print_section("SAVE OUTPUT", 7, 8)
        date_str = state["meeting_date"]
        topic    = state["topic"]
        base     = f"{date_str}_Work_MM_{topic}"
        md_path  = unique_path(DIR_MARKDOWN, base, ".md")
        pdf_path = unique_path(DIR_PDF,      base, ".pdf")
        transcript_path = unique_path(DIR_TRANSCRIPTS, f"{date_str}_{topic}_Transcript", ".md")

        md_path.write_text(state["notes"], encoding="utf-8")
        print_ok(f"Markdown → [white]{md_path.name}[/white]")

        transcript_path.write_text(generate_transcript_markdown(state), encoding="utf-8")
        print_ok(f"Transcript → [white]{transcript_path.name}[/white]")
        state["transcript_path"] = str(transcript_path)

        try:
            with Spinner("Generating PDF"):
                generate_pdf(state["notes"], pdf_path, date_str, topic)
            print_ok(f"PDF      → [white]{pdf_path.name}[/white]")
            state["pdf_path"] = str(pdf_path)
        except Exception as e:
            print_warn(f"PDF generation failed: {e}")
            log_error("pdf_generation_failed", e, {"audio": state.get("audio_filename", "")})
            pdf_path = None
            state["pdf_path"] = None

        state["md_path"]      = str(md_path)
        state["current_step"] = 8
        save_state(stem, state)

    # Step 8: ORGANISE
    if state["current_step"] <= 8:
        print_section("ORGANISE", 8, 8)
        if audio_path.exists():
            moved = safe_move(audio_path, DIR_COMPLETED)
            print_ok(f"Audio → Completed/{moved.name}")
        else:
            print_info("Audio already moved.")

        if state.get("continuation"):
            cont_path = Path(state["continuation"])
            if cont_path.exists():
                moved2 = safe_move(cont_path, DIR_COMPLETED)
                print_ok(f"Continuation → Completed/{moved2.name}")

        log(f"Processed: {state['audio_filename']} -> {state.get('topic','?')} ({state['meeting_date']})")
        clear_state(stem)
        print_completion_card(state)

        pdf_path = state.get("pdf_path")
        if pdf_path and Path(pdf_path).exists():
            if ask_confirm("Open the PDF?", default=True):
                try:
                    os.startfile(pdf_path)
                except Exception as e:
                    print_warn(f"Could not open PDF: {e}")
                    log_error("pdf_open_failed", e, {"pdf_path": str(pdf_path)})
                    print_info(f"Path: {pdf_path}")

# ---------------------------------------------------------------------------
# Menu options
# ---------------------------------------------------------------------------
def pick_file_gui() -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Select audio file to transcribe",
            filetypes=[("Audio files", "*.m4a *.mp3 *.wav *.ogg *.flac *.aac *.wma *.webm"), ("All files", "*.*")],
        )
        root.destroy()
        return Path(path) if path else None
    except Exception as e:
        print_warn(f"File picker unavailable: {e}")
        log_error("file_picker_failed", e)
        return None

def option_browse():
    print_section("BROWSE FOR FILE")
    p = pick_file_gui()
    if p is None:
        print_info("No file selected. Enter path manually:")
        raw = ask_text("Path:")
        if not raw:
            return
        p = Path(raw)

    if not p.exists():
        print_err(f"File not found: {p}")
        return
    if p.suffix.lower() not in AUDIO_EXTENSIONS:
        print_err(f"Unsupported format: {p.suffix}")
        return

    completed = DIR_COMPLETED / p.name
    if completed.exists():
        print_warn("This file was previously processed.")
        if not ask_confirm("Reprocess?", default=False):
            return
        reason = ask_text("Reason for reprocessing:")
        log(f"Reprocessing: {p.name} | Reason: {reason}")
        shutil.move(str(completed), str(DIR_NOT_TRANSCRIBED / p.name))
        p = DIR_NOT_TRANSCRIBED / p.name
    else:
        dst = DIR_NOT_TRANSCRIBED / p.name
        if not dst.exists():
            shutil.move(str(p), str(dst))
            print_ok(f"Moved to Not Transcribed: {p.name}")
        p = dst

    existing_state = load_state(p.stem)
    if existing_state:
        print_info(f"Checkpoint found at step {existing_state.get('current_step', '?')}.")
        if not ask_confirm("Resume from checkpoint?", default=True):
            clear_state(p.stem)
            existing_state = None

    process_file(p, existing_state)

def option_check_folder():
    print_section("NOT TRANSCRIBED FOLDER")
    files = list_audio_files(DIR_NOT_TRANSCRIBED)
    if not files:
        print_info(f"No audio files in: {DIR_NOT_TRANSCRIBED}")
        return

    print_info(f"{len(files)} file(s) found")
    console.print()

    choices = [
        Choice(
            value=str(f),
            name=f"{f.name}  ({f.stat().st_size/1024/1024:.1f} MB  ·  {datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime('%Y-%m-%d %H:%M')})",
        )
        for f in files
    ]
    picked = ask_select("Select file to process:", choices=choices)
    if not picked:
        return

    p = Path(picked)
    existing_state = load_state(p.stem)
    if existing_state:
        print_info(f"Checkpoint found at step {existing_state.get('current_step', '?')}.")
        if not ask_confirm("Resume from checkpoint?", default=True):
            clear_state(p.stem)
            existing_state = None

    process_file(p, existing_state)

def option_batch():
    print_section("BATCH PROCESS")
    files = list_audio_files(DIR_NOT_TRANSCRIBED)
    if not files:
        print_info(f"No audio files in: {DIR_NOT_TRANSCRIBED}")
        return

    print_info(f"{len(files)} file(s) in queue")
    console.print()

    processed, awaiting_review, skipped = 0, 0, 0
    for f in files:
        console.print(f"  [cyan]───[/cyan]  [white]{f.name}[/white]  [dim]({f.stat().st_size/1024/1024:.1f} MB)[/dim]")
        if not ask_confirm("Process this file?", default=True):
            skipped += 1
            continue

        existing_state = load_state(f.stem)
        if existing_state:
            print_info(f"Checkpoint found at step {existing_state.get('current_step', '?')}.")
            if not ask_confirm("Resume from checkpoint?", default=True):
                clear_state(f.stem)
                existing_state = None

        try:
            result = process_file(f, existing_state)
            if result == "awaiting_review":
                awaiting_review += 1
            else:
                processed += 1
        except KeyboardInterrupt:
            console.print()
            print_warn("Batch interrupted. Progress checkpointed.")
            break
        except Exception as e:
            print_err(f"Failed: {e}")
            log_error("batch_file_failed", e, {"audio": str(f)})
            traceback.print_exc()
            skipped += 1

    console.print()
    print_section("BATCH COMPLETE")
    print_ok(f"Processed: [white]{processed}[/white]")
    if awaiting_review:
        print_info(f"Awaiting review: {awaiting_review}")
    if skipped:
        print_info(f"Skipped:   {skipped}")

def option_resume():
    print_section("RESUME CHECKPOINT")
    all_states = list_incomplete_states()
    states = [s for s in all_states if not s.get("awaiting_review")]
    if not states:
        print_info("No interrupted jobs found.")
        if any(s.get("awaiting_review") for s in all_states):
            print_info("Reviewed jobs are available under 'Finalize reviewed meeting'.")
        return

    print_info(f"{len(states)} interrupted job(s)")
    console.print()

    choices = [
        Choice(
            value=s["_stem"],
            name=f"{s.get('audio_filename','unknown')}  ·  {s.get('meeting_date','?')}  ·  stopped at step {s.get('current_step','?')}",
        )
        for s in states
    ]
    picked_stem = ask_select("Select job to resume:", choices=choices)
    if not picked_stem:
        return

    s     = next(x for x in states if x["_stem"] == picked_stem)
    fname = s.get("audio_filename", "")
    candidates = [DIR_NOT_TRANSCRIBED / fname, DIR_COMPLETED / fname, ROOT / fname]
    audio_path = next((c for c in candidates if c.exists()), DIR_NOT_TRANSCRIBED / fname)

    if not ask_confirm(f"Resume '{fname}' from step {s.get('current_step')}?", default=True):
        return

    process_file(audio_path, load_state(picked_stem))

def option_finalize_review():
    print_section("FINALIZE REVIEWED MEETING")
    states = [s for s in list_incomplete_states() if s.get("awaiting_review")]
    if not states:
        print_info("No meetings are awaiting review.")
        return

    choices = [
        Choice(
            value=s["_stem"],
            name=(f"{s.get('audio_filename','unknown')}  ·  "
                  f"{s.get('meeting_date','?')}  ·  review ready"),
        )
        for s in states
    ]
    picked_stem = ask_select("Select reviewed meeting:", choices=choices)
    if not picked_stem:
        return

    state = next(s for s in states if s["_stem"] == picked_stem)
    review_path = Path(state.get("review_path") or "")
    if not review_path.is_file():
        print_err(f"Review file not found: {review_path}")
        return

    try:
        reviewed = parse_review_markdown(review_path)
    except Exception as e:
        print_err(f"Review file could not be read: {e}")
        print_info("Fix the reported line or restore the required headings, then try again.")
        log_error("review_parse_failed", e, {"review_path": str(review_path)})
        return

    print_ok(f"Review loaded: [white]{len(reviewed['transcript'])} segments[/white]")
    print_info(f"Meeting date: {reviewed['meeting_date']}")
    if reviewed["has_speakers"]:
        names = sorted({seg["speaker"] for seg in reviewed["transcript"]})
        print_info("Speakers: " + ", ".join(names))
    if reviewed["notes_instructions"]:
        print_info("Custom notes instructions found.")
    console.print()
    if not ask_confirm("Approve this review and send the transcript text to Claude?", default=False):
        print_info("Nothing was sent. You can continue editing the review file.")
        return

    state.update(reviewed)
    state["awaiting_review"] = False
    state["current_step"] = 6
    state["notes"] = None
    state["topic"] = None
    state["token_in"] = 0
    state["token_out"] = 0
    state["cost"] = 0.0
    save_state(picked_stem, state)
    save_tx_archive(picked_stem, state)

    fname = state.get("audio_filename", "")
    candidates = [DIR_NOT_TRANSCRIBED / fname, DIR_COMPLETED / fname, ROOT / fname]
    audio_path = next((candidate for candidate in candidates if candidate.exists()), None)
    if audio_path is None:
        print_err(f"Audio file not found: {fname}")
        print_info("Restore it to Recordings/Not Transcribed and try again.")
        state["awaiting_review"] = True
        save_state(picked_stem, state)
        return

    process_file(audio_path, state)

def option_reprocess():
    print_section("REPROCESS FROM STEP")
    archives = list_tx_archives()
    if not archives:
        print_info("No transcript archives found.")
        print_info("Archives are saved automatically after transcription on future runs.")
        return

    print_info(f"{len(archives)} transcript archive(s)")
    console.print()

    choices = [
        Choice(
            value=a["_stem"],
            name=f"{a.get('audio_filename','unknown')}  ·  {a.get('meeting_date','?')}  ·  {int(a.get('audio_duration',0))//60}m",
        )
        for a in archives
    ]
    picked_stem = ask_select("Select transcript archive:", choices=choices)
    if not picked_stem:
        return

    a    = next(x for x in archives if x["_stem"] == picked_stem)
    fname = a.get("audio_filename", "")

    step_choices = [
        Choice(value="4", name="Speaker ID  (re-runs diarization → review → summarise → save)"),
        Choice(value="6", name="Summarise   (uses archived transcript, re-generates notes + PDF)"),
    ]
    restart_step_str = ask_select("Restart from which step?", choices=step_choices)
    if not restart_step_str:
        return
    restart_step = int(restart_step_str)

    archived_has_speakers = any(
        (seg.get("speaker_id") or seg.get("speaker")) != "TRANSCRIPT"
        and bool(seg.get("speaker"))
        for seg in (a.get("transcript") or [])
    )
    archived_speaker_count = len({
        seg.get("speaker") for seg in (a.get("transcript") or [])
        if seg.get("speaker")
    }) if archived_has_speakers else 0

    state = {
        "current_step":   restart_step,
        "audio_filename": a["audio_filename"],
        "meeting_date":   a["meeting_date"],
        "transcript":     a["transcript"],
        "audio_duration": a.get("audio_duration", 0),
        "has_speakers":   archived_has_speakers if restart_step == 6 else False,
        "speaker_count":  archived_speaker_count if restart_step == 6 else 0,
        "notes":          None,
        "topic":          None,
        "token_in":       0,
        "token_out":      0,
        "cost":           0.0,
        "user_context":   a.get("user_context", ""),
        "notes_instructions": a.get("notes_instructions", "") or "",
        "continuation":   a.get("continuation"),
        "awaiting_review": False,
        "review_path":    None,
        "md_path":        None,
        "pdf_path":       None,
        "transcript_path": None,
    }

    candidates = [DIR_COMPLETED / fname, DIR_NOT_TRANSCRIBED / fname, ROOT / fname]
    audio_path = next((c for c in candidates if c.exists()), None)
    if audio_path is None:
        print_warn(f"Audio file not found: {fname}")
        print_info("Move the file back to 'Recordings/Not Transcribed/' and try again.")
        return

    if not ask_confirm(f"Restart from step {restart_step} for '{fname}'?", default=True):
        return

    save_state(picked_stem, state)
    process_file(audio_path, state)

# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------
def print_main_menu():
    spk_label = "[green]ENABLED[/green]" if HF_TOKEN else "[dim]DISABLED[/dim]"
    model_tag = CLAUDE_MODEL.split("-")[1].upper() if "-" in CLAUDE_MODEL else CLAUDE_MODEL.upper()
    console.print(Panel(
        f"[dim]SPEAKER ID:[/dim] {spk_label}   [dim]WHISPER:[/dim] [white]{WHISPER_MODEL}[/white]   [dim]CLAUDE:[/dim] [white]{model_tag}[/white]",
        title="[bold blue]LOCALPLAUD[/bold blue]  [dim]·  private meeting transcription + summarisation[/dim]",
        border_style="blue",
        padding=(0, 2),
    ))
    console.print()

def main():
    startup_animation()

    menu_choices = [
        Choice(value="1", name="Browse for a file         (opens file picker)"),
        Choice(value="2", name="Scan Not Transcribed folder"),
        Choice(value="3", name="Batch process queue"),
        Choice(value="4", name="Resume checkpoint"),
        Choice(value="5", name="Finalize reviewed meeting"),
        Choice(value="6", name="Reprocess from step       (redo speaker ID or summary)"),
        Choice(value="q", name="Quit"),
    ]

    while True:
        print_main_menu()
        choice = ask_select("What would you like to do?", choices=menu_choices)
        if choice is None or choice == "q":
            break

        if choice == "1":
            option_browse()
        elif choice == "2":
            option_check_folder()
        elif choice == "3":
            option_batch()
        elif choice == "4":
            option_resume()
        elif choice == "5":
            option_finalize_review()
        elif choice == "6":
            option_reprocess()

        console.print()
        if not ask_confirm("Process another file?", default=True):
            break

    console.print()
    console.print("  [dim]Goodbye.[/dim]")
    console.print()

if __name__ == "__main__":
    if "--doctor" in sys.argv:
        sys.exit(run_doctor())
    if not ANTHROPIC_API_KEY:
        console.print("\n  [bold red]✗[/bold red]  ANTHROPIC_API_KEY not set.")
        console.print("  [dim]       Run install.bat or add your key to .env[/dim]\n")
        sys.exit(1)
    main()
