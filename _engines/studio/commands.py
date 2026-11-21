"""Instant local commands. Anything not understood here is queued for Claude.

Scene commands (typed in a scene's box):
  cut | delete | remove        keep | restore
  trim start 1.5 | trim end 2  split at 3.2
  speed 1.5 | slow mo          mute | unmute | volume 50%
  zoom 1.2 | zoom in | no zoom captions on | captions off
  remove silences | keep silences
  text: Your title here        text top|center|bottom | remove text
Global commands (the 💬 box with no scene named; suggest.py hands them here):
  format 9:16 | 16:9 | 1:1 | original     fit crop | fit pad
  captions on|off   captions upper|normal  captions top|center|bottom
  highlight #FF0000 caption color #FFFFFF  words 3
  remove silences | keep silences          logo <file in Brand Kit> | no logo
  logo top-left|top-right|bottom-left|bottom-right
  speed 1.1 | mute | unmute | zoom 1.1      (applied to every scene)
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import core
import library

HEX = r"#?([0-9a-fA-F]{6})"


def _num(s: str) -> float:
    return float(s.replace(",", "."))


# ----------------------------------------------------------------- B-roll (cutaways over the voice)
# A scene with role "broll" is not part of the main sequence: its clip is footage to cover OTHER scenes with.
# A speaker scene's cutaway: sc["broll"] = {"src": clip file or Assets file, "where": "top"|"bottom"|"full",
# "at": seconds (optional)}. Split layout: top = the card panel, bottom = over YOU (card stays). Full layout: top /
# bottom = that half of the screen (you in the other half).
# The voice keeps playing, the B-roll is silent and never adds length. Without "at", consecutive scenes covered by
# the same clip continue its footage (pipeline.with_broll works that out at render time – survives splits/trims).

def is_broll(sc: dict) -> bool:
    return sc.get("role") == "broll"


def broll_clips(data: dict) -> list[str]:
    """Clip files whose scenes are ALL B-roll (in clip order)."""
    out = []
    for s in data.get("sources", []):
        mine = [x for x in data.get("scenes", []) if x["src"] == s["file"]]
        if mine and all(is_broll(x) for x in mine):
            out.append(s["file"])
    return out


def _sc_len(sc: dict) -> float:
    return max(0.0, (sc["out"] - sc["in"]) / float(sc.get("speed") or 1.0))


def auto_broll(data: dict, only: str | None = None) -> list[str]:
    """Place every B-roll clip that no scene uses yet as a cutaway over the speaker scenes where the clip sits in the
    timeline (after it; at the end of the video: the scenes before it), covering about the clip's length (split
    layout, top panel) or at most 6 s (full screen hides the speaker – short cutaways read better)."""
    replies = []
    scenes = data["scenes"]
    split = data["settings"].get("layout") == "split"
    skipped = {s["file"] for s in data["sources"] if s.get("include") is False}
    used = {s["broll"].get("src") for s in scenes if isinstance(s.get("broll"), dict)}
    for f in ([only] if only else broll_clips(data)):
        if f in used or f in skipped:
            continue
        src = core.source_by_file(data, f) or {}
        mine = [x for x in scenes if x["src"] == f]
        if not mine:
            continue
        clip_len = float(src.get("duration") or sum(x["out"] - x["in"] for x in mine))
        cap = clip_len if split else min(clip_len, 6.0)
        pos = scenes.index(mine[0])
        ok = lambda x: not is_broll(x) and x["src"] not in skipped  # noqa: E731
        cands = [x for x in scenes[pos:] if ok(x)] or [x for x in reversed(scenes[:pos]) if ok(x)]
        chosen, acc = [], 0.0
        for x in cands:
            if not x.get("keep", True):
                continue  # a cut scene isn't in the video: look past it
            if x.get("broll"):
                break  # never overwrite the user's own cutaway; coverage stays one contiguous run
            if chosen and acc + _sc_len(x) > cap + 0.5:
                break
            chosen.append(x)
            acc += _sc_len(x)
        if not chosen:
            continue
        for x in chosen:
            x["broll"] = {"src": f}  # no "where": follows the look (split -> top panel, full layout -> full screen)
        nos = sorted(scenes.index(x) + 1 for x in chosen)
        where = "in the top panel" if split else "full screen"
        replies.append(f'B-roll "{f}" covers scene{"s" if len(nos) > 1 else ""} '
                       f'{nos[0]}{f"–{nos[-1]}" if len(nos) > 1 else ""} {where} – your voice keeps playing.')
        used.add(f)
    return replies


def _match_clip(data: dict, name: str) -> str | None:
    name = name.strip().strip("\"'")
    files = [s["file"] for s in data["sources"]]
    m = re.fullmatch(r"clip (\d+)", name, flags=re.I)
    if m:
        i = int(m.group(1)) - 1
        return files[i] if 0 <= i < len(files) else None
    for f in files:
        if f.lower() == name.lower():
            return f
    hits = [f for f in files if name.lower() in f.lower()]
    if len(hits) == 1:
        return hits[0]
    if data.get("id") and (core.raw_dir(data["id"]) / "Assets" / Path(name).name).is_file():
        return Path(name).name  # a file in Raw Footage/<project>/Assets (storyboard B-roll slots)
    return None


def _broll_scene_cmd(data: dict, sc: dict, raw: str, t: str) -> str | None:
    t = re.sub(r"\bb[- ]?roll\b", "broll", t)
    raw_n = re.sub(r"(?i)\bb[- ]?roll\b", "broll", raw)
    split = data["settings"].get("layout") == "split"
    if re.fullmatch(r"(?:role|make it|make this|set as|mark as|as|this is) (?:broll|cutaway)(?: clip| footage| scene)?", t):
        if is_broll(sc):
            return "This scene is already B-roll."
        sc["role"] = "broll"
        sc.pop("broll", None)  # B-roll footage can't itself be covered
        msg = "Scene is now B-roll: it leaves the main video and can cover other scenes (your voice keeps playing)."
        if (sc.get("transcript") or "").strip() and not sc.get("no_speech"):
            msg += " ⚠ It had speech – those words are no longer in the video (role speaker brings it back)."
        if sc["src"] in broll_clips(data):
            placed = auto_broll(data, only=sc["src"])
            msg += " " + (" ".join(placed) if placed else "Pick a speaker scene and choose this clip under B-roll.")
        return msg
    if re.fullmatch(r"(?:role|make it|make this|set as|mark as|as|this is) (?:speaker|main|talking|a-?roll|normal)(?: scene| clip)?", t):
        if not is_broll(sc):
            return "This scene is already a speaker scene."
        sc.pop("role", None)
        msg = "Scene is back in the main video."
        if sc["src"] not in broll_clips(data):
            n = 0
            for x in data["scenes"]:
                if isinstance(x.get("broll"), dict) and x["broll"].get("src") == sc["src"]:
                    x.pop("broll")
                    n += 1
            if n:
                msg += f" Its clip no longer covers {n} scene(s) (the same footage would show twice)."
        return msg
    if not t.startswith(("broll", "cutaway", "no broll", "remove broll")):
        return None
    if t in ("broll off", "no broll", "remove broll", "broll none", "broll remove", "cutaway off"):
        if not sc.get("broll"):
            return "! This scene has no B-roll."
        sc.pop("broll")
        return "B-roll removed from this scene."
    if t == "broll auto":
        return None  # handled as a whole-video command by apply()
    if is_broll(sc):
        return "! This scene IS B-roll footage – select a speaker scene to cover it with B-roll."
    m = re.fullmatch(BROLL_FIT_RE, t)
    if m:
        if not sc.get("broll"):
            return "! No B-roll on this scene yet – first: broll <clip name>"
        fit = _fit_of(m.group(1))
        sc["broll"] = {k: v for k, v in sc["broll"].items() if k != "fit"} | ({"fit": "fit"} if fit == "fit" else {})
        return f"B-roll {_fit_txt(fit)}."
    m = re.fullmatch(BROLL_WHERE_RE, t)
    if m:
        if not sc.get("broll"):
            return "! No B-roll on this scene yet – first: broll <clip name>"
        where = _where_of(m.group(1))
        sc["broll"] = {k: v for k, v in sc["broll"].items() if k != "where"} | ({"where": where} if where else {})
        return f"B-roll {_where_txt(where, split)}."
    m = re.fullmatch(r"broll (?:from|at|starts? at|start) ([\d.,]+)\s*(?:s|sec|seconds)?", t)
    if m:
        if not sc.get("broll"):
            return "! No B-roll on this scene yet – first: broll <clip name>"
        sc["broll"] = dict(sc["broll"], at=round(_num(m.group(1)), 2))
        return f"B-roll starts {sc['broll']['at']:g}s into its clip."
    if re.fullmatch(r"broll (?:continue|auto start|follow)", t):
        if not sc.get("broll"):
            return "! No B-roll on this scene yet."
        sc["broll"].pop("at", None)
        return "B-roll continues from the previous scene's footage."
    m = re.fullmatch(r"(?:broll|cutaway)(?:\s+(.+))?", raw_n.strip(), flags=re.I | re.S)
    if not m:
        return None
    name = (m.group(1) or "").strip()
    if name:
        f = _match_clip(data, name)
        if not f:
            return f'! No clip called "{name}". B-roll clips: {", ".join(broll_clips(data)) or "none yet"}'
    else:
        cl = broll_clips(data)
        if not cl:
            return "! No B-roll clip yet – select a scene of the B-roll clip and set Role: B-roll first."
        f = cl[0]
    if f == sc["src"]:
        return "! A scene can't use its own clip as B-roll."
    old = sc.get("broll") if isinstance(sc.get("broll"), dict) else {}
    sc["broll"] = {"src": f, **({"where": old["where"]} if old.get("where") else {}),  # explicit choices kept
                   **({"fit": "fit"} if old.get("fit") == "fit" else {})}
    return f'B-roll "{f}" over this scene ({_where_txt(core.broll_where(sc["broll"], split), split)}) – your voice keeps playing.'


BROLL_WHERE_RE = (r"broll (top|panel|top panel|top half|upper half|upper|bottom|bottom half|lower half|lower|"
                  r"bottom panel|over me|full|fullscreen|full screen|full-screen|auto|layout)")


BROLL_FIT_RE = (r"broll (fit|whole|whole picture|show all|no crop|bars|contain|letterbox|"
                r"fill|crop|cover|zoom to fill|fill screen)")


def _fit_of(w: str) -> str:
    return "fit" if w in ("fit", "whole", "whole picture", "show all", "no crop", "bars", "contain", "letterbox") else "fill"


def _fit_txt(fit: str) -> str:
    return ("shows the WHOLE picture (nothing cut off; the empty space is a blurred copy of it)" if fit == "fit"
            else "fills its space (edges cropped if the shape differs)")


def _where_of(w: str) -> str | None:
    w = w.strip()
    if w.startswith("full"):
        return "full"
    if w.startswith(("bottom", "lower")) or w == "over me":
        return "bottom"
    if w in ("auto", "layout"):
        return None
    return "top"


def _where_txt(where: str | None, split: bool) -> str:
    if where is None:
        return "follows the layout (" + ("top panel" if split else "full screen") + ")"
    if where == "full":
        return "full screen (hides you, your voice continues)"
    if split:
        return "in the top panel (card area)" if where == "top" else "over YOU in the bottom panel (the card stays on top)"
    return "in the top half (you in the bottom half)" if where == "top" else "in the bottom half (you in the top half)"


def _broll_global_cmd(data: dict, t: str) -> str | None:
    t = re.sub(r"\bb[- ]?roll\b", "broll", t)
    if t in ("broll auto", "auto broll", "place broll", "broll place"):
        r = auto_broll(data)
        return " ".join(r) if r else "! Nothing to place – every B-roll clip is already used, or there is none " \
                                      "(set a clip's scene to Role: B-roll first)."
    if t in ("broll off", "no broll", "remove broll", "remove all broll", "broll none"):
        n = 0
        for s in data["scenes"]:
            if s.pop("broll", None):
                n += 1
        return f"B-roll removed from {n} scene(s)." if n else "! No scene has B-roll."
    m = re.fullmatch(BROLL_FIT_RE, t)
    if m:
        fit = _fit_of(m.group(1))
        n = 0
        for s in data["scenes"]:
            if isinstance(s.get("broll"), dict):
                s["broll"] = {k: v for k, v in s["broll"].items() if k != "fit"} | ({"fit": "fit"} if fit == "fit" else {})
                n += 1
        return f"All B-roll ({n} scene(s)) {_fit_txt(fit)}." if n else "! No scene has B-roll."
    m = re.fullmatch(BROLL_WHERE_RE, t)
    if m:
        where = _where_of(m.group(1))
        n = 0
        for s in data["scenes"]:
            if isinstance(s.get("broll"), dict):
                s["broll"] = {k: v for k, v in s["broll"].items() if k != "where"} | ({"where": where} if where else {})
                n += 1
        split = data["settings"].get("layout") == "split"
        return f"All B-roll ({n} scene(s)) {_where_txt(where, split)}." if n else "! No scene has B-roll."
    return None


def _scene_cmd(sc: dict, t: str, data: dict) -> str | None:
    """Apply one scene-level command. Returns reply text, or None if not understood."""
    if re.fullmatch(r"(cut|delete|remove|drop)( (this|this scene|scene|it))?", t):
        sc["keep"] = False
        return "Scene cut."
    if re.fullmatch(r"(keep|restore|undo cut|uncut|bring back)( (this|this scene|scene|it))?", t):
        sc["keep"] = True
        return "Scene restored."
    # absolute trim (Inspector / timeline handles): "trim start to 0.4" = start 0.4 s after the scene's original start
    m = re.fullmatch(r"trim (start|end) to ([\d.,]+)\s*(?:s|sec|secs|seconds)?", t)
    if m:
        x = _num(m.group(2))
        o_in, o_out = sc.get("orig_in", sc["in"]), sc.get("orig_out", sc["out"])
        if m.group(1) == "start":
            new_in = round(o_in + x, 3)
            if sc["out"] - new_in < 0.3:
                return "!Trim too large – the scene would be shorter than 0.3 s."
            sc["in"] = new_in
        else:
            new_out = round(o_out - x, 3)
            if new_out - sc["in"] < 0.3:
                return "!Trim too large – the scene would be shorter than 0.3 s."
            sc["out"] = new_out
        return f"{'Start' if m.group(1) == 'start' else 'End'} trimmed by {x:g}s." if x else \
            f"{'Start' if m.group(1) == 'start' else 'End'} back to the original."
    m = re.fullmatch(r"trim (start|begin|beginning|front|head|end|back|tail) (?:by )?([\d.,]+)\s*(?:s|sec|secs|seconds)?", t)
    if m:
        x = _num(m.group(2))
        if m.group(1) in ("start", "begin", "beginning", "front", "head"):
            if sc["out"] - (sc["in"] + x) < 0.3:
                return "!Trim too large – the scene would be shorter than 0.3 s."
            sc["in"] = round(sc["in"] + x, 3)
        else:
            if (sc["out"] - x) - sc["in"] < 0.3:
                return "!Trim too large – the scene would be shorter than 0.3 s."
            sc["out"] = round(sc["out"] - x, 3)
        return f"Trimmed {x:g}s from the {'start' if m.group(1) in ('start','begin','beginning','front','head') else 'end'}."
    if t in ("reset trim", "untrim", "undo trim", "full length", "restore length"):
        sc["in"], sc["out"] = sc.get("orig_in", sc["in"]), sc.get("orig_out", sc["out"])
        return "Trim removed – scene back to its full length."
    m = re.fullmatch(r"(?:speed|playback) (?:to )?([\d.,]+)\s*x?", t) or re.fullmatch(r"([\d.,]+)\s*x( speed)?", t)
    if m:
        x = _num(m.group(1))
        if not 0.25 <= x <= 4:
            return "!Speed must be between 0.25x and 4x."
        sc["speed"] = x
        return f"Speed set to {x:g}x."
    if t in ("slow mo", "slowmo", "slow motion", "slow-mo"):
        sc["speed"] = 0.5
        return "Slow motion (0.5x)."
    if t in ("normal speed", "reset speed"):
        sc["speed"] = 1.0
        return "Speed reset to 1x."
    if t in ("mute", "mute audio", "no audio", "silence"):
        sc["mute"] = True
        return "Muted."
    if t in ("unmute", "audio on", "sound on"):
        sc["mute"] = False
        return "Unmuted."
    m = re.fullmatch(r"(?:volume|vol) ([\d.,]+)\s*(%?)", t)
    if m:
        x = _num(m.group(1))
        # "volume 3%" is 3 % (was read as 3x = 300 %); without % a number above 4 is still a percentage
        x = x / 100 if (m.group(2) or x > 4) else x
        sc["volume"] = round(max(0.0, min(x, 4.0)), 3)
        sc["mute"] = False
        return f"Volume {sc['volume'] * 100:.0f}%."
    m = re.fullmatch(r"zoom (?:to )?([\d.,]+)\s*x?", t)
    if m:
        x = _num(m.group(1))
        if not 1.0 <= x <= 3.0:
            return "!Zoom must be between 1 and 3."
        sc["zoom"] = x
        return f"Zoom {x:g}x."
    # zoom moments (punch-ins inside the scene; times in seconds from the scene start, like "split at")
    if t in ("punch off", "no punch", "zoom moments off", "remove punches", "punch clear"):
        n = len(sc.pop("punches", None) or [])
        return f"Removed {n} zoom moment(s)." if n else "No zoom moments in this scene."
    m = re.fullmatch(r"punch remove (\d+)", t)
    if m:
        ps = sorted(sc.get("punches") or [])
        k = int(m.group(1)) - 1
        if not 0 <= k < len(ps):
            return f"!There is no zoom moment {k + 1} (this scene has {len(ps)})."
        ps.pop(k)
        if ps:
            sc["punches"] = ps
        else:
            sc.pop("punches", None)
        return f"Zoom moment {k + 1} removed."
    m = re.fullmatch(r"(?:punch|zoom moment)(?: in)? (?:at |from )?([\d.,]+)\s*(?:-|to)\s*([\d.,]+)(?:\s*s)?(?: (?:zoom |x)?([\d.,]+)x?)?", t)
    if m:
        a, b = _num(m.group(1)), _num(m.group(2))
        z = _num(m.group(3)) if m.group(3) else 1.15
        if b - a < 0.2:
            return "!A zoom moment needs at least 0.2 s (e.g. punch 1.2-2.5)."
        if not 1.02 <= z <= 2.0:
            return "!Zoom strength must be between 1.02 and 2 (e.g. punch 1.2-2.5 1.2)."
        s0, e0 = round(sc["in"] + a, 3), round(min(sc["out"], sc["in"] + b), 3)
        if s0 >= sc["out"] - 0.1:
            return f"!The scene is only {sc['out'] - sc['in']:.1f}s long."
        sc["punches"] = sorted([p for p in (sc.get("punches") or []) if p[1] <= s0 or p[0] >= e0] + [[s0, e0, z]])
        return f"Zoom moment {a:g}–{min(b, sc['out'] - sc['in']):.1f}s at {z:g}× (the scene stays one scene)."
    if t in ("zoom in", "punch in", "punch-in"):
        sc["zoom"] = 1.2
        return "Punch-in zoom 1.2x."
    if t in ("no zoom", "zoom out", "reset zoom", "zoom off"):
        sc["zoom"] = 1.0
        return "Zoom removed."
    if t in ("captions on", "subtitles on", "show captions"):
        was_auto = sc.pop("captions_auto_off", False)  # an explicit choice replaces the automatic one
        sc["captions"] = True
        return "Captions on for this scene." + (" (It had no detected speech – check the words make sense.)" if was_auto else "")
    if t in ("captions off", "subtitles off", "hide captions", "no captions"):
        sc.pop("captions_auto_off", None)
        sc["captions"] = False
        return "Captions off for this scene."
    # exact height: "captions at 65%" = the caption line's centre 65 % down from the top of the screen
    m = re.fullmatch(r"(?:captions?|subtitles?) (?:at|position|pos|place|height|y) ([\d.,]+)\s*%?", t)
    if m:
        x = round(_num(m.group(1)), 1)
        if not 5 <= x <= 95:
            return "!Caption height must be between 5 and 95 (% from the top of the screen)."
        sc["cap_pos"] = x
        st = data["settings"] if data else {}
        seam = float(st.get("split_ratio", 0.44) or 0.44) * 100 if st.get("layout") == "split" else None
        warn = ""
        if x > 80:
            warn = " ⚠ Below 80 % Instagram/YouTube put their caption text and buttons over it – 70-78 % is safer."
        elif seam and x < seam - 2:
            warn = f" ⚠ That is on the top card (the card area ends at {seam:.0f} %)."
        elif x < 10:
            warn = " ⚠ The very top is covered by the app's header on Reels/Shorts."
        return f"Captions at {x:g} % from the top in this scene.{warn}"
    m = re.fullmatch(r"(?:captions?|subtitles?) (?:at|position|pos|place) (head|top|center|centre|middle|below|bottom|auto|default)", t)
    if m:
        v = {"top": "head", "centre": "center", "middle": "center", "bottom": "below", "default": "auto"}.get(m.group(1), m.group(1))
        if v == "auto":
            sc.pop("cap_pos", None)
            return "Caption position follows the look (split layout: on the seam above your head)."
        sc["cap_pos"] = v
        return {"head": "Captions near the head (top) in this scene.", "center": "Captions in the middle in this scene.",
                "below": "Captions low (bottom) in this scene."}[v]
    if t in ("captions auto", "captions default", "subtitles auto"):
        sc.pop("captions_auto_off", None)
        if sc.get("no_speech"):
            sc["captions"], sc["captions_auto_off"] = False, True
            return "Captions follow the video – off here (no speech detected)."
        sc["captions"] = None
        return "Captions follow the whole-video setting."
    if t in ("silences auto", "jump cuts auto", "silence auto", "silences default"):
        sc["remove_silence"] = None
        return "Pause cutting follows the whole-video setting."
    if t in ("remove silences", "remove silence", "cut silences", "cut silence", "jump cut", "jump cuts"):
        sc["remove_silence"] = True
        return "Silences will be cut from this scene."
    if t in ("keep silences", "keep silence", "no jump cuts"):
        sc["remove_silence"] = False
        return "Silences kept in this scene."
    if t in ("remove text", "no text", "text off", "delete text"):
        sc["text"] = ""
        return "Text removed."
    m = re.fullmatch(r"text (?:position )?(top|center|centre|middle|bottom)", t)
    if m:
        sc["text_pos"] = {"centre": "center", "middle": "center"}.get(m.group(1), m.group(1))
        return f"Text moved to {sc['text_pos']}."
    return None


def _split(data: dict, sc: dict, at: float) -> str:
    dur = sc["out"] - sc["in"]
    if not 0.3 <= at <= dur - 0.3:
        return f"!Split point must be between 0.3 and {dur - 0.3:.1f} s inside this scene."
    cut = round(sc["in"] + at, 3)
    new = copy.deepcopy(sc)
    new["id"] = core.new_id("sc")
    new["in"] = cut
    new["orig_in"] = cut
    sc["out"] = cut
    sc["orig_out"] = cut  # a split is not a trim – keep the "trimmed" badge honest
    # thumbnail + transcripts are refreshed by the caller (needs pipeline)
    idx = data["scenes"].index(sc)
    data["scenes"].insert(idx + 1, new)
    return f"Split at {at:.2f}s → new scene {idx + 2}."


MAX_SCENES = 300


def _duplicate(data: dict, src: dict, after: dict) -> str:
    """Copy a scene (with ALL its edits: trims, speed, zoom moments, captions, card, text, B-roll cover, volume…)
    and put the copy right after `after`. The copy is always IN the video: a cut scene comes back kept, a B-roll
    scene becomes a normal scene with its own sound (a copied cutaway stub would do nothing)."""
    if len(data["scenes"]) >= MAX_SCENES:
        return f"!Too many scenes ({MAX_SCENES}) – cut or remove some first."
    new = copy.deepcopy(src)
    new["id"] = core.new_id("sc")
    notes = []
    if not new.get("keep", True):
        new["keep"] = True
        notes.append("it was cut – the copy is in the video")
    if new.get("role") == "broll":
        new.pop("role", None)
        notes.append("the copy plays as its own scene, with its own sound")
    new.pop("thumb", None)  # made by the caller for the new id
    n = data["scenes"].index(src) + 1  # BEFORE the insert (a copy pasted in front of it would shift its number)
    idx = data["scenes"].index(after) + 1
    data["scenes"].insert(idx, new)
    note = f" ({'; '.join(notes)})" if notes else ""
    return f"Copy of scene {n} added as scene {idx + 1}{note} – {len(data['scenes'])} scenes now. Drag it anywhere."


# ----------------------------------------------------------------- library: transitions, effects, stickers, sounds
# scene["transition"] = {"type": xfade id, "dur": s, "sound": {file, sync, gain_db, name} | None}  (INTO this scene)
# scene["effects"]    = [effect id, ...]
# scene["stickers"]   = [{"file", "name", "at", "dur", "pos", "size", "anim"}]   (times = seconds into the scene)
# scene["sfx"]        = [{"file", "name", "at", "vol", "sync", "gain_db"}]      (at = the moment the hit should land)
# Everything the render needs is stored in the scene (file + sync + level), so the render never needs the catalog.
MAX_STICKERS, MAX_SFX, MAX_EFFECTS = 8, 12, 4
_TIME = r"([\d.,]+)\s*(?:s|sec|seconds)?"


def _trans_id(name: str) -> str | None:
    k = library.slug(name).replace("_", "")
    for tid, (nm, _g, _s) in library.TRANSITIONS.items():
        if k in (tid, library.slug(nm).replace("_", "")):
            return tid
    return None


_FX_ALIAS = {"blackwhite": "bw", "bnw": "bw", "greyscale": "bw", "grayscale": "bw", "monochrome": "bw",
             "kenburns": "slowzoom", "zoom": "punchin", "punch": "punchin", "whiteflash": "flash", "fade": "fadein",
             "negative": "invertflash", "invert": "invertflash", "film": "vintage", "old": "vintage", "retro": "vintage",
             "colour": "punchy", "color": "punchy", "vibrant": "punchy", "chromatic": "rgbsplit",
             "zoomin": "punchin", "pushin": "slowzoom", "zoomslow": "slowzoom"}


def _effect_id(name: str) -> str | None:
    """An effect by id or name, forgiving: 'black and white', 'B&W', 'punch in', 'ken burns'…"""
    k = library.slug(name.replace("&", " ")).replace("_", "").replace("and", "")
    k = _FX_ALIAS.get(k, k)
    for e, (nm, _g, _d) in library.EFFECTS.items():
        if k in (e, library.slug(nm.replace("&", " ")).replace("_", "").replace("and", "")):
            return e
    return None


def _sound_ref(it: dict) -> dict:
    return {"file": it["file"], "name": it["name"], "sync": round(float(it.get("sync") or 0), 3),
            "gain_db": float(it.get("gain_db") or 0)}


def _opts(rest: str, full: bool = True) -> tuple[dict, str]:
    """Pull 'at 1.2', 'for 2', 'vol 80%', 'size 25%', a position and an animation out of a command tail.
    full=False (sounds): only at / vol – a sound id like "pop-soft" or "whoosh-left" must stay whole."""
    o = {}
    pats = [("at", r"\bat " + _TIME), ("dur", r"\bfor " + _TIME), ("vol", r"\b(?:vol|volume) ([\d.,]+)\s*%?"),
            ("size", r"\bsize ([\d.,]+)\s*%?")]
    for key, pat in pats:
        m = re.search(pat, rest)
        if m:
            o[key] = _num(m.group(1))
            rest = rest[:m.start()] + rest[m.end():]
    if not full:
        return o, re.sub(r"\s+", " ", rest).strip()
    m = re.search(r"\b(top-left|top-right|bottom-left|bottom-right|top left|top right|bottom left|bottom right|"
                  r"top|bottom|left|right|center|centre|middle)\b", rest)
    if m:
        o["pos"] = {"centre": "center", "middle": "center"}.get(m.group(1), m.group(1).replace(" ", "-"))
        rest = rest[:m.start()] + rest[m.end():]
    m = re.search(r"\b(pop|bounce|still|static)\b", rest)
    if m:
        o["anim"] = "still" if m.group(1) == "static" else m.group(1)
        rest = rest[:m.start()] + rest[m.end():]
    return o, re.sub(r"\s+", " ", rest).strip()


def _apply_sticker_opts(st: dict, o: dict) -> list[str]:
    done = []
    if "at" in o:
        st["at"] = round(max(0.0, o["at"]), 2)
        done.append(f"at {st['at']:g} s")
    if "dur" in o:
        st["dur"] = round(max(0.2, o["dur"]), 2)
        done.append(f"for {st['dur']:g} s")
    if "pos" in o:
        st["pos"] = o["pos"]
        done.append(o["pos"])
    if "size" in o:
        v = o["size"] / 100 if o["size"] > 1 else o["size"]
        st["size"] = round(min(0.9, max(0.05, v)), 3)
        done.append(f"size {round(st['size'] * 100)} %")
    if "anim" in o:
        st["anim"] = o["anim"]
        done.append(o["anim"])
    return done


def _library_scene_cmd(data: dict, sc: dict, t: str) -> str | None:
    # ---- transition INTO this scene
    if re.fullmatch(r"(?:transition|trans) (?:off|none|cut|remove|no)|no transition", t):
        if not sc.get("transition"):
            return "! This scene has no transition."
        sc.pop("transition")
        return "Transition removed – a straight cut into this scene."
    m = re.fullmatch(r"(?:transition|trans) sound (on|off)", t)
    if m:
        tr = sc.get("transition")
        if not tr:
            return "! No transition on this scene yet."
        if m.group(1) == "off":
            tr["sound"] = None
            return "Transition plays without a sound."
        it = library.find("sound", library.TRANSITIONS[tr["type"]][2] or "whoosh-fast-soft")
        if not it:
            return "! No whoosh sounds in the Library yet – run Library setup."
        tr["sound"] = _sound_ref(it)
        return f'Transition sound: "{it["name"]}".'
    m = re.fullmatch(r"(?:transition|trans) (.+?)(?: " + _TIME + r")?( silent| no sound| with sound)?", t)
    if m:
        tid = _trans_id(m.group(1))
        if not tid:
            return f'! No transition called "{m.group(1)}". Try: fade, slide left, zoom in, wipe up, circle open…'
        dur = round(min(1.0, max(0.15, _num(m.group(2)))), 2) if m.group(2) else \
            (sc.get("transition") or {}).get("dur") or library.DEFAULT_TRANS_DUR
        old = sc.get("transition") or {}
        snd = None
        if m.group(3) != " silent" and m.group(3) != " no sound":
            keep_off = old and old.get("sound") is None and m.group(3) is None and old.get("type")
            want = library.TRANSITIONS[tid][2]
            if want and not keep_off:
                it = library.find("sound", want)
                snd = _sound_ref(it) if it else None
            elif m.group(3) == " with sound":
                it = library.find("sound", "whoosh-fast-soft")
                snd = _sound_ref(it) if it else None
        sc["transition"] = {"type": tid, "dur": dur, "sound": snd}
        vid = [x for x in data["scenes"] if x.get("keep", True) and x.get("role") != "broll"]
        first = " ⚠ This is the first scene – nothing comes before it, so no transition shows (use effect “fade in”)." \
            if vid and vid[0] is sc else ""
        return (f"Transition into this scene: {library.TRANSITIONS[tid][0]} ({dur:g} s)"
                f"{' + ' + snd['name'] if snd else ', no sound'}. Lengths don't change.{first}")
    # ---- effects
    if re.fullmatch(r"(?:effects?|fx) (?:off|none|clear|remove all)|no effects?", t):
        if not sc.get("effects"):
            return "! This scene has no effects."
        sc.pop("effects")
        return "Effects removed."
    m = re.fullmatch(r"(?:effects?|fx) (?:remove|off) (.+)|no (.+) effect", t)
    if m:
        name = _effect_id(m.group(1) or m.group(2)) or "?"
        eff = [e for e in sc.get("effects") or [] if e != name]
        if eff == (sc.get("effects") or []):
            return f'! No "{m.group(1) or m.group(2)}" effect on this scene.'
        sc["effects"] = eff
        if not eff:
            sc.pop("effects")
        return "Effect removed."
    m = re.fullmatch(r"(?:effects?|fx) (.+)", t)
    if m:
        eid = _effect_id(m.group(1))
        if not eid:
            return f'! No effect called "{m.group(1)}". Try: flash, shake, punch-in, glitch, black & white, vintage…'
        eff = list(sc.get("effects") or [])
        if eid in eff:
            return f"{library.EFFECTS[eid][0]} is already on this scene."
        if len(eff) >= MAX_EFFECTS:
            return f"! At most {MAX_EFFECTS} effects per scene – remove one first."
        clash = {"punchin", "slowzoom", "zoomout"}
        if eid in clash:
            eff = [e for e in eff if e not in clash]  # one camera zoom at a time
        sc["effects"] = eff + [eid]
        return f"Effect: {library.EFFECTS[eid][0]} – {library.EFFECTS[eid][2]}"
    # ---- stickers
    if re.fullmatch(r"stickers? (?:off|none|clear|remove all)|no stickers?", t):
        if not sc.get("stickers"):
            return "! This scene has no stickers."
        for st_ in sc.pop("stickers"):
            _drop_pair(sc, st_)
        return "Stickers removed (with their sounds)."
    m = re.fullmatch(r"sticker (?:remove|delete|off) (\d+)|sticker (\d+) (?:remove|delete|off)", t)
    if m:
        i = int(m.group(1) or m.group(2)) - 1
        lst = sc.get("stickers") or []
        if not 0 <= i < len(lst):
            return f"! No sticker {i + 1} on this scene."
        gone = lst.pop(i)
        if not lst:
            sc.pop("stickers")
        _drop_pair(sc, gone)
        return f'Sticker "{gone.get("name", "")}" removed.'
    m = re.fullmatch(r"sticker (\d+) (.+)", t)
    if m:
        i = int(m.group(1)) - 1
        lst = sc.get("stickers") or []
        if not 0 <= i < len(lst):
            return f"! No sticker {i + 1} on this scene."
        o, left = _opts(m.group(2))
        if left or not o:
            return "! Sticker settings: at 1.5 · for 2 · top-right / center / bottom … · size 25% · pop / bounce / still"
        old_at = float(lst[i].get("at") or 0)
        done = _apply_sticker_opts(lst[i], o)
        if "at" in o:  # its own sound moves with it
            for x in sc.get("sfx") or []:
                if isinstance(x, dict) and x.get("pair") and abs(float(x.get("at") or 0) - old_at) < 0.06:
                    x["at"] = lst[i]["at"]
        return f"Sticker {i + 1}: " + ", ".join(done) + "."
    m = re.fullmatch(r"sticker (.+?)( silent| no sound| without sound)?", t)
    if m:
        silent = bool(m.group(2))
        o, name = _opts(m.group(1))
        if re.fullmatch(r"\d+", name or ""):
            return f"! Say what to change, e.g. sticker {name} at 1.5 · sticker {name} top-left · sticker remove {name}."
        it = library.find("sticker", name) if name else None
        if not it:
            return f'! No sticker called "{name}" in the Library.' + ("" if library.catalog()["stickers"] else
                                                                       " (The Library is empty – run Library setup.)")
        lst = sc.setdefault("stickers", [])
        if len(lst) >= MAX_STICKERS:
            return f"! At most {MAX_STICKERS} stickers per scene."
        st = {"file": it["file"], "name": it["name"], "at": 0.3, "dur": 2.0, "pos": "top-right", "size": 0.22, "anim": "pop"}
        _apply_sticker_opts(st, o)
        lst.append(st)
        snd = "" if silent else _pair_sound(sc, it, st["at"])
        return f'Sticker "{it["name"]}" ({len(lst)}) at {st["at"]:g} s for {st["dur"]:g} s, {st["pos"]}{snd}.'
    # ---- sound effects
    if t in ("sound off", "audio off", "sound on", "audio on"):
        return _scene_cmd(sc, "mute" if t.endswith("off") else "unmute", data)
    if re.fullmatch(r"(?:sfx|sound effects?|sounds) (?:off|none|clear|remove all)|no (?:sfx|sound effects)", t):
        if not sc.get("sfx"):
            return "! This scene has no sound effects."
        sc.pop("sfx")
        return "Sound effects removed."
    m = re.fullmatch(r"(?:sfx|sound) (?:remove|delete|off) (\d+)|(?:sfx|sound) (\d+) (?:remove|delete|off)", t)
    if m:
        i = int(m.group(1) or m.group(2)) - 1
        lst = sc.get("sfx") or []
        if not 0 <= i < len(lst):
            return f"! No sound effect {i + 1} on this scene."
        gone = lst.pop(i)
        if not lst:
            sc.pop("sfx")
        return f'Sound "{gone.get("name", "")}" removed.'
    m = re.fullmatch(r"(?:sfx|sound) (\d+) (.+)", t)
    if m:
        i = int(m.group(1)) - 1
        lst = sc.get("sfx") or []
        if not 0 <= i < len(lst):
            return f"! No sound effect {i + 1} on this scene."
        o, left = _opts(m.group(2), full=False)
        if left or not ({"at", "vol"} & set(o)):
            return "! Sound settings: at 1.5 · vol 80%"
        if "at" in o:
            lst[i]["at"] = round(max(0.0, o["at"]), 2)
        if "vol" in o:
            lst[i]["vol"] = round(min(2.0, max(0.0, o["vol"] / 100 if o["vol"] > 2 else o["vol"])), 2)
        return f"Sound {i + 1}: at {lst[i]['at']:g} s, volume {round(lst[i].get('vol', 1) * 100)} %."
    m = re.fullmatch(r"(sfx|sound effect|sound) (.+)", t)
    if m:
        o, name = _opts(m.group(2), full=False)
        # bare "sound …" is also everyday speech ("sound louder") – only an exact library name counts there
        it = library.find("sound", name, exact=m.group(1) == "sound") if name else None
        if not it and m.group(1) == "sound":
            return None
        if not it:
            return f'! No sound called "{name}" in the Library.' + ("" if library.catalog()["sounds"] else
                                                                     " (The Library is empty – run Library setup.)")
        pair = [x for x in sc.get("sfx") or [] if isinstance(x, dict) and x.get("pair")]
        if pair and "at" not in o:  # a sound picked by name replaces the one chosen for the sticker, at its moment
            o["at"] = float(pair[-1].get("at") or 0.0)
            sc["sfx"] = [x for x in sc["sfx"] if x is not pair[-1]]
        lst = sc.setdefault("sfx", [])
        if len(lst) >= MAX_SFX:
            return f"! At most {MAX_SFX} sound effects per scene."
        e = dict(_sound_ref(it), at=round(max(0.0, o.get("at", 0.0)), 2),
                 vol=round(min(2.0, max(0.0, (o["vol"] / 100 if o["vol"] > 2 else o["vol"]))), 2) if "vol" in o else 1.0)
        lst.append(e)
        return f'Sound "{it["name"]}" ({len(lst)}) hits at {e["at"]:g} s into the scene.'
    return None


def _drop_pair(sc: dict, sticker: dict) -> None:
    """Remove the sound that was added FOR this sticker (same moment, tagged pair); the user's own sounds stay."""
    if not isinstance(sticker, dict) or not sc.get("sfx"):
        return
    at = float(sticker.get("at") or 0)
    keep = [x for x in sc["sfx"] if not (isinstance(x, dict) and x.get("pair") and abs(float(x.get("at") or 0) - at) < 0.06)]
    if keep:
        sc["sfx"] = keep
    else:
        sc.pop("sfx")


def _pair_sound(sc: dict, sticker: dict, at: float) -> str:
    """A sticker pops in with the sound that matches it (money -> cash register, warning -> alert, profit -> chime…),
    unless a sound already lands within 0.3 s of it. Tagged "pair": a sound the user names later replaces it."""
    if any(abs(float(x.get("at") or 0) - at) < 0.3 for x in sc.get("sfx") or [] if isinstance(x, dict)):
        return ""
    if len(sc.get("sfx") or []) >= MAX_SFX:
        return ""
    s = library.sticker_sound(sticker)
    if not s:
        return ""
    sc.setdefault("sfx", []).append({"file": s["file"], "name": s["name"], "sync": s["sync"], "gain_db": s["gain_db"],
                                     "at": round(at, 2), "vol": s["vol"], "pair": True})
    return f' + “{s["name"]}” sound (say “sticker … silent” for none)'


def _library_global_cmd(data: dict, t: str) -> str | None:
    m = re.fullmatch(r"transitions? (?:off|none|cut|remove)|no transitions", t)
    if m:
        n = sum(1 for s in data["scenes"] if s.pop("transition", None))
        return f"Transitions removed from {n} scene(s)." if n else "! No scene has a transition."
    m = re.fullmatch(r"transitions? (?:everywhere |all )?(.+?)(?: " + _TIME + r")?( silent| no sound)?", t)
    if m:
        tid = _trans_id(m.group(1))
        if not tid:
            return f'! No transition called "{m.group(1)}".'
        vid = [x for x in data["scenes"] if x.get("keep", True) and x.get("role") != "broll"]
        n = 0
        for sc in vid[1:]:
            r = _library_scene_cmd(data, sc, f"transition {tid}" + (f" {m.group(2)}" if m.group(2) else "") +
                                   (m.group(3) or ""))
            n += r is not None and not r.startswith("!")
        return f"Transition {library.TRANSITIONS[tid][0]} between all {n + 1} scenes ({n} cuts)."
    for word, key, label in (("stickers", "stickers", "Stickers"), ("sfx", "sfx", "Sound effects"),
                             ("sound effects", "sfx", "Sound effects"), ("effects", "effects", "Effects")):
        if t in (f"{word} off", f"no {word}", f"remove all {word}", f"{word} none"):
            n = sum(1 for s in data["scenes"] if s.pop(key, None))
            return f"{label} removed from {n} scene(s)." if n else f"! No scene has {label.lower()}."
    return None


def _global_cmd(data: dict, t: str) -> str | None:
    st = data["settings"]
    m = re.fullmatch(r"(?:format|aspect|ratio|make it) (9:16|16:9|1:1|4:5|original|vertical|portrait|landscape|square|reels?|shorts?)", t)
    if m:
        v = {"vertical": "9:16", "portrait": "9:16", "reel": "9:16", "reels": "9:16", "short": "9:16", "shorts": "9:16",
             "landscape": "16:9", "square": "1:1", "4:5": "1:1"}.get(m.group(1), m.group(1))
        st["format"] = v
        return f"Format set to {v}."
    m = re.fullmatch(r"fit (crop|pad|fill|letterbox)", t)
    if m:
        st["fit"] = "pad" if m.group(1) in ("pad", "letterbox") else "crop"
        return f"Fit mode: {st['fit']}."
    if t in ("captions on", "subtitles on"):
        st["captions"] = True
        return "Captions on (all scenes without their own setting)."
    if t in ("captions off", "subtitles off", "no captions"):
        st["captions"] = False
        return "Captions off (all scenes without their own setting)."
    m = re.fullmatch(r"(?:captions?|subtitles?) (?:at|position|pos|place|height|y) (?:([\d.,]+)\s*%?|auto|default)", t)
    if m:
        if m.group(1) is not None:
            x = round(_num(m.group(1)), 1)
            if not 5 <= x <= 95:
                return "!Caption height must be between 5 and 95 (% from the top of the screen)."
        else:
            x = None
        st["caption_y"] = x
        own = [s for s in data["scenes"] if s.get("cap_pos") is not None]
        for s in own:
            s.pop("cap_pos", None)
        cleared = f" ({len(own)} scene(s) had their own position – they follow this now)" if own else ""
        if x is None:
            return "Captions follow the look's position in every scene." + cleared
        seam = float(st.get("split_ratio", 0.44) or 0.44) * 100 if st.get("layout") == "split" else None
        warn = (" ⚠ Below 80 % Instagram/YouTube put their caption text and buttons over it – 70-78 % is safer." if x > 80
                else f" ⚠ That is on the top card (the card area ends at {seam:.0f} %)." if seam and x < seam - 2
                else " ⚠ The very top is covered by the app's header on Reels/Shorts." if x < 10 else "")
        return f"Captions at {x:g} % from the top in ALL scenes{cleared}.{warn}"
    m = re.fullmatch(r"captions? (upper|uppercase|caps|normal|lower)", t)
    if m:
        st["caption_case"] = "upper" if m.group(1) in ("upper", "uppercase", "caps") else "normal"
        return f"Caption case: {st['caption_case']}."
    m = re.fullmatch(r"captions? (?:position )?(top|center|centre|middle|bottom)", t)
    if m:
        st["caption_position"] = {"centre": "center", "middle": "center"}.get(m.group(1), m.group(1))
        return f"Captions at {st['caption_position']}."
    m = re.fullmatch(rf"(?:highlight|highlight colou?r|accent) {HEX}", t)
    if m:
        st["highlight_color"] = "#" + m.group(1).upper()
        return f"Highlight colour {st['highlight_color']}."
    m = re.fullmatch(rf"(?:caption|captions|text) colou?r {HEX}", t)
    if m:
        st["caption_color"] = "#" + m.group(1).upper()
        return f"Caption colour {st['caption_color']}."
    m = re.fullmatch(r"(?:caption )?words (\d)", t)
    if m:
        st["caption_words"] = max(1, min(int(m.group(1)), 8))
        return f"Up to {st['caption_words']} words per caption."
    if t in ("remove silences", "remove silence", "cut silences", "jump cut", "jump cuts"):
        st["remove_silence"] = True
        return "Silence removal on for the whole video."
    if t in ("keep silences", "keep silence", "no jump cuts"):
        st["remove_silence"] = False
        return "Silence removal off."
    if t in ("no logo", "remove logo", "logo off"):
        st["logo"] = None
        return "Logo removed."
    m = re.fullmatch(r"logo (top-left|top-right|bottom-left|bottom-right|top left|top right|bottom left|bottom right)", t)
    if m:
        st["logo_position"] = m.group(1).replace(" ", "-")
        return f"Logo position {st['logo_position']}."
    return None


def _card_cmd(data: dict, sc: dict, raw: str) -> str:
    """card: title bajaj *IPO* aaj | card: number 2x+ | listing gain | card: chart 1,2,3 | label | card off"""
    import cards
    body = re.sub(r"^(?:no card|card)\s*[:\-]?\s*", "", raw, flags=re.I)
    if re.fullmatch(r"no card", raw.strip(), flags=re.I) or body.lower() in ("off", "none", "clear", "remove"):
        sc["card"] = {"kind": "none"}
        msg = "Empty top panel for this scene."
    elif body.lower() in ("inherit", "auto", "same", "reset"):
        sc.pop("card", None)
        msg = "This scene keeps showing the previous card."
    else:
        c = cards.parse(body)
        if not c:
            return "! Card needs content, e.g.  card: title bajaj *IPO* aaj   ·   card: number 2x | listing gain"
        sc["card"] = c
        msg = {"title": f'Title card: "{c.get("text")}".', "number": f'Number card: {c.get("value")} {c.get("label", "")}.',
               "chart": f'Chart card ({len(c.get("points", []))} points).', "none": "Empty top panel."}[c["kind"]]
    if data["settings"].get("layout") != "split":
        msg += " (Cards show in the split layout – say: layout split)"
    return msg


def _layout_cmd(data: dict, raw: str, t: str) -> str | None:
    st = data["settings"]
    if t in ("layout split", "split screen", "split layout"):  # not bare "split": that's the scene split command
        st["layout"] = "split"
        if st.get("format") == "original":
            st["format"] = "9:16"
        return "Split layout: cards on top, you below, captions on the seam (format 9:16)."
    if t in ("layout full", "full screen", "full layout", "no split"):
        st["layout"] = "full"
        return "Full-screen layout."
    m = re.fullmatch(r"headline\s*[:\-]?\s*(.*)", raw, flags=re.I | re.S)
    if m:
        st["headline"] = re.sub(r"[{}\\\r\n]", " ", m.group(1)).strip()[:100]
        return f'Headline card: "{st["headline"]}".' if st["headline"] else "Headline removed."
    if t in ("music on", "add music", "music", "quiet music", "background music", "music auto"):
        st["music"] = "auto"
        return "Quiet background music on (ducks under your voice). Own track? Put it in Brand Kit\\music."
    if t in ("music off", "no music", "remove music"):
        st["music"] = "off"
        return "Music off."
    if t in ("audio polish", "audio polish on", "clean audio", "polish audio", "loudness on"):
        st["audio_polish"] = True
        return "Voice clean-up + loudness -16 LUFS on."
    if t in ("audio polish off", "raw audio", "loudness off"):
        st["audio_polish"] = False
        return "Audio polish off."
    if t in ("background off", "green screen off", "keep background", "no background"):
        st["bg_mode"] = "off"
        return "Green screen kept as filmed."
    if t in ("background on", "green screen on", "remove green screen", "replace background", "background auto"):
        st["bg_mode"] = "auto"
        return "Green screen replaced with the off-white background."
    m = re.fullmatch(r"face (up|down|higher|lower)(?: ([\d.]+))?", t)
    if m:
        step = float(m.group(2) or 0.04)
        d = step if m.group(1) in ("up", "higher") else -step
        st["face_y"] = round(max(0.0, min(1.0, float(st.get("face_y", 0.27)) + d)), 3)
        return f"Speaker framing moved {m.group(1)} (face_y {st['face_y']})."
    return None


def _caption_lang_cmd(data: dict, raw: str, t: str) -> str | None:
    """Caption script (Hindi / Hinglish) and per-word spellings. Spellings apply to the whole video."""
    st = data["settings"]
    m = re.fullmatch(r"(?:captions?|subtitles?)(?: in| script| language)? (hinglish|roman|english letters|latin)", t)
    if m:
        st["caption_script"] = "hinglish"
        return "Captions in Hinglish (Hindi in English letters). Wrong word? spell <word> as <correct>"
    m = re.fullmatch(r"(?:captions?|subtitles?)(?: in| script| language)? (hindi|devanagari|original|as spoken|हिंदी|हिन्दी)", t)
    if m:
        st["caption_script"] = "original"
        return "Captions in Hindi script / as spoken."
    if t in ("clear spellings", "reset spellings", "remove spellings"):
        n = len(st.get("caption_fixes") or {})
        st["caption_fixes"] = {}
        return f"Removed {n} caption spelling(s)."
    # only "spell": "fix audio to be louder" must stay a request for Claude, not become a spelling
    m = re.fullmatch(r"spell\s+(\S+)\s+(?:as|=|->|→)\s*(.*)", raw, flags=re.I | re.S)
    if m:
        from hinglish import norm_key
        key, val = norm_key(m.group(1)), m.group(2).strip().strip("\"'")
        if not key:
            return "! Which word? e.g.  spell daval as double"
        val = re.sub(r"[{}\\\r\n]", "", val)[:80]
        if val.lower() in ("-", "nothing", "blank", "remove", "hide"):
            val = ""
        fixes = dict(st.get("caption_fixes") or {})
        fixes[key] = val
        st["caption_fixes"] = fixes
        return f'Captions: "{m.group(1)}" → "{val}" everywhere.' if val else f'Captions: "{m.group(1)}" hidden everywhere.'
    m = re.fullmatch(r"(?:unspell|unfix|reset spelling(?: of)?)\s+(\S+)", raw, flags=re.I)
    if m:
        from hinglish import norm_key
        fixes = dict(st.get("caption_fixes") or {})
        if fixes.pop(norm_key(m.group(1)), None) is None:
            return f'! No spelling saved for "{m.group(1)}".'
        st["caption_fixes"] = fixes
        return f'Spelling for "{m.group(1)}" removed.'
    return None


GLOBAL_TO_ALL = re.compile(r"(speed .*|[\d.,]+\s*x.*|slow ?mo.*|slow motion|normal speed|mute|unmute|volume .*|zoom .*|no zoom|punch in|"
                           r"captions? at (?:head|top|center|centre|middle|below|bottom))")


def apply(data: dict, scene_id: str | None, text: str) -> tuple[list[str], list[str], bool]:
    """Apply a command string. Returns (replies, unhandled_parts, changed)."""
    replies, unhandled, changed = [], [], False
    if re.match(r"\s*text\s*[:\-]", text, flags=re.I):
        parts = [text.strip()]  # a title may itself contain ';' – don't split it into commands
    else:
        parts = [p.strip() for p in re.split(r"[;\n]+", text) if p.strip()]
    sc = core.scene_by_id(data, scene_id) if scene_id else None
    for raw in parts:
        t = re.sub(r"\s+", " ", raw.lower().strip().rstrip(".!"))
        reply = _caption_lang_cmd(data, raw.strip(), t)  # works from the scene box and the whole-video box
        if reply is None:
            reply = _layout_cmd(data, raw.strip(), t)
        if reply is None and (sc is None or re.sub(r"\bb[- ]?roll\b", "broll", t) in ("broll auto", "auto broll")):
            reply = _broll_global_cmd(data, t)  # "broll auto" from either box; others only from the whole-video box
        if reply is None and sc is not None:
            reply = _broll_scene_cmd(data, sc, raw.strip(), t)
        if reply is not None:
            pass
        elif sc is not None and re.match(r"(card[:\-\s]|no card$)", t):
            reply = _card_cmd(data, sc, raw.strip())
        elif sc is not None:
            m = re.fullmatch(r"text[:\-]?\s+(.+)", raw.strip(), flags=re.I | re.S)
            if m and not re.fullmatch(r"(top|center|centre|middle|bottom|off|position .*)", m.group(1).strip().lower()):
                sc["text"] = m.group(1).strip()
                reply = f'Text overlay: "{sc["text"]}".'
            else:
                m = re.fullmatch(r"split(?: at| here at)? ([\d.,]+)\s*(?:s|sec|seconds)?", t)
                d = re.fullmatch(r"(?:duplicate|copy|dup|clone)(?: (?:this )?scene)?|paste(?: scene)? (\d+)(?: here)?", t)
                if m:
                    reply = _split(data, sc, _num(m.group(1)))
                elif d and d.group(1):  # paste scene N = a copy of scene N goes right after the selected scene
                    k = int(d.group(1))
                    reply = (_duplicate(data, data["scenes"][k - 1], sc) if 1 <= k <= len(data["scenes"])
                             else f"!There is no scene {k} (1-{len(data['scenes'])}).")
                elif d:
                    reply = _duplicate(data, sc, sc)
                else:
                    reply = _library_scene_cmd(data, sc, t)
                    if reply is None:
                        reply = _scene_cmd(sc, t, data)
        else:
            reply = _library_global_cmd(data, t)
            if reply is None:
                reply = _global_cmd(data, t)
            if reply is None:
                m = re.fullmatch(r"logo (.+)", raw.strip(), flags=re.I)
                if m and (core.BRAND / m.group(1).strip()).exists():
                    data["settings"]["logo"] = m.group(1).strip()
                    reply = f"Logo: {m.group(1).strip()}."
            if reply is None and GLOBAL_TO_ALL.fullmatch(t):
                results = [_scene_cmd(s, t, data) for s in data["scenes"]]
                if results and all(r is not None for r in results):
                    reply = f"{results[0]} (applied to all {len(results)} scenes)"
        if reply is None:
            unhandled.append(raw)
        else:
            replies.append(reply)
            if not reply.startswith("!"):
                changed = True
    return replies, unhandled, changed
