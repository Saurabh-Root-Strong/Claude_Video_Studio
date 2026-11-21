"""Extra timeline TRACKS: any number of Video / B-roll / Audio / Text tracks, added and deleted with the + button.

Model ("connected clips", like Final Cut Pro's magnetic timeline): the scenes stay the main storyline; every track item
is CONNECTED to a scene (anchor = scene id) at an offset (seconds into that scene, edited time). Reorder, trim or speed
up scenes and the B-roll / music / titles connected to them move along – they never drift onto the wrong words.
An item may run on past its scene's end; its absolute time is simply start(anchor) + off.

    data["tracks"] = [{"id", "kind": video|broll|audio|text, "name", "off": bool (hidden / muted), "items": [...]}]
    item (all):   {"id", "anchor": scene id, "off": s, "dur": s}
    video/broll:  + "src": {"t": clip|asset|lib|music, "n": name}, "in": s into the source, "src_len", "where":
                    full|top|bottom|pip, "fit": fill|fit, "vol" (video only: its own sound, 0-2)
    audio:        + "src", "in", "src_len", "vol", "fade_in", "fade_out"
    text:         + "text", "style": title|lower|top

Stacking: later tracks are drawn on top of earlier ones; all of them sit above the scene's own picture / B-roll and
BELOW the captions and stickers (words stay readable). Video and B-roll pictures are rendered per scene in slices (the
scene cache stays valid); audio (incl. a video item's own sound) is mixed once over the whole timeline (no clicks at
scene joins).
"""
from __future__ import annotations

import re
from pathlib import Path

import core
import library

KINDS = {"video": "Video", "broll": "B-roll", "audio": "Audio", "text": "Text"}
WHERE = ("full", "top", "bottom", "pip")
TEXT_STYLES = ("title", "lower", "top")
MAX_TRACKS, MAX_ITEMS = 12, 60
AUDIO_EXT = core.AUDIO_EXT


def tracks_of(data: dict) -> list[dict]:
    t = data.get("tracks")
    return t if isinstance(t, list) else []


def _find(data: dict, tid: str) -> dict | None:
    return next((t for t in tracks_of(data) if t.get("id") == tid), None)


def media_path(pid: str, data: dict, ref) -> Path | None:
    """The file behind an item's source reference, or None (missing / skipped / outside its folder)."""
    if not isinstance(ref, dict) or not isinstance(ref.get("n"), str) or not ref["n"]:
        return None
    t, n = ref.get("t"), ref["n"]
    if t == "clip":
        s = core.source_by_file(data, n)
        p = core.raw_dir(pid) / n
        return p if s is not None and s.get("include", True) is not False and p.is_file() else None
    if t == "asset":
        return core.broll_path(pid, {"sources": []}, Path(n).name)  # Assets/ (or its trash, for undo)
    if t == "lib":
        return library.path_of(n)
    if t == "music":
        p = (core.BRAND / "music" / Path(n).name)
        return p if p.is_file() else None
    return None


def _num(v, lo: float, hi: float, default: float | None = None) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return round(min(hi, max(lo, x)), 3)


def _clean_item(pid: str, data: dict, kind: str, it: dict, old: dict | None = None) -> tuple[dict | None, str]:
    """Validate an item (new or changed fields merged onto old). Returns (item, error)."""
    import pipeline
    cur = dict(old or {})
    cur.update({k: v for k, v in it.items() if k not in ("id",)})
    out = {"id": cur.get("id") or core.new_id("ti")}
    if cur.get("from_scene"):
        out["from_scene"] = cur["from_scene"]  # lifted from Video 1: dragging it back restores THAT scene
    if not core.scene_by_id(data, str(cur.get("anchor") or "")):
        return None, "That scene no longer exists – the page will refresh."
    out["anchor"] = cur["anchor"]
    out["off"] = _num(cur.get("off"), 0.0, 3600.0, 0.0)
    out["dur"] = _num(cur.get("dur"), 0.2, 600.0, 3.0)
    if kind == "text":
        txt = re.sub(r"\s+", " ", str(cur.get("text") or "")).strip()[:200]
        if not txt:
            return None, "Type the text first."
        out["text"] = txt
        out["style"] = cur.get("style") if cur.get("style") in TEXT_STYLES else "title"
        return out, ""
    src = cur.get("src")
    p = media_path(pid, data, src)
    if not p:
        return None, "That file isn't available (missing, skipped, or not in the project)."
    if p.suffix.lower() in AUDIO_EXT and kind != "audio":
        return None, "That is a sound file – put it on an Audio track."
    out["src"] = {"t": src["t"], "n": src["n"]}
    still = core.is_still(p.name)
    if kind == "audio" and still:
        return None, "That is a picture – put it on a Video or B-roll track."
    if (old or {}).get("src") == out["src"] and old.get("src_len") is not None:
        out["src_len"] = old["src_len"]
    else:
        try:
            m = pipeline.probe(p)
        except Exception:  # noqa: BLE001
            return None, "That file can't be read."
        if kind == "audio" and not m.get("has_audio"):
            return None, "That file has no sound."
        out["src_len"] = 0.0 if still else round(float(m.get("duration") or 0), 3)
        out["has_audio"] = bool(m.get("has_audio"))
    if "has_audio" not in out:
        out["has_audio"] = bool((old or {}).get("has_audio", True))
    out["in"] = _num(cur.get("in"), 0.0, 36000.0, 0.0)
    if out["src_len"] > 0:  # a video / sound can't play past its end: shorten to what's left
        if out["in"] >= out["src_len"] - 0.2:
            out["in"] = max(0.0, round(out["src_len"] - 1.0, 3))
        out["dur"] = round(min(out["dur"], max(0.2, out["src_len"] - out["in"])), 3)
    if kind in ("video", "broll"):
        out["where"] = cur.get("where") if cur.get("where") in WHERE else "full"
        out["fit"] = "fit" if cur.get("fit") == "fit" else "fill"
        if kind == "video":
            out["vol"] = _num(cur.get("vol"), 0.0, 2.0, 1.0)
    else:
        out["vol"] = _num(cur.get("vol"), 0.0, 2.0, 1.0)
        out["fade_in"] = _num(cur.get("fade_in"), 0.0, 10.0, 0.0)
        out["fade_out"] = _num(cur.get("fade_out"), 0.0, 10.0, 0.0)
    return out, ""


def apply_op(pid: str, data: dict, body: dict) -> tuple[str, str, dict]:
    """One timeline-track edit (the caller snapshots for undo). Returns (reply, error, extra)."""
    op = body.get("op")
    tr = data.setdefault("tracks", [])
    if op == "lane":  # the built-in lanes: Video 1 mute, B-roll / Cards hide, any lane lock
        return _lane_op(data, body)
    if op in ("item_add", "scene_to_track") and body.get("new_kind"):  # dropped on the empty space: new track first
        r, err, x = apply_op(pid, data, {"op": "add", "kind": body["new_kind"]})
        if err:
            return "", err, {}
        body = dict(body, track=x["track"])
    if op == "add":
        kind = body.get("kind")
        if kind not in KINDS:
            return "", "Unknown track type.", {}
        if len(tr) >= MAX_TRACKS:
            return "", f"At most {MAX_TRACKS} extra tracks.", {}
        n = 1 + sum(1 for t in tr if t["kind"] == kind)
        t = {"id": core.new_id("tk"), "kind": kind, "name": f"{KINDS[kind]} {n + (1 if kind in ('video', 'broll') else 0)}",
             "off": False, "items": []}
        tr.append(t)
        return f"Added track “{t['name']}”.", "", {"track": t["id"]}
    t = _find(data, str(body.get("track") or ""))
    if not t:
        return "", "That track no longer exists – the page will refresh.", {}
    if op == "lock":
        t["locked"] = not t.get("locked")
        return f"Track “{t['name']}” {'locked – it can’t be changed until you unlock it' if t['locked'] else 'unlocked'}.", "", {}
    if t.get("locked") and op in ("item_add", "item_set", "item_remove", "scene_to_track", "remove", "item_to_scene",
                                  "item_move_track"):
        return "", f"Track “{t['name']}” is locked – click 🔒 to unlock it first.", {}
    if op == "remove":
        tr.remove(t)
        return f"Deleted track “{t['name']}”" + (f" and its {len(t['items'])} item(s)" if t["items"] else "") + " (Undo brings it back).", "", {}
    if op == "rename":
        name = re.sub(r"\s+", " ", str(body.get("name") or "")).strip()[:40]
        if not name:
            return "", "Give the track a name.", {}
        t["name"] = name
        return f"Track renamed to “{name}”.", "", {}
    if op == "mute":
        t["mute"] = not t.get("mute")
        return f"Track “{t['name']}” sound {'off' if t['mute'] else 'on'}.", "", {}
    if op == "toggle":
        t["off"] = not t.get("off")
        verb = ("muted" if t["off"] else "unmuted") if t["kind"] == "audio" else ("hidden" if t["off"] else "shown")
        return f"Track “{t['name']}” {verb}.", "", {}
    if op == "move":
        i, j = tr.index(t), tr.index(t) + (1 if body.get("dir") == "up" else -1)
        if not 0 <= j < len(tr):
            return "", "It is already at the edge.", {}
        tr[i], tr[j] = tr[j], tr[i]
        return "Track order changed (higher = drawn on top).", "", {}
    if op == "item_add":
        if len(t["items"]) >= MAX_ITEMS:
            return "", f"At most {MAX_ITEMS} items per track.", {}
        it, err = _clean_item(pid, data, t["kind"], body.get("item") or {})
        if err:
            return "", err, {}
        t["items"].append(it)
        return f"Added to “{t['name']}”.", "", {"item": it["id"], "track": t["id"]}
    if op == "scene_to_track":
        return _scene_to_track(pid, data, t, body)
    it0 = next((x for x in t["items"] if x.get("id") == body.get("item")), None)
    if not it0:
        return "", "That item no longer exists – the page will refresh.", {}
    if op == "item_set":
        it, err = _clean_item(pid, data, t["kind"], body.get("set") or {}, it0)
        if err:
            return "", err, {}
        t["items"][t["items"].index(it0)] = it
        return "", "", {"item": it["id"]}
    if op == "item_remove":
        t["items"].remove(it0)
        return f"Removed from “{t['name']}”.", "", {}
    if op == "item_to_scene":
        return _item_to_scene(pid, data, t, it0, body)
    if op == "item_move_track":
        return _item_move_track(pid, data, t, it0, body)
    return "", "Unknown edit.", {}


def _lane_op(data: dict, body: dict) -> tuple[str, str, dict]:
    lane, what = body.get("lane"), body.get("what")
    if what == "lock" and lane in ("main", "broll", "cards"):
        locks = data.setdefault("ui_locks", {})
        locks[lane] = not locks.get(lane)
        name = {"main": "Video 1", "broll": "B-roll", "cards": "Cards"}[lane]
        return f"“{name}” {'locked' if locks[lane] else 'unlocked'}.", "", {}
    key = {("main", "mute"): "main_mute", ("broll", "hide"): "broll_off", ("cards", "hide"): "cards_off"}.get((lane, what))
    if not key:
        return "", "Unknown lane switch.", {}
    st = data["settings"]
    st[key] = not st.get(key)
    return {"main_mute": "Your scenes' own sound is " + ("OFF (only music / track audio plays)" if st[key] else "back on"),
            "broll_off": "Scene B-roll " + ("hidden" if st[key] else "shown again"),
            "cards_off": "Top cards " + ("hidden" if st[key] else "shown again")}[key] + ".", "", {}


COMPAT = {"video": ("video", "broll"), "broll": ("video", "broll"), "audio": ("audio",), "text": ("text",)}


def _item_move_track(pid: str, data: dict, t: dict, it0: dict, body: dict) -> tuple[str, str, dict]:
    """Drag a block onto ANOTHER track: Video <-> B-roll (pictures), Audio <-> Audio, Text <-> Text."""
    to = _find(data, str(body.get("to") or ""))
    if not to or to is t:
        return "", "That track no longer exists – the page will refresh.", {}
    if to.get("locked"):
        return "", f"Track “{to['name']}” is locked – click 🔒 to unlock it first.", {}
    if to["kind"] not in COMPAT.get(t["kind"], ()):
        what = {"audio": "sounds", "text": "titles"}.get(t["kind"], "pictures")
        art = lambda w: ("an " if w[0] in "AEIOU" else "a ") + w  # noqa: E731
        return "", f"{art(KINDS[t['kind']]).capitalize()} block can't go on {art(KINDS[to['kind']])} track ({what} only).", {}
    if len(to["items"]) >= MAX_ITEMS:
        return "", f"At most {MAX_ITEMS} items per track.", {}
    src = dict(it0, **{k: body[k] for k in ("anchor", "off") if k in body})
    if to["kind"] == "video" and "vol" not in src:
        src["vol"] = 1.0
    it, err = _clean_item(pid, data, to["kind"], src)
    if err:
        return "", err, {}
    t["items"].remove(it0)
    to["items"].append(it)
    note = " (silent there – a B-roll track has no sound)" if to["kind"] == "broll" and t["kind"] == "video" else ""
    return f"Moved to “{to['name']}”{note}.", "", {"item": it["id"], "track": to["id"]}


def _item_to_scene(pid: str, data: dict, t: dict, it0: dict, body: dict) -> tuple[str, str, dict]:
    """A Video / B-roll block dragged DOWN onto Video 1: it becomes a scene of the main line again, before the scene
    it was dropped on (`before` = scene id or "end"). A block lifted from Video 1 restores THAT scene – its captions,
    cuts, card, speed come back – trimmed the way the block was; any other footage clip becomes a new scene."""
    if (data.get("ui_locks") or {}).get("main"):
        return "", "Video 1 is locked – click 🔒 on its header to unlock it first.", {}
    if t["kind"] not in ("video", "broll"):
        return "", "Only picture blocks (Video / B-roll tracks) can go into Video 1.", {}
    src = it0.get("src") or {}
    if src.get("t") != "clip" or not core.source_by_file(data, src.get("n")):
        return "", "Only your footage clips can be scenes in Video 1 – this is an upload / picture (add it in Footage first).", {}
    s_meta = core.source_by_file(data, src["n"])
    if s_meta.get("include") is False:
        return "", "That clip is set to Skip in Footage – include it first.", {}
    before = body.get("before") or "end"
    if before != "end" and not core.scene_by_id(data, before):
        return "", "That scene no longer exists – the page will refresh.", {}
    clip_len = float(s_meta.get("duration") or it0.get("src_len") or 0)
    a = max(0.0, float(it0.get("in") or 0))
    b = a + max(0.3, float(it0.get("dur") or 0.3))
    if clip_len > 0:
        b = min(b, clip_len)
    if b - a < 0.3:
        return "", "Too short to be a scene (under 0.3 s).", {}
    sc = core.scene_by_id(data, it0.get("from_scene") or "")
    restored = bool(sc and sc["src"] == src["n"] and not sc.get("keep", True))
    if not restored:  # lifted before blocks remembered their scene: the cut scene with the same clip + start is it
        sc = next((s for s in data["scenes"] if not s.get("keep", True) and s.get("role") != "broll"
                   and s["src"] == src["n"] and abs(float(s["in"]) - float(it0.get("in") or 0)) < 0.05), None)
        restored = sc is not None
    if restored:
        sc["keep"] = True
        sc["in"], sc["out"] = round(a, 3), round(b, 3)
        data["scenes"].remove(sc)
    else:
        sc = {"id": core.new_id("sc"), "src": src["n"], "in": round(a, 3), "out": round(b, 3),
              "orig_in": round(a, 3), "orig_out": round(b, 3), "keep": True}
        if t["kind"] == "broll":
            sc["mute"] = True  # it was silent on the B-roll track – keep what the user heard
        elif float(it0.get("vol", 1.0) if it0.get("vol") is not None else 1.0) != 1.0:
            sc["volume"] = round(float(it0["vol"]), 2)
    idx = len(data["scenes"])
    if before != "end":
        idx = data["scenes"].index(core.scene_by_id(data, before))
    data["scenes"].insert(idx, sc)
    t["items"].remove(it0)
    # blocks connected to the lifted scene's stand-in stay where they are; nothing else moves
    no = data["scenes"].index(sc) + 1
    return (f"Back in Video 1 as scene {no}" + (" – with its captions, cuts and settings" if restored else
            " – a new scene from that part of the clip") + ". Undo reverts it."), "", \
        {"scene": sc["id"], "new_scene": not restored}


def _in_video(data: dict, sc: dict) -> bool:
    skipped = {s["file"] for s in data["sources"] if s.get("include") is False}
    return sc.get("keep", True) and sc.get("role") != "broll" and sc["src"] not in skipped


def _scene_to_track(pid: str, data: dict, t: dict, body: dict) -> tuple[str, str, dict]:
    """A scene dragged from the Video lane UP onto a Video / B-roll / Audio track: it leaves the main line (the video
    closes the gap; the scene stays on the timeline as a cut block – Restore brings it back) and its clip plays on the
    track where it was dropped. `t` = drop time on the timeline as the page showed it (the scene still in place)."""
    import pipeline
    if t["kind"] == "text":
        return "", "Scenes can go on a Video, B-roll or Audio track – not on a Text track.", {}
    sc = core.scene_by_id(data, str(body.get("scene") or ""))
    if not sc:
        return "", "That scene no longer exists – the page will refresh.", {}
    if not _in_video(data, sc):
        return "", "Only a scene that is in the video can be moved to a track (restore it first).", {}
    vid = [s for s in data["scenes"] if _in_video(data, s)]
    if len(vid) < 2:
        return "", "The video needs at least one scene in its main line.", {}
    st = data["settings"]
    lens = {s["id"]: pipeline.scene_out_len(s, pipeline.load_silences(pid, s["src"]), st) for s in vid}
    start, acc = {}, 0.0
    for s in vid:
        start[s["id"]] = acc
        acc += lens[s["id"]]
    drop = max(0.0, float(body.get("t") or 0))
    S, L = start[sc["id"]], lens[sc["id"]]
    t2 = drop if drop <= S else max(S, drop - L)  # the main line closes the gap: later times move left by L
    rest, acc = [s for s in vid if s is not sc], 0.0
    anchor, off = rest[-1], 0.0
    for s in rest:
        ln = lens[s["id"]]
        if t2 < acc + ln - 1e-6:
            anchor, off = s, t2 - acc
            break
        acc += ln
    else:
        off = t2 - (acc - lens[rest[-1]["id"]])
    item = {"anchor": anchor["id"], "off": round(off, 3), "src": {"t": "clip", "n": sc["src"]}, "in": sc["in"],
            "dur": round(sc["out"] - sc["in"], 3), "where": "full", "from_scene": sc["id"]}
    vol = 0.0 if sc.get("mute") else float(sc.get("volume", 1.0) if sc.get("volume") is not None else 1.0)
    if t["kind"] in ("video", "audio"):
        item["vol"] = vol
    it, err = _clean_item(pid, data, t["kind"], item)
    if err:
        return "", err, {}
    if len(t["items"]) >= MAX_ITEMS:
        return "", f"At most {MAX_ITEMS} items per track.", {}
    # items connected to this scene ride along with it (like connected clips): re-connect them at the new spot
    for tk in tracks_of(data):
        for x in tk.get("items") or []:
            if x.get("anchor") == sc["id"]:
                x["anchor"], x["off"] = it["anchor"], round(it["off"] + float(x.get("off") or 0), 3)
    t["items"].append(it)
    sc["keep"] = False
    no = data["scenes"].index(sc) + 1
    lost = [w for w, c in (("its captions", (sc.get("transcript") or "").strip() and not sc.get("no_speech")),
                           ("its jump cuts", sc.get("remove_silence") or sc.get("cuts")),
                           ("its speed", float(sc.get("speed") or 1) != 1.0)) if c]
    what = {"video": "its picture and sound", "broll": "its picture (silent)", "audio": "its sound"}[t["kind"]]
    return (f"Scene {no} moved to “{t['name']}”: {what} plays there; the main video closed the gap"
            + (f" ({', '.join(lost)} stay with the scene, not the track)" if lost else "")
            + ". Restore the scene or Undo to bring it back."), "", {"item": it["id"], "track": t["id"]}


# ----------------------------------------------------------------- render plan
def plan(pid: str, data: dict, scenes: list[dict], durs: list[float]) -> tuple[list[list[dict]], list[dict]]:
    """Where every track item lands in THIS render (scenes = the rendered ones, durs = their exact lengths).
    Returns (per-scene picture slices, whole-timeline sounds). Items of hidden/muted tracks, items whose scene is not
    in the video, and missing files are left out."""
    starts, t = {}, 0.0
    for sc, d in zip(scenes, durs):
        starts[sc["id"]] = t
        t += d
    total = t
    edges = []
    acc = 0.0
    for d in durs:
        edges.append((acc, acc + d))
        acc += d
    slices: list[list[dict]] = [[] for _ in scenes]
    sounds: list[dict] = []
    for layer, tk in enumerate(tracks_of(data)):
        if tk.get("off") or tk.get("kind") not in KINDS:
            continue
        for it in tk.get("items") or []:
            if not isinstance(it, dict) or it.get("anchor") not in starts:
                continue
            A = starts[it["anchor"]] + float(it.get("off") or 0)
            B = min(total, A + float(it.get("dur") or 0))
            if B - A < 0.05 or A >= total:
                continue
            kind = tk["kind"]
            p = None if kind == "text" else media_path(pid, data, it.get("src"))
            if kind != "text" and (not p or not library.media_ok(p)):
                continue
            if kind in ("audio", "video") and p is not None and not (kind == "video" and tk.get("mute")):
                if kind == "audio" or (it.get("has_audio", True) and float(it.get("vol", 1.0) or 0) > 0.001):
                    sounds.append({"path": p, "start": A, "dur": B - A, "in": float(it.get("in") or 0),
                                   "vol": float(it.get("vol", 1.0) if it.get("vol") is not None else 1.0),
                                   "fade_in": float(it.get("fade_in") or 0), "fade_out": float(it.get("fade_out") or 0)})
            if kind == "audio":
                continue
            st = p.stat() if p else None
            for i, (s0, s1) in enumerate(edges):
                a, b = max(A, s0), min(B, s1)
                if b - a < 0.02:
                    continue
                sl = {"kind": kind, "layer": layer, "a": round(a - s0, 4), "b": round(b - s0, 4)}
                if kind == "text":
                    sl.update(text=it.get("text", ""), style=it.get("style", "title"))
                else:
                    sl.update(file=str(p), sig=[st.st_size, st.st_mtime], still=core.is_still(p.name),
                              src_in=round(float(it.get("in") or 0) + (a - A), 4), where=it.get("where", "full"),
                              fit=it.get("fit", "fill"), hdr=False)
                slices[i].append(sl)
    return slices, sounds
