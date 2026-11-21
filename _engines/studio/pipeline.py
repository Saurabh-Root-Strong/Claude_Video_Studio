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
import commands
import core
import library
import tracks as tracks_mod
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
               "format=duration,size:format_tags=creation_time:stream=codec_type,codec_name,width,height,"
               "r_frame_rate:stream_disposition=attached_pic:stream_side_data=rotation:stream_tags=rotate",
               "-of", "json", path])
    j = json.loads(out[out.index("{"):])
    fmt = j.get("format", {})
    dur = float(fmt.get("duration") or 0)
    meta = {"duration": round(dur, 3), "width": 0, "height": 0, "fps": 0.0, "has_audio": False, "has_video": False}
    try:
        meta["bytes"] = int(fmt.get("size") or 0)
    except ValueError:
        pass
    ct = (fmt.get("tags") or {}).get("creation_time")
    if ct:
        meta["recorded"] = str(ct)[:19]  # camera/phone recording time, for "sort by recording time"
    for s in j.get("streams", []):
        if (s.get("disposition") or {}).get("attached_pic"):
            continue  # cover art in an audio file is not video
        if s.get("codec_type") == "video" and not meta["has_video"]:
            meta["codec"] = s.get("codec_name", "")
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


SILENCE_V = "silences.v2"  # marker: silences stored down to 0.2 s (before 2026-11-19: 0.4 s)


def load_silences(pid: str, src_file: str) -> list[list[float]]:
    """Pauses for a source. Projects analysed before pauses were stored down to 0.2 s get re-detected once
    (one quick ffmpeg pass), otherwise 'cut pauses > 0.35 s' would silently miss the 0.35-0.4 s ones."""
    p = core.work_file(pid, src_file, "silences.json")
    sil = core.read_json(p, []) or []
    marker = core.work_dir(pid) / f"{src_file}.{SILENCE_V}"
    src = core.raw_dir(pid) / src_file
    if not marker.exists() and src.is_file():
        try:
            sil = detect_silences(src)
            core._atomic_write(core.work_dir(pid) / f"{src_file}.silences.json", sil)
            marker.write_text("min 0.2 s\n", encoding="utf-8")
        except PipelineError:
            pass  # keep the old list – still valid, just coarser
    return sil


def detect_silences(src: Path, noise_db: int = -35, min_dur: float = 0.2) -> list[list[float]]:
    # stored at 0.2 s so "cut pauses > 0.35 s" works; every consumer filters to its own minimum (0.3 / 0.4 s)
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
        real = [(s, e) for s, e in silences if e - s >= 0.3]  # snap to real pauses, not 0.2 s breaths
        before = [e for s, e in real if e <= a + 0.05 and a - e <= 4.0]
        after = [s for s, e in real if s >= b - 0.05 and s - b <= 4.0]
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
    """Re-transcribe each suspect window on its own (whisper behaves on short clips) and splice the result in.
    Capped: at most 12 windows / a third of the file – beyond that the whole transcript is suspect and repairing
    piecemeal would cost more than it fixes (log tells the user to re-analyze with the language set)."""
    wins = suspect_windows(words, silences, duration)
    budget = max(30.0, (duration or 0) / 3)
    picked, used = [], 0.0
    for a, b in wins:  # in time order
        if len(picked) >= 12 or used + (b - a) > budget:
            log(f"{len(wins) - len(picked)} more suspect stretch(es) left as is – if captions look wrong, "
                f"set Language (tab 2) and re-analyze")
            break
        picked.append((a, b))
        used += b - a
    for k, (a, b) in enumerate(picked):
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


# HDR phone footage (iPhone default: HLG / Dolby Vision 10-bit; many Androids: HDR10/PQ) turned grey and washed-out
# when squeezed into normal 8-bit video. Tone-map it to SDR BT.709 first – the look a phone shows when you share.
HDR_TRC = ("arib-std-b67", "smpte2084")
_HDR_CACHE: dict = {}


def is_hdr(path: Path) -> bool:
    try:
        st = Path(path).stat()
    except OSError:
        return False
    key = (str(path), st.st_size, st.st_mtime)
    if key not in _HDR_CACHE:
        try:
            out = run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=color_transfer",
                       "-of", "default=nw=1:nk=1", path]).strip().lower()
        except PipelineError:
            out = ""
        _HDR_CACHE[key] = any(t in out for t in HDR_TRC)
    return _HDR_CACHE[key]


def hdr_chain() -> list[str]:
    return ["zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709", "tonemap=tonemap=mobius:desat=0",
            "zscale=t=bt709:m=bt709:r=tv", "format=yuv420p"]


def thumbnail(src: Path, t: float, out: Path, width: int = 320) -> None:
    pre = ",".join(hdr_chain()) + "," if is_hdr(src) else ""
    run([FFMPEG, "-y", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", src, "-frames:v", "1",
         "-vf", f"{pre}scale={width}:-2", "-q:v", "4", out])


STRIP_N = 24  # frames in a clip's film strip (the timeline shows as many as fit the block)


def clip_assets(pid: str, file: str, meta: dict) -> dict:
    """Timeline graphics for a clip: a film strip (STRIP_N square frames side by side) and an audio waveform.
    Returns {"strip":..., "wave":...} with whatever could be made; failures just leave a field out."""
    src = core.raw_dir(pid) / file
    dur = float(meta.get("duration") or 0)
    out: dict = {}
    if not src.is_file() or dur <= 0.05:
        return out
    thumbs = core.work_dir(pid) / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", file)
    strip = thumbs / f"strip_{safe}.jpg"
    try:  # evenly spaced frames, centre-cropped square so portrait and landscape tile the same way
        run([FFMPEG, "-y", "-v", "error", "-i", src, "-an", "-vf",
             f"fps={STRIP_N / max(dur, 0.1):.5f},scale=80:80:force_original_aspect_ratio=increase,crop=80:80,"
             f"tile={STRIP_N}x1", "-frames:v", "1", "-q:v", "5", strip], timeout=max(120, int(dur)))
        out["strip"] = strip.name
    except (PipelineError, subprocess.TimeoutExpired):
        pass
    if meta.get("has_audio"):
        wave = thumbs / f"wave_{safe}.png"
        try:
            run([FFMPEG, "-y", "-v", "error", "-i", src, "-filter_complex",
                 "aformat=channel_layouts=mono,showwavespic=s=1600x64:colors=0x5eead4:scale=sqrt",
                 "-frames:v", "1", wave], timeout=max(120, int(dur)))
            out["wave"] = wave.name
        except (PipelineError, subprocess.TimeoutExpired):
            pass
    return out


def media_assets(pid: str, path: Path) -> dict:
    """Film strip + waveform for ANY timeline file (upload, Brand Kit music, library sound) – made once per file version
    and cached in work/thumbs (names carry a hash of the path + size + date, so a replaced file gets new ones)."""
    try:
        st = path.stat()
        meta = probe(path)
    except (OSError, PipelineError):
        return {}
    dur = float(meta.get("duration") or 0)
    key = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:14]
    thumbs = core.work_dir(pid) / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    out: dict = {"duration": dur}
    strip, wave = thumbs / f"ma_{key}_strip.jpg", thumbs / f"ma_{key}_wave.png"
    if meta.get("has_video") and not core.is_still(path.name) and dur > 0.05:
        if not strip.is_file():
            try:
                run([FFMPEG, "-y", "-v", "error", "-i", path, "-an", "-vf",
                     f"fps={STRIP_N / max(dur, 0.1):.5f},scale=80:80:force_original_aspect_ratio=increase,crop=80:80,"
                     f"tile={STRIP_N}x1", "-frames:v", "1", "-q:v", "5", strip], timeout=max(120, int(dur)))
            except (PipelineError, subprocess.TimeoutExpired):
                pass
        if strip.is_file():
            out["strip"] = strip.name
    if meta.get("has_audio") and dur > 0.02:
        if not wave.is_file():
            try:
                run([FFMPEG, "-y", "-v", "error", "-i", path, "-filter_complex",
                     "aformat=channel_layouts=mono,showwavespic=s=1600x64:colors=0x5eead4:scale=sqrt",
                     "-frames:v", "1", wave], timeout=max(120, int(dur)))
            except (PipelineError, subprocess.TimeoutExpired):
                pass
        if wave.is_file():
            out["wave"] = wave.name
    return out


def make_proxy(src: Path, out: Path, has_audio: bool) -> None:
    """Light 540p H.264 copy for the browser player (plays HEVC/ProRes/MKV sources, easy on 7 GB RAM)."""
    out.parent.mkdir(exist_ok=True)
    tmp = out.with_name(out.stem + ".part.mp4")
    pre = ",".join(hdr_chain()) + "," if is_hdr(src) else ""
    common = ["-vf", f"{pre}scale=-2:'min(540,ih)'", "-g", "30", "-pix_fmt", "yuv420p"]
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

SPEECH_WPS = 0.5  # real speech is ~2-3 words/s; whisper "hearing" words in music/ambience gives ~0.1-0.4


def is_speech(n_words: int, seconds: float) -> bool:
    """False when a stretch has too few words to be real speech (whisper hallucinating on music/noise).
    A short stretch with a few words still counts (a 1.5 s 'thank you dosto' is speech)."""
    if n_words == 0:
        return False
    if seconds <= 0:
        return True
    return n_words / seconds >= SPEECH_WPS or (n_words >= 3 and seconds < 4.0)


def mark_no_speech(data: dict, words_by_src: dict) -> list[str]:
    """Captions OFF for scenes without real speech (a clip of music / B-roll, or a silent stretch).
    Only scenes whose captions are still 'auto' (None) are touched – a user's explicit captions on/off wins.
    Returns the ids switched off."""
    off = []
    for s in data["scenes"]:
        sw = words_in(words_by_src.get(s["src"]) or [], s["in"], s["out"])
        src = core.source_by_file(data, s["src"]) or {}
        silent = src.get("speech") is False or not is_speech(len(sw), s["out"] - s["in"])
        s["no_speech"] = silent
        if silent and s.get("captions") is None:
            s["captions"] = False
            s["captions_auto_off"] = True
            off.append(s["id"])
    return off


def update_speech_flag(s: dict, words: list[dict], src: dict | None) -> None:
    """Re-check 'is anyone speaking' after a split / trim changed the scene's range (a split often leaves a silent
    tail where whisper invents words). Only AUTOMATIC caption choices follow; an explicit on/off is kept."""
    sw = words_in(words or [], s["in"], s["out"])
    silent = (src or {}).get("speech") is False or not is_speech(len(sw), s["out"] - s["in"])
    s["no_speech"] = silent
    if silent and s.get("captions") is None:
        s["captions"], s["captions_auto_off"] = False, True
    elif not silent and s.get("captions_auto_off"):
        s.pop("captions_auto_off", None)
        s["captions"] = None


def place_scenes(scenes: list[dict], new: list[dict], src_file: str, order: list[str]) -> list[dict]:
    """Put a clip's (re)analyzed scenes into the timeline WITHOUT disturbing the user's arrangement:
    re-analysis replaces the clip's scenes where its first old scene was; a new clip goes after the last scene of
    the clips before it in the clip order (or before the first scene of a later clip). Earlier code re-sorted the
    whole timeline by clip, wiping any manual scene reordering."""
    old_pos = next((i for i, x in enumerate(scenes) if x["src"] == src_file), None)
    rest = [x for x in scenes if x["src"] != src_file]
    if old_pos is not None:
        at = sum(1 for x in scenes[:old_pos] if x["src"] != src_file)
    else:
        rank = {f: i for i, f in enumerate(order)}
        me = rank.get(src_file, len(order))
        earlier = [i for i, x in enumerate(rest) if rank.get(x["src"], len(order)) < me]
        later = [i for i, x in enumerate(rest) if rank.get(x["src"], len(order)) > me]
        # right after the last scene of an earlier clip; else before the first scene of a later clip; else at end
        at = earlier[-1] + 1 if earlier else (later[0] if later else len(rest))
    return rest[:at] + new + rest[at:]


def regroup_scenes(scenes: list[dict], order: list[str]) -> list[dict]:
    """Scenes grouped by clip in `order`, keeping each clip's own scene order."""
    rank = {f: i for i, f in enumerate(order)}
    return sorted(scenes, key=lambda x: rank.get(x["src"], len(order)))  # stable sort keeps in-clip order


def scenes_interleaved(scenes: list[dict]) -> bool:
    """True if the user arranged scenes across clips (a clip's scenes are not one contiguous block)."""
    seen, last = set(), None
    for x in scenes:
        if x["src"] != last:
            if x["src"] in seen:
                return True
            seen.add(x["src"])
            last = x["src"]
    return False


def analyze_project(pid: str, reanalyze: bool = False) -> int:
    data = core.load(pid)
    lang = data["settings"].get("language", "auto")
    wd = core.work_dir(pid)
    (wd / "thumbs").mkdir(exist_ok=True)
    todo = [s for s in data["sources"] if reanalyze or not s.get("analyzed")]
    if not todo:
        core.job_log(pid, "Nothing new to analyze.")
        return 0

    failed = 0
    for idx, src in enumerate(todo, 1):
        tag = f"[{idx}/{len(todo)}] {src['file']}"
        try:
            _analyze_one(pid, src, tag, lang, wd)
        except Exception as e:  # one bad file must not stop the others
            failed += 1
            core.job_log(pid, f"{tag}: FAILED – {e}")
            msg = str(e).splitlines()[0][:160] if str(e) else type(e).__name__
            if "ffprobe" in msg or "Invalid data" in str(e):
                msg = "can't read this file – it is damaged, still being copied, or not really a video"
            core.mutate(pid, lambda d, f=src["file"], m=msg: (core.source_by_file(d, f) or {}).update(error=m))
    if failed and failed == len(todo):
        raise PipelineError("Every file failed to analyze – see the activity log.")
    return len(todo) - failed  # clips analysed OK (the worker renders a preview when > 0)


def _analyze_one(pid: str, src: dict, tag: str, lang: str, wd: Path) -> None:
        path = core.raw_dir(pid) / src["file"]
        key = src["file"]  # FULL name: clip.mp4 and clip.mov must not share work files
        if not path.is_file():
            raise PipelineError("the file is missing from the Raw Footage folder")
        core.job_log(pid, f"{tag}: probing")
        meta = probe(path)
        if not meta["has_video"]:
            raise PipelineError("this file has sound only, no picture – for background music put it in the Brand Kit/music folder")
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
            urdu = sum(1 for w in words if re.search(r"[؀-ۿ]", w["w"]))
            if words and urdu > len(words) * 0.3:
                # whisper's language auto-detect often writes Hindi speech in Urdu (Arabic) script – Hinglish and
                # Hindi captions can't fix that; re-transcribing with language = Hindi does
                core.job_log(pid, f"{tag}: ⚠ transcript is in Urdu script ({urdu}/{len(words)} words). If the "
                                  f"speech is Hindi, set Language = Hindi (tab 2) and click Re-analyze.")
        core.set_job(pid, progress=f"{tag}: detecting scenes")
        cuts = detect_cuts(path)
        core.job_log(pid, f"{tag}: {len(cuts)} visual cuts")
        with open(wd / f"{key}.words.json", "w", encoding="utf-8") as f:
            json.dump(words, f, ensure_ascii=False)
        with open(wd / f"{key}.silences.json", "w", encoding="utf-8") as f:
            json.dump(silences, f)
        (wd / f"{key}.{SILENCE_V}").write_text("min 0.2 s\n", encoding="utf-8")

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
        clip_speech = is_speech(len(words), meta["duration"])
        if not clip_speech:
            core.job_log(pid, f"{tag}: no speech detected ({len(words)} words in {meta['duration']:.1f}s – music or "
                              f"ambient sound) → captions off for this clip (type 'captions on' in a scene to undo)")

        def fn(d, src_file=src["file"], meta=meta, new_scenes=new_scenes, clip_speech=clip_speech):
            s = core.source_by_file(d, src_file)
            if s is None:
                return
            s.update(meta)
            s["analyzed"] = True
            s["words"] = len(words)
            s["speech"] = clip_speech
            # captions off where nobody speaks (the new scenes are all still on "auto")
            mark_no_speech({"scenes": new_scenes, "sources": d["sources"]}, {src_file: words})
            # a clip with nobody speaking stays a NORMAL scene (plays in the video, captions off). B-roll cutaways are
            # opt-in only (role broll / drag onto a scene) – the user wants every clip simply on the timeline.
            # replace this source's scenes, keep position of other sources' scenes
            if any(x["src"] == src_file for x in d["scenes"]):
                core.snapshot(pid, d)  # re-analysis replaces edited scenes – make it undoable
            d["scenes"] = place_scenes(d["scenes"], new_scenes, src_file, [x["file"] for x in d["sources"]])
            # B-roll clips the user chose (role broll) that nobody uses yet – e.g. after their speaker scenes were
            # re-analyzed – get placed again
            placed.extend(commands.auto_broll(d))
            s.pop("error", None)
        placed: list[str] = []
        core.mutate(pid, fn)
        for msg in placed:  # job_log takes the project lock itself – never call it inside mutate()
            core.job_log(pid, f"{tag}: {msg}")


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
    try:
        out = run([YTDLP, "--no-playlist", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
                   "--restrict-filenames", "-o", out_tpl, "--no-simulate", "-O", "after_move:filepath", url],
                  timeout=60 * 30)
    except PipelineError as e:  # say WHY in plain words – the raw yt-dlp text means nothing to a creator
        t = str(e).lower()
        why = ("no internet, or the website can't be reached" if any(k in t for k in ("getaddrinfo", "failed to resolve", "timed out", "connection"))
               else "the video is private or needs a login" if any(k in t for k in ("private", "sign in", "login", "log in", "cookies", "members"))
               else "this website isn't supported" if "unsupported url" in t
               else "the video was removed or the link is wrong" if any(k in t for k in ("404", "not found", "unavailable", "removed"))
               else "the download failed")
        raise PipelineError(f"Could not download the reference: {why}. Tip: download the video yourself and drag the "
                            f"file into References.") from e
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
    """Ranges (relative to scene.in) to keep after optional silence removal and the scene's word cuts
    (scene["cuts"] = [[src_start, src_end], ...] in SOURCE seconds: filler words, false starts)."""
    dur = scene["out"] - scene["in"]
    rs = scene.get("remove_silence")
    if rs is None:
        rs = settings.get("remove_silence", False)
    cuts = []
    for c in scene.get("cuts") or []:
        try:
            s2, e2 = max(float(c[0]), scene["in"]) - scene["in"], min(float(c[1]), scene["out"]) - scene["in"]
        except (TypeError, ValueError, IndexError):
            continue
        if e2 - s2 >= 0.04:
            cuts.append((s2, e2))
    if not rs and not cuts:
        return [(0.0, dur)]
    pad, min_sil = 0.12, float(settings.get("silence_min", 0.6))
    for s, e in silences if rs else []:
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
    if not ranges and scene.get("cuts"):
        return []  # word cuts removed everything: the scene is effectively cut (render skips it)
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


def zoom_windows(scene: dict, ranges, speed: float, out_dur: float, fps: int) -> list[tuple[float, float, float]]:
    """scene["punches"] = [[src_start, src_end, zoom], ...] (source seconds) -> windows on the segment's OUTPUT
    timeline (after cuts + speed), sorted, non-overlapping, at least one frame long."""
    out = []
    for p in scene.get("punches") or []:
        try:
            s, e, z = float(p[0]), float(p[1]), float(p[2])
        except (TypeError, ValueError, IndexError):
            continue
        s, e = max(s, scene["in"]), min(e, scene["out"])
        if e - s < 0.05 or z <= 1.001:
            continue
        a = _map_time(s - scene["in"], ranges, speed)
        b = _map_time(e - scene["in"], ranges, speed)
        a = out_dur if a is None else a
        b = out_dur if b is None else b
        if b - a >= 1.0 / fps:
            out.append((a, b, min(z, 3.0)))
    out.sort()
    clean, t = [], 0.0
    for a, b, z in out:
        a = max(a, t)
        if b - a >= 1.0 / fps:
            clean.append((a, b, z))
            t = b
    return clean


def zoom_pieces(windows, geo, base_zoom: float) -> str:
    """Filter text: split the stream into time pieces, each with its own zoom geometry, joined again – a snap
    punch-in INSIDE the scene (the scene stays one scene on the timeline)."""
    segs, t = [], 0.0
    for a, b, z in windows:
        if a > t + 1e-3:
            segs.append((t, a, base_zoom))
        segs.append((a, b, z))
        t = b
    segs.append((t, None, base_zoom))
    n = len(segs)
    txt = f"split={n}" + "".join(f"[zq{i}]" for i in range(n))
    for i, (a, b, z) in enumerate(segs):
        tr = f"trim=start={a:.4f}" + (f":end={b:.4f}" if b is not None else "")
        txt += f";[zq{i}]{tr},setpts=PTS-STARTPTS,{','.join(geo(z))}[zr{i}]"
    return txt + ";" + "".join(f"[zr{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0"


def caption_center_y(scene: dict, w: int, h: int, seam_y: int | None, settings: dict | None = None) -> int | None:
    """Pinned caption centre row (\\an5), or None = the look's style alignment (bottom/center/top margins)."""
    cp = scene.get("cap_pos")  # per-scene override: head | center | below | a number = % from the top (None = video)
    if cp is None and settings and isinstance(settings.get("caption_y"), (int, float)):
        cp = settings["caption_y"]  # the whole video's exact height
    if isinstance(cp, (int, float)) and not isinstance(cp, bool):
        return round(h * min(max(float(cp), 3.0), 97.0) / 100)
    if cp in ("head", "center", "below"):
        if seam_y:  # split layout: Auto = ON the seam; head = just under it (over the hair/forehead), middle, low
            lo = h - seam_y
            return {"head": seam_y + round(lo * 0.16), "center": seam_y + lo // 2, "below": round(h * 0.78)}[cp]  # 78 %: above the Reels/Shorts UI
        m = round(h * (0.22 if h > w else 0.14))
        return {"head": m, "center": h // 2, "below": h - m}[cp]
    return seam_y or None


def caption_band(scene: dict, settings: dict, w: int, h: int, seam_y: int | None) -> tuple[int, int]:
    """Rows the captions can cover – room for TWO lines (long Hindi words wrap). Mirrors write_ass placement."""
    portrait = h > w
    fs = round(min(w, h) * (0.085 if portrait else 0.072))
    line = round(fs * 1.25)
    y = caption_center_y(scene, w, h, seam_y, settings)
    if y is not None:
        return y - line, y + line                 # \an5: lines grow both ways from the centre
    pos = settings.get("caption_position", "bottom")
    mv = round(h * (0.20 if portrait else 0.10))
    if pos == "center":
        return h // 2 - line, h // 2 + line
    if pos == "top":
        return mv, mv + 2 * line                  # \an8: grows downwards from the top margin
    return h - mv - 2 * line, h - mv              # \an2: grows upwards from the bottom margin


def text_layout(text: str, pos: str, w: int, h: int, band: tuple[int, int] | None,
                top_limit: int) -> tuple[list[str], int, int, bool]:
    """On-screen text: wrap to the frame width (drawtext never wraps – long text ran off both edges), shrink if it
    needs more than 3 lines, and keep it OFF the captions: a collision moves the text to the free side (nearest
    first) inside [top_limit, 80 % of the height]. Returns (lines, font size, top y, moved)."""
    import textwrap
    fs, floor = round(min(w, h) * 0.065), round(min(w, h) * 0.038)
    while True:
        per = max(6, int(w * 0.86 / (fs * 0.56)))           # ~0.56 em per bold Latin character
        lines = textwrap.wrap(text, per, break_long_words=True) or [text]
        if len(lines) <= 3 or fs <= floor:                   # very long text: more lines rather than unreadable
            break
        fs = max(floor, round(fs * 0.88))
    lh = round(fs * 1.55)                                    # line pitch incl. the box border
    th = lh * len(lines)
    y = {"top": round(h * 0.08), "center": (h - th) // 2, "bottom": round(h * 0.80) - th}.get(pos, round(h * 0.08))
    moved = False
    gap = round(fs * 0.4)
    if band and y - gap < band[1] and y + th + gap > band[0]:
        hi = round(h * 0.80) - th
        cands = sorted([band[0] - gap - th, band[1] + gap], key=lambda c: abs(c - y))
        ok = [c for c in cands if top_limit <= c <= hi]
        y = ok[0] if ok else min(max(cands[0], top_limit), hi)
        moved = True
    return lines, fs, y, moved


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
    y = caption_center_y(scene, w, h, seam_y, settings)
    at = f"{{\\an5\\pos({w // 2},{y})}}" if y is not None else ""
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


_BG_LEN: dict = {}


def green_background(settings: dict) -> tuple | None:
    """What replaces a green screen: None (keep as filmed) | ("color",) | ("picture"|"video", path, sig, length).
    A picture / video that went missing falls back to the clean wall colour (the render never fails over it)."""
    mode = settings.get("bg_mode", "auto")
    if mode == "off":
        return None
    if mode in ("picture", "video"):
        p = library.path_of(settings.get("bg_file") or "")
        if p:
            st_ = p.stat()
            if core.is_still(p.name):
                return ("picture", p, (p.name, st_.st_size, st_.st_mtime), 0.0)
            k = (str(p), st_.st_size, st_.st_mtime)
            if k not in _BG_LEN:  # asked once per scene per render: probe the file once
                try:
                    _BG_LEN[k] = float(probe(p).get("duration") or 0.0)
                except Exception:  # noqa: BLE001
                    _BG_LEN[k] = 0.0
            return ("video", p, (p.name, st_.st_size, st_.st_mtime), _BG_LEN[k])
    return ("color",)


def green_key(pid: str, src_file: str) -> str | None:
    """Green-screen colour (hex RRGGBB) sampled from the frame border, or None if the background isn't green.
    Cached per source file (work/<file>.key.json); a replaced source (size/mtime change) is re-sampled."""
    src = core.raw_dir(pid) / src_file
    st = src.stat()
    cache = core.work_dir(pid) / f"{src_file}.key.json"
    c = core.read_json(cache, None)
    hdr = is_hdr(src)  # sample the SAME (tone-mapped) picture the chroma key will see
    sig = [st.st_size, st.st_mtime] + (["hdr"] if hdr else [])
    if isinstance(c, dict) and c.get("sig") == sig:
        return c.get("key")
    dur = (probe(src).get("duration") or 2.0)
    samples = []
    for t in (dur * 0.2, dur * 0.5, dur * 0.8):
        r = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                            "-vf", (",".join(hdr_chain()) + "," if hdr else "") + "scale=16:28", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
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
    core._atomic_write(cache, {"sig": sig, "key": key})
    return key


def _ensure_title_font(wd: Path) -> bool:
    font = wd / "title_font.ttf"
    if not font.exists():
        for cand in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arial.ttf"):
            if Path(cand).exists():
                shutil.copy(cand, font)
                break
    return font.exists()


def _track_overlays(graph: list, inputs: list, in_idx: int, vlabel: str, ov: list[dict], w: int, h: int, fps: int,
                    top_h: int, settings: dict, wd: Path, sid: str, band, dim_card: bool = False) -> tuple[str, int]:
    """Timeline-track pictures and titles on this scene (slices from tracks.plan), lowest layer first."""
    acc = (settings.get("highlight_color") or "#F97316").lstrip("#")
    for k, o in enumerate(sorted(ov, key=lambda x: x.get("layer", 0))):
        a, b = float(o["a"]), float(o["b"])
        if o["kind"] == "text":
            if not _ensure_title_font(wd):
                continue
            pos = {"title": "center", "lower": "bottom", "top": "top"}.get(o.get("style"), "center")
            top_limit = top_h + round(h * 0.02) if top_h and pos != "top" else round(h * 0.06)
            lines, fs, ty, _m = text_layout(str(o.get("text", "")).replace("*", ""), pos, w, h, band, top_limit)
            box = "black@0.6" if o.get("style") == "title" else f"0x{acc}@0.92"
            parts = []
            for i, ln in enumerate(lines):
                (wd / f"trk_{sid}_{k}_{i}.txt").write_text(ln, encoding="utf-8")
                parts.append(f"drawtext=fontfile=title_font.ttf:textfile=trk_{sid}_{k}_{i}.txt:expansion=none:fontsize={fs}:"
                             f"fontcolor=white:box=1:boxcolor={box}:boxborderw={round(fs * 0.35)}:x=(w-text_w)/2:"
                             f"y={ty + i * round(fs * 1.55) + round(fs * 0.35)}:enable='between(t,{a:.3f},{b:.3f})'")
            if parts:
                graph.append(f"[{vlabel}]{','.join(parts)}[tov{k}]")
                vlabel = f"tov{k}"
            continue
        p = Path(o["file"])
        if not p.is_file():
            continue
        where = o.get("where") if o.get("where") in tracks_mod.WHERE else "full"
        if where == "full":
            x, y, bw, bh = 0, 0, w, h
        elif where == "top":
            bh = top_h or h // 2 // 2 * 2
            x, y, bw = 0, 0, w
        elif where == "bottom":
            bh = (h - top_h) if top_h else h // 2 // 2 * 2
            x, y, bw = 0, h - bh, w
        else:  # pip: a framed box top-right (clear of the captions)
            bw, bh = round(w * 0.44) // 2 * 2, round(h * 0.28) // 2 * 2
            x, y = w - bw - round(w * 0.04) - 6, round(h * 0.09)
        if o.get("still"):
            inputs += ["-loop", "1", "-framerate", str(fps), "-i", p]
        else:
            inputs += ["-ss", f"{float(o.get('src_in') or 0):.3f}", "-t", f"{b - a + 0.5:.3f}", "-i", p]
        br = {"fit": o.get("fit"), "hdr": False if o.get("still") else is_hdr(p)}
        extra = [f"pad={bw + 6}:{bh + 6}:3:3:white"] if where == "pip" else []
        if dim_card and where == "top" and top_h:  # under the card's white text: blend with the panel colour (like scene B-roll)
            pc = (settings.get("panel_color") or "#0F1B2D").lstrip("#")
            pr, pg, pb = (int(pc[i:i + 2], 16) if len(pc) == 6 else 30 for i in (0, 2, 4))
            extra = [f"lutrgb=r='val*0.45+{pr * 0.55:.1f}':g='val*0.45+{pg * 0.55:.1f}':b='val*0.45+{pb * 0.55:.1f}'",
                     "format=yuv420p"]
        graph.append(_broll_graph(f"{in_idx}:v", f"ovs{k}", br, fps, bw, bh, extra))
        graph.append(f"[ovs{k}]setpts=PTS-STARTPTS+{a:.4f}/TB[ovd{k}]")
        graph.append(f"[{vlabel}][ovd{k}]overlay=x={x}:y={y}:eof_action=pass:enable='between(t,{a:.3f},{b:.3f})'[tov{k}]")
        vlabel = f"tov{k}"
        in_idx += 1
    return vlabel, in_idx


def scene_out_len(scene: dict, silences, settings: dict) -> float:
    """The exact length render_segment will give this scene (whole frames) – known BEFORE rendering."""
    fps = int(settings.get("fps", 30) or 30)
    ranges = kept_ranges(scene, silences, settings)
    speed = float(scene.get("speed") or 1.0)
    return max(1, round(sum(b - a for a, b in ranges) / speed * fps)) / fps


def _video_len(p: Path) -> float:
    """Length of a file's video stream (frames / rate); 0.0 if unknown."""
    try:
        o = run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                 "stream=nb_read_packets,r_frame_rate,duration", "-of", "json", p])
        s = (json.loads(o[o.index("{"):]).get("streams") or [{}])[0]
        num, _, den = (s.get("r_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den or 1) if float(num or 0) else 0.0
        if s.get("nb_read_packets") and fps:
            return round(int(s["nb_read_packets"]) / fps, 4)
        return float(s.get("duration") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _sticker_sigs(scene: dict) -> list:
    out = []
    for s in scene.get("stickers") or []:
        p = library.path_of(s.get("file")) if isinstance(s, dict) else None
        out.append([s.get("file"), p.stat().st_mtime if p else None] if isinstance(s, dict) else None)
    return out


def _sfx_events(scenes: list[dict], durs: list[float]) -> tuple[list[tuple], list]:
    """Transitions INTO scenes and every sound effect, on the edited video's timeline.
    Returns (transitions [(index, xfade type, seconds)], sounds [(path, start s, trim s, gain dB)]).
    A sound is placed so its SYNC point (the hit / the middle of a whoosh) meets its moment: start = moment - sync."""
    trans, snd = [], []
    t0 = 0.0
    for i, (sc, d) in enumerate(zip(scenes, durs)):
        tr = sc.get("transition") if isinstance(sc.get("transition"), dict) else None
        if i > 0 and tr and tr.get("type") in library.TRANSITIONS:
            td = min(max(0.15, float(tr.get("dur") or library.DEFAULT_TRANS_DUR)), 1.0, durs[i - 1] * 0.5, d * 0.5)
            if td >= 0.1:
                trans.append((i, tr["type"], round(td, 3)))
                s = tr.get("sound")
                if isinstance(s, dict):
                    snd.append((s.get("file"), t0 + td / 2 - float(s.get("sync") or 0), float(s.get("gain_db") or 0), 1.0))
        for e in sc.get("sfx") or []:
            if isinstance(e, dict):
                at = max(0.0, float(e.get("at") or 0))
                if at >= d:
                    continue  # the scene got shorter than where this sound was placed
                snd.append((e.get("file"), t0 + at - float(e.get("sync") or 0), float(e.get("gain_db") or 0),
                            min(2.0, max(0.0, float(e.get("vol", 1.0) if e.get("vol") is not None else 1.0)))))
        t0 += d
    out = []
    for f, start, gain, vol in snd:
        p = library.path_of(f)
        if p and vol > 0.001 and start < t0 and library.media_ok(p):
            trim = max(0.0, -start)
            out.append((p, max(0.0, start), trim, gain + 20 * math.log10(vol)))
    return trans, out[:60]


def assemble(wd: Path, scenes: list[dict], segs: list[Path], durs: list[float], out: Path, fps: int,
             preview: bool, extra_sounds: list[dict] | None = None) -> bool:
    """Join the scene segments WITH transitions and sound effects. False = nothing to add (caller stream-copies).
    Transitions don't change any length: the outgoing scene's last frame is held for the transition (the voice of the
    next scene starts exactly at the cut), so captions, music and the page's scene times all stay in place."""
    trans, sounds = _sfx_events(scenes, durs)
    sounds = [{"path": p, "start": st_, "trim": tr_, "gain": g_, "dur": None, "fi": 0.0, "fo": 0.0}
              for p, st_, tr_, g_ in sounds]
    for x in extra_sounds or []:
        if x["vol"] > 0.001:
            sounds.append({"path": x["path"], "start": x["start"], "trim": x["in"], "gain": 20 * math.log10(x["vol"]),
                           "dur": x["dur"], "fi": min(x["fade_in"], x["dur"] / 2), "fo": min(x["fade_out"], x["dur"] / 2)})
    sounds = sounds[:80]
    if not trans and not sounds:
        return False
    tmap = {i: (ty, d) for i, ty, d in trans}
    lst = wd / "concat.txt"
    if trans:
        inputs = []
        for p in segs:
            inputs += ["-i", p.name if p.parent == wd else str(p)]
        g = [f"[{i}:v]fps={fps},setpts=PTS-STARTPTS,format=yuv420p,setsar=1,settb=AVTB[v{i}]" for i in range(len(segs))]
        acc, acc_len = "v0", durs[0]
        for i in range(1, len(segs)):
            if i in tmap:
                ty, d = tmap[i]
                g.append(f"[{acc}]tpad=stop_mode=clone:stop_duration={d:.3f}[p{i}];"
                         f"[p{i}][v{i}]xfade=transition={ty}:duration={d:.3f}:offset={acc_len:.4f},settb=AVTB[x{i}]")
            else:
                g.append(f"[{acc}][v{i}]concat=n=2:v=1:a=0,settb=AVTB[x{i}]")
            acc, acc_len = f"x{i}", acc_len + durs[i]
        g.append(f"[{acc}]settb=AVTB,format=yuv420p[vout]")
        g.append("".join(f"[{i}:a]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=0:a=1[ac]")
        a_in, base = len(segs), "[ac]"
    else:  # sounds only: the joined picture is stream-copied
        inputs = ["-f", "concat", "-safe", "0", "-i", lst.name]
        g, a_in, base = [], 1, "[0:a]"
    for k, sd in enumerate(sounds):
        inputs += ["-i", str(sd["path"])]
        ms = round(sd["start"] * 1000)
        cut = f"atrim=start={sd['trim']:.3f}" + (f":duration={sd['dur']:.3f}" if sd["dur"] else "")
        fades = (f",afade=t=in:d={sd['fi']:.3f}" if sd["fi"] > 0 else "") + \
                (f",afade=t=out:st={max(0.0, sd['dur'] - sd['fo']):.3f}:d={sd['fo']:.3f}" if sd["fo"] > 0 and sd["dur"] else "")
        g.append(f"[{a_in + k}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                 f"{cut},asetpts=PTS-STARTPTS{fades},volume={sd['gain']:.2f}dB,adelay={ms}:all=1[s{k}]")
    if sounds:
        g.append(f"{base}{''.join(f'[s{k}]' for k in range(len(sounds)))}amix=inputs={1 + len(sounds)}:duration=first:"
                 f"normalize=0:dropout_transition=0,alimiter=limit=0.97:attack=2:release=50:latency=1[aout]")
    else:
        g.append(f"{base}anull[aout]")
    total = sum(durs)
    vmap = ["-map", "[vout]"] if trans else ["-map", "0:v", "-c:v", "copy"]

    def enc(nvenc: bool) -> list[str]:
        if not trans:
            return []
        if nvenc:
            return ["-c:v", "h264_nvenc", "-preset", "p1" if preview else "p5", "-cq", "30" if preview else "21", "-b:v", "0"]
        return ["-c:v", "libx264", "-preset", "veryfast" if preview else "medium", "-crf", "28" if preview else "20"]
    base_cmd = [FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(g), *vmap, "-map", "[aout]"]
    tail = ["-r", str(fps), "-g", str(fps * 2), "-c:a", "aac", "-b:a", "256k", "-t", f"{total:.3f}",
            "-movflags", "+faststart", out.name]
    try:
        run(base_cmd + enc(True) + tail, cwd=wd, timeout=max(600, int(total * 10)))
    except PipelineError:
        if not trans:
            raise
        run(base_cmd + enc(False) + tail, cwd=wd, timeout=max(900, int(total * 20)))
    return True


def _broll_graph(inp: str, out: str, br: dict, fps: int, bw: int, bh: int, extra: list[str] | None = None) -> str:
    """Filtergraph text: B-roll input -> [out], a bw x bh picture. fill = cover + centre crop (like a phone);
    fit = the WHOLE picture, the empty space filled with a soft blurred, darkened copy of it (no black bars)."""
    extra = extra or []
    if br.get("fit") != "fit":
        return f"[{inp}]{','.join(_broll_chain(fps, bw, bh, br.get('hdr')) + extra)}[{out}]"
    head = (hdr_chain() if br.get("hdr") else []) + [f"fps={fps}", "setpts=PTS-STARTPTS", "format=yuv420p"]
    sw, sh = max(16, bw // 8 // 2 * 2), max(16, bh // 8 // 2 * 2)  # blur at 1/8 size: fast and very soft
    return (f"[{inp}]{','.join(head)},split=2[{out}_b][{out}_f];"
            f"[{out}_b]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},boxblur=6:2,"
            f"scale={bw}:{bh},eq=brightness=-0.12:saturation=0.8,setsar=1[{out}_bg];"
            f"[{out}_f]scale={bw}:{bh}:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1[{out}_fg];"
            f"[{out}_bg][{out}_fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p{(',' + ','.join(extra)) if extra else ''}[{out}]")


def _broll_chain(fps: int, bw: int, bh: int, hdr: bool = False) -> list[str]:
    """B-roll footage -> a bw x bh picture (cover + centre crop, like a phone fills the screen)."""
    return (hdr_chain() if hdr else []) + [f"fps={fps}", "setpts=PTS-STARTPTS", f"scale={bw}:{bh}:force_original_aspect_ratio=increase",
            f"crop={bw}:{bh}", "setsar=1", "format=yuv420p"]


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
                                              "caption_position", "caption_y", "caption_case", "caption_words",
                                              "caption_script", "caption_fixes",
                                              "silence_min", "logo", "logo_position", "remove_silence",
                                              "layout", "split_ratio", "bg_mode", "bg_color", "bg_file", "panel_color",
                                              "face_y", "grade", "main_mute")}
    src_path = core.raw_dir(pid) / scene["src"]
    st = src_path.stat()
    logo_p = core.BRAND / settings["logo"] if settings.get("logo") else None
    logo_sig = logo_p.stat().st_mtime if logo_p and logo_p.exists() else None
    split = settings.get("layout") == "split" and h >= w
    bg_src_ = green_background(settings)  # None = keep the green as filmed
    gkey = green_key(pid, scene["src"]) if bg_src_ else None
    bg_t0 = float(scene.get("_bg_t0") or 0.0) if bg_src_ and bg_src_[0] == "video" else 0.0
    # cache key covers everything that changes the pixels/audio: scene edits, style, the words in range,
    # the source file itself and the logo file – so re-analysis or a replaced logo never reuses a stale segment
    key = _seg_hash({k: v for k, v in scene.items() if k not in ("notes", "transcript", "thumb", "sb_applied", "transition", "sfx")},
                    relevant, caps,
                    w, h, fps, preview, words_in(words, scene["in"], scene["out"]), st.st_size, st.st_mtime,
                    logo_sig, "nvenc" if nvenc else "x264", gkey, _sticker_sigs(scene),
                    (bg_src_[2], round(bg_t0, 3)) if gkey and bg_src_ and bg_src_[0] != "color" else None,
                    20)  # bump when caption/card/render logic changes
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

    v = (hdr_chain() if is_hdr(src_path) else []) + [f"fps={fps}"]
    if not full:
        v += [f"select='{expr}'", f"setpts=N/{fps}/TB"]
    if abs(speed - 1.0) > 1e-3:
        v.append(f"setpts=PTS/{speed:.4f}")
    z = float(scene.get("zoom") or 1.0)
    zw = zoom_windows(scene, ranges, speed, out_dur, fps)  # zoom moments (punch-ins) inside this scene
    pre = []  # graph parts that must come BEFORE the main [0:v] chain's consumers (split layout)
    top_h = 0
    br = scene.get("_broll") if isinstance(scene.get("_broll"), dict) and Path(scene["_broll"].get("file", "")).is_file() else None
    br_where = (br["where"] if br["where"] in core.BROLL_WHERE else "full") if br else None
    half = not split and br_where in ("top", "bottom")  # full layout: B-roll in one half, you in the other
    bg_in = (2 if br else 1) if gkey and bg_src_ and bg_src_[0] != "color" else None

    def bg_graph(label: str, W: int, H: int) -> str:
        """What shows where the green was: the clean wall colour, or the chosen picture / video covering the area."""
        if bg_in is None:
            c = (settings.get("bg_color") or "#F4F1EC").lstrip("#")
            return f"color=c=0x{c}:s={W}x{H}:r={fps},format=yuv420p,vignette=angle=PI/14[{label}]"  # faint: PI/7 turned off-white grey
        return (f"[{bg_in}:v]fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
                f"format=yuv420p[{label}]")
    card = None if br_where == "full" else scene.get("_card")  # a full-screen cutaway covers the top-panel card
    card_on = split and cards.valid(card) and card.get("kind") != "none"
    if split:
        top_h, bot_h = split_heights(h, settings)
        sw0, sh0 = (src or {}).get("width") or 1080, (src or {}).get("height") or 1920
        def geo(zz, sw0=sw0, sh0=sh0, bot_h=bot_h):  # cover the panel; a punch-in is extra scale on the same face point
            f = max(w / sw0, bot_h / sh0) * zz
            S_w, S_h = int(sw0 * f) // 2 * 2, int(sh0 * f) // 2 * 2
            cx = max(0, (S_w - w) // 2)
            cy = int(min(max(0.0, float(settings.get("face_y", 0.27)) * S_h - 0.42 * bot_h), S_h - bot_h)) // 2 * 2
            return [f"scale={S_w}:{S_h}", f"crop={w}:{bot_h}:{cx}:{cy}", "setsar=1"]  # pieces must match to concat
        if settings.get("grade", True):
            v.append("eq=brightness=0.03:contrast=1.04")
        if gkey:
            v += ["format=yuva444p", f"chromakey=0x{gkey}:0.08:0.03", "despill=type=green:mix=0.6:expand=0"]
        v.append(zoom_pieces(zw, geo, z) if zw else ",".join(geo(z)))
        bg = (settings.get("bg_color") or "#F4F1EC").lstrip("#")
        panel = (settings.get("panel_color") or "#0F1B2D").lstrip("#")
        seam = (settings.get("highlight_color") or "#F97316").lstrip("#")
        t = max(2, round(h * 0.0021)) // 2 * 2
        if gkey and br_where != "bottom":
            pre.append(bg_graph("bg", w, bot_h))
        if br_where == "bottom":
            # B-roll replaces YOU in the bottom panel (the card stays on top); the speaker picture isn't used
            pre.append(_broll_graph("1:v", "sp", br, fps, w, bot_h))
            gkey_bg = False
        else:
            gkey_bg = bool(gkey)
        if br_where == "top":
            # B-roll fills the top panel; under a card it's dimmed with the panel colour so the white text stays readable
            # exact RGB blend 45 % footage / 55 % panel colour (drawbox@alpha on YUV lost the footage's colour: red -> grey)
            pr, pg, pb = (int(panel[i:i + 2], 16) if len(panel) == 6 else 30 for i in (0, 2, 4))
            dim = [f"lutrgb=r='val*0.45+{pr * 0.55:.1f}':g='val*0.45+{pg * 0.55:.1f}':b='val*0.45+{pb * 0.55:.1f}'",
                   "format=yuv420p"] if card_on else []
            pre.append(_broll_graph("1:v", "top", br, fps, w, top_h, dim))
        else:
            pre.append(f"color=c=0x{panel}:s={w}x{top_h}:r={fps},format=yuv420p[top]")
        speaker_tail = (f"[bg][sp]overlay=0:0:shortest=1[bot];" if gkey_bg else "") + \
                       f"[top][{'bot' if gkey_bg else 'sp'}]vstack=inputs=2:shortest=1," \
                       f"drawbox=x=0:y={top_h - t // 2}:w={w}:h={t}:color=0x{seam}:t=fill,setsar=1"
        if br_where == "full":  # cutaway hides the whole frame; captions still go on top (post)
            speaker_tail += f"[comp];{_broll_graph('1:v', 'br', br, fps, w, h)};[comp][br]overlay=0:0:shortest=1"
    elif half:
        # you in one half of the screen (cover-cropped around your face), the B-roll in the other
        bh = h // 2 // 2 * 2
        sp_h = h - bh
        sw0, sh0 = (src or {}).get("width") or 1080, (src or {}).get("height") or 1920

        def geo(zz, sw0=sw0, sh0=sh0, sp_h=sp_h):
            f = max(w / sw0, sp_h / sh0) * zz
            S_w, S_h = int(sw0 * f) // 2 * 2, int(sh0 * f) // 2 * 2
            cx = max(0, (S_w - w) // 2)
            cy = int(min(max(0.0, float(settings.get("face_y", 0.27)) * S_h - 0.42 * sp_h), S_h - sp_h)) // 2 * 2
            return [f"scale={S_w}:{S_h}", f"crop={w}:{sp_h}:{cx}:{cy}", "setsar=1"]
        if settings.get("grade", True):
            v.append("eq=brightness=0.03:contrast=1.04")
        v.append(zoom_pieces(zw, geo, z) if zw else ",".join(geo(z)))
    else:
        def geo(zz):
            g = [f"crop=trunc(iw/{zz:.3f}/2)*2:trunc(ih/{zz:.3f}/2)*2"] if zz > 1.001 else []
            if settings.get("fit") == "pad":
                g.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black")
            else:
                g.append(f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}")
            return g + ["setsar=1"]
        v.append(zoom_pieces(zw, geo, z) if zw else ",".join(geo(z)))

    sid = scene["id"]
    post = []  # filters applied to the composed frame
    _ensure_fonts(wd)
    extra = []
    if split:
        extra = cards.events(card, w, top_h, out_dur, bool(scene.get("_card_new")),
                             settings.get("highlight_color") or "#F97316", "Poppins", _ass_time)
    if (caps and words) or extra:
        ass = wd / f"cap_{sid}.ass"
        if write_ass(ass, scene, words if caps else [], ranges, settings, w, h,
                     seam_y=top_h if split else None, extra=extra):
            post.append(f"subtitles=cap_{sid}.ass:fontsdir=fonts")
    if (scene.get("text") or "").strip():
        font = wd / "title_font.ttf"
        if not font.exists():
            for cand in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arial.ttf"):
                if Path(cand).exists():
                    shutil.copy(cand, font)
                    break
        # captions really on screen in this scene? then the text must not sit on them
        cap_on = bool(caps and words and words_in(words, scene["in"], scene["out"]))
        band = caption_band(scene, settings, w, h, top_h if split else None) if cap_on else None
        pos = scene.get("text_pos", "top")
        top_limit = top_h + round(h * 0.02) if split and pos != "top" else round(h * 0.06)  # never onto the card
        tlines, fs, ty, _moved = text_layout(scene["text"].strip(), pos, w, h, band, top_limit)
        for i, ln in enumerate(tlines):  # one drawtext per line: each line centred, own box
            (wd / f"txt_{sid}_{i}.txt").write_text(ln, encoding="utf-8")
            post.append(f"drawtext=fontfile=title_font.ttf:textfile=txt_{sid}_{i}.txt:expansion=none:fontsize={fs}:"
                        f"fontcolor=white:box=1:boxcolor=black@0.55:boxborderw={round(fs * 0.35)}:x=(w-text_w)/2:"
                        f"y={ty + i * round(fs * 1.55) + round(fs * 0.35)}")

    fx = [f for e in (scene.get("effects") or []) if isinstance(e, str)
          for f in library.effect_filters(e, w, h, fps, out_dur)]
    post = fx + post
    inputs = ["-ss", f"{scene['in']:.3f}", "-t", f"{dur:.3f}", "-i", core.raw_dir(pid) / scene["src"]]
    in_idx = 1
    if br:
        # input 1 = the B-roll, looped (a cutaway longer than the clip wraps around); its audio is never used.
        # A picture is held for the whole scene.
        if br.get("still"):
            inputs += ["-loop", "1", "-framerate", str(fps), "-i", Path(br["file"])]
        else:
            inputs += ["-ss", f"{float(br['at']):.3f}", "-stream_loop", "-1", "-i", Path(br["file"])]
        in_idx = 2
    if bg_in is not None:  # the picture is held / the video loops for the whole scene, continuing from where the
        if bg_src_[0] == "picture":  # previous scene left it (no restart at every cut); its sound is never used
            inputs += ["-loop", "1", "-framerate", str(fps), "-i", bg_src_[1]]
        else:
            ln = bg_src_[3] or 0.0
            inputs += ["-ss", f"{(bg_t0 % ln) if ln > 0.5 else 0.0:.3f}", "-stream_loop", "-1", "-i", bg_src_[1]]
        in_idx = bg_in + 1
    ov = [o for o in (scene.get("_ov") or []) if isinstance(o, dict)]
    post_deferred = None
    if ov:  # track overlays go UNDER the captions / text / effects chain: compose first, overlay, then `post`
        post_deferred, post = post, []
    if split:
        graph = pre + ([] if br_where == "bottom" else [f"[0:v]{','.join(v)}[sp]"]) + \
            [f"{speaker_tail}{(',' + ','.join(post)) if post else ''}[v0]"]
    elif half:
        seam_c = (settings.get("highlight_color") or "#F97316").lstrip("#")
        t = max(2, round(h * 0.0021)) // 2 * 2
        bh = h // 2 // 2 * 2
        y_seam = bh if br_where == "top" else h - bh
        order = "[br][sp0]" if br_where == "top" else "[sp0][br]"
        sp_hh = h - bh
        spk = ([bg_graph("bgH", w, sp_hh),  # green screen in your half too (wall / picture / video behind you)
                f"[0:v]{','.join(v)},format=yuva444p,chromakey=0x{gkey}:0.08:0.03,despill=type=green:mix=0.6:expand=0[spk]",
                "[bgH][spk]overlay=0:0:shortest=1,format=yuv420p[sp0]"] if gkey else [f"[0:v]{','.join(v)}[sp0]"])
        graph = spk + [_broll_graph("1:v", "br", br, fps, w, bh),
                 f"{order}vstack=inputs=2:shortest=1,drawbox=x=0:y={y_seam - t // 2}:w={w}:h={t}:color=0x{seam_c}:t=fill,"
                 f"setsar=1{(',' + ','.join(post)) if post else ''}[v0]"]
    else:
        if gkey:  # green screen in the full layout: you, keyed, over the wall colour / picture / video
            g0 = [bg_graph("bgF", w, h),
                  f"[0:v]{','.join(v)},format=yuva444p,chromakey=0x{gkey}:0.08:0.03,despill=type=green:mix=0.6:expand=0[spk]",
                  "[bgF][spk]overlay=0:0:shortest=1,format=yuv420p[sp0]"]
        else:
            g0 = [f"[0:v]{','.join(v)}[sp0]"]
        if br:
            graph = g0 + [_broll_graph("1:v", "br", br, fps, w, h),
                          f"[sp0][br]overlay=0:0:shortest=1{(',' + ','.join(post)) if post else ''}[v0]"]
        else:
            graph = g0 + [f"[sp0]{','.join(post) or 'null'}[v0]"]
    vlabel = "v0"
    if post_deferred is not None:
        cap_on = bool(caps and words and words_in(words, scene["in"], scene["out"]))
        band = caption_band(scene, settings, w, h, top_h if split else None) if cap_on else None
        vlabel, in_idx = _track_overlays(graph, inputs, in_idx, vlabel, ov, w, h, fps, top_h if split else 0,
                                         settings, wd, sid, band, dim_card=bool(split and card_on))
        graph.append(f"[{vlabel}]{','.join(post_deferred) or 'null'}[vpost]")
        vlabel = "vpost"
    for k, stk in enumerate(s for s in (scene.get("stickers") or []) if isinstance(s, dict)):
        p = library.path_of(stk.get("file"))
        if not p or not library.media_ok(p):
            continue  # deleted / undecodable library file: the page warns, the render goes on without it
        a = max(0.0, float(stk.get("at") or 0.0))
        if a >= out_dur - 0.05:
            continue  # the scene got shorter (trim / speed) – it would start after the scene ends
        b = out_dur if not stk.get("dur") else min(out_dur, a + max(0.2, float(stk["dur"])))
        size = max(16, round(w * min(0.9, max(0.05, float(stk.get("size") or 0.22)))) // 2 * 2)
        cx, cy = library.STICKER_POS.get(stk.get("pos") or "top-right", library.STICKER_POS["top-right"])
        anim = stk.get("anim") if stk.get("anim") in library.STICKER_ANIM else "pop"
        y0 = f"{cy * h:.0f}-h/2"
        y = {"pop": f"{y0}+{h * 0.035:.0f}*max(0\\,1-(t-{a:.3f})/0.25)",
             "bounce": f"{y0}+{h * 0.012:.0f}*sin((t-{a:.3f})*6)"}.get(anim, y0)
        if p.suffix.lower() == ".gif":
            inputs += ["-ignore_loop", "0", "-i", p]
        else:
            inputs += ["-loop", "1", "-framerate", str(fps), "-i", p]
        fades = (f",fade=t=in:st={a:.3f}:d=0.2:alpha=1" if anim != "still" else "") + \
                (f",fade=t=out:st={max(a, b - 0.15):.3f}:d=0.15:alpha=1" if b < out_dur - 0.05 else "")
        graph.append(f"[{in_idx}:v]fps={fps},format=rgba,scale={size}:-1{fades}[stk{k}]")
        graph.append(f"[{vlabel}][stk{k}]overlay=x={cx * w:.0f}-w/2:y={y}:eval=frame:"
                     f"enable='between(t,{a:.3f},{b:.3f})'[vst{k}]")
        vlabel = f"vst{k}"
        in_idx += 1
    logo = settings.get("logo")
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
        # 32-sample frames FIRST: aselect keeps/drops whole frames (default ~1024 samples = 21 ms), so cuts landed
        # up to 21 ms off the planned point – off the picture cut and outside the de-click fades
        a.append("asetnsamples=n=32:p=0")
        if not full:
            a += [f"aselect='{expr}'", "asetpts=N/SR/TB"]
        # de-click: every join (jump cut, word cut) and both segment ends are hard cuts in the waveform – an audible
        # click mid-speech. A 5 ms fade on each side of every cut removes it (too short to hear as a dip).
        # Measured on a 400 Hz tone: click at a jump cut 2347 -> 285 (clean sine step ~154).
        joins, acc = [], 0.0
        for ra, rb in ranges[:-1]:
            acc += rb - ra
            joins.append(acc)
        total_in = sum(rb - ra for ra, rb in ranges)
        F, D = 0.005, 0.001  # 5 ms ramp + 1 ms silence either side: the join lands within one 32-sample frame
        terms = [f"min(1,t/{F})", f"min(1,max(0,{total_in:.4f}-t)/{F})"] + [f"min(1,max(0,abs(t-{j:.4f})-{D})/{F})" for j in joins[:150]]
        a += [f"volume='{'*'.join(terms)}':eval=frame"]
        a += _atempo_chain(speed)
        vol = 0.0 if scene.get("mute") or settings.get("main_mute") else float(scene.get("volume", 1.0))
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
           "-frames:v", str(n_frames), "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", out.name]
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
    if settings.get("cards_off"):  # the timeline's Cards lane is hidden (👁): empty top panels
        return [dict(sc, _card=None, _card_new=False) for sc in scenes]
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


def with_broll(pid: str, data: dict, scenes: list[dict], cache: dict) -> list[dict]:
    """Resolve each scene's B-roll cutaway into _broll = {file, at, where, sig} (part of the segment cache key).
    Consecutive scenes covered by the same clip CONTINUE its footage (computed here, so splits/trims/reorders never
    replay the same seconds); a scene's own "at" restarts it. A clip that's skipped or missing is dropped silently
    (the page warns). Full layout has no top panel -> full screen."""
    split = data["settings"].get("layout") == "split"
    if data["settings"].get("broll_off"):  # the timeline's B-roll lane is hidden (👁)
        return [{k: v for k, v in sc.items() if k != "_broll"} for sc in scenes]
    out, prev, t_next, lens = [], None, 0.0, {}
    for sc in scenes:
        b = sc.get("broll") if isinstance(sc.get("broll"), dict) else None
        p = core.broll_path(pid, data, b.get("src")) if b else None
        if not p or b["src"] == sc["src"]:
            out.append(sc)
            prev = None
            continue
        if b["src"] not in lens:
            s = core.source_by_file(data, b["src"]) or {}
            try:
                lens[b["src"]] = 0.0 if core.is_still(p.name) else float(s.get("duration") or probe(p).get("duration") or 0.0)
            except PipelineError:
                lens[b["src"]] = 0.0
        clip_len = lens[b["src"]]
        at = b.get("at")
        try:
            at = float(at) if at is not None else (t_next if prev == b["src"] else 0.0)
        except (TypeError, ValueError):
            at = 0.0
        at = at % clip_len if clip_len > 0.2 else 0.0  # past the end: wrap (the input loops anyway)
        st = p.stat()
        where = core.broll_where(b, split)
        still = core.is_still(p.name)
        out.append(dict(sc, _broll={"file": str(p), "at": 0.0 if still else round(at, 3), "where": where,
                                         "sig": [st.st_size, st.st_mtime], "hdr": False if still else is_hdr(p),
                                         "still": still, "fit": "fit" if b.get("fit") == "fit" else "fill"}))
        prev = b["src"]
        words, sil = cache.get(sc["src"], ([], []))
        t_next = at + estimated_duration(sc, sil, data["settings"])
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
    out = core.ENGINES / "studio" / "cache" / "pad_v2.m4a"
    if out.exists() and out.stat().st_size > 10000:
        return out
    out.parent.mkdir(exist_ok=True)
    chords = [(220.0, 261.63, 329.63), (174.61, 220.0, 261.63), (261.63, 329.63, 392.0), (196.0, 246.94, 293.66)]
    tone = lambda f: f"(sin(2*PI*{f}*t)+0.3*sin(4*PI*{f}*t)+0.12*sin(6*PI*{f}*t))"  # noqa: E731
    body = "+".join(f"eq(floor(mod(t,32)/8),{k})*({'+'.join(tone(f) for f in ch)})" for k, ch in enumerate(chords))
    expr = f"0.06*pow(sin(PI*mod(t,8)/8),0.7)*({body})"  # each chord swells in/out: no clicks at changes
    tmp = out.with_name("pad_v2.part.m4a")
    run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"aevalsrc='{expr}|{expr}':s=48000:d=32",  # quoted: commas!
         "-af", "highpass=f=120,lowpass=f=1400", "-c:a", "aac", "-b:a", "160k", tmp])  # dry: v1's aecho read as an echo on the voice
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


def duck_filter(lufs: float, duck_db: float) -> str:
    """Music ducking: compressor keyed by the voice. Ratio 20 => reduction ~= speech level - threshold.
    Threshold offset calibrated by measurement (music RMS during speech vs in pauses, -14 LUFS voice):
    duck 18 -> 18.8 dB measured. Shallow settings duck ~1-3 dB deeper than asked (attack/release smoothing)."""
    thr_db = lufs + 5.0 - float(duck_db) / 0.95
    return (f"sidechaincompress=threshold={10 ** (thr_db / 20):.5f}:ratio=20:knee=1:attack=15:release=350:"
            f"detection=rms")


def _num(v, default: float) -> float:
    try:
        return float(v) if v is not None and v != "" else default
    except (TypeError, ValueError):
        return default


def measure_lufs(path: Path) -> float | None:
    out = run([FFMPEG, "-hide_banner", "-i", path, "-vn", "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
              timeout=900)
    found = re.findall(r"I:\s+(-?[\d.]+) LUFS", out)
    v = float(found[-1]) if found else None
    return v if v is not None and v > -70 else None  # silent / unmeasurable


def voice_profile(src: Path, dur: float) -> dict:
    """Measure the voice before cleaning it: room-noise floor (10th percentile of 50 ms frames, digital silence
    excluded) and speech level (80th percentile). Measured on a phone/Filmora clip: floor -87 dB (already
    denoised + gated) – the old fixed `afftdn=nf=-25` then treated the voice itself as noise and removed ~half of
    its energy (-12 dB at 6-10 kHz): the dull, watery 'echo' sound."""
    out = run([FFMPEG, "-hide_banner", "-v", "info", "-i", src, "-vn", "-af",
               "aformat=channel_layouts=mono,aresample=48000,asetnsamples=n=2400:p=0,"
               "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
               "ametadata=print:key=lavfi.astats.Overall.RMS_level", "-f", "null", "-"],
              timeout=max(600, int(dur * 2)))
    vals = sorted(float(v) for v in re.findall(r"RMS_level=(-?[\d.]+)", out) if float(v) > -90)
    if len(vals) < 10:
        return {"floor": None, "speech": None}
    pick = lambda q: vals[min(len(vals) - 1, int(q * len(vals)))]  # noqa: E731
    return {"floor": pick(0.10), "speech": pick(0.80)}


def voice_chain(prof: dict) -> str:
    """Broadcast-style voice chain, adapted to the measured recording:
    rumble cut -> denoise ONLY when there is real room noise (strength from the measured floor) -> mud cut
    (boxy room, 250 Hz) -> presence (+2.5 dB 3.2 kHz: words cut through on phone speakers) -> light air ->
    de-ess (tames the s/sh the boost brings up) -> gentle compression keyed to the speech level."""
    f = [ "highpass=f=80", "highpass=f=60"]  # 4-pole-ish rumble cut, voice untouched
    floor, speech = prof.get("floor"), prof.get("speech")
    if floor is not None and floor > -58:  # audible hiss / room tone: denoise just above the measured floor
        nf = max(-80, min(-20, round(floor + 2)))
        nr = 8 if floor < -48 else 12
        f.append(f"afftdn=nr={nr}:nf={nf}:tn=1")
    f += ["equalizer=f=250:t=q:w=1.1:g=-2.5", "equalizer=f=3200:t=q:w=1.2:g=2.5",
          "treble=g=2:f=8000:t=s:w=0.7", "deesser=i=0.35:m=0.5:f=0.5:s=o"]
    thr = max(-36.0, min(-10.0, (speech if speech is not None else -20.0) - 4.0))
    f.append(f"acompressor=threshold={thr:.1f}dB:ratio=2.5:attack=10:release=160:knee=6:makeup=1")
    return ",".join(f)


def finish_audio(src: Path, dst: Path, dur: float, music: Path | None, polish: bool,
                 lufs: float = -16.0, duck_db: float = 10.0, trim_db: float = 0.0) -> None:
    """Final mix over the joined video (video stream copied): voice clean-up + loudness target, optional music
    sitting 12 LU under the voice between lines and ducked a further `duck_db` under dialogue."""
    voice = "aresample=48000"
    lufs = max(-24.0, min(-10.0, float(lufs)))
    tgt = f"I={lufs:g}:TP=-1.0:LRA=7"
    if polish:
        clean = voice_chain(voice_profile(src, dur))
        # two-pass loudnorm: one-pass (dynamic) overshoots on short reels (measured -14.3 instead of -16)
        meas = run([FFMPEG, "-hide_banner", "-i", src, "-vn", "-af",
                    f"{clean},loudnorm={tgt}:print_format=json", "-f", "null", "-"],
                   timeout=max(600, int(dur * 2)))
        try:
            m = json.loads(meas[meas.rindex("{"):meas.rindex("}") + 1])
            ln = (f"loudnorm={tgt}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
                  f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:"
                  f"linear=true")
        except (ValueError, KeyError):
            ln = f"loudnorm={tgt}"
        if "-inf" in ln:  # silent track: nothing to normalise
            ln = "anull"
        voice += f",{clean},{ln},aresample=48000"
    inputs = ["-i", src]
    trim_db = max(-6.0, min(6.0, float(trim_db or 0.0)))
    # final correction gain (measured after a first mix) + peak limiter at -1 dBTP.
    # alimiter: level=false, or it auto-gains everything up to 0 dB (measured: -14.5 LUFS instead of -16)
    tail = (f",volume={trim_db:.2f}dB" if abs(trim_db) > 0.05 else "") + \
           (",alimiter=limit=0.891:level=false" if (music or abs(trim_db) > 0.05) else "")
    if music:
        inputs += ["-stream_loop", "-1", "-i", music]
        fade = max(0.0, dur - 1.5)
        # fixed measured gain (loudnorm on a looped stream pumped it up 1.5 LU): music bed 12 LU under the voice
        gain = (lufs - 12.0) - music_lufs(music)
        graph = (f"[0:a]{voice},asplit=2[vo][key];"
                 f"[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                 f"volume={gain:.2f}dB,equalizer=f=2500:t=q:w=1.5:g=-5,afade=t=in:d=1,afade=t=out:st={fade:.2f}:d=1.5[m];"
                 + (f"[m][key]{duck_filter(lufs, duck_db)}[md];" if duck_db > 0.5 else "[key]anullsink;[m]anull[md];") +
                 f"[vo][md]amix=inputs=2:duration=first:normalize=0{tail}[a]")
    else:
        graph = f"[0:a]{voice}{tail}[a]"
    run([FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-t", f"{dur:.3f}", "-movflags", "+faststart", dst],
        timeout=max(600, int(dur * 3)))


def render_project(pid: str, preview: bool) -> Path:
    data = core.load(pid)
    skipped = {s["file"] for s in data["sources"] if s.get("include") is False}  # clip set to "Skip" in Footage
    scenes = [s for s in data["scenes"] if s.get("keep", True) and s["src"] not in skipped]
    broll_only = bool(scenes) and all(s.get("role") == "broll" for s in scenes)
    scenes = [s for s in scenes if s.get("role") != "broll"]  # B-roll scenes only appear as cutaways over others
    if not scenes:
        raise PipelineError("Every kept scene is B-roll – set at least one scene to Role: Speaker." if broll_only else
                            "No scenes are kept – nothing to render." +
                            (" (Some clips are set to Skip in the Footage tab.)" if skipped else ""))
    missing = sorted({s["src"] for s in scenes if not (core.raw_dir(pid) / s["src"]).is_file()})
    if missing:
        raise PipelineError(f"Source video missing from Raw Footage: {', '.join(missing)}")
    sig = core.edit_sig(data)  # what this render shows – lets the page say "edited since this render"
    w, h = target_size(data, preview)
    wd = core.work_dir(pid)
    cache: dict[str, tuple[list, list]] = {}
    scenes = with_effective_cards(scenes, data["settings"])

    for sc in scenes:
        if sc["src"] not in cache:
            cache[sc["src"]] = (core.read_json(core.work_file(pid, sc["src"], "words.json"), []) or [],
                                load_silences(pid, sc["src"]))
    # word cuts may remove a whole scene: skip it rather than render an empty segment
    scenes = [sc for sc in scenes if kept_ranges(sc, cache[sc["src"]][1], data["settings"])]
    if not scenes:
        raise PipelineError("Every kept scene was cut away completely – nothing to render.")
    scenes = with_broll(pid, data, scenes, cache)
    track_sounds: list[dict] = []
    if tracks_mod.tracks_of(data):
        plan_durs = [scene_out_len(sc, cache[sc["src"]][1], data["settings"]) for sc in scenes]
        ov, track_sounds = tracks_mod.plan(pid, data, scenes, plan_durs)
        scenes = [dict(sc, _ov=o) if o else sc for sc, o in zip(scenes, ov)]
    gb = green_background(data["settings"])
    if gb and gb[0] == "video":
        acc, out_sc = 0.0, []
        for sc in scenes:
            out_sc.append(dict(sc, _bg_t0=round(acc, 3)))
            acc += scene_out_len(sc, cache[sc["src"]][1], data["settings"])
        scenes = out_sc
    elif data["settings"].get("bg_mode") in ("picture", "video"):
        core.job_log(pid, "Green screen: the chosen picture / video is missing – using the clean wall colour instead.")

    def render_all(nvenc: bool) -> list[Path]:
        segs = []
        for i, sc in enumerate(scenes, 1):
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
        used = [int(m.group(1)) for p in core.EXPORTS.glob(f"{data['id']}_v*.mp4")
                for m in [re.match(re.escape(data["id"]) + r"_v(\d+)(?: \(\d+\))?\.mp4$", p.name)] if m]
        n = max([len([r for r in data.get("renders", []) if r.get("kind") == "final"])] + used) + 1
        out = core.unique_path(core.EXPORTS, f"{data['id']}_v{n}.mp4")
    tmp_out = out.with_name(out.stem + ".part.mp4")
    st = data["settings"]
    music = music_file(pid, st)
    import refaudio
    rref = refaudio.ref_of(data, st.get("ref_audio"))
    if rref:  # 🎵 the look plays its reference's background music (voice removed) – it REPLACES the music bed
        try:
            core.set_job(pid, progress=f"Background audio of {refaudio.label(data, st['ref_audio'])}")
            music = refaudio.bg_audio(pid, rref, log=lambda m: core.job_log(pid, m))
        except refaudio.RefAudioError as e:
            core.job_log(pid, f"Background audio left out: {e}")
    elif str(st.get("ref_audio") or "none") != "none":
        core.job_log(pid, "Background audio left out: that reference was removed – pick another in the look settings.")
    polish = bool(st.get("audio_polish", False))
    lufs = _num(st.get("loudness"), -16.0)
    duck = _num(st.get("duck_db"), 10.0)  # 0 is a valid choice ("don't duck") – no `or default` here
    durs = [_video_len(sp) for sp in segs]
    fx_join = wd / "joined_fx.part.mp4"
    try:
        core.set_job(pid, progress="Joining scenes")
        has_fx = all(d > 0 for d in durs) and assemble(wd, scenes, segs, durs, fx_join,
                                                       int(st.get("fps", 30) or 30), preview, track_sounds)
    except PipelineError as e:
        fx_join.unlink(missing_ok=True)
        core.job_log(pid, f"Transitions / sound effects could not be added ({str(e).splitlines()[-1][:120]}) – "
                          f"rendered without them")
        has_fx = False
    try:
        if has_fx and not (music or polish):
            os.replace(fx_join, tmp_out)
        elif music or polish:
            joined = wd / "joined.part.mp4"
            try:
                if has_fx:
                    os.replace(fx_join, joined)
                else:
                    run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst.name, "-c", "copy",
                         joined.name], cwd=wd)
                jdur = probe(joined)["duration"]
                core.set_job(pid, progress="Mixing audio" + (" + music" if music else ""))
                try:
                    finish_audio(joined, tmp_out, jdur, music, polish, lufs, duck)
                except PipelineError as e:
                    if not music:
                        raise
                    # a damaged/unsupported music file must not cost the whole render
                    core.job_log(pid, f"Music '{music.name}' could not be mixed ({str(e).splitlines()[-1][:100]})"
                                      f" – rendered without music")
                    music = None
                    finish_audio(joined, tmp_out, jdur, None, polish, lufs, duck)
                if polish:
                    # short clips: the two-pass measurement can land ~1 LU off; measure the real mix and correct once
                    got = measure_lufs(tmp_out)
                    if got is not None and abs(got - lufs) > 0.5:
                        tmp2 = tmp_out.with_name(tmp_out.stem + "2.mp4")
                        finish_audio(joined, tmp2, jdur, music, polish, lufs, duck, trim_db=lufs - got)
                        os.replace(tmp2, tmp_out)
            finally:
                joined.unlink(missing_ok=True)
        else:
            run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst.name, "-c", "copy",
                 "-movflags", "+faststart", tmp_out], cwd=wd)
    except BaseException:
        fx_join.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)  # never leave a half-written file in Exports
        tmp_out.with_name(tmp_out.stem + "2.mp4").unlink(missing_ok=True)
        raise
    os.replace(tmp_out, out)  # never leave a half-written file in Exports
    dur = probe(out)["duration"]
    _prune_segments(wd, "preview" if preview else "final", segs)
    # REAL scene start/length in this file (the page's playhead/scene jumps; estimates drift by frames per scene)
    times, t = [], 0.0
    for sc, d_ in zip(scenes, durs):
        if d_ <= 0:  # timing info is a nicety, never fail a finished render for it
            times = None
            break
        times.append([sc["id"], round(t, 3), round(d_, 3)])
        t += d_

    def fn(d):
        d.setdefault("renders", []).append({"kind": "preview" if preview else "final", "file": out.name,
                                            "created": core.now_iso(), "duration": dur, "size": f"{w}x{h}",
                                            "sig": sig, "times": times})
        # keep EVERY final export's record (the Exports page lists them) – auto-preview adds a record per edit and a
        # plain "last 30" made finals vanish from the page after ~30 edits; previews: only the last 5 matter
        rs = d["renders"]
        fin = [r for r in rs if r.get("kind") == "final"][-200:]
        pv = [r for r in rs if r.get("kind") != "final"][-5:]
        d["renders"] = [r for r in rs if r in fin or r in pv]
    core.mutate(pid, fn)
    return out
