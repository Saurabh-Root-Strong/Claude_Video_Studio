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

HINGLISH / CAPTION WORDING ("fix the Hinglish", "captions in Hinglish", wrong caption words):
`cmd <pid> - "captions hinglish"`, then `studio_cli.py captions <pid>` lists every distinct word -> what the captions
show. The offline converter (hinglish.py) gets plain Hindi right but can't know English words whisper wrote in
Devanagari or misheard (एक्सपेक्टेट, पॉल्ड). Use the brief/context to fix them all in ONE call:
`cmd <pid> - "spell ekspektet as expected; spell पॉल्ड as hold"` (key = original word OR its shown Hinglish;
`spell X as -` hides a word). Spellings apply in both scripts and are per project (settings.caption_fixes).
For ADDING words to the built-in dictionary for every future project, edit `BUILTIN` in hinglish.py.

BRAND KIT + CARDS: the approved profile is `Brand Kit\brand-kit.md` (+ `brand-kit.json`; new projects start on look "none" – the user picks ★ Brand Kit/R1 in Scenes → Look;
`POST /api/projects/<pid>/apply_brand` for old ones). Split layout = cards on top: per scene
`cmd <pid> <n> "card: title text with *keyword*"` / `"card: number 2x+ | label"` / `"card: chart 1,2,3 | label"` /
`"card off"`; a scene without a card keeps the previous one. Card text in Hinglish, 2-5 words, ONE orange *keyword*,
only facts the speaker says or data the user gives (never invent numbers for number/chart cards).

STORYBOARD (plan -> user review -> apply -> render): write a creative plan JSON
({"summary":..., "<sceneNo>": {"role", "card", "punch":[{s,e,zoom,text,why}], "cuts":[{s,e,text,reason,default}],
"broll":{"want"|"file"}}}) to `Storyboards/<pid>/claude_plan.json`, then `studio_cli.py storyboard build <pid> <plan>`
(adds auto items: dead-air trims, pauses, filler/false-start candidates; flags squeezed whisper timings). User reviews
at http://127.0.0.1:8070/storyboard/<pid> (Scenes tab → Storyboard) and clicks "Send feedback" → inbox command
kind=storyboard. Then `storyboard show <pid>` (statuses, switches, notes), rework rejected beats per notes (edit plan,
rebuild — statuses/notes are kept), `storyboard apply <pid>` (approved beats only, undoable), render. VERIFY any
word cut on the audio first: whisper word times inside fast speech can be off 0.3 s (re-transcribe a padded clip
with context; micro-silences inside words like "apne" are NOT word gaps). Never render before the user's feedback.

B-ROLL CUTAWAYS: scene["role"]="broll" = footage that never plays alone (opt-in only since 2026-11-19: no-speech clips stay normal scenes, captions off);
a speaker scene's `broll` = {"src": clip file or Assets/<name>, "where": "top"|"bottom"|"full" (omit = follow the look: split ->
top panel, else full screen; split: top = card panel, bottom = over the speaker, card stays; full layout: top/bottom =
that half, speaker in the other half). Pictures (png/jpg…) work too (held for the scene). "fit": "fit" = whole picture over a blurred copy (omit = fill/crop; `broll fit|fill`). Deleting an upload (`broll_asset/remove`) moves it to Assets/.trash (undo restores). The page uploads B-roll
straight into Raw Footage/<pid>/Assets (`upload?kind=broll&scene=<id>`, same file reused), "at": seconds (omit = continue the previous scene's footage)}. Voice keeps playing, B-roll
is silent and adds no length. Commands: `broll <clip>`, `broll top|bottom|full|layout`, `broll from 3`, `broll continue`, `broll off`,
`role broll|speaker`, whole-video `broll auto|off|top|bottom|full`. Storyboard plan "broll": {"file": <project clip or Assets
file>, "want": ..., "where": optional} -> applied as a cutaway. Absolute trims: `trim start to 0.4` / `trim end to 0.2`
(from the original edges); `captions auto`, `silences auto`.

LOOKS (one style preset per reference; project renders with the active one = project settings):
STUDY REFERENCE command (kind=ref_style, field ref=<id>): analyse that reference like the Brand Kit study (frames,
captions, pacing, audio loudness/music), then `studio_cli.py look <pid> R2 key=value ...` (STYLE keys only: format,
caption_*, highlight_color, remove_silence, silence_min, layout, bg_*, panel_color, music, loudness, duck_db, ...).
Writing the active look changes the project settings directly. Reply with what you set and why.
BRIEFS: `brief` = about this video (shared); `look_briefs[<look id>]` = how each look should feel. `show` prints the
video brief + the ACTIVE look's brief – follow both (first cut, storyboard, studying a reference).

PLAN THE EDIT command (kind=plan_edit; old projects may still have kind=first_cut – treat the same): read `show`
(video brief + active look brief + settings), references, scene table → write Storyboards/<pid>/claude_plan.json →
`storyboard build`. Do NOT edit scenes or render: the user approves beats first (STORYBOARD section). Reply with the
plan summary and "open Scenes → Storyboard to review".
LOOKS also include ★ Brand Kit (id `brandkit`): read live from Brand Kit/brand-kit.json, read-only from projects
(`look brandkit` is refused) – change the brand by editing brand-kit.json + brand-kit.md.

FIRST CUT command (legacy wording below – a first cut is now ALWAYS a storyboard plan, never direct edits) (kind=first_cut): read brief + references, then cut weak/duplicate/filler scenes, set
format/captions/silence removal per the brief, add text titles where useful, reorder if the brief asks, render preview,
reply with a short scene-by-scene summary.

After changing studio code: run `.venv\Scripts\python.exe selftest.py` (temp root: rotation, scene splitting,
queue, split/undo, inbox, A/V sync per segment, Hinglish captions, host/origin/path security, cross-process lock) and
restart the server (`Start Studio.bat`). HyperFrames: always `init --skip-skills`; env HYPERFRAMES_SKIP_SKILLS /
NO_TELEMETRY / NO_UPDATE_CHECK = 1 are set; its skills live in `D:\Claude_Video_Editor\.claude\skills` (project scope).

Rules: project.json is shared with the running UI — always go through studio_cli.py / core.mutate (locks + atomic
write), never hand-edit while the server runs. RAM is tight (7 GB): one render at a time.

LIBRARY (library.py, folder <root>/Library, never in git): transitions (ffmpeg xfade ids in library.TRANSITIONS), effects
(library.EFFECTS recipes), stickers (Microsoft Fluent Emoji 3D, MIT + Library/Stickers/My stickers), sounds (Wyzowl SFX Kit,
CC0, with sync points + levels, + Library/Sounds/My sounds). `python library.py setup` downloads/repairs it.
Scene keys: "transition" {type, dur, sound} = INTO this scene (length-neutral: outgoing last frame held, voice cut stays);
"effects" [ids]; "stickers" [{file, name, at, dur, pos, size, anim}]; "sfx" [{file, name, at, vol, sync, gain_db}]
(sound starts at at - sync so its hit lands at `at`). Commands: `transition <name> [0.5]`, `transition off|sound on|off`,
whole video `transitions <name>|off`; `effect <name>`, `effect remove <name>`, `effects off`; `sticker <id> at 1 for 2
top-right size 30% pop`, `sticker N <opts>`, `sticker remove N`; `sfx <id> at 0.5 vol 80%`, `sfx N <opts>`, `sfx remove N`.
Transitions + sounds are added when the segments are joined (pipeline.assemble); a failure there renders without them
and logs it. Effects + stickers are per segment (cache key includes the sticker files).
