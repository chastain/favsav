"""The run report and the optional email notification.

Sending mail is optional in every sense.  If SMTP is not configured, this module does
not open a socket, does not raise, and does not influence the exit code of the run.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import List, Optional, Tuple

from .config import SmtpConfig
from .sync import human_bytes

LOG = logging.getLogger("favsav.notify")


class NotifyError(Exception):
    """Raised when a message could not be handed to the SMTP server."""


@dataclass
class RunReport:
    """Everything worth telling the user about one run."""

    started: datetime
    finished: datetime
    dry_run: bool = False
    #: ``(item name, destination path, size in bytes)`` for every file copied.
    copied: List[Tuple[str, str, int]] = field(default_factory=list)
    pruned: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    unchanged: int = 0
    dest_free: Optional[int] = None
    dest_total: Optional[int] = None
    dest_path: str = ""
    source_free: Optional[int] = None
    source_total: Optional[int] = None
    source_path: str = ""
    item_count: int = 0

    @property
    def changed(self) -> bool:
        """True when something happened worth reporting."""
        return bool(self.copied or self.pruned or self.failures)

    @property
    def copied_count(self) -> int:
        return len(self.copied)

    @property
    def pruned_count(self) -> int:
        return len(self.pruned)

    @property
    def copied_bytes(self) -> int:
        return sum(size for _item, _path, size in self.copied)

    def add_copy(self, item: str, path: str, size: int = 0) -> None:
        self.copied.append((item, path, size))


def _format_disk(free: Optional[int], total: Optional[int]) -> str:
    if free is None or total is None:
        return "unknown"
    used = max(0, total - free)
    percent = (float(used) / float(total) * 100.0) if total else 0.0
    return "%s free of %s (%.1f%% used)" % (human_bytes(free), human_bytes(total), percent)


def build_report(report: RunReport, config: SmtpConfig) -> Tuple[str, str]:
    """Build the subject and body of the notification."""
    if report.failures:
        headline = "failed"
    elif report.copied_count:
        headline = "%d file%s copied" % (report.copied_count, "" if report.copied_count == 1 else "s")
    elif report.pruned_count:
        headline = "%d file%s removed" % (report.pruned_count, "" if report.pruned_count == 1 else "s")
    else:
        headline = "no changes"

    prefix = config.subject_prefix or ""
    subject = "%s%s" % (prefix, headline)
    if report.dry_run:
        subject = "%s%s (dry run)" % (prefix, headline)

    lines: List[str] = []
    lines.append("favsav run report")
    lines.append("=" * 60)
    lines.append("")
    lines.append("Started:   %s" % format_datetime(report.started))
    lines.append("Finished:  %s" % format_datetime(report.finished))
    if report.dry_run:
        lines.append("Mode:      DRY RUN - nothing was written or deleted")
    lines.append("")

    lines.append("Summary")
    lines.append("-" * 60)
    lines.append("  Favorites handled:  %d" % report.item_count)
    lines.append("  Files copied:       %d" % report.copied_count)
    lines.append("  Files removed:      %d" % report.pruned_count)
    lines.append("  Files unchanged:    %d" % report.unchanged)
    lines.append("  Failures:           %d" % len(report.failures))
    lines.append("")

    lines.append("Disk space")
    lines.append("-" * 60)
    if report.dest_path:
        lines.append("  Destination %s" % report.dest_path)
        lines.append("    %s" % _format_disk(report.dest_free, report.dest_total))
    if config.include_source_disk and report.source_path:
        lines.append("  Source %s" % report.source_path)
        lines.append("    %s" % _format_disk(report.source_free, report.source_total))
    lines.append("")

    if report.copied:
        lines.append("Copied files")
        lines.append("-" * 60)
        for item, path, size in report.copied:
            lines.append("  %s" % path)
            lines.append("      from: %s" % item)
            if size:
                lines.append("      size: %s" % human_bytes(size))
        lines.append("")

    if report.pruned and config.include_removed:
        lines.append("Removed files")
        lines.append("-" * 60)
        for path in report.pruned:
            lines.append("  %s" % path)
        lines.append("")

    if report.failures:
        lines.append("Failures")
        lines.append("-" * 60)
        for failure in report.failures:
            lines.append("  %s" % failure)
        lines.append("")

    if report.skipped:
        lines.append("Skipped")
        lines.append("-" * 60)
        for name, reason in report.skipped:
            lines.append("  %s (%s)" % (name, reason))
        lines.append("")

    body = "\n".join(lines)
    return subject, body


def _security_mode(config: SmtpConfig) -> str:
    """Resolve the effective transport security."""
    if config.security != "auto":
        return config.security
    if config.port == 465:
        return "ssl"
    return "starttls"


def send_email(config: SmtpConfig, subject: str, body: str) -> None:
    """Send one message.

    Raises :class:`NotifyError` if the message could not be delivered.  The caller
    decides how serious that is; it never decides whether to try at all, which is
    settled by :meth:`SmtpConfig.unconfigured_reason`.
    """
    mode = _security_mode(config)
    sender = config.from_addr or config.username
    if not sender:
        raise NotifyError("no sender address configured")
    recipients = [addr for addr in config.to_addrs if addr]
    if not recipients:
        raise NotifyError("no recipients configured")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = (
        "%s <%s>" % (config.from_name, sender) if config.from_name else sender
    )
    message["To"] = ", ".join(recipients)
    message["Date"] = format_datetime(datetime.now())
    message["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    message.set_content(body)

    try:
        if mode == "ssl":
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                config.host, config.port, timeout=config.timeout, context=context
            ) as client:
                _authenticate(client, config)
                client.send_message(message, from_addr=sender, to_addrs=recipients)
        elif mode == "starttls":
            with smtplib.SMTP(config.host, config.port, timeout=config.timeout) as client:
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
                _authenticate(client, config)
                client.send_message(message, from_addr=sender, to_addrs=recipients)
        else:
            with smtplib.SMTP(config.host, config.port, timeout=config.timeout) as client:
                client.ehlo()
                _authenticate(client, config)
                client.send_message(message, from_addr=sender, to_addrs=recipients)
    except (smtplib.SMTPException, socket.error, OSError) as exc:
        raise NotifyError("could not send the notification: %s" % exc)


def _authenticate(client: smtplib.SMTP, config: SmtpConfig) -> None:
    if config.username:
        client.login(config.username, config.password or "")


def notify(report: RunReport, config: SmtpConfig) -> Optional[str]:
    """Send the report if it is worth sending and mail is configured.

    Returns a short description of what happened, or ``None`` when nothing was sent.
    Never raises: a failure to send mail is reported by the return value so the caller
    can decide whether it matters.
    """
    reason = config.unconfigured_reason()
    if reason is not None:
        LOG.info("not sending a notification: %s", reason)
        return None

    if not report.changed and not config.notify_on_noop:
        LOG.info("nothing changed, not sending a notification")
        return None

    subject, body = build_report(report, config)
    try:
        send_email(config, subject, body)
    except NotifyError as exc:
        LOG.error("%s", exc)
        return "failed: %s" % exc
    return "sent"
