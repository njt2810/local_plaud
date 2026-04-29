# LocalPlaud - Private Meeting Transcription

> Audio stays on your machine. Only the text transcript is sent to Claude.

---

## What It Does

LocalPlaud listens to your meeting recordings and produces professional meeting notes:

- Full transcript with speaker labels (if enabled)
- Action items with owners and due dates
- Key decisions and discussion topics
- Follow-up email draft ready to send
- Markdown and PDF output

---

## Quick Start

1. **Double-click `install.bat`** in the folder you extracted the zip into
2. Follow the prompts (API key, optional HuggingFace token, model choice)
3. **Double-click `Run LocalPlaud.bat`** in your install folder
4. Drop a recording into `Recordings\Not Transcribed\` and choose option 2

Installer creates a dedicated `.venv` (virtual environment), so LocalPlaud dependencies are isolated from other Python apps.

---

## Installation Requirements

- **Windows 10 or 11**
- **Python 3.10 or newer** - download from [python.org](https://www.python.org/)
  - During install, tick "Add Python to PATH"
- **Claude API key** - get one at [console.anthropic.com](https://console.anthropic.com)
- **HuggingFace token** (optional) - for speaker identification, at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)

---

## Features

### Audio Transcription
- Uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (local, private)
- Supports `.m4a`, `.mp3`, `.wav`, `.ogg`, `.flac`, `.aac`, `.wma`, `.webm`
- Automatic language detection
- GPU acceleration if available (NVIDIA CUDA)

### Speaker Identification
- Powered by [pyannote.audio](https://github.com/pyannote/pyannote-audio)
- Detects who is speaking and when
- You name each speaker after a sample excerpt is shown
- Requires a HuggingFace account and token (free)
- First run downloads ~1GB model

### AI Summarisation
- Sends ONLY the text transcript to Claude (audio never leaves your machine)
- Produces structured notes: TL;DR, action items, decisions, key topics, email draft
- Loads permanent business context from `context.md`
- You can add one-time context per meeting (e.g., "Budget review with Franke Ltd")

### Resume / Recovery
- If processing is interrupted (crash, Ctrl+C, power loss), your progress is checkpointed
- Choose option 4 from the main menu to resume without re-transcribing

### Batch Processing
- Process multiple recordings at once from the `Not Transcribed` folder
- Each file gets its own output; you are prompted before each one

---

## Cost Breakdown

Costs are per meeting (roughly 1 hour of audio):

| Model   | Typical cost | Best for |
|---------|-------------|----------|
| Haiku   | ~$0.05      | Daily standups, short calls |
| Sonnet  | ~$0.15      | Important strategy sessions, client calls |

Transcription (Whisper) is **free** - it runs locally on your machine.

---

## Folder Structure

```
LocalPlaud/
  localplaud.py              - Main application
  .env                       - Your API keys and settings (keep private)
  context.md                 - Your permanent business context for Claude
  processing_log.txt         - History of all processed files
  README.md                  - This file

  Recordings/
    Not Transcribed/         - Drop audio files here, or use option 1/2
    Completed/               - Audio moves here after processing

  Meeting Minutes/
    Markdown/                - YYYY-MM-DD_Work_MM_Topic.md
    PDF/                     - YYYY-MM-DD_Work_MM_Topic.pdf

  .localplaud_state/         - Checkpoint data for resume (hidden)
```

---

## context.md - Permanent Business Context

Edit `context.md` to tell Claude about your organisation. This is loaded every time and helps Claude:

- Use correct company and product names
- Understand your team structure
- Recognise industry-specific terms
- Know who key stakeholders are

Example content:
```
## About Our Organisation
We are a 12-person SaaS startup building HR software for SMEs in Australia.

## Key People
- Alice Smith - CEO
- Bob Jones - Head of Sales
- Carol Lee - Lead Developer

## Products
- HRFlow - our main product (employee onboarding and payroll)
- HRFlow Lite - free tier for teams under 10

## Common Terms
- MRR - Monthly Recurring Revenue
- CAC - Customer Acquisition Cost
- ICP - Ideal Customer Profile
```

---

## Troubleshooting

### Run health checks
From the install folder:

```bash
.venv\Scripts\python.exe localplaud.py --doctor
.venv\Scripts\python.exe scripts\smoke_test.py
```

`--doctor` validates settings, imports, and writable folders. `smoke_test.py` is a quick non-interactive startup check.
If you skip speaker ID during install, LocalPlaud now skips downloading speaker-ID packages.

### "Python not found" during install
Make sure Python is installed and "Add to PATH" was ticked. Restart your terminal after installing Python.

### "ANTHROPIC_API_KEY not set"
Open `.env` in your install folder (it's a plain text file) and check the key is present and correct.

### Speaker ID not working
- Check your HuggingFace token is in `.env` as `HF_TOKEN=hf_xxx...`
- On first run, it downloads a ~1GB model - this takes a few minutes
- You must accept the pyannote model licence at huggingface.co/pyannote/speaker-diarization-3.1

### Transcription is slow
- Use the `turbo` model (fastest)
- A 1-hour recording takes ~1 min on turbo, ~8 min on large-v3
- If you have an NVIDIA GPU, LocalPlaud will use it automatically

### PDF won't open
Make sure you have a PDF reader installed (Adobe Acrobat, Edge, or any PDF viewer).

### "AudioDecoder not defined" error
This should not happen in this version - LocalPlaud uses the correct workaround for Windows. If you see it, make sure you're running the latest `localplaud.py` from this installer.

### The script crashed mid-transcription
Use option 4 (Resume) from the main menu. Your transcript is saved and you won't need to re-transcribe.

---

## Privacy

| What                   | Where it goes         |
|------------------------|-----------------------|
| Audio file             | Stays on your machine |
| Transcript text        | Sent to Claude API    |
| context.md contents    | Sent to Claude API    |
| Meeting notes / PDF    | Stays on your machine |
| API keys               | Stored in `.env` only |

Anthropic's API data handling: [anthropic.com/privacy](https://www.anthropic.com/privacy)

---

## Updating Settings

Edit `.env` in your install folder to change:

```
ANTHROPIC_API_KEY=sk-ant-...
HF_TOKEN=hf_...
WHISPER_MODEL=turbo
CLAUDE_MODEL=claude-haiku-4-5-20251001
```

Valid values:
- `WHISPER_MODEL`: `turbo`, `medium`, `large-v3`, `small`, `base`
- `CLAUDE_MODEL`: `claude-haiku-4-5-20251001`, `claude-sonnet-4-6`

---

## Supported Audio Formats

`.m4a`, `.mp3`, `.wav`, `.ogg`, `.flac`, `.aac`, `.wma`, `.webm`

Works with recordings from:
- Plaud Note / Plaud Pin
- iPhone Voice Memos
- Android voice recorders
- Zoom, Teams, Webex local recordings
- Any meeting recorder app

---

*LocalPlaud is not affiliated with Plaud or Anthropic.*
