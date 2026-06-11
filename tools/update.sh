#!/usr/bin/env bash
# phive-web — update / rebuild orchestrator
#
# Refreshes every derived artefact under site/validation-assets/ from the
# pinned upstream sources in config/sources.yaml + config/formats.yaml:
#
#   1. Fetch phive-rules     — shallow+sparse git clone @ tag → work/.
#   2. Fetch XSD deps        — ph-ubl21 / ph-cii-d16b from Maven Central → work/.
#   3. Compile SEF           — every committed XSLT → .sef.json (vendored xslt3 + Node).
#   4. Build manifest        — copy SEFs + XSD closures into site/validation-assets/,
#                              emit manifest.json.
#   5. Headless browser test — drive Chromium through site/index.html (optional).
#
# Only the copied/transformed artefacts under site/validation-assets/ are
# committed; the upstream clone + JARs under work/ are gitignored.
#
# Each step prints a header + wall-clock time. Best-effort steps skip with
# a message when an optional tool (node / saxonche / playwright) is absent;
# a real failure exits non-zero.
#
# Run from the repo root:
#   bash tools/update.sh            # incremental — reuse the work/ cache
#   bash tools/update.sh --clean    # drop work/ first → fresh deps + recompile

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ── argument parsing ────────────────────────────────────────────────
CLEAN=0
PRUNE=0
for arg in "$@"; do
  case "$arg" in
    --clean) CLEAN=1 ;;
    --prune) PRUNE=1 ;;
    -h|--help)
      cat <<'USAGE'
Usage: bash tools/update.sh [--clean] [--prune]

Refreshes site/validation-assets/ from the tracked phive-rules ref.

  (no args)   incremental — reuse the work/ cache (fast re-run)
  --clean     drop work/ first → fresh clone, fresh deps, full recompile
  --prune     delete work/ AFTER the build → keep only the committed/
              deployable output (site/validation-assets/). Frees the GBs of
              clone + Maven JARs + uncompressed .sef.json. Note: the next run
              re-fetches + recompiles, and the CLI validator (tools/validate.py)
              needs work/, so re-run without --prune to use it.
  -h, --help  show this help

Nothing under work/ or site/validation-assets/ is committed (.gitignore);
--prune is purely local disk hygiene.
USAGE
      exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# --clean drops everything generated so the run starts from a blank slate:
#   • work/ — the phive-rules clone (and the SEFs compiled inside it) plus the
#     Maven JARs / OASIS zip + extracted schemas under work/deps;
#   • site/validation-assets/ — the gzipped SEF + XSD + manifest.json.
# build_manifest.py also clears the asset subtrees it owns each run, but
# removing the whole dir here also sweeps any stale leftovers from an
# interrupted previous build (and formats/modules no longer produced).
if [[ "$CLEAN" -eq 1 ]]; then
  printf "\033[1;33m▸ --clean: removing work/, site/validation-assets/ and fetched engines (fresh build)\033[0m\n"
  rm -rf "$ROOT/work" "$ROOT/site/validation-assets" "$ROOT/site/assets/vendor"
fi

# uv runs each tool with just the deps it needs — no venv required.
PY="uv run --with pyyaml python"

# ── timing ──────────────────────────────────────────────────────────
if [[ -n "${EPOCHREALTIME-}" ]]; then
  _now() { printf "%s\n" "$EPOCHREALTIME"; }
else
  _now() { printf "%d\n" "$SECONDS"; }
fi
_fmt() { awk -v t="$1" 'BEGIN { if (t<1) printf "%.0fms", t*1000; else if (t<60) printf "%.1fs", t; else printf "%dm%02ds", int(t/60), int(t)%60 }'; }
_diff() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.3f\n", b-a }'; }

START="$(_now)"; declare -a NAMES=() TIMES=(); CUR=""; CUR_T=""
step() {
  if [[ -n "$CUR" ]]; then local e; e="$(_diff "$CUR_T" "$(_now)")"; printf "  \033[2m└ %s\033[0m\n" "$(_fmt "$e")"; NAMES+=("$CUR"); TIMES+=("$e"); fi
  printf "\n\033[1;36m== %s ==\033[0m\n" "$1"; CUR="$1"; CUR_T="$(_now)"
}
finalize() {
  if [[ -n "$CUR" ]]; then local e; e="$(_diff "$CUR_T" "$(_now)")"; printf "  \033[2m└ %s\033[0m\n" "$(_fmt "$e")"; NAMES+=("$CUR"); TIMES+=("$e"); fi
  printf "\n\033[1;36m== Timing summary ==\033[0m\n"; local i
  for i in "${!TIMES[@]}"; do printf "  %8s  %s\n" "$(_fmt "${TIMES[$i]}")" "${NAMES[$i]}"; done
  printf "  %8s  total\n" "$(_fmt "$(_diff "$START" "$(_now)")")"
}

step "1/6 Fetching vendored engines (Saxon-JS, xmllint-wasm, xslt3)"
$PY tools/fetch_vendor.py

step "2/6 Fetching phive-rules (sparse clone @ tracked ref)"
$PY tools/fetch_phive.py

step "3/6 Fetching base XSD dependencies (Maven Central)"
$PY tools/fetch_deps.py

step "4/6 Compiling Schematron XSLT → SEF (Saxon-JS HE target)"
if command -v node >/dev/null 2>&1; then
  uv run python tools/compile_sef.py
else
  echo "  (node not installed; skipping SEF compile — the Schematron layer"
  echo "   will be inactive in the browser. Install Node.js to enable it.)"
fi

step "5/6 Building manifest + staging validation assets"
$PY tools/build_manifest.py

step "6/6 Headless in-browser validator test"
if uv run --with playwright python -c "import playwright" >/dev/null 2>&1; then
  uv run --with playwright python tools/validate_browser_headless.py
else
  echo "  (playwright not installed; skipping. Enable with:"
  echo "   uv add playwright && uv run playwright install chromium)"
fi

finalize

# --prune: drop the heavy intermediates now that the deployable output exists.
# work/ holds the clone (schematron sources, uncompressed .sef.json compiled
# in place), the Maven JARs, the OASIS zip and the extracted dep XSDs — none of
# it committed, none needed to serve site/. Keeps only site/validation-assets/.
if [[ "$PRUNE" -eq 1 ]]; then
  if [[ -d "$ROOT/work" ]]; then
    SZ="$(du -sh "$ROOT/work" 2>/dev/null | cut -f1)"
    rm -rf "$ROOT/work"
    printf "\033[1;33m▸ --prune: removed work/ (freed %s); site/validation-assets/ kept\033[0m\n" "${SZ:-?}"
  fi
fi

printf "\n\033[1;32m✓ update complete\033[0m\n"
