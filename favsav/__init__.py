"""favsav - back up Jellyfin-favorite media to a secondary disk.

favsav asks a Jellyfin server which items are marked as favorite, works out where
those items live on disk, and keeps a destination directory in sync with them.

It never writes to Jellyfin, never guesses a file path, and never deletes anything it
did not copy itself.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
