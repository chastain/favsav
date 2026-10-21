"""A small read-only Jellyfin API client.

Only the standard library is used (``urllib.request`` and ``json``).

The client is deliberately thin: it speaks HTTP and returns decoded JSON.  Everything
about *meaning* -- which items are favorites, how a favorite maps to a file -- lives in
:mod:`favsav.mapping`.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

#: Jellyfin's own API version header.  This is the client version, not the server
#: version; it selects the response shape and is stable across 10.8 - 11.x.
API_VERSION = "10.8"


class JellyfinError(Exception):
    """Base class for every error raised by this module."""


class JellyfinAuthError(JellyfinError):
    """The server rejected the API key."""


class JellyfinNotFound(JellyfinError):
    """The server does not know the requested item."""


class JellyfinUnavailable(JellyfinError):
    """The server could not be reached."""


@dataclass
class Item:
    """A library item, reduced to the fields favsav actually uses."""

    id: str
    name: str
    type: str
    path: Optional[str] = None
    location_type: Optional[str] = None
    child_count: Optional[int] = None
    parent_id: Optional[str] = None
    media_sources: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Item":
        return cls(
            id=data.get("Id") or "",
            name=data.get("Name") or "",
            type=data.get("Type") or "",
            path=data.get("Path") or None,
            location_type=data.get("LocationType") or None,
            child_count=data.get("ChildCount"),
            parent_id=data.get("ParentId") or None,
            media_sources=data.get("MediaSources") or [],
            raw=data,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Item(%r, %r, %r)" % (self.type, self.name, self.path)


@dataclass
class User:
    """A Jellyfin user."""

    id: str
    name: str
    is_admin: bool = False

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "User":
        policy = data.get("Policy") or {}
        return cls(
            id=data.get("Id") or "",
            name=data.get("Name") or "",
            is_admin=bool(policy.get("IsAdministrator")),
        )


class JellyfinClient:
    """Read-only client for the parts of the Jellyfin API favsav needs."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 30,
        retries: int = 3,
        backoff: float = 2.0,
        page_size: int = 5000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retries = max(1, retries)
        self.backoff = backoff
        self.page_size = page_size

    # ------------------------------------------------------------------ HTTP

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        url = "%s%s" % (self.base_url, path)
        if params:
            cleaned = {}
            for key, value in params.items():
                if value is None or value == "":
                    continue
                if isinstance(value, bool):
                    value = "true" if value else "false"
                cleaned[key] = value
            if cleaned:
                url += "?" + urllib.parse.urlencode(cleaned)
        return url

    def _request(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Any:
        url = self._url(path, params)
        headers = {
            "Accept": "application/json",
            "X-Emby-Token": self.api_key,
            "User-Agent": "favsav",
        }
        last_error: Optional[Exception] = None

        for attempt in range(self.retries):
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
                if not payload:
                    return {}
                return json.loads(payload.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:400]
                except Exception:  # pragma: no cover - best effort
                    pass
                if exc.code in (401, 403):
                    raise JellyfinAuthError(
                        "Jellyfin rejected the API key (HTTP %s): %s" % (exc.code, body)
                    )
                if exc.code == 404:
                    raise JellyfinNotFound("HTTP 404 for %s" % url)
                if exc.code == 400:
                    raise JellyfinError("HTTP 400 for %s: %s" % (url, body))
                last_error = exc
            except (urllib.error.URLError, socket.timeout, OSError) as exc:
                last_error = exc
            except ValueError as exc:
                raise JellyfinError("could not decode the response from %s: %s" % (url, exc))

            if attempt + 1 < self.retries:
                time.sleep(self.backoff ** attempt)

        raise JellyfinUnavailable("could not reach %s: %s" % (self.base_url, last_error))

    def _paged(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Iterator[Dict[str, Any]]:
        """Yield every item of a paged query, following ``startIndex``/``limit``."""
        params = dict(params or {})
        start = 0
        while True:
            query = dict(params)
            query["startIndex"] = start
            query["limit"] = self.page_size
            data = self._request(path, query)
            items = data.get("Items") if isinstance(data, dict) else None
            if not items:
                return
            for item in items:
                yield item
            start += len(items)
            total = data.get("TotalRecordCount") if isinstance(data, dict) else None
            if total is None or start >= total:
                return

    # --------------------------------------------------------------- queries

    def system_info(self) -> Dict[str, Any]:
        """Return ``/System/Info`` -- used to learn Jellyfin's own data directories."""
        return self._request("/System/Info")

    def data_paths(self) -> List[str]:
        """Directories Jellyfin uses for its own metadata.

        Favorites that live inside these are virtual containers (box sets, playlists)
        rather than real media, so they are expanded instead of copied.
        """
        try:
            info = self.system_info()
        except JellyfinError:
            return []
        paths = []
        for key in ("ProgramDataPath", "MetadataPath", "CachePath"):
            value = info.get(key)
            if isinstance(value, str) and value:
                paths.append(value.rstrip("/"))
        # Older servers spell it differently.
        for key in ("DataPath", "ConfigurationDirectory"):
            value = info.get(key)
            if isinstance(value, str) and value:
                paths.append(value.rstrip("/"))
        seen = []
        for path in paths:
            if path not in seen:
                seen.append(path)
        return seen

    def list_users(self) -> List[User]:
        """Return every user known to the server."""
        data = self._request("/Users")
        entries = data if isinstance(data, list) else []
        users = [User.from_json(entry) for entry in entries if isinstance(entry, dict)]
        return [user for user in users if user.id]

    def find_users(self, names: List[str]) -> List[User]:
        """Resolve a list of user names or ids to users.

        An empty list means "every user on the server".
        """
        users = self.list_users()
        if not names:
            return users
        wanted = {name.strip().lower() for name in names if name.strip()}
        matched = []
        for user in users:
            if user.name.lower() in wanted or user.id.lower() in wanted:
                matched.append(user)
        found = {user.name.lower() for user in matched} | {user.id.lower() for user in matched}
        missing = [name for name in wanted if name.lower() not in found]
        if missing:
            raise JellyfinError("no such Jellyfin user(s): %s" % ", ".join(sorted(missing)))
        return matched

    def list_favorites(
        self, user_id: str, item_types: Optional[List[str]] = None
    ) -> List[Item]:
        """Return the items marked favorite by one user.

        ``filters=IsFavorite`` is used rather than ``isFavorite=true``: the two are
        equivalent on 10.9 and later, but the filter parameter is the one that has
        been stable across every server release favsav supports.
        """
        params: Dict[str, Any] = {
            "userId": user_id,
            "filters": "IsFavorite",
            "recursive": "true",
            "fields": "Path,MediaSources,ExtraIds",
            "enableImages": "false",
            "enableUserData": "true",
        }
        if item_types:
            params["includeItemTypes"] = ",".join(item_types)
        items = [Item.from_json(entry) for entry in self._paged("/Items", params)]
        return [item for item in items if item.id]

    def list_children(self, parent_id: str, user_id: Optional[str] = None) -> List[Item]:
        """Return the direct children of an item.

        Some items (virtual seasons in particular) only expose their children when
        a user is specified, because Jellyfin generates them per user.  Passing a
        user id makes those children visible.
        """
        params: Dict[str, Any] = {
            "parentId": parent_id,
            "recursive": "false",
            "fields": "Path,MediaSources",
            "enableImages": "false",
        }
        if user_id:
            params["userId"] = user_id
        return [Item.from_json(entry) for entry in self._paged("/Items", params)]

    def list_by_ids(self, item_ids: List[str]) -> List[Item]:
        """Return the items with these ids."""
        if not item_ids:
            return []
        params = {
            "ids": ",".join(item_ids),
            "fields": "Path,MediaSources",
            "enableImages": "false",
        }
        return [Item.from_json(entry) for entry in self._paged("/Items", params)]

    def extras_of(self, item: Item) -> List[Item]:
        """Return the extras (trailers and the like) of an item.

        Jellyfin reports the extras of an item as a list of ids.  They are resolved
        here so that their paths can be mirrored like any other file.
        """
        raw = item.raw or {}
        ids = raw.get("ExtraIds") or []
        if not isinstance(ids, list):
            return []
        wanted = [str(value) for value in ids if value]
        if not wanted:
            return []
        try:
            return [entry for entry in self.list_by_ids(wanted) if entry.path]
        except JellyfinError:
            return []

    def get_item(self, user_id: str, item_id: str) -> Item:
        """Return one item.

        ``GET /Items/{id}`` is not available on every supported server version, so the
        per-user route is used instead.
        """
        params = {"fields": "Path,MediaSources"}
        data = self._request("/Users/%s/Items/%s" % (user_id, item_id), params)
        return Item.from_json(data)
