#!/usr/bin/env node
/**
 * Create the database, run every migration, and load the reference data.
 *
 * This exists because the analysis layer can no longer assume a sync has
 * happened. On a network that cannot reach rbi.org.in — which is the network
 * this is actually deployed on — `npm run sync` never succeeds, so the
 * database was never created and every Python subcommand died with
 * "Database not found". `npm run init` is the offline entry point:
 * schema first, documents later, from wherever they can be obtained.
 *
 * Safe to re-run. Reference rows are upserted, never duplicated, and
 * existing document data is untouched.
 *
 *   npm run init
 *   npm run init -- --reseed     re-read seed/*.json over the existing rows
 */
import { readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { db, DB_PATH, initSchema } from "../db/schema.js";

const here = dirname(fileURLToPath(import.meta.url));
// Works from both src/cli (tsx) and dist/cli (built).
const seedDir = [join(here, "..", "..", "seed"), join(here, "..", "..", "..", "seed")].find(existsSync);

type Area = { id: string; name: string; description?: string };
type Owner = { id: string; role: string; line?: string };

function loadJson<T>(name: string): T[] {
  if (!seedDir) throw new Error("seed/ directory not found next to the package root");
  const p = join(seedDir, name);
  if (!existsSync(p)) throw new Error(`missing seed file: ${p}`);
  return JSON.parse(readFileSync(p, "utf-8")) as T[];
}

function seedReference(reseed: boolean): { areas: number; owners: number } {
  const areas = loadJson<Area>("business_areas.json");
  const owners = loadJson<Owner>("owners.json");

  // ON CONFLICT DO UPDATE when --reseed, DO NOTHING otherwise: a compliance
  // officer who renamed a business area in the database should not have that
  // silently reverted every time someone runs init.
  const areaSql = reseed
    ? `INSERT INTO business_areas (id,name,description) VALUES (?,?,?)
       ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description`
    : `INSERT INTO business_areas (id,name,description) VALUES (?,?,?) ON CONFLICT(id) DO NOTHING`;
  const ownerSql = reseed
    ? `INSERT INTO owners (id,role,line) VALUES (?,?,?)
       ON CONFLICT(id) DO UPDATE SET role=excluded.role, line=excluded.line`
    : `INSERT INTO owners (id,role,line) VALUES (?,?,?) ON CONFLICT(id) DO NOTHING`;

  const insArea = db.prepare(areaSql);
  const insOwner = db.prepare(ownerSql);

  db.exec("BEGIN");
  try {
    for (const a of areas) insArea.run(a.id, a.name, a.description ?? null);
    for (const o of owners) insOwner.run(o.id, o.role, o.line ?? null);
    db.exec("COMMIT");
  } catch (e) {
    db.exec("ROLLBACK");
    throw e;
  }
  return { areas: areas.length, owners: owners.length };
}

function count(table: string): number {
  const r = db.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get() as { n: number };
  return r.n;
}

function main() {
  const reseed = process.argv.includes("--reseed");

  const before = (db.prepare("PRAGMA user_version").get() as { user_version: number }).user_version;
  initSchema();
  const after = (db.prepare("PRAGMA user_version").get() as { user_version: number }).user_version;

  const seeded = seedReference(reseed);

  console.log(`database        ${DB_PATH}`);
  console.log(`schema          v${before} -> v${after}`);
  console.log(`business_areas  ${count("business_areas")} rows (${seeded.areas} in seed${reseed ? ", reseeded" : ""})`);
  console.log(`owners          ${count("owners")} rows (${seeded.owners} in seed${reseed ? ", reseeded" : ""})`);
  console.log(`documents       ${count("documents")} rows`);
  console.log(`clauses         ${count("clauses")} rows`);
  console.log(`requirements    ${count("requirements")} rows`);
  console.log(`req_mappings    ${count("req_mappings")} rows`);
  console.log("");
  console.log("Next:  npm run sync            (if rbi.org.in is reachable)");
  console.log("  or:  PYTHONPATH=python python3 -m rbi_intel ingest --file <file.pdf> ...");
}

main();
