import TurndownService from "turndown";
import pLimit from "p-limit";
import { politeFetch, sleep } from "../util/http.js";
import { extractPdfText, cleanText } from "./pdf.js";
import {
  parseNotificationList,
  parseMasterDirectionList,
  parseMasterCircularList,
  parseAmendmentDirectionList,
  parseStandaloneCircularList,
  parseGuidanceNoteList,
  parseWithdrawnCirculars,
  extractBodyHtml,
  findPdfUrl,
  type ListItem,
  type ParseReport,
} from "./parse.js";
import { upsertDocs, knownHash, knownDateTitle, markWithdrawn, docsByRbiId, type DocInput } from "../db/queries.js";

const td = new TurndownService({ headingStyle: "atx", codeBlockStyle: "fenced" });
const limit = pLimit(5);

const NOTIF_URL = "https://www.rbi.org.in/Scripts/NotificationUser.aspx";
const MD_URL    = "https://www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx";
const MC_URL    = "https://www.rbi.org.in/Scripts/BS_ViewMasterCirculardetails.aspx";
const SC_URL    = "https://www.rbi.org.in/Scripts/BS_ViewListofstandalonecirculars.aspx";
const AMD_URL   = "https://www.rbi.org.in/Scripts/Fs_AmendmentDirections.aspx";
const WD_URL    = "https://www.rbi.org.in/Scripts/NotificationUserWithdrawnCircular.aspx";

export type Logger = (msg: string) => void;

interface Tokens { vs: string; vsg: string; ev: string }

async function fetchViewstateTokens(): Promise<Tokens> {
  const res = await politeFetch(NOTIF_URL);
  const html = await res.text();
  const tokens = {
    vs: html.match(/id="__VIEWSTATE"\s+value="([^"]*)"/)?.[1] ?? "",
    vsg: html.match(/id="__VIEWSTATEGENERATOR"\s+value="([^"]*)"/)?.[1] ?? "",
    ev: html.match(/id="__EVENTVALIDATION"\s+value="([^"]*)"/)?.[1] ?? "",
  };
  if (!tokens.vs) {
    throw new Error(
      "Could not read __VIEWSTATE from NotificationUser.aspx. RBI may have changed the page, " +
        "or a proxy returned an interstitial. Run `npm run doctor` for detail."
    );
  }
  return tokens;
}

/* ------------------------------------------------------------------ */
/* Listings                                                            */
/* ------------------------------------------------------------------ */

export async function fetchNotificationMonth(year: number, month: number, tokens?: Tokens): Promise<ParseReport> {
  const t = tokens ?? (await fetchViewstateTokens());
  const body = new URLSearchParams({
    __VIEWSTATE: t.vs,
    __VIEWSTATEGENERATOR: t.vsg,
    __EVENTVALIDATION: t.ev,
    hdnYear: String(year),
    hdnMonth: String(month),
    "UsrFontCntr$btn": "",
  }).toString();

  const res = await politeFetch(NOTIF_URL, {
    method: "POST",
    body,
    headers: { "Content-Type": "application/x-www-form-urlencoded", Referer: NOTIF_URL },
  });
  return parseNotificationList(await res.text());
}

export async function fetchMasterDirectionList(): Promise<ParseReport> {
  const res = await politeFetch(MD_URL, { timeoutMs: 45_000 });
  return parseMasterDirectionList(await res.text());
}

export async function fetchMasterCircularList(): Promise<ParseReport> {
  const res = await politeFetch(MC_URL, { timeoutMs: 45_000 });
  return parseMasterCircularList(await res.text());
}

export async function fetchAmendmentDirectionList(): Promise<ParseReport> {
  const res = await politeFetch(AMD_URL, { timeoutMs: 45_000 });
  return parseAmendmentDirectionList(await res.text());
}

export async function fetchStandaloneCircularList(): Promise<ParseReport> {
  const res = await politeFetch(SC_URL, { timeoutMs: 45_000 });
  return parseStandaloneCircularList(await res.text());
}

/* ------------------------------------------------------------------ */
/* Bodies                                                              */
/* ------------------------------------------------------------------ */

export async function fetchBody(item: ListItem): Promise<{ text: string; via: string; pdfUrl: string | null }> {
  let pdfUrl = item.pdfUrl;
  try {
    const res = await politeFetch(item.htmlUrl);
    const html = await res.text();
    if (!pdfUrl) pdfUrl = findPdfUrl(html);

    const { html: bodyHtml, strategy } = extractBodyHtml(html);
    const markdown = bodyHtml ? cleanText(td.turndown(bodyHtml)) : "";

    // RBI serves many documents as a stub page wrapping a PDF. Anything this
    // short is a stub, not a document.
    if (markdown.length >= 400) return { text: markdown, via: `html:${strategy}`, pdfUrl };

    if (pdfUrl) {
      const pdfText = await extractPdfText(pdfUrl);
      if (pdfText.length > markdown.length) return { text: pdfText, via: "pdf", pdfUrl };
    }
    return { text: markdown, via: `html:${strategy}:short`, pdfUrl };
  } catch (e) {
    if (pdfUrl) {
      const pdfText = await extractPdfText(pdfUrl);
      return { text: pdfText, via: "pdf:html-failed", pdfUrl };
    }
    return { text: "", via: `failed:${e instanceof Error ? e.message : String(e)}`, pdfUrl };
  }
}

/* ------------------------------------------------------------------ */
/* Classification                                                      */
/* ------------------------------------------------------------------ */

/**
 * Order matters. "Amendment to Master Direction on X" is an amending
 * circular, not a Master Direction — the original package's substring
 * check classified it as the latter and it would then pollute
 * list_master_directions with documents that are not consolidated rules.
 *
 * New source pages added in Phase 1:
 *   "mc"  → Master Circulars listing — always master_circular
 *   "sc"  → Standalone Circulars listing — always circular
 *   "amd" → Amendment Directions listing — always amendment
 */
export function classify(title: string, sourcePage: "md" | "notification" | "mc" | "sc" | "amd" | "gn"): string {
  // Source pages whose doc_type is unambiguous regardless of title.
  if (sourcePage === "mc")  return "master_circular";
  if (sourcePage === "amd") return "amendment";
  if (sourcePage === "sc")  return "circular";
  if (sourcePage === "gn")  return "guidance_note";

  const t = title.toLowerCase();
  if (/^(amendment|amendments|corrigendum|addendum)\b/.test(t) || /\bamendment to (the )?master/.test(t)) {
    return "amendment";
  }
  if (sourcePage === "md") return "master_direction";
  if (t.includes("master circular")) return "master_circular";
  if (t.includes("master direction")) return "master_direction";
  if (/\bdraft\b/.test(t)) return "draft";
  if (/\bregulations?\b.*\b(19|20)\d{2}\b/.test(t)) return "regulation";
  if (t.includes("circular")) return "circular";
  return "notification";
}

/** RBI reference numbers, e.g. "DOR.AUT.REC.No.12/24.01.001/2025-26". */
export function extractRefNo(body: string): string | null {
  const m = (body || "").slice(0, 4000).match(/\b([A-Z]{2,6}(?:\.[A-Z0-9]{1,10}){1,4}\.No\.?\s*[A-Z0-9\/.\-]{3,40})/);
  return m ? m[1].replace(/\s+/g, "") : null;
}

/* ------------------------------------------------------------------ */
/* Sync drivers                                                        */
/* ------------------------------------------------------------------ */

async function materialise(
  items: ListItem[],
  sourcePage: "md" | "notification" | "mc" | "sc" | "amd" | "gn",
  log: Logger,
  force: boolean,
  /**
   * Controls which items require a fresh body fetch:
   *
   *  false        — always fetch all items (original behaviour).
   *  true         — skip items already in the DB (immutable sources: notifications,
   *                 amendment directions, standalone circulars).
   *  "date-title" — skip items whose listing date AND title are unchanged vs. the
   *                 stored row. Used for Master Directions and Master Circulars,
   *                 which RBI can edit in place. RBI always bumps the publication
   *                 date when it amends a direction, so matching on date+title is a
   *                 safe proxy for "body unchanged". Makes incremental sync ~10×
   *                 faster on a warm database.
   */
  skipExisting: boolean | "date-title"
): Promise<DocInput[]> {
  let todo: ListItem[];
  if (force) {
    todo = items;
  } else if (skipExisting === "date-title") {
    todo = items.filter((it) => {
      const s = knownDateTitle(it.id);
      // New doc (no stored record) or listing date/title changed → must re-fetch.
      return !s || s.date !== it.date || s.title !== it.title;
    });
  } else if (skipExisting) {
    todo = items.filter((it) => !knownHash(it.id));
  } else {
    todo = items;
  }

  const skipped = items.length - todo.length;
  if (skipped) {
    const reason = skipExisting === "date-title" ? "date+title unchanged" : "already indexed";
    log(`  (${skipped} ${reason}, skipping body fetch)`);
  }

  return Promise.all(
    todo.map((it) =>
      limit(async () => {
        const { text, via, pdfUrl } = await fetchBody(it);
        await sleep(200);
        if (via.startsWith("failed") || !text) log(`  ! ${it.id} body empty (${via})`);

        return {
          id: it.id,
          regulator: "RBI",
          doc_type: classify(it.title, sourcePage),
          title: it.title,
          date: it.date,
          department: it.category ?? null,
          category: it.subCategory ?? it.category ?? null,
          ref_no: extractRefNo(text),
          source_url: it.htmlUrl,
          pdf_url: pdfUrl,
          body: text,
          indexed_at: new Date().toISOString(),
        } satisfies DocInput;
      })
    )
  );
}

export interface SyncOutcome {
  inserted: number;
  changed: number;
  unchanged: number;
  changedIds: string[];
  warnings: string[];
}

/** Scrape the Master Directions page — the consolidated, currently-in-force rules.
 *
 * @param subCategories  If provided, only sync Master Directions whose sub-category
 *   (the entity-type heading, e.g. "Commercial Banks") matches one of the given
 *   values (case-insensitive substring match).  Pass an empty array or omit to
 *   sync everything.
 */
export async function syncMasterDirections(
  log: Logger,
  force = false,
  subCategories: string[] = [],
): Promise<SyncOutcome> {
  log("Fetching Master Directions listing...");
  const report = await fetchMasterDirectionList();

  let items = report.items;
  if (subCategories.length > 0) {
    const lower = subCategories.map((s) => s.toLowerCase());
    items = items.filter((it) => {
      const sub = (it.subCategory ?? it.category ?? "").toLowerCase();
      return lower.some((f) => sub.includes(f));
    });
    log(
      `Master Directions: filtered to [${subCategories.join(", ")}] — ` +
        `${items.length} of ${report.items.length} kept`
    );
  } else {
    log(
      `Master Directions: ${report.items.length} found across ` +
        `${new Set(report.items.map((i) => i.category)).size} departments`
    );
  }
  report.warnings.forEach((w) => log(`  ! ${w}`));

  // "date-title" skip: only re-fetch MDs whose listing date or title changed.
  // RBI always bumps the date when it amends a direction in place, so this is
  // safe and makes incremental sync ~10× faster on a warm database.
  const docs = await materialise(items, "md", log, force, "date-title");
  const r = upsertDocs(docs);
  log(`Master Directions: ${r.inserted} new, ${r.changed} amended, ${r.unchanged} unchanged`);
  if (r.changed) log(`  amended: ${r.changedIds.join(", ")}`);
  return { ...r, warnings: report.warnings };
}

/* ------------------------------------------------------------------ */
/* Phase-1 additions: Master Circulars, Standalone Circulars,         */
/* Amendment Directions                                                */
/* ------------------------------------------------------------------ */

/**
 * Scrape the Master Circulars listing page.
 * Master Circulars consolidate the year's instructions for a topic and are
 * issued once per RBI year, then occasionally amended — so we always
 * re-fetch them (skipExisting = false), same as Master Directions.
 */
export async function syncMasterCirculars(log: Logger, force = false): Promise<SyncOutcome> {
  log("Fetching Master Circulars listing…");
  const report = await fetchMasterCircularList();
  log(
    `Master Circulars: ${report.items.length} found` +
      (report.headingsSeen.length ? ` across ${report.headingsSeen.length} sections` : "")
  );
  report.warnings.forEach((w) => log(`  ! ${w}`));

  // Same date-title optimisation as Master Directions.
  const docs = await materialise(report.items, "mc", log, force, "date-title");
  const r = upsertDocs(docs);
  log(`Master Circulars: ${r.inserted} new, ${r.changed} amended, ${r.unchanged} unchanged`);
  if (r.changed) log(`  amended: ${r.changedIds.join(", ")}`);
  return { ...r, warnings: report.warnings };
}

/**
 * Scrape the Amendment Directions listing page.
 * These documents are point-in-time (amending circulars) — once indexed
 * they do not change, so we skip already-known ids (skipExisting = true).
 */
export async function syncAmendmentDirections(log: Logger, force = false): Promise<SyncOutcome> {
  log("Fetching Amendment Directions listing…");
  const report = await fetchAmendmentDirectionList();
  log(
    `Amendment Directions: ${report.items.length} found` +
      (report.headingsSeen.length ? ` across ${report.headingsSeen.length} sections` : "")
  );
  report.warnings.forEach((w) => log(`  ! ${w}`));

  const docs = await materialise(report.items, "amd", log, force, true);
  const r = upsertDocs(docs);
  log(`Amendment Directions: ${r.inserted} new, ${r.changed} amended, ${r.unchanged} unchanged`);
  if (r.changed) log(`  amended: ${r.changedIds.join(", ")}`);
  return { ...r, warnings: report.warnings };
}

/**
 * Scrape the Standalone Circulars listing page.
 * These are point-in-time circulars — once indexed they do not change,
 * so we skip already-known ids (skipExisting = true).
 */
export async function syncStandaloneCirculars(log: Logger, force = false): Promise<SyncOutcome> {
  log("Fetching Standalone Circulars listing…");
  const report = await fetchStandaloneCircularList();
  log(`Standalone Circulars: ${report.items.length} found`);
  report.warnings.forEach((w) => log(`  ! ${w}`));

  const docs = await materialise(report.items, "sc", log, force, true);
  const r = upsertDocs(docs);
  log(`Standalone Circulars: ${r.inserted} new, ${r.changed} amended, ${r.unchanged} unchanged`);
  if (r.changed) log(`  amended: ${r.changedIds.join(", ")}`);
  return { ...r, warnings: report.warnings };
}

/**
 * Scrape the Guidance Notes from the Standalone Circulars page.
 * Guidance Notes appear as plain hyperlinks (not in the inner table) whose
 * text starts with "Guidance Note". They are immutable once published —
 * skip already-indexed ones.
 *
 * We reuse the same SC_URL fetch rather than making a separate HTTP request,
 * so the caller can pass the already-fetched HTML when it has it.
 */
export async function syncGuidanceNotes(log: Logger, force = false, html?: string): Promise<SyncOutcome> {
  log("Parsing Guidance Notes from Standalone Circulars page…");
  const pageHtml = html ?? (await (await politeFetch(SC_URL, { timeoutMs: 45_000 })).text());
  const report = parseGuidanceNoteList(pageHtml);
  log(`Guidance Notes: ${report.items.length} found`);
  report.warnings.forEach((w) => log(`  ! ${w}`));

  const docs = await materialise(report.items, "gn", log, force, true);
  const r = upsertDocs(docs);
  log(`Guidance Notes: ${r.inserted} new, ${r.changed} amended, ${r.unchanged} unchanged`);
  return { ...r, warnings: report.warnings };
}

export interface WithdrawnOutcome {
  scanned: number;    // items on the withdrawn page
  matched: number;    // items matched to a document in our DB
  newlyWithdrawn: number;  // documents newly marked withdrawn this run
  warnings: string[];
}

/**
 * Fetch the RBI withdrawn-circulars page and mark matching documents as
 * status = 'withdrawn' in the database.
 *
 * Matching strategy: the ?Id= parameter in the withdrawn page's hyperlinks
 * is the same internal RBI document ID used in all other RBI page URLs, so
 * we do a LIKE search on source_url. Documents that are already marked
 * withdrawn are left untouched (idempotent).
 */
export async function syncWithdrawnDocuments(log: Logger): Promise<WithdrawnOutcome> {
  log("Fetching withdrawn-circulars page…");
  const res = await politeFetch(WD_URL, { timeoutMs: 45_000 });
  const html = await res.text();
  const { items, warnings } = parseWithdrawnCirculars(html);

  log(`Withdrawn page: ${items.length} entries`);
  warnings.forEach((w) => log(`  ! ${w}`));

  const now = new Date().toISOString();
  let matched = 0;
  let newlyWithdrawn = 0;

  for (const item of items) {
    if (!item.rbiId) continue;
    const docs = docsByRbiId(item.rbiId);
    if (!docs.length) continue;
    matched++;

    for (const doc of docs) {
      const wasNew = markWithdrawn({
        id: doc.id,
        reason: item.withdrawalSource,
        withdrawnDate: item.publishedDate || null,
        detectedAt: now,
      });
      if (wasNew) {
        newlyWithdrawn++;
        log(`  ✓ ${doc.id}: marked withdrawn (${item.withdrawalSource})`);
      }
    }
  }

  log(`Withdrawn: ${matched} matched, ${newlyWithdrawn} newly marked.`);
  return { scanned: items.length, matched, newlyWithdrawn, warnings };
}

/** Scrape notifications/circulars month by month. */
export async function syncNotifications(monthsBack: number, log: Logger, force = false): Promise<SyncOutcome> {
  const tokens = await fetchViewstateTokens();
  const now = new Date();
  const agg: SyncOutcome = { inserted: 0, changed: 0, unchanged: 0, changedIds: [], warnings: [] };

  for (let i = 0; i < monthsBack; i++) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    const year = d.getFullYear();
    const month = d.getMonth() + 1;

    let report: ParseReport;
    try {
      report = await fetchNotificationMonth(year, month, tokens);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      log(`${year}-${String(month).padStart(2, "0")}: fetch failed (${msg}) — skipping`);
      agg.warnings.push(`${year}-${month}: ${msg}`);
      await sleep(3000);
      continue;
    }

    log(`${year}-${String(month).padStart(2, "0")}: ${report.items.length} documents`);
    report.warnings.forEach((w) => agg.warnings.push(`${year}-${month}: ${w}`));

    if (report.items.length) {
      const docs = await materialise(report.items, "notification", log, force, true);
      const r = upsertDocs(docs);
      agg.inserted += r.inserted;
      agg.changed += r.changed;
      agg.unchanged += r.unchanged;
      agg.changedIds.push(...r.changedIds);
    }
    await sleep(500);
  }
  return agg;
}
