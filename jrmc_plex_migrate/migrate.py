"""Migration orchestration: tie the JRMC and Plex clients together."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .config import Config
from .jrmc import JrmcClient, JrmcPlaylist
from .plex_sync import PlexClient
from .smartlist import TranslationError, translate

log = logging.getLogger(__name__)


@dataclass
class PlaylistReport:
    name: str
    jrmc_type: str  # playlist | smartlist
    action: str = "pending"  # created-static | created-smart | skipped | failed | dry-run
    mode: str = "static"  # static | smart
    total: int = 0
    matched: int = 0
    unmatched_tracks: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def unmatched(self) -> int:
        return len(self.unmatched_tracks)


# A translated smart playlist whose live item count is below this fraction of the
# JRiver snapshot is treated as a bad translation (e.g. artist names that differ
# in Plex) and replaced with a faithful static snapshot instead.
SMART_MIN_FRACTION = 0.25


def _target_name(cfg: Config, name: str) -> str:
    return f"{cfg.migrate.playlist_prefix}{name}"


def _resolve_artist_filters(plex: PlexClient, sf) -> None:
    """Rewrite artist.title filter values to Plex's exact artist titles."""
    for key in list(sf.filters):
        if key.rstrip("!") != "artist.title":
            continue
        val = sf.filters[key]
        names = val if isinstance(val, list) else [val]
        resolved = plex.resolve_artists(names)
        sf.filters[key] = resolved if len(resolved) > 1 else resolved[0]


def migrate_all(
    cfg: Config,
    jrmc: JrmcClient,
    plex: PlexClient,
    name_filter: Optional[str] = None,
    dry_run: bool = False,
    user_only: bool = False,
) -> list[PlaylistReport]:
    playlists = jrmc.list_playlists()

    # Recover smartlist rules from the library file once (best-effort).
    rule_map: dict[str, str] = {}
    if cfg.migrate.smartlists == "translate":
        smart_names = [p.name for p in playlists if p.is_smart]
        try:
            rule_map = jrmc.load_smartlist_rules(smart_names)
            log.info("Recovered rules for %d/%d smartlists",
                     len(rule_map), len(smart_names))
        except Exception as exc:  # noqa: BLE001 - best effort
            log.warning("Could not load smartlist rules: %s", exc)

    reports: list[PlaylistReport] = []
    for pl in playlists:
        if pl.is_group:
            continue
        # User-created lists live at the playlist-tree root; imported albums and
        # JRiver's built-in smartlists live inside groups (Path has a separator).
        if user_only and "\\" in (pl.path or pl.name):
            continue
        if name_filter and name_filter.lower() not in pl.name.lower():
            continue
        reports.append(_migrate_one(cfg, jrmc, plex, pl, dry_run, rule_map))
    return reports


def _migrate_one(
    cfg: Config,
    jrmc: JrmcClient,
    plex: PlexClient,
    pl: JrmcPlaylist,
    dry_run: bool,
    rule_map: dict[str, str],
) -> PlaylistReport:
    rpt = PlaylistReport(name=pl.name, jrmc_type=pl.type)
    target = _target_name(cfg, pl.name)
    log.info("Processing %s %r", pl.type, pl.name)

    # Respect existing-playlist policy up front.
    existing = plex.get_playlist(target)
    if existing is not None and cfg.migrate.existing == "skip":
        rpt.action = "skipped"
        rpt.note = "a Plex playlist with this name already exists"
        return rpt

    # The JRiver snapshot: needed for the static path, and to sanity-check a
    # translated smart playlist's live count.
    tracks = jrmc.playlist_files(pl.id)
    rpt.total = len(tracks)
    if not tracks:
        rpt.action = "skipped"
        rpt.note = "empty in JRiver"
        return rpt

    rule = cfg.rules.get(pl.name) or rule_map.get(pl.name) or pl.rule
    want_smart = pl.is_smart and cfg.migrate.smartlists == "translate"

    if want_smart and rule:
        try:
            sf = translate(rule)
        except TranslationError as exc:
            log.info("  rule not translatable (%s); using static", exc)
            rpt.note = f"rule not translatable ({exc}); static"
        else:
            _resolve_artist_filters(plex, sf)
            if dry_run:
                rpt.mode = "smart"
                rpt.action = "dry-run"
                rpt.note = f"would create smart playlist; filters={sf.filters}"
                return rpt
            plex.delete_playlist(target)  # idempotent; clears any existing
            try:
                created = plex.create_smart_playlist(target, sf)
                count = len(created.items())
            except Exception as exc:  # noqa: BLE001 - report & fall back
                log.warning("  smart create failed (%s); using static", exc)
                plex.delete_playlist(target)
                rpt.note = f"smart create failed ({exc}); static"
            else:
                if count > 0 and count >= SMART_MIN_FRACTION * len(tracks):
                    rpt.mode = "smart"
                    rpt.action = "created-smart"
                    rpt.note = f"{count} items (source {len(tracks)}); {sf.filters}"
                    return rpt
                # Implausibly small -> probably a metadata mismatch; use static.
                plex.delete_playlist(target)
                rpt.note = (f"dynamic gave {count} vs source {len(tracks)}; "
                            "used static")
    elif want_smart and not rule:
        rpt.note = "no rule recovered; static"

    return _migrate_static(plex, target, tracks, dry_run, rpt)


def _migrate_static(
    plex: PlexClient,
    target: str,
    tracks: list,
    dry_run: bool,
    rpt: PlaylistReport,
) -> PlaylistReport:
    rpt.mode = "static"
    matched_plex = []
    for jt in tracks:
        res = plex.find_track(jt)
        if res.matched:
            matched_plex.append(res.plex_track)
            rpt.matched += 1
        else:
            rpt.unmatched_tracks.append(str(jt))

    if dry_run:
        rpt.action = "dry-run"
        return rpt

    if not matched_plex:
        rpt.action = "failed"
        rpt.note = (rpt.note + "; " if rpt.note else "") + "no tracks matched in Plex"
        return rpt

    plex.delete_playlist(target)  # idempotent; clears existing or failed smart try
    plex.create_static_playlist(target, matched_plex)
    rpt.action = "created-static"
    return rpt
