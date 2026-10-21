"""Turn Jellyfin favorites into a list of things to copy.

The rules this module implements are deliberately simple, because the layouts it will
meet are not:

* A favorite's location comes from Jellyfin's ``Path`` field and from nowhere else.
  Paths are never reconstructed from titles, season numbers or the library hierarchy.
* A favorite that points at a real file or directory is copied as it is found on disk.
  A directory is walked in full, whatever its shape happens to be.
* A favorite that points at something virtual -- a box set, a playlist, a season
  Jellyfin invented -- has no file of its own and is expanded into its children.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .config import CONTAINER_TYPES, Root
from .jellyfin import Item, JellyfinClient

#: Reasons an item may end up without a copy target.
SKIP_NO_PATH = "no media path"
SKIP_OUTSIDE_ROOTS = "outside the configured roots"
SKIP_CONTAINER_EMPTY = "container has no media in it"
SKIP_DEPTH = "expansion depth limit reached"
SKIP_STRM = "strm item (points at a remote URL)"
SKIP_NOT_FOUND = "not found on disk"
SKIP_DUPLICATE = "already covered by another favorite"


@dataclass
class RootMatch:
    """A source path located inside one of the configured roots."""

    root: Root
    relative: str


class RootMatcher:
    """Matches absolute paths against the configured roots.

    The longest matching prefix wins, so a more specific root placed later in the
    ``roots`` list still behaves the way a reader would expect.
    """

    def __init__(self, roots: Sequence[Root]) -> None:
        self.roots = list(roots)
        self._sorted = sorted(self.roots, key=lambda root: len(root.source), reverse=True)

    def match(self, path: str) -> Optional[RootMatch]:
        if not path:
            return None
        normalised = os.path.normpath(path)
        for root in self._sorted:
            source = os.path.normpath(root.source)
            if normalised == source:
                return RootMatch(root=root, relative="")
            prefix = source + os.sep
            if normalised.startswith(prefix):
                relative = normalised[len(prefix) :]
                if relative:
                    return RootMatch(root=root, relative=relative)
        return None


@dataclass
class Resolved:
    """One favorite item, with the source and destination it maps to."""

    item: Item
    source: Optional[str] = None
    dest: Optional[str] = None
    kind: str = "skip"
    reason: str = ""
    depth: int = 0
    origin: Optional[Item] = None
    extras: List[str] = field(default_factory=list)
    is_extra: bool = False

    @property
    def name(self) -> str:
        return self.item.name

    @property
    def type(self) -> str:
        return self.item.type

    @property
    def copyable(self) -> bool:
        return self.kind in ("file", "dir") and bool(self.source and self.dest)


def _is_under(path: str, parents: Iterable[str]) -> bool:
    """True when ``path`` lies inside one of ``parents``."""
    if not path:
        return False
    normalised = os.path.normpath(path)
    for parent in parents:
        if not parent:
            continue
        base = os.path.normpath(parent)
        if normalised == base or normalised.startswith(base + os.sep):
            return True
    return False


def _looks_virtual(item: Item, data_paths: Sequence[str]) -> bool:
    """True when the item is a Jellyfin construct rather than a file on the media disk.

    Box sets and playlists live inside Jellyfin's own data directory.  Seasons and
    other nodes Jellyfin invented have no path at all.
    """
    if item.type in CONTAINER_TYPES:
        return True
    if not item.path:
        return True
    if data_paths and _is_under(item.path, data_paths):
        return True
    return False


def _destination_for(dest_root: str, root: Root, relative: str) -> str:
    """Build the destination path for a source path relative to its root."""
    parts = []
    if root.subdir:
        parts.append(root.subdir)
    if relative:
        parts.append(relative)
    return os.path.join(dest_root, *parts) if parts else dest_root


def _inside(dest_root: str, path: str) -> bool:
    """Guard against a destination escaping the destination root."""
    base = os.path.abspath(dest_root)
    target = os.path.abspath(path)
    return target == base or target.startswith(base + os.sep)


def resolve(
    items: Sequence[Item],
    matcher: RootMatcher,
    dest_root: str,
    client: JellyfinClient,
    data_paths: Sequence[str] = (),
    include_strm: bool = False,
    include_extras: bool = False,
    max_depth: int = 5,
    user_id: Optional[str] = None,
) -> List[Resolved]:
    """Map favorite items to files and directories.

    Returns one :class:`Resolved` per *leaf* that was found, plus one per favorite
    that could not be copied, with ``reason`` explaining why.
    """
    results: List[Resolved] = []
    seen_sources: Dict[str, str] = {}

    queue: List[Tuple[Item, Optional[Item], int]] = [(item, None, 0) for item in items]
    visited: set = set()

    while queue:
        item, origin, depth = queue.pop(0)
        key = item.id or ("path:" + (item.path or ""))
        if key in visited:
            continue
        visited.add(key)

        if _looks_virtual(item, data_paths):
            if depth >= max_depth:
                results.append(
                    Resolved(item=item, kind="skip", reason=SKIP_DEPTH, depth=depth, origin=origin)
                )
                continue
            try:
                children = client.list_children(item.id, user_id=user_id)
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                results.append(
                    Resolved(
                        item=item,
                        kind="skip",
                        reason="could not list children: %s" % exc,
                        depth=depth,
                        origin=origin,
                    )
                )
                continue
            children = [child for child in children if child.id]
            if not children:
                results.append(
                    Resolved(
                        item=item,
                        kind="skip",
                        reason=SKIP_CONTAINER_EMPTY, depth=depth, origin=origin
                    )
                )
                continue
            for child in children:
                queue.append((child, origin or item, depth + 1))
            continue

        path = item.path or ""
        if path.lower().endswith(".strm") and not include_strm:
            results.append(
                Resolved(item=item, kind="skip", reason=SKIP_STRM, depth=depth, origin=origin)
            )
            continue

        match = matcher.match(path)
        if match is None:
            results.append(
                Resolved(
                    item=item,
                    kind="skip",
                    reason=SKIP_OUTSIDE_ROOTS,
                    depth=depth,
                    origin=origin,
                )
            )
            continue

        source = os.path.join(match.root.local, match.relative) if match.relative else match.root.local
        source = os.path.normpath(source)
        dest = os.path.normpath(_destination_for(dest_root, match.root, match.relative))

        if not _inside(dest_root, dest):
            results.append(
                Resolved(
                    item=item,
                    kind="skip",
                    reason="destination would escape the destination root",
                    depth=depth,
                    origin=origin,
                )
            )
            continue

        if source in seen_sources:
            results.append(
                Resolved(
                    item=item,
                    kind="skip",
                    reason=SKIP_DUPLICATE,
                    depth=depth,
                    origin=origin,
                )
            )
            continue

        if not os.path.exists(source):
            results.append(
                Resolved(item=item, kind="skip", reason=SKIP_NOT_FOUND, depth=depth, origin=origin)
            )
            continue

        seen_sources[source] = item.id
        kind = "dir" if os.path.isdir(source) else "file"
        resolved = Resolved(
            item=item,
            source=source,
            dest=dest,
            kind=kind,
            depth=depth,
            origin=origin,
        )
        results.append(resolved)

        # Extras (trailers and the like) are separate library items that live next
        # to the media file.  They are mirrored like any other file.
        if include_extras and kind == "file":
            for extra in _resolve_extras(item, matcher, dest_root, client, seen_sources):
                results.append(extra)

    return results


def _resolve_extras(
    item: Item,
    matcher: RootMatcher,
    dest_root: str,
    client: JellyfinClient,
    seen_sources: Dict[str, str],
) -> List[Resolved]:
    """Map the extras of an item to files, ignoring the ones we cannot place."""
    found: List[Resolved] = []
    for extra in client.extras_of(item):
        path = extra.path or ""
        match = matcher.match(path)
        if match is None:
            continue
        source = os.path.normpath(
            os.path.join(match.root.local, match.relative) if match.relative else match.root.local
        )
        dest = os.path.normpath(_destination_for(dest_root, match.root, match.relative))
        if not _inside(dest_root, dest):
            continue
        if source in seen_sources or not os.path.exists(source):
            continue
        seen_sources[source] = extra.id
        found.append(
            Resolved(
                item=extra,
                source=source,
                dest=dest,
                kind="file",
                is_extra=True,
            )
        )
    return found
