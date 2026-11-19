"""CVE Studio – local web UI for the Claude Video Editor.

Run:  .venv\\Scripts\\python.exe app.py      then open http://127.0.0.1:8070
"""
from __future__ import annotations

import copy
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
import hinglish
import pipeline
import storyboard

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
            auto_preview = False
            if kind == "analyze":
                n_done = pipeline.analyze_project(pid, reanalyze=args.get("reanalyze", False))
                msg = "Analysis complete."
                # new scenes exist -> render the COMPLETE video (all clips, in clip order) so the Scenes page's
                # "Edited video" shows it, instead of an old render from before these clips were added
                auto_preview = bool(n_done) and args.get("auto_preview", True)
            elif kind in ("preview", "final"):
                out = pipeline.render_project(pid, preview=(kind == "preview"))
                msg = f"Rendered {out.name}"
            elif kind == "ref_download":
                core.job_log(pid, f"Downloading {args['url']}")
                name = pipeline.download_reference(pid, args["url"])
                core.job_log(pid, f"Saved {name}; analysing")
                info = pipeline.analyze_reference(pid, name)
                core.mutate(pid, lambda d: d["references"].append({"id": core.new_id("r"), "file": name, "url": args["url"], **info}))
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
            if auto_preview:
                d_ = core.load(pid)
                if any(s.get("keep", True) for s in d_["scenes"]) and "preview" not in ((d_.get("job") or {}).get("pending") or []):
                    core.job_log(pid, "Rendering a preview of the complete video for the Scenes page…")
                    enqueue(pid, "preview")
        except Exception as e:  # report every failure to the UI rather than dying silently
            traceback.print_exc()
            try:
                core.job_log(pid, f"ERROR: {e}")
                core.set_job(pid, status="error", error=str(e), progress="Failed", finished=core.now_iso())
            except Exception:  # project deleted/unreadable – reporting must never kill the worker thread
                traceback.print_exc()
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

def _cap_commands(d: dict, keep: int = 400) -> None:
    """History is polled every 2 s – keep it bounded. Never drop anything still waiting for Claude."""
    cmds = d.get("commands", [])
    if len(cmds) > keep:
        pending = [c for c in cmds if c["status"] == "claude"]
        rest = [c for c in cmds if c["status"] != "claude"][-(keep - len(pending)):]
        ids = {c["id"] for c in pending + rest}
        d["commands"] = [c for c in cmds if c["id"] in ids]


def _exists(pid: str):
    if not core.valid_pid(pid):
        abort(404, "project not found")


def _decorate(data: dict) -> dict:
    """Add derived display fields (estimated durations, numbering)."""
    data["edit_sig"] = core.edit_sig(data)  # before any derived fields are added
    # every look's values for the page (the active look IS the project settings; never-opened looks are previewed
    # from the simple settings + auto-detection, and only stored once edited or chosen)
    looks, active = data.get("looks", {}), data.get("style_source", "none")
    view = {}
    for lid in ["none"] + [r["id"] for r in data.get("references", [])]:
        if lid == active:
            view[lid] = core.style_of(data["settings"])
        elif lid in looks:
            view[lid] = looks[lid]
        else:
            base = looks.get("none") or core.style_of(data["settings"])
            ref = next((r for r in data["references"] if r["id"] == lid), {})
            view[lid], notes = core.derive_look(base, ref) if lid != "none" else (base, [])
            if ref and not ref.get("look_notes"):
                ref["look_notes"] = notes
    data["looks_view"] = view
    sil_cache: dict[str, list] = {}
    total_orig = total_est = 0.0
    skipped = {s["file"] for s in data["sources"] if s.get("include") is False}
    per_src: dict[str, dict] = {}
    for i, sc in enumerate(data["scenes"], 1):
        if sc["src"] not in sil_cache:
            sil_cache[sc["src"]] = core.read_json(core.work_file(data["id"], sc["src"], "silences.json"), []) or []
        sc["no"] = i
        sc["dur"] = round(sc["out"] - sc["in"], 2)
        sc["est"] = 0.0 if sc["src"] in skipped else \
            pipeline.estimated_duration(sc, sil_cache[sc["src"]], data["settings"])
        sc["src_skipped"] = sc["src"] in skipped
        ps = per_src.setdefault(sc["src"], {"scenes": 0, "kept": 0, "est": 0.0, "thumb": sc.get("thumb")})
        ps["scenes"] += 1
        ps["kept"] += 1 if sc.get("keep", True) else 0
        ps["est"] += sc["est"]
        # what the captions will actually say (Hinglish and/or user spellings), so the page matches the video
        st = data["settings"]
        if st.get("caption_script") == "hinglish" or st.get("caption_fixes"):
            sc["caption_view"] = hinglish.caption_text((sc.get("transcript") or "").split(), st)
        else:
            sc.pop("caption_view", None)
        total_orig += sc["dur"]
        total_est += sc["est"]
    data["totals"] = {"orig": round(total_orig, 1), "est": round(total_est, 1),
                      "kept": sum(1 for s in data["scenes"] if s.get("keep", True) and s["src"] not in skipped),
                      "count": len(data["scenes"])}
    raw = core.raw_dir(data["id"])
    for s in data["sources"]:
        ps = per_src.get(s["file"], {})
        s["missing"] = not (raw / s["file"]).is_file()
        if not s.get("bytes") and not s["missing"]:
            s["bytes"] = (raw / s["file"]).stat().st_size
        s["scenes"] = ps.get("scenes", 0)
        s["kept"] = ps.get("kept", 0)
        s["est"] = round(ps.get("est", 0.0), 1)
        s.setdefault("include", True)
        if not s.get("thumb") and ps.get("thumb"):
            s["thumb_scene"] = ps["thumb"]  # older projects: use the first scene's frame as the poster
    data["footage"] = {"clips": len(data["sources"]),
                       "included": sum(1 for s in data["sources"] if s.get("include", True)),
                       "duration": round(sum(s.get("duration") or 0 for s in data["sources"]), 1),
                       "bytes": sum(s.get("bytes") or 0 for s in data["sources"]),
                       "new": sum(1 for s in data["sources"] if not s.get("analyzed") and not s.get("missing"))}
    data["inbox"] = sum(1 for c in data.get("commands", []) if c["status"] == "claude")
    data["undo_depth"] = core.history_depth(data["id"])
    sb = core.read_json(storyboard.sb_path(data["id"]), None)
    data["storyboard"] = {"status": sb.get("status"), "beats": len(sb.get("beats", [])),
                          "reviewed": sum(1 for b in sb.get("beats", []) if b["status"] != "pending")} if sb else None
    return data


def _refresh_scene_meta(pid: str, data: dict, scene_ids: set[str], new_thumbs: set[str]):
    wd = core.work_dir(pid)
    words_cache: dict[str, list] = {}
    for sc in data["scenes"]:
        if sc["id"] not in scene_ids:
            continue
        if sc["src"] not in words_cache:
            words_cache[sc["src"]] = core.read_json(core.work_file(pid, sc["src"], "words.json"), []) or []
        sc["transcript"] = " ".join(w["w"] for w in pipeline.words_in(words_cache[sc["src"]], sc["in"], sc["out"]))
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
    saved, skipped, dupes = [], [], []
    # camera files arrive in any order from a multi-select: add them in natural name order (C0002 before C0010)
    files = sorted(files, key=lambda f: _natural_key(f.filename or ""))
    known = {}
    if kind == "source":
        for s in core.load(pid)["sources"]:
            fp = s.get("fp") or _fingerprint(folder / s["file"])
            if fp:
                known[fp] = s["file"]
    for f in files:
        name = core.safe_filename(f.filename or "upload")
        ext = Path(name).suffix.lower()
        if kind == "source" and ext not in core.VIDEO_EXT:
            skipped.append(name)
            continue
        dest = core.unique_path(folder, name)
        try:
            f.save(dest)
        except OSError as e:  # disk full / permission – don't leave a half-written file behind
            dest.unlink(missing_ok=True)
            return jsonify(error=f"Could not save {name}: {e.strerror or e}. Is the disk full?"), 507
        if kind == "source":
            fp = _fingerprint(dest)
            if fp and fp in known:  # same clip dropped twice (any name): keep one copy
                dest.unlink(missing_ok=True)
                dupes.append(f"{name} (= {known[fp]})")
                continue
            known[fp] = dest.name
        saved.append(dest.name)
    if not saved:
        if dupes:
            return jsonify(error=f"Already in this project: {', '.join(dupes)}"), 409
        return jsonify(error=f"No supported files – footage must be a video ({', '.join(sorted(core.VIDEO_EXT))})."), 400

    def fn(d):
        for n in saved:
            if kind == "source":
                d["sources"].append({"file": n, "analyzed": False, "added": core.now_iso(), "include": True,
                                     "fp": _fingerprint(folder / n)})
            else:
                d["references"].append({"id": core.new_id("r"), "file": n, "kind": "pending", "added": core.now_iso()})
    core.mutate(pid, fn)
    if kind == "source":
        for n in saved:  # quick probe + poster frame so the clip card is complete immediately
            meta = {}
            try:
                meta = pipeline.probe(folder / n)
            except Exception:
                meta = {"error": "could not read this file (damaged or unsupported?)"}
            thumb = _clip_thumb(pid, n, meta.get("duration") or 0)
            if thumb:
                meta["thumb"] = thumb
            meta.update(pipeline.clip_assets(pid, n, meta))  # timeline film strip + waveform
            core.mutate(pid, lambda d, n=n, meta=meta: (core.source_by_file(d, n) or {}).update(meta))
    else:
        enqueue(pid, "ref_analyze", files=saved)
    return jsonify(saved=saved, skipped=skipped, duplicates=dupes)


def _natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _fingerprint(path: Path) -> str | None:
    """size + hash of the first 2 MB: cheap and enough to recognise the same clip under another name."""
    import hashlib
    try:
        with open(path, "rb") as f:
            head = f.read(2 * 1024 * 1024)
        return f"{path.stat().st_size}-{hashlib.sha1(head).hexdigest()[:16]}"
    except OSError:
        return None


def _clip_thumb(pid: str, file: str, duration: float) -> str | None:
    name = f"src_{re.sub(r'[^A-Za-z0-9._-]', '_', file)}.jpg"
    (core.work_dir(pid) / "thumbs").mkdir(parents=True, exist_ok=True)
    try:
        pipeline.thumbnail(core.raw_dir(pid) / file, min(1.0, max(0.0, duration * 0.1)),
                           core.work_dir(pid) / "thumbs" / name)
        return name
    except Exception:
        return None


@app.post("/api/projects/<pid>/source/remove")
def api_remove_source(pid):
    _exists(pid)
    file = (request.json or {}).get("file")

    def fn(d):
        if not core.source_by_file(d, file):
            return
        core.snapshot(pid, d)  # removing a clip deletes its edited scenes – Undo must bring them back
        d["sources"] = [s for s in d["sources"] if s["file"] != file]
        d["scenes"] = [s for s in d["scenes"] if s["src"] != file]
    core.mutate(pid, fn)
    return jsonify(ok=True)


@app.post("/api/projects/<pid>/sources/order")
def api_sources_order(pid):
    """Set the clip order. body: {files:[...]} (custom) or {sort:"name"|"recorded"|"duration"}, force:bool.
    Scenes follow: each clip's scenes stay together in clip order. If the user had arranged scenes ACROSS clips,
    answer 409 first (regrouping would undo that arrangement) unless force=true."""
    _exists(pid)
    body = request.json or {}
    result = {}

    def fn(d):
        cur = [s["file"] for s in d["sources"]]
        if body.get("sort"):
            key = {"name": lambda s: _natural_key(s["file"]),
                   "recorded": lambda s: (s.get("recorded") or "9999", _natural_key(s["file"])),
                   "duration": lambda s: (s.get("duration") or 0, _natural_key(s["file"]))}.get(body["sort"])
            if not key:
                result["error"] = "unknown sort"
                return
            new = [s["file"] for s in sorted(d["sources"], key=key)]
        else:
            new = list(body.get("files") or [])
        if sorted(new) != sorted(cur):  # the page was out of date (a clip was added/removed meanwhile)
            result["error"] = "The clip list changed – refreshing."
            result["code"] = 409
            return
        if new == cur:
            result["unchanged"] = True
            return
        if pipeline.scenes_interleaved(d["scenes"]) and not body.get("force"):
            result["error"] = ("You arranged scenes across clips in the Scenes tab. Reordering clips will regroup "
                               "every clip's scenes together (Undo can bring your arrangement back).")
            result["code"] = 409
            result["confirm"] = True
            return
        core.snapshot(pid, d)
        by = {s["file"]: s for s in d["sources"]}
        d["sources"] = [by[f] for f in new]
        d["scenes"] = pipeline.regroup_scenes(d["scenes"], new)
    core.mutate(pid, fn)
    if result.get("error"):
        return jsonify(error=result["error"], confirm=result.get("confirm", False)), result.get("code", 400)
    return jsonify(ok=True, unchanged=result.get("unchanged", False))


_assets_lock = threading.Lock()


@app.post("/api/projects/<pid>/footage/assets")
def api_footage_assets(pid):
    """Make missing timeline graphics (film strip, waveform, poster) – for clips added before the timeline existed.
    One request at a time; the page asks once per project."""
    _exists(pid)
    if not _assets_lock.acquire(blocking=False):
        return jsonify(ok=True, busy=True)
    made = 0
    try:
        for s in core.load(pid)["sources"]:
            if s.get("strip") or not (core.raw_dir(pid) / s["file"]).is_file():
                continue
            meta = s if s.get("duration") else {**s, **pipeline.probe(core.raw_dir(pid) / s["file"])}
            extra = pipeline.clip_assets(pid, s["file"], meta)
            if not s.get("thumb"):
                t = _clip_thumb(pid, s["file"], meta.get("duration") or 0)
                if t:
                    extra["thumb"] = t
            if extra:
                made += 1
                core.mutate(pid, lambda d, f=s["file"], x=extra: (core.source_by_file(d, f) or {}).update(x))
    finally:
        _assets_lock.release()
    return jsonify(ok=True, made=made)


@app.post("/api/projects/<pid>/source/include")
def api_source_include(pid):
    """Include / Skip a clip in the video without deleting it or its edits."""
    _exists(pid)
    body = request.json or {}

    def fn(d):
        s = core.source_by_file(d, body.get("file"))
        if s:
            core.snapshot(pid, d)
            s["include"] = bool(body.get("include", True))
        return bool(s)
    _, ok = core.mutate(pid, fn)
    return (jsonify(ok=True), 200) if ok else (jsonify(error="no such clip"), 404)


@app.post("/api/projects/<pid>/reference/remove")
def api_remove_ref(pid):
    _exists(pid)
    body = request.json or {}
    rid, file, url = body.get("id"), body.get("file"), body.get("url")

    def fn(d):
        gone = [r for r in d["references"] if (rid and r["id"] == rid) or
                (not rid and r.get("file") == file and r.get("url") == url)]
        if not gone:
            return False
        core.snapshot(pid, d)
        for r in gone:
            if d.get("style_source") == r["id"]:  # removing the active look: fall back to the simple settings
                _switch_style(d, "none")
            d.get("looks", {}).pop(r["id"], None)
            d.get("look_briefs", {}).pop(r["id"], None)
        d["references"] = [r for r in d["references"] if r not in gone]
        return True
    core.mutate(pid, fn)
    return jsonify(ok=True)


def _ensure_look(d: dict, lid: str) -> dict:
    """The stored look `lid` (created from the simple settings + auto-detection the first time it's needed)."""
    looks = d.setdefault("looks", {})
    if lid == d.get("style_source", "none"):
        return d["settings"]
    if lid not in looks:
        base = looks.get("none") or core.style_of(d["settings"])
        ref = next((r for r in d["references"] if r["id"] == lid), None)
        looks[lid], notes = core.derive_look(base, ref or {}) if lid != "none" else (core.style_of(base), [])
        if ref is not None:
            ref["look_notes"] = notes
    return looks[lid]


def _switch_style(d: dict, target: str) -> list[str]:
    """Make `target` ('none' or a reference id) the active look. The outgoing look's style is stored first, so
    switching back and forth never loses anything. Returns notes about an auto-derived first look."""
    looks = d.setdefault("looks", {})
    cur = d.get("style_source", "none")
    looks[cur] = core.style_of(d["settings"])
    notes: list[str] = []
    if target not in looks:
        ref = next((r for r in d["references"] if r["id"] == target), None)
        look, notes = core.derive_look(looks.get("none") or core.style_of(d["settings"]), ref or {})
        looks[target] = look
        if ref is not None:
            ref["look_notes"] = notes
    d["settings"].update(copy.deepcopy(looks[target]))
    d["style_source"] = target
    return notes


@app.post("/api/projects/<pid>/style_source")
def api_style_source(pid):
    """Choose which look the project uses: 'none' (simple settings) or a reference id. Undoable."""
    _exists(pid)
    target = str((request.json or {}).get("source") or "none")
    out = {}

    def fn(d):
        if target != "none" and not any(r["id"] == target for r in d["references"]):
            out["error"] = "That reference no longer exists."
            return
        if d.get("style_source", "none") == target:
            out["unchanged"] = True
            return
        core.snapshot(pid, d)
        out["notes"] = _switch_style(d, target)
    core.mutate(pid, fn)
    if out.get("error"):
        return jsonify(error=out["error"]), 404
    return jsonify(ok=True, **out)


@app.post("/api/projects/<pid>/reference/<rid>/study")
def api_ref_study(pid, rid):
    """Ask Claude for a deep style analysis of one reference (fills in that reference's look)."""
    _exists(pid)
    res = {}

    def fn(d):
        refs = d["references"]
        r = next((x for x in refs if x["id"] == rid), None)
        if not r:
            res["error"] = "no such reference"
            return
        no = refs.index(r) + 1
        if any(c.get("kind") == "ref_style" and c.get("ref") == rid and c["status"] == "claude" for c in d["commands"]):
            res["already"] = True
            return
        d["commands"].append({"id": core.new_id("cmd"), "scene": None, "scene_no": None, "kind": "ref_style",
                              "ref": rid, "text": f"STUDY REFERENCE R{no} ({r.get('file') or r.get('url')}) – "
                                                 f"analyse its editing style and fill in its look.",
                              "status": "claude", "reply": "", "by": None, "created": core.now_iso()})
    core.mutate(pid, fn)
    if res.get("error"):
        return jsonify(error=res["error"]), 404
    return jsonify(ok=True, already=res.get("already", False))


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
        core.mutate(pid, lambda d: d["references"].append({"id": core.new_id("r"), "url": url, "kind": "link", "added": core.now_iso()}))
    return jsonify(ok=True)


@app.post("/api/projects/<pid>/brief")
def api_brief(pid):
    _exists(pid)
    body = request.json or {}
    rejected = []
    look = body.get("look")  # which look the page is EDITING ('none' / ref id); style keys go there

    def fn(d):
        if "brief" in body:
            d["brief"] = str(body["brief"])[:20000]  # about this video – shared by every look
        if "look_brief" in body and look:
            if look == "none" or any(r["id"] == look for r in d["references"]):
                d.setdefault("look_briefs", {})[look] = str(body["look_brief"])[:20000]  # how this look should feel
        if "name" in body and str(body["name"]).strip():
            d["name"] = str(body["name"]).strip()[:120]
        active = d.get("style_source", "none")
        target = d["settings"]
        if look and look != active:
            if look != "none" and not any(r["id"] == look for r in d["references"]):
                rejected.append("look (reference no longer exists)")
                return
            target = _ensure_look(d, look)  # editing a look the video doesn't use: the video stays as it is
        for k, v in (body.get("settings") or {}).items():
            ok, clean = core.clean_setting(k, v)
            if ok:
                (target if k in core.STYLE_KEYS else d["settings"])[k] = clean  # footage keys stay project-wide
            elif k in core.DEFAULT_SETTINGS:
                rejected.append(k)
    core.mutate(pid, fn)
    if rejected:
        return jsonify(ok=False, error=f"Not saved (invalid value): {', '.join(rejected)}"), 400
    return jsonify(ok=True)


@app.post("/api/projects/<pid>/apply_brand")
def api_apply_brand(pid):
    """Overlay the approved Brand Kit (Brand Kit/brand-kit.json "settings") on this project. Undoable."""
    _exists(pid)
    kit = core.read_json(core.BRAND / "brand-kit.json", None)
    if not isinstance(kit, dict) or not isinstance(kit.get("settings"), dict):
        return jsonify(error="No Brand Kit yet (Brand Kit/brand-kit.json missing or invalid)."), 400
    applied = []

    look = (request.json or {}).get("look")

    def fn(d):
        core.snapshot(pid, d)
        target = _ensure_look(d, look) if look and look != d.get("style_source", "none") else d["settings"]
        for k, v in kit["settings"].items():
            if k in core.DEFAULT_SETTINGS:
                ok, clean = core.clean_setting(k, v)
                if ok:
                    (target if k in core.STYLE_KEYS else d["settings"])[k] = clean
                    applied.append(k)
    core.mutate(pid, fn)
    return jsonify(ok=True, applied=applied)


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
        _cap_commands(d)
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


# ----------------------------------------------------------------- storyboard review (plan before render)

@app.get("/storyboard/<pid>")
def storyboard_page(pid):
    _exists(pid)
    p = storyboard.html_path(pid)
    if not p.exists():
        abort(404, "no storyboard yet – ask Claude to plan the edit")
    return send_file(p, mimetype="text/html", max_age=0)


@app.get("/api/projects/<pid>/storyboard")
def api_storyboard(pid):
    _exists(pid)
    sb = core.read_json(storyboard.sb_path(pid), None)
    return (jsonify(storyboard.with_stale(pid, sb)), 200) if sb else (jsonify(error="no storyboard yet"), 404)


@app.post("/api/projects/<pid>/storyboard/submit")
def api_storyboard_submit(pid):
    _exists(pid)
    sb = core.read_json(storyboard.sb_path(pid), None)
    if not sb:
        return jsonify(error="no storyboard yet"), 404
    ok = sum(1 for b in sb["beats"] if b["status"] == "approved")
    bad = sum(1 for b in sb["beats"] if b["status"] == "rejected")

    def fn(d):
        d["commands"].append({"id": core.new_id("cmd"), "scene": None, "scene_no": None, "kind": "storyboard",
                              "text": f"STORYBOARD FEEDBACK – {ok} approved, {bad} rejected, "
                                      f"{len(sb['beats']) - ok - bad} pending. Apply approved beats, rework rejected "
                                      f"ones from the notes, then render.",
                              "status": "claude", "reply": "", "by": None, "created": core.now_iso()})
    core.mutate(pid, fn)
    return jsonify(ok=True)


@app.post("/api/projects/<pid>/storyboard/<sid>")
def api_storyboard_review(pid, sid):
    _exists(pid)
    body = request.json or {}
    try:
        b = storyboard.set_review(pid, sid, body.get("status"), body.get("notes"), body.get("items_on"))
    except ValueError as e:
        return jsonify(error=str(e)), 409 if "applied" in str(e) else 404
    return jsonify(b)


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
    if os.name == "nt":
        os.startfile(target)  # local convenience – app is bound to 127.0.0.1 only
    else:
        import subprocess
        import sys
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(target)])
    return jsonify(ok=True)


if __name__ == "__main__":
    for d in (core.RAW, core.REFS, core.STORY, core.EXPORTS, core.BRAND):
        d.mkdir(parents=True, exist_ok=True)
    import urllib.request
    url = f"http://127.0.0.1:{PORT}"
    if core.server_alive(PORT):
        try:  # is it US (double-clicked twice) or some other program on the port?
            with urllib.request.urlopen(f"{url}/api/projects", timeout=3) as r:
                ours = r.headers.get_content_type() == "application/json"
        except Exception:
            ours = False
        if ours:
            print(f"CVE Studio is already running – opening {url}")
            webbrowser.open(url)
            raise SystemExit(0)
        print(f"ERROR: another program is using port {PORT}. Close it, or start the studio on another port:\n"
              f"  set CVE_PORT=8071  then run Start Studio.bat again.")
        raise SystemExit(1)
    n = core.reset_stale_jobs()
    if n:
        print(f"Reset {n} job(s) interrupted when the studio was last closed.")
    if os.environ.get("CVE_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
