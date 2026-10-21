"""Command line interface.

Exit codes
----------
``0``  success
``1``  something failed (copy errors, or a notification failure with
       ``--strict-notify``)
``2``  a usage or configuration problem
``3``  the run succeeded but the notification could not be sent
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__, config as config_module, mapping, notify, state, sync
from .config import Config, ConfigError
from .jellyfin import (
    JellyfinAuthError,
    JellyfinClient,
    JellyfinError,
    JellyfinUnavailable,
)
from .mapping import RootMatcher

LOG = logging.getLogger("favsav")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_NOTIFY = 3

#: Reasons the destination may be unusable.
DEST_MISSING = "the destination directory does not exist"
DEST_NOT_MOUNTED = "the destination is not a mounted filesystem"
DEST_WRONG_DEVICE = "the destination is not on the expected device"
DEST_NOT_WRITABLE = "the destination is not writable"


# --------------------------------------------------------------------- helpers


def _setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.INFO
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(levelname)-7s %(message)s",
    )


def _acquire_lock(path: str):
    """Take an exclusive lock so two runs cannot overlap."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle = open(path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise ConfigError("another favsav run is already in progress (%s)" % path)
        raise
    # Group-writable so that both the service account and an admin running favsav
    # by hand can acquire the lock.
    try:
        os.chmod(path, 0o660)
    except OSError:
        pass
    handle.write("%d\n" % os.getpid())
    handle.flush()
    return handle


def _device_of(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def check_destination(config: Config) -> List[str]:
    """Return a list of problems with the destination; empty means it is usable."""
    problems: List[str] = []
    dest = config.dest_root

    if not os.path.isdir(dest):
        problems.append(DEST_MISSING)
        return problems

    if config.require_mount and not os.path.ismount(dest):
        problems.append(DEST_NOT_MOUNTED)

    if config.device:
        expected = _device_of(config.device)
        actual = _device_of(dest)
        if expected is None:
            problems.append("the configured device %s does not exist" % config.device)
        elif actual is None or actual != expected:
            problems.append(DEST_WRONG_DEVICE)

    if not os.access(dest, os.W_OK | os.X_OK):
        problems.append(DEST_NOT_WRITABLE)

    return problems


def _write_test_file(directory: str) -> bool:
    probe = os.path.join(directory, ".favsav-write-test")
    try:
        with open(probe, "w") as handle:
            handle.write("")
        os.remove(probe)
    except OSError:
        return False
    return True


def _collect_favorites(
    client: JellyfinClient, config: Config
) -> Tuple[List, Dict[str, List[str]], Optional[str]]:
    """Return the union of every user's favorites.

    Favorites live per user, so the union over all users is taken and each item is
    credited to the users who marked it.

    A user id is also returned, so that container expansion (which needs a user
    context for virtual seasons) has one to work with.
    """
    users = client.find_users(config.users)
    if not users:
        raise JellyfinError("the server reported no users")

    items: Dict[str, Any] = {}
    favourites_by: Dict[str, List[str]] = {}
    for user in users:
        LOG.info("reading favorites for %s", user.name)
        for item in client.list_favorites(user.id, config.item_types or None):
            if not item.id:
                continue
            if item.id not in items:
                items[item.id] = item
            favourites_by.setdefault(item.id, []).append(user.name)
    LOG.info("%d favorite item(s) from %d user(s)", len(items), len(users))
    user_id = users[0].id if users else None
    return list(items.values()), favourites_by, user_id


def _source_disk(config: Config) -> Tuple[str, Optional[int], Optional[int]]:
    """Return the source path to report on, plus its free and total bytes."""
    for root in config.roots:
        if os.path.isdir(root.local):
            usage = sync.free_space(root.local)
            if usage:
                return root.local, usage[0], usage[1]
    return "", None, None


# ----------------------------------------------------------------- the commands


def cmd_list(config: Config, client: JellyfinClient) -> int:
    """Show the favorites and where each one would go."""
    items, by_user, user_id = _collect_favorites(client, config)
    matcher = RootMatcher(config.roots)
    resolved = mapping.resolve(
        items,
        matcher,
        config.dest_root,
        client,
        data_paths=client.data_paths(),
        include_strm=config.include_strm,
        include_extras=config.include_extras,
        max_depth=config.max_expansion_depth,
        user_id=user_id,
    )

    print("Favorite items: %d" % len(items))
    print()
    for entry in resolved:
        users = ", ".join(by_user.get(entry.item.id, []))
        if entry.copyable:
            print("%s  [%s]" % (entry.name, entry.item.type))
            print("    source: %s" % entry.source)
            print("    dest:   %s" % entry.dest)
        else:
            print("%s  [%s]  SKIPPED: %s" % (entry.name, entry.item.type, entry.reason))
        if users:
            print("    favorited by: %s" % users)
        print()
    return EXIT_OK


def cmd_verify(config: Config) -> int:
    """Check that everything the manifest claims is still in the destination."""
    manifest = state.Manifest.load(config.state_file)
    if not manifest.entries:
        mirror = state.Manifest.load(config.dest_state_file)
        if mirror.entries:
            LOG.info("using the manifest stored in the destination")
            manifest = mirror
    if not manifest.entries:
        print("The manifest is empty; there is nothing to verify.")
        return EXIT_OK

    missing: List[str] = []
    wrong: List[str] = []
    for relative in manifest.paths():
        entry = manifest.entries[relative]
        target = os.path.join(config.dest_root, relative)
        if not os.path.exists(target):
            missing.append(relative)
            continue
        try:
            size = os.path.getsize(target)
        except OSError:
            missing.append(relative)
            continue
        if entry.size and size != entry.size:
            wrong.append("%s (expected %d bytes, found %d)" % (relative, entry.size, size))

    for relative in missing:
        print("MISSING  %s" % relative)
    for note in wrong:
        print("CHANGED  %s" % note)

    print()
    print("%d file(s) in the manifest, %d missing, %d changed." % (
        len(manifest.entries), len(missing), len(wrong)
    ))
    return EXIT_FAILURE if (missing or wrong) else EXIT_OK


def cmd_sync(config: Config, client: JellyfinClient, args: argparse.Namespace) -> int:
    """The main run: copy, optionally prune, then report."""
    started = datetime.now()
    dry_run = config.dry_run

    items, by_user, user_id = _collect_favorites(client, config)
    matcher = RootMatcher(config.roots)
    resolved = mapping.resolve(
        items,
        matcher,
        config.dest_root,
        client,
        data_paths=client.data_paths(),
        include_strm=config.include_strm,
        include_extras=config.include_extras,
        max_depth=config.max_expansion_depth,
        user_id=user_id,
    )

    report = notify.RunReport(started=started, finished=started, dry_run=dry_run)
    report.item_count = len(items)

    manifest = state.Manifest.load(config.state_file)
    if not manifest.entries:
        mirror = state.Manifest.load(config.dest_state_file)
        if mirror.entries:
            LOG.info("using the manifest stored in the destination")
            manifest = mirror

    wanted: List[str] = []

    for entry in resolved:
        if not entry.copyable:
            report.skipped.append((entry.name, entry.reason))
            LOG.info("skipping %s: %s", entry.name, entry.reason)
            continue

        source = entry.source or ""
        dest = entry.dest or ""
        LOG.info("handling %s (%s) from %s", entry.name, entry.kind, source)
        results = sync.copy_item(
            source,
            dest,
            include_sidecars=config.include_sidecars,
            skip_unchanged=config.skip_unchanged,
            dry_run=dry_run,
            follow_symlinks=config.follow_symlinks,
            mode=config.copy_mode,
        )

        for result in results:
            relative = state.relative_to(config.dest_root, result.dest)
            if not relative:
                continue
            if result.status in (sync.STATUS_COPIED, sync.STATUS_UNCHANGED, sync.STATUS_WOULD_COPY):
                wanted.append(relative)
            if result.status == sync.STATUS_COPIED:
                report.add_copy(
                    entry.name, relative, result.size
                )
                LOG.info("copied %s", relative)
            elif result.status == sync.STATUS_WOULD_COPY:
                report.add_copy(entry.name, relative, result.size)
                LOG.info("would copy %s", relative)
            elif result.status == sync.STATUS_UNCHANGED:
                report.unchanged += 1
            else:
                message = "%s: %s" % (relative, result.error)
                report.failures.append(message)
                LOG.error("failed %s", message)

            if result.status in (sync.STATUS_COPIED, sync.STATUS_UNCHANGED):
                try:
                    stat_result = os.stat(result.source)
                    manifest.record(
                        relative,
                        result.source,
                        size=stat_result.st_size,
                        mtime=stat_result.st_mtime,
                        item=entry.name,
                        item_type=entry.item.type,
                    )
                except OSError:
                    manifest.record(relative, result.source, item=entry.name, item_type=entry.item.type)

    # ------------------------------------------------------------------ prune
    if args.prune:
        removed = state.prune(manifest, config.dest_root, wanted, dry_run=dry_run)
        for relative, _entry in removed:
            report.pruned.append(relative)
            LOG.info("pruned %s", relative)

    # ----------------------------------------------------------------- report
    report.finished = datetime.now()
    usage = sync.free_space(config.dest_root) if os.path.isdir(config.dest_root) else None
    if usage:
        report.dest_free, report.dest_total = usage
    report.dest_path = config.dest_root
    source_path, source_free, source_total = _source_disk(config)
    report.source_path = source_path
    report.source_free = source_free
    report.source_total = source_total

    if not dry_run:
        try:
            manifest.save(config.state_file)
            if config.dest_root and os.path.isdir(config.dest_root):
                manifest.save(config.dest_state_file)
        except OSError as exc:
            LOG.error("could not write the manifest: %s", exc)
            report.failures.append("could not write the manifest: %s" % exc)

    _write_report_file(config, report)

    print()
    print("Copied:   %d" % report.copied_count)
    print("Removed:  %d" % report.pruned_count)
    print("Unchanged:%d" % report.unchanged)
    print("Failed:   %d" % len(report.failures))
    if dry_run:
        print("(dry run - nothing was written)")

    notify_status = "disabled"
    if args.no_notify:
        notify_status = "disabled by --no-notify"
    else:
        outcome = notify.notify(report, config.smtp)
        if outcome is None:
            reason = config.smtp.unconfigured_reason()
            notify_status = "not sent (%s)" % (reason or "nothing changed")
        else:
            notify_status = outcome

    print("Notification: %s" % notify_status)

    if report.failures:
        return EXIT_FAILURE
    if notify_status.startswith("failed") and args.strict_notify:
        return EXIT_FAILURE
    if notify_status.startswith("failed"):
        return EXIT_NOTIFY
    return EXIT_OK


def _write_report_file(config: Config, report: notify.RunReport) -> None:
    """Write the same report to disk, so it is available even without mail."""
    try:
        subject, body = notify.build_report(report, config.smtp)
        os.makedirs(config.state_dir, exist_ok=True)
        with open(config.report_file, "w", encoding="utf-8") as handle:
            handle.write("Subject: %s\n\n" % subject)
            handle.write(body)
        try:
            os.chmod(config.report_file, 0o660)
        except OSError:
            pass
    except OSError as exc:
        LOG.warning("could not write the report file: %s", exc)


def cmd_init_config(path: str, force: bool) -> int:
    """Write an example configuration file."""
    if os.path.exists(path) and not force:
        print("%s already exists; pass --force to overwrite it." % path, file=sys.stderr)
        return EXIT_USAGE
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(config_module.example_config())
    except OSError as exc:
        print("could not write %s: %s" % (path, exc), file=sys.stderr)
        return EXIT_USAGE
    print("Wrote %s" % path)
    print("Edit it, then run: favsav --check-destination")
    return EXIT_OK


# -------------------------------------------------------------------- parsing


def _load_env_file(path: str) -> None:
    """Load KEY=VALUE pairs from an environment file.

    Only sets variables that are not already in the environment, so an explicit
    export always wins.  Comments and blank lines are ignored.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if key and key not in os.environ:
                    os.environ[key] = value
    except (OSError, IOError):
        pass


def _auto_load_env(config_path: Optional[str]) -> None:
    """Source the favsav environment file, like systemd's EnvironmentFile= does.

    This makes manual runs behave the same as service runs: the secrets in
    /etc/favsav/favsav.env (or next to the config file) are available without the
    user having to source them by hand.
    """
    candidates = []
    if os.environ.get("FAVSAV_ENV_FILE"):
        candidates.append(os.environ["FAVSAV_ENV_FILE"])
    candidates.append("/etc/favsav/favsav.env")
    if config_path:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(config_path)), "favsav.env"))
    for candidate in candidates:
        _load_env_file(candidate)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="favsav",
        description=(
            "Back up the media you marked as favorite in Jellyfin to a second disk."
        ),
        epilog=(
            "favsav never writes to Jellyfin, and never deletes anything it did not "
            "copy itself.  Run it with --dry-run first."
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="configuration file (default: %s)" % config_module.DEFAULT_CONFIG_PATH,
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the favorites and where they would go; change nothing",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="check that the destination still holds everything the manifest records",
    )
    parser.add_argument(
        "--check-destination",
        action="store_true",
        help="only check that the destination is usable, then exit",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=None,
        help="plan only; write nothing (this is the default)",
    )
    parser.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        default=None,
        help="actually copy and remove files",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="remove files that favsav copied for items that are no longer favorites",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="do not send the notification email for this run",
    )
    parser.add_argument(
        "--strict-notify",
        action="store_true",
        help="treat a failed notification as a failed run",
    )
    parser.add_argument(
        "--init-config",
        metavar="PATH",
        nargs="?",
        const=config_module.DEFAULT_CONFIG_PATH,
        help="write an example configuration file and exit",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --init-config, overwrite an existing file",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log everything")
    parser.add_argument("-q", "--quiet", action="store_true", help="log only warnings")
    parser.add_argument(
        "--version", action="version", version="favsav %s" % __version__
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.quiet)

    if args.init_config:
        return cmd_init_config(args.init_config, args.force)

    _auto_load_env(args.config)

    try:
        config = config_module.load(args.config)
    except ConfigError as exc:
        print("Configuration problem: %s" % exc, file=sys.stderr)
        return EXIT_USAGE

    if args.dry_run is not None:
        config.dry_run = args.dry_run

    if args.check_destination:
        problems = check_destination(config)
        if problems:
            for problem in problems:
                print("Destination problem: %s" % problem, file=sys.stderr)
            return EXIT_FAILURE
        print("Destination %s looks usable." % config.dest_root)
        return EXIT_OK

    lock_handle = None
    try:
        lock_handle = _acquire_lock(os.path.join(config.state_dir, "favsav.lock"))
    except (ConfigError, OSError) as exc:
        print("Could not lock: %s" % exc, file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.verify:
            # Verifying the destination does not need the Jellyfin server, so it
            # works even when the server is down.
            return cmd_verify(config)

        try:
            config_module.validate(config)
        except ConfigError as exc:
            print("Configuration problem: %s" % exc, file=sys.stderr)
            return EXIT_USAGE

        if args.list:
            client = JellyfinClient(
                config.jellyfin_url, config.api_key, timeout=config.jellyfin_timeout
            )
            try:
                return cmd_list(config, client)
            except JellyfinAuthError as exc:
                print("%s" % exc, file=sys.stderr)
                return EXIT_USAGE
            except JellyfinUnavailable as exc:
                print("Could not reach Jellyfin: %s" % exc, file=sys.stderr)
                return EXIT_FAILURE
            except JellyfinError as exc:
                print("Jellyfin error: %s" % exc, file=sys.stderr)
                return EXIT_FAILURE

        problems = check_destination(config)
        if problems:
            for problem in problems:
                print("Destination problem: %s" % problem, file=sys.stderr)
            print(
                "Refusing to run.  Fix the destination or pass --no-dry-run only "
                "after you understand why.",
                file=sys.stderr,
            )
            return EXIT_FAILURE

        client = JellyfinClient(
            config.jellyfin_url, config.api_key, timeout=config.jellyfin_timeout
        )
        try:
            return cmd_sync(config, client, args)
        except JellyfinAuthError as exc:
            print("%s" % exc, file=sys.stderr)
            return EXIT_USAGE
        except JellyfinUnavailable as exc:
            print("Could not reach Jellyfin: %s" % exc, file=sys.stderr)
            return EXIT_FAILURE
        except JellyfinError as exc:
            print("Jellyfin error: %s" % exc, file=sys.stderr)
            return EXIT_FAILURE
    finally:
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
            except OSError:
                pass


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
