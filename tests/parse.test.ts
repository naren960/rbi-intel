/**
 * Parser tests against synthetic HTML shaped like RBI's pages.
 *
 * IMPORTANT CAVEAT: these fixtures are reconstructions, not captures. They
 * prove the parsing *logic* is right — date grouping, ID namespacing,
 * category tracking, chapter/annex separation. They cannot prove the CSS
 * selectors still match rbi.org.in today. That is what `npm run doctor`
 * is for, and it must be run on a machine that can reach the site.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { parseNotificationList, parseMasterDirectionList, extractBodyHtml } from "../src/scrapers/parse.js";
import { toISODate } from "../src/util/date.js";
import { classify, extractRefNo } from "../src/scrapers/rbi.js";

test("dates parse without timezone drift", () => {
  // The original package did new Date(s).toISOString(), which in IST
  // shifted every date back by one day.
  assert.equal(toISODate("Aug 14, 2026"), "2026-08-14");
  assert.equal(toISODate("August 14, 2026"), "2026-08-14");
  assert.equal(toISODate("Jan 01, 2020"), "2020-01-01");
  assert.equal(toISODate("14 Aug 2026"), "2026-08-14");
  assert.equal(toISODate("14/08/2026"), "2026-08-14");
  assert.equal(toISODate("2026-08-14"), "2026-08-14");
  assert.equal(toISODate("rubbish"), "");
});

test("notification listing groups rows under date headers", () => {
  const html = `<table>
    <tr><td>Aug 10, 2026</td></tr>
    <tr><td><a href="NotificationUser.aspx?Id=13675&Mode=0">Gold Loan Directions</a></td>
        <td><a href="https://rbidocs.rbi.org.in/rdocs/notification/PDFs/NOT2334.PDF">PDF</a></td></tr>
    <tr><td><a href="NotificationUser.aspx?Id=13674&Mode=0">Priority Sector Lending</a></td></tr>
    <tr><td>Jul 31, 2026</td></tr>
    <tr><td><a href="NotificationUser.aspx?Id=13600&Mode=0">Master Circular on Bank Finance</a></td></tr>
  </table>`;
  const r = parseNotificationList(html);
  assert.equal(r.items.length, 3);
  assert.equal(r.items[0].id, "rbi:nt:13675", "notification ids must be namespaced");
  assert.equal(r.items[0].date, "2026-08-10");
  assert.equal(r.items[1].date, "2026-08-10", "second row inherits the same date header");
  assert.equal(r.items[2].date, "2026-07-31", "date header switches correctly");
  assert.ok(r.items[0].pdfUrl?.endsWith(".PDF"));
  assert.equal(r.items[0].htmlUrl, "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=13675&Mode=0");
});

test("empty month is reported as empty, not as a parse failure", () => {
  const r = parseNotificationList("<html><body>No Notification Found</body></html>");
  assert.equal(r.strategy, "empty-month");
  assert.equal(r.items.length, 0);
  assert.equal(r.warnings.length, 0, "an empty month is not a warning");
});

test("a listing with no matching rows raises a warning rather than passing silently", () => {
  const r = parseNotificationList("<html><body><table><tr><td>something else</td></tr></table></body></html>");
  assert.equal(r.items.length, 0);
  assert.ok(r.warnings.length > 0, "zero rows with no 'not found' marker must warn");
});

test("Master Direction listing captures the category hierarchy", () => {
  const html = `<body><table>
    <tr><td class="tableheader"><b>Department of Regulation</b></td></tr>
    <tr><td><b>Commercial Banks</b></td></tr>
    <tr><td>Nov 28, 2025</td>
        <td><a href="BS_ViewMasDirections.aspx?id=13136">Credit Risk Management Directions, 2025</a></td>
        <td><a href="/rdocs/notification/PDFs/174MD.PDF">PDF</a></td></tr>
    <tr><td>Feb 25, 2016</td>
        <td><a href="BS_ViewMasDirections.aspx?id=11566">Know Your Customer Direction, 2016</a></td></tr>
    <tr><td><b>Small Finance Banks</b></td></tr>
    <tr><td>Jul 03, 2018</td>
        <td><a href="BS_ViewMasDirections.aspx?id=11322">Operational Guidelines</a></td></tr>
  </table></body>`;
  const r = parseMasterDirectionList(html);
  assert.equal(r.items.length, 3);
  assert.equal(r.items[0].id, "rbi:md:13136", "MD ids use a separate namespace from notifications");
  assert.equal(r.items[0].category, "Department of Regulation");
  assert.equal(r.items[0].subCategory, "Commercial Banks");
  assert.equal(r.items[2].subCategory, "Small Finance Banks", "sub-category must switch");
  assert.equal(r.items[0].date, "2025-11-28");
});

test("MD and notification ids never collide despite sharing numbers", () => {
  const md = parseMasterDirectionList(
    `<body><table><tr><td><a href="BS_ViewMasDirections.aspx?id=13136">A Direction</a></td></tr></table></body>`
  );
  const nt = parseNotificationList(
    `<table><tr><td>Aug 10, 2026</td></tr><tr><td><a href="NotificationUser.aspx?Id=13136">A Notification</a></td></tr></table>`
  );
  assert.notEqual(md.items[0].id, nt.items[0].id, "same numeric id on different pages must not merge");
});

test("body extraction prefers the content container over the whole page", () => {
  const long = "Regulated Entities shall ensure compliance with these Directions. ".repeat(8);
  const html = `<body><div id="nav">menu</div><div id="pnlDetails"><p>${long}</p></div></body>`;
  const r = extractBodyHtml(html);
  assert.equal(r.strategy, "#pnlDetails");
  assert.ok(r.html?.includes("Regulated Entities"));
  assert.ok(!r.html?.includes("menu"));
});

test("classifier separates amendments from the directions they amend", () => {
  assert.equal(classify("Amendment to Master Direction on KYC", "notification"), "amendment");
  assert.equal(classify("Master Direction - Know Your Customer, 2016", "notification"), "master_direction");
  assert.equal(classify("Anything at all", "md"), "master_direction", "the MD page is authoritative for its own entries");
  assert.equal(classify("Master Circular on Housing Finance", "notification"), "master_circular");
  assert.equal(classify("Draft guidelines on securitisation", "notification"), "draft");
  assert.equal(classify("Circular on gold loans", "notification"), "circular");
});

test("reference numbers are extracted from the masthead", () => {
  assert.equal(
    extractRefNo("RBI/2025-26/45 DOR.AUT.REC.No.12/24.01.001/2025-26 dated August 1, 2025"),
    "DOR.AUT.REC.No.12/24.01.001/2025-26"
  );
  assert.equal(extractRefNo("no reference here"), null);
});
