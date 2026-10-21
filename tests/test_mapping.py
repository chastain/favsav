"""Tests for the favorite-to-path mapping.

These are the tests that encode the two rules the design turns on:

* the location comes from Jellyfin's ``Path`` field and from nowhere else;
* a directory is copied as it is found on disk, whatever its shape.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import FakeJellyfin, write
from favsav.config import parse_roots
from favsav.jellyfin import Item
from favsav.mapping import (
    SKIP_CONTAINER_EMPTY,
    SKIP_OUTSIDE_ROOTS,
    RootMatcher,
    resolve,
)


def make_item(item_id, name, item_type, path=None, location_type="FileSystem"):
    return Item(
        id=item_id,
        name=name,
        type=item_type,
        path=path,
        location_type=location_type,
    )


class RootMatcherTests(unittest.TestCase):
    def test_exact_match(self):
        matcher = RootMatcher(parse_roots("/srv/media"))
        match = matcher.match("/srv/media")
        self.assertIsNotNone(match)
        self.assertEqual("", match.relative)

    def test_a_file_inside_the_root(self):
        matcher = RootMatcher(parse_roots("/srv/media"))
        match = matcher.match("/srv/media/Movies/film.mkv")
        self.assertEqual("Movies/film.mkv", match.relative)

    def test_a_path_outside_every_root_does_not_match(self):
        matcher = RootMatcher(parse_roots("/srv/media"))
        self.assertIsNone(matcher.match("/srv/other/film.mkv"))

    def test_a_sibling_with_a_shared_prefix_does_not_match(self):
        matcher = RootMatcher(parse_roots("/srv/media"))
        self.assertIsNone(matcher.match("/srv/media-backup/film.mkv"))

    def test_the_longest_prefix_wins(self):
        matcher = RootMatcher(
            parse_roots("/srv/media=/mnt/a, /srv/media/tv=/mnt/b=tv")
        )
        match = matcher.match("/srv/media/tv/Show/ep.mkv")
        self.assertEqual("/mnt/b", match.root.local)
        # Relative to the root that matched, not to the first one.
        self.assertEqual("Show/ep.mkv", match.relative)

    def test_a_local_override_does_not_change_the_matching(self):
        matcher = RootMatcher(parse_roots("/srv/media=/mnt/media"))
        match = matcher.match("/srv/media/Movies/film.mkv")
        self.assertEqual("/mnt/media", match.root.local)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.source = os.path.join(self.tempdir, "source")
        self.dest = os.path.join(self.tempdir, "dest")
        os.makedirs(self.source)
        os.makedirs(self.dest)
        self.matcher = RootMatcher(parse_roots(self.source))
        self.server = FakeJellyfin()
        self.url = self.server.start()
        self.addCleanup(self.server.stop)

    def _resolve(self, items, **kwargs):
        from favsav.jellyfin import JellyfinClient

        client = JellyfinClient(self.url, "key")
        params = {
            "data_paths": ["/var/lib/jellyfin"],
            "include_strm": False,
            "max_depth": 5,
        }
        params.update(kwargs)
        return resolve(items, self.matcher, self.dest, client, **params)

    def test_a_file_favorite_maps_to_a_single_file(self):
        path = write(os.path.join(self.source, "Movies", "film.mkv"))
        item = make_item("1", "film", "Movie", path)
        results = self._resolve([item])
        self.assertEqual(1, len(results))
        self.assertTrue(results[0].copyable)
        self.assertEqual("file", results[0].kind)
        self.assertEqual(path, results[0].source)
        self.assertEqual(os.path.join(self.dest, "Movies", "film.mkv"), results[0].dest)

    def test_a_directory_favorite_maps_to_the_whole_tree(self):
        path = os.path.join(self.source, "TV", "Show")
        write(os.path.join(path, "ep1.mkv"))
        item = make_item("1", "Show", "Series", path)
        results = self._resolve([item])
        self.assertEqual(1, len(results))
        self.assertEqual("dir", results[0].kind)
        self.assertEqual(os.path.join(self.dest, "TV", "Show"), results[0].dest)

    def test_an_item_outside_the_roots_is_reported_not_dropped(self):
        item = make_item("1", "elsewhere", "Movie", "/somewhere/else/film.mkv")
        results = self._resolve([item])
        self.assertEqual(1, len(results))
        self.assertFalse(results[0].copyable)
        self.assertEqual(SKIP_OUTSIDE_ROOTS, results[0].reason)

    def test_a_missing_source_file_is_reported(self):
        item = make_item("1", "gone", "Movie", os.path.join(self.source, "gone.mkv"))
        results = self._resolve([item])
        self.assertFalse(results[0].copyable)
        self.assertIn("not found", results[0].reason)

    def test_a_strm_item_is_skipped_by_default(self):
        path = write(os.path.join(self.source, "movie.strm"))
        item = make_item("1", "movie", "Movie", path)
        results = self._resolve([item])
        self.assertFalse(results[0].copyable)
        self.assertIn("strm", results[0].reason)

    def test_a_strm_item_is_kept_when_asked_for(self):
        path = write(os.path.join(self.source, "movie.strm"))
        item = make_item("1", "movie", "Movie", path)
        results = self._resolve([item], include_strm=True)
        self.assertTrue(results[0].copyable)

    def test_a_box_set_is_expanded_into_its_children(self):
        self.server.add_item("box", "Box", "BoxSet", "/var/lib/jellyfin/data/collections/Box")
        write(os.path.join(self.source, "Movies", "a.mkv"))
        write(os.path.join(self.source, "Movies", "b.mkv"))
        self.server.add_item("a", "a", "Movie", os.path.join(self.source, "Movies", "a.mkv"))
        self.server.add_item("b", "b", "Movie", os.path.join(self.source, "Movies", "b.mkv"))
        self.server.add_child("box", "a")
        self.server.add_child("box", "b")

        item = make_item("box", "Box", "BoxSet", "/var/lib/jellyfin/data/collections/Box")
        results = self._resolve([item])
        self.assertEqual(2, len(results))
        self.assertTrue(all(entry.copyable for entry in results))
        self.assertEqual(
            sorted(
                [os.path.join(self.dest, "Movies", "a.mkv"), os.path.join(self.dest, "Movies", "b.mkv")]
            ),
            sorted(entry.dest for entry in results),
        )

    def test_an_empty_box_set_is_reported(self):
        self.server.add_item("box", "Box", "BoxSet", "/var/lib/jellyfin/data/collections/Box")
        item = make_item("box", "Box", "BoxSet", "/var/lib/jellyfin/data/collections/Box")
        results = self._resolve([item])
        self.assertEqual(1, len(results))
        self.assertFalse(results[0].copyable)
        self.assertEqual(SKIP_CONTAINER_EMPTY, results[0].reason)

    def test_a_pathless_item_is_expanded(self):
        # This is what a virtual season looks like: it exists in the library but has
        # no directory behind it.
        write(os.path.join(self.source, "TV", "Show", "Version 1.0", "ep.mkv"))
        self.server.add_item("ep", "ep", "Episode", os.path.join(self.source, "TV", "Show", "Version 1.0", "ep.mkv"))
        self.server.add_child("season", "ep")
        item = make_item("season", "Season 1", "Season", None, location_type="Virtual")
        results = self._resolve([item])
        self.assertEqual(1, len(results))
        self.assertTrue(results[0].copyable)
        self.assertEqual(
            os.path.join(self.dest, "TV", "Show", "Version 1.0", "ep.mkv"), results[0].dest
        )

    def test_expansion_stops_at_the_depth_limit(self):
        self.server.add_item("child", "child", "Movie", None)
        self.server.add_child("box", "child")
        item = make_item("box", "Box", "BoxSet", "/var/lib/jellyfin/data/collections/Box")
        results = self._resolve([item], max_depth=0)
        self.assertEqual(1, len(results))
        self.assertFalse(results[0].copyable)
        self.assertIn("depth", results[0].reason)

    def test_the_same_file_reached_twice_is_only_copied_once(self):
        path = write(os.path.join(self.source, "Movies", "film.mkv"))
        first = make_item("1", "film", "Movie", path)
        second = make_item("2", "the same film again", "Movie", path)
        results = self._resolve([first, second])
        self.assertEqual(2, len(results))
        self.assertTrue(results[0].copyable)
        self.assertFalse(results[1].copyable)
        self.assertIn("already covered", results[1].reason)

    def test_a_subdirectory_moves_the_mirror(self):
        source = os.path.join(self.tempdir, "source")
        dest = os.path.join(self.tempdir, "dest")
        matcher = RootMatcher(parse_roots("%s=%s=movies" % (source, source)))
        from favsav.jellyfin import JellyfinClient

        client = JellyfinClient(self.url, "key")
        path = write(os.path.join(source, "Movies", "film.mkv"))
        item = make_item("1", "film", "Movie", path)
        results = resolve([item], matcher, dest, client)
        self.assertEqual(os.path.join(dest, "movies", "Movies", "film.mkv"), results[0].dest)

    def test_the_destination_cannot_escape_the_destination_root(self):
        # A relative root could otherwise produce a path outside the destination.
        matcher = RootMatcher(parse_roots(self.source))
        from favsav.jellyfin import JellyfinClient

        client = JellyfinClient(self.url, "key")
        item = make_item("1", "film", "Movie", os.path.join(self.source, "film.mkv"))
        write(os.path.join(self.source, "film.mkv"))
        results = resolve([item], matcher, self.dest, client)
        self.assertTrue(results[0].dest.startswith(self.dest))


class ExtrasTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.source = os.path.join(self.tempdir, "source")
        self.dest = os.path.join(self.tempdir, "dest")
        os.makedirs(self.source)
        os.makedirs(self.dest)
        self.matcher = RootMatcher(parse_roots(self.source))
        self.server = FakeJellyfin()
        self.url = self.server.start()
        self.addCleanup(self.server.stop)

    def _resolve(self, items, **kwargs):
        from favsav.jellyfin import JellyfinClient

        client = JellyfinClient(self.url, "key")
        params = {
            "data_paths": ["/var/lib/jellyfin"],
            "include_strm": False,
            "include_extras": True,
            "max_depth": 5,
        }
        params.update(kwargs)
        return resolve(items, self.matcher, self.dest, client, **params)

    def test_an_extra_is_mirrored_alongside_its_media(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        trailer = write(os.path.join(self.source, "Movies", "film-trailer.mp4"))
        self.server.add_item("trailer", "Trailer", "Trailer", trailer)
        item = make_item("1", "film", "Movie", media)
        item.raw = {"ExtraIds": ["trailer"]}

        results = self._resolve([item])

        self.assertEqual(2, len(results))
        self.assertTrue(results[1].is_extra)
        self.assertEqual(trailer, results[1].source)
        self.assertEqual(os.path.join(self.dest, "Movies", "film-trailer.mp4"), results[1].dest)

    def test_extras_are_left_out_when_they_are_not_wanted(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        trailer = write(os.path.join(self.source, "Movies", "film-trailer.mp4"))
        self.server.add_item("trailer", "Trailer", "Trailer", trailer)
        item = make_item("1", "film", "Movie", media)
        item.raw = {"ExtraIds": ["trailer"]}

        results = self._resolve([item], include_extras=False)

        self.assertEqual(1, len(results))

    def test_an_extra_outside_the_roots_is_ignored(self):
        media = write(os.path.join(self.source, "Movies", "film.mkv"))
        trailer = write(os.path.join(self.tempdir, "elsewhere", "trailer.mp4"))
        self.server.add_item("trailer", "Trailer", "Trailer", trailer)
        item = make_item("1", "film", "Movie", media)
        item.raw = {"ExtraIds": ["trailer"]}

        results = self._resolve([item])

        self.assertEqual(1, len(results))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
