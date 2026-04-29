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
for _d in [DIR_NOT_TRANSCRIBED, DIR_COMPLETED, DIR_MARKDOWN, DIR_PDF, DIR_STATE]:
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
    archive = {k: state[k] for k in (
        "audio_filename", "meeting_date", "audio_duration",
        "transcript", "user_context", "continuation",
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
    print_section("TRANSCRIBE", 3, 7)

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
def _find_contextual_excerpt(labelled: list[dict], target: str, window: int = 14) -> list[dict]:
    UNNAMED = re.compile(r"^SPEAKER_\d+$", re.IGNORECASE)
    appearances = [i for i, s in enumerate(labelled) if s["speaker"] == target]
    if not appearances:
        return []
    best_start, best_score = max(0, appearances[0] - window // 2), -1
    for idx in appearances:
        start = max(0, idx - window // 2)
        end   = min(len(labelled), start + window)
        score = len({s["speaker"] for s in labelled[start:end] if s["speaker"] != target and not UNNAMED.match(s["speaker"])})
        if score > best_score:
            best_score, best_start = score, start
    return labelled[best_start : min(len(labelled), best_start + window)]

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
    print_section("SPEAKER ID", 4, 7)

    from pyannote.audio import Pipeline
    import torchaudio
    import torch

    with Spinner("Loading pyannote speaker model  (first run ~1 GB download)"):
        try:
            pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", token=HF_TOKEN)
        except TypeError:
            pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=HF_TOKEN)

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

    labelled     = [{**seg, "speaker": best_speaker(seg["start"], seg["end"])} for seg in segments]
    all_speakers = sorted({s["speaker"] for s in labelled})
    speaker_color = {spk: SPEAKER_COLORS[i % len(SPEAKER_COLORS)] for i, spk in enumerate(all_speakers)}

    spk_tags = "  ".join(f"[{speaker_color[s]}]{s}[/]" for s in all_speakers)
    print_ok(f"Detected [white]{len(all_speakers)} speaker(s)[/white]: {spk_tags}")
    console.print()

    mid = len(labelled) // 2
    excerpt = labelled[max(0, mid - 15) : min(len(labelled), mid + 15)]
    console.print(Panel(
        _render_excerpt(excerpt, speaker_color),
        title="[bold cyan]TRANSCRIPT EXCERPT[/bold cyan]",
        subtitle="[dim]middle of recording — all speakers shown[/dim]",
        border_style="dim",
        padding=(0, 1),
    ))
    console.print()

    console.print("  [bold cyan]NAME SPEAKERS[/bold cyan]  [dim]Press Enter to keep the label as-is[/dim]")
    console.print()

    name_map = {}
    for spk in all_speakers:
        color = speaker_color.get(spk, "white")
        console.print(f"  [{color}]▶  {spk}[/]")
        ans = ask_text("Name this speaker (Enter to keep):")
        name_map[spk] = ans if ans else spk

    for seg in labelled:
        seg["speaker"] = name_map.get(seg["speaker"], seg["speaker"])
    speaker_color = {name_map.get(old, old): col for old, col in speaker_color.items()}

    UNNAMED = re.compile(r"^SPEAKER_\d+$", re.IGNORECASE)
    unidentified = sorted({s["speaker"] for s in labelled if UNNAMED.match(s["speaker"])})

    if unidentified:
        console.print()
        print_info(f"{len(unidentified)} speaker(s) still unidentified.")
        print_info("Showing contextual excerpts — each alongside people you've already named.")

        for spk in unidentified:
            color   = speaker_color.get(spk, "magenta")
            ctx     = _find_contextual_excerpt(labelled, spk, window=14)
            console.print()
            console.print(Panel(
                _render_excerpt(ctx, speaker_color),
                title=f"[yellow]UNIDENTIFIED:[/yellow]  [{color}]{spk}[/]",
                border_style="yellow",
                padding=(0, 1),
            ))
            console.print()
            console.print(f"  [{color}]▶  {spk}[/]")
            ans      = ask_text(f"Name this speaker (Enter to keep as '{spk}'):")
            new_name = ans.strip() if ans.strip() else spk
            if new_name != spk:
                for seg in labelled:
                    if seg["speaker"] == spk:
                        seg["speaker"] = new_name
                speaker_color[new_name] = speaker_color.pop(spk, color)
                print_ok(f"[{color}]{spk}[/]  →  [white]{new_name}[/white]")

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
                ts = f"[{int(seg['start']//60):02d}:{int(seg['start']%60):02d}]"
                lines.append(f"\n{spk} {ts}:")
                prev_speaker = spk
            lines.append(f"  {seg['text']}")
        else:
            lines.append(seg["text"])
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Claude summarisation
# ---------------------------------------------------------------------------
CLAUDE_PROMPT = """\
You are an expert meeting notes assistant like Otter.ai or Plaud.
You will receive the full transcript of a business meeting. Your job is to
produce comprehensive, structured meeting notes that capture EVERYTHING discussed.

{context_section}

{user_context_section}

CRITICAL RULES:
- Cover ALL topics discussed -- do not skip any topic, even if it was brief
- Use real names, companies, numbers, and dates from the transcript
- Preserve proper nouns EXACTLY as spoken -- do not rename companies or people
- Distinguish between "discussed" vs "decided" vs "suggested"
- Do NOT invent anything not present in the transcript
- Be thorough -- it is better to include too much than to miss something
{speaker_rules}

IMPORTANT: Your response MUST begin with this exact line:
TOPIC: [2-4 word topic in kebab-case, e.g. Strategy-Session, Client-Call-Franke, Weekly-Standup]

Then use EXACTLY these Markdown sections in this order:

## Meeting Type
Identify: Strategy Session / Standup / Client Call / Brainstorm / 1:1 / Other

## TL;DR
Maximum 3 lines. The absolute essence of this meeting.

## Action Items
Checkbox format, sorted by priority:
{action_format}
If none identified, write "No action items identified."

## Decisions Made
Bullet list of concrete decisions with brief rationale.
If none: "No explicit decisions recorded."

## Key Discussion Topics
Group related items into sub-headings with 2-5 bullet points each:
### [Topic Name]
- Specific detail using real names, companies, and numbers

## Open Questions / Parking Lot
Items raised but not resolved or explicitly deferred.

## Summary
4-6 sentence chronological narrative. Write so someone who wasn't there fully understands.

## Key Quotes & Insights
Notable statements that capture sentiment or important positions:
{quotes_format}

## Follow-Up Email Draft
Professional email (3-4 paragraphs) to send to all attendees.
Start with "Hi team," and end with "Best regards".

TRANSCRIPT:
{transcript}
"""

def build_prompt(transcript_text: str, has_speakers: bool, user_context: str) -> str:
    context_section = ""
    if FILE_CONTEXT.exists():
        ctx = FILE_CONTEXT.read_text(encoding="utf-8").strip()
        if ctx:
            context_section = f"BUSINESS CONTEXT:\n{ctx}"

    user_context_section = ""
    if user_context.strip():
        user_context_section = f"MEETING-SPECIFIC CONTEXT:\n{user_context.strip()}"

    if has_speakers:
        speaker_rules = "- Attribute action items and quotes to specific speakers"
        action_format = "- [ ] Person: Task -- due Date"
        quotes_format = '"Quote" -- Speaker Name'
    else:
        speaker_rules = "- Do NOT assign action items to specific people (speakers not identified)"
        action_format = "- [ ] Task -- due Date (no person)"
        quotes_format = '"Quote" (no speaker attribution)'

    return CLAUDE_PROMPT.format(
        context_section=context_section,
        user_context_section=user_context_section,
        speaker_rules=speaker_rules,
        action_format=action_format,
        quotes_format=quotes_format,
        transcript=transcript_text,
    )

def summarise(segments: list[dict], has_speakers: bool, user_context: str) -> tuple[str, str, int, int, float]:
    print_section("SUMMARISE", 5, 7)
    print_info(f"Model: [white]{CLAUDE_MODEL}[/white]")

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    transcript_text = format_transcript(segments, has_speakers)
    prompt          = build_prompt(transcript_text, has_speakers, user_context)

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
            "continuation":   None,
            "md_path":        None,
            "pdf_path":       None,
        }

    # Step 1: VALIDATE
    if state["current_step"] <= 1:
        print_section("VALIDATE", 1, 7)
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
        print_section("PREPARE", 2, 7)

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

    # Step 5: SUMMARISE
    if state["current_step"] <= 5:
        if state["notes"] is None:
            (state["notes"], state["topic"],
             state["token_in"], state["token_out"],
             state["cost"]) = summarise(state["transcript"], state["has_speakers"], state["user_context"])
        state["current_step"] = 6
        save_state(stem, state)

    # Step 6: SAVE OUTPUT
    if state["current_step"] <= 6:
        print_section("SAVE OUTPUT", 6, 7)
        date_str = state["meeting_date"]
        topic    = state["topic"]
        base     = f"{date_str}_Work_MM_{topic}"
        md_path  = unique_path(DIR_MARKDOWN, base, ".md")
        pdf_path = unique_path(DIR_PDF,      base, ".pdf")

        md_path.write_text(state["notes"], encoding="utf-8")
        print_ok(f"Markdown → [white]{md_path.name}[/white]")

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
        state["current_step"] = 7
        save_state(stem, state)

    # Step 7: ORGANISE
    if state["current_step"] <= 7:
        print_section("ORGANISE", 7, 7)
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

    processed, skipped = 0, 0
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
            process_file(f, existing_state)
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
    if skipped:
        print_info(f"Skipped:   {skipped}")

def option_resume():
    print_section("RESUME CHECKPOINT")
    states = list_incomplete_states()
    if not states:
        print_info("No interrupted jobs found.")
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
        Choice(value="4", name="Speaker ID  (re-runs diarization → summarise → save)"),
        Choice(value="5", name="Summarise   (skips diarization, re-generates notes + PDF)"),
    ]
    restart_step_str = ask_select("Restart from which step?", choices=step_choices)
    if not restart_step_str:
        return
    restart_step = int(restart_step_str)

    state = {
        "current_step":   restart_step,
        "audio_filename": a["audio_filename"],
        "meeting_date":   a["meeting_date"],
        "transcript":     a["transcript"],
        "audio_duration": a.get("audio_duration", 0),
        "has_speakers":   False,
        "speaker_count":  0,
        "notes":          None,
        "topic":          None,
        "token_in":       0,
        "token_out":      0,
        "cost":           0.0,
        "user_context":   a.get("user_context", ""),
        "continuation":   a.get("continuation"),
        "md_path":        None,
        "pdf_path":       None,
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
        Choice(value="5", name="Reprocess from step       (redo speaker ID or summary)"),
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
