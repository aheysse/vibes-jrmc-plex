"""Copy JRiver play counts onto matching Plex tracks.

Plex has no API to set viewCount directly (the edit endpoint 500s), but
markPlayed() / scrobble *increments* it. So to reproduce a play count of N we
scrobble N times. We do the bulk (N-1) scrobbles in parallel (order doesn't
matter for the count), then a final single scrobble per track in ascending
JRiver last-played order so each track's lastViewedAt lands in that order --
making a dynamic "Recently Played" list approximately reflect JRiver's recency.

Note: Plex play counts are independent of JRiver's, and lastViewedAt cannot be
set to a historical date; it self-corrects as you play music in Plex.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .config import Config
from .jrmc import JrmcClient
from .plex_sync import PlexClient

log = logging.getLogger(__name__)

_PLAY_FIELDS = ["Name", "Artist", "Album", "Album Artist", "Number Plays", "Last Played"]


@dataclass
class PlayReport:
    total_played: int = 0
    matched: int = 0
    total_scrobbles: int = 0
    done_scrobbles: int = 0
    errors: int = 0
    unmatched_tracks: list[str] = field(default_factory=list)

    @property
    def unmatched(self) -> int:
        return len(self.unmatched_tracks)


def _as_int(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def migrate_plays(
    cfg: Config,
    jrmc: JrmcClient,
    plex: PlexClient,
    dry_run: bool = False,
    workers: int = 8,
) -> PlayReport:
    rpt = PlayReport()
    played = jrmc.search("[Media Type]=[Audio] [Number Plays]=>0", fields=_PLAY_FIELDS)
    rpt.total_played = len(played)

    # Match to Plex, de-duplicating onto unique Plex tracks (keep max plays /
    # latest last-played when several JRiver tracks map to the same Plex track).
    by_key: dict[int, dict] = {}
    for jt in played:
        plays = _as_int(jt.raw.get("Number Plays", "0"))
        if plays <= 0:
            continue
        res = plex.find_track(jt)
        if not res.matched:
            rpt.unmatched_tracks.append(str(jt))
            continue
        key = res.plex_track.ratingKey
        last = _as_int(jt.raw.get("Last Played", "0"))
        cur = by_key.get(key)
        if cur is None or plays > cur["plays"]:
            by_key[key] = {"track": res.plex_track, "plays": plays, "last": last}
        if cur is not None and last > cur["last"]:
            cur["last"] = last

    rpt.matched = len(by_key)
    rpt.total_scrobbles = sum(v["plays"] for v in by_key.values())
    if dry_run:
        return rpt

    items = list(by_key.values())

    def scrobble_n(track, n: int) -> int:
        done = 0
        for _ in range(n):
            try:
                track.markPlayed()
                done += 1
            except Exception:  # noqa: BLE001 - count and continue
                rpt.errors += 1
        return done

    # Phase 1: bulk (plays - 1) scrobbles, parallel (order irrelevant).
    log.info("Phase 1: %d bulk scrobbles across %d tracks",
             rpt.total_scrobbles - rpt.matched, rpt.matched)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(scrobble_n, it["track"], it["plays"] - 1)
            for it in items if it["plays"] > 1
        ]
        for fut in as_completed(futures):
            rpt.done_scrobbles += fut.result()

    # Phase 2: one final scrobble per track in ascending last-played order, so
    # lastViewedAt ends up ordered by JRiver recency. Sequential by design.
    log.info("Phase 2: %d ordered final scrobbles", len(items))
    for it in sorted(items, key=lambda x: x["last"]):
        rpt.done_scrobbles += scrobble_n(it["track"], 1)

    return rpt
