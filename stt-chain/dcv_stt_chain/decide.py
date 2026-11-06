"""Which text should Home Assistant get? Decided from what Home Assistant can act on.

The rule (Jeremy's): Whisper's text wins if Home Assistant can act on it; failing that, the
command sentence found inside Whisper's text; failing that, Speech-to-Phrase's text; and if
none of them is actionable, Whisper's text exactly as before. This module only decides; the
handler logs the decision (shadow) or acts on it (enforce).

Everything here works on text that has been through clean(): single spaces and a length cap,
so no pattern below can be made slow by a crafted transcript.
"""

import re
from typing import Optional

MAX_TEXT_CHARS = 2000  # a transcript longer than this is cut before anything else looks at it
MAX_CHECK_CHARS = 512  # Home Assistant is only asked about sentences up to this long
MAX_PIECES = 6
MAX_PIECE_CHARS = 200

_STRONG = re.compile(r"(?<=[.?!])\s+")  # a full stop, question or exclamation mark
_WEAK = re.compile(r"\s(?=turn\s)", re.IGNORECASE)  # "... on turn the kitchen light off" run together
_NEGATION = re.compile(
    r"\b(?:do not|cannot|never|not|no|nor|neither|nothing|nobody|stop|cancel|without|shan'?t|mightn'?t"
    r"|(?:do|did|is|does|are|was|were|have|has|had|could|should|would|must|need|ca|wo|ai)n'?t)\b"
)
# For refusing a swap: only words that reverse a command. "stop" and "cancel" are commands themselves.
_REVERSAL = re.compile(
    r"\b(?:do not|cannot|never|not|no|nor|neither|nothing|nobody|without|shan'?t|mightn'?t"
    r"|(?:do|did|is|does|are|was|were|have|has|had|could|should|would|must|need|ca|wo|ai)n'?t)\b"
)
_CONJUNCTION = re.compile(r"\b(?:and|or|then|but|also|plus)\s*$")


def clean(text) -> str:
    """Single spaces, bounded length, always a string."""
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:MAX_TEXT_CHARS]


def split(text: str) -> list:
    """(piece, promotable) for each sentence of a transcript.

    A piece made by cutting before the word "turn" is only promotable when what comes before it
    is not a negation ("don't turn off ...") and does not end in a conjunction ("... on the
    light and turn off the fan"): in those cases the second half alone means something else.
    """
    out = []
    for sentence in _STRONG.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        before = ""
        for i, part in enumerate(_WEAK.split(sentence)):
            if len(out) >= MAX_PIECES:
                return out  # nothing beyond the limit is ever looked at, so no work is spent on it
            part = part.strip()
            if not part:
                continue
            ok = True
            if i > 0:
                lowered = before[-200:].lower().replace("’", "'")  # the words just before the cut
                ok = not _NEGATION.search(lowered) and not _CONJUNCTION.search(lowered)
            out.append((part[:MAX_PIECE_CHARS], ok))
            before += " " + part
    return out


def pieces(text: str) -> list:
    """Sentences of a transcript, so a command can be picked out of surrounding speech."""
    return [p for p, _ in split(text)]


def sentences_to_check(whisper: str, phrase: str) -> list:
    """Whisper's whole text, its pieces (if more than one), then Speech-to-Phrase's text.

    No repeats, nothing empty, nothing longer than MAX_CHECK_CHARS (it would be cut, and the
    answer would then be about a different text than the one that gets used).
    """
    candidates = [whisper]
    parts = split(whisper) if whisper else []
    if len(parts) > 1:
        candidates += [p for p, _ in parts]
    candidates.append(phrase)
    ordered = []
    for s in candidates:
        if s and len(s) <= MAX_CHECK_CHARS and s not in ordered:
            ordered.append(s)
    return ordered


def _negated(text: str) -> bool:
    return bool(_REVERSAL.search(text.lower().replace("\u2019", "'")))


def overturns(whisper: str, phrase: str) -> bool:
    """Speech-to-Phrase's grammar can only say commands, so it may drop a "don't": when Whisper
    heard a negation and Speech-to-Phrase's text has none, the swap would do the opposite of
    what was said, so it is refused."""
    return _negated(whisper) and not _negated(phrase)


def decide(whisper: str, phrase: str, results: Optional[list], asked: Optional[list] = None) -> dict:
    """`results` is HaChecker.check's answer for `asked` (default sentences_to_check), or None."""
    if results is None:
        return {
            "use": "whisper_raw",
            "text": whisper,
            "ha": {"available": False, "whisper": None, "phrase": None, "intent": None},
            "enforced": False,
        }
    asked = asked if asked is not None else sentences_to_check(whisper, phrase)
    seen = dict(zip(asked, results))

    def ok(text: str) -> bool:
        r = seen.get(text)
        return bool(text and r and r.get("actionable"))

    if ok(whisper):
        use, text = "whisper", whisper
    else:
        trimmed = next((p for p, promotable in split(whisper) if promotable and p != whisper and ok(p)), None)
        if trimmed is not None:
            use, text = "whisper_trimmed", trimmed
        elif ok(phrase) and not overturns(whisper, phrase):
            use, text = "phrase", phrase
        else:
            use, text = "whisper_raw", whisper
    return {
        "use": use,
        "text": text,
        "ha": {
            "available": True,
            "whisper": ok(whisper) if whisper else None,
            "phrase": ok(phrase) if phrase else None,
            "intent": (seen.get(text) or {}).get("intent") if use != "whisper_raw" else None,
        },
        "enforced": False,
    }
