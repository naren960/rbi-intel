#!/usr/bin/env node
/**
 * Pre-flight check against the live RBI site.
 *
 * The failure mode this exists to prevent: RBI changes its markup, the
 * selectors stop matching, and the scraper reports "0 docs found" as if
 * that were a fact about the month rather than a bug. Every check here
 * prints what it actually saw, so a shape change is obvious immediately.
 *
 * Run this first on any machine before trusting a sync.
 */
import { writeFileSync, mkdirSync } from "node:fs";
import { politeFetch } from "../util/http.js";
import { parseNotificationList, parseMasterDirectionList, extractBodyHtml } from "../scrapers/parse.js";
import { toISODate } from "../util/date.js";
import { fetchNotificationMonth, fetchMasterDirectionList, classify, extractRefNo } from "../scrapers/rbi.js";

const NOTIF_URL = "https://www.rbi.org.in/Scripts/NotificationUser.aspx";
const MD_URL = "https://www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx";

let failures = 0;
const dump = process.argv.includes("--dump");
if (dump) mkdirSync("fixtures", { recursive: true });

function pass(label: string, detail = "") {
  console.log(`  PASS  ${label}${detail ? " — " + detail : ""}`);
}
function fail(label: string, detail: string) {
  failures++;
  console.log(`  FAIL  ${label} — ${detail}`);
}
function info(label: string) {
  console.log(`        ${label}`);
}

async function main() {
  console.log("rbi-intel doctor\n");

  /* --- 0. Offline logic checks (no network) --- */
  console.log("Offline checks");
  const d = toISODate("Aug 14, 2026");
  d === "2026-08-14" ? pass("date parsing timezone-safe", d) : fail("date parsing", `got ${d}, expected 2026-08-14`);

  const c = classify("Amendment to Master Direction on KYC", "notification");
  c === "amendment" ? pass("classifier handles amending titles", c) : fail("classifier", `got ${c}, expected amendment`);

  const ref = extractRefNo("RBI/2025-26/45 DOR.AUT.REC.No.12/24.01.001/2025-26 dated August 1, 2025");
  ref ? pass("reference-number extraction", ref) : fail("reference-number extraction", "no match");

  /* --- 1. Reachability --- */
  console.log("\nNetwork");
  let notifHtml = "";
  try {
    const res = await politeFetch(NOTIF_URL);
    notifHtml = await res.text();
    pass("NotificationUser.aspx reachable", `${notifHtml.length} bytes`);
  } catch (e) {
    fail("NotificationUser.aspx reachable", e instanceof Error ? e.message : String(e));
    console.log("\nCannot reach rbi.org.in. Everything below is skipped.");
    process.exit(1);
  }

  if (dump) {
    writeFileSync("fixtures/live-notifications.html", notifHtml);
    info("wrote fixtures/live-notifications.html");
  }

  /* --- 2. ASP.NET tokens --- */
  const vs = notifHtml.match(/id="__VIEWSTATE"\s+value="([^"]*)"/)?.[1];
  const ev = notifHtml.match(/id="__EVENTVALIDATION"\s+value="([^"]*)"/)?.[1];
  vs ? pass("__VIEWSTATE present", `${vs.length} chars`) : fail("__VIEWSTATE present", "not found — POST-based month paging will not work");
  ev ? pass("__EVENTVALIDATION present") : info("__EVENTVALIDATION absent (may be optional)");

  /* --- 3. Notification listing parse --- */
  console.log("\nNotification listing");
  const landing = parseNotificationList(notifHtml);
  landing.items.length
    ? pass("landing page rows parsed", `${landing.items.length} items, ${landing.headingsSeen.length} date headers`)
    : fail("landing page rows parsed", "0 items — selectors likely stale");
  landing.warnings.forEach((w) => info(`warning: ${w}`));
  if (landing.items[0]) {
    const s = landing.items[0];
    info(`sample: ${s.id} | ${s.date} | ${s.title.slice(0, 70)}`);
    s.date ? pass("dates resolved") : fail("dates resolved", "empty date on first item");
  }

  /* --- 4. Month POST --- */
  const now = new Date();
  const prev = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  try {
    const r = await fetchNotificationMonth(prev.getFullYear(), prev.getMonth() + 1);
    r.items.length
      ? pass(`month POST ${prev.getFullYear()}-${prev.getMonth() + 1}`, `${r.items.length} items`)
      : fail(`month POST ${prev.getFullYear()}-${prev.getMonth() + 1}`, "0 items — viewstate POST may be rejected");
  } catch (e) {
    fail("month POST", e instanceof Error ? e.message : String(e));
  }

  /* --- 5. Master Directions listing --- */
  console.log("\nMaster Directions listing");
  try {
    const res = await politeFetch(MD_URL, { timeoutMs: 45_000 });
    const html = await res.text();
    if (dump) {
      writeFileSync("fixtures/live-master-directions.html", html);
      info("wrote fixtures/live-master-directions.html");
    }
    const md = parseMasterDirectionList(html);
    md.items.length
      ? pass("Master Directions parsed", `${md.items.length} items`)
      : fail("Master Directions parsed", "0 items — check the BS_ViewMasDirections href pattern");
    md.warnings.forEach((w) => info(`warning: ${w}`));

    const cats = [...new Set(md.items.map((i) => i.category).filter(Boolean))];
    cats.length
      ? pass("category headings detected", `${cats.length}: ${cats.slice(0, 6).join(" | ")}`)
      : fail("category headings detected", "none — the hierarchy layer will be empty");

    const subs = [...new Set(md.items.map((i) => i.subCategory).filter(Boolean))];
    info(`sub-categories: ${subs.length ? subs.slice(0, 8).join(" | ") : "none"}`);

    /* --- 6. Body extraction on one real document --- */
    console.log("\nDocument body extraction");
    const sample = md.items.find((i) => i.htmlUrl);
    if (sample) {
      const dres = await politeFetch(sample.htmlUrl);
      const dhtml = await dres.text();
      const { html: bodyHtml, strategy } = extractBodyHtml(dhtml);
      const len = bodyHtml?.length ?? 0;
      len > 500
        ? pass("body container found", `${strategy}, ${len} bytes html`)
        : fail("body container found", `strategy=${strategy}, only ${len} bytes — likely a PDF-only stub, PDF fallback will be used`);
      info(`sample doc: ${sample.id} ${sample.title.slice(0, 60)}`);
      info(`pdf: ${sample.pdfUrl ?? "none in listing"}`);
    }
  } catch (e) {
    fail("Master Directions listing", e instanceof Error ? e.message : String(e));
  }

  console.log(`\n${failures === 0 ? "All checks passed. Safe to run `npm run sync`." : `${failures} check(s) failed — fix before trusting a sync.`}`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
