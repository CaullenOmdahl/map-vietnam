#!/usr/bin/env python3
"""Map Vietnam in its own window: the map server runs inside this process and a native window
(pywebview: WebView2 on Windows, WKWebView on macOS, Qt or GTK WebKit on Linux) shows the viewer.
Nobody sees a browser, a terminal or an address bar.

    python scripts/desktop.py                  open the window (start.py does this for you)
    python scripts/desktop.py --no-window      only serve, and print the address (tests, CI smoke checks)
    python scripts/desktop.py --port 9000      choose the port on 127.0.0.1 (default: any free one)
    python scripts/desktop.py --run-script S   run scripts/S as `python S` would; the packaged app
                                               starts its child jobs this way (see paths.py)

Threads: the server runs in a daemon thread; the window owns the main thread, as every GUI toolkit
requires. Stopping is the same from every side. "Quit Map Vietnam" in the viewer stops the server
(which first ends the record and map jobs), the server thread then closes the window, and the
process exits. Closing the window stops the server the same way (its clean-up, not a kill), then
exits. A signal (Control-C, SIGTERM from start.py) does the same.

Two things a native window lacks and this module adds, in a small script injected into every page
load, so the viewer's own code stays untouched:
  * links to other sites (record sources, map archives) open in the user's default browser, never
    inside the app window or as a second window;
  * the export links (GPX, KML, GeoJSON) show the system's Save dialog, because a webview has no
    download bar. The window fetches the file from its own server and writes it where asked.

If no window can be opened here (no GUI toolkit installed, no display) the map opens in the default
browser instead, with a one-line explanation, so the app is never unusable.

The decisions (is this link ours? is this an export? what is the file called?) are plain functions,
tested without a GUI. Nothing heavy is imported before the `--run-script` check, which must stay
cheap: the packaged app relaunches itself that way for every child job.
"""

import argparse
import importlib
import re
import signal
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

import paths

TITLE = "Map Vietnam"
WINDOW_SIZE = (1280, 820)
MIN_SIZE = (360, 560)  # small enough for the viewer's phone-like layout
STOP_TIMEOUT_S = 40  # a server stopping ends its jobs first (each waits for its child process)

EXPORT_PATH = re.compile(r"^/api/cases/[^/]+/export\.(gpx|kml|geojson)$")
EXPORT_TYPES = {"gpx": "GPX", "kml": "KML", "geojson": "GeoJSON"}


# ---- decisions, without a GUI ---------------------------------------------------------------------
def origin_of(url):
    """The (scheme, host, port) of an http(s) address, or None for anything else. Ports are filled
    in so http://127.0.0.1:80 and http://127.0.0.1 agree."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None


def is_external(url, own_url):
    """True for a web address on another site: the one kind of link that belongs in the user's
    browser. Our own pages, javascript:, mailto:, blob: and the like are left to the window."""
    theirs = origin_of(url)
    return theirs is not None and theirs != origin_of(own_url)


def export_format(url, own_url):
    """ "gpx", "kml" or "geojson" if url is one of our own case-export links, else None."""
    if origin_of(url) is None or origin_of(url) != origin_of(own_url):
        return None
    found = EXPORT_PATH.match(urlsplit(url).path)
    return found.group(1) if found else None


def export_filename(name, fmt):
    """A file name safe to offer in a Save dialog: the case's name, without path separators or
    characters Windows refuses, ending in .<fmt>."""
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or "")).strip(" .")
    if stem.lower().endswith("." + fmt):
        stem = stem[: -len(fmt) - 1].rstrip(" .")
    return f"{stem or 'map-vietnam-case'}.{fmt}"


# ---- what the page may ask the window to do ------------------------------------------------------
class WindowApi:
    """The methods the injected script calls (window.pywebview.api.*). Everything it is given is
    checked here again: the page is the viewer, but a page is never trusted with the computer."""

    def __init__(self, own_url, window=None, opener=None, save_dialog=None):
        self._own_url = own_url
        self._window = window
        self._open = opener or webbrowser.open
        self._save_dialog = save_dialog or self._ask_where_to_save

    def open_external(self, url):
        """Open another site's address in the default browser. True if it was one."""
        if not isinstance(url, str) or not is_external(url, self._own_url):
            return False
        self._open(url)
        return True

    def save_export(self, url, name):
        """Fetch one of our own export links and save it where the person says. Returns
        {"saved": path}, {"cancelled": true} or {"error": plain message}."""
        fmt = export_format(url, self._own_url) if isinstance(url, str) else None
        if fmt is None:
            return {"error": "That is not an export from this map."}
        try:
            # Never through a proxy: this is the local server.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=30) as response:
                data = response.read()
        except OSError as e:
            return {"error": f"Could not make the export: {e}"}
        target = self._save_dialog(export_filename(name, fmt))
        if not target:
            return {"cancelled": True}
        try:
            Path(target).write_bytes(data)
        except OSError as e:
            return {"error": f"Could not save the file there: {e.strerror or e}"}
        return {"saved": str(target)}

    def _ask_where_to_save(self, filename):
        import webview

        kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None)
        chosen = self._window.create_file_dialog(
            kind if kind is not None else webview.SAVE_DIALOG, save_filename=filename
        )
        # Windows and Linux return a path (or a one-item list); a cancelled dialog returns None.
        if isinstance(chosen, (list, tuple)):
            chosen = chosen[0] if chosen else None
        return chosen or None


SHIM = r"""
(() => {
  if (window.__mapVietnamShim) return;
  window.__mapVietnamShim = true;
  const EXPORT = /^\/api\/cases\/[^/]+\/export\.(gpx|kml|geojson)$/;
  const say = text => { try { toast(text, 5000); } catch (e) {} };
  const api = () => window.pywebview && window.pywebview.api;
  const onClick = event => {
    const a = event.target && event.target.closest && event.target.closest("a[href]");
    if (!a || event.defaultPrevented || (event.type === "auxclick" && event.button !== 1)) return;
    const url = new URL(a.href, location.href);
    if (url.origin === location.origin && EXPORT.test(url.pathname)) {
      event.preventDefault();
      if (!api()) return say("The window is still starting. Try again in a moment.");
      api().save_export(url.href, a.getAttribute("download") || "").then(r => {
        if (r.saved) say("Saved " + r.saved);
        else if (r.error) say(r.error);
      });
    } else if (/^https?:$/.test(url.protocol) && url.origin !== location.origin) {
      event.preventDefault();
      if (api()) api().open_external(url.href);
    }
  };
  document.addEventListener("click", onClick, true);
  document.addEventListener("auxclick", onClick, true);
  // A script that opens a window itself goes to the browser as well.
  const open = window.open;
  window.open = (url, ...rest) => {
    const to = url ? new URL(url, location.href) : null;
    if (to && /^https?:$/.test(to.protocol) && to.origin !== location.origin && api()) { api().open_external(to.href); return null; }
    return open.call(window, url, ...rest);
  };
})();
"""


# ---- the server ---------------------------------------------------------------------------------
def start_server(port):
    """The map server, running in a daemon thread. Returns (server, thread); the thread ends when
    the server has stopped and its socket is closed. Interrupted work is recovered as
    server.main() does it."""
    import library
    import server

    lib = library.Library()
    lib.recover_interrupted()  # maps whose adding was cut short when the app last stopped
    lib.write_catalog()  # the command line may have changed the maps while no server ran
    srv = server.make_server(port, map_library=lib)

    def serve():
        try:
            srv.serve_forever()
        finally:
            srv.server_close()

    thread = threading.Thread(target=serve, name="map-server", daemon=True)
    thread.start()
    return srv, thread


def stop_server(srv, thread):
    """Stop it the way "Quit" does (jobs end first) and wait for that to finish."""
    srv.quit()
    thread.join(STOP_TIMEOUT_S)


def stop_on_signals(srv):
    """Control-C, SIGTERM and SIGHUP stop the server cleanly (the window then closes or the
    wait ends). Python only allows this on the main thread; elsewhere (a test) it is skipped."""
    if threading.current_thread() is not threading.main_thread():
        return
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda signum, frame: srv.quit())


def wait_for(thread):
    """Block until the server thread ends, in short steps so a signal is handled promptly (on
    Windows a bare join() would not be interrupted)."""
    while thread.is_alive():
        thread.join(0.3)


# ---- the window ---------------------------------------------------------------------------------
def on_page_loaded(window, own_url, opener=None):
    """What to do each time the window finishes loading a page. Our own page gets the shim. Anything
    else (a navigation that slipped past the shim: a script setting location, a form post) is never
    left on screen, because the window has no address bar to get back from and the page would be
    handed save_export and open_external: send the window home and show the address in the browser."""
    try:
        current = window.get_current_url()
    except Exception:  # the window is closing; there is nothing to protect
        return
    if current and origin_of(current) != origin_of(own_url):
        window.load_url(own_url)
        if is_external(current, own_url):
            (opener or webbrowser.open)(current)
        return
    window.run_js(SHIM)


def window_toolkit(frozen=None, platform=None):
    """Which window toolkit to ask pywebview for, or None to let it choose. The packaged Linux app
    carries only the Qt one, so it asks for Qt by name; otherwise pywebview tries GTK first and prints
    an import traceback before it settles on Qt."""
    frozen = paths.is_frozen() if frozen is None else frozen
    platform = sys.platform if platform is None else platform
    return "qt" if frozen and platform.startswith("linux") else None


def check_window():
    """Load the window toolkit without opening a window. Returns the exit status: 0 if it loaded.
    The release build runs this on the packaged app, so a toolkit missing from the bundle (a hidden
    import PyInstaller did not find) fails the build instead of reaching a user."""
    try:
        guilib = importlib.import_module("webview.guilib")  # the package attribute of that name is a placeholder

        guilib.initialize(window_toolkit())
    except Exception as error:
        print(f"The window toolkit could not be loaded: {error!r}", flush=True)
        return 1
    print("window toolkit ok", flush=True)
    return 0


def open_window(url, srv, thread):
    """Show the viewer in a native window until it is closed or the server stops. Returns True if
    a window was shown; raises if none could be (the caller falls back to the browser)."""
    import webview

    api = WindowApi(url)
    window = webview.create_window(
        TITLE, url, js_api=api, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=MIN_SIZE
    )
    api._window = window
    window.events.loaded += lambda: on_page_loaded(window, url)

    def close_when_server_stops():
        thread.join()
        try:
            window.destroy()
        except Exception:  # the window is already gone: the person closed it
            pass

    threading.Thread(target=close_when_server_stops, name="close-window", daemon=True).start()
    webview.start(gui=window_toolkit())  # returns when the window has closed
    return True


def run(args):
    """Serve, then show the window (or just serve with --no-window). Returns the exit status."""
    srv, thread = start_server(args.port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/app/"
    stop_on_signals(srv)
    try:
        if args.no_window:
            print(url, flush=True)
            wait_for(thread)
            return 0
        try:
            open_window(url, srv, thread)
        except Exception as error:  # no GUI toolkit, no display, a broken backend
            if thread.is_alive():
                print(
                    f"Map Vietnam could not open its own window here ({error}); opening it in your web browser instead.",
                    flush=True,
                )
                if not webbrowser.open(url):
                    print(f"Open {url} in your web browser.", flush=True)
                wait_for(thread)
        return 0
    finally:
        stop_server(srv, thread)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Open Map Vietnam in its own window.")
    ap.add_argument("--no-window", action="store_true", help="only serve; print the address and wait")
    ap.add_argument("--check-window", action="store_true", help="load the window toolkit, then exit (build checks)")
    ap.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free port)")
    args = ap.parse_args(argv)
    if not 0 <= args.port <= 65535:
        ap.error("--port must be a number from 0 to 65535")
    return args


def main(argv=None):
    """Open the window, or serve only. Returns the exit status."""
    argv = sys.argv[1:] if argv is None else list(argv)
    paths.attach_standard_streams()  # a windowed Windows app has no console unless it is given handles
    if argv[:1] == [paths.RUN_SCRIPT_FLAG]:
        return paths.run_script_from_argv(argv)  # before anything heavy is imported
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = parse_args(argv)
    if args.check_window:
        return check_window()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
