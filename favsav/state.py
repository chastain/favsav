"""The manifest: what favsav copied, and therefore what it may prune.

Pruning is the only destructive thing favsav does, so it is deliberately narrow.  A file
is only ever removed if it appears in the manifest, which means favsav put it there
itself.  Anything else in the destination is left alone, even if it looks like a
leftover.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Tuple

LOG = logging.getLogger("favsav.state")

MANIFEST_VERSION = 1


@dataclass
class Entry:
    """One file favsav copied."""

    relative: str
    source: str
    size: int = 0
    mtime: float = 0.0
    item: str = ""
    item_type: str = ""

    def to_json(self) -> Dict[str, object]:
        return {
            "source": self.source,
            "size": self.size,
            "mtime": self.mtime,
            "item": self.item,
            "item_type": self.item_type,
        }

    @classmethod
    def from_json(cls, relative: str, data: Dict[str, Any]) -> "Entry":
        return cls(
            relative=relative,
            source=str(data.get("source") or ""),
            size=int(data.get("size") or 0),
            mtime=float(data.get("mtime") or 0.0),
            item=str(data.get("item") or ""),
            item_type=str(data.get("item_type") or ""),
        )


@dataclass
class Manifest:
    """The set of files favsav believes it owns in the destination."""

    version: int = MANIFEST_VERSION
    entries: Dict[str, Entry] = field(default_factory=dict)

    # ------------------------------------------------------------------ load

    @classmethod
    def load(cls, path: str) -> "Manifest":
        manifest = cls()
        if not path or not os.path.exists(path):
            return manifest
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            LOG.error("could not read the manifest at %s: %s", path, exc)
            return manifest
        if not isinstance(data, dict):
            LOG.error("the manifest at %s is not a JSON object; ignoring it", path)
            return manifest
        raw_entries = data.get("entries")
        if not isinstance(raw_entries, dict):
            LOG.error("the manifest at %s has no usable entries; ignoring it", path)
            return manifest
        for relative, entry in raw_entries.items():
            if not isinstance(entry, dict):
                continue
            manifest.entries[str(relative)] = Entry.from_json(str(relative), entry)
        LOG.info("loaded %d entries from %s", len(manifest.entries), path)
        return manifest

    # ------------------------------------------------------------------ save

    def save(self, path: str) -> None:
        """Write the manifest atomically."""
        if not path:
            return
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        payload = {
            "version": self.version,
            "entries": {key: entry.to_json() for key, entry in sorted(self.entries.items())},
        }
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=".state-", suffix=".tmp", delete=False
        )
        try:
            with handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            # Group-writable so that the service account and an admin running favsav
            # by hand can both overwrite a manifest created by the other.
            os.chmod(handle.name, 0o660)
            os.replace(handle.name, path)
        except BaseException:
            try:
                os.remove(handle.name)
            except OSError:
                pass
            raise

    # ---------------------------------------------------------------- update

    def record(
        self,
        relative: str,
        source: str,
        size: int = 0,
        mtime: float = 0.0,
        item: str = "",
        item_type: str = "",
    ) -> None:
        self.entries[relative] = Entry(
            relative=relative,
            source=source,
            size=size,
            mtime=mtime,
            item=item,
            item_type=item_type,
        )

    def forget(self, relative: str) -> None:
        self.entries.pop(relative, None)

    def owned_by(self, relative: str) -> bool:
        return relative in self.entries

    def paths(self) -> List[str]:
        return sorted(self.entries.keys())


def relative_to(dest_root: str, path: str) -> str:
    """Return ``path`` relative to ``dest_root``, or the absolute path if outside."""
    base = os.path.abspath(dest_root)
    target = os.path.abspath(path)
    if target == base:
        return ""
    if target.startswith(base + os.sep):
        return target[len(base) + 1 :]
    return target


def prune(
    manifest: Manifest,
    dest_root: str,
    wanted: Iterable[str],
    dry_run: bool = False,
) -> List[Tuple[str, Entry]]:
    """Remove manifest files that are no longer wanted.

    Only files recorded in the manifest are considered, and only files inside the
    destination root are ever removed.  Returns the list of ``(relative_path, entry)``
    that were (or would be) deleted.
    """
    keep = {os.path.normpath(path) for path in wanted}
    removed: List[Tuple[str, Entry]] = []

    for relative in manifest.paths():
        if os.path.normpath(relative) in keep:
            continue
        entry = manifest.entries[relative]
        target = os.path.normpath(os.path.join(dest_root, relative))
        base = os.path.abspath(dest_root)
        absolute = os.path.abspath(target)
        if not (absolute == base or absolute.startswith(base + os.sep)):
            LOG.warning("refusing to prune %s: outside the destination root", relative)
            continue
        try:
            if os.path.isdir(absolute) and not os.path.islink(absolute):
                LOG.warning("refusing to prune %s: it is a directory", relative)
                continue
            if dry_run:
                LOG.info("would remove %s", absolute)
            else:
                os.remove(absolute)
                LOG.info("removed %s", absolute)
        except FileNotFoundError:
            pass
        except OSError as exc:
            LOG.error("could not remove %s: %s", absolute, exc)
            continue
        if not dry_run:
            manifest.forget(relative)
        removed.append((relative, entry))

    return removed
