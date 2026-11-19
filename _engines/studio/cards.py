"""Top-panel cards for the split layout, drawn as ASS subtitle events (no extra render engine needed).

A card is a dict stored on a scene:
    {"kind": "title",  "text": "bajaj *IPO* listing aaj"}          *word* = keyword (orange)
    {"kind": "number", "value": "2x+", "label": "listing gain"}
    {"kind": "chart",  "points": [10, 12, 11, 15, 18], "label": "Nifty 5 days"}
    {"kind": "none"}                                                  empty panel
A scene without a card keeps showing the previous scene's card (it does not re-animate), so one card can span
several scenes. Only a NEW card pops in.
"""
from __future__ import annotations

import re

KINDS = ("title", "number", "chart", "none")


def _c(hexcol: str, alpha: int = 0) -> str:
    h = (hexcol or "#FFFFFF").lstrip("#")
    h = h if len(h) == 6 else "FFFFFF"
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def clean_text(s: str) -> str:
    return re.sub(r"[{}\\\r\n]", " ", str(s or "")).strip()[:120]


def parse(text: str) -> dict | None:
    """'title bajaj *IPO* aaj' | 'number 2x+ | listing gain' | 'chart 10,12,15 | label' | 'none' -> card dict."""
    t = text.strip()
    m = re.fullmatch(r"(title|number|chart|none|off|clear)\b\s*(.*)", t, flags=re.I | re.S)
    if not m:
        return {"kind": "title", "text": clean_text(t)} if t else None
    kind, rest = m.group(1).lower(), m.group(2).strip()
    if kind in ("none", "off", "clear"):
        return {"kind": "none"}
    if kind == "title":
        return {"kind": "title", "text": clean_text(rest)} if rest else None
    main, _, label = rest.partition("|")
    if kind == "number":
        return {"kind": "number", "value": clean_text(main)[:16], "label": clean_text(label)[:40]} if main.strip() else None
    pts = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", main)]
    return {"kind": "chart", "points": pts[:60], "label": clean_text(label)[:40]} if len(pts) >= 2 else None


def valid(card) -> bool:
    if not isinstance(card, dict) or card.get("kind") not in KINDS:
        return False
    k = card["kind"]
    if k == "title":
        return bool(str(card.get("text", "")).strip())
    if k == "number":
        return bool(str(card.get("value", "")).strip())
    if k == "chart":
        p = card.get("points")
        return isinstance(p, list) and len(p) >= 2 and all(isinstance(x, (int, float)) for x in p)
    return True


def _rich(text: str, base: str, hi: str) -> str:
    """*keyword* -> orange keyword."""
    out, on = [], False
    for part in re.split(r"(\*)", clean_text(text)):
        if part == "*":
            on = not on
            out.append(f"{{\\c{hi if on else base}}}")
        else:
            out.append(part)
    return "".join(out)


def events(card: dict | None, w: int, top_h: int, dur: float, animate: bool, accent: str, font: str,
           ass_time) -> list[str]:
    """ASS Dialogue lines drawing `card` inside the top panel (y 0..top_h) for the whole segment."""
    if not card or card.get("kind") in (None, "none") or dur <= 0.05:
        return []
    white, hi = _c("#FFFFFF"), _c(accent)
    t0, t1 = ass_time(0), ass_time(dur)
    ms = int(dur * 1000)
    pop = "\\fad(160,0)\\fscx86\\fscy86\\t(0,160,\\fscx100\\fscy100)" if animate else ""
    push = f"\\t({160 if animate else 0},{ms},\\fscx104\\fscy104)"  # slow 4 % push-in so the panel never sits still
    lines = []
    k = card["kind"]
    if k == "title":
        fs = round(top_h * 0.19)
        lines.append(f"Dialogue: 1,{t0},{t1},Card,,{round(w * 0.08)},{round(w * 0.08)},0,,"
                     f"{{\\an5\\pos({w // 2},{round(top_h * 0.5)})\\fn{font}\\b1\\fs{fs}\\c{white}{pop}{push}}}"
                     f"{_rich(card.get('text', ''), white, hi)}")
    elif k == "number":
        lines.append(f"Dialogue: 1,{t0},{t1},Card,,0,0,0,,{{\\an5\\pos({w // 2},{round(top_h * 0.44)})\\fn{font}\\b1"
                     f"\\fs{round(top_h * 0.42)}\\c{hi}{pop}{push}}}{clean_text(card.get('value'))}")
        if card.get("label"):
            fade = "\\fad(300,0)" if animate else ""  # (no backslash inside f-string braces: Python < 3.12)
            lines.append(f"Dialogue: 1,{t0},{t1},Card,,0,0,0,,{{\\an5\\pos({w // 2},{round(top_h * 0.74)})\\fn{font}"
                         f"\\b0\\fs{round(top_h * 0.12)}\\c{white}{fade}}}"
                         f"{clean_text(card.get('label'))}")
    elif k == "chart":
        pts = [float(x) for x in card["points"]]
        lo, hi_v = min(pts), max(pts)
        span = (hi_v - lo) or 1.0
        x0, x1, y0, y1 = w * 0.10, w * 0.90, top_h * 0.30, top_h * 0.82
        xy = [(x0 + (x1 - x0) * i / (len(pts) - 1), y1 - (y1 - y0) * (p - lo) / span) for i, p in enumerate(pts)]
        area = f"m {x0:.0f} {y1:.0f} " + " ".join(f"l {x:.0f} {y:.0f}" for x, y in xy) + f" l {x1:.0f} {y1:.0f}"
        lw = max(3, round(top_h * 0.012))
        line = (f"m {xy[0][0]:.0f} {xy[0][1]:.0f} " + " ".join(f"l {x:.0f} {y:.0f}" for x, y in xy[1:]) + " " +
                " ".join(f"l {x:.0f} {y - lw:.0f}" for x, y in reversed(xy)))
        fade = "\\fad(300,0)" if animate else ""
        lines.append(f"Dialogue: 1,{t0},{t1},Card,,0,0,0,,{{\\an7\\pos(0,0)\\bord0\\shad0\\1c{hi}\\1a&HB0&{fade}\\p1}}"
                     f"{area}{{\\p0}}")
        lines.append(f"Dialogue: 2,{t0},{t1},Card,,0,0,0,,{{\\an7\\pos(0,0)\\bord0\\shad0\\1c{hi}{fade}\\p1}}{line}{{\\p0}}")
        if card.get("label"):
            lines.append(f"Dialogue: 2,{t0},{t1},Card,,0,0,0,,{{\\an7\\pos({round(w * 0.10)},{round(top_h * 0.09)})"
                         f"\\fn{font}\\b1\\fs{round(top_h * 0.08)}\\c{white}{fade}}}{clean_text(card['label'])}")
        up = pts[-1] >= pts[0]
        chg = f"{'+' if up else ''}{(pts[-1] - pts[0]) / abs(pts[0]) * 100:.1f}%" if pts[0] else ""
        if chg:
            lines.append(f"Dialogue: 2,{t0},{t1},Card,,0,0,0,,{{\\an9\\pos({round(w * 0.90)},{round(top_h * 0.09)})"
                         f"\\fn{font}\\b1\\fs{round(top_h * 0.08)}\\c{_c('#22C55E' if up else '#EF4444')}{fade}}}{chg}")
    return lines
