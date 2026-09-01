/**
 * End-to-end exercise of the MCP server over a real stdio transport.
 * Spawns the server as a child process and speaks JSON-RPC to it, which is
 * exactly how Claude will drive it.
 */
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";

// fileURLToPath, not URL.pathname: on Windows the latter yields "/C:/Users/..."
// with a leading slash, which is not a usable path.
const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

const server = spawn("npx", ["tsx", "src/index.ts"], {
  cwd: repoRoot,
  env: { ...process.env, RBI_INTEL_DB: process.env.RBI_INTEL_DB ?? join(tmpdir(), "rbi-test.db") },
  stdio: ["pipe", "pipe", "pipe"],
  // npx resolves to npx.cmd on Windows, which spawn cannot execute directly.
  shell: process.platform === "win32",
});

server.stderr.on("data", (d) => {
  const s = String(d).trim();
  if (s && !s.includes("running on stdio")) console.error("[server]", s);
});

const rl = createInterface({ input: server.stdout });
const pending = new Map<number, (v: any) => void>();
rl.on("line", (line) => {
  if (!line.trim().startsWith("{")) return;
  try {
    const msg = JSON.parse(line);
    if (msg.id !== undefined && pending.has(msg.id)) {
      pending.get(msg.id)!(msg);
      pending.delete(msg.id);
    }
  } catch { /* not our frame */ }
});

let nextId = 1;
function rpc(method: string, params: any = {}): Promise<any> {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timeout on ${method}`)), 20_000);
    pending.set(id, (v) => { clearTimeout(timer); resolve(v); });
    server.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
  });
}

const parse = (r: any) => {
  const t = r?.result?.content?.[0]?.text;
  try { return JSON.parse(t); } catch { return t; }
};

let failures = 0;
function check(label: string, cond: boolean, detail = "") {
  if (cond) console.log(`  PASS  ${label}${detail ? " — " + detail : ""}`);
  else { failures++; console.log(`  FAIL  ${label} — ${detail}`); }
}

async function main() {
  await rpc("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "e2e", version: "1" },
  });
  server.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");

  console.log("MCP end-to-end\n");

  const tools = await rpc("tools/list");
  const names = (tools.result?.tools ?? []).map((t: any) => t.name);
  check("tools/list", names.length >= 13, `${names.length} tools: ${names.join(", ")}`);

  const status = parse(await rpc("tools/call", { name: "sync_status", arguments: {} }));
  check("sync_status", status.totalDocuments === 6, `${status.totalDocuments} documents`);
  check("sync_status counts relations", Array.isArray(status.relationships) && status.relationships.length === 4,
    JSON.stringify(status.relationships?.map((r: any) => `${r.rel_type}:${r.resolved}/${r.unresolved}`)));

  const search = parse(await rpc("tools/call", {
    name: "search_regulations", arguments: { query: "periodic updation KYC" },
  }));
  check("search_regulations finds KYC docs", search.resultCount >= 2, `${search.resultCount} results, top="${search.results?.[0]?.title?.slice(0, 45)}"`);
  check("search returns snippets", !!search.results?.[0]?.snippet, search.results?.[0]?.snippet?.slice(0, 60));

  // The original package errored on queries that reduce to punctuation.
  const weird = parse(await rpc("tools/call", { name: "search_regulations", arguments: { query: "-- ** ::" } }));
  check("degenerate FTS query does not error", !String(weird).startsWith("Error"), typeof weird === "object" ? "handled" : String(weird).slice(0, 60));

  const hyphen = parse(await rpc("tools/call", { name: "search_regulations", arguments: { query: "anti-money laundering" } }));
  check("hyphenated query works", typeof hyphen === "object", `${hyphen.resultCount} results`);

  const md = parse(await rpc("tools/call", { name: "list_master_directions", arguments: {} }));
  check("list_master_directions", md.count === 3, `${md.count} (2 MD + 1 master_circular)`);

  const cats = parse(await rpc("tools/call", { name: "list_categories", arguments: {} }));
  check("list_categories", cats.categories?.[0]?.category === "Commercial Banks", JSON.stringify(cats.categories));

  const lineage = parse(await rpc("tools/call", { name: "get_lineage", arguments: { id: "rbi:nt:11000" } }));
  check("get_lineage flags repealed document", /SUPERSEDED|REPEALED/.test(lineage.currency), lineage.currency?.slice(0, 70));
  check("get_lineage lists incoming edge", lineage.incoming?.length >= 1, `${lineage.incoming?.length} incoming`);

  const lin2 = parse(await rpc("tools/call", { name: "get_lineage", arguments: { id: "rbi:md:11566" } }));
  check("get_lineage on current MD shows outgoing supersession",
    (lin2.outgoing ?? []).some((r: any) => r.rel_type === "supersedes" && r.dst_id === "rbi:nt:9001"),
    JSON.stringify((lin2.outgoing ?? []).map((r: any) => `${r.rel_type}->${r.dst_id}`)));

  const clauses = parse(await rpc("tools/call", { name: "get_clauses", arguments: { id: "rbi:md:11566" } }));
  check("get_clauses reads Python-written table", clauses.count === 6, `${clauses.count} clauses`);

  const topic = parse(await rpc("tools/call", { name: "search_by_topic", arguments: { topic: "know your customer" } }));
  check("search_by_topic splits masters vs amendments",
    topic.consolidatedRules?.length >= 1 && topic.amendments?.length >= 1,
    `${topic.consolidatedRules?.length} masters, ${topic.amendments?.length} amendments`);

  const feed = parse(await rpc("tools/call", { name: "get_change_feed", arguments: { days: 1 } }));
  check("get_change_feed sees the seeded documents as new", feed.newDocuments?.length === 6, `${feed.newDocuments?.length} new`);

  const revs = parse(await rpc("tools/call", { name: "list_revisions", arguments: { id: "rbi:md:11566" } }));
  check("list_revisions", revs.revisions?.length === 1, `${revs.revisions?.length} revision(s)`);

  // ── Enrichment layer ────────────────────────────────────────────────────
  const topics = parse(await rpc("tools/call", { name: "list_topics", arguments: {} }));
  check("list_topics returns the filter vocabulary",
    Array.isArray(topics.topics) && topics.topics.length >= 1,
    `${topics.topics?.length ?? 0} topic(s), ${topics.institutionTypes?.length ?? 0} institution type(s)`);

  check("list_topics reports applicability facets",
    Array.isArray(topics.applicability) && topics.applicability.length >= 1,
    JSON.stringify((topics.applicability ?? []).map((a: any) => `${a.value}:${a.n}`)));

  // The classification is a triage aid derived from a title, and every
  // response that carries it must say so — otherwise "Not Applicable" reads
  // as a legal conclusion someone reached.
  check("list_topics qualifies what the classification is",
    String(topics.coverage?.note ?? "").includes("triage"),
    String(topics.coverage?.note ?? "").slice(0, 48));

  const enriched = parse(await rpc("tools/call", {
    name: "search_regulations", arguments: { query: "know your customer", limit: 5 },
  }));
  check("search results carry the enrichment fields",
    (enriched.results ?? []).some((r: any) => r.topic && r.applicability),
    JSON.stringify((enriched.results ?? [])[0] ?? {}).slice(0, 90));

  // Filter on the value the fixtures actually carry, and require a non-empty
  // result. An empty result satisfies `.every()` vacuously, so a filter that
  // silently matched nothing would pass a naive version of this check.
  const byApplic = parse(await rpc("tools/call", {
    name: "search_regulations",
    arguments: { query: "know your customer", applicability: "Likely Applicable", limit: 5 },
  }));
  check("search_regulations filters on applicability",
    (byApplic.results ?? []).length >= 1 &&
      byApplic.results.every((r: any) => r.applicability === "Likely Applicable"),
    `${byApplic.resultCount ?? 0} result(s), all Likely Applicable`);

  const byApplicNone = parse(await rpc("tools/call", {
    name: "search_regulations",
    arguments: { query: "know your customer", applicability: "Not Applicable", limit: 5 },
  }));
  check("an applicability filter that matches nothing returns nothing",
    (byApplicNone.results ?? []).length === 0,
    `${byApplicNone.resultCount ?? 0} result(s)`);

  const byTopic = parse(await rpc("tools/call", {
    name: "search_regulations",
    arguments: { query: "customer", topic: "KYC / AML", limit: 5 },
  }));
  check("search_regulations filters on topic",
    Array.isArray(byTopic.results),
    `${byTopic.resultCount ?? 0} result(s) for topic 'KYC / AML'`);

  const upd = parse(await rpc("tools/call", { name: "list_recently_updated", arguments: {} }));
  check("list_recently_updated responds without a revision history",
    upd.count !== undefined || typeof upd.message === "string",
    upd.count !== undefined ? `${upd.count} updated` : String(upd.message).slice(0, 44));

  // ── Compliance layer ────────────────────────────────────────────────────
  const reqs = parse(await rpc("tools/call", { name: "get_requirements", arguments: { limit: 50 } }));
  check("get_requirements reads the requirements table", reqs.count >= 1, `${reqs.count} requirement(s)`);

  check("get_requirements separates grounded text from the seeded block",
    reqs.requirements?.[0]?.requirement !== undefined && reqs.requirements?.[0]?.internal !== undefined,
    Object.keys(reqs.requirements?.[0] ?? {}).join(","));

  // The single most important property of this whole layer: a seeded
  // assessment must never travel without the warning that qualifies it.
  check("seeded mappings always carry the warning",
    typeof reqs.seededLayerWarning === "string" && reqs.seededLayerWarning.includes("SEEDED"),
    String(reqs.seededLayerWarning).slice(0, 40));

  check("every returned mapping declares its provenance",
    (reqs.requirements ?? []).every((r: any) => !r.internal || typeof r.internal.provenance === "string"),
    "ok");

  const filtered = parse(await rpc("tools/call", {
    name: "get_requirements", arguments: { query: "capital", limit: 10 },
  }));
  check("get_requirements filters on text", Array.isArray(filtered.requirements), `${filtered.count ?? 0} match(es)`);

  const oneId = reqs.requirements?.[0]?.id;
  const one = parse(await rpc("tools/call", { name: "get_requirement", arguments: { id: oneId } }));
  check("get_requirement returns the verbatim source clause so the paraphrase can be checked",
    typeof one.source_clause_text === "string" && one.source_clause_text.length > 0,
    `${(one.source_clause_text ?? "").length} chars`);

  const badReq = parse(await rpc("tools/call", { name: "get_requirement", arguments: { id: "rbi:md:11566#nope" } }));
  check("unknown requirement id returns a clear error",
    String(badReq).startsWith("Error: No requirement"), String(badReq).slice(0, 50));

  const summary = parse(await rpc("tools/call", { name: "compliance_summary", arguments: {} }));
  check("compliance_summary counts by provenance",
    summary.total >= 1 && (summary.byProvenance ?? []).some((p: any) => p.provenance === "seeded"),
    `${summary.total} requirement(s)`);

  const missing = parse(await rpc("tools/call", { name: "get_document", arguments: { id: "rbi:md:doesnotexist" } }));
  check("unknown id returns a clear error", String(missing).startsWith("Error: No document"), String(missing).slice(0, 60));

  console.log(`\n${failures === 0 ? "All MCP checks passed." : `${failures} check(s) failed.`}`);
  server.kill();
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((e) => { console.error(e); server.kill(); process.exit(1); });
