"""🖼 PICTURES & CLIPS FOR THE STORY: drop the pictures, GIFs and short clips that go with what you say – Powell at the
podium and a market chart for "the Fed rate", gold bars, a fire transition between your reasons, a sparkle overlay, a
transparent GIF – click ▶ Place, and the studio puts each one where it belongs, timed to your words.

Every file gets a ROLE (detected, you can change it per file):
  cutaway     covers the screen for a moment while your voice plays (photos, stock clips, a GIF meme) – full screen,
              top half or corner box (the Show setting); a picture not the frame's shape is shown WHOLE over a blur
  transition  a short effect clip (fire, light leak, swipe, glitch…) played over the CUT to your next point – at
              each "pehla / doosra / teesra … reason" moment (else at scene changes), with its own whoosh sound;
              "transition once" = just the first such moment
  overlay     an effect over you (sparkles, smoke, fire burst) at the word it matches – you stay visible
  sticker     a transparent picture / GIF popping up in the corner at its word
How a clip is blended (detected from its pixels): a transparent GIF / WebM / PNG keeps its transparency; a clip on a
BLACK background (most fire / light-leak packs) is screen-blended – the black disappears, the flames glow; a GREEN
screen clip is keyed; anything else is a normal picture. GIFs play animated and loop.

How a cutaway / overlay / sticker finds its scene (offline, instant):
  1. your pin: a note "scene 3" (or dragging the block on the timeline – that becomes a pin) always wins
  2. its NAME + your NOTE vs the words you say in each scene (Hindi via Hinglish, your typed caption script if you
     pasted one): the same word ("powell" / "adani") is strongest, then the same TOPIC (finance topic list below:
     "powell.jpg" belongs where you say "Fed" / "byaj"), weighted so "market" alone is weak
  3. no clue (IMG_2034.jpg): story ORDER – it follows the one before it (a chart right after Powell goes straight
     after Powell when there is room, else the next scene)
Then 🤖 Claude (auto-reply on) LOOKS at every picture and re-pins the ones names couldn't place. Your pins stay.

Timing: a beat before the matched word, else spread over the scene; never two cutaways at once; the first second of
the video stays on your face (the hook); at most ~70 % of a scene is covered. Transitions are centred on the moment.

Model: data["pictures"] = {"items": [entry], "style": auto|full|top|pip, "ran", "report": [lines]}
    entry = {"file", "note", "added", "media": {kind still|anim|video, dur, has_audio, blend, keyc, w, h},
             "as": role or None (= detected), "sound": bool | None, "pin": {"scene", "at", "dur", "by"} | None,
             "where"/"fit" (own override), "off", "skip": [moment keys], "placed": [{item, anchor, off, dur, where,
             fit, key}], "scene_no", "why"}
The blocks live on ONE Video track "Pictures & clips" (track["pictures"] = True; a Video track so a transition's
whoosh plays – cutaways are silent). Re-running rebuilds only its own blocks: a block you dragged / trimmed becomes
your pin (a transition block you moved is left to you), a block you deleted stays out.
"""
from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path

import core
import tracks

STYLES = ("auto", "full", "top", "pip")
ROLES = ("cutaway", "pip", "overlay", "sticker", "transition", "transition_once")
ROLE_NAMES = {"cutaway": "Cutaway – full screen", "pip": "Corner box – you stay visible",
              "overlay": "Overlay – over you, see-through", "sticker": "Sticker – small corner pop",
              "transition": "Transition – every new point", "transition_once": "Transition – once"}
NEEDS_EYES = ("cutaway", "pip", "overlay", "sticker")  # placed on the words a picture SHOWS (Claude looks)
BLENDS = ("normal", "alpha", "screen", "key", "soft")  # soft = an ordinary picture laid over you at half strength
MAX_POOL = 40
STILL_DUR, CLIP_MAX, MIN_SHOW = 2.5, 4.0, 1.2
TRANS_MAX, OVER_MAX, STICKER_DUR = 3.0, 5.0, 2.2
COVER = 0.7  # at most this share of a scene under cutaways
MAX_MOMENTS = 6
FX_RE = re.compile(r"transition|swipe|wipe|whoosh|swoosh|swish|light[ _-]?leak|glitch|flash|zoom[ _-]?blur|shutter|"
                   r"film[ _-]?burn|(?<![a-z])burn|fire|flame|smoke|spark|particle|confetti|explo|blast|flare|lightning|(?<![a-z])ink(?![a-z])|"
                   r"shatter|(?<![a-z])dust|overlay|(?<![a-z])v?fx(?![a-z])|effect|bokeh|(?<![a-z])lens|magic|(?<![a-z])glow|energy|thunder", re.I)
GENERIC = {"img", "image", "images", "photo", "photos", "pic", "pics", "picture", "screenshot", "screen", "shot", "wa",
           "whatsapp", "dsc", "dcim", "pxl", "final", "copy", "new", "edit", "edited", "untitled", "download", "file",
           "jpeg", "jpg", "png", "webp", "gif", "bmp", "mp4", "mov", "webm", "the", "and", "for", "with", "of", "in",
           "on", "a", "an", "to", "hd", "4k", "pexels", "unsplash", "pixabay", "freepik", "shutterstock", "getty",
           "stock photo", "scene", "broll", "b", "roll", "clip", "video", "vid", "version", "v1", "v2", "large", "small",
           "thumb", "transition", "overlay", "effect", "fx", "vfx", "green", "black", "background", "bg", "alpha"}
# topic, weight (how telling a shared topic is), stems: <= 3 letters match exactly, longer ones as word starts
TOPICS = [
    ("fed", 3, ["fed", "powell", "federal", "fomc", "jerome", "usfed", "yellen", "wall street", "america", "usa"]),
    ("rbi", 3, ["rbi", "repo", "reserve bank", "governor", "malhotra", "shaktikanta"]),
    ("rates", 1, ["rate", "interest", "byaj", "byaaj", "cut", "hike", "yield", "bond", "emi"]),
    ("gold", 2, ["gold", "sona", "sone", "bullion", "jewel", "golden", "mcx"]),
    ("silver", 3, ["silver", "chandi", "chaandi"]),
    ("dollar", 2, ["dollar", "dolar", "dalar", "usd", "dxy", "currency", "rupee", "rupaya", "rupye", "inr", "forex"]),
    ("war", 3, ["war", "yudh", "yuddh", "jung", "israel", "iran", "russia", "ukraine", "gaza", "missile", "attack",
                "geopolit", "tension", "conflict", "army", "military", "bomb"]),
    ("inflation", 3, ["inflation", "mehngai", "mehangai", "mahangai", "mahngai", "cpi", "price rise"]),
    ("china", 3, ["china", "chinese", "pboc", "beijing"]),
    ("centralbank", 2, ["central bank", "centralbank", "reserves", "kharid", "buying", "purchase"]),
    ("crude", 3, ["crude", "oil", "tel", "opec", "brent", "petrol"]),
    ("etf", 3, ["etf", "mutual fund", "mutualfund", "sip", "sgb", "sovereign"]),
    ("trump", 3, ["trump", "tariff", "biden", "election", "white house"]),
    ("tax", 3, ["tax", "gst", "budget", "nirmala", "sitharaman"]),
    ("crash", 2, ["crash", "girawat", "fall", "bear", "loss", "nuksan", "nuksaan", "red", "smoke", "shatter", "crack"]),
    ("rally", 2, ["rally", "record", "bull", "jump", "tezi", "uchhal", "all time high", "ath", "boom", "rocket", "fire",
                  "flame", "aag", "blast"]),
    ("market", 1, ["market", "nifty", "sensex", "stock", "share", "chart", "graph", "index", "candle", "trend",
                   "bazaar", "bazar", "trading"]),
    ("money", 1, ["paisa", "paise", "money", "cash", "crore", "karod", "lakh", "profit", "return", "invest", "wealth",
                  "coin", "sparkle", "spark"]),
]
WEAK_STEMS = [s for _, wt, stems in TOPICS if wt == 1 for s in stems if " " not in s]
SCENE_RE = re.compile(r"\bscene\s*#?\s*(\d{1,3})\b", re.I)
AT_RE = re.compile(r"\bat\s+(\d+(?:\.\d+)?)\s*s?\b", re.I)
# "pehla / doosra / teesra … reason": a new point starts -> a transition moment
ORDINALS = {"pehla", "pahla", "pehli", "doosra", "dusra", "doosri", "dusri", "teesra", "tisra", "teesri", "tisri",
            "chautha", "chauthi", "chotha", "paanchva", "panchva", "paanchwa", "chhatha", "first", "second", "third",
            "fourth", "fifth", "sixth", "1st", "2nd", "3rd", "4th", "5th"}
LIST_NOUNS = ("reason", "point", "tip", "kaaran", "karan", "wajah", "step", "tarika", "tareeka", "baat", "bada", "badi",
              "big", "mistake", "galti", "rule", "thing", "factor", "cheez", "sawal", "number", "important", "main")


# ------------------------------------------------------------------ pool
def pool(data: dict) -> dict:
    p = data.get("pictures")
    if not isinstance(p, dict):
        p = data["pictures"] = {"items": [], "style": "auto"}
    p.setdefault("items", [])
    if p.get("style") not in STYLES:
        p["style"] = "auto"
    return p


def entry(data: dict, file: str) -> dict | None:
    return next((e for e in pool(data)["items"] if e.get("file") == file), None)


def _natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _records(e: dict) -> list[dict]:
    pl = e.get("placed")
    if isinstance(pl, dict):  # before clips: one block per picture
        return [pl]
    return [x for x in (pl or []) if isinstance(x, dict)]


# ------------------------------------------------------------------ what a file IS (looked at once, on add)
def _sample(p: Path, t: float, anim: bool) -> bytes:
    args = [str(core.FFMPEG), "-v", "error"] + (["-ss", f"{t:.2f}"] if t > 0 and not anim else []) + ["-i", str(p)]
    if anim and t > 0:
        args += ["-vf", f"trim=start={t:.2f},setpts=PTS-STARTPTS,scale=32:32,format=rgba"]
    else:
        args += ["-vf", "scale=32:32,format=rgba"]
    r = subprocess.run(args + ["-frames:v", "1", "-f", "rawvideo", "-"], capture_output=True, timeout=60)
    return r.stdout if r.returncode == 0 and len(r.stdout) >= 32 * 32 * 4 else b""


def _shown_size(p: Path) -> tuple[int, int]:
    """The size as it will be SHOWN: a phone photo / video stores 4000x3000 + "turn it 90°" (EXIF / rotation
    metadata); ffmpeg turns it when decoding, ffprobe reports it unturned – so measure a decoded frame."""
    r = subprocess.run([str(core.FFMPEG), "-v", "info", "-i", str(p), "-frames:v", "1", "-vf", "showinfo", "-f", "null",
                        "-"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    m_ = re.search(r"\bs:(\d+)x(\d+)", r.stderr or "")
    return (int(m_.group(1)), int(m_.group(2))) if m_ else (0, 0)


def _loudness(p: Path) -> float | None:
    """Mean loudness (dB) of a clip's sound – so a whoosh pack mastered at full blast doesn't drown your voice."""
    r = subprocess.run([str(core.FFMPEG), "-v", "info", "-i", str(p), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    m_ = re.search(r"mean_volume:\s*(-?[\d.]+)", r.stderr or "")
    return float(m_.group(1)) if m_ else None


SFX_TARGET_DB = -24.0  # a transition's whoosh sits clearly UNDER a voice (speech is about -16..-20 dB mean)


def inspect(path: Path) -> dict:
    """{kind still|anim|video, dur, has_audio, w, h (as shown), blend normal|alpha|screen|key, keyc, snd_vol}:
    a few 32x32 frames tell whether it is see-through, on black, or on a green screen. Slow-ish (several ffmpeg
    runs): call it OUTSIDE the project lock."""
    import media as media_lib
    try:
        m = media_lib._probe(path)
    except Exception:  # noqa: BLE001
        m = {}
    ext = path.suffix.lower()
    dur = float(m.get("dur") or 0)
    if ext in core.IMAGE_EXT:
        kind = "anim" if ext == ".gif" and (dur > 0.15 or int(m.get("frames") or 0) > 1) else "still"
    else:
        kind = "video"
    sw, sh = _shown_size(path)
    out = {"kind": kind, "dur": round(dur if kind != "still" else 0.0, 3), "has_audio": bool(m.get("has_audio")),
           "w": sw or m.get("w") or 0, "h": sh or m.get("h") or 0, "blend": "normal"}
    if out["has_audio"]:
        mv = _loudness(path)
        if mv is not None and mv > -80:
            out["snd_db"] = round(mv, 1)
            out["snd_vol"] = round(min(1.5, max(0.05, 10 ** ((SFX_TARGET_DB - mv) / 20))), 3)
    # first, middle, last frame: a fire / light-leak transition fills the screen at its peak but starts and ends dark
    times = [0.0] if kind == "still" else [min(0.1, dur * 0.05), dur * 0.5, max(0.0, dur - 0.12)]
    px = [b for b in (_sample(path, t, kind == "anim") for t in times) if b]
    if not px:
        return out
    fx = bool(FX_RE.search(path.stem))
    n = 32
    ring = [(x, y) for y in range(n) for x in range(n) if x in (0, 1, n - 2, n - 1) or y in (0, 1, n - 2, n - 1)]
    see_through = sum(1 for b in px for i in range(3, len(b), 4) if b[i] < 250) / (len(px) * n * n)
    if see_through >= 0.03:
        out["blend"] = "alpha"
        return out
    edge = [(b[(y * n + x) * 4], b[(y * n + x) * 4 + 1], b[(y * n + x) * 4 + 2]) for b in px for x, y in ring]
    green = [c for c in edge if c[1] > 110 and c[1] > c[0] * 1.5 and c[1] > c[2] * 1.5]
    blue = [c for c in edge if c[2] > 110 and c[2] > c[0] * 1.5 and c[2] > c[1] * 1.3]
    for keyed in (green, blue) if kind != "still" else ():  # a green-edged PHOTO / chart is not a green screen
        if len(keyed) >= 0.6 * len(edge):
            r_, g_, b_ = (round(sum(c[i] for c in keyed) / len(keyed)) for i in range(3))
            out["blend"], out["keyc"] = "key", f"{r_:02X}{g_:02X}{b_:02X}"
            return out
    def dark_sides(b: bytes) -> bool:  # all four edges mostly black in this frame
        return all(sum(1 for x, y in ring if cond(x, y) and max(b[(y * n + x) * 4:(y * n + x) * 4 + 3]) < 40)
                   >= 0.7 * sum(1 for x, y in ring if cond(x, y))
                   for cond in (lambda x, y: x < 2, lambda x, y: x >= n - 2, lambda x, y: y < 2, lambda x, y: y >= n - 2))
    dk = [dark_sides(b) for b in px]
    bright = any(max(b[i:i + 3]) > 120 for b in px for i in range(0, len(b), 4))
    # on black: dark at BOTH ends (or all along), or dark somewhere and the name says it is an effect. A stock clip
    # that only fades in from black stays a normal picture.
    if kind != "still" and bright and ((dk[0] and dk[-1]) or (fx and any(dk))):
        out["blend"] = "screen"
    return out


def media_of(pid: str, e: dict) -> dict:
    m = e.get("media")
    if not isinstance(m, dict) or "blend" not in m:
        p = core.assets_dir(pid) / e["file"]
        m = inspect(p) if p.is_file() else {"kind": "still" if core.is_still(e["file"]) else "video", "dur": 0,
                                            "blend": "normal"}
        e["media"] = m
    return m


def auto_role(name: str, m: dict) -> tuple[str, str]:
    """(role, why) from the file's name + what it is."""
    fx = bool(FX_RE.search(Path(name).stem))
    kind, blend, dur = m.get("kind"), m.get("blend"), float(m.get("dur") or 0)
    moving = kind in ("anim", "video")
    if blend == "alpha" and kind in ("still", "anim") and not fx:
        return "sticker", "transparent – pops up in the corner"
    if moving and (fx or blend in ("screen", "key", "alpha")):
        why = ("the name says effect" if fx else {"screen": "on a black background", "key": "on a green screen",
                                                   "alpha": "see-through"}.get(blend, ""))
        if kind == "anim" or dur <= TRANS_MAX:
            return "transition", f"short effect clip ({why}) – plays over each new point"
        return "overlay", f"effect clip ({why}) – plays over you"
    if blend == "alpha":
        return "sticker", "transparent – pops up in the corner"
    return "cutaway", "covers the screen for a moment while you talk"


def role_of(pid: str, e: dict) -> str:
    if e.get("as") in ROLES:
        return e["as"]
    return auto_role(e["file"], media_of(pid, e))[0]


def add(data: dict, files: list[str], media: dict | None = None) -> tuple[list[str], list[str]]:
    """Add uploaded Assets files to the pool (one upload batch is put in natural name order: 1_fed, 2_chart, 10_…).
    media = {file: inspect(...)} measured BEFORE taking the project lock (else inspected here)."""
    p, added, skipped = pool(data), [], []
    files = list(dict.fromkeys(files))
    if files and all(re.match(r"\s*\d", Path(f).name) for f in files):  # numbered 1_fed, 2_chart: that IS the order
        files.sort(key=lambda f: _natural(Path(f).name))
    for f in files:
        f = Path(str(f)).name
        e = entry(data, f)
        if e and not e.get("off"):
            skipped.append(f"{f} (already added)")
            continue
        if f.lower().endswith(tuple(core.AUDIO_EXT)):
            skipped.append(f"{f} (a sound – put sounds on an Audio track)")
            continue
        path = core.assets_dir(data["id"]) / f
        if not path.is_file():
            skipped.append(f"{f} (not uploaded)")
            continue
        if len([x for x in p["items"] if not x.get("off")]) >= MAX_POOL:
            skipped.append(f"{f} (at most {MAX_POOL} files)")
            continue
        if e:  # removed before and added again: fresh start
            p["items"].remove(e)
        p["items"].append({"file": f, "note": "", "added": core.now_iso(), "pin": None, "placed": [],
                           "media": (media or {}).get(f) or inspect(path)})
        added.append(f)
    return added, skipped


TRACK_NAME = "Pictures & clips"


def _track(data: dict, create: bool = False) -> dict | None:
    t = next((t for t in tracks.tracks_of(data) if t.get("pictures")), None)
    if t is None and create:
        t = {"id": core.new_id("tk"), "kind": "video", "name": TRACK_NAME, "off": False, "items": [], "pictures": True}
        data.setdefault("tracks", []).append(t)
    if t is not None and t.get("kind") == "broll":  # the first version used a silent B-roll track
        t["kind"] = "video"
        if t.get("name") == "Pictures":
            t["name"] = TRACK_NAME
        for x in t["items"]:
            x.setdefault("vol", 0.0)
    return t


def locked(data: dict) -> str:
    t = _track(data)
    return f"The “{t['name']}” track is locked – click 🔒 on it to unlock it first." if t and t.get("locked") else ""


def _unplace(data: dict, e: dict) -> None:
    t = _track(data)
    ids = {r.get("item") for r in _records(e)}
    if t and ids:
        t["items"] = [x for x in t["items"] if x.get("id") not in ids]
    e["placed"] = []


def remove(data: dict, file: str) -> bool:
    e = entry(data, file)
    if not e:
        return False
    _unplace(data, e)
    pool(data)["items"].remove(e)
    return True


def clear(data: dict) -> int:
    p = pool(data)
    n = len(p["items"])
    for e in list(p["items"]):
        _unplace(data, e)
    p["items"] = []
    p.pop("report", None)
    t = _track(data)
    if t and not t["items"]:
        data["tracks"].remove(t)
    return n


def set_note(data: dict, file: str, note: str) -> bool:
    e = entry(data, file)
    if not e:
        return False
    e["note"] = re.sub(r"\s+", " ", str(note or "")).strip()[:200]
    return True


def set_role(data: dict, file: str, role: str | None) -> str:
    e = entry(data, file)
    if not e:
        return "That file isn't in the list."
    if role not in ROLES and role not in (None, "", "auto"):
        return "Unknown use."
    e["as"] = role if role in ROLES else None
    e.pop("skip", None)  # a new role = fresh moments
    if role in ("transition", "transition_once"):  # a pin is a scene for a cutaway; a transition chooses its moments
        e["pin"] = None
    return ""


def set_sound(data: dict, file: str, on: bool) -> str:
    e = entry(data, file)
    if not e:
        return "That file isn't in the list."
    e["sound"] = bool(on)
    return ""


def set_pin(data: dict, file: str, scene_id: str | None, at=None, dur=None, by: str = "you") -> str:
    """Pin to a scene (None = unpin: placed by its words again). '' = ok, else the reason."""
    e = entry(data, file)
    if not e:
        return "That file isn't in the list."
    if scene_id is None:
        e["pin"] = None
        return ""
    sc = core.scene_by_id(data, scene_id)
    if not sc or not _in_video(data, sc):
        return "That scene isn't in the video."
    if by == "claude" and (e.get("pin") or {}).get("by") == "you":
        return "You pinned this one yourself – Claude leaves it."
    pin = {"scene": scene_id, "by": by}
    for k, v in (("at", at), ("dur", dur)):
        try:
            if v is not None and v != "":
                pin[k] = round(max(0.0, float(v)), 2)
        except (TypeError, ValueError):
            pass
    e["pin"] = pin
    e.pop("off", None)
    return ""


# ------------------------------------------------------------------ reading the scenes
def _in_video(data: dict, sc: dict) -> bool:
    skipped = {s["file"] for s in data["sources"] if s.get("include") is False}
    return sc.get("keep", True) and sc.get("role") != "broll" and sc["src"] not in skipped


def _norm(w: str) -> str:
    import autoedit
    return autoedit._norm(w)


def _tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    out = []
    for t in re.split(r"[^\w\u0900-\u097F₹%]+|_", text.lower()):  # Hindi vowel signs are not \w
        if not t or t.isdigit() or t in GENERIC or len(t) < 2:
            continue
        out.append(t)
    return out


def _stem_hit(tok: str, stem: str) -> bool:
    return tok == stem if len(stem) <= 3 else tok.startswith(stem)


def _topics(toks: list[str]) -> dict[str, int]:
    """Topics hit by a token list (two-word stems checked on neighbours)."""
    pairs = [f"{toks[i]} {toks[i + 1]}" for i in range(len(toks) - 1)]
    triples = [f"{toks[i]} {toks[i + 1]} {toks[i + 2]}" for i in range(len(toks) - 2)]
    hit = {}
    for name, wt, stems in TOPICS:
        for s in stems:
            pool_ = triples if s.count(" ") == 2 else pairs if " " in s else toks
            if any(_stem_hit(t, s) for t in pool_):
                hit[name] = wt
                break
    return hit


def scenes_info(pid: str, data: dict) -> list[dict]:
    """Every scene in the video: id, no, start (edited clock), len, words [(norm, out_time, shown)]."""
    import pipeline
    import typedcaps
    st = data["settings"]
    out, t, wcache, scache = [], 0.0, {}, {}
    for sc in data["scenes"]:
        if not _in_video(data, sc):
            continue
        src = sc["src"]
        if src not in wcache:
            raw = core.read_json(core.work_file(pid, src, "words.json"), []) or []
            try:
                wcache[src] = typedcaps.words_for(data, src, raw)
            except Exception:  # noqa: BLE001
                wcache[src] = raw
            scache[src] = pipeline.load_silences(pid, src)
        ranges = pipeline.kept_ranges(sc, scache[src], st)
        speed = float(sc.get("speed") or 1.0)
        ln = pipeline.scene_out_len(sc, scache[src], st)
        words = []
        ws_ = pipeline.words_in(wcache[src], sc["in"], sc["out"])
        if not sc.get("no_speech") or any(w.get("fixed") for w in ws_):  # your typed script counts even there
            for w in ws_:
                at = pipeline._map_time(max(0.0, float(w["s"]) - sc["in"]), ranges, speed)
                if at is None:
                    continue  # inside a cut pause
                n = _norm(w.get("w", ""))
                if n:
                    words.append((n, round(min(max(0.0, at), max(0.0, ln - 0.2)), 2), w.get("w", "").strip()))
        out.append({"id": sc["id"], "no": data["scenes"].index(sc) + 1, "start": round(t, 2), "len": round(ln, 2),
                    "words": words, "has_broll": bool(sc.get("broll")), "src": src})
        t += ln
    return out


def _clues(e: dict) -> list[str]:
    """Words in the file name + note; Hindi script read as Hinglish ("सोना की कीमत.jpg" -> sona, kimat) so it meets
    the scene words (which are Hinglish too)."""
    out = []
    for t in _tokens(Path(e["file"]).stem) + _tokens(SCENE_RE.sub(" ", e.get("note") or "")):
        if re.search(r"[ऀ-ॿ]", t):
            t = _norm(t)
            if not t or t in GENERIC or len(t) < 2:
                continue
        out.append(t)
    return out


def _score(clues: list[str], topics: dict, scn: dict) -> tuple[int, float | None, str]:
    """(score, output time of the word that matched, why)."""
    best, at, why = 0, None, ""
    toks = [w[0] for w in scn["words"]]
    for c in clues:  # the same word said: strongest (not "market" / "chart": said everywhere, weak on purpose)
        if len(c) < 4 or any(_stem_hit(c, s) for s in WEAK_STEMS):
            continue
        for n, t, shown in scn["words"]:
            if len(n) >= 4 and (n.startswith(c) or c.startswith(n)):
                if 5 > best:
                    best, at, why = 5, t, f"you say “{shown}”"
                break
    st = _topics(toks)
    shared = {k: v for k, v in topics.items() if k in st}
    tscore = sum(shared.values())
    if tscore and tscore >= best:
        top = max(shared, key=shared.get)
        stems = next(s for name, _, s in TOPICS if name == top)
        hit = next(((t, shown) for n, t, shown in scn["words"] if any(_stem_hit(n, s) for s in stems if " " not in s)),
                   None)
        best = max(best, tscore)
        if at is None and hit:
            at = hit[0]
        why = why or (f"topic {top}" + (f" – you say “{hit[1]}”" if hit else ""))
    return best, at, why


# ------------------------------------------------------------------ plan + apply
def _frame_ar(data: dict) -> float:
    import pipeline
    try:
        w, h = pipeline.target_size(data, False)
        return w / h if h else 0.5625
    except Exception:  # noqa: BLE001
        return 0.5625


def _fit_for(data: dict, e: dict, where: str) -> str:
    m = e.get("media") or (data.get("assets") or {}).get(e["file"]) or {}
    w, h = float(m.get("w") or m.get("width") or 0), float(m.get("h") or m.get("height") or 0)
    if not w or not h:
        return "fit"
    ar = _frame_ar(data)
    slot = {"full": ar, "top": ar * 2, "pip": 0.44 / 0.28 * ar}.get(where, ar)
    if data["settings"].get("layout") == "split" and where == "top":
        slot = ar / max(0.2, float(data["settings"].get("split_ratio") or 0.4))
    return "fill" if abs(math.log((w / h) / slot)) < 0.18 else "fit"


SFX_UNDER_VOICE = 8.0  # dB a transition's whoosh sits under YOUR voice (a cue, never louder than the words)


def voice_db(pid: str, data: dict) -> float | None:
    """Mean loudness of your footage's sound (the voice), measured once per set of clips and remembered."""
    srcs = sorted(s["file"] for s in data["sources"] if s.get("include", True) is not False and s.get("has_audio", True))
    p = pool(data)
    sig = [[f, (core.raw_dir(pid) / f).stat().st_size if (core.raw_dir(pid) / f).is_file() else 0] for f in srcs]
    if p.get("voice") and p["voice"].get("sig") == sig:
        return p["voice"].get("db")
    vals = [v for v in (_loudness(core.raw_dir(pid) / f) for f, n in sig if n) if v is not None and v > -70]
    db = round(sum(vals) / len(vals), 1) if vals else None
    p["voice"] = {"sig": sig, "db": db}
    return db


def _look(pid: str, data: dict, e: dict, role: str) -> dict:
    """where / fit / blend / loop / volume of this file's blocks."""
    m = media_of(pid, e)
    style = pool(data)["style"]
    split = data["settings"].get("layout") == "split"
    blend = m.get("blend") if m.get("blend") in BLENDS else "normal"
    moving = m.get("kind") in ("anim", "video")
    snd = m.get("has_audio") and (e.get("sound") if e.get("sound") is not None else role in ("transition", "transition_once", "overlay"))
    if role == "cutaway":
        where = e.get("where") if e.get("where") in tracks.WHERE else ("top" if split else "full") if style == "auto" else style
        fit = e.get("fit") if e.get("fit") in ("fit", "fill") else _fit_for(data, e, where)
        if blend in ("screen", "key"):
            blend = "normal"  # a cutaway covers the screen: no see-through
    elif role in ("sticker", "pip"):  # a framed box (you stay visible); a sticker is smaller and unframed if see-through
        where, fit = "pip", "fit"
    else:  # transition / overlay: the whole frame, see-through where the clip is black / green / transparent
        where, fit = "full", "fill"
    if role == "overlay" and blend == "normal":  # an ordinary photo "over you": half see-through, not a cover
        blend = "soft"
    return {"where": where, "fit": fit, "blend": blend, "keyc": m.get("keyc") if blend == "key" else None,
            "box": "small" if role == "sticker" else None,
            # a GIF always repeats (a one-frame GIF would otherwise last 0.05 s); a moving sticker too
            "loop": m.get("kind") == "anim" or e["file"].lower().endswith(".gif") or (role == "sticker" and moving),
            # its sound, levelled so it sits under your voice (overlays a little softer: they run longer)
            "vol": _sfx_vol(pid, data, m, role) if snd else 0.0,
            "fit_auto": role == "cutaway" and not e.get("fit")}  # re-decided per frame shape at export


def _sfx_vol(pid: str, data: dict, m: dict, role: str) -> float:
    """Its sound levelled to sit SFX_UNDER_VOICE dB under your voice (overlays 3 dB softer: they run longer)."""
    vd, sd = voice_db(pid, data), m.get("snd_db")
    under = SFX_UNDER_VOICE + (3.0 if role == "overlay" else 0.0)
    if vd is not None and sd is not None:
        return round(min(1.5, max(0.03, 10 ** ((vd - under - sd) / 20))), 3)
    return round(float(m.get("snd_vol") or 0.5) * (1.0 if role != "overlay" else 0.7), 3)


def _dur(pid: str, e: dict, role: str) -> float:
    if (e.get("pin") or {}).get("dur"):
        return float(e["pin"]["dur"])
    m = media_of(pid, e)
    d = float(m.get("dur") or 0)
    if role == "sticker":
        return STICKER_DUR
    if role == "pip":
        return max(MIN_SHOW, min(CLIP_MAX, d)) if m.get("kind") == "video" and d else STILL_DUR
    if role in ("transition", "transition_once"):
        return max(0.4, min(TRANS_MAX, d if m.get("kind") != "still" else 1.0)) if d or m.get("kind") == "still" else 1.0
    if role == "overlay":
        return max(MIN_SHOW, min(OVER_MAX, d)) if m.get("kind") == "video" and d else STILL_DUR
    if m.get("kind") == "video" and d:
        return max(MIN_SHOW, min(CLIP_MAX, d))
    return STILL_DUR


def _place_group(pid: str, data: dict, scs: list[dict], entries: list[dict], left: list, role_name: str,
                 cover: float) -> list[dict]:
    """Cutaways (or overlays / stickers, as their own layer): each on its scene, timed to its word."""
    if not entries:
        return []
    by_id = {s["id"]: s for s in scs}
    first_id = scs[0]["id"]
    roles = {e["file"]: role_of(pid, e) for e in entries}

    def lead(s):  # keep the hook: the opening of the video shows your face
        return min(1.0, s["len"] * 0.25) if s["id"] == first_id and role_name == "cutaway" else 0.15

    def room(s):
        return max(0.0, s["len"] - lead(s) - 0.1)

    def cap(s):
        return max(0, int(room(s) * cover / MIN_SHOW + 1e-6)) if room(s) >= MIN_SHOW else 0

    assign: dict[str, list] = {s["id"]: [] for s in scs}  # scene id -> [(entry, at_hint, why, pin)]
    prev_idx, prev_strong = -1, False
    follows: dict[str, str] = {}  # file -> the one it goes with (shown straight AFTER it, never before)
    idx_of = {s["id"]: i for i, s in enumerate(scs)}
    for e in entries:
        pin = e.get("pin") or {}
        m = SCENE_RE.search(e.get("note") or "")
        if not pin and m:  # "scene 3" in your note = your pin
            n = int(m.group(1))
            sc = data["scenes"][n - 1] if 0 < n <= len(data["scenes"]) else None
            if sc and sc["id"] in by_id:
                am = AT_RE.search(e.get("note") or "")
                pin = {"scene": sc["id"], "by": "you", **({"at": float(am.group(1))} if am else {})}
        left_note = ""
        if pin and pin.get("scene") not in by_id:
            e["pin"], pin = None, {}
            left_note = "its pinned scene is no longer in the video – placed by its words instead"
        if pin:
            assign[pin["scene"]].append((e, pin.get("at"), "pinned by " + ("you" if pin.get("by") != "claude" else "Claude"), pin))
            prev_idx, prev_strong = idx_of[pin["scene"]], True
            continue
        clues = _clues(e)
        tops = _topics(clues)
        scored = [(_score(clues, tops, s), i) for i, s in enumerate(scs) if not (s["has_broll"] and role_name == "cutaway")]
        best = max((x[0][0] for x in scored), default=0)
        choice, at, why = None, None, ""
        if best >= 3:  # a real clue: the best scene (ties: one without anything yet, earliest)
            cands = [(sc_, i) for sc_, i in scored if sc_[0] == best]
            cands.sort(key=lambda x: (len(assign[scs[x[1]]["id"]]) >= cap(scs[x[1]]), len(assign[scs[x[1]]["id"]]), x[1]))
            (_, at, why), choice = cands[0][0], cands[0][1]
            prev_strong = True
        else:  # weak / no clue: story order – join the one before it, else the next scene
            weak = {i: sc_ for sc_, i in scored if sc_[0] > 0}
            order = [i for _, i in scored]
            if prev_idx >= 0 and prev_strong and prev_idx in order and len(assign[scs[prev_idx]["id"]]) < cap(scs[prev_idx]):
                choice, why = prev_idx, "right after the one before it (they go together)"
                last = assign[scs[prev_idx]["id"]][-1]
                at = None if last[1] is None else float(last[1]) + 0.01
                follows[e["file"]] = last[0]["file"]
            else:
                after = [i for i in order if i > prev_idx] or order
                after.sort(key=lambda i: (len(assign[scs[i]["id"]]) >= cap(scs[i]), len(assign[scs[i]["id"]]), i))
                choice = after[0] if after else None
                why = "story order (no clue in the name or note)"
                if choice is not None and choice in weak:
                    at, why = weak[choice][1], weak[choice][2] or why
            prev_strong = False
        if choice is None:
            left.append((e["file"], "no scene left to put it on"))
            continue
        if len(assign[scs[choice]["id"]]) >= cap(scs[choice]):  # full scene: the nearest later scene with room
            alt = [i for i in range(len(scs)) if not (scs[i]["has_broll"] and role_name == "cutaway")
                   and len(assign[scs[i]["id"]]) < cap(scs[i])]
            if not alt:
                left.append((e["file"], "the video has no room left (every scene is full) – remove some"))
                continue
            alt.sort(key=lambda i: (i < choice, abs(i - choice)))
            choice, at, why = alt[0], None, why + f" – scene {scs[choice]['no']} was full, so the next one"
        assign[scs[choice]["id"]].append((e, at, why + (f"; {left_note}" if left_note else ""), {}))
        prev_idx = choice
    places = []
    for s in scs:
        lst = assign[s["id"]]
        if not lst:
            continue
        lo, hi = lead(s), max(lead(s), s["len"] - 0.1)
        span = hi - lo
        items = []
        for k, (e, at, why, pin) in enumerate(lst):
            fixed = pin.get("at") is not None  # your exact spot (a dragged block): kept as it is, no drift per run
            if fixed:  # your spot – but never past the scene's end ("scene 3 at 50" on a 10 s scene)
                at = min(float(pin["at"]), max(0.0, s["len"] - MIN_SHOW))
            d0 = _dur(pid, e, roles[e["file"]])
            d = d0 if pin.get("dur") else min(d0, max(MIN_SHOW, span))
            want = at if fixed else (max(lo, float(at) - 0.15) if at is not None
                                     else lo + span * (k + 0.5) / len(lst) - d / 2)
            items.append({"e": e, "want": max(0.0 if fixed else lo, want), "dur": d, "why": why, "fixed": fixed})
        items.sort(key=lambda x: x["want"])
        free = [x for x in items if not x["fixed"]]
        total = sum(x["dur"] for x in free)
        budget = max(MIN_SHOW * len(free), span * cover - sum(x["dur"] for x in items if x["fixed"]))
        if free and total > budget:  # too many for the scene: shorten the free ones, never under MIN_SHOW
            f = budget / total
            for x in free:
                x["dur"] = max(MIN_SHOW, round(x["dur"] * f, 2))
        # your own spots first, exactly where you put them; the rest fill the free time nearest their word, one at a
        # time (never two at once), a little later rather than before the word
        taken = sorted((x["want"], x["want"] + x["dur"]) for x in items if x["fixed"])
        for x in items:
            if x["fixed"]:
                x["at"] = x["want"]
        for x in free:  # already in word order
            gaps, cur = [], lo
            for a0, b0 in sorted(taken):
                if a0 > cur:
                    gaps.append((cur, min(a0, hi)))
                cur = max(cur, b0)
            if cur < hi:
                gaps.append((cur, hi))
            part = next((y for y in items if y["e"]["file"] == follows.get(x["e"]["file"]) and "at" in y), None)
            if part:  # after its partner – unless there is no room after it at all
                after = [(max(g0, part["at"] + part["dur"]), g1) for g0, g1 in gaps if g1 > part["at"] + part["dur"]]
                if any(g1 - g0 >= min(x["dur"], MIN_SHOW) - 1e-6 for g0, g1 in after):
                    gaps = after
            best = None
            for g0, g1 in gaps:
                if g1 - g0 < min(x["dur"], MIN_SHOW) - 1e-6:
                    continue
                dd = min(x["dur"], g1 - g0)
                a = min(max(x["want"], g0), g1 - dd)
                cost = abs(a - x["want"]) * (1.5 if a < x["want"] else 1.0)
                if best is None or cost < best[0]:
                    best = (cost, a, dd)
            if best is None:
                x["skip"] = True
                left.append((x["e"]["file"], f"scene {s['no']} has no free time left for it"))
                continue
            x["at"], x["dur"] = best[1], best[2]
            taken.append((x["at"], x["at"] + x["dur"]))
        for x in items:
            if x.get("skip"):
                continue
            places.append({"file": x["e"]["file"], "scene": s["id"], "no": s["no"], "at": round(x["at"], 2),
                           "dur": round(x["dur"], 2), "why": x["why"], "t": round(s["start"] + x["at"], 2),
                           "key": None})
    return places


def moments(scs: list[dict]) -> list[dict]:
    """Where a new point starts: "pehla / doosra / teesra … reason" (ordinal + a list word within 3 words), else a
    change of clip, else a few evenly spread scene cuts. [{t (edited clock), scene, why, key}]"""
    out = []
    flat = [(n, t, shown, s) for s in scs for n, t, shown in s["words"]]  # across scene cuts ("Second" | "bada reason")
    for i, (n, t, shown, s) in enumerate(flat):
        nxt = [w[0] for w in flat[i + 1:i + 4]]
        prev = flat[i - 1][2] if i else "."
        num = n == "number" and nxt and re.fullmatch(r"\d|one|two|three|four|five|ek|do|teen|char|paanch", nxt[0])
        starts = prev.rstrip().endswith((".", "?", "!", "।"))  # "… rate. Second, dollar …" – a sentence opens with it
        if (n in ORDINALS and (any(x.startswith(LIST_NOUNS) for x in nxt) or starts)) or num:
            out.append({"t": s["start"] + t, "scene": s["id"], "why": f"new point – you say “{shown}”",
                        "key": f"{s['id']}@{t:.1f}"})
    if not out:
        cuts = [{"t": b["start"], "scene": b["id"], "why": "a new clip starts", "key": f"cut:{b['id']}"}
                for a, b in zip(scs, scs[1:]) if a["src"] != b["src"]]
        if not cuts and len(scs) > 1:
            allc = [{"t": b["start"], "scene": b["id"], "why": "scene change", "key": f"cut:{b['id']}"}
                    for b in scs[1:]]
            k = min(3, len(allc))
            cuts = [allc[round((j + 0.5) * len(allc) / k - 0.5)] for j in range(k)]
        out = cuts
    res = []
    for m in sorted(out, key=lambda m: m["t"]):  # never in the opening second; at least 2 s apart
        if m["t"] < 1.0 or (res and m["t"] - res[-1]["t"] < 2.0):
            continue
        res.append(m)
    return res[:MAX_MOMENTS]


def _anchor(scs: list[dict], t_abs: float) -> tuple[dict, float]:
    total = scs[-1]["start"] + scs[-1]["len"]
    t_abs = max(0.0, min(t_abs, total - 0.3))
    s = next((s for s in scs if s["start"] <= t_abs < s["start"] + s["len"]), scs[-1])
    return s, round(t_abs - s["start"], 2)


def _place_transitions(pid: str, data: dict, scs: list[dict], entries: list[dict], left: list) -> list[dict]:
    if not entries:
        return []
    ms = moments(scs)
    by_id = {s["id"]: s for s in scs}
    places, used = [], set()
    free = []
    for e in entries:  # a pinned transition: "scene 3" = into scene 3 (its start), "scene 3 at 2" = that moment
        pin = e.get("pin") or {}
        m = SCENE_RE.search(e.get("note") or "")
        if not pin and m:
            n = int(m.group(1))
            sc = data["scenes"][n - 1] if 0 < n <= len(data["scenes"]) else None
            am = AT_RE.search(e.get("note") or "")
            if sc and sc["id"] in by_id:
                pin = {"scene": sc["id"], "by": "you", **({"at": float(am.group(1))} if am else {})}
        if pin and pin.get("scene") in by_id:
            s = by_id[pin["scene"]]
            d = _dur(pid, e, "transition")
            t = s["start"] + (float(pin["at"]) if pin.get("at") is not None else 0.0)
            a_s, off = _anchor(scs, t if pin.get("at") is not None else t - d * 0.5)
            places.append({"file": e["file"], "scene": a_s["id"], "no": a_s["no"], "at": off, "dur": round(d, 2),
                           "why": "pinned by " + ("you" if pin.get("by") != "claude" else "Claude"),
                           "t": round(a_s["start"] + off, 2), "key": f"pin:{pin['scene']}"})
        else:
            free.append(e)
    if not free:
        return places
    if not ms:
        for e in free:
            left.append((e["file"], "the video has only one scene and no “pehla / doosra … reason” moment – "
                                    "pick a scene for it"))
        return places
    # every moment gets one transition, taken in turn from your transition files ("once" ones take only one)
    once_done: set = set()
    j = 0
    for m in ms:
        tries = 0
        while tries < len(free):
            e = free[j % len(free)]
            j += 1
            tries += 1
            if m["key"] in (e.get("skip") or []):
                continue
            if role_of(pid, e) == "transition_once" and e["file"] in once_done:
                continue
            d = _dur(pid, e, "transition")
            a_s, off = _anchor(scs, m["t"] - d * 0.5 - 0.05)  # its middle (the peak) right on the word / cut
            places.append({"file": e["file"], "scene": a_s["id"], "no": by_id[m["scene"]]["no"], "at": off,
                           "dur": round(d, 2), "why": m["why"], "t": round(a_s["start"] + off, 2), "key": m["key"]})
            once_done.add(e["file"])
            used.add(e["file"])
            break
    for e in free:
        if e["file"] not in used:
            left.append((e["file"], "every new-point moment already has a transition – pick a scene for it"))
    return places


def plan(pid: str, data: dict) -> dict:
    """Decide where every file goes (no changes). {"places": [{file, scene, no, at, dur, why, t, key}],
    "left": [(file, why)]}"""
    p = pool(data)
    scs = scenes_info(pid, data)
    live = [e for e in p["items"] if not e.get("off")]
    if not scs:
        return {"places": [], "left": [(e["file"], "no scenes in the video") for e in live]}
    left, ok = [], []
    for e in live:
        if (core.assets_dir(pid) / e["file"]).is_file():
            ok.append(e)
        else:
            left.append((e["file"], "the file is gone from Assets (deleted?)"))
    rl = {e["file"]: role_of(pid, e) for e in ok}
    places = _place_group(pid, data, scs, [e for e in ok if rl[e["file"]] == "cutaway"], left, "cutaway", COVER)
    places += _place_group(pid, data, scs, [e for e in ok if rl[e["file"]] in ("overlay", "sticker", "pip")], left, "extra", 0.9)
    places += _place_transitions(pid, data, scs, [e for e in ok if rl[e["file"]] in ("transition", "transition_once")], left)
    return {"places": places, "left": left}


def _harvest(data: dict) -> list[str]:
    """Read the timeline back: a block you moved / trimmed = your pin (a transition block = yours to keep); a block
    you deleted = left out (a transition: only that moment)."""
    notes, t = [], _track(data)
    if t is None:  # the whole track was deleted: ▶ Place = start fresh (not "you removed every block")
        for e in pool(data)["items"]:
            e["placed"] = []
        return notes
    ids = {x.get("id"): x for x in t["items"]}
    for e in pool(data)["items"]:
        recs = _records(e)
        if not recs:
            continue
        multi = len(recs) > 1 or e.get("as") in ("transition", "transition_once") or any(r.get("key") for r in recs)
        keep = []
        for r in recs:
            it = ids.get(r.get("item"))
            if it is None:
                if multi and r.get("key"):
                    e.setdefault("skip", []).append(r["key"])
                else:
                    e["off"] = True
                    notes.append(f"{e['file']}: left out – you removed its block (add it again to bring it back)")
                continue
            moved = it.get("anchor") != r.get("anchor") or abs(float(it.get("off") or 0) - float(r.get("off") or 0)) > 0.05 \
                or abs(float(it.get("dur") or 0) - float(r.get("dur") or 0)) > 0.05
            if moved and multi:  # your hand-placed transition: stays on the timeline as yours, that moment is taken
                if r.get("key"):
                    e.setdefault("skip", []).append(r["key"])
                notes.append(f"{e['file']}: the block you moved is kept as yours")
                continue
            if moved:
                e["pin"] = {"scene": it["anchor"], "at": round(float(it.get("off") or 0), 2),
                            "dur": round(float(it.get("dur") or 0), 2), "by": "you"}
            if it.get("where") != r.get("where"):
                e["where"] = it.get("where")
            if it.get("fit") != r.get("fit"):
                e["fit"] = it.get("fit")
            keep.append(r)
        e["placed"] = keep
    return notes


def run(pid: str, data: dict) -> dict:
    """Read your timeline edits back, plan, rebuild the track's own blocks (call inside mutate after a snapshot)."""
    p = pool(data)
    t0 = _track(data)
    if t0 and t0.get("locked"):
        raise ValueError(f"The “{t0['name']}” track is locked – click 🔒 on it to unlock it first.")
    notes = _harvest(data)
    pl = plan(pid, data)
    t = _track(data, create=bool(pl["places"]))
    mine = {r.get("item") for e in p["items"] for r in _records(e)} - {None}
    if t:
        t["items"] = [x for x in t["items"] if x.get("id") not in mine]
    for e in p["items"]:
        e["placed"] = []
        e.pop("why", None)
        e.pop("scene_no", None)
    by_file = {e["file"]: e for e in p["items"]}
    placed, report = 0, []
    order = {"cutaway": 0, "overlay": 1, "pip": 2, "sticker": 2, "transition": 3, "transition_once": 3}  # later = on top
    for x in sorted(pl["places"], key=lambda x: order.get(role_of(pid, by_file[x["file"]]), 0)):
        e = by_file[x["file"]]
        role = role_of(pid, e)
        if len(t["items"]) >= tracks.MAX_ITEMS:
            pl["left"].append((x["file"], f"the track is full ({tracks.MAX_ITEMS} blocks)"))
            continue
        lk = _look(pid, data, e, role)
        m_ = media_of(pid, e)
        if m_.get("w") and m_.get("h"):
            lk["ar"] = round(float(m_["w"]) / float(m_["h"]), 4)
        it, err = tracks._clean_item(pid, data, "video", {"anchor": x["scene"], "off": x["at"], "dur": x["dur"],
                                                          "src": {"t": "asset", "n": x["file"]}, "in": 0, **lk})
        if err:
            pl["left"].append((x["file"], err))
            continue
        t["items"].append(it)
        e["placed"].append({"item": it["id"], "anchor": it["anchor"], "off": it["off"], "dur": it["dur"],
                            "where": it["where"], "fit": it["fit"], "key": x.get("key")})
        if not e.get("scene_no"):
            e["scene_no"], e["why"] = x["no"], x["why"]
        placed += 1
        look = ({"alpha": "see-through", "screen": "black blended away", "key": "green keyed out",
                 "soft": "half see-through over you"}.get(lk["blend"]) or ("corner box" if it["where"] == "pip" else None)
                or ("whole picture over blur" if it["fit"] == "fit" else "fills"))
        report.append(f"{x['file']} ({ROLE_NAMES[role]}) → scene {x['no']} at {x['at']:.1f}s for {it['dur']:.1f}s "
                      f"({look}{', with its sound' if lk['vol'] > 0 else ''}) – {x['why']}")
    for e in p["items"]:
        if len(e["placed"]) > 1:
            e["why"] = f"{len(e['placed'])} moments: scenes " + ", ".join(
                str(r_["no"]) for r_ in sorted((x for x in pl["places"] if x["file"] == e["file"]), key=lambda x: x["t"]))
    for f, why in pl["left"]:
        if f in by_file and not by_file[f]["placed"]:
            by_file[f]["why"] = "not placed – " + why
        report.append(f"{f}: not placed – {why}")
    if t and not t["items"] and not p["items"]:
        data["tracks"].remove(t)
    if t and placed and t.get("off"):
        notes.append(f"The “{t['name']}” track is HIDDEN – click its 👁 to show the pictures in the video.")
    p["ran"], p["report"] = core.now_iso(), notes + report
    return {"placed": placed, "left": len(pl["left"]), "report": p["report"], "hidden": bool(t and t.get("off"))}


def describe(pid: str, data: dict) -> str:
    """For Claude: every file (full path to LOOK at), its role, where it is now, and every scene's words with times."""
    p = pool(data)
    lines = [f"style={p['style']}  layout={data['settings'].get('layout', 'full')}  roles: {', '.join(ROLES)}"]
    for e in p["items"]:
        pin, m = e.get("pin") or {}, media_of(pid, e)
        recs = _records(e)
        look_at = core.assets_dir(pid) / e["file"]
        if m.get("kind") in ("video", "anim"):  # a clip can't be opened as a picture: its poster frame can
            import hashlib
            th = core.work_dir(pid) / "thumbs" / f"asset_{hashlib.sha1(e['file'].encode()).hexdigest()[:12]}.jpg"
            look_at = th if th.is_file() else look_at
        lines.append(f"FILE {e['file']}  look_at={look_at}  role={role_of(pid, e)}"
                     f"{'' if e.get('as') else ' (detected)'}  kind={m.get('kind')} blend={m.get('blend')} "
                     f"dur={m.get('dur')}  note={e.get('note')!r}  {'OFF (user removed it) ' if e.get('off') else ''}"
                     f"now: {('scene ' + str(e.get('scene_no')) + ' @' + str(recs[0]['off']) + 's') if recs else 'not placed'}"
                     f"{f' (+{len(recs) - 1} more)' if len(recs) > 1 else ''}  pin={pin.get('by') or '-'}  why={e.get('why', '')!r}")
    for s in scenes_info(pid, data):
        ws = " ".join(f"{shown}@{t:.1f}" for _, t, shown in s["words"][:120])
        lines.append(f"SCENE {s['no']} len {s['len']:.1f}s{' (has its own B-roll – no cutaways)' if s['has_broll'] else ''}: {ws}")
    return "\n".join(lines)


def claude_text(data: dict) -> str:
    n = len([e for e in pool(data)["items"] if not e.get("off")])
    return (f"place pictures: LOOK at each of the {n} uploaded picture(s) / clip(s) and pin each cutaway, overlay or "
            f"sticker to the scene where the speaker talks about what it shows (Powell -> the Fed / rate part, a chart "
            f"next to the picture it belongs with), timed to that word (Read each look_at file to SEE it – for clips it is "
            f"a frame of the clip); leave transitions on their moments unless one "
            f"is clearly wrong. Use `pictures <pid> show`, `pictures <pid> pin <file> <sceneNo> [at] [dur]`, "
            f"`pictures <pid> role <file> <role>` if the detected role is wrong, then `pictures <pid> run`. "
            f"Leave files pinned by the user alone.")
