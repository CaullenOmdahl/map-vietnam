#!/bin/sh
# Double-click this file in Finder (macOS) to start Map Vietnam and open it in your browser.
# It finds a Python 3.12 or newer and runs start.py from this folder. The Terminal window that
# opens must stay open while the map is in use; "Quit Map Vietnam" in the map (or closing the
# window, or Control-C) stops it.

cd "$(dirname "$0")" || exit 1

# Newest first. python.org installs into /Library/Frameworks and links python3.x into
# /usr/local/bin; Homebrew uses /opt/homebrew (Apple silicon) or /usr/local (Intel).
for py in python3.15 python3.14 python3.13 python3.12 \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
    /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$py" >/dev/null 2>&1 &&
        "$py" -c 'import sys; sys.exit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
        exec "$py" start.py "$@"
    fi
done

echo "Map Vietnam needs Python 3.12 or newer, and none was found on this Mac."
echo "Download Python from https://www.python.org/downloads/ , install it,"
echo "then double-click 'Start Map Vietnam.command' again."
exit 1
