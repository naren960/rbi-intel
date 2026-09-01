import { createHash } from "node:crypto";
import { db } from "./schema.js";

export interface DocRow {
  id: string;
  regulator: string;
  doc_type: string;
  title: string;
  date: string;
  department: string | null;
  category: string | null;
  ref_no: string | null;
  source_url: string;
  pdf_url: string | null;
  body: string | null;
  body_hash: string | null;
  status: string;
  first_seen: string | null;
  last_changed: string | null;
  indexed_at: string;
}

export type DocInput = Omit<DocRow, "body_hash" | "first_seen" | "last_changed" | "status"> &
  Partial<Pick<DocRow, "status">>;

export interface UpsertResult {
  inserted: number;
  changed: number;
  unchanged: number;
  changedIds: string[];
}

export function sha256(s: string): string {
  return createHash("sha256").update(s ?? "", "utf8").digest("hex");
}

const stmts: Record<string, any> = {};
function stmt(key: string, sql: string) {
  if (!stmts[key]) stmts[key] = db.prepare(sql);
  return stmts[key];
}

/**
 * node:sqlite requires named-parameter object keys to include the prefix
 * character that matches the SQL placeholder (`@name` → `{ '@name': val }`).
 * better-sqlite3 accepted bare keys (`{ name: val }`) for any of @/:/$name.
 *
 * This helper prefixes every key with `@` to match the SQL in this file,
 * letting the rest of the code stay in the more-readable bare-key style.
 */
function namedParams(obj: Record<string, any>): Record<string, any> {
  const out: Record<string, any> = {};
  for (const [k, v] of Object.entries(obj)) {
    out[`@${k}`] = v;
  }
  return out;
}

/**
 * node:sqlite has no db.transaction() helper.
 * This provides an equivalent: wraps a function in BEGIN/COMMIT with
 * automatic ROLLBACK on error.
 */
function withTransaction<T>(fn: () => T): T {
  db.exec("BEGIN");
  try {
    const result = fn();
    db.exec("COMMIT");
    return result;
  } catch (e) {
    try { db.exec("ROLLBACK"); } catch { /* ignore rollback error */ }
    throw e;
  }
}

/**
 * Insert or update documents, recording a revision whenever the body text
 * actually changed. This is what makes "which directions were amended"
 * answerable — the original package overwrote body in place and lost history.
 */
export function upsertDocs(docs: DocInput[]): UpsertResult {
  const now = new Date().toISOString();
  const res: UpsertResult = { inserted: 0, changed: 0, unchanged: 0, changedIds: [] };

  const getExisting = stmt("getExisting", "SELECT id, body_hash, body, title FROM documents WHERE id = ?");
  const insert = stmt(
    "insertDoc",
    `INSERT INTO documents
       (id, regulator, doc_type, title, date, department, category, ref_no,
        source_url, pdf_url, body, body_hash, status, first_seen, last_changed, indexed_at)
     VALUES
       (@id, @regulator, @doc_type, @title, @date, @department, @category, @ref_no,
        @source_url, @pdf_url, @body, @body_hash, @status, @first_seen, @last_changed, @indexed_at)`
  );
  const update = stmt(
    "updateDoc",
    `UPDATE documents SET
       title=@title, doc_type=@doc_type, date=@date, department=@department,
       category=COALESCE(@category, category), ref_no=COALESCE(@ref_no, ref_no),
       source_url=@source_url, pdf_url=@pdf_url,
       body=@body, body_hash=@body_hash, last_changed=@last_changed, indexed_at=@indexed_at
     WHERE id=@id`
  );
  const touch = stmt("touchDoc", "UPDATE documents SET indexed_at=? WHERE id=?");
  const nextRev = stmt(
    "nextRev",
    "SELECT COALESCE(MAX(revision_no), 0) + 1 AS n FROM document_revisions WHERE doc_id = ?"
  );
  const insertRev = stmt(
    "insertRev",
    `INSERT INTO document_revisions (doc_id, revision_no, body_hash, body, title, captured_at, char_delta)
     VALUES (?, ?, ?, ?, ?, ?, ?)`
  );

  withTransaction(() => {
    for (const d of docs) {
      const body = d.body ?? "";
      const hash = sha256(body);
      const existing = getExisting.get(d.id) as
        | { id: string; body_hash: string | null; body: string | null; title: string }
        | undefined;

      // Built explicitly rather than spread: only include keys that the
      // statement references, to avoid confusion with extra keys.
      const base = {
        id: d.id,
        regulator: d.regulator ?? "RBI",
        doc_type: d.doc_type,
        title: d.title,
        date: d.date ?? "",
        department: d.department ?? null,
        category: d.category ?? null,
        ref_no: d.ref_no ?? null,
        source_url: d.source_url,
        pdf_url: d.pdf_url ?? null,
        body,
        body_hash: hash,
        indexed_at: now,
      };

      if (!existing) {
        insert.run(namedParams({
          ...base,
          status: d.status ?? "active",
          first_seen: now,
          last_changed: now,
        }));
        const n = (nextRev.get(d.id) as { n: number }).n;
        insertRev.run(d.id, n, hash, body, d.title, now, body.length);
        res.inserted++;
        continue;
      }

      if (existing.body_hash === hash && existing.title === d.title) {
        touch.run(now, d.id);
        res.unchanged++;
        continue;
      }

      const delta = body.length - (existing.body?.length ?? 0);
      const { regulator, ...updatable } = base;
      update.run(namedParams({ ...updatable, last_changed: now }));
      const n = (nextRev.get(d.id) as { n: number }).n;
      insertRev.run(d.id, n, hash, body, d.title, now, delta);
      res.changed++;
      res.changedIds.push(d.id);
    }
  });

  return res;
}

export function docExists(id: string): boolean {
  return !!stmt("exists", "SELECT 1 FROM documents WHERE id = ?").get(id);
}

/** Body hash for a doc we already have — lets the scraper skip re-fetching unchanged pages. */
export function knownHash(id: string): string | undefined {
  const r = stmt("knownHash", "SELECT body_hash FROM documents WHERE id = ?").get(id) as
    | { body_hash: string }
    | undefined;
  return r?.body_hash;
}

/**
 * Date and title already stored for a document.
 *
 * Used by materialise() to skip the body fetch for Master Directions and
 * Master Circulars when the listing shows the same date and title as what's
 * already in the database. RBI always bumps the publication date when it
 * amends a direction in place, so matching on date+title is a safe proxy for
 * "body unchanged". The --force flag bypasses this entirely.
 */
export function knownDateTitle(id: string): { date: string; title: string } | undefined {
  const r = stmt("knownDateTitle", "SELECT date, title FROM documents WHERE id = ?").get(id) as
    | { date: string; title: string }
    | undefined;
  return r;
}

/**
 * Mark a document as withdrawn by setting status = 'withdrawn' and recording
 * when/why it was withdrawn. Safe to call multiple times — idempotent.
 */
export function markWithdrawn(opts: {
  id: string;
  reason: string;
  withdrawnDate: string | null;
  detectedAt: string;
}): boolean {
  const r = db
    .prepare(
      `UPDATE documents
         SET status = 'withdrawn',
             withdrawn_reason = @reason,
             withdrawn_date   = @withdrawnDate,
             withdrawn_at     = @detectedAt
       WHERE id = @id AND status != 'withdrawn'`
    )
    .run(namedParams(opts));
  return (r.changes as number) > 0;
}

/**
 * All documents that match a source_url fragment containing the given RBI
 * internal document ID. Used by the withdrawn-circulars scraper to locate
 * documents by the ?Id= parameter shared across all RBI page URLs.
 */
export function docsByRbiId(rbiId: string): { id: string; title: string }[] {
  return db
    .prepare(
      `SELECT id, title FROM documents
       WHERE source_url LIKE @pat1 OR source_url LIKE @pat2`
    )
    .all(namedParams({ pat1: `%Id=${rbiId}&%`, pat2: `%Id=${rbiId}` })) as {
    id: string;
    title: string;
  }[];
}

export function getDoc(id: string): DocRow | undefined {
  return stmt("getDoc", "SELECT * FROM documents WHERE id = ?").get(id) as DocRow | undefined;
}

/**
 * FTS5 query sanitiser.
 *
 * Fix over the original: tokens that reduce to nothing after stripping
 * FTS operator characters produced a bare `""`, which FTS5 rejects with
 * "fts5: syntax error". We drop empty tokens and fall back to a LIKE scan
 * when nothing survives, so a query never hard-errors.
 */
export function escapeFts(q: string): string {
  return q
    .trim()
    // Hyphens and slashes are word separators in RBI prose ("anti-money
    // laundering", "KYC/AML"). Deleting them welded tokens together into
    // strings that match nothing; splitting on them lets the unicode61
    // tokenizer match each part, which is what it already does when indexing.
    .replace(/[-/–—]+/g, " ")
    .split(/\s+/)
    .map((w) => w.replace(/["*^:(){}\[\]]/g, "").trim())
    .filter((w) => w.length > 0)
    .map((w) => `"${w}"`)
    .join(" ");
}

export function searchDocs(opts: {
  query: string;
  docType?: string;
  category?: string;
  status?: string;
  institutionType?: string;
  topic?: string;
  applicability?: string;
  limit?: number;
}): (DocRow & { snippet: string })[] {
  if (!opts.query.trim()) return [];
  const limit = Math.min(opts.limit ?? 10, 50);
  const match = escapeFts(opts.query);

  if (!match) {
    // Every token was punctuation — degrade to a title LIKE scan rather than error.
    let sql = "SELECT *, '' AS snippet FROM documents WHERE title LIKE @like";
    const p: Record<string, any> = { like: `%${opts.query.trim()}%`, limit };
    if (opts.docType) { sql += " AND doc_type = @docType"; p.docType = opts.docType; }
    if (opts.status) { sql += " AND status = @status"; p.status = opts.status; }
    if (opts.institutionType) { sql += " AND institution_type = @inst"; p.inst = opts.institutionType; }
    // Match the primary topic OR any secondary — a document about capital
    // adequacy that scored marginally higher on disclosures is still about
    // capital adequacy, and a filter that only reads the winner hides it.
    if (opts.topic) { sql += " AND (primary_topic = @topic OR secondary_topics LIKE @topicLike)"; p.topic = opts.topic; p.topicLike = `%"${opts.topic}"%`; }
    if (opts.applicability) { sql += " AND applicability = @applic"; p.applic = opts.applicability; }
    sql += " ORDER BY date DESC LIMIT @limit";
    return db.prepare(sql).all(namedParams(p)) as any;
  }

  let sql = `
    SELECT d.*, snippet(documents_fts, 1, '<<', '>>', ' … ', 18) AS snippet
    FROM documents_fts f
    JOIN documents d ON d.rowid = f.rowid
    WHERE documents_fts MATCH @q
  `;
  const params: Record<string, any> = { q: match, limit };
  if (opts.docType) { sql += " AND d.doc_type = @docType"; params.docType = opts.docType; }
  if (opts.category) { sql += " AND d.category LIKE @category"; params.category = `%${opts.category}%`; }
  if (opts.status) { sql += " AND d.status = @status"; params.status = opts.status; }
  if (opts.institutionType) { sql += " AND d.institution_type = @inst"; params.inst = opts.institutionType; }
  if (opts.topic) { sql += " AND (d.primary_topic = @topic OR d.secondary_topics LIKE @topicLike)"; params.topic = opts.topic; params.topicLike = `%"${opts.topic}"%`; }
  if (opts.applicability) { sql += " AND d.applicability = @applic"; params.applic = opts.applicability; }
  sql += " ORDER BY rank, d.date DESC LIMIT @limit";
  return db.prepare(sql).all(namedParams(params)) as any;
}

export function recentDocs(opts: { docType?: string; category?: string; limit?: number }): DocRow[] {
  const limit = Math.min(opts.limit ?? 15, 100);
  let sql = "SELECT * FROM documents WHERE 1=1";
  const params: Record<string, any> = { limit };
  if (opts.docType) { sql += " AND doc_type = @docType"; params.docType = opts.docType; }
  if (opts.category) { sql += " AND category LIKE @category"; params.category = `%${opts.category}%`; }
  sql += " ORDER BY date DESC LIMIT @limit";
  return db.prepare(sql).all(namedParams(params)) as unknown as DocRow[];
}

/** Documents whose text changed, or which first appeared, since a given ISO timestamp. */
export function changesSince(sinceIso: string, limit = 50) {
  return db
    .prepare(
      `SELECT d.id, d.title, d.doc_type, d.category, d.date, d.source_url,
              d.first_seen, d.last_changed, d.status,
              (SELECT COUNT(*) FROM document_revisions r WHERE r.doc_id = d.id) AS revisions,
              /* Classify by evidence, not just by age. A document can both
                 appear and be edited inside the same window; keying off
                 first_seen alone reported that as merely 'new' and silently
                 hid the amendment. More than one stored revision is proof
                 the text actually changed, so that wins. */
              CASE
                WHEN (SELECT COUNT(*) FROM document_revisions r2 WHERE r2.doc_id = d.id) > 1
                     AND d.last_changed >= @since THEN 'amended'
                WHEN d.first_seen >= @since THEN 'new'
                ELSE 'amended'
              END AS change_kind,
              (SELECT char_delta FROM document_revisions r WHERE r.doc_id = d.id
                 ORDER BY revision_no DESC LIMIT 1) AS char_delta
       FROM documents d
       WHERE d.last_changed >= @since OR d.first_seen >= @since
       ORDER BY d.last_changed DESC
       LIMIT @limit`
    )
    .all(namedParams({ since: sinceIso, limit }));
}

/** Does this database carry the v6 enrichment columns? */
export function hasEnrichment(): boolean {
  const cols = db.prepare("PRAGMA table_info(documents)").all() as { name: string }[];
  return cols.some((c) => c.name === "primary_topic");
}

/** Facet counts for the enrichment dimensions, for filter discovery. */
export function listEnrichmentFacets() {
  if (!hasEnrichment()) return null;
  const facet = (col: string, limit = 100) =>
    db
      .prepare(
        `SELECT ${col} AS value, COUNT(*) AS n FROM documents
         WHERE ${col} IS NOT NULL AND ${col} <> ''
         GROUP BY ${col} ORDER BY n DESC LIMIT ?`
      )
      .all(limit) as { value: string; n: number }[];
  return {
    institutionTypes: facet("institution_type"),
    topics: facet("primary_topic"),
    topicGroups: facet("topic_group"),
    applicability: facet("applicability"),
    publicationYears: facet("publication_year", 40),
  };
}

/** Documents RBI has edited in place, most recently updated first. */
export function recentlyUpdatedDocs(limit = 50) {
  if (!hasEnrichment()) return [];
  return db
    .prepare(
      `SELECT id, title, date, updated_date, category, institution_type,
              primary_topic, applicability, status, source_url
       FROM documents WHERE has_update = 1
       ORDER BY updated_date DESC, date DESC LIMIT ?`
    )
    .all(limit);
}

export function listCategories() {
  return db
    .prepare(
      `SELECT category, COUNT(*) AS n
       FROM documents WHERE category IS NOT NULL AND category <> ''
       GROUP BY category ORDER BY n DESC`
    )
    .all();
}

/** Outgoing + incoming edges for one document. */
export function relationsFor(docId: string) {
  const out = db
    .prepare(
      `SELECT r.rel_type, r.dst_id, r.dst_ref_text, r.confidence, r.method, r.evidence,
              d.title AS dst_title, d.date AS dst_date, d.doc_type AS dst_type
       FROM relations r LEFT JOIN documents d ON d.id = r.dst_id
       WHERE r.src_id = ? ORDER BY r.confidence DESC`
    )
    .all(docId);
  const inc = db
    .prepare(
      `SELECT r.rel_type, r.src_id, r.confidence, r.method, r.evidence,
              d.title AS src_title, d.date AS src_date, d.doc_type AS src_type
       FROM relations r JOIN documents d ON d.id = r.src_id
       WHERE r.dst_id = ? ORDER BY d.date DESC`
    )
    .all(docId);
  return { outgoing: out, incoming: inc };
}

export function revisionsFor(docId: string) {
  return db
    .prepare(
      `SELECT revision_no, body_hash, title, captured_at, char_delta,
              LENGTH(body) AS body_length
       FROM document_revisions WHERE doc_id = ? ORDER BY revision_no DESC`
    )
    .all(docId);
}

export function getRevisionBody(docId: string, revisionNo: number) {
  return db
    .prepare("SELECT body, title, captured_at FROM document_revisions WHERE doc_id = ? AND revision_no = ?")
    .get(docId, revisionNo) as { body: string; title: string; captured_at: string } | undefined;
}

export function clausesFor(docId: string, limit = 500) {
  return db
    .prepare("SELECT id, clause_label, chapter, seq, text, needs_review FROM clauses WHERE doc_id = ? ORDER BY seq LIMIT ?")
    .all(docId, limit);
}

export function docCount() {
  return db
    .prepare("SELECT doc_type, status, COUNT(*) AS n FROM documents GROUP BY doc_type, status ORDER BY n DESC")
    .all() as { doc_type: string; status: string; n: number }[];
}

export function relationCount() {
  return db
    .prepare(
      `SELECT rel_type,
              SUM(CASE WHEN dst_id IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
              SUM(CASE WHEN dst_id IS NULL THEN 1 ELSE 0 END) AS unresolved
       FROM relations GROUP BY rel_type`
    )
    .all();
}

export function getSyncMeta(key: string): string | undefined {
  const row = stmt("getMeta", "SELECT value FROM sync_meta WHERE key = ?").get(key) as { value: string } | undefined;
  return row?.value;
}

export function setSyncMeta(key: string, value: string): void {
  stmt("setMeta", "INSERT INTO sync_meta (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(
    key,
    value
  );
}

export function startSyncRun(source: string): number {
  const r = db
    .prepare("INSERT INTO sync_runs (started_at, source) VALUES (?, ?)")
    .run(new Date().toISOString(), source);
  // node:sqlite returns lastInsertRowid as bigint; Number() converts it safely.
  return Number(r.lastInsertRowid);
}

export function finishSyncRun(id: number, newDocs: number, changedDocs: number, errors?: string) {
  db.prepare("UPDATE sync_runs SET finished_at=?, new_docs=?, changed_docs=?, errors=? WHERE id=?").run(
    new Date().toISOString(),
    newDocs,
    changedDocs,
    errors ?? null,
    id
  );
}

/* ==================================================================== */
/* Compliance layer (schema v5)                                          */
/*                                                                       */
/* `requirements` is grounded — each row paraphrases clause text that     */
/* exists in the source document. `req_mappings` is not: it is seeded     */
/* scaffolding drafted with no access to any real policy register.       */
/* These functions keep the two joinable but never merge them into one   */
/* flat object, so a caller cannot accidentally serve an assessment      */
/* without the provenance that qualifies it.                             */
/* ==================================================================== */

export type RequirementRow = {
  id: string;
  doc_id: string;
  clause_id: string;
  clause_label: string;
  chapter: string | null;
  clause_title: string | null;
  requirement: string;
  obligation_type: string | null;
  branch_relevance: string | null;
  applicability: string | null;
  timeline: string | null;
  keywords: string | null;
  doc_title: string;
  doc_status: string;
  business_area: string | null;
  business_area_name: string | null;
  policy: string | null;
  process: string | null;
  control: string | null;
  control_type: string | null;
  owner_process_role: string | null;
  owner_control_role: string | null;
  evidence_required: string | null;
  classification: string | null;
  finding: string | null;
  recommendation: string | null;
  severity: string | null;
  provenance: string | null;
};

const REQ_SELECT = `
  SELECT r.id, r.doc_id, r.clause_id, c.clause_label, c.chapter,
         r.clause_title, r.requirement, r.obligation_type, r.branch_relevance,
         r.applicability, r.timeline, r.keywords,
         d.title AS doc_title, d.status AS doc_status,
         m.business_area, b.name AS business_area_name,
         m.policy, m.process, m.control, m.control_type,
         op.role AS owner_process_role, oc.role AS owner_control_role,
         m.evidence_required, m.classification, m.finding, m.recommendation,
         m.severity, m.provenance
  FROM requirements r
  JOIN clauses c   ON c.id = r.clause_id
  JOIN documents d ON d.id = r.doc_id
  LEFT JOIN req_mappings m    ON m.req_id = r.id
  LEFT JOIN business_areas b  ON b.id = m.business_area
  LEFT JOIN owners op         ON op.id = m.owner_process
  LEFT JOIN owners oc         ON oc.id = m.owner_control
`;

/** Does this database have the v5 compliance tables at all? */
export function hasComplianceLayer(): boolean {
  const r = db
    .prepare("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='req_mappings'")
    .get() as { n: number };
  return r.n > 0;
}

export function requirementsFor(
  opts: {
    docId?: string;
    obligationType?: string;
    classification?: string;
    severity?: string;
    businessArea?: string;
    query?: string;
    limit?: number;
  } = {}
): RequirementRow[] {
  const where: string[] = [];
  const params: unknown[] = [];

  if (opts.docId) { where.push("r.doc_id = ?"); params.push(opts.docId); }
  if (opts.obligationType) { where.push("r.obligation_type = ?"); params.push(opts.obligationType); }
  if (opts.classification) { where.push("m.classification = ?"); params.push(opts.classification); }
  if (opts.severity) { where.push("m.severity = ?"); params.push(opts.severity); }
  if (opts.businessArea) { where.push("m.business_area = ?"); params.push(opts.businessArea); }

  if (opts.query?.trim()) {
    // LIKE rather than FTS: `requirements` has no FTS index of its own, and the
    // set is small enough (hundreds per document) that a scan is cheaper than
    // maintaining a second index. Search the grounded clause text too, so a
    // query in the regulation's own vocabulary still hits when the model's
    // paraphrase chose different words.
    where.push(
      "(r.requirement LIKE ? OR r.clause_title LIKE ? OR r.keywords LIKE ? OR c.text LIKE ?)"
    );
    const like = `%${opts.query.trim()}%`;
    params.push(like, like, like, like);
  }

  const sql =
    REQ_SELECT +
    (where.length ? ` WHERE ${where.join(" AND ")}` : "") +
    " ORDER BY r.doc_id, c.seq LIMIT ?";
  params.push(opts.limit ?? 100);

  return db.prepare(sql).all(...(params as any[])) as RequirementRow[];
}

export function requirementById(id: string): RequirementRow | undefined {
  return db.prepare(REQ_SELECT + " WHERE r.id = ?").get(id) as RequirementRow | undefined;
}

export function complianceSummary(docId?: string) {
  const scope = docId ? " WHERE r.doc_id = ?" : "";
  const p = docId ? [docId] : [];

  const byClassification = db
    .prepare(
      `SELECT COALESCE(m.classification, '(not assessed)') AS classification,
              COUNT(*) AS n
       FROM requirements r LEFT JOIN req_mappings m ON m.req_id = r.id${scope}
       GROUP BY 1 ORDER BY n DESC`
    )
    .all(...(p as any[])) as { classification: string; n: number }[];

  const bySeverity = db
    .prepare(
      `SELECT COALESCE(m.severity, '(none)') AS severity, COUNT(*) AS n
       FROM requirements r LEFT JOIN req_mappings m ON m.req_id = r.id${scope}
       GROUP BY 1 ORDER BY n DESC`
    )
    .all(...(p as any[])) as { severity: string; n: number }[];

  const byProvenance = db
    .prepare(
      `SELECT COALESCE(m.provenance, '(not scaffolded)') AS provenance, COUNT(*) AS n
       FROM requirements r LEFT JOIN req_mappings m ON m.req_id = r.id${scope}
       GROUP BY 1 ORDER BY n DESC`
    )
    .all(...(p as any[])) as { provenance: string; n: number }[];

  const byArea = db
    .prepare(
      `SELECT COALESCE(b.name, '(unassigned)') AS business_area, COUNT(*) AS n
       FROM requirements r
       LEFT JOIN req_mappings m ON m.req_id = r.id
       LEFT JOIN business_areas b ON b.id = m.business_area${scope}
       GROUP BY 1 ORDER BY n DESC LIMIT 25`
    )
    .all(...(p as any[])) as { business_area: string; n: number }[];

  const total = byClassification.reduce((s, c) => s + c.n, 0);
  return { total, byClassification, bySeverity, byProvenance, byArea };
}

export function requirementCount(): number {
  const r = db.prepare("SELECT COUNT(*) AS n FROM requirements").get() as { n: number };
  return r.n;
}
