"""🎵 BACKGROUND AUDIO OF A REFERENCE: a look can play its reference video's background music under your voice –
with the reference's own VOICE taken out (Demucs htdemucs by Meta, MIT licence, runs on the CPU in its own folder
_engines/separate so the studio stays light). Separated once per reference file, cached next to it:
    References/<project>/.bgaudio/<reference name>.m4a
Setting (per look):  settings["ref_audio"] = "none" | "ref:<reference id>"
When on it REPLACES the Music bed (one bed, never two) and ducks under the voice like music does.
Music inside someone else's video is usually copyrighted: fine to practise with, risky to publish – the UI says so.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import core

SEP = core.ENGINES / "separate"
PY = SEP / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
RUNNER = SEP / "run_sep.py"


class RefAudioError(RuntimeError):
    pass


import threading  # noqa: E402

# ONE separation at a time in this studio (a reference's music, an Extract "music / voice only", a Studio voice): each
# holds minutes of audio + the model in memory – two at once on a 7 GB PC is how the machine runs out of memory
SEP_LOCK = threading.Lock()


_AVAIL = {"t": 0.0, "v": False}


def available() -> bool:
    """Is the voice-remover installed? Cached 60 s – the page asks on every 2-second poll and a recursive search of
    the .venv (thousands of files) each time was pure waste."""
    import time
    now = time.monotonic()
    if now - _AVAIL["t"] > 60:
        sp = [SEP / ".venv" / "Lib" / "site-packages", *(SEP / ".venv" / "lib").glob("python*/site-packages")]
        _AVAIL.update(t=now, v=PY.is_file() and RUNNER.is_file() and any((d / "demucs" / "__init__.py").is_file() for d in sp))
    return _AVAIL["v"]


def ref_of(data: dict, value) -> dict | None:
    m = re.fullmatch(r"ref:(.+)", str(value or ""))
    return next((r for r in data.get("references", []) if r.get("id") == m.group(1)), None) if m else None


def cache_path(pid: str, ref: dict) -> Path:
    return core.refs_dir(pid) / ".bgaudio" / f"{Path(ref['file']).stem}.m4a"


def ready(pid: str, ref: dict) -> bool:
    out, src = cache_path(pid, ref), core.refs_dir(pid) / ref["file"]
    return out.is_file() and out.stat().st_size > 1000 and (not src.is_file() or out.stat().st_mtime >= src.stat().st_mtime)


def bg_audio(pid: str, ref: dict, log=None) -> Path:
    """The reference's background music (voice removed); separated on first use (~1 min per minute on the CPU)."""
    out = cache_path(pid, ref)
    if ready(pid, ref):
        return out
    src = core.refs_dir(pid) / ref["file"]
    if not src.is_file():
        raise RefAudioError(f"The reference file “{ref['file']}” is missing.")
    import pipeline
    if not pipeline.probe(src).get("has_audio"):
        raise RefAudioError(f"“{ref['file']}” has no sound – nothing to take the background music from.")
    if not available():
        raise RefAudioError("The voice-remover (Demucs) isn't installed – run Setup.bat once (it adds it), then try again.")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.m4a")
    if log:
        log(f"Taking the voice out of “{ref['file']}” – first time only, about a minute per minute of video…")
    with SEP_LOCK:
        r = subprocess.run([str(PY), "-I", str(RUNNER), str(core.FFMPEG), str(src), str(tmp)], capture_output=True,
                           text=True, timeout=3600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise RefAudioError("Separating the background music failed: " + ((r.stderr or r.stdout).strip().splitlines() or ["?"])[-1][:200])
    os.replace(tmp, out)
    m = re.search(r"LEVEL (-?[\d.]+) (-?[\d.]+)", r.stdout or "")
    if m and log and float(m.group(1)) < float(m.group(2)) - 25:
        log(f"Note: “{ref['file']}” has almost no background music (it is nearly all voice) – the bed will be very quiet.")
    return out


def label(data: dict, value) -> str:
    r = ref_of(data, value)
    if not r:
        return ""
    i = data["references"].index(r) + 1
    return f"R{i}"
