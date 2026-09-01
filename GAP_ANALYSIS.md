# RBI-Intel: Python Pipeline vs Node.js — Gap Analysis

_Generated from a cell-by-cell review of `RBI_CIRCULARS_UPDATE_PAGE.py` on 2026-08-18._
_Updated 2026-08-18 after merging the `rbi_pipeline` clause/requirement scripts (v3.1.0)._
_Updated 2026-08-18 after porting the D.4–D.6 enrichment classifiers (v3.2.0). P1–P3 now closed._

---

## Status Key

| Symbol | Meaning |
|--------|---------|
| ✅ | Implemented in Node.js (equivalent or better) |
| 🆕 | Implemented in this session (was missing, now added) |
| ⚠️ | Partial — some aspects missing |
| ❌ | Not yet implemented |
| 🚫 | Not applicable (Confluence publisher, local PDF storage) |

---

## Section A — Configuration & Utilities

| Python cell | Description | Node.js status |
|-------------|-------------|----------------|
| A.1 Config | RBI URLs, output paths, category prefixes | ✅ URLs in `rbi.ts`; paths via `RBI_INTEL_DB` env var |
| A.2 Logging | `logging.basicConfig` | ✅ `console.error` via Logger type |
| A.3 HTTP session | `requests.Session` with retry adapter | ✅ `politeFetch()` in `util/http.ts` with retry |
| A.4 `add_repository_key()` | Canonical `{PREFIX}-{Id}` key per category | ✅ Doc IDs: `rbi:md:123`, `rbi:nt:123` etc. |
| A.5 `sleep_politely()` | 200 ms delay between requests | ✅ `sleep(200)` between body fetches |
| A.6 `create_session()` | HTTP session factory | ✅ `politeFetch()` |

---

## Section B — Acquisition Infrastructure

| Python cell | Description | Node.js status |
|-------------|-------------|----------------|
| B.1 `crawl_page()` | Fetch one URL with retry | ✅ `politeFetch()` |
| B.2 `download_one()` | Download one PDF to local file | 🚫 Node.js stores body text in DB, not PDFs on disk |
| B.3 `download_batch()` | Parallel PDF downloader | 🚫 Same as above; `extractPdfText()` is inline |
| B.4 `verify_downloads()` | Verify downloaded PDFs | 🚫 Not applicable — no local PDF files |
| B.5 Retry logic | Max retries with back-off | ✅ Built into `politeFetch()` |

**Design difference:** The Python pipeline downloads PDFs to disk for further analysis; Node.js extracts text in-memory via `pdf-parse` and stores only the text in the database. Both approaches are valid; the Node.js approach uses less disk and is simpler to maintain.

---

## Section C — Parsers & Repository Builder

| Python cell | Description | Node.js status |
|-------------|-------------|----------------|
| C.1 `get_tables()` | Fetch all source page HTML into a dict | ✅ Per-source fetch functions in `rbi.ts` |
| C.2 `is_rbi_date()`, `extract_detail_url()`, `extract_document_id()` | URL/date helpers | ✅ Equivalent helpers in `parse.ts` |
| C.3 `repository_table_score()` / `find_repository_table()` | Score-based table locator | ⚠️ Node.js uses simpler selector-based approach (`td.tableheader`, `a.link2`/`a.links`). Works for current RBI markup; lacks the scoring fallback. |
| C.4 `parse_repository_page()` | Generic MD/MC/AMD parser | ✅ `parseRepositoryPage()` in `parse.ts` |
| C.5 `parse_master_directions()` | Master Directions | ✅ `parseMasterDirectionList()` |
| C.6 `parse_master_circulars()` | Master Circulars | ✅ `parseMasterCircularList()` |
| C.7 `parse_amendment_directions()` | Amendment Directions | ✅ `parseAmendmentDirectionList()` |
| C.8 `parse_guidance_notes()` | Guidance Notes (from SC page) | 🆕 `parseGuidanceNoteList()` + `syncGuidanceNotes()` |
| C.9 `parse_standalone_circulars()` | Standalone Circulars | ✅ `parseStandaloneCircularList()` |
| C.10 `parse_notifications()` | Notifications | ✅ `parseNotificationList()` |
| C.11 `PARSER_REGISTRY` / per-parser error guard | Fail-safe per-category parsing | ✅ Per-function try/catch in `sync.ts` |
| C.12 `build_repository_frame()` | Dedup, scope filter, integer ID | ✅ `upsertDocs()` deduplicates by ID; no scope filter (intentional — Node.js keeps everything) |
| C.13 Bulk PDF download | Download all PDFs in parallel | 🚫 Not applicable |
| C.14 Failed downloads report | Track download failures | 🚫 Not applicable |
| C.15 Verify + save logs | Verify PDF integrity | 🚫 Not applicable |

---

## Section D — Repository Enrichment

| Python cell | Description | Node.js status |
|-------------|-------------|----------------|
| D.1 `parse_date()` | Parse mixed-format dates | ✅ `toISODate()` in `util/date.ts` |
| D.2 `normalize_title()` | Extract "(Updated as on ...)" suffix | 🆕 `normalise_title()` in `python/rbi_intel/ingest.py`. Title is stored clean; the update date is parsed out. |
| D.3 `enrich_dates()` | Add Publication/Updated dates and years | ⚠️ Partial. `updated_date` is captured (in `sync_meta`, keyed `updated_date:<doc_id>`) and dates are parsed component-wise so IST does not shift them. No `publication_year` / `has_update` columns yet. |
| D.4 Institution type detection | Match title to INSTITUTION_TYPES list | 🆕 `taxonomy.institution_type()` in both languages. 30 normalised classes, 123 surface forms. Stored in `documents.institution_type`. |
| D.5 `REGULATORY_TOPIC_DICTIONARY` | 50+ topic keyword lists | 🆕 `seed/taxonomy.json` — all 60 original topics preserved, 39 added, 460 keywords, grouped into 7 families. |
| D.6 `classify_regulatory_topics()` | Multi-label topic classification | 🆕 `taxonomy.topics_for()`. Primary + ranked secondaries + per-topic scores, all stored. |
| D.6 `classify_applicability()` | Applicable / Likely / Not Applicable | 🆕 `taxonomy.applicability()`. Rule order preserved exactly; three matching bugs fixed (see below). |
| D.7 Build `library_df` | Assemble all enriched columns | 🆕 `python rbi.py enrich` writes them onto `documents` (schema v6). Runs automatically after `sync` and `ingest`. |
| D.8 Enrichment quality report | Institution / topic / applicability stats | 🆕 `enrich --report-only`, the Overview tab, and the `list_topics` MCP tool. Reports unclassified titles so gaps are actionable. |
| D.9 Download/verification merge | Merge download status onto library | 🚫 Not applicable. |
| D.10 Excel workbook export | Multi-sheet `.xlsx` with formatting | ❌ Not needed for API/MCP use case; could be added as a separate export command. |
| D.11.1 Withdrawn Repository Builder | Scrape withdrawn circulars page | 🆕 `syncWithdrawnDocuments()` + `parseWithdrawnCirculars()` — marks `status = 'withdrawn'` in DB. |
| D.11.2 Withdrawn Document Matcher | Match withdrawn list against library | 🆕 Done inside `syncWithdrawnDocuments()` via `docsByRbiId()`. |
| D.12 Circular number extraction | Regex to extract DOR.REC.No.x/yyyy-yy | ✅ `extractRefNo()` in `rbi.ts` — same regex approach. |

---

## Section E — Presentation (Confluence HTML)

| Python cell | Description | Node.js status |
|-------------|-------------|----------------|
| E.1 `build_presentation_df()` | Sort by applicability | 🚫 Confluence-specific |
| E.2 Cell renderers | HTML-escape, link, date format | 🚫 Confluence-specific |
| E.3–E.4 `build_confluence_page()` | Full storage-format HTML | 🚫 Confluence-specific |

---

## Section F — Publisher (Confluence)

| Python cell | Description | Node.js status |
|-------------|-------------|----------------|
| F.1–F.5 Confluence session + update | Authenticated PAT publish | 🚫 Not needed. Node.js exposes data via MCP tools / CLI. |

---

## What Was Implemented in This Session

### 1. MD/MC sync speed fix (queries.ts + rbi.ts)
`materialise()` now accepts `"date-title"` as a third skip mode. For Master Directions and Master Circulars it skips the body HTTP fetch when the listing date AND title are unchanged vs. the stored row. On a warm database with 382 MDs and none amended, this eliminates all 382 body fetches — the listing fetch still happens, but body fetch cost drops from ~10 minutes to ~10 seconds.

### 2. Guidance Notes (parse.ts + rbi.ts + sync.ts)
`parseGuidanceNoteList()` extracts links starting with "Guidance Note" from the Standalone Circulars page. `syncGuidanceNotes()` indexes them with `doc_type = 'guidance_note'`. Flag: `--no-gn`.

### 3. Withdrawn Documents (schema.ts + queries.ts + parse.ts + rbi.ts + sync.ts)
Schema v4 adds `withdrawn_reason`, `withdrawn_date`, `withdrawn_at` columns to `documents`. `parseWithdrawnCirculars()` handles both RRA 2.0 and departmental table layouts. `syncWithdrawnDocuments()` matches via RBI document ID in `source_url` and calls `markWithdrawn()`. Flag: `--no-wd`.

---

## What Was Implemented in the Merge (v3.1.0)

The standalone `rbi_pipeline` Python scripts are now part of this package.

### 4. Schema v5 — the compliance layer
`business_areas`, `owners` and `req_mappings`. The internal mapping lives in a
table of its own, not as columns on `requirements`, with a mandatory
`provenance` column. Grounded extraction and seeded scaffolding are structurally
separable rather than separable-if-you-remember.

Also `documents.source` ('rbi' | 'local'), and `requirements.branch_relevance`
/ `requirements.needs_review`.

### 5. `npm run init` — offline entry point
Creates the database, runs every migration, seeds reference data. Needs no
network, so a machine that cannot reach rbi.org.in still gets a working
database. Previously every path began at `npm run sync`, which on that network
never succeeds.

### 6. `python -m rbi_intel ingest` — local PDF/DOCX/TXT
Port of `00_ingest_local.py`, writing straight into `documents` with
`source='local'`, full revision handling, header/footer stripping and title
normalisation.

### 7. `python rbi.py extract` — the previously-dead `requirements` table
Port of `03_extract_requirements.py`. The v3 schema declared `requirements` and
nothing ever wrote to it. Keys are now the clause id (`rbi:md:12798#CHIV-A-106`)
rather than a per-run counter, so they are stable across re-runs. Resumable:
rows commit as produced and a quota-terminated run reports where it stopped.

### 8. `python rbi.py scaffold` — the internal layer
Port of `04_scaffold_internal_layer.py`, off its dead `import anthropic`.
Writes `req_mappings` with `provenance='seeded'`. `--force` refuses to overwrite
rows a human has promoted to `'reviewed'` or `'sourced'`.

### 9. `python/rbi_intel/llm.py` — provider-agnostic LLM access
Port of `gemini_helper.py`, generalised over Gemini, Anthropic and an offline
`stub`. Keeps the free-tier behaviour that matters: a per-minute 429 is retried
with Google's own suggested delay; a **daily** quota exhaustion is detected and
raised immediately instead of being retried for half an hour.

### 10. `python rbi.py validate` — data integrity
Port of `05_validate.py` and `schema.Inventory.cross_reference_errors()`,
restricted to what SQLite constraints cannot express: enum drift from free-text
model output, unresolved `OWN-UNMAPPED` / `BA-99` placeholders, coverage holes,
and a collapsed assessment distribution.

### 11. `python rbi.py export` — legacy `inventory.json`
Keeps the original React artifact and anything else built against
`rbi_branch_regulatory_inventory.json` working.

### 12. Streamlit — Requirements and Ask tabs
The two things the SQLite dashboard lacked and the file-based one had. `Ask`
ports the weighted scorer from `06_query_cli.py` as an FTS5-plus-field-weight
hybrid, and additionally scores the **source clause text** — the only grounded
field, and absent from the original, which only ever held the paraphrase.

### 13. MCP — `get_requirements`, `get_requirement`, `compliance_summary`
13 tools became 16. Every response carrying a seeded mapping also carries the
warning that qualifies it, and this is asserted in the e2e suite.

### 14. Chunker bug — parenthesised vs bare numbering
Not a port; found while running the merged pipeline over the real Capital
Adequacy MD. RBI runs two independent numbering sequences inside one chapter —
`1.` `2.` `3.` for paragraphs and `(1)` `(2)` `(3)` for definitions — and both
reduced to the same label, collided, and were suffixed `-dup2` **and flagged
`needs_review`**. `extract` skips flagged clauses, so 296 of 729 clauses (41%
of the document, including nearly every definition and the entire AT1/Tier-2
criteria) would have vanished from the requirements layer with no error
anywhere.

Three changes: the label records its numbering form; lettered sub-sections
(`A`, `B`, `C`) are tracked as a namespace level, since RBI restarts `(1)` under
each; and a label collision no longer implies suspect content — `needs_review`
now answers "is the TEXT usable", suffixing answers "is the LABEL unique".
Result on that document: 296 flagged → 0, with labels like `CHI-C-(1)` that
match how a clause would actually be cited.

---

## Remaining Gaps (Priority Order)

### ~~P1 — Title normalisation (D.2 / D.3)~~ — CLOSED (v3.2.0)
`normalise_title()` and `parse_loose_date()` in `ingest.py`; `updated_date`,
`publication_year` and `has_update` promoted to real columns in schema v6, with
a fallback that reads forward any date a v5 database left in `sync_meta`.
Surfaced as the `list_recently_updated` MCP tool, which answers "which
consolidated rules changed recently" without needing a revision history at all.

### ~~P2 — Institution type + regulatory topic (D.4 / D.5 / D.6)~~ — CLOSED (v3.2.0)
### ~~P3 — Applicability classification (D.6)~~ — CLOSED (v3.2.0)

See "Enrichment taxonomy" below. Filterable from `search_regulations`, the
Search tab, and the `list_topics` vocabulary tool.

### Enrichment taxonomy — how it is built (v3.2.0)

**One file, two matchers.** `seed/taxonomy.json` is the single source of truth,
compiled by `python/rbi_intel/taxonomy.py` and `src/util/taxonomy.ts`. Python
classifies in bulk; Node classifies at write time so a freshly synced document
is filterable immediately. Two implementations of one rule set is exactly the
arrangement that drifts silently, so `tests/taxonomy.test.ts` and the parity
check inside it run both over a 60-title corpus and compare every field —
540 assertions. Neither language can move alone.

**Provenance is recorded per entry.** `source: "sber"` marks the taxonomy from
`RBI_CIRCULARS_UPDATE_PAGE.py`; `source: "added"` marks entries drafted from
public RBI vocabulary, which should be reviewed before being relied on. All 60
original topics survive the merge, asserted by a test.

| | Sber | Added | Total |
|---|---|---|---|
| Topics | 60 | 39 | **99** |
| Keywords | 331 | 129 | **460** |
| Institution classes | 21 | 9 | **30** |

**Three defects fixed rather than transcribed.** Each has a regression test
written against the wrong behaviour.

1. **Unanchored acronyms.** The exclusion list ran as
   `re.search(re.escape(p), title, IGNORECASE)` — no word boundary. `LAB`
   (Local Area Banks) therefore matched inside "avai**lab**le",
   "col**lab**orative" and "**Lab**our". A Master Direction on *Available for
   Sale* categories was classified **Not Applicable** and dropped from the
   applicable set with no error anywhere. Acronyms now require boundaries and
   exact case.

2. **Trailing `\b` after a bracket.** `\bScheduled Commercial Banks \(SCB\)\b`
   needs a word character straight after the `)`; in every real title the next
   character is a space, so that entry could never fire. Boundaries are now
   `(?<!\w)` / `(?!\w)` lookarounds, which is what was meant.

3. **`APPLICABLE_PATTERNS` missing commas.** The three `"Guidance Note on ..."`
   strings had no commas between them, so Python concatenated them into one
   187-character string that matches nothing. The list was also dead — nothing
   read it, because `classify_applicability()` uses inline regexes. Applicability
   now derives its exclusions from the institution taxonomy itself, so a surface
   form added in one place cannot go missing from the other.

Two further changes, less severe but the same class of problem: institution
matching walked the list in the order it was written and returned the first
hit, so "Commercial Banks" beat "Scheduled Commercial Banks" on a title
containing both — specificity is now explicit; and topic ties resolved in dict
insertion order, which was stable but arbitrary and invisible — ties now break
on longest matched keyword, then name.

**Coverage on a real-title corpus** went from 20% unclassified to 3% after the
report surfaced which titles were missing keywords. The two remaining are
correct: one is genuinely off-taxonomy, one is a nonsense control.

### P4 — C.3 Table scorer for resilient parsing
**Impact:** If RBI redesigns a listing page, the current CSS-class selectors (`td.tableheader`, `a.link2`) will return 0 items silently. The Python scoring approach would fall back gracefully.

**Suggested fix:** Optional enhancement; current approach works and `npm run doctor` would surface failures.

### P5 — Excel / CSV export
**Partly addressed.** The Requirements tab has a "Download filtered set (CSV)"
button, and `python rbi.py export` writes the legacy `inventory.json`.

**Still open:** a multi-sheet formatted `.xlsx` of the *document library*
(D.10), as distinct from the requirements. Low priority for an API-first use
case; `openpyxl` from the Python side would be less work than `exceljs`.

---

## UI and CLI fixes (v3.3.0)

### 15. `rbi.py` — a launcher that works on Windows
Every documented Python command was `PYTHONPATH=python python3 -m rbi_intel ...`,
which is POSIX shell syntax. `cmd.exe` reads the leading assignment as a program
name and answers `'PYTHONPATH' is not recognized as an internal or external
command`; `python3` generally does not exist on Windows either. So every
instruction the dashboard and README gave a Windows user failed on its first
token — which is also why the Ask and Requirements tabs stayed empty: the
commands they told you to run could not run. `python rbi.py <cmd>` sets up its
own import path and works everywhere. `npm run extract|scaffold|enrich|validate`
wrap the common ones.

### 16. "Open in Document viewer" did nothing
`goto()` set an `_page_idx` that the sidebar radio read through `index=`. Once a
keyed widget exists, Streamlit takes its value from `session_state[key]` and
ignores `index=` entirely — so the button set a variable, triggered a rerun and
landed on the same page. Navigation now writes the radio's own key before the
widget is instantiated.

### 17. Lineage now follows the open document
Two faults. The text box read `session_state["lineage_id"]` while its widget key
was `lineage_doc_id` — a name nothing ever wrote. And seeding the widget key
from another page does not survive: Streamlit collects the state of widgets that
were not rendered in a run. Lineage now syncs from `doc_id`, a plain non-widget
pointer, via a marker recording which document it last followed — so it tracks
the viewer without overwriting anything typed by hand.

### 18. Requirements renamed to Obligations
The records are obligations on the bank — the schema already calls the field
`obligation_type` — and "Requirements" invites the software reading in a repo
that also ships a `requirements.txt`.

### 19. Sber India branding applied
`.streamlit/config.toml` carries the palette (Forest Green `#1A991A`, Charcoal
`#262626`, from brandfetch.com/sberbank.ru). Config alone only reaches the
primary accent, so `inject_brand_css()` extends it to metrics, expanders,
buttons, dataframe headers and code blocks, and the logo is inlined as a data
URI so it renders with no network and regardless of the working directory.
A brand mark was added for the favicon.

---

## New CLI Commands and Flags

Node:
```
npm run init            create/migrate the database and seed reference data
npm run init -- --reseed  re-read seed/*.json over existing rows
npm run dashboard       streamlit
npm run test:all        Node + Python suites
--no-gn                 Skip Guidance Notes sync
--no-wd                 Skip withdrawn-documents check
--no-enrich             Skip post-sync classification
```

Python — use `python rbi.py <cmd>` (works on Windows, macOS and Linux):
```
python rbi.py ingest --file X --doc-id rbi:md:N [--title --date --type
                           --category --url --no-clean --chunk]
python rbi.py extract  [doc_id] [--provider --model --limit --sleep
                              --force --include-flagged]
python rbi.py scaffold [doc_id] [--provider --model --limit --sleep --force]
python rbi.py enrich   [doc_id] [--force --rewrite-titles --report-only -v]
python rbi.py taxonomy ["<title>"] [--list institutions|topics|groups]
python rbi.py validate
python rbi.py export -o inventory.json [--doc-id ...]
```

Environment: `RBI_INTEL_DB`, `RBI_INTEL_LLM` (gemini|anthropic|stub),
`RBI_INTEL_MODEL`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`.

Existing flags unchanged: `--no-md`, `--no-mc`, `--no-sc`, `--no-amd`, `--no-relations`, `--force`, `--quick`, `--months N`, `--category X`, `--md-only`.
