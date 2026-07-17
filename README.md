# phive-web

**Standalone, client-side validation for e-invoices** — XSD structure +
Schematron business rules, running **entirely in your browser**, for every
format published by [phax/phive-rules](https://github.com/phax/phive-rules)
(EN 16931, Peppol BIS / PINT, XRechnung, ZUGFeRD / Factur-X, and more).

Drop in one or many documents and validate them locally — nothing is ever
uploaded — against the whole phive-rules corpus, with a **format drop-down**
that defaults to **Auto-detect (latest)**.

```
┌─ config/ ───────────┐   ┌─ tools/update.sh ─────────────────────────┐   ┌─ site/ ──────────────┐
│ sources.yaml  (tag, │   │ 1 fetch_phive   git sparse clone @ tag     │   │ index.html           │
│   modules, mvn deps)│──▶│ 2 fetch_deps    ph-ubl21 / ph-cii-d16b     │──▶│ assets/ (engines)    │
│ formats.yaml  (the  │   │ 3 compile_sef   XSLT → SEF (xslt3, fetched) │   │ validation-assets/   │
│   format catalogue  │   │ 4 build_manifest stage SEF + XSD, manifest │   │   manifest.json      │
│   + detect rules)   │   │ 5 headless test Playwright smoke (optional)│   │   schematron/ schemas│
└─────────────────────┘   └────────────────────────────────────────────┘  └──────────────────────┘
```

## How it validates

| Layer | Engine (browser) | Engine (CLI) | Catches |
|-------|------------------|--------------|---------|
| **XSD** | xmllint-wasm (libxml2) | lxml | structure, cardinality, types, unknown elements |
| **Schematron** | Saxon-JS HE (SEF) | SaxonC HE (saxonche) | calculation chains, VAT/tax math, code lists, conditional rules |

**Auto-detect** keys off the document's **root element**, then — for UBL and
CII — its **Customization ID** (`cbc:CustomizationID`, or for CII
`ram:GuidelineSpecifiedDocumentContextParameter/ram:ID`). The most specific
rule wins (XRechnung before plain EN 16931), and it always resolves to the
**latest** shipped version of that specification. The detection rules and the
selectable formats are authored in [`config/formats.yaml`](config/formats.yaml).

## Analytics (bulk runs)

During a run the status bar shows live throughput and ETA
(`Validating X / N · R/s · ETA …`). After validating a batch, the
**Analytics** tab turns the results into a printable dashboard: summary cards
(files / passed / **passed with warnings** / failed / pass-rate /
warnings-total / deprecated-format / best-match-detect), a **processing-time**
section (total time, throughput, engine load, and per-file avg / median / p95,
plus the XSD-vs-Schematron split), segmented pass/warning/fail bars by format
and by module, a **top-10 failing Schematron rules** table (each tagged error
vs warning), and a per-file table with separate error and warning counts.
Outcomes are three-way: a file **passes**, **passes with warnings** (only
`flag="warning"` Schematron assertions fired — the document is still valid), or
**fails** (an XSD error or a fatal Schematron assertion).
Filter by outcome, format, rule (click a rule to drill in), or filename
to spot trends and decide what to fix first; **🖨 Print report** produces a
clean hard copy. It all runs client-side on the same in-browser results.

## Design philosophy

- **uv-managed Python tooling.** Every script runs via `uv run --with <deps>`
  — no manual venv needed. `uv sync` once if you want one for your IDE.
- **Engines fetched at build, pinned, not committed.** `fetch_vendor.py`
  pulls them (versions pinned in `config/sources.yaml`): the browser
  `SaxonJS2.js` from Saxonica's versioned zip (it isn't on npm), and
  `xmllint-wasm` + the Node `xslt3` SEF compiler (+ Saxon-JS Node build) from
  the npm CDN (jsDelivr) — no `npm` CLI, just pinned HTTPS downloads. The
  browser runtime lands in the gitignored `site/assets/vendor/`; the Node
  compiler in `work/vendor/`.
- **Upstream is fetched, never committed.** The phive-rules clone and the
  Maven JARs live under the gitignored `work/`; the generated
  `site/validation-assets/` (gzipped SEF + XSD + `manifest.json`) is a **build
  output**, also gitignored — for the full corpus it is hundreds of MB.
  Deploy = run the orchestrator, then publish `site/`.
- **The drop-down is the phive VES registry.** `tools/parse_ves_registry.py`
  parses the `*Validation.java` classes (the same source behind
  [peppol.helger.com](https://peppol.helger.com/public/locale-en_US/menuitem-validation-ws2))
  for each format's display name, version, deprecation flag, rulesets and
  doctype — so the catalogue matches helger and tracks upstream on a re-run.
  Formats are grouped by module with a **Hide deprecated** toggle (default on).
  Auto-detect maps the document's Customization ID to the latest non-deprecated
  matching format (EN 16931 / Peppol / CIUS family); other formats are
  manual-select. (Schematron-linking coverage is partial — see `CLAUDE.md`.)

## Quick start

```bash
# 1. Build the validation assets from the tracked phive-rules ref.
#    Needs: git, network, Node.js (for the Schematron SEF layer).
bash tools/update.sh            # incremental — reuses the work/ cache
bash tools/update.sh --clean    # drop work/ first → fresh deps + full recompile

# 2. Serve the static site and open it (no-cache dev server, auto-opens browser).
uv run python serve.py            # → http://localhost:8765/  (--port / --no-browser)

# 3. (Optional) bulk-validate from the CLI — same engines, no browser:
uv run --with lxml --with saxonche --with pyyaml \
    python tools/validate.py path/to/invoice.xml --format en16931-cii
```

The `site/` directory is a plain static site — deploy it to any static host
(GitHub Pages, S3, nginx). No server-side component, no build step at serve
time.

**Caching:** the engines and rule assets are large and fetched repeatedly (one
xmllint/Saxon load per worker, plus the SEF/XSD per format). Serve
`validation-assets/` and `assets/vendor/` with a long `Cache-Control`
(`public, max-age=…`) so the browser caches them across workers and reloads;
the dev `serve.py` already does this (and keeps the HTML/JS/CSS no-store for
live edits). Most static hosts/CDNs cache aggressively by default.

### GitHub Pages

`site/validation-assets/` is gitignored (a build output), so it must be
generated at deploy time. The included workflow
[`.github/workflows/pages.yml`](.github/workflows/pages.yml) does this: it runs
`tools/update.sh` (fetch → compile SEF → manifest) and publishes `site/` via
the Pages **artifact** deploy — which skips Jekyll, so the asset trees ship
verbatim (a `site/.nojekyll` is also included as a safety net). To enable it:
repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.

Notes:
- All asset paths are relative, so a project page
  (`https://<user>.github.io/<repo>/`) works without configuration.
- Pages caches assets (≈10 min + revalidation) — fine for the worker/reload
  fetches. After a content change, the new build gets fresh URLs/ETags.
- Mind the size: `all: true` (full corpus) is hundreds of MB of gzipped SEF —
  within the Pages 1 GB site limit, but watch the soft bandwidth quota. The
  default `core_set` is small.

## Coverage

`config/sources.yaml` controls what gets built:

- `all: true` → the **entire** phive-rules corpus (every `phive-rules-*`
  module, every version), enumerated from git. Every compiled ruleset becomes
  a selectable format automatically. ~2–3 GB of SEF before gzip (~150–300 MB
  after) — gitignored, so it's a local/deploy build output.
- `all: false` → only the modules in `core_set`.

You don't author formats to surface them — discovery does that from whatever
compiled. You only touch `config/formats.yaml` to **enrich** a format with an
XSD layer, multi-ruleset composition, or an auto-detect rule. When helger
ships new rules, just re-run `tools/update.sh` (or `--clean` for a fresh
fetch).

## Layout

| Path | What |
|------|------|
| `config/sources.yaml` | phive-rules repo, tracked ref (tag/branch/`latest`), module scope, Maven XSD deps |
| `config/formats.yaml` | format catalogue + auto-detect rules (authored) |
| `tools/*.py`, `tools/update.sh` | the orchestrator (see header comments) |
| `serve.py` | no-cache dev preview server for `site/` |
| `site/` | the deployable static site (index.html, assets/) |
| `site/assets/vendor/` | **fetched, gitignored** Saxon-JS + xmllint-wasm browser runtimes |
| `site/validation-assets/` | **generated, gitignored** gzipped SEF + XSD + `manifest.json` (build output) |
| `work/` | **gitignored** clone + Maven JARs + Node `xslt3` compiler (all fetched) |

## Prerequisites

- **git** + network — `fetch_phive.py`, `fetch_deps.py`.
- **Node.js** — `compile_sef.py` (Schematron layer). Without it the XSD layer
  still works; the browser shows Schematron as skipped. SEF compilation runs
  one Node process per CPU core; cap it with `PHIVE_SEF_JOBS=<n>`.
- **uv** — runs the Python tooling.
- **Playwright + Chromium** (optional) — the headless verification step
  (`uv add playwright && uv run playwright install chromium`).
