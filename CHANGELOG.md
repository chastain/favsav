# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-21

First release.

### Added

- Reads the favorites of every Jellyfin user through the API and backs up the union.
- Mirrors the source tree under the destination root, preserving the layout.
- Copies a favorited file on its own, and a favorited directory in full -- whatever
  shape that directory happens to have.
- Copies same-stem sidecars (subtitles, `.nfo`, artwork) found next to a media file.
- Expands box sets and playlists into their members.
- A manifest of everything favsav copied, so that pruning can only ever remove
  favsav's own files.
- `--prune`, behind an explicit flag, to remove copies of items that are no longer
  favorites.
- Dry run by default; `--no-dry-run` to actually copy.
- `--list`, `--verify`, `--check-destination`, `--no-notify`, `--strict-notify` and
  `--init-config`.
- Optional SMTP notification after a run that changed something, listing every file
  copied and removed, and the free space on the destination disk. Unconfigured SMTP
  is not an error and never affects the exit code.
- systemd service and timer.
