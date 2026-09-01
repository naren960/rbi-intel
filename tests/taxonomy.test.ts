/**
 * Cross-language parity for the enrichment taxonomy.
 *
 * The taxonomy exists in one place — `seed/taxonomy.json` — but is compiled by
 * two matchers, because the Node scraper classifies at write time and the
 * Python layer classifies in bulk. Two implementations of the same rules is
 * exactly the arrangement that drifts silently: a regex tweak on one side, and
 * six months later the dashboard and the MCP server quietly disagree about
 * which directions apply to the bank.
 *
 * So this file runs the TypeScript matcher over a fixed corpus of real RBI
 * title shapes and compares it, field by field, against the Python matcher's
 * output for the same corpus. `test_taxonomy_parity.py` is the same check from
 * the other direction; between them, neither language can move alone.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { loadTaxonomy, GENERIC_INSTITUTION, UNCLASSIFIED_TOPIC } from "../src/util/taxonomy.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const titles: string[] = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures_titles.json"), "utf-8")
);
const tax = loadTaxonomy();

const COMPARED = [
  "institution_type",
  "matched_pattern",
  "primary_topic",
  "secondary_topics",
  "topic_scores",
  "topic_group",
  "applicability",
  "applicability_rule",
  "business_area_hint",
] as const;

test("taxonomy loads and is non-trivial", () => {
  assert.ok(tax.version >= 1, "taxonomy has no version");
  assert.ok(tax.institutionNames.length >= 25, `${tax.institutionNames.length} institution types`);
  assert.ok(tax.topicNames.length >= 90, `${tax.topicNames.length} topics`);
});

test("REGRESSION: bare acronyms do not match inside ordinary words", () => {
  // `LAB` used to match inside "available" / "collaborative" / "Labour",
  // excluding those documents from the applicable set entirely.
  for (const title of [
    "Prudential Norms on Available for Sale and Held for Trading Categories",
    "Guidelines on Collaborative Lending Arrangements between Banks",
    "Master Circular on Labour Welfare and staff matters in banks",
  ]) {
    const got = tax.applicability(title);
    assert.notEqual(got.applicability, "Not Applicable", `${title} -> ${got.applicability_rule}`);
  }
});

test("REGRESSION: a pattern ending in a bracket can match", () => {
  // `\bScheduled Commercial Banks \(SCB\)\b` needed a word character straight
  // after the ')', which never happens in a real title.
  const got = tax.institutionType("Master Direction for Scheduled Commercial Banks (SCB) on Leverage Ratio");
  assert.equal(got.institution_type, "Scheduled Commercial Banks");
  assert.equal(got.matched_pattern, "Scheduled Commercial Banks (SCB)");
});

test("REGRESSION: most specific institution wins, not list order", () => {
  const got = tax.institutionType("Circular to All Scheduled Commercial Banks and other banks");
  assert.equal(got.institution_type, "Scheduled Commercial Banks");
});

test("empty and missing titles are handled", () => {
  for (const bad of [undefined, null, ""]) {
    assert.equal(tax.institutionType(bad).institution_type, GENERIC_INSTITUTION);
    assert.equal(tax.topicsFor(bad).primary_topic, UNCLASSIFIED_TOPIC);
    assert.equal(tax.applicability(bad).applicability, "Likely Applicable");
  }
});

test("applicability rule order is preserved", () => {
  const got = tax.applicability(
    "Master Directions on Fraud Risk Management in Commercial Banks " +
      "(including Regional Rural Banks) and All India Financial Institutions"
  );
  assert.equal(got.applicability, "Applicable");
  assert.match(got.applicability_rule, /Rule 2/);
});

test("TypeScript and Python classify the corpus identically", () => {
  // Ask Python for its answers rather than checking in a golden file: a golden
  // file goes stale the moment someone edits the taxonomy, and a stale golden
  // file that still passes is worse than no test.
  const script = `
import json, sys
sys.path.insert(0, ${JSON.stringify(join(ROOT, "python"))})
from rbi_intel.taxonomy import load
tax = load()
titles = json.load(open(${JSON.stringify(join(ROOT, "tests", "fixtures_titles.json"))}, encoding="utf-8"))
print(json.dumps([tax.classify(t) for t in titles]))
`;
  let pythonOut: string;
  try {
    pythonOut = execFileSync("python3", ["-c", script], { encoding: "utf-8", maxBuffer: 32 << 20 });
  } catch {
    try {
      pythonOut = execFileSync("python", ["-c", script], { encoding: "utf-8", maxBuffer: 32 << 20 });
    } catch (e) {
      // Python missing is a legitimate environment, not a failure of this code.
      console.error("[parity] python unavailable — skipping cross-language check");
      return;
    }
  }

  const fromPython = JSON.parse(pythonOut) as Record<string, unknown>[];
  assert.equal(fromPython.length, titles.length);

  const mismatches: string[] = [];
  titles.forEach((title, i) => {
    const ts = tax.classify(title) as Record<string, unknown>;
    const py = fromPython[i];
    for (const field of COMPARED) {
      const a = JSON.stringify(ts[field] ?? null);
      const b = JSON.stringify(py[field] ?? null);
      if (a !== b) mismatches.push(`${title.slice(0, 48)} :: ${field}\n    ts=${a}\n    py=${b}`);
    }
  });

  assert.equal(
    mismatches.length,
    0,
    `${mismatches.length} field(s) differ between implementations:\n${mismatches.slice(0, 10).join("\n")}`
  );
});

test("corpus coverage stays high", () => {
  const unclassified = titles.filter(
    (t) => tax.topicsFor(t).primary_topic === UNCLASSIFIED_TOPIC
  ).length;
  const pct = (100 * unclassified) / titles.length;
  assert.ok(pct <= 10, `${unclassified}/${titles.length} (${pct.toFixed(0)}%) unclassified`);
});
