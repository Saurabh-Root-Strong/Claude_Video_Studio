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
| **References & Brief** | Drop sample videos or images, or paste links (downloaded with yt-dlp). Each video reference gets a contact sheet and its pacing (average shot length). Set the style: format (16:9 / 9:16 / 1:1), word-highlight captions (Hindi script or Hinglish – Hindi in English letters), colours, silence removal, logo. |
| **Scenes** | Thumbnails, timings and the words spoken in each scene. Click a scene to loop it. Type commands per scene or for the whole video. Undo, render preview, export final. |
| **Exports** | Final renders go to `Exports/`. Only changed scenes are re-rendered. |

### Instant commands
- **Scene (split layout):** `card: title bajaj *IPO* aaj` · `card: number 2x+ | listing gain` · `card: chart 10,12,15 | label` · `card off`
- **Scene:** `cut` · `keep` · `trim start 1.5` · `trim end 2` · `split at 3.2` · `speed 1.5` · `slow mo` · `mute` · `volume 50%` · `zoom 1.3` · `captions off` · `remove silences` · `text: Your title` · `text top|center|bottom`
- **Whole video:** `format 9:16` · `fit pad` · `captions upper` · `captions top` · `captions hinglish` / `captions hindi` · `spell daval as double` · `layout split` · `headline: ...` · `music on` · `face up` · `highlight #22C55E` · `words 3` · `remove silences` · `logo logo.png` · `speed 1.1`

Anything else ("tighten this to 30 seconds", "animated title like reference 1") is queued for Claude.
In Claude Code, say **`process studio inbox`**. Claude follows [`_engines/studio/CLAUDE.md`](_engines/studio/CLAUDE.md),
does the edit, and replies on the page.

## Install on any Windows PC (one click)

1. **Download:** `git clone https://github.com/Saurabh-Root-Strong/Claude_Video_Studio.git`
   (or GitHub → **Code → Download ZIP** and extract it anywhere, e.g. `D:\Claude_Video_Editor`).
2. **Double-click `Setup.bat`.** It installs and checks everything, and it's safe to run again:

   | Installs | Notes |
   |---|---|
   | FFmpeg, yt-dlp (winget) | skipped if already present |
   | Python 3.12 + the studio's environment | skipped if Python 3.10+ exists |
   | whisper.cpp `b5454` (CPU build, + CUDA build and cuBLAS on NVIDIA PCs) | pinned version |
   | Speech models: large-v3-turbo (547 MB), Silero VAD | SHA-256 verified, downloads resume if they stall |
   | Node.js LTS + Remotion + HyperFrames (`npm ci`, exact versions from lockfiles) | skip with `-SkipMotion` |
   | Working folders: Raw Footage, References, Storyboards, Exports, Brand Kit | |

   Options: `Setup.bat -CpuOnly` (ignore the GPU), `-SkipMotion` (no Node/motion graphics), `-RunTests`.
   Expect about 1.5 GB of downloads (about 2.5 GB with motion graphics).
3. **Double-click `Start Studio.bat`.** The studio opens at http://127.0.0.1:8070. (On a PC that isn't set up, it
   offers to run Setup first.)

Requirements: Windows 10/11 with `winget` (built in; otherwise install "App Installer" from the Microsoft Store).
An NVIDIA GPU is used automatically if present. Without one, the CPU build works, just slower.

**Your video projects are not in git.** Footage, exports and project files stay on the PC where you made them
(the repo is public, and video is too large for git). To move a project to another PC, copy its folders
(`Raw Footage/<project>`, `References/<project>`, `Storyboards/<project>`) across, e.g. via a drive or cloud folder.

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

## Brand Kit
`Brand Kit/brand-kit.md` is the approved editing profile ("Clean Split": cards on top, speaker on a keyed off-white background below, captions on an orange seam, ducked music, -14 LUFS). `brand-kit.json` holds its settings: it is the read-only look **★ Brand Kit** – new projects start on **None · simple settings**; any project can pick ★ Brand Kit (or a reference look R1, R2…) in Scenes → Look. Poppins (OFL) is bundled in `_engines/studio/fonts/`.

## Storyboard review
Before rendering, Claude can plan the edit beat by beat (`studio_cli.py storyboard build <pid> <plan.json>`): dead-air trims, pause shortening, filler/false-start cuts (word timings verified), punch-in zooms on hooks, cards and B-roll slots. The review dashboard (Scenes tab → **Storyboard**, or `/storyboard/<pid>`; also saved as `Storyboards/<pid>/storyboard.html`) shows each beat as a planned frame with a timeline and per-item switches, plus Approve / Reject / Notes. *Send feedback to Claude* puts it in the inbox; `storyboard apply <pid>` applies approved beats once (undoable).

## Footage bin & timeline
Tab 1 is a media bin: add many clips (natural name order, duplicates refused), reorder by drag / arrows / sort (name, recording time, length), Skip a clip without deleting it, and a timeline with film strips, waveforms, multi-select, drag-to-reorder and back-to-back playback. Clip order drives scene order; analysing a new clip never reshuffles a hand-made scene order. After analysis a preview of the complete video renders automatically and **Next: Scenes →** leads on. Clips/scenes with no real speech (< 0.5 words/s, e.g. Whisper hearing words in music) get captions off automatically.

## Scenes editor
Timeline (blocks sized by edited length: click, drag to move, drag an edge to trim; B-roll and card lanes; keyboard
Space / arrows / S / Delete / I / O / Ctrl+Z) + Inspector (role, trim, speed, zoom, audio, captions, pauses, B-roll,
card, text, click-a-word caption fixes). **B-roll cutaways**: clips without speech become B-roll automatically and cover
speaker scenes while the voice keeps playing (top panel in the split layout, full screen otherwise; consecutive scenes
continue the clip's footage). Every control sends a normal command, so it is undoable and scriptable.

## Looks (one style per reference)
Every reference (R1, R2…) has its own look (all style settings) and its own brief; *None* is the simple settings. Page 2 edits any look without touching the video; the **Look** dropdown on the Scenes page chooses which look the video uses. Footage facts (language, face position, headline, spellings) stay project-wide. Each style setting has a **?** explaining what it does, why, and the recommended value.

## Library (transitions, effects, stickers, sound effects)

`Library/` (next to `Raw Footage/`, not in git) holds free assets for the Scenes → Inspector → ✨ Library panel:
~1,600 Microsoft Fluent Emoji 3D stickers (MIT), the 320-sound Wyzowl SFX Kit (CC0, with sync points), preview clips for
47 ffmpeg `xfade` transitions and 17 effect recipes. `Setup` downloads it (`_engines/studio/library.py setup`); add your own
files in `Library/Stickers/My stickers` and `Library/Sounds/My sounds`. Sources and licences: `Library/LICENSES.md`.
