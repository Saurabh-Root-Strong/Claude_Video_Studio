"""Export final: where it goes (platform preset), frame, resolution, file type, quality, fps.

Pure logic (no ffmpeg here) so the page, the worker and the selftest share ONE definition:
  normalize(data, raw)   -> validated options (unknown / bad values fall back, never crash a render)
  dims(data, opts)       -> (w, h) even pixels for that frame + resolution
  warnings(data, opts)   -> plain-language heads-ups (too long for the platform, upscaling, 60 fps from 30 ...)
  catalog(data)          -> everything the Export dialog shows (presets, choices, the edit's own frame/size/length)

The edit itself is never changed: an export to another frame renders the same scenes / captions / cards laid out
for that frame (pipeline.render_project(opts=...)), the project's settings stay as they are.
"""
from __future__ import annotations

FRAMES = {  # id -> (label, w-ratio, h-ratio)
    "9:16": ("9:16 vertical", 9, 16),
    "4:5": ("4:5 portrait", 4, 5),
    "1:1": ("1:1 square", 1, 1),
    "16:9": ("16:9 landscape", 16, 9),
}
RES = {"source": "As uploaded", "480": "480p", "720": "720p HD", "1080": "1080p Full HD",
       "1440": "2K (1440p)", "2160": "4K (2160p)"}
FPS = ("edit", "24", "25", "30", "60")
QUALITY = {"standard": "Standard – smaller file", "high": "High (recommended)", "max": "Maximum – biggest file"}

# file types. web = plays in the studio's player (and in any browser); master_q = quality of the H.264 master the
# conversion starts from (a re-encode from a "high" master would stack two losses)
FORMATS = {
    "mp4": {"label": "MP4 · H.264", "ext": "mp4", "web": True, "note": "Plays everywhere – best for uploading."},
    "mp4_hevc": {"label": "MP4 · H.265 (HEVC)", "ext": "mp4", "web": False,
                 "note": "About 40 % smaller at the same quality. Phones, Mac, Windows 11 play it; some old PCs / browsers can't."},
    "mov": {"label": "MOV · H.264", "ext": "mov", "web": True, "note": "For Apple apps – iPhone, iMovie, Final Cut."},
    "mov_prores": {"label": "MOV · ProRes 422", "ext": "mov", "web": False,
                   "note": "Editing master for Premiere / Final Cut / DaVinci – about 15× bigger. Not for uploading."},
    "webm": {"label": "WebM · VP9", "ext": "webm", "web": True,
             "note": "For websites. Slow to make (CPU only)."},
    "mkv": {"label": "MKV · H.264", "ext": "mkv", "web": False, "note": "Same video as MP4 in an MKV box (VLC, OBS, archives)."},
    "mp3": {"label": "MP3 · audio only", "ext": "mp3", "web": True,
            "note": "Just the final sound (voice + music + effects) – for podcasts / WhatsApp voice."},
}

# where it will be posted. frames = what that platform shows full-size (first = its native one); max_len seconds
# (None = no practical limit); tips = what a pro checks before uploading. Limits as published in 2025 – they change,
# so they WARN, never block.
PLATFORMS = {
    "edit": {"label": "Same as my edit", "icon": "🎬", "frames": None, "res": "1080", "fps": "edit", "fmt": "mp4",
             "max_len": None, "tip": "Exactly the frame you edited in."},
    "reel": {"label": "Instagram Reel", "icon": "📸", "frames": ["9:16"], "res": "1080", "fps": "edit", "fmt": "mp4",
             "max_len": 180, "max_mb": 4000,
             "tip": "9:16, 1080×1920. Instagram re-compresses anything bigger, so 2K/4K only makes the upload slower. "
                    "Keep words out of the bottom 20 % (the caption + buttons cover it)."},
    "short": {"label": "YouTube Short", "icon": "▶", "frames": ["9:16", "1:1"], "res": "1080", "fps": "edit", "fmt": "mp4",
              "max_len": 180,
              "tip": "9:16 and up to 3 minutes becomes a Short by itself. YouTube keeps 2K/4K quality better than Instagram."},
    "youtube": {"label": "YouTube video", "icon": "📺", "frames": ["16:9"], "res": "1080", "fps": "edit", "fmt": "mp4",
                "max_len": None,
                "tip": "16:9 landscape. A vertical edit is placed on a soft blurred copy of itself (no black bars)."},
    "tiktok": {"label": "TikTok", "icon": "🎵", "frames": ["9:16"], "res": "1080", "fps": "edit", "fmt": "mp4",
               "max_len": 600, "tip": "9:16, 1080×1920. Keep words out of the right edge and the bottom 25 %."},
    "x": {"label": "X (Twitter)", "icon": "✖", "frames": ["9:16", "16:9", "1:1", "4:5"], "res": "1080", "fps": "edit",
          "fmt": "mp4", "max_len": 140, "max_mb": 512,
          "tip": "Any frame works. Free accounts: up to 2 min 20 s and 512 MB."},
    "linkedin": {"label": "LinkedIn", "icon": "💼", "frames": ["4:5", "1:1", "9:16", "16:9"], "res": "1080", "fps": "edit",
                 "fmt": "mp4", "max_len": 600, "max_mb": 5000,
                 "tip": "4:5 fills the feed on phones without being cut; pick 9:16 for LinkedIn's vertical video feed. "
                        "Most people watch muted – keep captions on."},
    "custom": {"label": "Custom", "icon": "⚙", "frames": None, "res": "source", "fps": "edit", "fmt": "mp4",
               "max_len": None, "tip": "Choose everything yourself."},
}


def edit_frame(data: dict) -> str:
    """The frame the user edited in, as a FRAMES id ('original' -> the nearest one of the first clip)."""
    fmt = (data.get("settings") or {}).get("format", "original")
    if fmt in FRAMES:
        return fmt
    w, h = source_size(data)
    r = w / h if h else 9 / 16
    return min(FRAMES, key=lambda k: abs(FRAMES[k][1] / FRAMES[k][2] - r))


def used_sources(data: dict) -> list[dict]:
    files = {s.get("src") for s in data.get("scenes", []) if s.get("keep", True)}
    out = [s for s in data.get("sources", []) if s.get("file") in files and s.get("include", True) is not False]
    return out or [s for s in data.get("sources", []) if s.get("width")]


def source_size(data: dict) -> tuple[int, int]:
    """Size of the biggest clip in the video (rotation already applied by analyse)."""
    best = (0, 0)
    for s in used_sources(data):
        w, h = int(s.get("width") or 0), int(s.get("height") or 0)
        if min(w, h) > min(best) or best == (0, 0):
            best = (w, h)
    return best if best[0] and best[1] else (1080, 1920)


def source_fps(data: dict) -> float:
    return max([float(s.get("fps") or 0) for s in used_sources(data)] or [0.0])


def edit_fps(data: dict) -> int:
    return int((data.get("settings") or {}).get("fps", 30) or 30)


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def normalize(data: dict, raw: dict | None) -> dict:
    """Validated options; every field always present. Unknown values -> the platform's default."""
    raw = raw if isinstance(raw, dict) else {}
    plat = raw.get("platform") if raw.get("platform") in PLATFORMS else "edit"
    p = PLATFORMS[plat]
    ef = edit_frame(data)
    frame = raw.get("frame")
    if frame == "edit" or frame not in FRAMES:
        frame = ef if not p["frames"] or ef in p["frames"] else p["frames"][0]
    dres = p["res"]
    if plat in ("edit", "custom"):
        # like the classic export: a fixed frame (9:16 / 1:1 / 16:9) renders at 1080 (sharp captions even from 576p
        # footage); "original" keeps the clip's own size (4K stays 4K)
        dres = "1080" if (data.get("settings") or {}).get("format", "original") in FRAMES else "source"
    res = str(raw.get("res") or dres)
    if res not in RES:
        res = dres
    fps = str(raw.get("fps") or p["fps"])
    if fps not in FPS:
        fps = p["fps"]
    fmt = raw.get("fmt") if raw.get("fmt") in FORMATS else p["fmt"]
    q = raw.get("quality") if raw.get("quality") in QUALITY else "high"
    return {"platform": plat, "frame": frame, "res": res, "fps": fps, "fmt": fmt, "quality": q}


def short_side(data: dict, opts: dict) -> int:
    if opts["res"] == "source":
        return min(max(min(source_size(data)), 240), 2160)  # 8K phone footage: 4K is the most a 4 GB GPU / 7 GB RAM can do
    return int(opts["res"])


def dims(data: dict, opts: dict) -> tuple[int, int]:
    s = short_side(data, opts)
    if (data.get("settings") or {}).get("format", "original") not in FRAMES and opts["frame"] == edit_frame(data):
        a, b = source_size(data)  # edited at the clip's own shape (e.g. 3:4): keep that exact shape
        if a and b:
            return (_even(s), _even(s * b / a)) if b >= a else (_even(s * a / b), _even(s))
    _, a, b = FRAMES[opts["frame"]]
    return (_even(s), _even(s * b / a)) if b >= a else (_even(s * a / b), _even(s))


def fps_of(data: dict, opts: dict) -> int:
    return edit_fps(data) if opts["fps"] == "edit" else int(opts["fps"])


def reframes(data: dict, opts: dict) -> bool:
    return opts["frame"] != edit_frame(data)


def flips_orientation(data: dict, opts: dict) -> bool:
    """Vertical edit -> landscape export (or back): cropping would cut away most of the picture."""
    w, h = source_size(data)
    W, H = dims(data, opts)
    return (w >= h) != (W >= H) and abs(w / h - W / H) > 0.5


def is_default(opts: dict | None) -> bool:
    return not opts


def tag(data: dict, opts: dict) -> str:
    """File name part: _reel_1080x1920 (platform + size), so several exports of one version stay apart."""
    w, h = dims(data, opts)
    return f"_{opts['platform']}_{w}x{h}" if FORMATS[opts["fmt"]]["ext"] != "mp3" else f"_{opts['platform']}_audio"


def est_mb(data: dict, opts: dict, seconds: float) -> float:
    """Rough file size (talking-head footage). Shown as '≈', never used to decide anything but the disk check."""
    if opts["fmt"] == "mp3":
        return seconds * 320 / 8 / 1024
    w, h = dims(data, opts)
    px = (w * h) / (1920 * 1080)
    fr = fps_of(data, opts) / 30
    if opts["fmt"] == "mov_prores":
        mbps = (220 if opts["quality"] == "max" else 147) * px * fr
    else:
        mbps = {"standard": 4.0, "high": 7.0, "max": 12.0}[opts["quality"]] * (px ** 0.75) * (fr ** 0.6)
        mbps *= {"mp4_hevc": 0.6, "webm": 0.65}.get(opts["fmt"], 1.0)
    return seconds * (mbps + 0.26) / 8


def est_minutes(data: dict, opts: dict, seconds: float) -> float:
    """Very rough make-time on this PC (GTX 1650 NVENC, 4-core CPU) – for the dialog's 'about N min'."""
    w, h = dims(data, opts)
    px = (w * h) / (1920 * 1080)
    t = seconds * 0.9 * max(px, 0.35) * (fps_of(data, opts) / 30)
    if opts["fmt"] == "webm":
        t += seconds * 1.6 * px
    elif opts["fmt"] in ("mp4_hevc", "mov_prores"):
        t += seconds * 0.4 * px
    return max(0.3, t / 60)


def warnings(data: dict, opts: dict, seconds: float) -> list[str]:
    out = []
    p = PLATFORMS[opts["platform"]]
    if p.get("max_len") and seconds > p["max_len"] + 0.5:
        m, s = divmod(int(p["max_len"]), 60)
        out.append(f"Your video is {int(seconds // 60)}:{int(seconds % 60):02d} long – {p['label']} takes up to "
                   f"{m}:{s:02d}. Trim it, or it may be refused / cut.")
    mb = est_mb(data, opts, seconds)
    if p.get("max_mb") and mb > p["max_mb"] * 0.9:
        out.append(f"The file may be about {mb:.0f} MB – {p['label']} takes up to {p['max_mb']} MB. Pick a lower "
                   f"quality or resolution.")
    src_short = min(source_size(data))
    # up to 1080 enlarging is GOOD (captions / titles are drawn sharp at that size); above it the file just grows
    if opts["res"] != "source" and int(opts["res"]) > max(src_short * 1.05, 1080) and opts["fmt"] != "mp3":
        out.append(f"Your footage is {src_short}p – {RES[opts['res']]} only enlarges it (bigger file, no extra detail).")
    if opts["fmt"] != "mp3" and short_side(data, opts) >= 1440:
        out.append("2K / 4K takes several times longer to make and needs a lot of memory – close other programs.")
    if opts["platform"] in ("reel", "tiktok") and opts["res"] in ("1440", "2160", "source") and short_side(data, opts) > 1080:
        out.append(f"{p['label']} shows at most 1080 wide – a bigger file is only a slower upload.")
    sf = source_fps(data)
    if opts["fmt"] != "mp3" and sf and opts["fps"] != "edit" and fps_of(data, opts) > sf + 1:
        out.append(f"Your footage was filmed at {sf:.0f} fps – {fps_of(data, opts)} fps repeats frames, it won't look smoother.")
    if reframes(data, opts) and opts["fmt"] != "mp3":
        if flips_orientation(data, opts):
            out.append(f"You edited in {edit_frame(data)} – the {opts['frame']} export shows your video on a soft blurred "
                       f"copy of itself. Captions and titles are laid out again for {opts['frame']}.")
        else:
            out.append(f"You edited in {edit_frame(data)} – {opts['frame']} cuts a little off the edges (kept around your "
                       f"face). Captions and titles are laid out again for {opts['frame']}.")
    if opts["fmt"] == "mov_prores":
        out.append("ProRes is an editing format: the file is very big and won't play in the studio's player.")
    if opts["fmt"] == "mp4_hevc":
        out.append("H.265 may not play in this browser – open it from the Exports folder.")
    return out


def summary(data: dict, opts: dict) -> str:
    """One line for the Exports list: 'Instagram Reel · 1080×1920 · 30 fps · MP4 · H.264 · High'."""
    f = FORMATS[opts["fmt"]]
    if opts["fmt"] == "mp3":
        return f"{PLATFORMS[opts['platform']]['label']} · {f['label']}"
    w, h = dims(data, opts)
    return (f"{PLATFORMS[opts['platform']]['label']} · {w}×{h} · {fps_of(data, opts)} fps · {f['label']} · "
            f"{opts['quality'].capitalize()}")


def catalog(data: dict) -> dict:
    w, h = source_size(data)
    return {
        "platforms": [{"id": k, **v} for k, v in PLATFORMS.items()],
        "frames": [{"id": k, "label": v[0]} for k, v in FRAMES.items()],
        "res": [{"id": k, "label": v} for k, v in RES.items()],
        "fps": list(FPS), "quality": [{"id": k, "label": v} for k, v in QUALITY.items()],
        "formats": [{"id": k, **v} for k, v in FORMATS.items()],
        "edit": {"frame": edit_frame(data), "fps": edit_fps(data), "src_w": w, "src_h": h,
                 "src_fps": round(source_fps(data), 2)},
    }
