/**
 * The headline capability: detecting that RBI edited a Master Direction
 * in place. The original package overwrote `body` on every sync, so this
 * was undetectable — the prior text was simply destroyed.
 *
 * Simulates two syncs of the same document with different text and asserts
 * that a revision is recorded, the change is reported, and both versions
 * remain readable.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

process.env.RBI_INTEL_DB = "/tmp/amend.db";
const { rmSync } = await import("node:fs");
for (const s of ["", "-wal", "-shm"]) { try { rmSync(`/tmp/amend.db${s}`); } catch {} }

const { initSchema } = await import("../src/db/schema.js");
const q = await import("../src/db/queries.js");
const { isoDaysAgo } = await import("../src/util/date.js");

initSchema();

const base = {
  id: "rbi:md:99001",
  regulator: "RBI",
  doc_type: "master_direction",
  title: "Master Direction - Test Direction, 2024",
  date: "2024-04-01",
  department: "Department of Regulation",
  category: "Commercial Banks",
  ref_no: "DOR.TST.REC.1/00.00.001/2024-25",
  source_url: "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=99001",
  pdf_url: null,
  indexed_at: new Date().toISOString(),
};

test("first sync inserts and records revision 1", () => {
  const r = q.upsertDocs([{ ...base, body: "1. Banks shall maintain a minimum ratio of nine per cent." }]);
  assert.equal(r.inserted, 1);
  assert.equal(r.changed, 0);
  assert.equal(q.revisionsFor(base.id).length, 1);
});

test("re-syncing identical text records no new revision", () => {
  const r = q.upsertDocs([{ ...base, body: "1. Banks shall maintain a minimum ratio of nine per cent." }]);
  assert.equal(r.inserted, 0);
  assert.equal(r.changed, 0);
  assert.equal(r.unchanged, 1);
  assert.equal(q.revisionsFor(base.id).length, 1, "unchanged text must not create a revision");
});

test("RBI editing the text in place is detected as an amendment", () => {
  const r = q.upsertDocs([
    { ...base, body: "1. Banks shall maintain a minimum ratio of eleven per cent, with effect from April 1, 2026." },
  ]);
  assert.equal(r.changed, 1);
  assert.deepEqual(r.changedIds, [base.id]);

  const revs = q.revisionsFor(base.id) as any[];
  assert.equal(revs.length, 2, "a second revision must be stored");
  assert.ok(revs[0].char_delta > 0, "character delta should be recorded");
});

test("both versions of the text remain retrievable", () => {
  const before = q.getRevisionBody(base.id, 1);
  const after = q.getRevisionBody(base.id, 2);
  assert.ok(before?.body.includes("nine per cent"), "original text must survive the overwrite");
  assert.ok(after?.body.includes("eleven per cent"));
});

test("the change feed reports it as amended, not new", () => {
  const rows = q.changesSince(isoDaysAgo(1)) as any[];
  const row = rows.find((r) => r.id === base.id);
  assert.ok(row, "document should appear in the change feed");
  assert.equal(row.change_kind, "amended");
  assert.equal(row.revisions, 2);
});

test("full-text search reflects the updated body, not the old one", () => {
  const hitNew = q.searchDocs({ query: "eleven per cent" });
  const hitOld = q.searchDocs({ query: "nine per cent" });
  assert.ok(hitNew.some((h) => h.id === base.id), "FTS must index the new text");
  assert.ok(!hitOld.some((h) => h.id === base.id), "FTS must not still match the superseded text");
});

test("hyphenated and slashed queries tokenise rather than weld", () => {
  q.upsertDocs([
    { ...base, id: "rbi:nt:99002", doc_type: "circular", title: "Anti-Money Laundering / KYC update",
      body: "Guidance on anti-money laundering standards and KYC/AML compliance for regulated entities." },
  ]);
  assert.ok(q.searchDocs({ query: "anti-money laundering" }).length > 0, "hyphenated phrase should match");
  assert.ok(q.searchDocs({ query: "KYC/AML" }).length > 0, "slashed phrase should match");
  assert.equal(q.searchDocs({ query: "-- ** ::" }).length, 0, "degenerate query returns empty, not an exception");
});
