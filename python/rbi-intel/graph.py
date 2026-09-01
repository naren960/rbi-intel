"""Turn the relation edges into a navigable hierarchy.

Two outputs matter:

  * lineage(doc_id)  — the ancestry and descendancy chain for one document:
    what it replaced, and what has replaced it. This is the question
    "is this rule still current, and if not, what supersedes it".

  * mermaid(...)     — a rendered diagram, either for one document's
    neighbourhood or for a whole category.
"""
from __future__ import annotations

import html
import sqlite3
from collections import defaultdict, deque
from typing import Iterable

# Edge types that represent replacement, i.e. the successor chain.
SUCCESSION = ("supersedes", "repeals", "withdraws")
MODIFY = ("amends", "consolidates")

EDGE_STYLE = {
    "supersedes": ("-->|supersedes|", "#b45309"),
    "repeals": ("-->|repeals|", "#b91c1c"),
    "withdraws": ("-->|withdraws|", "#b91c1c"),
    "amends": ("-.->|amends|", "#1d4ed8"),
    "consolidates": ("-.->|consolidates|", "#047857"),
    "references": ("-.->|refers to|", "#6b7280"),
}

STATUS_CLASS = {
    "active": "active",
    "superseded": "superseded",
    "repealed": "dead",
    "withdrawn": "dead",
}


def _load(conn: sqlite3.Connection, min_confidence: float):
    docs = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT id, title, date, doc_type, category, status, source_url FROM documents"
        )
    }
    edges = [
        dict(r)
        for r in conn.execute(
            "SELECT src_id, dst_id, rel_type, confidence, evidence FROM relations "
            "WHERE dst_id IS NOT NULL AND confidence >= ?",
            (min_confidence,),
        )
    ]
    return docs, edges


def lineage(conn: sqlite3.Connection, doc_id: str, depth: int = 4, min_confidence: float = 0.6) -> dict:
    docs, edges = _load(conn, min_confidence)
    if doc_id not in docs:
        raise SystemExit(f"Unknown document id: {doc_id}")

    # forward[a] = documents a supersedes/repeals (a replaced them)
    forward: dict[str, list[dict]] = defaultdict(list)
    backward: dict[str, list[dict]] = defaultdict(list)
    for e in edges:
        forward[e["src_id"]].append(e)
        backward[e["dst_id"]].append(e)

    def walk(start: str, adj, key: str, types: Iterable[str]) -> list[dict]:
        out, seen = [], {start}
        qq = deque([(start, 0)])
        while qq:
            node, d = qq.popleft()
            if d >= depth:
                continue
            for e in adj.get(node, []):
                if e["rel_type"] not in types:
                    continue
                nxt = e[key]
                if nxt in seen or nxt not in docs:
                    continue
                seen.add(nxt)
                out.append({
                    "id": nxt,
                    "hops": d + 1,
                    "via": e["rel_type"],
                    "confidence": e["confidence"],
                    **{k: docs[nxt][k] for k in ("title", "date", "doc_type", "status", "source_url")},
                })
                qq.append((nxt, d + 1))
        return out

    replaced = walk(doc_id, forward, "dst_id", SUCCESSION)
    replaced_by = walk(doc_id, backward, "src_id", SUCCESSION)
    amended_by = [
        {
            "id": e["src_id"],
            "via": e["rel_type"],
            "confidence": e["confidence"],
            **{k: docs[e["src_id"]][k] for k in ("title", "date", "doc_type", "source_url")},
        }
        for e in backward.get(doc_id, [])
        if e["rel_type"] in MODIFY and e["src_id"] in docs
    ]
    amends = [
        {
            "id": e["dst_id"],
            "via": e["rel_type"],
            "confidence": e["confidence"],
            **{k: docs[e["dst_id"]][k] for k in ("title", "date", "doc_type", "source_url")},
        }
        for e in forward.get(doc_id, [])
        if e["rel_type"] in MODIFY and e["dst_id"] in docs
    ]

    current = sorted(replaced_by, key=lambda x: (x["hops"], x.get("date") or ""))[-1] if replaced_by else None

    return {
        "document": docs[doc_id],
        "is_current": not replaced_by,
        "current_successor": current,
        "replaced": sorted(replaced, key=lambda x: x.get("date") or ""),
        "replaced_by": sorted(replaced_by, key=lambda x: x.get("date") or ""),
        "amended_by": sorted(amended_by, key=lambda x: x.get("date") or ""),
        "amends": sorted(amends, key=lambda x: x.get("date") or ""),
    }


def _node_id(doc_id: str) -> str:
    return "N" + doc_id.replace(":", "_").replace("-", "_")


def _label(doc: dict, max_len: int = 58) -> str:
    t = (doc.get("title") or doc["id"]).strip()
    if len(t) > max_len:
        t = t[: max_len - 1].rsplit(" ", 1)[0] + "…"
    t = html.escape(t).replace('"', "'")
    date = (doc.get("date") or "")[:10]
    return f"{t}<br/><small>{date} · {doc.get('doc_type','')}</small>"


def mermaid(
    conn: sqlite3.Connection,
    doc_id: str | None = None,
    category: str | None = None,
    depth: int = 2,
    min_confidence: float = 0.6,
    max_nodes: int = 60,
) -> str:
    """Render a Mermaid flowchart of a document's neighbourhood or a category."""
    docs, edges = _load(conn, min_confidence)

    adj: dict[str, list[dict]] = defaultdict(list)
    for e in edges:
        adj[e["src_id"]].append(e)
        adj[e["dst_id"]].append(e)

    if doc_id:
        if doc_id not in docs:
            raise SystemExit(f"Unknown document id: {doc_id}")
        keep, qq = {doc_id}, deque([(doc_id, 0)])
        while qq and len(keep) < max_nodes:
            node, d = qq.popleft()
            if d >= depth:
                continue
            for e in adj.get(node, []):
                for other in (e["src_id"], e["dst_id"]):
                    if other not in keep and other in docs:
                        keep.add(other)
                        qq.append((other, d + 1))
    elif category:
        keep = {i for i, d in docs.items() if (d.get("category") or "").lower().find(category.lower()) >= 0}
        if not keep:
            raise SystemExit(f"No documents in category matching '{category}'")
        keep = set(list(keep)[:max_nodes])
    else:
        raise SystemExit("Pass either doc_id or category")

    shown = [e for e in edges if e["src_id"] in keep and e["dst_id"] in keep]

    lines = ["flowchart TD"]
    for i in sorted(keep):
        d = docs[i]
        cls = STATUS_CLASS.get(d.get("status") or "active", "active")
        shape_l, shape_r = ("[[", "]]") if d.get("doc_type") in ("master_direction", "master_circular") else ("[", "]")
        lines.append(f'  {_node_id(i)}{shape_l}"{_label(d)}"{shape_r}:::{cls}')

    for e in shown:
        arrow = EDGE_STYLE.get(e["rel_type"], ("-->", "#6b7280"))[0]
        lines.append(f"  {_node_id(e['src_id'])} {arrow} {_node_id(e['dst_id'])}")

    if doc_id:
        lines.append(f"  class {_node_id(doc_id)} focus")

    lines += [
        "  classDef active fill:#ecfdf5,stroke:#047857,stroke-width:1px,color:#064e3b;",
        "  classDef superseded fill:#fffbeb,stroke:#b45309,stroke-width:1px,color:#78350f;",
        "  classDef dead fill:#fef2f2,stroke:#b91c1c,stroke-width:1px,color:#7f1d1d,stroke-dasharray:4 3;",
        "  classDef focus stroke:#1d4ed8,stroke-width:3px;",
    ]
    return "\n".join(lines)


def stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT rel_type, "
        "SUM(CASE WHEN dst_id IS NOT NULL THEN 1 ELSE 0 END) AS resolved, "
        "SUM(CASE WHEN dst_id IS NULL THEN 1 ELSE 0 END) AS unresolved "
        "FROM relations GROUP BY rel_type ORDER BY 2 DESC"
    ).fetchall()
    statuses = conn.execute(
        "SELECT status, COUNT(*) n FROM documents GROUP BY status"
    ).fetchall()
    return {
        "relations": [dict(r) for r in rows],
        "document_status": [dict(r) for r in statuses],
    }
