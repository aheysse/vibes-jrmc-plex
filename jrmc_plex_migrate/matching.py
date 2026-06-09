"""Fuzzy metadata matching between JRiver tracks and Plex tracks.

Matching is by metadata (artist / album / title), not file path, so we
normalize aggressively (strip accents, "feat." tags, bracketed suffixes,
punctuation) and score candidates on a weighted similarity.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

_FEAT = re.compile(r"\s*[\(\[]?\s*(feat\.?|ft\.?|featuring|with)\s+.*$", re.IGNORECASE)
_BRACKET = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_NONALNUM = re.compile(r"[^a-z0-9]+")
# Common, low-information trailing qualifiers that differ between libraries.
_QUALIFIERS = re.compile(
    r"\b(remaster(ed)?|deluxe|edition|version|mono|stereo|"
    r"explicit|clean|bonus track|album version|single version)\b",
    re.IGNORECASE,
)


def normalize(s: str) -> str:
    """Lowercase, de-accent, drop feat./bracketed/qualifier noise and punctuation."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _FEAT.sub("", s)
    s = _BRACKET.sub("", s)
    s = _QUALIFIERS.sub("", s)
    s = _NONALNUM.sub(" ", s)
    return s.strip()


def similarity(a: str, b: str) -> float:
    """Similarity in [0, 1] between two normalized strings."""
    na, nb = normalize(a), normalize(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


@dataclass
class Candidate:
    """A Plex track candidate with its component and combined scores."""

    plex_track: object
    title: str
    artist: str
    album: str
    score: float


def score_candidate(
    jt_title: str,
    jt_artist: str,
    jt_album: str,
    cand_title: str,
    cand_artist: str,
    cand_album: str,
    *,
    require_artist: bool = True,
    use_album: bool = True,
) -> float:
    """Weighted match score in [0, 1].

    Title carries the most weight, artist next, album least. If require_artist
    is set and the artist barely matches, the score is heavily penalized so a
    same-named song by a different artist is not accepted.
    """
    t = similarity(jt_title, cand_title)
    a = similarity(jt_artist, cand_artist)
    if use_album and jt_album and cand_album:
        al = similarity(jt_album, cand_album)
        score = 0.6 * t + 0.3 * a + 0.1 * al
    else:
        score = 0.65 * t + 0.35 * a

    if require_artist and a < 0.5 and jt_artist:
        score *= 0.5
    return score
