"""End-to-end self-test for CVE Studio. Runs in a TEMP root – never touches the real folders.

    .venv\\Scripts\\python.exe selftest.py
"""
from __future__ import annotations

import json
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
    z = [s for s in d["scenes"] if s.get("zoom") == 1.1]
    check("storyboard: dashboard served, review saved, approved punch-in = split + zoom, card set",
          page.status_code == 200 and b"Storyboard" in page.data and r.get_json()["status"] == "approved" and
          len(d["scenes"]) == n_before + 2 and len(z) == 1 and abs(z[0]["in"] - (mid - 0.6)) < 0.01 and
          d["scenes"][0].get("card", {}).get("text") == "test *card*")
    again = storyboard.apply(pid)
    r409 = post(f"/api/projects/{pid}/storyboard/{sid0}", {"status": "rejected"})
    check("storyboard apply runs ONCE per beat (no double split); applied beat is locked",
          len(core.load(pid)["scenes"]) == n_before + 2 and "nothing to apply" in again[0] and r409.status_code == 409)
    post(f"/api/projects/{pid}/undo")
    sbu = client.get(f"/api/projects/{pid}/storyboard").get_json()
    check("storyboard apply is undoable – and after undo the beat can be applied again",
          len(core.load(pid)["scenes"]) == n_before and sbu["beats"][0]["applied"] is False and
          "applied" in storyboard.apply(pid)[0])
    post(f"/api/projects/{pid}/undo")
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
    post(f"/api/projects/{pid}/render", {"kind": "final"})
    j = wait_job(pid, "final")
    check("final export renders", j["status"] == "done", j.get("error") or j.get("progress"))
    out = core.EXPORTS / core.load(pid)["renders"][-1]["file"]
    rec = core.load(pid)["renders"][-1]
    tms = rec.get("times") or []
    check("render records REAL per-scene start/length (sum = file length +-0.15 s, starts increasing)",
          tms and abs(sum(x[2] for x in tms) - rec["duration"]) < 0.15 and all(tms[i][1] < tms[i + 1][1] for i in range(len(tms) - 1)),
          f"{tms} vs {rec['duration']}")
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
    check("Remove clip is undoable (its edited scenes come back)", gone and len(core.load(p2)["scenes"]) == n_sc)
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
    check("new project starts from the Brand Kit (bad/unknown keys ignored)",
          st3["layout"] == "split" and st3["music"] == "auto" and st3["silence_min"] == 0.6 and "bogus_key" not in st3)
    d3n = core.load(p3)
    check("new project starts ON the ★ Brand Kit look; 'None · simple' is truly plain",
          d3n["style_source"] == "brandkit" and d3n["looks"]["none"]["layout"] == "full")
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
    check("B-roll commands: top panel warns without split layout; 'broll from 1.5' sets the clip start",
          "split layout" in r_top["replies"][0] and b1["where"] == "top" and b1["at"] == 1.5, str(r_top))
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
