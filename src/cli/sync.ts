#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { initSchema } from "../db/schema.js";
import {
  syncMasterDirections,
  syncMasterCirculars,
  syncAmendmentDirections,
  syncStandaloneCirculars,
  syncGuidanceNotes,
  syncWithdrawnDocuments,
  syncNotifications,
} from "../scrapers/rbi.js";
import * as q from "../db/queries.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function arg(name: string, fallback: number): number {
  const i = process.argv.indexOf(`--${name}`);
  if (i === -1) return fallback;
  const v = Number(process.argv[i + 1]);
  return Number.isFinite(v) ? v : fallback;
}

/**
 * Collect all values for a repeatable flag, plus comma-separated values
 * within a single flag value.
 *
 * --category "Commercial Banks" --category "Small Finance Banks"
 * --category "Commercial Banks,Small Finance Banks"
 */
function argList(name: string): string[] {
  const flag = `--${name}`;
  const out: string[] = [];
  const argv = process.argv;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === flag && argv[i + 1]) {
      argv[i + 1].split(",").map((s) => s.trim()).filter(Boolean).forEach((v) => out.push(v));
      i++;
    }
  }
  return out;
}

async function main() {
  initSchema();
  const log = (m: string) => console.error(`[sync] ${m}`);
  const force = process.argv.includes("--force");
  const skipMd  = process.argv.includes("--no-md");
  const skipMc  = process.argv.includes("--no-mc");
  const skipSc  = process.argv.includes("--no-sc");
  const skipAmd = process.argv.includes("--no-amd");
  const skipGn  = process.argv.includes("--no-gn");
  const skipWd  = process.argv.includes("--no-wd");
  const mdOnly = process.argv.includes("--md-only");
  const mdCategories = argList("category");
  const skipRelations = process.argv.includes("--no-relations");
  const skipEnrich = process.argv.includes("--no-enrich");

  // First-ever sync: go back 36 months to build history.
  // Subsequent syncs: 12 months is enough to catch anything missed.
  // --months flag always overrides; --quick overrides to 3.
  const isFirstRun = !q.getSyncMeta("last_sync");
  const defaultMonths = process.argv.includes("--quick") ? 3 : (isFirstRun ? 36 : 12);
  const months = arg("months", defaultMonths);
  if (isFirstRun) {
    log(`First sync detected — fetching ${months} months of notification history.`);
  }

  const runId = q.startSyncRun("cli");
  let inserted = 0;
  let changed = 0;
  const warnings: string[] = [];

  try {
    if (!skipMd) {
      const r = await syncMasterDirections(log, force, mdCategories);
      inserted += r.inserted;
      changed += r.changed;
      warnings.push(...r.warnings);
    }
    if (!mdOnly) {
      // ── Phase-1 sources ────────────────────────────────────────────────
      if (!skipMc) {
        const r = await syncMasterCirculars(log, force);
        inserted += r.inserted;
        changed += r.changed;
        warnings.push(...r.warnings);
      }
      if (!skipAmd) {
        const r = await syncAmendmentDirections(log, force);
        inserted += r.inserted;
        changed += r.changed;
        warnings.push(...r.warnings);
      }
      if (!skipSc) {
        const r = await syncStandaloneCirculars(log, force);
        inserted += r.inserted;
        changed += r.changed;
        warnings.push(...r.warnings);
      }
      // Guidance Notes live on the same SC page — parse them with a second
      // fetch rather than caching the HTML (keeping sync.ts simple). The SC
      // page is small and both calls are covered by the same polite 200ms delay.
      if (!skipGn) {
        const r = await syncGuidanceNotes(log, force);
        inserted += r.inserted;
        changed += r.changed;
        warnings.push(...r.warnings);
      }
      // Withdrawn document marking — updates status in-place, no inserted/changed count.
      if (!skipWd) {
        const wr = await syncWithdrawnDocuments(log);
        warnings.push(...wr.warnings);
      }
      // ── Notifications (month-by-month) ─────────────────────────────────
      log(`Notifications: last ${months} months`);
      const r = await syncNotifications(months, log, force);
      inserted += r.inserted;
      changed += r.changed;
      warnings.push(...r.warnings);
    }

    q.setSyncMeta("last_sync", new Date().toISOString());
    q.finishSyncRun(runId, inserted, changed, warnings.join("\n") || undefined);

    log(`Done. ${inserted} new, ${changed} amended.`);
    if (warnings.length) {
      log(`${warnings.length} warning(s):`);
      warnings.slice(0, 20).forEach((w) => log(`  ! ${w}`));
    }

    // Post-sync analysis. Both steps are idempotent and derive entirely from
    // data already in the database, so running them automatically costs
    // nothing on a no-op sync and stops the index from drifting out of date
    // because someone forgot a second command.
    const pythonDir = path.resolve(__dirname, "../../python");
    const pyEnv = { ...process.env, PYTHONPATH: pythonDir };
    const runPy = (args: string[], label: string, hint: string) => {
      log(`Running ${label} (python -m rbi_intel ${args.join(" ")})…`);
      const r = spawnSync("python", ["-m", "rbi_intel", ...args], {
        env: pyEnv,
        stdio: "inherit",
        shell: process.platform === "win32",
      });
      if (r.status === 0) log(`${label} done.`);
      else log(`${label} exited with code ${r.status}. Run manually: ${hint}`);
    };

    // Classify by institution type / topic / applicability. Cheap (title-only,
    // no network, no model) and it is what makes a freshly synced document
    // filterable straight away instead of only after a manual enrich.
    if (!skipEnrich) {
      runPy(["enrich"], "document enrichment",
        "set PYTHONPATH=python && python -m rbi_intel enrich");
    }

    // Rebuild the lineage/relations graph so Lineage and Diagram stay current.
    if (!skipRelations) {
      runPy(["relations"], "relations builder",
        "set PYTHONPATH=python && python -m rbi_intel relations");
    }

    process.exit(0);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    q.finishSyncRun(runId, inserted, changed, msg);
    console.error(`[sync] FAILED: ${msg}`);
    process.exit(1);
  }
}

main();
