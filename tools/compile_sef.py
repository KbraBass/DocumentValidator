#!/usr/bin/env python3
"""Step 3 of the orchestrator — compile Schematron XSLT → Saxon-JS SEF.

phive-rules ships pre-compiled XSLT 2.0 (one self-contained stylesheet
per ruleset). Saxon-JS HE (`SaxonJS2.js`) in the browser only executes
SEF (Stylesheet Export File, JSON form) — its `transform()` rejects raw
XSL. saxonche HE cannot export SEF (Saxon-EE only), so we shell out to
Node + the `xslt3` CLI fetched by fetch_vendor.py into
work/vendor/node_modules/ (xslt3 + the saxon-js Node build + an axios stub).

Prerequisite: Node.js on PATH (any modern version), and step 1
(fetch_vendor.py) having run.

Walks every `*.xslt` under the cloned modules' external/schematron/ and
writes a sibling `*.sef.json` (in place, inside the gitignored
work/phive-rules/). Skips files whose SEF is already newer than the
source.

Usage:
    uv run python tools/compile_sef.py
"""
from __future__ import annotations

import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from _common import PHIVE_DIR, VENDOR_XSLT3, log, run


def compile_one(xslt) -> tuple[str, str]:
    """Compile one XSLT → sibling .sef.json. Returns (status, detail) where
    status is 'cached' | 'compiled' | 'failed'. Each call spawns its own
    Node process, so these are safe to run concurrently from threads — the
    GIL is released while `subprocess.run` waits on Node."""
    sef = xslt.with_suffix(".sef.json")
    if sef.exists() and sef.stat().st_mtime >= xslt.stat().st_mtime:
        return "cached", ""
    p = run([
        "node", str(VENDOR_XSLT3),
        f"-xsl:{xslt}", f"-export:{sef}", "-nogo", "-t",
    ], check=False)
    if p.returncode != 0 or not sef.exists():
        return "failed", p.stderr.strip()[:400]
    return "compiled", ""


def _workers() -> int:
    """One worker per core (each Node compile is single-threaded + CPU-bound).
    Override with PHIVE_SEF_JOBS."""
    env = os.environ.get("PHIVE_SEF_JOBS")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    return max(1, (os.cpu_count() or 4))


def main() -> int:
    if not shutil.which("node"):
        log("node not available — install Node.js to enable the Schematron "
            "layer. The browser tool's XSD layer still works without it.")
        return 0
    if not VENDOR_XSLT3.exists():
        log(f"vendored xslt3 missing: {VENDOR_XSLT3}")
        return 1
    if not PHIVE_DIR.exists():
        log("no clone — run fetch_phive.py first")
        return 1

    xslts = sorted(PHIVE_DIR.glob(
        "phive-rules-*/src/main/resources/external/schematron/**/*.xslt"))
    if not xslts:
        log("no .xslt found under the clone")
        return 0

    n = len(xslts)
    jobs = min(_workers(), n)
    log(f"Compiling {n} XSLT → SEF (Saxon-JS HE target) on {jobs} parallel job(s)…")

    compiled = cached = 0
    failures: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(compile_one, x): x for x in xslts}
        for fut in as_completed(futures):
            x = futures[fut]
            rel = x.relative_to(PHIVE_DIR)
            status, detail = fut.result()
            done += 1
            if status == "failed":
                failures.append(str(rel))
                print(f"    [{done}/{n}] ✗ {rel}")
                if detail:
                    print(f"            {detail}")
            elif status == "compiled":
                compiled += 1
                print(f"    [{done}/{n}] ✓ {rel}")
            else:  # cached — keep the log quiet, just count
                cached += 1

    ok = compiled + cached
    if failures:
        # Some upstream rulesets use XPath/patterns the older Java Saxon
        # tolerated but Saxon-JS rejects (XPST0008/XPST0017, …). These are
        # data-level, not toolchain, errors: report them, drop them, and
        # keep going — build_manifest.py excludes any ruleset with no SEF,
        # so a failed format simply doesn't appear in the drop-down.
        log(f"⚠ {len(failures)} ruleset(s) failed to compile — excluded from the build:")
        for f in failures[:25]:
            log(f"    {f}")
        if len(failures) > 25:
            log(f"    …and {len(failures) - 25} more")
    log(f"✓ {ok}/{n} ready ({compiled} compiled, {cached} cached, {len(failures)} failed)")
    # Non-fatal: only hard-fail if NOTHING compiled (a broken toolchain).
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
