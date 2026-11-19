"""Instant local commands. Anything not understood here is queued for Claude.

Scene commands (typed in a scene's box):
  cut | delete | remove        keep | restore
  trim start 1.5 | trim end 2  split at 3.2
  speed 1.5 | slow mo          mute | unmute | volume 50%
  zoom 1.2 | zoom in | no zoom captions on | captions off
  remove silences | keep silences
  text: Your title here        text top|center|bottom | remove text
Global commands (whole-video box):
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

import core

HEX = r"#?([0-9a-fA-F]{6})"


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _scene_cmd(sc: dict, t: str, data: dict) -> str | None:
    """Apply one scene-level command. Returns reply text, or None if not understood."""
    if re.fullmatch(r"(cut|delete|remove|drop)( (this|this scene|scene|it))?", t):
        sc["keep"] = False
        return "Scene cut."
    if re.fullmatch(r"(keep|restore|undo cut|uncut|bring back)( (this|this scene|scene|it))?", t):
        sc["keep"] = True
        return "Scene restored."
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
    m = re.fullmatch(r"(?:volume|vol) ([\d.,]+)\s*%?", t)
    if m:
        x = _num(m.group(1))
        x = x / 100 if x > 4 else x
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
    if t in ("zoom in", "punch in", "punch-in"):
        sc["zoom"] = 1.2
        return "Punch-in zoom 1.2x."
    if t in ("no zoom", "zoom out", "reset zoom", "zoom off"):
        sc["zoom"] = 1.0
        return "Zoom removed."
    if t in ("captions on", "subtitles on", "show captions"):
        sc["captions"] = True
        return "Captions on for this scene."
    if t in ("captions off", "subtitles off", "hide captions", "no captions"):
        sc["captions"] = False
        return "Captions off for this scene."
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


GLOBAL_TO_ALL = re.compile(r"(speed .*|[\d.,]+\s*x.*|slow ?mo.*|slow motion|normal speed|mute|unmute|volume .*|zoom .*|no zoom|punch in)")


def apply(data: dict, scene_id: str | None, text: str) -> tuple[list[str], list[str], bool]:
    """Apply a command string. Returns (replies, unhandled_parts, changed)."""
    replies, unhandled, changed = [], [], False
    parts = [p.strip() for p in re.split(r"[;\n]+", text) if p.strip()]
    sc = core.scene_by_id(data, scene_id) if scene_id else None
    for raw in parts:
        t = re.sub(r"\s+", " ", raw.lower().strip().rstrip(".!"))
        reply = None
        if sc is not None:
            m = re.fullmatch(r"text[:\-]?\s+(.+)", raw.strip(), flags=re.I | re.S)
            if m and not re.fullmatch(r"(top|center|centre|middle|bottom|off|position .*)", m.group(1).strip().lower()):
                sc["text"] = m.group(1).strip()
                reply = f'Text overlay: "{sc["text"]}".'
            else:
                m = re.fullmatch(r"split(?: at| here at)? ([\d.,]+)\s*(?:s|sec|seconds)?", t)
                reply = _split(data, sc, _num(m.group(1))) if m else _scene_cmd(sc, t, data)
        else:
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
