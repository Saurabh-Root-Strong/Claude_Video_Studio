"""📁 MEDIA – one place to drop your own audio, music, videos, transitions, stickers and pictures. Each file is sorted
into the folder where the studio already looks for that kind of thing, so it is usable at once in every project:

    music        Brand Kit/music               Music setting, "scene 6 calm music", Audio tracks
    sounds       Library/Sounds/My sounds      Library ▸ Sounds, "cash sound", auto-measured hit point + loudness
    stickers     Library/Stickers/My stickers  Library ▸ Stickers, "money sticker" (PNG / WebP / GIF, transparent)
    pictures     Library/Pictures              "+ this project" -> a B-roll picture
    videos       Library/Videos                "+ this project" -> a B-roll / Video-track clip
    transitions  Library/Transitions           short overlay clips (whoosh / swipe / light leak) for a Video track

How it decides (you can move a file to another kind afterwards):
  audio  <= 15 s -> sounds, longer -> music        image with transparency (or a GIF) -> stickers, else pictures
  video  name says transition / swipe / wipe / light leak / glitch… or <= 2.5 s -> transitions, else videos
Every file is test-decoded first (a renamed / damaged file is refused, not stored); the same file twice is kept once;
names keep any language (Hindi) and are made safe for Windows; deleting moves the file to Library/.trash.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import core
import library

KINDS = {
    "music": ("Music", "🎵", "background music – Music setting, “scene 6 calm music”, Audio tracks"),
    "sounds": ("Sound effects", "🔊", "short sounds – Library ▸ Sounds, “cash sound”"),
    "stickers": ("Stickers", "🙂", "transparent pictures / GIFs that pop up – Library ▸ Stickers"),
    "pictures": ("Pictures", "🖼", "photos – add to a project as B-roll"),
    "videos": ("Videos", "🎞", "clips – add to a project as B-roll or on a Video track"),
    "transitions": ("Transitions", "✨", "short overlay clips (swipe, light leak, glitch) – on a Video track over a cut"),
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXT = set(core.VIDEO_EXT) | {".gif"}
AUDIO_EXT = set(core.AUDIO_EXT)
ACCEPT = IMAGE_EXT | VIDEO_EXT | AUDIO_EXT
STICKER_OK = library.STICKER_EXT  # png / gif / webp
TRANS_WORDS = re.compile(r"transition|swipe|wipe|whoosh|light[ _-]?leak|glitch|flash|zoom[ _-]?blur|swish|slide|shutter|"
                         r"film[ _-]?burn|burn|spin", re.I)
SOUND_MAX = 15.0
TRANS_MAX = 2.5
MAX_BYTES = 2 * 1024 ** 3


def folder(kind: str) -> Path:
    return {"music": core.BRAND / "music", "sounds": library.MY_SOUNDS, "stickers": library.MY_STICKERS,
            "pictures": library.LIB / "Pictures", "videos": library.LIB / "Videos",
            "transitions": library.LIB / "Transitions"}[kind]


def url_of(kind: str, p: Path) -> str:
    from urllib.parse import quote
    if kind == "music":
        return "/media/_/brand/" + quote(f"music/{p.name}")
    return "/media/_/library/" + quote(p.relative_to(library.LIB).as_posix())


def _probe(p: Path) -> dict:
    r = subprocess.run([str(core.FFPROBE), "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,pix_fmt,width,height,nb_frames", "-of", "json", str(p)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return {}
    j = json.loads(r.stdout or "{}")
    st = j.get("streams") or []
    v = next((s for s in st if s.get("codec_type") == "video"), None)
    return {"dur": float((j.get("format") or {}).get("duration") or 0), "has_audio": any(s.get("codec_type") == "audio" for s in st),
            "has_video": v is not None, "pix_fmt": (v or {}).get("pix_fmt") or "", "w": (v or {}).get("width"),
            "h": (v or {}).get("height"), "frames": int((v or {}).get("nb_frames") or 0) if str((v or {}).get("nb_frames") or "").isdigit() else 0}


def _decodes(p: Path, audio: bool) -> bool:
    args = ["-t", "1", "-vn"] if audio else ["-frames:v", "1"]
    r = subprocess.run([str(core.FFMPEG), "-v", "error", "-i", str(p), *args, "-f", "null", "-"],
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def _alpha(p: Path, m: dict) -> bool:
    """Real transparency: an alpha channel that is not fully opaque (a PNG saved as RGBA with no see-through pixel
    is a photo)."""
    if not re.search(r"a|pal8", m.get("pix_fmt", "")) or m.get("pix_fmt") in ("yuv420p", "yuvj420p"):
        return False
    r = subprocess.run([str(core.FFMPEG), "-v", "error", "-i", str(p), "-frames:v", "1", "-vf",
                        "alphaextract,scale=64:64,format=gray", "-f", "rawvideo", "-"], capture_output=True, timeout=60)
    return r.returncode == 0 and bool(r.stdout) and min(r.stdout) < 250


def classify(p: Path, m: dict, name: str = "") -> tuple[str, str]:
    ext = p.suffix.lower()
    if ext in AUDIO_EXT or (m.get("has_audio") and not m.get("has_video")):
        return ("sounds", f"short sound ({m['dur']:.1f} s)") if m["dur"] <= SOUND_MAX else ("music", f"music ({m['dur']:.0f} s)")
    if ext == ".gif":
        return "stickers", "GIF"
    if ext in IMAGE_EXT:
        return ("stickers", "transparent picture") if _alpha(p, m) else ("pictures", "photo")
    if TRANS_WORDS.search(Path(name).stem if name else p.stem):  # the NAME the user gave it (p is the temp file)
        return "transitions", "the name says transition"
    if m["dur"] and m["dur"] <= TRANS_MAX:
        return "transitions", f"short clip ({m['dur']:.1f} s)"
    return "videos", "video clip"


def _fp(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        h.update(f.read(1 << 20))
    return f"{p.stat().st_size}:{h.hexdigest()}"


def _fits(kind: str, p: Path, m: dict) -> str:
    """'' when a file can live in `kind`, else why not."""
    ext = p.suffix.lower()
    if kind in ("music", "sounds"):
        return "" if m.get("has_audio") and (ext in AUDIO_EXT) else "only sound files (MP3, WAV, M4A …) go there"
    if kind == "stickers":
        return "" if ext in STICKER_OK else "stickers must be PNG, WebP or GIF"
    if kind == "pictures":
        return "" if ext in IMAGE_EXT and ext != ".gif" else "only pictures go there"
    return "" if m.get("has_video") and ext not in IMAGE_EXT else "only video clips go there"


def add_file(tmp: Path, name: str, want: str | None = None) -> dict:
    """Store one uploaded file (already saved at tmp). Returns {"ok", "kind", "name", "why"}; never raises."""
    name = core.safe_filename(name or "upload")
    ext = Path(name).suffix.lower()
    try:
        if ext not in ACCEPT:
            return {"ok": False, "name": name, "why": "not a picture, video or sound file" +
                    (" – fonts aren't supported here yet" if ext in (".ttf", ".otf") else "")}
        if tmp.stat().st_size > MAX_BYTES:
            return {"ok": False, "name": name, "why": "bigger than 2 GB"}
        m = _probe(tmp)
        audio_only = ext in AUDIO_EXT
        if not m or not (m.get("has_audio") if audio_only else m.get("has_video")) or not _decodes(tmp, audio_only):
            if ext == ".webp":
                return {"ok": False, "name": name, "why": "an animated WebP can't be used – save it as a GIF (or a still PNG)"}
            return {"ok": False, "name": name, "why": "can't be read (damaged, or not really this kind of file)"}
        kind, why = (want, "you chose it") if want in KINDS else classify(tmp, m, name)
        bad = _fits(kind, tmp, m)
        if bad:
            return {"ok": False, "name": name, "why": f"not a {KINDS[kind][0].lower()} file – {bad}"}
        dest_dir = folder(kind)
        dest_dir.mkdir(parents=True, exist_ok=True)
        fp, size = _fp(tmp), tmp.stat().st_size
        for k2 in KINDS:  # every kind: the same clip dropped again lands in ONE place
            d2 = folder(k2)
            if not d2.is_dir():
                continue
            for q in d2.iterdir():
                if q.is_file() and q.stat().st_size == size and _fp(q) == fp:
                    return {"ok": False, "dup": True, "name": name, "kind": k2,
                            "why": f"already in {KINDS[k2][0]} as “{q.name}”" + ("" if k2 == kind else " – use “move…” there to change its kind")}
        dest = core.unique_path(dest_dir, name)
        shutil.move(str(tmp), dest)
        library._cache["sig"] = None  # the Library panel / commands see it at once
        return {"ok": True, "name": dest.name, "kind": kind, "why": why}
    finally:
        tmp.unlink(missing_ok=True)


def _thumb(kind: str, p: Path) -> str | None:
    """A small poster frame for a video / transition clip (cached in Library/.thumbs)."""
    if kind not in ("videos", "transitions"):
        return None
    tdir = library.LIB / ".thumbs"
    key = hashlib.sha1(f"{p}|{p.stat().st_size}|{p.stat().st_mtime}".encode()).hexdigest()[:16] + ".jpg"
    t = tdir / key
    if not t.is_file():
        tdir.mkdir(parents=True, exist_ok=True)
        subprocess.run([str(core.FFMPEG), "-v", "error", "-y", "-ss", "0.3", "-i", str(p), "-frames:v", "1", "-vf",
                        "scale=240:-2", str(t)], capture_output=True, timeout=60)
        if not t.is_file():
            subprocess.run([str(core.FFMPEG), "-v", "error", "-y", "-i", str(p), "-frames:v", "1", "-vf", "scale=240:-2", str(t)],
                           capture_output=True, timeout=60)
    return "/media/_/library/.thumbs/" + key if t.is_file() else None


_DUR: dict = {}


def listing() -> dict:
    out = {}
    for kind, (label, icon, use) in KINDS.items():
        d = folder(kind)
        items = []
        if d.is_dir():
            for p in sorted(d.iterdir(), key=lambda q: q.name.lower()):
                if not p.is_file() or p.name.startswith(".") or p.suffix.lower() not in ACCEPT or p.name.endswith(".json"):
                    continue
                st = p.stat()
                k = (str(p), st.st_size, st.st_mtime)
                if k not in _DUR:
                    _DUR[k] = 0.0 if p.suffix.lower() in IMAGE_EXT else round((_probe(p) or {}).get("dur", 0.0), 2)
                items.append({"name": p.name, "size": st.st_size, "dur": _DUR[k], "url": url_of(kind, p),
                              "thumb": _thumb(kind, p), "added": st.st_mtime})
        out[kind] = {"label": label, "icon": icon, "use": use, "folder": str(d), "items": items}
    return out


def _path(kind: str, name: str) -> Path:
    if kind not in KINDS:
        raise ValueError("Unknown kind.")
    d = folder(kind)
    p = (d / Path(str(name)).name)
    if not p.is_file() or p.resolve().parent != d.resolve():
        raise ValueError("That file is no longer there – the page will refresh.")
    return p


def used_by(kind: str, p: Path) -> list[str]:
    """Projects whose edit points at this file (stickers / sounds / tracks / music / green-screen background)."""
    ref = p.name if kind == "music" else p.relative_to(library.LIB).as_posix()
    needles = [json.dumps(ref)[1:-1]]
    out = []
    for pj in sorted(core.STORY.glob("*/project.json")):
        try:
            txt = pj.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(nd in txt for nd in needles):
            d = core.read_json(pj, {}) or {}
            out.append(d.get("name") or pj.parent.name)
    return out


def _warn(kind: str, p: Path) -> str:
    u = used_by(kind, p)
    return (f" ⚠ Used in {', '.join(u[:3])}{' …' if len(u) > 3 else ''} – there it's now missing (the studio leaves it out "
            f"and shows a warning; Undo there doesn't bring the file back – move it back here).") if u else ""


def move(kind: str, name: str, to: str) -> str:
    p = _path(kind, name)
    if to not in KINDS or to == kind:
        raise ValueError("Pick another kind.")
    m = _probe(p)
    bad = _fits(to, p, m)
    if bad:
        raise ValueError(f"Can't move it to {KINDS[to][0]}: {bad}.")
    d = folder(to)
    d.mkdir(parents=True, exist_ok=True)
    warn = _warn(kind, p)
    dest = core.unique_path(d, p.name)
    shutil.move(str(p), dest)
    library._cache["sig"] = None
    return f"Moved “{p.name}” to {KINDS[to][0]}." + warn


def remove(kind: str, name: str) -> str:
    p = _path(kind, name)
    warn = _warn(kind, p)
    t = library.LIB / ".trash" / kind
    t.mkdir(parents=True, exist_ok=True)
    shutil.move(str(p), core.unique_path(t, p.name))
    library._cache["sig"] = None
    return f"Deleted “{p.name}” (kept in Library/.trash/{kind} in case you need it back)." + warn


def add_to_project(pid: str, kind: str, name: str) -> str:
    """Copy a picture / video / transition clip into the project's Assets: it then shows in Cover with B-roll and on
    the timeline tracks' file list."""
    if kind not in ("pictures", "videos", "transitions"):
        raise ValueError("Only pictures and videos are added to a project – music, sounds and stickers are already "
                         "usable in every project.")
    p = _path(kind, name)
    d = core.assets_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    for q in d.iterdir():
        if q.is_file() and q.stat().st_size == p.stat().st_size and _fp(q) == _fp(p):
            return f"“{p.name}” is already in this project (as “{q.name}”)."
    dest = core.unique_path(d, p.name)
    shutil.copy2(p, dest)
    os.utime(dest)
    return f"Added “{dest.name}” to this project – pick it in Cover with B-roll or on a timeline track."
