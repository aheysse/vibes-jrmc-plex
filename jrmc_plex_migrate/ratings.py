"""Copy JRiver star ratings onto the matching Plex tracks.

JRiver stores ratings as 1-5 stars; Plex stores userRating on a 0-10 scale
(2 == 1 star), so we write rating * 2. Only rated JRiver tracks are touched;
unrated Plex tracks are left alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Config
from .jrmc import JrmcClient
from .plex_sync import PlexClient

log = logging.getLogger(__name__)

_RATING_FIELDS = ["Name", "Artist", "Album", "Album Artist", "Rating"]


@dataclass
class RatingReport:
    total_rated: int = 0
    matched: int = 0
    set: int = 0
    already: int = 0
    unmatched_tracks: list[str] = field(default_factory=list)

    @property
    def unmatched(self) -> int:
        return len(self.unmatched_tracks)


def migrate_ratings(
    cfg: Config, jrmc: JrmcClient, plex: PlexClient, dry_run: bool = False
) -> RatingReport:
    rpt = RatingReport()
    rated = jrmc.search("[Media Type]=[Audio] [Rating]=>0", fields=_RATING_FIELDS)
    rpt.total_rated = len(rated)
    log.info("JRiver reports %d rated audio tracks", rpt.total_rated)

    for jt in rated:
        try:
            stars = int(jt.rating)
        except ValueError:
            continue
        target = stars * 2  # 1-5 stars -> Plex 0-10

        res = plex.find_track(jt)
        if not res.matched:
            rpt.unmatched_tracks.append(f"{jt} ({stars}★)")
            continue
        rpt.matched += 1

        current = getattr(res.plex_track, "userRating", None)
        if current is not None and abs(float(current) - target) < 0.01:
            rpt.already += 1
            continue
        if not dry_run:
            plex.set_rating(res.plex_track, float(target))
        rpt.set += 1

    return rpt
