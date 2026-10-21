"""Configuration loading.

The configuration is an INI file (read with :mod:`configparser`, which is in the
standard library on every supported Python version).  Secrets may live either in the
INI file or in an environment file such as ``/etc/favsav/favsav.env``; the environment
takes precedence so that the INI file can stay world-readable.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from typing import List, Optional

DEFAULT_CONFIG_PATH = "/etc/favsav/config.ini"
DEFAULT_STATE_DIR = "/var/lib/favsav"
DEFAULT_STATE_FILE = os.path.join(DEFAULT_STATE_DIR, "state.json")

#: Extensions that are treated as sidecars of a media file.  Jellyfin consumes these
#: at scan time and does not expose them as library items, so they can only be found by
#: looking at the source directory.
SIDECAR_EXTENSIONS = (
    ".srt",
    ".sub",
    ".ass",
    ".ssa",
    ".vtt",
    ".nfo",
    ".jpg",
    ".jpeg",
    ".png",
    ".txt",
)

#: Item types favsav knows how to treat as containers: they have no file of their own
#: and must be expanded into their children.
CONTAINER_TYPES = ("BoxSet", "Playlist", "CollectionFolder")


class ConfigError(Exception):
    """Raised when the configuration is missing or nonsensical."""


@dataclass
class Root:
    """One mirror root.

    ``source`` is the path prefix as Jellyfin reports it, ``local`` is where those
    files actually live for this tool, and ``subdir`` optionally places the mirrored
    tree in a named subdirectory of the destination root.
    """

    source: str
    local: str
    subdir: str = ""

    @property
    def dest_base(self) -> str:
        return os.path.join(self.local, self.subdir) if self.subdir else self.local


@dataclass
class SmtpConfig:
    """Everything needed to send the run report by email."""

    enabled: bool = False
    host: str = ""
    port: int = 587
    security: str = "auto"
    username: str = ""
    password: str = ""
    from_addr: str = ""
    from_name: str = ""
    to_addrs: List[str] = field(default_factory=list)
    subject_prefix: str = "[favsav] "
    timeout: int = 30
    notify_on_noop: bool = False
    include_removed: bool = True
    include_source_disk: bool = True
    max_list_items: int = 0

    def unconfigured_reason(self) -> Optional[str]:
        """Return why notifications cannot be sent, or ``None`` if they can.

        The check runs before any socket is opened so that an installation without
        SMTP settings never touches the network for mail.
        """
        if not self.enabled:
            return "smtp.enabled is false"
        if not self.host:
            return "no smtp host configured"
        if not self.to_addrs:
            return "no smtp recipient configured"
        if self.username and not self.password:
            return "smtp username set but no password"
        return None


@dataclass
class Config:
    """The whole configuration, already validated."""

    path: Optional[str] = None

    # [jellyfin]
    jellyfin_url: str = "http://127.0.0.1:8096"
    api_key: str = ""
    jellyfin_timeout: int = 30
    users: List[str] = field(default_factory=list)

    # [paths]
    roots: List[Root] = field(default_factory=list)
    dest_root: str = ""

    # [dest]
    require_mount: bool = True
    device: str = ""

    # [copy]
    copy_mode: str = "copy"
    include_sidecars: bool = True
    include_extras: bool = True
    include_strm: bool = False
    follow_symlinks: bool = False
    skip_unchanged: bool = True

    # [smtp]
    smtp: SmtpConfig = field(default_factory=SmtpConfig)

    # [behaviour]
    dry_run: bool = True
    item_types: List[str] = field(default_factory=list)
    max_expansion_depth: int = 5

    # [state]
    state_file: str = DEFAULT_STATE_FILE
    state_dir: str = DEFAULT_STATE_DIR

    @property
    def dest_state_file(self) -> str:
        """A copy of the manifest kept beside the data, so a restored backup carries it."""
        return os.path.join(self.dest_root, ".favsav", "state.json")

    @property
    def report_file(self) -> str:
        return os.path.join(self.state_dir, "last-report.txt")


def _get_bool(parser: configparser.ConfigParser, section: str, option: str, default: bool) -> bool:
    if not parser.has_option(section, option):
        return default
    raw = parser.get(section, option).strip().lower()
    if raw in ("1", "yes", "true", "on"):
        return True
    if raw in ("0", "no", "false", "off"):
        return False
    raise ConfigError("[%s] %s: expected a boolean, got %r" % (section, option, raw))


def _get_int(parser: configparser.ConfigParser, section: str, option: str, default: int) -> int:
    if not parser.has_option(section, option):
        return default
    raw = parser.get(section, option).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError("[%s] %s: expected an integer, got %r" % (section, option, raw))


def _get_list(parser: configparser.ConfigParser, section: str, option: str) -> List[str]:
    if not parser.has_option(section, option):
        return []
    raw = parser.get(section, option)
    return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]


def _normalise_prefix(value: str) -> str:
    value = value.strip()
    if len(value) > 1:
        value = value.rstrip("/")
    return value


def parse_roots(value: str) -> List[Root]:
    """Parse the ``roots`` option.

    Each comma-separated entry is ``<source>``, ``<source>=<local>`` or
    ``<source>=<local>=<subdir>``.
    """
    roots: List[Root] = []
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("=")
        if len(parts) > 3:
            raise ConfigError("roots: too many '=' in %r" % entry)
        source = _normalise_prefix(parts[0])
        local = _normalise_prefix(parts[1]) if len(parts) > 1 and parts[1].strip() else source
        subdir = parts[2].strip().strip("/") if len(parts) > 2 else ""
        if not source:
            raise ConfigError("roots: empty source prefix in %r" % entry)
        roots.append(Root(source=source, local=local, subdir=subdir))
    return roots


def load(path: Optional[str] = None) -> Config:
    """Load and validate the configuration.

    ``path`` may be omitted, in which case :data:`DEFAULT_CONFIG_PATH` is used.  A
    missing file is only an error when it was explicitly requested; otherwise the
    built-in defaults are used so that ``favsav --help`` and ``--init-config`` work
    without an installation.
    """
    explicit = path is not None
    path = path or DEFAULT_CONFIG_PATH

    parser = configparser.ConfigParser()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except FileNotFoundError:
        if explicit:
            raise ConfigError("configuration file not found: %s" % path)
    except configparser.Error as exc:
        raise ConfigError("could not parse %s: %s" % (path, exc))

    config = Config(path=path if explicit else None)

    config.jellyfin_url = parser.get("jellyfin", "url", fallback="http://127.0.0.1:8096").strip()
    config.api_key = os.environ.get("FAVSAV_API_KEY", "").strip() or parser.get(
        "jellyfin", "api_key", fallback=""
    ).strip()
    config.jellyfin_timeout = _get_int(parser, "jellyfin", "timeout", 30)
    config.users = _get_list(parser, "jellyfin", "users")

    roots_raw = parser.get("paths", "roots", fallback="").strip()
    if roots_raw:
        config.roots = parse_roots(roots_raw)
    config.dest_root = parser.get("paths", "dest_root", fallback="").strip()

    config.require_mount = _get_bool(parser, "dest", "require_mount", True)
    config.device = parser.get("dest", "device", fallback="").strip()

    config.copy_mode = parser.get("copy", "mode", fallback="copy").strip().lower()
    if config.copy_mode not in ("copy", "hardlink", "symlink"):
        raise ConfigError("[copy] mode: expected copy, hardlink or symlink")
    config.include_sidecars = _get_bool(parser, "copy", "include_sidecars", True)
    config.include_extras = _get_bool(parser, "copy", "include_extras", True)
    config.include_strm = _get_bool(parser, "copy", "include_strm", False)
    config.follow_symlinks = _get_bool(parser, "copy", "follow_symlinks", False)
    config.skip_unchanged = _get_bool(parser, "copy", "skip_unchanged", True)

    smtp = SmtpConfig()
    smtp.enabled = _get_bool(parser, "smtp", "enabled", False)
    smtp.host = parser.get("smtp", "host", fallback="").strip()
    smtp.port = _get_int(parser, "smtp", "port", 587)
    smtp.security = parser.get("smtp", "security", fallback="auto").strip().lower()
    if smtp.security not in ("auto", "starttls", "ssl", "none"):
        raise ConfigError("[smtp] security: expected auto, starttls, ssl or none")
    smtp.username = parser.get("smtp", "username", fallback="").strip()
    smtp.password = os.environ.get("FAVSAV_SMTP_PASSWORD", "") or parser.get(
        "smtp", "password", fallback=""
    )
    smtp.from_addr = parser.get("smtp", "from", fallback="").strip() or smtp.username
    smtp.from_name = parser.get("smtp", "from_name", fallback="favsav").strip()
    smtp.to_addrs = _get_list(parser, "smtp", "to")
    smtp.subject_prefix = parser.get("smtp", "subject_prefix", fallback="[favsav] ")
    smtp.timeout = _get_int(parser, "smtp", "timeout", 30)
    smtp.notify_on_noop = _get_bool(parser, "smtp", "notify_on_noop", False)
    smtp.include_removed = _get_bool(parser, "smtp", "include_removed", True)
    smtp.include_source_disk = _get_bool(parser, "smtp", "include_source_disk", True)
    smtp.max_list_items = _get_int(parser, "smtp", "max_list_items", 0)
    config.smtp = smtp

    config.dry_run = _get_bool(parser, "behaviour", "dry_run", True)
    config.item_types = _get_list(parser, "behaviour", "item_types")
    config.max_expansion_depth = _get_int(parser, "behaviour", "max_expansion_depth", 5)

    config.state_dir = parser.get("state", "dir", fallback=DEFAULT_STATE_DIR).strip()
    config.state_file = parser.get("state", "file", fallback="").strip() or os.path.join(
        config.state_dir, "state.json"
    )

    return config


def validate(config: Config) -> None:
    """Check the parts of the configuration that are needed for a real run."""
    if not config.jellyfin_url:
        raise ConfigError("[jellyfin] url is required")
    if not config.api_key:
        raise ConfigError(
            "no Jellyfin API key configured (set [jellyfin] api_key or FAVSAV_API_KEY)"
        )
    if not config.roots:
        raise ConfigError("[paths] roots is required, e.g. roots = /srv/jellyfin-media")
    if not config.dest_root:
        raise ConfigError("[paths] dest_root is required")
    for root in config.roots:
        if not root.local.startswith("/"):
            raise ConfigError("[paths] roots: local prefix must be absolute: %r" % root.local)


# --------------------------------------------------------------------- example

#: Where the example configuration lives, for a source checkout.
EXAMPLE_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "packaging",
    "config.ini.example",
)


def example_config() -> str:
    """Return the example configuration.

    The file in ``packaging/`` is the single source of truth: it is what the
    installer copies and what ``--init-config`` writes.  Keeping one copy means the
    two can never drift apart.  The built-in text is only a fallback for the case
    where favsav has been installed without it.
    """
    try:
        with open(EXAMPLE_CONFIG_PATH, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return _BUILTIN_EXAMPLE


_BUILTIN_EXAMPLE = """\
# favsav configuration.
#
# Copy this file to /etc/favsav/config.ini and edit it.  See the README for a
# full description of every option.

[jellyfin]
url = http://127.0.0.1:8096
api_key =

[paths]
roots = /srv/jellyfin-media
dest_root = /mnt/backup

[behaviour]
dry_run = true
"""
