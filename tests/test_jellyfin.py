"""Tests for the Jellyfin API client."""

from __future__ import annotations

import unittest

from tests.support import FakeJellyfin
from favsav.jellyfin import (
    JellyfinClient,
    JellyfinError,
    JellyfinUnavailable,
)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeJellyfin()
        self.url = self.server.start()
        self.addCleanup(self.server.stop)
        self.client = JellyfinClient(self.url, "key", retries=1, backoff=0.01)

    def test_users_are_listed(self):
        self.server.add_user("u1", "alice")
        self.server.add_user("u2", "bob")
        users = self.client.list_users()
        self.assertEqual(["alice", "bob"], [user.name for user in users])

    def test_users_can_be_selected_by_name(self):
        self.server.add_user("u1", "alice")
        self.server.add_user("u2", "bob")
        users = self.client.find_users(["bob"])
        self.assertEqual(["bob"], [user.name for user in users])

    def test_an_unknown_user_is_an_error(self):
        self.server.add_user("u1", "alice")
        with self.assertRaises(JellyfinError):
            self.client.find_users(["carol"])

    def test_no_user_filter_means_every_user(self):
        self.server.add_user("u1", "alice")
        self.server.add_user("u2", "bob")
        users = self.client.find_users([])
        self.assertEqual(2, len(users))

    def test_favorites_are_returned_for_a_user(self):
        self.server.add_user("u1", "alice")
        self.server.add_item("i1", "film", "Movie", "/media/film.mkv", favorite=True)
        self.server.add_item("i2", "other", "Movie", "/media/other.mkv")
        favorites = self.client.list_favorites("u1")
        self.assertEqual(["film"], [item.name for item in favorites])

    def test_the_favorites_filter_is_used(self):
        self.server.add_user("u1", "alice")
        self.server.add_item("i1", "film", "Movie", "/media/film.mkv", favorite=True)
        self.client.list_favorites("u1")
        self.assertTrue(
            any("filters=IsFavorite" in request for request in self.server.requests),
            "the IsFavorite filter was not used",
        )

    def test_pagination_follows_every_page(self):
        self.server.add_user("u1", "alice")
        for index in range(25):
            self.server.add_item(
                "i%d" % index, "film%d" % index, "Movie", "/media/%d.mkv" % index, favorite=True
            )
        small = JellyfinClient(self.url, "key", retries=1, backoff=0.01, page_size=10)
        favorites = small.list_favorites("u1")
        self.assertEqual(25, len(favorites))

    def test_children_are_listed(self):
        self.server.add_item("parent", "Box", "BoxSet")
        self.server.add_item("child", "film", "Movie", "/media/film.mkv")
        self.server.add_child("parent", "child")
        children = self.client.list_children("parent")
        self.assertEqual(["film"], [item.name for item in children])

    def test_children_can_be_listed_for_a_specific_user(self):
        self.server.add_user("u1", "alice")
        self.server.add_item("parent", "Season 1", "Season", None, location_type="Virtual")
        self.server.add_item("child", "ep", "Episode", "/media/ep.mkv")
        self.server.add_child("parent", "child")
        # Without a user, the virtual season exposes nothing.
        self.assertEqual([], self.client.list_children("parent"))
        # With a user, the episodes appear.
        children = self.client.list_children("parent", user_id="u1")
        self.assertEqual(["ep"], [item.name for item in children])

    def test_the_path_is_only_returned_when_it_was_asked_for(self):
        self.server.add_user("u1", "alice")
        self.server.add_item("i1", "film", "Movie", "/media/film.mkv", favorite=True)
        favorites = self.client.list_favorites("u1")
        self.assertEqual("/media/film.mkv", favorites[0].path)

    def test_data_paths_are_read_from_the_system_info(self):
        self.server.system_info = {"ProgramDataPath": "/var/lib/jellyfin", "CachePath": "/var/cache/jellyfin"}
        paths = self.client.data_paths()
        self.assertIn("/var/lib/jellyfin", paths)

    def test_an_unreachable_server_is_reported(self):
        client = JellyfinClient("http://127.0.0.1:1", "key", retries=1, backoff=0.01)
        with self.assertRaises(JellyfinUnavailable):
            client.list_users()

    def test_a_missing_route_is_reported(self):
        with self.assertRaises(JellyfinError):
            self.client.get_item("u1", "nope")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
