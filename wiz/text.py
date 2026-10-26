"""
Answer normalisation and similarity.

Auto-grading rule, deliberately narrow: an answer is scored automatically ONLY
if its normalised key is identical to the key of the official answer or of an
accepted alternate. Normalisation removes things that can never change the
meaning of a short quiz answer (case, accents, punctuation, spacing, a leading
"the"/"a"/"an", number words). Anything that is merely *similar* is never
scored automatically -- it is only suggested to the human reviewer, because
1947/1948, Serbia/Siberia and Hindu/Hindi are one edit apart and all different.
"""

import re
import unicodedata

_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20",
}
_ARTICLES = ("the", "a", "an")
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_DIGITS = re.compile(r"\d+")


def normalize(text):
    """Readable canonical form: lower-case words separated by single spaces."""
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().replace("&", " and ")
    words = _NON_WORD.sub(" ", s).split()
    if len(words) > 1 and words[0] in _ARTICLES:
        words = words[1:]
    return " ".join(_NUMBER_WORDS.get(w, w) for w in words)


def key(text):
    """
    Grouping/grading key: `normalize` with the spaces removed too, so that
    "Spider-Man", "spider man" and "Spiderman" are one answer.
    """
    return normalize(text).replace(" ", "")


def levenshtein(a, b, limit=None):
    """Edit distance; gives up early (returns limit + 1) once it must exceed `limit`."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if limit is not None and len(a) - len(b) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if limit is not None and min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def similarity(a, b):
    """0..1 similarity of two keys."""
    if not a or not b:
        return 0.0
    longest = max(len(a), len(b))
    if longest > 120:
        a, b, longest = a[:120], b[:120], 120
    return 1.0 - levenshtein(a, b) / float(longest)


def suggest(answer_key, answer_norm, accepted):
    """
    Why a human should look at this answer first, or None.

    `accepted` is a list of (key, normalized, raw) for the official answer and
    its alternates. Returns {"kind", "score", "against"}.
    """
    if not answer_key:
        return None
    best = None
    a_digits = _DIGITS.findall(answer_norm)
    a_tokens = set(answer_norm.split())
    for k, norm, raw in accepted:
        if not k:
            continue
        # a different number is a different answer, however close the text
        if (a_digits or _DIGITS.findall(norm)) and a_digits != _DIGITS.findall(norm):
            continue
        s = similarity(answer_key, k)
        kind = "close"
        tokens = set(norm.split())
        if len(k) >= 4 and tokens and (tokens <= a_tokens or a_tokens <= tokens):
            s, kind = max(s, 0.9), "contains"
        if min(len(answer_key), len(k)) < 4 and kind == "close":
            continue
        if s >= 0.8 and (best is None or s > best["score"]):
            best = {"kind": kind, "score": round(s, 3), "against": raw}
    return best
