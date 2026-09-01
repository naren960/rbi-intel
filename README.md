<p align="center">
  <img src="branding/sber-india-logo.svg" alt="Sber India" width="420"/>
</p>

<p align="center">
  <strong style="color:#1A991A;">RBI Regulatory Intelligence Platform</strong><br/>
  <em>Built for Sber India — powered by open-source tooling</em>
</p>

---

# rbi-intel

RBI regulatory intelligence: ingestion, revision tracking, document classification, a relationship graph, a clause-level obligations register, and an MCP server — built by merging the `india-reg-mcp` Node package with the Python clause pipeline from the RBI_intelligence project.

> **Sber India deployment.** This build is maintained for Sber India's compliance and regulatory monitoring workflow. Brand assets: `branding/`. Primary colour: `#1A991A`. Source: [brandfetch.com/sberbank.ru](https://brandfetch.com/sberbank.ru).

Node does acquisition and serving. Python does analysis. They meet at one SQLite file.

---

## Why both languages

Neither half was sufficient alone, and each was already good at something the other was bad at.

| | Node/TypeScript | Python |
|---|---|---|
| **Owns** | scraping, revision detection, FTS search, MCP protocol, schema migrations | local ingestion, clause chunking, relation extraction, graph analysis, requirement extraction, validation |
| **Why** | RBI's ASP.NET viewstate POST flow was already solved here; the MCP SDK is first-class in TS; `node:sqlite` + FTS5 is fast and synchronous | the chunker and fragment classifier were already validated against real RBI PDFs; regex/graph work is far more readable in Python; so is the LLM plumbing |
| **Writes** | `documents`, `document_revisions`, `sync_runs`, `business_areas`, `owners` | `clauses`, `relations`, `requirements`, `req_mappings` |

The shared database is the entire contract. Either side can be rewritten without touching the other.

```
rbi.org.in                       a PDF on disk
    │  Node scrapers                  │  python rbi.py ingest
    │  (viewstate POST, cheerio)      │  (pdfplumber, header stripping)
    ▼                                 ▼
        ~/.rbi-intel/regdata.db
    ▲                                 │
    │  Python analysis                ▼  Node MCP server — 18 tools over stdio
    │  relations · chunks · graph   Claude
    │  requirements · scaffolding     │
    └─────────────────────────────────┴──  Streamlit dashboard — 8 tabs
```

Two entry doors, because rbi.org.in is unreachable from many corporate
networks and a scraper-only design is unusable there.

---

## What was wrong with `india-reg-mcp`, and what changed

The original is a competent piece of work — the viewstate handling and FTS5 setup are kept largely intact. But for tracking amendments and building a hierarchy it had gaps that were not fixable by configuration.

**Structural gaps**

1. **Master Directions were never scraped.** Despite the README listing `BS_ViewMasterDirections.aspx` as a data source, no code ever fetched it. Only `NotificationUser.aspx` was scraped, month by month. Since RBI *edits Master Directions in place and re-dates them*, month-window scraping misses them structurally — and Master Directions are the anchor of any hierarchy. **Added** a dedicated MD scraper that also captures RBI's own department/entity grouping.

2. **No revision history.** `upsertDoc` overwrote `body` in place, so the previous text was destroyed on every re-sync. "Which directions were amended" was therefore unanswerable. **Added** a `document_revisions` table keyed on a SHA-256 content hash; every distinct text RBI serves is retained.

3. **No relationships of any kind.** Search was flat. **Added** the whole `relations` layer.

4. **SEBI code was dead.** `sebi.ts` shipped but was never imported by the server, and `run-sync` was RBI-only — the README's SEBI claims did not match the binary. **Removed** rather than left as a trap; easy to revive if you want SEBI later.

**Bugs found and fixed**

5. **Timezone date corruption.** `new Date("Aug 14, 2026").toISOString()` parses to *local* midnight then converts to UTC — in IST every date shifted back one day. Silent, systematic, and it corrupts any chronological reasoning about which circular came first. Now parsed component-wise.

6. **ID namespace collision.** Master Directions use `BS_ViewMasDirections.aspx?id=` and notifications use `NotificationUser.aspx?Id=` — *different* ID spaces with overlapping numbers. The original's `rbi:${id}` scheme would silently merge unrelated documents once MDs were added. Now `rbi:md:` and `rbi:nt:`.

7. **Misclassification of amendments.** A title substring check meant "Amendment to Master Direction on X" was classified as a Master Direction, polluting the list of consolidated rules with documents that amend them.

8. **FTS query fragility.** Tokens that reduced to nothing produced a bare `""` and an FTS5 syntax error; hyphens were deleted rather than split, so "anti-money laundering" welded into a token matching nothing.

9. **FTS index desync risk.** `initSchema()` ran `DROP TRIGGER IF EXISTS documents_ai` on *every* startup. Any write landing while the trigger was absent silently desynced the index with no way to notice. Triggers are now created once at migration time and the index is rebuilt.

---

## Install

```bash
npm install              # REQUIRED FIRST — tsx and typescript are devDependencies
npm run build
npm run init             # creates the database, runs migrations, seeds reference data
```

Or in one step: `npm run setup` (installs, builds, initialises, then runs the doctor).

`npm run init` is separate from `sync` on purpose. It needs no network, so a
machine that cannot reach rbi.org.in still gets a working database — which is
the situation this build is actually deployed into. It is safe to re-run.

> **`'tsx' is not recognized as an internal or external command`** means `npm install`
> has not run in this folder, or ran with `--production` / `--omit=dev`. The
> tarball intentionally ships without `node_modules`. Run `npm install` and retry.
>
> After `npm run build` you do not need `tsx` at all — use `npm run doctor:built`
> and `npm run sync:built`, which run plain `node` against `dist/`.

### Windows notes

- Works on Windows; paths and process spawning are handled platform-neutrally.
- **No native compilation required.** The SQLite layer uses Node's built-in
  `node:sqlite` module (available since Node 22.5.0), so `npm install` does not
  build any native addon. Visual Studio Build Tools are not needed.
- **Node 22.5.0 or later is required** (the `node:sqlite` module is experimental
  before 22.5 and stable from 22.5 onward). Use
  [nvm-windows](https://github.com/coreybutler/nvm-windows) to switch if needed:
  `nvm install 22 && nvm use 22`
- The database lands in `%USERPROFILE%\.rbi-intel\regdata.db`. Override with the
  `RBI_INTEL_DB` environment variable:
  `set RBI_INTEL_DB=C:\path\to\regdata.db` (cmd) or
  `$env:RBI_INTEL_DB="C:\path\to\regdata.db"` (PowerShell).
- **Use `python rbi.py <command>` for the analysis layer.** The older
  `PYTHONPATH=python python3 -m rbi_intel ...` form is POSIX shell syntax:
  `cmd.exe` reads the leading assignment as a program name and answers
  `'PYTHONPATH' is not recognized as an internal or external command`, and
  `python3` usually does not exist on Windows either. `rbi.py` sets up its own
  import path, so one form works on every OS. `npm run extract`, `npm run
  scaffold`, `npm run enrich` and `npm run validate` wrap the common ones.

Python needs nothing beyond the standard library for the core path:

```bash
pip install -r python/requirements.txt   # only for the optional extras
```

Database location defaults to `~/.rbi-intel/regdata.db`. Override with `RBI_INTEL_DB`.

---

## Use it

**Always run `npm run init`, then the doctor, on a new machine.**

```bash
npm run doctor
```

It checks date parsing, classification, reachability, the ASP.NET tokens, both listing parsers and body extraction — and prints what it actually saw. This exists because the original's failure mode was reporting "0 docs found" as though that were a fact about the month rather than a broken selector. `npm run doctor -- --dump` saves the live HTML into `fixtures/` so parser fixes can be tested offline.

```bash
npm run sync             # Master Directions + last 36 months of notifications
npm run sync:quick       # Master Directions + last 3 months
npm run sync:md          # Master Directions only — fast, and where amendments show up
```

Then build the graph:

```bash
python rbi.py relations
python rbi.py chunk --all-master-directions
```

### When rbi.org.in is not reachable

The scrapers are the right way in when the network allows it. When it does not,
ingest the document from disk — the rest of the pipeline is identical, and the
row is marked `source = 'local'` so a hand-placed file is never mistaken for
something verified against RBI's own site.

```bash
python rbi.py ingest \
  --file "Capital Adequacy MD.pdf" \
  --doc-id rbi:md:12798 \
  --title "Reserve Bank of India (Commercial Banks - Prudential Norms on Capital Adequacy) Directions, 2025 (Updated as on July 01, 2026)" \
  --date "November 28, 2025" \
  --category "Commercial Banks" \
  --url "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12798" \
  --chunk
```

`(Updated as on ...)` is stripped from the title and stored as the update date,
so the same document does not look like a new one after every RBI edit.
Re-ingesting an edited copy appends a `document_revisions` row rather than
overwriting — the prior text is the only evidence an amendment happened.

### The compliance layer

Chunking gives you clauses. These two steps turn clauses into something a
compliance officer can work from.

```bash
export GEMINI_API_KEY=...          # free, aistudio.google.com/apikey
python rbi.py extract  --limit 10   # test batch first
python rbi.py extract               # the full run
python rbi.py scaffold
python rbi.py validate
```

`extract` is grounded: each record paraphrases clause text that exists in the
document and can be checked against it.

**`scaffold` is not.** It drafts the policy / process / control / owner mapping
and gap assessment a consultant would sketch *before* any evidence exists.
There is no policy register behind it. Rows land in a separate `req_mappings`
table carrying `provenance = 'seeded'`, every MCP response containing one also
carries a warning, and the dashboard banners it. Promote a row to `'reviewed'`
or `'sourced'` once a person has actually checked it; `--force` will not
overwrite those.

Both steps are resumable. Records commit as they are produced, so a run cut
short by a daily quota keeps what it got and reports where it stopped — re-run
without `--force` to continue. `--provider stub` runs the whole chain offline
with canned text, which is how the test suite exercises it.

Then check the result:

```bash
python rbi.py validate    # exits 1 on any error
python rbi.py export -o inventory.json
npm run dashboard
```

`validate` catches what a type system cannot: enum drift from free-text model
output, orphaned rows, unresolved `OWN-UNMAPPED` placeholders, documents that
were chunked but never extracted, and — the sharpest check, inherited from the
original pipeline — an assessment distribution that has collapsed into a single
bucket, which means the model agreed rather than assessed.

`export` writes the legacy `inventory.json` shape, so the original React
artifact and anything else built against `rbi_branch_regulatory_inventory.json`
keeps working.

### Document enrichment — institution, topic, applicability

Every document is classified from its title into three dimensions, so the
corpus is filterable without reading it:

| | |
|---|---|
| `institution_type` | normalised regulated-entity class — *Commercial Banks*, *Urban Co-operative Banks*, *NBFC*. Distinct from `category`, which is whatever RBI's listing page happened to group it under. |
| `primary_topic` | subject, from a 99-topic dictionary in 7 families. Secondary topics and per-topic keyword scores are stored too, so a surprising classification can be explained rather than merely disbelieved. |
| `applicability` | *Applicable* / *Likely Applicable* / *Not Applicable* for a commercial bank. |

```bash
python rbi.py enrich              # classify what is new
python rbi.py enrich --force      # reclassify everything
python rbi.py enrich --report-only
python rbi.py taxonomy "Master Direction on Digital Lending"
```

It runs automatically after `sync` and after `ingest`, needs no network or
model, and derives entirely from titles already stored — so `--force` rebuilds
the lot in seconds and a taxonomy change is an ordinary edit, never a migration.
Skip it with `sync --no-enrich`.

**`applicability` is triage, not a legal determination.** *Not Applicable*
means no rule matched this bank, and the rule that fired is recorded next to it
in `applicability_rule` so a surprising call can be traced to its cause.

The taxonomy lives in **`seed/taxonomy.json`** and is read by both languages —
`python/rbi_intel/taxonomy.py` and `src/util/taxonomy.ts`. Do not fork it; a
parity test runs both matchers over a 60-title corpus and compares every field.
Each entry records whether it came from the Sber dictionary or was added, so the
two remain reviewable apart.

Watch the coverage number rather than the totals. A keyword taxonomy is only as
good as its fit to the corpus it actually meets, so `enrich --report-only`
prints the unclassified titles — that list is the to-do for the next few
keywords, and a rising share is the signal that RBI's vocabulary has moved.

### Choosing an LLM provider

One module, `python/rbi_intel/llm.py`, backs every LLM call.

| | |
|---|---|
| `RBI_INTEL_LLM=gemini` | free tier, rate-limited to ~13 RPM. The default when `GEMINI_API_KEY` is set. |
| `RBI_INTEL_LLM=anthropic` | paid. The default when only `ANTHROPIC_API_KEY` is set. |
| `RBI_INTEL_LLM=stub` | offline, deterministic, free. No key, no network. |
| `RBI_INTEL_MODEL=...` | override the model for either provider. |

A per-minute 429 is retried using the provider's own suggested delay. A
**daily** quota exhaustion is not — it is detected, reported, and raised
immediately, because retrying it only burns wall-clock time until tomorrow.

Query it:

```bash
python rbi.py lineage rbi:md:11566
python rbi.py diagram rbi:md:11566 --html -o lineage.html
python rbi.py diagram --category "Commercial Banks" --html -o commercial.html
python rbi.py stats
```

Register the MCP server:

```bash
claude mcp add -s user rbi-intel node /ABSOLUTE/PATH/TO/rbi-intel/dist/index.js
```

Keep it fresh — Master Directions change in place, so a weekly re-sync is what surfaces amendments:

```cron
0 2 * * 0 cd /path/to/rbi-intel && npm run sync:quick >> ~/.rbi-intel/sync.log 2>&1 && python rbi.py relations >> ~/.rbi-intel/sync.log 2>&1
```

---

## MCP tools (18)

| Tool | What it answers |
|---|---|
| `search_regulations` | full-text search, filterable by type/category/status **and institution type / topic / applicability** |
| `get_document` | full text + metadata + revision count |
| `get_recent` | what RBI issued lately |
| `list_master_directions` | the consolidated in-force rules |
| `list_categories` | valid category filter values, with counts |
| `search_by_topic` | consolidated rules **and** amendments **and** related circulars |
| `get_lineage` | what a document supersedes/amends, and what has since superseded it |
| `get_change_feed` | new vs amended documents in a window |
| `list_revisions` | audit trail of in-place edits |
| `diff_revisions` | the before and after text |
| `get_clauses` | clause-level breakdown |
| `list_topics` | the enrichment vocabulary — valid institution / topic / applicability filter values |
| `list_recently_updated` | which Master Directions RBI has re-dated, newest first |
| `get_requirements` | **what a bank must actually do** — obligations, deadlines, thresholds |
| `get_requirement` | one requirement plus the verbatim clause it came from |
| `compliance_summary` | coverage: requirements by assessment, severity, provenance, business area |
| `sync_latest` | incremental scrape |
| `sync_status` | index health |

---

## How relationships are extracted

RBI states lineage in prose, not metadata, so extraction is two stages kept deliberately separate:

1. **Trigger** — a phrase establishing a relationship *type*: `in supersession of`, `stand repealed`, `in partial modification of`, `stands withdrawn`, `consolidates the instructions`.
2. **Reference** — a document identifier near that trigger: a departmental reference (`DBOD.No.BP.BC.9/21.04.048/2014-15`), an `RBI/2025-26/74` number, or a `circular ... dated <date>` phrase.

References are then resolved against the index. Three safeguards matter:

- **Masthead-only aliasing.** A document is only aliased by identifiers in its first 400 characters. Scanning the whole body registered every *cited* circular as an alias for the *citing* one, and the resolver then confidently pointed edges at the wrong document. This was caught in testing and is now a regression test.
- **Chronology guard.** A document cannot supersede, repeal or amend something published after it. Later-dated candidates are rejected outright.
- **Unresolved edges are kept.** A reference that matches no indexed document is stored with `dst_id` NULL. That is your backlog of documents worth fetching — discarding it would hide the gap.

Every edge carries the sentence it came from, plus a confidence combining trigger strength with resolution quality. Nothing is a black box: `get_lineage` returns the evidence text.

**Deliberately regex-first, not LLM-first.** RBI's phrasing is formulaic, the rules are auditable, it costs nothing, and it runs offline. An LLM pass is worth adding later for unusual phrasings — as a supplement writing `method='llm'` rows, not a replacement.

---

## Testing, and what is *not* tested

```bash
npm test                                                 # 24 Node tests
python tests/test_relations.py                           # 14 Python tests
python tests/test_taxonomy.py                            # 17 Python tests
python tests/test_compliance.py                          # 16 Python tests

# 34 MCP checks — seed runs the Python analysis and the stub-provider
# extraction automatically, so the compliance tools have rows to read.
export RBI_INTEL_DB=/tmp/rbi-test.db
npx tsx src/cli/init.ts
npx tsx tests/seed.ts && npx tsx tests/mcp-e2e.ts
unset RBI_INTEL_DB
```

Or: `npm run test:all`.

On Windows (cmd):
```cmd
set RBI_INTEL_DB=%TEMP%\rbi-test.db
npx tsx src/cli/init.ts
npx tsx tests/seed.ts && npx tsx tests/mcp-e2e.ts
set RBI_INTEL_DB=
```

Covered end-to-end offline: schema and migrations, revision detection, change-feed classification, FTS behaviour including the degenerate cases, listing parsers, ID namespacing, the classifier, relation extraction across all four lineage verbs, reference resolution safety, chunking and clause labelling, local ingestion and its revision handling, title normalisation, date parsing, requirement extraction and its resumability under quota exhaustion, seeded-mapping provenance, validation, export, and every MCP tool driven over a real stdio JSON-RPC transport.

The compliance tests use `--provider stub`, so they need no API key and make no
network call — the plumbing is verified independently of whether any provider
is reachable, which on the target network is a live question.

**Not covered, and you should verify it yourself:** the live network path. The environment this was built in cannot reach `rbi.org.in` at the socket level, so the scrapers have never run against the real site from here. The parser fixtures are *reconstructions* of RBI's markup based on the live URL patterns and page structure, not captures — they prove the logic, not that the CSS selectors still match today.

This is exactly what `npm run doctor` is for. Run it first. If a selector has drifted, it will tell you which one and what it saw instead, and `--dump` gives you the HTML to fix the fixture against.

---

## Next

The graph is built but thin until a full sync populates it. In rough order of value:

1. Run `init`, then `doctor`, then a full `sync`, then `relations` — see what the real resolution rate looks like.
2. Tune the heading detection in `parseMasterDirectionList` against real markup (the doctor reports how many MDs got no department heading).
3. Add an LLM pass over the unresolved references and unusual phrasings, writing `method='llm'` edges alongside the regex ones. `llm.py` is in place for it.
4. Review the `source: "added"` half of `seed/taxonomy.json` against your own judgement — 39 topics and 9 institution classes were drafted from public RBI vocabulary rather than inherited, and they are marked as such precisely so they can be checked.
5. Reconcile the seeded `req_mappings` against a real policy register and promote the rows to `provenance = 'sourced'`. Until that happens the internal layer is illustrative and nothing else.

---

<p align="center">
  <img src="branding/sber-india-logo.svg" alt="Sber India" width="220"/><br/>
  <sub>© Sber India &nbsp;·&nbsp; Brand colours: <code>#1A991A</code> (Forest Green) · <code>#262626</code> (Charcoal) &nbsp;·&nbsp; <a href="https://brandfetch.com/sberbank.ru">brandfetch.com/sberbank.ru</a></sub>
</p>
