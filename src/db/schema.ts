/**
 * Shared SQLite schema — the contract between the Node ingestion/MCP layer
 * and the Python analysis layer.
 *
 * Ownership:
 *   Node writes:   documents, document_revisions, md_categories, sync_meta, sync_runs
 *   Python writes: clauses, relations, requirements, req_mappings
 *   Reference data: business_areas, owners  (seeded once by `npm run init`)
 *   Both read:     everything
 *
 * Migrations are versioned via PRAGMA user_version so an existing
 * ~/.india-reg-mcp/regdata.db can be upgraded in place without data loss.
 *
 * Uses node:sqlite (built into Node ≥22.5) — no native compilation required.
 */
import { DatabaseSync } from "node:sqlite";
import { homedir } from "node:os";
import { join, dirname } from "node:path";
import { mkdirSync, existsSync } from "node:fs";

const DEFAULT_DIR = join(homedir(), ".rbi-intel");

/** Override with RBI_INTEL_DB=/path/to/regdata.db — used by tests and by the Python layer. */
export const DB_PATH = process.env.RBI_INTEL_DB ?? join(DEFAULT_DIR, "regdata.db");

const dir = dirname(DB_PATH);
if (!existsSync(dir)) mkdirSync(dir, { recursive: true });

export const db = new DatabaseSync(DB_PATH);

// node:sqlite has no .pragma() helper — use raw SQL instead.
db.exec("PRAGMA journal_mode = WAL");
db.exec("PRAGMA foreign_keys = ON");
db.exec("PRAGMA busy_timeout = 10000");

const SCHEMA_VERSION = 6;

export function initSchema(): void {
  // node:sqlite returns { user_version: number } from PRAGMA queries.
  const row = db.prepare("PRAGMA user_version").get() as { user_version: number };
  const current = row.user_version;

  if (current < 1) migrateV1();
  if (current < 2) migrateV2();
  if (current < 3) migrateV3();
  if (current < 4) migrateV4();
  if (current < 5) migrateV5();
  if (current < 6) migrateV6();

  db.exec(`PRAGMA user_version = ${SCHEMA_VERSION}`);
}

/** v1 — core document store, compatible with the original india-reg-mcp layout. */
function migrateV1() {
  db.exec(`
    CREATE TABLE IF NOT EXISTS documents (
      id           TEXT PRIMARY KEY,
      regulator    TEXT NOT NULL DEFAULT 'RBI',
      doc_type     TEXT NOT NULL,
      title        TEXT NOT NULL,
      date         TEXT NOT NULL,
      department   TEXT,
      source_url   TEXT NOT NULL,
      pdf_url      TEXT,
      body         TEXT,
      indexed_at   TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_regulator ON documents(regulator);
    CREATE INDEX IF NOT EXISTS idx_doctype   ON documents(doc_type);
    CREATE INDEX IF NOT EXISTS idx_date      ON documents(date);

    CREATE TABLE IF NOT EXISTS sync_meta (
      key   TEXT PRIMARY KEY,
      value TEXT
    );
  `);
}

/**
 * v2 — the columns the original package was missing, and which make
 * amendment/withdrawal tracking possible at all.
 *
 *  - body_hash    : detects that RBI silently edited a Master Direction in place
 *  - status       : active | superseded | repealed | withdrawn
 *  - category     : RBI's own regulated-entity grouping (Commercial Banks, NBFC, ...)
 *  - first_seen / last_changed : the temporal spine for "what changed this week"
 */
function migrateV2() {
  addColumn("documents", "body_hash", "TEXT");
  addColumn("documents", "status", "TEXT NOT NULL DEFAULT 'active'");
  addColumn("documents", "category", "TEXT");
  addColumn("documents", "ref_no", "TEXT");
  addColumn("documents", "first_seen", "TEXT");
  addColumn("documents", "last_changed", "TEXT");

  db.exec(`
    CREATE INDEX IF NOT EXISTS idx_status   ON documents(status);
    CREATE INDEX IF NOT EXISTS idx_category ON documents(category);
    CREATE INDEX IF NOT EXISTS idx_refno    ON documents(ref_no);
    CREATE INDEX IF NOT EXISTS idx_changed  ON documents(last_changed);

    /* Every distinct body text RBI has served for a document.
       Master Directions are edited in place — without this the prior
       text is destroyed on re-sync and amendments become undetectable. */
    CREATE TABLE IF NOT EXISTS document_revisions (
      doc_id      TEXT NOT NULL,
      revision_no INTEGER NOT NULL,
      body_hash   TEXT NOT NULL,
      body        TEXT,
      title       TEXT,
      captured_at TEXT NOT NULL,
      char_delta  INTEGER,
      PRIMARY KEY (doc_id, revision_no),
      FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
    );

    /* RBI's Master Directions page groups documents by regulated-entity
       category. That grouping IS a hierarchy level and the original
       package discarded it entirely. */
    CREATE TABLE IF NOT EXISTS md_categories (
      doc_id    TEXT NOT NULL,
      category  TEXT NOT NULL,
      PRIMARY KEY (doc_id, category),
      FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
    );

    /* Directed edges between documents. dst_id is NULL when the reference
       was found in text but could not be resolved to an indexed document —
       those are kept deliberately, they are the backlog worth chasing. */
    CREATE TABLE IF NOT EXISTS relations (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      src_id       TEXT NOT NULL,
      dst_id       TEXT,
      dst_ref_text TEXT,
      rel_type     TEXT NOT NULL,
      evidence     TEXT,
      confidence   REAL NOT NULL DEFAULT 0.5,
      method       TEXT NOT NULL DEFAULT 'regex',
      created_at   TEXT NOT NULL,
      FOREIGN KEY (src_id) REFERENCES documents(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_rel_src  ON relations(src_id);
    CREATE INDEX IF NOT EXISTS idx_rel_dst  ON relations(dst_id);
    CREATE INDEX IF NOT EXISTS idx_rel_type ON relations(rel_type);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_rel_uniq
      ON relations(src_id, rel_type, COALESCE(dst_id, ''), COALESCE(dst_ref_text, ''));

    CREATE TABLE IF NOT EXISTS sync_runs (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      started_at  TEXT NOT NULL,
      finished_at TEXT,
      source      TEXT NOT NULL,
      new_docs    INTEGER DEFAULT 0,
      changed_docs INTEGER DEFAULT 0,
      errors      TEXT
    );
  `);
}

/** v3 — Python-owned analysis tables + the FTS index (rebuilt, never assumed in sync). */
function migrateV3() {
  db.exec(`
    CREATE TABLE IF NOT EXISTS clauses (
      id           TEXT PRIMARY KEY,
      doc_id       TEXT NOT NULL,
      clause_label TEXT NOT NULL,
      chapter      TEXT,
      seq          INTEGER NOT NULL,
      text         TEXT NOT NULL,
      needs_review INTEGER NOT NULL DEFAULT 0,
      FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_clause_doc ON clauses(doc_id);

    CREATE TABLE IF NOT EXISTS requirements (
      id              TEXT PRIMARY KEY,
      clause_id       TEXT NOT NULL,
      doc_id          TEXT NOT NULL,
      clause_title    TEXT,
      requirement     TEXT NOT NULL,
      obligation_type TEXT,
      applicability   TEXT,
      timeline        TEXT,
      keywords        TEXT,
      extracted_at    TEXT NOT NULL,
      model           TEXT,
      FOREIGN KEY (clause_id) REFERENCES clauses(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_req_doc ON requirements(doc_id);

    DROP TRIGGER IF EXISTS documents_ai;
    DROP TRIGGER IF EXISTS documents_ad;
    DROP TRIGGER IF EXISTS documents_au;

    CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
      title, body,
      content='documents',
      content_rowid='rowid',
      tokenize='porter unicode61'
    );

    CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
      INSERT INTO documents_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
    END;
    CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
      INSERT INTO documents_fts(documents_fts, rowid, title, body) VALUES ('delete', old.rowid, old.title, old.body);
    END;
    CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN
      INSERT INTO documents_fts(documents_fts, rowid, title, body) VALUES ('delete', old.rowid, old.title, old.body);
      INSERT INTO documents_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
    END;
  `);

  // The original package dropped and recreated documents_ai on every start,
  // which silently desyncs FTS if any write landed while the trigger was gone.
  // Rebuilding once at migration time guarantees the index matches the table.
  rebuildFts();
}

/**
 * v4 — Withdrawal tracking.
 *
 * RBI publishes a separate page listing circulars that have been withdrawn:
 *   /Scripts/NotificationUserWithdrawnCircular.aspx
 *
 * Three columns are added to `documents`:
 *   withdrawn_reason  — who withdrew it ("RRA 2.0", "Department of Regulation", etc.)
 *   withdrawn_date    — the date recorded by RBI on the withdrawn page (may differ from
 *                       the original publication date)
 *   withdrawn_at      — ISO timestamp of when our system detected the withdrawal
 *
 * status = 'withdrawn' is set by markWithdrawn() in queries.ts; the existing status
 * column (active | superseded | repealed | withdrawn) already supports this.
 */
function migrateV4() {
  addColumn("documents", "withdrawn_reason", "TEXT");
  addColumn("documents", "withdrawn_date",   "TEXT");
  addColumn("documents", "withdrawn_at",     "TEXT");
}

/**
 * v5 — the compliance layer, ported from the standalone Python pipeline
 * (03_extract_requirements.py / 04_scaffold_internal_layer.py / schema.py).
 *
 * The single most important decision here is that the internal mapping lives
 * in its OWN table rather than as columns on `requirements`.
 *
 * `requirements` is grounded: every row paraphrases text that exists in the
 * source document and can be checked against it. `req_mappings` is not — it
 * is a first-draft RACI sketched before any real policy register exists, and
 * the original pipeline was emphatic that the two must never be confused.
 * A separate table with a mandatory `provenance` column makes that structural
 * rather than a matter of remembering to read the metadata: you cannot select
 * a mapping without also selecting the word 'seeded'.
 *
 * Also adds `documents.source` so locally-ingested PDFs (the only path that
 * works from a network that cannot reach rbi.org.in) are distinguishable from
 * scraped ones rather than silently claiming the same provenance.
 */
function migrateV5() {
  addColumn("documents", "source", "TEXT NOT NULL DEFAULT 'rbi'");

  db.exec(`
    /* Reference data. Seeded from seed/*.json by \`npm run init\`; small,
       hand-curated, and pointed at by req_mappings foreign keys. */
    CREATE TABLE IF NOT EXISTS business_areas (
      id          TEXT PRIMARY KEY,
      name        TEXT NOT NULL,
      description TEXT
    );

    CREATE TABLE IF NOT EXISTS owners (
      id   TEXT PRIMARY KEY,
      role TEXT NOT NULL,
      line TEXT
    );

    /* The internal layer. One row per requirement, at most.
       provenance: 'seeded'   — model-drafted placeholder, NOT evidence
                   'reviewed' — a human has checked it against something real
                   'sourced'  — reconciled against an actual policy register */
    CREATE TABLE IF NOT EXISTS req_mappings (
      req_id              TEXT PRIMARY KEY,
      business_area       TEXT,
      business_area_guess TEXT,
      policy              TEXT,
      process             TEXT,
      control             TEXT,
      control_type        TEXT,
      owner_process       TEXT,
      owner_control       TEXT,
      evidence_required   TEXT,
      classification      TEXT,
      finding             TEXT,
      recommendation      TEXT,
      severity            TEXT,
      provenance          TEXT NOT NULL DEFAULT 'seeded',
      model               TEXT,
      created_at          TEXT NOT NULL,
      FOREIGN KEY (req_id)        REFERENCES requirements(id)   ON DELETE CASCADE,
      FOREIGN KEY (business_area) REFERENCES business_areas(id),
      FOREIGN KEY (owner_process) REFERENCES owners(id),
      FOREIGN KEY (owner_control) REFERENCES owners(id)
    );

    CREATE INDEX IF NOT EXISTS idx_map_area  ON req_mappings(business_area);
    CREATE INDEX IF NOT EXISTS idx_map_class ON req_mappings(classification);
    CREATE INDEX IF NOT EXISTS idx_map_prov  ON req_mappings(provenance);

    /* branch_relevance was a first-class field in the Python schema; the v3
       requirements table only had 'applicability'. Keep both — applicability
       is "who does this apply to", branch_relevance is "how directly does a
       branch execute it", and collapsing them loses a real distinction. */
  `);

  addColumn("requirements", "branch_relevance", "TEXT");
  addColumn("requirements", "needs_review", "INTEGER NOT NULL DEFAULT 0");
}

/**
 * v6 — document enrichment (gaps D.2–D.6 / P1–P3 in GAP_ANALYSIS.md).
 *
 * Classification derived from the document TITLE by `seed/taxonomy.json`,
 * which both `src/util/taxonomy.ts` and `python/rbi_intel/taxonomy.py` read.
 *
 *   institution_type   normalised regulated-entity class ("Commercial Banks")
 *                      — distinct from `category`, which is RBI's own listing
 *                      grouping and is whatever RBI's page happened to say
 *   primary_topic      highest-scoring subject from the topic dictionary
 *   secondary_topics   JSON array of the remaining matches, ranked
 *   topic_scores       JSON object of topic -> keyword hit count, kept so a
 *                      surprising classification can be explained rather than
 *                      just disbelieved
 *   applicability      Applicable | Likely Applicable | Not Applicable
 *   applicability_rule which rule fired, for the same reason
 *
 * Plus the D.3 date columns, promoted out of `sync_meta` now that there is a
 * classifier writing them on every path:
 *   updated_date       parsed from "(Updated as on ...)" in the title
 *   publication_year   integer year of `date`, for cheap year facets
 *   has_update         1 when RBI has edited the document in place
 *
 * All are nullable and all are recomputable: `python -m rbi_intel enrich
 * --force` rebuilds every row from the titles already stored. Nothing here is
 * a source of truth, so a taxonomy change is never a migration.
 */
function migrateV6() {
  addColumn("documents", "institution_type",   "TEXT");
  addColumn("documents", "primary_topic",      "TEXT");
  addColumn("documents", "secondary_topics",   "TEXT");
  addColumn("documents", "topic_scores",       "TEXT");
  addColumn("documents", "topic_group",        "TEXT");
  addColumn("documents", "applicability",      "TEXT");
  addColumn("documents", "applicability_rule", "TEXT");
  addColumn("documents", "updated_date",       "TEXT");
  addColumn("documents", "publication_year",   "INTEGER");
  addColumn("documents", "has_update",         "INTEGER NOT NULL DEFAULT 0");
  addColumn("documents", "enriched_at",        "TEXT");

  db.exec(`
    CREATE INDEX IF NOT EXISTS idx_inst_type   ON documents(institution_type);
    CREATE INDEX IF NOT EXISTS idx_topic       ON documents(primary_topic);
    CREATE INDEX IF NOT EXISTS idx_applic      ON documents(applicability);
    CREATE INDEX IF NOT EXISTS idx_pub_year    ON documents(publication_year);
    CREATE INDEX IF NOT EXISTS idx_updated     ON documents(updated_date);
  `);
}

export function rebuildFts(): void {
  db.exec(`INSERT INTO documents_fts(documents_fts) VALUES ('rebuild');`);
}

function addColumn(table: string, column: string, type: string) {
  const cols = db.prepare(`PRAGMA table_info(${table})`).all() as { name: string }[];
  if (!cols.some((c) => c.name === column)) {
    db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${type}`);
  }
}
