#!/usr/bin/env python3
"""Step 2 of the orchestrator — fetch the base XSD dependencies.

phive-rules modules do not vendor the UBL / CII XSDs; they declare them
as Maven dependencies (e.g. `com.helger.ubl:ph-ubl21`,
`com.helger.cii:ph-cii-d16b`) whose versions are pinned by the cloned
parent POM's <properties> (`${ph-ubl.version}` = 10.2.0, …).

This tool resolves each version from the parent POM, then obtains the
artefact JAR from the Maven repository — via `mvn dependency:copy` when a
Maven CLI is on PATH, otherwise by downloading directly from Maven
Central (`repo1.maven.org`, the canonical Maven repository). Both land
the same JAR in the gitignored work/deps/. It then extracts
`external/schemas/**` from each JAR into work/deps/schemas/<name>/ for
build_manifest.py to copy the needed closure into the committed site.

Network requirement: access to Maven Central (and optionally a Maven CLI).

Usage:
    uv run --with pyyaml python tools/fetch_deps.py
"""
from __future__ import annotations

import re
import shutil
import sys
import urllib.request
import zipfile

import yaml

from _common import DEPS_DIR, DEPS_XSD, PHIVE_DIR, SOURCES, log, run

CENTRAL = "https://repo1.maven.org/maven2"
PARENT_POM = PHIVE_DIR / "pom.xml"


def _resolve_property(name: str) -> str:
    """Read <name>value</name> from the cloned parent POM <properties>."""
    if not PARENT_POM.exists():
        raise SystemExit(f"parent POM missing — run fetch_phive.py first: {PARENT_POM}")
    text = PARENT_POM.read_text(encoding="utf-8")
    m = re.search(rf"<{re.escape(name)}>([^<]+)</{re.escape(name)}>", text)
    if not m:
        raise SystemExit(f"property <{name}> not found in {PARENT_POM}")
    return m.group(1).strip()


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _download_central(group: str, artifact: str, version: str, dest) -> bool:
    gp = group.replace(".", "/")
    url = f"{CENTRAL}/{gp}/{artifact}/{version}/{artifact}-{version}.jar"
    log(f"GET {url}")
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as fh:
            shutil.copyfileobj(r, fh)
        return True
    except Exception as e:  # noqa: BLE001 — report and continue
        log(f"  download failed: {e}")
        return False


def _mvn_copy(group: str, artifact: str, version: str, dest_dir) -> bool:
    """Use a Maven CLI if available (honours the user's settings/mirrors)."""
    coord = f"{group}:{artifact}:{version}:jar"
    p = run([
        "mvn", "-q",
        "org.apache.maven.plugins:maven-dependency-plugin:3.6.1:copy",
        f"-Dartifact={coord}",
        f"-DoutputDirectory={dest_dir}",
        "-Dmdep.stripVersion=false",
    ], check=False)
    return p.returncode == 0


def _extract_prefixed(archive, out_dir, prefix: str, only_xsd: bool = False) -> int:
    """Extract every entry under `prefix` (stripped) into out_dir."""
    n = 0
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            name = info.filename
            if name.endswith("/") or prefix not in name:
                continue
            if only_xsd and not name.lower().endswith(".xsd"):
                continue
            rel = name.split(prefix, 1)[1]
            target = out_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            n += 1
    return n


def _fetch_maven(deps) -> int:
    use_mvn = _have("mvn")
    log(f"Maven CLI {'found' if use_mvn else 'not found'} — "
        f"{'using mvn' if use_mvn else 'downloading from Maven Central directly'}")
    ok = 0
    for d in deps:
        name, group, artifact = d["name"], d["group"], d["artifact"]
        version = d.get("version") or _resolve_property(d["version_property"])
        jar = DEPS_DIR / f"{artifact}-{version}.jar"
        log(f"• {name}: {group}:{artifact}:{version}")
        if not jar.exists():
            got = (_mvn_copy(group, artifact, version, DEPS_DIR) and jar.exists()) \
                if use_mvn else False
            if not got:
                got = _download_central(group, artifact, version, jar)
            if not got:
                log(f"  ✗ could not obtain {artifact}-{version}.jar")
                continue
        else:
            log("  (jar cached)")
        out = DEPS_XSD / name
        if out.exists():
            shutil.rmtree(out)
        count = _extract_prefixed(jar, out, "external/schemas/")
        log(f"  extracted {count} XSD → {out.relative_to(DEPS_DIR.parent)}")
        ok += 1
    return ok


def _fetch_zips(deps) -> int:
    """Authoritative schema distributions fetched as a zip (e.g. the OASIS
    UBL 2.1 set), with `subdir` extracted and its prefix stripped so
    `entry` in formats.yaml is relative to it."""
    ok = 0
    for d in deps:
        name, url, subdir = d["name"], d["url"], d.get("subdir", "")
        zpath = DEPS_DIR / (url.rsplit("/", 1)[-1])
        log(f"• {name}: {url}")
        if not zpath.exists():
            try:
                with urllib.request.urlopen(url, timeout=300) as r, open(zpath, "wb") as fh:
                    shutil.copyfileobj(r, fh)
            except Exception as e:  # noqa: BLE001
                log(f"  ✗ download failed: {e}")
                continue
        else:
            log("  (zip cached)")
        out = DEPS_XSD / name
        if out.exists():
            shutil.rmtree(out)
        count = _extract_prefixed(zpath, out, subdir, only_xsd=True)
        log(f"  extracted {count} XSD → {out.relative_to(DEPS_DIR.parent)}")
        ok += 1
    return ok


def main() -> int:
    cfg = yaml.safe_load(SOURCES.read_text())
    maven = cfg.get("maven_xsd_deps", [])
    zips = cfg.get("zip_xsd_deps", [])
    total = len(maven) + len(zips)
    if not total:
        log("no XSD deps configured — nothing to fetch")
        return 0

    DEPS_DIR.mkdir(parents=True, exist_ok=True)
    ok = 0
    if maven:
        ok += _fetch_maven(maven)
    if zips:
        ok += _fetch_zips(zips)

    log(f"✓ {ok}/{total} XSD dependencies ready")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
