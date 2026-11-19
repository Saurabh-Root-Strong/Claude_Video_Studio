# Claude Video Studio — notes for Claude Code

Local video editor where Claude is the editor. The user edits in the browser (http://127.0.0.1:8070) and asks
Claude to process an inbox of commands the UI can't do instantly.

- **New PC / fresh clone:** run `Setup.bat` (or `powershell -ExecutionPolicy Bypass -File setup.ps1`). It installs
  FFmpeg, yt-dlp, Python venv, whisper.cpp (CUDA if NVIDIA) + cuBLAS + models, Node + Remotion/HyperFrames.
  Idempotent. `-CpuOnly`, `-SkipMotion`, `-RunTests` switches.
- **Start:** `Start Studio.bat` (offers setup if missing).
- **"process studio inbox":** follow [_engines/studio/CLAUDE.md](_engines/studio/CLAUDE.md).
- **After changing studio code:** `_engines\studio\.venv\Scripts\python.exe _engines\studio\selftest.py` (23 checks,
  temp folder) and restart the server.
- **Never commit** footage/exports/projects/models — `.gitignore` is a whitelist; keep it that way. Repo is public.
- Machine notes: one heavy job at a time (designed for 8 GB RAM); HyperFrames `init` always with `--skip-skills`.
