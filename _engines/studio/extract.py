"""⬇ EXTRACT (Media tab) – get just the part you need out of a video, ready to download or keep:

    mp3     sound only      MP3, VBR ~245 kbps (LAME V0) – music / voice / sound effect from any clip
    mp4     video + sound   H.264 + AAC MP4 that plays everywhere (stream-copied, lossless, when it already is one)
    mute    video only      the same MP4 with the sound removed (for B-roll, a green-screen background, a transition)

From: a link (YouTube, Instagram, X, Facebook … anything yt-dlp reads), a file you drop, or a clip already in Media.
Optional from / to trims it (accurate to the frame: a trimmed MP4 is re-encoded, never cut on a keyframe jump).
Outputs land in Library/Extracted (not shown to the editor until you "Save to Media"); jobs run one at a time in the
background with live progress, so a 1-hour download doesn't block the studio.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import core
import library
import media as media_lib

OUT = library.LIB / "Extracted"
TMP = library.LIB / ".incoming" / "extract"
MODES = {"mp3": ("🎵 MP3 – sound only", ".mp3", " (audio)"), "mp4": ("🎞 MP4 – video + sound", ".mp4", ""),
         "mute": ("🔇 MP4 – video only, no sound", ".mp4", " (no sound)")}
MAX_JOBS_KEPT = 30
# 🎵 MP3 – which part of the sound: everything, or split by the voice-remover (Demucs, _engines/separate)
PARTS = {"all": ("whole sound", ""), "music": ("background music only – voice removed", " (music only)"),
         "voice": ("voice only – music removed", " (voice only)")}
SEP_MAX = 15 * 60  # separating holds the whole stretch in memory (7 GB PC): longer than this -> trim it first
SEP_SPEED = 1.0    # seconds of CPU work per second of sound (first estimate; the progress bar follows the clock)

_jobs: dict[str, dict] = {}
_order: list[str] = []
_lock = threading.Lock()
_run_lock = threading.Lock()  # one extraction at a time: two big downloads at once only make both slow


class ExtractError(Exception):
    pass


def parse_time(s) -> float | None:
    """'', None -> None; '75' / '1:15' / '0:01:15' / '1:15.5' / '1m15s' -> seconds."""
    s = str(s or "").strip().lower().replace(",", ".")
    if not s:
        return None
    m = re.fullmatch(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s?)?", s)
    if m and any(m.groups()) and not re.fullmatch(r"\d+(?:\.\d+)?", s):
        h, mi, se = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0)
    parts = s.split(":")
    if not 1 <= len(parts) <= 3 or not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        raise ExtractError(f"“{s}” isn't a time – write it like 1:15 or 75.")
    t = 0.0
    for p in parts:
        t = t * 60 + float(p)
    return t


def _fmt(t: float) -> str:
    t = int(round(t))
    return f"{t // 3600}h{t % 3600 // 60:02d}m{t % 60:02d}s" if t >= 3600 else f"{t // 60}m{t % 60:02d}s"


def _set(jid: str, **kw):
    with _lock:
        if jid in _jobs:
            _jobs[jid].update(kw)


def jobs() -> list[dict]:
    with _lock:
        return [dict(_jobs[j]) for j in reversed(_order)]


def _probe(p: Path) -> dict:
    r = subprocess.run([str(core.FFPROBE), "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name",
                        "-of", "json", str(p)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return {}
    j = json.loads(r.stdout or "{}")
    st = j.get("streams") or []
    v = next((s for s in st if s.get("codec_type") == "video" and s.get("codec_name") not in ("mjpeg", "png")), None)
    a = next((s for s in st if s.get("codec_type") == "audio"), None)
    return {"dur": float((j.get("format") or {}).get("duration") or 0), "v": (v or {}).get("codec_name"),
            "a": (a or {}).get("codec_name")}


def _popen_lines(cmd: list, on_line, timeout: float):
    """Run a tool, feeding each output line to on_line; raise ExtractError with its last lines on failure."""
    p = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    tail: list[str] = []
    t0 = time.time()
    for line in p.stdout:
        line = line.rstrip()
        if line:
            tail = (tail + [line])[-15:]
            on_line(line)
        if time.time() - t0 > timeout:
            p.kill()
            raise ExtractError("It took too long and was stopped.")
    p.wait()
    if p.returncode != 0:
        raise ExtractError("\n".join(tail))
    return tail


def _download(jid: str, url: str, mode: str, work: Path) -> tuple[Path, str]:
    fmt = "ba/b" if mode == "mp3" else "bv*[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<=1080]+ba/b[height<=1080]/b"
    if mode == "mute":
        fmt = "bv*[height<=1080][vcodec^=avc1]/bv*[height<=1080]/b[height<=1080]/b"
    meta = {"title": ""}

    def on(line):
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m:
            _set(jid, progress=round(float(m.group(1)) * 0.8, 1), stage="Downloading…")
        if line.startswith("TITLE:"):
            meta["title"] = line[6:].strip()

    cmd = [core.YTDLP, "--no-playlist", "--newline", "--no-part", "--match-filter", "!is_live", "-f", fmt,
           "--merge-output-format", "mp4" if mode != "mp3" else "mkv", "--ffmpeg-location", str(Path(core.FFMPEG).parent),
           "-o", str(work / "dl.%(ext)s"), "--print", "before_dl:TITLE:%(title)s", "--no-simulate", "--", url]
    try:
        _popen_lines(cmd, on, timeout=60 * 60 * 2)
    except ExtractError as e:
        t = str(e).lower()
        why = ("no internet, or the website can't be reached" if any(k in t for k in ("getaddrinfo", "failed to resolve", "timed out", "connection"))
               else "it's private or needs a login" if any(k in t for k in ("private", "sign in", "login", "log in", "cookies", "members"))
               else "it's a live stream – wait until it ends" if "is_live" in t or "live event" in t
               else "this website isn't supported" if "unsupported url" in t
               else "it was removed or the link is wrong" if any(k in t for k in ("404", "not found", "unavailable", "removed"))
               else "the download failed")
        raise ExtractError(f"Could not download it: {why}.") from e
    got = [p for p in work.iterdir() if p.is_file() and p.name.startswith("dl.")]
    if not got:
        raise ExtractError("The download finished but no file came out – try again, or download it yourself and drop the file here.")
    return max(got, key=lambda p: p.stat().st_size), meta["title"]


def _convert(jid: str, src: Path, mode: str, a: float | None, b: float | None, dest: Path):
    m = _probe(src)
    if not m:
        raise ExtractError("That file can't be read (damaged, or not a video / sound file).")
    if mode == "mp3" and not m["a"]:
        raise ExtractError("This video has no sound – nothing to make an MP3 from.")
    if mode in ("mp4", "mute") and not m["v"]:
        raise ExtractError("There's no picture in this file – choose 🎵 MP3 instead.")
    dur = m["dur"]
    if a is not None and dur and a >= dur:
        raise ExtractError(f"“From” ({_fmt(a)}) is after the end of the clip ({_fmt(dur)}).")
    b = None if b is None or (dur and b >= dur) else b
    span = ((b if b is not None else dur) - (a or 0)) or dur or 1
    trim = (a or 0) > 0 or b is not None
    pre = ["-ss", f"{a:.3f}"] if a else []
    post = ["-t", f"{(b - (a or 0)):.3f}"] if b is not None else []
    if mode == "mp3":
        enc = ["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-q:a", "0", "-map_metadata", "-1"]
    else:
        copy_v = not trim and m["v"] == "h264"
        venc = ["-c:v", "copy"] if copy_v else ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
        if mode == "mute" or not m["a"]:
            aenc = ["-an"]
        else:
            aenc = ["-c:a", "copy"] if (not trim and m["a"] == "aac") else ["-c:a", "aac", "-b:a", "192k"]
        enc = ["-map", "0:v:0", *(["-map", "0:a:0"] if aenc != ["-an"] else []), *venc, *aenc, "-movflags", "+faststart"]

    with _lock:
        base = 80.0 if (_jobs.get(jid) or {}).get("downloaded") else 0.0  # a download already filled 0-80 %

    def on(line):
        mm = re.match(r"out_time_us=(\d+)", line)
        if mm:
            frac = min(1.0, max(0.0, int(mm.group(1)) / 1e6 / span))
            _set(jid, progress=round(min(99.0, base + (100 - base) * frac), 1), stage="Converting…")

    tmp_out = dest.with_name(dest.stem + ".part" + dest.suffix)
    try:
        _popen_lines([core.FFMPEG, "-hide_banner", "-v", "error", "-y", *pre, "-i", src, *post, *enc,
                      "-progress", "pipe:1", "-nostats", tmp_out], on, timeout=60 * 60 * 3)
        if not tmp_out.is_file() or tmp_out.stat().st_size < 1024:
            raise ExtractError("Nothing came out – is the trim range inside the clip?")
        tmp_out.replace(dest)
    except ExtractError as e:
        tmp_out.unlink(missing_ok=True)
        msg = str(e)
        raise ExtractError(msg if len(msg) < 200 and "\n" not in msg else "Converting failed – the file may be damaged.") from e


def _name(stem: str, mode: str, a, b, part: str = "all") -> str:
    stem = re.sub(r"\s+", " ", core.safe_filename(stem or "extract")).strip(" .")[:80] or "extract"
    rng = f" {_fmt(a or 0)}-{_fmt(b)}" if b is not None else (f" from {_fmt(a)}" if a else "")
    tag = PARTS[part][1] if mode == "mp3" and part in PARTS and part != "all" else MODES[mode][2]
    return f"{stem}{tag}{rng}{MODES[mode][1]}"


def _separate(jid: str, src: Path, part: str, a: float | None, b: float | None, dest: Path, work: Path) -> str:
    """MP3 of ONLY the background music (voice removed) or ONLY the voice. Cuts the asked range FIRST (separating
    costs ~1 s of CPU per second of sound), separates, encodes. Returns a note for the user ('' = none)."""
    import refaudio
    if not refaudio.available():
        raise ExtractError("The voice-remover isn't installed – run Setup.bat once (it adds it), then try again. "
                           "“Whole sound” works without it.")
    m = _probe(src)
    if not m:
        raise ExtractError("That file can't be read (damaged, or not a video / sound file).")
    if not m["a"]:
        raise ExtractError("This video has no sound – nothing to take music or a voice from.")
    dur = m["dur"]
    if a is not None and dur and a >= dur:
        raise ExtractError(f"“From” ({_fmt(a)}) is after the end of the clip ({_fmt(dur)}).")
    b = None if b is None or (dur and b >= dur) else b
    span = ((b if b is not None else dur) - (a or 0)) or dur or 0
    if span > SEP_MAX:
        raise ExtractError(f"Separating music from voice works on up to {SEP_MAX // 60} minutes at a time (this is "
                           f"{_fmt(span)}) – set From / To to the part you need.")
    if span and span < 1.0:
        raise ExtractError("That's under a second – nothing to separate.")
    with _lock:
        base = 80.0 if (_jobs.get(jid) or {}).get("downloaded") else 0.0
    _set(jid, stage="Cutting…", progress=round(base + (100 - base) * 0.03, 1))
    cut = work / "cut.wav"
    _popen_lines([core.FFMPEG, "-hide_banner", "-v", "error", "-y", *(["-ss", f"{a:.3f}"] if a else []), "-i", src,
                  *(["-t", f"{(b - (a or 0)):.3f}"] if b is not None else []), "-vn", "-ac", "2", "-ar", "44100",
                  "-c:a", "pcm_s16le", cut], lambda _l: None, timeout=60 * 30)
    sep_out = work / "part.wav"
    est = max(20.0, span * SEP_SPEED + 15)  # + model load
    what = "music" if part == "music" else "voice"
    while not refaudio.SEP_LOCK.acquire(timeout=2):  # one separation at a time (memory): wait, and say so
        _set(jid, stage="Waiting – another voice / music separation is running…")
    try:
        p = subprocess.Popen([str(refaudio.PY), "-I", str(refaudio.RUNNER), str(core.FFMPEG), str(cut), str(sep_out),
                              "vocals" if part == "voice" else "music"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        t0 = time.time()
        while p.poll() is None:  # no progress from the model: the bar follows the clock (never claims 100 %)
            el = time.time() - t0
            frac = min(0.95, el / est)
            _set(jid, stage=f"Separating the {what} from the rest… {int(el)} s (about {int(est)} s)",
                 progress=round(base + (100 - base) * (0.05 + 0.85 * frac), 1))
            if el > 60 * 90:
                p.kill()
                raise ExtractError("Separating took over 90 minutes and was stopped.")
            time.sleep(1)
        out, err = p.communicate()
    finally:
        refaudio.SEP_LOCK.release()
    if p.returncode != 0 or not sep_out.is_file():
        last = ((err or out or "").strip().splitlines() or ["?"])[-1]
        raise ExtractError("Separating failed – " + ("the computer ran out of memory, try a shorter part"
                                                   if "memory" in last.lower() else last[:160]))
    _set(jid, stage="Making the MP3…", progress=round(base + (100 - base) * 0.95, 1))
    tmp_out = dest.with_name(dest.stem + ".part" + dest.suffix)
    try:
        _popen_lines([core.FFMPEG, "-hide_banner", "-v", "error", "-y", "-i", sep_out, "-c:a", "libmp3lame", "-q:a",
                      "0", "-map_metadata", "-1", tmp_out], lambda _l: None, timeout=60 * 30)
        tmp_out.replace(dest)
    finally:
        tmp_out.unlink(missing_ok=True)
    lv = re.search(r"LEVEL (-?[\d.]+) (-?[\d.]+)", out or "")
    if lv and float(lv.group(1)) < float(lv.group(2)) - 22:
        return ("Almost no background music in it – it's nearly all talking, so the file is very quiet."
                if part == "music" else "Almost no voice in it – it's nearly all music, so the file is very quiet.")
    return ""


def _run(jid: str, mode: str, a, b, url: str | None, src: Path | None, stem: str, tmp_src: bool, part: str = "all"):
    work = TMP / jid
    with _run_lock:
        try:
            work.mkdir(parents=True, exist_ok=True)
            _set(jid, status="running", stage="Downloading…" if url else "Converting…")
            if url:
                src, title = _download(jid, url, mode, work)
                stem = title or stem
                _set(jid, downloaded=True, title=stem)
            OUT.mkdir(parents=True, exist_ok=True)
            dest = core.unique_path(OUT, _name(stem, mode, a, b, part))
            note = ""
            if mode == "mp3" and part in ("music", "voice"):
                note = _separate(jid, src, part, a, b, dest, work)
            else:
                _convert(jid, src, mode, a, b, dest)
            _set(jid, status="done", progress=100, stage="Ready", file=dest.name, size=dest.stat().st_size, note=note)
        except ExtractError as e:
            _set(jid, status="error", stage="", error=str(e))
        except Exception as e:  # noqa: BLE001  a job must always end, never hang as "running"
            _set(jid, status="error", stage="", error=f"Something went wrong ({str(e)[:120]}).")
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if tmp_src and src:
                Path(src).unlink(missing_ok=True)


def start(mode: str, frm=None, to=None, url: str | None = None, src: Path | None = None, stem: str = "",
          tmp_src: bool = False, part: str = "all") -> dict:
    """Queue one extraction. Exactly one of url / src. Raises ExtractError for bad input (nothing queued).
    part (MP3 only): all | music (voice removed) | voice (music removed)."""
    part = part if part in PARTS and mode == "mp3" else "all"
    try:
        if mode not in MODES:
            raise ExtractError("Choose MP3, MP4 or video only.")
        if part != "all":
            import refaudio
            if not refaudio.available():
                raise ExtractError("The voice-remover isn't installed – run Setup.bat once (it adds it). "
                                   "“Whole sound” works without it.")
        a, b = parse_time(frm), parse_time(to)
        if a is not None and b is not None and b <= a:
            raise ExtractError("“To” must be after “From”.")
        if url:
            url = url.strip()
            if not re.match(r"https?://[^\s/$.?#][^\s]*$", url, re.I):
                raise ExtractError("Paste a full link that starts with https://")
        elif not src or not Path(src).is_file():
            raise ExtractError("Paste a link, or choose a file.")
    except ExtractError:
        if tmp_src and src:
            Path(src).unlink(missing_ok=True)
        raise
    jid = core.new_id("x")
    label = MODES[mode][0] + (f" · {PARTS[part][0]}" if part != "all" else "")
    job = {"id": jid, "mode": mode, "part": part, "mode_label": label, "status": "queued", "progress": 0, "stage": "Waiting…",
           "title": stem or (url or ""), "from": a, "to": b, "file": None, "error": "", "created": core.now_iso(), "url": url or ""}
    with _lock:
        _jobs[jid] = job
        _order.append(jid)
        while len(_order) > MAX_JOBS_KEPT:  # old finished jobs drop off the list (their files stay in Library/Extracted)
            old = next((j for j in _order if _jobs[j]["status"] in ("done", "error")), None)
            if not old:
                break
            _order.remove(old)
            _jobs.pop(old, None)
    threading.Thread(target=_run, args=(jid, mode, a, b, url, Path(src) if src else None, stem, tmp_src, part),
                     daemon=True).start()
    return dict(job)


def files() -> list[dict]:
    if not OUT.is_dir():
        return []
    out = []
    for p in sorted(OUT.iterdir(), key=lambda q: q.stat().st_mtime, reverse=True):
        if p.is_file() and p.suffix.lower() in (".mp3", ".mp4") and ".part." not in p.name:
            from urllib.parse import quote
            out.append({"name": p.name, "size": p.stat().st_size, "added": p.stat().st_mtime,
                        "url": "/media/_/library/" + quote(f"Extracted/{p.name}"), "audio": p.suffix.lower() == ".mp3"})
    return out


def path_of(name: str) -> Path:
    p = OUT / Path(str(name)).name
    if not p.is_file() or p.resolve().parent != OUT.resolve():
        raise ExtractError("That file is no longer there – the page will refresh.")
    return p


def remove(name: str) -> str:
    p = path_of(name)
    t = library.LIB / ".trash" / "extracted"
    t.mkdir(parents=True, exist_ok=True)
    shutil.move(str(p), core.unique_path(t, p.name))
    return f"Deleted “{p.name}” (kept in Library/.trash/extracted)."


def to_media(name: str, kind: str | None) -> dict:
    """Keep a copy in Media (sorted like a dropped file, or into `kind`); the extracted file stays for downloading."""
    p = path_of(name)
    inc = library.LIB / ".incoming"
    inc.mkdir(parents=True, exist_ok=True)
    tmp = inc / f"{core.new_id('up')}{p.suffix.lower()}"
    shutil.copy2(p, tmp)
    return media_lib.add_file(tmp, p.name, kind if kind in media_lib.KINDS else None)
