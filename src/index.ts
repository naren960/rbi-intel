#!/usr/bin/env node
/**
 * rbi-intel — MCP server over the RBI regulatory corpus.
 *
 * Tools fall into three groups:
 *   retrieval  — search / get / recent / categories        (Node-populated tables)
 *   lineage    — relationships, revisions, change feed     (Python-populated tables)
 *   ops        — sync, status
 *
 * Nothing here scrapes on demand except sync_latest.
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { initSchema } from "./db/schema.js";
import * as q from "./db/queries.js";
import { syncMasterDirections, syncNotifications } from "./scrapers/rbi.js";
import { DISCLAIMER, ok, err, emptyDbMsg } from "./util/format.js";
import { isoDaysAgo } from "./util/date.js";

initSchema();

const server = new McpServer({ name: "rbi-intel", version: "3.0.0" });

const DOC_TYPES = [
  "circular",
  "master_direction",
  "master_circular",
  "amendment",
  "notification",
  "regulation",
  "draft",
] as const;

const slim = (d: q.DocRow) => {
  const anyd = d as any;
  const out: Record<string, unknown> = {
    id: d.id,
    type: d.doc_type,
    title: d.title,
    date: d.date,
    category: d.category,
    status: d.status,
    ref_no: d.ref_no,
    source: d.source_url,
  };
  // Enrichment (schema v6) — omitted entirely on an un-migrated database
  // rather than emitted as a row of nulls that reads like "no topic".
  if (anyd.primary_topic) {
    out.institution_type = anyd.institution_type;
    out.topic = anyd.primary_topic;
    out.applicability = anyd.applicability;
    if (anyd.updated_date) out.updated_date = anyd.updated_date;
  }
  return out;
};

/* ================================================================== */
/* Retrieval                                                           */
/* ================================================================== */

server.tool(
  "search_regulations",
  "Full-text search across indexed RBI documents (circulars, Master Directions, notifications, amendments). " +
    "Returns title, date, type, a highlighted snippet and the official source link. " +
    "Use this to answer 'what are the rules on X' with cited primary sources.",
  {
    query: z.string().describe("Search terms, e.g. 'digital lending', 'KYC periodic updation', 'provisioning NPA'"),
    doc_type: z.enum(DOC_TYPES).optional().describe("Restrict to one document type. master_direction = consolidated current rules."),
    category: z.string().optional().describe("Restrict to an RBI category, e.g. 'Commercial Banks', 'NBFC'"),
    status: z.enum(["active", "superseded", "repealed", "withdrawn"]).optional(),
    institution_type: z.string().optional()
      .describe("Normalised regulated-entity class, e.g. 'Commercial Banks', 'Urban Co-operative Banks'. " +
        "Different from `category`, which is RBI's own listing grouping. Call list_topics for valid values."),
    topic: z.string().optional()
      .describe("Regulatory subject, e.g. 'Capital Adequacy', 'KYC / AML'. Matches primary OR secondary topic. " +
        "Call list_topics for valid values."),
    applicability: z.enum(["Applicable", "Likely Applicable", "Not Applicable"]).optional()
      .describe("Whether the document binds a commercial bank. Derived from the title by a rule set — " +
        "a filter for triage, not a legal determination."),
    limit: z.number().default(10).describe("Max results (1-50)"),
  },
  async ({ query, doc_type, category, status, institution_type, topic, applicability, limit }) => {
    try {
      if (!q.docCount().length) return emptyDbMsg();
      const results = q.searchDocs({
        query, docType: doc_type, category, status,
        institutionType: institution_type, topic, applicability, limit,
      });
      return ok({
        query,
        resultCount: results.length,
        results: results.map((r) => ({ ...slim(r), snippet: r.snippet })),
        note: "Use get_document for full text, get_lineage for what amended or superseded a document.",
        disclaimer: DISCLAIMER,
      });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

server.tool(
  "get_document",
  "Retrieve the full text of a document by id, together with its metadata, revision count and official link.",
  {
    id: z.string().describe("Document id, e.g. 'rbi:md:13136' or 'rbi:nt:13675'"),
    max_chars: z.number().default(12000).describe("Truncate body to this many characters"),
  },
  async ({ id, max_chars }) => {
    try {
      const doc = q.getDoc(id);
      if (!doc) return err(`No document with id ${id}. Use search_regulations to find valid ids.`);
      const revs = q.revisionsFor(id) as any[];
      const body =
        doc.body && doc.body.length > max_chars
          ? doc.body.slice(0, max_chars) + `\n\n[... truncated at ${max_chars} chars — full text at ${doc.source_url} ...]`
          : doc.body;
      return ok({
        ...slim(doc),
        department: doc.department,
        pdf: doc.pdf_url,
        first_seen: doc.first_seen,
        last_changed: doc.last_changed,
        revision_count: revs.length,
        body,
        disclaimer: DISCLAIMER,
      });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

server.tool(
  "get_recent",
  "Most recently dated RBI documents, optionally filtered by type or category. Answers 'what has RBI issued lately'.",
  {
    doc_type: z.enum(DOC_TYPES).optional(),
    category: z.string().optional(),
    limit: z.number().default(15).describe("Max results (1-100)"),
  },
  async ({ doc_type, category, limit }) => {
    try {
      if (!q.docCount().length) return emptyDbMsg();
      return ok({ results: q.recentDocs({ docType: doc_type, category, limit }).map(slim), disclaimer: DISCLAIMER });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

server.tool(
  "list_master_directions",
  "List RBI Master Directions — the consolidated, currently-in-force rules on each subject. " +
    "Best starting point for the current state of regulation on a topic. Optionally filter by category.",
  { category: z.string().optional().describe("e.g. 'Commercial Banks', 'NBFC', 'Payment and Settlement'") },
  async ({ category }) => {
    try {
      if (!q.docCount().length) return emptyDbMsg();
      const md = q.recentDocs({ docType: "master_direction", category, limit: 100 });
      const mc = q.recentDocs({ docType: "master_circular", category, limit: 100 });
      const all = [...md, ...mc].sort((a, b) => (b.date || "").localeCompare(a.date || ""));
      return ok({ count: all.length, documents: all.map(slim), disclaimer: DISCLAIMER });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

server.tool(
  "list_categories",
  "List the RBI category groupings present in the index (Commercial Banks, NBFC, Payment and Settlement System, ...) with document counts. Use this to discover valid values for the `category` filter.",
  {},
  async () => {
    try {
      return ok({ categories: q.listCategories(), disclaimer: DISCLAIMER });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

server.tool(
  "search_by_topic",
  "Topic search returning BOTH the consolidated Master Directions AND the recent circulars/amendments on a subject, " +
    "so you get the current baseline plus what has changed since. Best tool for 'give me everything on X'.",
  { topic: z.string().describe("e.g. 'digital lending', 'NBFC capital adequacy', 'KYC', 'gold loan'") },
  async ({ topic }) => {
    try {
      if (!q.docCount().length) return emptyDbMsg();
      const masters = [
        ...q.searchDocs({ query: topic, docType: "master_direction", limit: 5 }),
        ...q.searchDocs({ query: topic, docType: "master_circular", limit: 3 }),
      ];
      const amendments = q.searchDocs({ query: topic, docType: "amendment", limit: 8 });
      const related = q.searchDocs({ query: topic, limit: 12 });
      return ok({
        topic,
        consolidatedRules: masters.map(slim),
        amendments: amendments.map(slim),
        relatedDocuments: related.map((r) => ({ ...slim(r), snippet: r.snippet })),
        guidance:
          "Start with consolidatedRules for the baseline. Then call get_lineage on each to see what has amended it. " +
          "amendments lists documents whose title marks them as amending something.",
        disclaimer: DISCLAIMER,
      });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

/* ================================================================== */
/* Lineage — reads the tables the Python layer writes                  */
/* ================================================================== */

server.tool(
  "get_lineage",
  "Show the regulatory lineage of a document: what it supersedes, amends, repeals or withdraws (outgoing), " +
    "and what has since amended, superseded or repealed it (incoming). This is how you tell whether a rule is still current. " +
    "Requires the relationship graph to have been built (`python -m rbi_intel relations`).",
  { id: z.string().describe("Document id, e.g. 'rbi:md:13136'") },
  async ({ id }) => {
    try {
      const doc = q.getDoc(id);
      if (!doc) return err(`No document with id ${id}.`);
      const { outgoing, incoming } = q.relationsFor(id);
      const supersededBy = (incoming as any[]).filter((r) => ["supersedes", "repeals", "withdraws"].includes(r.rel_type));
      return ok({
        document: slim(doc),
        currency:
          supersededBy.length > 0
            ? `SUPERSEDED / REPEALED by ${supersededBy.length} later document(s) — do not treat as current without checking.`
            : "No superseding document found in the index. Note: absence of an edge is not proof a rule is current.",
        outgoing,
        incoming,
        unresolvedReferences: (outgoing as any[]).filter((r) => !r.dst_id).map((r) => r.dst_ref_text),
        disclaimer: DISCLAIMER,
      });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

server.tool(
  "get_change_feed",
  "What changed in the RBI corpus recently: newly published documents, and existing documents whose text RBI edited in place " +
    "(the usual way a Master Direction gets amended). Each entry says whether it is 'new' or 'amended' and by how many characters.",
  {
    days: z.number().default(30).describe("Look back this many days"),
    limit: z.number().default(50),
  },
  async ({ days, limit }) => {
    try {
      const rows = q.changesSince(isoDaysAgo(days), limit) as any[];
      return ok({
        window_days: days,
        newDocuments: rows.filter((r) => r.change_kind === "new"),
        amendedDocuments: rows.filter((r) => r.change_kind === "amended"),
        note: "'amended' means the body text served by RBI differs from what we last stored. Use diff_revisions to see what changed.",
        disclaimer: DISCLAIMER,
      });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

server.tool(
  "list_revisions",
  "List every stored revision of a document, with capture timestamp and character delta. " +
    "Master Directions are edited in place by RBI; this is the audit trail of those edits.",
  { id: z.string() },
  async ({ id }) => {
    try {
      const revs = q.revisionsFor(id);
      if (!revs.length) return err(`No revisions stored for ${id}.`);
      return ok({ id, revisions: revs, disclaimer: DISCLAIMER });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

server.tool(
  "diff_revisions",
  "Return the text of two stored revisions of a document so the difference can be read directly. " +
    "Use list_revisions first to see which revision numbers exist.",
  {
    id: z.string(),
    from_revision: z.number().describe("Earlier revision number"),
    to_revision: z.number().describe("Later revision number"),
    max_chars: z.number().default(8000),
  },
  async ({ id, from_revision, to_revision, max_chars }) => {
    try {
      const a = q.getRevisionBody(id, from_revision);
      const b = q.getRevisionBody(id, to_revision);
      if (!a) return err(`Revision ${from_revision} of ${id} not found.`);
      if (!b) return err(`Revision ${to_revision} of ${id} not found.`);
      const cut = (s: string) => (s && s.length > max_chars ? s.slice(0, max_chars) + "\n[... truncated ...]" : s);
      return ok({
        id,
        from: { revision: from_revision, captured_at: a.captured_at, title: a.title, body: cut(a.body) },
        to: { revision: to_revision, captured_at: b.captured_at, title: b.title, body: cut(b.body) },
        char_delta: (b.body?.length ?? 0) - (a.body?.length ?? 0),
        disclaimer: DISCLAIMER,
      });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

server.tool(
  "get_clauses",
  "Return the clause-level breakdown of a document, as produced by the Python chunking layer. " +
    "Use when a question is about a specific numbered clause rather than the document as a whole.",
  { id: z.string(), limit: z.number().default(200) },
  async ({ id, limit }) => {
    try {
      const rows = q.clausesFor(id, limit) as any[];
      if (!rows.length)
        return ok({
          id,
          clauses: [],
          message: "No clauses stored. Run `python -m rbi_intel chunk --doc " + id + "` to produce them.",
        });
      return ok({ id, count: rows.length, clauses: rows, disclaimer: DISCLAIMER });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);



server.tool(
  "list_topics",
  "The enrichment vocabulary: valid values for the institution_type, topic and applicability filters, " +
    "with document counts. Call this before filtering so the filter values are real ones rather than guesses.",
  {},
  async () => {
    try {
      const facets = q.listEnrichmentFacets();
      if (!facets)
        return ok({
          message:
            "This database predates schema v6. Run `npm run init` to migrate, then " +
            "`python -m rbi_intel enrich` to classify the documents already indexed.",
        });
      const classified = facets.topics.reduce((sum, t) => sum + t.n, 0);
      const unclassified = facets.topics.find((t) => t.value === "Unclassified")?.n ?? 0;
      return ok({
        institutionTypes: facets.institutionTypes,
        topicGroups: facets.topicGroups,
        topics: facets.topics,
        applicability: facets.applicability,
        publicationYears: facets.publicationYears,
        coverage: {
          classified,
          unclassified,
          note:
            "Classification is derived from the document TITLE by a keyword taxonomy " +
            "(seed/taxonomy.json). It is a triage aid: 'Not Applicable' means no rule matched " +
            "this bank, not that the document has been legally assessed as inapplicable.",
        },
        disclaimer: DISCLAIMER,
      });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

server.tool(
  "list_recently_updated",
  "Master Directions RBI has edited in place, most recently updated first, using the " +
    "'(Updated as on ...)' date parsed out of the title. This is the cheapest answer to " +
    "'which consolidated rules changed recently' — it needs no revision history, so it works " +
    "even for documents indexed only once.",
  { limit: z.number().default(25) },
  async ({ limit }) => {
    try {
      const rows = q.recentlyUpdatedDocs(limit) as any[];
      if (!rows.length)
        return ok({
          documents: [],
          message:
            "No documents carry an update date. Either none have been re-dated by RBI, or " +
            "enrichment has not run — try `python -m rbi_intel enrich --force`.",
        });
      return ok({
        count: rows.length,
        documents: rows.map((r) => ({
          id: r.id, title: r.title, published: r.date, updated: r.updated_date,
          institution_type: r.institution_type, topic: r.primary_topic,
          applicability: r.applicability, status: r.status, source: r.source_url,
        })),
        note: "The update date is RBI's own, taken from the title. Use list_revisions to see " +
          "text changes this index actually captured.",
        disclaimer: DISCLAIMER,
      });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

/* ================================================================== */
/* Compliance layer                                                    */
/*                                                                     */
/* Two layers with very different standing are exposed here, and the   */
/* difference is the whole point:                                      */
/*                                                                     */
/*   requirements  — paraphrased from clause text that exists in the   */
/*                   document. Checkable against the source.           */
/*   req_mappings  — a first-draft RACI and gap assessment drafted     */
/*                   with no access to any real policy register.       */
/*                                                                     */
/* Every response that carries a mapping also carries SEEDED_WARNING.  */
/* An assistant reading this output should never report a seeded       */
/* classification as the bank's compliance position.                   */
/* ================================================================== */

const SEEDED_WARNING =
  "The `internal` block on each requirement (policy / process / control / owner / " +
  "assessment) is SEEDED: model-drafted placeholder scaffolding with no evidence " +
  "behind it, not a statement about this bank. Only rows with provenance " +
  "'reviewed' or 'sourced' have been checked by a person. Do not report a seeded " +
  "classification as the bank's actual compliance position.";

const NO_LAYER_MSG =
  "This database has no compliance layer. Run `npm run init` to migrate it to " +
  "schema v5, then `python -m rbi_intel chunk`, `extract` and `scaffold`.";

/** Split a row into its grounded half and its seeded half, deliberately. */
const shapeRequirement = (r: q.RequirementRow) => {
  let keywords: string[] = [];
  try {
    keywords = JSON.parse(r.keywords ?? "[]");
  } catch {
    keywords = [];
  }
  const out: Record<string, unknown> = {
    id: r.id,
    document: { id: r.doc_id, title: r.doc_title, status: r.doc_status },
    clause: r.clause_label,
    chapter: r.chapter,
    title: r.clause_title,
    // Grounded — derived from the clause text.
    requirement: r.requirement,
    obligation_type: r.obligation_type,
    branch_relevance: r.branch_relevance,
    applies_to: r.applicability,
    timeline: r.timeline,
    keywords,
  };
  if (r.provenance) {
    out.internal = {
      provenance: r.provenance,
      business_area: r.business_area_name,
      policy: r.policy,
      process: r.process,
      control: r.control,
      control_type: r.control_type,
      owner_process: r.owner_process_role,
      owner_control: r.owner_control_role,
      evidence_required: r.evidence_required,
      assessment: {
        classification: r.classification,
        finding: r.finding,
        recommendation: r.recommendation,
        severity: r.severity,
      },
    };
  }
  return out;
};

server.tool(
  "get_requirements",
  "Search the clause-level regulatory requirements extracted from RBI directions. " +
    "Use this — not search_regulations — when the question is about what a bank must actually DO: " +
    "obligations, deadlines, thresholds, who owns a control. Returns a grounded `requirement` field " +
    "plus, where it exists, a SEEDED `internal` block that must not be treated as evidence of compliance.",
  {
    query: z.string().optional().describe("Free text; matches title, paraphrase, keywords and the source clause text"),
    document_id: z.string().optional().describe("Restrict to one document, e.g. rbi:md:12798"),
    obligation_type: z
      .enum(["Governance", "Process", "Reporting", "Screening", "Timeline", "Record-keeping", "Assurance"])
      .optional(),
    classification: z
      .enum(["Compliant", "Partially Compliant", "Gap", "Not Applicable", "To Be Confirmed"])
      .optional()
      .describe("Filter on the SEEDED assessment — only meaningful for exploring the scaffolding"),
    severity: z.enum(["High", "Medium", "Low"]).optional(),
    limit: z.number().default(25),
  },
  async ({ query, document_id, obligation_type, classification, severity, limit }) => {
    try {
      if (!q.hasComplianceLayer()) return ok({ requirements: [], message: NO_LAYER_MSG });
      const rows = q.requirementsFor({
        query,
        docId: document_id,
        obligationType: obligation_type,
        classification,
        severity,
        limit,
      });
      if (!rows.length) {
        return ok({
          requirements: [],
          message:
            q.requirementCount() === 0
              ? "No requirements extracted yet. Run `python -m rbi_intel extract`."
              : "No requirements matched those filters.",
        });
      }
      const shaped = rows.map(shapeRequirement);
      const anySeeded = rows.some((r) => r.provenance === "seeded");
      return ok({
        count: shaped.length,
        requirements: shaped,
        ...(anySeeded ? { seededLayerWarning: SEEDED_WARNING } : {}),
        disclaimer: DISCLAIMER,
      });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

server.tool(
  "get_requirement",
  "Fetch one extracted requirement in full by its id (e.g. rbi:md:12798#CHIV-A-106), " +
    "including the verbatim source clause it was derived from so the paraphrase can be checked.",
  { id: z.string() },
  async ({ id }) => {
    try {
      if (!q.hasComplianceLayer()) return err(NO_LAYER_MSG);
      const row = q.requirementById(id);
      if (!row) return err(`No requirement with id ${id}. Ids look like rbi:md:12798#CHIV-A-106.`);
      const clause = (q.clausesFor(row.doc_id, 100000) as any[]).find((c) => c.id === row.clause_id);
      return ok({
        ...shapeRequirement(row),
        source_clause_text: clause?.text ?? null,
        ...(row.provenance === "seeded" ? { seededLayerWarning: SEEDED_WARNING } : {}),
        disclaimer: DISCLAIMER,
      });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

server.tool(
  "compliance_summary",
  "Counts of extracted requirements broken down by seeded assessment, severity, provenance and " +
    "business area — overall or for one document. Use it to see coverage and where the scaffolding " +
    "still needs human review, not to report a compliance position.",
  { document_id: z.string().optional() },
  async ({ document_id }) => {
    try {
      if (!q.hasComplianceLayer()) return ok({ message: NO_LAYER_MSG });
      const s = q.complianceSummary(document_id);
      if (!s.total) return ok({ total: 0, message: "No requirements extracted yet." });
      const seeded = s.byProvenance.find((p) => p.provenance === "seeded")?.n ?? 0;
      return ok({
        scope: document_id ?? "all documents",
        ...s,
        ...(seeded ? { seededLayerWarning: SEEDED_WARNING } : {}),
        disclaimer: DISCLAIMER,
      });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

/* ================================================================== */
/* Ops                                                                 */
/* ================================================================== */

server.tool(
  "sync_latest",
  "Refresh the index from rbi.org.in. Always re-fetches the Master Directions listing (RBI edits those in place, " +
    "so re-fetching is how amendments are detected) and optionally the last N months of notifications. " +
    "Takes minutes — it scrapes politely.",
  {
    months_back: z.number().default(2).describe("Months of notification history to check. 0 = Master Directions only."),
    master_directions: z.boolean().default(true).describe("Also refresh the Master Directions listing"),
  },
  async ({ months_back, master_directions }) => {
    const log: string[] = [];
    const push = (m: string) => log.push(m);
    const runId = q.startSyncRun("mcp:sync_latest");
    let inserted = 0;
    let changed = 0;
    try {
      if (master_directions) {
        const r = await syncMasterDirections(push);
        inserted += r.inserted;
        changed += r.changed;
      }
      if (months_back > 0) {
        const r = await syncNotifications(months_back, push);
        inserted += r.inserted;
        changed += r.changed;
      }
      q.setSyncMeta("last_sync", new Date().toISOString());
      q.finishSyncRun(runId, inserted, changed);
      return ok({
        message: "Sync complete.",
        newDocuments: inserted,
        amendedDocuments: changed,
        nextStep:
          changed || inserted
            ? "Run `python -m rbi_intel relations` to refresh the relationship graph over the new text."
            : "Nothing changed.",
        log,
        disclaimer: DISCLAIMER,
      });
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      q.finishSyncRun(runId, inserted, changed, msg);
      return err(`${msg}\n\nLog:\n${log.join("\n")}`);
    }
  }
);

server.tool(
  "sync_status",
  "Index health: document counts by type and status, relationship counts, and when the index was last synced.",
  {},
  async () => {
    try {
      const counts = q.docCount();
      return ok({
        totalDocuments: counts.reduce((s, c) => s + c.n, 0),
        byTypeAndStatus: counts,
        relationships: q.relationCount(),
        categories: q.listCategories().length,
        lastSync: q.getSyncMeta("last_sync") ?? "never",
        relationsBuiltAt: q.getSyncMeta("relations_built_at") ?? "never",
      });
    } catch (e) {
      return err(e instanceof Error ? e.message : String(e));
    }
  }
);

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("rbi-intel MCP server running on stdio");
