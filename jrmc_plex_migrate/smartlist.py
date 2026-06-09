"""Best-effort translation of a JRiver smartlist rule into a Plex smart filter.

JRiver's search language and Plex's smart-playlist filters do not map 1:1, so
this translator covers the cases it can express and raises TranslationError on
anything else, letting the caller fall back to a static snapshot.

JRiver search expression (subset we handle):
    [Field]=value                  field is value
    [Field]=[value with spaces]    bracketed value
    [Field]=[a],[b]                OR list (a or b)        -> {'key': [a, b]}
    [Field]="value"                quoted value
    -[Field]=[value]               negation                -> {'key!': value}
    [Field]=>N =<N =>=N =<=N        numeric comparison
    [Field]=A-B                    numeric range           -> two keys (>=A, <=B)
    [Media Type]=[Audio]           dropped (music section is all audio)
    ~n=N                           limit
    ~sort=[Field]                  sort (mapped where possible, else dropped)
    other ~modifiers               dropped (ordering/display only, not membership)

JRiver escapes reserved characters inside values with '/', e.g. [Blink/-182];
we unescape these. Terms are space-separated and combined with AND, matching
how a Plex filters dict combines its keys.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


class TranslationError(Exception):
    """Raised when a JRMC rule cannot be expressed exactly as a Plex filter."""


# JRiver field name -> (plex filter key, value kind)
#   value kind: "str" | "int" | "rating5" (0-5 scale -> Plex 0-10)
_FIELD_MAP: dict[str, tuple[str, str]] = {
    "genre": ("genre", "str"),
    "artist": ("artist.title", "str"),
    "album artist": ("artist.title", "str"),
    "album artist (auto)": ("artist.title", "str"),
    "album": ("album.title", "str"),
    "name": ("track.title", "str"),
    "date (year)": ("year", "int"),
    "year": ("year", "int"),
    "rating": ("userRating", "rating5"),
    "number plays": ("track.viewCount", "int"),
    "play count": ("track.viewCount", "int"),
    "plays": ("track.viewCount", "int"),
    "mood": ("mood", "str"),
}

# JRiver sort field -> Plex sort key (best effort; unmapped sorts are dropped).
_SORT_MAP: dict[str, str] = {
    "name": "track.titleSort",
    "artist": "artist.titleSort",
    "album": "album.titleSort",
    "date (year)": "track.year",
    "year": "track.year",
    "rating": "track.userRating",
    "number plays": "track.viewCount",
    "play count": "track.viewCount",
    "plays": "track.viewCount",
    "date imported": "track.addedAt",
    "last played": "track.lastViewedAt",
}

# Plex only has strict '>>' (greater) and '<<' (less) operators -- no >=/<=.
# Since every numeric field we translate (rating, year, play count) is an
# integer, ">= N" is expressed as ">> (N-1)" and "<= N" as "<< (N+1)".
def _compare(plex_key: str, op: str, value: int) -> tuple[str, int]:
    if op == ">":
        return plex_key + ">>", value
    if op == ">=":
        return plex_key + ">>", value - 1
    if op == "<":
        return plex_key + "<<", value
    if op == "<=":
        return plex_key + "<<", value + 1
    raise TranslationError(f"unsupported operator {op!r}")

_TERM_RE = re.compile(
    r"""^(?P<neg>-)?\[(?P<field>[^\]]+)\]
        =(?P<op>>=|<=|>|<)?(?P<value>.*)$""",
    re.VERBOSE,
)
_RANGE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


@dataclass
class PlexSmartFilter:
    filters: dict = field(default_factory=dict)
    sort: Optional[str] = None
    limit: Optional[int] = None
    libtype: str = "track"
    # Notes on lossy aspects (e.g. dropped sort) for reporting.
    notes: list[str] = field(default_factory=list)


def _unescape(s: str) -> str:
    """Undo JRiver's '/' escaping inside a value, e.g. '/-' -> '-', '//' -> '/'."""
    return re.sub(r"/(.)", r"\1", s)


def _split_terms(rule: str) -> list[str]:
    """Split a rule into terms on spaces, keeping bracketed groups intact."""
    terms: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in rule:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == " " and depth == 0:
            if buf:
                terms.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        terms.append("".join(buf))
    return terms


def _parse_values(raw: str) -> list[str]:
    """Parse a value that may be bracketed, quoted, and/or a comma-OR list."""
    raw = raw.strip()
    parts = re.findall(r"\[([^\]]*)\]", raw)
    if not parts:
        stripped = raw.strip().strip('"')
        if "," in stripped:
            parts = [p.strip().strip('"') for p in stripped.split(",")]
        elif stripped:
            parts = [stripped]
    return [_unescape(p) for p in parts if p != ""]


def _convert_value(value: str, kind: str):
    if kind == "int":
        try:
            return int(value)
        except ValueError as exc:
            raise TranslationError(f"expected integer, got {value!r}") from exc
    if kind == "rating5":
        try:
            return int(round(float(value) * 2))  # JRMC 0-5 -> Plex 0-10
        except ValueError as exc:
            raise TranslationError(f"expected rating, got {value!r}") from exc
    return value


def _apply_sort(sf: PlexSmartFilter, raw: str, seq_field: Optional[str]) -> None:
    # ~sort=[Field] or ~sort=[Field]-d  (trailing '-' / '-d' = descending)
    desc = bool(re.search(r"-d?\s*$", raw))
    fields = re.findall(r"\[([^\]]+)\]", raw)
    if not fields:
        return
    name = fields[0].strip().lower()
    # JRiver can sort by a ~seq= alias (e.g. ~seq=[Number Plays] ~sort=[seq]).
    if name == "seq" and seq_field:
        name = seq_field
    key = _SORT_MAP.get(name)
    if key:
        sf.sort = f"{key}:desc" if desc else f"{key}:asc"
    else:
        sf.notes.append("sort order not preserved")


def translate(rule: str) -> PlexSmartFilter:
    """Translate a JRMC smartlist rule into a PlexSmartFilter or raise.

    Raises TranslationError on anything that cannot be expressed exactly.
    """
    if not rule or not rule.strip():
        raise TranslationError("empty rule")

    sf = PlexSmartFilter()
    seq_field: Optional[str] = None
    for term in _split_terms(rule):
        if not term:
            continue
        if term.startswith("~"):
            mod = term[1:]
            low = mod.lower()
            if low.startswith("sort="):
                _apply_sort(sf, mod[len("sort="):], seq_field)
            elif low.startswith("n="):
                try:
                    sf.limit = int(mod[len("n="):])
                except ValueError as exc:
                    raise TranslationError(f"bad limit modifier {term!r}") from exc
            elif low.startswith("seq="):
                m = re.search(r"\[([^\]]+)\]", mod[len("seq="):])
                if m:
                    seq_field = m.group(1).strip().lower()
            else:
                # ~a, ~nocase, etc. affect display/grouping, not membership.
                sf.notes.append(f"dropped modifier ~{mod}")
            continue

        m = _TERM_RE.match(term)
        if not m:
            raise TranslationError(f"could not parse term {term!r}")
        field_name = m.group("field").strip().lower()

        # [Media Type]=[Audio] is a no-op in a Plex music section.
        if field_name == "media type":
            continue

        mapping = _FIELD_MAP.get(field_name)
        if mapping is None:
            raise TranslationError(f"unsupported field [{m.group('field')}]")
        plex_key, kind = mapping

        op = m.group("op") or "="
        raw_value = m.group("value")
        negate = bool(m.group("neg"))

        if op == "=":
            # Numeric range, e.g. [Date (year)]=1997-2006 or [Rating]=2-5
            rng = _RANGE_RE.match(raw_value) if kind != "str" else None
            if rng and not negate:
                lo = _convert_value(rng.group(1), kind)
                hi = _convert_value(rng.group(2), kind)
                gk, gv = _compare(plex_key, ">=", lo)
                lk, lv = _compare(plex_key, "<=", hi)
                sf.filters[gk] = gv
                sf.filters[lk] = lv
                continue

            values = _parse_values(raw_value)
            if not values:
                raise TranslationError(f"empty value in term {term!r}")
            converted = [_convert_value(v, kind) for v in values]
            key = plex_key + "!" if negate else plex_key
            sf.filters[key] = converted if len(converted) > 1 else converted[0]
        else:
            # Comparison operator: numeric fields only, single value, no negation.
            if kind == "str":
                raise TranslationError(f"comparison on text field in {term!r}")
            if negate:
                raise TranslationError(f"negated comparison in {term!r}")
            values = _parse_values(raw_value)
            if len(values) != 1:
                raise TranslationError(f"comparison needs one value in {term!r}")
            key, val = _compare(plex_key, op, _convert_value(values[0], kind))
            sf.filters[key] = val

    if not sf.filters:
        raise TranslationError("rule produced no usable filters")
    return sf
