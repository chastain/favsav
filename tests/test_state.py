"""Tests for the manifest and for pruning."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.support import write
from favsav.state import Manifest, prune, relative_to


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tempdir, "state.json")

    def test_a_missing_manifest_loads_as_empty(self):
        manifest = Manifest.load(self.path)
        self.assertEqual({}, manifest.entries)

    def test_entries_survive_a_save_and_load(self):
        manifest = Manifest()
        manifest.record("Movies/film.mkv", "/src/Movies/film.mkv", size=10, item="film")
        manifest.save(self.path)
        loaded = Manifest.load(self.path)
        self.assertIn("Movies/film.mkv", loaded.entries)
        self.assertEqual(10, loaded.entries["Movies/film.mkv"].size)
        self.assertEqual("film", loaded.entries["Movies/film.mkv"].item)

    def test_saving_is_atomic(self):
        manifest = Manifest()
        manifest.record("a", "/src/a")
        manifest.save(self.path)
        leftovers = [name for name in os.listdir(self.tempdir) if name != "state.json"]
        self.assertEqual([], leftovers)

    def test_a_corrupt_manifest_is_ignored_rather_than_crashing(self):
        write(self.path, "{not json")
        manifest = Manifest.load(self.path)
        self.assertEqual({}, manifest.entries)

    def test_a_manifest_without_entries_is_ignored(self):
        write(self.path, json.dumps({"version": 1}))
        manifest = Manifest.load(self.path)
        self.assertEqual({}, manifest.entries)


class RelativeToTests(unittest.TestCase):
    def test_a_path_inside_the_root_is_made_relative(self):
        self.assertEqual("a/b.mkv", relative_to("/dest", "/dest/a/b.mkv"))

    def test_a_path_outside_the_root_is_left_absolute(self):
        self.assertEqual("/other/b.mkv", relative_to("/dest", "/other/b.mkv"))

    def test_the_root_itself_is_empty(self):
        self.assertEqual("", relative_to("/dest", "/dest"))


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.dest = os.path.join(self.tempdir, "dest")
        os.makedirs(self.dest)
        self.manifest = Manifest()

    def test_a_file_favsav_copied_is_removed(self):
        target = write(os.path.join(self.dest, "old.mkv"))
        self.manifest.record("old.mkv", "/src/old.mkv")
        removed = prune(self.manifest, self.dest, [])
        self.assertEqual(1, len(removed))
        self.assertFalse(os.path.exists(target))
        self.assertEqual({}, self.manifest.entries)

    def test_a_file_that_is_still_wanted_is_kept(self):
        target = write(os.path.join(self.dest, "keep.mkv"))
        self.manifest.record("keep.mkv", "/src/keep.mkv")
        removed = prune(self.manifest, self.dest, ["keep.mkv"])
        self.assertEqual([], removed)
        self.assertTrue(os.path.exists(target))

    def test_a_file_favsav_never_copied_is_left_alone(self):
        target = write(os.path.join(self.dest, "someone-elses.mkv"))
        removed = prune(self.manifest, self.dest, [])
        self.assertEqual([], removed)
        self.assertTrue(os.path.exists(target))

    def test_a_dry_run_removes_nothing(self):
        target = write(os.path.join(self.dest, "old.mkv"))
        self.manifest.record("old.mkv", "/src/old.mkv")
        removed = prune(self.manifest, self.dest, [], dry_run=True)
        self.assertEqual(1, len(removed))
        self.assertTrue(os.path.exists(target))

    def test_a_path_outside_the_destination_is_refused(self):
        outside = write(os.path.join(self.tempdir, "outside.mkv"))
        self.manifest.record("../outside.mkv", "/src/outside.mkv")
        removed = prune(self.manifest, self.dest, [])
        self.assertEqual([], removed)
        self.assertTrue(os.path.exists(outside))

    def test_a_directory_is_never_removed(self):
        os.makedirs(os.path.join(self.dest, "folder"))
        self.manifest.record("folder", "/src/folder")
        removed = prune(self.manifest, self.dest, [])
        self.assertEqual([], removed)
        self.assertTrue(os.path.isdir(os.path.join(self.dest, "folder")))

    def test_only_manifest_entries_are_considered(self):
        write(os.path.join(self.dest, "unmanaged.mkv"))
        self.manifest.record("managed.mkv", "/src/managed.mkv")
        removed = prune(self.manifest, self.dest, [])
        self.assertEqual(["managed.mkv"], [rel for rel, _entry in removed])
        self.assertTrue(os.path.exists(os.path.join(self.dest, "unmanaged.mkv")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
