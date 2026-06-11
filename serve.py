#!/usr/bin/env python3
"""Dev preview server for the phive-web validator.

Launches a small HTTP server rooted at `site/` and opens the validator
page in the default browser. Serving over a real HTTP origin (rather than
opening the file:// URL) matters: the page lazy-loads ES modules
(xmllint-wasm) and fetches the manifest + SEF + XSD assets, which browsers
block under file://.

Usage:
    uv run python serve.py                    # default port 8765
    uv run python serve.py --port 8000
    uv run python serve.py --no-browser       # don't auto-open

Stop the server with Ctrl-C. This is a developer-only tool — there is no
auth, no TLS, and no production-relevant features here.
"""
from __future__ import annotations

import argparse
import http.server
import pathlib
import socket
import sys
import webbrowser

ROOT = pathlib.Path(__file__).resolve().parent
SITE_ROOT = ROOT / "site"
MANIFEST = SITE_ROOT / "validation-assets" / "manifest.json"


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static file handler rooted at `site/`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE_ROOT), **kwargs)

    # Large, build-stable assets the browser SHOULD cache. The validation
    # assets (gzipped SEFs, XSD closures) and the vendored runtimes
    # (SaxonJS2.js ~2.4 MB, xmllint.wasm ~0.8 MB) are fetched repeatedly —
    # once per worker in the pool, plus on every reload. Without caching the
    # dev server re-sends all of it every time (the no-store default below).
    _CACHEABLE = ("/validation-assets/", "/assets/vendor/")

    def end_headers(self):                                                  # noqa: N802 — std lib name
        """Cache the immutable assets so repeat validations / reloads hit the
        browser cache instead of re-downloading; keep the app shell
        (HTML/JS/CSS) no-store so edits-then-refresh always shows fresh.
        After rebuilding assets, hard-reload (Cmd/Ctrl-Shift-R) to refresh."""
        path = self.path.split("?", 1)[0]
        if any(seg in path for seg in self._CACHEABLE):
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):                                       # quieter access log
        sys.stderr.write("  %s — %s\n" % (self.address_string(), fmt % args))


class Server(http.server.ThreadingHTTPServer):
    """Subclass to set `allow_reuse_address = True` at class level so
    rapid stop/start cycles don't hit a stale-TIME_WAIT bind error."""
    allow_reuse_address = True
    daemon_threads = True


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765,
                        help="port to listen on (default: 8765)")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't auto-open the page in a browser")
    args = parser.parse_args(argv)

    if not (SITE_ROOT / "index.html").exists():
        print(f"error: {SITE_ROOT}/index.html does not exist.", file=sys.stderr)
        return 1
    if not MANIFEST.exists():
        print(f"warning: {MANIFEST.relative_to(ROOT)} is missing — the format\n"
              "         drop-down will be empty. Build the assets first:\n"
              "           bash tools/update.sh", file=sys.stderr)

    if _port_in_use(args.port):
        print(f"error: port {args.port} already in use. "
              f"Re-run with --port to pick another.", file=sys.stderr)
        return 2

    url = f"http://localhost:{args.port}/"
    rel = SITE_ROOT.relative_to(ROOT) if SITE_ROOT.is_relative_to(ROOT) else SITE_ROOT
    print(f"Serving {rel} on {url}")
    print("Press Ctrl-C to stop.\n")

    with Server(("", args.port), Handler) as httpd:
        if not args.no_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
