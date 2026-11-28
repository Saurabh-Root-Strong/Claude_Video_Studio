"""CVE Studio – local web UI for the Claude Video Editor.

Run:  .venv\\Scripts\\python.exe app.py      then open http://127.0.0.1:8070
"""
from __future__ import annotations

import copy
import json
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
import autoedit
import claudebot
import exportopts
import extract
import media as media_lib
import pictures
import refaudio
import typedcaps
import suggest
import library
import tracks
import pipeline
import storyboard

HERE = Path(__file__).parent
PORT = int(os.environ.get("CVE_PORT", "8070"))
app = Flask(__name__, static_folder=str(HERE / "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = None
_ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", "localhost"}  # bare 'localhost' = Flask test client


@app.after_request
def _no_stale_ui(resp):
    """The page, its script and stylesheet must never come from the browser cache: after an update the user would
    keep running old code (seen: new '?' help buttons missing until Ctrl+F5). 'no-cache' = always revalidate."""
    if request.path == "/" or request.path.startswith("/static/") or request.path.startswith("/storyboard/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


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
                if kind == "final":
                    with _pend_lock:
                        _pend_final.get(pid, set()).discard(args.get("_key"))
                out = pipeline.render_project(pid, preview=(kind == "preview"), opts=args.get("opts"))
                msg = f"Rendered {out.name}"
            elif kind == "ref_download":
                core.job_log(pid, f"Downloading {args['url']}")
                name = pipeline.download_reference(pid, args["url"])
                core.job_log(pid, f"Saved {name}; analysing")
                info = pipeline.analyze_reference(pid, name)
                core.mutate(pid, lambda d: d["references"].append({"id": core.new_id("r"), "file": name, "url": args["url"], **info}))
                msg = f"Reference downloaded: {name}"
            elif kind == "ref_audio":  # take the voice out of a reference once, so renders don't wait for it
                r_ = next((r for r in core.load(pid).get("references", []) if r.get("id") == args["ref"]), None)
                if not r_:
                    msg = "That reference was removed."
                else:
                    core.set_job(pid, progress=f"Taking the voice out of {r_['file']} (first time only)…")
                    refaudio.bg_audio(pid, r_, log=lambda m: core.job_log(pid, m))
                    msg = f"Background audio of {r_['file']} is ready."
                    auto_preview = bool(core.load(pid)["scenes"])
            elif kind == "caption_align":  # your pasted caption text -> the exact moment each word is said
                core.set_job(pid, progress="Lining up your captions with your voice… (first time: downloads the 1.2 GB voice model)")
                msg = typedcaps.align(pid, log=lambda m: core.job_log(pid, m))
                auto_preview = bool(core.load(pid)["scenes"])
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
                err = str(e)
                low = err.lower()
                if "no space left" in low or "not enough space" in low or "disk full" in low or "errno 28" in low:
                    err = ("The disk is full – free some space (empty the Recycle Bin, delete old exports or "
                           "Storyboards/<project>/work/segs) and try again.\n\n" + err)
                elif "permission denied" in low or "being used by another process" in low:
                    err = ("A file is open in another program (video player / antivirus scan?) – close it and try "
                           "again.\n\n" + err)
                core.job_log(pid, f"ERROR: {err}")
                core.set_job(pid, status="error", error=err, progress="Failed", finished=core.now_iso())
            except Exception:  # project deleted/unreadable – reporting must never kill the worker thread
                traceback.print_exc()
        finally:
            _jobs.task_done()


threading.Thread(target=_worker, daemon=True).start()


_pend_final: dict[str, set] = {}  # pid -> export choices waiting (one Reel + one YouTube export may both wait)
_pend_lock = threading.Lock()


def enqueue(pid: str, kind: str, **args):
    dup = {}
    if kind == "final":
        # the SAME export waiting twice (double click) is waste; DIFFERENT choices (Reel, then YouTube) both run
        args["_key"] = json.dumps(args.get("opts") or {}, sort_keys=True)
        with _pend_lock:
            if args["_key"] in _pend_final.setdefault(pid, set()):
                return
            _pend_final[pid].add(args["_key"])

    def fn(d):
        job = d.get("job") or {}
        if core.job_busy(d):          # don't clobber the running job's status – just note it's waiting
            # a preview already WAITING renders the newest edits when it starts – a second one is pure waste
            # (auto-preview from two open tabs, double clicks)
            if kind == "preview" and kind in (job.get("pending") or []):
                dup["skip"] = True
                return
            job.setdefault("pending", []).append(kind)
            d["job"] = job
        else:
            d["job"] = {"kind": kind, "status": "queued", "progress": "Queued", "error": None,
                        "log": job.get("log", []), "pending": []}
    core.mutate(pid, fn)
    if not dup:
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
    bk = core.brandkit_look()  # read live: the Brand Kit file is shared by every project
    if bk:
        view[core.BRANDKIT] = bk
    data["brandkit"] = {"available": bool(bk), "missing_but_used": active == core.BRANDKIT and not bk}
    data["looks_view"] = view
    sil_cache: dict[str, list] = {}
    typed_cache: dict[str, list] = {}
    pid = data.get("id") or ""
    total_orig = total_est = 0.0
    skipped = {s["file"] for s in data["sources"] if s.get("include") is False}
    per_src: dict[str, dict] = {}
    t_edit = 0.0
    split = data["settings"].get("layout") == "split"
    for i, sc in enumerate(data["scenes"], 1):
        if sc["src"] not in sil_cache:
            sil_cache[sc["src"]] = core.read_json(core.work_file(data["id"], sc["src"], "silences.json"), []) or []
        sc["no"] = i
        sc["dur"] = round(sc["out"] - sc["in"], 2)
        # B-roll scenes add no length: they only show as cutaways over speaker scenes
        sc["est"] = 0.0 if sc["src"] in skipped or sc.get("role") == "broll" else \
            pipeline.estimated_duration(sc, sil_cache[sc["src"]], data["settings"])
        sc["src_skipped"] = sc["src"] in skipped
        sc["start_at"] = round(t_edit, 3)  # where this scene starts in the edited video (timeline / preview seek)
        t_edit += sc["est"]
        b = sc.get("broll") if isinstance(sc.get("broll"), dict) else None
        if b:
            ok = core.broll_path(data["id"], data, b.get("src")) is not None and b.get("src") != sc["src"]
            src_b = core.source_by_file(data, b.get("src"))
            sc["broll_view"] = {"src": b.get("src"), "where": core.broll_where(b, split), "chosen": b.get("where") in core.BROLL_WHERE,
                                "fit": "fit" if b.get("fit") == "fit" else "fill",
                                "at": b.get("at"), "ok": ok, "still": core.is_still(b.get("src") or ""),
                                "why": "" if ok else ("this clip is set to Skip in Footage" if src_b and src_b.get("include") is False
                                                      else "the file is missing")}
        else:
            sc.pop("broll_view", None)
        def _bad(x):  # gone from the Library, or a file ffmpeg can't read (cached check)
            pth = library.path_of(x.get("file"))
            return not pth or not library.media_ok(pth)
        lib_missing = [x.get("name") or x.get("file") for x in (sc.get("stickers") or []) + (sc.get("sfx") or [])
                       if isinstance(x, dict) and _bad(x)]
        tr_s = (sc.get("transition") or {}).get("sound") if isinstance(sc.get("transition"), dict) else None
        if isinstance(tr_s, dict) and _bad(tr_s):
            lib_missing.append(tr_s.get("name"))
        if lib_missing:
            sc["lib_missing"] = lib_missing
        else:
            sc.pop("lib_missing", None)
        ps = per_src.setdefault(sc["src"], {"scenes": 0, "kept": 0, "est": 0.0, "thumb": sc.get("thumb")})
        ps["scenes"] += 1
        ps["kept"] += 1 if sc.get("keep", True) else 0
        ps["est"] += sc["est"]
        # what the captions will actually say (Hinglish and/or user spellings), so the page matches the video
        st = data["settings"]
        typed = typedcaps.view(pid, data, sc, typed_cache)
        sc["caption_typed"] = bool(typed is not None and typedcaps.scene_has_typed(data, sc))
        if typed is not None:
            sc["caption_view"] = typed
        elif st.get("caption_script") == "hinglish" or st.get("caption_fixes"):
            sc["caption_view"] = hinglish.caption_text((sc.get("transcript") or "").split(), st)
        else:
            sc.pop("caption_view", None)
        # no real speech (music / ambience): the "words" are whisper guessing – don't show them as if spoken.
        # Kept in `heard` (the Inspector can reveal them); shown normally only if captions are switched ON here
        if sc.get("no_speech") and sc.get("captions") is not True and (sc.get("transcript") or "").strip():
            sc["heard"] = sc.get("caption_view", sc["transcript"])
            sc["transcript"] = ""
            sc.pop("caption_view", None)
        total_orig += sc["dur"]
        total_est += sc["est"]
    data["totals"] = {"orig": round(total_orig, 1), "est": round(total_est, 1),
                      "kept": sum(1 for s in data["scenes"] if s.get("keep", True) and s["src"] not in skipped
                                  and s.get("role") != "broll"),
                      "count": len(data["scenes"]),
                      "broll": sum(1 for s in data["scenes"] if s.get("role") == "broll")}
    used: dict[str, list[int]] = {}
    for s in data["scenes"]:
        if isinstance(s.get("broll"), dict) and s.get("keep", True):
            used.setdefault(s["broll"].get("src"), []).append(s["no"])
    data["broll_clips"] = [{"file": f, "duration": (core.source_by_file(data, f) or {}).get("duration"),
                            "used_by": used.get(f, []),
                            "thumb": next((x.get("thumb") for x in data["scenes"] if x["src"] == f), None)}
                           for f in commands.broll_clips(data)]
    # uploaded B-roll (Raw Footage/<pid>/Assets): pictures and videos that only ever show OVER scenes
    adir, meta = core.assets_dir(data["id"]), data.get("assets") or {}
    thumbs = core.project_dir(data["id"]) / "work" / "thumbs"
    data["broll_assets"] = [{"file": p.name, "still": core.is_still(p.name), "duration": (meta.get(p.name) or {}).get("duration"),
                             "audio": p.suffix.lower() in core.AUDIO_EXT,
                             "used_by": used.get(p.name, []),
                             "thumb": _asset_thumb_name(p.name) if (thumbs / _asset_thumb_name(p.name)).is_file() else None}
                            for p in (sorted(adir.iterdir(), key=lambda p: p.name.lower()) if adir.is_dir() else [])
                            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in (BROLL_EXT | core.AUDIO_EXT)
                            and not core.source_by_file(data, p.name)]
    mdir = core.BRAND / "music"
    data.setdefault("ui_locks", {})
    data["ref_audio_tool"] = refaudio.available()
    data["claude_bot"] = claudebot.status(data.get("id") or "")
    data["pictures_roles"] = pictures.ROLE_NAMES
    in_vid = {s["id"] for s in data["scenes"] if s.get("keep", True) and s.get("role") != "broll" and s["src"] not in skipped}
    ptk = next((t for t in data.get("tracks") or [] if t.get("pictures")), None)
    tk_ids = {x.get("id") for x in (ptk or {}).get("items") or []}
    for e in ((data.get("pictures") or {}).get("items") or []) if isinstance(data.get("pictures"), dict) else []:
        m = e.get("media") if isinstance(e.get("media"), dict) else {  # never probe here (polled every 2 s)
            "kind": "still" if core.is_still(e.get("file", "")) else "video", "blend": "normal"}
        e["auto_role"] = pictures.auto_role(e.get("file", ""), m)[0]
        e["role"] = e["as"] if e.get("as") in pictures.ROLES else e["auto_role"]
        recs = pictures._records(e)
        # its scene was cut / re-analysed away, or its block deleted: it no longer shows – ▶ Place fixes it
        e["stale"] = bool(recs) and any(r.get("anchor") not in in_vid or r.get("item") not in tk_ids for r in recs)
    if ptk:
        data["pictures_track"] = {"hidden": bool(ptk.get("off")), "locked": bool(ptk.get("locked")),
                                  "muted": bool(ptk.get("mute")), "name": ptk.get("name")}
    bgm = {}  # pictures / videos from the Media tab, for "Green screen: picture / video"
    for k, sub in (("pictures", "Pictures"), ("videos", "Videos")):
        d_ = library.LIB / sub
        bgm[k] = sorted((f"{sub}/{p.name}" for p in d_.iterdir() if p.is_file() and not p.name.startswith(".")
                         and p.suffix.lower() in media_lib.ACCEPT), key=str.lower) if d_.is_dir() else []
    data["bg_media"] = bgm
    for r in data.get("references", []):
        r["bg_ready"] = bool(r.get("file")) and refaudio.ready(data["id"], r)
    data["music_files"] = sorted(p.name for p in mdir.iterdir() if p.is_file() and p.suffix.lower() in core.AUDIO_EXT) \
        if mdir.is_dir() else []
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
                       "new": sum(1 for s in data["sources"] if not s.get("analyzed") and not s.get("error") and not s.get("missing"))}
    # an export deleted / moved out of the Exports folder (or a cleaned-up preview): say so, don't show a dead player
    for r in data.get("renders", []):
        f = (core.EXPORTS if r.get("kind") == "final" else core.work_dir(data["id"])) / r.get("file", "")
        r["missing"] = not f.is_file()
    data["inbox"] = sum(1 for c in data.get("commands", []) if c["status"] == "claude")
    data["undo_depth"] = core.history_depth(data["id"])
    sb = core.read_json(storyboard.sb_path(data["id"]), None)
    data["storyboard"] = {"status": sb.get("status"), "applied_at": sb.get("applied_at"), "beats": len(sb.get("beats", [])),
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
        pipeline.update_speech_flag(sc, words_cache[sc["src"]], core.source_by_file(data, sc["src"]))
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
    if area not in ("library", "brand") and not core.valid_pid(pid):  # shared folders: no project needed ("_")
        abort(404)
    work = core.project_dir(pid) / "work"   # no mkdir here: a GET must never create folders
    base = {
        "raw": core.RAW / pid, "refs": core.REFS / pid, "work": work, "proxy": work / "proxy",
        "thumbs": work / "thumbs", "sheets": work / "refs", "exports": core.EXPORTS, "brand": core.BRAND,
        "library": library.LIB,
    }.get(area)
    if base is None:
        abort(404)
    p = (base / name).resolve()
    if not p.is_relative_to(base.resolve()) or not p.is_file():
        abort(404)
    if area == "exports" and request.args.get("dl"):  # "⬇ Download" on the Exports page: save, don't play
        return send_file(p, as_attachment=True, download_name=p.name, conditional=True)
    resp = send_file(p, conditional=True)
    # library files (1,600 sticker pictures, 64 preview clips) are re-drawn on every Inspector change: let the browser
    # keep them a day instead of re-asking for each one every time; everything else may change any moment
    resp.headers["Cache-Control"] = "max-age=86400" if area == "library" else "no-cache"
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
    try:
        return jsonify(_decorate(core.load(pid)))
    except core.ProjectDamaged as e:
        return jsonify(error=str(e), damaged=True), 500


@app.post("/api/projects/<pid>/autoedit")
def api_autoedit(pid):
    """✨ Auto-edit: read every scene, place transitions / effects / stickers / sounds / zoom moments (one undo step),
    then render the preview. {"level": subtle|balanced|energetic|null, "action": "apply"|"clear"}"""
    _exists(pid)
    body = request.json or {}
    res = {}

    def fn(d):
        if not any(s.get("keep", True) and s.get("role") != "broll" for s in d["scenes"]):
            res["error"] = "No scenes yet – add footage and click Analyze first."
            return
        if (d.get("job") or {}).get("kind") == "analyze" and (d.get("job") or {}).get("status") == "running":
            res["error"] = "Analysis is still running – wait for it to finish."
            return
        core.snapshot(pid, d)
        if body.get("action") == "clear":
            n = autoedit.clear_auto(d)
            d.pop("auto_edit", None)
            if not n:
                core.drop_snapshot(pid)
            res.update(reply=f"Removed {n} auto-edit change(s) – your own edits stay." if n else "Nothing from auto-edit to remove.")
            return
        autoedit.clear_auto(d)  # plan on a clean slate: its own earlier zooms / transitions must not block new ones
        pl = autoedit.plan(pid, d, body.get("level"))
        cnt = autoedit.apply(d, pl)
        res.update(level=pl["level"], counts=cnt, report=pl["report"],
                   reply=f"Auto-edit ({pl['level']}): {cnt['stickers']} sticker(s), {cnt['sfx']} sound(s), "
                         f"{cnt['punches']} zoom moment(s), {cnt['transitions']} transition(s), {cnt['effects']} effect(s). "
                         f"Rendering the edited view – review it, change any scene, Undo removes it all.")
        d["commands"].append({"id": core.new_id("cmd"), "scene": None, "scene_no": None, "text": f"auto-edit {pl['level']}",
                              "status": "done", "reply": res["reply"], "by": "studio", "created": core.now_iso()})
        _cap_commands(d)
    core.mutate(pid, fn)
    if res.get("error"):
        return jsonify(error=res["error"]), 400
    enqueue(pid, "preview")
    return jsonify(ok=True, **res)


@app.post("/api/projects/<pid>/pictures")
def api_pictures(pid):
    """🖼 Pictures for the story (pictures.py). {"op": add {files} | remove {file} | note {file, note} |
    pin {file, scene|null} | style {style} | run | clear}. run/remove/clear are one undo step and update the preview;
    run also asks 🤖 Claude to LOOK at the pictures and re-pin the ones their names couldn't place."""
    _exists(pid)
    body = request.json or {}
    op = body.get("op")
    res: dict = {}
    lib_meta: dict = {}
    if op == "add_lib":  # a file from the Media library (Transitions, Videos, Pictures, Stickers): copied into Assets
        try:  # (before the project lock: copying / probing a clip takes a moment)
            name = media_lib.add_to_project(pid, str(body.get("kind")), str(body.get("name")), want_name=True)
            dest = core.assets_dir(pid) / name
            m = pipeline.probe(dest)
            still = core.is_still(name)
            lib_meta.update(name=name, info={"duration": 0.0 if still else m.get("duration"), "width": m.get("width", 0),
                                             "height": m.get("height", 0), "still": still, "audio": False,
                                             "has_audio": bool(m.get("has_audio")), "hdr": False})
            (core.work_dir(pid) / "thumbs").mkdir(parents=True, exist_ok=True)
            pipeline.thumbnail(dest, 0.0 if still else min(1.0, (m.get("duration") or 0) * 0.3),
                               core.work_dir(pid) / "thumbs" / _asset_thumb_name(name))
        except ValueError as e:
            lib_meta["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            if not lib_meta.get("name"):
                lib_meta["error"] = f"Could not add that file ({str(e)[:120]})."

    voice_cache = None
    if op == "run":  # how loud your voice is (levels the whoosh under it) – measured once, outside the lock
        try:
            d0 = core.load(pid)
            pictures.voice_db(pid, d0)
            voice_cache = pictures.pool(d0).get("voice")
        except Exception:  # noqa: BLE001
            voice_cache = None
    pre_media: dict = {}  # what each file IS (several ffmpeg reads) – measured before taking the project lock
    if op in ("add", "add_lib"):
        want = [lib_meta["name"]] if lib_meta.get("name") else \
            [Path(str(f)).name for f in (body.get("files") or []) if isinstance(f, str)][:pictures.MAX_POOL]
        for f in want:
            fp_ = core.assets_dir(pid) / f
            if fp_.is_file() and fp_.suffix.lower() not in core.AUDIO_EXT:
                try:
                    pre_media[f] = pictures.inspect(fp_)
                except Exception:  # noqa: BLE001  measured again (inside) if this failed
                    pass

    def fn(d):
        p = pictures.pool(d)
        if op in ("add", "add_lib"):
            if op == "add_lib":
                if lib_meta.get("error"):
                    res["error"] = lib_meta["error"]
                    return
                d.setdefault("assets", {})[lib_meta["name"]] = lib_meta["info"]
                files = [lib_meta["name"]]
            else:
                files = [str(f) for f in (body.get("files") or []) if isinstance(f, str)][:pictures.MAX_POOL]
            added, skipped = pictures.add(d, files, pre_media)
            kinds = {}
            for f in added:
                kinds.setdefault(pictures.ROLE_NAMES[pictures.role_of(pid, pictures.entry(d, f))], []).append(f)
            res.update(added=added, skipped=skipped,
                       reply=(f"Added {len(added)}: " + "; ".join(f"{len(v)} as {k.lower()}" for k, v in kinds.items())
                              + " – change any in its “Use as” list, then ▶ Place." if added else "")
                       + (f" Skipped: {', '.join(skipped)}." if skipped else ""))
            return
        if op in ("role", "sound", "note", "style", "pin"):
            # once the pictures are ON the video, a change shows at once (re-placed, one Undo step) – before, it
            # was only stored and the video looked unchanged until "Place again"
            live = bool(p.get("ran")) and any(pictures._records(e) for e in p["items"]) and not pictures.locked(d)
            if live:
                core.snapshot(pid, d)
            f = str(body.get("file") or "")
            if op == "role":
                res["error"] = pictures.set_role(d, f, body.get("as"))
            elif op == "sound":
                res["error"] = pictures.set_sound(d, f, bool(body.get("on")))
            elif op == "note":
                res["error"] = "" if pictures.set_note(d, f, body.get("note") or "") else "That picture isn't in the list."
            elif op == "style":
                if body.get("style") not in pictures.STYLES:
                    res["error"] = "Unknown placement."
                else:
                    p["style"] = body["style"]
                    for e in p["items"]:  # your choice for ALL: per-block overrides read back from the timeline give way
                        e.pop("where", None)
                        e.pop("fit", None)
            else:
                sc = body.get("scene")
                if sc not in (None, "") and str(sc).isdigit():
                    n = int(sc)
                    sc = d["scenes"][n - 1]["id"] if 0 < n <= len(d["scenes"]) else "?"
                res["error"] = pictures.set_pin(d, f, sc or None)
            if res.get("error") or not live:
                if live:
                    core.drop_snapshot(pid)
                if not res.get("error"):
                    res.pop("error", None)
                    res["reply"] = "Saved – click ▶ Place to put it on the video."
                return
            res.pop("error", None)
            try:
                r = pictures.run(pid, d)
            except ValueError as e:
                core.drop_snapshot(pid)
                res["reply"] = f"Saved, but not re-placed: {e}"
                return
            mine = [ln for ln in r["report"] if f and ln.startswith(f)] if op != "style" else []
            res.update(r, render=True, applied=True,
                       reply="Updated on the video" + (f": {mine[0]}" if mine else "") + " – the edited view refreshes in a moment.")
            return
        if op in ("remove", "clear", "run"):
            j = d.get("job") or {}
            if op == "run" and j.get("kind") == "analyze" and j.get("status") in ("running", "queued"):
                res["error"] = "Analysis is still running – wait for it to finish."
                return
            if op in ("remove", "clear") and pictures.locked(d):
                res["error"] = pictures.locked(d)
                return
            core.snapshot(pid, d)
            if op == "remove":
                if not pictures.remove(d, str(body.get("file") or "")):
                    core.drop_snapshot(pid)
                    res["error"] = "That picture isn't in the list."
                    return
                res["reply"] = "Removed (Undo brings it back)."
                res["render"] = True
                return
            if op == "clear":
                n = pictures.clear(d)
                if not n:
                    core.drop_snapshot(pid)
                res.update(reply=f"Removed {n} picture(s) and their blocks (Undo brings them back).", render=bool(n))
                return
            if not [e for e in p["items"] if not e.get("off")]:
                core.drop_snapshot(pid)
                res["error"] = "Add pictures first (📎 or drop them on this box)."
                return
            if not pictures.scenes_info(pid, d):
                core.drop_snapshot(pid)
                res["error"] = "No scenes in the video yet – add footage and click Analyze first."
                return
            if voice_cache:
                p["voice"] = voice_cache
            try:
                r = pictures.run(pid, d)
            except ValueError as e:
                core.drop_snapshot(pid)
                res["error"] = str(e)
                return
            ask = (claudebot.enabled() and claudebot.exe() and body.get("claude", True) is not False
                   # only files NOBODY has pinned yet (Claude already looked at the ones it pinned – asking again
                   # on every "Place again" cost ~2 min each); transitions go on their spoken moments by themselves
                   and any(not e.get("off") and not e.get("pin") and not pictures.SCENE_RE.search(e.get("note") or "")
                           and pictures.role_of(pid, e) in pictures.NEEDS_EYES for e in p["items"])
                   and not any(c.get("status") == "claude" and c.get("kind") == "pictures" for c in d["commands"]))
            reply = (f"Placed {r['placed']} block(s) from {len([e for e in p['items'] if not e.get('off')])} file(s)"
                     + (f", {r['left']} not placed (see the list)" if r["left"] else "")
                     + " – updating your edited video."
                     + (" ⚠ The track is hidden – click its 👁 to see them." if r.get("hidden") else "")
                     + (" 🤖 Claude is now looking at the pictures to check each one is on the right words." if ask else ""))
            d["commands"].append({"id": core.new_id("cmd"), "scene": None, "scene_no": None, "kind": "pictures_auto",
                                  "text": f"place {len(p['items'])} picture(s)", "status": "done", "reply": reply,
                                  "by": "studio", "created": core.now_iso()})
            if ask:
                d["commands"].append({"id": core.new_id("cmd"), "scene": None, "scene_no": None, "kind": "pictures",
                                      "text": pictures.claude_text(d).replace("<pid>", pid), "status": "claude",
                                      "reply": "", "by": None, "created": core.now_iso()})
            _cap_commands(d)
            res.update(r, reply=reply, render=True, claude=bool(ask))
            return
        res["error"] = "Unknown picture action."
    core.mutate(pid, fn)
    if res.get("error"):
        return jsonify(error=res["error"]), 400
    if res.pop("render", False):
        enqueue(pid, "preview")
    return jsonify(ok=True, **res)


@app.get("/api/projects/<pid>/voice_check")
def api_voice_check(pid):
    """🎙 How your recording sounds: loudness, room noise, distortion – per clip, measured once (cached by file size +
    time in work/voice_check.json) – and what the studio does about it, in plain words."""
    _exists(pid)
    d = core.load(pid)
    cache_f = core.work_dir(pid) / "voice_check.json"
    cache = core.read_json(cache_f, {}) or {}
    out, changed = [], False
    for s in d["sources"]:
        if s.get("include") is False or s.get("has_audio") is False:
            continue
        p = core.raw_dir(pid) / s["file"]
        if not p.is_file():
            continue
        st_ = p.stat()
        k = f"{s['file']}|{st_.st_size}|{st_.st_mtime}"
        if k not in cache or (cache[k].get("v") != 2 and not cache[k].get("error")):  # v2: clipping = flat runs
            try:
                prof = pipeline.voice_profile(p, float(s.get("duration") or 60))
                lu = pipeline.measure_lufs(p)
                # real distortion = FLAT-TOPPED runs at full scale (astats flat factor / peak count) – not a few
                # peaks touching 0 dB, which every phone limiter produces (that was a false alarm)
                vd = pipeline.run([core.FFMPEG, "-hide_banner", "-i", p, "-vn", "-af",
                                   "astats=measure_perchannel=none:measure_overall=Peak_level+Flat_factor+Peak_count",
                                   "-f", "null", "-"], timeout=900)
                ff_ = re.findall(r"Flat factor:\s*(-?[\d.]+)", vd)
                pc_ = re.findall(r"Peak count:\s*(-?[\d.]+)", vd)
                pk_ = re.findall(r"Peak level dB:\s*(-?[\d.]+)", vd)
                cache[k] = {"lufs": lu, "floor": prof.get("floor"), "speech": prof.get("speech"),
                            "flat": float(ff_[-1]) if ff_ else 0.0, "peaks": float(pc_[-1]) if pc_ else 0.0,
                            "peak": float(pk_[-1]) if pk_ else None, "v": 2}
            except Exception as e:  # noqa: BLE001
                cache[k] = {"error": str(e)[:120]}
            changed = True
        out.append(dict(cache[k], file=s["file"]))
    if changed:
        cache_f.parent.mkdir(parents=True, exist_ok=True)
        core._atomic_write(cache_f, cache)
    st = d["settings"]
    mode, target = pipeline.voice_mode(st), float(st.get("loudness") or -16)
    hints = []
    for c in out:
        if c.get("error") or c.get("lufs") is None:
            continue
        name = c["file"] if len(out) > 1 else "Your recording"
        if c["lufs"] < target - 4:
            hints.append(f"{name} is quiet ({c['lufs']:.0f} LUFS)" + (
                f" – raised to {target:.0f} LUFS" if mode != "raw" else " – ⚠ “As recorded” leaves it quiet for viewers"))
        if c.get("floor") is not None and c.get("speech") is not None and c["floor"] > -58 and c["speech"] - c["floor"] < 20:
            hints.append(f"background noise is high (only {c['speech'] - c['floor']:.0f} dB under your voice)" + (
                " – Studio removes it" if mode == "studio" else " – Clear lowers it; Studio (AI) removes it best"
                if mode == "clear" else " – choose Studio (AI) to remove it, or Clear to lower it"))
        if c.get("flat", 0) >= 1.0 or (c.get("peak") is not None and c["peak"] >= -0.1 and c.get("peaks", 0) >= 100):
            hints.append("your voice distorts in loud parts (the mic was overloaded) – that can't be fully repaired; "
                         "next time record a little further from the mic")
    if out and not hints:
        hints.append("your recording sounds healthy")
    return jsonify(clips=out, hints=hints, mode=mode, target=target)


@app.post("/api/projects/<pid>/suggest")
def api_suggest(pid):
    """💬 Suggestions after auto-edit: a plain-words note for many scenes at once ("scene 6 calm music. scene 5 money
    sticker and cash sound"). One undo step; what the studio can't do goes to Claude's inbox with its scene."""
    _exists(pid)
    body = request.json or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify(error="Type your suggestions first."), 400
    if len(text) > 4000:
        return jsonify(error="That note is very long – send it in parts (under 4000 characters)."), 400
    res = {}

    def fn(d):
        j = d.get("job") or {}
        if j.get("kind") == "analyze" and j.get("status") in ("running", "queued"):
            res["error"] = "Analysis is still running – wait for it to finish."
            return
        if not any(s.get("keep", True) and s.get("role") != "broll" for s in d["scenes"]):
            res["error"] = "No scenes yet – add footage and click Analyze first."
            return
        sc0 = body.get("scene") or None
        if sc0 and not core.scene_by_id(d, sc0):  # a per-scene box for a scene that's gone must not edit the whole video
            res["error"] = "That scene is no longer in the project – open Scenes and type it in 💬 Tell the editor."
            return
        core.snapshot(pid, d)
        out = suggest.apply(pid, d, text, sc0)
        if not out["changed"]:
            core.drop_snapshot(pid)
        for sid, req in out["queued"]:
            sc = core.scene_by_id(d, sid) if sid else None
            d["commands"].append({"id": core.new_id("cmd"), "scene": sid,
                                  "scene_no": d["scenes"].index(sc) + 1 if sc else None,
                                  "text": f"suggestion: {req}", "status": "claude", "reply": "", "by": None,
                                  "created": core.now_iso()})
        done = [r for r in out["results"] if r["ok"]]
        d["commands"].append({"id": core.new_id("cmd"), "scene": None, "scene_no": None, "text": f"suggestions: {text[:300]}",
                              "status": "done" if done else "error", "by": "studio", "created": core.now_iso(),
                              "reply": f"{len(done)} of {len(out['results'])} suggestion(s) done"
                                       + (f", {len(out['queued'])} sent to Claude" if out["queued"] else "") + "."})
        _cap_commands(d)
        d["suggestions"] = ([{"text": text, "at": core.now_iso(), "results": out["results"]}]
                            + list(d.get("suggestions") or []))[:10]
        res.update(results=out["results"], changed=out["changed"], queued=len(out["queued"]))
    core.mutate(pid, fn)
    if res.get("error"):
        return jsonify(error=res["error"]), 400
    if res.get("changed"):
        d_ = core.load(pid)
        if typedcaps.needs_align(d_) and typedcaps.aligner_available():
            enqueue(pid, "caption_align")  # renders the preview itself when done (one render, with exact timing)
        else:
            enqueue(pid, "preview")
    return jsonify(ok=True, **res)


@app.post("/api/projects/<pid>/scene_caption")
def api_scene_caption(pid):
    """✏ Inspector → Words → Edit text: {"scene": id, "text": "..."} = this scene's captions say exactly that (timed
    to your voice, one undo step); {"scene": id, "auto": true} = back to the automatic captions for this scene."""
    _exists(pid)
    b = request.json or {}
    res = {}

    def fn(d):
        core.snapshot(pid, d)
        try:
            if b.get("auto"):
                res["reply"] = typedcaps.scene_auto(pid, d, str(b.get("scene") or ""))
            else:
                res["reply"] = typedcaps.set_scene_text(pid, d, str(b.get("scene") or ""), str(b.get("text") or ""))
        except ValueError as e:
            core.drop_snapshot(pid)
            res["error"] = str(e)
            return
        d["commands"].append({"id": core.new_id("cmd"), "scene": b.get("scene"), "scene_no": None,
                              "text": "caption text" + (" auto" if b.get("auto") else f": {str(b.get('text'))[:200]}"),
                              "status": "done", "reply": res["reply"], "by": "studio", "created": core.now_iso()})
        _cap_commands(d)
    core.mutate(pid, fn)
    if res.get("error"):
        return jsonify(error=res["error"]), 400
    d_ = core.load(pid)
    if typedcaps.needs_align(d_) and typedcaps.aligner_available():
        enqueue(pid, "caption_align")  # renders the edited view itself when done
    else:
        enqueue(pid, "preview")
    return jsonify(ok=True, reply=res["reply"])


@app.post("/api/projects/<pid>/media_assets")
def api_media_assets(pid):
    """Film strip + waveform of a timeline item's file (so a track block looks like the clip it is)."""
    _exists(pid)
    p = tracks.media_path(pid, core.load(pid), (request.json or {}).get("src"))
    if not p:
        return jsonify(error="file not available"), 404
    return jsonify(pipeline.media_assets(pid, p))


@app.post("/api/projects/<pid>/tracks")
def api_tracks(pid):
    """Timeline tracks + their items: {"op": add|remove|rename|toggle|move|item_add|item_set|item_remove, ...}.
    Each change is ONE undo step; a refused change leaves everything as it was."""
    _exists(pid)
    body = request.json or {}
    res = {}

    def fn(d):
        before = copy.deepcopy(d.get("tracks") or [])
        core.snapshot(pid, d)
        reply, err, extra = tracks.apply_op(pid, d, body)
        if err:
            d["tracks"] = before
            core.drop_snapshot(pid)
            res["error"] = err
            return
        res.update(extra, reply=reply)
        if extra.get("scene"):  # a block came back into Video 1: its thumbnail / words for the scene list + captions
            _refresh_scene_meta(pid, d, {extra["scene"]}, {extra["scene"]})
        if body.get("op") == "lock" or (body.get("op") == "lane" and body.get("what") == "lock"):
            core.drop_snapshot(pid)  # a lock is a page state, not an edit: not an undo step
            return
        if reply:
            d["commands"].append({"id": core.new_id("cmd"), "scene": None, "scene_no": None, "text": f"timeline: {body.get('op')}",
                                  "status": "done", "reply": reply, "by": "studio", "created": core.now_iso()})
            _cap_commands(d)
    core.mutate(pid, fn)
    if res.get("error"):
        return jsonify(error=res["error"]), 400
    return jsonify(ok=True, **res)


# ----------------------------------------------------------------- Library (transitions, effects, stickers, sounds)
_LIBSETUP = {"running": False, "log": []}


@app.get("/api/library")
def api_library():
    cat = library.catalog()
    return jsonify(dict(cat, setup=dict(_LIBSETUP, log=_LIBSETUP["log"][-6:])))


@app.post("/api/library/setup")
def api_library_setup():
    """Download the free packs (stickers + sounds) and make previews – in the background, once at a time."""
    if _LIBSETUP["running"]:
        return jsonify(ok=True, already=True)
    _LIBSETUP.update(running=True, log=["Starting…"])

    def work():
        try:
            library.setup(log=lambda m: _LIBSETUP["log"].append(str(m)))
        except Exception as e:  # noqa: BLE001
            _LIBSETUP["log"].append(f"Failed: {e}")
        finally:
            _LIBSETUP["running"] = False
    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/library/open")
def api_library_open():
    library.MY_STICKERS.mkdir(parents=True, exist_ok=True)
    library.MY_SOUNDS.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(library.LIB)  # local convenience – app is bound to 127.0.0.1 only
    return jsonify(ok=True)


# ----------------------------------------------------------------- 📁 Media: your own files, sorted automatically

@app.get("/api/media")
def api_media():
    return jsonify(kinds=media_lib.listing())


@app.post("/api/media/upload")
def api_media_upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="No files received."), 400
    want = request.args.get("kind") or None
    inc = library.LIB / ".incoming"
    inc.mkdir(parents=True, exist_ok=True)
    results = []
    for f in files:
        tmp = inc / f"{core.new_id('up')}{Path(core.safe_filename(f.filename or 'x')).suffix.lower()}"
        try:
            f.save(tmp)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            results.append({"ok": False, "name": f.filename, "why": f"could not save ({e.strerror or e}) – is the disk full?"})
            continue
        results.append(media_lib.add_file(tmp, f.filename or "upload", want if want in media_lib.KINDS else None))
    return jsonify(ok=True, results=results)


@app.post("/api/claude_auto")
def api_claude_auto():
    """🤖 {"on": true|false} switch Claude auto-reply; {"retry": pid} give a project's open requests one more run."""
    b = request.json or {}
    if "on" in b:
        claudebot.set_enabled(bool(b["on"]))
    if b.get("retry") and core.valid_pid(str(b["retry"])):
        claudebot.retry(str(b["retry"]))
    return jsonify(ok=True, enabled=claudebot.enabled())


@app.get("/api/extract")
def api_extract_list():
    return jsonify(jobs=extract.jobs(), files=extract.files(), modes={k: v[0] for k, v in extract.MODES.items()},
                   parts={k: v[0] for k, v in extract.PARTS.items()}, separator=refaudio.available())


@app.post("/api/extract")
def api_extract_start():
    """⬇ Extract: {mode: mp3|mp4|mute, from, to} + ONE of: url (link), media "kind/name" (a clip in Media),
    or a multipart file. Runs in the background; poll GET /api/extract."""
    form = request.form if request.files else (request.json or {})
    mode, frm, to = str(form.get("mode") or ""), form.get("from"), form.get("to")
    part = str(form.get("part") or "all")  # MP3: all | music (voice removed) | voice (music removed)
    try:
        f = request.files.get("file")
        if f:
            name = core.safe_filename(f.filename or "upload")
            ext = Path(name).suffix.lower()
            if ext not in media_lib.VIDEO_EXT | media_lib.AUDIO_EXT:
                return jsonify(error="Choose a video or sound file."), 400
            extract.TMP.mkdir(parents=True, exist_ok=True)
            tmp = extract.TMP / f"{core.new_id('up')}{ext}"
            f.save(tmp)
            job = extract.start(mode, frm, to, src=tmp, stem=Path(name).stem, tmp_src=True, part=part)
        elif form.get("media"):
            kind, _, nm = str(form.get("media")).partition("/")
            try:
                src = media_lib._path(kind, nm)
            except ValueError as e:
                return jsonify(error=str(e)), 400
            job = extract.start(mode, frm, to, src=src, stem=src.stem, part=part)
        else:
            job = extract.start(mode, frm, to, url=str(form.get("url") or ""), part=part)
    except extract.ExtractError as e:
        return jsonify(error=str(e)), 400
    except OSError as e:
        return jsonify(error=f"Could not save the file ({e.strerror or e}) – is the disk full?"), 400
    return jsonify(ok=True, job=job)


@app.post("/api/extract/<action>")
def api_extract_action(action):
    b = request.json or {}
    try:
        if action == "remove":
            return jsonify(ok=True, reply=extract.remove(str(b.get("name"))))
        if action == "to_media":
            r = extract.to_media(str(b.get("name")), b.get("kind") or None)
            if r.get("ok"):
                library._cache["sig"] = None
            return jsonify(ok=True, result=r)
        if action == "open":
            extract.OUT.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(extract.OUT)
            return jsonify(ok=True)
    except extract.ExtractError as e:
        return jsonify(error=str(e)), 400
    abort(404)


@app.get("/api/extract/download/<path:name>")
def api_extract_download(name):
    try:
        p = extract.path_of(name)
    except extract.ExtractError:
        abort(404)
    return send_file(p, as_attachment=True, download_name=p.name, conditional=True)


@app.post("/api/media/<action>")
def api_media_action(action):
    b = request.json or {}
    try:
        if action == "move":
            msg = media_lib.move(str(b.get("kind")), str(b.get("name")), str(b.get("to")))
        elif action == "remove":
            msg = media_lib.remove(str(b.get("kind")), str(b.get("name")))
        elif action == "open":
            k = b.get("kind") if b.get("kind") in media_lib.KINDS else None
            d = media_lib.folder(k) if k else library.LIB
            d.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(d)  # local convenience – app is bound to 127.0.0.1 only
            msg = ""
        else:
            abort(404)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, reply=msg)


@app.post("/api/projects/<pid>/media/use")
def api_media_use(pid):
    _exists(pid)
    b = request.json or {}
    try:
        msg = media_lib.add_to_project(pid, str(b.get("kind")), str(b.get("name")))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, reply=msg)


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
    if kind == "broll":
        return _upload_broll(pid, files[0], request.args.get("scene") or None)
    if kind == "asset":  # a file for a timeline track (video / picture / SOUND), not tied to a scene
        return _upload_broll(pid, files[0], None, allow_audio=True)
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


BROLL_EXT = core.VIDEO_EXT | core.IMAGE_EXT


def _asset_thumb_name(name: str) -> str:
    import hashlib
    return f"asset_{hashlib.sha1(name.encode()).hexdigest()[:12]}.jpg"


def _upload_broll(pid: str, f, sid: str | None, allow_audio: bool = False):
    """A video or picture uploaded for ONE scene's B-roll. Saved in Raw Footage/<pid>/Assets (not footage: it is
    never analysed, adds no scenes, no captions). The same file uploaded again is reused, not stored twice.
    With `sid`, that scene is covered with it right away (one undo step; its top/bottom/full choice is kept)."""
    name = core.safe_filename(f.filename or "broll")
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext not in BROLL_EXT and not (allow_audio and ext in core.AUDIO_EXT):
        return jsonify(error=f"“{name}” can't be B-roll – use a video ({', '.join(sorted(core.VIDEO_EXT))}) "
                             f"or a picture ({', '.join(sorted(core.IMAGE_EXT))})."), 400
    folder = core.assets_dir(pid)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".upload-{core.new_id('u')}{ext}"
    try:
        f.save(tmp)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return jsonify(error=f"Could not save {name}: {e.strerror or e}. Is the disk full?"), 507
    if tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return jsonify(error=f"“{name}” is empty (0 bytes)."), 400
    fp, reused, dest = _fingerprint(tmp), False, None
    for p in folder.iterdir():  # same file uploaded before (any name): reuse it
        if p.is_file() and p != tmp and not p.name.startswith(".") and p.stat().st_size == tmp.stat().st_size \
                and _fingerprint(p) == fp:
            tmp.unlink(missing_ok=True)
            dest, reused = p, True
            break
    if dest is None:
        d0 = core.load(pid)
        taken = {s["file"].lower() for s in d0["sources"]}  # a footage clip of the same name would win the lookup
        if name.lower() in taken:
            name = f"{stem} (B-roll){ext}"
        dest = core.unique_path(folder, name)
        tmp.replace(dest)
    try:
        meta = pipeline.probe(dest)
    except Exception:
        meta = {}
    is_audio = dest.suffix.lower() in core.AUDIO_EXT
    if is_audio and not meta.get("has_audio"):
        if not reused:
            dest.unlink(missing_ok=True)
        return jsonify(error=f"Can't read any sound from “{name}” – is it damaged?"), 400
    if not is_audio and (not meta.get("has_video") or not meta.get("width")):
        if not reused:
            dest.unlink(missing_ok=True)
        return jsonify(error=f"Can't read a picture from “{name}” – is it damaged, or a sound-only file?"), 400
    still = core.is_still(dest.name)
    info = {"duration": 0.0 if still else meta.get("duration"), "width": meta.get("width", 0), "height": meta.get("height", 0),
            "still": still, "audio": is_audio, "has_audio": bool(meta.get("has_audio")),
            "hdr": False if (still or is_audio) else pipeline.is_hdr(dest)}
    try:
        if is_audio:
            raise ValueError("no picture")
        (core.work_dir(pid) / "thumbs").mkdir(parents=True, exist_ok=True)
        pipeline.thumbnail(dest, 0.0 if still else min(1.0, (meta.get("duration") or 0) * 0.1),
                           core.work_dir(pid) / "thumbs" / _asset_thumb_name(dest.name))
    except Exception:
        pass
    res = {"file": dest.name, "reused": reused, **info}

    def fn(d):
        d.setdefault("assets", {})[dest.name] = info
        if not sid:
            return
        sc = core.scene_by_id(d, sid)
        if sc is None:
            res["note"] = "The scene no longer exists – the file is saved; pick it under B-roll on another scene."
            return
        if sc.get("role") == "broll":
            res["note"] = "That scene IS B-roll footage – pick a speaker scene to cover it."
            return
        core.snapshot(pid, d)
        old = sc.get("broll") if isinstance(sc.get("broll"), dict) else {}
        sc["broll"] = {"src": dest.name, **({"where": old["where"]} if old.get("where") else {}),
                       **({"fit": "fit"} if old.get("fit") == "fit" else {})}
        split = d["settings"].get("layout") == "split"
        no = d["scenes"].index(sc) + 1
        res["reply"] = (f'B-roll "{dest.name}" over scene {no} ({commands._where_txt(core.broll_where(sc["broll"], split), split)})'
                        f' – your voice keeps playing.' + (" (Same file as before – reused, not stored twice.)" if reused else ""))
        d["commands"].append({"id": core.new_id("cmd"), "scene": sid, "scene_no": no, "text": f"upload B-roll: {dest.name}",
                              "status": "done", "reply": res["reply"], "by": "studio", "created": core.now_iso()})
        _cap_commands(d)
    core.mutate(pid, fn)
    return jsonify(res)


@app.post("/api/projects/<pid>/broll_asset/remove")
def api_broll_asset_remove(pid):
    """Remove an uploaded B-roll file from the project. Scenes using it lose that B-roll (ONE undo step). The file goes
    to Assets/.trash, so Undo shows it again; trash older than 30 days is cleaned up here."""
    _exists(pid)
    name = str((request.json or {}).get("file") or "")
    folder = core.assets_dir(pid)
    p = folder / Path(name).name
    if not name or Path(name).name != name or name.startswith(".") or not p.is_file():
        return jsonify(error="That upload no longer exists."), 404
    d0 = core.load(pid)
    if core.source_by_file(d0, name):
        return jsonify(error="That is a footage clip – remove it in the Footage tab."), 400
    trash = folder / ".trash"
    trash.mkdir(exist_ok=True)
    dest = trash / name
    try:
        if dest.exists():
            dest.unlink()
        p.replace(dest)
    except OSError:
        return jsonify(error=f"“{name}” is in use (a render may be reading it) – try again in a moment."), 409
    import time as _t
    for old in trash.iterdir():  # purge long-gone uploads
        try:
            if old.is_file() and _t.time() - old.stat().st_mtime > 30 * 86400:
                old.unlink()
        except OSError:
            pass
    os.utime(dest)  # its 30 days start now
    res = {}

    def fn(d):
        users = [s for s in d["scenes"] if isinstance(s.get("broll"), dict) and s["broll"].get("src") == name]
        core.snapshot(pid, d, restore_assets=[name])  # undo moves the file back out of the trash
        for s in users:
            s.pop("broll", None)
        n_items = 0
        for tk in d.get("tracks") or []:  # timeline-track items using it go too (Undo brings them back)
            keep = [it for it in tk.get("items", []) if not ((it.get("src") or {}).get("t") == "asset" and it["src"].get("n") == name)]
            n_items += len(tk.get("items", [])) - len(keep)
            tk["items"] = keep
        nos = [d["scenes"].index(s) + 1 for s in users]
        res["reply"] = f"Removed “{name}” from the project." + (
            f" Scene(s) {', '.join(map(str, nos))} no longer show it." if nos else "") + (
            f" {n_items} timeline item(s) removed." if n_items else "") + " (Undo brings it back.)"
        d["commands"].append({"id": core.new_id("cmd"), "scene": None, "scene_no": None, "text": f"remove upload: {name}",
                              "status": "done", "reply": res["reply"], "by": "studio", "created": core.now_iso()})
        _cap_commands(d)
    core.mutate(pid, fn)
    return jsonify(ok=True, reply=res["reply"])


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
    notes: list[str] = []
    if cur == core.BRANDKIT:
        # the Brand Kit is read-only (shared by all projects): never store a project's copy of it.
        # Scene commands may have changed the video's settings while it was active – those don't travel back.
        bk = core.brandkit_look()
        if bk and core.style_of(d["settings"]) != bk:
            notes.append("changes made while ★ Brand Kit was active were not kept (it's shared and read-only)")
    else:
        looks[cur] = core.style_of(d["settings"])
    if target == core.BRANDKIT:
        bk = core.brandkit_look()
        if not bk:
            raise ValueError("There is no Brand Kit (Brand Kit/brand-kit.json is missing or invalid).")
        d["settings"].update(copy.deepcopy(bk))  # always the LATEST file
        d["style_source"] = target
        return notes
    if target not in looks:
        ref = next((r for r in d["references"] if r["id"] == target), None)
        look, dnotes = core.derive_look(looks.get("none") or core.style_of(d["settings"]), ref or {})
        notes += dnotes
        looks[target] = look
        if ref is not None:
            ref["look_notes"] = dnotes
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
        if target == core.BRANDKIT:
            if not core.brandkit_look():
                out["error"] = "There is no Brand Kit (Brand Kit/brand-kit.json is missing or invalid)."
                return
        elif target != "none" and not any(r["id"] == target for r in d["references"]):
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
            if look in ("none", core.BRANDKIT) or any(r["id"] == look for r in d["references"]):
                d.setdefault("look_briefs", {})[look] = str(body["look_brief"])[:20000]  # how this look should feel
        if "name" in body and str(body["name"]).strip():
            d["name"] = str(body["name"]).strip()[:120]
        active = d.get("style_source", "none")
        target = d["settings"]
        if look == core.BRANDKIT:
            target = None  # read-only: shared by every project – its style keys are never written from a project
        elif look and look != active:
            if look != "none" and not any(r["id"] == look for r in d["references"]):
                rejected.append("look (reference no longer exists)")
                return
            target = _ensure_look(d, look)  # editing a look the video doesn't use: the video stays as it is
        for k, v in (body.get("settings") or {}).items():
            ok, clean = core.clean_setting(k, v)
            if ok and k == "ref_audio" and clean != "none" and not refaudio.ref_of(d, clean):
                ok = False  # that reference was removed meanwhile
            if ok:
                if k not in core.STYLE_KEYS:
                    d["settings"][k] = clean  # footage keys stay project-wide
                elif target is not None:
                    target[k] = clean
            elif k in core.DEFAULT_SETTINGS and (target is not None or k not in core.STYLE_KEYS):
                rejected.append(k)
    core.mutate(pid, fn)
    if rejected:
        return jsonify(ok=False, error=f"Not saved (invalid value): {', '.join(rejected)}"), 400
    ra = (body.get("settings") or {}).get("ref_audio")
    d2 = core.load(pid)
    r_ = refaudio.ref_of(d2, ra)
    if r_ and not refaudio.ready(pid, r_) and refaudio.available():
        enqueue(pid, "ref_audio", ref=r_["id"])  # take the voice out now, so the next render doesn't wait for it
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
    body = request.json or {}
    kind = body.get("kind", "preview")
    if kind not in ("preview", "final"):
        return jsonify(error="bad kind"), 400
    if kind == "final" and isinstance(body.get("opts"), dict):
        data = core.load(pid)
        if not any(s.get("keep", True) for s in data["scenes"]):
            return jsonify(error="Nothing to export yet – no scenes are kept."), 400
        opts = exportopts.normalize(data, body["opts"])  # never trust the page's values as ffmpeg arguments
        enqueue(pid, kind, opts=opts)
        return jsonify(ok=True, opts=opts, summary=exportopts.summary(data, opts))
    enqueue(pid, kind)
    return jsonify(ok=True)


@app.get("/api/projects/<pid>/export_options")
def api_export_options(pid):
    _exists(pid)
    return jsonify(exportopts.catalog(core.load(pid)))


@app.post("/api/projects/<pid>/export_plan")
def api_export_plan(pid):
    """What the Export dialog shows for the current choices: size, file estimate, heads-ups. No side effects."""
    _exists(pid)
    body = request.json or {}
    data = core.load(pid)
    opts = exportopts.normalize(data, body.get("opts"))
    try:
        secs = min(max(float(body.get("seconds") or 0), 0.0), 6 * 3600)
    except (TypeError, ValueError):
        secs = 0.0
    w, h = exportopts.dims(data, opts)
    return jsonify(opts=opts, w=w, h=h, fps=exportopts.fps_of(data, opts), summary=exportopts.summary(data, opts),
                   mb=round(exportopts.est_mb(data, opts, secs), 1), minutes=round(exportopts.est_minutes(data, opts, secs), 1),
                   warnings=exportopts.warnings(data, opts, secs), reframes=exportopts.reframes(data, opts))


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
        return {"replies": replies, "queued": unhandled,
                "new": [s["id"] for s in d["scenes"] if s["id"] in new_ids]}  # in timeline order (paste/split select it)
    _, result = core.mutate(pid, fn)
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result)


@app.post("/api/projects/<pid>/first_cut")
def api_first_cut(pid):
    """'✨ Claude: plan the edit' – asks Claude for a STORYBOARD of the whole edit (beats, cuts, cards, punch-ins).
    Nothing is edited or rendered until the user approves beats on the storyboard page."""
    _exists(pid)
    res = {}

    def fn(d):
        j = d.get("job") or {}
        if j.get("kind") == "analyze" and j.get("status") in ("running", "queued"):
            res["error"], res["code"] = "Wait for the analysis to finish – the scenes are about to change.", 409
            return
        if not d["scenes"]:
            res["error"], res["code"] = "Analyze your footage first (Footage tab).", 400
            return
        if any(c.get("kind") in ("plan_edit", "first_cut") and c["status"] == "claude" for c in d["commands"]):
            res["already"] = True  # one request at a time
            return
        act = d.get("style_source", "none")
        refs = d.get("references", [])
        look = ("★ Brand Kit" if act == core.BRANDKIT else "None (simple)" if act == "none"
                else next((f"R{i + 1}" for i, r in enumerate(refs) if r["id"] == act), act))
        not_analyzed = sum(1 for s in d["sources"] if not s.get("analyzed"))
        d["commands"].append({
            "id": core.new_id("cmd"), "scene": None, "scene_no": None, "kind": "plan_edit",
            "text": (f"PLAN THE EDIT – storyboard for {len(d['scenes'])} scenes using look {look}: follow 'About this "
                     f"video'{'' if (d.get('brief') or '').strip() else ' (empty)'} and the {look} brief. User approves "
                     f"beats before anything renders." + (f" ({not_analyzed} clip(s) not analyzed – not in the plan.)"
                                                          if not_analyzed else "")),
            "status": "claude", "reply": "", "by": None, "created": core.now_iso()})
    core.mutate(pid, fn)
    if res.get("error"):
        return jsonify(error=res["error"]), res["code"]
    return jsonify(ok=True, already=res.get("already", False))


# ----------------------------------------------------------------- storyboard review (plan before render)

@app.get("/storyboard/<pid>")
def storyboard_page(pid):
    _exists(pid)
    sb = core.read_json(storyboard.sb_path(pid), None)
    if not sb:
        abort(404, "no storyboard yet – ask Claude to plan the edit")
    p = storyboard.write_html(pid, sb)  # regenerate from the current template
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


@app.post("/api/projects/<pid>/storyboard/apply")
def api_storyboard_apply(pid):
    """Apply the APPROVED beats to the edit right away (mechanical: trims, cuts, punch-ins, cards, B-roll) – no Claude
    needed, one undo step. Rejected beats with notes are queued for Claude to rework; pending beats stay as they are."""
    _exists(pid)
    sb0 = core.read_json(storyboard.sb_path(pid), None)
    if not sb0:
        return jsonify(error="no storyboard yet"), 404
    try:
        log = storyboard.apply(pid)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    sb = storyboard.with_stale(pid, core.read_json(storyboard.sb_path(pid), {}) or {})
    beats = sb.get("beats", [])
    applied = sum(1 for b in beats if b.get("applied"))
    rework = [b for b in beats if b["status"] == "rejected" and (b.get("notes") or "").strip()]
    stale = [b for b in beats if b["status"] == "approved" and b.get("stale") and not b.get("applied")]
    pending = sum(1 for b in beats if b["status"] == "pending")
    did = [x for x in log if "applied" in x and "not applied" not in x]
    reply = (f"Storyboard: {len(did)} beat(s) applied to the edit." if did else "Storyboard: nothing new to apply.") + \
        (f" {len(rework)} rejected beat(s) sent to Claude to rework." if rework else "") + \
        (f" {len(stale)} approved beat(s) skipped – their scene changed since planning." if stale else "") + \
        (f" {pending} beat(s) still pending (not applied)." if pending else "")

    def fn(d):
        d["commands"].append({"id": core.new_id("cmd"), "scene": None, "scene_no": None, "text": "storyboard: apply approved beats",
                              "status": "done", "reply": reply, "by": "studio", "created": core.now_iso()})
        if rework or stale:
            d["commands"].append({"id": core.new_id("cmd"), "scene": None, "scene_no": None, "kind": "storyboard",
                                  "text": "STORYBOARD REWORK – " +
                                          "; ".join([f"beat {b['no']} rejected: {b['notes'].strip()}" for b in rework] +
                                                    [f"beat {b['no']} approved but its scene changed – re-plan it" for b in stale]),
                                  "status": "claude", "reply": "", "by": None, "created": core.now_iso()})
        _cap_commands(d)
    core.mutate(pid, fn)
    return jsonify(ok=True, reply=reply, log=log, applied=applied, newly=len(did), rework=len(rework),
                   stale=len(stale), pending=pending)


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


@app.post("/api/projects/<pid>/broll")
def api_broll(pid):
    """Timeline B-roll drags in ONE undo step: {"set": {scene_id: {"src", "where"?, "at"?} | null}}.
    Refused (400, nothing changed) if any entry is invalid: unknown scene, a B-roll scene as target, the scene's own
    clip, an unknown clip, a bad where/at."""
    _exists(pid)
    body = request.json or {}
    changes = body.get("set")
    if not isinstance(changes, dict) or not changes or len(changes) > 500:
        return jsonify(error="nothing to change"), 400
    res = {}

    def fn(d):
        clean = {}
        for sid, b in changes.items():
            sc = core.scene_by_id(d, sid)
            if not sc:
                res["error"] = "A scene no longer exists – the page will refresh."
                return
            if b is None:
                clean[sid] = None
                continue
            if not isinstance(b, dict) or not isinstance(b.get("src"), str):
                res["error"] = "bad B-roll entry"
                return
            if sc.get("role") == "broll":
                res["error"] = f"Scene {d['scenes'].index(sc) + 1} is B-roll footage itself – it can't be covered."
                return
            src = b["src"]
            if src == sc["src"]:
                res["error"] = f"Scene {d['scenes'].index(sc) + 1} can't use its own clip as B-roll."
                return
            if not core.source_by_file(d, src) and not (core.raw_dir(pid) / "Assets" / Path(src).name).is_file():
                res["error"] = f'No clip called "{src}".'
                return
            e = {"src": src if core.source_by_file(d, src) else Path(src).name}
            if b.get("where") in core.BROLL_WHERE:
                e["where"] = b["where"]
            if b.get("fit") == "fit":
                e["fit"] = "fit"
            if b.get("at") is not None:
                try:
                    e["at"] = round(max(0.0, float(b["at"])), 2)
                except (TypeError, ValueError):
                    res["error"] = "bad B-roll start"
                    return
            clean[sid] = e
        if all((core.scene_by_id(d, sid).get("broll") or None) == v for sid, v in clean.items()):
            res["unchanged"] = True
            return
        core.snapshot(pid, d)
        for sid, v in clean.items():
            sc = core.scene_by_id(d, sid)
            if v is None:
                sc.pop("broll", None)
            else:
                sc["broll"] = v
        on = sorted(d["scenes"].index(core.scene_by_id(d, s)) + 1 for s, v in clean.items() if v)
        off = sorted(d["scenes"].index(core.scene_by_id(d, s)) + 1 for s, v in clean.items() if v is None)
        msg = (f"B-roll now on scene(s) {', '.join(map(str, on))}." if on else "") + \
              (f" Removed from scene(s) {', '.join(map(str, off))}." if off else "")
        d["commands"].append({"id": core.new_id("cmd"), "scene": None, "scene_no": None, "text": "timeline: B-roll",
                              "status": "done", "reply": msg.strip(), "by": "studio", "created": core.now_iso()})
        _cap_commands(d)
        res["reply"] = msg.strip()
    core.mutate(pid, fn)
    if res.get("error"):
        return jsonify(error=res["error"]), 400
    return jsonify(ok=True, reply=res.get("reply", ""), unchanged=res.get("unchanged", False))


@app.post("/api/projects/<pid>/broll/insert")
def api_broll_insert(pid):
    """Put a B-roll clip INTO the video as its own scene(s) (e.g. an intro), before scene `before` (or "end").
    Its B-roll scenes become normal scenes (own picture + sound) and its cutaways elsewhere are removed (the same
    footage would show twice). One undo step."""
    _exists(pid)
    body = request.json or {}
    f, before = body.get("file"), body.get("before")
    res = {}

    def fn(d):
        mine = [s for s in d["scenes"] if s["src"] == f and s.get("role") == "broll"]
        if not mine:
            res["error"] = ("That clip is already in the video – drag its scenes in the Video lane to move them."
                            if core.source_by_file(d, f) else f'No clip called "{f}".')
            return
        rest = [s for s in d["scenes"] if s not in mine]
        if before in (None, "end"):
            at = len(rest)
        else:
            at = next((i for i, s in enumerate(rest) if s["id"] == before), None)
            if at is None:
                res["error"] = "That scene no longer exists – the page will refresh."
                return
        core.snapshot(pid, d)
        for s in mine:
            s.pop("role", None)
            s.pop("role_auto", None)
        n = 0
        for s in rest:
            if isinstance(s.get("broll"), dict) and s["broll"].get("src") == f:
                s.pop("broll")
                n += 1
        d["scenes"] = rest[:at] + mine + rest[at:]
        no = d["scenes"].index(mine[0]) + 1
        res["reply"] = (f'"{f}" is now scene {no} – it plays on its own with its own sound.' +
                        (f" Its B-roll on {n} other scene(s) was removed (same footage twice)." if n else ""))
        d["commands"].append({"id": core.new_id("cmd"), "scene": None, "scene_no": None, "text": "timeline: insert clip",
                              "status": "done", "reply": res["reply"], "by": "studio", "created": core.now_iso()})
        _cap_commands(d)
    core.mutate(pid, fn)
    if res.get("error"):
        return jsonify(error=res["error"]), 400
    return jsonify(ok=True, reply=res["reply"])


@app.post("/api/projects/<pid>/scene/<sid>/move")
def api_move(pid, sid):
    _exists(pid)
    body = request.json or {}
    direction = body.get("dir", "up")
    to = body.get("to")  # timeline drag: the scene id it is dropped BEFORE (None/"end" = after the last scene)

    def fn(d):
        sc = core.scene_by_id(d, sid)
        if not sc:
            return
        i = d["scenes"].index(sc)
        if "to" in body:
            if to == sid:
                return
            rest = [x for x in d["scenes"] if x["id"] != sid]
            tgt = next((k for k, x in enumerate(rest) if x["id"] == to), None) if to not in (None, "end") else len(rest)
            if tgt is None or rest[:tgt] + [sc] + rest[tgt:] == d["scenes"]:
                return  # unknown target (scene vanished) or no change: no undo step
            core.snapshot(pid, d)
            d["scenes"] = rest[:tgt] + [sc] + rest[tgt:]
            return
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
        if "tracks" in snap:
            d["tracks"] = snap["tracks"]
        if "pictures" in snap:
            # files you ADDED to the list after that step (not yet placed) stay in the list – undoing a trim must not
            # make your uploads vanish; everything that touches the timeline goes back with the tracks
            later = [e for e in ((d.get("pictures") or {}).get("items") or []) if not pictures._records(e)]
            if snap.get("pictures") is not None:
                d["pictures"] = snap["pictures"]
            else:
                d.pop("pictures", None)
            have = {e.get("file") for e in ((d.get("pictures") or {}).get("items") or [])}
            keep = [e for e in later if e.get("file") not in have]
            if keep:
                pictures.pool(d)["items"].extend(keep)
        # a clip removed in Footage: its scenes come back above – its entry (audio, size, analysis) must come back too,
        # else those scenes render silent and the clip is missing from Footage. Clips added since are kept.
        have = {s["file"] for s in d["sources"]}
        used = {s["src"] for s in d["scenes"]}
        for s in snap.get("sources") or []:
            if s.get("file") not in have and s.get("file") in used:
                d["sources"].append(s)
        core.untrash_assets(pid, d, snap.get("restore_assets"))
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
    if "CVE_ROOT" not in os.environ:  # never in self-tests / sandboxes (temp roots): they'd start real Claude runs
        claudebot.start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
