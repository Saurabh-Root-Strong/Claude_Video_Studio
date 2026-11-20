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
from pathlib import Path

import core

HEX = r"#?([0-9a-fA-F]{6})"


def _num(s: str) -> float:
    return float(s.replace(",", "."))


# ----------------------------------------------------------------- B-roll (cutaways over the voice)
# A scene with role "broll" is not part of the main sequence: its clip is footage to cover OTHER scenes with.
# A speaker scene's cutaway: sc["broll"] = {"src": clip file, "where": "top"|"full", "at": seconds (optional)}.
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
    m = re.fullmatch(r"broll (top|panel|top panel|full|fullscreen|full screen)", t)
    if m:
        if not sc.get("broll"):
            return "! No B-roll on this scene yet – first: broll <clip name>"
        where = "full" if m.group(1).startswith("full") else "top"
        sc["broll"] = dict(sc["broll"], where=where)
        return f"B-roll {'full screen (hides you, voice continues)' if where == 'full' else 'in the top panel'}." + \
            ("" if split or where == "full" else " (The top panel needs the split layout – it shows full screen now.)")
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
    sc["broll"] = {"src": f, **({"where": old["where"]} if old.get("where") else {})}  # explicit choice kept, else follows the look
    shown = "top panel" if split and sc["broll"].get("where", "top") == "top" else "full screen"
    return f'B-roll "{f}" over this scene ({shown}) – your voice keeps playing.'


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
    m = re.fullmatch(r"broll (top|panel|top panel|full|fullscreen|full screen)", t)
    if m:
        where = "full" if m.group(1).startswith("full") else "top"
        n = 0
        for s in data["scenes"]:
            if isinstance(s.get("broll"), dict):
                s["broll"] = dict(s["broll"], where=where)
                n += 1
        return f"All B-roll ({n} scene(s)) {'full screen' if where == 'full' else 'in the top panel'}." if n else "! No scene has B-roll."
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


GLOBAL_TO_ALL = re.compile(r"(speed .*|[\d.,]+\s*x.*|slow ?mo.*|slow motion|normal speed|mute|unmute|volume .*|zoom .*|no zoom|punch in)")


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
