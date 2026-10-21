"""Tests for the configuration loader."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import write
from favsav import config as config_module
from favsav.config import Config, ConfigError, parse_roots


class ParseRootsTests(unittest.TestCase):
    def test_single_prefix_uses_itself_as_local_path(self):
        roots = parse_roots("/srv/media")
        self.assertEqual(1, len(roots))
        self.assertEqual("/srv/media", roots[0].source)
        self.assertEqual("/srv/media", roots[0].local)
        self.assertEqual("", roots[0].subdir)

    def test_local_override(self):
        roots = parse_roots("/srv/media=/mnt/media")
        self.assertEqual("/srv/media", roots[0].source)
        self.assertEqual("/mnt/media", roots[0].local)

    def test_local_override_and_subdirectory(self):
        roots = parse_roots("/srv/media=/mnt/media=movies")
        self.assertEqual("/mnt/media", roots[0].local)
        self.assertEqual("movies", roots[0].subdir)

    def test_several_roots(self):
        roots = parse_roots("/srv/media, /srv/other=/mnt/other=other")
        self.assertEqual(2, len(roots))
        self.assertEqual("/srv/media", roots[0].source)
        self.assertEqual("/srv/other", roots[1].source)
        self.assertEqual("other", roots[1].subdir)

    def test_trailing_slashes_are_trimmed(self):
        roots = parse_roots("/srv/media/")
        self.assertEqual("/srv/media", roots[0].source)

    def test_empty_entries_are_ignored(self):
        roots = parse_roots("/srv/media,, ")
        self.assertEqual(1, len(roots))

    def test_too_many_parts_is_an_error(self):
        with self.assertRaises(ConfigError):
            parse_roots("/a=/b=/c=/d")


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tempdir, "config.ini")

    def test_defaults(self):
        write(self.path, "[jellyfin]\nurl = http://localhost:8096\n")
        config = config_module.load(self.path)
        self.assertEqual("http://localhost:8096", config.jellyfin_url)
        # The shipped default is to plan only and touch nothing.
        self.assertTrue(config.dry_run)
        self.assertTrue(config.include_sidecars)
        self.assertFalse(config.smtp.enabled)

    def test_dry_run_can_be_switched_off_in_the_file(self):
        write(self.path, "[behaviour]\ndry_run = false\n")
        config = config_module.load(self.path)
        self.assertFalse(config.dry_run)

    def test_boolean_parsing(self):
        write(self.path, "[copy]\ninclude_sidecars = no\n[dest]\nrequire_mount = off\n")
        config = config_module.load(self.path)
        self.assertFalse(config.include_sidecars)
        self.assertFalse(config.require_mount)

    def test_bad_boolean_is_reported(self):
        write(self.path, "[copy]\ninclude_sidecars = perhaps\n")
        with self.assertRaises(ConfigError):
            config_module.load(self.path)

    def test_bad_integer_is_reported(self):
        write(self.path, "[jellyfin]\ntimeout = soon\n")
        with self.assertRaises(ConfigError):
            config_module.load(self.path)

    def test_api_key_from_the_environment_wins(self):
        write(self.path, "[jellyfin]\napi_key = from-file\n")
        os.environ["FAVSAV_API_KEY"] = "from-env"
        try:
            config = config_module.load(self.path)
        finally:
            del os.environ["FAVSAV_API_KEY"]
        self.assertEqual("from-env", config.api_key)

    def test_smtp_password_from_the_environment(self):
        write(self.path, "[smtp]\npassword = from-file\n")
        os.environ["FAVSAV_SMTP_PASSWORD"] = "from-env"
        try:
            config = config_module.load(self.path)
        finally:
            del os.environ["FAVSAV_SMTP_PASSWORD"]
        self.assertEqual("from-env", config.smtp.password)

    def test_users_are_split_on_commas(self):
        write(self.path, "[jellyfin]\nusers = alice, bob\n")
        config = config_module.load(self.path)
        self.assertEqual(["alice", "bob"], config.users)

    def test_missing_file_requested_explicitly_is_an_error(self):
        with self.assertRaises(ConfigError):
            config_module.load(os.path.join(self.tempdir, "nope.ini"))

    def test_missing_default_file_is_tolerated(self):
        config = config_module.load()
        self.assertEqual("", config.api_key)


class ValidateTests(unittest.TestCase):
    def _config(self, **kwargs) -> Config:
        config = Config()
        config.jellyfin_url = "http://localhost:8096"
        config.api_key = "key"
        config.roots = parse_roots("/srv/media")
        config.dest_root = "/mnt/backup"
        for name, value in kwargs.items():
            setattr(config, name, value)
        return config

    def test_a_complete_configuration_validates(self):
        config_module.validate(self._config())

    def test_a_missing_api_key_is_an_error(self):
        with self.assertRaises(ConfigError):
            config_module.validate(self._config(api_key=""))

    def test_missing_roots_is_an_error(self):
        with self.assertRaises(ConfigError):
            config_module.validate(self._config(roots=[]))

    def test_missing_destination_is_an_error(self):
        with self.assertRaises(ConfigError):
            config_module.validate(self._config(dest_root=""))

    def test_a_relative_local_prefix_is_an_error(self):
        config = self._config()
        config.roots = parse_roots("/srv/media=relative")
        with self.assertRaises(ConfigError):
            config_module.validate(config)


class SmtpGuardTests(unittest.TestCase):
    def test_disabled_smtp_is_not_configured(self):
        config = Config()
        self.assertIsNotNone(config.smtp.unconfigured_reason())

    def test_enabled_but_incomplete_is_not_configured(self):
        config = Config()
        config.smtp.enabled = True
        self.assertIsNotNone(config.smtp.unconfigured_reason())

    def test_a_complete_smtp_configuration_is_configured(self):
        config = Config()
        config.smtp.enabled = True
        config.smtp.host = "smtp.example.com"
        config.smtp.to_addrs = ["me@example.com"]
        self.assertIsNone(config.smtp.unconfigured_reason())

    def test_a_username_without_a_password_is_not_configured(self):
        config = Config()
        config.smtp.enabled = True
        config.smtp.host = "smtp.example.com"
        config.smtp.to_addrs = ["me@example.com"]
        config.smtp.username = "me@example.com"
        self.assertIsNotNone(config.smtp.unconfigured_reason())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
