# favsav

Back up the media you marked as **favorite** in [Jellyfin](https://jellyfin.org) to a
second disk.

Mark something as a favorite in Jellyfin and favsav copies it to your backup disk.
Remove the favorite and favsav deletes the copy again. Everything else on your media
disk is left alone, and favsav never writes to Jellyfin.

- **No dependencies.** Python 3.8 or newer and the standard library. Nothing to
  `pip install`.
- **Dry run by default.** The first time you run it, it only tells you what it would
  do.
- **Nothing is deleted unless favsav put it there.** Every copy is recorded in a
  manifest, and only files in that manifest can be pruned.
- **Email is optional.** If you do not configure SMTP, favsav never opens a mail
  connection and never complains about it.

---

## Table of contents

- [How it decides what to back up](#how-it-decides-what-to-back-up)
- [Requirements](#requirements)
- [Install](#install)
- [Configure](#configure)
- [Run it](#run-it)
- [Run it on a schedule](#run-it-on-a-schedule)
- [Email notifications](#email-notifications)
- [Safety](#safety)
- [How it works](#how-it-works)
- [Command reference](#command-reference)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

---

## How it decides what to back up

**1. The favorite list comes from the Jellyfin API.**

favsav asks the server which items are marked as favorite, for every user, and takes
the union. It never reads Jellyfin's database: the schema of that database changes
between releases, while the API has been stable.

**2. The file location comes from Jellyfin's `Path` field, and from nowhere else.**

favsav never guesses a path from a title, a season number or a library hierarchy. It
takes the absolute path the server reports and mirrors the tree that is actually
there.

This matters more than it sounds. On a real library, a series called *The IT Crowd*
has seasons named `Season 1` to `Season 4` in Jellyfin's model -- with **no path at
all**, because no folder on disk is called `Season 1`. The disk actually holds
`Version 1.0/`, `Version 2.0/` and so on, and Jellyfin worked out the seasons from
the episode *file names*. A tool that built the path from the library hierarchy would
have looked for `The IT Crowd/Season 1/S01E01.mkv` and found nothing.

**3. A directory is copied exactly as it is found.**

There is no expectation about the shape of a folder. On one library, 13 of 44 series
had no season subdirectory at all (`Cosmos/episode.mkv`), two more mixed depths inside
the same show, and the season folders that did exist were called `Season 01`,
`season-1`, `Version 1.0` and `Letterkenny - Season 3` -- four spellings of the same
thing. favsav walks the tree and copies every file it finds, at whatever depth,
whatever it is called.

**4. A file is copied on its own, not with its folder.**

Favouriting a movie copies that movie's file. It does not copy the directory the
movie happens to sit in -- for a flat library that would pull in the whole shelf.

**5. Sidecars come along.**

Subtitles, `.nfo` metadata and artwork that share a file name with a media file are
copied too. Jellyfin does not list these as library items, so they can only be found
by looking at the directory the media file is in.

**6. A box set or playlist is expanded into its members.**

Jellyfin's collections live inside Jellyfin's own data directory, not on your media
disk. favsav expands them into the movies they contain and copies those.

---

## Requirements

- Python **3.8 or newer** (tested on 3.8 through 3.13). Standard library only.
- A Jellyfin server, **10.8 or newer**. It is normally the machine the media lives on.
- Read access to the media library, and write access to the backup disk.
- Optional: an SMTP account, if you want an email after each run.

---

## Install

```sh
git clone https://github.com/echastain/favsav.git
cd favsav
sudo ./packaging/install.sh
```

This installs the tool in `/usr/local/bin/favsav`, creates a `favsav` system user,
writes an example configuration to `/etc/favsav/config.ini` and installs the systemd
units. It does not start anything and does not copy any files.

You can also run it straight from a checkout:

```sh
python3 -m favsav --help
```

---

## Configure

Edit `/etc/favsav/config.ini`. The only settings you really need are the Jellyfin
address, the API key, and the two paths:

```ini
[jellyfin]
url = http://127.0.0.1:8096
api_key = your-api-key

[paths]
roots = /srv/jellyfin-media
dest_root = /mnt/backup
```

Get the API key from **Dashboard -> Advanced -> API Keys**. It is a read-only key in
practice, but it is still a secret, so prefer keeping it out of a world-readable
file:

```sh
echo 'FAVSAV_API_KEY=your-api-key' > /etc/favsav/favsav.env
chown root:favsav /etc/favsav/favsav.env
chmod 640 /etc/favsav/favsav.env
```

### The two paths

`roots` maps Jellyfin's view of the disk onto yours. It is a comma-separated list; the
longest matching prefix wins.

| Form | Meaning |
| --- | --- |
| `/srv/media` | Jellyfin's path is also the local path |
| `/srv/media=/mnt/media` | Jellyfin says `/srv/media/...`, the file is really at `/mnt/media/...` |
| `/srv/media=/mnt/media=movies` | ...and the copy goes into `<dest_root>/movies/` |

`dest_root` is where the mirrored tree is written. The layout under it mirrors the
layout under the root, so `/srv/media/Movies/film.mkv` becomes
`<dest_root>/Movies/film.mkv`.

### Every option

| Option | Default | Meaning |
| --- | --- | --- |
| `[jellyfin] url` | `http://127.0.0.1:8096` | Base URL of the server |
| `[jellyfin] api_key` | -- | API key (or `FAVSAV_API_KEY`) |
| `[jellyfin] timeout` | `30` | Seconds to wait for the server |
| `[jellyfin] users` | *(empty)* | Which users' favorites to use; empty means all of them |
| `[paths] roots` | -- | The mirror roots, as described above |
| `[paths] dest_root` | -- | Where the copy is written |
| `[dest] require_mount` | `true` | Refuse to run unless `dest_root` is a mounted filesystem |
| `[dest] device` | *(empty)* | Also require the destination to be on this block device |
| `[copy] mode` | `copy` | `copy`, `hardlink` or `symlink` |
| `[copy] include_sidecars` | `true` | Copy same-stem subtitles, `.nfo` and artwork |
| `[copy] include_extras` | `true` | Copy the extras (trailers and friends) Jellyfin reports for an item |
| `[copy] include_strm` | `false` | Copy `.strm` items (they point at a remote URL) |
| `[copy] follow_symlinks` | `false` | Follow symlinks inside the source tree |
| `[copy] skip_unchanged` | `true` | Skip files whose size and mtime already match |
| `[smtp] enabled` | `false` | Send an email after each run |
| `[smtp] notify_on_noop` | `false` | Also email when nothing changed |
| `[behaviour] dry_run` | `true` | Plan only; write nothing |
| `[behaviour] item_types` | *(empty)* | Restrict to these item types |
| `[behaviour] max_expansion_depth` | `5` | How deep to expand box sets before giving up |
| `[state] dir` | `/var/lib/favsav` | Where the manifest and the last report live |

---

## Run it

```sh
favsav --list                 # what is favorited, and where it would go
favsav --check-destination    # is the backup disk usable right now?
favsav                        # a dry run: shows what would change
favsav --no-dry-run           # actually copy
favsav --no-dry-run --prune   # copy, and remove what is no longer favorited
```

`dry_run = true` is the shipped default, so plain `favsav` never writes anything.
`--no-dry-run` is what you want once the dry run looks right.

To have un-favoriting remove the copy automatically, add `--prune` to the systemd
unit's `ExecStart` (see below). Pruning only ever touches files favsav copied itself.

---

## Run it on a schedule

```sh
sudo systemctl enable --now favsav.timer
systemctl list-timers favsav.timer
```

The timer runs the service daily at 04:30 with `--prune`, so copies of items that are
no longer favorites are removed automatically. If you would rather prune by hand, edit
the service's `ExecStart` and drop `--prune`.

The unit runs as the unprivileged `favsav` user, reads the media library and writes
only to the destination and to `/var/lib/favsav`.

**Important:** `ProtectSystem=strict` makes the whole filesystem read-only for the
service, so `ReadWritePaths` in the unit must list your actual `dest_root` and
`/var/lib/favsav`. If your destination is on the system disk (not a separate mount),
also remove the `RequiresMountsFor` line. Edit the unit with
`sudo systemctl edit --full favsav.service` and then `sudo systemctl daemon-reload`.

---

## Email notifications

Email is entirely optional. If `[smtp] enabled` is false, or the settings are
incomplete, favsav does not open a socket, does not raise an error, and does not
change its exit code. It works perfectly well without it.

When it *is* configured, an email is sent after a run that changed something --
copied a file, removed a file, or hit an error. A run that changed nothing sends
nothing, unless `[smtp] notify_on_noop` is true.

```ini
[smtp]
enabled = true
host = smtp.example.com
port = 587
security = auto          ; auto | starttls | ssl | none
username = you@example.com
from = you@example.com
from_name = favsav
to = you@example.com
```

Put the password in `/etc/favsav/favsav.env` as `FAVSAV_SMTP_PASSWORD`.

The message lists **every** file that was copied and **every** file that was removed,
with their full paths, the totals, and how much space is left on the backup disk. The
same report is always written to `/var/lib/favsav/last-report.txt`, whether or not
mail is configured.

Port 465 means implicit TLS (`security = ssl`); port 587 means STARTTLS.

---

## Safety

The short version: favsav is built so that running it wrong is boring.

- **Dry run by default.** Nothing is written or deleted until you ask for it.
- **Only favourites are touched.** The source library is opened read-only, and
  Jellyfin is never written to at all.
- **Only favsav's own files are deleted.** Every copy is written to a manifest.
  Pruning removes only files in that manifest -- anything else in the destination is
  left alone, even if it looks like a leftover.
- **The destination must be a real disk.** `require_mount` refuses to run unless
  `dest_root` is a mountpoint, so an unmounted backup disk cannot be mistaken for an
  empty folder and filled up.
- **Copies are atomic.** Each file is written to a temporary name and renamed into
  place, so an interrupted run never leaves a half-written file where a complete one
  belongs.
- **Symlinks are not followed** by default, so a link pointing outside the library
  cannot pull unrelated data into the backup.
- **One run at a time.** A lock file stops two runs from overlapping.
- **Nothing escapes the destination.** Every destination path is checked to be inside
  `dest_root` before anything is written.
- **A copy of the manifest lives beside the data** (`<dest_root>/.favsav/state.json`),
  so a restored backup still knows what it owns.

---

## How it works

```
favsav
  |
  +-- config.py      read the INI file
  +-- jellyfin.py    ask the API which items are favorites
  +-- mapping.py     work out where each one lives  ->  source path, destination path
  +-- sync.py        copy the files
  +-- state.py       record what was copied; prune what is no longer wanted
  +-- notify.py      build the report; send it if mail is configured
  +-- cli.py         put it together
```

The pieces are deliberately small, and each one is on its own:

- `mapping.py` turns favorites into paths. It only ever uses Jellyfin's `Path` field.
- `sync.py` copies from the filesystem. It knows nothing about Jellyfin.
- `state.py` owns the manifest, and is the only code that can delete anything.
- `notify.py` never lets a mail problem change the outcome of a backup.

---

## Command reference

| Command | What it does |
| --- | --- |
| `favsav` | Sync. Dry run unless `--no-dry-run` is given. |
| `favsav --list` | Show the favorites and where each would go. Changes nothing. |
| `favsav --verify` | Check that the destination still holds everything the manifest records. |
| `favsav --check-destination` | Check the destination is usable, then exit. |
| `favsav --dry-run` | Force a dry run. |
| `favsav --no-dry-run` | Actually copy. |
| `favsav --prune` | Also remove copies of items that are no longer favorites. |
| `favsav --no-notify` | Do not send the email for this run. |
| `favsav --strict-notify` | Treat a failed email as a failed run. |
| `favsav --init-config [PATH]` | Write an example configuration file. |
| `favsav -v` / `-q` | More or less logging. |

Exit codes: `0` success, `1` failure, `2` a usage or configuration problem, `3` the
backup worked but the email could not be sent.

---

## Troubleshooting

**"the destination is not a mounted filesystem"**
The backup disk is not mounted, or is mounted somewhere else. Check `findmnt` and
`dest_root`. If your backup disk really is a plain folder on the same disk, set
`require_mount = false` -- but then an unmounted disk will silently swallow your
backup.

**"outside the configured roots"**
An item is favorited but its path is not under any `roots` entry. Usually the media
lives somewhere else, or the local prefix is wrong. `favsav --list` shows exactly
which path was rejected.

**"not found on disk"**
Jellyfin knows about the file but it is not there. A stale library entry, or the
media is on a disk that is not mounted.

**Nothing happens and no mail arrives**
Check the log. `journalctl -u favsav` shows what the run did. If mail is not
configured, that is expected.

**A series was copied but some episodes are missing**
favsav copies what is in the folder, not what Jellyfin lists. If Jellyfin lists an
episode that is not on disk, there is nothing to copy.

---

## Development

```sh
python3 -m unittest discover -s tests -t .
```

The tests need no network and no Jellyfin: they run against a small fake server and
a temporary media tree. They cover the parts that matter -- that a directory is
copied whatever its shape, that only favourites are pruned, that the manifest round
trips, and that an unconfigured SMTP setup never touches the network.

To try it against a real server without writing anything:

```sh
favsav --config /path/to/favsav.ini --list
```

---

## License

MIT. See [LICENSE](LICENSE).
