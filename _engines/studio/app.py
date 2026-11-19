"""CVE Studio – local web UI for the Claude Video Editor.

Run:  .venv\\Scripts\\python.exe app.py      then open http://127.0.0.1:8070
"""
from __future__ import annotations

import os
import queue
import re
import threading
import traceback
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import commands
import core
import pipeline

HERE = Path(__file__).parent
PORT = int(os.environ.get("CVE_PORT", "8070"))
app = Flask(__name__, static_folder=str(HERE / "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = None
_ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", "localhost"}  # bare 'localhost' = Flask test client


@app.before_request
def _local_only():
    """Block DNS-rebinding and cross-site requests: any web page you visit could otherwise POST here."""
    if request.host not in _ALLOWED_HOSTS:
        abort(403)
    origin = request.headers.get("Origin")
    if request.method != "GET" and origin and origin.split("://", 1)[-1] not in _ALLOWED_HOSTS:
        abort(403)

# ----------------------------------------------------------------- job worker (one heavy job at a time: 7 GB RAM)

_jobs: "queue.Queue[tuple[str, str, dict]]" = queue.Queue()


def _worker():
    while True:
        pid, kind, args = _jobs.get()
        try:
            def start(d, kind=kind):
                job = d.get("job") or {}
                pend = job.get("pending") or []
                if kind in pend:
                    pend.remove(kind)
                log = (job.get("log") or [])[-40:]  # keep the previous job's log (its errors) visible
                if log:
                    log.append("────────")
                d["job"] = {"kind": kind, "status": "running", "progress": "Starting…", "started": core.now_iso(),
                            "finished": None, "error": None, "log": log, "pending": pend}
            core.mutate(pid, start)
            if kind == "analyze":
                pipeline.analyze_project(pid, reanalyze=args.get("reanalyze", False))
                msg = "Analysis complete."
            elif kind in ("preview", "final"):
                out = pipeline.render_project(pid, preview=(kind == "preview"))
                msg = f"Rendered {out.name}"
            elif kind == "ref_download":
                core.job_log(pid, f"Downloading {args['url']}")
                name = pipeline.download_reference(pid, args["url"])
                core.job_log(pid, f"Saved {name}; analysing")
                info = pipeline.analyze_reference(pid, name)
                core.mutate(pid, lambda d: d["references"].append({"file": name, "url": args["url"], **info}))
                msg = f"Reference downloaded: {name}"
            elif kind == "ref_analyze":
                for name in args["files"]:
                    core.job_log(pid, f"Analysing reference {name}")
                    try:
                        info = pipeline.analyze_reference(pid, name)
                    except Exception as e:  # keep going; mark this one failed instead of "analysing…" forever
                        core.job_log(pid, f"Reference {name} failed: {e}")
                        info = {"kind": "error", "error": str(e)[:300]}

                    def fn(d, name=name, info=info):
                        for r in d["references"]:
                            if r.get("file") == name:
                                r.update(info)
                    core.mutate(pid, fn)
                msg = "References analysed."
            else:
                msg = f"Unknown job {kind}"
            core.job_log(pid, msg)
            core.set_job(pid, status="done", progress=msg, finished=core.now_iso())
        except Exception as e:  # report every failure to the UI rather than dying silently
            traceback.print_exc()
            core.job_log(pid, f"ERROR: {e}")
            core.set_job(pid, status="error", error=str(e), progress="Failed", finished=core.now_iso())
        finally:
            _jobs.task_done()


threading.Thread(target=_worker, daemon=True).start()


def enqueue(pid: str, kind: str, **args):
    def fn(d):
        job = d.get("job") or {}
        if core.job_busy(d):          # don't clobber the running job's status – just note it's waiting
            job.setdefault("pending", []).append(kind)
            d["job"] = job
        else:
            d["job"] = {"kind": kind, "status": "queued", "progress": "Queued", "error": None,
                        "log": job.get("log", []), "pending": []}
    core.mutate(pid, fn)
    _jobs.put((pid, kind, args))


# ----------------------------------------------------------------- helpers

def _exists(pid: str):
    if not core.valid_pid(pid):
        abort(404, "project not found")


def _decorate(data: dict) -> dict:
    """Add derived display fields (estimated durations, numbering)."""
    wd = core.work_dir(data["id"])
    sil_cache: dict[str, list] = {}
    total_orig = total_est = 0.0
    for i, sc in enumerate(data["scenes"], 1):
        stem = Path(sc["src"]).stem
        if stem not in sil_cache:
            sil_cache[stem] = core.read_json(wd / f"{stem}.silences.json", []) or []
        sc["no"] = i
        sc["dur"] = round(sc["out"] - sc["in"], 2)
        sc["est"] = pipeline.estimated_duration(sc, sil_cache[stem], data["settings"])
        total_orig += sc["dur"]
        total_est += sc["est"]
    data["totals"] = {"orig": round(total_orig, 1), "est": round(total_est, 1),
                      "kept": sum(1 for s in data["scenes"] if s.get("keep", True)), "count": len(data["scenes"])}
    data["inbox"] = sum(1 for c in data.get("commands", []) if c["status"] == "claude")
    data["undo_depth"] = core.history_depth(data["id"])
    return data


def _refresh_scene_meta(pid: str, data: dict, scene_ids: set[str], new_thumbs: set[str]):
    wd = core.work_dir(pid)
    words_cache: dict[str, list] = {}
    for sc in data["scenes"]:
        if sc["id"] not in scene_ids:
            continue
        stem = Path(sc["src"]).stem
        if stem not in words_cache:
            words_cache[stem] = core.read_json(wd / f"{stem}.words.json", []) or []
        sc["transcript"] = " ".join(w["w"] for w in pipeline.words_in(words_cache[stem], sc["in"], sc["out"]))
        if sc["id"] in new_thumbs:
            try:
                pipeline.thumbnail(core.raw_dir(pid) / sc["src"], sc["in"] + (sc["out"] - sc["in"]) / 2,
                                   wd / "thumbs" / f"{sc['id']}.jpg")
                sc["thumb"] = f"{sc['id']}.jpg"
            except pipeline.PipelineError:
                pass


# ----------------------------------------------------------------- pages + media

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/media/<pid>/<area>/<path:name>")
def media(pid, area, name):
    if not core.valid_pid(pid):
        abort(404)
    work = core.project_dir(pid) / "work"   # no mkdir here: a GET must never create folders
    base = {
        "raw": core.RAW / pid, "refs": core.REFS / pid, "work": work, "proxy": work / "proxy",
        "thumbs": work / "thumbs", "sheets": work / "refs", "exports": core.EXPORTS, "brand": core.BRAND,
    }.get(area)
    if base is None:
        abort(404)
    p = (base / name).resolve()
    if not p.is_relative_to(base.resolve()) or not p.is_file():
        abort(404)
    resp = send_file(p, conditional=True)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ----------------------------------------------------------------- API

@app.get("/api/projects")
def api_projects():
    return jsonify(core.list_projects())


@app.post("/api/projects")
def api_new_project():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify(error="Give the project a name."), 400
    return jsonify(core.new_project(name))


@app.get("/api/projects/<pid>")
def api_project(pid):
    _exists(pid)
    return jsonify(_decorate(core.load(pid)))


@app.get("/api/brandkit")
def api_brandkit():
    core.BRAND.mkdir(exist_ok=True)
    return jsonify(sorted(p.name for p in core.BRAND.iterdir() if p.is_file()))


@app.post("/api/projects/<pid>/upload")
def api_upload(pid):
    _exists(pid)
    kind = request.args.get("kind", "source")
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="No files received."), 400
    folder = core.raw_dir(pid) if kind == "source" else core.refs_dir(pid)
    saved = []
    for f in files:
        name = core.safe_filename(f.filename or "upload")
        ext = Path(name).suffix.lower()
        if kind == "source" and ext not in core.VIDEO_EXT:
            continue
        dest = core.unique_path(folder, name)
        f.save(dest)
        saved.append(dest.name)
    if not saved:
        return jsonify(error="No supported files (footage must be a video file)."), 400

    def fn(d):
        for n in saved:
            if kind == "source":
                d["sources"].append({"file": n, "analyzed": False, "added": core.now_iso()})
            else:
                d["references"].append({"file": n, "kind": "pending", "added": core.now_iso()})
    core.mutate(pid, fn)
    if kind == "source":
        for n in saved:  # quick probe so duration/resolution show immediately
            try:
                meta = pipeline.probe(folder / n)
                core.mutate(pid, lambda d, n=n, meta=meta: core.source_by_file(d, n).update(meta))
            except Exception:
                pass
    else:
        enqueue(pid, "ref_analyze", files=saved)
    return jsonify(saved=saved)


@app.post("/api/projects/<pid>/source/remove")
def api_remove_source(pid):
    _exists(pid)
    file = (request.json or {}).get("file")

    def fn(d):
        d["sources"] = [s for s in d["sources"] if s["file"] != file]
        d["scenes"] = [s for s in d["scenes"] if s["src"] != file]
    core.mutate(pid, fn)
    return jsonify(ok=True)


@app.post("/api/projects/<pid>/reference/remove")
def api_remove_ref(pid):
    _exists(pid)
    file = (request.json or {}).get("file")
    url = (request.json or {}).get("url")

    def fn(d):
        d["references"] = [r for r in d["references"] if not (r.get("file") == file and r.get("url") == url)]
    core.mutate(pid, fn)
    return jsonify(ok=True)


@app.post("/api/projects/<pid>/reference_url")
def api_ref_url(pid):
    _exists(pid)
    body = request.json or {}
    url = (body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify(error="Paste a full link starting with http(s)://"), 400
    if body.get("download"):
        enqueue(pid, "ref_download", url=url)
    else:
        core.mutate(pid, lambda d: d["references"].append({"url": url, "kind": "link", "added": core.now_iso()}))
    return jsonify(ok=True)


@app.post("/api/projects/<pid>/brief")
def api_brief(pid):
    _exists(pid)
    body = request.json or {}

    def fn(d):
        if "brief" in body:
            d["brief"] = body["brief"]
        if "name" in body and body["name"].strip():
            d["name"] = body["name"].strip()
        for k, v in (body.get("settings") or {}).items():
            if k in core.DEFAULT_SETTINGS:
                d["settings"][k] = v
    core.mutate(pid, fn)
    return jsonify(ok=True)


@app.post("/api/projects/<pid>/analyze")
def api_analyze(pid):
    _exists(pid)
    data = core.load(pid)
    if not data["sources"]:
        return jsonify(error="Upload footage first."), 400
    enqueue(pid, "analyze", reanalyze=bool((request.json or {}).get("reanalyze")))
    return jsonify(ok=True)


@app.post("/api/projects/<pid>/render")
def api_render(pid):
    _exists(pid)
    kind = (request.json or {}).get("kind", "preview")
    if kind not in ("preview", "final"):
        return jsonify(error="bad kind"), 400
    enqueue(pid, kind)
    return jsonify(ok=True)


@app.post("/api/projects/<pid>/command")
def api_command(pid):
    _exists(pid)
    body = request.json or {}
    text = (body.get("text") or "").strip()
    sid = body.get("scene") or None
    if not text:
        return jsonify(error="Type a command."), 400

    def fn(d):
        sc = core.scene_by_id(d, sid) if sid else None
        if sid and sc is None:
            return {"error": "Scene no longer exists."}
        before_ids = {s["id"] for s in d["scenes"]}
        core.snapshot(pid, d)
        replies, unhandled, changed = commands.apply(d, sid, text)
        if not changed:
            core.drop_snapshot(pid)  # nothing changed – drop the snapshot
        new_ids = {s["id"] for s in d["scenes"]} - before_ids
        touched = set(new_ids) | ({sid} if sid else set())
        if changed and sid:
            _refresh_scene_meta(pid, d, touched, new_thumbs=new_ids | ({sid} if new_ids else set()))
        no = d["scenes"].index(sc) + 1 if sc in d["scenes"] else None
        if replies:
            err = all(r.startswith("!") for r in replies)
            parts = [p.strip() for p in re.split(r"[;\n]+", text) if p.strip()]
            handled = [p for p in parts if p not in unhandled]
            d["commands"].append({"id": core.new_id("cmd"), "scene": sid, "scene_no": no,
                                  "text": "; ".join(handled) or text,
                                  "status": "error" if err else "done",
                                  "reply": " ".join(r.lstrip("!") for r in replies), "by": "studio",
                                  "created": core.now_iso()})
        if unhandled:
            d["commands"].append({"id": core.new_id("cmd"), "scene": sid, "scene_no": no,
                                  "text": "; ".join(unhandled), "status": "claude", "reply": "",
                                  "by": None, "created": core.now_iso()})
        return {"replies": replies, "queued": unhandled}
    _, result = core.mutate(pid, fn)
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result)


@app.post("/api/projects/<pid>/first_cut")
def api_first_cut(pid):
    _exists(pid)

    def fn(d):
        d["commands"].append({"id": core.new_id("cmd"), "scene": None, "scene_no": None, "kind": "first_cut",
                              "text": "FIRST CUT – edit the whole video following the brief and references.",
                              "status": "claude", "reply": "", "by": None, "created": core.now_iso()})
    core.mutate(pid, fn)
    return jsonify(ok=True)


@app.post("/api/projects/<pid>/command/<cid>/cancel")
def api_cancel_command(pid, cid):
    _exists(pid)

    def fn(d):
        for c in d["commands"]:
            if c["id"] == cid and c["status"] == "claude":
                c["status"] = "cancelled"
    core.mutate(pid, fn)
    return jsonify(ok=True)


@app.post("/api/projects/<pid>/scene/<sid>/move")
def api_move(pid, sid):
    _exists(pid)
    direction = (request.json or {}).get("dir", "up")

    def fn(d):
        sc = core.scene_by_id(d, sid)
        if not sc:
            return
        i = d["scenes"].index(sc)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(d["scenes"]):
            core.snapshot(pid, d)
            d["scenes"][i], d["scenes"][j] = d["scenes"][j], d["scenes"][i]
    core.mutate(pid, fn)
    return jsonify(ok=True)


@app.post("/api/projects/<pid>/undo")
def api_undo(pid):
    _exists(pid)

    def fn(d):
        snap = core.pop_snapshot(pid)
        if not snap:
            return False
        d["scenes"], d["settings"] = snap["scenes"], snap["settings"]
        d["commands"].append({"id": core.new_id("cmd"), "scene": None, "scene_no": None, "text": "undo",
                              "status": "done", "reply": f"Restored state from {snap['at'][11:]}.", "by": "studio",
                              "created": core.now_iso()})
        return True
    _, ok = core.mutate(pid, fn)
    return jsonify(ok=ok)


@app.post("/api/projects/<pid>/open")
def api_open(pid):
    _exists(pid)
    which = (request.json or {}).get("which", "exports")
    target = {"exports": core.EXPORTS, "raw": core.raw_dir(pid), "refs": core.refs_dir(pid),
              "project": core.project_dir(pid), "brand": core.BRAND}.get(which, core.EXPORTS)
    target.mkdir(parents=True, exist_ok=True)
    os.startfile(target)  # local convenience – app is bound to 127.0.0.1 only
    return jsonify(ok=True)


if __name__ == "__main__":
    import socket
    for d in (core.RAW, core.REFS, core.STORY, core.EXPORTS, core.BRAND):
        d.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{PORT}"
    with socket.socket() as s:
        busy = s.connect_ex(("127.0.0.1", PORT)) == 0
    if busy:  # already running (double-clicked twice) – just show it
        print(f"CVE Studio is already running – opening {url}")
        webbrowser.open(url)
        raise SystemExit(0)
    if os.environ.get("CVE_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
