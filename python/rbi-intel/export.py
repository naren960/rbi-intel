"""Export the database into the legacy `inventory.json` shape.

The original project's React artifact, `06_query_cli.py` and
`rbi_branch_regulatory_inventory.json` all speak one JSON dialect defined by
`schema.py`. Moving to a database should not break those consumers, so this
writes exactly that shape back out — `meta`, `directions`, `business_areas`,
`owners`, `requirements`, with `mapping` and `assessment` nested inside each
requirement.

Two honesty constraints on the way out:

  * `meta.internal_layer_provenance` states plainly that the mapping and
    assessment blocks are seeded unless promoted. The original file said so
    and it is the single most important field in the document.
  * Only business areas and owners actually referenced are emitted. The
    original validator treated an unreferenced business area as an error, and
    exporting all 37 seeds into a file describing 91 requirements would fail
    its own round-trip check.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = "2.0"


def _direction_rows(conn: sqlite3.Connection, doc_ids: list[str] | None):
    sql = (
        "SELECT DISTINCT d.id, d.title, d.date, d.ref_no, d.source_url, d.category, "
        "d.doc_type, d.status, d.source "
        "FROM documents d JOIN requirements r ON r.doc_id = d.id"
    )
    params: list = []
    if doc_ids:
        sql += " WHERE d.id IN (%s)" % ",".join("?" * len(doc_ids))
        params += doc_ids
    return conn.execute(sql + " ORDER BY d.date", params).fetchall()


def _short(title: str) -> str:
    """A compact handle for a Master Direction title.

    RBI titles run to 90+ characters; the artifact's `short` field is meant to
    fit in a table cell. Take the parenthesised subject where there is one —
    "(Commercial Banks - Prudential Norms on Capital Adequacy)" — since that is
    the part that actually distinguishes one direction from another.
    """
    import re
    m = re.search(r"\(([^)]{6,80})\)", title or "")
    if m and "updated as on" not in m.group(1).lower():
        return m.group(1).strip()
    return (title or "")[:60].strip()


def build(conn: sqlite3.Connection, doc_ids: list[str] | None = None) -> dict:
    directions = []
    for d in _direction_rows(conn, doc_ids):
        updated = conn.execute(
            "SELECT value FROM sync_meta WHERE key = ?", (f"updated_date:{d['id']}",)
        ).fetchone()
        directions.append({
            "id": d["id"],
            "short": _short(d["title"]),
            "title": d["title"],
            "reference": d["ref_no"] or "",
            "issued": d["date"],
            "last_updated": (updated["value"] if updated else d["date"]),
            "url": d["source_url"],
            "issuing_department": d["category"] or "",
            "statutory_basis": "",
            "applies_to": d["category"] or "",
            "summary": "",
            "branch_relevance_note": "",
            "status": d["status"],
            "source": d["source"],
        })

    req_sql = (
        "SELECT r.id, r.doc_id, r.clause_title, r.requirement, r.obligation_type, "
        "       r.applicability, r.branch_relevance, r.timeline, r.keywords, "
        "       c.clause_label, c.chapter, c.needs_review, "
        "       m.business_area, m.policy, m.process, m.control, m.control_type, "
        "       m.owner_process, m.owner_control, m.evidence_required, "
        "       m.classification, m.finding, m.recommendation, m.severity, m.provenance "
        "FROM requirements r "
        "JOIN clauses c ON c.id = r.clause_id "
        "LEFT JOIN req_mappings m ON m.req_id = r.id"
    )
    params: list = []
    if doc_ids:
        req_sql += " WHERE r.doc_id IN (%s)" % ",".join("?" * len(doc_ids))
        params += doc_ids
    req_sql += " ORDER BY r.doc_id, c.seq"

    requirements = []
    used_areas: set[str] = set()
    used_owners: set[str] = set()

    for r in conn.execute(req_sql, params):
        try:
            keywords = json.loads(r["keywords"] or "[]")
        except json.JSONDecodeError:
            keywords = []
        if r["business_area"]:
            used_areas.add(r["business_area"])
        for o in (r["owner_process"], r["owner_control"]):
            if o:
                used_owners.add(o)

        requirements.append({
            "req_id": r["id"],
            "direction_id": r["doc_id"],
            "clause": r["clause_label"],
            "chapter": r["chapter"],
            "clause_title": r["clause_title"] or "",
            "business_area": r["business_area"] or "BA-99",
            "requirement": r["requirement"],
            "obligation_type": r["obligation_type"] or "",
            "branch_relevance": r["branch_relevance"] or "Medium",
            "applicability": r["applicability"],
            "timeline": r["timeline"],
            "needs_manual_review": bool(r["needs_review"]),
            "mapping": {
                "policy": r["policy"] or "",
                "process": r["process"] or "",
                "control": r["control"] or "",
                "control_type": r["control_type"] or "Preventive",
                "owner_process": r["owner_process"] or "OWN-UNMAPPED",
                "owner_control": r["owner_control"] or "OWN-UNMAPPED",
                "evidence_required": r["evidence_required"] or "",
                "provenance": r["provenance"] or "not-yet-scaffolded",
            },
            "assessment": {
                "classification": r["classification"] or "To Be Confirmed",
                "finding": r["finding"] or "",
                "recommendation": r["recommendation"] or "",
                "severity": r["severity"] or "Medium",
            },
            "keywords": keywords,
        })

    areas = [
        dict(r) for r in conn.execute(
            "SELECT id, name, description FROM business_areas ORDER BY id"
        ) if r["id"] in used_areas
    ]
    owners = [
        dict(r) for r in conn.execute(
            "SELECT id, role, line FROM owners ORDER BY id"
        ) if r["id"] in used_owners
    ]

    seeded = sum(1 for x in requirements if x["mapping"]["provenance"] == "seeded")

    return {
        "meta": {
            "tool": "rbi-intel",
            "schema_version": SCHEMA_VERSION,
            "generated_on": datetime.now(timezone.utc).isoformat(),
            "scope": f"{len(directions)} direction(s), {len(requirements)} requirement(s)",
            "regulatory_layer_provenance":
                "Paraphrased by a language model from clause text stored in the rbi-intel "
                "database. Verify against the official RBI source before relying on it.",
            "internal_layer_provenance":
                f"SEEDED PLACEHOLDER. {seeded} of {len(requirements)} mapping/assessment blocks "
                f"were drafted by a language model with no access to any real policy register, "
                f"and are illustrative only. They must be replaced with actual policy, process "
                f"and control artefacts before any compliance use.",
            "classification_scheme": [
                "Compliant", "Partially Compliant", "Gap", "Not Applicable", "To Be Confirmed",
            ],
        },
        "directions": directions,
        "business_areas": areas,
        "owners": owners,
        "requirements": requirements,
    }


def write_inventory(conn: sqlite3.Connection, out: pathlib.Path,
                    doc_ids: list[str] | None = None) -> dict:
    data = build(conn, doc_ids)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {
        "out": str(out),
        "directions": len(data["directions"]),
        "requirements": len(data["requirements"]),
        "business_areas": len(data["business_areas"]),
        "owners": len(data["owners"]),
        "bytes": out.stat().st_size,
    }
