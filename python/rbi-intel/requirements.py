"""Clause text -> structured requirement records.

Port of `03_extract_requirements.py`, moved off JSONL files and onto the
shared database. The v3 schema already declared a `requirements` table and
nothing in the package ever wrote a row to it; this is the missing writer.

Three things change in the move, all of them simplifications:

  * **The key.** The file pipeline minted `CREDIT-001`, `CREDIT-002` … from a
    counter that only made sense within one run of one script — re-run it after
    fixing a prompt and every ID shifted, invalidating anything that referenced
    them. Here the key is the clause it came from (`rbi:md:12798#CHIV-2`),
    which is stable across re-runs by construction.

  * **Resumability.** The notebook's `run_once()` guard existed to stop a
    kernel restart from re-spending 30 minutes of API quota. A row in the
    database is a better guard: extraction skips clauses that already have a
    requirement unless `--force`, so an interrupted run resumes exactly where
    it stopped.

  * **Quota death is a checkpoint, not a crash.** Each record is committed as
    it is produced. When the daily cap hits at clause 240 of 396, those 240
    rows are already durable and the run reports what remains.

The system prompt is carried over verbatim, including the negative instruction
about `clause_title` — the model reliably answered "Clause CHIII-6" until it
was told in those words not to.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone

from .llm import LLMError, Provider, QuotaExhausted, default_sleep

SYSTEM_PROMPT = """You extract regulatory requirements from a single clause of an RBI Master Direction.

Rules:
- Paraphrase the requirement in your own words. Do not copy the source sentence verbatim.
- Stay strictly within what this clause says. Do not infer obligations from context outside the given text.
- If the clause is a bare heading, a chapter title, a cross-reference, or says only "Deleted." — set skip=true.
- If the text reads as garbled table or formula content with little sentence structure — set skip=true.
- If the clause sets a deadline, cadence, or numeric threshold, capture it exactly in timeline; otherwise use null.
- obligation_type must be one of: Governance, Process, Reporting, Screening, Timeline, Record-keeping, Assurance.
- branch_relevance: how directly does a bank branch (not head office or Board) carry out this obligation? High, Medium, or Low.
- applicability: which entities the clause binds, in a short phrase, if the clause says so; otherwise null.
- keywords: 4-8 short lowercase search terms a compliance officer would type to find this clause.

For clause_title: write a SHORT DESCRIPTIVE LABEL (3-7 words) capturing the SUBJECT of the obligation,
like "Board-approved credit risk policy" or "Large exposure reporting threshold".
NEVER write "Clause X" or echo the clause number as the title."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "skip":             {"type": "boolean"},
        "reason":           {"type": "string"},
        "clause_title":     {"type": "string"},
        "requirement":      {"type": "string"},
        "obligation_type":  {"type": "string",
                             "enum": ["Governance", "Process", "Reporting",
                                      "Screening", "Timeline", "Record-keeping", "Assurance"]},
        "branch_relevance": {"type": "string", "enum": ["High", "Medium", "Low"]},
        "applicability":    {"type": "string"},
        "timeline":         {"type": "string"},
        "keywords":         {"type": "array", "items": {"type": "string"}},
    },
    "required": ["skip"],
}

OBLIGATION_TYPES = {"Governance", "Process", "Reporting", "Screening",
                    "Timeline", "Record-keeping", "Assurance"}
RELEVANCE = {"High", "Medium", "Low"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pending_clauses(
    conn: sqlite3.Connection,
    doc_id: str | None = None,
    force: bool = False,
    include_flagged: bool = False,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Clauses awaiting extraction.

    `needs_review` clauses are excluded by default. Those are the ones the
    chunker's fragment classifier could not confirm — duplicate labels, table
    debris — and sending them costs quota to be told 'skip'.
    """
    sql = [
        "SELECT c.id, c.doc_id, c.clause_label, c.chapter, c.text, d.title AS doc_title",
        "FROM clauses c JOIN documents d ON d.id = c.doc_id",
        "WHERE 1=1",
    ]
    params: list = []
    if doc_id:
        sql.append("AND c.doc_id = ?")
        params.append(doc_id)
    if not include_flagged:
        sql.append("AND c.needs_review = 0")
    if not force:
        sql.append("AND NOT EXISTS (SELECT 1 FROM requirements r WHERE r.clause_id = c.id)")
    sql.append("ORDER BY c.doc_id, c.seq")
    if limit:
        sql.append("LIMIT ?")
        params.append(limit)
    return conn.execute(" ".join(sql), params).fetchall()


def _clean_str(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    # Models emit the string "null"/"none"/"n/a" in schema mode where a real
    # null was wanted. Left in, they poison every downstream filter.
    return None if s.lower() in ("", "null", "none", "n/a", "na") else s


def store_requirement(conn: sqlite3.Connection, clause: sqlite3.Row, result: dict, model: str) -> None:
    obligation = _clean_str(result.get("obligation_type"))
    if obligation not in OBLIGATION_TYPES:
        obligation = None
    relevance = _clean_str(result.get("branch_relevance"))
    if relevance not in RELEVANCE:
        relevance = None

    keywords = result.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",")]
    keywords = sorted({str(k).strip().lower() for k in keywords if str(k).strip()})

    title = _clean_str(result.get("clause_title")) or ""
    # Belt and braces on the bug the prompt already forbids: if the model
    # echoed the clause label anyway, the title carries no information and is
    # better empty than misleadingly duplicated.
    if title.lower().replace(" ", "") in (
        f"clause{clause['clause_label']}".lower().replace(" ", ""),
        clause["clause_label"].lower().replace(" ", ""),
    ):
        title = ""

    conn.execute(
        "INSERT INTO requirements (id, clause_id, doc_id, clause_title, requirement, "
        "obligation_type, applicability, branch_relevance, timeline, keywords, "
        "needs_review, extracted_at, model) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET clause_title=excluded.clause_title, "
        "requirement=excluded.requirement, obligation_type=excluded.obligation_type, "
        "applicability=excluded.applicability, branch_relevance=excluded.branch_relevance, "
        "timeline=excluded.timeline, keywords=excluded.keywords, "
        "needs_review=excluded.needs_review, extracted_at=excluded.extracted_at, "
        "model=excluded.model",
        (
            clause["id"], clause["id"], clause["doc_id"],
            title,
            _clean_str(result.get("requirement")) or "",
            obligation,
            _clean_str(result.get("applicability")),
            relevance,
            _clean_str(result.get("timeline")),
            json.dumps(keywords, ensure_ascii=False),
            int(not title or obligation is None),
            _now(),
            model,
        ),
    )
    conn.commit()


def extract(
    conn: sqlite3.Connection,
    provider: Provider,
    doc_id: str | None = None,
    force: bool = False,
    include_flagged: bool = False,
    limit: int | None = None,
    sleep: float | None = None,
    quiet: bool = False,
) -> dict:
    rows = pending_clauses(conn, doc_id, force, include_flagged, limit)
    if not rows:
        return {"clauses": 0, "kept": 0, "skipped": 0, "note": "nothing pending — pass --force to redo"}

    pause = default_sleep(provider) if sleep is None else sleep
    kept = skipped = failed = 0
    stopped: str | None = None

    for i, c in enumerate(rows, 1):
        if not quiet:
            print(f"[extract] {i}/{len(rows)}  {c['id']}", file=sys.stderr)
        try:
            result = provider.json_call(
                system_prompt=SYSTEM_PROMPT,
                user_content=f"Clause {c['clause_label']}:\n{c['text']}",
                response_schema=RESPONSE_SCHEMA,
                max_output_tokens=1200,   # schema-mode framing truncates below ~1200
            )
        except QuotaExhausted as e:
            # Everything committed so far is durable. Say where we stopped so
            # the run can be resumed tomorrow without --force.
            stopped = str(e)
            print(f"[extract] STOPPED at {i}/{len(rows)}: {e}", file=sys.stderr)
            break
        except LLMError as e:
            failed += 1
            print(f"           !! {e}", file=sys.stderr)
            time.sleep(pause)
            continue

        if result.get("skip"):
            skipped += 1
            if not quiet:
                print(f"           -> skipped ({result.get('reason') or 'n/a'})", file=sys.stderr)
        else:
            store_requirement(conn, c, result, provider.model)
            kept += 1
        time.sleep(pause)

    out = {
        "clauses": len(rows), "kept": kept, "skipped": skipped, "failed": failed,
        "remaining": len(rows) - kept - skipped - failed,
        "provider": provider.name, "model": provider.model,
    }
    if stopped:
        out["stopped"] = stopped
    return out
