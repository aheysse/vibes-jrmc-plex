"""Command-line interface for jrmc-plex-migrate."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import Config, ConfigError, load_config
from .jrmc import JrmcClient
from .migrate import PlaylistReport, migrate_all
from .plays import migrate_plays
from .plex_sync import PlexClient
from .ratings import migrate_ratings


def _build_jrmc(cfg: Config) -> JrmcClient:
    client = JrmcClient(
        cfg.jrmc.base_url,
        username=cfg.jrmc.username,
        password=cfg.jrmc.password,
        verify_ssl=cfg.jrmc.verify_ssl,
    )
    if not client.alive():
        raise SystemExit(
            f"Could not reach JRiver MCWS at {cfg.jrmc.base_url} "
            "(check the URL/port and that Media Network is enabled)."
        )
    client.authenticate()
    return client


def _build_plex(cfg: Config) -> PlexClient:
    return PlexClient(
        cfg.plex.base_url,
        cfg.plex.token,
        music_section=cfg.plex.music_section,
        match_threshold=cfg.migrate.match_threshold,
        require_artist=cfg.matching.require_artist,
        use_album=cfg.matching.use_album,
    )


def cmd_list(cfg: Config, args: argparse.Namespace) -> int:
    jrmc = _build_jrmc(cfg)
    playlists = jrmc.list_playlists()
    if args.filter:
        playlists = [p for p in playlists if args.filter.lower() in p.name.lower()]
    print(f"{'TYPE':<10} {'RULE?':<6} NAME")
    print("-" * 60)
    for p in playlists:
        if p.is_group:
            continue
        has_rule = "yes" if (p.rule or cfg.rules.get(p.name)) else "-"
        print(f"{p.type:<10} {has_rule:<6} {p.name}")
    n = sum(1 for p in playlists if not p.is_group)
    print(f"\n{n} playlist(s)/smartlist(s).")
    return 0


def _print_reports(reports: list[PlaylistReport], dry_run: bool) -> None:
    print()
    header = "DRY RUN - nothing was created in Plex" if dry_run else "MIGRATION RESULTS"
    print(header)
    print("=" * len(header))
    for r in reports:
        line = f"[{r.action:<14}] {r.name}  ({r.mode}"
        if r.mode == "static":
            line += f", {r.matched}/{r.total} matched"
        line += ")"
        print(line)
        if r.note:
            print(f"    note: {r.note}")
        if r.unmatched:
            shown = r.unmatched_tracks[:10]
            print(f"    unmatched ({r.unmatched}):")
            for t in shown:
                print(f"      - {t}")
            if r.unmatched > len(shown):
                print(f"      ... and {r.unmatched - len(shown)} more")
    # Summary
    created = sum(1 for r in reports if r.action.startswith("created"))
    skipped = sum(1 for r in reports if r.action == "skipped")
    failed = sum(1 for r in reports if r.action == "failed")
    print(
        f"\nSummary: {len(reports)} processed, {created} created, "
        f"{skipped} skipped, {failed} failed."
    )


def cmd_migrate(cfg: Config, args: argparse.Namespace) -> int:
    if args.replace:
        cfg.migrate.existing = "replace"
    jrmc = _build_jrmc(cfg)
    plex = _build_plex(cfg)
    reports = migrate_all(
        cfg,
        jrmc,
        plex,
        name_filter=args.filter,
        dry_run=args.dry_run,
        user_only=args.user_only,
    )
    _print_reports(reports, args.dry_run)
    return 0


def cmd_migrate_ratings(cfg: Config, args: argparse.Namespace) -> int:
    jrmc = _build_jrmc(cfg)
    plex = _build_plex(cfg)
    rpt = migrate_ratings(cfg, jrmc, plex, dry_run=args.dry_run)
    header = "DRY RUN - ratings" if args.dry_run else "RATING MIGRATION"
    print(f"\n{header}\n{'=' * len(header)}")
    print(f"JRiver rated tracks:   {rpt.total_rated}")
    print(f"Matched in Plex:       {rpt.matched}")
    verb = "would set" if args.dry_run else "set"
    print(f"Ratings {verb}:        {rpt.set}")
    print(f"Already correct:       {rpt.already}")
    print(f"Unmatched:             {rpt.unmatched}")
    for t in rpt.unmatched_tracks[:15]:
        print(f"    - {t}")
    if rpt.unmatched > 15:
        print(f"    ... and {rpt.unmatched - 15} more")
    return 0


def cmd_migrate_plays(cfg: Config, args: argparse.Namespace) -> int:
    jrmc = _build_jrmc(cfg)
    plex = _build_plex(cfg)
    rpt = migrate_plays(cfg, jrmc, plex, dry_run=args.dry_run, workers=args.workers)
    header = "DRY RUN - play counts" if args.dry_run else "PLAY-COUNT MIGRATION"
    print(f"\n{header}\n{'=' * len(header)}")
    print(f"JRiver played tracks:    {rpt.total_played}")
    print(f"Matched in Plex:         {rpt.matched}")
    verb = "would scrobble" if args.dry_run else "scrobbles done"
    print(f"Total plays ({verb}): {rpt.total_scrobbles}"
          + ("" if args.dry_run else f" ({rpt.done_scrobbles} sent, {rpt.errors} errors)"))
    print(f"Unmatched:               {rpt.unmatched}")
    for t in rpt.unmatched_tracks[:15]:
        print(f"    - {t}")
    if rpt.unmatched > 15:
        print(f"    ... and {rpt.unmatched - 15} more")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jrmc-plex-migrate",
        description="Migrate JRiver Media Center playlists/smartlists to Plex.",
    )
    p.add_argument("--config", default="config.toml", help="Path to config TOML.")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    sub = p.add_subparsers(dest="command", required=True)

    pl = sub.add_parser("list", help="List JRiver playlists/smartlists (no changes).")
    pl.add_argument("--filter", help="Only show playlists whose name contains this.")
    pl.set_defaults(func=cmd_list)

    pm = sub.add_parser("migrate", help="Migrate playlists to Plex.")
    pm.add_argument("--filter", help="Only migrate playlists whose name contains this.")
    pm.add_argument(
        "--user-only",
        action="store_true",
        help="Only migrate user-created (root-level) lists, skipping imported "
        "albums and JRiver's built-in smartlists.",
    )
    pm.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite playlists that already exist in Plex (overrides config).",
    )
    pm.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without creating anything in Plex.",
    )
    pm.set_defaults(func=cmd_migrate)

    pr = sub.add_parser(
        "migrate-ratings",
        help="Copy JRiver star ratings onto matching Plex tracks.",
    )
    pr.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be rated without writing to Plex.",
    )
    pr.set_defaults(func=cmd_migrate_ratings)

    pp = sub.add_parser(
        "migrate-plays",
        help="Copy JRiver play counts onto matching Plex tracks (by scrobbling).",
    )
    pp.add_argument("--dry-run", action="store_true",
                    help="Report what would be scrobbled without writing.")
    pp.add_argument("--workers", type=int, default=8,
                    help="Parallel scrobble workers (default 8).")
    pp.set_defaults(func=cmd_migrate_plays)
    return p


def main(argv: list[str] | None = None) -> int:
    # Track/playlist names contain non-ASCII characters; force UTF-8 output so
    # printing the report can't crash on a legacy Windows console code page.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
