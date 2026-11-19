"""Storyboard review: a beat-by-beat edit PLAN the user approves before anything is rendered.

    build(pid, creative)   -> Storyboards/<pid>/storyboard.json  (+ storyboard.html, the review dashboard)
    apply(pid)             -> applies APPROVED beats/items to project.json (undoable), returns a summary

A beat = one scene. Each beat lists items, each individually switchable:
    trim      head/tail dead air (scene in/out)
    pause     a pause > silence_min, shortened by silence removal (informational: global setting)
    cut       word-level cut: filler word / false start (scene["cuts"])
    punch     punch-in zoom on a hook (applied by splitting the scene and zooming the hook part)
    card      top-panel graphic (scene["card"])
    broll     B-roll slot (file from Raw Footage/<pid>/Assets, or "none available")
Automatic parts (pauses, trims, filler/false-start candidates) come from the transcript; creative parts (cards,
punch-ins, optional cuts) come from Claude via `creative` = {scene_no: {"card":..., "punch":[...], "cuts":[...]}}.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import cards
import core
import hinglish
import pipeline

FILLERS = {"um", "umm", "uh", "uhh", "uhm", "hmm", "hm", "er", "erm", "आ", "अं", "उम", "उम्म", "हम्म", "अ"}
ASSET_EXT = {".mp4", ".mov", ".mkv", ".webm", ".png", ".jpg", ".jpeg", ".webp", ".gif"}


def sb_path(pid: str) -> Path:
    return core.project_dir(pid) / "storyboard.json"


def html_path(pid: str) -> Path:
    return core.project_dir(pid) / "storyboard.html"


def _compressed(words: list[dict], i: int) -> bool:
    """Word sits in a stretch whisper squeezed (identical tiny durations) – its timing can't be trusted for a cut."""
    d = lambda k: words[k]["e"] - words[k]["s"]  # noqa: E731
    near = [k for k in (i - 1, i, i + 1) if 0 <= k < len(words)]
    return d(i) <= 0.1 and sum(1 for k in near if abs(d(k) - d(i)) < 0.006) >= 2


def assets(pid: str) -> list[str]:
    folder = core.raw_dir(pid) / "Assets"
    return sorted(p.name for p in folder.glob("*") if p.suffix.lower() in ASSET_EXT) if folder.is_dir() else []


def _lock(pid: str):
    """storyboard.json is read-modify-written by the web server (several threads) and the CLI."""
    return core._FileLock(core.project_dir(pid) / ".storyboard.lock")


def _sig(items: list[dict]) -> str:
    return json.dumps([(i["id"], i["label"]) for i in items], ensure_ascii=False)


def with_stale(pid: str, sb: dict) -> dict:
    """Sync the plan with the project as it is NOW:
    * applied = the scene still carries this beat's marker (an Undo in the studio removes it -> re-applicable)
    * stale   = the beat's scene no longer exists (re-analysis / delete since planning)."""
    scenes = {s["id"]: s for s in core.load(pid)["scenes"]}
    for b in sb.get("beats", []):
        sc = scenes.get(b["scene"])
        b["applied"] = bool(sc and sc.get("sb_applied") == b.get("sig"))
        b["stale"] = sc is None
    return sb


def build(pid: str, creative: dict | None = None) -> dict:
    with _lock(pid):
        return _build(pid, creative)


def _build(pid: str, creative: dict | None = None) -> dict:
    data = core.load(pid)
    st = data["settings"]
    creative = {str(k): v for k, v in (creative or {}).items()}
    old = core.read_json(sb_path(pid), {}) or {}
    old_beats = {b["scene"]: b for b in old.get("beats", [])}
    min_sil = float(st.get("silence_min", 0.35))
    beats, total_before, total_after = [], 0.0, 0.0
    cache: dict[str, tuple] = {}
    for no, sc in enumerate(data["scenes"], 1):
        if sc["src"] not in cache:
            cache[sc["src"]] = (core.read_json(core.work_file(pid, sc["src"], "words.json"), []) or [],
                                pipeline.load_silences(pid, sc["src"]))
        words, sil = cache[sc["src"]]
        sw = [w for w in words if sc["in"] <= (w["s"] + w["e"]) / 2 < sc["out"]]
        items = []
        # head / tail dead air
        if sw:
            head = sw[0]["s"] - sc["in"]
            if head > 0.2:
                items.append({"id": f"trimin", "type": "trim", "edge": "in", "to": round(sw[0]["s"] - 0.1, 3),
                              "label": f"Trim {head - 0.1:.2f}s dead air at the start"})
            tail = sc["out"] - sw[-1]["e"]
            if tail > 0.3:
                items.append({"id": f"trimout", "type": "trim", "edge": "out", "to": round(sw[-1]["e"] + 0.15, 3),
                              "label": f"Trim {tail - 0.15:.2f}s dead air at the end"})
        for a, b in sil:
            if b - a >= min_sil and sc["in"] < (a + b) / 2 < sc["out"] and sw and sw[0]["s"] < a and b < sw[-1]["e"]:
                items.append({"id": f"pause{a:.2f}", "type": "pause", "s": a, "e": b,
                              "label": f"Pause {b - a:.2f}s at {a:.1f}s → shortened to ~0.24s (natural breath)",
                              "auto": True})
        # filler words + false starts (immediately repeated word / 2-3 word phrase)
        keys = [hinglish.norm_key(w["w"]) for w in sw]
        for i, w in enumerate(sw):
            k = keys[i]
            reason = None
            if k in FILLERS:
                reason = "filler"
            else:
                for n in (3, 2, 1):
                    if i + 2 * n <= len(sw) and keys[i:i + n] == keys[i + n:i + 2 * n] and all(keys[i:i + n]):
                        if sw[i + n]["s"] - sw[i + n - 1]["e"] < 0.8:
                            reason = f"false start (repeated “{' '.join(hinglish.caption_word(x['w'], st) for x in sw[i:i + n])}”)"
                            w = {"w": " ".join(x["w"] for x in sw[i:i + n]), "s": sw[i]["s"], "e": sw[i + n - 1]["e"]}
                        break
            if reason:
                items.append(_cut_item(sw, i, w, reason, st))
        # plan entries keyed by scene ID (stable) or scene number (convenient, but shifts if scenes change)
        cr = creative.get(sc["id"]) or creative.get(str(no), {})
        for c in cr.get("cuts", []):
            idx = [i for i, w in enumerate(sw) if abs(w["s"] - c["s"]) < 0.05]
            w = {"w": c.get("text", ""), "s": c["s"], "e": c["e"]}
            items.append(_cut_item(sw, idx[0] if idx else 0, w, c.get("reason", "suggested cut"), st,
                                   default=c.get("default", True)))
        for p in cr.get("punch", []):
            items.append({"id": f"punch{p['s']:.2f}", "type": "punch", "s": p["s"], "e": p["e"],
                          "zoom": p.get("zoom", 1.12), "label": f"Punch-in {p.get('zoom', 1.12)}× on “{p['text']}” "
                          f"({p['s']:.1f}–{p['e']:.1f}s) – {p.get('why', 'hook')}"})
        card = cr.get("card") or sc.get("card")
        if card and cards.valid(card):
            items.append({"id": "card", "type": "card", "card": card, "label": _card_label(card)})
        av = assets(pid)
        if cr.get("broll"):
            ok = cr["broll"].get("file") in av
            items.append({"id": "broll", "type": "broll", "file": cr["broll"].get("file") if ok else None,
                          "want": cr["broll"].get("want", ""),
                          "label": (f"B-roll: {cr['broll']['file']}" if ok else
                                    f"B-roll slot: {cr['broll'].get('want', '')} – no Assets file yet, card stands in"),
                          "default": ok})
        for it in items:
            it.setdefault("default", True)
        prev = old_beats.get(sc["id"], {})
        if prev and prev.get("sig") != _sig(items):
            # the plan for this beat changed since the user reviewed it: an old "approved" must not carry over
            # (a reworked rejected beat needs a fresh look too) – notes are kept so the history isn't lost
            prev = {"notes": prev.get("notes", ""), "status": "pending", "items_on": {}}
        on = {it["id"]: prev.get("items_on", {}).get(it["id"], it["default"]) for it in items}
        est_before = pipeline.estimated_duration(dict(sc, keep=True), sil, dict(st, remove_silence=False))
        line = hinglish.caption_text([w["w"] for w in sw], st)
        srcrec = core.source_by_file(data, sc["src"]) or {}
        thumb = _planned_thumb(pid, data, sc) or sc.get("thumb")
        beats.append({
            "scene": sc["id"], "no": no, "in": sc["in"], "out": sc["out"], "src": sc["src"], "thumb": thumb,
            "proxy": srcrec.get("proxy"),
            "line": line, "words": [{"t": hinglish.caption_word(w["w"], st), "s": w["s"], "e": w["e"]} for w in sw],
            "role": cr.get("role", ""), "items": items, "items_on": on, "sig": _sig(items),
            "applied": bool(prev.get("applied")) and prev.get("sig") == _sig(items),
            "status": prev.get("status", "pending"), "notes": prev.get("notes", ""),
            "dur_before": round(est_before, 2),
        })
        total_before += est_before
    sb = {"pid": pid, "name": data["name"], "created": core.now_iso(), "status": "awaiting review",
          "summary": creative.get("summary", ""), "settings": {k: st.get(k) for k in (
              "format", "layout", "caption_script", "caption_words", "silence_min", "loudness", "duck_db", "music",
              "highlight_color", "panel_color", "bg_color")},
          "assets": assets(pid), "beats": beats}
    for b in sb["beats"]:
        b["dur_after"] = _est_after(data, b, cache)
        total_after += b["dur_after"]
    sb["dur_before"], sb["dur_after"] = round(total_before, 1), round(total_after, 1)
    core._atomic_write(sb_path(pid), sb)
    write_html(pid, sb)
    return sb


def _planned_thumb(pid: str, data: dict, sc: dict) -> str | None:
    """Mid-scene frame as it will look in the split layout's speaker panel (green screen keyed to the background),
    so the mock-up shows the planned frame, not the raw green clip."""
    st = data["settings"]
    if st.get("layout") != "split":
        return None
    key = pipeline.green_key(pid, sc["src"]) if st.get("bg_mode", "auto") == "auto" else None
    name = f"sb_{sc['id']}.jpg"
    out = core.work_dir(pid) / "thumbs" / name
    bg = (st.get("bg_color") or "#F4F1EC").lstrip("#")
    t = sc["in"] + (sc["out"] - sc["in"]) / 2
    # setpts: after -ss the frame's timestamp isn't 0, overlay would emit a background-only first frame
    vf = (f"[0:v]setpts=PTS-STARTPTS,scale=360:-2,format=yuva444p,chromakey=0x{key}:0.08:0.03,despill=type=green:mix=0.6[k];"
          f"color=c=0x{bg}:s=360x640[b];[b][k]overlay=(W-w)/2:0:shortest=1,format=yuv420p") if key else "scale=360:-2"
    try:
        pipeline.run([pipeline.FFMPEG, "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", core.raw_dir(pid) / sc["src"],
                      "-frames:v", "1", "-filter_complex" if key else "-vf", vf, "-q:v", "4", out])
        return name
    except pipeline.PipelineError:
        return None


def _cut_item(sw, i, w, reason, st, default=True) -> dict:
    risky = _compressed(sw, i) if 0 <= i < len(sw) else False
    return {"id": f"cut{w['s']:.2f}", "type": "cut", "s": round(w["s"] - 0.02, 3), "e": round(w["e"] + 0.02, 3),
            "text": hinglish.caption_word(w["w"], st) if " " not in w["w"] else w["w"],
            "label": f"Cut “{hinglish.caption_text(w['w'].split(), st)}” ({w['s']:.2f}–{w['e']:.2f}s) – {reason}"
                     + (" ⚠ word timing uncertain here (whisper squeezed it) – listen before approving" if risky else ""),
            "risky": risky, "default": default and not risky}


def _card_label(c: dict) -> str:
    if c["kind"] == "title":
        kw = ", ".join(re.findall(r"\*(.+?)\*", c["text"])) or "–"  # (no backslash inside f-string braces: py<3.12)
        return f"Top card (title): {c['text'].replace('*', '')}  – orange: {kw}"
    if c["kind"] == "number":
        return f"Top card (number): {c['value']} · {c.get('label', '')}"
    if c["kind"] == "chart":
        return f"Top card (chart): {c.get('label', '')} ({len(c['points'])} points)"
    return "Top panel empty"


def _scene_after(sc: dict, beat: dict) -> dict:
    s = copy.deepcopy(sc)
    on = beat["items_on"]
    for it in beat["items"]:
        if not on.get(it["id"]):
            continue
        if it["type"] == "trim":
            s["in" if it["edge"] == "in" else "out"] = it["to"]
        elif it["type"] == "cut":
            s.setdefault("cuts", []).append([it["s"], it["e"]])
    return s


def _est_after(data: dict, beat: dict, cache: dict) -> float:
    sc = core.scene_by_id(data, beat["scene"])
    if not sc:
        return 0.0
    s = _scene_after(sc, beat)
    return pipeline.estimated_duration(dict(s, keep=True), cache[sc["src"]][1], data["settings"])


def set_review(pid: str, scene: str, status: str | None, notes: str | None, items_on: dict | None) -> dict:
    with _lock(pid):
        return _set_review(pid, scene, status, notes, items_on)


def _set_review(pid: str, scene: str, status, notes, items_on) -> dict:
    sb = core.read_json(sb_path(pid), None)
    if not sb:
        raise ValueError("no storyboard yet")
    b = next((x for x in sb["beats"] if x["scene"] == scene), None)
    if not b:
        raise ValueError("unknown beat")
    with_stale(pid, sb)
    if b.get("applied"):
        raise ValueError("this beat is already applied to the edit – use Undo in the studio to take it back")
    if status in ("pending", "approved", "rejected"):
        b["status"] = status
    if notes is not None:
        b["notes"] = str(notes)[:2000]
    if isinstance(items_on, dict):
        for k, v in items_on.items():
            if k in b["items_on"]:
                b["items_on"][k] = bool(v)
    data = core.load(pid)
    cache = {}
    sc = core.scene_by_id(data, scene)
    if sc:
        cache[sc["src"]] = (None, pipeline.load_silences(pid, sc["src"]))
        b["dur_after"] = _est_after(data, b, cache)
    sb["dur_after"] = round(sum(x.get("dur_after", 0) for x in sb["beats"]), 1)
    sb["updated"] = core.now_iso()
    core._atomic_write(sb_path(pid), sb)
    return b


def apply(pid: str) -> list[str]:
    """Apply approved beats (only their switched-on items). Rejected/pending beats are left unchanged.
    Each beat is applied ONCE (re-running apply would trim/split it again); undo in the studio reverts."""
    with _lock(pid):
        return _apply(pid)


def _apply(pid: str) -> list[str]:
    sb = core.read_json(sb_path(pid), None)
    if not sb:
        raise ValueError("no storyboard yet")
    with_stale(pid, sb)
    log: list[str] = []
    todo = [b for b in sb["beats"] if b["status"] == "approved" and not b.get("applied") and not b.get("stale")]
    log += [f"beat {b['no']}: its scene no longer exists (re-analyzed?) – rebuild the storyboard"
            for b in sb["beats"] if b["status"] == "approved" and b.get("stale")]
    if not todo:
        return ["nothing to apply (no approved beats that aren't applied yet)"]
    done_ids: list[str] = []

    def fn(d):
        core.snapshot(pid, d)
        for b in todo:
            sc = core.scene_by_id(d, b["scene"])
            if not sc:
                log.append(f"beat {b['no']}: scene no longer exists – skipped")
                continue
            new = _scene_after(sc, b)
            sc.update({k: new[k] for k in ("in", "out") if k in new})
            sc["sb_applied"] = b["sig"]  # marker: undo removes it, so the beat becomes applicable again
            if new.get("cuts"):
                sc["cuts"] = sorted(new["cuts"])
            for it in b["items"]:
                if b["items_on"].get(it["id"]) and it["type"] == "card":
                    sc["card"] = it["card"]
            # punch-ins: split off the hook part and zoom it (snap zoom on the cut, like the profile says)
            punches = sorted((it for it in b["items"] if it["type"] == "punch" and b["items_on"].get(it["id"])),
                             key=lambda it: it["s"])
            parts = [sc]
            for p in punches:
                for t in (p["s"], p["e"]):  # split at the hook's edges (unless within 0.3 s of an existing edge)
                    host = next((x for x in parts if x["in"] + 0.3 < t < x["out"] - 0.3), None)
                    if host:
                        new_sc = copy.deepcopy(host)
                        new_sc["id"] = core.new_id("sc")
                        new_sc["in"] = new_sc["orig_in"] = round(t, 3)
                        host["out"] = host["orig_out"] = round(t, 3)
                        new_sc.pop("card", None)  # the card keeps showing (inherited), no re-pop
                        new_sc["zoom"] = 1.0
                        d["scenes"].insert(d["scenes"].index(host) + 1, new_sc)
                        parts.append(new_sc)
                for x in parts:  # the part(s) lying inside the hook get the zoom
                    if x["in"] >= p["s"] - 0.31 and x["out"] <= p["e"] + 0.31:
                        x["zoom"] = p["zoom"]
            for x in parts:
                if x.get("cuts"):
                    x["cuts"] = [c for c in x["cuts"] if c[1] > x["in"] and c[0] < x["out"]]
            n_on = sum(1 for v in b["items_on"].values() if v)
            log.append(f"beat {b['no']}: applied {n_on} item(s)" + (f", split into {len(parts)} for punch-ins"
                                                                     if len(parts) > 1 else ""))
            done_ids.append(b["scene"])
        return True
    core.mutate(pid, fn)
    for b in sb["beats"]:
        if b["scene"] in done_ids:
            b["applied"] = True
    left = sum(1 for b in sb["beats"] if not b.get("applied"))
    sb["status"] = "applied" if not left else f"partly applied ({left} beat(s) not applied)"
    sb["applied_at"] = core.now_iso()
    core._atomic_write(sb_path(pid), sb)
    write_html(pid, sb)
    return log


def write_html(pid: str, sb: dict) -> Path:
    """The dashboard file in Storyboards/<pid>/. Served live by the studio at /storyboard/<pid> (saves go to the
    studio); opened directly from disk it still shows everything with the data embedded below."""
    tpl = (Path(__file__).parent / "static" / "storyboard.html").read_text(encoding="utf-8")
    blob = json.dumps(sb, ensure_ascii=False).replace("</", "<\\/")
    out = html_path(pid)
    out.write_text(tpl.replace("/*__EMBEDDED__*/null", blob), encoding="utf-8")
    return out
