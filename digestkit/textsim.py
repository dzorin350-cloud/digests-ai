"""Text helpers without models: URL canonicalization, word stems, hashed vectors, near-duplicate check,
watchlist matching and date hints.

The hashed stem vector is a cheap, language-agnostic stand-in for embeddings: Russian inflection is
handled by cutting words to a 6-letter stem ("Полипласта" and "Полипласт" give the same stem).
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import numpy as np

DIM = 4096
_WORD = re.compile(r"[\w][\w\-]*", re.U)
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|yclid$|mc_|_hs|ref$|from$|rss$|ncid$|cmpid$)", re.I)
STOP = set(
    "это как так что для при или его она они был была были будет будут уже ещё еще также которые который "
    "которая которое после через более менее может могут года году году между этом этой этого the and for "
    "with from that this are was were will have has been into over about after their its новости news".split()
)


def canonical_url(url: str) -> str:
    p = urlsplit(url.strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)])
    path = p.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit(("https", host, path, query, ""))


def domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def stems(text: str) -> list[str]:
    out = []
    for w in _WORD.findall(text.lower().replace("ё", "е")):
        if len(w) < 3 and not w.isdigit():
            continue
        if w in STOP:
            continue
        out.append(w[:6] if not w.isdigit() else w)
    return out


def _h(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "little")


def hashed_vec(title: str, body: str = "", dim: int = DIM) -> np.ndarray:
    """Unit vector of hashed stems and stem bigrams; title counts double."""
    v = np.zeros(dim, dtype=np.float32)
    for part, weight in ((title, 2.0), (body[:600], 1.0)):
        st = stems(part)
        for t in st:
            v[_h(t) % dim] += weight
        for a, b in zip(st, st[1:], strict=False):
            v[_h(a + " " + b) % dim] += weight * 0.5
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def shingles(text: str, k: int = 4) -> set[int]:
    st = stems(text)
    if len(st) < k:
        return {_h(" ".join(st))} if st else set()
    return {_h(" ".join(st[i:i + k])) for i in range(len(st) - k + 1)}


def jaccard(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: set[int], b: set[int]) -> float:
    """Share of a's shingles found in b (a reprint of b with a few extra lines still scores high)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a)


def term_regex(term: str) -> re.Pattern[str]:
    """Word-start match that tolerates Russian endings: 'Полипласт' matches 'Полипласта', 'ВУШ' matches 'ВУШ'."""
    t = re.escape(term.strip().lower().replace("ё", "е"))
    tail = r"\w{0,4}" if len(term) >= 5 else ""
    return re.compile(r"(?<![\w])" + t + tail + r"(?![\w])", re.I | re.U)


def mentions(text: str, terms: list[str]) -> bool:
    low = text.lower().replace("ё", "е")
    return any(term_regex(t).search(low) for t in terms if t.strip())


_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "мая": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
    "nov": 11, "dec": 12,
}
_RU_DATE = re.compile(r"\b(\d{1,2})\s+(январ\w*|феврал\w*|март\w*|апрел\w*|ма[яй]|июн\w*|июл\w*|август\w*|"
                      r"сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)(?:\s+(20\d\d))?", re.I)
_EN_DATE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\.?\s+(\d{1,2})(?:,?\s+(20\d\d))?", re.I)
_NUM_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d\d)\b")


def date_hints(text: str, ref: datetime, limit: int = 4) -> list[str]:
    """Explicit dates mentioned in the text (ISO), to help tell the event date from the publication date."""
    found: list[date] = []

    def add(y: int | None, m: int, d: int) -> None:
        try:
            dt = date(y or ref.year, m, d)
            if y is None and dt > ref.date():  # "28 декабря" read in January is last year
                dt = date(ref.year - 1, m, d)
        except ValueError:
            return
        found.append(dt)

    for d, mon, y in _RU_DATE.findall(text):
        key = next((k for k in sorted(_MONTHS, key=len, reverse=True) if mon.lower().startswith(k)), None)
        if key:
            add(int(y) if y else None, _MONTHS[key], int(d))
    for mon, d, y in _EN_DATE.findall(text):
        add(int(y) if y else None, _MONTHS[mon.lower()[:3]], int(d))
    for d, m, y in _NUM_DATE.findall(text):
        add(int(y), int(m), int(d))
    uniq = list(dict.fromkeys(x.isoformat() for x in found))
    return uniq[:limit]


def lead(text: str, limit: int = 600) -> str:
    out = ""
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        out = (out + " " + para).strip()
        if len(out) >= limit:
            break
    return out[:limit]
