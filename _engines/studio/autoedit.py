"""✨ AUTO-EDIT: one click on the Scenes page – the studio reads every scene like an editor and places transitions,
effects, stickers, sound effects and zoom moments where they help, then the preview renders for review.

How it decides (rules of a good Reels editor, not randomness):
  * the WORDS and their exact timing (speech recognition; Hindi script read through Hinglish) find the moment that
    matters in a scene: profit / loss / money / warning / tip / question / IPO / hold / call-to-action ...
    -> a sticker pops in ON that word (and vanishes), a matching sound lands its hit on it, a zoom moment punches in
  * transitions only where the TOPIC changes (new card, other clip, after B-roll) – straight cuts elsewhere
  * the first scene gets a hook (punch-in on its key word), the last a call-to-action sticker + fade-out
  * scenes without speech get a slow zoom so a still shot stays alive
  * strength: subtle / balanced / energetic caps how much is added (default from the look's brief)
Everything it adds is tagged "auto": running it again replaces only its own work – the user's edits are never touched.
It never picks the unlicensed meme sounds (only the CC0 SFX Kit), and it explains every choice in a report.
"""
from __future__ import annotations

import re

import core
import hinglish
import library
import pipeline

# keyword stems (romanised, lowercase) -> category. Matched as word starts so "munafe", "profits", "girawat" count.
CATS = {
    "warning": ["warning", "risk", "savdhan", "dhyan", "galti", "mistake", "scam", "fraud", "danger", "khatra", "avoid",
                "bach", "dhoka", "trap", "bewakoof", "loss nahi"],
    "money": ["paisa", "paise", "rupee", "rupay", "rupaye", "crore", "karod", "lakh", "money", "cash", "income", "salary",
              "dividend", "kamai", "kamao", "₹"],
    "up": ["profit", "munaf", "gain", "badh", "upar", "high", "record", "double", "2x", "3x", "10x", "tez", "rally",
           "boom", "jump", "growth", "grow", "return", "listing gain", "multibagger", "uchhal"],
    "down": ["loss", "nuksan", "nuksaan", "gir", "crash", "down", "neeche", "niche", "fall", "low", "drop", "girawat",
             "mandi", "doob", "dub"],
    "question": ["kya", "kyun", "kyon", "kaise", "kab", "kitna", "question", "sawal", "why", "how", "what"],
    "idea": ["tip", "idea", "secret", "trick", "samjh", "rule", "formula", "strategy", "tarika", "fayda", "faayda", "lesson"],
    "ipo": ["ipo", "listing", "launch", "allot", "subscribe kar", "gmp"],
    "hold": ["hold", "long term", "longterm", "patience", "sabr", "invest", "sip", "wait"],
    "cta": ["follow", "subscribe", "like", "share", "comment", "save"],
}
PRIORITY = ["warning", "money", "up", "down", "ipo", "idea", "hold", "question", "cta"]
LOOK = {  # category -> (stickers to rotate through, sound id, sound volume)
    "warning": (["warning", "red_exclamation_mark", "no_entry"], "notification-alert-clear", 0.7),
    "money": (["money_bag", "money_with_wings", "dollar_banknote", "coin"], "money-register", 0.6),
    "up": (["chart_increasing", "rocket", "fire", "high_voltage"], "notification-success-chime", 0.55),
    "down": (["chart_decreasing", "face_screaming_in_fear", "cross_mark"], "notification-failure-fall", 0.6),
    "question": (["thinking_face", "red_question_mark", "eyes"], "tone-ding-mid", 0.5),
    "idea": (["light_bulb", "brain", "bullseye"], "notification-success-sparkle", 0.55),
    "ipo": (["bell", "party_popper", "megaphone"], "notification-success-ding-ding", 0.55),
    "hold": (["hourglass_done", "gem_stone", "locked"], "tone-ding-low", 0.5),
    "cta": (["backhand_index_pointing_down", "bell", "thumbs_up"], "pop-medium-in", 0.6),
}
WHY = {"warning": "a warning", "money": "money talk", "up": "a gain / profit", "down": "a fall / loss",
       "question": "a question", "idea": "a tip", "ipo": "the IPO", "hold": "holding long term", "cta": "the call to action"}
LEVELS = {  # share of scenes that may get a sticker / zoom, transitions at most every Nth cut, effects allowed
    "subtle": {"stickers": 0.3, "punches": 0.25, "trans_every": 4, "effects": False},
    "balanced": {"stickers": 0.6, "punches": 0.45, "trans_every": 3, "effects": True},
    "energetic": {"stickers": 0.9, "punches": 0.7, "trans_every": 2, "effects": True},
}


def default_level(data: dict) -> str:
    """From the active look's brief / the video brief: 'calm, clean' -> subtle; 'energetic, punchy, fast' -> energetic."""
    lb = (data.get("look_briefs") or {}).get(data.get("style_source", "none"), "") + " " + (data.get("brief") or "")
    t = lb.lower()
    if re.search(r"\b(energetic|punchy|fast|hype|viral|high energy|dynamic)\b", t):
        return "energetic"
    if re.search(r"\b(calm|clean|minimal|simple|subtle|serious|corporate)\b", t):
        return "subtle"
    return "balanced"


def _norm(w: str) -> str:
    try:
        r = hinglish.to_hinglish(w)
    except Exception:  # noqa: BLE001
        r = w
    return re.sub(r"[^\w₹%]+", "", r.lower())


def _hit(words: list[dict], first_scene: bool, last_scene: bool) -> tuple[str, dict] | None:
    """The category + the word where it lands (strongest category, its first word)."""
    found: dict[str, dict] = {}
    toks = [(_norm(w["w"]), w) for w in words]
    joined = [(toks[i][0] + " " + toks[i + 1][0], toks[i][1]) for i in range(len(toks) - 1)]
    for cat, stems in CATS.items():
        for t, w in toks + joined:
            if any(t.startswith(s) or (" " in s and t == s) for s in stems):
                found.setdefault(cat, w)
                break
    if words and words[-1]["w"].strip().endswith("?"):
        found.setdefault("question", words[-1])
    if last_scene and "cta" in found:
        return "cta", found["cta"]
    for cat in PRIORITY:
        if cat in found and not (cat == "cta" and not last_scene):
            if cat == "question" and not first_scene and not words[-1]["w"].strip().endswith("?") and len(found) > 1:
                continue  # "kya" in passing is not a question scene when something stronger is there
            return cat, found[cat]
    return None


FILLER = {"dosto", "doston", "aapko", "aapka", "aapke", "hamare", "hamara", "humko", "kyunki", "kyonki", "lekin", "isliye",
           "matlab", "basically", "actually", "toh", "agar", "jaise", "waise", "abhi", "yahan", "wahan", "uske", "inke",
           "unke", "inka", "unka", "hamesha", "sakte", "sakta", "karna", "karne", "karte", "honge", "raha", "rahe", "rahi",
           "because", "really", "should", "would", "could", "there", "their", "about", "which", "these", "those"}


def _emphasis(words: list[dict]) -> dict | None:
    """The word to punch in on: a number / % / ₹, else a long CONTENT word near the middle (not a filler like 'dosto')."""
    for w in words:
        if re.search(r"\d|%|₹|x\b", w["w"]):
            return w
    longw = [w for w in words if len(_norm(w["w"])) >= 5 and _norm(w["w"]) not in FILLER]
    return longw[len(longw) // 2] if longw else None


def plan(pid: str, data: dict, level: str | None = None) -> dict:
    """Decide everything (no changes). Returns {"level", "scenes": {id: {...}}, "report": [lines]}."""
    level = level if level in LEVELS else default_level(data)
    cfg = LEVELS[level]
    st = data["settings"]
    split = st.get("layout") == "split"
    skipped = {s["file"] for s in data["sources"] if s.get("include") is False}
    vid = [s for s in data["scenes"] if s.get("keep", True) and s.get("role") != "broll" and s["src"] not in skipped]
    cat_lib = library.catalog()
    have_st = {x["id"] for x in cat_lib["stickers"]}
    snd = {x["id"]: x for x in cat_lib["sounds"] if not x.get("risky")}
    words_cache: dict[str, list] = {}
    sil_cache: dict[str, list] = {}
    out: dict = {}
    report = []
    n = len(vid)
    budget_st, budget_pu = round(n * cfg["stickers"] + 0.49), round(n * cfg["punches"] + 0.49)
    used_st: dict[str, int] = {}
    pos_flip = 0
    since_trans = 99
    prev = None
    # effective cards (split layout) to spot topic changes
    head = (st.get("headline") or "").strip()
    shown = {"kind": "title", "text": head} if head else None
    for i, sc in enumerate(vid):
        if sc["src"] not in words_cache:
            words_cache[sc["src"]] = core.read_json(core.work_file(pid, sc["src"], "words.json"), []) or []
            sil_cache[sc["src"]] = pipeline.load_silences(pid, sc["src"])
        ws = pipeline.words_in(words_cache[sc["src"]], sc["in"], sc["out"])
        speech = bool(ws) and not sc.get("no_speech")
        ranges = pipeline.kept_ranges(sc, sil_cache[sc["src"]], st)
        speed = float(sc.get("speed") or 1.0)
        out_len = pipeline.scene_out_len(sc, sil_cache[sc["src"]], st)

        def at_of(w):  # a word's moment on the scene's OUTPUT timeline (after jump cuts + speed)
            t = pipeline._map_time(max(0.0, float(w["s"]) - sc["in"]), ranges, speed)
            return None if t is None else round(min(max(0.0, t), max(0.0, out_len - 0.2)), 2)
        a: dict = {"stickers": [], "sfx": [], "effects": [], "punches": [], "transition": None, "why": []}
        first, last = i == 0, i == n - 1
        c = sc.get("card")
        eff = c if c and (c.get("kind") == "none" or c.get("text") or c.get("value") or len(c.get("points") or []) >= 2) else shown
        topic_change = prev is not None and (
            (split and eff != shown) or sc["src"] != prev["src"] or (prev.get("broll") or {}).get("where") == "full")
        shown = eff
        # ---- transition at a topic change (never on the first scene); not over a user's own transition
        if i > 0 and topic_change and since_trans >= cfg["trans_every"] and not (sc.get("transition") and not sc["transition"].get("auto")):
            tid = "smoothleft" if level != "energetic" else "slideleft"
            a["transition"] = {"type": tid, "dur": 0.35 if level != "subtle" else 0.3, "auto": True}
            a["why"].append(f"{library.TRANSITIONS[tid][0]} in – new topic")
            since_trans = 0
        else:
            since_trans += 1
        # ---- the moment that matters -> sticker + matching sound (+ zoom)
        hit = _hit(ws, first, last) if speech else None
        if hit and budget_st > 0:
            cat, w = hit
            t = at_of(w)
            choices = [x for x in LOOK[cat][0] if x in have_st]
            if t is not None and choices:
                k = used_st.get(cat, 0)
                used_st[cat] = k + 1
                sid = choices[k % len(choices)]
                it = library.find("sticker", sid, exact=True)
                pos = ("bottom-right", "bottom-left")[pos_flip % 2] if split else ("top-right", "top-left")[pos_flip % 2]
                pos_flip += 1
                dur = round(min(1.6, max(0.8, out_len - t - 0.1)), 2)
                if it and dur >= 0.6:
                    a["stickers"].append({"file": it["file"], "name": it["name"], "at": t, "dur": dur, "pos": pos,
                                          "size": 0.2 if level == "subtle" else 0.24, "anim": "pop", "auto": True})
                    budget_st -= 1
                    s_id = LOOK[cat][1]
                    if s_id in snd:
                        s = snd[s_id]
                        a["sfx"].append({"file": s["file"], "name": s["name"], "at": t, "vol": LOOK[cat][2],
                                         "sync": round(float(s.get("sync") or 0), 3), "gain_db": float(s.get("gain_db") or 0),
                                         "auto": True})
                    else:  # never a silent pop-up: the sticker's own matching sound
                        s2 = library.sticker_sound(it, w.get("w", ""))
                        if s2:
                            s_id = s2["name"]
                            a["sfx"].append({"file": s2["file"], "name": s2["name"], "at": t, "vol": s2["vol"],
                                             "sync": s2["sync"], "gain_db": s2["gain_db"], "auto": True})
                    a["why"].append(f"{it['name']} sticker + “{s_id.replace('-', ' ')}” on “{_norm(w['w'])}” ({WHY[cat]})")
        # ---- zoom moment on the key word / number (the hook always gets one)
        emp = (hit[1] if hit else None) or (_emphasis(ws) if speech else None)
        if speech and emp and (first or budget_pu > 0):
            s0, e0 = max(sc["in"], float(emp["s"]) - 0.1), min(sc["out"], float(emp["e"]) + 0.45)
            if e0 - s0 >= 0.3 and not sc.get("punches"):
                a["punches"].append([round(s0, 3), round(e0, 3), 1.12 if level == "subtle" else 1.18])
                a["why"].append(f"zoom-in on “{_norm(emp['w'])}”" + (" (hook)" if first else ""))
                if not first:
                    budget_pu -= 1
        # ---- effects: open / close / keep a speechless shot alive
        if cfg["effects"]:
            if last and n > 1:
                a["effects"].append("fadeout")
                a["why"].append("fade-out ending")
            if not speech and out_len >= 1.5:
                a["effects"].append("slowzoom")
                a["why"].append("slow zoom – no speech, keeps the shot alive")
            if level == "energetic" and hit and hit[0] in ("up", "warning") and not first:
                a["effects"].append("flash" if hit[0] == "up" else "shake")
                a["why"].append("flash" if hit[0] == "up" else "camera shake")
        out[sc["id"]] = a
        report.append(f"Scene {data['scenes'].index(sc) + 1}: " + ("; ".join(a["why"]) if a["why"] else "clean – nothing needed"))
        prev = sc
    return {"level": level, "scenes": out, "report": report}


def clear_auto(data: dict) -> int:
    """Remove everything a previous auto-edit added (the user's own edits stay)."""
    n = 0
    for sc in data["scenes"]:
        for key in ("stickers", "sfx"):
            if sc.get(key):
                keep = [x for x in sc[key] if not (isinstance(x, dict) and x.get("auto"))]
                n += len(sc[key]) - len(keep)
                if keep:
                    sc[key] = keep
                else:
                    sc.pop(key)
        if isinstance(sc.get("transition"), dict) and sc["transition"].get("auto"):
            sc.pop("transition")
            n += 1
        for e in sc.pop("auto_fx", None) or []:
            if e in (sc.get("effects") or []):
                sc["effects"].remove(e)
                n += 1
        if sc.get("effects") == []:
            sc.pop("effects")
        for p in sc.pop("auto_punch", None) or []:
            if p in (sc.get("punches") or []):
                sc["punches"].remove(p)
                n += 1
        if sc.get("punches") == []:
            sc.pop("punches")
    return n


def apply(data: dict, pl: dict) -> dict:
    """Write a plan into the scenes (call inside mutate, after a snapshot). Returns counts."""
    clear_auto(data)
    cnt = {"stickers": 0, "sfx": 0, "transitions": 0, "effects": 0, "punches": 0}
    for sc in data["scenes"]:
        a = pl["scenes"].get(sc["id"])
        if not a:
            continue
        if a["transition"]:
            snd = None
            want = library.TRANSITIONS[a["transition"]["type"]][2]
            it = library.find("sound", want, exact=True) if want else None
            if it:
                snd = {"file": it["file"], "name": it["name"], "sync": round(float(it.get("sync") or 0), 3),
                       "gain_db": float(it.get("gain_db") or 0)}
            sc["transition"] = dict(a["transition"], sound=snd)
            cnt["transitions"] += 1
        for k in ("stickers", "sfx"):
            if a[k]:
                sc[k] = (sc.get(k) or []) + a[k]
                cnt[k] += len(a[k])
        add_fx = [e for e in a["effects"] if e not in (sc.get("effects") or [])]
        if add_fx:
            sc["effects"] = (sc.get("effects") or []) + add_fx
            sc["auto_fx"] = add_fx
            cnt["effects"] += len(add_fx)
        if a["punches"]:
            sc["punches"] = sorted((sc.get("punches") or []) + a["punches"])
            sc["auto_punch"] = a["punches"]
            cnt["punches"] += len(a["punches"])
    data.setdefault("settings", {})
    data["auto_edit"] = {"level": pl["level"], "at": core.now_iso(), "report": pl["report"], "counts": cnt}
    return cnt
