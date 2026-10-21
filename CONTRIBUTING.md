# Contributing

Thanks for taking a look.

## Getting set up

favsav has no dependencies. Clone it and run the tests:

```sh
git clone https://github.com/echastain/favsav.git
cd favsav
python3 -m unittest discover -s tests -t .
```

Python 3.8 or newer. Please keep it that way -- the tool is meant to run on whatever
Python a media server happens to have.

## Reporting a bug

Include:

- the output of `favsav -v --list` (with the API key removed),
- the version of Jellyfin (`Dashboard -> Advanced -> Jellyfin Info`),
- the version of Python,
- what you expected and what happened instead.

## Pull requests

- Keep the standard library rule. Adding a dependency means every user needs to
  install it, and the whole point is that favsav is a file you can copy onto a server
  and run.
- Add a test with the change. The tests must not need a network or a Jellyfin
  server.
- Keep the two rules that the design turns on intact:
  1. never construct a file path from item names or from the library hierarchy --
     only ever use the absolute `Path` Jellyfin reports;
  2. never assume a directory shape -- walk the tree and copy what is there.
- Update `CHANGELOG.md`.

## Things that are deliberately not supported

- Writing to Jellyfin. favsav is read-only towards the server.
- Pruning anything favsav did not copy.
- Following symlinks out of the media library by default.
- Watching the library for changes. Use the timer, or run it yourself.

## Reporting a security problem

Please do not open a public issue. See [SECURITY.md](SECURITY.md).
