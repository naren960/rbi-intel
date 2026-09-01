"""Classify stored documents by institution type, regulatory topic and
applicability, and fill the D.3 date columns.

This is the enrichment layer described as sections D.2–D.6 of the original
`RBI_CIRCULARS_UPDATE_PAGE.py`, moved off a pandas DataFrame and onto the
shared database. All of it derives from the document TITLE, so it is cheap,
offline, deterministic, and safe to recompute at any time — `--force` rebuilds
every row from titles that are already stored. Nothing here is a source of
truth, which is what makes changing the taxonomy an ordinary edit rather than
a migration.

Runs automatically after `npm run sync`, and after `ingest`.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone

from .taxonomy import GENERIC_INSTITUTION, UNCLASSIFIED_TOPIC, load

RE_UPDATED = re.compile(r"\(\s*updated\s+as\s+on\s+([A-Za-z0-9 ,\-/]+?)\s*\)", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _year(date: str | None) -> int | None:
    if not date or len(date) < 4 or not date[:4].isdigit():
        return None
    return int(date[:4])


def enrich_row(row: sqlite3.Row, tax) -> dict:
    """Classify one document. Pure — no database access, easy to test."""
    from .ingest import normalise_title, parse_loose_date

    raw_title = row["title"] or ""
    display_title, title_updated = normalise_title(raw_title)

    # Classify against the CLEAN title. Leaving "(Updated as on July 01, 2026)"
    # in adds no signal and its "on" / date tokens can only add noise.
    c = tax.classify(display_title)

    # `ingest` normalises the title before storing it, so by the time enrich
    # runs the "(Updated as on ...)" marker is already gone and re-deriving it
    # from the stored title yields nothing. Falling back to the stored value
    # keeps a re-run idempotent instead of quietly erasing the date — the exact
    # kind of silent regression a --force reclassify should never cause.
    stored = row["stored_updated"] if "stored_updated" in row.keys() else None
    updated_date = title_updated or stored

    return {
        "id": row["id"],
        "display_title": display_title,
        "institution_type": c["institution_type"],
        "primary_topic": c["primary_topic"],
        "secondary_topics": json.dumps(c["secondary_topics"], ensure_ascii=False),
        "topic_scores": json.dumps(c["topic_scores"], ensure_ascii=False),
        "topic_group": c["topic_group"],
        "applicability": c["applicability"],
        "applicability_rule": c["applicability_rule"],
        "updated_date": updated_date,
        "publication_year": _year(row["date"]),
        "has_update": int(bool(updated_date)),
        "business_area_hint": c["business_area_hint"],
    }


def pending(conn: sqlite3.Connection, doc_id: str | None, force: bool) -> list[sqlite3.Row]:
    sql = ["SELECT id, title, date, updated_date AS stored_updated FROM documents WHERE 1=1"]
    params: list = []
    if doc_id:
        sql.append("AND id = ?")
        params.append(doc_id)
    if not force:
        sql.append("AND (enriched_at IS NULL OR institution_type IS NULL)")
    sql.append("ORDER BY date DESC")
    return conn.execute(" ".join(sql), params).fetchall()


def _legacy_updated_dates(conn: sqlite3.Connection) -> dict[str, str]:
    """Pick up update dates written to sync_meta before v6 gave them a column.

    Documents ingested against schema v5 stored the parsed date under
    `updated_date:<doc_id>` because there was nowhere else to put it. Reading
    it back here means migrating forward does not lose data that was already
    correctly extracted.
    """
    try:
        return {
            r["key"].split(":", 1)[1]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM sync_meta WHERE key LIKE 'updated_date:%'"
            )
        }
    except sqlite3.OperationalError:
        return {}


def run(
    conn: sqlite3.Connection,
    doc_id: str | None = None,
    force: bool = False,
    rewrite_titles: bool = False,
    quiet: bool = True,
) -> dict:
    """Enrich documents.

    `rewrite_titles` additionally strips "(Updated as on ...)" from
    `documents.title` itself. Off by default: the raw title is what RBI served,
    and overwriting it loses that. The parsed date lands in `updated_date`
    either way, which is what the queries actually need.
    """
    tax = load()
    rows = pending(conn, doc_id, force)
    if not rows:
        return {"documents": 0, "note": "nothing pending — pass --force to reclassify"}

    legacy = _legacy_updated_dates(conn)
    now = _now()
    updated = 0
    for row in rows:
        e = enrich_row(row, tax)
        if not e["updated_date"] and row["id"] in legacy:
            e["updated_date"] = legacy[row["id"]]
            e["has_update"] = 1
        conn.execute(
            "UPDATE documents SET institution_type=?, primary_topic=?, secondary_topics=?, "
            "topic_scores=?, topic_group=?, applicability=?, applicability_rule=?, "
            "updated_date=?, publication_year=?, has_update=?, enriched_at=?"
            + (", title=?" if rewrite_titles else "")
            + " WHERE id=?",
            (
                e["institution_type"], e["primary_topic"], e["secondary_topics"],
                e["topic_scores"], e["topic_group"], e["applicability"],
                e["applicability_rule"], e["updated_date"], e["publication_year"],
                e["has_update"], now,
                *([e["display_title"]] if rewrite_titles else []),
                e["id"],
            ),
        )
        updated += 1
        if not quiet:
            print(f"[enrich] {e['id']}  {e['institution_type']} / {e['primary_topic']} "
                  f"/ {e['applicability']}")
    conn.commit()
    return {"documents": updated, "taxonomy_version": tax.version, **report(conn)}


def report(conn: sqlite3.Connection) -> dict:
    """Section D.8 — enrichment quality.

    The number that matters is `unclassified`. A topic dictionary is only as
    good as its coverage of the corpus it meets, and a rising unclassified
    share is the signal that RBI has started using vocabulary the dictionary
    does not know. Printing the unclassified titles is what makes the gap
    actionable rather than merely visible.
    """
    def group(col: str, limit: int = 30):
        return {
            r[0] or "(none)": r[1]
            for r in conn.execute(
                f"SELECT {col}, COUNT(*) n FROM documents GROUP BY {col} "
                f"ORDER BY n DESC LIMIT ?", (limit,)
            )
        }

    total = conn.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"]
    unclassified = conn.execute(
        "SELECT COUNT(*) n FROM documents WHERE primary_topic = ? OR primary_topic IS NULL",
        (UNCLASSIFIED_TOPIC,),
    ).fetchone()["n"]
    generic = conn.execute(
        "SELECT COUNT(*) n FROM documents WHERE institution_type = ? OR institution_type IS NULL",
        (GENERIC_INSTITUTION,),
    ).fetchone()["n"]

    samples = [
        r["title"][:110]
        for r in conn.execute(
            "SELECT title FROM documents WHERE primary_topic = ? ORDER BY date DESC LIMIT 10",
            (UNCLASSIFIED_TOPIC,),
        )
    ]

    return {
        "total_documents": total,
        "by_applicability": group("applicability"),
        "by_institution_type": group("institution_type"),
        "top_topics": group("primary_topic", 15),
        "by_topic_group": group("topic_group"),
        "coverage": {
            "unclassified_topic": unclassified,
            "unclassified_pct": round(100 * unclassified / total, 1) if total else 0.0,
            "generic_institution": generic,
            "generic_institution_pct": round(100 * generic / total, 1) if total else 0.0,
            "with_update_date": conn.execute(
                "SELECT COUNT(*) n FROM documents WHERE has_update = 1").fetchone()["n"],
        },
        "unclassified_examples": samples,
    }


def format_report(r: dict) -> str:
    lines = []
    if "documents" in r:
        lines.append(f"enriched {r['documents']} document(s) "
                     f"(taxonomy v{r.get('taxonomy_version', '?')})")
    lines.append("")
    for label, key in (
        ("applicability", "by_applicability"),
        ("institution type", "by_institution_type"),
        ("topic group", "by_topic_group"),
        ("top topics", "top_topics"),
    ):
        block = r.get(key) or {}
        if not block:
            continue
        lines.append(f"── {label} ──")
        for k, v in block.items():
            lines.append(f"  {str(k)[:52]:<54} {v}")
        lines.append("")

    cov = r.get("coverage", {})
    lines.append("── coverage ──")
    lines.append(f"  documents                                              {r.get('total_documents', 0)}")
    lines.append(f"  unclassified topic                                     "
                 f"{cov.get('unclassified_topic', 0)} ({cov.get('unclassified_pct', 0)}%)")
    lines.append(f"  generic / unmatched institution                        "
                 f"{cov.get('generic_institution', 0)} ({cov.get('generic_institution_pct', 0)}%)")
    lines.append(f"  carrying an '(Updated as on ...)' date                 {cov.get('with_update_date', 0)}")

    if r.get("unclassified_examples"):
        lines.append("")
        lines.append("── unclassified titles (add keywords to seed/taxonomy.json) ──")
        for t in r["unclassified_examples"]:
            lines.append(f"  {t}")
    lines.append("")
    return "\n".join(lines)
