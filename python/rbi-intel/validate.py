"""Data-integrity checks over the shared database.

Port of `05_validate.py` + the `cross_reference_errors()` method on
`schema.Inventory`. Roughly half of what the Pydantic version checked is now
enforced by SQLite itself — foreign keys, primary-key uniqueness, NOT NULL —
so this file deliberately does not re-check those. What it checks is the half
a type system cannot express:

  * **Enum drift.** `obligation_type`, `classification`, `severity` and
    `control_type` are free-text columns fed by a model. A single
    "Partially compliant" (lower c) silently disappears from every filter.

  * **Unresolved placeholders.** `OWN-UNMAPPED` / `BA-99` are deliberate,
    visible failures from the scaffold step. They are only useful if something
    counts them.

  * **Coverage holes.** A direction with clauses but no requirements means an
    extraction run died partway; a requirement with no mapping means the
    scaffold step never reached it. Both look like success until counted.

  * **Distribution sanity.** The original pipeline's sharpest check: if every
    assessment came back "Compliant", the model was agreeing rather than
    assessing. A single-bucket distribution is a red flag about the run, not
    a happy result.

Exit status is 1 when any ERROR is present, 0 for warnings only, so it can be
used as a gate.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter

OBLIGATION_TYPES = {"Governance", "Process", "Reporting", "Screening",
                    "Timeline", "Record-keeping", "Assurance"}
CLASSIFICATIONS = {"Compliant", "Partially Compliant", "Gap", "Not Applicable", "To Be Confirmed"}
SEVERITIES = {"Low", "Medium", "High"}
CONTROL_TYPES = {"Preventive", "Detective", "Corrective"}
RELEVANCE = {"High", "Medium", "Low"}
DOC_STATUSES = {"active", "superseded", "repealed", "withdrawn"}


def _rows(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def _enum_check(conn, table, column, allowed, errors, label):
    bad = _rows(
        conn,
        f"SELECT {column} AS v, COUNT(*) AS n FROM {table} "
        f"WHERE {column} IS NOT NULL AND {column} <> '' GROUP BY {column}",
    )
    for r in bad:
        if r["v"] not in allowed:
            errors.append(f"ERROR  {label}: {r['n']} row(s) with invalid {column} {r['v']!r}")


def run(conn: sqlite3.Connection) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    # ── Referential integrity SQLite will not enforce retroactively ──────────
    # FKs only apply to writes made while PRAGMA foreign_keys was on. Rows
    # written by an older build, or by a tool that forgot the pragma, can still
    # dangle — so check explicitly rather than trusting the constraint.
    orphans = [
        ("clauses -> documents",
         "SELECT COUNT(*) n FROM clauses c LEFT JOIN documents d ON d.id=c.doc_id WHERE d.id IS NULL"),
        ("requirements -> clauses",
         "SELECT COUNT(*) n FROM requirements r LEFT JOIN clauses c ON c.id=r.clause_id WHERE c.id IS NULL"),
        ("req_mappings -> requirements",
         "SELECT COUNT(*) n FROM req_mappings m LEFT JOIN requirements r ON r.id=m.req_id WHERE r.id IS NULL"),
        ("req_mappings.business_area -> business_areas",
         "SELECT COUNT(*) n FROM req_mappings m LEFT JOIN business_areas b ON b.id=m.business_area "
         "WHERE m.business_area IS NOT NULL AND b.id IS NULL"),
        ("req_mappings.owner_process -> owners",
         "SELECT COUNT(*) n FROM req_mappings m LEFT JOIN owners o ON o.id=m.owner_process "
         "WHERE m.owner_process IS NOT NULL AND o.id IS NULL"),
        ("req_mappings.owner_control -> owners",
         "SELECT COUNT(*) n FROM req_mappings m LEFT JOIN owners o ON o.id=m.owner_control "
         "WHERE m.owner_control IS NOT NULL AND o.id IS NULL"),
        ("relations.src -> documents",
         "SELECT COUNT(*) n FROM relations r LEFT JOIN documents d ON d.id=r.src_id WHERE d.id IS NULL"),
    ]
    for label, sql in orphans:
        n = _rows(conn, sql)[0]["n"]
        if n:
            errors.append(f"ERROR  {n} orphaned row(s): {label}")

    # ── Enum drift ───────────────────────────────────────────────────────────
    _enum_check(conn, "requirements", "obligation_type", OBLIGATION_TYPES, errors, "requirements")
    _enum_check(conn, "requirements", "branch_relevance", RELEVANCE, errors, "requirements")
    _enum_check(conn, "req_mappings", "classification", CLASSIFICATIONS, errors, "req_mappings")
    _enum_check(conn, "req_mappings", "severity", SEVERITIES, errors, "req_mappings")
    _enum_check(conn, "req_mappings", "control_type", CONTROL_TYPES, errors, "req_mappings")
    _enum_check(conn, "documents", "status", DOC_STATUSES, errors, "documents")

    # ── Content quality ──────────────────────────────────────────────────────
    n_docs = _rows(conn, "SELECT COUNT(*) n FROM documents")[0]["n"]
    n_clauses = _rows(conn, "SELECT COUNT(*) n FROM clauses")[0]["n"]
    n_reqs = _rows(conn, "SELECT COUNT(*) n FROM requirements")[0]["n"]
    n_maps = _rows(conn, "SELECT COUNT(*) n FROM req_mappings")[0]["n"]

    empty_req = _rows(conn, "SELECT COUNT(*) n FROM requirements WHERE requirement IS NULL OR TRIM(requirement)=''")[0]["n"]
    if empty_req:
        errors.append(f"ERROR  {empty_req} requirement(s) with empty requirement text")

    no_title = _rows(conn, "SELECT COUNT(*) n FROM requirements WHERE clause_title IS NULL OR TRIM(clause_title)=''")[0]["n"]
    if no_title:
        warnings.append(f"WARN   {no_title} requirement(s) with no clause_title "
                        f"(the model may be echoing the clause label — check the prompt)")

    dup_kw = _rows(conn, "SELECT COUNT(*) n FROM requirements WHERE keywords IS NULL OR keywords IN ('','[]')")[0]["n"]
    if dup_kw:
        warnings.append(f"WARN   {dup_kw} requirement(s) with no keywords (weakens Ask-tab retrieval)")

    # ── Unresolved placeholders from the scaffold step ───────────────────────
    unmapped_owner = _rows(
        conn, "SELECT COUNT(*) n FROM req_mappings WHERE owner_process='OWN-UNMAPPED' "
              "OR owner_control='OWN-UNMAPPED'")[0]["n"]
    if unmapped_owner:
        warnings.append(f"WARN   {unmapped_owner} mapping(s) with OWN-UNMAPPED — "
                        f"the model named a role outside the fixed list; assign manually")

    unmapped_area = _rows(conn, "SELECT COUNT(*) n FROM req_mappings WHERE business_area='BA-99'")[0]["n"]
    if unmapped_area:
        warnings.append(f"WARN   {unmapped_area} mapping(s) still on BA-99 (unclassified business area)")

    # ── Coverage holes ───────────────────────────────────────────────────────
    chunked_no_reqs = _rows(
        conn,
        "SELECT d.id, d.title, COUNT(c.id) AS n FROM documents d JOIN clauses c ON c.doc_id=d.id "
        "WHERE NOT EXISTS (SELECT 1 FROM requirements r WHERE r.doc_id=d.id) "
        "GROUP BY d.id ORDER BY n DESC LIMIT 10",
    )
    for r in chunked_no_reqs:
        warnings.append(f"WARN   {r['id']} has {r['n']} clauses but zero requirements "
                        f"— `extract` never ran for it")

    reqs_no_map = _rows(
        conn, "SELECT COUNT(*) n FROM requirements r "
              "WHERE NOT EXISTS (SELECT 1 FROM req_mappings m WHERE m.req_id=r.id)")[0]["n"]
    if reqs_no_map:
        warnings.append(f"WARN   {reqs_no_map} requirement(s) with no internal mapping "
                        f"— run `scaffold`")

    unused_areas = _rows(
        conn, "SELECT COUNT(*) n FROM business_areas b WHERE b.id <> 'BA-99' AND NOT EXISTS "
              "(SELECT 1 FROM req_mappings m WHERE m.business_area=b.id)")[0]["n"]
    if unused_areas and n_maps:
        warnings.append(f"INFO   {unused_areas} business area(s) unused by any requirement")

    # ── Distribution sanity ──────────────────────────────────────────────────
    dist = Counter(
        r["classification"] or "(null)"
        for r in _rows(conn, "SELECT classification FROM req_mappings")
    )
    if n_maps >= 20 and len(dist) == 1:
        errors.append(
            f"ERROR  every one of {n_maps} assessments is {next(iter(dist))!r}. "
            f"A single-bucket distribution means the model agreed rather than assessed — "
            f"re-check the calibration guidance in the scaffold prompt."
        )
    elif n_maps >= 20:
        top, top_n = dist.most_common(1)[0]
        if top_n / n_maps > 0.85:
            warnings.append(f"WARN   {top_n}/{n_maps} ({top_n/n_maps:.0%}) of assessments are "
                            f"{top!r} — distribution looks degenerate")

    # ── Provenance ───────────────────────────────────────────────────────────
    prov = Counter(r["provenance"] for r in _rows(conn, "SELECT provenance FROM req_mappings"))
    local_docs = _rows(conn, "SELECT COUNT(*) n FROM documents WHERE source='local'")[0]["n"]

    return {
        "counts": {
            "documents": n_docs, "  of which locally ingested": local_docs,
            "clauses": n_clauses, "requirements": n_reqs, "req_mappings": n_maps,
            "relations": _rows(conn, "SELECT COUNT(*) n FROM relations")[0]["n"],
            "unresolved relations": _rows(
                conn, "SELECT COUNT(*) n FROM relations WHERE dst_id IS NULL")[0]["n"],
        },
        "classification_distribution": dict(dist.most_common()),
        "mapping_provenance": dict(prov.most_common()),
        "errors": errors,
        "warnings": warnings,
        "ok": not errors,
    }


def report(result: dict) -> str:
    lines = ["", "── counts ──"]
    for k, v in result["counts"].items():
        lines.append(f"  {k:<28} {v}")

    if result["classification_distribution"]:
        lines += ["", "── seeded assessment distribution ──"]
        for k, v in result["classification_distribution"].items():
            lines.append(f"  {k:<28} {v}")

    if result["mapping_provenance"]:
        lines += ["", "── mapping provenance ──"]
        for k, v in result["mapping_provenance"].items():
            lines.append(f"  {k:<28} {v}")
        if result["mapping_provenance"].get("seeded"):
            lines.append("  NOTE: 'seeded' rows are model-drafted placeholders, not evidence "
                         "of compliance.")

    lines += ["", "── findings ──"]
    if not result["errors"] and not result["warnings"]:
        lines.append("  none — clean")
    for e in result["errors"]:
        lines.append(f"  {e}")
    for w in result["warnings"]:
        lines.append(f"  {w}")

    lines += ["", f"RESULT: {'PASS' if result['ok'] else 'FAIL'} "
                  f"({len(result['errors'])} error(s), {len(result['warnings'])} warning(s))", ""]
    return "\n".join(lines)
