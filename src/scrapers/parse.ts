/**
 * Pure HTML -> structured-item parsers.
 *
 * Deliberately separated from the network layer so they can be unit-tested
 * against saved fixtures with no egress. Every selector strategy reports
 * which branch fired, so `npm run doctor` can tell you the page changed
 * shape instead of silently returning zero rows — the failure mode the
 * original package had.
 */
import * as cheerio from "cheerio";
import { toISODate } from "../util/date.js";

export interface ListItem {
  id: string;
  rbiId: string;
  title: string;
  date: string;
  htmlUrl: string;
  pdfUrl: string | null;
  category?: string | null;
  subCategory?: string | null;
}

export interface ParseReport {
  strategy: string;
  items: ListItem[];
  headingsSeen: string[];
  warnings: string[];
}

const ORIGIN = "https://www.rbi.org.in";

export function absolute(href: string, base = `${ORIGIN}/Scripts/`): string {
  if (!href) return "";
  if (/^https?:\/\//i.test(href)) return href;
  try {
    return new URL(href, base).toString();
  } catch {
    return href;
  }
}

/* ------------------------------------------------------------------ */
/* Notifications listing (NotificationUser.aspx)                       */
/* ------------------------------------------------------------------ */

/**
 * Rows are grouped under standalone date-header rows ("Aug 10, 2026").
 * We track the current date as we walk rows in document order.
 */
export function parseNotificationList(html: string): ParseReport {
  const $ = cheerio.load(html);
  const items: ListItem[] = [];
  const warnings: string[] = [];
  const headingsSeen: string[] = [];
  let currentDate = "";
  let orphaned = 0;

  if (/No\s+Notification\s+Found/i.test(html)) {
    return { strategy: "empty-month", items: [], headingsSeen: [], warnings: [] };
  }

  $("table tr").each((_, tr) => {
    const $tr = $(tr);
    const text = $tr.text().trim();

    // A date header row contains nothing but the date.
    const dateMatch = text.match(/^([A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4})$/);
    if (dateMatch) {
      const iso = toISODate(dateMatch[1]);
      if (iso) {
        currentDate = iso;
        headingsSeen.push(dateMatch[1]);
      } else {
        warnings.push(`Unparseable date header: "${dateMatch[1]}"`);
      }
      return;
    }

    const link = $tr.find('a[href*="NotificationUser.aspx?Id="]').first();
    if (!link.length) return;

    const href = link.attr("href") || "";
    const idMatch = href.match(/[?&]Id=(\d+)/i);
    if (!idMatch) return;

    if (!currentDate) {
      orphaned++;
      return;
    }

    const pdf = $tr.find('a[href$=".PDF"], a[href$=".pdf"]').first();
    items.push({
      id: `rbi:nt:${idMatch[1]}`,
      rbiId: idMatch[1],
      title: collapse(link.text()),
      date: currentDate,
      htmlUrl: absolute(href),
      pdfUrl: pdf.length ? absolute(pdf.attr("href") || "", ORIGIN) : null,
    });
  });

  if (orphaned) warnings.push(`${orphaned} notification row(s) appeared before any date header and were skipped.`);
  if (!items.length) warnings.push("No notification rows matched — the listing markup may have changed.");

  return { strategy: "notification-rows", items, headingsSeen, warnings };
}

/* ------------------------------------------------------------------ */
/* Master Directions listing (BS_ViewMasterDirections.aspx)            */
/* ------------------------------------------------------------------ */

const MD_LINK = 'a[href*="BS_ViewMasDirections.aspx"], a[href*="BS_ViewMasterDirections.aspx?id="]';

/**
 * The Master Directions page groups entries under two levels of heading:
 * an RBI department (e.g. "Department of Regulation") and, within it, a
 * regulated-entity band (e.g. "Commercial Banks", "Small Finance Banks").
 *
 * That grouping is a real hierarchy level and the original package threw it
 * away entirely — it never fetched this page at all, so Master Directions
 * were only captured if they happened to also appear in a scraped month of
 * notifications. Master Directions are edited in place and re-dated, so
 * month-window scraping misses them structurally.
 *
 * We walk the document in order, tracking the most recent heading at each
 * level, and attach both to every link we pass.
 */
export function parseMasterDirectionList(html: string): ParseReport {
  const $ = cheerio.load(html);
  const items: ListItem[] = [];
  const warnings: string[] = [];
  const headingsSeen: string[] = [];
  const seen = new Set<string>();

  let category: string | null = null;
  let subCategory: string | null = null;
  let currentDate = "";

  // Single depth-first pass in document order.
  $("body")
    .find("*")
    .each((_, el) => {
      const $el = $(el);
      const tag = (el as any).tagName?.toLowerCase() ?? "";

      // ---- Master Direction link ----
      if (tag === "a") {
        const href = $el.attr("href") || "";
        if (/BS_ViewMas(ter)?Directions\.aspx\?id=/i.test(href)) {
          const idMatch = href.match(/[?&]id=(\d+)/i);
          const title = collapse($el.text());
          if (!idMatch || title.length < 4) return;

          const id = `rbi:md:${idMatch[1]}`;
          if (seen.has(id)) return;
          seen.add(id);

          // PDF link usually sits in the same row.
          const row = $el.closest("tr, li, div").first();
          const pdf = row.find('a[href$=".PDF"], a[href$=".pdf"]').first();

          // A date may sit in the row, or have been seen as a preceding heading.
          const rowDate = firstDateIn(row.text()) || currentDate;

          items.push({
            id,
            rbiId: idMatch[1],
            title,
            date: rowDate,
            htmlUrl: absolute(href),
            pdfUrl: pdf.length ? absolute(pdf.attr("href") || "", ORIGIN) : null,
            category,
            subCategory,
          });
        }
        return;
      }

      // ---- Heading candidate ----
      const own = ownText($, $el);
      if (!own) return;

      const d = own.match(/^([A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4})$/);
      if (d) {
        currentDate = toISODate(d[1]);
        return;
      }

      if (!isHeadingish(tag, $el, own)) return;

      headingsSeen.push(own);
      if (/^Department of |Foreign Exchange|Payment and Settlement|Issuer of|Banker (and|to)|Consumer Education|Financial (Inclusion|Market)/i.test(own)) {
        category = own;
        subCategory = null;
      } else {
        subCategory = own;
      }
    });

  if (!items.length) {
    warnings.push(
      "No Master Direction links matched. Expected hrefs containing 'BS_ViewMasDirections.aspx?id='. " +
        "Run `npm run doctor` to dump what the page actually returned."
    );
  }
  const uncategorised = items.filter((i) => !i.category).length;
  if (uncategorised) warnings.push(`${uncategorised}/${items.length} Master Directions have no department heading — heading detection may need tuning.`);
  const undated = items.filter((i) => !i.date).length;
  if (undated) warnings.push(`${undated}/${items.length} Master Directions have no date.`);

  return { strategy: "md-document-order-walk", items, headingsSeen, warnings };
}

/* ------------------------------------------------------------------ */
/* Generic "repository" listing                                        */
/* (Master Circulars, Amendment Directions — identical HTML layout)   */
/* ------------------------------------------------------------------ */

/**
 * Shared parser for RBI pages that use td.tableheader cells for date/section
 * headings and a.link2 / a.links anchors for the actual document links.
 * Both the Master Circulars page and the Amendment Directions page use this
 * layout, so we factor the logic here and expose thin named wrappers below.
 *
 * @param idPrefix  Short stable namespace for the document id ("mc" / "amd").
 * @param strategy  Human-readable label stored in ParseReport.strategy.
 */
function parseRepositoryPage(html: string, idPrefix: string, strategy: string): ParseReport {
  const $ = cheerio.load(html);
  const items: ListItem[] = [];
  const warnings: string[] = [];
  const headingsSeen: string[] = [];
  const seen = new Set<string>();

  let category: string | null = null;
  let currentDate = "";

  $("body")
    .find("*")
    .each((_, el) => {
      const $el = $(el);
      const tag = (el as any).tagName?.toLowerCase() ?? "";

      /* ---- td.tableheader — date or section heading ---- */
      if (tag === "td") {
        const cls = ($el.attr("class") || "").toLowerCase();
        if (cls.includes("tableheader")) {
          const text = collapse($el.text());
          if (!text) return;
          const dateInHeader = firstDateIn(text);
          if (dateInHeader) {
            currentDate = dateInHeader;
          } else {
            category = text;
            headingsSeen.push(text);
          }
          return;
        }
      }

      /* ---- Document link (a.link2 or a.links) ---- */
      if (tag === "a") {
        const cls = ($el.attr("class") || "").toLowerCase();
        if (!cls.includes("link2") && !cls.includes("links")) return;

        const href = $el.attr("href") || "";
        if (!href) return;

        const title = collapse($el.text());
        if (title.length < 4) return;

        // Prefer a numeric ?id= / ?Id= param; fall back to a slug of the href.
        const idMatch = href.match(/[?&][Ii][Dd]=(\d+)/);
        const rbiId = idMatch ? idMatch[1] : hrefSlug(href);
        const id = `rbi:${idPrefix}:${rbiId}`;

        if (seen.has(id)) return;
        seen.add(id);

        const row = $el.closest("tr, li, div").first();
        const pdf = row.find('a[href$=".PDF"], a[href$=".pdf"]').first();
        const rowDate = firstDateIn(row.text()) || currentDate;

        items.push({
          id,
          rbiId,
          title,
          date: rowDate,
          htmlUrl: absolute(href),
          pdfUrl: pdf.length ? absolute(pdf.attr("href") || "", ORIGIN) : null,
          category,
          subCategory: null,
        });
        return;
      }

      /* ---- Fallback heading detection ---- */
      const own = ownText($, $el);
      if (!own) return;

      const d = own.match(/^([A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4})$/);
      if (d) {
        currentDate = toISODate(d[1]);
        return;
      }
      if (!isHeadingish(tag, $el, own)) return;
      headingsSeen.push(own);
      category = own;
    });

  if (!items.length) {
    warnings.push(
      `No ${strategy} links matched (expected a.link2 or a.links anchors). ` +
        "Run `npm run doctor` to inspect the live page."
    );
  }

  return { strategy, items, headingsSeen, warnings };
}

/** Stable short slug derived from an href (used when no ?id= is present). */
function hrefSlug(href: string): string {
  return href.replace(/^.*\//, "").replace(/[^A-Za-z0-9_-]/g, "_").slice(0, 50);
}

/**
 * Parse the Master Circulars listing page.
 * URL: https://www.rbi.org.in/Scripts/BS_ViewMasterCirculardetails.aspx
 */
export function parseMasterCircularList(html: string): ParseReport {
  return parseRepositoryPage(html, "mc", "mc-repository-walk");
}

/**
 * Parse the Amendment Directions listing page.
 * URL: https://www.rbi.org.in/Scripts/Fs_AmendmentDirections.aspx
 */
export function parseAmendmentDirectionList(html: string): ParseReport {
  return parseRepositoryPage(html, "amd", "amd-repository-walk");
}

/* ------------------------------------------------------------------ */
/* Standalone Circulars listing                                        */
/* (BS_ViewListofstandalonecirculars.aspx)                            */
/* ------------------------------------------------------------------ */

/**
 * The Standalone Circulars page wraps an inner table.tablebg with four
 * columns per row:
 *   0: S.No.
 *   1: Circular Number  ← contains the <a href="…"> link
 *   2: Circular Name    ← plain text title
 *   3: Date
 *
 * Row 0 is a header row and must be skipped.
 */
export function parseStandaloneCircularList(html: string): ParseReport {
  const $ = cheerio.load(html);
  const items: ListItem[] = [];
  const warnings: string[] = [];
  const seen = new Set<string>();

  // The data table is the inner table.tablebg; fall back to the largest table
  // on the page if that selector doesn't fire.
  let dataTable = $("table.tablebg").first();
  if (!dataTable.length) {
    let maxRows = 0;
    $("table").each((_, t) => {
      const n = $(t).find("tr").length;
      if (n > maxRows) { maxRows = n; dataTable = $(t); }
    });
  }

  if (!dataTable.length) {
    return {
      strategy: "sc-tablebg",
      items: [],
      headingsSeen: [],
      warnings: ["Could not find the standalone circulars table — page structure may have changed."],
    };
  }

  const rows = dataTable.find("tr").toArray();

  for (const tr of rows.slice(1)) {          // skip header row
    const $tr = $(tr);
    const cells = $tr.find("td").toArray();
    if (cells.length < 2) continue;

    // When we have ≥4 columns: link in col 1, title in col 2, date in col 3.
    // When we have 2–3 columns (alternate layout): link in col 0, title in col 1, date in col 2/last.
    const linkCellIdx  = cells.length >= 4 ? 1 : 0;
    const titleCellIdx = cells.length >= 4 ? 2 : 1;

    const linkCell = $(cells[linkCellIdx]);
    const link = linkCell.find("a").first();
    const href = link.attr("href") || "";
    if (!href) continue;

    // Title: prefer the dedicated title cell; fall back to link text.
    const title = collapse($(cells[titleCellIdx]).text()) || collapse(link.text());
    if (title.length < 4) continue;

    // Date: last column.
    const dateText = collapse($(cells[cells.length - 1]).text());
    const date = toISODate(dateText) || firstDateIn(dateText) || "";

    // ID from the href query string.
    const idMatch = href.match(/[?&][Ii][Dd]=(\d+)/);
    const rbiId = idMatch ? idMatch[1] : hrefSlug(href);
    const id = `rbi:sc:${rbiId}`;

    if (seen.has(id)) continue;
    seen.add(id);

    const pdf = $tr.find('a[href$=".PDF"], a[href$=".pdf"]').first();

    items.push({
      id,
      rbiId,
      title,
      date,
      htmlUrl: absolute(href),
      pdfUrl: pdf.length ? absolute(pdf.attr("href") || "", ORIGIN) : null,
    });
  }

  if (!items.length) {
    warnings.push(
      "No standalone circular rows matched. " +
        "Run `npm run doctor` to inspect the live page."
    );
  }

  return { strategy: "sc-tablebg", items, headingsSeen: [], warnings };
}

/* ------------------------------------------------------------------ */
/* Guidance Notes                                                       */
/* (parsed from BS_ViewListofstandalonecirculars.aspx — same fetch)   */
/* ------------------------------------------------------------------ */

/**
 * Guidance Notes live on the same page as Standalone Circulars but are not in
 * the inner table — they appear as plain hyperlinks whose visible text starts
 * with "Guidance Note". Parse them separately so they keep their own category
 * and doc_type in the database.
 */
export function parseGuidanceNoteList(html: string): ParseReport {
  const $ = cheerio.load(html);
  const items: ListItem[] = [];
  const warnings: string[] = [];
  const seen = new Set<string>();

  $("a").each((_, el) => {
    const $a = $(el);
    const title = collapse($a.text());
    if (!title.toLowerCase().startsWith("guidance note")) return;

    const href = $a.attr("href") || "";
    if (!href) return;

    const idMatch = href.match(/[?&][Ii][Dd]=(\d+)/);
    const rbiId = idMatch ? idMatch[1] : hrefSlug(href);
    const id = `rbi:gn:${rbiId}`;
    if (seen.has(id)) return;
    seen.add(id);

    // Guidance Notes typically have no direct PDF link — the detail page is the canonical URL.
    items.push({
      id,
      rbiId,
      title,
      date: "",            // RBI does not publish a date for these in the listing
      htmlUrl: absolute(href),
      pdfUrl: null,
    });
  });

  if (!items.length) {
    warnings.push("No Guidance Note links found on the page (links starting with 'Guidance Note').");
  }

  return { strategy: "guidance-notes-links", items, headingsSeen: [], warnings };
}

/* ------------------------------------------------------------------ */
/* Withdrawn Circulars                                                  */
/* (NotificationUserWithdrawnCircular.aspx)                           */
/* ------------------------------------------------------------------ */

export interface WithdrawnItem {
  rbiId: string;             // ?Id= or ?id= parameter from the href
  detailUrl: string;
  circularNumber: string;    // Only present in departmental tables
  title: string;
  publishedDate: string;     // Date printed on the withdrawn page (may be blank)
  withdrawalSource: string;  // "RRA 2.0" | "Department of Regulation" | "Department of Supervision"
}

export interface WithdrawnReport {
  items: WithdrawnItem[];
  warnings: string[];
}

/**
 * Classify an HTML table by its first-row headers to distinguish the two table
 * types on the withdrawn-circulars page:
 *   RRA — columns: Date | Subject | Department
 *   DEPT — columns: S.No. | Circular Number | Title/Name | Date
 *   UNKNOWN — anything else (nav table, layout table, etc.)
 */
function classifyWithdrawnTable($: cheerio.CheerioAPI, table: any): "RRA" | "DEPT" | "UNKNOWN" {
  const $table = $(table);
  const firstRow = $table.find("tr").first();
  const headers = firstRow
    .find("th, td")
    .map((_, c) => collapse($(c).text()).toLowerCase())
    .toArray();

  const hStr = headers.join("|");
  if (hStr.includes("subject") && hStr.includes("department")) return "RRA";
  if (hStr.includes("circular number") || (hStr.includes("circular") && hStr.includes("date"))) return "DEPT";
  return "UNKNOWN";
}

/** Extract the nearby heading text that precedes a table — used to identify the department. */
function detectDepartment($: cheerio.CheerioAPI, table: any): string {
  let node: any = table;
  for (let i = 0; i < 20; i++) {
    node = $(node).prev()[0] ?? $(node).parent()[0];
    if (!node) break;
    const text = collapse($(node).text());
    if (/Department\s+of\s+Regulation/i.test(text)) return "Department of Regulation";
    if (/Department\s+of\s+Supervision/i.test(text)) return "Department of Supervision";
  }
  return "Unknown";
}

function extractHrefId(href: string): string {
  const m = href.match(/[?&][Ii][Dd]=(\d+)/);
  return m ? m[1] : hrefSlug(href);
}

/**
 * Parse the RBI withdrawn-circulars page into a flat list of WithdrawnItem records.
 * Recognises two table layouts automatically from header structure.
 */
export function parseWithdrawnCirculars(html: string): WithdrawnReport {
  const $ = cheerio.load(html);
  const items: WithdrawnItem[] = [];
  const warnings: string[] = [];

  const tables = $("table").toArray();
  let recognised = 0;

  for (const table of tables) {
    const type = classifyWithdrawnTable($, table);
    if (type === "UNKNOWN") continue;
    recognised++;

    const $table = $(table);
    const rows = $table.find("tr").toArray().slice(1); // skip header row

    if (type === "RRA") {
      for (const tr of rows) {
        const cells = $(tr).find("td").toArray();
        if (cells.length < 2) continue;

        const publishedDate = collapse($(cells[0]).text());
        const title = collapse($(cells[1]).text());
        const department = cells.length >= 3 ? collapse($(cells[2]).text()) : "RRA 2.0";

        const anchor = $(cells[1]).find("a").first();
        const href = anchor.attr("href") || "";
        const rbiId = href ? extractHrefId(href) : "";

        if (!title) continue;
        items.push({
          rbiId,
          detailUrl: href ? absolute(href) : "",
          circularNumber: "",
          title,
          publishedDate,
          withdrawalSource: department || "RRA 2.0",
        });
      }
    } else {
      // DEPT table: S.No. | Circular Number | Title | Date
      const dept = detectDepartment($, table);

      for (const tr of rows) {
        const cells = $(tr).find("td").toArray();
        if (cells.length < 3) continue;

        const circularNumber = collapse($(cells[1]).text());
        const title = collapse($(cells[2]).text());
        const publishedDate = cells.length >= 4 ? collapse($(cells[3]).text()) : "";

        // Hyperlink may be on col 1 (Circular Number) or col 2 (Title).
        let anchor = $(cells[1]).find("a").first();
        if (!anchor.length) anchor = $(cells[2]).find("a").first();
        const href = anchor.attr("href") || "";
        const rbiId = href ? extractHrefId(href) : "";

        if (!circularNumber && !title) continue;
        items.push({
          rbiId,
          detailUrl: href ? absolute(href) : "",
          circularNumber,
          title,
          publishedDate,
          withdrawalSource: dept,
        });
      }
    }
  }

  if (recognised === 0) {
    warnings.push("No withdrawn-circular tables recognised — page structure may have changed.");
  }

  return { items, warnings };
}

/* ------------------------------------------------------------------ */
/* Document body extraction                                            */
/* ------------------------------------------------------------------ */

/** Pull the PDF URL out of a document page when the listing did not carry one. */
export function findPdfUrl(html: string): string | null {
  const $ = cheerio.load(html);
  const a = $('a[href$=".PDF"], a[href$=".pdf"], a[href*="rbidocs.rbi.org.in"][href*=".PDF"]').first();
  const href = a.attr("href");
  return href ? absolute(href, ORIGIN) : null;
}

/** Isolate the main content container of an RBI document page. */
export function extractBodyHtml(html: string): { html: string | null; strategy: string } {
  const $ = cheerio.load(html);
  const candidates = [
    "#pnlDetails",
    "#tdData",
    ".tablecontent2",
    "table.tablebg",
    "#example-min",
    "#content",
  ];
  for (const sel of candidates) {
    const el = $(sel).first();
    if (el.length && collapse(el.text()).length > 200) {
      return { html: el.html(), strategy: sel };
    }
  }
  const body = $("body");
  return { html: body.length ? body.html() : null, strategy: "body-fallback" };
}

/* ------------------------------------------------------------------ */

function collapse(s: string): string {
  return (s || "").replace(/\s+/g, " ").trim();
}

/** Text belonging directly to this element, excluding descendants' own blocks. */
function ownText($: cheerio.CheerioAPI, $el: cheerio.Cheerio<any>): string {
  if ($el.find("a").length) return ""; // containers with links are not headings
  const t = collapse($el.text());
  if (!t || t.length > 90 || t.length < 3) return "";
  return t;
}

function isHeadingish(tag: string, $el: cheerio.Cheerio<any>, text: string): boolean {
  if (/^h[1-5]$/.test(tag)) return true;
  const cls = ($el.attr("class") || "").toLowerCase();
  if (/head|title|category|group/.test(cls)) return true;
  if ((tag === "td" || tag === "div" || tag === "p") && ($el.find("b, strong").length > 0 || /^[A-Z]/.test(text))) {
    // Reject sentence-like text: headings do not end in a full stop and are short.
    return !/[.;:]$/.test(text) && text.split(/\s+/).length <= 10;
  }
  return false;
}

function firstDateIn(text: string): string {
  const m = (text || "").match(/([A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4})/);
  return m ? toISODate(m[1]) : "";
}
