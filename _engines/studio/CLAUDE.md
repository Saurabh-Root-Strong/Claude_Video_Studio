# CVE Studio — how Claude works the inbox

The user edits in the browser UI (http://127.0.0.1:8070, launched by `D:\Claude_Video_Editor\Start Studio.bat`).
Commands the UI can't do instantly land in project.json with `status: "claude"`.
When the user says **"process studio inbox"**:

1. `.venv\Scripts\python.exe studio_cli.py inbox` — list pending commands.
2. `studio_cli.py show <pid>` — brief, settings, references (contact-sheet paths), scene table.
   Read reference sheets (`work\refs\*.sheet.jpg`) and scene thumbs with the Read tool to see style.
3. Do the work:
   - Instant edits: `studio_cli.py cmd <pid> <sceneNo|-> "cut; speed 1.2"` (same grammar as the UI, see commands.py).
   - Raw fields: `studio_cli.py set <pid> <sceneNo> in=3.2 out=7.9 text="Title" text_pos=center`.
   - Motion graphics: render with Remotion (`_engines\remotion`) or HyperFrames (`_engines\hyperframes`, `-w 1`,
     never publish/share/cloud) — v1 renderer has no overlay track yet, so explain what was made and where.
4. `studio_cli.py done <pid> <cmd_id> "what I did, in plain words"` for each command (UI shows the reply).
5. Optionally `studio_cli.py render <pid> preview` so the user can watch the result in the UI.

FIRST CUT command (kind=first_cut): read brief + references, then cut weak/duplicate/filler scenes, set
format/captions/silence removal per the brief, add text titles where useful, reorder if the brief asks, render preview,
reply with a short scene-by-scene summary.

After changing studio code: run `.venv\Scripts\python.exe selftest.py` (temp root, 21 checks: rotation, scene
splitting, queue, split/undo, inbox, A/V sync per segment, host/origin/path security, cross-process lock) and
restart the server (`Start Studio.bat`). HyperFrames: always `init --skip-skills`; env HYPERFRAMES_SKIP_SKILLS /
NO_TELEMETRY / NO_UPDATE_CHECK = 1 are set; its skills live in `D:\Claude_Video_Editor\.claude\skills` (project scope).

Rules: project.json is shared with the running UI — always go through studio_cli.py / core.mutate (locks + atomic
write), never hand-edit while the server runs. RAM is tight (7 GB): one render at a time.
