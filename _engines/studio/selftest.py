"""End-to-end self-test for CVE Studio. Runs in a TEMP root – never touches the real folders.

    .venv\\Scripts\\python.exe selftest.py
"""
from __future__ import annotations

import json
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
    r = post(f"/api/projects/{pid}/command", {"scene": s1, "text": "add a glitch transition"}).get_json()
    check("unknown command goes to Claude inbox", r["queued"] == ["add a glitch transition"])
    post(f"/api/projects/{pid}/command", {"scene": None, "text": "remove silences"})
    post(f"/api/projects/{pid}/command", {"scene": scenes[1]["id"], "text": "text: 100% up; speed 1.2"})

    # ---------------------------------------------------------------- render + A/V sync
    post(f"/api/projects/{pid}/render", {"kind": "final"})
    j = wait_job(pid, "final")
    check("final export renders", j["status"] == "done", j.get("error") or j.get("progress"))
    out = core.EXPORTS / core.load(pid)["renders"][-1]["file"]

    def sdur(path, sel):
        o = subprocess.run([core.FFPROBE, "-v", "error", "-select_streams", sel, "-show_entries", "stream=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
        return float(o or 0)

    diffs = [abs(sdur(p, "a:0") - sdur(p, "v:0")) * 1000 for p in (core.work_dir(pid) / "segs").glob("*.mp4")]
    check("every segment: audio length == video length (<=25 ms)", diffs and max(diffs) <= 25,
          f"max {max(diffs):.0f} ms over {len(diffs)} segments")
    fd = abs(sdur(out, "a:0") - sdur(out, "v:0")) * 1000
    check("final export A/V lengths match (<=40 ms)", fd <= 40, f"{fd:.0f} ms")

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
finally:
    ok = sum(1 for _, k, _ in results if k)
    print(f"\n{ok}/{len(results)} checks passed.  temp root: {ROOT}")
    if ok == len(results):
        shutil.rmtree(ROOT, ignore_errors=True)
    sys.exit(0 if ok == len(results) else 1)
