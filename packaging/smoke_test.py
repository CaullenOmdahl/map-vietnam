#!/usr/bin/env python3
"""Smoke test of a built (frozen) Map Vietnam: does the packaged app start and answer?

    python packaging/smoke_test.py "dist/Map Vietnam/Map Vietnam.exe"

It starts the executable with --no-window on a free port and a throw-away data folder
(MAPVIETNAM_HOME), then checks that
  * the viewer page (/app/) and the map library (/api/library) are served,
  * a child job runs the way the app starts them: `<exe> --run-script catalog.py --help`,
  * the window toolkit loads (`<exe> --check-window`; no window is opened),
  * POST /api/quit stops the app, and the process exits.
It exits 0 when all of that held, and 1 with a plain message otherwise. Standard library only, so
the release workflow can run it on every platform without installing anything.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request

START_TIMEOUT_S = 90  # the first start unpacks and imports a lot
STOP_TIMEOUT_S = 60


def fail(message):
    print(f"FAILED: {message}", file=sys.stderr)
    return 1


def fetch(url, data=None):
    # Never through a proxy: this is the local server.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with opener.open(request, timeout=30) as response:
        return response.status, response.read()


def first_line(process, timeout):
    """The app's first line of output (its address), or None if it does not come in time."""
    found = []
    reader = threading.Thread(target=lambda: found.append(process.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    return found[0].strip() if found and found[0] else None


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    exe = os.path.abspath(argv[1])
    with tempfile.TemporaryDirectory(prefix="map-vietnam-smoke-") as home:
        env = {**os.environ, "MAPVIETNAM_HOME": home}

        child = subprocess.run(
            [exe, "--run-script", "catalog.py", "--help"],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=START_TIMEOUT_S,
        )
        if child.returncode != 0 or "usage" not in child.stdout.lower():
            return fail(f"--run-script catalog.py --help exited {child.returncode}: {child.stdout} {child.stderr}")
        print("child script ok:", child.stdout.splitlines()[0])

        # The window toolkit is the part --no-window never touches: load it, without opening a window.
        toolkit = subprocess.run(
            [exe, "--check-window"],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=START_TIMEOUT_S,
        )
        if toolkit.returncode != 0:
            return fail(f"--check-window exited {toolkit.returncode}: {toolkit.stdout} {toolkit.stderr}")
        print("window toolkit ok")

        app = subprocess.Popen(
            [exe, "--no-window"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            url = first_line(app, START_TIMEOUT_S)
            if not url or not url.startswith("http://127.0.0.1:"):
                return fail(f"the app did not print its address (got {url!r})")
            print("serving at", url)
            base = url.split("/app/")[0]
            status, body = fetch(url)
            if status != 200 or b"<html" not in body.lower():
                return fail(f"{url} gave {status}")
            print("viewer page ok")
            status, body = fetch(base + "/api/library")
            maps = json.loads(body).get("maps")
            if status != 200 or not maps:
                return fail(f"/api/library gave {status} and {len(maps or [])} maps")
            print(f"library ok: {len(maps)} maps can be added")
            fetch(base + "/api/quit", data=b"{}")
            app.wait(STOP_TIMEOUT_S)
            print("quit ok, exit status", app.returncode)
            return 0 if app.returncode == 0 else fail(f"the app exited with {app.returncode}")
        except Exception as error:
            return fail(f"{type(error).__name__}: {error}")
        finally:
            if app.poll() is None:
                app.kill()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
