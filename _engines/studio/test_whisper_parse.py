"""Unit tests for pipeline.parse_whisper_json (Hindi split-character handling). Run: python test_whisper_parse.py"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
import pipeline  # noqa: E402


def build(pieces: list[bytes]) -> bytes:
    """Fake whisper.cpp JSON; texts are raw bytes (may be invalid UTF-8, like the real output)."""
    items = []
    for i, p in enumerate(pieces):
        items.append(b'{"offsets":{"from":%d,"to":%d},"text":"%s"}' % (i * 100, i * 100 + 90, p))
    return b'{"transcription":[' + b",".join(items) + b"]}"


TA = "त".encode()          # e0 a4 a4
O = "ो".encode()           # e0 a5 8b
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got}")


# 1. letter split across two pieces -> rejoined into one word, start time of first piece
w = pipeline.parse_whisper_json(build([b" " + TA[:2], TA[2:] + O, b" hi"]))
check("split letter rejoined", [(x["w"], x["s"]) for x in w], [("तो", 0.0), ("hi", 0.2)])

# 2. rest of the letter DROPPED by whisper (real case) -> half letter discarded, nothing swallowed
w = pipeline.parse_whisper_json(build([b" \xe0\xa4", b"", " तो".encode(), " दोस्तो".encode(), b" end"]))
check("dropped bytes don't swallow the rest", [x["w"] for x in w], ["तो", "दोस्तो", "end"])

# 3. plain English, punctuation piece glued to its word, [BLANK_AUDIO] skipped
w = pipeline.parse_whisper_json(build([b" Hello", b",", b" world", b" [BLANK_AUDIO]"]))
check("english + punctuation glue", [x["w"] for x in w], ["Hello,", "world"])

# 4. any real whisper output from local projects (git-ignored, so skipped on a fresh clone)
import core  # noqa: E402
for real in sorted(core.STORY.glob("*/work/*.whisper.json"))[:3]:
    w = pipeline.parse_whisper_json(real.read_bytes())
    n_items = len(json.loads(real.read_bytes().decode("utf-8", "surrogateescape"))["transcription"])
    longest = max(len(x["w"]) for x in w)
    check(f"real file {real.parent.parent.name}: no giant entry", longest < 25, True)
    check(f"real file {real.parent.parent.name}: no broken chars", any("\ufffd" in x["w"] for x in w), False)
    check(f"real file {real.parent.parent.name}: times in order",
          all(w[i]["s"] <= w[i + 1]["s"] for i in range(len(w) - 1)), True)
    print(f"      {len(w)} words from {n_items} pieces; longest word {longest} chars")

sys.exit(1 if fails else 0)
