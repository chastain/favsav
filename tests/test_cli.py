"""End-to-end tests of the command line interface.

These run the real entry point against a fake Jellyfin server, a fake media tree and
a temporary destination, so they cover the whole path from "the user marked something
as favorite" to "the file is on the backup disk".
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import FakeJellyfin, tree, write
from favsav import cli, config as config_module
from favsav.state import Manifest


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.source = os.path.join(self.tempdir, "media")
        self.dest = os.path.join(self.tempdir, "backup")
        self.state = os.path.join(self.tempdir, "state")
        os.makedirs(self.source)
        os.makedirs(self.dest)

        self.server = FakeJellyfin()
        self.url = self.server.start()
        self.addCleanup(self.server.stop)
        self.server.add_user("u1", "alice")

        self.config_path = os.path.join(self.tempdir, "favsav.ini")
        self._write_config()

    def _write_config(self, extra: str = "") -> None:
        write(
            self.config_path,
            "[jellyfin]\n"
            "url = %s\n"
            "api_key = test-key\n"
            "\n"
            "[paths]\n"
            "roots = %s\n"
            "dest_root = %s\n"
            "\n"
            "[dest]\n"
            "require_mount = false\n"
            "\n"
            "[behaviour]\n"
            "dry_run = false\n"
            "\n"
            "[state]\n"
            "dir = %s\n"
            "\n"
            "[smtp]\n"
            "enabled = false\n"
            "%s" % (self.url, self.source, self.dest, self.state, extra),
        )

    def _run(self, *args) -> int:
        return cli.main(["--config", self.config_path] + list(args))

    # ------------------------------------------------------------------ tests

    def test_a_favorite_movie_is_copied(self):
        media = write(os.path.join(self.source, "Movies", "Chopping Mall", "film.mkv"), "data")
        self.server.add_item("i1", "Chopping Mall", "Movie", media, favorite=True)

        code = self._run()

        self.assertEqual(cli.EXIT_OK, code)
        self.assertTrue(os.path.exists(os.path.join(self.dest, "Movies", "Chopping Mall", "film.mkv")))

    def test_a_favorite_series_is_copied_in_full(self):
        show = os.path.join(self.source, "TV", "The IT Crowd")
        write(os.path.join(show, "Version 1.0", "S01E01.mkv"))
        write(os.path.join(show, "Version 2.0", "S02E01.mkv"))
        self.server.add_item("i1", "The IT Crowd", "Series", show, favorite=True)

        code = self._run()

        self.assertEqual(cli.EXIT_OK, code)
        self.assertEqual(
            ["Version 1.0/S01E01.mkv", "Version 2.0/S02E01.mkv"],
            tree(os.path.join(self.dest, "TV", "The IT Crowd")),
        )

    def test_a_flat_series_is_copied_in_full(self):
        show = os.path.join(self.source, "TV", "Cosmos")
        write(os.path.join(show, "ep1.mkv"))
        write(os.path.join(show, "ep2.mkv"))
        self.server.add_item("i1", "Cosmos", "Series", show, favorite=True)

        self._run()

        self.assertEqual(
            ["ep1.mkv", "ep2.mkv"], tree(os.path.join(self.dest, "TV", "Cosmos"))
        )

    def test_a_sidecar_is_copied_alongside_the_media(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        write(os.path.join(self.source, "Movies", "film.srt"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)

        self._run()

        self.assertEqual(
            ["film.mkv", "film.srt"], sorted(os.listdir(os.path.join(self.dest, "Movies")))
        )

    def test_a_dry_run_copies_nothing(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)
        self._write_config(extra="")
        with open(self.config_path) as handle:
            content = handle.read()
        write(self.config_path, content.replace("dry_run = false", "dry_run = true"))

        code = self._run()

        self.assertEqual(cli.EXIT_OK, code)
        self.assertEqual([], [p for p in tree(self.dest) if not p.startswith(".favsav")])

    def test_the_manifest_records_what_was_copied(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)

        self._run()

        manifest = Manifest.load(os.path.join(self.state, "state.json"))
        self.assertIn("Movies/film.mkv", manifest.entries)

    def test_unfavoriting_and_pruning_removes_the_copy(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)
        self._run()
        self.assertTrue(os.path.exists(os.path.join(self.dest, "Movies", "film.mkv")))

        # The user takes the favorite off.
        self.server.favourites = {}
        code = self._run("--prune")

        self.assertEqual(cli.EXIT_OK, code)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "Movies", "film.mkv")))

    def test_pruning_leaves_files_favsav_did_not_copy(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)
        self._run()

        stranger = write(os.path.join(self.dest, "not-ours.mkv"))
        self.server.favourites = {}
        self._run("--prune")

        self.assertTrue(os.path.exists(stranger))

    def test_without_prune_nothing_is_removed(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)
        self._run()

        self.server.favourites = {}
        self._run()

        self.assertTrue(os.path.exists(os.path.join(self.dest, "Movies", "film.mkv")))

    def test_a_dry_run_prune_removes_nothing(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)
        self._run()

        self.server.favourites = {}
        with open(self.config_path) as handle:
            content = handle.read()
        write(self.config_path, content.replace("dry_run = false", "dry_run = true"))

        code = self._run("--prune")

        self.assertEqual(cli.EXIT_OK, code)
        self.assertTrue(os.path.exists(os.path.join(self.dest, "Movies", "film.mkv")))
        manifest = Manifest.load(os.path.join(self.state, "state.json"))
        self.assertIn("Movies/film.mkv", manifest.entries)

    def test_a_dry_run_does_not_prune_a_file_it_would_copy(self):
        # The file is still a favorite, so it must not be listed for removal.
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)
        with open(self.config_path) as handle:
            content = handle.read()
        write(self.config_path, content.replace("dry_run = false", "dry_run = true"))

        code = self._run("--prune")

        self.assertEqual(cli.EXIT_OK, code)
        with open(os.path.join(self.state, "last-report.txt")) as handle:
            report = handle.read()
        self.assertNotIn("Removed files", report)

    def test_verify_works_without_a_reachable_server(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)
        self._run()

        # Point the config at a server that is not there.
        with open(self.config_path) as handle:
            content = handle.read()
        write(self.config_path, content.replace(self.url, "http://127.0.0.1:1"))

        code = self._run("--verify")

        self.assertEqual(cli.EXIT_OK, code)

    def test_an_env_file_next_to_the_config_is_loaded(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)
        # Put the API key in an env file instead of the config.
        with open(self.config_path) as handle:
            content = handle.read()
        write(self.config_path, content.replace("api_key = test-key", "api_key ="))
        write(
            os.path.join(self.tempdir, "favsav.env"),
            "FAVSAV_API_KEY=test-key\n",
        )

        code = self._run("--list")

        self.assertEqual(cli.EXIT_OK, code)

    def test_a_second_run_copies_nothing_new(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)
        self._run()

        code = self._run()

        self.assertEqual(cli.EXIT_OK, code)
        manifest = Manifest.load(os.path.join(self.state, "state.json"))
        self.assertIn("Movies/film.mkv", manifest.entries)

    def test_list_changes_nothing(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)

        code = self._run("--list")

        self.assertEqual(cli.EXIT_OK, code)
        self.assertEqual([], tree(self.dest))

    def test_a_missing_destination_is_refused(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)
        os.rmdir(self.dest)

        code = self._run()

        self.assertEqual(cli.EXIT_FAILURE, code)

    def test_check_destination_reports_a_missing_directory(self):
        os.rmdir(self.dest)
        code = self._run("--check-destination")
        self.assertEqual(cli.EXIT_FAILURE, code)

    def test_check_destination_passes_for_a_usable_destination(self):
        code = self._run("--check-destination")
        self.assertEqual(cli.EXIT_OK, code)

    def test_the_run_report_is_written_to_disk(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)
        self._run()

        report_path = os.path.join(self.state, "last-report.txt")
        self.assertTrue(os.path.exists(report_path))
        with open(report_path) as handle:
            body = handle.read()
        self.assertIn("Movies/film.mkv", body)

    def test_a_missing_configuration_is_a_usage_error(self):
        code = cli.main(["--config", os.path.join(self.tempdir, "nope.ini")])
        self.assertEqual(cli.EXIT_USAGE, code)

    def test_no_notification_is_needed_for_the_run_to_succeed(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        self.server.add_item("i1", "film", "Movie", media, favorite=True)
        code = self._run("--no-notify")
        self.assertEqual(cli.EXIT_OK, code)

    def test_an_item_outside_the_roots_is_reported_not_copied(self):
        stray = write(os.path.join(self.tempdir, "elsewhere", "film.mkv"))
        self.server.add_item("i1", "stray", "Movie", stray, favorite=True)

        code = self._run()

        self.assertEqual(cli.EXIT_OK, code)
        self.assertEqual([], [p for p in tree(self.dest) if not p.startswith(".favsav")])

    def test_init_config_writes_a_usable_file(self):
        target = os.path.join(self.tempdir, "written.ini")

        code = cli.main(["--init-config", target])

        self.assertEqual(cli.EXIT_OK, code)
        self.assertTrue(os.path.exists(target))
        config = config_module.load(target)
        self.assertTrue(config.dry_run)

    def test_init_config_refuses_to_overwrite_without_force(self):
        target = write(os.path.join(self.tempdir, "written.ini"), "keep me")

        code = cli.main(["--init-config", target])

        self.assertEqual(cli.EXIT_USAGE, code)
        with open(target) as handle:
            self.assertEqual("keep me", handle.read())

    def test_init_config_overwrites_with_force(self):
        target = write(os.path.join(self.tempdir, "written.ini"), "keep me")

        code = cli.main(["--init-config", target, "--force"])

        self.assertEqual(cli.EXIT_OK, code)
        config = config_module.load(target)
        self.assertTrue(config.dry_run)

    def test_the_example_configuration_paths_match_the_documentation(self):
        # The generated example must be the file that ships, not a second copy that
        # can drift away from it.
        content = config_module.example_config()
        example = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__))),
            "packaging",
            "config.ini.example",
        )
        if os.path.exists(example):
            with open(example) as handle:
                self.assertEqual(handle.read(), content)
        self.assertIn("[paths]", content)
        self.assertIn("dry_run = true", content)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
