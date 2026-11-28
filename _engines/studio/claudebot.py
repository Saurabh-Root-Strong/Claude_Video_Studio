"""🤖 CLAUDE AUTO-REPLY – what the studio can't do by itself (a note it doesn't understand, "① Plan story", a reference
to study, storyboard rework) lands in the project's inbox (commands with status "claude"). Before, it waited there
until you typed "process studio inbox" in VS Code. Now the studio starts Claude Code itself, headless, a few seconds
after something arrives:

    claude -p "<process the inbox of project X>"   (cwd = the studio root, CLAUDE.md files apply)

Safety: Claude may ONLY read files and run the studio's own command line (studio_cli.py, which goes through the same
locks / validation / undo as the page) and write storyboard plans – every other tool is refused (headless = no prompt,
anything not allowed is denied). One run at a time; a command is tried ONCE automatically (no loop that burns usage
if Claude can't finish it – it then says so and waits for you). Switch: the 🤖 box on the Scenes page
(Library/.studio.json "claude_auto"; default on).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import traceback

import core

CFG = core.ROOT / "Library" / ".studio.json"
LOG = core.ROOT / "Library" / ".claude_runs.log"
PY = "_engines/studio/.venv/Scripts/python.exe"
CLI = "_engines/studio/studio_cli.py"
DEBOUNCE = 4.0        # s after the newest item: a note that queues 3 things starts ONE run
RUN_TIMEOUT = 40 * 60
ALLOWED = [
    "Read", "Glob", "Grep",
    f"Bash({PY} {CLI}:*)", f"Bash(./{PY} {CLI}:*)", "Bash(_engines\\studio\\.venv\\Scripts\\python.exe _engines\\studio\\studio_cli.py:*)",
    f"PowerShell({PY} {CLI}:*)", "PowerShell(_engines\\studio\\.venv\\Scripts\\python.exe _engines\\studio\\studio_cli.py:*)",
    "PowerShell(& _engines\\studio\\.venv\\Scripts\\python.exe _engines\\studio\\studio_cli.py:*)",
    "Write(Storyboards/**)", "Edit(Storyboards/**)",
]

_state: dict = {"running": None, "started": None, "last": {}, "tried": set()}
_lock = threading.Lock()


def _cfg() -> dict:
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def enabled() -> bool:
    return _cfg().get("claude_auto", True) is not False


def set_enabled(on: bool) -> None:
    c = _cfg()
    c["claude_auto"] = bool(on)
    CFG.parent.mkdir(parents=True, exist_ok=True)
    core._atomic_write(CFG, c)


def exe() -> str | None:
    return shutil.which("claude")


def status(pid: str) -> dict:
    with _lock:
        last = dict(_state["last"].get(pid) or {})
        return {"enabled": enabled(), "installed": bool(exe()), "running": _state["running"] == pid,
                "busy_other": bool(_state["running"]) and _state["running"] != pid, "started": _state["started"]
                if _state["running"] == pid else None, "last": last}


def _pending(d: dict) -> list[dict]:
    return [c for c in d.get("commands", []) if c.get("status") == "claude"]


def _prompt(pid: str, items: list[dict]) -> str:
    lst = "\n".join(f"- {c['id']} (scene {c.get('scene_no') or 'whole video'}): {c.get('text', '')[:600]}" for c in items)
    return (f"process studio inbox – ONLY project {pid}. Follow _engines/studio/CLAUDE.md. The user is waiting in the "
            f"browser; they cannot answer questions, so never ask – decide sensibly.\n"
            f"Run the studio command line exactly like this, from the current folder:\n"
            f"  {PY} {CLI} <args>\n"
            f"(inbox / show {pid} / cmd {pid} <sceneNo|-> \"...\" / set ... / storyboard ... / render {pid} preview / "
            f"done {pid} <cmd_id> \"reply\"). You cannot run any other program or edit code.\n"
            f"Pending commands:\n{lst}\n"
            f"Finish EVERY one with `done {pid} <cmd_id> \"<what you did, in plain words>\"`. A reply longer than one "
            f"short sentence, or with quotes or line breaks (lists, suggestions, plans): FIRST Write it to "
            f"Storyboards/{pid}/reply_<cmd_id>.md, then `done {pid} <cmd_id> @Storyboards/{pid}/reply_<cmd_id>.md` – "
            f"a quoted shell argument gets cut at its first quote / line break. If something can't be "
            f"done with these tools, still mark it done and say why and what the user can do instead. Keep replies "
            f"short. When anything changed the video, end with `render {pid} preview`.")


def _run(pid: str, items: list[dict]) -> None:
    ids = {c["id"] for c in items}
    t0 = time.time()
    res = {"ids": sorted(ids), "started": core.now_iso()}
    try:
        # --setting-sources project: your personal Claude Code settings (which may allow EVERY command, e.g.
        # "Bash(*)") are NOT loaded – only ALLOWED below + this repo's own .claude settings apply. No MCP servers.
        cmd = [exe(), "-p", _prompt(pid, items), "--output-format", "json", "--setting-sources", "project",
               "--strict-mcp-config", "--permission-mode", "default", "--allowedTools", *ALLOWED]
        r = subprocess.run(cmd, cwd=str(core.ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=RUN_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = {}
        for line in reversed((r.stdout or "").strip().splitlines()):  # the JSON result is the last line
            try:
                out = json.loads(line)
                break
            except ValueError:
                continue
        res.update(code=r.returncode, secs=round(time.time() - t0), cost=out.get("total_cost_usd"),
                   result=str(out.get("result") or r.stderr or "")[:400])
        left = [c["id"] for c in _pending(core.load(pid)) if c["id"] in ids]
        res["left"] = left
        if left:
            res["error"] = (f"Claude finished but {len(left)} request(s) are still open" if r.returncode == 0 else
                            "Claude could not run: " + ((r.stderr or r.stdout or "").strip().splitlines() or ["?"])[-1][:200])
    except subprocess.TimeoutExpired:
        res["error"] = "Claude took over 40 minutes and was stopped."
    except Exception as e:  # noqa: BLE001  the runner must never take the studio down
        traceback.print_exc()
        res["error"] = f"Claude could not start ({str(e)[:160]})."
    finally:
        res["finished"] = core.now_iso()
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"pid": pid, **res}, ensure_ascii=False) + "\n")
        except OSError:
            pass
        with _lock:
            _state["last"][pid] = res
            _state["running"], _state["started"] = None, None


def _tick() -> None:
    if not enabled() or not exe():
        return
    with _lock:
        if _state["running"]:
            return
    now = time.time()
    for p in core.list_projects():
        pid = p.get("id")
        try:
            d = core.load(pid)
        except Exception:  # noqa: BLE001  a damaged project must not stop the others
            continue
        items = [c for c in _pending(d) if c["id"] not in _state["tried"]]
        if not items:
            continue
        newest = max(time.mktime(time.strptime(c.get("created", core.now_iso())[:19], "%Y-%m-%dT%H:%M:%S")) for c in items)
        if now - newest < DEBOUNCE:
            continue  # more may be coming from the same note
        with _lock:
            if _state["running"]:
                return
            _state["running"], _state["started"] = pid, core.now_iso()
            _state["tried"].update(c["id"] for c in items)  # each command gets ONE automatic try
        threading.Thread(target=_run, args=(pid, items), daemon=True).start()
        return


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(3)


def start() -> None:
    """Called once by the studio server. Items already waiting when the studio starts are NOT auto-run (they may be
    days old) – only what arrives from now on; "process studio inbox" still handles the old ones."""
    for p in core.list_projects():
        try:
            _state["tried"].update(c["id"] for c in _pending(core.load(p["id"])))
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_loop, daemon=True).start()


def retry(pid: str) -> None:
    """The user clicked "try again": give the open items one more automatic run."""
    d = core.load(pid)
    with _lock:
        for c in _pending(d):
            _state["tried"].discard(c["id"])
        _state["last"].pop(pid, None)
