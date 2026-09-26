/* Replay contract fixtures through the JavaScript port.
 *
 *   node tests/run_cases.js data/model.json tests/fixtures/cases-*.json
 *
 * Prints one JSON object per line: {"file": ..., "case": i, "rows": [...]}.
 * tests/test_contract.py compares those rows to the Python ones. This file does
 * no asserting of its own -- the Python side owns the comparison so there is only
 * one definition of "identical".
 */
"use strict";
const fs = require("fs");
const path = require("path");
const M = require(path.join(__dirname, "..", "web", "app.js"));

const [modelPath, ...files] = process.argv.slice(2);
if (!modelPath || !files.length) {
  console.error("usage: node tests/run_cases.js <model.json> <cases.json...>");
  process.exit(2);
}
const model = new M.Model(JSON.parse(fs.readFileSync(modelPath, "utf8")));

for (const f of files) {
  const cases = JSON.parse(fs.readFileSync(f, "utf8"));
  cases.forEach((c, i) => {
    const rows = M.computeRows(c.now, c.preds, c.vehicles, model, c.walk, c.qs,
                               c.horizon, c.berths, c.slots, new Set(c.skipped));
    process.stdout.write(JSON.stringify({file: path.basename(f), case: i,
                                         rows: rows}) + "\n");
  });
}
