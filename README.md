# Claude Video Studio

A local, browser-based video editor where **Claude Code is the editor**. Upload footage and reference samples,
write a brief, and edit **scene by scene** with plain-language commands. Simple commands (cut, trim, speed,
captions, zoom, text) run instantly. Anything else goes to an inbox that Claude Code processes on request.

Everything runs on your own PC. No cloud rendering, no API key.

![flow](https://img.shields.io/badge/flow-upload%20→%20analyze%20→%20edit%20scenes%20→%20export-orange)

## What it does

| Step | What happens |
|---|---|
| **Footage** | Drop videos. *Analyze* transcribes every word with timestamps (whisper.cpp on the GPU, works for Hindi + English), detects camera cuts and pauses, and splits the video into scenes. It also makes a light 540p copy for smooth playback. |
| **References & Brief** | Drop sample videos or images, or paste links (downloaded with yt-dlp). Each video reference gets a contact sheet and its pacing (average shot length). Set the style: format (16:9 / 9:16 / 1:1), word-highlight captions, colours, silence removal, logo. |
| **Scenes** | Thumbnails, timings and the words spoken in each scene. Click a scene to loop it. Type commands per scene or for the whole video. Undo, render preview, export final. |
| **Exports** | Final renders go to `Exports/`. Only changed scenes are re-rendered. |

### Instant commands
- **Scene:** `cut` · `keep` · `trim start 1.5` · `trim end 2` · `split at 3.2` · `speed 1.5` · `slow mo` · `mute` · `volume 50%` · `zoom 1.3` · `captions off` · `remove silences` · `text: Your title` · `text top|center|bottom`
- **Whole video:** `format 9:16` · `fit pad` · `captions upper` · `captions top` · `highlight #22C55E` · `words 3` · `remove silences` · `logo logo.png` · `speed 1.1`

Anything else ("tighten this to 30 seconds", "animated title like reference 1") is queued for Claude.
In Claude Code, say **`process studio inbox`**. Claude follows [`_engines/studio/CLAUDE.md`](_engines/studio/CLAUDE.md),
does the edit, and replies on the page.

## Setup (Windows)

Requirements: Windows 10/11, Python 3.12+, Node.js 20+, an NVIDIA GPU is recommended (CPU works, slower).

```powershell
# 1. engines
winget install Gyan.FFmpeg
winget install yt-dlp.yt-dlp          # also installs Deno

# 2. whisper.cpp (CUDA build) + models -> _engines\whisper\
#    https://github.com/ggml-org/whisper.cpp/releases  : whisper-bin-win-cuda-11.8.0-x64.zip  -> _engines\whisper\cuda\
#                                                        whisper-bin-x64.zip (CPU fallback)    -> _engines\whisper\cpu\
#    cuBLAS 11 DLLs (not bundled in the zip):  pip download nvidia-cublas-cu11 --no-deps --platform win_amd64 --only-binary=:all: --python-version 3.12
#                                              copy cublas64_11.dll + cublasLt64_11.dll from the wheel into _engines\whisper\cuda\Release\
#    models -> _engines\whisper\models\ :
#      https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
#      https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin
#    (large downloads stall sometimes: _engines\_downloads\resume_dl.ps1 resumes them)

# 3. studio app
cd _engines\studio
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 4. optional motion-graphics engines
cd ..\remotion;    npm install
cd ..\hyperframes; npm install
```

Then double-click **`Start Studio.bat`** and open http://127.0.0.1:8070.

The app creates its working folders next to `_engines/`: `Raw Footage/`, `References/`, `Storyboards/` (project
files), `Exports/`, `Brand Kit/`. They are git-ignored.

## Tests

```powershell
cd _engines\studio
.venv\Scripts\python.exe selftest.py           # 23 end-to-end checks in a temporary folder
.venv\Scripts\python.exe test_whisper_parse.py  # whisper JSON parsing (Hindi split characters)
```

`selftest.py` generates its own test videos (`make_fixtures.py`) and covers: phone rotation, scene splitting,
job queue, split/undo, the Claude inbox, audio/video sync of every rendered segment, host/origin/path
security, and the cross-process project lock.

## Layout

```
Start Studio.bat          launcher
_engines/studio/          Flask app: app.py (API), pipeline.py (analysis + render), commands.py (instant
                          commands), core.py (storage + locking), studio_cli.py (Claude's inbox tool), static/ (UI)
_engines/remotion/        Remotion project (kinetic captions composition)
_engines/hyperframes/     HyperFrames project (HTML graphic cards)
_engines/README.md        engine notes and test results
```

## Known limits
- Motion graphics from Remotion/HyperFrames are not yet placed into the timeline automatically.
- The inbox is processed when you ask Claude. There is no push notification.
- The server binds to `127.0.0.1` only. One heavy job runs at a time (designed for an 8 GB RAM laptop).
- HyperFrames: run `init` with `--skip-skills`. Set `HYPERFRAMES_NO_TELEMETRY=1` if you don't want usage data sent.
