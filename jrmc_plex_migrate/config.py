"""Configuration loading for jrmc-plex-migrate.

Reads a TOML config file (see config.example.toml) into typed dataclasses.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """Raised when the config file is missing required values."""


@dataclass
class JrmcConfig:
    base_url: str
    username: str = ""
    password: str = ""
    verify_ssl: bool = True


@dataclass
class PlexConfig:
    base_url: str
    token: str
    music_section: str = "Music"


@dataclass
class MigrateConfig:
    playlist_prefix: str = ""
    existing: str = "skip"  # skip | replace
    smartlists: str = "translate"  # translate | static
    match_threshold: float = 0.72


@dataclass
class MatchingConfig:
    require_artist: bool = True
    use_album: bool = True


@dataclass
class Config:
    jrmc: JrmcConfig
    plex: PlexConfig
    migrate: MigrateConfig
    matching: MatchingConfig
    # Optional manual smartlist rule overrides: {playlist_name: jrmc_rule_string}
    rules: dict[str, str] = field(default_factory=dict)


def _require(table: dict, key: str, where: str) -> str:
    val = table.get(key)
    if val in (None, ""):
        raise ConfigError(f"Missing required config value [{where}].{key}")
    return val


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}\n"
            "Copy config.example.toml to config.toml and fill it in."
        )
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    jrmc_t = data.get("jrmc", {})
    plex_t = data.get("plex", {})
    mig_t = data.get("migrate", {})
    match_t = data.get("matching", {})

    jrmc = JrmcConfig(
        base_url=_require(jrmc_t, "base_url", "jrmc"),
        username=jrmc_t.get("username", "") or "",
        password=jrmc_t.get("password", "") or "",
        verify_ssl=bool(jrmc_t.get("verify_ssl", True)),
    )
    plex = PlexConfig(
        base_url=_require(plex_t, "base_url", "plex"),
        token=_require(plex_t, "token", "plex"),
        music_section=plex_t.get("music_section", "Music") or "Music",
    )
    existing = (mig_t.get("existing", "skip") or "skip").lower()
    if existing not in ("skip", "replace"):
        raise ConfigError("[migrate].existing must be 'skip' or 'replace'")
    smartlists = (mig_t.get("smartlists", "translate") or "translate").lower()
    if smartlists not in ("translate", "static"):
        raise ConfigError("[migrate].smartlists must be 'translate' or 'static'")
    migrate = MigrateConfig(
        playlist_prefix=mig_t.get("playlist_prefix", "") or "",
        existing=existing,
        smartlists=smartlists,
        match_threshold=float(mig_t.get("match_threshold", 0.72)),
    )
    matching = MatchingConfig(
        require_artist=bool(match_t.get("require_artist", True)),
        use_album=bool(match_t.get("use_album", True)),
    )
    rules = {str(k): str(v) for k, v in (data.get("rules", {}) or {}).items()}

    return Config(jrmc=jrmc, plex=plex, migrate=migrate, matching=matching, rules=rules)
