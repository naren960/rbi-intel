/**
 * Seeds the database with documents whose wording is modelled on real RBI
 * prose, so the relation extractor is exercised against the phrasings it
 * will actually meet rather than against sentences invented to suit it.
 *
 * Also runs the Python analysis layer (relations + chunking) so the seeded
 * database is in the same state as a post-sync post-analysis production DB.
 *
 * Run: RBI_INTEL_DB=/tmp/test.db npx tsx tests/seed.ts
 */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { initSchema } from "../src/db/schema.js";
import { upsertDocs, type DocInput } from "../src/db/queries.js";

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

initSchema();

const doc = (o: Partial<DocInput> & { id: string; title: string; date: string; body: string }): DocInput => ({
  regulator: "RBI",
  doc_type: "circular",
  department: null,
  category: null,
  ref_no: null,
  source_url: `https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=${o.id.split(":").pop()}`,
  pdf_url: null,
  indexed_at: new Date().toISOString(),
  ...o,
});

const docs: DocInput[] = [
  doc({
    id: "rbi:nt:9001",
    doc_type: "master_circular",
    title: "Master Circular on Know Your Customer (KYC) Norms / Anti-Money Laundering Standards",
    date: "2013-07-01",
    category: "Commercial Banks",
    ref_no: "DBOD.AML.BC.No.15/14.01.001/2013-14",
    body:
      "RBI/2013-14/94 DBOD.AML.BC.No.15/14.01.001/2013-14 dated July 1, 2013. " +
      "Master Circular on Know Your Customer Norms. In exercise of the powers conferred by Section 35A of the " +
      "Banking Regulation Act, 1949, the Reserve Bank of India issues the following directions. " +
      "1. Banks shall put in place a comprehensive policy framework covering Customer Acceptance Policy. " +
      "2. Banks shall carry out periodic updation of KYC records at least once in every ten years for low risk customers.",
  }),

  doc({
    id: "rbi:md:11566",
    doc_type: "master_direction",
    title: "Master Direction - Know Your Customer (KYC) Direction, 2016",
    date: "2016-02-25",
    category: "Commercial Banks",
    department: "Department of Regulation",
    ref_no: "DBR.AML.BC.No.81/14.01.001/2015-16",
    body:
      "RBI/2015-16/42 DBR.AML.BC.No.81/14.01.001/2015-16 dated February 25, 2016. " +
      "In exercise of the powers conferred under Section 35A of the Banking Regulation Act, 1949, the Reserve Bank " +
      "of India issues the Directions hereinafter specified. " +
      "In supersession of the Master Circular DBOD.AML.BC.No.15/14.01.001/2013-14 dated July 1, 2013, " +
      "the instructions contained herein shall apply. " +
      "CHAPTER I\n" +
      "1. Short title and commencement. These Directions shall be called the Reserve Bank of India (Know Your Customer) Directions, 2016. " +
      "2. Applicability. The provisions of these Directions shall apply to every entity regulated by the Reserve Bank of India. " +
      "3. Definitions. In these Directions, unless the context otherwise requires, the terms herein shall bear the meanings assigned to them. " +
      "CHAPTER II\n" +
      "4. Customer Acceptance Policy. Regulated Entities shall frame a Customer Acceptance Policy and ensure that no account is opened in anonymous or fictitious name. " +
      "5. Periodic updation. Regulated Entities shall carry out periodic updation at least once in every two years for high risk customers, " +
      "once in every eight years for medium risk customers and once in every ten years for low risk customers. " +
      "AAA to AA All 0.00\n" +
      "6. Record management. Regulated Entities shall maintain records of all transactions for a period of at least five years.",
  }),

  doc({
    id: "rbi:nt:13675",
    doc_type: "amendment",
    title: "Amendment to Master Direction on Know Your Customer (KYC) Direction, 2016",
    date: "2026-08-10",
    category: "Commercial Banks",
    ref_no: "DOR.AML.REC.44/14.01.001/2026-27",
    body:
      "RBI/2026-27/58 DOR.AML.REC.44/14.01.001/2026-27 dated August 10, 2026. " +
      "In partial modification of our Master Direction - Know Your Customer (KYC) Direction, 2016 dated February 25, 2016, " +
      "it has been decided to revise the periodicity of updation. " +
      "Accordingly, paragraph 5 of the said Master Direction is amended to read as under. " +
      "Regulated Entities shall ensure that customer due diligence records are updated at the intervals specified.",
  }),

  doc({
    id: "rbi:nt:11000",
    doc_type: "circular",
    title: "Prudential Guidelines on Credit Exposure Norms",
    date: "2014-07-15",
    category: "Commercial Banks",
    ref_no: "DBOD.No.BP.BC.9/21.04.048/2014-15",
    body:
      "RBI/2014-15/103 DBOD.No.BP.BC.9/21.04.048/2014-15 dated July 15, 2014. " +
      "Please refer to our circular DBOD.No.BP.BC.31/21.04.048/2012-13 dated August 2, 2012 on the captioned subject. " +
      "Banks shall not have exposure to a single borrower exceeding fifteen per cent of capital funds.",
  }),

  doc({
    id: "rbi:md:13136",
    doc_type: "master_direction",
    title: "Reserve Bank of India (Credit Risk Management) Directions, 2025",
    date: "2025-11-28",
    category: "Commercial Banks",
    department: "Department of Regulation",
    ref_no: "DOR.CRE.REC.No.62/21.04.048/2025-26",
    body:
      "RBI/2025-26/74 DOR.CRE.REC.No.62/21.04.048/2025-26 dated November 28, 2025. " +
      "In exercise of the powers conferred by Sections 21 and 35A of the Banking Regulation Act, 1949, " +
      "the Reserve Bank of India issues these Directions. " +
      "This Master Direction consolidates the instructions contained in the circulars listed below. " +
      "The instructions contained in the following circulars stand repealed with effect from the date of these Directions: " +
      "circular DBOD.No.BP.BC.9/21.04.048/2014-15 dated July 15, 2014 on Prudential Guidelines on Credit Exposure Norms. " +
      "CHAPTER I\n" +
      "1. Short title. These Directions shall be called the Reserve Bank of India (Credit Risk Management) Directions, 2025. " +
      "2. Applicability. These Directions shall apply to all Commercial Banks excluding Regional Rural Banks. " +
      "CHAPTER II\n" +
      "3. Credit risk governance. The Board of the bank shall approve a credit risk management policy which shall be reviewed at least annually. " +
      "4. Exposure limits. Banks shall ensure that exposure to a single counterparty does not exceed twenty per cent of eligible capital base. " +
      "ANNEX I\n" +
      "1. Illustrative computation of exposure. Exposure MV 1050 Haircut 0 % Adjusted 1050",
  }),

  doc({
    id: "rbi:nt:12500",
    doc_type: "circular",
    title: "Withdrawal of circulars on Discretionary Lending Limits",
    date: "2026-03-05",
    category: "Commercial Banks",
    ref_no: "DOR.CRE.REC.18/21.04.048/2025-26",
    body:
      "RBI/2025-26/119 DOR.CRE.REC.18/21.04.048/2025-26 dated March 5, 2026. " +
      "On a review, it has been decided that the instructions contained in circular " +
      "DBOD.No.BP.BC.31/21.04.048/2012-13 dated August 2, 2012 stand withdrawn with immediate effect.",
  }),
];

const r = upsertDocs(docs);
console.log(JSON.stringify({ seeded: docs.length, ...r }, null, 2));

/** Run Python analysis so relations + clauses are populated for the MCP e2e tests. */
function py(args: string[]): void {
  const env = { ...process.env, PYTHONPATH: join(repoRoot, "python") };
  const res = spawnSync("python3", ["-m", "rbi_intel", ...args], {
    cwd: repoRoot,
    env,
    encoding: "utf-8",
    // On Windows: python may be `python` not `python3`
    shell: process.platform === "win32",
  });
  if (res.error) {
    // Python not installed — skip silently; MCP lineage/clause checks will
    // fail but the rest of the e2e suite still runs.
    console.error(`[seed] python3 unavailable — skipping ${args[0]}:`, res.error.message);
    return;
  }
  if (res.status !== 0) {
    console.error(`[seed] python3 -m rbi_intel ${args.join(" ")} exited ${res.status}:`);
    if (res.stderr) console.error(res.stderr.trim());
    return;
  }
  if (res.stdout.trim()) console.log(res.stdout.trim());
}

py(["relations"]);
// Chunk by explicit ID rather than --all-master-directions, which filters
// LENGTH(body) > 2000 to skip real PDF stubs — the test bodies are shorter.
for (const id of ["rbi:md:11566", "rbi:md:13136"]) {
  py(["chunk", id]);
}

// Compliance layer, via the stub provider: no API key, no network, no cost.
// The MCP tools that read `requirements` and `req_mappings` need rows to read.
// Classify by institution type / topic / applicability so the enrichment
// tools and filters have something to read.
py(["enrich", "--force"]);
py(["extract", "--provider", "stub"]);
py(["scaffold", "--provider", "stub"]);
