@echo off
setlocal enabledelayedexpansion
title LocalPlaud Installer
set LOGFILE=%~dp0install_log.txt
echo Install started %date% %time% > "%LOGFILE%"

echo.
echo ============================================================
echo   LocalPlaud - Private Meeting Transcription
echo   Installer
echo ============================================================
echo.

:: Check Python
echo [CHECK] Looking for Python 3.10+...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+ from python.org
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] Python !PYVER! found
echo.

:: Get install location via GUI folder picker
echo [SETUP] Choosing install location...
echo        A folder picker will open. Choose where to install LocalPlaud.
echo        Press Cancel to use default: %USERPROFILE%\LocalPlaud
echo.

python -c "import tkinter as tk; from tkinter import filedialog; root=tk.Tk(); root.withdraw(); root.attributes('-topmost',True); d=filedialog.askdirectory(title='Choose LocalPlaud install folder (a LocalPlaud subfolder will be created)'); print(d if d else 'CANCELLED')" > "%TEMP%\lp_dir.txt" 2>nul
set /p CHOSEN_DIR=<"%TEMP%\lp_dir.txt"
del "%TEMP%\lp_dir.txt" 2>nul
:: Normalize forward slashes to backslashes (tkinter returns forward slashes on Windows)
set CHOSEN_DIR=!CHOSEN_DIR:/=\!

if "!CHOSEN_DIR!"=="CANCELLED" (
    set INSTALL_DIR=%USERPROFILE%\LocalPlaud
    echo [INFO] Using default: !INSTALL_DIR!
) else if "!CHOSEN_DIR!"=="" (
    set INSTALL_DIR=%USERPROFILE%\LocalPlaud
    echo [INFO] Using default: !INSTALL_DIR!
) else (
    set INSTALL_DIR=!CHOSEN_DIR!\LocalPlaud
    echo [OK] Install location: !INSTALL_DIR!
)
echo.

:: Get Claude API key
echo [SETUP] Claude API Key (required)
echo        Get yours at: console.anthropic.com
echo.
set /p ANTHROPIC_API_KEY=Enter Claude API key:
if "!ANTHROPIC_API_KEY!"=="" (
    echo [ERROR] Claude API key is required. Exiting.
    pause
    exit /b 1
)
echo [OK] API key received.
echo.

:: Get HuggingFace token
echo [SETUP] HuggingFace Token (optional - enables speaker identification)
echo        Get yours at: huggingface.co/settings/tokens
echo        Press Enter to skip (speaker ID will be disabled)
echo.
set /p HF_TOKEN=Enter HuggingFace token (or press Enter to skip):
if "!HF_TOKEN!"=="" (
    echo [INFO] Skipping speaker identification.
    set ENABLE_DIARIZATION=0
) else (
    echo [OK] HuggingFace token received. Speaker ID will be enabled.
    set ENABLE_DIARIZATION=1
)
echo.

:: Choose Whisper model
echo [SETUP] Choose Whisper transcription model:
echo.
echo   1. turbo   (fastest, ~1 min/hr audio, good accuracy)  [RECOMMENDED]
echo   2. medium  (balanced, ~3 min/hr audio, better accuracy)
echo   3. large-v3 (slowest, ~8 min/hr audio, best accuracy)
echo.
set /p WHISPER_CHOICE=Select (1/2/3) [default=1]:
if "!WHISPER_CHOICE!"=="2" (
    set WHISPER_MODEL=medium
) else if "!WHISPER_CHOICE!"=="3" (
    set WHISPER_MODEL=large-v3
) else (
    set WHISPER_MODEL=turbo
)
echo [OK] Whisper model: !WHISPER_MODEL!
echo.

:: Choose Claude model
echo [SETUP] Choose Claude summarisation model:
echo.
echo   1. Haiku  (claude-haiku-4-5-20251001)  - ~$0.05/meeting  [RECOMMENDED]
echo   2. Sonnet (claude-sonnet-4-6)           - ~$0.15/meeting
echo.
set /p CLAUDE_CHOICE=Select (1/2) [default=1]:
if "!CLAUDE_CHOICE!"=="2" (
    set CLAUDE_MODEL=claude-sonnet-4-6
) else (
    set CLAUDE_MODEL=claude-haiku-4-5-20251001
)
echo [OK] Claude model: !CLAUDE_MODEL!
echo.

:: Create folder structure
echo [CREATE] Building folder structure...
mkdir "!INSTALL_DIR!\Recordings\Not Transcribed" 2>nul
mkdir "!INSTALL_DIR!\Recordings\Completed" 2>nul
mkdir "!INSTALL_DIR!\Meeting Minutes\Markdown" 2>nul
mkdir "!INSTALL_DIR!\Meeting Minutes\PDF" 2>nul
mkdir "!INSTALL_DIR!\.localplaud_state" 2>nul
echo [OK] Folders created.
echo STEP: Folders created >> "%LOGFILE%"
echo.

:: Copy files
echo [COPY] Copying LocalPlaud files...
set INSTALLER_DIR=%~dp0

if exist "!INSTALLER_DIR!localplaud.py" (
    copy /Y "!INSTALLER_DIR!localplaud.py" "!INSTALL_DIR!\localplaud.py" >nul
    echo [OK] localplaud.py copied.
) else (
    echo [ERROR] localplaud.py not found next to installer. Cannot continue.
    pause
    exit /b 1
)

if exist "!INSTALLER_DIR!README.md" (
    copy /Y "!INSTALLER_DIR!README.md" "!INSTALL_DIR!\README.md" >nul
    echo [OK] README.md copied.
)
if exist "!INSTALLER_DIR!requirements.txt" (
    copy /Y "!INSTALLER_DIR!requirements.txt" "!INSTALL_DIR!\requirements.txt" >nul
    echo [OK] requirements.txt copied.
)
if exist "!INSTALLER_DIR!requirements-diarization.txt" (
    copy /Y "!INSTALLER_DIR!requirements-diarization.txt" "!INSTALL_DIR!\requirements-diarization.txt" >nul
    echo [OK] requirements-diarization.txt copied.
)
if not exist "!INSTALL_DIR!\scripts" mkdir "!INSTALL_DIR!\scripts" 2>nul
if exist "!INSTALLER_DIR!scripts\smoke_test.py" (
    copy /Y "!INSTALLER_DIR!scripts\smoke_test.py" "!INSTALL_DIR!\scripts\smoke_test.py" >nul
    echo [OK] smoke_test.py copied.
)
echo.

:: Create .env file
echo [CREATE] Writing settings (.env)...
(
    echo ANTHROPIC_API_KEY=!ANTHROPIC_API_KEY!
    echo HF_TOKEN=!HF_TOKEN!
    echo WHISPER_MODEL=!WHISPER_MODEL!
    echo CLAUDE_MODEL=!CLAUDE_MODEL!
) > "!INSTALL_DIR!\.env"
echo [OK] .env created.
echo.

:: Create context.md template
echo [CREATE] Writing context.md template...
(
    echo # Business Context
    echo.
    echo ## About Our Organisation
    echo [Describe your company, team, or organisation here - e.g. "We are a 12-person SaaS startup building HR software for SMEs"]
    echo.
    echo ## Key People
    echo [List key people and their roles - e.g. "Alice Smith - CEO, Bob Jones - Head of Sales"]
    echo.
    echo ## Products / Services
    echo [Describe your main products or services]
    echo.
    echo ## Common Terms / Jargon
    echo [List any industry-specific or company-specific terms Claude should understand]
    echo.
    echo ## Meeting Context
    echo [Any standing context for meetings - e.g. "We have a weekly Monday standup and a monthly board review"]
    echo.
    echo ---
    echo Edit this file to give Claude better context about your business.
    echo This context is sent with every meeting to improve summaries.
) > "!INSTALL_DIR!\context.md"
echo [OK] context.md created.
echo.

:: Create processing_log.txt
echo [CREATE] Creating processing log...
(
    echo LocalPlaud Processing Log
    echo ============================================================
    echo.
) > "!INSTALL_DIR!\processing_log.txt"
echo [OK] processing_log.txt created.
echo STEP: Files created >> "%LOGFILE%"
echo.

:: Install Python packages
echo [INSTALL] Installing Python packages...
echo          This may take a few minutes on first run.
echo.

echo [INSTALL] Creating virtual environment...
python -m venv "!INSTALL_DIR!\.venv"
if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)
set PYTHON_EXE=!INSTALL_DIR!\.venv\Scripts\python.exe
echo [OK] Virtual environment created.
echo.

echo [INSTALL] Core packages in virtual environment...
echo STEP: Starting pip upgrade >> "%LOGFILE%"
"!PYTHON_EXE!" -m pip install --upgrade pip --quiet
set PIP_EL=!errorlevel!
echo STEP: pip upgrade done exit=!PIP_EL! >> "%LOGFILE%"
echo STEP: Starting core install >> "%LOGFILE%"
if exist "!INSTALLER_DIR!requirements.txt" (
    "!PYTHON_EXE!" -m pip install -r "!INSTALLER_DIR!requirements.txt" --quiet
) else (
    "!PYTHON_EXE!" -m pip install faster-whisper anthropic reportlab python-dotenv --quiet
)
set PIP_EL=!errorlevel!
echo STEP: core install done exit=!PIP_EL! >> "%LOGFILE%"
if "!PIP_EL!" NEQ "0" (
    echo STEP: FAILED >> "%LOGFILE%"
    echo [ERROR] Failed to install core packages. Check your internet connection.
    pause
    exit /b 1
)
echo STEP: core install passed >> "%LOGFILE%"
echo [OK] Core packages installed.
echo STEP: A >> "%LOGFILE%"
echo.
echo STEP: B >> "%LOGFILE%"
echo DIARIZATION=!ENABLE_DIARIZATION! >> "%LOGFILE%"

if "!ENABLE_DIARIZATION!" NEQ "1" goto :skip_pyannote
echo STEP: C-entering diarization >> "%LOGFILE%"
echo [INSTALL] Speaker identification packages (pyannote, torchaudio)...
echo           This is a large download (~2GB). Please wait...
if exist "!INSTALLER_DIR!requirements-diarization.txt" (
    "!PYTHON_EXE!" -m pip install -r "!INSTALLER_DIR!requirements-diarization.txt" --quiet
) else (
    "!PYTHON_EXE!" -m pip install pyannote.audio torchaudio soundfile --quiet
)
if errorlevel 1 (
    echo [WARN] Speaker ID packages failed. You can still use LocalPlaud without speaker ID.
    echo        To try again: pip install pyannote.audio torchaudio soundfile
) else (
    echo [OK] Speaker ID packages installed.
)
echo STEP: D-diarization done >> "%LOGFILE%"
echo.

:skip_pyannote
echo STEP: E-after diarization >> "%LOGFILE%"
echo STEP: Core packages installed >> "%LOGFILE%"

:: Test imports
echo [TEST] Verifying installation...
"!PYTHON_EXE!" -c "import faster_whisper; print('[OK] faster_whisper')"
"!PYTHON_EXE!" -c "import anthropic; print('[OK] anthropic')"
"!PYTHON_EXE!" -c "import reportlab; print('[OK] reportlab')"
"!PYTHON_EXE!" -c "import dotenv; print('[OK] python-dotenv')"
if "!ENABLE_DIARIZATION!" NEQ "1" goto :skip_dia_imports
"!PYTHON_EXE!" -c "import pyannote.audio; print('[OK] pyannote.audio')" 2>nul || echo [WARN] pyannote.audio import failed
"!PYTHON_EXE!" -c "import torchaudio; print('[OK] torchaudio')" 2>nul || echo [WARN] torchaudio import failed
"!PYTHON_EXE!" -c "import soundfile; print('[OK] soundfile')" 2>nul || echo [WARN] soundfile import failed
:skip_dia_imports
echo.
"!PYTHON_EXE!" "!INSTALL_DIR!\localplaud.py" --doctor
if exist "!INSTALL_DIR!\scripts\smoke_test.py" "!PYTHON_EXE!" "!INSTALL_DIR!\scripts\smoke_test.py"

echo STEP: Imports verified >> "%LOGFILE%"

:: Create launcher bat
echo [CREATE] Creating launcher...
python -c "p=r'!INSTALL_DIR!'; f=open(p+'\\Run LocalPlaud.bat','w'); f.write('@echo off\r\ncd /d '+chr(34)+p+chr(34)+'\r\n'+chr(34)+p+'\\.venv\\Scripts\\python.exe'+chr(34)+' localplaud.py\r\npause\r\n'); f.close()"
echo STEP: Launcher created >> "%LOGFILE%"
echo [OK] Launcher created.
echo.

echo ============================================================
echo   INSTALLATION COMPLETE
echo ============================================================
echo.
echo   Install location: !INSTALL_DIR!
echo   Whisper model:    !WHISPER_MODEL!
echo   Claude model:     !CLAUDE_MODEL!
if "!ENABLE_DIARIZATION!"=="1" echo   Speaker ID:       ENABLED
if "!ENABLE_DIARIZATION!" NEQ "1" echo   Speaker ID:       DISABLED
echo.
echo   TO START: Double-click "Run LocalPlaud.bat" in !INSTALL_DIR!
echo.
echo   NEXT STEPS:
echo   1. Edit context.md with info about your organisation
echo   2. Drop audio files into: Recordings\Not Transcribed\
echo   3. Run LocalPlaud and choose option 2 or 1
echo.
explorer "!INSTALL_DIR!"
pause
