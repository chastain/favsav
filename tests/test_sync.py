"""Tests for the copy engine.

The important one here is :meth:`CopyItemTests.test_the_shape_of_the_source_does_not_matter`,
which is the layout-independence guarantee turned into an executable test.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import tree, write
from favsav.sync import (
    STATUS_COPIED,
    STATUS_FAILED,
    STATUS_UNCHANGED,
    STATUS_WOULD_COPY,
    copy_file,
    copy_item,
    plan_files,
    sidecars_for,
)


class CopyFileTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.source = os.path.join(self.tempdir, "src")
        self.dest = os.path.join(self.tempdir, "dst")
        os.makedirs(self.source)

    def test_a_file_is_copied_with_its_content(self):
        src = write(os.path.join(self.source, "a.mkv"), "hello")
        dst = os.path.join(self.dest, "a.mkv")
        result = copy_file(src, dst)
        self.assertEqual(STATUS_COPIED, result.status)
        with open(dst) as handle:
            self.assertEqual("hello", handle.read())

    def test_the_destination_directory_is_created(self):
        src = write(os.path.join(self.source, "a.mkv"))
        dst = os.path.join(self.dest, "deep", "deeper", "a.mkv")
        result = copy_file(src, dst)
        self.assertEqual(STATUS_COPIED, result.status)
        self.assertTrue(os.path.exists(dst))

    def test_no_temporary_file_is_left_behind(self):
        src = write(os.path.join(self.source, "a.mkv"))
        dst = os.path.join(self.dest, "a.mkv")
        copy_file(src, dst)
        self.assertEqual(["a.mkv"], os.listdir(self.dest))

    def test_a_dry_run_writes_nothing(self):
        src = write(os.path.join(self.source, "a.mkv"))
        dst = os.path.join(self.dest, "a.mkv")
        result = copy_file(src, dst, dry_run=True)
        self.assertEqual(STATUS_WOULD_COPY, result.status)
        self.assertFalse(os.path.exists(dst))

    def test_a_missing_source_fails_cleanly(self):
        result = copy_file(os.path.join(self.source, "nope"), os.path.join(self.dest, "x"))
        self.assertEqual(STATUS_FAILED, result.status)
        self.assertTrue(result.error)

    def test_the_modification_time_is_preserved(self):
        src = write(os.path.join(self.source, "a.mkv"))
        os.utime(src, (1000000, 1000000))
        dst = os.path.join(self.dest, "a.mkv")
        copy_file(src, dst)
        self.assertEqual(1000000, int(os.stat(dst).st_mtime))


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.source = os.path.join(self.tempdir, "src")
        os.makedirs(self.source)

    def test_a_subtitle_with_the_same_stem_is_a_sidecar(self):
        media = write(os.path.join(self.source, "film.mkv"))
        write(os.path.join(self.source, "film.srt"))
        found = sidecars_for(media)
        self.assertEqual([os.path.join(self.source, "film.srt")], found)

    def test_a_file_with_a_different_stem_is_not(self):
        media = write(os.path.join(self.source, "film.mkv"))
        write(os.path.join(self.source, "other.srt"))
        self.assertEqual([], sidecars_for(media))

    def test_a_differently_suffixed_version_is_not_a_sidecar(self):
        media = write(os.path.join(self.source, "film.mkv"))
        write(os.path.join(self.source, "film.1080p.mkv"))
        self.assertEqual([], sidecars_for(media))

    def test_several_sidecars_are_all_found(self):
        media = write(os.path.join(self.source, "film.mkv"))
        write(os.path.join(self.source, "film.srt"))
        write(os.path.join(self.source, "film.nfo"))
        write(os.path.join(self.source, "film.jpg"))
        self.assertEqual(3, len(sidecars_for(media)))


class CopyItemTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.source = os.path.join(self.tempdir, "src")
        self.dest = os.path.join(self.tempdir, "dst")
        os.makedirs(self.source)

    def test_a_single_file_is_copied(self):
        src = write(os.path.join(self.source, "film.mkv"))
        results = copy_item(src, os.path.join(self.dest, "film.mkv"))
        self.assertEqual(1, len(results))
        self.assertEqual(STATUS_COPIED, results[0].status)

    def test_a_directory_is_copied_in_full(self):
        write(os.path.join(self.source, "TV", "Show", "Season 01", "ep1.mkv"))
        write(os.path.join(self.source, "TV", "Show", "Season 01", "ep2.mkv"))
        write(os.path.join(self.source, "TV", "Show", "poster.jpg"))
        results = copy_item(
            os.path.join(self.source, "TV", "Show"), os.path.join(self.dest, "TV", "Show")
        )
        self.assertEqual(3, len(results))
        self.assertEqual(
            ["Season 01/ep1.mkv", "Season 01/ep2.mkv", "poster.jpg"],
            tree(os.path.join(self.dest, "TV", "Show")),
        )

    def test_the_shape_of_the_source_does_not_matter(self):
        """A flat series, a nested series and a mixed one must all copy the same.

        This is the guarantee that favsav does not assume a directory layout: the
        three shapes below hold the same files, only the nesting differs.
        """
        files = ["ep1.mkv", "ep2.mkv", "poster.jpg"]

        flat = os.path.join(self.source, "flat")
        for name in files:
            write(os.path.join(flat, name))

        nested = os.path.join(self.source, "nested")
        for name in files:
            write(os.path.join(nested, "Season 01", name))

        mixed = os.path.join(self.source, "mixed")
        write(os.path.join(mixed, "ep1.mkv"))
        write(os.path.join(mixed, "Season 02", "ep2.mkv"))
        write(os.path.join(mixed, "Season 02", "poster.jpg"))

        shapes = {}
        for name in ("flat", "nested", "mixed"):
            dest = os.path.join(self.dest, name)
            copy_item(os.path.join(self.source, name), dest)
            shapes[name] = sorted(os.path.basename(p) for p in tree(dest))

        self.assertEqual(sorted(files), shapes["flat"])
        self.assertEqual(shapes["flat"], shapes["nested"])
        self.assertEqual(shapes["flat"], shapes["mixed"])

    def test_a_deeply_nested_tree_is_copied(self):
        deep = os.path.join(self.source, "a", "b", "c", "d")
        write(os.path.join(deep, "ep.mkv"))
        copy_item(os.path.join(self.source, "a"), os.path.join(self.dest, "a"))
        self.assertTrue(os.path.exists(os.path.join(self.dest, "a", "b", "c", "d", "ep.mkv")))

    def test_sidecars_of_a_media_file_are_copied(self):
        media = write(os.path.join(self.source, "film.mkv"))
        write(os.path.join(self.source, "film.srt"))
        write(os.path.join(self.source, "film.nfo"))
        copy_item(media, os.path.join(self.dest, "film.mkv"))
        self.assertEqual(["film.mkv", "film.nfo", "film.srt"], sorted(os.listdir(self.dest)))

    def test_sidecars_can_be_switched_off(self):
        media = write(os.path.join(self.source, "film.mkv"))
        write(os.path.join(self.source, "film.srt"))
        copy_item(media, os.path.join(self.dest, "film.mkv"), include_sidecars=False)
        self.assertEqual(["film.mkv"], os.listdir(self.dest))

    def test_an_unchanged_file_is_not_copied_again(self):
        src = write(os.path.join(self.source, "film.mkv"))
        dest = os.path.join(self.dest, "film.mkv")
        copy_item(src, dest)
        results = copy_item(src, dest)
        self.assertEqual(STATUS_UNCHANGED, results[0].status)

    def test_a_changed_file_is_copied_again(self):
        src = write(os.path.join(self.source, "film.mkv"), "one")
        dest = os.path.join(self.dest, "film.mkv")
        copy_item(src, dest)
        write(src, "two")
        os.utime(src, (2000000, 2000000))
        results = copy_item(src, dest)
        self.assertEqual(STATUS_COPIED, results[0].status)
        with open(dest) as handle:
            self.assertEqual("two", handle.read())

    def test_empty_directories_are_recreated(self):
        os.makedirs(os.path.join(self.source, "TV", "Show", "Empty"))
        write(os.path.join(self.source, "TV", "Show", "ep.mkv"))
        copy_item(os.path.join(self.source, "TV", "Show"), os.path.join(self.dest, "TV", "Show"))
        self.assertTrue(os.path.isdir(os.path.join(self.dest, "TV", "Show", "Empty")))

    def test_a_symlink_is_not_followed_by_default(self):
        target = write(os.path.join(self.source, "real.mkv"))
        os.makedirs(os.path.join(self.source, "dir"))
        os.symlink(target, os.path.join(self.source, "dir", "link.mkv"))
        copy_item(os.path.join(self.source, "dir"), os.path.join(self.dest, "dir"))
        self.assertEqual([], tree(os.path.join(self.dest, "dir")))

    def test_a_dry_run_copies_nothing(self):
        write(os.path.join(self.source, "film.mkv"))
        results = copy_item(
            os.path.join(self.source, "film.mkv"), os.path.join(self.dest, "film.mkv"), dry_run=True
        )
        self.assertEqual(STATUS_WOULD_COPY, results[0].status)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "film.mkv")))


class PlanTests(unittest.TestCase):
    def test_a_sidecar_is_not_listed_twice(self):
        tempdir = tempfile.mkdtemp()
        source = os.path.join(tempdir, "src")
        media = write(os.path.join(source, "film.mkv"))
        write(os.path.join(source, "film.srt"))
        pairs = plan_files(media, os.path.join(tempdir, "dst", "film.mkv"))
        sources = [pair[0] for pair in pairs]
        self.assertEqual(len(sources), len(set(sources)))


class CopyModeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.source = os.path.join(self.tempdir, "src")
        self.dest = os.path.join(self.tempdir, "dst")
        os.makedirs(self.source)

    def test_a_hardlink_shares_the_bytes(self):
        src = write(os.path.join(self.source, "film.mkv"), "data")
        dst = os.path.join(self.dest, "film.mkv")
        results = copy_item(src, dst, mode="hardlink")
        self.assertEqual(STATUS_COPIED, results[0].status)
        self.assertEqual(os.stat(src).st_ino, os.stat(dst).st_ino)

    def test_a_symlink_points_at_the_source(self):
        src = write(os.path.join(self.source, "film.mkv"), "data")
        dst = os.path.join(self.dest, "film.mkv")
        results = copy_item(src, dst, mode="symlink")
        self.assertEqual(STATUS_COPIED, results[0].status)
        self.assertTrue(os.path.islink(dst))
        self.assertEqual(os.path.abspath(src), os.path.realpath(dst))

    def test_no_temporary_file_is_left_behind_by_a_link(self):
        src = write(os.path.join(self.source, "film.mkv"))
        dst = os.path.join(self.dest, "film.mkv")
        copy_item(src, dst, mode="symlink")
        self.assertEqual(["film.mkv"], os.listdir(self.dest))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
