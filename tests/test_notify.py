"""Tests for the report and the optional notification.

The rule these tests exist to protect: when SMTP is not configured, favsav must not
touch the network for mail, must not raise, and must not change the exit code.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from favsav.config import SmtpConfig
from favsav.notify import RunReport, build_report, notify, send_email


def make_report(**kwargs) -> RunReport:
    now = datetime(2024, 5, 1, 12, 0, 0)
    report = RunReport(started=now, finished=now)
    for key, value in kwargs.items():
        setattr(report, key, value)
    return report


class UnconfiguredSmtpTests(unittest.TestCase):
    def test_nothing_is_sent_when_smtp_is_disabled(self):
        config = SmtpConfig(enabled=False)
        report = make_report(copied=[("film", "Movies/film.mkv", 10)])
        self.assertIsNone(notify(report, config))

    def test_nothing_is_sent_when_the_host_is_missing(self):
        config = SmtpConfig(enabled=True, to_addrs=["me@example.com"])
        report = make_report(copied=[("film", "Movies/film.mkv", 10)])
        self.assertIsNone(notify(report, config))

    def test_nothing_is_sent_when_there_is_no_recipient(self):
        config = SmtpConfig(enabled=True, host="smtp.example.com")
        report = make_report(copied=[("film", "Movies/film.mkv", 10)])
        self.assertIsNone(notify(report, config))

    def test_nothing_is_sent_when_the_run_changed_nothing(self):
        config = SmtpConfig(
            enabled=True, host="smtp.example.com", to_addrs=["me@example.com"]
        )
        report = make_report()
        self.assertIsNone(notify(report, config))

    def test_a_no_op_run_can_be_asked_to_notify(self):
        sent = []

        config = SmtpConfig(
            enabled=True,
            host="smtp.example.com",
            to_addrs=["me@example.com"],
            notify_on_noop=True,
        )
        # Patch the sender so nothing leaves the machine.
        import favsav.notify as module

        original = module.send_email
        module.send_email = lambda cfg, subject, body: sent.append((subject, body))
        try:
            outcome = notify(make_report(), config)
        finally:
            module.send_email = original
        self.assertEqual("sent", outcome)
        self.assertEqual(1, len(sent))


class BuildReportTests(unittest.TestCase):
    def setUp(self):
        self.config = SmtpConfig(subject_prefix="[favsav] ")

    def test_every_copied_file_is_listed_in_full(self):
        report = make_report(
            copied=[
                ("Chopping Mall", "Videos/Movies/Chopping.Mall.1986/Chopping.Mall.1986.mp4", 100),
                ("Dream Warriors", "Videos/Movies/Dream.Warriors/Dream.Warriors.1080p.mp4", 200),
            ]
        )
        _subject, body = build_report(report, self.config)
        self.assertIn(
            "Videos/Movies/Chopping.Mall.1986/Chopping.Mall.1986.mp4", body
        )
        self.assertIn("Videos/Movies/Dream.Warriors/Dream.Warriors.1080p.mp4", body)

    def test_every_removed_file_is_listed_in_full(self):
        report = make_report(pruned=["Videos/Movies/Old/Old.1980.mkv"])
        _subject, body = build_report(report, self.config)
        self.assertIn("Videos/Movies/Old/Old.1980.mkv", body)

    def test_the_disk_space_is_reported(self):
        report = make_report(dest_free=50 * 1024**3, dest_total=500 * 1024**3, dest_path="/mnt/backup")
        _subject, body = build_report(report, self.config)
        self.assertIn("Disk space", body)
        self.assertIn("/mnt/backup", body)
        self.assertIn("50.0 GiB free", body)

    def test_the_counts_are_reported(self):
        report = make_report(
            copied=[("a", "a.mkv", 1)],
            pruned=["b.mkv"],
            failures=["c.mkv: disk full"],
        )
        _subject, body = build_report(report, self.config)
        self.assertIn("Files copied:       1", body)
        self.assertIn("Files removed:      1", body)
        self.assertIn("Failures:           1", body)

    def test_the_subject_says_what_happened(self):
        report = make_report(copied=[("a", "a.mkv", 1)])
        subject, _body = build_report(report, self.config)
        self.assertEqual("[favsav] 1 file copied", subject)

    def test_the_subject_says_when_something_failed(self):
        report = make_report(failures=["a.mkv: no space"])
        subject, _body = build_report(report, self.config)
        self.assertEqual("[favsav] failed", subject)

    def test_a_dry_run_is_marked_as_one(self):
        report = make_report(dry_run=True, copied=[("a", "a.mkv", 1)])
        subject, body = build_report(report, self.config)
        self.assertIn("dry run", subject)
        self.assertIn("DRY RUN", body)

    def test_removed_files_can_be_left_out(self):
        config = SmtpConfig(subject_prefix="", include_removed=False)
        report = make_report(pruned=["secret.mkv"])
        _subject, body = build_report(report, config)
        self.assertNotIn("secret.mkv", body)


class SendEmailTests(unittest.TestCase):
    def test_a_missing_sender_is_an_error(self):
        config = SmtpConfig(host="smtp.example.com", to_addrs=["me@example.com"])
        with self.assertRaises(Exception):
            send_email(config, "subject", "body")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
