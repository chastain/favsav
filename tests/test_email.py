"""Tests for sending the notification.

These run against a small SMTP server that speaks just enough of the protocol to
accept a message, so the transport is really exercised -- the socket is opened, the
TLS handshake happens and the message is delivered.
"""

from __future__ import annotations

import contextlib
import ssl
import unittest
from datetime import datetime
from unittest import mock

from tests.support import FakeSMTP
from favsav.config import SmtpConfig
from favsav.notify import NotifyError, RunReport, notify, send_email


@contextlib.contextmanager
def trusted_self_signed():
    """Let the client accept the stub server's self-signed certificate.

    The tests are about favsav's SMTP logic, not about the trust store, so the
    verification is switched off here rather than in the production code.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with mock.patch("favsav.notify.ssl.create_default_context", return_value=context):
        yield


def make_report(**kwargs) -> RunReport:
    now = datetime(2024, 5, 1, 12, 0, 0)
    report = RunReport(started=now, finished=now)
    for key, value in kwargs.items():
        setattr(report, key, value)
    return report


class SendTests(unittest.TestCase):
    def _config(self, port: int, **kwargs) -> SmtpConfig:
        config = SmtpConfig(
            enabled=True,
            host="127.0.0.1",
            port=port,
            security="none",
            username="me@example.com",
            password="secret",
            from_addr="me@example.com",
            from_name="favsav",
            to_addrs=["you@example.com"],
            timeout=10,
        )
        for key, value in kwargs.items():
            setattr(config, key, value)
        return config

    def test_a_message_is_delivered(self):
        server = FakeSMTP()
        port = server.start()
        self.addCleanup(server.stop)

        send_email(self._config(port), "the subject", "the body")

        self.assertEqual(1, len(server.messages))
        message = server.last_message()
        self.assertIn("the subject", message["data"])
        self.assertIn("the body", message["data"])

    def test_the_sender_and_recipient_are_set(self):
        server = FakeSMTP()
        port = server.start()
        self.addCleanup(server.stop)

        send_email(self._config(port), "subject", "body")

        message = server.last_message()
        self.assertIn("me@example.com", message["from"])
        self.assertIn("you@example.com", message["to"][0])

    def test_starttls_is_used_when_asked_for(self):
        server = FakeSMTP()
        port = server.start()
        self.addCleanup(server.stop)
        if not server.tls_available:
            self.skipTest("openssl is not available, so no certificate could be made")

        with trusted_self_signed():
            send_email(self._config(port, security="starttls"), "subject", "body")

        self.assertEqual(1, len(server.messages))

    def test_implicit_tls_is_used_when_asked_for(self):
        server = FakeSMTP(implicit_tls=True)
        port = server.start()
        self.addCleanup(server.stop)
        if not server.tls_available:
            self.skipTest("openssl is not available, so no certificate could be made")

        with trusted_self_signed():
            send_email(self._config(port, security="ssl"), "subject", "body")

        self.assertEqual(1, len(server.messages))

    def test_a_server_that_is_not_there_is_reported(self):
        config = self._config(1)
        with self.assertRaises(NotifyError):
            send_email(config, "subject", "body")


class NotifyTests(unittest.TestCase):
    def test_a_changed_run_is_sent(self):
        server = FakeSMTP()
        port = server.start()
        self.addCleanup(server.stop)

        config = SmtpConfig(
            enabled=True,
            host="127.0.0.1",
            port=port,
            security="none",
            username="me@example.com",
            password="secret",
            from_addr="me@example.com",
            to_addrs=["you@example.com"],
        )
        report = make_report(copied=[("film", "Movies/film.mkv", 10)])

        outcome = notify(report, config)

        self.assertEqual("sent", outcome)
        self.assertEqual(1, len(server.messages))

    def test_a_failed_send_is_reported_not_raised(self):
        config = SmtpConfig(
            enabled=True,
            host="127.0.0.1",
            port=1,
            security="none",
            username="me@example.com",
            password="secret",
            from_addr="me@example.com",
            to_addrs=["you@example.com"],
        )
        report = make_report(copied=[("film", "Movies/film.mkv", 10)])

        outcome = notify(report, config)

        self.assertTrue(outcome.startswith("failed"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
