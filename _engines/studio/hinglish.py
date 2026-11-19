"""Devanagari -> Hinglish (Hindi written in English letters) for captions.

Offline and rule-based, word by word, so each caption word keeps its own timing and highlight.

Plain letter-by-letter transliteration reads badly ("karana", "sakata"), because written Hindi carries a silent
"a" after most consonants. We drop it the way speakers do (schwa deletion):
  * at the end of a word            करना -> karna, सोच -> soch
  * in the middle, between V+C and C+V   सकता -> sakta, आपके -> aapke, लगभग -> lagbhag
then apply a few readability habits (aa/ee/oo in short closed words: paas, theek, bhool) and a dictionary of
common words whose usual Hinglish spelling the rules can't guess (में -> mein, नहीं -> nahi, मार्केट -> market).
The user can override any word with the `spell X as Y` command (settings["caption_fixes"]).
Text in any other script (Latin "IPO", digits) passes through unchanged.
"""
from __future__ import annotations

import re
import unicodedata

CONS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "f", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "ळ": "l", "व": "v",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
}
NUKTA_CONS = {"क": "k", "ख": "kh", "ग": "g", "ज": "z", "ड": "d", "ढ": "dh", "फ": "f", "य": "y"}  # ladka, padhai
VOWELS = {  # independent vowel letters
    "अ": "a", "आ": "aa", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e", "ऎ": "e", "ऒ": "o",
}
MATRAS = {
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e", "ॆ": "e", "ॊ": "o",
}
LONG = {"ा": "aa", "ी": "ee", "ू": "oo"}  # spelt doubled in short closed words: paas, theek, bhool
HALANT, NUKTA, ANUSVARA, CHANDRA, VISARGA = "्", "़", "ं", "ँ", "ः"
LABIAL = {"p", "f", "b", "bh", "m"}
DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
DEVA = re.compile(r"[ऀ-ॿ]")

# Usual Hinglish spellings the rules can't produce (keys are normalised: no nukta, chandrabindu -> anusvara).
BUILTIN = {
    # everyday Hindi
    "है": "hai", "हैं": "hain", "में": "mein", "मैं": "main", "नहीं": "nahi", "नही": "nahi", "यह": "yeh",
    "वह": "woh", "ये": "ye", "वो": "wo", "और": "aur", "भी": "bhi", "लिए": "liye", "लिये": "liye",
    "दोनों": "dono", "क्यों": "kyun", "क्यूं": "kyun", "कहां": "kahan", "यहां": "yahan", "वहां": "wahan",
    "हूं": "hoon", "हां": "haan", "हम": "hum", "तुम": "tum", "अच्छा": "achha", "अच्छी": "achhi",
    "अच्छे": "achhe", "ज्यादा": "zyada", "वाला": "wala", "वाले": "wale", "वाली": "wali", "कुछ": "kuch",
    "बहुत": "bahut", "फिर": "phir", "एक": "ek", "पहले": "pehle", "कैसे": "kaise", "ऐसे": "aise",
    "वैसे": "waise", "जैसे": "jaise", "सबसे": "sabse", "इसलिए": "isliye", "रहे": "rahe", "गए": "gaye",
    "गई": "gayi", "हुए": "hue", "दोस्तो": "dosto", "दोस्तों": "doston", "कि": "ki", "रुपये": "rupaye",
    "रुपए": "rupaye", "लाख": "lakh", "करोड़": "crore", "करोड": "crore", "प्रतिशत": "pratishat",
    # English words commonly spoken in Hindi finance / creator videos
    "मार्केट": "market", "मार्किट": "market", "शेयर": "share", "शेयर्स": "shares", "स्टॉक": "stock",
    "स्टॉक्स": "stocks", "आईपीओ": "IPO", "इन्वेस्ट": "invest", "इन्वेस्टमेंट": "investment",
    "इन्वेस्टर": "investor", "इन्वेस्टर्स": "investors", "पोर्टफोलियो": "portfolio", "पॉर्टफोलियो": "portfolio",
    "प्रॉफिट": "profit", "प्रोफिट": "profit", "लॉस": "loss", "ट्रेडिंग": "trading", "ट्रेड": "trade",
    "ट्रेडर": "trader", "ट्रेडर्स": "traders", "फॉलो": "follow", "लाइक": "like", "सब्सक्राइब": "subscribe",
    "चैनल": "channel", "वीडियो": "video", "वीडियोस": "videos", "पेज": "page", "थैंक": "thank",
    "थैंक्स": "thanks", "यू": "you", "इन्फॉर्मेशन": "information", "इन्फोर्मेशन": "information",
    "इनफार्मेशन": "information", "एक्सपेक्टेड": "expected", "एक्सपेक्टेट": "expected",
    "एक्सचेंज": "exchange", "एक्सचेंजेस": "exchanges", "एक्सचेंजेज": "exchanges", "लिस्ट": "list",
    "लिस्टेड": "listed", "लिस्टिंग": "listing", "न्यूज": "news", "बैंक": "bank", "बैंकिंग": "banking",
    "निफ्टी": "Nifty", "सेंसेक्स": "Sensex", "प्राइस": "price", "टारगेट": "target", "लॉन्ग": "long",
    "लॉंग": "long", "शॉर्ट": "short", "टर्म": "term", "होल्ड": "hold", "डबल": "double", "रिटर्न": "return",
    "रिटर्न्स": "returns", "कंपनी": "company", "कंपनियां": "companies", "इंडिया": "India", "फंड": "fund",
    "फंड्स": "funds", "म्यूचुअल": "mutual", "ओके": "okay", "हेलो": "hello", "हैलो": "hello",
    "गाइज": "guys", "गाइस": "guys", "डेटा": "data", "डाटा": "data", "चार्ट": "chart", "लेवल": "level",
    "लेवल्स": "levels", "सपोर्ट": "support", "रेजिस्टेंस": "resistance", "ब्रेकआउट": "breakout",
    "ऑप्शन": "option", "ऑप्शंस": "options", "ऑप्शन्स": "options", "फ्यूचर": "future", "फ्यूचर्स": "futures",
    "कॉल": "call", "पुट": "put", "बाय": "buy", "सेल": "sell", "सेलिंग": "selling", "बाइंग": "buying",
    "रिस्क": "risk", "सेक्टर": "sector", "रिजल्ट": "result", "रिजल्ट्स": "results", "ग्रोथ": "growth",
    "वैल्यू": "value", "वैल्युएशन": "valuation", "डिविडेंड": "dividend", "बोनस": "bonus", "स्प्लिट": "split",
    "अपडेट": "update", "चेक": "check", "लिंक": "link", "कमेंट": "comment", "कमेंट्स": "comments",
    "शेयर्ड": "shared", "प्लीज": "please", "सर": "sir", "टिप्स": "tips", "बजट": "budget", "टैक्स": "tax",
    "जीएसटी": "GST", "आरबीआई": "RBI", "सेबी": "SEBI", "एनएसई": "NSE", "बीएसई": "BSE",
    # whisper's usual misspellings of English words in Hindi speech (none of these is a Hindi word)
    "डवल": "double", "एक्षेंजेस": "exchanges", "एक्षेंज": "exchange", "शौट": "short", "टम": "term",
}


def norm_key(word: str) -> str:
    """Lookup key: NFC, no nukta, chandrabindu -> anusvara, surrounding punctuation stripped, lowercase Latin."""
    w = unicodedata.normalize("NFC", word)
    w = unicodedata.normalize("NFD", w).replace(NUKTA, "")
    w = unicodedata.normalize("NFC", w).replace(CHANDRA, ANUSVARA)
    return w.strip(PUNCT_CHARS).lower()


PUNCT_CHARS = ".,!?;:।॥\"'()[]-–—…“”‘’"
PUNCT = re.escape(PUNCT_CHARS)


def _syllables(word: str):
    """Split a Devanagari run into units: [cons_roman|None, vowel_roman|'' (halant)|None (inherent a), matra_char, coda]."""
    units = []
    i, n = 0, len(word)
    while i < n:
        ch = word[i]
        if ch in CONS:
            roman = CONS[ch]
            if i + 1 < n and word[i + 1] == NUKTA:
                roman = NUKTA_CONS.get(ch, roman)
                i += 1
            unit = [roman, None, "", ""]
            nxt = word[i + 1] if i + 1 < n else ""
            if nxt == HALANT:
                unit[1] = ""
                i += 1
            elif nxt in MATRAS:
                unit[1], unit[2] = MATRAS[nxt], nxt
                i += 1
            units.append(unit)
        elif ch in VOWELS:
            units.append([None, VOWELS[ch], ch, ""])
        elif ch in (ANUSVARA, CHANDRA, VISARGA) and units:
            units[-1][3] += ch
        elif ch in MATRAS and units:  # stray matra (broken whisper token) – attach to previous
            units[-1][1] = MATRAS[ch]
        elif ch == NUKTA or ch == HALANT:
            pass
        else:
            units.append([None, ch.translate(DIGITS), "", ""])  # danda, digits, anything else
        i += 1
    return units


def _translit_run(word: str) -> str:
    u = _syllables(word)
    if not u:
        return ""
    # final schwa: dropped unless the word is one letter, or ends in a cluster with r/y/v (mitra, vakya)
    last = u[-1]
    if last[0] and last[1] is None and len(u) > 1 and not last[3]:
        cluster_keep = u[-2][0] and u[-2][1] == "" and last[0] in ("r", "y", "v")
        if not cluster_keep:
            last[1] = ""
    # medial schwa, right to left: V C [a] C V  ->  drop (v[1] == "" means "no vowel": halant or already dropped).
    # Not after a nasal coda – that would make a 3-consonant cluster (ज़िंदगी stays zindagi, not zindgi).
    for k in range(len(u) - 2, 0, -1):
        cur, prev, nxt = u[k], u[k - 1], u[k + 1]
        if (cur[0] and cur[1] is None and not cur[3] and prev[1] != "" and not prev[3] and
                nxt[0] and nxt[1] != ""):
            cur[1] = ""
    nuclei = sum(1 for x in u if x[1] not in ("", ) and (x[0] or x[2]))
    closed_mono = nuclei == 1 and (u[-1][1] == "" or u[-1][3])
    out = []
    for idx, (c, v, mchar, coda) in enumerate(u):
        if c is None and mchar and v and mchar in VOWELS:
            # independent vowel: आ is "aa" only word-initially (aaj, aap; but hua); ए after a vowel -> ye (jayega)
            if mchar == "आ" and idx > 0:
                v = "a"
            elif idx > 0 and u[idx - 1][1] != "" and mchar == "ए":
                v = "ye"
        if c:
            # च्छ -> chh (achha), not chchh
            if c == "ch" and v == "" and idx + 1 < len(u) and u[idx + 1][0] == "chh":
                c = ""
            # व between vowels is "w" (sawal, jawab, hawa); at the start it stays "v" (vikas, video)
            if c == "v" and idx > 0 and u[idx - 1][1] != "":
                c = "w"
            out.append(c)
        if v is None:
            v = "a"
        elif closed_mono and mchar in LONG:
            v = LONG[mchar]
        out.append(v)
        for z in coda:
            if z == VISARGA:
                out.append("h")
            else:
                nxt = u[idx + 1][0] if idx + 1 < len(u) else None
                out.append("m" if nxt in LABIAL else "n")
    return "".join(out)


def to_hinglish(word: str, fixes: dict | None = None) -> str:
    """One caption word -> Hinglish. Fixes (user spellings) win over the dictionary, which wins over the rules.
    A fix may be keyed by the original word OR by the Hinglish the rules produced (what the user sees)."""
    base = _auto_hinglish(word)
    if fixes:
        for k in (norm_key(word), norm_key(base)):
            if k in fixes:
                return fixes[k]
    return base


def _auto_hinglish(word: str) -> str:
    if not DEVA.search(word):
        return word
    key = norm_key(word)
    if key in BUILTIN:
        # keep the surrounding punctuation of the original token (NOT \W: Devanagari vowel signs count as \W)
        m = re.match(rf"^([{PUNCT}]*)(.*?)([{PUNCT}]*)$", word)
        lead, trail = (m.group(1), m.group(3)) if m else ("", "")
        return f"{lead}{BUILTIN[key]}{trail.replace('।', '.')}"
    # transliterate Devanagari runs, keep everything else (hyphens, Latin, punctuation)
    parts = re.split(r"([ऀ-ॿ]+)", unicodedata.normalize("NFC", word))
    out = "".join(_translit_run(p) if p and DEVA.match(p) else p for p in parts)
    return out.replace("।", ".").replace("॥", ".")


def caption_word(word: str, settings: dict) -> str:
    """The text a caption shows for one transcript word, given the project's caption settings."""
    fixes = settings.get("caption_fixes") or {}
    if settings.get("caption_script") == "hinglish":
        return to_hinglish(word, fixes)
    hit = fixes.get(norm_key(word)) if fixes else None
    return word if hit is None else hit


def caption_text(words: list[str], settings: dict) -> str:
    return " ".join(w for w in (caption_word(x, settings) for x in words) if w)
