#!/usr/bin/env python3
"""Step 0 of the orchestrator — fetch the vendored engines.

Pulls the (large, build-stable) runtimes at build time instead of committing
them, pinned in config/sources.yaml `vendor`:

  • Browser runtime → site/assets/vendor/ (gitignored, shipped in the site):
      - SaxonJS2.js (Schematron) — extracted from Saxonica's versioned zip;
        the browser build is NOT on npm, only that zip ships it.
      - xmllint-wasm (XSD) — index-browser.mjs + xmllint-browser.mjs +
        xmllint.wasm, from the npm CDN (jsDelivr).
  • Build tool → work/vendor/node_modules/ (gitignored, used by compile_sef):
      - xslt3 (CLI) + saxon-js (Node build SaxonJS2N.js) from the npm CDN,
        plus a generated `axios` stub (saxon-js requires it for HTTP, which
        the offline `-nogo` SEF compile never triggers).

Idempotent: existing files are left as-is (re-run is cheap). `update.sh
--clean` removes them to force a fresh fetch. Network: jsDelivr + Saxonica.

Usage:
    uv run --with pyyaml python tools/fetch_vendor.py
"""
from __future__ import annotations

import io
import sys
import urllib.request
import zipfile

import yaml

from _common import SITE, SOURCES, WORK, log

SITE_VENDOR = SITE / "assets" / "vendor"
BUILD_NODE_MODULES = WORK / "vendor" / "node_modules"

AXIOS_STUB_PKG = '{ "name": "axios", "version": "0.0.0-stub", "main": "./index.js" }\n'
AXIOS_STUB_JS = (
    "// Stub: saxon-js `require('axios')` for HTTP fetches, which the offline\n"
    "// `-nogo` SEF compile never triggers. Avoids vendoring real axios + deps.\n"
    "module.exports = function () { throw new Error('axios stub: no network in SEF compile'); };\n"
    "module.exports.default = module.exports;\n"
)


def _get(url: str) -> bytes:
    log(f"GET {url}")
    with urllib.request.urlopen(url, timeout=180) as r:
        return r.read()


def _save(url: str, dest) -> bool:
    if dest.exists():
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.write_bytes(_get(url))
        return True
    except Exception as e:  # noqa: BLE001
        log(f"  ✗ {e}")
        return False


def main() -> int:
    v = (yaml.safe_load(SOURCES.read_text()) or {}).get("vendor") or {}
    cdn = v.get("cdn", "https://cdn.jsdelivr.net/npm")
    ok = True

    # 1. Browser Saxon-JS — extract SaxonJS2.js from Saxonica's zip.
    saxon_js = SITE_VENDOR / "saxonjs" / "SaxonJS2.js"
    if not saxon_js.exists():
        try:
            zf = zipfile.ZipFile(io.BytesIO(_get(v["saxon_browser_zip"])))
            member = next(n for n in zf.namelist() if n.endswith("/SaxonJS2.js")
                          or n == "SaxonJS2.js")
            saxon_js.parent.mkdir(parents=True, exist_ok=True)
            saxon_js.write_bytes(zf.read(member))
            log(f"  ✓ SaxonJS2.js ({saxon_js.stat().st_size // 1024} KB)")
        except Exception as e:  # noqa: BLE001
            log(f"  ✗ Saxon-JS browser build: {e}")
            ok = False
    else:
        log("  SaxonJS2.js cached")

    # 2. xmllint-wasm browser files.
    xv = v.get("xmllint_wasm", "4.0.2")
    for f in ("index-browser.mjs", "xmllint-browser.mjs", "xmllint.wasm"):
        ok &= _save(f"{cdn}/xmllint-wasm@{xv}/{f}", SITE_VENDOR / "xmllint-wasm" / f)

    # 3. Node build tool (xslt3 + saxon-js Node build + axios stub).
    sv, x3 = v.get("saxon_js", "2.7.0"), v.get("xslt3", "2.7.0")
    ok &= _save(f"{cdn}/xslt3@{x3}/xslt3.js", BUILD_NODE_MODULES / "xslt3" / "xslt3.js")
    ok &= _save(f"{cdn}/xslt3@{x3}/package.json", BUILD_NODE_MODULES / "xslt3" / "package.json")
    ok &= _save(f"{cdn}/saxon-js@{sv}/SaxonJS2N.js", BUILD_NODE_MODULES / "saxon-js" / "SaxonJS2N.js")
    ok &= _save(f"{cdn}/saxon-js@{sv}/package.json", BUILD_NODE_MODULES / "saxon-js" / "package.json")
    axios = BUILD_NODE_MODULES / "axios"
    if not (axios / "index.js").exists():
        axios.mkdir(parents=True, exist_ok=True)
        (axios / "package.json").write_text(AXIOS_STUB_PKG, encoding="utf-8")
        (axios / "index.js").write_text(AXIOS_STUB_JS, encoding="utf-8")

    log("✓ vendor engines ready" if ok else "✗ some vendor fetches failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
