"""Plex side: connect, match tracks by metadata, and create playlists.

Built on python-plexapi. Track matching queries the music section per JRMC
track (with a small in-process cache) and scores candidates with matching.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from plexapi.server import PlexServer

from .jrmc import JrmcTrack
from .matching import normalize, score_candidate, similarity
from .smartlist import PlexSmartFilter

log = logging.getLogger(__name__)


@dataclass
class MatchResult:
    jrmc_track: JrmcTrack
    plex_track: Optional[object]
    score: float

    @property
    def matched(self) -> bool:
        return self.plex_track is not None


class PlexClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        music_section: str = "Music",
        match_threshold: float = 0.72,
        require_artist: bool = True,
        use_album: bool = True,
    ):
        self.server = PlexServer(base_url, token)
        self.section = self.server.library.section(music_section)
        if self.section.type != "artist":
            raise ValueError(
                f"Section {music_section!r} is type {self.section.type!r}, "
                "expected a music ('artist') library."
            )
        self.match_threshold = match_threshold
        self.require_artist = require_artist
        self.use_album = use_album
        # Cache: normalized title -> list of plex track candidates.
        self._title_cache: dict[str, list] = {}
        self._artist_index: dict[str, str] | None = None

    # ---- artist-name resolution (for smart-playlist filters) ------------

    def _artists(self) -> dict[str, str]:
        """Lazily build {normalized artist title -> actual Plex artist title}."""
        if self._artist_index is None:
            self._artist_index = {}
            for artist in self.section.all():
                title = getattr(artist, "title", "")
                if title:
                    self._artist_index.setdefault(normalize(title), title)
        return self._artist_index

    def resolve_artists(self, names: list[str]) -> list[str]:
        """Map JRiver artist names to Plex's exact artist titles.

        Plex smart filters match titles exactly, but spellings differ (e.g. a
        Unicode hyphen). We match on the normalized form, then fall back to a
        fuzzy match, and finally keep the original if nothing is close.
        """
        index = self._artists()
        resolved: list[str] = []
        for name in names:
            nn = normalize(name)
            if nn in index:
                resolved.append(index[nn])
                continue
            best, best_score = None, 0.0
            for norm_title, title in index.items():
                sc = similarity(nn, norm_title)
                if sc > best_score:
                    best, best_score = title, sc
            resolved.append(best if best_score >= 0.88 else name)
        # De-duplicate while preserving order.
        seen, out = set(), []
        for r in resolved:
            if r not in seen:
                seen.add(r)
                out.append(r)
        return out

    # ---- track matching -------------------------------------------------

    def _candidates(self, title: str) -> list:
        key = normalize(title)
        if key in self._title_cache:
            return self._title_cache[key]
        candidates: list = []
        try:
            # Exact-ish title filter first (fast, case-insensitive in Plex).
            candidates = self.section.searchTracks(title=title, maxresults=50)
            if not candidates:
                # Looser contains search on the most distinctive word.
                words = [w for w in key.split() if len(w) > 3] or key.split()
                if words:
                    longest = max(words, key=len)
                    candidates = self.section.searchTracks(
                        filters={"track.title__icontains": longest}, maxresults=80
                    )
        except Exception as exc:  # plexapi raises various BadRequest types
            log.debug("Plex search failed for title %r: %s", title, exc)
            candidates = []
        self._title_cache[key] = candidates
        return candidates

    def find_track(self, jt: JrmcTrack) -> MatchResult:
        best = None
        best_score = 0.0
        jt_artist = jt.artist or jt.album_artist
        for cand in self._candidates(jt.title):
            cand_artist = getattr(cand, "originalTitle", None) or getattr(
                cand, "grandparentTitle", ""
            )
            cand_album = getattr(cand, "parentTitle", "")
            score = score_candidate(
                jt.title,
                jt_artist,
                jt.album,
                getattr(cand, "title", ""),
                cand_artist or "",
                cand_album or "",
                require_artist=self.require_artist,
                use_album=self.use_album,
            )
            if score > best_score:
                best_score = score
                best = cand
        if best is not None and best_score >= self.match_threshold:
            return MatchResult(jt, best, best_score)
        return MatchResult(jt, None, best_score)

    def set_rating(self, plex_track, rating_0_to_10: float) -> None:
        """Set a track's Plex user rating (0-10 scale; 2 == 1 star)."""
        plex_track.rate(rating_0_to_10)

    # ---- playlist operations -------------------------------------------

    def get_playlist(self, title: str):
        for pl in self.server.playlists():
            if pl.title == title:
                return pl
        return None

    def delete_playlist(self, title: str) -> bool:
        pl = self.get_playlist(title)
        if pl is not None:
            pl.delete()
            return True
        return False

    def create_static_playlist(self, title: str, plex_tracks: list):
        if not plex_tracks:
            raise ValueError("refusing to create an empty playlist")
        return self.server.createPlaylist(title, items=plex_tracks)

    def create_smart_playlist(self, title: str, sf: PlexSmartFilter):
        kwargs = {
            "section": self.section,
            "smart": True,
            "libtype": sf.libtype,
            "filters": sf.filters,
        }
        if sf.sort:
            kwargs["sort"] = sf.sort
        if sf.limit:
            kwargs["limit"] = sf.limit
        return self.server.createPlaylist(title, **kwargs)
