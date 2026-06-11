#!/usr/bin/env python3
"""End-to-end check on the in-browser validator (Playwright + Chromium).

Serves site/ over a local HTTP server, drives a real Chromium through
site/index.html, and for every fixture in tests/fixtures/expectations.json:
  • uploads the file,
  • (optionally) forces a format via the drop-down, else leaves it on
    Auto-detect,
  • clicks Validate,
  • asserts the detected format and the result badge match expectations.

The fixtures deliberately exercise:
  • a valid and an invalid document,
  • auto-detect by Customization-ID EXACT match (Peppol BIS), and
  • auto-detect by Customization-ID PREFIX / startswith (plain EN 16931).

Gated on Playwright + Chromium; skipped with a hint if not installed.

Usage:
    uv run --with playwright python tools/validate_browser_headless.py
    # one-time: uv run --with playwright playwright install chromium
"""
from __future__ import annotations

import json
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from _common import ROOT, log

SITE = ROOT / "site"
EXPECT = ROOT / "tests" / "fixtures" / "expectations.json"


def _serve(directory) -> tuple[ThreadingHTTPServer, int]:
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("playwright not installed — skipping. Enable with:")
        log("  uv add playwright && uv run playwright install chromium")
        return 0
    if not EXPECT.exists():
        log(f"no fixtures/expectations at {EXPECT} — skipping browser test")
        return 0

    cases = json.loads(EXPECT.read_text())
    httpd, port = _serve(SITE)
    base = f"http://127.0.0.1:{port}/index.html"
    log(f"serving {SITE} on {base}")

    failures = 0
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as e:  # noqa: BLE001
                log(f"Chromium unavailable ({e}) — run: uv run playwright install chromium")
                return 0
            page = browser.new_page()
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(base)
            page.wait_for_function(
                "document.querySelectorAll('#phive-format option').length > 1",
                timeout=20000)
            n_formats = page.eval_on_selector_all("#phive-format option", "els => els.length")
            log(f"drop-down populated: {n_formats - 1} format(s)")

            for case in cases:
                fixture = ROOT / case["file"]
                page.eval_on_selector("#phive-format", "el => el.value = ''")
                if case.get("force_format"):
                    page.select_option("#phive-format", case["force_format"])
                page.set_input_files("#phive-input", str(fixture))
                page.click("#phive-run")
                page.wait_for_function(
                    "document.querySelector('#phive-status').textContent.startsWith('Done')",
                    timeout=60000)

                badge = page.eval_on_selector("[data-testid='badge']", "el => el.textContent").strip()
                meta = page.eval_on_selector(".phive-row__meta", "el => el.textContent")
                banner = page.eval_on_selector(".phive-row__banner", "el => el.className")
                want_badge = case["expect_badge"]
                want_key = case.get("expect_format")
                want_match = case.get("expect_match")        # 'exact' | 'best'
                ok = (badge == want_badge
                      and (want_key is None or want_key in meta)
                      and (want_match is None or f"phive-banner--{want_match}" in banner))
                tag = "✓" if ok else "✗"
                log(f"  {tag} {case['file']}: badge={badge!r} (want {want_badge!r}); "
                    f"detected {want_key!r}: {want_key in meta if want_key else 'n/a'}; "
                    f"banner {want_match!r}: "
                    f"{f'phive-banner--{want_match}' in banner if want_match else 'n/a'}")
                if not ok:
                    failures += 1
                page.click("#phive-clear")

            # ── Analytics dashboard: validate all fixtures at once, then
            #    switch to the Analytics tab and assert the dashboard built.
            page.set_input_files("#phive-input", [str(ROOT / c["file"]) for c in cases])
            page.click("#phive-run")
            page.wait_for_function(
                "document.querySelector('#phive-status').textContent.startsWith('Done')",
                timeout=120000)
            page.click(".phive__tab[data-tab='analytics']")
            page.wait_for_selector(".phive-card", timeout=10000)
            cards = page.eval_on_selector_all(".phive-card .phive-card__n", "els => els.map(e=>e.textContent)")
            has_top_errors = page.query_selector("[data-rule]") is not None
            # filter to Failed and confirm the file table shrinks
            page.select_option("#phive-an-outcome", "fail")
            page.wait_for_timeout(200)
            fail_rows = page.eval_on_selector_all(".phive-an__sec:last-child .phive-an__tbl tbody tr", "els => els.length")
            an_ok = len(cards) >= 5 and has_top_errors and fail_rows >= 1
            log(f"  {'✓' if an_ok else '✗'} analytics: cards={cards[:5]} top-errors={has_top_errors} failed-rows={fail_rows}")
            if not an_ok:
                failures += 1

            if errors:
                log(f"  browser console errors: {errors[:5]}")
            browser.close()
    finally:
        httpd.shutdown()

    if failures:
        log(f"✗ {failures} browser case(s) failed")
        return 1
    log(f"✓ all {len(cases)} browser case(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
