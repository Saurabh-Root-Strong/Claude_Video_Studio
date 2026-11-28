"""End-to-end self-test for CVE Studio. Runs in a TEMP root – never touches the real folders.

    .venv\\Scripts\\python.exe selftest.py
"""
from __future__ import annotations

import json
import math
import queue
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).parent
FIX = HERE.parent / "tests" / "studio"
ROOT = Path(tempfile.mkdtemp(prefix="cve_selftest_"))
os.environ["CVE_ROOT"] = str(ROOT)
for d in ("Raw Footage", "References", "Storyboards", "Exports", "Brand Kit"):
    (ROOT / d).mkdir()

sys.path.insert(0, str(HERE))
import make_fixtures  # noqa: E402
make_fixtures.main()  # test videos are generated, not stored in git
import app as appmod  # noqa: E402  (imports core with CVE_ROOT set)
import core  # noqa: E402
import pipeline  # noqa: E402
import exportopts  # noqa: E402

client = appmod.app.test_client()
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def wait_job(pid, kind, timeout=600):
    """Wait until `kind` has finished AND nothing else is queued; return the job record."""
    t = time.time()
    while time.time() - t < timeout:
        j = core.load(pid).get("job") or {}
        if j.get("status") in ("done", "error") and not j.get("pending") and appmod._jobs.unfinished_tasks == 0:
            return j
        time.sleep(1)
    return core.load(pid).get("job")


def post(path, body=None, **kw):
    return client.post(path, json=body or {}, **kw)


try:
    # ---------------------------------------------------------------- probe: phone rotation
    meta = pipeline.probe(FIX / "phone_rot.mp4")
    check("rotated phone video reports portrait size", meta["width"] < meta["height"], f"{meta['width']}x{meta['height']}")

    # ---------------------------------------------------------------- whisper JSON parsing (Hindi split chars)
    r = subprocess.run([sys.executable, str(HERE / "test_whisper_parse.py")], capture_output=True, text=True,
                       encoding="utf-8")
    check("whisper parser unit tests (Hindi split/dropped bytes)", r.returncode == 0,
          r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:])

    # ---------------------------------------------------------------- Hindi-style scene split (sparse punctuation)
    hw = [{"w": f"शब्द{i}", "s": i * 0.3, "e": i * 0.3 + 0.25} for i in range(60)]
    hs = [[3.4, 3.9], [8.5, 9.0], [11.5, 11.95], [15.5, 16.0]]
    hb = pipeline.build_scenes(18.0, [], hw, hs)
    check("no-punctuation speech splits at ~0.5 s pauses", len(hb) == 5, str(hb))

    # ---------------------------------------------------------------- whisper glitch detection (fake repeats)
    said = "aage kya hoga agar aap lene ki soch rahe hain".split()
    good = [{"w": x, "s": 1 + i * 0.3, "e": 1.25 + i * 0.3} for i, x in enumerate(said)]
    glitch = good[:2] + [{"w": x, "s": 2 + i * 0.08, "e": 2.08 + i * 0.08} for i, x in enumerate(said * 2)]
    sw = pipeline.suspect_windows(glitch, [[0.5, 1.9], [5.0, 5.6]], 10.0)
    check("whisper glitch (sentence repeated, 0.08 s words) detected; normal speech not flagged",
          pipeline.suspect_windows(good, [], 10.0) == [] and sw == [(1.9, 5.0)], str(sw))

    # ---------------------------------------------------------------- captions never overlap (one line at a time)
    import re as _re
    fast = [{"w": f"w{i}", "s": 1 + i * 0.12, "e": 1 + i * 0.12 + 0.3} for i in range(12)]  # compressed, overlapping
    sc_ = {"id": "t", "in": 0.0, "out": 4.0, "speed": 1.0}
    ass = ROOT / "t.ass"
    pipeline.write_ass(ass, sc_, fast, [(0.0, 4.0)], dict(core.DEFAULT_SETTINGS), 1080, 1920)
    ts = [tuple(_re.findall(r"Dialogue: \d,([\d:.]+),([\d:.]+)", l)[0]) for l in ass.read_text("utf-8-sig").splitlines()
          if l.startswith("Dialogue")]
    sec = lambda t: int(t.split(":")[0]) * 3600 + int(t.split(":")[1]) * 60 + float(t.split(":")[2])  # noqa: E731
    overl = sum(1 for k in range(len(ts) - 1) if sec(ts[k][1]) > sec(ts[k + 1][0]) + 1e-6)
    check("caption events never overlap (no stacked double lines)", overl == 0 and len(ts) >= 10, f"{overl} overlaps")

    # ---------------------------------------------------------------- Hinglish captions (Hindi in English letters)
    import hinglish
    cases = {"करना": "karna", "सकता": "sakta", "आपके": "aapke", "लगभग": "lagbhag", "पास": "paas", "अच्छा": "achha",
             "में": "mein", "ज़िंदगी": "zindagi", "जाएगा": "jayega", "हुआ": "hua", "सवाल": "sawal", "है।": "hai.",
             "लग-बगा": "lag-baga", "IPO": "IPO", "मार्केट": "market", "२०२६": "2026", "दोस्तो,": "dosto,"}
    bad = {k: hinglish.to_hinglish(k) for k, v in cases.items() if hinglish.to_hinglish(k) != v}
    check("Hinglish transliteration (schwa drop, dictionary, punctuation)", not bad, str(bad))
    hs_ = dict(core.DEFAULT_SETTINGS, caption_script="hinglish", caption_fixes={"ekspektet": "expected"})
    hw_ = [{"w": w, "s": 0.2 + i * 0.4, "e": 0.5 + i * 0.4} for i, w in enumerate(["आप", "एक्सपेक्टेट", "करना"])]
    pipeline.write_ass(ass, sc_, hw_, [(0.0, 4.0)], hs_, 1080, 1920)
    body = ass.read_text("utf-8-sig")
    check("Hinglish captions render in English letters + spell fix by shown word",
          "aap" in body and "expected" in body and "karna" in body and not hinglish.DEVA.search(body))
    pipeline.write_ass(ass, sc_, hw_, [(0.0, 4.0)], dict(hs_, caption_script="original", caption_fixes={}), 1080, 1920)
    check("Hindi script captions unchanged when script = original", "करना" in ass.read_text("utf-8-sig"))

    # ---------------------------------------------------------------- project + upload + analyze
    r = post("/api/projects", {"name": "Self Test"})
    pid = r.get_json()["id"]
    with open(FIX / "recap_test.mp4", "rb") as f:
        r = client.post(f"/api/projects/{pid}/upload?kind=source", data={"files": (f, "recap_test.mp4")},
                        content_type="multipart/form-data")
    check("upload footage", r.status_code == 200, str(r.get_json()))
    post(f"/api/projects/{pid}/analyze")
    post(f"/api/projects/{pid}/render", {"kind": "preview"})   # queued behind analyze
    j0 = core.load(pid)["job"]
    check("second job queues instead of clobbering", j0.get("status") in ("running", "queued"), str(j0.get("pending")))
    j = wait_job(pid, "analyze")   # waits for analyze AND the queued preview
    log = "\n".join(j.get("log", []))
    check("analysis finished", "Analysis complete." in log and "FAILED" not in log, log[-300:] if "FAILED" in log else "")
    check("queued preview ran after analysis", j["kind"] == "preview" and j["status"] == "done", j.get("error") or "")
    check("log keeps previous job's lines", "Analysis complete." in log)
    d = core.load(pid)
    scenes = d["scenes"]
    bounds = [round(s["out"], 1) for s in scenes]
    check("scenes split at sentence/clause pauses", len(scenes) == 6, f"{len(scenes)} scenes, ends {bounds}")
    check("visual cut is a scene boundary", any(abs(b - 13.1) < 0.15 for b in bounds), str(bounds))
    check("preview proxy created", bool(d["sources"][0].get("proxy")) and
          (core.work_dir(pid) / "proxy" / d["sources"][0]["proxy"]).exists())

    # ---------------------------------------------------------------- commands
    s1, s3 = scenes[0]["id"], scenes[2]["id"]
    n_scenes = len(scenes)
    post(f"/api/projects/{pid}/command", {"scene": s3, "text": "cut"})
    post(f"/api/projects/{pid}/command", {"scene": s1, "text": "split at 2"})
    d = core.load(pid)
    a, b = d["scenes"][0], d["scenes"][1]
    trimmed = lambda s: s["in"] != s["orig_in"] or s["out"] != s["orig_out"]  # noqa: E731
    check("split does not mark halves as trimmed", not trimmed(a) and not trimmed(b))
    r = post(f"/api/projects/{pid}/undo").get_json()
    check("undo reverts split", r["ok"] and len(core.load(pid)["scenes"]) == n_scenes)
    # copy / paste a scene: the copy has every edit, a new id, sits after the target; a cut scene's copy is IN the video
    post(f"/api/projects/{pid}/command", {"scene": s1, "text": "speed 1.25"})
    r = post(f"/api/projects/{pid}/command", {"scene": s1, "text": "duplicate"}).get_json()
    d = core.load(pid)
    cp = d["scenes"][1]
    check("duplicate: copy right after, new id, same clip + edits, page told its id",
          len(d["scenes"]) == n_scenes + 1 and r.get("new") == [cp["id"]] and cp["id"] != s1
          and (cp["src"], cp["in"], cp["out"], cp.get("speed")) == (d["scenes"][0]["src"], d["scenes"][0]["in"], d["scenes"][0]["out"], 1.25),
          str(r))
    n3 = [i for i, x in enumerate(d["scenes"]) if x["id"] == s3][0] + 1
    r = post(f"/api/projects/{pid}/command", {"scene": s1, "text": f"paste scene {n3}"}).get_json()
    d = core.load(pid)
    cp3 = d["scenes"][1]
    check("paste scene N: copy of N after the selected scene; a cut scene's copy is kept",
          len(d["scenes"]) == n_scenes + 2 and cp3["in"] == core.scene_by_id(d, s3)["in"] and cp3.get("keep", True)
          and not core.scene_by_id(d, s3).get("keep", True), str(r))
    r = post(f"/api/projects/{pid}/command", {"scene": s1, "text": "paste scene 99"}).get_json()
    check("paste of a scene that doesn't exist refused", r["replies"][0].startswith("!") and len(core.load(pid)["scenes"]) == n_scenes + 2)
    post(f"/api/projects/{pid}/undo"); post(f"/api/projects/{pid}/undo"); post(f"/api/projects/{pid}/undo")
    check("undo removes the copies (one step each)", len(core.load(pid)["scenes"]) == n_scenes
          and core.scene_by_id(core.load(pid), s1).get("speed") in (None, 1, 1.0))
    r = post(f"/api/projects/{pid}/command", {"scene": s1, "text": "speed 9"}).get_json()
    check("bad command rejected", r["replies"] and r["replies"][0].startswith("!"))
    vols = []
    for v in ("volume 3%", "volume 150", "volume 1.5", "volume 0%"):
        post(f"/api/projects/{pid}/command", {"scene": s1, "text": v})
        vols.append(core.scene_by_id(core.load(pid), s1)["volume"])
    post(f"/api/projects/{pid}/command", {"scene": s1, "text": "volume 100%"})
    check("volume: '3%' = 3 % (not 300 %), '150' = 150 %, '1.5' = 1.5x, '0%' = silent", vols == [0.03, 1.5, 1.5, 0.0], str(vols))
    r = post(f"/api/projects/{pid}/command", {"scene": s1, "text": "add a glitch transition"}).get_json()
    check("unknown command goes to Claude inbox", r["queued"] == ["add a glitch transition"])
    post(f"/api/projects/{pid}/command", {"scene": None, "text": "remove silences"})
    post(f"/api/projects/{pid}/command", {"scene": scenes[1]["id"], "text": "text: 100% up; Sensex flat"})
    check("text: keeps its ';' (not split into commands)",
          core.scene_by_id(core.load(pid), scenes[1]["id"])["text"] == "100% up; Sensex flat")
    post(f"/api/projects/{pid}/command", {"scene": scenes[1]["id"], "text": "speed 1.2"})
    post(f"/api/projects/{pid}/command", {"scene": scenes[3]["id"], "text": "trim end 1"})
    post(f"/api/projects/{pid}/command", {"scene": scenes[3]["id"], "text": "reset trim"})
    sc4 = core.scene_by_id(core.load(pid), scenes[3]["id"])
    check("reset trim restores full length", sc4["out"] == sc4["orig_out"])
    # on-screen text vs captions: a long centred text + centred captions in one scene (the final export below
    # renders it – multi-line drawtext must work); unit checks of the layout rule
    post(f"/api/projects/{pid}/command", {"scene": scenes[4]["id"],
                                          "text": "text: Nifty 50 closes at a record high as banks and IT stocks rally into the close"})
    post(f"/api/projects/{pid}/command", {"scene": scenes[4]["id"], "text": "text center"})
    post(f"/api/projects/{pid}/command", {"scene": scenes[4]["id"], "text": "captions at center"})
    W_, H_ = 1080, 1920
    bandc = pipeline.caption_band({"cap_pos": "center"}, {}, W_, H_, None)
    tl, tfs, ty, mv = pipeline.text_layout("Nifty at record high", "center", W_, H_, bandc, round(H_ * 0.06))
    th = len(tl) * round(tfs * 1.55)
    no_hit = ty + th <= bandc[0] or ty >= bandc[1]
    lng, lfs, ly, _ = pipeline.text_layout("Nifty 50 closes at a record high as banks and IT stocks rally into the close",
                                           "top", W_, H_, None, 100)
    xl, xfs, _, _ = pipeline.text_layout("Nifty 50 closes at a record high as banks and IT stocks rally into the close "
                                         "while midcaps lag and the rupee firms against the dollar", "top", W_, H_, None, 100)
    fits = (all(len(x) * lfs * 0.56 <= W_ * 0.9 for x in lng) and len(lng) <= 3
            and all(len(x) * xfs * 0.56 <= W_ * 0.9 for x in xl) and xfs >= round(W_ * 0.038))
    bands = pipeline.caption_band({}, {}, W_, H_, 845)          # split, captions on the seam
    _, sfs, sy, smv = pipeline.text_layout("Nifty up", "center", W_, H_, bands, 845 + 38)
    _, _, by, bmv = pipeline.text_layout("Nifty up", "bottom", W_, H_, pipeline.caption_band({}, {"caption_position": "bottom"}, W_, H_, None), 100)
    check("on-screen text never sits on the captions (moves to the free side), long text wraps to <=3 lines inside the "
          "frame, split layout never pushes it onto the card",
          mv and no_hit and fits and smv and sy >= 845 + 38 and bmv and by + round(sfs * 1.55) <= H_ * 0.8 + 1,
          f"center y={ty} band={bandc} lines={lng} split y={sy} bottom y={by}")
    # speech re-check after split/trim: a silent tail turns captions off automatically, speech brings them back;
    # an explicit captions choice is never touched
    wds = [{"w": x, "s": 0.2 + i * 0.4, "e": 0.5 + i * 0.4} for i, x in enumerate("aaj market upar hai dosto".split())]
    tail = {"in": 3.0, "out": 9.0, "captions": None}
    pipeline.update_speech_flag(tail, wds, {"speech": True})
    head_ = dict(tail, **{"in": 0.0, "out": 2.5})
    pipeline.update_speech_flag(head_, wds, {"speech": True})
    expl = {"in": 3.0, "out": 9.0, "captions": True}
    pipeline.update_speech_flag(expl, wds, {"speech": True})
    check("speech re-check after split: silent tail -> captions auto-off, speech -> back to auto, explicit On kept",
          tail["no_speech"] and tail["captions"] is False and tail.get("captions_auto_off")
          and not head_["no_speech"] and head_["captions"] is None and "captions_auto_off" not in head_
          and expl["captions"] is True, f"{tail} {head_} {expl}")
    # music scene: whisper's guesses are NOT shown as words (kept in `heard`); shown again once captions are On
    sid0 = scenes[0]["id"]
    core.mutate(pid, lambda d: core.scene_by_id(d, sid0).update(no_speech=True, captions=False, captions_auto_off=True))
    v0 = next(x for x in client.get(f"/api/projects/{pid}").get_json()["scenes"] if x["id"] == sid0)
    core.mutate(pid, lambda d: core.scene_by_id(d, sid0).update(captions=True))
    v1 = next(x for x in client.get(f"/api/projects/{pid}").get_json()["scenes"] if x["id"] == sid0)
    core.mutate(pid, lambda d: core.scene_by_id(d, sid0).update(no_speech=False, captions=None, captions_auto_off=False))
    check("no-speech scene: guessed words hidden (in 'heard'), visible again when captions are switched On",
          v0["transcript"] == "" and v0.get("heard") and "caption_view" not in v0 and v1["transcript"] and not v1.get("heard"),
          f"{v0.get('transcript')!r} {v0.get('heard')!r} / {v1.get('transcript')!r}")

    # ---------------------------------------------------------------- storyboard: plan -> review -> apply
    import storyboard
    d = core.load(pid)
    s0 = d["scenes"][0]
    mid = (s0["in"] + s0["out"]) / 2
    sbd = storyboard.build(pid, {"1": {"card": {"kind": "title", "text": "test *card*"},
                                       "punch": [{"s": round(mid - 0.6, 2), "e": round(mid + 0.6, 2), "zoom": 1.1,
                                                  "text": "x"}]}})
    page = client.get(f"/storyboard/{pid}")
    sid0 = sbd["beats"][0]["scene"]
    r = post(f"/api/projects/{pid}/storyboard/{sid0}", {"status": "approved", "notes": "ok"})
    n_before = len(core.load(pid)["scenes"])
    storyboard.apply(pid)
    d = core.load(pid)
    pz = core.scene_by_id(d, sid0).get("punches") or []
    check("storyboard: dashboard served, review saved, approved punch-in = a zoom MOMENT inside the scene (NOT split "
          "into extra scenes), card set",
          page.status_code == 200 and b"Storyboard" in page.data and r.get_json()["status"] == "approved" and
          len(d["scenes"]) == n_before and len(pz) == 1 and abs(pz[0][0] - (mid - 0.6)) < 0.01 and pz[0][2] == 1.1 and
          d["scenes"][0].get("card", {}).get("text") == "test *card*", f"{len(d['scenes'])} vs {n_before}, {pz}")
    again = storyboard.apply(pid)
    r409 = post(f"/api/projects/{pid}/storyboard/{sid0}", {"status": "rejected"})
    check("storyboard apply runs ONCE per beat (no double zoom moment); applied beat is locked",
          len(core.scene_by_id(core.load(pid), sid0).get("punches") or []) == 1 and "nothing to apply" in again[0]
          and r409.status_code == 409)
    post(f"/api/projects/{pid}/undo")
    sbu = client.get(f"/api/projects/{pid}/storyboard").get_json()
    check("storyboard apply is undoable – and after undo the beat can be applied again",
          not core.scene_by_id(core.load(pid), sid0).get("punches") and sbu["beats"][0]["applied"] is False and
          "applied" in storyboard.apply(pid)[0])
    post(f"/api/projects/{pid}/undo")
    # the page's "Apply to video" button: endpoint applies approved beats without Claude, one undo step,
    # rejected-with-notes beats go to Claude; a scene trimmed in the studio after planning is NOT overwritten
    sbx = client.get(f"/api/projects/{pid}/storyboard").get_json()
    sid1 = sbx["beats"][1]["scene"]
    post(f"/api/projects/{pid}/storyboard/{sid1}", {"status": "rejected", "notes": "different card please"})
    ra = post(f"/api/projects/{pid}/storyboard/apply", {}).get_json()
    d = core.load(pid)
    inbox = [c for c in d["commands"] if c["status"] == "claude" and "beat 2 rejected: different card please" in c["text"]]
    post(f"/api/projects/{pid}/undo")
    back = len(core.load(pid)["scenes"]) == n_before
    for c in inbox:  # don't leave a Claude request behind for later checks
        post(f"/api/projects/{pid}/command/{c['id']}/cancel")
    sid2 = sbx["beats"][2]["scene"] if len(sbx["beats"]) > 2 else sid0
    post(f"/api/projects/{pid}/storyboard/{sid1}", {"status": "pending", "notes": ""})
    post(f"/api/projects/{pid}/command", {"scene": sid2, "text": "trim start 0.3"})
    stale = next(b for b in client.get(f"/api/projects/{pid}/storyboard").get_json()["beats"] if b["scene"] == sid2)
    post(f"/api/projects/{pid}/undo")
    page2 = client.get(f"/storyboard/{pid}")
    check("'Apply to video': approved beats applied by the studio (no Claude), rejected notes queued for Claude, one "
          "undo; a scene trimmed after planning is marked stale; page served from the current template",
          ra.get("ok") and ra["newly"] >= 1 and ra["rework"] == 1 and inbox and back and stale["stale"]
          and b"applyBtn" in page2.data, f"{ra} inbox={len(inbox)} back={back} stale={stale.get('stale')}")
    sb2 = storyboard.build(pid, {"1": {"card": {"kind": "title", "text": "CHANGED *card*"}}})
    check("reworked beat loses its old approval (user must see the new version)", sb2["beats"][0]["status"] == "pending"
          and sb2["beats"][0]["notes"] == "ok")
    # word cuts: partial cut shortens, a cut covering the whole scene removes it (not "keep everything")
    s0 = core.load(pid)["scenes"][0]
    part = pipeline.kept_ranges(dict(s0, cuts=[[s0["in"] + 0.5, s0["in"] + 1.0]], remove_silence=False), [], {})
    full = pipeline.kept_ranges(dict(s0, cuts=[[s0["in"] - 1, s0["out"] + 1]], remove_silence=False), [], {})
    check("word cuts: partial cut removes 0.5 s; whole-scene cut leaves nothing",
          abs(sum(b - a for a, b in part) - (s0["out"] - s0["in"] - 0.5)) < 0.01 and full == [])
    # projects analysed before pauses were stored down to 0.2 s get re-detected once
    src0 = core.load(pid)["sources"][0]["file"]
    (core.work_dir(pid) / f"{src0}.{pipeline.SILENCE_V}").unlink(missing_ok=True)
    core._atomic_write(core.work_file(pid, src0, "silences.json"), [[1.0, 1.5]])
    sil2 = pipeline.load_silences(pid, src0)
    check("legacy pause data upgraded once (finer pauses detected, marker written)",
          sil2 != [[1.0, 1.5]] and (core.work_dir(pid) / f"{src0}.{pipeline.SILENCE_V}").exists())

    # ---------------------------------------------------------------- settings validation
    r = post(f"/api/projects/{pid}/brief", {"settings": {"caption_font": "Arial, Bold{x}"}})
    check("caption font sanitised (no comma/brace)", r.status_code == 200 and
          core.load(pid)["settings"]["caption_font"] == "Arial Boldx")
    r = post(f"/api/projects/{pid}/brief", {"settings": {"caption_words": "abc", "highlight_color": "red"}})
    st = core.load(pid)["settings"]
    check("invalid settings rejected, not stored", r.status_code == 400 and st["caption_words"] == 4
          and st["highlight_color"] == "#F97316")
    # ---------------------------------------------------------------- looks: one style per reference
    with open(FIX / "phone_rot.mp4", "rb") as f1, open(FIX / "recap_test.mp4", "rb") as f2:
        client.post(f"/api/projects/{pid}/upload?kind=reference", data={"files": [(f1, "vertical_ref.mp4"), (f2, "wide_ref.mp4")]},
                    content_type="multipart/form-data")
    wait_job(pid, "ref_analyze")
    refs = core.load(pid)["references"]
    rA, rB = refs[0]["id"], refs[1]["id"]
    base_words, base_fmt = core.load(pid)["settings"]["caption_words"], core.load(pid)["settings"]["format"]
    r = post(f"/api/projects/{pid}/style_source", {"source": rA}).get_json()
    stA = core.load(pid)["settings"]
    post(f"/api/projects/{pid}/brief", {"settings": {"caption_words": 6, "language": "hi"}})
    post(f"/api/projects/{pid}/style_source", {"source": "none"})
    stN = core.load(pid)["settings"]
    post(f"/api/projects/{pid}/style_source", {"source": rA})
    stA2 = core.load(pid)["settings"]
    check("each reference keeps its own look: R1 auto 9:16 from a vertical reference, edits stay in R1, "
          "'None' keeps the simple settings, language (footage) is shared",
          stA["format"] == "9:16" and any("9:16" in n for n in r["notes"]) and stA["caption_words"] == base_words and
          stN["caption_words"] == base_words and stN["format"] == base_fmt and stN["language"] == "hi" and
          stA2["caption_words"] == 6 and stA2["format"] == "9:16", f"{stA['format']} {stN['caption_words']} {stA2['caption_words']}")
    subprocess.run([sys.executable, str(HERE / "studio_cli.py"), "look", pid, "R2", "highlight_color=\"#22C55E\"", "language=en"],
                   env={**os.environ, "CVE_ROOT": str(ROOT)}, capture_output=True)
    dd = core.load(pid)
    check("Claude can fill an inactive look (R2) without touching the active one; non-style keys refused",
          dd["looks"][rB]["highlight_color"] == "#22C55E" and dd["settings"]["highlight_color"] != "#22C55E" and
          "language" not in dd["looks"][rB] and dd["references"][1].get("studied"))
    # 🎵 background audio of a reference (voice removed): stored per look, a removed / unknown reference refused,
    # a prepared file is used without re-separating (here a stand-in tone instead of a real Demucs run)
    import refaudio
    cpa = refaudio.cache_path(pid, refs[0])
    cpa.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=330:d=4", "-c:a", "aac", str(cpa)], check=True)
    r_ra = post(f"/api/projects/{pid}/brief", {"settings": {"ref_audio": f"ref:{rA}"}})
    r_bad = post(f"/api/projects/{pid}/brief", {"settings": {"ref_audio": "ref:nosuchref"}})
    d_ra = core.load(pid)
    pv = client.get(f"/api/projects/{pid}").get_json()
    check("background audio: R1's music set on R1's look, unknown reference refused, prepared file reused (no new job)",
          r_ra.status_code == 200 and r_bad.status_code == 400 and d_ra["settings"]["ref_audio"] == f"ref:{rA}"
          and refaudio.bg_audio(pid, refaudio.ref_of(d_ra, d_ra["settings"]["ref_audio"])) == cpa
          and pv["references"][0]["bg_ready"] and (d_ra.get("job") or {}).get("kind") != "ref_audio",
          f"{r_bad.get_json()} {d_ra['settings'].get('ref_audio')}")
    post(f"/api/projects/{pid}/brief", {"settings": {"ref_audio": "none"}})
    q1 = post(f"/api/projects/{pid}/reference/{rB}/study").get_json()
    q2 = post(f"/api/projects/{pid}/reference/{rB}/study").get_json()
    check("'Ask Claude to study' queues one inbox job per reference", q1["ok"] and not q1["already"] and q2["already"])
    # page 2 edits a look the video doesn't use -> video unchanged; Scenes' Look dropdown then switches to it
    before = core.load(pid)["settings"]["caption_words"]
    post(f"/api/projects/{pid}/brief", {"look": rB, "settings": {"caption_words": 7, "face_y": 0.31}})
    dd = core.load(pid)
    lv = client.get(f"/api/projects/{pid}").get_json()["looks_view"]
    check("editing an unused look (page 2) leaves the video alone; footage keys (face position) stay project-wide",
          dd["settings"]["caption_words"] == before and dd["looks"][rB]["caption_words"] == 7 and
          dd["settings"]["face_y"] == 0.31 and lv[rB]["caption_words"] == 7 and lv[rA]["caption_words"] == before)
    post(f"/api/projects/{pid}/brief", {"look": rB, "look_brief": "high energy, fast cuts", "brief": "IPO reel, 30 s"})
    post(f"/api/projects/{pid}/brief", {"look": "none", "look_brief": "calm and simple"})
    dd = core.load(pid)
    check("each look has its own brief; 'about this video' is shared",
          dd["look_briefs"][rB] == "high energy, fast cuts" and dd["look_briefs"]["none"] == "calm and simple"
          and dd["brief"] == "IPO reel, 30 s")
    post(f"/api/projects/{pid}/style_source", {"source": rB})
    check("choosing that look in Scenes applies it to the video", core.load(pid)["settings"]["caption_words"] == 7
          and core.load(pid)["settings"]["highlight_color"] == "#22C55E")
    post(f"/api/projects/{pid}/style_source", {"source": rA})
    post(f"/api/projects/{pid}/reference/remove", {"id": rA})
    dd = core.load(pid)
    check("removing the ACTIVE reference returns to the simple settings and drops its look",
          dd["style_source"] == "none" and dd["settings"]["format"] == base_fmt and rA not in dd["looks"])
    r1 = post(f"/api/projects/{pid}/command", {"scene": s1, "text": "captions hinglish; spell Market as MARKET"})
    r2 = post(f"/api/projects/{pid}/brief", {"settings": {"caption_fixes": ["not", "a", "dict"]}})
    r3 = post(f"/api/projects/{pid}/command", {"scene": None, "text": "fix audio to be louder"}).get_json()
    st = core.load(pid)["settings"]
    check("caption script + spell commands (from a scene box) stored; bad fixes rejected",
          st["caption_script"] == "hinglish" and st["caption_fixes"] == {"market": "MARKET"}
          and r2.status_code == 400 and r3["queued"] == ["fix audio to be louder"], str(r1.get_json()))
    view = client.get(f"/api/projects/{pid}").get_json()["scenes"][0]
    check("page shows what the captions will say", "caption_view" in view, view.get("caption_view", "")[:60])
    post(f"/api/projects/{pid}/command", {"scene": None, "text": "captions hindi; clear spellings"})
    st = core.load(pid)["settings"]
    check("back to Hindi script + spellings cleared", st["caption_script"] == "original" and st["caption_fixes"] == {})

    # ---------------------------------------------------------------- worker survives a vanished project
    ghost = post("/api/projects", {"name": "Ghost"}).get_json()["id"]
    shutil.rmtree(core.STORY / ghost)
    appmod._jobs.put((ghost, "analyze", {}))
    time.sleep(2)

    # ---------------------------------------------------------------- render + A/V sync
    dup = post(f"/api/projects/{pid}/command", {"scene": scenes[1]["id"], "text": "duplicate"}).get_json()["new"][0]
    post(f"/api/projects/{pid}/render", {"kind": "final"})
    j = wait_job(pid, "final")
    check("final export renders", j["status"] == "done", j.get("error") or j.get("progress"))
    out = core.EXPORTS / core.load(pid)["renders"][-1]["file"]
    rec = core.load(pid)["renders"][-1]
    tms = rec.get("times") or []
    check("render records REAL per-scene start/length (sum = file length +-0.15 s, starts increasing)",
          tms and abs(sum(x[2] for x in tms) - rec["duration"]) < 0.15 and all(tms[i][1] < tms[i + 1][1] for i in range(len(tms) - 1)),
          f"{tms} vs {rec['duration']}")
    check("a duplicated scene plays twice in the export", dup in [x[0] for x in tms] and scenes[1]["id"] in [x[0] for x in tms], str(tms))
    # a second waiting preview/export is dropped (auto-preview from two tabs, double clicks)
    core.mutate(pid, lambda d: d.update(job={"kind": "analyze", "status": "running", "pending": ["preview"], "log": []}))
    appmod.enqueue(pid, "preview")
    pend = core.load(pid)["job"]["pending"]
    core.mutate(pid, lambda d: d.update(job={"kind": "final", "status": "done", "pending": [], "log": []}))
    check("only ONE waiting preview is queued", pend == ["preview"] and appmod._jobs.qsize() == 0, f"{pend} q={appmod._jobs.qsize()}")

    def sdur(path, sel):
        o = subprocess.run([core.FFPROBE, "-v", "error", "-select_streams", sel, "-show_entries", "stream=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
        return float(o or 0)

    diffs = [abs(sdur(p, "a:0") - sdur(p, "v:0")) * 1000 for p in (core.work_dir(pid) / "segs").glob("*.mp4")]
    check("every segment: audio length == video length (<=25 ms)", diffs and max(diffs) <= 25,
          f"max {max(diffs):.0f} ms over {len(diffs)} segments")
    fd = abs(sdur(out, "a:0") - sdur(out, "v:0")) * 1000
    check("final export A/V lengths match (<=40 ms)", fd <= 40, f"{fd:.0f} ms")
    check("worker thread survived a job on a deleted project", j["status"] == "done")
    wdp = core.work_dir(pid)
    keep = set()
    for k in ("preview", "final"):
        f = wdp / f"segs_{k}.txt"
        if f.exists():
            keep.update(x for x in f.read_text(encoding="utf-8").splitlines() if x)
    on_disk = {p.name for p in (wdp / "segs").glob("seg_*.mp4")}
    check("segment cache pruned to latest preview + final", on_disk <= keep, f"{len(on_disk)} files, {len(keep)} in use")
    check("no half-written .part files left in Exports", not list(core.EXPORTS.glob("*.part.mp4")))

    # ---------------------------------------------------------------- Export dialog: platform / frame / size / type
    def streams(path):
        o = subprocess.run([core.FFPROBE, "-v", "error", "-show_entries",
                            "stream=codec_type,codec_name,codec_tag_string,width,height,r_frame_rate:format=format_name,duration",
                            "-of", "json", str(path)], capture_output=True, text=True).stdout
        return json.loads(o or "{}")

    st_before = dict(core.load(pid)["settings"])
    ver_before = {r.get("ver") for r in core.load(pid)["renders"] if r.get("kind") == "final"}
    xp_runs = [({"platform": "youtube", "res": "480"}, "mp4", ("h264",), (854, 480)),       # vertical edit -> 16:9 blur
               ({"platform": "custom", "frame": "4:5", "res": "480", "fmt": "mov", "fps": "60"}, "mov", ("h264",), (480, 600)),
               ({"platform": "reel", "res": "480", "fmt": "mp4_hevc", "quality": "standard"}, "mp4", ("hevc",), None),
               ({"platform": "x", "res": "480", "fmt": "webm"}, "webm", ("vp9", "opus"), None),
               ({"platform": "custom", "res": "480", "fmt": "mov_prores"}, "mov", ("prores", "pcm_s16le"), None),
               ({"platform": "linkedin", "res": "480", "fmt": "mkv"}, "mkv", ("h264",), None),
               ({"platform": "edit", "fmt": "mp3"}, "mp3", ("mp3",), "audio")]
    xp_res = []
    for opts_, ext_, codecs_, size_ in xp_runs:
        r_ = post(f"/api/projects/{pid}/render", {"kind": "final", "opts": opts_})
        j = wait_job(pid, "final")
        rec = core.load(pid)["renders"][-1]
        f_ = core.EXPORTS / rec["file"]
        s_ = streams(f_) if f_.is_file() else {}
        cods = {x.get("codec_name") for x in s_.get("streams", [])}
        vs = next((x for x in s_.get("streams", []) if x.get("codec_type") == "video"), {})
        ok = (r_.status_code == 200 and j["status"] == "done" and f_.suffix == "." + ext_ and set(codecs_) <= cods
              and (size_ is None or size_ == "audio" and not vs or size_ == (vs.get("width"), vs.get("height")))
              and abs(float((s_.get("format") or {}).get("duration") or 0) - core.load(pid)["renders"][-2]["duration"]) < 0.5
              and rec.get("summary") and rec.get("opts", {}).get("platform") == opts_["platform"])
        xp_res.append((opts_.get("fmt", "mp4"), ok, rec["file"], sorted(c for c in cods if c), vs.get("width"), vs.get("height"),
                       vs.get("r_frame_rate"), j.get("error")))
    check("Export dialog: YouTube 16:9 / 4:5 MOV 60 fps / H.265 / WebM / ProRes / MKV / MP3 each make the right file "
          "(container, codecs, size, length)", all(x[1] for x in xp_res), str([x for x in xp_res if not x[1]] or xp_res[:2]))
    recs_ = [r for r in core.load(pid)["renders"] if r.get("opts")]
    hev = next((streams(core.EXPORTS / r["file"]) for r in recs_ if r["opts"]["fmt"] == "mp4_hevc"), {})
    check("H.265 MP4 is tagged hvc1 (plays on iPhone / Mac)",
          any(x.get("codec_tag_string") == "hvc1" for x in hev.get("streams", [])), str(hev.get("streams")))
    f60 = next((streams(core.EXPORTS / r["file"]) for r in recs_ if r["opts"]["fmt"] == "mov"), {})
    check("fps choice applies (60 fps MOV)", any(x.get("r_frame_rate") == "60/1" for x in f60.get("streams", [])), str(f60))
    check("exports of ONE edit share one version number; names say platform + size",
          len({r.get("ver") for r in recs_}) == 1 and {r.get("ver") for r in recs_} <= ver_before  # = the plain export above (same edit)
          and any("_youtube_854x480." in r["file"] for r in recs_) and any(r["file"].endswith("_edit_audio.mp3") for r in recs_),
          str([r["file"] for r in recs_]))
    check("player only uses exports that look like the edit (16:9 / MP3 / ProRes flagged)",
          all(r.get("matches_edit") == (r["opts"]["frame"] == exportopts.edit_frame(core.load(pid)) and r["opts"]["fps"] == "edit")
              for r in recs_ if r["opts"]["fmt"] != "mp3") and
          all(r.get("web") is False for r in recs_ if r["opts"]["fmt"] in ("mov_prores", "mkv", "mp4_hevc")) and
          all(r.get("audio_only") for r in recs_ if r["opts"]["fmt"] == "mp3"), str([(r["file"], r.get("matches_edit"), r.get("web")) for r in recs_]))
    st_after = core.load(pid)["settings"]
    check("an export never changes the project's own settings (format, fps, fit)",
          all(st_after.get(k) == st_before.get(k) for k in ("format", "fps", "fit", "layout")) and "_vq" not in st_after
          and "_face_crop" not in st_after, f"{[(k, st_before.get(k), st_after.get(k)) for k in ('format', 'fps', 'fit')]}")
    check("no half-written .part files after converted exports", not [p for p in core.EXPORTS.iterdir() if ".part" in p.name])
    pl_ = post(f"/api/projects/{pid}/export_plan", {"opts": {"platform": "nope", "frame": "7:3", "res": "99999", "fmt": "exe;rm",
                                                            "fps": "1000", "quality": "ultra"}, "seconds": "x"}).get_json()
    pl4 = post(f"/api/projects/{pid}/export_plan", {"opts": {"platform": "reel", "res": "2160"}, "seconds": 200}).get_json()
    check("export plan: junk values fall back to safe defaults; 4K Reel / too long / big file warned",
          pl_["opts"]["platform"] == "edit" and pl_["opts"]["fmt"] == "mp4" and pl_["opts"]["res"] in ("1080", "source")
          and pl_["opts"]["fps"] == "edit" and pl_["opts"]["quality"] == "high" and pl_["opts"]["frame"] in ("9:16", "16:9", "1:1", "4:5")
          and pl4["w"] == 2160 and pl4["h"] == 3840 and any("3:00" in w for w in pl4["warnings"])
          and any("1080 wide" in w for w in pl4["warnings"]), f"{pl_['opts']} {pl4['warnings']}")
    # queue: the SAME export clicked twice waits once; a DIFFERENT one (Reel, then YouTube) waits too
    core.mutate(pid, lambda d: d.update(job={"kind": "analyze", "status": "running", "pending": [], "log": []}))
    real_q, appmod._jobs = appmod._jobs, queue.Queue()  # the worker stays blocked on the real queue: nothing renders
    try:
        for o_ in ({"platform": "reel"}, {"platform": "reel"}, {"platform": "youtube"}):
            appmod.enqueue(pid, "final", opts=o_)
        q1, q0 = appmod._jobs.qsize(), 0
    finally:
        appmod._jobs = real_q
        appmod._pend_final.pop(pid, None)
    core.mutate(pid, lambda d: d.update(job={"kind": "final", "status": "done", "pending": [], "log": []}))
    check("export queue: identical export dropped, a different one kept", q1 - q0 == 2, f"{q1 - q0} queued")

    # ---------------------------------------------------------------- GPU encoder failure -> whole render on CPU
    real_run = pipeline.run

    def nvenc_broken(cmd, *a, **k):
        if "h264_nvenc" in [str(c) for c in cmd]:
            raise pipeline.PipelineError("simulated: NVENC unavailable")
        return real_run(cmd, *a, **k)
    pipeline.run = nvenc_broken
    try:
        post(f"/api/projects/{pid}/command", {"scene": scenes[0]["id"], "text": "zoom 1.1"})  # force fresh segments
        post(f"/api/projects/{pid}/render", {"kind": "preview"})
        j = wait_job(pid, "preview")
    finally:
        pipeline.run = real_run
    log = "\n".join(j.get("log", []))
    check("NVENC failure -> every segment re-rendered on CPU (no mixed encoders)",
          j["status"] == "done" and "re-rendering on the CPU" in log, j.get("error") or "")

    # ---------------------------------------------------------------- ✨ Library: transitions, effects, stickers, sounds
    import library
    library._cache["sig"] = None
    cat0 = client.get("/api/library").get_json()
    check("library catalog: transitions + effects built in, empty packs = not ready (page offers the download)",
          len(cat0["transitions"]) >= 40 and len(cat0["effects"]) >= 15 and not cat0["ready"], f"{len(cat0['transitions'])} {cat0['ready']}")
    library.MY_STICKERS.mkdir(parents=True, exist_ok=True)
    library.MY_SOUNDS.mkdir(parents=True, exist_ok=True)
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xFF00FF:s=200x200", "-frames:v", "1",
                    str(library.MY_STICKERS / "magenta box.png")], check=True)
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=1000:d=0.3", "-af", "volume=0.9",
                    str(library.MY_SOUNDS / "beep.wav")], check=True)
    library._cache["sig"] = None
    dl = core.load(pid)
    vl = [x for x in dl["scenes"] if x.get("keep", True) and x.get("role") != "broll"]
    l1, l2 = vl[1]["id"], vl[2]["id"]
    rr = [post(f"/api/projects/{pid}/command", {"scene": sid_, "text": c}).get_json()["replies"][0] for sid_, c in
          [(l1, "transition slide left 0.5"), (l2, "effect black and white"), (l2, "sticker magenta box at 0.2 for 3 center size 30% still"),
           (l2, "sfx beep at 0.6"), (l2, "sticker 1 top-left"), (l2, "sfx nope-nope"), (l1, "transition nosuch")]]
    dl = core.load(pid)
    s2_ = core.scene_by_id(dl, l2)
    check("library commands: transition / effect / sticker / sound stored; unknown names refused",
          core.scene_by_id(dl, l1)["transition"]["type"] == "slideleft" and s2_["effects"] == ["bw"]
          and s2_["stickers"][0]["pos"] == "top-left" and s2_["sfx"][0]["at"] == 0.6
          and rr[5].startswith("!") and rr[6].startswith("!"), str(rr))
    post(f"/api/projects/{pid}/command", {"scene": l2, "text": "sticker 1 center"})
    fake = [{"kind": "final", "file": f"old_final_{i}.mp4", "created": "2026-01-01T00:00:00"} for i in range(3)] + \
           [{"kind": "preview", "file": f"pv_{i}.mp4", "created": "2026-01-02T00:00:00"} for i in range(40)]
    core.mutate(pid, lambda d: d.update(renders=fake + d.get("renders", [])))
    post(f"/api/projects/{pid}/render", {"kind": "preview"})
    jl = wait_job(pid, "preview")
    rk = core.load(pid)["renders"]
    check("render history keeps EVERY final export (auto-preview no longer pushes them off the Exports page)",
          all(any(r["file"] == f"old_final_{i}.mp4" for r in rk) for i in range(3))
          and sum(1 for r in rk if r["kind"] != "final") <= 5, f"{len(rk)} records")
    recl = core.load(pid)["renders"][-1]
    fl = core.work_dir(pid) / recl["file"]
    ok_l = jl["status"] == "done" and fl.exists()
    if ok_l:
        tl = {i: (a_, d_) for i, a_, d_ in recl["times"]}
        mm = pipeline.probe(fl)

        def lpx(t, fx, fy):
            b_ = subprocess.run([core.FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(fl), "-frames:v", "1", "-vf",
                                 f"format=rgb24,crop=1:1:{int(mm['width'] * fx)}:{int(mm['height'] * fy)}", "-f", "rawvideo",
                                 "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
            return tuple(b_[:3])
        t2 = tl[l2][0]
        stk_px, grey_px = lpx(t2 + 1.0, 0.5, 0.45), lpx(t2 + 1.0, 0.1, 0.9)

        def peak(t0, t1):
            o_ = subprocess.run([core.FFMPEG, "-v", "info", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", str(fl), "-af",
                                 "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
            m_ = _re.search(r"max_volume: (-?[\d.]+)", o_)
            return float(m_.group(1)) if m_ else -99
        beep_db = peak(t2 + 0.6, t2 + 0.85)
        sum_t = sum(d_ for _, d_ in tl.values())
    check("library render: lengths unchanged by the transition, sticker on top, black & white picture, beep at its time",
          ok_l and abs(mm["duration"] - sum_t) < 0.08 and stk_px[0] > 200 and stk_px[1] < 70 and stk_px[2] > 200
          and abs(grey_px[0] - grey_px[1]) < 12 and abs(grey_px[1] - grey_px[2]) < 12 and beep_db > -12
          and not any("could not be added" in x for x in jl.get("log", [])),
          (jl.get("error") or "") + (f" dur {mm['duration']} vs {sum_t:.3f} stk {stk_px} grey {grey_px} beep {beep_db}" if ok_l else ""))
    # a broken sound file must not cost the render: it goes on without transitions/sounds and says so
    (library.MY_SOUNDS / "beep.wav").write_bytes(b"RIFF not really a wav" * 20)
    post(f"/api/projects/{pid}/render", {"kind": "preview"})
    jb2 = wait_job(pid, "preview")
    sc2v = next(x for x in client.get(f"/api/projects/{pid}").get_json()["scenes"] if x["id"] == l2)
    check("broken library sound -> skipped (transition still made), the scene shows it as missing",
          jb2["status"] == "done" and not any("could not be added" in x for x in jb2.get("log", []))
          and "beep" in (sc2v.get("lib_missing") or []), (jb2.get("error") or "") + str(sc2v.get("lib_missing")))
    rs = [post(f"/api/projects/{pid}/command", {"scene": l2, "text": c}).get_json() for c in ("sound off", "sound on", "sticker 1")]
    check("'sound off/on' = mute/unmute (never a library sound); 'sticker 1' never adds the keycap emoji",
          rs[0]["replies"] == ["Muted."] and rs[1]["replies"] == ["Unmuted."] and rs[2]["replies"][0].startswith("!"), str(rs))
    for sid_, c in ((l1, "transition off"), (l2, "effects off"), (l2, "stickers off"), (l2, "sfx off")):
        post(f"/api/projects/{pid}/command", {"scene": sid_, "text": c})

    # ---------------------------------------------------------------- timeline TRACKS (connected to scenes)
    def tk(body):
        return post(f"/api/projects/{pid}/tracks", body)
    blue_png, tone = ROOT / "blue card.png", ROOT / "tone.wav"
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x1030E0:s=640x360", "-frames:v", "1", str(blue_png)], check=True)
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=700:d=2", "-af", "volume=0.9", str(tone)], check=True)
    ups = [client.post(f"/api/projects/{pid}/upload?kind=asset", data={"files": (open(f_, "rb"), f_.name)},
                       content_type="multipart/form-data").get_json() for f_ in (blue_png, tone)]
    dt = core.load(pid)
    vt = [x for x in dt["scenes"] if x.get("keep", True) and x.get("role") != "broll"]
    d0 = pipeline.scene_out_len(vt[0], pipeline.load_silences(pid, vt[0]["src"]), dt["settings"])
    tb_ = tk({"op": "add", "kind": "broll"}).get_json()["track"]
    tt_ = tk({"op": "add", "kind": "text"}).get_json()["track"]
    ta_ = tk({"op": "add", "kind": "audio"}).get_json()["track"]
    r_it = tk({"op": "item_add", "track": tb_, "item": {"anchor": vt[0]["id"], "off": d0 - 0.6, "dur": 1.4,
                                                        "src": {"t": "asset", "n": "blue card.png"}, "where": "full"}}).get_json()
    tk({"op": "item_add", "track": tt_, "item": {"anchor": vt[1]["id"], "off": 1.2, "dur": 1.0, "text": "Track title", "style": "top"}})
    tk({"op": "item_add", "track": ta_, "item": {"anchor": vt[2]["id"], "off": 0.5, "dur": 1.0, "src": {"t": "asset", "n": "tone.wav"}, "vol": 1}})
    bad = [tk(b_).status_code for b_ in (
        {"op": "item_add", "track": tb_, "item": {"anchor": "nope", "off": 0, "dur": 1, "src": {"t": "asset", "n": "blue card.png"}}},
        {"op": "item_add", "track": tb_, "item": {"anchor": vt[0]["id"], "off": 0, "dur": 1, "src": {"t": "asset", "n": "tone.wav"}}},
        {"op": "item_add", "track": ta_, "item": {"anchor": vt[0]["id"], "off": 0, "dur": 1, "src": {"t": "asset", "n": "blue card.png"}}},
        {"op": "item_add", "track": tb_, "item": {"anchor": vt[0]["id"], "off": 0, "dur": 1, "src": {"t": "clip", "n": "../../x.mp4"}}},
        {"op": "add", "kind": "weird"})]
    dtk = core.load(pid)
    check("tracks: add Video/B-roll/Audio/Text tracks + items; bad scene / wrong file type / unknown file refused",
          all(u.get("file") for u in ups) and r_it.get("item") and len(dtk["tracks"]) == 3 and bad == [400] * 5
          and sum(len(t_["items"]) for t_ in dtk["tracks"]) == 3, f"{ups} {bad}")
    post(f"/api/projects/{pid}/render", {"kind": "preview"})
    jt = wait_job(pid, "preview")
    rect = core.load(pid)["renders"][-1]
    ft = core.work_dir(pid) / rect["file"]
    if jt["status"] == "done":
        tmt = {i: (a_, d_) for i, a_, d_ in rect["times"]}
        mt = pipeline.probe(ft)

        def tpx(t, fx=0.08, fy=0.5):
            b_ = subprocess.run([core.FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(ft), "-frames:v", "1", "-vf",
                                 f"format=rgb24,crop=1:1:{int(mt['width'] * fx)}:{int(mt['height'] * fy)}", "-f", "rawvideo",
                                 "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
            return tuple(b_[:3])
        s1_ = tmt[vt[1]["id"]][0]
        isblue = lambda c: c and c[2] > 150 and c[0] < 80  # noqa: E731
        bt = [tpx(s1_ - 0.3), tpx(s1_ + 0.5), tpx(s1_ + 1.2)]

        def tpeak(t0, t1):
            o_ = subprocess.run([core.FFMPEG, "-v", "info", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", str(ft), "-af",
                                 "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
            m_ = _re.search(r"max_volume: (-?[\d.]+)", o_)
            return float(m_.group(1)) if m_ else -99
        s2t = tmt[vt[2]["id"]][0]
        tone_db = tpeak(s2t + 0.55, s2t + 1.4)
    check("track render: one B-roll item flows across a scene cut (both sides) and ends on time; audio item at its time; length unchanged",
          jt["status"] == "done" and isblue(bt[0]) and isblue(bt[1]) and not isblue(bt[2]) and tone_db > -10
          and abs(mt["duration"] - sum(d_ for _, d_ in tmt.values())) < 0.08,
          (jt.get("error") or "") + (f" px {bt} tone {tone_db}" if jt["status"] == "done" else ""))
    ma_snd = post(f"/api/projects/{pid}/media_assets", {"src": {"t": "asset", "n": "tone.wav"}}).get_json()
    ma_vid = post(f"/api/projects/{pid}/media_assets", {"src": {"t": "clip", "n": vt[0]["src"]}}).get_json()
    ma_bad = post(f"/api/projects/{pid}/media_assets", {"src": {"t": "asset", "n": "../project.json"}})
    check("track blocks get the clip's look: waveform for a sound, film strip + waveform for a video; bad path refused",
          ma_snd.get("wave") and not ma_snd.get("strip") and ma_vid.get("strip") and ma_bad.status_code == 404
          and (core.work_dir(pid) / "thumbs" / ma_snd["wave"]).is_file(), f"{ma_snd} {ma_vid}")
    dep0 = core.history_depth(pid)
    tk({"op": "toggle", "track": tb_})
    hidden_ok = core.load(pid)["tracks"][0]["off"] is True
    tk({"op": "remove", "track": tt_})
    gone_t = len(core.load(pid)["tracks"]) == 2
    post(f"/api/projects/{pid}/undo")
    back_t = len(core.load(pid)["tracks"]) == 3
    rdel_t = post(f"/api/projects/{pid}/broll_asset/remove", {"file": "blue card.png"}).get_json()
    no_item = not core.load(pid)["tracks"][0]["items"]
    post(f"/api/projects/{pid}/undo")
    check("tracks: hide, delete + Undo brings the track back; deleting an upload removes its items (Undo restores)",
          hidden_ok and gone_t and back_t and no_item and core.load(pid)["tracks"][0]["items"],
          f"{rdel_t} hidden {hidden_ok} gone {gone_t} back {back_t} no_item {no_item}")  # (undo history is capped at 30)
    # a scene dragged UP from the Video lane onto a Video track: leaves the main line, plays on the track; Undo restores
    tv_ = tk({"op": "add", "kind": "video"}).get_json()["track"]
    dv = core.load(pid)
    vv = [x for x in dv["scenes"] if x.get("keep", True) and x.get("role") != "broll"]
    r_lift = tk({"op": "scene_to_track", "track": tv_, "scene": vv[1]["id"], "t": 0.5}).get_json()
    dl_ = core.load(pid)
    lifted = core.scene_by_id(dl_, vv[1]["id"])
    itl = next(t_ for t_ in dl_["tracks"] if t_["id"] == tv_)["items"]
    r_txt = tk({"op": "scene_to_track", "track": tt_, "scene": vv[2]["id"], "t": 0.5})
    post(f"/api/projects/{pid}/undo")
    du_ = core.load(pid)
    check("scene dragged onto a Video track: leaves the main line, its clip plays there (connected at the drop spot); "
          "Text track refused; Undo puts it back",
          r_lift.get("item") and lifted["keep"] is False and len(itl) == 1 and itl[0]["src"]["n"] == vv[1]["src"]
          and itl[0]["anchor"] == vv[0]["id"] and r_txt.status_code == 400
          and core.scene_by_id(du_, vv[1]["id"]).get("keep", True) is True
          and not next(t_ for t_ in du_["tracks"] if t_["id"] == tv_)["items"], str(r_lift))
    # ...and dragged back DOWN onto Video 1: the same scene returns (captions etc.), at the drop spot
    dvb = core.load(pid)
    itb = next(t_ for t_ in dvb["tracks"] if t_["id"] == tv_)["items"]
    rb_lift = tk({"op": "scene_to_track", "track": tv_, "scene": vv[1]["id"], "t": 0.5}).get_json()
    r_back = tk({"op": "item_to_scene", "track": tv_, "item": rb_lift["item"], "before": "end"}).get_json()
    d_back = core.load(pid)
    sb_ = core.scene_by_id(d_back, vv[1]["id"])
    vis_b = [x["id"] for x in d_back["scenes"] if x.get("keep", True) and x.get("role") != "broll"]
    r_up = tk({"op": "item_to_scene", "track": tt_, "item": "nope", "before": "end"})
    check("block dragged back DOWN onto Video 1 restores the SAME scene (kept, at the drop spot), its track item gone",
          not itb and r_back.get("scene") == vv[1]["id"] and not r_back.get("new_scene") and sb_.get("keep", True)
          and vis_b[-1] == vv[1]["id"] and not next(t_ for t_ in d_back["tracks"] if t_["id"] == tv_)["items"]
          and r_up.status_code == 400, f"{r_back}")
    post(f"/api/projects/{pid}/undo")
    post(f"/api/projects/{pid}/undo")
    # editor controls: lock a track (edits refused, not an undo step), lane switches (Video 1 mute / B-roll + cards hide,
    # undoable), a drop on the empty zone makes the track in the same step
    tk({"op": "lock", "track": tv_})
    r_lk = tk({"op": "item_add", "track": tv_, "item": {"anchor": vv[0]["id"], "off": 0, "dur": 1, "src": {"t": "asset", "n": "blue card.png"}}})
    tk({"op": "lock", "track": tv_})
    m0 = core.load(pid)["settings"].get("main_mute")
    tk({"op": "lane", "lane": "main", "what": "mute"})
    m1 = core.load(pid)["settings"].get("main_mute")
    post(f"/api/projects/{pid}/undo")
    m2 = core.load(pid)["settings"].get("main_mute")
    tk({"op": "lane", "lane": "main", "what": "lock"})
    lk_main = core.load(pid).get("ui_locks", {}).get("main")
    tk({"op": "lane", "lane": "main", "what": "lock"})
    n_tr = len(core.load(pid)["tracks"])
    r_new = tk({"op": "item_add", "new_kind": "broll", "item": {"anchor": vv[0]["id"], "off": 0.2, "dur": 1,
                                                                "src": {"t": "asset", "n": "blue card.png"}}}).get_json()
    d_new = core.load(pid)
    check("editor controls: locked track refuses edits; Video 1 mute toggles (Undo restores); lane lock kept; "
          "drop on the empty zone = new track + item in one step",
          r_lk.status_code == 400 and not m0 and m1 is True and not m2 and lk_main is True
          and len(d_new["tracks"]) == n_tr + 1 and r_new.get("track") and r_new.get("item")
          and d_new["tracks"][-1]["kind"] == "broll" and len(d_new["tracks"][-1]["items"]) == 1, f"{r_lk.get_json()} {r_new}")
    for t_ in list(core.load(pid)["tracks"]):
        tk({"op": "remove", "track": t_["id"]})

    # ---------------------------------------------------------------- ✨ auto-edit (one click, own edits kept, one undo)
    post(f"/api/projects/{pid}/command", {"scene": vt[0]["id"], "text": "sticker magenta box at 0.1"})  # the user's own
    dae0 = core.load(pid)
    own0 = [len(s.get("stickers") or []) for s in dae0["scenes"]]
    r_ae = post(f"/api/projects/{pid}/autoedit", {"level": "energetic"}).get_json()
    dae1 = core.load(pid)
    autos = sum(1 for s in dae1["scenes"] for x in (s.get("stickers") or []) + (s.get("sfx") or []) if x.get("auto"))
    zooms = sum(len(s.get("auto_punch") or []) + len(s.get("auto_fx") or []) for s in dae1["scenes"])
    r_ae2 = post(f"/api/projects/{pid}/autoedit", {"level": "energetic"}).get_json()  # again: replaces, never stacks
    autos2 = sum(1 for s in core.load(pid)["scenes"] for x in (s.get("stickers") or []) + (s.get("sfx") or []) if x.get("auto"))
    zooms2 = sum(len(s.get("auto_punch") or []) + len(s.get("auto_fx") or []) for s in core.load(pid)["scenes"])
    r_clr = post(f"/api/projects/{pid}/autoedit", {"action": "clear"}).get_json()
    dae2 = core.load(pid)
    check("auto-edit: runs on every scene with a report; running again replaces (no stacking); clear removes only its "
          "own work – the user's sticker stays",
          r_ae.get("ok") and len(r_ae.get("report") or []) >= 3 and autos2 == autos and r_ae2.get("ok")
          and zooms2 == zooms and zooms > 0
          and [len(s.get("stickers") or []) for s in dae2["scenes"]] == own0
          and not any(x.get("auto") for s in dae2["scenes"] for x in (s.get("stickers") or []) + (s.get("sfx") or []))
          and "auto_edit" not in dae2, f"{r_ae.get('reply')} | {r_clr.get('reply')} | autos {autos}/{autos2} zooms+fx {zooms}/{zooms2}")
    post(f"/api/projects/{pid}/command", {"scene": vt[0]["id"], "text": "stickers off"})

    # ---------------------------------------------------------------- 💬 suggestions (plain words, many scenes, one undo)
    import suggest
    pr = [suggest.parse(t_, 7) for t_ in (
        "in the scene six I want calm music. And scene five I want money sticker and cash sound",
        "add a fire sticker in scene 2 and a whoosh sound in scene 4", "money sticker in scene 5 and 6",
        "scenes 2-4 black and white effect", "scene no. 3 sticker on 'double'", "scene 1 purple\nremove silences",
        "last scene: thumbs up sticker. also bell sound", "5th scene shake effect")]
    check("suggestions: notes split per scene (scene six / scene no. 3 / 5th scene / 2-4 / 5 and 6 / last; request "
          "before or after the scene; a new line without a scene = whole video)",
          pr[0] == [([6], "i want calm music"), ([5], "i want money sticker and cash sound")]
          and pr[1] == [([2], "add a fire sticker in"), ([4], "and a whoosh sound in")] and pr[2] == [([5, 6], "money sticker in")]
          and pr[3] == [([2, 3, 4], "black and white effect")] and pr[4] == [([3], "sticker on 'double'")]
          and pr[5] == [([1], "purple"), ([], "remove silences")] and pr[6] == [([7], "thumbs up sticker"), ([7], "also bell sound")]
          and pr[7] == [([5], "shake effect")] and suggest._atoms("money sticker and cash sound, no transition")
          == ["money sticker", "cash sound", "no transition"] and suggest._atoms("black and white effect") == ["black and white effect"]
          and suggest.parse('scene 2 text: Big "News" & More. Today', 7) == [([2], 'text: big "news" & more. today')]
          and suggest._atoms('text: big "news" & more. today') == ['text: big "news" & more. today']
          and suggest.FILLER_RX.sub(" ", "scene 7 follow sound accordingly, deep think as world best editor").split() == ["scene", "7", "follow", "sound", ","]
          and bool(suggest.CONTEXT_START.match("when page ko follow karna mat bhulna"))
          and not suggest.ACTION_RX.search("page ko follow karna mat bhulna") and suggest.ACTION_RX.search("follow button sound"),
          str(pr))
    dsg = core.load(pid)
    n1, n2 = dsg["scenes"].index(vt[0]) + 1, dsg["scenes"].index(vt[1]) + 1
    nq0 = sum(1 for c in dsg["commands"] if c["status"] == "claude")
    r_sg = post(f"/api/projects/{pid}/suggest", {"text": f"scene {n1}: magenta sticker at 0.3 and beep sound\n"
                                                          f"scene {n2} shake effect, no transition\nscene 99 fire sticker\n"
                                                          f"scene {n1} make it purple sparkly vibes"}).get_json()
    dsg1 = core.load(pid)
    s1_, s2s = dsg1["scenes"][n1 - 1], dsg1["scenes"][n2 - 1]
    oks = [x["ok"] for x in r_sg.get("results") or []]
    post(f"/api/projects/{pid}/undo")
    dsg2 = core.load(pid)
    check("suggestions: sticker + its sound at the asked time, effect on another scene, unknown scene refused, the rest "
          "sent to Claude; ONE undo removes it all",
          r_sg.get("ok") and r_sg.get("changed") and [x.get("name") for x in s1_.get("stickers") or []] == ["magenta box"]
          and (s1_["stickers"][0]["at"] == 0.3) and [x.get("name") for x in s1_.get("sfx") or []] == ["beep"]
          and s1_["sfx"][0]["at"] == 0.3 and "shake" in (s2s.get("effects") or []) and oks.count(False) == 1 and None in oks
          and sum(1 for c in dsg1["commands"] if c["status"] == "claude") == nq0 + 1 and dsg1.get("suggestions")
          and not dsg2["scenes"][n1 - 1].get("stickers") and "shake" not in (dsg2["scenes"][n2 - 1].get("effects") or []),
          f"{[(x['scene'], x['text'], x['reply']) for x in r_sg.get('results') or []]}")
    r_ob = post(f"/api/projects/{pid}/suggest", {"text": f"scene {n1} text: Big News Today\nhighlight #22C55E\n"
                                                          f"scene {n1} magenta sticker. also beep sound"}).get_json()
    dob = core.load(pid)
    s_ob = dob["scenes"][n1 - 1]
    post(f"/api/projects/{pid}/undo")
    check("one box: a line without a scene = whole-video command; capitals kept as typed; a named sound replaces "
          "the one picked for the sticker (no double hit)",
          r_ob.get("ok") and s_ob.get("text") == "Big News Today" and all(x["ok"] for x in r_ob["results"])
          and [x["name"] for x in s_ob.get("sfx") or []] == ["beep"],
          f"{[(x['scene'], x['text'], x['reply'][:60]) for x in r_ob.get('results') or []]} {s_ob.get('text')}")

    # ---------------------------------------------------------------- 📁 media tab: drop anything, sorted automatically
    import media as media_lib
    mfx = ROOT / "_media_fx"
    mfx.mkdir(exist_ok=True)
    gen = [("tick tone.wav", ["-f", "lavfi", "-i", "sine=f=1500:d=0.4"]),
           ("calm bed.mp3", ["-f", "lavfi", "-i", "sine=f=220:d=30"]),
           ("logo cut.png", ["-f", "lavfi", "-i", "color=c=red@0.0:s=120x120,format=rgba,drawbox=x=30:y=30:w=60:h=60:color=red@1:t=fill", "-frames:v", "1"]),
           ("beach photo.jpg", ["-f", "lavfi", "-i", "testsrc=s=320x240", "-frames:v", "1"]),
           ("light leak.mp4", ["-f", "lavfi", "-i", "testsrc=s=320x240:d=3", "-pix_fmt", "yuv420p"]),
           ("city drone.mp4", ["-f", "lavfi", "-i", "testsrc=s=320x240:d=4", "-pix_fmt", "yuv420p"])]
    for n_, a_ in gen:
        subprocess.run([core.FFMPEG, "-v", "error", "-y", *a_, str(mfx / n_)], check=True)
    (mfx / "notes.txt").write_text("hello")
    (mfx / "fake.mp4").write_bytes(b"not a video at all" * 100)

    def mup(names, kind=None):
        fs = [open(mfx / n_, "rb") for n_ in names]
        try:
            return client.post("/api/media/upload" + (f"?kind={kind}" if kind else ""),
                               data={"files": [(f_, n_) for f_, n_ in zip(fs, names)]}, content_type="multipart/form-data").get_json()
        finally:
            for f_ in fs:
                f_.close()
    rm_ = mup([n_ for n_, _ in gen] + ["notes.txt", "fake.mp4"])
    got = {r["name"]: (r.get("kind"), r["ok"]) for r in rm_["results"]}
    rdup = mup(["tick tone.wav"])["results"][0]
    rforce = mup(["beach photo.jpg"], "stickers")["results"][0]
    ml = client.get("/api/media").get_json()["kinds"]
    library._cache["sig"] = None
    cat_m = library.catalog()
    check("media: dropped files sorted – short sound -> sounds, 30 s -> music, transparent PNG -> stickers, photo -> pictures, "
          "‘light leak’ -> transitions, clip -> videos; text / fake video refused; same file twice kept once; a JPG can't be a sticker; "
          "new sound + sticker show in the Library at once",
          got.get("tick tone.wav") == ("sounds", True) and got.get("calm bed.mp3") == ("music", True)
          and got.get("logo cut.png") == ("stickers", True) and got.get("beach photo.jpg") == ("pictures", True)
          and got.get("light leak.mp4") == ("transitions", True) and got.get("city drone.mp4") == ("videos", True)
          and got.get("notes.txt", (None, True))[1] is False and got.get("fake.mp4", (None, True))[1] is False
          and rdup.get("dup") and not rforce["ok"]
          and [i["name"] for i in ml["music"]["items"]] == ["calm bed.mp3"] and len(ml["videos"]["items"]) == 1
          and any(s["name"] == "tick tone" for s in cat_m["sounds"]) and any(s["name"] == "logo cut" for s in cat_m["stickers"]),
          f"{got} dup={rdup} force={rforce.get('why')}")
    ru = post(f"/api/projects/{pid}/media/use", {"kind": "videos", "name": "city drone.mp4"}).get_json()
    ru2 = post(f"/api/projects/{pid}/media/use", {"kind": "videos", "name": "city drone.mp4"}).get_json()
    rmv_bad = post("/api/media/move", {"kind": "pictures", "name": "beach photo.jpg", "to": "music"})
    rmv = post("/api/media/move", {"kind": "transitions", "name": "light leak.mp4", "to": "videos"}).get_json()
    rdel = post("/api/media/remove", {"kind": "sounds", "name": "tick tone.wav"}).get_json()
    rtrav = post("/api/media/remove", {"kind": "sounds", "name": "../../Brand Kit/brand-kit.json"})
    pa = client.get(f"/api/projects/{pid}").get_json()
    ml2 = client.get("/api/media").get_json()["kinds"]
    check("media: '+ this project' copies a clip into the project's B-roll files (once); move checks the kind; delete goes "
          "to the trash; a path in the name can't reach outside the folder",
          ru.get("ok") and any(a["file"] == "city drone.mp4" for a in pa.get("broll_assets") or []) and "already" in ru2["reply"]
          and rmv_bad.status_code == 400 and rmv.get("ok") and len(ml2["videos"]["items"]) == 2 and not ml2["transitions"]["items"]
          and rdel.get("ok") and "tick tone.wav" not in [i["name"] for i in ml2["sounds"]["items"]] and (library.LIB / ".trash" / "sounds" / "tick tone.wav").is_file()
          and rtrav.status_code == 400,
          f"{ru} {ru2} {rmv_bad.get_json()} {rmv}")
    # 🟩 green screen -> picture / video behind you (files from the Media tab)
    r_gv = post(f"/api/projects/{pid}/brief", {"settings": {"bg_mode": "video", "bg_file": "Videos/city drone.mp4"}})
    r_gbad = post(f"/api/projects/{pid}/brief", {"settings": {"bg_file": "../Brand Kit/brand-kit.json"}})
    r_gnone = post(f"/api/projects/{pid}/brief", {"settings": {"bg_file": "Videos/nope.mp4"}})
    st_g = core.load(pid)["settings"]
    gb1 = pipeline.green_background(st_g)
    gb_pic = pipeline.green_background(dict(st_g, bg_mode="picture", bg_file="Pictures/beach photo.jpg"))
    gb_gone = pipeline.green_background(dict(st_g, bg_file="Videos/deleted.mp4"))
    gb_off = pipeline.green_background(dict(st_g, bg_mode="off"))
    check("green screen: video / picture behind you from Media; paths outside Library/Pictures|Videos and missing files "
          "refused; a file deleted later falls back to the clean wall; 'keep as filmed' = none",
          r_gv.status_code == 200 and r_gbad.status_code == 400 and r_gnone.status_code == 400
          and st_g["bg_mode"] == "video" and st_g["bg_file"] == "Videos/city drone.mp4"
          and gb1[0] == "video" and gb1[3] > 3 and gb_pic[0] == "picture" and gb_gone == ("color",) and gb_off is None,
          f"{gb1} {gb_pic} {gb_gone}")
    post(f"/api/projects/{pid}/brief", {"settings": {"bg_mode": "auto", "bg_file": ""}})
    shutil.rmtree(mfx, ignore_errors=True)

    # ---------------------------------------------------------------- stale job after a closed studio
    core.set_job(pid, status="running", kind="final", pending=["preview"])
    core.reset_stale_jobs()
    j = core.load(pid)["job"]
    check("stale 'running' job reset at startup", j["status"] == "error" and not j["pending"], j.get("progress"))

    # ---------------------------------------------------------------- same name, different extension
    p2 = post("/api/projects", {"name": "Stem Clash"}).get_json()["id"]
    for src_name, up_name in (("recap_test.mp4", "clip.mp4"), ("phone_rot.mp4", "clip.mov")):
        with open(FIX / src_name, "rb") as f:
            client.post(f"/api/projects/{p2}/upload?kind=source", data={"files": (f, up_name)},
                        content_type="multipart/form-data")
    post(f"/api/projects/{p2}/analyze")
    wait_job(p2, "analyze")
    dmov = core.load(p2)
    mov_scenes = [s for s in dmov["scenes"] if s["src"] == "clip.mov"]
    mp4_scenes = [s for s in dmov["scenes"] if s["src"] == "clip.mp4"]
    check("clip without speech -> captions off automatically; talking clip untouched",
          core.source_by_file(dmov, "clip.mov").get("speech") is False and mov_scenes and
          all(s["captions"] is False and s.get("captions_auto_off") for s in mov_scenes) and
          all(s["captions"] is None for s in mp4_scenes) and core.source_by_file(dmov, "clip.mp4").get("speech"))
    sp = [pipeline.is_speech(3, 10.7), pipeline.is_speech(2, 1.5), pipeline.is_speech(25, 10.0), pipeline.is_speech(0, 3)]
    fake = {"scenes": [{"id": "x", "src": "m", "in": 0, "out": 10, "captions": True},
                       {"id": "y", "src": "m", "in": 10, "out": 20, "captions": None}],
            "sources": [{"file": "m", "speech": False}]}
    check("speech rule: 3 words in 10.7 s = music; 'thank you' in 1.5 s = speech; user's explicit 'captions on' wins",
          sp == [False, True, True, False] and pipeline.mark_no_speech(fake, {"m": []}) == ["y"]
          and fake["scenes"][0]["captions"] is True)
    r = post(f"/api/projects/{p2}/command", {"scene": mov_scenes[0]["id"], "text": "captions on"}).get_json()
    s_on = core.scene_by_id(core.load(p2), mov_scenes[0]["id"])
    check("'captions on' brings captions back on a no-speech scene (with a heads-up)",
          s_on["captions"] is True and "captions_auto_off" not in s_on and "no detected speech" in r["replies"][0])
    post(f"/api/projects/{p2}/undo")
    pj2 = client.get(f"/api/projects/{p2}").get_json()
    rlast = (pj2.get("renders") or [{}])[-1]
    check("after analysis a preview of the COMPLETE video is rendered automatically (fresh fingerprint)",
          rlast.get("kind") == "preview" and rlast.get("sig") == pj2["edit_sig"] and
          any("complete video" in x for x in pj2["job"]["log"]), str(rlast))
    post(f"/api/projects/{p2}/command", {"scene": pj2["scenes"][0]["id"], "text": "mute"})
    check("an edit after the render makes that render 'out of date'",
          client.get(f"/api/projects/{p2}").get_json()["edit_sig"] != rlast.get("sig"))
    post(f"/api/projects/{p2}/undo")
    w_mp4 = core.read_json(core.work_file(p2, "clip.mp4", "words.json"), [])
    w_mov = core.read_json(core.work_file(p2, "clip.mov", "words.json"), [])
    check("clip.mp4 and clip.mov keep separate transcripts", len(w_mp4) > 20 and w_mov == [] and
          core.work_file(p2, "clip.mp4", "words.json") != core.work_file(p2, "clip.mov", "words.json"),
          f"mp4 {len(w_mp4)} words, mov {len(w_mov)} words")

    # ---------------------------------------------------------------- Footage bin: add, order, skip, remove
    for nm, col in (("C0010.mp4", "red"), ("C0002.mp4", "blue")):
        subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={col}:s=320x240:d=1.5",
                        "-c:v", "libx264", "-preset", "ultrafast", str(ROOT / nm)], check=True)
    files = [("files", (open(ROOT / "C0010.mp4", "rb"), "C0010.mp4")), ("files", (open(ROOT / "C0002.mp4", "rb"), "C0002.mp4"))]
    r = client.post(f"/api/projects/{p2}/upload?kind=source", data={"files": [f for _, f in files]},
                    content_type="multipart/form-data")
    order = [s["file"] for s in core.load(p2)["sources"]]
    check("multi-select upload lands in natural name order (C0002 before C0010) with poster + size",
          order[-2:] == ["C0002.mp4", "C0010.mp4"] and core.load(p2)["sources"][-1].get("thumb")
          and core.load(p2)["sources"][-1].get("bytes"), str(order))
    src_rec = core.source_by_file(core.load(p2), "clip.mp4")
    strip_ok = bool(src_rec.get("strip")) and (core.work_dir(p2) / "thumbs" / src_rec["strip"]).exists() and bool(src_rec.get("wave"))
    core.mutate(p2, lambda d: [s.pop(k, None) for s in d["sources"] for k in ("strip", "wave")])
    ra = post(f"/api/projects/{p2}/footage/assets").get_json()
    check("timeline film strip + waveform made on upload, and regenerated for older clips",
          strip_ok and ra["made"] >= 3 and all(s.get("strip") for s in core.load(p2)["sources"]), str(ra))
    with open(ROOT / "C0002.mp4", "rb") as f:
        r = client.post(f"/api/projects/{p2}/upload?kind=source", data={"files": (f, "copy of C0002.mp4")},
                        content_type="multipart/form-data")
    check("same clip under another name is refused as a duplicate (no second copy on disk)",
          r.status_code == 409 and not (core.raw_dir(p2) / "copy of C0002.mp4").exists(), str(r.get_json()))
    # scenes follow the clip order; a manual cross-clip arrangement is protected by a confirm
    d2 = core.load(p2)
    srcs = [s["file"] for s in d2["sources"]]
    rev = list(reversed(srcs))
    r = post(f"/api/projects/{p2}/sources/order", {"files": rev})
    seq = [s["src"] for s in core.load(p2)["scenes"]]
    check("reorder clips -> sources reordered, scenes regrouped in the new clip order",
          r.status_code == 200 and [s["file"] for s in core.load(p2)["sources"]] == rev and
          seq == sorted(seq, key=rev.index), str(seq))
    stale = post(f"/api/projects/{p2}/sources/order", {"files": rev[:-1]})
    check("reorder with an out-of-date clip list is refused (409), nothing changes",
          stale.status_code == 409 and [s["file"] for s in core.load(p2)["sources"]] == rev)
    core.mutate(p2, lambda d: d.__setitem__("scenes", d["scenes"][-1:] + d["scenes"][:-1]))  # cross-clip arrangement
    if pipeline.scenes_interleaved(core.load(p2)["scenes"]):
        r1 = post(f"/api/projects/{p2}/sources/order", {"sort": "name"})
        r2 = post(f"/api/projects/{p2}/sources/order", {"sort": "name", "force": True})
        names = [s["file"] for s in core.load(p2)["sources"]]
        check("manual scene arrangement across clips -> confirm first, then sort by name",
              r1.status_code == 409 and r1.get_json()["confirm"] and r2.status_code == 200 and
              names == sorted(names, key=appmod._natural_key), str(names))
    # skip a clip: out of totals/render, still in the project
    one = core.load(p2)["sources"][0]["file"]
    post(f"/api/projects/{p2}/source/include", {"file": one, "include": False})
    pj = client.get(f"/api/projects/{p2}").get_json()
    check("Skip keeps the clip but leaves it out of the edit length",
          any(s["file"] == one and s["include"] is False for s in pj["sources"]) and
          all(s["est"] == 0 for s in pj["scenes"] if s["src"] == one) and pj["footage"]["included"] == len(pj["sources"]) - 1)
    post(f"/api/projects/{p2}/source/include", {"file": one, "include": True})
    n_sc = len(core.load(p2)["scenes"])
    post(f"/api/projects/{p2}/source/remove", {"file": "clip.mp4"})
    gone = all(s["src"] != "clip.mp4" for s in core.load(p2)["scenes"])
    post(f"/api/projects/{p2}/undo")
    d2u = core.load(p2)
    check("Remove clip is undoable (its edited scenes AND the clip itself come back – else they'd render silent)",
          gone and len(d2u["scenes"]) == n_sc and any(s["file"] == "clip.mp4" and s.get("has_audio") is not None
                                                     for s in d2u["sources"]), str([s["file"] for s in d2u["sources"]]))
    # analysing a new clip must not reshuffle a hand-made scene order
    a_ = [{"id": "a1", "src": "A"}, {"id": "b1", "src": "B"}, {"id": "a2", "src": "A"}]
    placed = pipeline.place_scenes(a_, [{"id": "c1", "src": "C"}], "C", ["A", "B", "C"])
    redone = pipeline.place_scenes(a_, [{"id": "b9", "src": "B"}], "B", ["A", "B", "C"])
    first = pipeline.place_scenes([{"id": "b1", "src": "B"}], [{"id": "a1", "src": "A"}], "A", ["A", "B"])
    check("new/re-analyzed clip slots in without reshuffling the user's scene order",
          [x["id"] for x in placed] == ["a1", "b1", "a2", "c1"] and [x["id"] for x in redone] == ["a1", "b9", "a2"]
          and [x["id"] for x in first] == ["a1", "b1"], f"{[x['id'] for x in placed]} {[x['id'] for x in redone]}")

    # ---------------------------------------------------------------- Brand Kit split layout: green screen, cards, music
    import cards
    pc = [cards.parse("title bajaj *IPO* aaj"), cards.parse("number 2x+ | listing gain"),
          cards.parse("chart 10, 12, 11.5, 15 | Nifty"), cards.parse("off"), cards.parse("chart 5")]
    check("card commands parse (title/number/chart/off; 1-point chart refused)",
          [c and c["kind"] for c in pc] == ["title", "number", "chart", "none", None] and pc[2]["points"][2] == 11.5)
    green = ROOT / "green.mp4"
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-i", str(FIX / "recap_test.mp4"), "-f", "lavfi", "-i",
                    "color=c=0x4CBF4C:s=720x1280:r=30", "-filter_complex",
                    "[0:v]scale=440:-2[p];[1:v][p]overlay=(W-w)/2:H-h-80:shortest=1[v]", "-map", "[v]", "-map", "0:a",
                    "-t", "12", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(green)], check=True)
    core.BRAND.mkdir(exist_ok=True)
    (core.BRAND / "brand-kit.json").write_text(json.dumps({"settings": {
        "layout": "split", "format": "9:16", "music": "auto", "audio_polish": True, "caption_font": "Poppins",
        "loudness": -14, "duck_db": 18,
        "bogus_key": 1, "silence_min": "not a number"}}), encoding="utf-8")
    p3 = post("/api/projects", {"name": "Split Test"}).get_json()["id"]
    st3 = core.load(p3)["settings"]
    d3n = core.load(p3)
    check("new project starts on 'None · simple settings' (plain, even with a Brand Kit)",
          d3n["style_source"] == "none" and st3["layout"] == "full" and st3["music"] == core.DEFAULT_SETTINGS["music"]
          and "bogus_key" not in st3)
    post(f"/api/projects/{p3}/style_source", {"source": "brandkit"})
    st3 = core.load(p3)["settings"]
    check("picking ★ Brand Kit in the Look dropdown applies it (bad/unknown keys ignored)",
          st3["layout"] == "split" and st3["music"] == "auto" and st3["silence_min"] == 0.6 and "bogus_key" not in st3)
    rp = post(f"/api/projects/{p3}/first_cut")
    check("plan the edit refused before analysis (no scenes)", rp.status_code == 400)
    # ★ Brand Kit is read-only from a project: style edits ignored, its brief + footage keys still save
    r = post(f"/api/projects/{p3}/brief", {"look": "brandkit", "look_brief": "calm brand look",
                                            "settings": {"layout": "full", "face_y": 0.33}})
    d3n = core.load(p3)
    cli_bk = subprocess.run([sys.executable, str(HERE / "studio_cli.py"), "look", p3, "brandkit", "layout=full"],
                            env={**os.environ, "CVE_ROOT": str(ROOT)}, capture_output=True, text=True)
    check("★ Brand Kit is read-only: style edits ignored (page and CLI), its brief and footage keys save",
          r.status_code == 200 and d3n["settings"]["layout"] == "split" and d3n["settings"]["face_y"] == 0.33 and
          d3n["look_briefs"]["brandkit"] == "calm brand look" and cli_bk.returncode != 0)
    post(f"/api/projects/{p3}/style_source", {"source": "none"})
    kit = json.loads((core.BRAND / "brand-kit.json").read_text(encoding="utf-8"))
    kit["settings"]["caption_words"] = 2
    (core.BRAND / "brand-kit.json").write_text(json.dumps(kit), encoding="utf-8")
    post(f"/api/projects/{p3}/style_source", {"source": "brandkit"})
    post(f"/api/projects/{p3}/command", {"scene": None, "text": "words 6"})
    rr = post(f"/api/projects/{p3}/style_source", {"source": "none"}).get_json()
    post(f"/api/projects/{p3}/style_source", {"source": "brandkit"})
    check("picking ★ Brand Kit loads the LATEST file; changes made while it was active aren't stored into it",
          core.load(p3)["settings"]["caption_words"] == 2 and any("not kept" in n for n in rr.get("notes", [])))
    with open(green, "rb") as f:
        client.post(f"/api/projects/{p3}/upload?kind=source", data={"files": (f, "green.mp4")},
                    content_type="multipart/form-data")
    post(f"/api/projects/{p3}/analyze")
    wait_job(p3, "analyze")
    check("green screen detected", pipeline.green_key(p3, "green.mp4") is not None and
          pipeline.green_key(pid, core.load(pid)["sources"][0]["file"]) is None)
    pa = post(f"/api/projects/{p3}/first_cut").get_json()
    pb = post(f"/api/projects/{p3}/first_cut").get_json()
    plan_cmds = [c for c in core.load(p3)["commands"] if c.get("kind") == "plan_edit" and c["status"] == "claude"]
    core.set_job(p3, status="running", kind="analyze")
    pc = post(f"/api/projects/{p3}/first_cut")
    core.set_job(p3, status="done", kind="analyze")
    check("✨ plan the edit: one storyboard request (names the look), no duplicates, refused while analyzing",
          pa["ok"] and not pa["already"] and pb["already"] and len(plan_cmds) == 1 and "★ Brand Kit" in plan_cmds[0]["text"]
          and pc.status_code == 409)
    d3 = core.load(p3)
    post(f"/api/projects/{p3}/command", {"scene": None, "text": "headline: test *card*"})
    r = post(f"/api/projects/{p3}/command", {"scene": d3["scenes"][-1]["id"], "text": "card: number 2x | gain"}).get_json()
    post(f"/api/projects/{p3}/command", {"scene": None, "text": "layout full"})
    r2 = post(f"/api/projects/{p3}/apply_brand").get_json()
    check("card command + Apply Brand Kit (undoable)", core.load(p3)["scenes"][-1].get("card", {}).get("value") == "2x"
          and core.load(p3)["settings"]["layout"] == "split" and "layout" in r2["applied"], str(r))
    post(f"/api/projects/{p3}/render", {"kind": "final"})
    j = wait_job(p3, "final")
    check("split layout + ducked music + loudness renders", j["status"] == "done", j.get("error") or "")
    if j["status"] == "done":
        o3 = core.EXPORTS / core.load(p3)["renders"][-1]["file"]
        top_h, _ = pipeline.split_heights(1920, core.load(p3)["settings"])

        def px(x, y, t=1.0):
            b = subprocess.run([core.FFMPEG, "-v", "error", "-ss", str(t), "-i", str(o3), "-frames:v", "1", "-vf",
                                f"format=rgb24,crop=1:1:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
            return tuple(b[:3])
        seam, top, bg = px(30, top_h), px(30, 30), px(30, top_h + 120)
        check("seam is brand orange, top panel navy, green replaced by off-white",
              seam[0] > 200 and seam[2] < 90 and top[2] > top[0] and max(top) < 70 and min(bg) > 185 and
              not (bg[1] > bg[0] + 20), f"seam {seam} top {top} bg {bg}")
        lo = subprocess.run([core.FFMPEG, "-hide_banner", "-i", str(o3), "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
        lufs = float(_re.findall(r"I:\s+(-?[\d.]+) LUFS", lo)[-1])
        check("final loudness -14 LUFS (+-0.6, measured + corrected)", -14.6 <= lufs <= -13.4, f"{lufs} LUFS")
        # damaged music file must not cost the render; duck 0 = no ducking (was silently 10)
        (core.BRAND / "music").mkdir(exist_ok=True)
        (core.BRAND / "music" / "broken.mp3").write_bytes(b"not really an mp3" * 50)
        post(f"/api/projects/{p3}/brief", {"settings": {"music": "broken.mp3", "duck_db": 0}})
        post(f"/api/projects/{p3}/render", {"kind": "preview"})
        jb = wait_job(p3, "preview")
        check("broken music file -> render still succeeds, without music, and says so",
              jb["status"] == "done" and any("could not be mixed" in x for x in jb.get("log", [])), jb.get("error") or "")
        check("duck_db 0 is kept as 0 (no '0 or default' bug)", core.load(p3)["settings"]["duck_db"] == 0)
        # 🎵 a chosen song: silent intro skipped, a short song repeats with crossfades (no gap), level / start validated
        song = core.BRAND / "music" / "intro song.mp3"
        subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=2", "-f", "lavfi",
                        "-i", "sine=f=330:d=4:r=44100", "-filter_complex",
                        "[1:a]aformat=channel_layouts=stereo,volume=0.3[s];[0:a][s]concat=n=2:v=0:a=1", str(song)], check=True)
        bad_ = [post(f"/api/projects/{p3}/brief", {"settings": s_}).status_code
                for s_ in ({"music_level": "huge"}, {"music_start": "abc"}, {"music": "../../x.mp3"})]
        okr = post(f"/api/projects/{p3}/brief", {"settings": {"music": "intro song.mp3", "music_level": "loud",
                                                              "music_start": "0:01.5", "duck_db": 10}})
        s3 = core.load(p3)["settings"]
        st_auto = pipeline.music_start(song, "auto")
        bed_, seek_ = pipeline.music_bed(song, st_auto, 20.0)
        sil_ = subprocess.run([core.FFMPEG, "-v", "info", "-i", str(bed_), "-af", "silencedetect=noise=-45dB:d=0.2", "-f",
                               "null", "-"], capture_output=True, text=True).stderr
        post(f"/api/projects/{p3}/brief", {"settings": {"music_start": "auto"}})
        n_bad0 = sum("could not be mixed" in x for x in (core.load(p3).get("job") or {}).get("log", []))  # log keeps old jobs
        post(f"/api/projects/{p3}/render", {"kind": "preview"})
        jm = wait_job(p3, "preview")
        check("music: a song you pick starts after its silent intro, repeats with a smooth crossfade when shorter than "
              "the video (no silent gap), level / start / file validated; the render mixes it",
              bad_ == [400, 400, 400] and okr.status_code == 200 and s3["music_level"] == "loud"
              and s3["music_start"] == 1.5 and 1.7 <= st_auto <= 2.0 and pipeline.song_len(bed_) >= 20
              and "silence_start" not in sil_ and jm["status"] == "done"
              and sum("could not be mixed" in x for x in jm.get("log", [])) == n_bad0,
              f"bad {bad_} start {st_auto} bed {pipeline.song_len(bed_):.1f}s {jm.get('error') or ''}")
        post(f"/api/projects/{p3}/brief", {"settings": {"music": "off"}})
        (core.BRAND / "music" / "broken.mp3").unlink()
        post(f"/api/projects/{p3}/brief", {"settings": {"music": "auto", "duck_db": 18}})
        # duck depth: ducked music alone vs the voice, music RMS in speech vs in pauses
        import math, struct, wave
        pad = pipeline.generated_pad()
        g = (f"[0:a]aresample=48000,loudnorm=I=-14:TP=-1,asplit=2[vo][key];[1:a]aformat=sample_fmts=fltp:"
             f"sample_rates=48000:channel_layouts=stereo,volume={-26 - pipeline.music_lufs(pad):.2f}dB[m];"
             f"[m][key]{pipeline.duck_filter(-14, 18)}[md]")
        subprocess.run([core.FFMPEG, "-v", "error", "-y", "-i", str(FIX / "recap_test.mp4"), "-stream_loop", "-1",
                        "-i", str(pad), "-filter_complex", g, "-map", "[md]", "-ac", "1", "-ar", "16000", "-t", "20",
                        str(ROOT / "md.wav"), "-map", "[vo]", "-ac", "1", "-ar", "16000", "-t", "20",
                        str(ROOT / "vo.wav")], check=True)
        rd = lambda p: (lambda w: struct.unpack(f"<{w.getnframes()}h", w.readframes(w.getnframes())))(wave.open(str(p)))  # noqa: E731
        md, vo = rd(ROOT / "md.wav"), rd(ROOT / "vo.wav")
        sp, gp = [], []
        for i in range(0, min(len(md), len(vo)) - 1600, 1600):
            v = math.sqrt(sum(a * a for a in vo[i:i + 1600]) / 1600) + 1e-9
            m = math.sqrt(sum(a * a for a in md[i:i + 1600]) / 1600) + 1e-9
            (sp if 20 * math.log10(v / 32768) > -35 else gp).append(20 * math.log10(m / 32768))
        depth = sorted(gp)[len(gp) // 2] - sorted(sp)[len(sp) // 2]
        check("music ducks ~18 dB under dialogue (+-2.5)", 15.5 <= depth <= 20.5, f"{depth:.1f} dB")
        fd = abs(sdur(o3, "a:0") - sdur(o3, "v:0")) * 1000
        check("split export A/V lengths match (<=60 ms)", fd <= 60, f"{fd:.0f} ms")

    # ---------------------------------------------------------------- B-roll uploaded for ONE scene (picture or video),
    # placed top / bottom / full – split layout (card panel / over you) and full layout (halves)
    red_png, blue_mp4 = ROOT / "red chart.png", ROOT / "blue.mp4"
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xE01010:s=640x360", "-frames:v", "1",
                    str(red_png)], check=True)
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x1030E0:s=1280x720:r=30:d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(blue_mp4)], check=True)

    def up(data, name, sid):
        return client.post(f"/api/projects/{p3}/upload?kind=broll&scene={sid}", data={"files": (data, name)},
                           content_type="multipart/form-data")
    d3 = core.load(p3)
    main3 = [s for s in d3["scenes"] if s.get("keep", True) and s.get("role") != "broll"]
    sa, sb = main3[0]["id"], main3[1]["id"]
    ra = up(open(red_png, "rb"), "red chart.png", sa).get_json()
    rb = up(open(red_png, "rb"), "same picture.png", sb).get_json()
    import io as _io
    rt = up(_io.BytesIO(b"hello"), "notes.txt", sb)
    rg = up(_io.BytesIO(b"not a video" * 100), "fake.mp4", sb)
    adir = core.assets_dir(p3)
    pj3 = client.get(f"/api/projects/{p3}").get_json()
    check("upload B-roll for a scene: Assets, scene covered, same file reused (not stored twice), bad files refused",
          ra.get("file") == "red chart.png" and rb.get("reused") and rb.get("file") == "red chart.png"
          and core.scene_by_id(core.load(p3), sa)["broll"]["src"] == "red chart.png"
          and len([x for x in adir.iterdir() if x.suffix == ".png"]) == 1 and rt.status_code == 400 and rg.status_code == 400
          and not (adir / "fake.mp4").exists() and not [x for x in adir.iterdir() if x.name.startswith(".")]
          and any(x["file"] == "red chart.png" and x["still"] for x in pj3["broll_assets"]), f"{ra} {rb} {rt.status_code} {rg.status_code}")
    rv = up(open(blue_mp4, "rb"), "blue.mp4", sb).get_json()
    post(f"/api/projects/{p3}/command", {"scene": sa, "text": "broll bottom"})
    post(f"/api/projects/{p3}/command", {"scene": sb, "text": "broll top"})
    pj3 = client.get(f"/api/projects/{p3}").get_json()
    bvs = {x["id"]: x.get("broll_view") for x in pj3["scenes"]}
    check("placement stored + shown: bottom (over you) / top (card panel)",
          rv.get("file") == "blue.mp4" and bvs[sa]["where"] == "bottom" and bvs[sb]["where"] == "top" and bvs[sa]["still"], str(bvs[sa]))

    def frame_px(kind, sid, fx, fy):
        rec = core.load(p3)["renders"][-1]
        f = (core.EXPORTS if kind == "final" else core.work_dir(p3)) / rec["file"]
        t0, du = next((a, d) for i, a, d in rec["times"] if i == sid)
        m = pipeline.probe(f)
        x, y = int(m["width"] * fx), int(m["height"] * fy)
        b = subprocess.run([core.FFMPEG, "-v", "error", "-ss", f"{t0 + min(0.4, du / 2):.3f}", "-i", str(f), "-frames:v", "1",
                            "-vf", f"format=rgb24,crop=1:1:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True).stdout
        return tuple(b[:3])
    red = lambda c: c and c[0] > 170 and c[1] < 90 and c[2] < 90  # noqa: E731
    blue = lambda c: c and c[2] > c[0] + 50 and c[2] > 90  # noqa: E731
    post(f"/api/projects/{p3}/render", {"kind": "preview"})
    jb = wait_job(p3, "preview")
    th = pipeline.split_heights(1920, core.load(p3)["settings"])[0] / 1920
    if jb["status"] == "done":
        a_bot, a_top = frame_px("preview", sa, 0.05, th + (1 - th) * 0.3), frame_px("preview", sa, 0.05, 0.03)
        b_top, b_bot = frame_px("preview", sb, 0.05, th * 0.5), frame_px("preview", sb, 0.05, th + (1 - th) * 0.3)
    check("split: picture BOTTOM covers you (card panel stays), video TOP fills the card panel",
          jb["status"] == "done" and red(a_bot) and not red(a_top) and blue(b_top) and not blue(b_bot),
          jb.get("error") or f"a bottom {a_bot} top {a_top} | b top {b_top} bottom {b_bot}")
    post(f"/api/projects/{p3}/command", {"scene": None, "text": "layout full"})
    post(f"/api/projects/{p3}/command", {"scene": sa, "text": "broll top half"})
    post(f"/api/projects/{p3}/command", {"scene": sb, "text": "broll bottom half"})
    post(f"/api/projects/{p3}/command", {"scene": main3[2]["id"], "text": f"broll red chart.png"})
    post(f"/api/projects/{p3}/command", {"scene": main3[2]["id"], "text": "broll full"})
    rfit = post(f"/api/projects/{p3}/command", {"scene": sa, "text": "broll whole picture"}).get_json()
    post(f"/api/projects/{p3}/render", {"kind": "preview"})
    jb = wait_job(p3, "preview")
    if jb["status"] == "done":
        a_band, a_mid = frame_px("preview", sa, 0.5, 0.025), frame_px("preview", sa, 0.5, 0.25)
        check("'whole picture': a wide picture in the top half is shown whole – empty space = darker blurred copy, not black",
              core.scene_by_id(core.load(p3), sa)["broll"].get("fit") == "fit" and red(a_mid)
              and a_band[0] > 90 and a_band[0] < a_mid[0] - 15, f"band {a_band} middle {a_mid} {rfit}")
        a_t, a_b = frame_px("preview", sa, 0.05, 0.2), frame_px("preview", sa, 0.05, 0.7)
        b_t, b_b = frame_px("preview", sb, 0.05, 0.2), frame_px("preview", sb, 0.05, 0.7)
        c_t, c_b = frame_px("preview", main3[2]["id"], 0.05, 0.2), frame_px("preview", main3[2]["id"], 0.05, 0.7)
    check("full layout: B-roll in the TOP half / BOTTOM half (you in the other half), full screen covers all",
          jb["status"] == "done" and red(a_t) and not red(a_b) and blue(b_b) and not blue(b_t) and red(c_t) and red(c_b),
          jb.get("error") or f"a {a_t}/{a_b} b {b_t}/{b_b} c {c_t}/{c_b}")
    # delete an upload: scenes using it drop it (one undo step), the file goes to Assets/.trash; undo restores both
    rdel = post(f"/api/projects/{p3}/broll_asset/remove", {"file": "red chart.png"}).get_json()
    d3d, pjd = core.load(p3), client.get(f"/api/projects/{p3}").get_json()
    gone = (not (adir / "red chart.png").exists() and (adir / ".trash" / "red chart.png").exists()
            and not any((s.get("broll") or {}).get("src") == "red chart.png" for s in d3d["scenes"])
            and not any(x["file"] == "red chart.png" for x in pjd["broll_assets"]))
    bad_del = [post(f"/api/projects/{p3}/broll_asset/remove", {"file": f}).status_code for f in ("../x.png", "nope.png", "green.mp4")]
    post(f"/api/projects/{p3}/undo")
    d3u = core.load(p3)
    check("delete an upload: scenes drop it, file to trash; bad names refused; Undo brings file + scenes back",
          gone and bad_del == [404, 404, 404] and (adir / "red chart.png").exists()
          and core.scene_by_id(d3u, sa)["broll"]["src"] == "red chart.png", f"{rdel} {bad_del}")
    for sid_ in (sa, sb, main3[2]["id"]):
        post(f"/api/projects/{p3}/command", {"scene": sid_, "text": "broll off"})
    post(f"/api/projects/{p3}/command", {"scene": None, "text": "layout split"})

    # ---------------------------------------------------------------- B-roll cutaways (Scenes page)
    d2 = core.load(p2)
    mov = [s for s in d2["scenes"] if s["src"] == "clip.mov"]
    check("no-speech clip stays a normal scene in the video (B-roll is opt-in)",
          mov and all(not s.get("role") and not s.get("broll") for s in d2["scenes"]),
          f"{[(s['src'], s.get('role'), s.get('broll')) for s in d2['scenes']]}")
    post(f"/api/projects/{p2}/command", {"scene": mov[0]["id"], "text": "role broll"})
    d2 = core.load(p2)
    mov = [s for s in d2["scenes"] if s["src"] == "clip.mov"]
    cov = [s for s in d2["scenes"] if (s.get("broll") or {}).get("src") == "clip.mov"]
    pj = client.get(f"/api/projects/{p2}").get_json()
    check("'role broll' makes the no-speech clip B-roll and covers a speaker scene (full layout: full screen)",
          mov and all(s.get("role") == "broll" for s in mov) and cov and
          all("where" not in s["broll"] for s in cov) and
          all(s["broll_view"]["where"] == "full" for s in pj["scenes"] if s.get("broll")) and
          all(s["est"] == 0 for s in pj["scenes"] if s["src"] == "clip.mov")
          and pj["broll_clips"] and pj["broll_clips"][0]["file"] == "clip.mov" and pj["broll_clips"][0]["used_by"],
          f"{[(s['src'], s.get('role'), s.get('broll')) for s in d2['scenes']]}")
    sc1 = cov[0]["id"]
    r_top = post(f"/api/projects/{p2}/command", {"scene": sc1, "text": "b-roll top"}).get_json()
    r_at = post(f"/api/projects/{p2}/command", {"scene": sc1, "text": "broll from 1.5"}).get_json()
    b1 = core.scene_by_id(core.load(p2), sc1)["broll"]
    check("B-roll commands: 'top' in the full layout = top half; 'broll from 1.5' sets the clip start",
          "top half" in r_top["replies"][0] and b1["where"] == "top" and b1["at"] == 1.5, str(r_top))
    r_self = post(f"/api/projects/{p2}/command", {"scene": mov[0]["id"], "text": "broll clip.mp4"}).get_json()
    r_sp = post(f"/api/projects/{p2}/command", {"scene": mov[0]["id"], "text": "role speaker"}).get_json()
    left = [s for s in core.load(p2)["scenes"] if s.get("broll")]
    r_br = post(f"/api/projects/{p2}/command", {"scene": mov[0]["id"], "text": "role broll"}).get_json()
    again = [s for s in core.load(p2)["scenes"] if (s.get("broll") or {}).get("src") == "clip.mov"]
    check("B-roll footage can't take a cutaway; Role: Speaker drops its cutaways; Role: B-roll places it again",
          r_self["replies"][0].startswith("!") and not left and "no longer covers" in r_sp["replies"][0]
          and again and "covers" in r_br["replies"][0], f"{r_sp} {r_br}")
    r_off = post(f"/api/projects/{p2}/command", {"scene": None, "text": "broll off"}).get_json()
    n_off = sum(1 for s in core.load(p2)["scenes"] if s.get("broll"))
    r_auto = post(f"/api/projects/{p2}/command", {"scene": None, "text": "broll auto"}).get_json()
    check("whole-video 'broll off' / 'broll auto'", n_off == 0 and "removed" in r_off["replies"][0] and
          any(s.get("broll") for s in core.load(p2)["scenes"]), str(r_auto))
    # continuity: two scenes covered by one clip continue its footage (no replay after a split)
    fake = {"settings": {"layout": "full"}, "sources": [{"file": "clip.mov", "duration": 30.0}], "id": p2}
    sa = {"id": "a", "src": "clip.mp4", "in": 0.0, "out": 2.0, "broll": {"src": "clip.mov"}}
    sb_ = {"id": "b", "src": "clip.mp4", "in": 2.0, "out": 3.5, "broll": {"src": "clip.mov"}}
    sc_ = {"id": "c", "src": "clip.mp4", "in": 3.5, "out": 4.0, "broll": {"src": "clip.mov", "at": 20}}
    wb = pipeline.with_broll(p2, fake, [sa, sb_, sc_], {})
    check("consecutive cutaways continue the clip; an explicit start restarts it",
          [x["_broll"]["at"] for x in wb] == [0.0, 2.0, 20.0], str([x.get("_broll") for x in wb]))
    # timeline B-roll drag/drop endpoint: many scenes in ONE undo step; any bad entry -> nothing changes
    d2 = core.load(p2)
    spk = [s for s in d2["scenes"] if s.get("role") != "broll"]
    depth0 = core.history_depth(p2)
    rb = post(f"/api/projects/{p2}/broll", {"set": {spk[0]["id"]: None, spk[1]["id"]: {"src": "clip.mov", "where": "full"},
                                                    spk[2]["id"]: {"src": "clip.mov", "at": 1}}})
    d2b = core.load(p2)
    g = lambda d, s: core.scene_by_id(d, s["id"]).get("broll")  # noqa: E731
    bad_own = post(f"/api/projects/{p2}/broll", {"set": {spk[0]["id"]: {"src": "clip.mov"}, spk[3]["id"]: {"src": "clip.mp4"}}})
    bad_br = post(f"/api/projects/{p2}/broll", {"set": {mov[0]["id"]: {"src": "clip.mp4"}}})
    bad_nf = post(f"/api/projects/{p2}/broll", {"set": {spk[0]["id"]: {"src": "nope.mp4"}}})
    same = post(f"/api/projects/{p2}/broll", {"set": {spk[1]["id"]: {"src": "clip.mov", "where": "full"}}}).get_json()
    check("timeline B-roll move: several scenes in one undo step; own clip / B-roll target / unknown clip refused, nothing changed",
          rb.status_code == 200 and g(d2b, spk[0]) is None and g(d2b, spk[1]) == {"src": "clip.mov", "where": "full"} and
          g(d2b, spk[2]) == {"src": "clip.mov", "at": 1.0} and core.history_depth(p2) == depth0 + 1 and
          bad_own.status_code == 400 and bad_br.status_code == 400 and bad_nf.status_code == 400 and
          g(core.load(p2), spk[0]) is None and same["unchanged"] and core.history_depth(p2) == depth0 + 1,
          f"{rb.get_json()} {bad_own.get_json()}")
    post(f"/api/projects/{p2}/undo")
    check("undo reverts the whole drag", [g(core.load(p2), s) for s in spk[:3]] == [g(d2, s) for s in spk[:3]])
    # B-roll clip inserted INTO the video as its own scene (intro), its cutaways removed; undo puts it back
    d2 = core.load(p2)
    first_spk = next(s for s in d2["scenes"] if s.get("role") != "broll")
    post(f"/api/projects/{p2}/broll", {"set": {first_spk["id"]: {"src": "clip.mov"}}})
    ri = post(f"/api/projects/{p2}/broll/insert", {"file": "clip.mov", "before": first_spk["id"]})
    d2i = core.load(p2)
    ids_i = [s["id"] for s in d2i["scenes"]]
    movs = [s for s in d2i["scenes"] if s["src"] == "clip.mov"]
    again_i = post(f"/api/projects/{p2}/broll/insert", {"file": "clip.mov", "before": "end"})
    check("drop a B-roll clip on a scene edge -> it becomes its own scene there (cutaways removed); twice refused",
          ri.status_code == 200 and all("role" not in s for s in movs) and ids_i.index(movs[0]["id"]) < ids_i.index(first_spk["id"])
          and not any((s.get("broll") or {}).get("src") == "clip.mov" for s in d2i["scenes"]) and again_i.status_code == 400,
          str(ri.get_json()))
    post(f"/api/projects/{p2}/undo")
    post(f"/api/projects/{p2}/undo")
    check("undo returns it to B-roll footage", all(s.get("role") == "broll" for s in core.load(p2)["scenes"] if s["src"] == "clip.mov"))
    # trim to / captions auto / drag reorder
    s0 = core.load(p2)["scenes"][0]
    post(f"/api/projects/{p2}/command", {"scene": s0["id"], "text": "trim start to 0.25"})
    t1 = core.scene_by_id(core.load(p2), s0["id"])
    post(f"/api/projects/{p2}/command", {"scene": s0["id"], "text": "trim start to 0"})
    t2 = core.scene_by_id(core.load(p2), s0["id"])
    big = post(f"/api/projects/{p2}/command", {"scene": s0["id"], "text": f"trim start to {s0['out'] - s0['orig_in']}"}).get_json()
    check("absolute trim (timeline handles): 'trim start to 0.25' / back to 0 / too large refused",
          abs(t1["in"] - (s0["orig_in"] + 0.25)) < 1e-6 and t2["in"] == s0["orig_in"] and big["replies"][0].startswith("!"))
    talk = next(s for s in core.load(p2)["scenes"] if not s.get("no_speech"))
    post(f"/api/projects/{p2}/command", {"scene": talk["id"], "text": "captions off"})
    post(f"/api/projects/{p2}/command", {"scene": talk["id"], "text": "captions auto"})
    post(f"/api/projects/{p2}/command", {"scene": mov[0]["id"], "text": "captions on"})
    post(f"/api/projects/{p2}/command", {"scene": mov[0]["id"], "text": "captions auto"})
    ms = core.scene_by_id(core.load(p2), mov[0]["id"])
    check("'captions auto' follows the video setting (and stays off where nobody speaks)",
          core.scene_by_id(core.load(p2), talk["id"])["captions"] is None and ms["captions"] is False
          and ms.get("captions_auto_off"))
    ids = [s["id"] for s in core.load(p2)["scenes"]]
    post(f"/api/projects/{p2}/scene/{ids[-1]}/move", {"to": ids[0]})
    moved = [s["id"] for s in core.load(p2)["scenes"]]
    post(f"/api/projects/{p2}/scene/{ids[0]}/move", {"to": "end"})
    post(f"/api/projects/{p2}/scene/{ids[0]}/move", {"to": "gone"})
    post(f"/api/projects/{p2}/undo")
    post(f"/api/projects/{p2}/undo")
    check("timeline drag: move a scene before another / to the end; unknown target ignored; undoable",
          moved == [ids[-1]] + ids[:-1] and [s["id"] for s in core.load(p2)["scenes"]] == ids, str(moved))
    # per-scene caption position: head / center / below / auto (command + where libass puts it)
    r_cp = post(f"/api/projects/{p2}/command", {"scene": ids[0], "text": "captions at bottom"}).get_json()
    cp1 = core.scene_by_id(core.load(p2), ids[0]).get("cap_pos")
    post(f"/api/projects/{p2}/command", {"scene": ids[0], "text": "captions at auto"})
    cp2 = core.scene_by_id(core.load(p2), ids[0]).get("cap_pos", "none")
    wl = [{"w": "aaj", "s": 0.1, "e": 0.5}, {"w": "market", "s": 0.6, "e": 1.0}]
    ys = {}
    for pos, seam in (("head", None), ("center", None), ("below", None), ("head", 800), ("center", 800), ("below", 800)):
        pa = ROOT / "cp.ass"
        pipeline.write_ass(pa, {"in": 0, "out": 2, "cap_pos": pos}, wl, [(0, 2)], {}, 1080, 1920, seam_y=seam)
        m = re.search(r"\\pos\(540,(\d+)\)", pa.read_text(encoding="utf-8-sig"))
        ys[(pos, seam)] = int(m.group(1)) if m else None
    check("caption position per scene: 'captions at bottom' = below, 'auto' clears; head < center < below; split head just under the seam",
          cp1 == "below" and cp2 == "none" and ys[("head", None)] < ys[("center", None)] < ys[("below", None)]
          and 800 < ys[("head", 800)] < 1000 and ys[("center", 800)] < ys[("below", 800)] <= round(1920 * 0.8), f"{r_cp} {cp1} {cp2} {ys}")
    # exact caption height in % from the top (+ warnings, limits, whole-video "all scenes")
    rn = post(f"/api/projects/{p2}/command", {"scene": ids[0], "text": "captions at 65%"}).get_json()
    v65 = core.scene_by_id(core.load(p2), ids[0]).get("cap_pos")
    r85 = post(f"/api/projects/{p2}/command", {"scene": ids[0], "text": "captions at 85"}).get_json()
    rbad = post(f"/api/projects/{p2}/command", {"scene": ids[0], "text": "captions at 120%"}).get_json()
    rall = post(f"/api/projects/{p2}/command", {"scene": None, "text": "captions at 70%"}).get_json()
    allv = {s.get("cap_pos") for s in core.load(p2)["scenes"]}
    cy_set = core.load(p2)["settings"].get("caption_y")
    y_follow = pipeline.caption_center_y({}, 1080, 1920, 845, {"caption_y": 70})      # a scene following the video
    y_own = pipeline.caption_center_y({"cap_pos": "below"}, 1080, 1920, 845, {"caption_y": 70})  # exception wins
    y65 = pipeline.caption_center_y({"cap_pos": 65}, 1080, 1920, 845)
    band = pipeline.caption_band({"cap_pos": 65}, {}, 1080, 1920, None)
    post(f"/api/projects/{p2}/undo")
    post(f"/api/projects/{p2}/undo")
    post(f"/api/projects/{p2}/undo")
    check("caption height by number: 'captions at 65%' -> 65 % from the top (also in the split layout), Reels warning "
          "below 80 %, out-of-range refused; the WHOLE-video number = one setting every scene follows (scene "
          "exceptions cleared), a scene's own position still wins",
          v65 == 65 and y65 == 1248 and band[0] < 1248 < band[1] and "⚠" in r85["replies"][0]
          and rbad["replies"][0].startswith("!") and allv == {None} and cy_set == 70 and "ALL" in rall["replies"][0]
          and y_follow == 1344 and y_own == round(1920 * 0.78),
          f"{rn} {v65} {y65} {r85['replies']} {rbad['replies']} {allv} {rall['replies']}")
    # split layout: B-roll in the top panel (dimmed under the card), then full screen; it never adds length
    red = ROOT / "redbroll.mp4"
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xE01010:s=640x360:r=30:d=3",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(red)], check=True)
    with open(red, "rb") as f:
        client.post(f"/api/projects/{p3}/upload?kind=source", data={"files": (f, "redbroll.mp4")},
                    content_type="multipart/form-data")
    est_before = client.get(f"/api/projects/{p3}").get_json()["totals"]["est"]
    post(f"/api/projects/{p3}/analyze")
    wait_job(p3, "analyze")
    wait_job(p3, "preview")
    pj3 = client.get(f"/api/projects/{p3}").get_json()
    redsc = [s for s in pj3["scenes"] if s["src"] == "redbroll.mp4"]
    check("new no-speech clip plays as its own scene (edit grows by its length)",
          redsc and not redsc[0].get("role") and pj3["totals"]["est"] - est_before > 2.5,
          f"{est_before} -> {pj3['totals']['est']}")
    for x in redsc:
        post(f"/api/projects/{p3}/command", {"scene": x["id"], "text": "role broll"})
    pj3 = client.get(f"/api/projects/{p3}").get_json()
    covd = [s for s in pj3["scenes"] if (s.get("broll") or {}).get("src") == "redbroll.mp4"]
    check("split layout: 'role broll' puts the clip in the TOP panel, edit length back to before",
          covd and covd[0]["broll_view"]["where"] == "top" and abs(pj3["totals"]["est"] - est_before) < 0.05,
          f"{est_before} -> {pj3['totals']['est']}")

    def frame_px(path, x, y, t):
        b = subprocess.run([core.FFMPEG, "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", "-vf",
                            f"format=rgb24,crop=1:1:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True).stdout
        return tuple(b[:3])
    for where in ("top", "full"):
        post(f"/api/projects/{p3}/command", {"scene": None, "text": f"broll {where}"})
        post(f"/api/projects/{p3}/render", {"kind": "preview"})
        jr = wait_job(p3, "preview")
        d3 = core.load(p3)
        pv = core.work_dir(p3) / d3["renders"][-1]["file"]
        w3, h3 = pipeline.target_size(d3, True)
        th, _ = pipeline.split_heights(h3, d3["settings"])
        at = client.get(f"/api/projects/{p3}").get_json()
        t0 = next(s["start_at"] for s in at["scenes"] if (s.get("broll") or {}).get("src") == "redbroll.mp4") + 0.4
        top_px, low_px = frame_px(pv, 20, 20, t0), frame_px(pv, 20, th + 60, t0)
        if where == "top":
            check("B-roll shows in the top panel (red, dimmed under the card), speaker area untouched",
                  jr["status"] == "done" and top_px[0] > top_px[1] + 50 and not (low_px[0] > low_px[1] + 80),
                  f"top {top_px} low {low_px} {jr.get('error') or ''}")
        else:
            check("B-roll 'full' covers the whole frame (voice continues)",
                  jr["status"] == "done" and low_px[0] > low_px[1] + 80 and top_px[0] > top_px[1] + 80,
                  f"top {top_px} low {low_px} {jr.get('error') or ''}")
    core.mutate(p3, lambda d: [s.update(keep=False) for s in d["scenes"] if s.get("role") != "broll"])
    post(f"/api/projects/{p3}/render", {"kind": "preview"})
    jx = wait_job(p3, "preview")
    check("only B-roll left -> clear error, not an empty video", jx["status"] == "error" and "B-roll" in (jx.get("error") or ""),
          jx.get("error") or "")
    core.mutate(p3, lambda d: [s.update(keep=True) for s in d["scenes"]])

    # ---------------------------------------------------------------- security
    check("foreign Host header blocked", client.get("/api/projects", headers={"Host": "evil.com"}).status_code == 403)
    check("cross-site POST blocked",
          client.post("/api/projects", json={"name": "x"}, headers={"Origin": "https://evil.com"}).status_code == 403)
    check("path traversal blocked", client.get(f"/media/{pid}/raw/..%2F..%2FExports%2F{out.name}").status_code == 404)
    client.get("/media/ghost-project/work/x.mp4")
    check("media GET creates no folders", not (core.STORY / "ghost-project").exists())

    # ---------------------------------------------------------------- cross-process lock
    code = ("import sys;sys.path.insert(0,r'%s');import core\n"
            "for _ in range(40):\n core.mutate('%s', lambda d: d.__setitem__('counter', d.get('counter',0)+1))") % (HERE, pid)
    core.mutate(pid, lambda d: d.__setitem__("counter", 0))
    env = {**os.environ, "CVE_ROOT": str(ROOT)}
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env) for _ in range(3)]
    for _ in range(40):
        core.mutate(pid, lambda d: d.__setitem__("counter", d.get("counter", 0) + 1))
    for p in procs:
        p.wait()
    n = core.load(pid)["counter"]
    check("4 writers x 40 updates, none lost (cross-process lock)", n == 160, f"counter={n}")

    # ---------------------------------------------------------------- audit 2026-11-20
    # file names: any language kept (Hindi vowel signs survive), Windows-forbidden / reserved names made safe
    check("file names: Hindi kept as written, forbidden characters + reserved names (con/nul) made safe",
          core.safe_filename("मार्केट update.MP4") == "मार्केट update.mp4" and core.safe_filename('a<b>:c?.mp4') == "a_b_c_.mp4"
          and core.safe_filename("con.mp4") == "_con.mp4" and core.slugify("nul") == "nul-project"
          and core.safe_filename("../../x.mp4") == "x.mp4")
    # damaged project.json: restored from the last good copy; the damaged file is kept; never silently vanishes
    pr = post("/api/projects", {"name": "damage test"}).get_json()["id"]
    post(f"/api/projects/{pr}/brief", {"brief": "first"})
    post(f"/api/projects/{pr}/brief", {"brief": "second"})  # 2nd save -> .bak holds a good copy
    pf = core.project_dir(pr) / "project.json"
    pf.write_text("{ half written", encoding="utf-8")
    rr = client.get(f"/api/projects/{pr}")
    kept = [x.name for x in core.project_dir(pr).iterdir() if x.name.startswith("project.json.damaged")]
    pf.write_text("{ broken", encoding="utf-8")
    (core.project_dir(pr) / "project.json.bak").write_text("also broken", encoding="utf-8")
    r2 = client.get(f"/api/projects/{pr}")
    listed = [x for x in client.get("/api/projects").get_json() if x["id"] == pr]
    check("damaged project file: restored from backup (damaged copy kept); unrecoverable one still LISTED with a clear error",
          rr.status_code == 200 and kept and r2.status_code == 500 and "footage is safe" in r2.get_json()["error"]
          and listed and listed[0].get("damaged"), f"{rr.status_code} {kept} {r2.status_code} {listed}")
    shutil.rmtree(core.project_dir(pr), ignore_errors=True)
    # an export deleted from the Exports folder is flagged, not shown as a dead player
    d_ = core.load(pid)
    fin = next((r for r in reversed(d_["renders"]) if r["kind"] == "final"), None)
    if fin:
        fp_ = core.EXPORTS / fin["file"]
        moved = fp_.with_name(fp_.name + ".away")
        fp_.rename(moved)
        miss = next(r for r in client.get(f"/api/projects/{pid}").get_json()["renders"] if r["file"] == fin["file"])
        moved.rename(fp_)
        check("export deleted outside the studio is flagged 'missing'", miss.get("missing") is True)
    # HDR (iPhone HLG) footage is detected and tone-mapped – not washed out
    hlg = ROOT / "hlg.mp4"
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1", "-vf",
                    "zscale=t=arib-std-b67:p=bt2020:m=bt2020nc:r=tv:tin=bt709:pin=bt709:min=bt709,format=yuv420p10le",
                    "-c:v", "libx265", "-x265-params", "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
                    str(hlg)], check=True)

    def sat(vf):
        b = subprocess.run([core.FFMPEG, "-v", "error", "-ss", "0.5", "-i", str(hlg), "-frames:v", "1", "-vf",
                            vf + "scale=32:18,format=rgb24", "-f", "rawvideo", "-"], capture_output=True).stdout
        return sum(max(b[i:i + 3]) - min(b[i:i + 3]) for i in range(0, len(b), 3)) / (len(b) / 3)
    s_naive, s_tm = sat(""), sat(",".join(pipeline.hdr_chain()) + ",")
    check("HDR (HLG) footage detected and tone-mapped (more colour than a plain conversion)",
          pipeline.is_hdr(hlg) and not pipeline.is_hdr(ROOT / "redbroll.mp4") and s_tm > s_naive * 1.5,
          f"naive {s_naive:.0f} tone-mapped {s_tm:.0f}")
    # a sound-only .mp4 says why instead of staying "not analysed" with no message
    ao = ROOT / "soundonly.mp4"
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:d=2", "-c:a", "aac", str(ao)], check=True)
    pa = post("/api/projects", {"name": "audio only"}).get_json()["id"]
    with open(ao, "rb") as fh:
        client.post(f"/api/projects/{pa}/upload?kind=source", data={"files": (fh, "soundonly.mp4")}, content_type="multipart/form-data")
    post(f"/api/projects/{pa}/analyze")
    wait_job(pa, "analyze")
    sa = core.load(pa)["sources"][0]
    check("sound-only .mp4: clear error ('sound only, no picture'), not a silent 'not analysed'",
          "sound only" in (sa.get("error") or ""), str(sa.get("error")))
    # de-click: a jump cut through a tone must not leave a waveform jump (was ~0.8x amplitude)
    tone = ROOT / "tone.mov"
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x568:r=30:d=3", "-f", "lavfi", "-i",
                    "sine=f=400:d=3:sample_rate=48000", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le",
                    "-shortest", str(tone)], check=True)
    pt = post("/api/projects", {"name": "declick"}).get_json()["id"]
    with open(tone, "rb") as fh:
        client.post(f"/api/projects/{pt}/upload?kind=source", data={"files": (fh, "tone.mov")}, content_type="multipart/form-data")
    post(f"/api/projects/{pt}/analyze")
    wait_job(pt, "analyze")
    wait_job(pt, "preview")
    core.mutate(pt, lambda d: d["scenes"][0].update(cuts=[[1.0013, 1.5071]], captions=False))
    outp = pipeline.render_project(pt, preview=False)
    wv = ROOT / "tone.wav"
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-i", str(outp), "-ac", "1", "-ar", "48000", str(wv)], check=True)
    ww = wave.open(str(wv))
    xs = struct.unpack(f"<{ww.getnframes()}h", ww.readframes(ww.getnframes()))
    ww.close()
    ampl = max(abs(v) for v in xs[int(1.6 * 48000):int(2.0 * 48000)])
    jump = max(abs(xs[i + 1] - xs[i]) for i in range(int(0.9 * 48000), int(1.1 * 48000)))
    check("jump cut has no click (biggest waveform step < 0.15 x amplitude; hard cut was ~0.8 x)",
          jump < 0.15 * ampl, f"step {jump} vs amplitude {ampl}")
    # yt-dlp failures in plain words
    _orig = pipeline.run
    pipeline.run = lambda *a, **k: (_ for _ in ()).throw(pipeline.PipelineError("ERROR: Private video. Sign in"))
    try:
        pipeline.download_reference(pid, "https://x.example/v")
        dl = ""
    except pipeline.PipelineError as e:
        dl = str(e)
    finally:
        pipeline.run = _orig
    check("reference download errors in plain words (private / login)", "private or needs a login" in dl, dl)
    # old split pieces glue back: same beat + touching + same settings -> one scene, zoomed piece = zoom moment,
    # a trimmed piece edge = a cut; a piece the user changed (speed) stays separate
    mk = lambda i, a, b, oa, ob, z, **k: {"id": i, "src": "f.mp4", "in": a, "out": b, "orig_in": oa, "orig_out": ob,  # noqa: E731
                                          "zoom": z, "keep": True, "speed": 1.0, "sb_applied": "S", **k}
    dm = {"scenes": [mk("a", 0.1, 1.6, 0.0, 1.6, 1.12, card={"kind": "title", "text": "x"}), mk("b", 1.6, 3.7, 1.6, 3.7, 1.0),
                     mk("c", 5.0, 6.0, 3.7, 6.0, 1.0), mk("d", 6.0, 7.0, 6.0, 7.0, 1.0, speed=1.5),
                     dict(mk("e", 9, 10, 9, 10, 1.0), sb_applied=None), dict(mk("f", 10, 11, 10, 11, 1.0), sb_applied=None)]}
    nm = storyboard.merge_punch_splits(dm)
    m0 = dm["scenes"][0]
    check("merge old punch-in pieces: one scene per beat, zoom moment kept, trimmed edge -> cut, changed piece / "
          "unrelated scenes untouched",
          nm == 2 and [x["id"] for x in dm["scenes"]] == ["a", "d", "e", "f"] and m0["in"] == 0.1 and m0["out"] == 6.0
          and m0["punches"] == [[0.1, 1.6, 1.12]] and m0["zoom"] == 1.0 and m0["cuts"] == [[3.7, 5.0]]
          and m0["card"]["text"] == "x", str(dm["scenes"]))
    # zoom moment = punch-in INSIDE one scene: zoomed only during its window, no frames lost, still one scene
    box = ROOT / "box.mov"
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0xE01010:s=320x568:r=30:d=3,drawbox=x=0:y=0:w=320:h=568:color=white:t=24",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(box)], check=True)
    pz_ = post("/api/projects", {"name": "zoom moment"}).get_json()["id"]
    with open(box, "rb") as fh:
        client.post(f"/api/projects/{pz_}/upload?kind=source", data={"files": (fh, "box.mov")}, content_type="multipart/form-data")
    post(f"/api/projects/{pz_}/analyze")
    wait_job(pz_, "analyze")
    wait_job(pz_, "preview")
    sz = core.load(pz_)["scenes"][0]["id"]
    rp = post(f"/api/projects/{pz_}/command", {"scene": sz, "text": "punch 1-2 1.5"}).get_json()
    split_ok = pipeline.render_project(pz_, preview=True).exists()  # Brand Kit split layout renders with a zoom moment
    core.mutate(pz_, lambda d: d["settings"].update(layout="full", format="original", captions=False))
    outz = pipeline.render_project(pz_, preview=False)

    def corner(t):
        b = subprocess.run([core.FFMPEG, "-v", "error", "-ss", str(t), "-i", str(outz), "-frames:v", "1", "-vf",
                            "crop=1:1:6:6,format=rgb24", "-f", "rawvideo", "-"], capture_output=True).stdout
        return tuple(b[:3])
    c0, c1, c2 = corner(0.5), corner(1.5), corner(2.5)
    white = lambda c: min(c) > 200  # noqa: E731
    red = lambda c: c[0] > 150 and c[1] < 90  # noqa: E731
    check("zoom moment: punch-in only inside its window (border visible before/after, zoomed past it during), "
          "same length, still ONE scene",
          white(c0) and red(c1) and white(c2) and abs(pipeline.probe(outz)["duration"] - 3.0) < 0.1
          and len(core.load(pz_)["scenes"]) == 1 and "stays one scene" in rp["replies"][0] and split_ok,
          f"{c0} {c1} {c2} {rp['replies']}")

    # ---------------------------------------------------------------- 🖼 PICTURES FOR THE STORY (pictures.py)
    import pictures as pics_mod
    pp = lambda b: post(f"/api/projects/{pid}/pictures", b)  # noqa: E731
    pfiles = {"powell.png": ("0xE01010", "1920x1080"), "market chart.png": ("0x10C020", "1600x900"),
              "IMG_0001.png": ("0x1030E0", "1080x1920")}
    for n_, (c_, s_) in pfiles.items():
        subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={c_}:s={s_}", "-frames:v", "1",
                        str(ROOT / n_)], check=True)
    for n_ in pfiles:
        with open(ROOT / n_, "rb") as fh:
            client.post(f"/api/projects/{pid}/upload?kind=asset", data={"files": (fh, n_)}, content_type="multipart/form-data")
    ra = pp({"op": "add", "files": ["IMG_0001.png", "powell.png", "market chart.png", "nope.png", "tone.wav"]}).get_json()
    ra2 = pp({"op": "add", "files": ["powell.png"]}).get_json()
    check("pictures: added in the order given; missing file / sound / a second copy refused",
          ra["added"] == ["IMG_0001.png", "powell.png", "market chart.png"] and len(ra["skipped"]) == 2
          and not ra2["added"], f"{ra} {ra2}")
    # the words: your typed script says "Fed interest rate" in one scene, "gold" in the next (story order test)
    dp0 = core.load(pid)
    vp = [s_ for s_ in dp0["scenes"] if pics_mod._in_video(dp0, s_)]
    lens_ = {x["id"]: x["len"] for x in pics_mod.scenes_info(pid, dp0)}
    ia = max(range(1, len(vp) - 1), key=lambda i: lens_[vp[i]["id"]])  # room for two pictures
    sA, sB = vp[ia], vp[ia + 1]
    typed_before = dp0.get("caption_typed")
    w_ = [["Fed", sA["in"] + 1.0, sA["in"] + 1.3], ["interest", sA["in"] + 1.35, sA["in"] + 1.7],
          ["rate", sA["in"] + 1.75, sA["in"] + 2.0], ["gold", sB["in"] + 0.5, sB["in"] + 0.8]]
    core.mutate(pid, lambda d: d.__setitem__("caption_typed", {sA["src"]: {"words": w_, "cover": []}}))
    rr = pp({"op": "run", "claude": False}).get_json()
    dp1 = core.load(pid)
    tkp = next((t_ for t_ in dp1["tracks"] if t_.get("pictures")), None)
    ent = {e_["file"]: e_ for e_ in dp1["pictures"]["items"]}
    pw, ch, im = ((ent[k_].get("placed") or [{}])[0] for k_ in ("powell.png", "market chart.png", "IMG_0001.png"))
    infoA = next(x for x in pics_mod.scenes_info(pid, dp1) if x["id"] == sA["id"])
    fed_t = next(t_ for n_, t_, _ in infoA["words"] if n_ == "fed")
    check("pictures: Powell on the scene that says “Fed” (a beat before the word), the chart straight after it, the "
          "nameless one by story order on the next scene; one Pictures track; fit = whole picture unless frame-shaped",
          tkp and len(tkp["items"]) == 3 and pw.get("anchor") == sA["id"] and abs(pw["off"] - max(0.15, fed_t - 0.15)) < 0.05
          and ch.get("anchor") == sA["id"] and abs(ch["off"] - (pw["off"] + pw["dur"])) < 0.05
          and im.get("anchor") not in (None, sA["id"]) and rr["placed"] == 3,
          f"{rr.get('report')} fed@{fed_t} lenA={infoA['len']}")
    # your fine-tuning on the timeline survives ▶ Place again; a deleted block stays out
    post(f"/api/projects/{pid}/tracks", {"op": "item_set", "track": tkp["id"], "item": ch["item"], "set": {"off": 0.2, "dur": 1.5}})
    post(f"/api/projects/{pid}/tracks", {"op": "item_remove", "track": tkp["id"], "item": im["item"]})
    pp({"op": "run", "claude": False})
    dp2 = core.load(pid)
    ent2 = {e_["file"]: e_ for e_ in dp2["pictures"]["items"]}
    tk2 = next(t_ for t_ in dp2["tracks"] if t_.get("pictures"))
    ch2 = ent2["market chart.png"]
    check("pictures: a block you moved keeps your spot after ▶ Place again (pinned as yours); a block you deleted "
          "stays out; no duplicate blocks",
          ch2["pin"] and ch2["pin"]["by"] == "you" and abs(ch2["placed"][0]["off"] - 0.2) < 0.01
          and abs(ch2["placed"][0]["dur"] - 1.5) < 0.01 and ent2["IMG_0001.png"].get("off") and len(tk2["items"]) == 2,
          f"{ch2} {[x.get('off') for x in tk2['items']]}")
    post(f"/api/projects/{pid}/render", {"kind": "preview"})
    jp = wait_job(pid, "preview")
    redok = False
    if jp["status"] == "done":
        rec = core.load(pid)["renders"][-1]
        fp_ = core.work_dir(pid) / rec["file"]
        st_ = {i: a_ for i, a_, _ in rec["times"]}[sA["id"]]
        mp_ = pipeline.probe(fp_)
        px = subprocess.run([core.FFMPEG, "-v", "error", "-ss", f"{st_ + ent2['powell.png']['placed'][0]['off'] + 0.5:.3f}",
                             "-i", str(fp_), "-frames:v", "1", "-vf",
                             f"format=rgb24,crop=1:1:{int(mp_['width'] * 0.08)}:{int(mp_['height'] * 0.2)}", "-f", "rawvideo", "-"],
                            capture_output=True).stdout
        redok = len(px) >= 3 and px[0] > 150 and px[1] < 90
    check("pictures render: Powell's picture is on screen during its moment", jp["status"] == "done" and redok,
          jp.get("error") or "")
    # Claude is asked ONCE to look at the pictures (only when auto-reply is on and not every picture is yours)
    real_en, real_exe = appmod.claudebot.enabled, appmod.claudebot.exe
    appmod.claudebot.enabled, appmod.claudebot.exe = (lambda: True), (lambda: "claude")
    try:
        q1 = pp({"op": "run"}).get_json()
        q2 = pp({"op": "run"}).get_json()
    finally:
        appmod.claudebot.enabled, appmod.claudebot.exe = real_en, real_exe
    waiting = [c_ for c_ in core.load(pid)["commands"] if c_.get("status") == "claude" and c_.get("kind") == "pictures"]
    desc = pics_mod.describe(pid, core.load(pid))
    check("pictures: 🤖 Claude asked once (not per click), with the project id; `pictures show` lists picture paths "
          "+ every scene's words with times",
          q1["claude"] and not q2["claude"] and len(waiting) == 1 and pid in waiting[0]["text"]
          and "FILE powell.png  look_at=" in desc and "Fed@" in desc, f"{q1.get('claude')} {q2.get('claude')} {len(waiting)}")
    for c_ in waiting:
        post(f"/api/projects/{pid}/command/{c_['id']}/cancel")
    ok_pin = pics_mod.set_pin(core.load(pid), "market chart.png", sB["id"], by="claude")
    depth = core.history_depth(pid)
    rc = pp({"op": "clear"}).get_json()
    dp3 = core.load(pid)
    post(f"/api/projects/{pid}/undo")
    dp4 = core.load(pid)
    check("pictures: Claude can't move a picture you pinned; Remove all clears list + track (one Undo brings both back)",
          ok_pin.startswith("You pinned") and not dp3["pictures"]["items"] and not any(t_.get("pictures") for t_ in dp3["tracks"])
          and len(dp4["pictures"]["items"]) == 3 and any(t_.get("pictures") for t_ in dp4["tracks"])
          and True,
          f"{rc} pin={ok_pin!r} items3={len(dp3['pictures']['items'])} trk3={[t_.get('pictures') for t_ in dp3['tracks']]} "
          f"items4={len(dp4['pictures']['items'])} depth {depth}->{core.history_depth(pid)}")
    # ---- clips: a fire transition on black (+ whoosh) goes on each "pehla / doosra … reason"; a transparent blinking
    # GIF is a corner sticker that animates; a plain GIF is a cutaway
    fire_f, stk_f, meme_f = ROOT / "fire transition.mp4", ROOT / "thumbs up.gif", ROOT / "meme.gif"
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=640x360:d=1.2:r=30,drawbox=x=220:y=80:w=200:h=200:color=0xFF7A10:t=fill",
                    "-f", "lavfi", "-i", "sine=f=300:d=1.2", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(fire_f)], check=True)
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black@0.0:s=200x200:d=1:r=10,format=rgba",
                    "-f", "lavfi", "-i", "color=c=red:s=80x80:d=1:r=10,format=rgba", "-filter_complex",
                    "[0:v][1:v]overlay=40:50:enable='lt(mod(t,1),0.5)',split[a][b];[a]palettegen=reserve_transparent=1[p];"
                    "[b][p]paletteuse=alpha_threshold=128", str(stk_f)], check=True)
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x240:d=1:r=10", str(meme_f)], check=True)
    for f_ in (fire_f, stk_f, meme_f):
        with open(f_, "rb") as fh:
            client.post(f"/api/projects/{pid}/upload?kind=asset", data={"files": (fh, f_.name)}, content_type="multipart/form-data")
    rc_ = pp({"op": "add", "files": [fire_f.name, stk_f.name, meme_f.name]}).get_json()
    dc = core.load(pid)
    roles_ = {f_: pics_mod.role_of(pid, pics_mod.entry(dc, f_)) for f_ in (fire_f.name, stk_f.name, meme_f.name)}
    check("clips: roles detected – fire on black = transition, transparent GIF = sticker, plain GIF = cutaway; the "
          "page is told what each became",
          roles_ == {fire_f.name: "transition", stk_f.name: "sticker", meme_f.name: "cutaway"}
          and pics_mod.entry(dc, fire_f.name)["media"]["blend"] == "screen" and "transition" in rc_.get("reply", "").lower(),
          f"{roles_} {rc_.get('reply')}")
    vC = [s_ for s_ in dc["scenes"] if pics_mod._in_video(dc, s_)]
    sC = vC[min(len(vC) - 1, ia + 2)]
    w2 = w_ + [["doosra", sB["in"] + 1.2, sB["in"] + 1.5], ["bada", sB["in"] + 1.55, sB["in"] + 1.8],
               ["reason", sB["in"] + 1.85, sB["in"] + 2.2]]
    if sC["id"] != sB["id"]:
        w2 += [["teesra", sC["in"] + 1.2, sC["in"] + 1.5], ["reason", sC["in"] + 1.55, sC["in"] + 1.9]]
    core.mutate(pid, lambda d: d.__setitem__("caption_typed", {sA["src"]: {"words": sorted(w2, key=lambda x: x[1]), "cover": []}}))
    pp({"op": "run", "claude": False})
    dc2 = core.load(pid)
    tkc = next(t_ for t_ in dc2["tracks"] if t_.get("pictures"))
    items_ = {x["id"]: x for x in tkc["items"]}
    fe = pics_mod.entry(dc2, fire_f.name)
    fire_items = [items_[r_["item"]] for r_ in fe["placed"]]
    ms_ = pics_mod.moments(pics_mod.scenes_info(pid, dc2))
    info2 = {x["id"]: x for x in pics_mod.scenes_info(pid, dc2)}
    mids = sorted(round(info2[x["anchor"]]["start"] + x["off"] + x["dur"] / 2, 1) for x in fire_items)
    se = pics_mod.entry(dc2, stk_f.name)
    sit = items_[se["placed"][0]["item"]] if se["placed"] else {}
    check("clips: the fire transition sits on EVERY new-point moment (its middle on the word), black blended away, "
          "with its sound; one Video track for all; the GIF sticker loops in the corner, see-through",
          tkc["kind"] == "video" and len(fire_items) == len(ms_) >= 1
          and all(abs(m_ - round(mm["t"], 1)) <= 0.2 for m_, mm in zip(mids, ms_))
          and all(x.get("blend") == "screen" and x.get("vol", 0) > 0 for x in fire_items)
          and sit.get("where") == "pip" and sit.get("blend") == "alpha" and sit.get("loop"),
          f"moments {[round(m['t'], 1) for m in ms_]} mids {mids} kind={tkc['kind']} sticker={sit}")
    post(f"/api/projects/{pid}/render", {"kind": "preview"})
    jc = wait_job(pid, "preview")
    fire_ok = stk_ok = False
    det = ""
    if jc["status"] == "done":
        recc = core.load(pid)["renders"][-1]
        fpc = core.work_dir(pid) / recc["file"]
        stc = {i: a_ for i, a_, _ in recc["times"]}
        mpc = pipeline.probe(fpc)
        W_, H_ = mpc["width"], mpc["height"]

        def pxc(t, x, y):
            b_ = subprocess.run([core.FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(fpc), "-frames:v", "1", "-vf",
                                 f"format=rgb24,crop=1:1:{int(x)}:{int(y)}", "-f", "rawvideo", "-"], capture_output=True).stdout
            return tuple(b_[:3]) if len(b_) >= 3 else (0, 0, 0)
        fi = fire_items[0]
        t_mid = stc[fi["anchor"]] + fi["off"] + fi["dur"] / 2
        t_before = stc[fi["anchor"]] + fi["off"] - 0.1
        spots = [(W_ * fx_, H_ * fy_) for fx_, fy_ in ((0.04, 0.04), (0.96, 0.04), (0.04, 0.5), (0.96, 0.5), (0.2, 0.15), (0.8, 0.15))]
        sx, sy = max(spots, key=lambda s_: sum(pxc(t_before, *s_)))  # a bright spot of YOUR picture, outside the flame
        c_mid, c_corner, c_corner0 = pxc(t_mid, W_ * 0.5, H_ * 0.3), pxc(t_mid, sx, sy), pxc(t_before, sx, sy)
        fire_ok = c_mid[0] > 170 and 50 < c_mid[1] < 170 and c_mid[2] < 90 and max(abs(a_ - b_) for a_, b_ in zip(c_corner, c_corner0)) < 45 and max(c_corner0) > 60
        # sticker box (pip, top-right): the GIF's red square blinks (on 0-0.5 s of each second)
        bw_, bh_ = round(W_ * 0.30) // 2 * 2, round(H_ * 0.19) // 2 * 2  # a sticker's (small) corner box
        x0_, y0_ = W_ - bw_ - round(W_ * 0.04) - 6, round(H_ * 0.09)
        s_ = min(bw_ / 200, bh_ / 200)
        gx, gy = x0_ + (bw_ - 200 * s_) / 2 + 80 * s_, y0_ + (bh_ - 200 * s_) / 2 + 90 * s_
        t0s = stc[sit["anchor"]] + sit["off"]
        reds = [pxc(t0s + dt, gx, gy) for dt in (0.25, 0.75, 1.25, 1.75)]
        isred = [c[0] > 170 and c[1] < 90 and c[2] < 90 for c in reds]
        stk_ok = isred[0] and not isred[1] and isred[2] and not isred[3]
        det = f"fire mid {c_mid} corner {c_corner} vs {c_corner0} sticker {reds}"
    check("clips render: fire glows over you with the black gone (your picture shows around it); the GIF sticker "
          "animates (blinks) for its whole block", jc["status"] == "done" and fire_ok and stk_ok, (jc.get("error") or "") + det)
    # ---- audit: the ways it can still go wrong
    import struct as _st
    land = ROOT / "landscape raw.jpg"
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=400x200", "-frames:v", "1",
                    str(land)], check=True)
    raw_ = land.read_bytes()
    tiff_ = b"MM" + _st.pack(">HI", 42, 8) + _st.pack(">H", 1) + _st.pack(">HHIHH", 0x0112, 3, 1, 6, 0) + _st.pack(">I", 0)
    app1_ = b"Exif\x00\x00" + tiff_
    phone = ROOT / "phone photo.jpg"  # stored 400x200 + EXIF "turn 90°" = a PORTRAIT photo on the phone
    phone.write_bytes(raw_[:2] + b"\xff\xe1" + _st.pack(">H", len(app1_) + 2) + app1_ + raw_[2:])
    mph = pics_mod.inspect(phone)
    dA = core.load(pid)
    fe2 = pics_mod.entry(dA, fire_f.name)
    fire_vol = [x.get("vol") for x in next(t_ for t_ in dA["tracks"] if t_.get("pictures"))["items"]
                if x["src"]["n"] == fire_f.name]
    pw_it = next(x for x in next(t_ for t_ in dA["tracks"] if t_.get("pictures"))["items"] if x["src"]["n"] == "powell.png")
    check("audit: a phone photo is measured the way it is SHOWN (EXIF turn); a transition's sound sits 8 dB under "
          "YOUR voice (not 90 % of a loud pack); cutaways keep their shape so each export frame picks fill / fit",
          mph["w"] < mph["h"] and fire_vol and (dA["pictures"].get("voice") or {}).get("db") is not None
          and all(abs(20 * math.log10(v) + fe2["media"]["snd_db"] - (dA["pictures"]["voice"]["db"] - 8)) < 1.0
                  for v in fire_vol)
          and pw_it.get("fit_auto") and abs(pw_it.get("ar", 0) - 1920 / 1080) < 0.01,
          f"phone {mph.get('w')}x{mph.get('h')} vol {fire_vol} snd {fe2['media'].get('snd_db')} voice {dA['pictures'].get('voice')} powell {pw_it}")
    tid_ = next(t_ for t_ in dA["tracks"] if t_.get("pictures"))["id"]
    post(f"/api/projects/{pid}/tracks", {"op": "lock", "track": tid_})
    lk_rm = pp({"op": "remove", "file": "powell.png"})
    lk_run = pp({"op": "run", "claude": False})
    post(f"/api/projects/{pid}/tracks", {"op": "lock", "track": tid_})
    post(f"/api/projects/{pid}/tracks", {"op": "remove", "track": tid_})  # you delete the whole track
    pp({"op": "run", "claude": False})
    dB = core.load(pid)
    live_b = [e_ for e_ in dB["pictures"]["items"] if not e_.get("off")]
    check("audit: a LOCKED track refuses Remove / Place; after you delete the whole track, ▶ Place starts fresh "
          "(nothing marked 'you removed it')",
          lk_rm.status_code == 400 and lk_run.status_code == 400 and "locked" in lk_rm.get_json()["error"]
          and any(t_.get("pictures") for t_ in dB["tracks"]) and all(pics_mod._records(e_) for e_ in live_b
                                                                      if e_["file"] != "IMG_0001.png"),
          f"{lk_rm.get_json()} {[(e_['file'], bool(e_.get('off')), len(pics_mod._records(e_))) for e_ in dB['pictures']['items']]}")
    # undo of something unrelated keeps a file you added afterwards; cutting a picture's scene shows a warning
    with open(land, "rb") as fh:
        client.post(f"/api/projects/{pid}/upload?kind=asset", data={"files": (fh, land.name)}, content_type="multipart/form-data")
    post(f"/api/projects/{pid}/command", {"scene": sC["id"], "text": "speed 1.1"})
    pp({"op": "add", "files": [land.name]})
    post(f"/api/projects/{pid}/undo")
    kept_new = pics_mod.entry(core.load(pid), land.name) is not None
    pw_anchor = pics_mod._records(pics_mod.entry(core.load(pid), "powell.png"))[0]["anchor"]
    post(f"/api/projects/{pid}/command", {"scene": pw_anchor, "text": "cut"})
    view_ = client.get(f"/api/projects/{pid}").get_json()
    stale_ = next(e_ for e_ in view_["pictures"]["items"] if e_["file"] == "powell.png").get("stale")
    post(f"/api/projects/{pid}/undo")
    # ---- a change to a placed picture shows AT ONCE; every "Use as" is visibly different; Claude isn't asked twice
    def blk(file_):
        dd_ = core.load(pid)
        rec_ = pics_mod._records(pics_mod.entry(dd_, file_))
        tk_ = next(t_ for t_ in dd_["tracks"] if t_.get("pictures"))
        return next((x for x in tk_["items"] if rec_ and x["id"] == rec_[0]["item"]), {})
    looks = {}
    for role_ in ("pip", "overlay", "sticker", "cutaway"):
        rr_ = pp({"op": "role", "file": "market chart.png", "as": role_}).get_json()
        b_ = blk("market chart.png")
        looks[role_] = (rr_.get("applied"), b_.get("where"), b_.get("blend"), b_.get("box"))
    check("roles: changing “Use as” re-places at once (no extra click); corner box = framed box, overlay = half "
          "see-through over you, sticker = smaller box, cutaway = full screen",
          looks["pip"] == (True, "pip", None, None) and looks["overlay"] == (True, "full", "soft", None)
          and looks["sticker"] == (True, "pip", None, "small") and looks["cutaway"][:3] == (True, "full", None), str(looks))
    real_en2, real_exe2 = appmod.claudebot.enabled, appmod.claudebot.exe
    appmod.claudebot.enabled, appmod.claudebot.exe = (lambda: True), (lambda: "claude")
    try:
        for c_ in core.load(pid)["commands"]:
            if c_.get("status") == "claude":
                post(f"/api/projects/{pid}/command/{c_['id']}/cancel")

        def pin_all(d):
            for e_ in d["pictures"]["items"]:
                if not e_.get("pin") and e_.get("as") not in ("transition", "transition_once"):
                    sc_ = next(s_ for s_ in d["scenes"] if pics_mod._in_video(d, s_))
                    e_["pin"] = {"scene": sc_["id"], "by": "claude"}
        core.mutate(pid, pin_all)  # as if Claude had looked at every picture
        again = pp({"op": "run"}).get_json()
    finally:
        appmod.claudebot.enabled, appmod.claudebot.exe = real_en2, real_exe2
    # a JPEG whose start time isn't 0: "-ss 0" made ffmpeg encode nothing -> no thumbnail, silently
    odd = ROOT / "odd start.jpg"
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x200", "-frames:v", "1",
                    "-output_ts_offset", "0.04", str(odd)], check=True)
    th_ = ROOT / "odd_thumb.jpg"
    pipeline.thumbnail(odd, 0.0, th_)
    check("Place again doesn't call Claude for pictures it already pinned (each call ~2 min); a picture always gets "
          "its thumbnail (never seeks inside a still)",
          again.get("claude") is False and th_.is_file(), f"claude={again.get('claude')} thumb={th_.is_file()}")
    check("audit: Undo of an earlier edit keeps a file you added after it; a picture whose scene you cut is flagged "
          "(“▶ Place again”) instead of silently vanishing",
          kept_new and stale_ is True, f"kept={kept_new} stale={stale_}")
    core.mutate(pid, lambda d: d.__setitem__("caption_typed", typed_before) if typed_before else d.pop("caption_typed", None))

    # ---------------------------------------------------------------- ⬇ Extract: MP3 of the music only / the voice only
    import extract as xmod
    import refaudio as ra_mod

    def xwait(job_):
        t0_ = time.time()
        while time.time() - t0_ < 600:
            jj_ = next(x for x in xmod.jobs() if x["id"] == job_["id"])
            if jj_["status"] in ("done", "error"):
                return jj_
            time.sleep(1)
        return {"status": "timeout"}
    tune = ROOT / "just a tune.wav"   # music, no voice at all
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=5", "-f", "lavfi", "-i",
                    "sine=f=660:d=5", "-filter_complex", "[0:a][1:a]amix=inputs=2,volume=0.6", str(tune)], check=True)
    long_ = ROOT / "long talk.wav"    # 16 min: over the separation limit
    subprocess.run([core.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=d=960:a=0.05:r=8000", str(long_)], check=True)
    j_mp4 = xmod.start("mp4", src=tune, stem="x", part="music")
    j_long = xwait(xmod.start("mp3", src=long_, stem="long", part="music"))
    if ra_mod.available():
        j_voice = xwait(xmod.start("mp3", src=tune, stem="tune", part="voice"))
        sep_ok = (j_voice["status"] == "done" and "(voice only)" in j_voice["file"]
                  and "Almost no voice" in (j_voice.get("note") or ""))
    else:
        j_voice, sep_ok = {"status": "skipped (voice-remover not installed)"}, True
    check("extract: MP3 'music only' / 'voice only' – a part over 15 min is refused (trim it), the choice is ignored "
          "for MP4, and voice from a tune with no voice comes out with a clear 'almost no voice' note",
          j_mp4["part"] == "all" and j_long["status"] == "error" and "15 minutes" in j_long.get("error", "") and sep_ok,
          f"mp4 part={j_mp4['part']} long={j_long.get('status')}:{j_long.get('error', '')[:80]} voice={j_voice.get('status')} "
          f"{j_voice.get('file')} note={j_voice.get('note')!r}")
    # ---------------------------------------------------------------- 🎙 VOICE: clarity + loudness for every video
    def render_lufs(settings_):
        post(f"/api/projects/{pid}/brief", {"settings": settings_})
        post(f"/api/projects/{pid}/render", {"kind": "preview"})
        jv_ = wait_job(pid, "preview")
        fv_ = core.work_dir(pid) / core.load(pid)["renders"][-1]["file"]
        return jv_, (pipeline.measure_lufs(fv_) if jv_["status"] == "done" else None)
    vc = client.get(f"/api/projects/{pid}/voice_check").get_json()
    st_v0 = dict(core.load(pid)["settings"])
    j_raw, l_raw = render_lufs({"voice": "raw", "music": "off", "audio_polish": False})
    j_nat, l_nat = render_lufs({"voice": "natural", "loudness": -14})
    j_clr, l_clr = render_lufs({"voice": "clear", "loudness": -16})
    auto_mode = pipeline.voice_mode({"voice": "auto", "audio_polish": True}), pipeline.voice_mode({"voice": "auto"})
    bad_v = post(f"/api/projects/{pid}/brief", {"settings": {"voice": "loud!"}}).status_code
    check("voice: every video reaches its loudness (natural -14 / clear -16, +-1) unless 'as recorded'; the old "
          "'clean up voice' switch still means clear; the recording check answers in plain words",
          l_nat is not None and abs(l_nat + 14) <= 1.0 and l_clr is not None and abs(l_clr + 16) <= 1.0
          and j_raw["status"] == "done" and auto_mode == ("clear", "natural") and bad_v == 400
          and isinstance(vc.get("hints"), list) and vc["hints"],
          f"raw {l_raw} natural {l_nat} clear {l_clr} auto {auto_mode} hints {vc.get('hints')}")
    if ra_mod.available():
        n_iso0 = sum("Isolating your voice" in x for x in (core.load(pid).get("job") or {}).get("log", []))
        j_st, l_st = render_lufs({"voice": "studio", "loudness": -16})
        isos_ = list((core.work_dir(pid) / "voice_iso").glob("*.wav")) if (core.work_dir(pid) / "voice_iso").is_dir() else []
        j_st2, _ = render_lufs({"voice": "studio", "loudness": -15})  # second time: the isolated voice is reused
        n_iso2 = sum("Isolating your voice" in x for x in j_st2.get("log", []))
        st_ok = (j_st["status"] == "done" and l_st is not None and abs(l_st + 16) <= 1.0 and isos_
                 and n_iso2 == sum("Isolating your voice" in x for x in j_st.get("log", [])) and n_iso2 > n_iso0)
        st_det = f"studio {j_st['status']} {l_st} isos {len(isos_)} isolating-lines {n_iso0}->{n_iso2}"
    else:
        st_ok, st_det = True, "skipped (voice-remover not installed)"
    check("voice: Studio isolates your voice ONCE per clip (cached, reused next render) and reaches the loudness",
          st_ok, st_det)
    post(f"/api/projects/{pid}/brief", {"settings": {k_: st_v0.get(k_) for k_ in ("voice", "loudness", "audio_polish", "music")}})
except Exception:  # sys.exit in `finally` would otherwise swallow the traceback and report "all passed"
    import traceback
    traceback.print_exc()
    check("self-test ran to the end (crashed – see traceback above)", False)
finally:
    ok = sum(1 for _, k, _ in results if k)
    print(f"\n{ok}/{len(results)} checks passed.  temp root: {ROOT}")
    if ok == len(results):
        shutil.rmtree(ROOT, ignore_errors=True)
    sys.exit(0 if ok == len(results) else 1)
