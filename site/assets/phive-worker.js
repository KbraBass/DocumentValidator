/* Validation worker — one per CPU core in the pool (phive-validator.js).
 *
 * A module worker so it can `import` the shared engine + xmllint-wasm ESM.
 * It owns the parallelisable, DOM-free work — XSD validation (xmllint-wasm).
 * Schematron (Saxon-JS) needs the DOM and so stays on the main thread; the
 * main thread also does the inspection / SVRL parsing / rendering. Messages:
 *   ← { type:'init', paths }                  → { type:'ready' } | { type:'error' }
 *   ← { type:'xsd', id, xmlText, fmt }         → { type:'result', id, xsd } | { type:'result', id, error }
 */
import { createValidator } from "./phive-engine.js";

let validator = null;

self.onmessage = async (e) => {
  const msg = e.data;
  if (msg.type === "init") {
    try {
      validator = createValidator(msg.paths);
      await validator.initXsd();          // xmllint only — no Saxon/DOM in a worker
      self.postMessage({ type: "ready" });
    } catch (err) {
      self.postMessage({ type: "error", error: String((err && err.message) || err) });
    }
    return;
  }
  if (msg.type === "xsd") {
    try {
      const xsd = await validator.validateXsdOnly(msg.xmlText, msg.fmt);
      self.postMessage({ type: "result", id: msg.id, xsd });
    } catch (err) {
      self.postMessage({ type: "result", id: msg.id, error: String((err && err.message) || err) });
    }
  }
};
