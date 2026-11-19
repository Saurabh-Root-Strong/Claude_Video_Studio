"""Paths, engine locations and project storage for CVE Studio.

A project lives in  Storyboards/<id>/project.json  (the edit plan).
Uploads go to       Raw Footage/<id>/   and   References/<id>/
Work files go to    Storyboards/<id>/work/
Exports go to       Exports/
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

# data root = the repo/workspace folder that contains _engines/ (…/<root>/_engines/studio/core.py)
ROOT = Path(os.environ.get("CVE_ROOT", Path(__file__).resolve().parents[2]))
RAW = ROOT / "Raw Footage"
REFS = ROOT / "References"
STORY = ROOT / "Storyboards"
EXPORTS = ROOT / "Exports"
BRAND = ROOT / "Brand Kit"
# engines live next to this code (…/_engines/studio/core.py), independent of where the DATA root is
ENGINES = Path(os.environ.get("CVE_ENGINES", Path(__file__).resolve().parent.parent))

_LINKS = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"


def _tool(name: str) -> str:
    p = _LINKS / f"{name}.exe"
    if p.exists():
        return str(p)
    found = shutil.which(name)
    return found or name


FFMPEG = _tool("ffmpeg")
FFPROBE = _tool("ffprobe")
YTDLP = _tool("yt-dlp")
WHISPER_GPU = ENGINES / "whisper" / "cuda" / "Release" / "whisper-cli.exe"
WHISPER_CPU = ENGINES / "whisper" / "cpu" / "Release" / "whisper-cli.exe"
WHISPER_MODEL = ENGINES / "whisper" / "models" / "ggml-large-v3-turbo-q5_0.bin"

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts", ".wmv"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

DEFAULT_SETTINGS = {
    "format": "original",       # original | 16:9 | 9:16 | 1:1
    "fit": "crop",              # crop | pad
    "fps": 30,
    "captions": True,
    "caption_font": "Arial",
    "caption_color": "#FFFFFF",
    "highlight_color": "#F97316",
    "caption_position": "bottom",  # bottom | center | top
    "caption_case": "normal",      # normal | upper
    "caption_words": 4,
    "remove_silence": False,
    "silence_min": 0.6,
    "logo": None,               # file name inside Brand Kit
    "logo_position": "top-right",
    "language": "auto",
}

_lock = threading.RLock()
PID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class _FileLock:
    """Cross-process lock (the web server and studio_cli.py both write project.json)."""

    def __init__(self, path: Path, timeout: float = 30.0):
        self.path, self.timeout, self.fd = path, timeout, None

    def _try(self) -> None:
        if os.name == "nt":
            import msvcrt
            os.lseek(self.fd, 0, 0)
            msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
        else:  # macOS / Linux
            import fcntl
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
        deadline = time.time() + self.timeout
        while True:
            try:
                self._try()
                return self
            except OSError:
                if time.time() > deadline:
                    os.close(self.fd)
                    raise TimeoutError(f"project is locked by another process: {self.path}")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self.fd, 0, 0)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def valid_pid(pid: str) -> bool:
    return bool(PID_RE.match(pid or "")) and (STORY / pid / "project.json").is_file()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").lower()
    return s[:40] or "project"


def safe_filename(name: str) -> str:
    name = Path(name).name
    stem, ext = os.path.splitext(name)
    stem = re.sub(r"[^\w\-. ()\[\]]+", "_", stem, flags=re.UNICODE).strip(" .") or "file"
    return f"{stem[:80]}{ext.lower()}"


def unique_path(folder: Path, name: str) -> Path:
    p = folder / name
    stem, ext = os.path.splitext(name)
    i = 2
    while p.exists():
        p = folder / f"{stem} ({i}){ext}"
        i += 1
    return p


def project_dir(pid: str) -> Path:
    return STORY / pid


def work_dir(pid: str) -> Path:
    d = project_dir(pid) / "work"
    d.mkdir(parents=True, exist_ok=True)
    return d


def raw_dir(pid: str) -> Path:
    d = RAW / pid
    d.mkdir(parents=True, exist_ok=True)
    return d


def refs_dir(pid: str) -> Path:
    d = REFS / pid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pfile(pid: str) -> Path:
    return project_dir(pid) / "project.json"


def new_project(name: str) -> dict:
    base = slugify(name)
    pid = base
    i = 2
    while project_dir(pid).exists():
        pid = f"{base}-{i}"
        i += 1
    project_dir(pid).mkdir(parents=True)
    work_dir(pid)
    data = {
        "id": pid,
        "name": name.strip() or pid,
        "created": now_iso(),
        "version": 0,
        "brief": "",
        "settings": copy.deepcopy(DEFAULT_SETTINGS),
        "sources": [],
        "references": [],
        "scenes": [],
        "commands": [],
        "renders": [],
        "job": None,
    }
    save(pid, data)
    return data


def load(pid: str) -> dict:
    with _lock:
        with open(_pfile(pid), "r", encoding="utf-8") as f:
            data = json.load(f)
    # forward-fill new settings keys
    for k, v in DEFAULT_SETTINGS.items():
        data.setdefault("settings", {}).setdefault(k, v)
    data.pop("history", None)  # undo history lives in history.json (pre-audit projects kept it inline)
    return data


def _atomic_write(p: Path, obj) -> None:
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    for attempt in range(20):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:  # file momentarily open (antivirus / reader)
            time.sleep(0.05 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    raise PermissionError(f"could not write {p} (file kept open by another program?)")


def save(pid: str, data: dict) -> None:
    with _lock:
        data["version"] = int(data.get("version", 0)) + 1
        data["updated"] = now_iso()
        _atomic_write(_pfile(pid), data)


def mutate(pid: str, fn):
    """Load, apply fn(data), save – atomically across threads AND processes."""
    with _lock, _FileLock(project_dir(pid) / ".lock"):
        data = load(pid)
        result = fn(data)
        save(pid, data)
        return data, result


# ---- undo history (separate file so polling project.json stays small)

def _hfile(pid: str) -> Path:
    return project_dir(pid) / "history.json"


def snapshot(pid: str, data: dict) -> None:
    """Push scenes + settings onto the undo stack (max 30). Call inside mutate()."""
    hist = read_json(_hfile(pid), []) or []
    hist.append({"at": now_iso(), "scenes": copy.deepcopy(data.get("scenes", [])),
                 "settings": copy.deepcopy(data.get("settings", {}))})
    _atomic_write(_hfile(pid), hist[-30:])


def drop_snapshot(pid: str) -> None:
    hist = read_json(_hfile(pid), []) or []
    if hist:
        _atomic_write(_hfile(pid), hist[:-1])


def pop_snapshot(pid: str) -> dict | None:
    hist = read_json(_hfile(pid), []) or []
    if not hist:
        return None
    snap = hist.pop()
    _atomic_write(_hfile(pid), hist)
    return snap


def history_depth(pid: str) -> int:
    return len(read_json(_hfile(pid), []) or [])


def list_projects() -> list[dict]:
    out = []
    if not STORY.exists():
        return out
    for d in sorted(STORY.iterdir()):
        pf = d / "project.json"
        if pf.exists():
            try:
                p = load(d.name)
                out.append({"id": p["id"], "name": p["name"], "updated": p.get("updated", p.get("created")),
                            "scenes": len(p.get("scenes", []))})
            except Exception:  # corrupt / partial file – skip, don't crash listing
                continue
    out.sort(key=lambda x: x.get("updated") or "", reverse=True)
    return out


def set_job(pid: str, **fields) -> None:
    def fn(d):
        job = d.get("job") or {}
        job.update(fields)
        d["job"] = job
    mutate(pid, fn)


def job_busy(data: dict) -> bool:
    j = data.get("job") or {}
    return j.get("status") in ("running", "queued")


def job_log(pid: str, line: str) -> None:
    def fn(d):
        job = d.get("job") or {}
        log = job.setdefault("log", [])
        log.append(f"{datetime.now():%H:%M:%S}  {line}")
        del log[:-60]
        d["job"] = job
    mutate(pid, fn)


def source_by_file(data: dict, file: str) -> dict | None:
    for s in data.get("sources", []):
        if s["file"] == file:
            return s
    return None


def scene_by_id(data: dict, sid: str) -> dict | None:
    for s in data.get("scenes", []):
        if s["id"] == sid:
            return s
    return None


def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
