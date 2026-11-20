"""CVE Studio LIBRARY: transitions, effects, stickers and sound effects for any scene.

Lives in <root>/Library (next to Raw Footage; never in git – the whitelist .gitignore keeps it out):

    Library/
      Stickers/Fluent Emoji/<group>/<id>.png   Microsoft Fluent Emoji 3D (MIT) + catalog.json
      Stickers/My stickers/                    drop your own PNG / GIF / WEBP here
      Sounds/SFX Kit/sounds/<category>/*.wav   Wyzowl SFX Kit, 320 sounds (CC0) + sounds.json (sync points, levels)
      Sounds/My sounds/                        drop your own WAV / MP3 / OGG / M4A here
      Previews/                                little preview clips of every transition and effect
      LICENSES.md                              where everything came from and its licence

Transitions (ffmpeg xfade) and effects (ffmpeg filter recipes, below) are built in: nothing to download.

    python library.py setup      download the packs + make previews (safe to run again: skips what's there)
    python library.py previews   (re)make the preview clips only
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import core

LIB = core.ROOT / "Library"
STICKERS = LIB / "Stickers"
SOUNDS = LIB / "Sounds"
PREVIEWS = LIB / "Previews"
FLUENT = STICKERS / "Fluent Emoji"
MY_STICKERS = STICKERS / "My stickers"
SFXKIT = SOUNDS / "SFX Kit"
MY_SOUNDS = SOUNDS / "My sounds"
STICKER_EXT = {".png", ".gif", ".webp"}
SOUND_EXT = {".wav", ".mp3", ".ogg", ".m4a", ".aac", ".flac"}

# ----------------------------------------------------------------- transitions (ffmpeg xfade) INTO a scene
# id -> (name, group, whoosh sound id from the SFX Kit that suits it, or None)
TRANSITIONS = {
    "fade": ("Cross fade", "Soft", None),
    "dissolve": ("Dissolve", "Soft", None),
    "fadeblack": ("Dip to black", "Soft", None),
    "fadewhite": ("Flash (dip to white)", "Soft", "whoosh-fast-soft"),
    "fadegrays": ("Fade through grey", "Soft", None),
    "hblur": ("Blur", "Soft", "whoosh-fast-soft"),
    "slideleft": ("Slide left", "Slide & push", "whoosh-fast-hard"),
    "slideright": ("Slide right", "Slide & push", "whoosh-fast-hard"),
    "slideup": ("Slide up", "Slide & push", "whoosh-fast-hard"),
    "slidedown": ("Slide down", "Slide & push", "whoosh-fast-hard"),
    "smoothleft": ("Smooth left", "Slide & push", "whoosh-fast-soft"),
    "smoothright": ("Smooth right", "Slide & push", "whoosh-fast-soft"),
    "smoothup": ("Smooth up", "Slide & push", "whoosh-fast-soft"),
    "smoothdown": ("Smooth down", "Slide & push", "whoosh-fast-soft"),
    "coverleft": ("Cover left", "Slide & push", "whoosh-fast-hard"),
    "coverright": ("Cover right", "Slide & push", "whoosh-fast-hard"),
    "coverup": ("Cover up", "Slide & push", "whoosh-fast-hard"),
    "coverdown": ("Cover down", "Slide & push", "whoosh-fast-hard"),
    "revealleft": ("Reveal left", "Slide & push", "whoosh-fast-soft"),
    "revealright": ("Reveal right", "Slide & push", "whoosh-fast-soft"),
    "revealup": ("Reveal up", "Slide & push", "whoosh-fast-soft"),
    "revealdown": ("Reveal down", "Slide & push", "whoosh-fast-soft"),
    "wipeleft": ("Wipe left", "Wipe", "whoosh-fast-soft"),
    "wiperight": ("Wipe right", "Wipe", "whoosh-fast-soft"),
    "wipeup": ("Wipe up", "Wipe", "whoosh-fast-soft"),
    "wipedown": ("Wipe down", "Wipe", "whoosh-fast-soft"),
    "wipetl": ("Wipe to top-left", "Wipe", "whoosh-fast-soft"),
    "wipetr": ("Wipe to top-right", "Wipe", "whoosh-fast-soft"),
    "wipebl": ("Wipe to bottom-left", "Wipe", "whoosh-fast-soft"),
    "wipebr": ("Wipe to bottom-right", "Wipe", "whoosh-fast-soft"),
    "diagtl": ("Diagonal top-left", "Wipe", "whoosh-fast-soft"),
    "diagbr": ("Diagonal bottom-right", "Wipe", "whoosh-fast-soft"),
    "hlwind": ("Wind left", "Wipe", "whoosh-fast-soft"),
    "hrwind": ("Wind right", "Wipe", "whoosh-fast-soft"),
    "circleopen": ("Circle open", "Shape", "whoosh-fast-soft"),
    "circleclose": ("Circle close", "Shape", "whoosh-fast-soft"),
    "circlecrop": ("Circle crop", "Shape", "whoosh-fast-soft"),
    "rectcrop": ("Box crop", "Shape", "whoosh-fast-soft"),
    "horzopen": ("Open sideways", "Shape", "whoosh-fast-soft"),
    "vertopen": ("Open up/down", "Shape", "whoosh-fast-soft"),
    "radial": ("Clock wipe", "Shape", "whoosh-fast-soft"),
    "hlslice": ("Slices left", "Shape", "whoosh-fast-hard"),
    "vuslice": ("Slices up", "Shape", "whoosh-fast-hard"),
    "zoomin": ("Zoom in", "Zoom & glitch", "whoosh-fast-hard"),
    "squeezeh": ("Squeeze", "Zoom & glitch", "whoosh-fast-hard"),
    "pixelize": ("Pixelate", "Zoom & glitch", "whoosh-fast-hard"),
    "distance": ("Distance", "Zoom & glitch", "whoosh-fast-soft"),
}
DEFAULT_TRANS_DUR = 0.4


# ----------------------------------------------------------------- effects: ffmpeg filter recipes on a scene's picture
# id -> (name, group, what it does). Filters are applied to the composed frame BEFORE captions/text/stickers, so the
# words stay steady and readable whatever the effect does.
EFFECTS = {
    "flash": ("Flash in", "Moments", "A white flash as the scene starts – a hard 'beat'."),
    "fadein": ("Fade in from black", "Moments", "The scene fades up from black (good for the first scene)."),
    "fadeout": ("Fade out to black", "Moments", "The scene fades to black at its end (good for the last scene)."),
    "punchin": ("Punch-in zoom", "Camera", "A quick zoom-in over the first 0.3 s, then it holds – emphasis."),
    "slowzoom": ("Slow zoom (Ken Burns)", "Camera", "A slow push-in over the whole scene – keeps a still shot alive."),
    "zoomout": ("Slow zoom out", "Camera", "Starts close and slowly pulls back."),
    "shake": ("Camera shake", "Camera", "A short, punchy shake as the scene starts – impact / shock."),
    "glitch": ("Glitch", "Stylise", "Short colour-split + noise bursts – 'breaking news' / tech feel."),
    "rgbsplit": ("RGB split", "Stylise", "Colour channels slightly apart for the whole scene."),
    "invertflash": ("Invert flash", "Stylise", "The picture flips to negative for a split second at the start."),
    "bw": ("Black & white", "Colour", "No colour – serious, past, 'what went wrong'."),
    "vintage": ("Vintage film", "Colour", "Faded film colours, grain and a dark edge."),
    "punchy": ("Punchy colour", "Colour", "More contrast and saturation – bright, social look."),
    "cinematic": ("Cinematic", "Colour", "Teal shadows, warm highlights, a touch more contrast."),
    "vignette": ("Vignette", "Colour", "Darker edges pull the eye to the centre."),
    "warm": ("Warm", "Colour", "Warmer, golden tones."),
    "cool": ("Cool", "Colour", "Colder, blue tones – calm / analytical."),
}


def effect_filters(eid: str, w: int, h: int, fps: int, dur: float) -> list[str]:
    """ffmpeg filters for one effect on a w x h picture of `dur` seconds (t = seconds into the scene)."""
    d = max(0.1, float(dur))
    zp = f":d=1:s={w}x{h}:fps={fps}"
    return {
        "flash": ["fade=t=in:st=0:d=0.25:color=white"],
        "fadein": [f"fade=t=in:st=0:d={min(0.5, d / 3):.2f}"],
        "fadeout": [f"fade=t=out:st={max(0.0, d - min(0.5, d / 3)):.3f}:d={min(0.5, d / 3):.2f}"],
        # zoompan works per frame at a fixed output size; upscale first so the integer crop doesn't jitter
        "punchin": [f"scale={w * 2}:{h * 2}", f"zoompan=z='1+0.15*min(1,in/{max(1, round(0.3 * fps))})'"
                    f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'{zp}"],
        "slowzoom": [f"scale={w * 2}:{h * 2}", f"zoompan=z='1+0.10*in/{max(1, round(d * fps))}'"
                     f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'{zp}"],
        "zoomout": [f"scale={w * 2}:{h * 2}", f"zoompan=z='1.12-0.12*in/{max(1, round(d * fps))}'"
                    f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'{zp}"],
        "shake": [f"crop=w=iw-{round(w * 0.05) // 2 * 2}:h=ih-{round(h * 0.05) // 2 * 2}"
                  f":x='(iw-ow)/2+(iw-ow)/2*sin(t*47)*max(0,1-t/0.6)':y='(ih-oh)/2+(ih-oh)/2*cos(t*39)*max(0,1-t/0.6)'",
                  f"scale={w}:{h}", "setsar=1"],
        "glitch": ["rgbashift=rh=-14:bh=14:rv=4:enable='lt(mod(t,1.1),0.12)'",
                   "noise=alls=40:allf=t:enable='lt(mod(t,1.1),0.12)'"],
        "rgbsplit": ["rgbashift=rh=-6:bh=6"],
        "invertflash": ["negate=enable='lt(t,0.1)'"],
        "bw": ["hue=s=0"],
        "vintage": ["curves=preset=vintage", "noise=alls=8:allf=t", "vignette=PI/4.5"],
        "punchy": ["eq=contrast=1.12:saturation=1.35"],
        "cinematic": ["colorbalance=rs=-0.06:bs=0.08:rh=0.06:bh=-0.06", "eq=contrast=1.06:saturation=1.05"],
        "vignette": ["vignette=PI/4"],
        "warm": ["colorbalance=rm=0.06:bm=-0.06:rh=0.04:bh=-0.04"],
        "cool": ["colorbalance=rm=-0.04:bm=0.07:rh=-0.03:bh=0.05"],
    }.get(eid, [])


# ----------------------------------------------------------------- stickers: placement + animation
STICKER_POS = {  # centre of the sticker as a fraction of the frame
    "top-left": (0.18, 0.14), "top": (0.5, 0.14), "top-right": (0.82, 0.14),
    "left": (0.18, 0.45), "center": (0.5, 0.45), "right": (0.82, 0.45),
    "bottom-left": (0.18, 0.66), "bottom": (0.5, 0.66), "bottom-right": (0.82, 0.66),
}
STICKER_ANIM = ("pop", "bounce", "still")

# finance reels: the stickers most worth having one click away (Fluent ids)
FINANCE_PICKS = [
    "chart_increasing", "chart_decreasing", "bar_chart", "money_bag", "money_with_wings", "dollar_banknote",
    "coin", "bank", "gem_stone", "rocket", "fire", "chart_increasing_with_yen", "hundred_points", "check_mark_button",
    "cross_mark", "warning", "red_exclamation_mark", "red_question_mark", "light_bulb", "thinking_face",
    "exploding_head", "face_screaming_in_fear", "smiling_face_with_sunglasses", "money_mouth_face", "partying_face",
    "party_popper", "trophy", "bullseye", "eyes", "brain", "megaphone", "bell", "alarm_clock", "hourglass_done",
    "pushpin", "backhand_index_pointing_down", "backhand_index_pointing_right", "thumbs_up", "thumbs_down",
    "clapping_hands", "flexed_biceps", "handshake", "loudspeaker", "high_voltage", "sparkles", "glowing_star",
    "crown", "stop_sign", "no_entry", "up_arrow", "down_arrow", "right_arrow", "chart_increasing", "calendar",
    "magnifying_glass_tilted_left", "locked", "key", "house", "briefcase", "credit_card", "receipt", "balance_scale",
]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or "item"


# ----------------------------------------------------------------- catalog (cached on folder changes)
_cache: dict = {"sig": None, "cat": None}
_lock = threading.Lock()


def _sig() -> tuple:
    out = []
    for p in (FLUENT / "catalog.json", SFXKIT / "sounds.json", MY_STICKERS, MY_SOUNDS, PREVIEWS):
        try:
            out.append((str(p), p.stat().st_mtime))
        except OSError:
            out.append((str(p), None))
    return tuple(out)


def _rel(p: Path) -> str:
    return p.relative_to(LIB).as_posix()


def catalog() -> dict:
    """Everything in the library, for the page and for commands. Re-read only when a folder/catalog changed."""
    with _lock:
        sig = _sig()
        if _cache["sig"] == sig and _cache["cat"] is not None:
            return _cache["cat"]
        prev = {p.stem for p in PREVIEWS.glob("*.mp4")} if PREVIEWS.is_dir() else set()
        cat = {
            "folder": str(LIB),
            "transitions": [{"id": k, "name": n, "group": g, "sound": snd,
                             "preview": f"Previews/transition_{k}.mp4" if f"transition_{k}" in prev else None}
                            for k, (n, g, snd) in TRANSITIONS.items()],
            "effects": [{"id": k, "name": n, "group": g, "desc": ds,
                         "preview": f"Previews/effect_{k}.mp4" if f"effect_{k}" in prev else None}
                        for k, (n, g, ds) in EFFECTS.items()],
            "stickers": [], "sounds": [], "sound_categories": [],
        }
        fl = core.read_json(FLUENT / "catalog.json", []) or []
        have = set()
        for it in fl:
            if (LIB / it["file"]).is_file():
                cat["stickers"].append(dict(it, pack="Fluent Emoji"))
                have.add(it["id"])
        cat["finance_picks"] = [x for x in dict.fromkeys(FINANCE_PICKS) if x in have]
        if MY_STICKERS.is_dir():
            for p in sorted(MY_STICKERS.iterdir()):
                if p.is_file() and p.suffix.lower() in STICKER_EXT:
                    if p.suffix.lower() == ".webp" and not media_ok(p):
                        continue  # animated WEBP: ffmpeg can't decode it – leave it out instead of failing renders
                    cat["stickers"].append({"id": "my_" + slug(p.stem), "name": p.stem, "group": "My stickers",
                                            "keywords": [], "file": _rel(p), "pack": "My stickers",
                                            "animated": p.suffix.lower() == ".gif"})
        kit = core.read_json(SFXKIT / "sounds.json", {}) or {}
        for s in kit.get("sounds", []):
            f = SFXKIT / s["file"]
            if f.is_file():
                cat["sounds"].append({
                    "id": s["id"], "name": s.get("name", s["id"]), "category": s.get("category", "other"),
                    "file": _rel(f), "duration": s.get("duration"), "sync": (s.get("sync") or {}).get("at", 0.0),
                    "desc": s.get("description", ""), "use_for": s.get("use_for", [])[:4], "tags": s.get("tags", []),
                    "gain_db": float(s.get("under_voiceover_db", 0) or 0), "pack": "SFX Kit"})
        if MY_SOUNDS.is_dir():
            for p in sorted(MY_SOUNDS.iterdir()):
                if p.is_file() and p.suffix.lower() in SOUND_EXT:
                    cat["sounds"].append({"id": "my_" + slug(p.stem), "name": p.stem, "category": "my sounds",
                                          "file": _rel(p), "duration": None, "sync": 0.0, "desc": "Your own sound.",
                                          "use_for": [], "tags": [], "gain_db": 0.0, "pack": "My sounds"})
        cat["sound_categories"] = sorted({s["category"] for s in cat["sounds"]})
        cat["sticker_groups"] = list(dict.fromkeys(s["group"] for s in cat["stickers"]))
        cat["ready"] = bool(cat["stickers"] or cat["sounds"])
        _cache.update(sig=sig, cat=cat)
        return cat


def find(kind: str, key: str, exact: bool = False) -> dict | None:
    """A sticker / sound by id, file name or name; exact=False also allows a fragment ("fire", "cash")."""
    items = catalog()["stickers" if kind == "sticker" else "sounds"]
    k = str(key).strip().lower()
    ks = slug(k)
    if not ks or ks.isdigit():
        return None  # "sticker 1" means sticker number 1, never the "1" keycap emoji
    for it in items:
        if it["id"] == ks or it["id"] == k or slug(it["name"]) == ks or Path(it["file"]).stem.lower() == k:
            return it
    if exact or len(ks) < 3:
        return None
    hits = [it for it in items if ks in slug(it["id"]) or ks in slug(it["name"])]
    if len(hits) == 1:
        return hits[0]
    if hits:  # several: prefer an exact word match, else the shortest name ("fire" -> fire, not firecracker)
        return sorted(hits, key=lambda it: (ks not in it["id"].split("_"), len(it["id"])))[0]
    return None


_OK: dict = {}


def media_ok(p: Path) -> bool:
    """Can ffmpeg really decode this sticker / sound? (an animated WEBP, a renamed file or a broken download can't).
    Cached per file version; a bad file is skipped instead of failing the whole render."""
    try:
        st = p.stat()
    except OSError:
        return False
    key = (str(p), st.st_size, st.st_mtime)
    if key not in _OK:
        r = subprocess.run([core.FFMPEG, "-v", "error", "-i", str(p), "-frames:v", "1", "-t", "0.2", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=30)
        _OK[key] = r.returncode == 0 and not r.stderr.strip()
    return _OK[key]


def path_of(rel: str | None) -> Path | None:
    """A library file (path relative to Library/), only if it really is inside the library and exists."""
    if not rel or not isinstance(rel, str):
        return None
    p = (LIB / rel).resolve()
    try:
        if not p.is_relative_to(LIB.resolve()) or not p.is_file():
            return None
    except OSError:
        return None
    return p


# ----------------------------------------------------------------- setup: download + previews
UA = {"User-Agent": "cve-studio-library-setup"}


def _get(url: str, dest: Path, tries: int = 3) -> bool:
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r, open(dest.with_suffix(dest.suffix + ".part"), "wb") as f:
                shutil.copyfileobj(r, f)
            os.replace(dest.with_suffix(dest.suffix + ".part"), dest)
            return True
        except OSError:
            continue
    dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
    return False


def fetch_sfx_kit(log=print) -> int:
    if (SFXKIT / "sounds.json").is_file() and len(list((SFXKIT / "sounds").rglob("*.wav"))) >= 300:
        log("SFX Kit: already there")
        return 0
    SFXKIT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        z = Path(td) / "sfx-kit.zip"
        log("SFX Kit (Wyzowl, CC0): downloading ~55 MB …")
        if not _get("https://codeload.github.com/wyzowl/sfx-kit/zip/refs/heads/main", z):
            log("SFX Kit: download failed – check the internet and run setup again")
            return 1
        with zipfile.ZipFile(z) as zf:
            for m in zf.infolist():
                parts = m.filename.split("/", 1)
                if len(parts) < 2 or not parts[1] or m.is_dir():
                    continue
                rel = parts[1]
                if not (rel.startswith("sounds/") or rel in ("sounds.json", "LICENSE", "GUIDE.md", "README.md")):
                    continue
                out = (SFXKIT / rel).resolve()
                if not out.is_relative_to(SFXKIT.resolve()):  # zip-slip guard
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(m) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    log(f"SFX Kit: {len(list((SFXKIT / 'sounds').rglob('*.wav')))} sounds")
    return 0


def fetch_fluent(log=print) -> int:
    FLUENT.mkdir(parents=True, exist_ok=True)
    log("Fluent Emoji 3D (Microsoft, MIT): reading the list …")
    tree_f = FLUENT / ".tree.json"
    if not tree_f.is_file() and not _get("https://api.github.com/repos/microsoft/fluentui-emoji/git/trees/main?recursive=1", tree_f):
        log("Fluent Emoji: could not read the list – check the internet and run setup again")
        return 1
    tree = [t["path"] for t in (core.read_json(tree_f, {}) or {}).get("tree", [])]
    base = "https://raw.githubusercontent.com/microsoft/fluentui-emoji/main/"
    pngs: dict[str, str] = {}
    for p in tree:  # one 3D picture per emoji: the plain one, or the "Default" skin tone
        m = re.match(r"assets/([^/]+)/(?:Default/)?3D/[^/]+_3d(?:_default)?\.png$", p)
        if m and (m.group(1) not in pngs or "/Default/" not in p):
            pngs[m.group(1)] = p
    meta_dir = FLUENT / ".meta"
    meta_dir.mkdir(exist_ok=True)

    def one(folder: str):
        mf = meta_dir / (slug(folder) + ".json")
        if not mf.is_file():
            _get(base + urllib.request.quote(f"assets/{folder}/metadata.json"), mf)
        meta = core.read_json(mf, {}) or {}
        group = re.sub(r"[^\w &-]+", "", meta.get("group") or "Other").strip() or "Other"
        eid = slug(meta.get("cldr") or folder)
        out = FLUENT / group / f"{eid}.png"
        if not out.is_file():
            out.parent.mkdir(parents=True, exist_ok=True)
            if not _get(base + urllib.request.quote(pngs[folder]), out):
                return None
        return {"id": eid, "name": (meta.get("cldr") or folder).capitalize(), "group": group,
                "keywords": meta.get("keywords", [])[:12], "glyph": meta.get("glyph", ""), "file": _rel(out)}

    items, done = [], 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        for it in ex.map(one, sorted(pngs)):
            done += 1
            if it:
                items.append(it)
            if done % 200 == 0:
                log(f"Fluent Emoji: {done}/{len(pngs)}")
    items.sort(key=lambda x: (x["group"], x["name"]))
    core._atomic_write(FLUENT / "catalog.json", items)
    log(f"Fluent Emoji: {len(items)} stickers")
    return 0 if items else 1


LICENSES = """# CVE Studio Library – where everything comes from

| Part | Source | Licence |
|---|---|---|
| Stickers/Fluent Emoji | Microsoft Fluent Emoji 3D – https://github.com/microsoft/fluentui-emoji | MIT (c) Microsoft Corporation |
| Sounds/SFX Kit | SFX Kit by Wyzowl – https://github.com/wyzowl/sfx-kit | CC0 1.0 (public domain, no credit needed) |
| Transitions | ffmpeg `xfade` filter, built in | part of ffmpeg |
| Effects | ffmpeg filter recipes in the studio code | – |
| Previews/ | made by `library.py previews` from plain colour cards | – |
| Stickers/My stickers, Sounds/My sounds | YOUR files – only add what you have the right to use | yours |

Not bundled (great, but their licences forbid redistribution or automated download – download by hand and drop the files
in My stickers / My sounds if you like them): Pixabay, Mixkit, Freesound (CC-BY files need credit), Kenney audio packs
(CC0 – fine to drop in too; their site only offers them through a browser download).

MIT licence text for Fluent Emoji: Stickers/Fluent Emoji/LICENSE. CC0 text for the SFX Kit: Sounds/SFX Kit/LICENSE.
"""


def make_previews(log=print, only_missing: bool = True) -> int:
    """1.6 s 360x640 clips: card A -> card B for each transition; a textured card for each effect."""
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    font = Path(__file__).parent / "fonts" / "Poppins-Bold.ttf"
    if not font.is_file():
        cands = sorted((Path(__file__).parent / "fonts").glob("*.ttf"))
        font = cands[0] if cands else None
    fnt = f":fontfile='{font.as_posix().replace(':', chr(92) + ':')}'" if font else ""
    W, H, FPS = 360, 640, 30

    def card(col, letter):
        return (f"color=c={col}:s={W}x{H}:r={FPS}:d=1.6,format=yuv420p,"
                f"drawgrid=w=60:h=60:t=1:c=white@0.12,"
                f"drawtext=text='{letter}'{fnt}:fontsize=220:fontcolor=white:x=(w-tw)/2:y=(h-th)/2")
    n = 0
    for k in TRANSITIONS:
        out = PREVIEWS / f"transition_{k}.mp4"
        if only_missing and out.is_file():
            continue
        g = f"{card('0x1E3A5F', 'A')}[a];{card('0xF97316', 'B')}[b];[a][b]xfade=transition={k}:duration=0.6:offset=0.5,format=yuv420p[v]"
        r = subprocess.run([core.FFMPEG, "-v", "error", "-y", "-filter_complex", g, "-map", "[v]", "-t", "1.6",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-movflags", "+faststart", str(out)],
                           capture_output=True, text=True)
        n += r.returncode == 0
        if r.returncode:
            log(f"preview {k}: {r.stderr.strip()[-200:]}")
    for k in EFFECTS:
        out = PREVIEWS / f"effect_{k}.mp4"
        if only_missing and out.is_file():
            continue
        base = (f"color=c=0x2B6CB0:s={W}x{H}:r={FPS}:d=1.6,format=yuv420p,drawgrid=w=40:h=40:t=2:c=white@0.25,"
                f"drawbox=x=60:y=200:w=240:h=240:color=0xF97316:t=fill,"
                f"drawtext=text='FX'{fnt}:fontsize=120:fontcolor=white:x=(w-tw)/2:y=(h-th)/2")
        g = f"{base},{','.join(effect_filters(k, W, H, FPS, 1.6))},format=yuv420p[v]"
        r = subprocess.run([core.FFMPEG, "-v", "error", "-y", "-filter_complex", g, "-map", "[v]", "-t", "1.6",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-movflags", "+faststart", str(out)],
                           capture_output=True, text=True)
        n += r.returncode == 0
        if r.returncode:
            log(f"preview {k}: {r.stderr.strip()[-200:]}")
    log(f"Previews: {n} made")
    return 0


def setup(log=print) -> int:
    LIB.mkdir(exist_ok=True)
    MY_STICKERS.mkdir(parents=True, exist_ok=True)
    MY_SOUNDS.mkdir(parents=True, exist_ok=True)
    (LIB / "LICENSES.md").write_text(LICENSES, encoding="utf-8")
    bad = fetch_sfx_kit(log)
    bad += fetch_fluent(log)
    lic = FLUENT / "LICENSE"
    if not lic.is_file():
        _get("https://raw.githubusercontent.com/microsoft/fluentui-emoji/main/LICENSE", lic)
    make_previews(log)
    _cache["sig"] = None
    log("Library ready." if not bad else "Library partly ready – run setup again to finish.")
    return bad


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "setup"
    if cmd == "previews":
        sys.exit(make_previews(only_missing=False))
    sys.exit(setup())
