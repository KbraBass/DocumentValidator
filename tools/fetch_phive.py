#!/usr/bin/env python3
"""Step 1 of the orchestrator — fetch phive-rules.

Shallow + sparse clones `phax/phive-rules` at the ref configured in
`config/sources.yaml` (`tag:` — a fixed tag, a branch like `master`, or
`latest`/`latest-release`) into the gitignored `work/phive-rules/`,
checking out only `src/main/{resources,java}` of the selected modules
(the whole corpus if `all: true`). Nothing here is committed — only the
SEFs and XSDs derived from it (under site/validation-assets/) are.

The ref is re-fetched on every run and HEAD moved to its latest commit, so
tracking a branch picks up new modules/versions as Helger pushes them (a
tag stays put). Network requirement: git + access to github.com.

Usage:
    uv run --with pyyaml python tools/fetch_phive.py
"""
from __future__ import annotations

import subprocess
import sys

import yaml

import json
import urllib.request

from _common import PHIVE_DIR, SOURCES, WORK, log, module_dir, run


def _git(*args: str, cwd=PHIVE_DIR, check=True) -> subprocess.CompletedProcess:
    return run(["git", *args], cwd=cwd, check=check)


def _resolve_ref(repo: str, ref: str) -> str:
    """Map a configured ref to a concrete git ref. `latest`/`latest-release`
    resolve to the newest GitHub release tag; anything else (a tag like
    `phive-rules-parent-pom-4.3.7` or a branch like `master`) is used as-is."""
    if ref not in ("latest", "latest-release"):
        return ref
    # github.com/<owner>/<name>(.git) → owner/name
    slug = repo.split("github.com/", 1)[-1].removesuffix(".git").strip("/")
    api = f"https://api.github.com/repos/{slug}/releases/latest"
    log(f"resolving {ref!r} via {api}…")
    with urllib.request.urlopen(api, timeout=60) as r:
        tag = json.load(r)["tag_name"]
    log(f"  → {tag}")
    return tag


def _clone(repo: str, ref: str) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    log(f"cloning {repo} @ {ref} (shallow, sparse, blobless)…")
    run([
        "git", "clone", "--depth", "1", "--filter=blob:none",
        "--sparse", "--branch", ref, repo, str(PHIVE_DIR),
    ], cwd=WORK)


def _head_commit() -> str:
    return _git("rev-parse", "--short", "HEAD", check=False).stdout.strip()


def _all_modules() -> list[str]:
    """Every phive-rules-<name> module at the checked-out tag, via
    `git ls-tree` (works on a blobless/sparse clone where the module
    directories aren't materialised on disk yet). `api` is the shared
    library, not a ruleset module — excluded."""
    p = _git("ls-tree", "--name-only", "HEAD")
    mods = [ln[len("phive-rules-"):] for ln in p.stdout.splitlines()
            if ln.startswith("phive-rules-")]
    return [m for m in sorted(mods) if m != "api"]


def main() -> int:
    cfg = yaml.safe_load(SOURCES.read_text())
    repo = cfg["repo"]
    ref = _resolve_ref(repo, cfg["tag"])

    if PHIVE_DIR.exists() and (PHIVE_DIR / ".git").exists():
        # Re-fetch the ref every run and move HEAD to its latest commit, so a
        # branch (e.g. `master`) picks up new modules/versions Helger pushes,
        # and a tag is a cheap no-op. FETCH_HEAD works for both tags + branches.
        before = _head_commit()
        log(f"fetching {ref!r}…")
        _git("fetch", "--depth", "1", "origin", ref)
        _git("checkout", "-f", "FETCH_HEAD")
        after = _head_commit()
        log(f"clone at {ref} {after}" + (f" (was {before})" if after != before else " (unchanged)"))
    else:
        if PHIVE_DIR.exists():
            run(["rm", "-rf", str(PHIVE_DIR)])
        _clone(repo, ref)
        log(f"clone at {ref} {_head_commit()}")

    # Module list — computed AFTER the clone so `all: true` can enumerate
    # every module from git (a fresh sparse clone has no module dirs on
    # disk yet, so a filesystem glob would find nothing).
    modules = _all_modules() if cfg.get("all") else list(cfg.get("core_set", []))

    # Sparse set: each module's resources (the compiled XSLT/XSD) AND its
    # src/main/java (the *Validation.java VES registry — parsed for display
    # names, versions, deprecation, and the schematron/XSD each format binds).
    # (Cone mode keeps repo-root files like the parent pom.xml available for
    # fetch_deps.py.)
    paths = []
    for m in modules:
        paths.append(f"phive-rules-{m}/src/main/resources")
        paths.append(f"phive-rules-{m}/src/main/java")
    log(f"sparse-checkout {len(modules)} module(s): {', '.join(modules)}")
    _git("sparse-checkout", "set", *paths)

    # Sanity: report how many compiled XSLT are now visible.
    xslt = list(PHIVE_DIR.glob(
        "phive-rules-*/src/main/resources/external/schematron/**/*.xslt"))
    log(f"✓ {len(xslt)} Schematron XSLT staged across {len(modules)} module(s)")
    missing = [m for m in modules if not module_dir(m).exists()]
    if missing:
        log(f"WARNING: no resources found for module(s): {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
