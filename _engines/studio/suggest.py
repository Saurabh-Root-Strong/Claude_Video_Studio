"""💬 SUGGESTIONS – after ✨ Auto-edit the user reviews the edited view and types changes in plain words, many scenes at
once:  "scene 6 calm music. scene 5 money sticker and cash sound; scene 3 zoom effect, no transition".

Each request is read like an editor's note:
  * WHICH scene(s):  scene 5 · scene no. 5 · 5th scene · scene five · scenes 2-4 · scene 2, 3 and 6 · first / last scene ·
    all scenes.  A sentence without a scene keeps the scene of the sentence before it ("scene 6 calm music. also fade out").
    "add a sticker in scene 5" (scene after the request) works as well as "scene 5: sticker".
  * WHAT: sticker / emoji · sound effect · music · transition · effect (zoom, shake, flash, glitch, black & white …) ·
    remove / no … – several per scene ("money sticker and cash sound").
  * WHEN: "at 2 s" or "on 'double'" / "when I say double" (the word's real moment, after jump cuts + speed);
    otherwise the scene's key word (the same moment auto-edit picks), else just after the scene starts.
A sticker / sound the user asks for REPLACES what auto-edit put on that scene (their own earlier edits stay). Anything the
studio cannot understand is passed to Claude (the inbox) with its scene, so nothing typed is lost.
Meme sounds (no licence) are only used when the user says "meme" – and the reply warns about publishing.
"""
from __future__ import annotations

import re

import autoedit
import commands
import core
import library
import pipeline
import tracks as tracks_mod
import typedcaps

NUM_WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                          "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
NUM_WORDS.update({"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
                  "ninth": 9, "tenth": 10, "pehla": 1, "pehle": 1, "doosra": 2, "dusra": 2, "teesra": 3, "tisra": 3,
                  "ek": 1, "do": None, "teen": 3, "char": 4, "paanch": 5, "panch": 5, "chhe": 6, "che": None, "saat": 7,
                  "aath": 8, "nau": 9, "das": 10})
NUM_WORDS = {k: v for k, v in NUM_WORDS.items() if v}
_N = r"(\d{1,3}|" + "|".join(sorted(NUM_WORDS, key=len, reverse=True)) + r")"
SCENE_WORD = r"(?:\bscenes?|\bscene\s*no\.?|\bscene\s*number|\bseen|\bscn|सीन)"
SCENE_REFS = [
    ("all", re.compile(r"\b(?:all|every|each|har|sabhi|saare|sare)\s+(?:the\s+)?scenes?\b")),
    ("last", re.compile(r"\b(?:the\s+)?(?:last|final|aakhri|akhri)\s+scene\b")),
    ("range", re.compile(SCENE_WORD + r"\s*#?\s*" + _N + r"\s*(?:-|–|to|se)\s*" + _N + r"\b")),
    ("list", re.compile(SCENE_WORD + r"\s*#?\s*" + _N + r"((?:\s*(?:,|and|&|aur|or)\s*" + _N + r"\b)*)")),
    ("ord", re.compile(r"\b" + _N + r"(?:st|nd|rd|th)?\s+(?:wala\s+|wale\s+)?scene\b")),
]
DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

STICKER_WORDS = [  # descriptor stems -> library sticker id (Fluent Emoji 3D)
    (("money", "paisa", "paise", "rupee", "rupay", "bag of money"), "money_bag"),
    (("cash", "note", "notes", "flying money"), "money_with_wings"),
    (("coin",), "coin"), (("dollar",), "dollar_banknote"), (("bank",), "bank"),
    (("fire", "aag", "lit"), "fire"), (("rocket", "moon", "to the moon"), "rocket"),
    (("up", "growth", "profit", "gain", "chart", "graph", "bull", "upar", "up arrow"), "chart_increasing"),
    (("down", "loss", "crash", "fall", "bear", "nuksan"), "chart_decreasing"),
    (("funny", "laugh", "haha", "lol", "hasna", "comedy"), "face_with_tears_of_joy"),
    (("shock", "shocked", "wow", "surprise", "omg", "amazed"), "astonished_face"),
    (("scared", "fear", "scream", "dar"), "face_screaming_in_fear"),
    (("mind blown", "mindblown", "blown", "explode"), "exploding_head"),
    (("thinking", "think", "question", "sochna", "soch", "confused"), "thinking_face"),
    (("question mark",), "red_question_mark"),
    (("idea", "tip", "bulb", "light bulb"), "light_bulb"), (("brain", "smart", "dimag"), "brain"),
    (("warning", "danger", "careful", "alert", "savdhan", "khatra", "caution"), "warning"),
    (("stop",), "stop_sign"),
    (("thumbs", "thumb", "thumbs up", "like button"), "thumbs_up"),
    (("heart", "love", "pyaar"), "red_heart"), (("clap", "applause", "taali", "tali"), "clapping_hands"),
    (("party", "celebrate", "celebration", "congrats", "badhai"), "party_popper"),
    (("bell", "subscribe", "notification"), "bell"),
    (("follow", "arrow", "point down", "pointing"), "backhand_index_pointing_down"),
    (("100", "hundred", "perfect"), "hundred_points"), (("star",), "star"), (("sparkle", "shine", "magic"), "sparkles"),
    (("tick", "check", "correct", "sahi", "check mark"), "check_mark_button"),
    (("cross", "wrong", "galat"), "cross_mark"),
    (("hourglass", "wait", "patience", "hold", "sabr", "timer"), "hourglass_done"),
    (("sad", "cry", "crying", "rona", "dukh"), "crying_face"),
    (("cool", "sunglasses", "swag"), "smiling_face_with_sunglasses"),
    (("angry", "gussa", "anger", "mad"), "pouting_face"),
    (("eyes", "dekho", "notice", "attention"), "eyes"), (("target", "goal", "aim"), "bullseye"),
    (("diamond", "gem", "valuable"), "gem_stone"), (("crown", "king", "raja"), "crown"),
    (("lightning", "energy", "power", "electric"), "high_voltage"), (("trophy", "win", "winner", "jeet"), "trophy"),
    (("dead", "skull", "rip"), "skull"), (("megaphone", "announce", "announcement"), "megaphone"),
    (("lock", "locked", "safe", "secure"), "locked"),
]
STICKER_CAT = {"money_bag": "money", "money_with_wings": "money", "coin": "money", "dollar_banknote": "money",
               "chart_increasing": "up", "rocket": "up", "fire": "up", "high_voltage": "up", "trophy": "up",
               "chart_decreasing": "down", "face_screaming_in_fear": "down", "cross_mark": "down",
               "thinking_face": "question", "red_question_mark": "question", "eyes": "question",
               "light_bulb": "idea", "brain": "idea", "bullseye": "idea", "warning": "warning", "stop_sign": "warning",
               "bell": "ipo", "party_popper": "ipo", "megaphone": "ipo", "hourglass_done": "hold", "gem_stone": "hold",
               "locked": "hold", "backhand_index_pointing_down": "cta", "thumbs_up": "cta"}
SOUND_WORDS = [  # descriptor stems -> library sound id (CC0 SFX Kit)
    (("cash register", "cha ching", "ka ching", "kaching", "register", "cash", "money", "paisa"), "money-register"),
    (("coin",), "money-coin-drop"),
    (("whoosh", "woosh", "swoosh", "swish", "swipe"), "whoosh-fast-soft"),
    (("pop", "bubble"), "pop-medium-in"),
    (("ding", "bell", "ting"), "notification-success-ding-ding"),
    (("success", "win", "correct", "positive", "chime"), "notification-success-chime"),
    (("fail", "failure", "wrong", "error", "negative", "loss", "sad"), "notification-failure-fall"),
    (("notification", "follow", "subscribe", "like button", "bell icon", "follow button"), "notification-success-ding-ding"),
    (("alert", "warning", "alarm"), "notification-alert-clear"),
    (("boom", "bass drop", "drop", "bass"), "boom-drop-medium"),
    (("impact", "hit", "punch", "thud", "slam"), "impact-medium-firm"),
    (("dramatic", "cinematic", "drum", "suspense", "epic"), "cinematic-drum-big"),
    (("thunder",), "cinematic-thunder-big"),
    (("riser", "build up", "buildup", "tension", "rise"), "riser-1s-soft"),
    (("click", "tap", "mouse"), "click-mouse-medium"), (("typing", "keyboard"), "keyboard-letter-medium"),
    (("clap", "applause"), "click-clap-medium"),
    (("question", "doubt", "hmm"), "tone-ding-mid"),
]
TRANS_WORDS = [(("slide left", "slide"), "slideleft"), (("slide right",), "slideright"), (("slide up",), "slideup"),
               (("smooth",), "smoothleft"), (("fade to black", "black"), "fadeblack"), (("white",), "fadewhite"),
               (("fade", "dissolve", "soft"), "fade"), (("zoom",), "zoomin"), (("wipe",), "wipeleft"),
               (("circle",), "circleopen"), (("glitch", "pixel"), "pixelize"), (("blur",), "hblur"),
               (("wind",), "hlwind"), (("cover",), "coverleft"), (("reveal",), "revealleft")]
EFFECT_WORDS = [(("black and white", "black & white", "b&w", "bw", "grayscale", "greyscale"), "bw"),
                (("slow zoom", "ken burns", "slowly zoom", "zoom slowly"), "slowzoom"),
                (("zoom out",), "zoomout"), (("zoom in", "zoom", "punch in", "punch"), "punchin"),
                (("shake", "camera shake", "earthquake", "jhatka"), "shake"), (("flash",), "flash"),
                (("glitch",), "glitch"), (("rgb", "chromatic"), "rgbsplit"), (("invert", "negative"), "invertflash"),
                (("vintage", "retro", "old film", "film"), "vintage"), (("cinematic", "movie"), "cinematic"),
                (("vignette",), "vignette"), (("warm",), "warm"), (("cool", "cold", "blue tone"), "cool"),
                (("punchy", "vibrant", "colourful", "colorful", "saturate"), "punchy"),
                (("fade in",), "fadein"), (("fade out", "fadeout", "end fade"), "fadeout")]
MUSIC_MOODS = {
    "calm": ("calm", "soft", "chill", "relax", "peace", "ambient", "slow", "lofi", "lo-fi", "piano", "gentle"),
    "upbeat": ("upbeat", "happy", "energetic", "energy", "fast", "fun", "positive", "bright", "dance", "party", "hype"),
    "dramatic": ("dramatic", "suspense", "tension", "epic", "cinematic", "dark", "thriller", "intense"),
    "sad": ("sad", "emotional", "emotion", "melancholy", "dukh"),
    "inspiring": ("inspiring", "inspirational", "motivational", "motivation", "corporate", "uplifting", "success"),
    "funky": ("funky", "funk", "groovy", "hiphop", "hip hop", "trap", "beat", "rap"),
}
STOP = {"i", "want", "need", "the", "a", "an", "in", "on", "for", "to", "of", "and", "aur", "me", "mein", "mai", "main",
        "pe", "par", "ko", "ka", "ki", "ke", "add", "put", "use", "give", "lagao", "laga", "daalo", "dalo", "dal",
        "chahiye", "chaiye", "please", "pls", "plz", "some", "type", "kind", "like", "this", "that", "is", "be", "with",
        "here", "there", "also", "bhi", "one", "ek", "should", "can", "you", "we", "it", "its", "my", "our", "show",
        "make", "do", "karo", "kar", "de", "do", "wala", "wali", "wale", "sa", "si", "se", "ho", "hai", "type of",
        "kind of", "little", "bit", "more", "very", "thoda", "jyada", "zyada", "big", "small", "large"}
_TIME = r"([\d]+(?:[.,]\d+)?)\s*(?:s|sec|secs|second|seconds|sekand)?\b"


# ----------------------------------------------------------------------------------------------- reading the note
def _num_of(tok: str) -> int | None:
    tok = tok.strip().lower()
    if tok.isdigit():
        return int(tok)
    return NUM_WORDS.get(tok)


def _scene_refs(t: str, n: int) -> list[tuple[int, int, list[int]]]:
    """[(start, end, [scene numbers 1-based])] for every scene mention in t (no overlaps, in order)."""
    found, taken = [], []
    for kind, rx in SCENE_REFS:
        for m in rx.finditer(t):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            if kind == "all":
                nums = list(range(1, n + 1))
            elif kind == "last":
                nums = [n]
            elif kind == "range":
                a, b = _num_of(m.group(1)), _num_of(m.group(2))
                nums = list(range(min(a, b), max(a, b) + 1)) if a and b else []
            elif kind == "list":
                nums = [_num_of(m.group(1))] + [_num_of(x) for x in re.findall(_N, m.group(2) or "")]
            else:
                nums = [_num_of(m.group(1))]
            nums = [x for x in nums if x]
            if nums:
                found.append((m.start(), m.end(), nums))
                taken.append((m.start(), m.end()))
    return sorted(found)


def _meaningful(s: str) -> bool:
    return any(w not in STOP for w in re.findall(r"[\w&']+", s))


def norm_cased(text: str) -> str:
    """The note with the parser's clean-ups but its own capitals (titles, file names, #COLOURS stay as typed)."""
    t = text.translate(DEV_DIGITS).replace("’", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"[ \t\r\f\v]+", " ", t)  # keep the line breaks: each line is its own request
    return re.sub(r"\b(no|num|nr)\.\s*(?=\d)", r"\1 ", t, flags=re.I)  # "scene no. 5" is not the end of a sentence


def parse(text: str, n_scenes: int, default: list[int] | None = None) -> list[tuple[list[int], str]]:
    """Split a note into (scene numbers, request) pairs. Scenes may be [] = the whole video / no scene named."""
    t = norm_cased(text).lower()
    out, cur = [], list(default or [])
    sents = []  # (new line?, sentence): a NEW LINE without a scene is a whole-video request; a sentence inside a
    for line in t.split("\n"):  # line without one belongs to that line's scene ("scene 6 calm music. also fade out")
        m_t = re.search(r"\b(?:text|card|headline|title)\s*[:\-]", line)
        parts = ([line[:m_t.start()].strip(" ,;"), line[m_t.start():].strip()] if m_t else
                 re.split(r";+|\.(?=\s|$)", line))  # from "text:" on, the rest of the line is the title, as typed
        if m_t and not parts[0]:
            parts = parts[1:]
        elif m_t and _scene_refs(parts[0], n_scenes) and not _meaningful(re.sub(SCENE_WORD + r"\s*#?\s*" + _N, " ", parts[0])):
            parts = [parts[0] + " " + parts[1]]  # "scene 2 text: …": keep the scene with its title
        for k, s in enumerate(x.strip(" ,") for x in parts if x.strip(" ,.")):
            sents.append((k == 0, s))
    for new_line, sent in sents:
        if new_line:
            cur = list(default or [])
        refs = _scene_refs(sent, n_scenes)
        if not refs:
            if _meaningful(sent):
                out.append((cur, sent))
            continue
        pre = sent[:refs[0][0]]
        if _meaningful(pre):  # "add a money sticker in scene 5 and a cash sound in scene 6": each scene takes the text BEFORE it
            last = 0
            for a, b, nums in refs:
                piece = sent[last:a]
                if _meaningful(piece):
                    out.append((nums, piece.strip(" ,:-")))
                elif out and out[-1][1]:  # "money sticker in scene 5 and 6" style leftovers: same request
                    out.append((nums, out[-1][1]))
                cur, last = nums, b
            tail = sent[last:]
            if _meaningful(tail):
                out.append((cur, tail.strip(" ,:-")))
        else:  # "scene 6: calm music, scene 5: money sticker": each scene takes the text AFTER it
            for k, (a, b, nums) in enumerate(refs):
                piece = sent[b: refs[k + 1][0] if k + 1 < len(refs) else len(sent)]
                cur = nums
                if _meaningful(piece):
                    out.append((nums, piece.strip(" ,:-")))
    return out


# how people talk to the editor, not edits: removed before reading the note ("…, deep think")
FILLER_RX = re.compile(r"\b(?:very\s+)?(?:deep(?:ly)?\s+think(?:ing)?|think\s+deep(?:ly)?|deep\s+dive)\b|"
                       r"\b(?:think\s+)?as\s+(?:the\s+|a\s+)?(?:world'?s?\s+)?best\b[^.,;\n]*|"
                       r"\b(?:accordingly|properly|nicely|asap)\b", re.I)
# words that make a piece a REQUEST; a piece without one ("when page ko follow karna mat bhulna") is context
ACTION_RX = re.compile(r"\b(?:stickers?|emojis?|icons?|sounds?|sfx|awaa?z|aawaz|music|song|bgm|gaana|gana|transitions?|"
                       r"effects?|fx|filters?|zoom\w*|shake|flash|glitch|text|card|title|headline|remove|delete|hata\w*|no|"
                       r"mute|unmute|speed|captions?|fade\w*|black|vintage|b-?roll|cut|keep|split|slow\w*|volume|louder|"
                       r"quieter|background|logo)\b", re.I)
HI_FILLER = {"karna", "karo", "karte", "mat", "bhulna", "bhoolna", "nahi", "hai", "hain", "tha", "thi", "wala", "wali",
             "page", "video", "bolo", "bolna", "bole", "jab", "tab", "phir", "abhi", "yahan", "wahan", "kuch", "sab"}
CONTEXT_START = re.compile(r"^\s*(?:when|whenever|where|while|jab|jab\s+bhi|jahan|jaha)\b", re.I)


def _atoms(req: str) -> list[str]:
    """'money sticker and cash sound, no transition' -> ['money sticker', 'cash sound', 'no transition']."""
    if re.match(r"\s*(?:text|card|headline|title)\b\s*[:\-]", req):
        return [req.strip()]  # words of a title are not separate requests
    r = req
    for p in ("black and white", "black & white", "rock and roll", "hip hop"):
        r = r.replace(p, p.replace(" and ", "_and_").replace(" & ", "_&_").replace(" ", "_"))
    parts = re.split(r"\s*(?:,|\+|\band\b|\baur\b|\bplus\b|\balso\b|\bthen\b|\bwith\b|&)\s*", r)
    return [p.replace("_", " ").strip() for p in parts if _meaningful(p.replace("_", " "))]


def _pick(words: list[tuple[tuple[str, ...], str]], s: str) -> str | None:
    best = None
    for stems, val in words:
        for st in stems:
            if re.search(r"(?<![\w])" + re.escape(st) + r"(?![\w])", s) and (best is None or len(st) > best[0]):
                best = (len(st), val)
    return best[1] if best else None


def _content(s: str, drop: tuple[str, ...]) -> list[str]:
    return [w for w in re.findall(r"[\w']+", s) if w not in STOP and w not in drop and not re.fullmatch(r"\d+(?:[.,]\d+)?|s|sec", w)]


# ------------------------------------------------------------------------------------------------- scene timing
class SceneTime:
    """A scene's words and the mapping from a source moment to the scene's OUTPUT timeline (jump cuts + speed)."""

    def __init__(self, pid: str, data: dict, sc: dict):
        self.sc = sc
        words = core.read_json(core.work_file(pid, sc["src"], "words.json"), []) or []
        self.words = pipeline.words_in(words, sc["in"], sc["out"])
        sil = pipeline.load_silences(pid, sc["src"])
        self.ranges = pipeline.kept_ranges(sc, sil, data["settings"])
        self.speed = float(sc.get("speed") or 1.0)
        self.out_len = pipeline.scene_out_len(sc, sil, data["settings"])

    def at_word(self, w: dict) -> float | None:
        t = pipeline._map_time(max(0.0, float(w["s"]) - self.sc["in"]), self.ranges, self.speed)
        return None if t is None else round(min(max(0.0, t), max(0.0, self.out_len - 0.2)), 2)

    def find_word(self, word: str) -> dict | None:
        k = autoedit._norm(word)
        if len(k) < 2:
            return None
        return next((w for w in self.words if autoedit._norm(w["w"]) == k), None) or \
            next((w for w in self.words if autoedit._norm(w["w"]).startswith(k)), None)

    def key_moment(self, data: dict, idx: int, n: int) -> tuple[float, str]:
        hit = autoedit._hit(self.words, idx == 0, idx == n - 1) if self.words else None
        w = hit[1] if hit else (autoedit._emphasis(self.words) if self.words else None)
        t = self.at_word(w) if w else None
        if t is None:
            return round(min(0.3, max(0.0, self.out_len - 1.0)), 2), "near the start"
        return t, f"on “{autoedit._norm(w['w'])}”"


def _when(atom: str, tm: SceneTime) -> tuple[float | None, str, str]:
    """('at 2 s' | "on 'double'" | 'when I say double') -> (time, how, atom without the timing words)."""
    m = re.search(r"\bat\s+" + _TIME, atom)
    if m:
        v = float(m.group(1).replace(",", "."))
        return round(min(max(0.0, v), max(0.0, tm.out_len - 0.3)), 2), f"at {v:g} s", (atom[:m.start()] + atom[m.end():]).strip()
    m = re.search(r"(?:\bon|\bat|\bwhen (?:i|he|she) (?:say|says|said)|\bjab .{0,12}?bol\w*)\s*[\"']([^\"']+)[\"']", atom) or \
        re.search(r"\bwhen (?:i|he|she|you) (?:say|says|said|speak|speaks) (\w+)", atom) or \
        re.search(r"\b(?:jab|jaise\s+hi)\s+(?:mai\s+|main\s+|me\s+)?(\w+)\s+(?:bol\w*|kah\w*|aay\w*|aat\w*)\b", atom) or \
        re.search(r"[\"']([^\"']+)[\"']", atom)  # Hindi too: "jab double bole" / "jaise hi follow bolu"
    if m:
        w = tm.find_word(m.group(1).split()[0])
        rest = (atom[:m.start()] + atom[m.end():]).strip()
        if w:
            return tm.at_word(w), f"on “{m.group(1)}”", rest
        return None, f"(“{m.group(1)}” isn't said in this scene – placed on its key word)", rest
    return None, "", atom


# ----------------------------------------------------------------------------------------------------- actions
def _w(how: str, at: float) -> str:
    return how if how.startswith("at ") else f"{how} ({at:g} s)"


def _sound_entry(it: dict, at: float, vol: float) -> dict:
    return dict(commands._sound_ref(it), at=round(max(0.0, at), 2), vol=round(vol, 2))


def _replace_auto(sc: dict, key: str) -> int:
    lst = sc.get(key) or []
    keep = [x for x in lst if not (isinstance(x, dict) and x.get("auto"))]
    if keep:
        sc[key] = keep
    else:
        sc.pop(key, None)
    return len(lst) - len(keep)


def _do_sticker(data, sc, tm, atom, idx, n, sound_too: bool) -> str:
    at, how, rest = _when(atom, tm)
    look = re.sub(r"\b(?:top|bottom|center|centre|middle)(?:[ -](?:left|right))?\b|\b(?:left|right)\b|\bpop[ -]?up\b|"
                  r"\b(?:big|large|bada|badi|small|chhota|chota|tiny)\b", " ", rest)
    sid = _pick(STICKER_WORDS, look)
    it = library.find("sticker", sid, exact=True) if sid else None
    if not it:  # a sticker by its own name: "rocket", "red heart", "cat"
        for w in sorted(_content(look, ("sticker", "stickers", "emoji", "emojis", "icon", "pop", "up")), key=len, reverse=True):
            it = library.find("sticker", w)
            if it:
                break
    if not it:
        hit = autoedit._hit(tm.words, idx == 0, idx == n - 1) if tm.words else None
        cat = hit[0] if hit else "idea"
        it = next((library.find("sticker", x, exact=True) for x in autoedit.LOOK[cat][0]
                   if library.find("sticker", x, exact=True)), None)
        if not it:
            return "!No stickers in the Library yet – run Library setup."
        how_pick = f" (no exact match – picked one for {autoedit.WHY[cat]})"
    else:
        how_pick = ""
    if at is None:
        at, how2 = tm.key_moment(data, idx, n)
        how = (how + " " if how.startswith("(") else "") + how2
    dur = round(min(1.8, max(0.7, tm.out_len - at - 0.1)), 2)
    gone = _replace_auto(sc, "stickers")
    if gone:
        _replace_auto(sc, "sfx")  # auto's sound went with its sticker
    lst = sc.setdefault("stickers", [])
    if any(x.get("file") == it["file"] and abs(float(x.get("at") or 0) - at) < 0.3 for x in lst if isinstance(x, dict)):
        return f"{it['name']} sticker is already there {_w(how, at)}."
    if len(lst) >= commands.MAX_STICKERS:
        return f"!At most {commands.MAX_STICKERS} stickers per scene – remove one first."
    split = data["settings"].get("layout") == "split"
    pos = "bottom-right" if split else "top-right"
    m = re.search(r"\b(top|bottom|center|centre|middle)(?:[ -](left|right))?\b", rest)
    if m:
        pos = "center" if m.group(1) in ("center", "centre", "middle") else f"{m.group(1)}-{m.group(2) or 'right'}"
        pos = pos if pos in library.STICKER_POS else "top-right"
    big = re.search(r"\b(big|large|bada|badi)\b", rest)
    small = re.search(r"\b(small|chhota|chota|tiny)\b", rest)
    lst.append({"file": it["file"], "name": it["name"], "at": at, "dur": dur, "pos": pos,
                "size": 0.32 if big else 0.16 if small else 0.24, "anim": "pop"})
    extra = ""
    if sound_too:
        s = library.sticker_sound(it, how)
        if s and len(sc.get("sfx") or []) < commands.MAX_SFX:
            # "pair": chosen FOR the sticker – a sound the user names later in the note replaces it (no double hit)
            sc.setdefault("sfx", []).append({"file": s["file"], "name": s["name"], "sync": s["sync"], "gain_db": s["gain_db"],
                                             "at": round(at, 2), "vol": s["vol"], "pair": True})
            extra = f" + “{s['name']}” sound"
    return f"{it['name']} sticker {_w(how, at)}{extra}{how_pick}" + (" – replaces auto-edit's sticker" if gone else "") + "."


def _do_sound(data, sc, tm, atom, idx, n) -> str:
    at, how, rest = _when(atom, tm)
    meme = bool(re.search(r"\bmemes?\b", rest))
    it = None
    if meme:
        risky = [x for x in library.catalog()["sounds"] if x.get("risky")]
        want = set(_content(rest, ("sound", "sounds", "meme", "memes", "sfx", "effect", "awaz", "aawaz")))
        scored = sorted(((len(want & set(library.slug(x["name"]).split("_"))), -len(x["name"]), x) for x in risky),
                        key=lambda z: (z[0], z[1]), reverse=True)
        if scored and scored[0][0]:
            it = scored[0][2]
        if not it and risky:
            return "!Which meme sound? Name it, e.g. “bruh meme sound”, “vine boom meme sound” (Library → Sounds → Meme sounds)."
    if not it:
        s_id = _pick(SOUND_WORDS, rest)
        it = library.find("sound", s_id, exact=True) if s_id else None
    if not it:
        for w in sorted(_content(rest, ("sound", "sounds", "sfx", "effect", "effects", "awaz", "aawaz", "awaaz")), key=len, reverse=True):
            it = library.find("sound", w)
            if it and it.get("risky"):
                it = None
            if it:
                break
    if not it:
        return None  # not understood as a sound
    if at is None:
        st = sc.get("stickers") or []
        if st:  # a sound goes with the scene's sticker moment
            at, how = st[-1]["at"], "with the sticker"
        else:
            at, how2 = tm.key_moment(data, idx, n)
            how = (how + " " if how.startswith("(") else "") + how2
    gone = _replace_auto(sc, "sfx")
    paired = [x for x in sc.get("sfx") or [] if isinstance(x, dict) and x.get("pair")]
    if paired:  # the user names the sound: the one picked to go with their sticker steps aside
        sc["sfx"] = [x for x in sc["sfx"] if x not in paired]
        gone = gone or -1
    lst = sc.setdefault("sfx", [])
    if len(lst) >= commands.MAX_SFX:
        return f"!At most {commands.MAX_SFX} sound effects per scene – remove one first."
    vol = 0.9 if re.search(r"\b(loud|louder|tez|zor)\b", rest) else 0.35 if re.search(r"\b(soft|low|quiet|halka|dheere)\b", rest) else 0.6
    lst.append(_sound_entry(it, at, vol))
    warn = " ⚠ meme sound: no licence – fine to try, risky to publish." if it.get("risky") else ""
    return f"“{it['name']}” sound {_w(how, at)}" + (" – replaces the sound picked for the sticker" if gone == -1 else " – replaces auto-edit's sound" if gone else "") + "." + warn


def _is_music(i: dict) -> bool:
    src = i.get("src") or {}
    return bool(i.get("music")) or src.get("t") == "music" or str(src.get("n") or "").startswith("Music/")


def music_files() -> list[tuple[dict, str]]:
    """[(track src ref, searchable name)] – Brand Kit/music and Library/Music."""
    out = []
    bk = core.BRAND / "music"
    if bk.is_dir():
        out += [({"t": "music", "n": p.name}, p.stem.lower()) for p in sorted(bk.iterdir())
                if p.suffix.lower() in core.AUDIO_EXT and p.is_file()]
    lm = library.LIB / "Music"
    if lm.is_dir():
        out += [({"t": "lib", "n": p.relative_to(library.LIB).as_posix()}, p.stem.lower()) for p in sorted(lm.rglob("*"))
                if p.suffix.lower() in core.AUDIO_EXT and p.is_file()]
    return out


def _do_music(pid, data, sc, tm, atom) -> str:
    if re.search(r"\b(no|remove|delete|hata\w*|off|band|stop)\b", atom):
        n = 0
        for t in tracks_mod.tracks_of(data):
            if t["kind"] == "audio" and not t.get("locked"):
                keep = [i for i in t["items"] if not (i.get("anchor") == sc["id"] and _is_music(i))]
                n += len(t["items"]) - len(keep)
                t["items"] = keep
        return "Music removed from this scene." if n else "!This scene has no music of its own" + \
            (" (the background music for the whole video is set on the Look / music setting – type “music off” (no scene) to switch it off)."
             if (data["settings"].get("music") or "off") != "off" else ".")
    files = music_files()
    if not files:
        return ("!No music files yet – put MP3s in “Brand Kit/music” or “Library/Music”, named by mood "
                "(calm_piano.mp3, upbeat_beat.mp3, dramatic_drums.mp3 …), then send this again.")
    mood = next((m for m, ws in MUSIC_MOODS.items() if any(re.search(r"\b" + re.escape(w) + r"\b", atom) for w in ws)), None)
    words = _content(atom, ("music", "song", "bgm", "gaana", "gana", "background", "track"))
    pick, why = None, ""
    for ref, name in files:  # a file named in the note ("use piano_dreams")
        if any(len(w) >= 4 and w in name for w in words):
            pick = ref
            break
    if not pick and mood:
        pick = next((ref for ref, name in files if any(w.replace(" ", "") in name.replace("_", "").replace("-", "") for w in MUSIC_MOODS[mood])), None)
        if not pick:
            why = f" (no “{mood}” music file found – name a file with “{mood}” in it to get that mood)"
    if not pick:
        pick = files[0][0]
    path = tracks_mod.media_path(pid, data, pick)
    try:
        ml = pipeline.music_lufs(path)
    except Exception:  # noqa: BLE001
        ml = -14.0
    voice = float(data["settings"].get("loudness") or -16.0)
    db = (voice - 18.0) - ml  # music 18 LU under the voice: a bed, words stay clear
    vol = round(min(1.0, max(0.05, 10 ** (db / 20))), 3)
    tr = next((t for t in tracks_mod.tracks_of(data) if t["kind"] == "audio" and t.get("name") == "Music" and not t.get("locked")), None)
    if tr is None:
        r, err, x = tracks_mod.apply_op(pid, data, {"op": "add", "kind": "audio"})
        if err:
            return "!" + err
        tr = tracks_mod._find(data, x["track"])
        tr["name"] = "Music"
    tr["items"] = [i for i in tr["items"] if not (i.get("anchor") == sc["id"] and _is_music(i))]
    item, err = tracks_mod._clean_item(pid, data, "audio", {"anchor": sc["id"], "off": 0.0, "dur": max(0.5, tm.out_len),
                                                             "src": pick, "in": 0.0, "vol": vol,
                                                             "fade_in": 0.4, "fade_out": 0.6})
    if err:
        return "!" + err
    item["music"] = True
    tr["items"].append(item)
    bed = (data["settings"].get("music") or "off") != "off" or str(data["settings"].get("ref_audio") or "none") != "none"
    return (f"Music “{pick['n']}” under this scene ({tm.out_len:.1f} s, soft under the voice, fades in/out){why}."
            + (" Note: whole-video background music (Music / reference background audio) is also on – switch it off in the look if the two clash." if bed else ""))


def _do_remove(data, sc, atom) -> str | None:
    if not re.search(r"\b(no|remove|delete|hata\w*|without|nahi|mat|off|clear)\b", atom):
        return None
    if re.search(r"\bstickers?|emojis?\b", atom):
        return commands._library_scene_cmd(data, sc, "stickers off")
    if re.search(r"\b(sound effects?|sfx|sounds?|awaa?z)\b", atom):
        return commands._library_scene_cmd(data, sc, "sfx off")
    if re.search(r"\btransitions?\b", atom):
        return commands._library_scene_cmd(data, sc, "transition off")
    if re.search(r"\bzoom", atom):
        had = bool(sc.get("punches")) or any(e in (sc.get("effects") or []) for e in ("punchin", "slowzoom", "zoomout"))
        sc.pop("punches", None)
        sc.pop("auto_punch", None)
        if sc.get("effects"):
            sc["effects"] = [e for e in sc["effects"] if e not in ("punchin", "slowzoom", "zoomout")] or None
            if not sc["effects"]:
                sc.pop("effects")
        return "Zoom removed." if had else "!This scene has no zoom."
    if re.search(r"\b(effects?|fx|filters?)\b", atom):
        eid = _pick(EFFECT_WORDS, atom)
        return commands._library_scene_cmd(data, sc, f"effect remove {eid}" if eid else "effects off")
    return None


def do_atom(pid: str, data: dict, sc: dict, atom: str, idx: int, n: int, clause: str, raw: str | None = None) -> str | None:
    """One request on one scene -> reply ('!…' = refused), or None = not understood."""
    tm = SceneTime(pid, data, sc)
    a = atom.strip()
    if re.search(r"\b(music|song|bgm|gaana|gana)\b", a):
        return _do_music(pid, data, sc, tm, a)
    r = _do_remove(data, sc, a)
    if r is not None:
        if r.startswith("!") and re.search(r"has no ", r):
            return "Already none here – nothing to remove."
        return r
    if re.search(r"\b(stickers?|emojis?|emoticons?|icons?)\b", a):
        wants_sound = not re.search(r"\b(sound|sfx|awaa?z|music)\b", clause) and \
            not re.search(r"\b(silent|no sound|without sound)\b", clause)
        return _do_sticker(data, sc, tm, a, idx, n, wants_sound)
    if re.search(r"\b(sound|sfx|awaa?z|aawaz|sound effect)\b", a) or (_pick(SOUND_WORDS, a) and not _pick(EFFECT_WORDS, a)
                                                                       and not re.search(r"\b(transition|effect|fx)\b", a)):
        r = _do_sound(data, sc, tm, a, idx, n)
        if r is not None:
            return r
    if re.search(r"\btransitions?\b", a):
        tid = commands._trans_id(re.sub(r".*?\btransitions?\b\s*", "", a).strip() or "x") or _pick(TRANS_WORDS, a) or "smoothleft"
        return commands._library_scene_cmd(data, sc, f"transition {tid}")
    eid = _pick(EFFECT_WORDS, a)
    if eid:
        if eid == "punchin" and tm.words and not re.search(r"\b(whole|full|entire|poora|pura)\b", a):
            # "zoom on scene 3": a punch-in moment on its key word (the real-editor move), not a whole-scene zoom
            at, how, _ = _when(a, tm)
            w = None
            if at is None:
                hit = autoedit._hit(tm.words, idx == 0, idx == n - 1)
                w = hit[1] if hit else autoedit._emphasis(tm.words)
            else:
                w = min(tm.words, key=lambda x: abs((tm.at_word(x) or 0) - at))
            if w:
                s0, e0 = max(sc["in"], float(w["s"]) - 0.1), min(sc["out"], float(w["e"]) + 0.5)
                if e0 - s0 >= 0.3:
                    sc["punches"] = sorted([p for p in (sc.get("punches") or []) if not (p[0] < e0 and s0 < p[1])]
                                           + [[round(s0, 3), round(e0, 3), 1.18]])
                    return f"Zoom-in moment on “{autoedit._norm(w['w'])}”."
        return commands._library_scene_cmd(data, sc, f"effect {eid}")
    # the studio's own commands (speed 1.2x, mute, captions off, text: …, card: …)
    replies, unhandled, _ = commands.apply(data, sc["id"], raw or a)  # as typed: "text: Big News" keeps its capitals
    if replies and not unhandled:
        return " ".join(replies)
    return None


def apply(pid: str, data: dict, text: str, scene_id: str | None = None) -> dict:
    """Run a whole suggestion note (call inside mutate, after a snapshot).
    Returns {"results": [{"scene": n|None, "text", "reply", "ok": True|False|None(=sent to Claude)}], "changed", "queued": [...]}."""
    vid = [s for s in data["scenes"] if s.get("keep", True) and s.get("role") != "broll"]
    n_all = len(data["scenes"])
    default = [data["scenes"].index(core.scene_by_id(data, scene_id)) + 1] if scene_id and core.scene_by_id(data, scene_id) else []
    results, queued, changed = [], [], False
    # a pasted caption script ("your captions aren't clear, use these: [0:00] So, gold …") is ONE request: show
    # those words as the captions, timed to the voice – never 30 lines each read as a separate edit
    if typedcaps.RESET.match(text):
        r = typedcaps.reset(data)
        return {"results": [{"scene": None, "text": text.strip(), "reply": r, "ok": True}], "changed": "again" in r, "queued": []}
    script = typedcaps.split_script(text)
    if script:
        label = f"your caption text ({sum(len(l.split()) for _, l in script)} words, {len(script)} lines)"
        try:
            r = typedcaps.apply_script(pid, data, script)
            return {"results": [{"scene": None, "text": label, "reply": r, "ok": True}], "changed": True, "queued": []}
        except ValueError as e:
            return {"results": [{"scene": None, "text": label, "reply": str(e), "ok": False}], "changed": False, "queued": []}
    text = FILLER_RX.sub(" ", text)
    if not text.strip(" ,.;\n"):
        return {"results": [], "changed": False, "queued": []}
    cased = norm_cased(text)
    low = cased.lower()

    def orig(piece: str) -> str:  # the request with the capitals the user typed
        i = low.find(piece)
        return cased[i:i + len(piece)] if i >= 0 and len(low) == len(cased) else piece
    for nums, req in parse(text, n_all, default):
        if not nums:  # no scene named: a whole-video change, else Claude
            req = orig(req)
            replies, unhandled, ch = commands.apply(data, None, req)
            changed |= ch
            for r in replies:
                results.append({"scene": None, "text": req, "reply": r.lstrip("!"), "ok": not r.startswith("!")})
            if unhandled:
                queued.append((None, req))
                results.append({"scene": None, "text": req, "reply": "Sent to Claude (no scene named and not a studio command).", "ok": None})
            continue
        for k in nums:
            if not 1 <= k <= n_all:
                results.append({"scene": k, "text": req, "reply": f"There is no scene {k} (1–{n_all}).", "ok": False})
                continue
            sc = data["scenes"][k - 1]
            if sc not in vid:
                results.append({"scene": k, "text": req, "reply": f"Scene {k} is cut / B-roll – it isn't in the video.", "ok": False})
                continue
            idx = vid.index(sc)
            atoms = _atoms(req) or [req]
            # context pieces ("when page ko follow karna mat bhulna") say WHEN, not WHAT: the first word of theirs that
            # is really spoken in this scene times the other requests of the same note
            ctx = [a for a in atoms if len(atoms) > 1 and (CONTEXT_START.match(a) or not ACTION_RX.search(a))]
            hint = None
            if ctx:
                tm_ = SceneTime(pid, data, sc)
                ask = " ".join(a for a in atoms if a not in ctx)
                cands = [wd for a in ctx for wd in _content(CONTEXT_START.sub("", a), ())
                         if len(wd) >= 3 and wd not in HI_FILLER and tm_.find_word(wd)]
                # a word in BOTH the context and the request ("follow") wins, then the longest one
                cands.sort(key=lambda wd: (not re.search(r"\b" + re.escape(wd) + r"\b", ask), -len(wd)))
                hint = cands[0] if cands else None
            for atom in atoms:
                if atom in ctx:
                    results.append({"scene": k, "text": atom, "ok": True, "reply":
                                    f"read as context – timing the rest on “{hint}”." if hint else "read as context (not a change on its own)."})
                    continue
                a2 = atom
                if hint and not re.search(r"\bat\s+\d|[\"']|\bwhen\b", atom):
                    a2 = f"{atom} on '{hint}'"
                try:
                    r = do_atom(pid, data, sc, a2, idx, len(vid), req, orig(atom))
                except Exception as e:  # noqa: BLE001  one odd request must not lose the others
                    r = f"!Could not do that ({str(e)[:80]})."
                if r is None:
                    queued.append((sc["id"], orig(atom)))
                    results.append({"scene": k, "text": atom, "reply": "Sent to Claude (its reply shows here).", "ok": None})
                else:
                    ok = not r.startswith("!")
                    changed |= ok
                    results.append({"scene": k, "text": atom, "reply": r.lstrip("!"), "ok": ok})
    return {"results": results, "changed": changed, "queued": queued}
