#!/bin/sh
# Install favsav on this machine.
#
#   sudo ./packaging/install.sh
#
# It installs the tool, the systemd units and an example configuration.  It does
# not start anything and does not change the configuration: read the README, edit
# /etc/favsav/config.ini, and run "favsav --list" before you let it copy anything.

set -eu

PREFIX="${PREFIX:-/usr/local}"
LIBDIR="$PREFIX/lib/favsav"
BINDIR="$PREFIX/bin"
CONFDIR="${CONFDIR:-/etc/favsav}"
STATEDIR="${STATEDIR:-/var/lib/favsav}"
USER_NAME="${USER_NAME:-favsav}"
GROUP_NAME="${GROUP_NAME:-favsav}"

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "This script needs to run as root." >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required but was not found." >&2
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
case "$PYTHON_VERSION" in
    3.8|3.9|3.1[0-9]|3.2[0-9]) ;;
    *)
        echo "Python 3.8 or newer is required; found $PYTHON_VERSION." >&2
        exit 1
        ;;
esac

echo "Installing favsav from $REPO_DIR"

# ---------------------------------------------------------------- the account
if ! getent group "$GROUP_NAME" >/dev/null 2>&1; then
    groupadd --system "$GROUP_NAME"
fi
if ! id "$USER_NAME" >/dev/null 2>&1; then
    useradd --system --gid "$GROUP_NAME" --home-dir "$STATEDIR" \
        --shell /usr/sbin/nologin "$USER_NAME"
fi

# ---------------------------------------------------------------- the tool
install -d -m 0755 "$LIBDIR"
rm -rf "$LIBDIR/favsav"
cp -r "$REPO_DIR/favsav" "$LIBDIR/favsav"
find "$LIBDIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

install -d -m 0755 "$BINDIR"
# The launcher already knows where an installation lives (/usr/local/lib/favsav),
# so it is copied verbatim.  Rewriting HERE with the lib directory used to break
# the path resolution, because HERE is meant to be the script's own directory.
install -m 0755 "$REPO_DIR/bin/favsav" "$BINDIR/favsav"

# ---------------------------------------------------------------- the config
install -d -m 0750 "$CONFDIR"
# The directory itself must be traversable by the favsav group, otherwise a user
# in that group (or the service account) cannot even read the files inside it.
chown root:"$GROUP_NAME" "$CONFDIR"
if [ ! -f "$CONFDIR/config.ini" ]; then
    install -m 0640 "$REPO_DIR/packaging/config.ini.example" "$CONFDIR/config.ini"
    echo "Wrote an example configuration to $CONFDIR/config.ini"
fi
if [ ! -f "$CONFDIR/favsav.env" ]; then
    install -m 0640 "$REPO_DIR/packaging/favsav.env.example" "$CONFDIR/favsav.env"
    echo "Wrote an example secrets file to $CONFDIR/favsav.env"
fi
chown root:"$GROUP_NAME" "$CONFDIR" "$CONFDIR/config.ini" "$CONFDIR/favsav.env" 2>/dev/null || true

# ---------------------------------------------------------------- the state
# 2770 (setgid + group write) so that files created by an admin running favsav
# by hand inherit the favsav group and stay writable by the service account.
install -d -m 2770 -o "$USER_NAME" -g "$GROUP_NAME" "$STATEDIR"

# ---------------------------------------------------------------- the units
if command -v systemctl >/dev/null 2>&1; then
    install -m 0644 "$REPO_DIR/packaging/favsav.service" /etc/systemd/system/favsav.service
    install -m 0644 "$REPO_DIR/packaging/favsav.timer" /etc/systemd/system/favsav.timer
    systemctl daemon-reload
    echo "Installed the systemd units.  Enable the timer with:"
    echo "    systemctl enable --now favsav.timer"
fi

cat <<EOF

favsav is installed.

Next steps:
  1. Edit $CONFDIR/config.ini  (Jellyfin URL, roots, dest_root)
  2. Put the API key in $CONFDIR/favsav.env  as FAVSAV_API_KEY
  3. Check the destination:      favsav --check-destination
  4. See what would happen:      favsav --list
  5. Let it run:                 favsav --no-dry-run

Nothing has been copied yet.
EOF
