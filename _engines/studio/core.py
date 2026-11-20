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
    "caption_y": None,  # exact caption height for the WHOLE video, % from the top (None = the look's position)
    "caption_case": "normal",      # normal | upper
    "caption_words": 4,
    "caption_script": "original",  # original (as transcribed, e.g. Hindi script) | hinglish (Hindi in English letters)
    "caption_fixes": {},           # word -> replacement, from the "spell X as Y" command
    "remove_silence": False,
    "silence_min": 0.6,
    "logo": None,               # file name inside Brand Kit
    "logo_position": "top-right",
    "language": "auto",
    # split layout (Brand Kit "Clean Split"): cards on top, speaker below, captions on the seam
    "layout": "full",              # full | split
    "split_ratio": 0.44,           # top panel share of the height
    "headline": "",                # default title card when a scene has no card of its own
    "bg_mode": "auto",             # auto = replace a detected green screen with bg_color | off
    "bg_color": "#F4F1EC",
    "panel_color": "#0F1B2D",
    "face_y": 0.27,                # where the speaker's eyes are in the source (0 top .. 1 bottom), for framing
    "grade": True,                 # light brightness/contrast lift on the speaker
    # final mix
    "audio_polish": False,         # high-pass + denoise + compression + loudness -16 LUFS
    "music": "off",                # off | auto (Brand Kit/music, else a generated soft pad) | file in Brand Kit/music
    "loudness": -16,               # final speech loudness, LUFS (audio_polish)
    "duck_db": 10,                 # how far music drops under dialogue, dB
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
    s = s[:40].strip("-") or "project"
    return f"{s}-project" if s.upper() in _WIN_RESERVED else s  # a folder can't be called "con" on Windows


def safe_filename(name: str) -> str:
    name = Path(name).name
    stem, ext = os.path.splitext(name)
    # keep any language as written (Hindi vowel signs are Unicode "marks", which \w drops – "मार्केट" became
    # "म_र_क_ट"); only characters Windows forbids in names, control characters and quotes are replaced
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", stem).strip(" .") or "file"
    if stem.split(".")[0].upper() in _WIN_RESERVED:  # "con.mp4" / "nul.mov" can't be created on Windows
        stem = f"_{stem}"
    return f"{stem[:80]}{ext.lower()}"


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


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
        "settings": plain_settings(),
        "sources": [],
        "references": [],
        "scenes": [],
        "commands": [],
        "renders": [],
        "job": None,
        # a new video starts on "None · simple settings"; the Look dropdown (Scenes) switches it to ★ Brand Kit / R1…
        "style_source": "none",
        "looks": {},
    }
    save(pid, data)
    return data


# ---- looks: every reference carries its own style preset; the project renders with the ACTIVE one.
# Only these keys belong to a look. Language, face position, headline and caption spellings describe the footage /
# this video, so they stay with the project whichever look is active.
STYLE_KEYS = ("format", "fit", "fps", "captions", "caption_script", "caption_font", "caption_color", "highlight_color",
              "caption_position", "caption_case", "caption_words", "remove_silence", "silence_min", "logo",
              "logo_position", "layout", "split_ratio", "bg_mode", "bg_color", "panel_color", "grade",
              "audio_polish", "music", "loudness", "duck_db")


def ref_id(r: dict) -> str:
    """Stable id for a reference (old projects stored none): derived from its file/url, so it never changes."""
    import hashlib
    if r.get("id"):
        return r["id"]
    return "r_" + hashlib.sha1(str(r.get("file") or r.get("url") or r.get("added")).encode()).hexdigest()[:8]


def style_of(settings: dict) -> dict:
    return {k: copy.deepcopy(settings[k]) for k in STYLE_KEYS if k in settings}


def derive_look(base: dict, ref: dict) -> tuple[dict, list[str]]:
    """First look for a reference: the simple settings plus what can be read off the reference automatically.
    Claude refines it on request (deep analysis like the Brand Kit study)."""
    look = copy.deepcopy(base)
    notes = []
    w, h = ref.get("width") or 0, ref.get("height") or 0
    if w and h:
        fmt = "9:16" if h > w * 1.2 else "16:9" if w > h * 1.2 else "1:1"
        look["format"] = fmt
        notes.append(f"format {fmt} (reference is {w}×{h})")
    if ref.get("avg_shot"):
        notes.append(f"pacing: a new shot every ~{ref['avg_shot']}s")
    return look, notes


_SIG_SKIP = {"thumb", "transcript", "notes", "no", "dur", "est", "caption_view", "src_skipped", "sb_applied",
             "broll_view", "start_at", "lib_missing"}


def edit_sig(d: dict) -> str:
    """Fingerprint of everything that changes the rendered video (scenes, settings, clip order/skip).
    Stored with each render; if the project's fingerprint differs, that render is out of date."""
    import hashlib
    scenes = [{k: v for k, v in s.items() if k not in _SIG_SKIP} for s in d.get("scenes", [])]
    srcs = [(s.get("file"), s.get("include", True)) for s in d.get("sources", [])]
    blob = json.dumps([scenes, d.get("settings", {}), srcs], sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


BRANDKIT = "brandkit"  # the Brand Kit as a look: read-only, read live from Brand Kit/brand-kit.json


def brandkit_look() -> dict | None:
    """Style values of the Brand Kit (validated), or None if there is no usable brand-kit.json.
    It is shared by every project, so the studio never edits it from a project page."""
    kit = read_json(BRAND / "brand-kit.json", None)
    if not isinstance(kit, dict) or not isinstance(kit.get("settings"), dict):
        return None
    look = style_of(DEFAULT_SETTINGS)
    for k, v in kit["settings"].items():
        if k in STYLE_KEYS:
            ok, val = clean_setting(k, v)
            if ok:
                look[k] = val
    return look


def brand_settings() -> dict:
    """Defaults for a NEW project: built-in defaults overlaid with the approved Brand Kit (Brand Kit/brand-kit.json,
    key "settings"). Invalid or unknown keys in that file are ignored, never stored."""
    s = copy.deepcopy(DEFAULT_SETTINGS)
    kit = read_json(BRAND / "brand-kit.json", {}) or {}
    for k, v in (kit.get("settings") or {}).items() if isinstance(kit, dict) else []:
        if k in DEFAULT_SETTINGS:
            ok, val = clean_setting(k, v)
            if ok:
                s[k] = val
    return s


def plain_settings() -> dict:
    """Settings of a NEW project: the plain look ("None · simple") for every STYLE key; the Brand Kit's footage keys
    (language, face position… – they describe you/your camera, not a look) still apply."""
    s = brand_settings()
    s.update(copy.deepcopy({k: DEFAULT_SETTINGS[k] for k in STYLE_KEYS if k in DEFAULT_SETTINGS}))
    return s


class ProjectDamaged(Exception):
    pass


def _recover(pid: str) -> dict:
    """project.json is unreadable (power cut / disk error / hand edit): restore the last good copy (.bak, written
    before every save). The damaged file is kept next to it for inspection – never deleted."""
    pf = _pfile(pid)
    bak = pf.with_name("project.json.bak")
    try:
        with open(bak, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        raise ProjectDamaged(f"Project '{pid}' is damaged and has no readable backup "
                             f"(Storyboards/{pid}/project.json). Your footage is safe in Raw Footage/{pid}.") from None
    try:
        shutil.copyfile(pf, pf.with_name(f"project.json.damaged-{now_iso().replace(':', '')}"))
    except OSError:
        pass
    _atomic_write(pf, data)
    try:
        log = data.setdefault("job", {}).setdefault("log", [])
        log.append(f"{now_iso()[11:19]}  project file was damaged – restored the last good copy (your latest change may be missing)")
        _atomic_write(pf, data)
    except Exception:  # noqa: BLE001 – the restore itself already succeeded
        pass
    return data


def load(pid: str) -> dict:
    # On Windows, opening the file in the instant another process os.replace()s it raises PermissionError,
    # so retry briefly instead of failing a page load or a job.
    for attempt in range(20):
        try:
            with _lock:
                with open(_pfile(pid), "r", encoding="utf-8") as f:
                    data = json.load(f)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)
        except json.JSONDecodeError:
            if attempt < 3:  # a write landing right now – retry briefly first
                time.sleep(0.05)
                continue
            with _lock:
                data = _recover(pid)
            break
    # forward-fill new settings keys
    for k, v in DEFAULT_SETTINGS.items():
        data.setdefault("settings", {}).setdefault(k, v)
    data.pop("history", None)  # undo history lives in history.json (pre-audit projects kept it inline)
    for r in data.get("references", []):  # stable ids (looks are keyed by them)
        r["id"] = ref_id(r)
    data.setdefault("looks", {})
    data.setdefault("style_source", "none")
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
        pf = _pfile(pid)
        try:  # last good copy for _recover(); only from a file that still parses (never back up damage)
            with open(pf, "r", encoding="utf-8") as f:
                json.load(f)
            shutil.copyfile(pf, pf.with_name("project.json.bak"))
        except (OSError, json.JSONDecodeError):
            pass
        _atomic_write(pf, data)


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


def snapshot(pid: str, data: dict, restore_assets: list[str] | None = None) -> None:
    """Push scenes + settings onto the undo stack (max 30). Call inside mutate().
    restore_assets: uploaded B-roll files this step moves to Assets/.trash – undo moves them back."""
    hist = read_json(_hfile(pid), []) or []
    hist.append({"at": now_iso(), "scenes": copy.deepcopy(data.get("scenes", [])),
                 "settings": copy.deepcopy(data.get("settings", {})),
                 "sources": copy.deepcopy(data.get("sources", [])),  # undo can bring back a removed clip's entry
                 **({"restore_assets": list(restore_assets)} if restore_assets else {})})
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


def untrash_assets(pid: str, data: dict, names: list[str] | None = None) -> None:
    """Move uploads back from Assets/.trash: the ones named, and any a scene points at again (after an undo)."""
    trash = assets_dir(pid) / ".trash"
    if not trash.is_dir():
        return
    want = set(names or []) | {s["broll"].get("src") for s in data.get("scenes", []) if isinstance(s.get("broll"), dict)}
    for n in want:
        if not isinstance(n, str) or Path(n).name != n:
            continue
        t, dest = trash / n, assets_dir(pid) / n
        if t.is_file() and not dest.exists():
            try:
                t.replace(dest)
            except OSError:
                pass  # still usable from the trash (broll_path looks there)


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
            except Exception:  # damaged beyond repair: still LIST it (a vanished project is worse than a broken one)
                out.append({"id": d.name, "name": f"{d.name} (damaged – can't open)", "updated": "", "scenes": 0,
                            "damaged": True})
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


def server_alive(port: int | None = None) -> bool:
    import socket
    port = port or int(os.environ.get("CVE_PORT", "8070"))
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def reset_stale_jobs() -> int:
    """At server start nothing can be running: a 'running'/'queued' job means the studio was closed mid-job.
    Without this the project stays 'busy' forever (buttons disabled, CLI refuses)."""
    n = 0
    for p in list_projects():
        def fn(d):
            j = d.get("job") or {}
            if j.get("status") in ("running", "queued") or j.get("pending"):
                j.update(status="error", error="Interrupted – the studio was closed while this was running. "
                                                "Start it again.", progress="Interrupted", pending=[])
                d["job"] = j
                return True
            return False
        try:
            _, changed = mutate(p["id"], fn)
            n += bool(changed)
        except Exception:
            continue
    return n


def work_file(pid: str, src_file: str, suffix: str) -> Path:
    """Per-source work file, keyed by the FULL file name (clip.mp4 and clip.mov must not share files).
    Falls back to the pre-audit stem-based name so existing projects keep working."""
    wd = project_dir(pid) / "work"
    new = wd / f"{src_file}.{suffix}"
    if new.exists():
        return new
    legacy = wd / f"{Path(src_file).stem}.{suffix}"
    return legacy if legacy.exists() else new


def job_log(pid: str, line: str) -> None:
    def fn(d):
        job = d.get("job") or {}
        log = job.setdefault("log", [])
        log.append(f"{datetime.now():%H:%M:%S}  {line}")
        del log[:-60]
        d["job"] = job
    mutate(pid, fn)


_ENUMS = {"format": {"original", "16:9", "9:16", "1:1"}, "fit": {"crop", "pad"},
          "caption_position": {"bottom", "center", "top"}, "caption_case": {"normal", "upper"},
          "caption_script": {"original", "hinglish"}, "layout": {"full", "split"}, "bg_mode": {"auto", "off"},
          "logo_position": {"top-right", "top-left", "bottom-right", "bottom-left"}}
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def clean_setting(key: str, value):
    """Validate/coerce one setting. Returns (ok, value). Bad input is rejected, never stored (it would break renders)."""
    try:
        if key in _ENUMS:
            return (value in _ENUMS[key]), value
        if key in ("captions", "remove_silence", "grade", "audio_polish"):
            return True, value in (True, "true", "1", 1, "on")
        if key in ("caption_color", "highlight_color", "bg_color", "panel_color"):
            return bool(_HEX.match(str(value))), str(value).upper()
        if key == "caption_y":
            if value in (None, "", "auto", "none"):
                return True, None
            v = round(float(value), 1)
            return 5 <= v <= 95, v
        if key == "caption_words":
            return True, max(1, min(int(value), 8))
        if key == "silence_min":
            return True, max(0.2, min(float(value), 3.0))
        if key == "fps":
            v = int(value)
            return v in (24, 25, 30, 50, 60), v
        if key == "caption_font":  # commas/braces would corrupt the subtitle style line
            v = re.sub(r"[,{}\\\r\n]", "", str(value)).strip()[:60]
            return True, v or "Arial"
        if key == "loudness":
            return True, round(max(-24.0, min(float(value), -10.0)), 1)
        if key == "duck_db":
            return True, round(max(0.0, min(float(value), 30.0)), 1)
        if key == "split_ratio":
            return True, round(max(0.25, min(float(value), 0.60)), 3)
        if key == "face_y":
            return True, round(max(0.0, min(float(value), 1.0)), 3)
        if key == "headline":
            return True, re.sub(r"[{}\\\r\n]", " ", str(value or "")).strip()[:100]
        if key == "music":
            v = str(value or "off").strip()
            if v in ("off", "auto", "none", ""):
                return True, "auto" if v == "auto" else "off"
            v = Path(v).name
            return (BRAND / "music" / v).is_file(), v
        if key == "caption_fixes":
            if not isinstance(value, dict) or len(value) > 1000:
                return False, value
            from hinglish import norm_key  # local import: hinglish has no deps, core must not import pipeline
            out = {}
            for k, v in value.items():
                k2, v2 = norm_key(str(k)), re.sub(r"[{}\\\r\n]", "", str(v)).strip()[:80]
                if not k2:
                    return False, value
                out[k2] = v2
            return True, out
        if key == "language":
            v = str(value).strip().lower()
            return bool(re.fullmatch(r"auto|[a-z]{2,3}", v)), v
        if key == "logo":
            if value in (None, "", "none"):
                return True, None
            v = Path(str(value)).name
            return (BRAND / v).is_file() and Path(v).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}, v
    except (TypeError, ValueError):
        return False, value
    return False, value


def source_by_file(data: dict, file: str) -> dict | None:
    for s in data.get("sources", []):
        if s["file"] == file:
            return s
    return None


def broll_path(pid: str, data: dict, name: str | None) -> Path | None:
    """File behind a scene's B-roll cutaway: a project clip (not skipped, still on disk) or Raw Footage/<pid>/Assets/<name>.
    None = the cutaway can't be shown (the scene then renders without it)."""
    if not name or not isinstance(name, str):
        return None
    s = source_by_file(data, name)
    if s is not None:
        p = raw_dir(pid) / name
        return p if s.get("include", True) is not False and p.is_file() else None
    p = raw_dir(pid) / "Assets" / Path(name).name
    if p.is_file():
        return p
    t = raw_dir(pid) / "Assets" / ".trash" / Path(name).name  # removed upload: an undo may still point at it
    return t if t.is_file() else None


BROLL_WHERE = ("full", "top", "bottom")  # full screen | top half / top panel | bottom half / over the speaker panel


def broll_where(b: dict | None, split: bool) -> str:
    """Where a scene's B-roll shows. Not chosen = follow the layout (split -> top panel, full layout -> full screen)."""
    w = (b or {}).get("where")
    return w if w in BROLL_WHERE else ("top" if split else "full")


def is_still(name: str) -> bool:
    """A picture (not an animated GIF): shown as a still for the whole scene."""
    ext = os.path.splitext(str(name))[1].lower()
    return ext in IMAGE_EXT and ext != ".gif"


def assets_dir(pid: str) -> Path:
    return raw_dir(pid) / "Assets"


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
