# jrmc-plex-migrate

> 🤖 Vibe coded with [Claude](https://claude.com/claude-code) — built end-to-end
> through conversation with Claude Code, including live discovery of both servers'
> APIs and the actual migration.

Migrate **JRiver Media Center** playlists and smartlists to a **Plex** server,
talking to the HTTP API of each (JRiver MCWS / "Media Network" on one side,
the Plex HTTP API via [python-plexapi](https://github.com/pkkid/python-plexapi)
on the other).

Tracks are matched **by metadata** (artist / album / title) — the two servers
don't need to share file paths. Normal playlists become static Plex playlists.
Smartlists are **translated into native Plex smart playlists** when their rule
can be expressed exactly as a Plex filter, and otherwise fall back to a static
snapshot of their current contents (so a migration never silently produces a
dynamic playlist that means something different from the original).

## Requirements

- Python 3.11+
- JRiver Media Center with **Media Network** (Library Server) enabled
  (Options → Media Network). Note the port — usually `52199`.
- A Plex Media Server with a **music** library and an
  [X-Plex-Token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy config.example.toml config.toml   # then edit config.toml
```

Edit `config.toml` with your JRiver and Plex connection details (see comments
in [`config.example.toml`](config.example.toml)).

## Usage

List what's on the JRiver side (read-only, makes no changes):

```powershell
.\.venv\Scripts\python -m jrmc_plex_migrate list
```

Preview a migration without touching Plex:

```powershell
.\.venv\Scripts\python -m jrmc_plex_migrate migrate --dry-run
```

Do the migration:

```powershell
.\.venv\Scripts\python -m jrmc_plex_migrate migrate
```

Copy JRiver star ratings onto the matching Plex tracks (1-5 stars → Plex
userRating 2-10). Useful before migrating rating-based smartlists, since Plex's
ratings are independent of JRiver's:

```powershell
.\.venv\Scripts\python -m jrmc_plex_migrate migrate-ratings --dry-run
.\.venv\Scripts\python -m jrmc_plex_migrate migrate-ratings
```

Copy JRiver play counts onto matching Plex tracks (needed for play-count
smartlists like "Top Hits" to translate dynamically). Plex can't set a play
count directly, so this reproduces it by scrobbling each track that many times
— it issues a lot of requests and writes to your Plex play history:

```powershell
.\.venv\Scripts\python -m jrmc_plex_migrate migrate-plays --dry-run
.\.venv\Scripts\python -m jrmc_plex_migrate migrate-plays
```

Useful flags:

- `--user-only` — only migrate **user-created** lists (those at the root of the
  JRiver playlist tree), skipping imported albums and JRiver's built-in
  smartlists, which live inside groups. This is almost always what you want.
- `--filter "Rock"` — only process playlists whose name contains the text.
- `--config other.toml` — use a different config file.
- `-v` / `--verbose` — debug logging (shows the HTTP calls being made).

Re-runs are safe: with `[migrate].existing = "skip"` (the default) any playlist
that already exists in Plex is left untouched, so you can re-run after fixing
metadata or adding `[rules]` without creating duplicates (use `"replace"` to
overwrite instead).

The migrate command prints a per-playlist report: the action taken
(`created-static`, `created-smart`, `skipped`, `failed`), how many tracks
matched, and a sample of any tracks it could **not** find in Plex.

## How smartlists are handled

`[migrate].smartlists = "translate"` (the default) attempts to build a native
**dynamic** Plex smart playlist from each smartlist's rule. Supported JRiver
fields include `Genre`, `Artist`, `Album Artist (auto)`, `Album`, `Name`,
`Date (year)`/`Year`, `Rating` (rescaled 0–5 → 0–10), and play counts
(`Number Plays`); operators `=`, `>`, `<`, `>=`, `<=`, ranges (`A-B`), OR-lists
(`[a],[b]`), negation (`-[Field]=...`), JRiver `/`-escaping, and the `~n=` limit.
`[Media Type]=[Audio]` is a no-op (the section is all audio); `~sort` and other
ordering modifiers are dropped. Anything else falls back to a static snapshot.

**Where the rules come from:** MCWS has no endpoint that returns a smartlist's
search rule, but JRiver stores it in `playlistx.jmd` inside the library folder.
When the tool runs on the same machine as JRiver it reads that file
automatically (locating the library via `Library/List`). If it can't (e.g.
running remotely), paste a rule into the `[rules]` table in `config.toml`.

**Safeguards / fidelity notes:**
- Artist names in a translated filter are resolved to Plex's *exact* artist
  titles (matching on a normalized form, then fuzzily), so differences like a
  Unicode vs ASCII hyphen ("The All‐American Rejects") still match.
- After creating a smart playlist, the tool compares its live item count to the
  JRiver snapshot; if it's implausibly small it replaces it with a faithful
  static snapshot.
- Plex has **no `>=`/`<=`** filter operators — only strict `>>`/`<<` — so the
  translator expresses `>= N` as `>> (N-1)` (valid because the numeric fields
  are integers). Plex play-count filters use `track.viewCount`.
- **Ratings and play counts are per-server.** Rating-based smartlists work
  dynamically after `migrate-ratings`; play-count lists ("Top Hits", "Recently
  Played") after `migrate-plays`. `lastViewedAt` can't be set to a historical
  date, so "Recently Played" ordering is approximate at first and self-corrects
  as you play music in Plex.

Set `[migrate].smartlists = "static"` to snapshot everything instead.

## Layout

```
jrmc_plex_migrate/
  config.py      load/validate config.toml
  jrmc.py        JRiver MCWS client (Authenticate, Playlists/List, Playlist/Files)
  matching.py    metadata normalization + fuzzy track scoring
  plex_sync.py   Plex connection, track matching, playlist creation (plexapi)
  smartlist.py   JRiver rule -> Plex smart-filter translator
  ratings.py     copy JRiver star ratings onto Plex tracks
  plays.py       copy JRiver play counts onto Plex tracks (scrobble)
  migrate.py     orchestration + per-playlist reporting
  cli.py         argparse CLI (list / migrate / migrate-ratings / migrate-plays)
tests/           offline unit tests for the translator and matcher
```

## Notes & limitations

- **Music only.** The tool targets a Plex music (`artist`) library.
- **Matching is fuzzy.** Tune `[migrate].match_threshold` if you see false
  matches (raise it) or too many misses (lower it). Run `--dry-run` first and
  review the unmatched list.
- MCWS response shapes vary slightly across Media Center versions. If `list`
  returns nothing or odd results on your server, run with `-v` and share the
  raw response so the parser can be adjusted — `Playlists/List` and
  `Playlist/Files` are the two endpoints involved.

## License

[MIT](LICENSE) © 2026 Aaron Heysse. Provided "as is", without warranty of any
kind.
