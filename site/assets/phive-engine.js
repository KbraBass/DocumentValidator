/* phive-web validation engine (shared by the main thread + Web Workers).
 *
 * This is the CPU-heavy half — XSD validation (xmllint-wasm / libxml2) and
 * Schematron transformation (Saxon-JS HE). It deliberately does NOT touch the
 * DOM (no DOMParser), so it runs unchanged inside a Web Worker, where the
 * worker pool gives true multi-core parallelism for large batches. The
 * DOM-bound bits — reading the root element / Customization ID, parsing the
 * SVRL output, and rendering — stay on the main thread in phive-validator.js.
 *
 * `validateFormat(xmlText, fmt)` returns the XSD result plus the RAW SVRL
 * string(s); the caller parses the SVRL into failures.
 */

export function createValidator(paths) {
  const assetUrl = (rel) => new URL(rel, paths.assetsBase).toString();
  let _saxon, _xmllint;
  const _xsdCache = {};        // xsd entry rel → { entryBase, files }
  const _sefCache = {};        // sef url → decompressed text (transient)
  const _sefInternal = {};     // sef url → parsed SEF object (reused per transform)

  // Saxon-JS is a classic script that attaches to the global object. On the
  // main thread we inject a <script>; in a worker (no document) we fetch and
  // eval it into the worker global.
  async function loadSaxon() {
    if (_saxon) return _saxon;
    if (globalThis.SaxonJS) return (_saxon = globalThis.SaxonJS);
    if (typeof document !== "undefined") {
      await new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = paths.saxon;
        s.onload = resolve;
        s.onerror = () => reject(new Error("Saxon-JS load failed"));
        document.head.appendChild(s);
      });
    } else {
      const code = await (await fetch(paths.saxon)).text();
      (0, eval)(code);   // indirect eval → runs in global scope, sets globalThis.SaxonJS
    }
    if (!globalThis.SaxonJS) throw new Error("SaxonJS not on global after load");
    return (_saxon = globalThis.SaxonJS);
  }

  async function loadXmllint() {
    if (_xmllint) return _xmllint;
    return (_xmllint = await import(paths.xmllint));
  }

  // Walk the entry schema's transitive schemaLocation graph, flatten every
  // file to its basename (xmllint-wasm's VFS doesn't auto-create nested dirs),
  // rewrite refs to basenames, hand the flat set to validateXML.
  async function loadXsd(entryRel) {
    if (_xsdCache[entryRel]) return _xsdCache[entryRel];
    const entryAbs = assetUrl(entryRel);
    const seen = new Map();
    async function walk(u) {
      if (seen.has(u)) return;
      const r = await fetch(u);
      if (!r.ok) { seen.set(u, ""); return; }
      const txt = await r.text();
      seen.set(u, txt);
      for (const m of txt.matchAll(/schemaLocation="([^"]+)"/g)) {
        if (/^(https?:|urn:)/.test(m[1])) continue;
        try { await walk(new URL(m[1], u).toString()); } catch (e) { /* skip */ }
      }
    }
    await walk(entryAbs);
    const flat = [];
    for (const [u, raw] of seen.entries()) {
      if (!raw) continue;
      const base = u.split("/").pop();
      const rewritten = raw.replace(/schemaLocation="([^"]+)"/g,
        (_, ref) => `schemaLocation="${ref.split("/").pop()}"`);
      flat.push({ fileName: base, contents: rewritten });
    }
    const entryBase = entryAbs.split("/").pop();
    return (_xsdCache[entryRel] = { entryBase, files: flat });
  }

  // Fetch a SEF, transparently gunzipping `.sef.json.gz` (sniff the magic so
  // it also works if a host auto-decodes the response).
  async function fetchSef(u) {
    if (_sefCache[u]) return _sefCache[u];
    const r = await fetch(u);
    if (!r.ok) throw new Error(`SEF ${r.status}`);
    const buf = new Uint8Array(await r.arrayBuffer());
    let text;
    if (buf[0] === 0x1f && buf[1] === 0x8b) {
      const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
      text = await new Response(stream).text();
    } else {
      text = new TextDecoder().decode(buf);
    }
    return (_sefCache[u] = text);
  }

  // Parse a SEF to a JS object ONCE and reuse it. Passing Saxon-JS a
  // pre-parsed SEF via `stylesheetInternal` skips the (multi-MB) JSON.parse +
  // SEF load that `stylesheetText` would redo on every single file — the
  // dominant per-file cost when validating a batch of one format.
  async function sefObject(u) {
    if (_sefInternal[u]) return _sefInternal[u];
    const obj = JSON.parse(await fetchSef(u));
    delete _sefCache[u];            // keep only the parsed form
    return (_sefInternal[u] = obj);
  }

  async function validateXsd(xmllint, xmlText, fmt) {
    if (!fmt.xsd) return { skipped: "format ships no XSD layer" };
    const { entryBase, files } = await loadXsd(fmt.xsd);
    const schema = files.find(f => f.fileName === entryBase);
    if (!schema) return { passed: false, errors: [`entry schema not found: ${fmt.xsd}`] };
    const preload = files.filter(f => f.fileName !== entryBase);
    const out = await xmllint.validateXML({
      xml: [{ fileName: "doc.xml", contents: xmlText }],
      schema: [{ fileName: entryBase, contents: schema.contents }],
      preload,
    });
    if (out.valid) return { passed: true, errors: [] };
    const errs = (out.errors || []).map(e => e.message || String(e));
    if (!errs.length && out.rawOutput) errs.push(out.rawOutput);
    return { passed: false, errors: errs };
  }

  // Returns { skipped } or { svrls: [{ ruleset, svrl }] } — RAW output; the
  // caller (main thread) parses the SVRL into failed-assert records.
  async function validateSchematron(saxon, xmlText, fmt) {
    if (!fmt.schematron || !fmt.schematron.length) return { skipped: "no Schematron rulesets" };
    const svrls = [];
    for (const sefRel of fmt.schematron) {
      const sefAbs = assetUrl(sefRel);
      let sef;
      try { sef = await sefObject(sefAbs); }
      catch (e) { return { skipped: `SEF fetch failed: ${sefRel}`, error: true }; }
      let svrl;
      try {
        const result = await saxon.transform({
          stylesheetInternal: sef,    // reuse the parsed SEF (compile-once)
          sourceText: xmlText,
          destination: "serialized",
          baseOutputURI: paths.assetsBase,
        }, "async");
        svrl = typeof result.principalResult === "string"
          ? result.principalResult
          : (result.principalResult?.serialize?.() ?? String(result.principalResult));
      } catch (err) {
        // The layer was supposed to run but couldn't — an ERROR, not a clean
        // skip. The caller must NOT treat this as a pass.
        return { skipped: "Saxon-JS error: " + (err.message || err), error: true };
      }
      svrls.push({ ruleset: sefRel.split("/").pop(), svrl });
    }
    return { svrls };
  }

  return {
    async init() { await Promise.all([loadSaxon(), loadXmllint()]); },
    // XSD only — DOM-free, so this is what runs inside Web Workers.
    async initXsd() { await loadXmllint(); },
    async validateXsdOnly(xmlText, fmt) {
      return validateXsd(await loadXmllint(), xmlText, fmt);
    },
    // Schematron only (Saxon-JS) — needs the DOM, so it stays on the main thread.
    async validateSchematronOnly(xmlText, fmt) {
      return validateSchematron(await loadSaxon(), xmlText, fmt);
    },
    // Both, in-thread (the no-Workers fallback path).
    async validateFormat(xmlText, fmt) {
      const xmllint = await loadXmllint();
      const saxon = await loadSaxon();
      const xsd = await validateXsd(xmllint, xmlText, fmt);
      const sch = await validateSchematron(saxon, xmlText, fmt);
      return { xsd, sch };
    },
  };
}
