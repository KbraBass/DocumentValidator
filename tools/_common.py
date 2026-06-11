"""Shared helpers for the phive-web orchestrator tools.

Keeps path constants, the config loaders, a subprocess runner and a
numeric-aware version sort in one place so every tool agrees on where
things live.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# ── Layout ──────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
SOURCES = CONFIG / "sources.yaml"
FORMATS = CONFIG / "formats.yaml"

WORK = ROOT / "work"
PHIVE_DIR = WORK / "phive-rules"          # sparse clone (gitignored)
DEPS_DIR = WORK / "deps"                  # Maven JARs + extracted XSDs (gitignored)
DEPS_XSD = DEPS_DIR / "schemas"           # external/schemas/** extracted here

SITE = ROOT / "site"
ASSETS = SITE / "validation-assets"       # the committed product
ASSETS_SCH = ASSETS / "schematron"
ASSETS_XSD = ASSETS / "schemas"
MANIFEST = ASSETS / "manifest.json"

# Node SEF compiler, fetched by fetch_vendor.py into the gitignored work/.
VENDOR_XSLT3 = WORK / "vendor" / "node_modules" / "xslt3" / "xslt3.js"

# Where compiled XSLT live inside the clone, per module.
SCHEMATRON_REL = "src/main/resources/external/schematron"


def log(msg: str) -> None:
    print(f"  {msg}")


def run(cmd, cwd=None, check=True, capture=True) -> subprocess.CompletedProcess:
    """Thin subprocess.run wrapper with sane defaults."""
    return subprocess.run(
        [str(c) for c in cmd], cwd=str(cwd) if cwd else None,
        check=check, text=True,
        capture_output=capture,
    )


def module_dir(module: str) -> Path:
    return PHIVE_DIR / f"phive-rules-{module}" / SCHEMATRON_REL


def selected_modules(cfg: dict) -> list[str]:
    """Module list to ingest: every phive-rules-* if `all`, else core_set."""
    if cfg.get("all"):
        # Discover from the clone if present; fall back to core_set.
        found = sorted(
            p.name[len("phive-rules-"):]
            for p in PHIVE_DIR.glob("phive-rules-*")
            if p.is_dir() and p.name != "phive-rules-api"
        ) if PHIVE_DIR.exists() else []
        return found or list(cfg.get("core_set", []))
    return list(cfg.get("core_set", []))


# ── Version handling ────────────────────────────────────────────────
_VER_PART = re.compile(r"(\d+|[a-zA-Z]+)")


def version_key(v: str):
    """Numeric-aware sort key. Handles 1.3.16 > 1.3.6, 1.3.6a > 1.3.6,
    and calendar-ish 2026.5 > 2024.11 correctly (segment-wise)."""
    key = []
    for seg in v.split("."):
        for tok in _VER_PART.findall(seg):
            key.append((0, int(tok)) if tok.isdigit() else (1, tok))
    return key


def latest_version(versions) -> str | None:
    versions = list(versions)
    return max(versions, key=version_key) if versions else None


# A version token: two-or-more dot-separated numbers, optional trailing
# letter (+digits) — 1.3.16, 1.0.3.11, 2.0, 2026.5, 1.3.6a, 3.0.2. Matched
# as a SUBSTRING because phive-rules carries versions both as path segments
# (`1.3.16/ubl/…`, `openpeppol/2026.5/…`) AND inside file names
# (`nlcius-cii-1.0.3.11.xslt`, `si-ubl-1.2.3.xslt`). Bare single integers
# are intentionally NOT versions (too many false hits, e.g. `…-2`).
VERSION_RX = re.compile(r"\d+(?:\.\d+)+[a-zA-Z]?\d*")


def templatize(rel_posix: str):
    """Split a ruleset path (relative to a module's schematron dir) into a
    version-independent template and the concrete version token(s) it
    carried. Every version token (segment or in-filename) becomes `{v}`, so
    all versions of one ruleset share a template and auto-discovery can keep
    only the latest. Returns (template, [versions]) — the list preserves
    order (e.g. `si-ubl-2.0-ext-gaccount-1.0` → ['2.0', '1.0'])."""
    versions: list[str] = []

    def _sub(m):
        versions.append(m.group(0))
        return "{v}"

    template = VERSION_RX.sub(_sub, rel_posix)
    return template, versions
