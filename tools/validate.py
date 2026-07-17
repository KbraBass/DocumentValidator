#!/usr/bin/env python3
"""CLI validator — the headless mirror of the in-browser engine.

Runs the SAME two layers as site/index.html on one or more XML files,
but in a long-lived Python process (lxml for XSD, saxonche/SaxonC HE for
Schematron) — for bulk runs that would exhaust browser memory.

Auto-detect (default) mirrors the page: by root element, then — for
UBL/CII — by Customization ID, resolving to the latest spec. Override
with `--format <key>` (a manifest format key).

XSD comes from the committed site/validation-assets/schemas/; Schematron
runs the source `.xslt` from the clone under work/ (saxonche executes
XSLT directly — no SEF needed off the browser).

Usage:
    uv run --with lxml --with saxonche --with pyyaml \
        python tools/validate.py path/to/doc.xml [more.xml …] [--format en16931-cii]
"""
from __future__ import annotations

import argparse
import json
import sys

import yaml
from lxml import etree

from _common import ASSETS_XSD, FORMATS, MANIFEST, log, module_dir

# build_manifest's resolver maps a format's schematron sources → source .xslt.
from build_manifest import resolve_source

NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
NS_RSM = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
NS_RAM = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
SVRL = "http://purl.oclc.org/dsdl/svrl"


def inspect(path):
    doc = etree.parse(str(path))
    root = doc.getroot()
    ns = etree.QName(root).namespace or ""
    local = etree.QName(root).localname
    cust = ""
    if ns == NS_RSM:  # CII
        el = root.find(f"{{{NS_RSM}}}ExchangedDocumentContext/"
                       f"{{{NS_RAM}}}GuidelineSpecifiedDocumentContextParameter/"
                       f"{{{NS_RAM}}}ID")
        cust = (el.text or "").strip() if el is not None else ""
    else:  # UBL
        el = root.find(f"{{{NS_CBC}}}CustomizationID")
        cust = (el.text or "").strip() if el is not None else ""
    return doc, f"{{{ns}}}{local}" if ns else local, ns, local, cust


def detect(manifest, ns, local, cust):
    for rule in manifest["detection"]:
        r = rule["root"]
        if r["namespace"] != ns or r["local_name"] != local:
            continue
        if any(s in cust for s in rule.get("customization_contains", [])):
            return rule["key"]
        if cust in rule.get("customization_exact", []):
            return rule["key"]
        if any(cust.startswith(p) for p in rule.get("customization_prefix", [])):
            return rule["key"]
        if not any(k in rule for k in
                   ("customization_contains", "customization_exact",
                    "customization_prefix")):
            return rule["key"]  # root-only rule
    return None


def fmt_by_key(manifest, key):
    return next((f for f in manifest["formats"] if f["key"] == key), None)


def validate_xsd(doc, fmt):
    if not fmt.get("xsd"):
        return None  # no XSD layer for this format
    xsd_path = ASSETS_XSD.parent / fmt["xsd"]  # validation-assets/<xsd>
    schema = etree.XMLSchema(etree.parse(str(xsd_path)))
    if schema.validate(doc):
        return []
    return [str(e) for e in schema.error_log]


def _xslt_sources(fmt, formats_cfg):
    """Source .xslt paths in the clone for a format's rulesets. Curated
    formats resolve via their formats.yaml family; auto-discovered formats
    map their manifest SEF path (schematron/<module>/<rel>.sef.json) back to
    the clone. saxonche runs the XSLT directly (no SEF off the browser)."""
    fam = next((f for f in formats_cfg["families"] if f["key"] == fmt["key"]), None)
    if fam:
        for s in fam["schematron"]:
            res = resolve_source(s["module"], s["path"])
            if res:
                yield res[2]
        return
    for rel in fmt.get("schematron", []):
        parts = rel.split("/")                       # schematron/<module>/<rel…>.sef.json.gz
        if len(parts) < 3 or parts[0] != "schematron":
            continue
        sub = "/".join(parts[2:])
        for suffix in (".sef.json.gz", ".sef.json"):
            if sub.endswith(suffix):
                sub = sub[:-len(suffix)] + ".xslt"
                break
        xslt = module_dir(parts[1]) / sub
        if xslt.exists():
            yield xslt


def validate_schematron(doc, fmt, formats_cfg):
    from saxonche import PySaxonProcessor
    failures = []
    with PySaxonProcessor(license=False) as proc:
        xsltproc = proc.new_xslt30_processor()
        src_xml = proc.parse_xml(xml_text=etree.tostring(doc, encoding="unicode"))
        for xslt in _xslt_sources(fmt, formats_cfg):
            exe = xsltproc.compile_stylesheet(stylesheet_file=str(xslt))
            svrl = exe.transform_to_string(xdm_node=src_xml)
            sdoc = etree.fromstring(svrl.encode("utf-8"))
            for fa in sdoc.iter(f"{{{SVRL}}}failed-assert"):
                text_el = fa.find(f"{{{SVRL}}}text")
                flag = (fa.get("flag") or fa.get("role") or "").strip().lower()
                severity = "warning" if flag in ("warning", "warn", "info", "information") else "fatal"
                failures.append({
                    "id": fa.get("id"),
                    "location": fa.get("location"),
                    "text": (text_el.text or "").strip() if text_el is not None else "",
                    "severity": severity,
                })
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--format", help="force a manifest format key")
    args = ap.parse_args()

    if not MANIFEST.exists():
        log("no manifest — run tools/update.sh first")
        return 1
    manifest = json.loads(MANIFEST.read_text())
    formats_cfg = yaml.safe_load(FORMATS.read_text())

    rc = 0
    for path in args.files:
        try:
            doc, qname, ns, local, cust = inspect(path)
        except Exception as e:  # noqa: BLE001
            print(f"✗ {path}: XML parse error — {e}")
            rc = 1
            continue
        key = args.format or detect(manifest, ns, local, cust)
        fmt = fmt_by_key(manifest, key) if key else None
        if not fmt:
            print(f"? {path}: no format (root={qname} cust={cust!r})")
            rc = 1
            continue
        print(f"\n{path}\n  detected: {fmt['key']}  (root={local}, cust={cust!r})")

        xsd_errs = validate_xsd(doc, fmt)
        if xsd_errs is None:
            print("  XSD:        (none for this format)")
        elif xsd_errs:
            print(f"  XSD:        FAIL — {len(xsd_errs)} error(s)")
            for e in xsd_errs[:10]:
                print(f"              {e}")
        else:
            print("  XSD:        pass")

        sch = validate_schematron(doc, fmt, formats_cfg)
        sch_errs = [f for f in sch if f["severity"] != "warning"]
        sch_warns = [f for f in sch if f["severity"] == "warning"]
        if sch_errs:
            print(f"  Schematron: FAIL — {len(sch_errs)} error(s)"
                  + (f", {len(sch_warns)} warning(s)" if sch_warns else ""))
        elif sch_warns:
            print(f"  Schematron: pass with {len(sch_warns)} warning(s)")
        else:
            print("  Schematron: pass")
        for f in sch_errs[:20]:
            print(f"              [error]   [{f['id']}] {f['text']}")
        for f in sch_warns[:20]:
            print(f"              [warning] [{f['id']}] {f['text']}")

        # Warnings do not reject the document — only XSD or fatal Schematron
        # errors set a non-zero exit code.
        if xsd_errs or sch_errs:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
