#!/usr/bin/env python3
"""Parse the phive-rules VES registry from the `*Validation*.java` classes.

phive-rules registers every Validation Executor Set (VES) in Java — the
same data the helger validation page renders. Each registration block
gives the canonical display name, the VESID (group:doctype:version), the
deprecation flag, the Schematron ruleset(s) it binds, and the XSD/doctype.
Parsing it (rather than the filesystem) is what lets our drop-down match
helger's names + versions + deprecation, and stay in sync when phive-rules
changes upstream.

Best-effort parser of the dominant builder pattern:

    @Deprecated                                            // ← deprecated VID
    public static final DVRCoordinate VID_X =
        PhiveRulesHelper.createCoordinate (GROUP_ID, "<doctype>", "<version>");
    ...
    final IReadableResource R = new ClassPathResource (sPrefix + "<file>.xslt", …);
    VesXmlBuilder.builder ()
                 .vesID (VID_X)
                 .displayName ("<name>")            // or .displayNamePrefix(…)
                 .deprecated ()                     // ← or deprecated on the VID
                 .addXSD (UBL21Marshaller.getAllInvoiceXSDs ())   // ← doctype/XSD
                 .addSchematron (… (R) …)           // ← ruleset(s)
                 .registerInto (aRegistry);

VES whose pattern we can't parse are skipped; their rulesets still surface
via build_manifest's filesystem fallback.

Usage (debug dump):
    uv run python tools/parse_ves_registry.py [module …]
"""
from __future__ import annotations

import re
import sys

from _common import PHIVE_DIR, module_dir

SCHEMATRON_MARKER = "external/schematron/"

_UBL = "urn:oasis:names:specification:ubl:schema:xsd:"
DOCTYPE_ROOT = {
    "Invoice":             (f"{_UBL}Invoice-2", "Invoice"),
    "CreditNote":          (f"{_UBL}CreditNote-2", "CreditNote"),
    "ApplicationResponse": (f"{_UBL}ApplicationResponse-2", "ApplicationResponse"),
    "Order":               (f"{_UBL}Order-2", "Order"),
    "OrderResponse":       (f"{_UBL}OrderResponse-2", "OrderResponse"),
    "OrderChange":         (f"{_UBL}OrderChange-2", "OrderChange"),
    "OrderCancellation":   (f"{_UBL}OrderCancellation-2", "OrderCancellation"),
    "Catalogue":           (f"{_UBL}Catalogue-2", "Catalogue"),
    "DespatchAdvice":      (f"{_UBL}DespatchAdvice-2", "DespatchAdvice"),
    "Reminder":            (f"{_UBL}Reminder-2", "Reminder"),
    "Statement":           (f"{_UBL}Statement-2", "Statement"),
}
CII_ROOT = ("urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
            "CrossIndustryInvoice")

RE_GROUP_ID = re.compile(r'String\s+GROUP_ID\s*=\s*"([^"]+)"')
RE_VID = re.compile(
    r'(?P<dep>@Deprecated\s+)?'
    r'public\s+static\s+final\s+DVRCoordinate\s+(?P<vid>VID_\w+)\s*=\s*'
    r'[\w.]*\.create(?:Derived)?Coordinate\s*\(\s*(?P<grp>[^,]+?)\s*,\s*'
    r'"(?P<doctype>[^"]+)"\s*,\s*(?P<version>"[^"]+"|[\w.]+)',
    re.DOTALL)
# Version embedded in a per-release class name, e.g. PeppolValidation2026_05.
RE_FILE_VERSION = re.compile(r'(\d{4})_(\d{1,2})')
# Capture the full RHS of any String constant (may be a concatenation of
# other String vars + literals, e.g. PREFIX_XSLT = PREFIX + "openpeppol/…").
RE_STRVAR = re.compile(r'(?:final\s+)?String\s+(\w+)\s*=\s*([^;]+);')
RE_CPR = re.compile(
    r'(?:IReadableResource|ClassPathResource)\s+(\w+)\s*=\s*'
    r'new\s+ClassPathResource\s*\(\s*(.+?)\)\s*;',
    re.DOTALL)
RE_VESID = re.compile(r'\.vesID\s*\(\s*(?:[\w.]*\.)?(VID_\w+)\s*\)')
# Capture the whole displayName / displayNamePrefix argument expression (may
# be a concat of literals + String vars, e.g. "OpenPeppol UBL Invoice" +
# sVersion) up to the next builder call; resolved via _resolve_expr.
RE_NAME = re.compile(r'\.displayName\s*\(\s*(.+?)\s*\)\s*(?=\.\w)', re.DOTALL)
RE_NAMEPREFIX = re.compile(r'\.displayNamePrefix\s*\(\s*(.+?)\s*\)\s*(?=\.\w)', re.DOTALL)
RE_DOCXSD = re.compile(r'getAll(\w+?)XSDs')
RE_STR = re.compile(r'"([^"]*)"')
# Tokenize a concat expr into string literals and bare identifiers.
RE_TOKEN = re.compile(r'"[^"]*"|[A-Za-z_]\w*')


def _resolve_expr(expr: str, strvars: dict) -> str:
    """Resolve a `<var> + "literal" + …` concatenation to a string. String
    literals are kept; identifiers known to be String constants (PATH_SI,
    sPrefix, PREFIX_XSLT, …) are substituted with their value; any other
    identifier (e.g. `_getCL`) is ignored."""
    parts = []
    for tok in RE_TOKEN.findall(expr):
        if tok.startswith('"'):
            parts.append(tok[1:-1])
        elif tok in strvars:
            parts.append(strvars[tok])
    return "".join(parts)


def _res_to_path(module: str, respath: str):
    i = respath.find(SCHEMATRON_MARKER)
    if i < 0:
        return None                       # different layout (external/sch, …)
    return module_dir(module) / respath[i + len(SCHEMATRON_MARKER):]


def _is_cii(span: str) -> bool:
    return any(t in span for t in
               ("CrossIndustryInvoice", "CIID16B", "CII_D16B", "createXSLT_CII"))


def parse_file(path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    module = next((p[len("phive-rules-"):] for p in path.parts
                   if p.startswith("phive-rules-")), "?")
    gm = RE_GROUP_ID.search(text)
    file_group = gm.group(1) if gm else module
    fvm = RE_FILE_VERSION.search(path.name)
    file_version = f"{fvm.group(1)}.{int(fvm.group(2))}" if fvm else None

    # Resolve String constants, iterating so concatenations that reference
    # earlier String vars settle (PREFIX_XSLT = PREFIX + "…").
    raw = dict(RE_STRVAR.findall(text))
    strvars: dict = {}
    # Per-release version constants are computed (…ARTEFACT_VERSION.getAsString()),
    # so they never resolve to a literal — recover them from the class name and
    # seed FIRST, so path prefixes that concatenate them (PREFIX_XSLT) resolve.
    if file_version:
        for var, rhs in raw.items():
            if "VERSION" in var.upper() or "getAsString" in rhs:
                strvars[var] = file_version
    for _ in range(6):
        changed = False
        for var, rhs in raw.items():
            val = _resolve_expr(rhs, strvars)
            if val and strvars.get(var) != val:
                strvars[var], changed = val, True
        if not changed:
            break

    cprs = {var: _resolve_expr(expr, strvars) for var, expr in RE_CPR.findall(text)}

    vids = {}
    for m in RE_VID.finditer(text):
        gexpr = m.group("grp")
        grp = RE_STR.findall(gexpr)[0] if '"' in gexpr else file_group
        vexpr = m.group("version")
        version = (RE_STR.findall(vexpr)[0] if '"' in vexpr
                   else strvars.get(vexpr.strip(), vexpr.strip()))
        if not version or not version[0].isdigit():
            continue              # unresolved version identifier — not a real VES
        vids[m.group("vid")] = (grp, m.group("doctype"), version, bool(m.group("dep")))

    out = []
    # One VES per `.registerInto`. Isolate just its builder chain (from the
    # last `.builder` in the chunk) so the file preamble — VID declarations,
    # imports — can't leak doctype/CII hints into it.
    for chunk in text.split(".registerInto"):
        if ".vesID" not in chunk:
            continue
        bi = chunk.rfind(".builder")
        span = chunk[bi:] if bi >= 0 else chunk
        mv = RE_VESID.search(span)
        if not mv or mv.group(1) not in vids:
            continue
        grp, doctype, version, vid_dep = vids[mv.group(1)]

        nm = RE_NAME.search(span)
        pm = RE_NAMEPREFIX.search(span)
        if nm:
            name = _resolve_expr(nm.group(1), strvars).strip()
        elif pm:
            name = f"{_resolve_expr(pm.group(1), strvars).strip()} {version}".strip()
        else:
            name = ""
        if not name:
            name = f"{doctype} {version}"
        deprecated = vid_dep or ".deprecated (" in span or ".deprecated(" in span

        # Schematron = the .xslt resource locals this builder references.
        seen, schematron = set(), []
        for var, respath in cprs.items():
            if not respath.endswith(".xslt"):
                continue
            if not re.search(rf"\b{re.escape(var)}\b", span):
                continue
            p = _res_to_path(module, respath)
            if p is not None and p not in seen:
                seen.add(p)
                schematron.append(p)

        root = None
        if _is_cii(span):
            root = CII_ROOT
        else:
            dx = RE_DOCXSD.search(span)
            if dx and dx.group(1) in DOCTYPE_ROOT:
                root = DOCTYPE_ROOT[dx.group(1)]

        out.append({
            "module": module,
            "vesid": f"{grp}:{doctype}:{version}",
            "group": grp, "doctype": doctype, "version": version,
            "name": name, "deprecated": deprecated,
            "schematron": schematron,            # absolute .xslt paths in the clone
            # Did this VES intend Schematron? Distinguishes genuine XSD-only
            # formats (FatturaPA, pure UBL/CII) from ones whose ruleset we
            # failed to link / that failed to compile.
            "has_sch_call": ".addSchematron" in span,
            "root": {"namespace": root[0], "local_name": root[1]} if root else None,
        })
    return out


def parse_all(modules=None) -> list[dict]:
    files = sorted(PHIVE_DIR.glob("phive-rules-*/src/main/java/**/*.java"))
    if modules:
        keep = set(modules)
        files = [f for f in files
                 if any(f"phive-rules-{m}/" in f.as_posix() for m in keep)]
    out = []
    for f in files:
        try:
            out.extend(parse_file(f))
        except Exception as e:  # noqa: BLE001
            print(f"  parse error in {f.name}: {e}", file=sys.stderr)
    # De-dup VESIDs (a few may be registered twice); keep the first.
    uniq = {}
    for v in out:
        uniq.setdefault(v["vesid"], v)
    return list(uniq.values())


def main() -> int:
    mods = sys.argv[1:] or None
    ves = parse_all(mods)
    by_mod: dict[str, int] = {}
    with_sch = with_root = dep = 0
    for v in ves:
        by_mod[v["module"]] = by_mod.get(v["module"], 0) + 1
        with_sch += bool(v["schematron"])
        with_root += bool(v["root"])
        dep += bool(v["deprecated"])
    print(f"Parsed {len(ves)} VES across {len(by_mod)} module(s): "
          f"{with_sch} with schematron, {with_root} with root, {dep} deprecated")
    for m, n in sorted(by_mod.items()):
        print(f"  {m}: {n}")
    print("\nsample:")
    for v in ves[:12]:
        print(f"  [{'D' if v['deprecated'] else ' '}] {v['vesid']:48} {v['name']!r} "
              f"sch={len(v['schematron'])} root={v['root']['local_name'] if v['root'] else '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
