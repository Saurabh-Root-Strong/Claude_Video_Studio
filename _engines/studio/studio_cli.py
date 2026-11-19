"""CLI for Claude (in VS Code) to work the CVE Studio inbox.

  python studio_cli.py inbox                      pending Claude commands, all projects
  python studio_cli.py show <pid>                 brief, settings, refs, scene table
  python studio_cli.py cmd <pid> <scene|-> "text" apply an instant command (scene no. or id, '-' = whole video)
  python studio_cli.py set <pid> <scene> key=value ...   set raw scene fields (in, out, keep, speed, text, ...)
  python studio_cli.py done <pid> <cmd_id> "reply"       mark a Claude command done with a reply
  python studio_cli.py analyze <pid> | render <pid> preview|final
"""
from __future__ import annotations

import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

import commands  # noqa: E402
import core  # noqa: E402
import pipeline  # noqa: E402


def _scene(d, ref):
    if ref in ("-", "", None):
        return None
    if ref.isdigit():
        i = int(ref) - 1
        if 0 <= i < len(d["scenes"]):
            return d["scenes"][i]["id"]
        raise SystemExit(f"no scene {ref}")
    if core.scene_by_id(d, ref):
        return ref
    raise SystemExit(f"no scene {ref}")


def inbox():
    n = 0
    for p in core.list_projects():
        d = core.load(p["id"])
        pend = [c for c in d["commands"] if c["status"] == "claude"]
        if not pend:
            continue
        print(f"\n=== {d['id']}  ({d['name']})  brief: {d['brief'][:200]!r}")
        for c in pend:
            n += 1
            where = f"scene {c['scene_no']} ({c['scene']})" if c.get("scene") else "WHOLE VIDEO"
            print(f"  [{c['id']}] {where}: {c['text']}")
            if c.get("scene"):
                sc = core.scene_by_id(d, c["scene"])
                if sc:
                    print(f"      {sc['src']} {sc['in']:.2f}-{sc['out']:.2f}s keep={sc['keep']} | {sc['transcript'][:160]}")
                    print(f"      thumb: {core.work_dir(d['id']) / 'thumbs' / sc['thumb']}")
    print(f"\n{n} pending command(s)." if n else "Inbox empty.")


def show(pid):
    d = core.load(pid)
    print(f"{d['name']}  [{pid}]\nBrief: {d['brief']}\nSettings: {json.dumps(d['settings'])}")
    print("Sources:", [(s['file'], s.get('duration'), f"{s.get('width')}x{s.get('height')}") for s in d["sources"]])
    for i, r in enumerate(d["references"], 1):
        sheet = core.work_dir(pid) / "refs" / r["sheet"] if r.get("sheet") else ""
        print(f"R{i}: {r.get('file') or r.get('url')} kind={r.get('kind')} avg_shot={r.get('avg_shot')} sheet={sheet}")
    wd = core.work_dir(pid)
    for i, s in enumerate(d["scenes"], 1):
        sil = core.read_json(wd / f"{s['src'].rsplit('.', 1)[0]}.silences.json", []) or []
        est = pipeline.estimated_duration(s, sil, d["settings"])
        flags = [] if s["keep"] else ["CUT"]
        for k, dv in (("speed", 1.0), ("zoom", 1.0), ("volume", 1.0)):
            if s.get(k, dv) != dv:
                flags.append(f"{k}={s[k]}")
        if s.get("mute"):
            flags.append("mute")
        if s.get("text"):
            flags.append(f"text={s['text']!r}")
        print(f"{i:>3} {s['id']} {s['in']:7.2f}-{s['out']:7.2f} ({s['out'] - s['in']:.1f}s→{est}s) {' '.join(flags)} | {s['transcript'][:110]}")


def cmd(pid, ref, text):
    def fn(d):
        sid = _scene(d, ref)
        core.snapshot(pid, d)
        replies, unhandled, changed = commands.apply(d, sid, text)
        if not changed:
            core.drop_snapshot(pid)
        sc = core.scene_by_id(d, sid) if sid else None
        d["commands"].append({"id": core.new_id("cmd"), "scene": sid,
                              "scene_no": d["scenes"].index(sc) + 1 if sc else None, "text": text,
                              "status": "done" if changed else "error", "reply": " ".join(replies) or "not understood",
                              "by": "claude", "created": core.now_iso()})
        return replies, unhandled
    _, (replies, unhandled) = core.mutate(pid, fn)
    print("replies:", replies, "| not understood:", unhandled)


def set_fields(pid, ref, pairs):
    def fn(d):
        sid = _scene(d, ref)
        sc = core.scene_by_id(d, sid)
        core.snapshot(pid, d)
        for p in pairs:
            k, _, v = p.partition("=")
            try:
                sc[k] = json.loads(v)
            except json.JSONDecodeError:
                sc[k] = v
        words = core.read_json(core.work_dir(pid) / f"{sc['src'].rsplit('.', 1)[0]}.words.json", []) or []
        sc["transcript"] = " ".join(w["w"] for w in pipeline.words_in(words, sc["in"], sc["out"]))
        return sc
    _, sc = core.mutate(pid, fn)
    print(json.dumps(sc, ensure_ascii=False))


def done(pid, cid, reply):
    def fn(d):
        for c in d["commands"]:
            if c["id"] == cid:
                c.update(status="done", reply=reply, by="claude", updated=core.now_iso())
                return True
        return False
    _, ok = core.mutate(pid, fn)
    print("marked done" if ok else "command not found")


def main(argv):
    if not argv:
        print(__doc__)
        return
    op, *rest = argv
    if op == "inbox":
        inbox()
    elif op == "show":
        show(rest[0])
    elif op == "cmd":
        cmd(rest[0], rest[1], rest[2])
    elif op == "set":
        set_fields(rest[0], rest[1], rest[2:])
    elif op == "done":
        done(rest[0], rest[1], rest[2])
    elif op in ("analyze", "render"):
        pid = rest[0]
        if core.job_busy(core.load(pid)):
            raise SystemExit("The studio is already running a job for this project – wait for it to finish.")
        kind = "analyze" if op == "analyze" else (rest[1] if len(rest) > 1 else "preview")
        core.set_job(pid, kind=kind, status="running", progress=f"Claude: {kind}", log=[], error=None, pending=[])
        try:
            if op == "analyze":
                pipeline.analyze_project(pid)
                msg = "Analysis complete."
            else:
                out = pipeline.render_project(pid, preview=(kind == "preview"))
                msg = f"Rendered {out.name}"
                print(out)
            core.set_job(pid, status="done", progress=msg, finished=core.now_iso())
        except Exception as e:  # never leave the UI stuck on "running"
            core.set_job(pid, status="error", error=str(e), progress="Failed", finished=core.now_iso())
            raise
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
