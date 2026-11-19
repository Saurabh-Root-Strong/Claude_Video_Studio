"""Analysis and rendering pipeline for CVE Studio (FFmpeg + whisper.cpp + yt-dlp)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import cards
import core
import hinglish
from core import FFMPEG, FFPROBE, YTDLP

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class PipelineError(RuntimeError):
    pass


def run(cmd: list, cwd: Path | None = None, timeout: int | None = None) -> str:
    """Run a command, return combined output; raise PipelineError with the tail on failure."""
    p = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout,
                       creationflags=NO_WINDOW)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        tail = "\n".join(out.strip().splitlines()[-12:])
        raise PipelineError(f"{Path(str(cmd[0])).name} failed (exit {p.returncode}):\n{tail}")
    return out


# ----------------------------------------------------------------- probing

def probe(path: Path) -> dict:
    out = run([FFPROBE, "-v", "error", "-show_entries",
               "format=duration:stream=codec_type,width,height,r_frame_rate:stream_side_data=rotation:stream_tags=rotate",
               "-of", "json", path])
    j = json.loads(out[out.index("{"):])
    dur = float(j.get("format", {}).get("duration") or 0)
    meta = {"duration": round(dur, 3), "width": 0, "height": 0, "fps": 0.0, "has_audio": False, "has_video": False}
    for s in j.get("streams", []):
        if s.get("codec_type") == "video" and not meta["has_video"]:
            meta["has_video"] = True
            meta["width"], meta["height"] = int(s.get("width") or 0), int(s.get("height") or 0)
            num, _, den = (s.get("r_frame_rate") or "0/1").partition("/")
            try:
                meta["fps"] = round(float(num) / float(den or 1), 3)
            except (ValueError, ZeroDivisionError):
                meta["fps"] = 0.0
            # phones store portrait video as landscape + a rotation flag; ffmpeg auto-rotates on decode,
            # so report the DISPLAYED size or "original" format would crop a vertical video to landscape
            rot = 0
            for sd in s.get("side_data_list") or []:
                if "rotation" in sd:
                    rot = int(float(sd["rotation"]))
            if not rot and (s.get("tags") or {}).get("rotate"):
                rot = int(float(s["tags"]["rotate"]))
            if abs(rot) % 180 == 90:
                meta["width"], meta["height"] = meta["height"], meta["width"]
            meta["rotation"] = rot
        elif s.get("codec_type") == "audio":
            meta["has_audio"] = True
    return meta


# ----------------------------------------------------------------- transcription

def extract_audio(src: Path, wav: Path) -> None:
    run([FFMPEG, "-y", "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav])


def transcribe(wav: Path, out_prefix: Path, language: str = "auto", duration: float = 0.0) -> list[dict]:
    """whisper.cpp word-level transcription -> [{"w": text, "s": sec, "e": sec}]."""
    base = ["-m", core.WHISPER_MODEL, "-f", wav, "-l", language or "auto",
            "-ml", "1", "-sow", "-ojf", "-of", out_prefix]
    vad = core.ENGINES / "whisper" / "models" / "ggml-silero-v5.1.2.bin"
    if vad.exists():  # VAD keeps word timings from drifting after pauses
        base += ["--vad", "-vm", vad]
    last_err = None
    # CPU whisper on long footage can run ~2-3x real time; never cap a 2-hour podcast at 1 hour
    timeout = int(max(3600, duration * 4))
    for exe in (core.WHISPER_GPU, core.WHISPER_CPU):
        if not Path(exe).exists():
            continue
        try:
            run([exe, *base], timeout=timeout)
            break
        except (PipelineError, subprocess.TimeoutExpired) as e:
            last_err = PipelineError(str(e))
    else:
        raise last_err or PipelineError("whisper-cli not found – run Setup.bat")
    return parse_whisper_json(Path(f"{out_prefix}.json").read_bytes())


def parse_whisper_json(raw: bytes) -> list[dict]:
    """Parse whisper.cpp -ojf output into words.

    With -ml 1, whisper.cpp splits on TOKENS, and a token can end in the middle of a multi-byte character
    (common in Hindi/Devanagari: one letter = 3 bytes). The file is then not valid UTF-8. We decode with
    surrogateescape, then glue a piece that ends mid-character onto the next piece. whisper.cpp sometimes DROPS
    the rest of that character entirely (seen on real Hindi footage: ' \\xe0\\xa4', then '', then ' तो'), so if
    the next non-empty piece cannot complete it, the half character is discarded instead of waiting forever.
    """
    data = json.loads(raw.decode("utf-8", "surrogateescape"))
    words, pending, pend_s = [], b"", None
    for item in data.get("transcription", []):
        off = item.get("offsets") or {}
        s, e = off.get("from", 0) / 1000.0, off.get("to", 0) / 1000.0
        chunk = (item.get("text") or "").encode("utf-8", "surrogateescape")
        if not chunk:
            continue
        text = None
        if pending:
            try:
                text = (pending + chunk).decode("utf-8")
                s = pend_s
            except UnicodeDecodeError:
                pass  # completion never came – drop the half character, handle this piece on its own
            pending, pend_s = b"", None
        if text is None:
            try:
                text = chunk.decode("utf-8")
            except UnicodeDecodeError as ex:
                if ex.reason == "unexpected end of data":   # ends mid-character: wait for the next piece
                    pending, pend_s = chunk, s
                    continue
                text = chunk.decode("utf-8", "ignore")      # broken in the middle: keep what is readable
        if not text.strip():
            continue
        # a piece that starts mid-word (no leading space) belongs to the previous word
        if words and not text.startswith(" ") and not text.lstrip().startswith("[") and s - words[-1]["e"] < 0.05:
            words[-1]["w"] += text.strip()
            words[-1]["e"] = round(max(e, words[-1]["e"]), 3)
            continue
        text = text.strip()
        if text.startswith("[") and text.endswith("]"):
            continue  # skip [BLANK_AUDIO], [Music] etc.
        if e <= s:
            e = s + 0.05
        words.append({"w": text, "s": round(s, 3), "e": round(e, 3)})
    if pending:
        tail = pending.decode("utf-8", "ignore").strip()
        if tail:
            words.append({"w": tail, "s": round(pend_s or 0, 3), "e": round((pend_s or 0) + 0.1, 3)})
    return words


# ----------------------------------------------------------------- detection

def detect_cuts(src: Path, threshold: float = 0.3) -> list[float]:
    out = run([FFMPEG, "-hide_banner", "-i", src, "-an", "-vf",
               f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"])
    return sorted({round(float(m), 3) for m in re.findall(r"pts_time:([\d.]+)", out)})


def detect_silences(src: Path, noise_db: int = -35, min_dur: float = 0.4) -> list[list[float]]:
    out = run([FFMPEG, "-hide_banner", "-i", src, "-vn", "-af",
               f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"])
    sil, start = [], None
    for line in out.splitlines():
        m = re.search(r"silence_start: ([\d.\-]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and start is not None:
            sil.append([round(start, 3), round(float(m.group(1)), 3)])
            start = None
    return sil


def suspect_windows(words: list[dict], silences: list[list[float]], duration: float) -> list[tuple[float, float]]:
    """Stretches where whisper.cpp likely HALLUCINATED (seen on real Hindi footage: a sentence said once came out
    twice, every word squeezed to exactly 0.08 s). Signals: (a) >= 6 consecutive words with identical tiny
    durations, (b) a 4+ word sequence immediately repeated. Returns padded time windows to re-transcribe."""
    hits: list[tuple[int, int]] = []
    n = len(words)
    i = 0
    while i < n:  # (a) uniform tiny durations
        j = i
        d0 = words[i]["e"] - words[i]["s"]
        while j + 1 < n and d0 <= 0.12 and abs((words[j + 1]["e"] - words[j + 1]["s"]) - d0) < 0.006:
            j += 1
        if j - i + 1 >= 6:
            hits.append((i, j))
        i = j + 1
    keys = [hinglish.norm_key(w["w"]) for w in words]
    for size in range(12, 3, -1):  # (b) immediate repeats, longest first
        for i in range(0, n - 2 * size + 1):
            if keys[i:i + size] == keys[i + size:i + 2 * size] and any(keys[i:i + size]):
                hits.append((i, i + 2 * size - 1))
    wins = []
    for i, j in hits:
        a, b = words[i]["s"], words[j]["e"]
        before = [e for s, e in silences if e <= a + 0.05 and a - e <= 4.0]
        after = [s for s, e in silences if s >= b - 0.05 and s - b <= 4.0]
        a = max(before) if before else a - 0.3
        b = min(after) if after else b + 0.3
        wins.append((max(0.0, a), min(duration or b, b)))
    wins.sort()
    merged: list[list[float]] = []
    for a, b in wins:
        if merged and a <= merged[-1][1] + 0.2:
            merged[-1][1] = max(merged[-1][1], b)
        elif b - a <= 60:
            merged.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in merged]


def repair_transcript(wav: Path, words: list[dict], silences: list[list[float]], language: str, duration: float,
                      work: Path, log=lambda m: None) -> list[dict]:
    """Re-transcribe each suspect window on its own (whisper behaves on short clips) and splice the result in."""
    for k, (a, b) in enumerate(suspect_windows(words, silences, duration)):
        clip = work / f"repair_{k}.wav"
        # pad the clip with the surrounding pause (+ 0.6 s of digital silence): a clip ending right where speech
        # stops makes whisper squeeze its last words onto one timestamp
        ca = max(0.0, a - 0.3)
        try:
            run([FFMPEG, "-y", "-v", "error", "-ss", f"{ca:.3f}", "-to", f"{min(duration or b + 0.4, b + 0.4):.3f}",
                 "-i", wav, "-af", "apad=pad_dur=0.6", clip])
            new = transcribe(clip, work / f"repair_{k}", language, b - ca + 1.0)
        except (PipelineError, subprocess.TimeoutExpired, OSError) as e:
            log(f"re-transcribe {a:.1f}-{b:.1f}s failed ({str(e)[:80]}) – kept original words")
            continue
        finally:
            clip.unlink(missing_ok=True)
            for ext in (".json",):
                (work / f"repair_{k}{ext}").unlink(missing_ok=True)
        # the clip edges are pause, so everything whisper hears in [ca, cb] belongs to this window – replace the
        # old words over that whole span (a narrower span left one stale word mixed in at the edge)
        cb = b + 0.4
        new = [{**w, "s": round(w["s"] + ca, 3), "e": round(w["e"] + ca, 3)} for w in new]
        new = [w for w in new if ca <= (w["s"] + w["e"]) / 2 <= cb]
        inside = lambda w: ca <= (w["s"] + w["e"]) / 2 <= cb  # noqa: E731
        old_n = sum(1 for w in words if inside(w))
        # accept only plausible text: not empty, not itself glitchy, and not a collapse (a duplicate halves the
        # count; 20 words -> 1 means the clip was wrong, not the transcript)
        if not new or suspect_windows(new, silences, duration) or len(new) < 0.35 * old_n:
            log(f"re-transcribe {a:.1f}-{b:.1f}s gave no better text – kept original words")
            continue
        new = [{**w, "_r": 1} for w in new]
        merged = sorted([w for w in words if not inside(w)] + new, key=lambda w: w["s"])
        # seam: the same word heard by both passes (new clip's last word + old word right after the pause)
        out = []
        for w in merged:
            if out and hinglish.norm_key(out[-1]["w"]) == hinglish.norm_key(w["w"]) and \
                    w["s"] - out[-1]["s"] < 0.8 and ("_r" in out[-1]) != ("_r" in w):
                if "_r" in out[-1]:
                    continue  # keep the new one (already in out), drop the old duplicate
                out[-1] = w
                continue
            out.append(w)
        words = [{k: v for k, v in w.items() if k != "_r"} for w in out]
        log(f"fixed a whisper glitch at {a:.1f}-{b:.1f}s ({old_n} words -> {len(new)})")
    return words


def fix_word_times(words: list[dict], silences: list[list[float]]) -> list[dict]:
    """Whisper stretches words across pauses. Pull starts out of silences, clip ends into them, keep order."""
    sil = [(a, b) for a, b in silences if b - a >= 0.3]
    out, prev_e = [], 0.0
    for w in words:
        s, e = w["s"], w["e"]
        for a, b in sil:
            if a + 0.05 <= s < b:      # starts inside a silence -> really starts when speech resumes
                s = b
            if s < a < e - 0.05 and e > a + 0.4:  # runs deep into a silence -> really ends when it starts
                e = a
        s = max(s, prev_e)
        e = max(e, s + 0.08)
        out.append({**w, "s": round(s, 3), "e": round(e, 3)})
        prev_e = e
    return out


def build_scenes(duration: float, cuts: list[float], words: list[dict], silences: list[list[float]] | None = None,
                 min_len: float = 1.5, target: float = 6.0, max_len: float = 12.0) -> list[tuple[float, float]]:
    """Scene boundaries, strongest first: visual cuts > pauses >= 0.7 s / sentence ends > comma pauses (if long)."""
    cand: dict[float, float] = {}

    def add(t: float, prio: float):
        t = round(t, 3)
        for k in list(cand):          # merge near-duplicates, keep the stronger one
            if abs(k - t) < 0.5:
                if cand[k] >= prio:
                    return
                del cand[k]
        cand[t] = prio

    # Pauses: >= 0.7 s always split; >= 0.4 s split once the scene is >= 2.5 s long. Punctuation can't be relied on:
    # with --vad whisper turns most periods into commas (English too), and Hindi gets almost none, while natural
    # pauses between spoken lines are ~0.4-0.6 s (measured on the user's first real clip; isolates retakes).
    long_sil = [(a, b) for a, b in (silences or []) if b - a >= 0.4]
    for c in cuts:
        add(c, 3)
    for a, b in long_sil:
        inside = [c for c in cuts if a <= c <= b]
        add(inside[0] if inside else (a + b) / 2, 3 if inside else (2 if b - a >= 0.7 else 1.5))
    for i, w in enumerate(words[:-1]):
        nxt = words[i + 1]
        gap = max(0.0, nxt["s"] - w["e"])
        t = w["e"] + gap / 2
        if any(a - 0.15 <= t <= b + 0.15 for a, b in long_sil):
            continue  # this pause is already a (stronger) candidate
        txt = w["w"].rstrip()
        if txt.endswith((".", "?", "!", "।")):
            add(t, 2)
        elif txt.endswith((",", ";", ":")) and gap >= 0.25:
            add(t, 1)

    segs, start = [], 0.0
    for t in sorted(cand):
        prio = cand[t]
        if t - start < min_len or duration - t < (0.5 if prio == 3 else min_len):
            continue
        if prio >= 2 or (prio == 1.5 and t - start >= 2.5) or t - start >= target:
            segs.append((start, t))
            start = t
    segs.append((start, duration))

    # split very long scenes at the word gap nearest to equal-length points
    gaps = [round((words[i]["e"] + words[i + 1]["s"]) / 2, 3) for i in range(len(words) - 1)]
    final = []
    for a, b in segs:
        n = math.ceil((b - a) / max_len) if b - a > max_len else 1
        if n == 1:
            final.append((a, b))
            continue
        points = []
        for k in range(1, n):
            ideal = a + (b - a) * k / n
            inside = [g for g in gaps if a + min_len < g < b - min_len]
            points.append(min(inside, key=lambda g: abs(g - ideal)) if inside else ideal)
        edges = [a, *sorted(set(points)), b]
        final.extend((edges[i], edges[i + 1]) for i in range(len(edges) - 1))
    return [(round(a, 3), round(b, 3)) for a, b in final if b - a > 0.05]


def thumbnail(src: Path, t: float, out: Path, width: int = 320) -> None:
    run([FFMPEG, "-y", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", src, "-frames:v", "1",
         "-vf", f"scale={width}:-2", "-q:v", "4", out])


def make_proxy(src: Path, out: Path, has_audio: bool) -> None:
    """Light 540p H.264 copy for the browser player (plays HEVC/ProRes/MKV sources, easy on 7 GB RAM)."""
    out.parent.mkdir(exist_ok=True)
    tmp = out.with_name(out.stem + ".part.mp4")
    common = ["-vf", "scale=-2:'min(540,ih)'", "-g", "30", "-pix_fmt", "yuv420p"]
    audio = ["-c:a", "aac", "-b:a", "96k", "-ac", "2"] if has_audio else ["-an"]
    for venc in (["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "30"], ["-c:v", "libx264", "-preset", "veryfast", "-crf", "30"]):
        try:
            run([FFMPEG, "-y", "-v", "error", "-i", src, *common, *venc, *audio, "-movflags", "+faststart", tmp])
            os.replace(tmp, out)
            return
        except PipelineError:
            continue
    tmp.unlink(missing_ok=True)


def words_in(words: list[dict], a: float, b: float) -> list[dict]:
    return [w for w in words if w["e"] > a and w["s"] < b and (min(w["e"], b) - max(w["s"], a)) >= 0.5 * (w["e"] - w["s"])]


# ----------------------------------------------------------------- project analysis

def analyze_project(pid: str, reanalyze: bool = False) -> None:
    data = core.load(pid)
    lang = data["settings"].get("language", "auto")
    wd = core.work_dir(pid)
    (wd / "thumbs").mkdir(exist_ok=True)
    todo = [s for s in data["sources"] if reanalyze or not s.get("analyzed")]
    if not todo:
        core.job_log(pid, "Nothing new to analyze.")
        return

    failed = 0
    for idx, src in enumerate(todo, 1):
        tag = f"[{idx}/{len(todo)}] {src['file']}"
        try:
            _analyze_one(pid, src, tag, lang, wd)
        except Exception as e:  # one bad file must not stop the others
            failed += 1
            core.job_log(pid, f"{tag}: FAILED – {e}")
    if failed and failed == len(todo):
        raise PipelineError("Every file failed to analyze – see the activity log.")


def _analyze_one(pid: str, src: dict, tag: str, lang: str, wd: Path) -> None:
        path = core.raw_dir(pid) / src["file"]
        key = src["file"]  # FULL name: clip.mp4 and clip.mov must not share work files
        if not path.is_file():
            raise PipelineError("the file is missing from the Raw Footage folder")
        core.job_log(pid, f"{tag}: probing")
        meta = probe(path)
        if not meta["has_video"]:
            core.job_log(pid, f"{tag}: no video stream – skipped")
            return
        if meta["duration"] <= 0.1:
            raise PipelineError("could not read the video length (file damaged or still being copied?)")
        core.set_job(pid, progress=f"{tag}: making preview copy")
        make_proxy(path, wd / "proxy" / f"{key}.proxy.mp4", meta["has_audio"])
        meta["proxy"] = f"{key}.proxy.mp4" if (wd / "proxy" / f"{key}.proxy.mp4").exists() else None

        words: list[dict] = []
        silences: list[list[float]] = []
        if meta["has_audio"]:
            core.set_job(pid, progress=f"{tag}: transcribing")
            core.job_log(pid, f"{tag}: transcribing with Whisper")
            wav = wd / f"{key}.16k.wav"
            try:
                extract_audio(path, wav)
                silences = detect_silences(path)
                words = fix_word_times(transcribe(wav, wd / f"{key}.whisper", lang, meta["duration"]), silences)
                # detect glitches on the timing-FIXED words: raw whisper times of a glitch are squeezed together,
                # only after fix_word_times do they sit where the speech really is
                words = fix_word_times(repair_transcript(wav, words, silences, lang, meta["duration"], wd,
                                                         log=lambda m: core.job_log(pid, f"{tag}: {m}")), silences)
            finally:
                wav.unlink(missing_ok=True)  # 16 kHz WAV is ~115 MB/hour – never leave it behind on failure
            core.job_log(pid, f"{tag}: {len(words)} words")
        core.set_job(pid, progress=f"{tag}: detecting scenes")
        cuts = detect_cuts(path)
        core.job_log(pid, f"{tag}: {len(cuts)} visual cuts")
        with open(wd / f"{key}.words.json", "w", encoding="utf-8") as f:
            json.dump(words, f, ensure_ascii=False)
        with open(wd / f"{key}.silences.json", "w", encoding="utf-8") as f:
            json.dump(silences, f)

        ranges = build_scenes(meta["duration"], cuts, words, silences)
        new_scenes = []
        for a, b in ranges:
            sid = core.new_id("sc")
            thumbnail(path, a + (b - a) / 2, wd / "thumbs" / f"{sid}.jpg")
            new_scenes.append({
                "id": sid, "src": src["file"], "in": a, "out": b, "orig_in": a, "orig_out": b,
                "keep": True, "speed": 1.0, "volume": 1.0, "mute": False, "zoom": 1.0,
                "captions": None, "remove_silence": None, "text": "", "text_pos": "top",
                "transcript": " ".join(w["w"] for w in words_in(words, a, b)),
                "thumb": f"{sid}.jpg", "notes": "",
            })
        core.job_log(pid, f"{tag}: {len(new_scenes)} scenes")

        def fn(d, src_file=src["file"], meta=meta, new_scenes=new_scenes):
            s = core.source_by_file(d, src_file)
            if s is None:
                return
            s.update(meta)
            s["analyzed"] = True
            s["words"] = len(words)
            # replace this source's scenes, keep position of other sources' scenes
            others = [x for x in d["scenes"] if x["src"] != src_file]
            if len(others) != len(d["scenes"]):
                core.snapshot(pid, d)  # re-analysis replaces edited scenes – make it undoable
            order = [x["file"] for x in d["sources"]]
            merged = others + new_scenes
            merged.sort(key=lambda x: (order.index(x["src"]) if x["src"] in order else 999, x["orig_in"]))
            d["scenes"] = merged
        core.mutate(pid, fn)


def analyze_reference(pid: str, ref_file: str) -> dict:
    path = core.refs_dir(pid) / ref_file
    ext = path.suffix.lower()
    info: dict = {"kind": "other"}
    if ext in core.IMAGE_EXT:
        info["kind"] = "image"
        return info
    if ext in core.AUDIO_EXT:
        info["kind"] = "audio"
        info.update({k: v for k, v in probe(path).items() if k in ("duration",)})
        return info
    if ext not in core.VIDEO_EXT:
        return info
    meta = probe(path)
    info.update({"kind": "video", **meta})
    cuts = detect_cuts(path)
    shots = len(cuts) + 1
    info["shots"] = shots
    info["avg_shot"] = round(meta["duration"] / shots, 2) if meta["duration"] else None
    sheet_dir = core.work_dir(pid) / "refs"
    sheet_dir.mkdir(exist_ok=True)
    sheet = sheet_dir / f"{Path(ref_file).stem}.sheet.jpg"
    n = 12
    rate = n / max(meta["duration"], 1.0)
    run([FFMPEG, "-y", "-v", "error", "-i", path, "-vf",
         f"fps={rate:.5f},scale=320:-2,tile=4x3:padding=4:color=black", "-frames:v", "1", "-q:v", "4", sheet])
    info["sheet"] = sheet.name
    return info


def download_reference(pid: str, url: str) -> str:
    out_tpl = str(core.refs_dir(pid) / "%(title).60s [%(id)s].%(ext)s")
    out = run([YTDLP, "--no-playlist", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
               "--restrict-filenames", "-o", out_tpl, "--no-simulate", "-O", "after_move:filepath", url],
              timeout=60 * 30)
    lines = [l.strip() for l in out.splitlines() if l.strip() and Path(l.strip()).suffix and Path(l.strip()).exists()]
    if not lines:
        raise PipelineError("yt-dlp finished but no file path was reported.")
    return Path(lines[-1]).name


# ----------------------------------------------------------------- rendering

def target_size(data: dict, preview: bool) -> tuple[int, int]:
    fmt = data["settings"].get("format", "original")
    if fmt == "9:16":
        w, h = 1080, 1920
    elif fmt == "1:1":
        w, h = 1080, 1080
    elif fmt == "16:9":
        w, h = 1920, 1080
    else:
        src = next((s for s in data["sources"] if s.get("width")), None)
        w, h = (src["width"], src["height"]) if src else (1920, 1080)
    if preview:
        f = 540 / min(w, h)
        if f < 1:
            w, h = int(w * f), int(h * f)
    return w - w % 2, h - h % 2


def kept_ranges(scene: dict, silences: list[list[float]], settings: dict) -> list[tuple[float, float]]:
    """Ranges (relative to scene.in) to keep after optional silence removal."""
    dur = scene["out"] - scene["in"]
    rs = scene.get("remove_silence")
    if rs is None:
        rs = settings.get("remove_silence", False)
    if not rs:
        return [(0.0, dur)]
    pad, min_sil = 0.12, float(settings.get("silence_min", 0.6))
    cuts = []
    for s, e in silences:
        s2, e2 = max(s, scene["in"]) - scene["in"], min(e, scene["out"]) - scene["in"]
        if e2 - s2 >= min_sil:
            cuts.append((s2 + pad, e2 - pad))
    ranges, cur = [], 0.0
    for s, e in sorted(cuts):
        if s > cur:
            ranges.append((cur, s))
        cur = max(cur, e)
    if cur < dur:
        ranges.append((cur, dur))
    ranges = [(round(a, 3), round(b, 3)) for a, b in ranges if b - a >= 0.08]
    return ranges or [(0.0, dur)]


def estimated_duration(scene: dict, silences: list[list[float]], settings: dict) -> float:
    if not scene.get("keep", True):
        return 0.0
    total = sum(b - a for a, b in kept_ranges(scene, silences, settings))
    return round(total / float(scene.get("speed") or 1.0), 2)


def _ass_color(hexcol: str) -> str:
    h = (hexcol or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _map_time(rel: float, ranges: list[tuple[float, float]], speed: float) -> float | None:
    acc = 0.0
    for a, b in ranges:
        if rel < a:
            return acc / speed  # inside a removed gap -> snap to next kept range
        if rel <= b:
            return (acc + rel - a) / speed
        acc += b - a
    return None


def write_ass(path: Path, scene: dict, words: list[dict], ranges, settings: dict, w: int, h: int,
              seam_y: int | None = None, extra: list[str] | None = None) -> bool:
    """Word-highlight captions (+ optional extra events, e.g. split-layout cards). seam_y = centre captions there."""
    speed = float(scene.get("speed") or 1.0)
    sw = [x for x in words_in(words, scene["in"], scene["out"])] if words else []
    mapped = []
    for x in sw:
        s = _map_time(x["s"] - scene["in"], ranges, speed)
        e = _map_time(min(x["e"], scene["out"]) - scene["in"], ranges, speed)
        if s is None or e is None or e - s < 0.02:
            continue
        txt = hinglish.caption_word(x["w"], settings)  # Hindi script as spoken, or Hinglish; plus user spellings
        txt = txt.replace("{", "(").replace("}", ")").replace("\\", "/").strip()
        if not txt:  # "spell X as -" style removals: drop the word, keep the others' timing
            continue
        if settings.get("caption_case") == "upper":
            txt = txt.upper()
        mapped.append({"w": txt, "s": s, "e": e, "end_punct": x["w"].rstrip().endswith((".", "?", "!", ",", "।"))})
    if not mapped and not extra:
        return False

    max_words = int(settings.get("caption_words", 4) or 4)
    chunks, cur = [], []
    for i, m in enumerate(mapped):
        if cur and (len(cur) >= max_words or m["s"] - cur[-1]["e"] > 0.5):
            chunks.append(cur)
            cur = []
        cur.append(m)
        if m["end_punct"]:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)

    portrait = h > w
    fs = round(min(w, h) * (0.085 if portrait else 0.072))
    pos = settings.get("caption_position", "bottom")
    align = {"bottom": 2, "center": 5, "top": 8}.get(pos, 2)
    mv = round(h * (0.20 if portrait else 0.10)) if pos != "center" else 0
    base = _ass_color(settings.get("caption_color"))
    hi = _ass_color(settings.get("highlight_color"))
    font = settings.get("caption_font") or "Arial"
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{fs},{base},{base},&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,"
        f"{max(2, round(fs * 0.09))},{max(1, round(fs * 0.04))},{align},{round(w * 0.06)},{round(w * 0.06)},{mv},1",
        f"Style: Card,{font},{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lines += extra or []
    at = f"{{\\an5\\pos({w // 2},{seam_y})}}" if seam_y else ""
    events = []
    for ch in chunks:
        for i, m in enumerate(ch):
            start = m["s"]
            end = ch[i + 1]["s"] if i + 1 < len(ch) else m["e"] + 0.15
            parts = []
            for j, other in enumerate(ch):
                parts.append(f"{{\\c{hi}}}{other['w']}{{\\c{base}}}" if j == i else other["w"])
            events.append([start, max(end, start + 0.05), " ".join(parts)])
    # One caption on screen at a time: overlapping events get STACKED by libass (two lines at once – seen on real
    # fast Hindi speech where whisper compresses word times). Clamp every event to end when the next begins.
    events.sort(key=lambda e: e[0])
    for k in range(len(events) - 1):
        events[k][1] = min(events[k][1], events[k + 1][0])
    for start, end, text in events:
        if end - start >= 0.02:
            lines.append(f"Dialogue: 3,{_ass_time(start)},{_ass_time(end)},Cap,,0,0,0,,{at}{text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return True


def _atempo_chain(speed: float) -> list[str]:
    parts = []
    s = speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    if abs(s - 1.0) > 1e-3:
        parts.append(f"atempo={s:.4f}")
    return parts


def _seg_hash(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


class NvencFailed(PipelineError):
    pass


FONTS_DIR = Path(__file__).resolve().parent / "fonts"


def _ensure_fonts(wd: Path) -> None:
    """libass gets fontsdir=fonts (relative: cwd is the work dir, avoids Windows drive-colon escaping)."""
    dst = wd / "fonts"
    dst.mkdir(exist_ok=True)
    for f in FONTS_DIR.glob("*.ttf"):
        t = dst / f.name
        if not t.exists() or t.stat().st_size != f.stat().st_size:
            shutil.copy(f, t)


def split_heights(h: int, settings: dict) -> tuple[int, int]:
    ratio = min(max(float(settings.get("split_ratio", 0.44) or 0.44), 0.25), 0.60)
    top = int(h * ratio) // 2 * 2
    return top, h - top


def green_key(pid: str, src_file: str) -> str | None:
    """Green-screen colour (hex RRGGBB) sampled from the frame border, or None if the background isn't green.
    Cached per source file (work/<file>.key.json); a replaced source (size/mtime change) is re-sampled."""
    src = core.raw_dir(pid) / src_file
    st = src.stat()
    cache = core.work_dir(pid) / f"{src_file}.key.json"
    c = core.read_json(cache, None)
    if isinstance(c, dict) and c.get("sig") == [st.st_size, st.st_mtime]:
        return c.get("key")
    dur = (probe(src).get("duration") or 2.0)
    samples = []
    for t in (dur * 0.2, dur * 0.5, dur * 0.8):
        r = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                            "-vf", "scale=16:28", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True, timeout=60, creationflags=NO_WINDOW)
        b = r.stdout
        if len(b) < 16 * 28 * 3:
            continue
        W, H = 16, 28
        px = lambda x, y: b[(y * W + x) * 3:(y * W + x) * 3 + 3]  # noqa: E731
        # left/right edge columns in the upper 60 % + the top row: background, rarely the person
        samples += [px(x, y) for y in range(int(H * 0.6)) for x in (0, 1, W - 2, W - 1)] + [px(x, 0) for x in range(W)]
    key = None
    if samples:
        med = [sorted(p[i] for p in samples)[len(samples) // 2] for i in range(3)]
        greenish = sum(1 for p in samples if p[1] > p[0] * 1.2 and p[1] > p[2] * 1.2 and p[1] > 70) / len(samples)
        if greenish > 0.6:
            key = "%02X%02X%02X" % tuple(med)
    core._atomic_write(cache, {"sig": [st.st_size, st.st_mtime], "key": key})
    return key


def render_segment(pid: str, data: dict, scene: dict, words, silences, w: int, h: int,
                   preview: bool, nvenc: bool = True) -> Path:
    settings = data["settings"]
    src = core.source_by_file(data, scene["src"])
    fps = int(settings.get("fps", 30) or 30)
    wd = core.work_dir(pid)
    seg_dir = wd / "segs"
    seg_dir.mkdir(exist_ok=True)
    caps = scene.get("captions")
    if caps is None:
        caps = settings.get("captions", True)
    relevant = {k: settings.get(k) for k in ("fit", "caption_font", "caption_color", "highlight_color",
                                              "caption_position", "caption_case", "caption_words",
                                              "caption_script", "caption_fixes",
                                              "silence_min", "logo", "logo_position", "remove_silence",
                                              "layout", "split_ratio", "bg_mode", "bg_color", "panel_color",
                                              "face_y", "grade")}
    src_path = core.raw_dir(pid) / scene["src"]
    st = src_path.stat()
    logo_p = core.BRAND / settings["logo"] if settings.get("logo") else None
    logo_sig = logo_p.stat().st_mtime if logo_p and logo_p.exists() else None
    split = settings.get("layout") == "split" and h >= w
    gkey = green_key(pid, scene["src"]) if split and settings.get("bg_mode", "auto") == "auto" else None
    # cache key covers everything that changes the pixels/audio: scene edits, style, the words in range,
    # the source file itself and the logo file – so re-analysis or a replaced logo never reuses a stale segment
    key = _seg_hash({k: v for k, v in scene.items() if k not in ("notes", "transcript", "thumb")}, relevant, caps,
                    w, h, fps, preview, words_in(words, scene["in"], scene["out"]), st.st_size, st.st_mtime,
                    logo_sig, "nvenc" if nvenc else "x264", gkey, 8)  # bump when caption/card/render logic changes
    out = seg_dir / f"seg_{key}.mp4"
    if out.exists() and out.stat().st_size > 1000:
        return out

    ranges = kept_ranges(scene, silences, settings)
    dur = scene["out"] - scene["in"]
    speed = float(scene.get("speed") or 1.0)
    # Snap the segment to a whole number of frames and force audio to EXACTLY that length,
    # otherwise each join in the concat gets a 10-90 ms audio gap/click (measured in the audit).
    n_frames = max(1, round(sum(b - a for a, b in ranges) / speed * fps))
    out_dur = n_frames / fps
    full = len(ranges) == 1 and ranges[0][0] <= 0.001 and ranges[0][1] >= dur - 0.001
    expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ranges)

    v = [f"fps={fps}"]
    if not full:
        v += [f"select='{expr}'", f"setpts=N/{fps}/TB"]
    if abs(speed - 1.0) > 1e-3:
        v.append(f"setpts=PTS/{speed:.4f}")
    z = float(scene.get("zoom") or 1.0)
    pre = []  # graph parts that must come BEFORE the main [0:v] chain's consumers (split layout)
    top_h = 0
    if split:
        top_h, bot_h = split_heights(h, settings)
        sw0, sh0 = (src or {}).get("width") or 1080, (src or {}).get("height") or 1920
        f = max(w / sw0, bot_h / sh0) * z  # cover the panel, punch-in = extra scale
        S_w, S_h = int(sw0 * f) // 2 * 2, int(sh0 * f) // 2 * 2
        cx = max(0, (S_w - w) // 2)
        cy = int(min(max(0.0, float(settings.get("face_y", 0.27)) * S_h - 0.42 * bot_h), S_h - bot_h)) // 2 * 2
        if settings.get("grade", True):
            v.append("eq=brightness=0.03:contrast=1.04")
        if gkey:
            v += ["format=yuva444p", f"chromakey=0x{gkey}:0.08:0.03", "despill=type=green:mix=0.6:expand=0"]
        v += [f"scale={S_w}:{S_h}", f"crop={w}:{bot_h}:{cx}:{cy}"]
        bg = (settings.get("bg_color") or "#F4F1EC").lstrip("#")
        panel = (settings.get("panel_color") or "#0F1B2D").lstrip("#")
        seam = (settings.get("highlight_color") or "#F97316").lstrip("#")
        t = max(2, round(h * 0.0021)) // 2 * 2
        if gkey:
            pre.append(f"color=c=0x{bg}:s={w}x{bot_h}:r={fps},format=yuv420p,vignette=angle=PI/14[bg]")  # faint: PI/7 turned off-white grey
        pre.append(f"color=c=0x{panel}:s={w}x{top_h}:r={fps},format=yuv420p[top]")
        speaker_tail = (f"[bg][sp]overlay=0:0:shortest=1[bot];" if gkey else "") + \
                       f"[top][{'bot' if gkey else 'sp'}]vstack=inputs=2:shortest=1," \
                       f"drawbox=x=0:y={top_h - t // 2}:w={w}:h={t}:color=0x{seam}:t=fill,setsar=1"
    else:
        if z > 1.001:
            v.append(f"crop=trunc(iw/{z:.3f}/2)*2:trunc(ih/{z:.3f}/2)*2")
        if settings.get("fit") == "pad":
            v.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black")
        else:
            v.append(f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}")
        v.append("setsar=1")

    sid = scene["id"]
    post = []  # filters applied to the composed frame
    _ensure_fonts(wd)
    extra = []
    if split:
        extra = cards.events(scene.get("_card"), w, top_h, out_dur, bool(scene.get("_card_new")),
                             settings.get("highlight_color") or "#F97316", "Poppins", _ass_time)
    if (caps and words) or extra:
        ass = wd / f"cap_{sid}.ass"
        if write_ass(ass, scene, words if caps else [], ranges, settings, w, h,
                     seam_y=top_h if split else None, extra=extra):
            post.append(f"subtitles=cap_{sid}.ass:fontsdir=fonts")
    if (scene.get("text") or "").strip():
        (wd / f"txt_{sid}.txt").write_text(scene["text"].strip(), encoding="utf-8")
        font = wd / "title_font.ttf"
        if not font.exists():
            for cand in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arial.ttf"):
                if Path(cand).exists():
                    shutil.copy(cand, font)
                    break
        y = {"top": "h*0.08", "center": "(h-text_h)/2", "bottom": "h*0.80-text_h"}.get(scene.get("text_pos", "top"), "h*0.08")
        fs = round(min(w, h) * 0.065)
        post.append(f"drawtext=fontfile=title_font.ttf:textfile=txt_{sid}.txt:expansion=none:fontsize={fs}:fontcolor=white:"
                    f"box=1:boxcolor=black@0.55:boxborderw={round(fs * 0.35)}:x=(w-text_w)/2:y={y}")

    inputs = ["-ss", f"{scene['in']:.3f}", "-t", f"{dur:.3f}", "-i", core.raw_dir(pid) / scene["src"]]
    if split:
        graph = pre + [f"[0:v]{','.join(v)}[sp]", f"{speaker_tail}{(',' + ','.join(post)) if post else ''}[v0]"]
    else:
        graph = [f"[0:v]{','.join(v + post)}[v0]"]
    vlabel = "v0"
    logo = settings.get("logo")
    in_idx = 1
    if logo and (core.BRAND / logo).exists():
        inputs += ["-i", core.BRAND / logo]
        lw = round(min(w, h) * 0.16)
        m = round(min(w, h) * 0.04)
        pos = {"top-right": f"W-w-{m}:{m}", "top-left": f"{m}:{m}", "bottom-right": f"W-w-{m}:H-h-{m}",
               "bottom-left": f"{m}:H-h-{m}"}.get(settings.get("logo_position"), f"W-w-{m}:{m}")
        graph.append(f"[{in_idx}:v]scale={lw}:-1[lg]")
        graph.append(f"[{vlabel}][lg]overlay={pos}[v1]")
        vlabel = "v1"
        in_idx += 1
    graph.append(f"[{vlabel}]tpad=stop_mode=clone:stop_duration=1,format=yuv420p[vout]")

    a = []
    if src and src.get("has_audio"):
        if not full:
            a += [f"aselect='{expr}'", "asetpts=N/SR/TB"]
        a += _atempo_chain(speed)
        vol = 0.0 if scene.get("mute") else float(scene.get("volume", 1.0))
        if abs(vol - 1.0) > 1e-3:
            a.append(f"volume={vol:.3f}")
        a += ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo",
              f"apad=whole_dur={out_dur:.6f}", f"atrim=end={out_dur:.6f}"]
        graph.append(f"[0:a]{','.join(a)}[aout]")
    else:
        inputs += ["-f", "lavfi", "-t", f"{out_dur:.6f}", "-i", "anullsrc=r=48000:cl=stereo"]
        graph.append(f"[{in_idx}:a]anull[aout]")

    if nvenc:
        venc = ["-c:v", "h264_nvenc", "-preset", "p1" if preview else "p5", "-cq", "30" if preview else "21", "-b:v", "0"]
    else:
        venc = ["-c:v", "libx264", "-preset", "veryfast" if preview else "medium", "-crf", "28" if preview else "20"]
    cmd = [FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(graph),
           "-map", "[vout]", "-map", "[aout]", *venc, "-r", str(fps), "-g", str(fps * 2),
           "-frames:v", str(n_frames), "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out.name]
    try:
        run(cmd, cwd=wd)  # cwd=wd so subtitle/text/font files are referenced without Windows drive-colon escaping
    except PipelineError as e:
        (wd / out.name).unlink(missing_ok=True)
        if nvenc:
            # don't fall back for THIS segment only: mixing NVENC and x264 streams in one stream-copy concat
            # can glitch at the joins. render_project re-renders every segment with x264 instead.
            raise NvencFailed(str(e)) from e
        raise
    # ffmpeg wrote into wd (cwd) — move into segs/
    shutil.move(str(wd / out.name), out)
    return out


def _prune_segments(wd: Path, kind: str, used: list[Path]) -> None:
    """Keep only segments used by the latest preview and the latest final render (cache otherwise grows forever)."""
    (wd / f"segs_{kind}.txt").write_text("\n".join(p.name for p in used), encoding="utf-8")
    keep = set()
    for k in ("preview", "final"):
        f = wd / f"segs_{k}.txt"
        if f.exists():
            keep.update(x for x in f.read_text(encoding="utf-8").splitlines() if x)
    for p in (wd / "segs").glob("seg_*.mp4"):
        if p.name not in keep:
            try:
                p.unlink()
            except OSError:
                pass


def with_effective_cards(scenes: list[dict], settings: dict) -> list[dict]:
    """Split layout: each scene shows its own card, else the previous one (no re-animation), else the headline.
    Returns copies with _card/_card_new (part of each segment's cache key)."""
    if settings.get("layout") != "split":
        return scenes
    head = (settings.get("headline") or "").strip()
    shown = {"kind": "title", "text": head} if head else None
    out, first = [], True
    for sc in scenes:
        c = sc.get("card")
        eff = c if cards.valid(c) else shown
        new = bool(eff) and (first or eff != shown)
        out.append(dict(sc, _card=eff, _card_new=new))
        shown, first = eff, False
    return out


MUSIC_EXT = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"}


def music_file(pid: str, settings: dict) -> Path | None:
    """settings.music: 'off' | 'auto' (first track in Brand Kit/music, else a generated soft pad) | file name."""
    m = settings.get("music") or "off"
    if m == "off":
        return None
    folder = core.BRAND / "music"
    if m != "auto":
        p = folder / Path(m).name
        if p.is_file():
            return p
    tracks = sorted(p for p in folder.glob("*") if p.suffix.lower() in MUSIC_EXT) if folder.is_dir() else []
    return tracks[0] if tracks else generated_pad()


def generated_pad() -> Path:
    """A 32 s loopable soft ambient chord pad (Am-F-C-G), synthesised once – original, no licence questions."""
    out = core.ENGINES / "studio" / "cache" / "pad_v1.m4a"
    if out.exists() and out.stat().st_size > 10000:
        return out
    out.parent.mkdir(exist_ok=True)
    chords = [(220.0, 261.63, 329.63), (174.61, 220.0, 261.63), (261.63, 329.63, 392.0), (196.0, 246.94, 293.66)]
    tone = lambda f: f"(sin(2*PI*{f}*t)+0.3*sin(4*PI*{f}*t)+0.12*sin(6*PI*{f}*t))"  # noqa: E731
    body = "+".join(f"eq(floor(mod(t,32)/8),{k})*({'+'.join(tone(f) for f in ch)})" for k, ch in enumerate(chords))
    expr = f"0.06*pow(sin(PI*mod(t,8)/8),0.7)*({body})"  # each chord swells in/out: no clicks at changes
    tmp = out.with_name("pad_v1.part.m4a")
    run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"aevalsrc='{expr}|{expr}':s=48000:d=32",  # quoted: commas!
         "-af", "lowpass=f=1400,aecho=0.8:0.6:90|170:0.25|0.15", "-c:a", "aac", "-b:a", "160k", tmp])
    os.replace(tmp, out)
    return out


def music_lufs(path: Path) -> float:
    """Integrated loudness of a music file, cached by path + size + mtime (cache/music_lufs.json)."""
    cache_f = core.ENGINES / "studio" / "cache" / "music_lufs.json"
    cache_f.parent.mkdir(exist_ok=True)
    st = path.stat()
    k = f"{path}|{st.st_size}|{st.st_mtime}"
    c = core.read_json(cache_f, {}) or {}
    if k in c:
        return float(c[k])
    out = run([FFMPEG, "-hide_banner", "-i", path, "-vn", "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
              timeout=600)
    found = re.findall(r"I:\s+(-?[\d.]+) LUFS", out)
    val = float(found[-1]) if found else -20.0
    if val < -70:  # silent file
        val = -20.0
    c[k] = val
    core._atomic_write(cache_f, c)
    return val


def finish_audio(src: Path, dst: Path, dur: float, music: Path | None, polish: bool) -> None:
    """Final mix over the joined video (video stream copied): voice clean-up + -16 LUFS, optional ducked music."""
    voice = "aresample=48000"
    if polish:
        clean = "highpass=f=80,afftdn=nf=-25,acompressor=threshold=0.1:ratio=3:attack=5:release=120:makeup=1.5"
        # two-pass loudnorm: one-pass (dynamic) overshoots on short reels (measured -14.3 instead of -16)
        meas = run([FFMPEG, "-hide_banner", "-i", src, "-vn", "-af",
                    f"{clean},loudnorm=I=-16:TP=-1.5:LRA=7:print_format=json", "-f", "null", "-"],
                   timeout=max(600, int(dur * 2)))
        try:
            m = json.loads(meas[meas.rindex("{"):meas.rindex("}") + 1])
            ln = (f"loudnorm=I=-16:TP=-1.5:LRA=7:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
                  f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:"
                  f"linear=true")
        except (ValueError, KeyError):
            ln = "loudnorm=I=-16:TP=-1.5:LRA=7"
        if "-inf" in ln:  # silent track: nothing to normalise
            ln = "anull"
        voice += f",{clean},{ln},aresample=48000"
    inputs = ["-i", src]
    if music:
        inputs += ["-stream_loop", "-1", "-i", music]
        fade = max(0.0, dur - 1.5)
        gain = -30.0 - music_lufs(music)  # fixed gain to -30 LUFS (loudnorm on a looped stream pumped it up 1.5 LU)
        graph = (f"[0:a]{voice},asplit=2[vo][key];"
                 f"[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                 f"volume={gain:.2f}dB,afade=t=in:d=1,afade=t=out:st={fade:.2f}:d=1.5[m];"
                 f"[m][key]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=450[md];"  # ~10 dB under speech
                 f"[vo][md]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.84:level=false[a]")  # level=true (default) auto-gains up to 0 dB
    else:
        graph = f"[0:a]{voice}[a]"
    run([FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{dur:.3f}", "-movflags", "+faststart", dst],
        timeout=max(600, int(dur * 3)))


def render_project(pid: str, preview: bool) -> Path:
    data = core.load(pid)
    scenes = [s for s in data["scenes"] if s.get("keep", True)]
    if not scenes:
        raise PipelineError("No scenes are kept – nothing to render.")
    missing = sorted({s["src"] for s in scenes if not (core.raw_dir(pid) / s["src"]).is_file()})
    if missing:
        raise PipelineError(f"Source video missing from Raw Footage: {', '.join(missing)}")
    w, h = target_size(data, preview)
    wd = core.work_dir(pid)
    cache: dict[str, tuple[list, list]] = {}
    scenes = with_effective_cards(scenes, data["settings"])

    def render_all(nvenc: bool) -> list[Path]:
        segs = []
        for i, sc in enumerate(scenes, 1):
            if sc["src"] not in cache:
                cache[sc["src"]] = (core.read_json(core.work_file(pid, sc["src"], "words.json"), []) or [],
                                    core.read_json(core.work_file(pid, sc["src"], "silences.json"), []) or [])
            words, sil = cache[sc["src"]]
            core.set_job(pid, progress=f"Rendering scene {i}/{len(scenes)}")
            segs.append(render_segment(pid, data, sc, words, sil, w, h, preview, nvenc=nvenc))
        return segs

    try:
        segs = render_all(nvenc=True)
    except NvencFailed as e:
        core.job_log(pid, f"GPU encoder failed ({str(e).splitlines()[0][:120]}) – re-rendering on the CPU")
        segs = render_all(nvenc=False)

    lst = wd / "concat.txt"
    lst.write_text("".join(f"file 'segs/{p.name}'\n" for p in segs), encoding="utf-8")
    stamp = core.now_iso().replace(":", "").replace("-", "")
    if preview:
        for old in wd.glob("preview_*.mp4"):
            try:
                old.unlink()
            except OSError:  # still open in the browser player – harmless, removed next time
                pass
        out = wd / f"preview_{stamp}.mp4"
    else:
        core.EXPORTS.mkdir(exist_ok=True)
        n = 1 + len([r for r in data.get("renders", []) if r.get("kind") == "final"])
        out = core.unique_path(core.EXPORTS, f"{data['id']}_v{n}.mp4")
    tmp_out = out.with_name(out.stem + ".part.mp4")
    st = data["settings"]
    music = music_file(pid, st)
    polish = bool(st.get("audio_polish", False))
    if music or polish:
        joined = wd / "joined.part.mp4"
        try:
            run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst.name, "-c", "copy",
                 joined.name], cwd=wd)
            core.set_job(pid, progress="Mixing audio" + (" + music" if music else ""))
            finish_audio(joined, tmp_out, probe(joined)["duration"], music, polish)
        finally:
            joined.unlink(missing_ok=True)
    else:
        run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst.name, "-c", "copy",
             "-movflags", "+faststart", tmp_out], cwd=wd)
    os.replace(tmp_out, out)  # never leave a half-written file in Exports
    dur = probe(out)["duration"]
    _prune_segments(wd, "preview" if preview else "final", segs)

    def fn(d):
        d.setdefault("renders", []).append({"kind": "preview" if preview else "final", "file": out.name,
                                            "created": core.now_iso(), "duration": dur, "size": f"{w}x{h}"})
        d["renders"] = d["renders"][-30:]
    core.mutate(pid, fn)
    return out
