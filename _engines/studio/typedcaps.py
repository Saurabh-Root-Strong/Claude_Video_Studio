"""📝 YOUR OWN CAPTION TEXT – paste the script of the video ("your captions aren't clear, use these: [0:00] So, gold …")
and the captions show YOUR words, timed to your voice.

Why it's needed: whisper can miss whole stretches (a quiet / fast / mixed-language take: 13 s heard as one word), and
a script is often a little cleaner than the take. So the text is timed in three layers:
  1. time stamps in the paste ("[0:07]") = when that line starts in the EDITED video (what you watched);
     no stamps -> the script is placed where its words are heard (whole video or one part of it)
  2. inside a line, every word whisper DID hear ("अचानक" ~ "achanak", "डाउंट्रेंड" ~ "downtrend") pins that word's time
  3. the words between two pins follow your voice: spread over the speaking time between them (pauses skipped),
     longer words a little longer

Stored in SOURCE time, so trims / silence settings / reorders after it keep each word on the sound it belongs to:
    data["caption_typed"] = {src: {"cover": [[a, b], ...], "words": [[text, s, e], ...]}}
`cover` = the stretches of the clip your text replaces (whisper's words there aren't shown; elsewhere they still are).
Whisper's own words keep driving silence removal / word cuts – only what the captions SAY changes.
One undo step; "captions auto" goes back to the automatic captions.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

import core
import hinglish

STAMP = re.compile(r"^\s*[\[(]?\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)\s*[\])]?\s*[-–:]?\s*")
INTENT = re.compile(r"\b(?:captions?|subtitles?|subs|script|transcript|kapshan)\b", re.I)
RESET = re.compile(r"^\s*(?:captions?\s+(?:auto|automatic|from\s+(?:the\s+)?audio|back)|reset\s+captions?|"
                   r"(?:use\s+)?auto(?:matic)?\s+captions?|remove\s+my\s+captions?|undo\s+my\s+captions?)\s*[.!]*\s*$", re.I)
WORD = re.compile(r"[^\s]+")
PIN = 0.6  # sound-alike similarity for a heard word to pin a typed word's time


def _secs(s: str) -> float:
    t = 0.0
    for p in s.split(":"):
        t = t * 60 + float(p)
    return t


def split_script(text: str) -> list[tuple[float | None, str]] | None:
    """The caption script inside a note as [(start seconds | None, line)], or None when the note isn't one.
    A script = 2+ time-stamped lines ("[0:07] Gold jo hai …"), or a caption instruction followed by 2+ lines of
    speech. Instruction lines before the first stamp / the speech are dropped."""
    lines = [l.rstrip() for l in text.replace("\r", "").split("\n")]
    stamped = [i for i, l in enumerate(lines) if STAMP.match(l) and len(STAMP.sub("", l).split()) >= 2]
    if len(stamped) >= 2:
        body = lines[stamped[0]:]
    else:
        head = next((i for i, l in enumerate(lines) if INTENT.search(l)), None)
        if head is None:
            return None
        body = lines[head + 1:]
        # an instruction continuing on the next line ("and make it proper") is not speech
        while body and (not body[0].strip() or (len(body[0].split()) <= 5 and re.match(r"\s*(?:and|aur|please|pls|make|use|fix)\b", body[0], re.I))):
            body.pop(0)
        if sum(1 for l in body if len(l.split()) >= 3) < 2:
            return None
    out = []
    for l in body:
        m = STAMP.match(l)
        txt = STAMP.sub("", l).strip() if m else l.strip()
        if txt:
            out.append((_secs(m.group(1)) if m else None, txt))
    return out if sum(len(t.split()) for _, t in out) >= 6 else None


def _key(w: str) -> str:
    """Sound-alike spelling: Hinglish letters, lower case, no punctuation."""
    t = hinglish._auto_hinglish(w) if hinglish.DEVA.search(w) else w
    return re.sub(r"[^a-z0-9]", "", t.lower())


def _skel(k: str) -> str:
    """Consonant skeleton: hai/hain/he, mein/me, downtrend/dauntrend come out alike."""
    k = k.replace("ph", "f").replace("w", "v").replace("z", "j").replace("q", "k").replace("ck", "k")
    s = k[:1] + re.sub(r"[aeiouyh]", "", k[1:])
    return re.sub(r"(.)\1+", r"\1", s)


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    r = SequenceMatcher(None, a, b).ratio()
    sa, sb = _skel(a), _skel(b)
    if sa and sb and min(len(sa), len(sb)) >= 2:
        r = max(r, SequenceMatcher(None, sa, sb).ratio() * 0.95)
    return r


def _align(uk: list[str], sk: list[str], partial: bool = False) -> list[tuple[int, int, float]]:
    """Needleman–Wunsch of typed keys vs heard keys (semi-global when `partial`: heard words before/after are free).
    Returns the matched pairs (typed index, heard index, similarity), in order."""
    n, m = len(uk), len(sk)
    if not n or not m:
        return []
    GAP = -0.6
    NEG = -1e9
    sim = [[_sim(a, b) for b in sk] for a in uk]
    score = [[NEG] * (m + 1) for _ in range(n + 1)]
    back = [[0] * (m + 1) for _ in range(n + 1)]
    score[0][0] = 0.0
    for j in range(1, m + 1):
        score[0][j], back[0][j] = (0.0 if partial else GAP * j), 2
    for i in range(1, n + 1):
        score[i][0], back[i][0] = GAP * i, 1
        si, sp, sc = sim[i - 1], score[i - 1], score[i]
        for j in range(1, m + 1):
            best, b = sp[j - 1] + (si[j - 1] - 0.55) * 4, 0
            v = sp[j] + GAP
            if v > best:
                best, b = v, 1
            v = sc[j - 1] + GAP
            if v > best:
                best, b = v, 2
            sc[j], back[i][j] = best, b
    i, j = n, m
    if partial:
        j = max(range(m + 1), key=lambda jj: score[n][jj])
    out = []
    while i > 0 and j > 0:
        b = back[i][j]
        if b == 0:
            out.append((i - 1, j - 1, sim[i - 1][j - 1]))
            i, j = i - 1, j - 1
        elif b == 1:
            i -= 1
        else:
            j -= 1
    return out[::-1]


# ---------------------------------------------------------------- the edited video's clock <-> the clips' clocks

def _timeline(pid: str, data: dict) -> tuple[list[tuple], dict]:
    """Pieces of the edited video: (out_a, out_b, src, src_a, src_b, speed), in order, plus silences per clip.
    Mirrors the render: kept scenes in order, each scene's kept ranges (silence removal + word cuts), speed."""
    import pipeline
    segs, sils, t = [], {}, 0.0
    for sc in data["scenes"]:
        if not sc.get("keep", True) or sc.get("role") == "broll" or sc.get("src_skipped"):
            continue
        src = sc["src"]
        if src not in sils:
            sils[src] = pipeline.load_silences(pid, src)
        sp = float(sc.get("speed") or 1.0) or 1.0
        for a, b in pipeline.kept_ranges(sc, sils[src], data["settings"]):
            if b - a < 0.02:
                continue
            d = (b - a) / sp
            segs.append((t, t + d, src, sc["in"] + a, sc["in"] + b, sp))
            t += d
    return segs, sils


def _to_out(segs, src: str, st: float) -> float | None:
    for oa, ob, s, a, b, sp in segs:
        if s == src and a - 1e-6 <= st <= b + 1e-6:
            return oa + (st - a) / sp
    return None


def _to_src(segs, ot: float) -> tuple[int, float]:
    """(piece index, clip time) for an edited-video time (clamped into the nearest piece)."""
    for k, (oa, ob, s, a, b, sp) in enumerate(segs):
        if ot < ob or k == len(segs) - 1:
            ot2 = min(max(ot, oa), ob)
            return k, a + (ot2 - oa) * sp
    return 0, segs[0][3]


def _speech(segs, sils) -> list[tuple[float, float]]:
    """Speaking stretches in edited-video time: every piece minus its pauses (>= 0.25 s)."""
    out = []
    for oa, ob, src, a, b, sp in segs:
        cur = a
        for s, e in sorted(sils.get(src) or []):
            if e - s < 0.25 or e <= cur or s >= b:
                continue
            if s > cur:
                out.append((oa + (cur - a) / sp, oa + (min(s, b) - a) / sp))
            cur = max(cur, e)
        if cur < b:
            out.append((oa + (cur - a) / sp, ob))
    return [(x, y) for x, y in out if y - x > 0.02]


def _spread(words: list[str], lo: float, hi: float, speech) -> list[tuple[float, float]]:
    """Times for words said between lo and hi: over the speaking time there (pauses skipped), by word length."""
    if not words:
        return []
    if hi - lo < 0.02:
        hi = lo + 0.12 * len(words)
    parts = [(max(a, lo), min(b, hi)) for a, b in speech if b > lo and a < hi]
    parts = [(a, b) for a, b in parts if b - a > 0.02]
    if sum(b - a for a, b in parts) < 0.15 * len(words):
        parts = [(lo, hi)]  # no measurable speech there: the whole stretch
    total = sum(b - a for a, b in parts)
    wts = [len(re.sub(r"\W", "", w)) + 2 for w in words]
    W = sum(wts)

    def at(x: float) -> float:  # x seconds of speaking time after lo -> clock time
        for a, b in parts:
            if x <= b - a:
                return a + x
            x -= b - a
        return parts[-1][1]
    out, acc = [], 0.0
    for wt in wts:
        s = at(acc)
        acc += total * wt / W
        out.append((s, max(at(acc), s + 0.04)))
    return out


def apply_script(pid: str, data: dict, script: list[tuple[float | None, str]],
                 window: tuple[float, float] | None = None) -> str:
    """Time a pasted script against the edited video and store it as the caption text. Returns the reply;
    raises ValueError when it can't be placed. `window` = (start, end) on the edited clock: the whole text belongs
    there (one scene's caption text typed in the Inspector)."""
    segs, sils = _timeline(pid, data)
    if not segs:
        raise ValueError("No scenes in the video yet – analyze the footage first.")
    total = segs[-1][1]
    speech = _speech(segs, sils)
    # what whisper heard, on the edited clock
    heard: list[tuple[float, float, str]] = []
    for src in {s[2] for s in segs}:
        for w in core.read_json(core.work_file(pid, src, "words.json"), []) or []:
            s_, e_ = _to_out(segs, src, w["s"]), _to_out(segs, src, min(w["e"], w["s"] + 3))
            mid = _to_out(segs, src, (w["s"] + w["e"]) / 2)
            if mid is not None and s_ is not None:
                heard.append((s_, max(e_ if e_ is not None else s_ + 0.2, s_ + 0.04), w["w"]))
    heard.sort()
    hk = [_key(w) for _, _, w in heard]

    # blocks: a stamped line starts one; an unstamped line belongs to the line above
    stamps_ok = window is None and any(t is not None for t, _ in script)
    if window is not None:
        blocks_w = [[window[0], window[1], [w for _, l in script for w in WORD.findall(l)]]]
    if stamps_ok:
        ts = [t for t, _ in script if t is not None]
        if ts != sorted(ts) or ts[0] > total or ts[-1] > total + 2:
            stamps_ok = False  # stamps of another cut / out of order: place it by its words instead
    blocks: list[list] = []  # [start, end, words]
    if stamps_ok:
        for t, line in script:
            if t is not None or not blocks:
                blocks.append([t or 0.0, None, []])
            blocks[-1][2] += WORD.findall(line)
        for k, b in enumerate(blocks):
            b[1] = blocks[k + 1][0] if k + 1 < len(blocks) else total
    elif window is not None:
        blocks = blocks_w
    else:
        words = [w for _, l in script for w in WORD.findall(l)]
        uk = [_key(w) for w in words]
        partial = len(words) < 0.6 * len(heard)
        pairs = [p for p in _align(uk, hk, partial) if p[2] >= PIN]
        if len(pairs) < max(3, 0.25 * min(len(words), len(heard))):
            raise ValueError(f"Can't tell where this text goes – only {len(pairs)} of its {len(words)} words are heard "
                             f"in the video. Put the start time before each line, like  [0:07] Gold jo hai …")
        a = heard[pairs[0][1]][0] if partial else 0.0
        b = heard[pairs[-1][1]][1] if partial else total
        blocks = [[a, b, words]]

    placed: list[tuple[str, float, float, int]] = []
    lines_of: list[int] = []  # one entry per block placed: the block number goes with each word (lines stay lines)
    pins = 0
    for a, b, words in blocks:
        if not words:
            continue
        idx = [j for j, (s, e, _) in enumerate(heard) if a <= (s + e) / 2 < b]
        wk = [_key(w) for w in words]
        # a pin must be a real word match: short words ("ka", "hai") pin only on an exact match
        pairs = [(i, idx[j], sm) for i, j, sm in _align(wk, [hk[j] for j in idx])
                 if sm >= PIN and (len(wk[i]) >= 4 or sm >= 0.99)]
        # ...and leave room for the words between pins (>= 0.12 s each): a pin that would squeeze them is a
        # chance look-alike – drop the weaker of the two (shorter word), until all fit
        while True:
            bad = None
            prev = (-1, a, None)  # (word index, end time, pair index)
            for q, (i, j, sm) in enumerate(pairs + [(len(words), None, 1.0)]):
                start = heard[j][0] if j is not None else b
                if start - prev[1] < 0.12 * (i - prev[0] - 1) - 0.05 or start < prev[1] - 0.3:
                    cand = [x for x in (prev[2], q if j is not None else None) if x is not None]
                    bad = min(cand, key=lambda x: (pairs[x][2] * len(wk[pairs[x][0]]))) if cand else None
                    break
                prev = (i, heard[j][1] if j is not None else b, q)
            if bad is None:
                break
            pairs.pop(bad)
        times: list[tuple[float, float] | None] = [None] * len(words)
        for i, j, _ in pairs:
            s, e = max(a, heard[j][0]), min(b, heard[j][1])
            times[i] = (s, e if e > s + 0.04 else s + 0.1)
        pins += len(pairs)
        # words between pins follow the voice
        i = 0
        while i < len(words):
            if times[i] is not None:
                i += 1
                continue
            k = i
            while k < len(words) and times[k] is None:
                k += 1
            lo = times[i - 1][1] if i > 0 else a
            hi = times[k][0] if k < len(words) else b
            for n, tt in enumerate(_spread(words[i:k], lo, max(hi, lo), speech)):
                times[i + n] = tt
            i = k
        # never out of order on screen: each word starts after the one before, inside its line's window
        last = a
        for w, (s, e) in zip(words, times):
            s = min(max(s, last), b - 0.04)
            e = min(max(e, s + 0.04), b)
            placed.append((w, s, e, len(lines_of)))
            last = s + 0.04
        lines_of.append(1)

    # back to the clips' own clocks
    typed: dict[str, dict] = {}
    lo_all, hi_all = blocks[0][0], blocks[-1][1]
    for oa, ob, src, sa, sb, sp in segs:
        x, y = max(oa, lo_all), min(ob, hi_all)
        if y - x > 0.01:
            typed.setdefault(src, {"cover": [], "words": []})["cover"].append([round(sa + (x - oa) * sp, 3), round(sa + (y - oa) * sp, 3)])
    for w, s, e, ln in placed:
        k, cs = _to_src(segs, s)
        src, sb_ = segs[k][2], segs[k][4]
        ce = min(sb_, cs + (e - s) * segs[k][5])
        typed.setdefault(src, {"cover": [], "words": []})["words"].append([w, round(cs, 3), round(max(ce, cs + 0.04), 3), ln])
    rev = core.new_id("cap")
    prev = data.get("caption_typed") or {}
    for src, v in typed.items():
        # only the stretch this text covers changes: your earlier text elsewhere in the clip stays
        o = prev.get(src) if isinstance(prev.get(src), dict) else None
        if o:
            inside = lambda t, cv=v["cover"]: any(a_ - 1e-3 <= t <= b_ + 1e-3 for a_, b_ in cv)  # noqa: E731
            keep = [x for x in (o.get("words") or []) if not inside((x[1] + x[2]) / 2)]
            base = 1 + max([x[3] for x in keep if len(x) > 3] or [-1])
            v["words"] = sorted(keep + [[w, s, e, ln + base] for w, s, e, ln in v["words"]], key=lambda x: x[1])
            v["cover"] = _merge((o.get("cover") or []) + v["cover"])
        v["rev"], v["state"] = rev, "estimate"  # the voice aligner (align()) refines these times in the background
    data["caption_typed"] = {**prev, **typed}
    # scenes whisper heard as "no speech" had their captions switched off automatically – your text is speech:
    # switch them back on (only those automatic offs; a scene YOU turned off stays off). "captions auto" undoes it.
    for sc in data["scenes"]:
        t = typed.get(sc["src"])
        if t and sc.get("captions_auto_off") and any(sc["in"] <= w[1] < sc["out"] for w in t["words"]):
            sc.pop("captions_auto_off", None)
            sc["captions"], sc["captions_typed_on"] = True, True
    n = len(placed)
    how = (f"each line at its time stamp ({len(blocks)} lines)" if stamps_ok else
           "placed where its words are heard")
    part = "" if (lo_all <= 0.5 and hi_all >= total - 0.5) else f" for {_fmt(lo_all)}–{_fmt(hi_all)} of the video – the rest keeps its captions"
    return (f"Captions now show your text{part}: {n} words, {how}; {pins} pinned to words heard in your voice, "
            f"the rest follow your speaking pace. Say “captions auto” to go back to the automatic captions.")


ALIGNER = core.ENGINES / "separate" / "run_align.py"


def aligner_available() -> bool:
    import refaudio
    return refaudio.available() and ALIGNER.is_file()


def needs_align(data: dict) -> bool:
    return any(isinstance(v, dict) and v.get("state") == "estimate" for v in (data.get("caption_typed") or {}).values())


def _speakable(w: str) -> str:
    """What the aligner listens for: Latin letters (Hindi script turned into Hinglish first)."""
    t = hinglish._auto_hinglish(w) if hinglish.DEVA.search(w) else w
    return re.sub(r"[^a-z']", "", t.lower())


def align(pid: str, log=None) -> str:
    """Background step after a pasted script: listen to the voice and put EVERY typed word on the moment it is said
    (MMS forced alignment, ~0.5 s of CPU per second of audio; the 1.2 GB model downloads on first use).
    Replaces the estimate in data["caption_typed"] only if the text wasn't changed meanwhile."""
    import json
    import subprocess
    import tempfile
    import refaudio
    data = core.load(pid)
    todo = {src: v for src, v in (data.get("caption_typed") or {}).items() if isinstance(v, dict) and v.get("state") == "estimate"}
    if not todo:
        return "Captions already aligned."
    if not aligner_available():
        return "Voice aligner not installed (run Setup.bat) – captions keep the estimated timing."
    done, total_w, low = {}, 0, 0
    for src, v in todo.items():
        words = v.get("words") or []
        if not words:
            continue
        cover = v.get("cover") or [[words[0][1], words[-1][2]]]
        a, b = min(c[0] for c in cover), max(c[1] for c in cover)
        toks, where = [], []
        for k, x in enumerate(words):
            if k and len(x) > 3 and len(words[k - 1]) > 3 and x[3] != words[k - 1][3]:
                toks.append("*")  # a new line: the voice may say something that isn't in the text in between
                where.append(None)
            toks.append(_speakable(str(x[0])))
            where.append(k)
        src_p = core.raw_dir(pid) / src
        if not src_p.is_file():
            continue
        if log:
            log(f"Lining up your caption text with your voice in {src} ({len(words)} words)…")
        with tempfile.TemporaryDirectory() as td:
            jp, op = f"{td}/job.json", f"{td}/out.json"
            json.dump({"blocks": [{"s": a, "e": b, "words": toks}]}, open(jp, "w", encoding="utf-8"))
            r = subprocess.run([str(refaudio.PY), "-I", str(ALIGNER), str(core.FFMPEG), str(src_p), jp, op],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode != 0:
                raise RuntimeError("Caption alignment failed: " + ((r.stderr or r.stdout).strip().splitlines() or ["?"])[-1][:200])
            times = json.load(open(op, encoding="utf-8"))["blocks"][0]["times"]
        got: list = [None] * len(words)
        for t, k in zip(times, where):
            if k is not None and t:
                got[k] = t
        # words with no letters ("2x", "₹500") or not heard: between their neighbours, by the estimate's spacing
        new = []
        for k, x in enumerate(words):
            if got[k]:
                s, e = got[k][0], got[k][1]
                low += got[k][2] < 0.1
            else:
                pk = next((j for j in range(k - 1, -1, -1) if got[j]), None)
                nk = next((j for j in range(k + 1, len(words)) if got[j]), None)
                ps, pe = (got[pk][1], words[pk][2]) if pk is not None else (a, a)
                ns, nst = (got[nk][0], words[nk][1]) if nk is not None else (b, b)
                f = (x[1] - pe) / (nst - pe) if nst - pe > 1e-3 else 0.5
                s = ps + (ns - ps) * min(1.0, max(0.0, f))
                e = s + min(0.4, x[2] - x[1])
            new.append([x[0], round(s, 3), round(e, 3), *x[3:]])
        for k in range(len(new) - 1):  # in order, no overlap, readable
            new[k + 1][1] = max(new[k + 1][1], new[k][1] + 0.04)
            new[k][2] = round(min(max(new[k][2], new[k][1] + 0.06), max(new[k + 1][1], new[k][1] + 0.06)), 3)
        done[src] = (v.get("rev"), new)
        total_w += len(new)

    def fn(d):
        for src, (rev, new) in done.items():
            v = (d.get("caption_typed") or {}).get(src)
            if isinstance(v, dict) and v.get("rev") == rev:  # text changed meanwhile: that newer text gets its own pass
                v["words"], v["state"] = new, "aligned"
    core.mutate(pid, fn)
    return (f"Captions lined up with your voice: {total_w} words on the exact moment you say them"
            + (f" ({low} sound different from the text – check those spots)" if low else "") + ".")


def scene_has_typed(data: dict, sc: dict) -> bool:
    v = (data.get("caption_typed") or {}).get(sc["src"])
    return bool(isinstance(v, dict) and any(sc["in"] <= (x[1] + x[2]) / 2 <= sc["out"] for x in v.get("words") or []))


def _merge(iv: list) -> list:
    out = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1] + 1e-3:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _scene_window(pid: str, data: dict, sc: dict) -> tuple[float, float] | None:
    """Where a scene plays on the edited clock (start, end) – None when it isn't in the video."""
    segs, _ = _timeline(pid, data)
    mine = [(oa, ob) for oa, ob, src, a, b, sp in segs if src == sc["src"] and a >= sc["in"] - 1e-3 and b <= sc["out"] + 1e-3]
    return (min(x[0] for x in mine), max(x[1] for x in mine)) if mine else None


def scene_words(pid: str, data: dict, sc: dict) -> list[str]:
    """What the captions say in one scene now (your text if typed, else the transcript as shown)."""
    import pipeline
    ws = core.read_json(core.work_file(pid, sc["src"], "words.json"), []) or []
    out = []
    for w in pipeline.words_in(words_for(data, sc["src"], ws), sc["in"], sc["out"]):
        out.append(w["w"] if w.get("fixed") else hinglish.caption_word(w["w"], data["settings"]))
    return [w for w in out if w]


def set_scene_text(pid: str, data: dict, sid: str, text: str) -> str:
    """✏ Inspector: the caption text of ONE scene, as you typed it – timed to your voice there (aligner refines)."""
    sc = core.scene_by_id(data, sid)
    if not sc:
        raise ValueError("That scene is no longer in the project.")
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        raise ValueError("Type the words first – or switch Captions off for this scene.")
    if len(text) > 3000:
        raise ValueError("That's a lot of text for one scene – split the scene or paste a script in Tell the editor.")
    win = _scene_window(pid, data, sc)
    if not win:
        raise ValueError("This scene isn't in the video (cut or skipped) – bring it back first.")
    apply_script(pid, data, [(None, text)], window=win)
    if sc.get("captions") is False and sc.get("captions_auto_off"):
        sc.pop("captions_auto_off", None)
        sc["captions"], sc["captions_typed_on"] = True, True
    n = len(text.split())
    return f"Scene {data['scenes'].index(sc) + 1}: captions now say your text ({n} words) – lining it up with your voice…"


def scene_auto(pid: str, data: dict, sid: str) -> str:
    """Back to the automatic (transcript) captions for one scene."""
    sc = core.scene_by_id(data, sid)
    v = (data.get("caption_typed") or {}).get(sc["src"]) if sc else None
    if not v:
        return "This scene already uses the automatic captions."
    a, b = sc["in"], sc["out"]
    v["words"] = [x for x in v["words"] if not (a <= (x[1] + x[2]) / 2 <= b)]
    cov = []
    for c0, c1 in v.get("cover") or []:  # cut [a, b] out of the covered stretches
        if c1 <= a or c0 >= b:
            cov.append([c0, c1])
        else:
            if c0 < a:
                cov.append([c0, a])
            if c1 > b:
                cov.append([b, c1])
    v["cover"] = cov
    if not v["words"]:
        data["caption_typed"].pop(sc["src"], None)
        if not data["caption_typed"]:
            data.pop("caption_typed")
    if sc.pop("captions_typed_on", None):
        sc["captions"], sc["captions_auto_off"] = False, True
    return f"Scene {data['scenes'].index(sc) + 1}: back to the automatic captions."


def _fmt(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def reset(data: dict) -> str:
    if not data.get("caption_typed"):
        return "Captions already come from the audio."
    data.pop("caption_typed", None)
    for sc in data["scenes"]:
        if sc.pop("captions_typed_on", None):
            sc["captions"], sc["captions_auto_off"] = False, True
    return "Captions are automatic again (from the audio)."


def words_for(data: dict, src: str, words: list[dict]) -> list[dict]:
    """The caption words for one clip: your text where you gave one, whisper's words elsewhere.
    A typed word carries fixed=True: shown exactly as you wrote it (no Hinglish conversion / spellings)."""
    t = (data.get("caption_typed") or {}).get(src)
    if not t or not isinstance(t, dict):
        return words
    cover = t.get("cover") or []
    keep = [w for w in words if not any(a <= (w["s"] + w["e"]) / 2 <= b for a, b in cover)]
    mine = [{"w": str(x[0]), "s": float(x[1]), "e": float(x[2]), "fixed": True} for x in t.get("words") or []
            if isinstance(x, (list, tuple)) and len(x) >= 3]
    return sorted(keep + mine, key=lambda w: w["s"])


def view(pid: str, data: dict, sc: dict, cache: dict) -> str | None:
    """What the captions say in one scene when you typed them (for the Scenes page), else None."""
    if not (data.get("caption_typed") or {}).get(sc["src"]):
        return None
    import pipeline
    if sc["src"] not in cache:
        cache[sc["src"]] = core.read_json(core.work_file(pid, sc["src"], "words.json"), []) or []
    return " ".join(w["w"] for w in pipeline.words_in(words_for(data, sc["src"], cache[sc["src"]]), sc["in"], sc["out"]))
