"""The copy engine.

Copying is done from the filesystem, never from Jellyfin's logical model.  A directory
favorite is walked in full and every file below it is copied, whatever the directory
happens to look like -- there is no expectation of season subdirectories, of a fixed
nesting depth, or of any particular file name.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from .config import SIDECAR_EXTENSIONS

LOG = logging.getLogger("favsav.sync")

STATUS_COPIED = "copied"
STATUS_UNCHANGED = "unchanged"
STATUS_FAILED = "failed"
STATUS_WOULD_COPY = "would copy"


@dataclass
class FileResult:
    """The outcome of copying one file."""

    source: str
    dest: str
    status: str
    error: str = ""
    size: int = 0

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_COPIED, STATUS_UNCHANGED, STATUS_WOULD_COPY)


@dataclass
class ItemResult:
    """The outcome of handling one favorite item."""

    name: str
    item_type: str
    kind: str
    source: Optional[str] = None
    dest: Optional[str] = None
    reason: str = ""
    files: List[FileResult] = field(default_factory=list)

    @property
    def copied(self) -> int:
        return sum(1 for f in self.files if f.status in (STATUS_COPIED, STATUS_WOULD_COPY))

    @property
    def unchanged(self) -> int:
        return sum(1 for f in self.files if f.status == STATUS_UNCHANGED)

    @property
    def failed(self) -> int:
        return sum(1 for f in self.files if f.status == STATUS_FAILED)

    @property
    def bytes_copied(self) -> int:
        return sum(f.size for f in self.files if f.status in (STATUS_COPIED, STATUS_WOULD_COPY))


def _same_file(source: str, dest: str) -> bool:
    """True when the destination already holds an identical copy.

    Size and modification time are compared.  The tolerance on the timestamp absorbs
    filesystems that store times with a coarse resolution.
    """
    try:
        src_stat = os.stat(source)
        dst_stat = os.stat(dest)
    except OSError:
        return False
    if src_stat.st_size != dst_stat.st_size:
        return False
    return abs(src_stat.st_mtime - dst_stat.st_mtime) < 2.0


def _link_file(source: str, dest: str, mode: str) -> FileResult:
    """Point the destination at the source instead of copying its bytes."""
    directory = os.path.dirname(dest)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        return FileResult(source, dest, STATUS_FAILED, str(exc))

    tmp = dest + ".favsav-tmp"
    try:
        if os.path.lexists(tmp):
            os.remove(tmp)
        if mode == "hardlink":
            os.link(source, tmp)
        else:
            os.symlink(os.path.abspath(source), tmp)
        os.replace(tmp, dest)
    except OSError as exc:
        try:
            if os.path.lexists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return FileResult(source, dest, STATUS_FAILED, str(exc))

    return FileResult(source, dest, STATUS_COPIED, size=0)


def sidecars_for(path: str, extensions: Sequence[str] = SIDECAR_EXTENSIONS) -> List[str]:
    """Return the sibling files of ``path`` that share its stem.

    This is the one place favsav looks at file names, and it looks only at the
    directory a media file already lives in.  Nothing about the directory's structure
    is assumed.
    """
    directory = os.path.dirname(path) or "."
    stem = os.path.splitext(os.path.basename(path))[0]
    if not os.path.isdir(directory):
        return []
    wanted = {ext.lower() for ext in extensions}
    found = []
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return []
    for entry in entries:
        if not entry.lower().endswith(tuple(wanted)):
            continue
        other_stem = os.path.splitext(entry)[0]
        if other_stem != stem:
            continue
        full = os.path.join(directory, entry)
        if os.path.abspath(full) == os.path.abspath(path):
            continue
        if not os.path.isfile(full):
            continue
        found.append(full)
    return found


def copy_file(source: str, dest: str, dry_run: bool = False, mode: str = "copy") -> FileResult:
    """Copy one file, atomically.

    The data is written to a temporary name next to the destination and renamed into
    place, so an interrupted run can never leave a half-written file where a complete
    one is expected.

    With ``mode`` set to ``hardlink`` or ``symlink`` the destination is pointed at
    the source instead of holding a second copy of the bytes.
    """
    try:
        size = os.path.getsize(source)
    except OSError as exc:
        return FileResult(source, dest, STATUS_FAILED, str(exc))

    if dry_run:
        return FileResult(source, dest, STATUS_WOULD_COPY, size=size)

    if mode in ("hardlink", "symlink"):
        return _link_file(source, dest, mode)

    directory = os.path.dirname(dest)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        return FileResult(source, dest, STATUS_FAILED, str(exc))

    tmp = dest + ".favsav-tmp"
    try:
        with open(source, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(source, tmp)
        os.replace(tmp, dest)
    except OSError as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return FileResult(source, dest, STATUS_FAILED, str(exc))
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise

    return FileResult(source, dest, STATUS_COPIED, size=size)


def _walk_files(root: str, follow_symlinks: bool = False) -> Iterable[Tuple[str, str]]:
    """Yield ``(absolute_path, relative_path)`` for every file under ``root``.

    Symlinks are not followed by default: a symlink pointing outside the library
    would otherwise pull unrelated data into the backup.
    """
    if os.path.isfile(root):
        yield root, os.path.basename(root)
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        dirnames.sort()
        for filename in sorted(filenames):
            full = os.path.join(dirpath, filename)
            if not follow_symlinks and os.path.islink(full):
                LOG.warning("skipping symlink: %s", full)
                continue
            if not os.path.isfile(full):
                continue
            yield full, os.path.relpath(full, root)


def plan_files(source: str, dest: str, include_sidecars: bool = True) -> List[Tuple[str, str]]:
    """Return the ``(source, dest)`` pairs that make up one favorite.

    For a directory this is every file in the tree, plus the sidecars of every media
    file among them.
    """
    pairs: List[Tuple[str, str]] = []
    if os.path.isfile(source):
        pairs.append((source, dest))
        if include_sidecars:
            for sidecar in sidecars_for(source):
                pairs.append((sidecar, os.path.join(os.path.dirname(dest), os.path.basename(sidecar))))
        return pairs

    seen = set()
    for full, relative in _walk_files(source):
        target = os.path.join(dest, relative)
        pairs.append((full, target))
        seen.add(target)
        if include_sidecars:
            for sidecar in sidecars_for(full):
                sidecar_target = os.path.join(
                    os.path.dirname(target), os.path.basename(sidecar)
                )
                if sidecar_target in seen:
                    continue
                seen.add(sidecar_target)
                pairs.append((sidecar, sidecar_target))
    return pairs


def copy_item(
    source: str,
    dest: str,
    include_sidecars: bool = True,
    skip_unchanged: bool = True,
    dry_run: bool = False,
    follow_symlinks: bool = False,
    mode: str = "copy",
) -> List[FileResult]:
    """Copy one favorite (file or directory) and return the per-file results."""
    results: List[FileResult] = []

    if os.path.isdir(source):
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as exc:
            return [FileResult(source, dest, STATUS_FAILED, str(exc))]
        # Recreate empty directories so the mirror stays faithful.
        for dirpath, dirnames, filenames in os.walk(source, followlinks=follow_symlinks):
            relative = os.path.relpath(dirpath, source)
            if relative == ".":
                continue
            target_dir = os.path.join(dest, relative)
            if dry_run:
                continue
            try:
                os.makedirs(target_dir, exist_ok=True)
            except OSError as exc:
                results.append(FileResult(dirpath, target_dir, STATUS_FAILED, str(exc)))

    for src, target in plan_files(source, dest, include_sidecars=include_sidecars):
        if skip_unchanged and os.path.exists(target) and _same_file(src, target):
            results.append(FileResult(src, target, STATUS_UNCHANGED, size=os.path.getsize(src)))
            continue
        results.append(copy_file(src, target, dry_run=dry_run, mode=mode))
    return results


def free_space(path: str) -> Optional[Tuple[int, int]]:
    """Return ``(free_bytes, total_bytes)`` for the filesystem holding ``path``."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return usage.free, usage.total


def human_bytes(value: int) -> str:
    """Format a byte count for humans."""
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(size) < 1024.0 or unit == "PiB":
            if unit == "B":
                return "%d B" % int(size)
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f PiB" % size  # pragma: no cover - unreachable
