#!/usr/bin/env python3
"""Step 4 of the orchestrator — assemble the committed validation assets.

Catalog source = the parsed phive-rules VES registry (tools/parse_ves_registry).
For every VES it emits a format with helger's display name, version and
deprecation flag, copies the (gzipped) SEF for each Schematron ruleset it
binds, and stages the XSD closure for its document type (UBL Invoice /
Credit Note, or CII). This makes the drop-down mirror the helger validation
catalog and stay in sync when phive-rules changes upstream.

Auto-detect (best-effort, broad): for UBL/CII formats the target
Customization ID(s) are extracted from each ruleset's compiled XSLT, so a
dropped document is matched to the latest non-deprecated format carrying
that Customization ID. Compiled rulesets that no VES references fall back to
generic filesystem-discovered formats (Schematron-only, manual select).

Only the copied artefacts are committed; the clone + JARs under work/ stay
gitignored. SEFs are gzipped (~10×); the browser gunzips via
DecompressionStream.

Usage:
    uv run --with pyyaml python tools/build_manifest.py
"""
from __future__ import annotations

import fnmatch
import gzip
import json
import re
import shutil
import sys
from datetime import datetime, timezone

import yaml

from _common import (ASSETS, ASSETS_SCH, ASSETS_XSD, DEPS_XSD, MANIFEST,
                     PHIVE_DIR, FORMATS, SOURCES, latest_version, log,
                     module_dir, run, templatize, version_key)
from parse_ves_registry import parse_all

SCHEMA_LOC = re.compile(r'schemaLocation\s*=\s*"([^"]+)"')
# Extended EN 16931 Customization IDs (the discriminating ones) embedded in a
# compiled ruleset, e.g. urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:…
RE_CUSTOM = re.compile(
    r'urn:cen\.eu:en16931:2017(?:#(?:compliant|conformant)#[A-Za-z0-9:._\-]+)+')

# Document root local-name → the XSD we can validate it against.
XSD_BY_ROOT = {
    "Invoice":    ("ph-ubl21", "maindoc/UBL-Invoice-2.1.xsd"),
    "CreditNote": ("ph-ubl21", "maindoc/UBL-CreditNote-2.1.xsd"),
    "CrossIndustryInvoice": ("ph-cii-d16b",
                             "d16b/data/standard/CrossIndustryInvoice_100pD16B.xsd"),
}

_sef_cache: dict = {}            # xslt path → manifest rel (copied once)
_custom_cache: dict = {}        # xslt path → frozenset of customization ids
_module_of_re = "phive-rules-"


def _module_of(p) -> str:
    return next((s[len(_module_of_re):] for s in p.parts
                 if s.startswith(_module_of_re)), "?")


def copy_sef(xslt_path):
    """Gzip the SEF beside `xslt_path` to schematron/<module>/<rel>.sef.json.gz
    (deduped across the many VES that share a ruleset). Returns the manifest
    path, or None if not compiled."""
    key = xslt_path.resolve()
    if key in _sef_cache:
        return _sef_cache[key]
    sef = xslt_path.with_suffix(".sef.json")
    if not sef.exists():
        return None
    module = _module_of(xslt_path)
    rel = xslt_path.relative_to(module_dir(module)).as_posix()[:-len(".xslt")]
    dst = ASSETS_SCH / module / (rel + ".sef.json.gz")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(sef, "rb") as fi, gzip.open(dst, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo)
    out = dst.relative_to(ASSETS).as_posix()
    _sef_cache[key] = out
    return out


def customizations(xslt_path) -> frozenset:
    key = xslt_path.resolve()
    if key not in _custom_cache:
        try:
            txt = xslt_path.read_text(encoding="utf-8", errors="replace")
            _custom_cache[key] = frozenset(RE_CUSTOM.findall(txt))
        except Exception:  # noqa: BLE001
            _custom_cache[key] = frozenset()
    return _custom_cache[key]


def xsd_closure(entry_file):
    seen, stack = set(), [entry_file.resolve()]
    while stack:
        f = stack.pop()
        if f in seen or not f.exists():
            continue
        seen.add(f)
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        for ref in SCHEMA_LOC.findall(text):
            if ref.startswith(("http://", "https://", "urn:")):
                continue
            stack.append((f.parent / ref).resolve())
    return seen


_xsd_done: set = set()


def stage_xsd(dep: str, entry_rel: str):
    dep_root = (DEPS_XSD / dep).resolve()
    entry = dep_root / entry_rel
    if not entry.exists():
        return None
    if (dep, entry_rel) not in _xsd_done:        # copy each entry's closure once
        for f in xsd_closure(entry):             # (Invoice vs CreditNote differ;
            try:                                 #  shared commons just overwrite)
                rel = f.relative_to(dep_root)
            except ValueError:
                continue
            dst = ASSETS_XSD / dep / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dst)
        _xsd_done.add((dep, entry_rel))
    return f"schemas/{dep}/{entry_rel}"


# ── resolve_source: kept for tools/validate.py (curated {v} globs) ─────
def resolve_source(module: str, path_pat: str):
    base = module_dir(module)
    rx = re.compile("^" + re.escape(path_pat).replace(r"\{v\}", r"([^/]+)") + "$")
    found = {}
    for f in base.glob(path_pat.replace("{v}", "*")):
        m = rx.match(f.relative_to(base).as_posix())
        if m:
            found[m.group(1)] = f
    if not found:
        return None
    latest = latest_version(found.keys())
    return latest, sorted(found.keys(), key=version_key), found[latest]


def discover_leftovers(used_xslt: set, ignore_globs) -> list[dict]:
    """Generic formats for compiled rulesets that no VES references (modules
    we couldn't parse, or odd layouts) — grouped by (module, templated path),
    latest version, Schematron-only, manual select."""
    groups: dict = {}
    for xslt in PHIVE_DIR.glob(
            "phive-rules-*/src/main/resources/external/schematron/**/*.xslt"):
        if xslt.resolve() in used_xslt or not xslt.with_suffix(".sef.json").exists():
            continue
        module = _module_of(xslt)
        rel = xslt.relative_to(module_dir(module)).as_posix()
        if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(f"{module}/{rel}", g)
               for g in ignore_globs):
            continue
        template, versions = templatize(rel)
        groups.setdefault((module, template), {})[tuple(versions)] = (xslt, rel)

    out = []
    for (module, _t), variants in sorted(groups.items()):
        vt = max(variants, key=lambda t: [version_key(v) for v in t])
        xslt, rel = variants[vt]
        sef = copy_sef(xslt)
        if not sef:
            continue
        out.append({
            "key": f"file:{module}:{rel}", "label": rel[:-len(".xslt")],
            "version": "/".join(dict.fromkeys(vt)), "deprecated": False,
            "group": module, "root": None, "schematron": [sef], "xsd": None,
            "autodetect": False,
        })
    return out


def main() -> int:
    src_cfg = yaml.safe_load(SOURCES.read_text())
    fmt_cfg = yaml.safe_load(FORMATS.read_text()) if FORMATS.exists() else {}
    ignore_globs = (fmt_cfg.get("discovery") or {}).get("ignore", []) or []

    for d in (ASSETS_SCH, ASSETS_XSD):
        if d.exists():
            shutil.rmtree(d)
    ASSETS.mkdir(parents=True, exist_ok=True)

    log("parsing VES registry…")
    ves = parse_all()
    log(f"  {len(ves)} VES parsed")

    formats, used_xslt = [], set()
    src_by_key: dict = {}                        # format key → [source xslt paths]
    for v in ves:
        sefs = []
        for xp in v["schematron"]:
            rel = copy_sef(xp)
            if rel:
                sefs.append(rel)
                used_xslt.add(xp.resolve())
        root = v["root"]
        xsd = None
        if root and root["local_name"] in XSD_BY_ROOT:
            xsd = stage_xsd(*XSD_BY_ROOT[root["local_name"]])
        if not sefs:
            # Keep genuine XSD-only formats (no .addSchematron in the VES —
            # e.g. FatturaPA, pure UBL/CII). Skip ones that INTEND schematron
            # but produced none (unlinked path or failed SEF compile).
            if v["has_sch_call"] or not xsd:
                continue
        formats.append({
            "key": v["vesid"], "label": v["name"], "version": v["version"],
            "deprecated": v["deprecated"], "group": v["module"],
            "root": root, "schematron": sefs, "xsd": xsd, "autodetect": False,
        })
        src_by_key[v["vesid"]] = [p for p in v["schematron"] if p.resolve() in used_xslt]

    # ── Auto-detect (best-effort) ──────────────────────────────────────
    # Exact rules from extended Customization IDs found in each format's
    # rulesets. On collision prefer non-deprecated, the most specific ruleset
    # (fewest customizations), then the latest version.
    def _score(fmt, c):
        # A document with Customization ID `c` should resolve to the format
        # that OWNS it: prefer one whose VESID shares a distinctive token with
        # the id (e.g. "peppol", "xrechnung", "nlcius"), then non-deprecated,
        # then the latest version. Avoids flagship ids leaking to a CIUS that
        # merely embeds them (e.g. ublbe also references Peppol billing).
        toks = [t for t in re.split(r"[^a-z0-9]+", c.lower())
                if len(t) >= 4 and t not in ("urn", "2017", "en16931",
                                             "compliant", "conformant", "poacc")]
        owns = any(t in fmt["key"].lower() for t in toks)
        return (owns, not fmt["deprecated"], version_key(fmt["version"]))

    by_custom: dict = {}                          # (root_local, custom) → (score, fmt)
    for fmt in formats:
        if not fmt["root"]:
            continue
        customs = set()
        for xp in src_by_key.get(fmt["key"], []):
            customs |= customizations(xp)
        rl = fmt["root"]["local_name"]
        for c in customs:
            sc = _score(fmt, c)
            if (rl, c) not in by_custom or sc > by_custom[(rl, c)][0]:
                by_custom[(rl, c)] = (sc, fmt)

    detection = []
    detected_keys = set()
    for (rl, c), (_sc, fmt) in by_custom.items():
        detection.append({
            "key": fmt["key"], "priority": 50, "root": fmt["root"],
            "customization_exact": [c],
        })
        detected_keys.add(fmt["key"])
    # Generic EN 16931 fallback: latest non-deprecated en16931 format per root.
    for rl in ("Invoice", "CreditNote", "CrossIndustryInvoice"):
        cands = [f for f in formats if f.get("root") and f["root"]["local_name"] == rl
                 and f["group"] == "en16931" and not f["deprecated"] and f["schematron"]]
        if cands:
            best = max(cands, key=lambda f: version_key(f["version"]))
            detection.append({
                "key": best["key"], "priority": 10, "root": best["root"],
                "customization_prefix": ["urn:cen.eu:en16931:2017"],
            })
            detected_keys.add(best["key"])
    detection.sort(key=lambda d: -d["priority"])
    for fmt in formats:
        fmt["autodetect"] = fmt["key"] in detected_keys

    # ── Filesystem fallback for unreferenced rulesets ──────────────────
    leftovers = discover_leftovers(used_xslt, ignore_globs)
    formats += leftovers

    # Order: group by module (registry groups first, then file: leftovers),
    # within a group non-deprecated first then by label.
    formats.sort(key=lambda f: (f["group"], f["deprecated"], f["label"]))
    groups = []
    for f in formats:
        if f["group"] not in groups:
            groups.append(f["group"])

    def _git_out(*args):
        return run(["git", "-C", str(PHIVE_DIR), *args], check=False).stdout.strip()
    commit = _git_out("rev-parse", "--short", "HEAD")
    describe = _git_out("describe", "--tags", "--always")

    manifest = {
        "source": {
            "repo": src_cfg["repo"],
            "ref": src_cfg["tag"],            # configured: a tag, branch, or latest
            "tag": describe or src_cfg["tag"],  # resolved (tag, or tag-N-gSHA on a branch)
            "commit": commit,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scope": "all" if src_cfg.get("all") else "core_set",
        },
        "groups": groups,
        "formats": formats,
        "detection": detection,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    n_dep = sum(1 for f in formats if f["deprecated"])
    n_auto = sum(1 for f in formats if f["autodetect"])
    log(f"✓ manifest.json — {len(formats)} formats "
        f"({len(ves)} VES + {len(leftovers)} filesystem), {n_dep} deprecated, "
        f"{n_auto} auto-detectable, {len(detection)} detect rules")
    return 0 if formats else 1


if __name__ == "__main__":
    sys.exit(main())
