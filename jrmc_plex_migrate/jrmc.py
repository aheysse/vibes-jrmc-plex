"""JRiver Media Center Web Service (MCWS) client.

Talks to the JRiver "Media Network" HTTP API. Only the read endpoints we need
for migration are implemented:

    Authenticate        -> validate credentials, obtain a Token
    Alive               -> connectivity check
    Playlists/List      -> enumerate playlists / smartlists / groups
    Playlist/Files      -> evaluate a (smart)list into its current tracks

MCWS responses are XML of the form:

    <Response Status="OK">
      <Item Name="Token">abc</Item>            # "flat" records (Authenticate)
    </Response>

    <Response Status="OK">
      <Item>                                    # "list" records (Files, Playlists)
        <Field Name="Name">Song</Field>
        <Field Name="Artist">Band</Field>
      </Item>
      ...
    </Response>
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

log = logging.getLogger(__name__)

# Fields we ask MCWS to return for each track. Names are JRiver field names.
TRACK_FIELDS = [
    "Name",
    "Artist",
    "Album",
    "Album Artist",
    "Genre",
    "Date (year)",
    "Track #",
    "Disc #",
    "Duration",
    "Filename",
    "Media Type",
    "Rating",
]

# Field names on a Playlists/List entry that might carry the smartlist rule.
_RULE_FIELD_CANDIDATES = ("Search", "Rules", "Rule", "Smartlist", "SmartList")


class McwsError(Exception):
    """Raised when MCWS returns a non-OK status or an unparseable response."""


@dataclass
class JrmcTrack:
    """A single track as reported by JRiver."""

    title: str
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    year: str = ""
    track_no: str = ""
    disc_no: str = ""
    filename: str = ""
    rating: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_fields(cls, d: dict) -> "JrmcTrack":
        return cls(
            title=d.get("Name", "") or "",
            artist=d.get("Artist", "") or "",
            album=d.get("Album", "") or "",
            album_artist=d.get("Album Artist", "") or "",
            genre=d.get("Genre", "") or "",
            year=d.get("Date (year)", "") or d.get("Year", "") or "",
            track_no=d.get("Track #", "") or "",
            disc_no=d.get("Disc #", "") or "",
            filename=d.get("Filename", "") or "",
            rating=d.get("Rating", "") or "",
            raw=d,
        )

    def __str__(self) -> str:
        a = self.artist or self.album_artist or "?"
        return f"{a} - {self.title}"


@dataclass
class JrmcPlaylist:
    """A JRiver playlist, smartlist, or group node."""

    id: str
    name: str
    path: str = ""
    type: str = "playlist"  # playlist | smartlist | group
    rule: Optional[str] = None

    @property
    def is_smart(self) -> bool:
        return self.type == "smartlist"

    @property
    def is_group(self) -> bool:
        return self.type == "group"


def _root(content: bytes) -> ET.Element:
    """Parse XML and verify MCWS reported success."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:  # pragma: no cover - defensive
        raise McwsError(f"Could not parse MCWS response as XML: {exc}") from exc
    status = root.attrib.get("Status")
    if status is not None and status != "OK":
        raise McwsError(f"MCWS returned Status={status!r}")
    return root


def _parse_flat(content: bytes) -> dict:
    """Parse <Item Name=..>value</Item> records into a single dict."""
    root = _root(content)
    out: dict[str, str] = {}
    for child in root:
        name = child.attrib.get("Name")
        if name is not None:
            out[name] = child.text or ""
    return out


def _parse_items(content: bytes) -> list[dict]:
    """Parse <Item><Field Name=..>value</Field>..</Item> records into dicts.

    Tolerates the occasional flat <Item Name=..> record by skipping it.
    """
    root = _root(content)
    items: list[dict] = []
    for item in root.findall("Item"):
        fields = item.findall("Field")
        if fields:
            items.append({f.attrib.get("Name", ""): (f.text or "") for f in fields})
    return items


_TOKEN_RE = re.compile(r"\((\d+):")


def _token_at(data: str, pos: int) -> Optional[str]:
    """If a '(<len>:' token starts at pos, return its <len>-char value."""
    m = _TOKEN_RE.match(data, pos)
    if not m:
        return None
    n = int(m.group(1))
    start = m.end()
    return data[start:start + n]


def _extract_search_after(data: str, name: str) -> Optional[str]:
    """Find the (6:Search)(<len>:rule) belonging to the smartlist named `name`.

    Each smartlist record stores its name as a length-prefixed token
    `(<len>:<name>)` followed (within its body) by its Search rule. We anchor on
    that exact token and read the next Search value after it.
    """
    anchor = f"({len(name)}:{name})"
    i = data.find(anchor)
    if i < 0:
        return None
    s = data.find("(6:Search)", i)
    if s < 0:
        return None
    return _token_at(data, s + len("(6:Search)"))


def _classify(type_value: str) -> str:
    t = (type_value or "").strip().lower()
    if "smart" in t:
        return "smartlist"
    if "group" in t:
        return "group"
    return "playlist"


class JrmcClient:
    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        verify_ssl: bool = True,
        timeout: int = 30,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.token: Optional[str] = None
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self._auth = HTTPBasicAuth(username, password) if username else None

    def _get(self, endpoint: str, params: Optional[dict] = None) -> requests.Response:
        url = self.base_url + endpoint
        p = dict(params or {})
        if self.token:
            p.setdefault("Token", self.token)
        log.debug("GET %s params=%s", url, p)
        resp = self.session.get(url, params=p, auth=self._auth, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    def authenticate(self) -> Optional[str]:
        """Validate credentials and capture a session token (if provided)."""
        resp = self._get("Authenticate")
        data = _parse_flat(resp.content)
        self.token = data.get("Token") or None
        return self.token

    def alive(self) -> bool:
        try:
            self._get("Alive")
            return True
        except requests.RequestException:
            return False

    def list_playlists(self) -> list[JrmcPlaylist]:
        """Enumerate every playlist, smartlist, and group on the server."""
        resp = self._get("Playlists/List")
        playlists: list[JrmcPlaylist] = []
        for d in _parse_items(resp.content):
            rule = None
            for key in _RULE_FIELD_CANDIDATES:
                if d.get(key):
                    rule = d[key]
                    break
            playlists.append(
                JrmcPlaylist(
                    id=d.get("ID") or d.get("Key") or d.get("PlaylistID") or "",
                    name=d.get("Name", ""),
                    path=d.get("Path", ""),
                    type=_classify(d.get("Type", "")),
                    rule=rule,
                )
            )
        return playlists

    def playlist_files(
        self, playlist_id: str, fields: Optional[list[str]] = None
    ) -> list[JrmcTrack]:
        """Evaluate a playlist (or smartlist) into its current list of tracks."""
        params = {"Action": "MPL", "Playlist": playlist_id}
        params["Fields"] = ",".join(fields or TRACK_FIELDS)
        resp = self._get("Playlist/Files", params)
        return [JrmcTrack.from_fields(d) for d in _parse_items(resp.content)]

    def search(
        self, query: str, fields: Optional[list[str]] = None
    ) -> list[JrmcTrack]:
        """Run a library search (JRiver search-language query) -> tracks."""
        params = {"Action": "MPL", "Query": query}
        params["Fields"] = ",".join(fields or TRACK_FIELDS)
        resp = self._get("Files/Search", params)
        return [JrmcTrack.from_fields(d) for d in _parse_items(resp.content)]

    def library_dir(self) -> Optional[str]:
        """Return the on-disk directory of the default library, if discoverable."""
        try:
            data = _parse_flat(self._get("Library/List").content)
        except (requests.RequestException, McwsError):
            return None
        for key, val in data.items():
            if key.startswith("Library") and "located at " in (val or ""):
                return val.split("located at ", 1)[1].strip()
        return None

    def load_smartlist_rules(
        self, names: list[str], library_dir: Optional[str] = None
    ) -> dict[str, str]:
        """Read smartlist Search rules from the library's playlistx.jmd.

        MCWS has no endpoint that returns a smartlist's rule, but JRiver stores
        it on disk. Given the smartlist names (from list_playlists), returns
        {name: rule}. Best-effort: returns {} if the file can't be located/read
        (e.g. when running on a different machine than the JRiver server).
        """
        import os

        library_dir = library_dir or self.library_dir()
        if not library_dir:
            return {}
        path = os.path.join(library_dir, "playlistx.jmd")
        try:
            # playlistx.jmd is UTF-16LE; (<len>:..) prefixes count characters.
            data = open(path, "rb").read().decode("utf-16-le", errors="replace")
        except OSError as exc:
            log.debug("Could not read %s: %s", path, exc)
            return {}

        rules: dict[str, str] = {}
        for name in names:
            rule = _extract_search_after(data, name)
            if rule:
                rules[name] = rule
        return rules
