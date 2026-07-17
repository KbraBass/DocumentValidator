/* In-browser phive-rules validator (standalone).
 *
 * Renders a format picker + multi-file drop zone inside
 * `<div id="phive-validator">`, then for each file:
 *   1. Parses the XML; reads root element + Customization ID.
 *   2. Resolves the validation format — the chosen drop-down entry, or, on
 *      "Auto-detect (latest)", the highest-priority matching rule from the
 *      manifest (root element, then CustomizationID), which always points at
 *      the LATEST shipped spec version.
 *   3. Runs XSD validation via xmllint-wasm (libxml2), when the format ships
 *      a schema.
 *   4. Runs each Schematron ruleset (SEF) via Saxon-JS HE.
 *   5. Renders per-file pass/fail with collapsible details.
 *
 * Everything runs client-side. The page fetches only the runtimes, the
 * schemas and the SEFs it depends on — it never transmits document data.
 *
 * For throughput, the CPU-heavy validation (XSD + Saxon-JS) runs in a pool of
 * Web Workers (one per core) via phive-engine.js, so large batches scale on
 * multi-core machines. The DOM-bound parts (inspection, SVRL parsing,
 * rendering) stay here on the main thread. Falls back to a single in-thread
 * engine when Workers are unavailable.
 */
import { createValidator } from "./phive-engine.js";

(function () {
  "use strict";

  const root = document.getElementById("phive-validator");
  if (!root) return;

  const PATHS = {
    assetsBase:  root.dataset.assetsBase     || "./validation-assets/",
    manifest:    root.dataset.manifest       || "./validation-assets/manifest.json",
    saxon:       root.dataset.saxonSrc       || "./assets/vendor/saxonjs/SaxonJS2.js",
    xmllint:     root.dataset.xmllintSrc     || "./assets/vendor/xmllint-wasm/index-browser.mjs",
    xmllintWasm: root.dataset.xmllintWasmSrc || "./assets/vendor/xmllint-wasm/xmllint.wasm",
  };
  const ASSETS_BASE = new URL(PATHS.assetsBase, document.baseURI).toString();
  const url = (rel) => new URL(rel, document.baseURI).toString();
  const assetUrl = (rel) => new URL(rel, ASSETS_BASE).toString();

  // UBL / CII namespaces for Customization-ID extraction.
  const NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2";
  const NS_RSM = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100";
  const NS_RAM = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100";
  const SVRL_NS = "http://purl.oclc.org/dsdl/svrl";

  root.innerHTML = `
    <div class="phive">
      <nav class="phive__tabs" role="tablist">
        <button type="button" class="phive__tab is-active" data-tab="validate">Validate</button>
        <button type="button" class="phive__tab" data-tab="analytics">Analytics <span id="phive-an-count" class="phive__tab-count" hidden></span></button>
      </nav>

      <section class="phive__panel" data-panel="validate">
        <div class="phive__controls">
          <label class="phive__format-label" for="phive-format">Validate as</label>
          <select id="phive-format"><option value="">Auto-detect (latest)</option></select>
          <label class="phive__dep-toggle"><input type="checkbox" id="phive-hide-deprecated" checked> Hide deprecated</label>
        </div>
        <label class="phive__drop" id="phive-drop">
          <input type="file" id="phive-input" accept=".xml,application/xml,text/xml" multiple hidden>
          <strong>Choose XML files</strong>
          <span class="phive__hint">…or drop them here. Multiple files OK. Nothing leaves your browser.</span>
        </label>
        <div class="phive__bar">
          <span id="phive-status">Loading manifest…</span>
          <button type="button" id="phive-run" disabled>Validate</button>
          <button type="button" id="phive-clear" disabled>Clear</button>
        </div>
        <ol class="phive__list" id="phive-results"></ol>
      </section>

      <section class="phive__panel" data-panel="analytics" hidden>
        <div id="phive-analytics"><p class="phive-an__empty">Validate one or more files to build the analytics dashboard.</p></div>
      </section>
    </div>
  `;

  // ── Tabs ──────────────────────────────────────────────────────────
  const tabs = [...root.querySelectorAll(".phive__tab")];
  const panels = [...root.querySelectorAll(".phive__panel")];
  tabs.forEach(t => t.addEventListener("click", () => {
    tabs.forEach(x => x.classList.toggle("is-active", x === t));
    const which = t.dataset.tab;
    panels.forEach(p => { p.hidden = p.dataset.panel !== which; });
  }));
  function showTab(which) {
    tabs.forEach(x => x.classList.toggle("is-active", x.dataset.tab === which));
    panels.forEach(p => { p.hidden = p.dataset.panel !== which; });
  }

  const fileInput = document.getElementById("phive-input");
  const dropZone  = document.getElementById("phive-drop");
  const statusEl  = document.getElementById("phive-status");
  const runBtn    = document.getElementById("phive-run");
  const clearBtn  = document.getElementById("phive-clear");
  const listEl    = document.getElementById("phive-results");
  const formatSel = document.getElementById("phive-format");
  const hideDepEl = document.getElementById("phive-hide-deprecated");

  let queuedFiles = [];
  let lastResults = [];          // structured per-file records (drives Analytics)
  let _timing = null;            // last run's wall-clock timing (drives the dashboard)

  fileInput.addEventListener("change", () => addFiles([...fileInput.files]));
  dropZone.addEventListener("dragover", e => { e.preventDefault(); dropZone.classList.add("is-drag"); });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("is-drag"));
  dropZone.addEventListener("drop", e => {
    e.preventDefault();
    dropZone.classList.remove("is-drag");
    addFiles([...e.dataTransfer.files].filter(f => /\.xml$/i.test(f.name)));
  });
  runBtn.addEventListener("click", () => runAll().catch(showFatal));
  clearBtn.addEventListener("click", () => {
    queuedFiles = [];
    listEl.innerHTML = "";
    lastResults = [];
    _timing = null;
    buildAnalytics();
    runBtn.disabled = true;
    clearBtn.disabled = true;
    statusEl.textContent = "Idle";
  });

  function addFiles(files) {
    queuedFiles = queuedFiles.concat(files);
    runBtn.disabled = !queuedFiles.length;
    clearBtn.disabled = !queuedFiles.length;
    statusEl.textContent = queuedFiles.length === 1
      ? "1 file queued" : `${queuedFiles.length} files queued`;
  }

  function showFatal(err) {
    console.error(err);
    statusEl.textContent = "Fatal: " + (err && err.message || err);
  }

  // ── Lazy-loaded engines + manifest ────────────────────────────────
  let _manifest;
  // Absolute paths handed to the engine / workers (workers have no baseURI).
  const ENGINE_PATHS = {
    saxon: url(PATHS.saxon),
    xmllint: url(PATHS.xmllint),
    assetsBase: ASSETS_BASE,
  };

  async function loadManifest() {
    if (_manifest) return _manifest;
    const r = await fetch(url(PATHS.manifest));
    if (!r.ok) throw new Error(`Manifest ${PATHS.manifest} ${r.status}`);
    _manifest = await r.json();
    return _manifest;
  }

  // Populate the drop-down from the manifest, grouped into <optgroup>s by
  // phive-rules module. Auto-detect stays the first/default option. The
  // "Hide deprecated" toggle re-renders the list, keeping the current
  // selection if it's still visible.
  function populateFormatSelect() {
    if (!_manifest) return;
    const hideDep = hideDepEl.checked;
    const prev = formatSel.value;
    // wipe everything except the first (Auto-detect) option
    while (formatSel.options.length > 1) formatSel.remove(1);
    const order = _manifest.groups && _manifest.groups.length
      ? _manifest.groups
      : [...new Set(_manifest.formats.map(f => f.group || "formats"))];
    let shown = 0;
    for (const g of order) {
      const inGroup = _manifest.formats.filter(f =>
        (f.group || "formats") === g && !(hideDep && f.deprecated));
      if (!inGroup.length) continue;
      const og = document.createElement("optgroup");
      og.label = g;
      for (const f of inGroup) {
        const opt = document.createElement("option");
        opt.value = f.key;
        const tags = [];
        if (f.version) tags.push("v" + f.version);
        if (f.deprecated) tags.push("deprecated");
        if (!f.autodetect && !f.deprecated) tags.push("manual");
        opt.textContent = tags.length ? `${f.label} — ${tags.join(", ")}` : f.label;
        og.appendChild(opt);
        shown++;
      }
      formatSel.appendChild(og);
    }
    // restore selection if still present, else fall back to Auto-detect
    formatSel.value = [...formatSel.options].some(o => o.value === prev) ? prev : "";
    const total = _manifest.formats.length;
    statusEl.textContent = `Idle — ${shown} of ${total} formats`
      + (hideDep ? " (deprecated hidden)" : "");
  }

  async function initUI() {
    try {
      await loadManifest();
    } catch (e) {
      statusEl.textContent = "Could not load manifest — run tools/update.sh";
      console.error(e);
      return;
    }
    hideDepEl.addEventListener("change", populateFormatSelect);
    populateFormatSelect();
  }

  // ── XML inspection + format resolution ────────────────────────────
  function inspectDocument(xmlText) {
    const doc = new DOMParser().parseFromString(xmlText, "application/xml");
    const err = doc.querySelector("parsererror");
    if (err) throw new Error("XML parse error: " + err.textContent.trim().split("\n")[0]);
    const el = doc.documentElement;
    const ns = el.namespaceURI || "";
    const local = el.localName;
    let cust = "";
    if (ns === NS_RSM) {
      const ctx = el.getElementsByTagNameNS(NS_RAM, "GuidelineSpecifiedDocumentContextParameter")[0];
      cust = (ctx?.getElementsByTagNameNS(NS_RAM, "ID")[0]?.textContent || "").trim();
    } else {
      cust = (el.getElementsByTagNameNS(NS_CBC, "CustomizationID")[0]?.textContent || "").trim();
    }
    return { rootQName: ns ? `{${ns}}${local}` : local, ns, local, customizationID: cust };
  }

  // Returns { key, match } where match is the detection quality:
  //   'exact'  — the document's Customization ID matched exactly (high confidence)
  //   'best'   — matched only by a substring / prefix / root element (verify!)
  function autoDetectKey(manifest, meta) {
    for (const rule of manifest.detection) {       // pre-sorted by priority
      if (rule.root.namespace !== meta.ns || rule.root.local_name !== meta.local) continue;
      const c = meta.customizationID;
      if ((rule.customization_exact || []).includes(c)) return { key: rule.key, match: "exact" };
      if ((rule.customization_contains || []).some(s => c.includes(s))) return { key: rule.key, match: "best" };
      if ((rule.customization_prefix || []).some(p => c.startsWith(p))) return { key: rule.key, match: "best" };
      const hasCust = rule.customization_contains || rule.customization_exact || rule.customization_prefix;
      if (!hasCust) return { key: rule.key, match: "best" };   // root-only rule
    }
    return null;
  }

  function resolveFormat(manifest, meta) {
    const chosen = formatSel.value;
    if (chosen) {
      return { fmt: manifest.formats.find(f => f.key === chosen) || null, auto: false, match: null };
    }
    const hit = autoDetectKey(manifest, meta);
    const fmt = hit ? (manifest.formats.find(f => f.key === hit.key) || null) : null;
    return { fmt, auto: true, match: hit ? hit.match : null };
  }

  // A failed assertion's severity is carried by the SVRL `flag` attribute
  // (phive-rules EN16931 / Peppol / XRechnung use flag="fatal" | "warning"),
  // with `role` as a fallback for rulesets that use it instead. Anything that
  // isn't explicitly a warning-like flag counts as a blocking error — so
  // unflagged rulesets keep their old "every failure fails the file" meaning.
  function svrlSeverity(node) {
    const flag = (node.getAttribute("flag") || node.getAttribute("role") || "").trim().toLowerCase();
    return (flag === "warning" || flag === "warn" || flag === "info" || flag === "information")
      ? "warning" : "fatal";
  }

  // The engine (worker or fallback) returns RAW SVRL; parse it into failures
  // here on the main thread (DOMParser is unavailable in workers). Failures are
  // split by severity: `errors` block the file, `warnings` don't.
  function parseSvrl(sch) {
    if (sch.skipped) return { skipped: sch.skipped, error: !!sch.error };
    const failures = [];
    for (const { ruleset, svrl } of sch.svrls) {
      const doc = new DOMParser().parseFromString(svrl, "application/xml");
      if (doc.querySelector("parsererror")) return { skipped: "SVRL parse error", error: true };
      for (const node of doc.getElementsByTagNameNS(SVRL_NS, "failed-assert")) {
        failures.push({
          id: node.getAttribute("id"),
          location: node.getAttribute("location"),
          text: (node.getElementsByTagNameNS(SVRL_NS, "text")[0]?.textContent || "").trim(),
          ruleset,
          severity: svrlSeverity(node),
        });
      }
    }
    const errors = failures.filter(f => f.severity !== "warning");
    const warnings = failures.filter(f => f.severity === "warning");
    // `passed` = no blocking errors (warnings alone don't fail the layer).
    return { passed: errors.length === 0, failures, errors, warnings };
  }

  // ── Engines ───────────────────────────────────────────────────────
  // The XSD layer (xmllint-wasm) is DOM-free and runs in a pool of Web
  // Workers — one per core — for real multi-core throughput on big batches.
  // Schematron (Saxon-JS) needs the DOM, so it can't run in a worker; it runs
  // on a single main-thread engine. When Workers are unavailable, that same
  // main-thread engine also does the XSD layer.
  const MAX_WORKERS = 12;
  let _pool = null;            // array of ready Workers (XSD), or null
  let _mainEngine = null;      // main-thread engine: always Schematron, + XSD fallback
  function workerCount() {
    return Math.max(1, Math.min(MAX_WORKERS, navigator.hardwareConcurrency || 4));
  }

  async function ensureEngines() {
    if (_mainEngine) return;
    _mainEngine = createValidator(ENGINE_PATHS);   // lazy-loads Saxon on first use
    if (typeof Worker === "undefined") return;
    try {
      const workers = [];
      await Promise.all(Array.from({ length: workerCount() }, () => new Promise((resolve, reject) => {
        let w;
        try { w = new Worker(url("./assets/phive-worker.js"), { type: "module" }); }
        catch (e) { return reject(e); }
        const onMsg = (e) => {
          if (e.data.type === "ready") { w.removeEventListener("message", onMsg); workers.push(w); resolve(); }
          else if (e.data.type === "error") { w.removeEventListener("message", onMsg); reject(new Error(e.data.error)); }
        };
        w.addEventListener("message", onMsg);
        w.addEventListener("error", reject);
        w.postMessage({ type: "init", paths: ENGINE_PATHS });
      })));
      _pool = workers;
    } catch (e) {
      console.warn("[validator] XSD worker pool unavailable, doing XSD in-thread:", e);
      _pool = null;
    }
  }

  // Schematron runs on the single main-thread engine, so transforms must be
  // SERIALISED — Saxon-JS keeps global + parsed-SEF state during a transform,
  // and interleaving concurrent lanes' transforms corrupts it (manifesting as
  // "Cannot compare xs:QName with xs:QName"). A promise chain enforces
  // one-at-a-time without losing the (parallel) XSD work in the workers.
  let _schChain = Promise.resolve();
  function schematronSerial(xmlText, fmt) {
    const p = _schChain.then(() => _mainEngine.validateSchematronOnly(xmlText, fmt));
    _schChain = p.then(() => {}, () => {});   // keep the chain alive past errors
    return p;
  }

  // XSD for one file: on a pool worker if available, else the main engine.
  function xsdOn(w, xmlText, fmt) {
    if (!w) return _mainEngine.validateXsdOnly(xmlText, fmt);
    return new Promise((resolve, reject) => {
      const id = Math.random().toString(36).slice(2);
      const onMsg = (e) => {
        if (e.data.type !== "result" || e.data.id !== id) return;
        w.removeEventListener("message", onMsg);
        e.data.error ? reject(new Error(e.data.error)) : resolve(e.data.xsd);
      };
      w.addEventListener("message", onMsg);
      w.postMessage({ type: "xsd", id, xmlText, fmt });
    });
  }

  // ── Orchestration ─────────────────────────────────────────────────
  async function runAll() {
    runBtn.disabled = true;
    clearBtn.disabled = true;
    listEl.innerHTML = "";
    lastResults = [];
    statusEl.textContent = "Loading runtimes…";

    const t0 = performance.now();
    const manifest = await loadManifest();
    await ensureEngines();
    const tValidate = performance.now();      // engines ready — validation starts
    const lanes = _pool || [null];     // null lane = XSD on the main engine
    const note = _pool ? ` · ${_pool.length} workers (XSD)` : "";
    const total = queuedFiles.length;
    const rows = queuedFiles.map(f => appendRow(f.name));   // preserve order
    lastResults = new Array(total);
    let done = 0, next = 0;
    statusEl.textContent = `Validating 0 / ${total}${note}`;

    function progress() {
      const secs = (performance.now() - tValidate) / 1000;
      const rate = secs > 0 ? done / secs : 0;
      const eta = rate > 0 ? (total - done) / rate : 0;
      statusEl.textContent = `Validating ${done} / ${total} · `
        + `${rate.toFixed(1)}/s` + (done < total ? ` · ETA ${fmtDur(eta * 1000)}` : "") + note;
    }

    // Each lane pulls the next file until the queue drains → parallel across
    // workers, results land back on the main thread for DOM rendering.
    async function lane(w) {
      while (true) {
        const i = next++;
        if (i >= total) return;
        const file = queuedFiles[i];
        const row = rows[i];
        const rec = {
          name: file.name, formatKey: null, formatLabel: null, group: null,
          deprecated: false, auto: false, match: null, root: "", custom: "",
          xsd: "na", sch: "na", failures: [], errorCount: 0, warnCount: 0,
          outcome: "error", ms: 0, xsdMs: 0, schMs: 0,
        };
        const ft0 = performance.now();
        try {
          const text = await file.text();
          const meta = inspectDocument(text);
          rec.root = meta.rootQName; rec.custom = meta.customizationID;
          const { fmt, auto, match } = resolveFormat(manifest, meta);
          rec.auto = auto; rec.match = match;
          renderMeta(row, meta, fmt, auto);
          renderBanner(row, auto, match, fmt);
          if (!fmt) { markRow(row, "skip", "No matching format"); rec.outcome = "skip"; }
          else {
            rec.formatKey = fmt.key; rec.formatLabel = fmt.label;
            rec.group = fmt.group || "?"; rec.deprecated = !!fmt.deprecated;
            // XSD in parallel on a worker; Schematron serialised on the main engine.
            const a = performance.now();
            const xsd = await xsdOn(w, text, fmt);
            const b = performance.now();
            const sch = parseSvrl(await schematronSerial(text, fmt));
            const c = performance.now();
            rec.xsdMs = b - a; rec.schMs = c - b;
            renderXsd(row, xsd);
            renderSch(row, sch);
            rec.xsd = xsd.error ? "error" : xsd.skipped ? "skip" : (xsd.passed ? "pass" : "fail");
            // Schematron layer has an extra "warn" state: passed (no blocking
            // errors) but with one or more warning-severity assertions.
            const schErrs = (sch.errors || []).length, schWarns = (sch.warnings || []).length;
            rec.sch = sch.error ? "error" : sch.skipped ? "skip"
              : schErrs ? "fail" : schWarns ? "warn" : "pass";
            rec.failures = (sch.failures || []).map(f =>
              ({ id: f.id || "—", text: f.text, ruleset: f.ruleset, severity: f.severity }));
            rec.errorCount = schErrs; rec.warnCount = schWarns;
            if (xsd.error || sch.error) {
              // A layer that should have run but errored — NOT a pass.
              rec.outcome = "error";
              markRow(row, "error", "Error");
            } else {
              // A clean skip (layer not applicable) doesn't fail the file.
              // Precedence: XSD/Schematron errors → fail; else warnings → warn;
              // else clean pass.
              const xsdOk = xsd.passed || xsd.skipped;
              if (!xsdOk || schErrs) { rec.outcome = "fail"; markRow(row, "fail", "Fail"); }
              else if (schWarns)     { rec.outcome = "warn"; markRow(row, "warn", `⚠ ${schWarns} warning${schWarns > 1 ? "s" : ""}`); }
              else                   { rec.outcome = "pass"; markRow(row, "ok", "Pass"); }
            }
          }
        } catch (err) {
          console.error(err);
          rec.outcome = "error";
          markRow(row, "error", "Error: " + (err.message || err));
        }
        rec.ms = performance.now() - ft0;
        lastResults[i] = rec;
        done++;
        progress();
      }
    }

    await Promise.all(lanes.map(lane));
    const tEnd = performance.now();
    _timing = {
      total: tEnd - t0, load: tValidate - t0, validation: tEnd - tValidate,
      files: total, workers: _pool ? _pool.length : 1,
    };
    statusEl.textContent = `Done — ${total} file(s) in ${fmtDur(_timing.validation)} · `
      + `${(total / (_timing.validation / 1000 || 1)).toFixed(1)}/s${note}`;
    runBtn.disabled = false;
    clearBtn.disabled = false;
    buildAnalytics();
  }

  // ── Rendering ─────────────────────────────────────────────────────
  function appendRow(name) {
    const li = document.createElement("li");
    li.className = "phive-row";
    li.innerHTML = `
      <header>
        <span class="phive-row__name">${escapeHtml(name)}</span>
        <span class="phive-row__badge phive-badge phive-badge--running" data-testid="badge">…</span>
      </header>
      <div class="phive-row__banner"></div>
      <dl class="phive-row__meta"></dl>
      <div class="phive-row__xsd"></div>
      <div class="phive-row__sch"></div>`;
    listEl.appendChild(li);
    return li;
  }

  function markRow(row, status, label) {
    const badge = row.querySelector(".phive-badge");
    badge.className = `phive-row__badge phive-badge phive-badge--${status}`;
    badge.textContent = label;
  }

  // Auto-detect confidence banner: green for an exact Customization-ID match,
  // yellow when the format was only a best guess (prefix / root element).
  function renderBanner(row, auto, match, fmt) {
    const el = row.querySelector(".phive-row__banner");
    if (!auto || !fmt) { el.innerHTML = ""; return; }
    if (match === "exact") {
      el.className = "phive-row__banner phive-banner phive-banner--exact";
      el.innerHTML = `<strong>✓ Exact match</strong> — detected from the Customization ID.`;
    } else {
      el.className = "phive-row__banner phive-banner phive-banner--best";
      el.innerHTML = `<strong>⚠ Best match</strong> — inferred from the root element / Customization ID prefix. `
        + `Confirm <code>${escapeHtml(fmt.label)}</code> is correct, or pick the exact format above.`;
    }
  }

  function renderMeta(row, meta, fmt, auto) {
    row.querySelector(".phive-row__meta").innerHTML = `
      <dt>Format</dt><dd>${fmt ? `${escapeHtml(fmt.label)} <code>${escapeHtml(fmt.key)}</code>` : "—"}${auto ? ' <span class="phive-note">(auto-detected)</span>' : ""}</dd>
      <dt>Root</dt><dd><code>${escapeHtml(meta.rootQName)}</code></dd>
      <dt>Customization ID</dt><dd><code>${escapeHtml(meta.customizationID || "—")}</code></dd>`;
  }

  function renderXsd(row, res) {
    const div = row.querySelector(".phive-row__xsd");
    if (res.skipped) { div.innerHTML = `<p class="phive-skip">XSD skipped — ${escapeHtml(res.skipped)}.</p>`; return; }
    if (res.passed)  { div.innerHTML = `<p class="phive-pass">XSD passed.</p>`; return; }
    div.innerHTML = `
      <details open>
        <summary><span class="phive-fail">XSD failed</span> — ${res.errors.length} issue(s)</summary>
        <ul>${res.errors.slice(0, 50).map(e => `<li>${escapeHtml(e)}</li>`).join("")}</ul>
        ${res.errors.length > 50 ? `<p class="phive-note">…and ${res.errors.length - 50} more.</p>` : ""}
      </details>`;
  }

  function renderSch(row, res) {
    const div = row.querySelector(".phive-row__sch");
    if (res.skipped) { div.innerHTML = `<p class="phive-skip">Schematron skipped — ${escapeHtml(res.skipped)}.</p>`; return; }
    const errors = res.errors || [], warnings = res.warnings || [];
    if (!errors.length && !warnings.length) { div.innerHTML = `<p class="phive-pass">Schematron passed.</p>`; return; }
    const item = (f) => `
      <li>
        <code>${escapeHtml(f.id || "—")}</code>
        ${f.ruleset ? `<span class="phive-note">(${escapeHtml(f.ruleset)})</span>` : ""}
        ${f.location ? ` <span class="phive-note">@ <code>${escapeHtml(f.location)}</code></span>` : ""}
        <div>${escapeHtml(f.text)}</div>
      </li>`;
    const group = (items, klass, headline) => items.length ? `
      <details open>
        <summary><span class="phive-${klass}">${headline}</span> — ${items.length} rule(s)</summary>
        <ul>${items.map(item).join("")}</ul>
      </details>` : "";
    div.innerHTML = group(errors, "fail", "Schematron errors")
      + group(warnings, "warn", "Schematron warnings");
  }

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function fmtDur(ms) {
    if (!isFinite(ms) || ms < 0) return "—";
    if (ms < 1000) return `${Math.round(ms)}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
    const m = Math.floor(ms / 60000), s = Math.round((ms % 60000) / 1000);
    return `${m}m${String(s).padStart(2, "0")}s`;
  }

  function percentile(sorted, p) {           // sorted ascending
    if (!sorted.length) return 0;
    const i = Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length));
    return sorted[i];
  }

  // ── Analytics dashboard ───────────────────────────────────────────
  const anEl = document.getElementById("phive-analytics");
  const anCountEl = document.getElementById("phive-an-count");
  let anFilter = { outcome: "all", format: "all", rule: "all", q: "" };

  const OUTCOMES = [
    { k: "pass", label: "Passed", cls: "ok" },
    { k: "warn", label: "Passed with warnings", cls: "warn" },
    { k: "fail", label: "Failed", cls: "fail" },
    { k: "skip", label: "Skipped", cls: "skip" },
    { k: "error", label: "Errors", cls: "error" },
  ];

  // Bucket an outcome into the four bar segments (clean pass / warnings / fail /
  // other = skip+error).
  function outcomeSeg(o) {
    return o === "pass" ? "pass" : o === "warn" ? "warn" : o === "fail" ? "fail" : "other";
  }

  function aggBy(rows, keyFn) {
    const m = new Map();
    for (const r of rows) {
      const k = keyFn(r) || "(none)";
      const a = m.get(k) || { key: k, total: 0, pass: 0, warn: 0, fail: 0, other: 0 };
      a.total++;
      a[outcomeSeg(r.outcome)]++;
      m.set(k, a);
    }
    return [...m.values()].sort((a, b) => b.total - a.total);
  }

  // Segmented horizontal bar (pass green / warn amber / fail red / other grey),
  // width by total.
  function segBars(items, max) {
    max = max || Math.max(1, ...items.map(i => i.total));
    return `<ul class="phive-bars">` + items.map(i => {
      const w = (i.total / max * 100);
      const seg = (n, cls) => n ? `<span class="phive-seg phive-seg--${cls}" style="flex:${n}" title="${n} ${cls}"></span>` : "";
      return `<li>
        <span class="phive-bars__lbl" title="${escapeHtml(i.key)}">${escapeHtml(i.key)}</span>
        <span class="phive-bars__track">
          <span class="phive-bars__fill" style="width:${w.toFixed(1)}%">${seg(i.pass, "ok")}${seg(i.warn || 0, "warn")}${seg(i.fail, "fail")}${seg(i.other, "other")}</span>
        </span>
        <span class="phive-bars__val">${i.total}${i.fail ? ` <small>${i.fail}✗</small>` : ""}${i.warn ? ` <small class="phive-bars__warn">${i.warn}⚠</small>` : ""}</span>
      </li>`;
    }).join("") + `</ul>`;
  }

  function filteredRows() {
    return lastResults.filter(r => {
      if (anFilter.outcome !== "all" && r.outcome !== anFilter.outcome) return false;
      if (anFilter.format !== "all" && (r.formatLabel || "(no format)") !== anFilter.format) return false;
      if (anFilter.rule !== "all" && !r.failures.some(f => f.id === anFilter.rule)) return false;
      if (anFilter.q && !r.name.toLowerCase().includes(anFilter.q)) return false;
      return true;
    });
  }

  function buildAnalytics() {
    if (!lastResults.length) {
      anEl.innerHTML = `<p class="phive-an__empty">Validate one or more files to build the analytics dashboard.</p>`;
      anCountEl.hidden = true;
      return;
    }
    anCountEl.hidden = false;
    anCountEl.textContent = lastResults.length;
    anFilter = { outcome: "all", format: "all", rule: "all", q: "" };

    const formats = [...new Set(lastResults.map(r => r.formatLabel || "(no format)"))].sort();
    const rules = [...new Set(lastResults.flatMap(r => r.failures.map(f => f.id)))].sort();
    const opt = (v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`;

    anEl.innerHTML = `
      <div class="phive-an">
        <div class="phive-an__toolbar no-print">
          <strong>Filters:</strong>
          <label>Outcome
            <select id="phive-an-outcome"><option value="all">All</option>${OUTCOMES.map(o => opt(o.k)).join("")}</select>
          </label>
          <label>Format
            <select id="phive-an-format"><option value="all">All</option>${formats.map(opt).join("")}</select>
          </label>
          <label>Rule
            <select id="phive-an-rule"><option value="all">All</option>${rules.map(opt).join("")}</select>
          </label>
          <label>File
            <input type="search" id="phive-an-q" placeholder="filename contains…">
          </label>
          <button type="button" id="phive-an-reset">Reset</button>
          <button type="button" id="phive-an-print">🖨 Print report</button>
        </div>
        <div id="phive-an-body"></div>
      </div>`;

    anEl.querySelector("#phive-an-print").addEventListener("click", () => window.print());
    anEl.querySelector("#phive-an-reset").addEventListener("click", () => {
      anFilter = { outcome: "all", format: "all", rule: "all", q: "" };
      anEl.querySelector("#phive-an-outcome").value = "all";
      anEl.querySelector("#phive-an-format").value = "all";
      anEl.querySelector("#phive-an-rule").value = "all";
      anEl.querySelector("#phive-an-q").value = "";
      renderAnalyticsBody();
    });
    ["outcome", "format", "rule"].forEach(k =>
      anEl.querySelector("#phive-an-" + k).addEventListener("change", e => {
        anFilter[k] = e.target.value; renderAnalyticsBody();
      }));
    anEl.querySelector("#phive-an-q").addEventListener("input", e => {
      anFilter.q = e.target.value.toLowerCase(); renderAnalyticsBody();
    });
    renderAnalyticsBody();
  }

  function renderAnalyticsBody() {
    const rows = filteredRows();
    const body = anEl.querySelector("#phive-an-body");
    const total = rows.length;
    const c = { pass: 0, warn: 0, fail: 0, skip: 0, error: 0 };
    rows.forEach(r => { c[r.outcome] = (c[r.outcome] || 0) + 1; });
    // A warning does not reject an e-invoice, so "valid" = clean pass + passed
    // with warnings. Warnings are still surfaced as their own card / segment.
    const validRate = total ? Math.round(((c.pass + c.warn) / total) * 100) : 0;
    const totalWarnings = rows.reduce((a, r) => a + (r.warnCount || 0), 0);

    // Top-10 failing Schematron rules across the filtered set, tracking whether
    // each rule is an error or a warning.
    const errMap = new Map();
    rows.forEach(r => r.failures.forEach(f => {
      const e = errMap.get(f.id) || { id: f.id, count: 0, files: new Set(), text: f.text, severity: f.severity };
      e.count++; e.files.add(r.name);
      if (f.severity !== "warning") e.severity = f.severity || "fatal";  // an error dominates the label
      errMap.set(f.id, e);
    }));
    const topErrors = [...errMap.values()]
      .sort((a, b) => b.count - a.count || b.files.size - a.files.size).slice(0, 10);

    const byFormat = aggBy(rows, r => r.formatLabel || "(no format)").slice(0, 12);
    const byGroup = aggBy(rows, r => r.group || "(none)");
    const deprecatedUsed = rows.filter(r => r.deprecated).length;
    const bestMatch = rows.filter(r => r.auto && r.match === "best").length;

    const card = (n, label, cls) => `<div class="phive-card phive-card--${cls || ""}"><span class="phive-card__n">${n}</span><span class="phive-card__l">${label}</span></div>`;
    const section = (title, inner, note) => `<section class="phive-an__sec"><h3>${title}${note ? ` <span class="phive-note">${note}</span>` : ""}</h3>${inner}</section>`;

    const outcomeBars = segBars(OUTCOMES.map(o => ({
      key: o.label, total: c[o.k] || 0,
      pass: o.k === "pass" ? c.pass : 0,
      warn: o.k === "warn" ? c.warn : 0,
      fail: o.k === "fail" ? c.fail : 0,
      other: (o.k === "skip" || o.k === "error") ? (c[o.k] || 0) : 0,
    })), Math.max(1, ...Object.values(c)));

    const sevPill = (s) => s === "warning"
      ? `<span class="phive-pill phive-pill--warn">warning</span>`
      : `<span class="phive-pill phive-pill--fail">error</span>`;
    const topErrTable = topErrors.length ? `
      <table class="phive-an__tbl">
        <thead><tr><th>#</th><th>Rule</th><th>Severity</th><th>Occurrences</th><th>Files</th><th>Message</th></tr></thead>
        <tbody>${topErrors.map((e, i) => `
          <tr>
            <td>${i + 1}</td>
            <td><button type="button" class="phive-link" data-rule="${escapeHtml(e.id)}"><code>${escapeHtml(e.id)}</code></button></td>
            <td>${sevPill(e.severity)}</td>
            <td>${e.count}</td><td>${e.files.size}</td>
            <td class="phive-an__msg">${escapeHtml(e.text || "")}</td>
          </tr>`).join("")}</tbody>
      </table>` : `<p class="phive-note">No Schematron findings in the current selection. 🎉</p>`;

    // Processing time. Batch wall-clock comes from the whole run (_timing);
    // per-file latency stats from the filtered selection.
    const durs = rows.map(r => r.ms).filter(n => n > 0).sort((a, b) => a - b);
    const sum = durs.reduce((a, b) => a + b, 0);
    const xsdSum = rows.reduce((a, r) => a + (r.xsdMs || 0), 0);
    const schSum = rows.reduce((a, r) => a + (r.schMs || 0), 0);
    const timeStats = (() => {
      if (!_timing) return "";
      const wall = _timing.validation;
      const thru = wall > 0 ? (_timing.files / (wall / 1000)) : 0;
      const grid = `
        <div class="phive-an__cards">
          ${card(fmtDur(_timing.validation), "Validation time")}
          ${card(thru.toFixed(1) + "/s", "Throughput")}
          ${card(fmtDur(_timing.load), "Engine load")}
          ${card(_timing.workers, "XSD workers")}
          ${card(durs.length ? fmtDur(sum / durs.length) : "—", "Avg / file")}
          ${card(fmtDur(percentile(durs, 50)), "Median / file")}
          ${card(fmtDur(percentile(durs, 95)), "p95 / file")}
        </div>
        ${segBars([
          { key: "XSD (Σ, parallel)", total: Math.round(xsdSum), pass: 0, fail: 0, other: Math.round(xsdSum) },
          { key: "Schematron (Σ, serial)", total: Math.round(schSum), pass: Math.round(schSum), fail: 0, other: 0 },
        ])}
        <p class="phive-note">Per-layer totals are summed across files (ms); XSD runs in
          parallel across workers, so its wall-clock share is much smaller than the sum.</p>`;
      return section("Processing time", grid,
        `batch ${fmtDur(_timing.total)} total (incl. ${fmtDur(_timing.load)} load)`);
    })();

    const fileCap = 500;
    const outcomeBadgeCls = { pass: "ok", warn: "warn", fail: "fail", skip: "skip", error: "error" };
    const outcomeBadgeTxt = { pass: "pass", warn: "warnings", fail: "fail", skip: "skip", error: "error" };
    const fileTable = `
      <table class="phive-an__tbl">
        <thead><tr><th>File</th><th>Format</th><th>XSD</th><th>Schematron</th><th>Outcome</th><th>Errors</th><th>Warnings</th></tr></thead>
        <tbody>${rows.slice(0, fileCap).map(r => `
          <tr>
            <td class="phive-an__file">${escapeHtml(r.name)}</td>
            <td>${r.formatLabel ? `${escapeHtml(r.formatLabel)}${r.deprecated ? ' <span class="phive-tag phive-tag--dep">deprecated</span>' : ""}` : "—"}</td>
            <td>${cell(r.xsd)}</td><td>${cell(r.sch)}</td>
            <td><span class="phive-badge phive-badge--${outcomeBadgeCls[r.outcome] || "error"}">${outcomeBadgeTxt[r.outcome] || r.outcome}</span></td>
            <td>${r.errorCount || ""}</td>
            <td>${r.warnCount ? `<span class="phive-warn">${r.warnCount}</span>` : ""}</td>
          </tr>`).join("")}</tbody>
      </table>${rows.length > fileCap ? `<p class="phive-note">Showing first ${fileCap} of ${rows.length} files.</p>` : ""}`;

    body.innerHTML = `
      <div class="phive-an__cards">
        ${card(total, "Files")}
        ${card(c.pass, "Passed", "ok")}
        ${card(c.warn, "Passed w/ warnings", "warn")}
        ${card(c.fail, "Failed", "fail")}
        ${card(c.skip + c.error, "Skipped/Error", "skip")}
        ${card(validRate + "%", "Pass rate (incl. warn)")}
        ${card(totalWarnings, "Warnings total", "warn")}
        ${card(deprecatedUsed, "Deprecated format")}
        ${card(bestMatch, "Best-match detect", "warn")}
      </div>
      <div class="phive-an__grid">
        ${section("Outcomes", outcomeBars)}
        ${section("By format family (module)", segBars(byGroup))}
      </div>
      ${section("By format", segBars(byFormat), byFormat.length === 12 ? "top 12" : "")}
      ${timeStats}
      ${section("Top 10 failing rules", topErrTable, "click a rule to filter · error vs warning")}
      ${section(`Files (${rows.length})`, fileTable)}`;

    // clicking a rule filters by it
    body.querySelectorAll("[data-rule]").forEach(b =>
      b.addEventListener("click", () => {
        anFilter.rule = b.dataset.rule;
        const sel = anEl.querySelector("#phive-an-rule");
        if (sel) sel.value = b.dataset.rule;
        renderAnalyticsBody();
      }));

    function cell(s) {
      const map = { pass: "ok", warn: "warn", fail: "fail", skip: "skip", na: "skip", error: "fail" };
      const txt = s === "na" ? "—" : s;
      return `<span class="phive-pill phive-pill--${map[s] || "skip"}">${txt}</span>`;
    }
  }

  initUI();
  buildAnalytics();
})();
