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
import threading
from pathlib import Path

# Enable ANSI colour codes on Windows
if sys.platform == "win32":
    os.system("")

# ---------------------------------------------------------------------------
# Bootstrap: load .env before anything else
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

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

AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".ogg", ".flac", ".aac", ".wma", ".webm"}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HF_TOKEN          = os.environ.get("HF_TOKEN", "")
WHISPER_MODEL     = os.environ.get("WHISPER_MODEL", "turbo")
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

HAIKU_COST_IN   = 0.80  / 1_000_000
HAIKU_COST_OUT  = 4.00  / 1_000_000
SONNET_COST_IN  = 3.00  / 1_000_000
SONNET_COST_OUT = 15.00 / 1_000_000

# ---------------------------------------------------------------------------
# Colour / terminal system
# ---------------------------------------------------------------------------
IS_TTY = sys.stdout.isatty()

class C:
    BLUE    = "\033[94m"  if IS_TTY else ""
    CYAN    = "\033[96m"  if IS_TTY else ""
    WHITE   = "\033[97m"  if IS_TTY else ""
    DIM     = "\033[2m"   if IS_TTY else ""
    GREEN   = "\033[92m"  if IS_TTY else ""
    RED     = "\033[91m"  if IS_TTY else ""
    YELLOW  = "\033[93m"  if IS_TTY else ""
    MAGENTA = "\033[95m"  if IS_TTY else ""
    BOLD    = "\033[1m"   if IS_TTY else ""
    RESET   = "\033[0m"   if IS_TTY else ""

# Palette assigned to speakers in order
SPEAKER_PALETTE = [C.CYAN, C.YELLOW, C.MAGENTA, C.GREEN, C.BLUE + C.BOLD, C.RED]

def tw() -> int:
    """Terminal width, clamped to sensible range."""
    try:
        return min(max(os.get_terminal_size().columns, 60), 120)
    except Exception:
        return 72

def hr(char: str = "─", color: str = "") -> str:
    return color + char * (tw() - 2) + C.RESET

def print_ok(msg: str):
    print(f"  {C.GREEN}[OK]{C.RESET}   {msg}")

def print_err(msg: str):
    print(f"  {C.RED}[ERR]{C.RESET}  {msg}")

def print_warn(msg: str):
    print(f"  {C.YELLOW}[WARN]{C.RESET} {msg}")

def print_info(msg: str):
    print(f"  {C.DIM}[INFO]{C.RESET} {msg}")

def print_section(title: str, step: int = 0, total: int = 0):
    print()
    w = tw()
    if step and total:
        left  = f"  {C.DIM}──{C.RESET} {C.BOLD}{C.CYAN}[ {step:02d} / {total:02d} ]  {title}{C.RESET} "
        vlen  = len(f"  ── [ {step:02d} / {total:02d} ]  {title} ")
    else:
        left  = f"  {C.BOLD}{C.CYAN}{title}{C.RESET} "
        vlen  = len(f"  {title} ")
    rule_len = max(w - vlen - 2, 4)
    print(left + C.DIM + "─" * rule_len + C.RESET)
    print()

def make_progress_bar(current: float, total: float, width: int = 24) -> str:
    pct    = min(1.0, current / total) if total > 0 else 0
    filled = int(width * pct)
    bar    = "█" * filled + "░" * (width - filled)
    return f"{C.BLUE}[{bar}]{C.RESET} {C.WHITE}{pct*100:5.1f}%{C.RESET}"

# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------
class Spinner:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, message: str):
        self.message  = message
        self._stop    = threading.Event()
        self._thread  = None
        self._start   = None

    def _run(self):
        i = 0
        while not self._stop.is_set():
            elapsed = time.time() - self._start
            frame   = self.FRAMES[i % len(self.FRAMES)]
            line    = f"  {C.CYAN}{frame}{C.RESET}  {self.message}  {C.DIM}({elapsed:.0f}s){C.RESET}"
            print(f"\r{line:<{tw()}}", end="", flush=True)
            i += 1
            time.sleep(0.1)

    def __enter__(self):
        if not IS_TTY:
            print(f"  {self.message}...")
            return self
        self._start  = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        if self._thread:
            self._thread.join()
        if IS_TTY:
            print(f"\r{' ' * tw()}\r", end="", flush=True)

# ---------------------------------------------------------------------------
# Startup animation
# ---------------------------------------------------------------------------
LOGO_LINES = [
    " _                    _ ____  _                 _",
    "| |    ___   ___ __ _| |  _ \\| | __ _ _   _  __| |",
    "| |   / _ \\ / __/ _` | |_) | |/ _` | | | |/ _` |",
    "| |__| (_) | (_| (_| |  __/| | (_| | |_| | (_| |",
    "|_____\\___/ \\___\\__,_|_|   |_|\\__,_|\\__,_|\\__,_|",
]

def startup_animation():
    print()
    for line in LOGO_LINES:
        print(f"  {C.BLUE}{C.BOLD}", end="", flush=True)
        if IS_TTY:
            for ch in line:
                print(ch, end="", flush=True)
                time.sleep(0.004)
        else:
            print(line, end="")
        print(C.RESET)

    print()
    tagline = "  Private Meeting Notes  ·  Audio stays local"
    if IS_TTY:
        print(C.DIM, end="")
        for ch in tagline:
            print(ch, end="", flush=True)
            time.sleep(0.012)
        print(C.RESET)
    else:
        print(tagline)
    print()
    time.sleep(0.15)

# ---------------------------------------------------------------------------
# Ensure folders exist
# ---------------------------------------------------------------------------
for _d in [DIR_NOT_TRANSCRIBED, DIR_COMPLETED, DIR_MARKDOWN, DIR_PDF, DIR_STATE]:
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
def log(msg: str):
    ts   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"[{ts}] {msg}"
    print(f"  {C.DIM}{line}{C.RESET}")
    try:
        with open(FILE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def safe_input(prompt: str, buffer: bool = False) -> str:
    """input() with optional pre-sleep to absorb leftover newlines."""
    if buffer:
        time.sleep(0.3)
        print()
    try:
        return input(f"  {C.YELLOW}{prompt}{C.RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""

# ---------------------------------------------------------------------------
# Checkpoint / resume
# ---------------------------------------------------------------------------
def state_path(stem: str) -> Path:
    return DIR_STATE / f"{stem}.json"

def save_state(stem: str, state: dict):
    p = state_path(stem)
    with open(p, "w", encoding="utf-8") as f:
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
        try:
            with open(p, "r", encoding="utf-8") as f:
                s = json.load(f)
            s["_state_file"] = str(p)
            s["_stem"]       = p.stem
            states.append(s)
        except Exception:
            pass
    return states

# ---------------------------------------------------------------------------
# File utilities
# ---------------------------------------------------------------------------
def list_audio_files(folder: Path) -> list[Path]:
    files = []
    for p in sorted(folder.iterdir()):
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
            files.append(p)
    return files

def file_info(p: Path) -> str:
    size_mb = p.stat().st_size / (1024 * 1024)
    mtime   = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    return f"{p.name}  {C.DIM}({size_mb:.1f} MB  ·  {mtime}){C.RESET}"

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
            y, mo, d = m.group(1), m.group(2), m.group(3)
            try:
                dt = datetime.date(int(y), int(mo), int(d))
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
    mtime = datetime.datetime.fromtimestamp(audio_path.stat().st_mtime)
    return mtime.strftime("%Y-%m-%d")

# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------
def transcribe(audio_path: Path) -> tuple[list[dict], float]:
    """Returns (segments, audio_duration_seconds)."""
    print_section("TRANSCRIBE", 3, 7)

    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        device = "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    print_info(f"Model: {C.WHITE}{WHISPER_MODEL}{C.RESET}  ·  Device: {C.WHITE}{device.upper()} / {compute_type}{C.RESET}")
    print_info(f"File:  {C.WHITE}{audio_path.name}{C.RESET}")
    print()

    from faster_whisper import WhisperModel

    with Spinner("Loading Whisper model"):
        model = WhisperModel(WHISPER_MODEL, device=device, compute_type=compute_type)
    print_ok("Whisper model loaded")
    print()

    segments_iter, info = model.transcribe(
        str(audio_path),
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    duration = info.duration
    print_info(f"Duration: {C.WHITE}{duration/60:.1f} min{C.RESET}  ·  Language: {C.WHITE}{info.language}{C.RESET}")
    print()

    segments_out = []
    count        = 0

    if IS_TTY:
        # Reserve 2 lines for live display
        print(f"  {C.DIM}Waiting for first segment...{C.RESET}")
        print()

    for seg in segments_iter:
        segments_out.append({
            "start": round(seg.start, 2),
            "end":   round(seg.end,   2),
            "text":  seg.text.strip(),
        })
        count += 1

        if IS_TTY:
            pb       = make_progress_bar(seg.end, duration, 26)
            t_str    = f"{seg.end/60:.1f} / {duration/60:.1f} min  ·  {count} seg"
            text_snip = seg.text.strip()
            max_text  = tw() - 8
            if len(text_snip) > max_text:
                text_snip = text_snip[:max_text - 1] + "…"
            text_line = f"  {C.DIM}› {text_snip}{C.RESET}"
            prog_line = f"  {pb}  {C.DIM}{t_str}{C.RESET}"
            # Move up 2 lines and overwrite
            sys.stdout.write(f"\033[2A\033[2K{text_line}\n\033[2K{prog_line}\n")
            sys.stdout.flush()
        elif count % 50 == 0:
            print(f"  ... {count} segments  ·  {seg.end:.0f}s processed")

    if IS_TTY:
        print()  # clear past the progress block

    print()
    print_ok(f"Transcription complete  ·  {C.WHITE}{count} segments{C.RESET}  ·  {C.WHITE}{duration/60:.1f} min{C.RESET}")
    return segments_out, duration

# ---------------------------------------------------------------------------
# Diarization helpers
# ---------------------------------------------------------------------------
def _find_contextual_excerpt(labelled: list[dict], target: str, window: int = 14) -> list[dict]:
    """Return the segment window where target speaker appears with the most OTHER named speakers."""
    UNNAMED = re.compile(r"^SPEAKER_\d+$", re.IGNORECASE)
    appearances = [i for i, s in enumerate(labelled) if s["speaker"] == target]
    if not appearances:
        return []

    best_start = max(0, appearances[0] - window // 2)
    best_score = -1

    for idx in appearances:
        start = max(0, idx - window // 2)
        end   = min(len(labelled), start + window)
        named_others = {
            s["speaker"] for s in labelled[start:end]
            if s["speaker"] != target and not UNNAMED.match(s["speaker"])
        }
        if len(named_others) > best_score:
            best_score = len(named_others)
            best_start = start

    return labelled[best_start : min(len(labelled), best_start + window)]

def _show_excerpt(excerpt: list[dict], speaker_color: dict, w: int):
    """Print a coloured chat-style excerpt block."""
    prev_s = None
    for seg in excerpt:
        s_name = seg["speaker"]
        s_col  = speaker_color.get(s_name, C.WHITE)
        ts     = f"{C.DIM}[{int(seg['start']//60):02d}:{int(seg['start']%60):02d}]{C.RESET}"
        text   = seg["text"]
        max_t  = w - 26
        if len(text) > max_t:
            text = text[:max_t - 1] + "…"
        if s_name != prev_s:
            print()
            print(f"  {ts}  {s_col}{C.BOLD}{s_name:<14}{C.RESET}")
            prev_s = s_name
        print(f"  {'':18}  {text}")

# ---------------------------------------------------------------------------
# Diarization
# ---------------------------------------------------------------------------
def diarize(audio_path: Path, segments: list[dict]) -> tuple[list[dict], int]:
    """Returns (labelled_segments, speaker_count)."""
    print_section("SPEAKER ID", 4, 7)

    from pyannote.audio import Pipeline
    import torchaudio
    import torch

    with Spinner("Loading pyannote speaker model  (first run ~1 GB download)"):
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=HF_TOKEN,
        )

    try:
        if torch.cuda.is_available():
            pipeline = pipeline.to(torch.device("cuda"))
    except Exception:
        pass

    print_ok("Speaker model loaded")
    print()

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
    print()

    with Spinner("Running diarization"):
        diarization = pipeline({"waveform": waveform, "sample_rate": sample_rate})

    # Build speaker turns — unwrap DiarizeOutput if newer pyannote version
    annotation = diarization.diarization if hasattr(diarization, "diarization") else diarization
    turns = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        turns.append({"start": turn.start, "end": turn.end, "speaker": speaker})

    def best_speaker(seg_start: float, seg_end: float) -> str:
        best, best_overlap = None, 0.0
        for t in turns:
            overlap = min(seg_end, t["end"]) - max(seg_start, t["start"])
            if overlap > best_overlap:
                best_overlap = overlap
                best = t["speaker"]
        return best or "UNKNOWN"

    labelled = [{**seg, "speaker": best_speaker(seg["start"], seg["end"])} for seg in segments]

    all_speakers   = sorted({s["speaker"] for s in labelled})
    speaker_color  = {spk: SPEAKER_PALETTE[i % len(SPEAKER_PALETTE)] for i, spk in enumerate(all_speakers)}

    print_ok(f"Detected {C.WHITE}{len(all_speakers)} speaker(s){C.RESET}: "
             + "  ".join(f"{speaker_color[s]}{s}{C.RESET}" for s in all_speakers))
    print()

    # --- Show 30-segment excerpt from middle of recording ---
    w         = tw()
    mid_idx   = len(labelled) // 2
    start_idx = max(0, mid_idx - 15)
    end_idx   = min(len(labelled), mid_idx + 15)
    excerpt   = labelled[start_idx:end_idx]

    print(f"  {C.DIM}┌{'─' * (w - 6)}┐{C.RESET}")
    print(f"  {C.DIM}│{C.RESET}  {C.BOLD}{C.CYAN}TRANSCRIPT EXCERPT{C.RESET}  {C.DIM}(middle of recording — all speakers shown){C.RESET}")
    print(f"  {C.DIM}└{'─' * (w - 6)}┘{C.RESET}")
    print()

    prev_speaker = None
    for seg in excerpt:
        spk   = seg["speaker"]
        color = speaker_color.get(spk, C.WHITE)
        ts    = f"{C.DIM}[{int(seg['start']//60):02d}:{int(seg['start']%60):02d}]{C.RESET}"
        text  = seg["text"]
        max_t = w - 26
        if len(text) > max_t:
            text = text[:max_t - 1] + "…"
        if spk != prev_speaker:
            print()
            print(f"  {ts}  {color}{C.BOLD}{spk:<14}{C.RESET}")
            prev_speaker = spk
        print(f"  {'':18}  {text}")

    print()
    print(f"  {C.DIM}{'─' * (w - 4)}{C.RESET}")
    print()

    # --- Round 1: name each speaker from the middle excerpt ---
    print(f"  {C.BOLD}{C.CYAN}NAME SPEAKERS{C.RESET}  {C.DIM}Press Enter to keep the label as-is{C.RESET}")
    print()

    name_map = {}
    for spk in all_speakers:
        color = speaker_color.get(spk, C.WHITE)
        ans = safe_input(f"Who is {color}{C.BOLD}{spk}{C.RESET}? (Enter to keep):", buffer=True)
        name_map[spk] = ans if ans else spk

    # Apply round-1 names
    for seg in labelled:
        seg["speaker"] = name_map.get(seg["speaker"], seg["speaker"])

    # Rebuild color map with new names
    speaker_color = {name_map.get(old, old): col for old, col in speaker_color.items()}

    # --- Round 2: catch speakers still labelled SPEAKER_XX ---
    UNNAMED = re.compile(r"^SPEAKER_\d+$", re.IGNORECASE)
    unidentified = sorted({s["speaker"] for s in labelled if UNNAMED.match(s["speaker"])})

    if unidentified:
        print()
        print(f"  {C.YELLOW}[INFO]{C.RESET}  {len(unidentified)} speaker(s) still unidentified.")
        print(f"  {C.DIM}Showing contextual excerpts — each unidentified speaker alongside people you've already named.{C.RESET}")

        for spk in unidentified:
            color   = speaker_color.get(spk, C.MAGENTA)
            excerpt = _find_contextual_excerpt(labelled, spk, window=14)
            w       = tw()

            print()
            print(f"  {C.YELLOW}┌{'─' * (w - 6)}┐{C.RESET}")
            print(f"  {C.YELLOW}│{C.RESET}  {C.BOLD}{C.YELLOW}UNIDENTIFIED:{C.RESET}  {color}{C.BOLD}{spk}{C.RESET}")
            print(f"  {C.YELLOW}└{'─' * (w - 6)}┘{C.RESET}")
            print()

            _show_excerpt(excerpt, speaker_color, w)

            print()
            print(f"  {C.DIM}{'─' * (w - 4)}{C.RESET}")
            print()

            ans      = safe_input(f"Who is {color}{C.BOLD}{spk}{C.RESET}? (Enter to keep as '{spk}'):", buffer=True)
            new_name = ans.strip() if ans.strip() else spk

            if new_name != spk:
                for seg in labelled:
                    if seg["speaker"] == spk:
                        seg["speaker"] = new_name
                old_color = speaker_color.pop(spk, color)
                speaker_color[new_name] = old_color
                print_ok(f"{color}{spk}{C.RESET}  →  {C.WHITE}{new_name}{C.RESET}")

    print()
    final_speakers = sorted({s["speaker"] for s in labelled})
    print_ok(
        f"Speaker labels finalised  ·  {C.WHITE}{len(final_speakers)} speaker(s){C.RESET}: "
        + "  ".join(f"{speaker_color.get(s, C.WHITE)}{s}{C.RESET}" for s in final_speakers)
    )
    return labelled, len(final_speakers)

# ---------------------------------------------------------------------------
# Transcript formatting
# ---------------------------------------------------------------------------
def format_transcript(segments: list[dict], has_speakers: bool) -> str:
    lines        = []
    prev_speaker = None
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
    """Returns (notes_markdown, topic_slug, token_in, token_out, cost)."""
    print_section("SUMMARISE", 5, 7)
    print_info(f"Model: {C.WHITE}{CLAUDE_MODEL}{C.RESET}")

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    transcript_text = format_transcript(segments, has_speakers)
    prompt          = build_prompt(transcript_text, has_speakers, user_context)

    print_info(f"Transcript: {C.WHITE}{len(transcript_text):,} chars{C.RESET}  ·  Prompt: {C.WHITE}{len(prompt):,} chars{C.RESET}")
    print()

    with Spinner("Claude is reading the transcript"):
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

    response_text = message.content[0].text
    in_tokens     = message.usage.input_tokens
    out_tokens    = message.usage.output_tokens

    if "haiku" in CLAUDE_MODEL:
        cost = in_tokens * HAIKU_COST_IN + out_tokens * HAIKU_COST_OUT
    else:
        cost = in_tokens * SONNET_COST_IN + out_tokens * SONNET_COST_OUT

    print_ok(f"Response received  ·  "
             f"{C.WHITE}{in_tokens:,} in / {out_tokens:,} out{C.RESET}  ·  "
             f"{C.GREEN}~${cost:.4f}{C.RESET}")

    # Extract TOPIC from first line
    lines      = response_text.strip().splitlines()
    topic_slug = "Meeting-Notes"
    notes      = response_text

    if lines and lines[0].upper().startswith("TOPIC:"):
        raw_topic  = lines[0].split(":", 1)[1].strip()
        topic_slug = re.sub(r"[^a-zA-Z0-9\-]", "", raw_topic.replace(" ", "-"))
        if not topic_slug:
            topic_slug = "Meeting-Notes"
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
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle,
    )
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER

    width, height = A4
    margin = 20 * mm

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin,  bottomMargin=margin,
    )

    def c(rgb):
        return colors.Color(*rgb)

    style_title    = ParagraphStyle("title",    fontSize=20, leading=26, textColor=c(NAVY), spaceAfter=4,  fontName="Helvetica-Bold")
    style_subtitle = ParagraphStyle("subtitle", fontSize=11, leading=14, textColor=c(BLUE), spaceAfter=10, fontName="Helvetica")
    style_h2       = ParagraphStyle("h2",       fontSize=13, leading=17, textColor=c(NAVY), spaceBefore=14, spaceAfter=4, fontName="Helvetica-Bold")
    style_h3       = ParagraphStyle("h3",       fontSize=11, leading=14, textColor=c(BLUE), spaceBefore=8,  spaceAfter=3, fontName="Helvetica-Bold")
    style_body     = ParagraphStyle("body",     fontSize=10, leading=14, textColor=c(BLACK), spaceAfter=3,  fontName="Helvetica")
    style_bullet   = ParagraphStyle("bullet",   fontSize=10, leading=14, textColor=c(BLACK), spaceAfter=2,  leftIndent=14, fontName="Helvetica")
    style_footer   = ParagraphStyle("footer",   fontSize=8,  leading=10, textColor=c(DGREY), spaceAfter=0,  fontName="Helvetica", alignment=TA_CENTER)

    story = []

    story.append(Paragraph(topic.replace("-", " "), style_title))
    story.append(Paragraph(f"Meeting Notes  |  {meeting_date}", style_subtitle))
    story.append(HRFlowable(width="100%", thickness=1.5, color=c(NAVY), spaceAfter=10))

    def md(text: str) -> str:
        """Escape HTML then convert inline markdown to ReportLab XML tags."""
        import re as _re
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        text = _re.sub(r'__(.+?)__',     r'<b>\1</b>', text)
        text = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
        return text

    in_email_block = False
    email_lines    = []

    def flush_email():
        nonlocal in_email_block, email_lines
        if email_lines:
            content = "<br/>".join(email_lines)
            data    = [[Paragraph(content, style_body)]]
            t       = Table(data, colWidths=[width - 2 * margin])
            t.setStyle(TableStyle([
                ("BACKGROUND",   (0, 0), (-1, -1), c(LGREY)),
                ("BOX",          (0, 0), (-1, -1), 0.5, c(BLUE)),
                ("LEFTPADDING",  (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING",   (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING",(0, 0), (-1, -1), 8),
            ]))
            story.append(t)
            story.append(Spacer(1, 8))
        in_email_block = False
        email_lines    = []

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
            checked = line.startswith("- [x] ")
            box  = "☑" if checked else "☐"
            story.append(Paragraph(f"{box}  {md(line[6:])}", style_bullet))
        elif line.startswith("- "):
            story.append(Paragraph(f"•  {md(line[2:])}", style_bullet))
        elif line.startswith("> "):
            story.append(Paragraph(f'<i>"{md(line[2:])}"</i>', style_body))
        elif line.startswith('"') and line.endswith('"'):
            story.append(Paragraph(f"<i>{md(line)}</i>", style_body))
        elif line in ("", "---"):
            story.append(Spacer(1, 4))
        else:
            if line:
                story.append(Paragraph(md(line), style_body))

    if in_email_block:
        flush_email()

    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=0.5, color=c(BLUE), spaceAfter=4))
    gen_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    story.append(Paragraph(
        f"Generated by LocalPlaud  |  {gen_ts}  |  Audio stays local — only text sent to Claude",
        style_footer,
    ))

    doc.build(story)

# ---------------------------------------------------------------------------
# Completion card
# ---------------------------------------------------------------------------
def print_completion_card(state: dict):
    w     = tw()
    inner = w - 4

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

    title_text = topic.replace("-", " ")
    dur_str    = f"{dur/60:.0f} min  ·  {segs} segments"
    if spks:
        dur_str += f"  ·  {spks} speaker{'s' if spks != 1 else ''}"

    def pad(s: str, visible_len: int) -> str:
        """Pad to fill inner width, accounting for ANSI codes in s."""
        spaces = inner - visible_len - 2
        return s + " " * max(spaces, 0)

    print()
    print(f"  {C.BLUE}┌{'─' * inner}┐{C.RESET}")
    row1 = f"  {C.GREEN}[COMPLETE]{C.RESET}  {C.BOLD}{C.WHITE}{title_text}{C.RESET}  {C.DIM}·  {date_str}{C.RESET}"
    print(f"  {C.BLUE}│{C.RESET}  {row1}")
    print(f"  {C.BLUE}├{'─' * inner}┤{C.RESET}")
    print(f"  {C.BLUE}│{C.RESET}  {C.DIM}Audio    {C.RESET}  {dur_str}")
    print(f"  {C.BLUE}│{C.RESET}  {C.DIM}Tokens   {C.RESET}  {tok_in:,} in  /  {tok_out:,} out  ·  {C.GREEN}~${cost:.4f}{C.RESET}")
    if md_path:
        print(f"  {C.BLUE}│{C.RESET}  {C.DIM}Markdown {C.RESET}  {Path(md_path).name}")
    if pdf_path:
        print(f"  {C.BLUE}│{C.RESET}  {C.DIM}PDF      {C.RESET}  {Path(pdf_path).name}")
    print(f"  {C.BLUE}└{'─' * inner}┘{C.RESET}")
    print()

# ---------------------------------------------------------------------------
# Processing pipeline
# ---------------------------------------------------------------------------
def process_file(audio_path: Path, state: dict | None = None):
    stem = audio_path.stem

    if state is None:
        state = {
            "current_step":  1,
            "audio_filename": audio_path.name,
            "meeting_date":  None,
            "transcript":    None,
            "audio_duration": 0,
            "has_speakers":  False,
            "speaker_count": 0,
            "notes":         None,
            "topic":         None,
            "token_in":      0,
            "token_out":     0,
            "cost":          0.0,
            "user_context":  "",
            "continuation":  None,
            "md_path":       None,
            "pdf_path":      None,
        }

    # -----------------------------------------------------------------------
    # Step 1: VALIDATE
    # -----------------------------------------------------------------------
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

    # -----------------------------------------------------------------------
    # Step 2: DATE + context
    # -----------------------------------------------------------------------
    if state["current_step"] <= 2:
        print_section("PREPARE", 2, 7)

        if state["meeting_date"] is None:
            date = detect_date(audio_path)
            print_info(f"Detected date: {C.WHITE}{date}{C.RESET}")
            confirm = safe_input(f"Use {date}? (Enter to confirm, or type YYYY-MM-DD):", buffer=True)
            if confirm:
                try:
                    datetime.date.fromisoformat(confirm)
                    date = confirm
                except ValueError:
                    print_warn(f"Invalid format — using detected date: {date}")
            state["meeting_date"] = date
            print_ok(f"Meeting date: {C.WHITE}{state['meeting_date']}{C.RESET}")

        if not state["user_context"]:
            print()
            print(f"  {C.DIM}Add one-time context for this meeting (e.g. 'Budget review with Franke Ltd'){C.RESET}")
            ctx = safe_input("Context (Enter to skip):", buffer=True)
            state["user_context"] = ctx

            time.sleep(0.3)
            print()
            cont = safe_input("Continuation of a previous recording? (Y/N):").upper()
            if cont == "Y":
                all_audio = list_audio_files(DIR_COMPLETED) + list_audio_files(DIR_NOT_TRANSCRIBED)
                if all_audio:
                    print()
                    for i, f in enumerate(all_audio, 1):
                        print(f"  {C.CYAN}[{i}]{C.RESET}  {file_info(f)}")
                    print()
                    choice = safe_input("Select number (Enter to skip):", buffer=True)
                    if choice.isdigit() and 1 <= int(choice) <= len(all_audio):
                        state["continuation"] = str(all_audio[int(choice) - 1])
                        print_ok(f"Continuation of: {all_audio[int(choice)-1].name}")

        state["current_step"] = 3
        save_state(stem, state)

    # -----------------------------------------------------------------------
    # Step 3: TRANSCRIBE
    # -----------------------------------------------------------------------
    if state["current_step"] <= 3:
        if state["transcript"] is None:
            state["transcript"], state["audio_duration"] = transcribe(audio_path)
        state["current_step"] = 4
        save_state(stem, state)

    # -----------------------------------------------------------------------
    # Step 4: DIARIZATION
    # -----------------------------------------------------------------------
    if state["current_step"] <= 4:
        if HF_TOKEN and not state["has_speakers"]:
            try:
                state["transcript"], state["speaker_count"] = diarize(audio_path, state["transcript"])
                state["has_speakers"] = True
            except Exception as e:
                print_warn(f"Speaker ID failed: {e}")
                print_info("Continuing without speaker labels.")
                state["has_speakers"] = False
        state["current_step"] = 5
        save_state(stem, state)

    # -----------------------------------------------------------------------
    # Step 5: SUMMARISE
    # -----------------------------------------------------------------------
    if state["current_step"] <= 5:
        if state["notes"] is None:
            (state["notes"], state["topic"],
             state["token_in"], state["token_out"],
             state["cost"]) = summarise(
                state["transcript"],
                state["has_speakers"],
                state["user_context"],
            )
        state["current_step"] = 6
        save_state(stem, state)

    # -----------------------------------------------------------------------
    # Step 6: SAVE OUTPUT
    # -----------------------------------------------------------------------
    if state["current_step"] <= 6:
        print_section("SAVE OUTPUT", 6, 7)

        date_str = state["meeting_date"]
        topic    = state["topic"]
        base     = f"{date_str}_Work_MM_{topic}"
        md_path  = unique_path(DIR_MARKDOWN, base, ".md")
        pdf_path = unique_path(DIR_PDF,      base, ".pdf")

        md_path.write_text(state["notes"], encoding="utf-8")
        print_ok(f"Markdown → {C.WHITE}{md_path.name}{C.RESET}")

        try:
            with Spinner("Generating PDF"):
                generate_pdf(state["notes"], pdf_path, date_str, topic)
            print_ok(f"PDF      → {C.WHITE}{pdf_path.name}{C.RESET}")
            state["pdf_path"] = str(pdf_path)
        except Exception as e:
            print_warn(f"PDF generation failed: {e}")
            pdf_path = None
            state["pdf_path"] = None

        state["md_path"]      = str(md_path)
        state["current_step"] = 7
        save_state(stem, state)

    # -----------------------------------------------------------------------
    # Step 7: ORGANISE
    # -----------------------------------------------------------------------
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
            ans = safe_input("Open the PDF? (Y/N):", buffer=True).upper()
            if ans == "Y":
                try:
                    os.startfile(pdf_path)
                except Exception as e:
                    print_warn(f"Could not open PDF: {e}")
                    print_info(f"Path: {pdf_path}")

# ---------------------------------------------------------------------------
# Menu helpers
# ---------------------------------------------------------------------------
def pick_file_gui() -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        filetypes = [
            ("Audio files", "*.m4a *.mp3 *.wav *.ogg *.flac *.aac *.wma *.webm"),
            ("All files",   "*.*"),
        ]
        path = filedialog.askopenfilename(
            title="Select audio file to transcribe",
            filetypes=filetypes,
        )
        root.destroy()
        return Path(path) if path else None
    except Exception as e:
        print_warn(f"File picker unavailable: {e}")
        return None

def option_browse():
    print_section("BROWSE FOR FILE")
    p = pick_file_gui()
    if p is None:
        print_info("No file selected. Enter path manually:")
        raw = safe_input("Path:", buffer=True)
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
        ans = safe_input("Reprocess? (Y/N):", buffer=True).upper()
        if ans != "Y":
            return
        reason = safe_input("Reason for reprocessing:", buffer=True)
        log(f"Reprocessing: {p.name} | Reason: {reason}")
        shutil.move(str(completed), str(DIR_NOT_TRANSCRIBED / p.name))
        p = DIR_NOT_TRANSCRIBED / p.name
    else:
        dst = DIR_NOT_TRANSCRIBED / p.name
        if not dst.exists():
            shutil.move(str(p), str(dst))
            print_ok(f"Moved to Not Transcribed: {p.name}")
        p = dst

    stem           = p.stem
    existing_state = load_state(stem)
    if existing_state:
        print_info(f"Checkpoint found at step {existing_state.get('current_step', '?')}.")
        ans = safe_input("Resume from checkpoint? (Y/N):", buffer=True).upper()
        if ans != "Y":
            clear_state(stem)
            existing_state = None

    process_file(p, existing_state)

def option_check_folder():
    print_section("NOT TRANSCRIBED FOLDER")
    files = list_audio_files(DIR_NOT_TRANSCRIBED)
    if not files:
        print_info(f"No audio files in: {DIR_NOT_TRANSCRIBED}")
        return

    print(f"  {C.DIM}{len(files)} file(s) found{C.RESET}")
    print()
    for i, f in enumerate(files, 1):
        print(f"  {C.CYAN}[{i}]{C.RESET}  {file_info(f)}")
    print()

    choice = safe_input("Select file number:", buffer=True)
    if not choice.isdigit() or not (1 <= int(choice) <= len(files)):
        print_warn("Invalid selection.")
        return

    p              = files[int(choice) - 1]
    existing_state = load_state(p.stem)
    if existing_state:
        print_info(f"Checkpoint found at step {existing_state.get('current_step', '?')}.")
        ans = safe_input("Resume from checkpoint? (Y/N):", buffer=True).upper()
        if ans != "Y":
            clear_state(p.stem)
            existing_state = None

    process_file(p, existing_state)

def option_batch():
    print_section("BATCH PROCESS")
    files = list_audio_files(DIR_NOT_TRANSCRIBED)
    if not files:
        print_info(f"No audio files in: {DIR_NOT_TRANSCRIBED}")
        return

    print(f"  {C.DIM}{len(files)} file(s) in queue{C.RESET}")
    print()

    processed = 0
    skipped   = 0

    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        print(f"  {C.CYAN}───{C.RESET}  {C.WHITE}{f.name}{C.RESET}  {C.DIM}({size_mb:.1f} MB){C.RESET}")
        ans = safe_input("Process this file? (Y/N):", buffer=True).upper()
        if ans != "Y":
            skipped += 1
            continue

        existing_state = load_state(f.stem)
        if existing_state:
            print_info(f"Checkpoint found at step {existing_state.get('current_step', '?')}.")
            ans2 = safe_input("Resume from checkpoint? (Y/N):", buffer=True).upper()
            if ans2 != "Y":
                clear_state(f.stem)
                existing_state = None

        try:
            process_file(f, existing_state)
            processed += 1
        except KeyboardInterrupt:
            print()
            print_warn("Batch interrupted. Progress checkpointed.")
            break
        except Exception as e:
            print_err(f"Failed: {e}")
            traceback.print_exc()
            skipped += 1

    print()
    print_section("BATCH COMPLETE")
    print_ok(f"Processed: {C.WHITE}{processed}{C.RESET}")
    if skipped:
        print_info(f"Skipped:   {skipped}")

def option_resume():
    print_section("RESUME CHECKPOINT")
    states = list_incomplete_states()
    if not states:
        print_info("No interrupted jobs found.")
        return

    print(f"  {C.DIM}{len(states)} interrupted job(s){C.RESET}")
    print()
    for i, s in enumerate(states, 1):
        step  = s.get("current_step", "?")
        fname = s.get("audio_filename", "unknown")
        date  = s.get("meeting_date", "unknown date")
        print(f"  {C.CYAN}[{i}]{C.RESET}  {C.WHITE}{fname}{C.RESET}  {C.DIM}·  {date}  ·  stopped at step {step}{C.RESET}")
    print()

    choice = safe_input("Select job number (Enter to cancel):", buffer=True)
    if not choice.isdigit() or not (1 <= int(choice) <= len(states)):
        return

    s     = states[int(choice) - 1]
    fname = s.get("audio_filename", "")
    stem  = s["_stem"]

    candidates = [
        DIR_NOT_TRANSCRIBED / fname,
        DIR_COMPLETED       / fname,
        ROOT                / fname,
    ]
    audio_path = next((c for c in candidates if c.exists()), DIR_NOT_TRANSCRIBED / fname)

    ans = safe_input(f"Resume '{fname}' from step {s.get('current_step')}? (Y/N):", buffer=True).upper()
    if ans != "Y":
        return

    process_file(audio_path, load_state(stem))

# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------
def print_main_menu():
    w         = tw()
    inner     = w - 4
    spk_label = f"{C.GREEN}ENABLED{C.RESET}" if HF_TOKEN else f"{C.DIM}DISABLED{C.RESET}"
    model_tag = CLAUDE_MODEL.split("-")[1].upper() if "-" in CLAUDE_MODEL else CLAUDE_MODEL.upper()

    print(f"  {C.BLUE}┌{'─' * inner}┐{C.RESET}")
    print(f"  {C.BLUE}│{C.RESET}  {C.BOLD}{C.BLUE}LOCALPLAUD{C.RESET}  {C.DIM}·  private meeting transcription + summarisation{C.RESET}")
    print(f"  {C.BLUE}│{C.RESET}  {C.DIM}SPEAKER ID:{C.RESET} {spk_label}   {C.DIM}WHISPER:{C.RESET} {C.WHITE}{WHISPER_MODEL}{C.RESET}   {C.DIM}CLAUDE:{C.RESET} {C.WHITE}{model_tag}{C.RESET}")
    print(f"  {C.BLUE}└{'─' * inner}┘{C.RESET}")
    print()
    print(f"  {C.DIM}SELECT INPUT SOURCE{C.RESET}  {C.DIM}{'─' * 30}{C.RESET}")
    print()
    print(f"  {C.CYAN}[1]{C.RESET}  Browse for a file  {C.DIM}(opens file picker){C.RESET}")
    print(f"  {C.CYAN}[2]{C.RESET}  Scan Not Transcribed folder")
    print(f"  {C.CYAN}[3]{C.RESET}  Batch process queue")
    print(f"  {C.CYAN}[4]{C.RESET}  Resume checkpoint")
    print()

def main():
    startup_animation()

    while True:
        print_main_menu()

        choice = safe_input("Select [1-4]:")
        if choice == "1":
            option_browse()
        elif choice == "2":
            option_check_folder()
        elif choice == "3":
            option_batch()
        elif choice == "4":
            option_resume()
        else:
            print_warn("Enter 1, 2, 3, or 4.")
            continue

        print()
        again = safe_input("Process another file? (Y/N):", buffer=True).upper()
        if again != "Y":
            break

    print()
    print(f"  {C.DIM}Goodbye.{C.RESET}")
    print()

if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        print(f"\n  {C.RED}[ERR]{C.RESET}  ANTHROPIC_API_KEY not set.")
        print(f"  {C.DIM}       Run install.bat or add your key to .env{C.RESET}\n")
        sys.exit(1)
    main()
