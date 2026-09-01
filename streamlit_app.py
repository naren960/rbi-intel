#!/usr/bin/env python3
"""
Streamlit dashboard for the rbi-intel SQLite database.

Provides the same capabilities as the 13 MCP tools — search, browse,
lineage, change feed, revisions — without needing Claude Desktop or
Claude Code.

Install:
    pip install streamlit pandas

Run:
    streamlit run streamlit_app.py

Custom database path (defaults to %USERPROFILE%\\.rbi-intel\\regdata.db):
    set RBI_INTEL_DB=C:\\path\\to\\regdata.db
    streamlit run streamlit_app.py
"""

import base64
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# Branding — Sber India
#
# Colours from brandfetch.com/sberbank.ru, matching .streamlit/config.toml.
# The logo is inlined as a data URI rather than served as a file so the page
# renders identically whether Streamlit is launched from the project root or
# from somewhere else, and works with no network.
# ---------------------------------------------------------------------------

BRAND_GREEN = "#1A991A"
BRAND_GREEN_DARK = "#127012"
BRAND_GREEN_TINT = "#EAF6EA"
BRAND_CHARCOAL = "#262626"
BRAND_MUTED = "#5F6B5F"


def _brand_dir() -> Path:
    return Path(__file__).resolve().parent / "branding"


@st.cache_data(show_spinner=False)
def logo_data_uri(filename: str) -> Optional[str]:
    path = _brand_dir() / filename
    if not path.exists():
        return None
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def inject_brand_css() -> None:
    """Apply the brand palette to the widgets Streamlit's theme does not reach.

    `.streamlit/config.toml` covers the primary accent, background and text.
    What it cannot restyle is the tab underline, metric values, expander
    headers, dataframe header row and code blocks — which is most of what this
    dashboard is made of, so without this the page reads as default Streamlit
    with one green button.
    """
    st.markdown(
        f"""
        <style>
          :root {{
            --sber-green: {BRAND_GREEN};
            --sber-green-dark: {BRAND_GREEN_DARK};
            --sber-green-tint: {BRAND_GREEN_TINT};
            --sber-charcoal: {BRAND_CHARCOAL};
          }}

          h1, h2, h3, h4 {{ color: var(--sber-charcoal); letter-spacing: -0.01em; }}
          h1 {{ border-bottom: 3px solid var(--sber-green); padding-bottom: .35rem; }}

          /* Sidebar */
          section[data-testid="stSidebar"] {{
            background: linear-gradient(180deg, var(--sber-green-tint) 0%, #FFFFFF 46%);
            border-right: 1px solid #E3EDE3;
          }}
          section[data-testid="stSidebar"] label {{ color: var(--sber-charcoal); }}

          /* Metrics */
          div[data-testid="stMetricValue"] {{
            color: var(--sber-green-dark); font-weight: 700;
          }}
          div[data-testid="stMetric"] {{
            background: #FFFFFF; border: 1px solid #E3EDE3;
            border-left: 4px solid var(--sber-green);
            border-radius: 6px; padding: .65rem .85rem;
          }}

          /* Expanders */
          details[data-testid="stExpander"] {{
            border: 1px solid #E3EDE3; border-radius: 6px;
          }}
          details[data-testid="stExpander"] summary:hover {{ color: var(--sber-green-dark); }}

          /* Buttons */
          .stButton > button {{
            border: 1px solid var(--sber-green); color: var(--sber-green-dark);
            background: #FFFFFF; border-radius: 5px; font-weight: 600;
          }}
          .stButton > button:hover {{
            background: var(--sber-green); color: #FFFFFF; border-color: var(--sber-green);
          }}
          .stDownloadButton > button {{
            background: var(--sber-green); color: #FFFFFF; border: 0; font-weight: 600;
          }}

          /* Dataframe header */
          div[data-testid="stDataFrame"] thead tr th {{
            background: var(--sber-green-tint) !important;
            color: var(--sber-charcoal) !important;
          }}

          /* Inline code / command hints */
          code {{ color: var(--sber-green-dark); background: var(--sber-green-tint); }}

          /* Brand header block */
          .sber-header {{
            display: flex; align-items: center; gap: 14px;
            padding: 2px 0 14px 0; margin-bottom: 6px;
          }}
          .sber-header img {{ height: 40px; }}
          .sber-header .sub {{
            font-size: 12.5px; color: {BRAND_MUTED}; line-height: 1.35;
          }}
          .sber-header .title {{
            font-size: 17px; font-weight: 700; color: var(--sber-charcoal);
          }}
          .sber-footer {{
            margin-top: 26px; padding-top: 10px; border-top: 1px solid #E3EDE3;
            font-size: 11.5px; color: {BRAND_MUTED};
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def brand_header() -> None:
    uri = logo_data_uri("sber-india-logo.svg")
    logo = f'<img src="{uri}" alt="Sber India"/>' if uri else ""
    st.markdown(
        f"""
        <div class="sber-header">
          {logo}
          <div>
            <div class="title">RBI Regulatory Intelligence</div>
            <div class="sub">Compliance and regulatory monitoring &middot; Sber India</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def brand_footer() -> None:
    st.markdown(
        '<div class="sber-footer">Sber India &middot; RBI Regulatory Intelligence &middot; '
        'Machine-extracted from published RBI sources. Verify against the official '
        'document before relying on any classification or assessment.</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def _default_db() -> Path:
    return Path.home() / ".rbi-intel" / "regdata.db"


def _db_path() -> Path:
    env = os.environ.get("RBI_INTEL_DB")
    return Path(env) if env else _default_db()


@st.cache_resource
def get_conn() -> sqlite3.Connection:
    path = _db_path()
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def db_exists() -> bool:
    p = _db_path()
    return p.exists() and p.stat().st_size > 0


# ---------------------------------------------------------------------------
# FTS query sanitiser  (port of escapeFts from queries.ts)
# ---------------------------------------------------------------------------

def escape_fts(q: str) -> str:
    """
    Convert a user query to a safe FTS5 MATCH expression.
    Hyphens/slashes become word boundaries so "anti-money laundering"
    matches as three separate tokens rather than one unmatched string.
    Returns empty string when nothing survives (caller falls back to LIKE).
    """
    tokens = re.sub(r"[-/\u2013\u2014]+", " ", q.strip()).split()
    clean = []
    for t in tokens:
        t2 = re.sub(r'["*^:(){}\[\]]', "", t).strip()
        if t2:
            clean.append(f'"{t2}"')
    return " ".join(clean)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def doc_count(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT doc_type, status, COUNT(*) AS n FROM documents "
        "GROUP BY doc_type, status ORDER BY n DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def relation_count(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT rel_type,
                  SUM(CASE WHEN dst_id IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                  SUM(CASE WHEN dst_id IS NULL    THEN 1 ELSE 0 END) AS unresolved
           FROM relations GROUP BY rel_type"""
    ).fetchall()
    return [dict(r) for r in rows]


def sync_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_categories(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT category, COUNT(*) AS n
           FROM documents WHERE category IS NOT NULL AND category <> ''
           GROUP BY category ORDER BY n DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def has_enrichment(conn: sqlite3.Connection) -> bool:
    """True when the database carries the schema v6 enrichment columns."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
        return "primary_topic" in cols
    except sqlite3.OperationalError:
        return False


def facet(conn: sqlite3.Connection, column: str, limit: int = 120) -> list[tuple[str, int]]:
    """Distinct values of an enrichment column, most common first."""
    if not has_enrichment(conn):
        return []
    try:
        return [
            (r[0], r[1])
            for r in conn.execute(
                f"SELECT {column}, COUNT(*) n FROM documents "
                f"WHERE {column} IS NOT NULL AND {column} <> '' "
                f"GROUP BY {column} ORDER BY n DESC LIMIT ?", (limit,)
            )
        ]
    except sqlite3.OperationalError:
        return []


def list_doc_types(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT doc_type FROM documents ORDER BY doc_type"
    ).fetchall()
    return [r[0] for r in rows]


def search_docs(
    conn: sqlite3.Connection,
    query: str,
    doc_type: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    institution_type: Optional[str] = None,
    topic: Optional[str] = None,
    applicability: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    if not query.strip():
        return []
    limit = min(limit, 50)
    match = escape_fts(query)

    if not match:
        # Degenerate query — fall back to LIKE on title
        sql = "SELECT *, '' AS snippet FROM documents WHERE title LIKE ?"
        params: list = [f"%{query.strip()}%"]
        if doc_type:
            sql += " AND doc_type = ?"; params.append(doc_type)
        if status:
            sql += " AND status = ?";   params.append(status)
        if institution_type:
            sql += " AND institution_type = ?"; params.append(institution_type)
        if topic:
            # Primary OR secondary. A document about capital adequacy that
            # scored marginally higher on disclosures is still about capital
            # adequacy, and a filter reading only the winner hides it.
            sql += " AND (primary_topic = ? OR secondary_topics LIKE ?)"
            params += [topic, f'%"{topic}"%']
        if applicability:
            sql += " AND applicability = ?"; params.append(applicability)
        sql += " ORDER BY date DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    sql = """
        SELECT d.*, snippet(documents_fts, 1, '<<', '>>', ' … ', 18) AS snippet
        FROM documents_fts f
        JOIN documents d ON d.rowid = f.rowid
        WHERE documents_fts MATCH ?
    """
    params = [match]
    if doc_type:
        sql += " AND d.doc_type = ?";              params.append(doc_type)
    if category:
        sql += " AND d.category LIKE ?";           params.append(f"%{category}%")
    if status:
        sql += " AND d.status = ?";                params.append(status)
    if institution_type:
        sql += " AND d.institution_type = ?";      params.append(institution_type)
    if topic:
        sql += " AND (d.primary_topic = ? OR d.secondary_topics LIKE ?)"
        params += [topic, f'%"{topic}"%']
    if applicability:
        sql += " AND d.applicability = ?";         params.append(applicability)
    sql += " ORDER BY rank, d.date DESC LIMIT ?"
    params.append(limit)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        # FTS5 not available or index missing — fall back to LIKE
        sql2 = "SELECT *, '' AS snippet FROM documents WHERE title LIKE ?"
        p2: list = [f"%{query.strip()}%"]
        if doc_type:
            sql2 += " AND doc_type = ?"; p2.append(doc_type)
        sql2 += " ORDER BY date DESC LIMIT ?"
        p2.append(limit)
        return [dict(r) for r in conn.execute(sql2, p2).fetchall()]


def recent_docs(
    conn: sqlite3.Connection,
    doc_type: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    limit = min(limit, 100)
    sql = "SELECT * FROM documents WHERE 1=1"
    params: list = []
    if doc_type:
        sql += " AND doc_type = ?"; params.append(doc_type)
    if category:
        sql += " AND category LIKE ?"; params.append(f"%{category}%")
    sql += " ORDER BY date DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def new_docs_in_window(conn: sqlite3.Connection, since_iso: str, limit: int = 200) -> list[dict]:
    """Documents whose RBI publication date falls within the window.

    Using d.date (not first_seen) means the window matches what RBI actually
    published in that period, regardless of when we indexed it — so a fresh
    import doesn't flood the feed with years of history.
    """
    rows = conn.execute(
        """SELECT d.id, d.title, d.doc_type, d.category, d.date, d.source_url,
                  d.first_seen, d.last_changed, d.status, 'new' AS change_kind,
                  (SELECT COUNT(*) FROM document_revisions r WHERE r.doc_id = d.id) AS revisions,
                  (SELECT char_delta FROM document_revisions r WHERE r.doc_id = d.id
                     ORDER BY revision_no DESC LIMIT 1) AS char_delta
           FROM documents d
           WHERE d.date >= ?
           ORDER BY d.date DESC
           LIMIT ?""",
        (since_iso, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def amended_docs_in_window(conn: sqlite3.Connection, since_iso: str, limit: int = 200) -> list[dict]:
    """Documents where we detected an in-place edit (≥2 stored revisions) within the window.

    These are Master Directions that RBI silently updated — the whole point
    of the revision tracking system.
    """
    rows = conn.execute(
        """SELECT d.id, d.title, d.doc_type, d.category, d.date, d.source_url,
                  d.first_seen, d.last_changed, d.status, 'amended' AS change_kind,
                  (SELECT COUNT(*) FROM document_revisions r WHERE r.doc_id = d.id) AS revisions,
                  (SELECT char_delta FROM document_revisions r WHERE r.doc_id = d.id
                     ORDER BY revision_no DESC LIMIT 1) AS char_delta
           FROM documents d
           WHERE d.last_changed >= ?
             AND (SELECT COUNT(*) FROM document_revisions r2 WHERE r2.doc_id = d.id) > 1
           ORDER BY d.last_changed DESC
           LIMIT ?""",
        (since_iso, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_doc(conn: sqlite3.Connection, doc_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def relations_for(conn: sqlite3.Connection, doc_id: str) -> tuple[list[dict], list[dict]]:
    outgoing = conn.execute(
        """SELECT r.rel_type, r.dst_id, r.dst_ref_text, r.confidence, r.method, r.evidence,
                  d.title AS dst_title, d.date AS dst_date, d.doc_type AS dst_type
           FROM relations r LEFT JOIN documents d ON d.id = r.dst_id
           WHERE r.src_id = ? ORDER BY r.confidence DESC""",
        (doc_id,),
    ).fetchall()
    incoming = conn.execute(
        """SELECT r.rel_type, r.src_id, r.confidence, r.method, r.evidence,
                  d.title AS src_title, d.date AS src_date, d.doc_type AS src_type
           FROM relations r JOIN documents d ON d.id = r.src_id
           WHERE r.dst_id = ? ORDER BY d.date DESC""",
        (doc_id,),
    ).fetchall()
    return [dict(r) for r in outgoing], [dict(r) for r in incoming]


def revisions_for(conn: sqlite3.Connection, doc_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT revision_no, body_hash, title, captured_at, char_delta,
                  LENGTH(body) AS body_length
           FROM document_revisions WHERE doc_id = ? ORDER BY revision_no DESC""",
        (doc_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_revision_body(conn: sqlite3.Connection, doc_id: str, rev_no: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT body, title, captured_at FROM document_revisions WHERE doc_id = ? AND revision_no = ?",
        (doc_id, rev_no),
    ).fetchone()
    return dict(row) if row else None


def clauses_for(conn: sqlite3.Connection, doc_id: str, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        "SELECT id, clause_label, chapter, seq, text, needs_review "
        "FROM clauses WHERE doc_id = ? ORDER BY seq LIMIT ?",
        (doc_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def all_master_directions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT id, title, date, department, category, ref_no, status, source_url, pdf_url
           FROM documents
           WHERE doc_type IN ('master_direction', 'master_circular')
           ORDER BY date DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def recent_by_type(conn: sqlite3.Connection, doc_type: str, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        """SELECT id, title, date, category, status, source_url, pdf_url
           FROM documents WHERE doc_type = ?
           ORDER BY date DESC LIMIT ?""",
        (doc_type, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def chunked_count(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT doc_id) FROM clauses"
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

STATUS_BADGE = {
    "active":     "🟢",
    "superseded": "🔴",
    "repealed":   "🔴",
    "withdrawn":  "🟠",
}

DOC_TYPE_LABEL = {
    "master_direction": "Master Direction",
    "master_circular":  "Master Circular",
    "circular":         "Circular",
    "amendment":        "Amendment",
    "notification":     "Notification",
}

APPLICABILITY_BADGE = {
    "Applicable":        "🟢",
    "Likely Applicable": "🟡",
    "Not Applicable":    "⚪",
}

REL_LABEL = {
    "supersedes": "🔁 Supersedes",
    "amends":     "✏️ Amends",
    "repeals":    "🚫 Repeals",
    "withdraws":  "↩️ Withdraws",
}


def cmd(*args: str) -> str:
    """Render a CLI hint that actually runs on the reader's machine.

    The hints used to be `PYTHONPATH=python python3 -m rbi_intel ...`, which is
    POSIX shell syntax. On Windows `cmd.exe` reads the leading assignment as a
    program name and answers

        'PYTHONPATH' is not recognized as an internal or external command

    and `python3` generally does not exist there either — so every instruction
    this dashboard gave a Windows user failed on its first token. `rbi.py` sets
    up its own import path, so one form works everywhere.
    """
    return "python rbi.py " + " ".join(args)


def fmt_date(s: Optional[str]) -> str:
    if not s:
        return "—"
    try:
        return datetime.fromisoformat(s[:10]).strftime("%d %b %Y")
    except Exception:
        return s[:10]


def doc_label(row: dict) -> str:
    badge = STATUS_BADGE.get(row.get("status", ""), "")
    date = fmt_date(row.get("date"))
    title = row.get("title", row.get("id", ""))
    return f"{badge} {date} · {title}"


def snippet_html(snippet: str) -> str:
    """Highlight <<terms>> from FTS5 snippet in the Streamlit markdown."""
    return re.sub(r"<<(.+?)>>", r"**\1**", snippet)


# ---------------------------------------------------------------------------
# Tab: Overview
# ---------------------------------------------------------------------------

def tab_overview(conn: sqlite3.Connection):
    st.subheader("Index health")

    counts = doc_count(conn)
    total = sum(r["n"] for r in counts)
    if total == 0:
        st.warning(
            "Database is empty. Run `npm run sync` in the rbi-intel folder first, "
            "then `" + cmd("relations") + "` to build the graph.",
            icon="⚠️",
        )
        return

    # Summary metrics
    by_type: dict[str, int] = {}
    for r in counts:
        by_type[r["doc_type"]] = by_type.get(r["doc_type"], 0) + r["n"]

    chunked = chunked_count(conn)
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total documents", total)
    col2.metric("Master Directions", by_type.get("master_direction", 0))
    col3.metric("Circulars / Notifications",
                by_type.get("circular", 0) + by_type.get("notification", 0))
    col4.metric("Amendments", by_type.get("amendment", 0))
    col5.metric("Docs chunked", chunked)

    # Doc counts table
    df = pd.DataFrame(counts)
    df["doc_type"] = df["doc_type"].map(lambda t: DOC_TYPE_LABEL.get(t, t))
    st.dataframe(df.rename(columns={"doc_type": "Type", "status": "Status", "n": "Count"}),
                 use_container_width=True, hide_index=True)

    # ── Browse by document type ──────────────────────────────────────────────
    st.subheader("Browse by document type")
    st.caption("Click **Open** on any row to jump to the Document viewer.")

    BROWSE_TYPES = [
        ("master_direction", "📘 Master Directions"),
        ("master_circular",  "📗 Master Circulars"),
        ("circular",         "📄 Circulars"),
        ("amendment",        "✏️ Amendments"),
        ("notification",     "🔔 Notifications"),
        ("draft",            "📝 Drafts"),
    ]

    for dtype, label in BROWSE_TYPES:
        if not by_type.get(dtype):
            continue
        with st.expander(f"{label}  —  {by_type[dtype]:,} documents", expanded=(dtype == "master_direction")):
            rows = recent_by_type(conn, dtype, limit=15)
            for r in rows:
                badge = STATUS_BADGE.get(r.get("status", ""), "")
                cols = st.columns([6, 1, 1])
                cols[0].markdown(
                    f"{badge} **{fmt_date(r['date'])}** · {r['title'][:100]}"
                    + (f"  ·  *{r.get('category', '')}*" if r.get("category") else "")
                )
                cols[1].markdown(f"[RBI ↗]({r['source_url']})")
                if cols[2].button("Open", key=f"ov_{r['id']}"):
                    goto("📄 Document", r["id"])
            if by_type[dtype] > 15:
                st.caption(f"Showing 10 most recent. Switch to **{label.split()[-1]}** tab for full list.")

    # ── Enrichment coverage ─────────────────────────────────────────────
    # The number worth watching is "unclassified". A keyword taxonomy is only
    # as good as its coverage of the corpus it actually meets, and a rising
    # unclassified share is the signal that RBI has started using vocabulary
    # the dictionary does not know yet.
    if has_enrichment(conn):
        applic = facet(conn, "applicability")
        if applic:
            st.subheader("Enrichment coverage")
            total = sum(n for _, n in applic)
            topics = facet(conn, "primary_topic")
            unclassified = dict(topics).get("Unclassified", 0)
            generic = dict(facet(conn, "institution_type")).get("Cross-Institution / Generic", 0)

            m = st.columns(4)
            by_val = dict(applic)
            m[0].metric("Applicable", f"{by_val.get('Applicable', 0):,}")
            m[1].metric("Likely applicable", f"{by_val.get('Likely Applicable', 0):,}")
            m[2].metric("Not applicable", f"{by_val.get('Not Applicable', 0):,}")
            # Only show the delta when there is something to flag. A zero with
            # a red "0% of corpus" arrow reads as a problem, which is the
            # opposite of what full coverage means.
            m[3].metric(
                "Unclassified topic", f"{unclassified:,}",
                delta=(f"{100 * unclassified / total:.0f}% of corpus"
                       if total and unclassified else None),
                delta_color="inverse",
            )

            c1, c2 = st.columns(2)
            with c1:
                groups = facet(conn, "topic_group")
                if groups:
                    st.caption("By topic group")
                    st.dataframe(
                        pd.DataFrame(groups, columns=["Topic group", "Documents"]),
                        use_container_width=True, hide_index=True, height=240,
                    )
            with c2:
                insts = facet(conn, "institution_type", limit=12)
                if insts:
                    st.caption("By institution type")
                    st.dataframe(
                        pd.DataFrame(insts, columns=["Institution type", "Documents"]),
                        use_container_width=True, hide_index=True, height=240,
                    )

            if unclassified:
                with st.expander(
                    f"⚠️ {unclassified:,} document(s) matched no topic — "
                    f"add keywords to seed/taxonomy.json"
                ):
                    rows = conn.execute(
                        "SELECT id, title, date FROM documents WHERE primary_topic = 'Unclassified' "
                        "ORDER BY date DESC LIMIT 40"
                    ).fetchall()
                    st.dataframe(
                        pd.DataFrame([{"Date": fmt_date(r["date"]), "Title": r["title"]} for r in rows]),
                        use_container_width=True, hide_index=True,
                    )
            if generic:
                st.caption(
                    f"{generic:,} document(s) name no regulated-entity class in the title "
                    f"and fall back to Cross-Institution / Generic. That is normal — most RBI "
                    f"titles do not name one — not a classification failure."
                )

    # Relation counts
    rels = relation_count(conn)
    if rels:
        st.subheader("Relationship graph")
        rdf = pd.DataFrame(rels)
        rdf["rel_type"] = rdf["rel_type"].map(lambda r: REL_LABEL.get(r, r))
        st.dataframe(rdf.rename(columns={"rel_type": "Relation", "resolved": "Resolved", "unresolved": "Unresolved"}),
                     use_container_width=True, hide_index=True)

    # Sync history
    runs = sync_runs(conn)
    if runs:
        st.subheader("Recent sync runs")
        sdf = pd.DataFrame(runs)[["started_at", "finished_at", "source", "new_docs", "changed_docs", "errors"]]
        sdf["started_at"] = sdf["started_at"].str[:16].str.replace("T", " ")
        sdf["finished_at"] = sdf["finished_at"].str[:16].str.replace("T", " ", regex=False).fillna("—")
        st.dataframe(sdf.rename(columns={
            "started_at": "Started", "finished_at": "Finished",
            "source": "Source", "new_docs": "New", "changed_docs": "Changed", "errors": "Errors"
        }), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Tab: Search
# ---------------------------------------------------------------------------

def tab_search(conn: sqlite3.Connection):
    cats = ["All"] + [r["category"] for r in list_categories(conn)]
    types = ["All"] + list_doc_types(conn)

    col1, col2, col3, col4 = st.columns([3, 1, 1, 1])
    query    = col1.text_input("Search query", placeholder="e.g. KYC periodic updation")
    f_type   = col2.selectbox("Type", types)
    f_cat    = col3.selectbox("Category", cats)
    f_status = col4.selectbox("Status", ["All", "active", "superseded", "repealed", "withdrawn"])

    f_inst = f_topic = f_applic = "All"
    if has_enrichment(conn):
        insts  = facet(conn, "institution_type")
        topics = facet(conn, "primary_topic")
        applic = facet(conn, "applicability")
        if insts or topics or applic:
            e1, e2, e3 = st.columns([1, 2, 1])
            f_inst = e1.selectbox(
                "Institution type", ["All"] + [v for v, _ in insts],
                format_func=lambda v: v if v == "All" else f"{v} ({dict(insts)[v]})",
                help="Normalised regulated-entity class, derived from the title. "
                     "Distinct from Category, which is RBI's own listing grouping.",
            )
            f_topic = e2.selectbox(
                "Regulatory topic", ["All"] + [v for v, _ in topics],
                format_func=lambda v: v if v == "All" else f"{v} ({dict(topics)[v]})",
                help="Matches the primary topic or any secondary one.",
            )
            f_applic = e3.selectbox(
                "Applicability", ["All"] + [v for v, _ in applic],
                format_func=lambda v: v if v == "All" else f"{v} ({dict(applic)[v]})",
                help="Rule-based triage from the document title — not a legal determination.",
            )

    if not query.strip():
        st.caption("Enter a search term above.")
        return

    results = search_docs(
        conn, query,
        doc_type=None if f_type == "All" else f_type,
        category=None if f_cat == "All" else f_cat,
        status=None if f_status == "All" else f_status,
        institution_type=None if f_inst == "All" else f_inst,
        topic=None if f_topic == "All" else f_topic,
        applicability=None if f_applic == "All" else f_applic,
        limit=30,
    )

    if not results:
        st.info("No matching documents found.")
        return

    st.caption(f"{len(results)} result(s)")

    for r in results:
        badge = STATUS_BADGE.get(r.get("status", ""), "")
        dtype = DOC_TYPE_LABEL.get(r.get("doc_type", ""), r.get("doc_type", ""))
        with st.expander(f"{badge} {fmt_date(r['date'])} · {r['title']}"):
            st.caption(f"**{dtype}** · `{r['id']}` · {r.get('category', '—')}")
            if r.get("primary_topic"):
                bits = [
                    f"🏛 {r.get('institution_type') or '—'}",
                    f"🏷 {r['primary_topic']}",
                    f"{APPLICABILITY_BADGE.get(r.get('applicability') or '', '')} "
                    f"{r.get('applicability') or '—'}",
                ]
                if r.get("updated_date"):
                    bits.append(f"✏️ updated {fmt_date(r['updated_date'])}")
                st.caption(" · ".join(bits))
            if r.get("snippet"):
                st.markdown("**Excerpt:** " + snippet_html(r["snippet"]))
            if r.get("ref_no"):
                st.caption(f"Ref: {r['ref_no']}")
            cols = st.columns(3)
            cols[0].markdown(f"[View on RBI]({r['source_url']})")
            if r.get("pdf_url"):
                cols[1].markdown(f"[PDF]({r['pdf_url']})")
            if st.button("Open in Document viewer", key=f"open_{r['id']}"):
                    goto("📄 Document", r["id"])


# ---------------------------------------------------------------------------
# Tab: Master Directions
# ---------------------------------------------------------------------------

def tab_master_directions(conn: sqlite3.Connection):
    cats = ["All"] + [r["category"] for r in list_categories(conn)]
    col1, col2 = st.columns([2, 1])
    f_cat    = col1.selectbox("Category / entity type", cats)
    f_status = col2.selectbox("Status", ["active", "All", "superseded", "repealed", "withdrawn"])

    mds = all_master_directions(conn)
    if f_cat != "All":
        mds = [m for m in mds if (m.get("category") or "").find(f_cat) >= 0]
    if f_status != "All":
        mds = [m for m in mds if m.get("status") == f_status]

    st.caption(f"{len(mds)} Master Directions / Circulars")

    for m in mds:
        badge = STATUS_BADGE.get(m.get("status", ""), "")
        label = f"{badge} {fmt_date(m['date'])} · {m['title']}"
        with st.expander(label):
            c1, c2 = st.columns(2)
            c1.caption(f"**Department:** {m.get('department') or '—'}")
            c2.caption(f"**Category:** {m.get('category') or '—'}")
            if m.get("ref_no"):
                st.caption(f"**Ref:** {m['ref_no']}")
            cols = st.columns(3)
            cols[0].markdown(f"[View on RBI]({m['source_url']})")
            if m.get("pdf_url"):
                cols[1].markdown(f"[PDF]({m['pdf_url']})")
            if st.button("Open in Document viewer", key=f"md_{m['id']}"):
                    goto("📄 Document", m["id"])


# ---------------------------------------------------------------------------
# Tab: Change Feed
# ---------------------------------------------------------------------------

def tab_change_feed(conn: sqlite3.Connection):
    window = st.selectbox(
        "Show changes in the last…",
        ["1 day", "7 days", "30 days", "90 days", "180 days", "1 year"],
        index=2,
    )

    days = {"1 day": 1, "7 days": 7, "30 days": 30, "90 days": 90, "180 days": 180, "1 year": 365}[window]
    # Use date-only ISO string so it compares cleanly against d.date (YYYY-MM-DD)
    since_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    since_ts   = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    new_rows     = new_docs_in_window(conn, since_date)
    amended_rows = amended_docs_in_window(conn, since_ts)

    col_a, col_b, col_c = st.columns(3)
    col_a.metric("Total", len(new_rows) + len(amended_rows))
    col_b.metric("🆕 Published by RBI", len(new_rows))
    col_c.metric("✏️ Silently amended", len(amended_rows))

    # ── Section 1: newly published ──────────────────────────────────────────
    st.subheader(f"🆕 Published in the last {window}")
    st.caption(
        "Filtered by the date RBI assigned to the document — not by when your local index first saw it."
    )
    if not new_rows:
        st.info(f"No documents with an RBI publication date in the last {window}.")
    else:
        for r in new_rows:
            badge = STATUS_BADGE.get(r.get("status", ""), "")
            dtype = DOC_TYPE_LABEL.get(r.get("doc_type", ""), r.get("doc_type", ""))
            with st.expander(f"{badge} {fmt_date(r['date'])} · {r['title']}"):
                c1, c2 = st.columns(2)
                c1.caption(f"**Type:** {dtype}")
                c2.caption(f"**Category:** {r.get('category') or '—'}")
                cols = st.columns(2)
                cols[0].markdown(f"[View on RBI]({r['source_url']})")
                if st.button("Open in Document viewer", key=f"cf_new_{r['id']}"):
                    goto("📄 Document", r["id"])

    st.divider()

    # ── Section 2: silently amended ─────────────────────────────────────────
    st.subheader(f"✏️ Silently amended in the last {window}")
    st.caption(
        "Master Directions where RBI edited the text in place — our scraper detected the change "
        "and stored both versions. Use the Document viewer → Revisions tab to diff them."
    )
    if not amended_rows:
        st.info(
            f"No in-place edits detected in the last {window}. "
            "Run `npm run sync:md` regularly — RBI does not announce in-place edits."
        )
    else:
        for r in amended_rows:
            badge = STATUS_BADGE.get(r.get("status", ""), "")
            dtype = DOC_TYPE_LABEL.get(r.get("doc_type", ""), r.get("doc_type", ""))
            delta = r.get("char_delta")
            delta_str = f"  ({'+' if delta and delta > 0 else ''}{delta:,} chars)" if delta else ""
            with st.expander(
                f"{badge} {fmt_date(r['date'])} · {r['title']}  ·  "
                f"rev {r.get('revisions', '?')}{delta_str}"
            ):
                c1, c2, c3 = st.columns(3)
                c1.caption(f"**Type:** {dtype}")
                c2.caption(f"**Original date:** {fmt_date(r.get('date'))}")
                c3.caption(f"**Last changed:** {fmt_date(r.get('last_changed'))}")
                cols = st.columns(2)
                cols[0].markdown(f"[View on RBI]({r['source_url']})")
                if st.button("Open in Document viewer", key=f"cf_amd_{r['id']}"):
                    goto("📄 Document", r["id"])


# ---------------------------------------------------------------------------
# Tab: Lineage
# ---------------------------------------------------------------------------

def tab_lineage(conn: sqlite3.Connection):
    st.header("🔗 Lineage")
    st.caption(
        "What this document supersedes / amends / repeals, and which later "
        "documents have cited it."
    )

    # Lineage follows the Document viewer. It is a view *of* a document, not an
    # independent search, so opening a document and then opening Lineage should
    # not ask which document you meant.
    #
    # Two bugs made that not work before: the text box read
    # session_state["lineage_id"] while its widget key was "lineage_doc_id" —
    # a name nothing ever wrote — and `value=` is ignored anyway once a keyed
    # widget has state. The current document now seeds the widget key itself,
    # before the widget is instantiated.
    # `doc_id` is the shared, non-widget pointer to whatever the viewer is
    # showing. It has to be non-widget: Streamlit garbage-collects the state of
    # widgets that were not rendered in a given run, so seeding the text box's
    # own key from another page is discarded before this page ever draws.
    # The marker below records which document the box was last synced to, so
    # the box follows the viewer when it moves and otherwise leaves whatever
    # was typed alone.
    current = st.session_state.get("doc_id") or ""
    if st.session_state.get("_lineage_synced_to") != current:
        st.session_state["lineage_doc_id"] = current
        st.session_state["_lineage_synced_to"] = current

    doc_id_input = st.text_input(
        "Document ID",
        placeholder="e.g. rbi:md:11566",
        key="lineage_doc_id",
        help="Follows the document open in the viewer. Edit it to look at another.",
    )
    if current:
        st.caption(f"Following the open document · `{current}`")

    # Quick-pick from Master Directions
    mds = all_master_directions(conn)
    md_options = {f"{fmt_date(m['date'])} · {m['title'][:80]}": m["id"] for m in mds}
    pick = st.selectbox("…or pick a Master Direction", ["—"] + list(md_options.keys()))
    if pick != "—":
        doc_id_input = md_options[pick]

    if not (doc_id_input or "").strip():
        st.info(
            "Open a document from Search or Master Directions and this tab will follow it, "
            "or paste an ID above.",
            icon="ℹ️",
        )
        return

    doc_id_input = doc_id_input.strip()
    doc = get_doc(conn, doc_id_input)
    if not doc:
        st.error(f"No document found with id `{doc_id_input}`.")
        return

    # Keep the viewer and this tab pointed at the same document, so switching
    # back to Document does not jump somewhere else — and move the sync marker
    # with it, or the next run would snap the box back to the old id.
    st.session_state["doc_id"] = doc_id_input
    st.session_state["_lineage_synced_to"] = doc_id_input

    if st.button("📄 Open this document in the viewer"):
        goto("📄 Document", doc_id_input)

    badge = STATUS_BADGE.get(doc.get("status", ""), "")
    dtype = DOC_TYPE_LABEL.get(doc.get("doc_type", ""), doc.get("doc_type", ""))
    st.markdown(f"### {badge} {doc['title']}")
    st.caption(f"{dtype} · {fmt_date(doc['date'])} · `{doc['id']}`")

    if doc.get("status") in ("superseded", "repealed", "withdrawn"):
        st.warning(
            f"This document is **{doc['status'].upper()}** — do not treat as the current in-force rule.",
            icon="⚠️",
        )

    outgoing, incoming = relations_for(conn, doc_id_input)

    if outgoing:
        st.subheader("This document…")
        for rel in outgoing:
            label = REL_LABEL.get(rel["rel_type"], rel["rel_type"])
            dst_title = rel.get("dst_title") or rel.get("dst_ref_text") or rel.get("dst_id") or "?"
            dst_date  = fmt_date(rel.get("dst_date"))
            conf      = rel.get("confidence", 0)
            with st.expander(f"{label} → **{dst_title}** ({dst_date})"):
                if rel.get("evidence"):
                    st.markdown(f"*Evidence:* {rel['evidence']}")
                st.caption(
                    f"Confidence: {conf:.0%} · Method: {rel.get('method', '?')}"
                    + (f" · `{rel['dst_id']}`" if rel.get("dst_id") else " · unresolved reference")
                )
                if rel.get("dst_id") and st.button("Open target document", key=f"out_{rel['dst_id']}"):
                    goto("📄 Document", rel["dst_id"])
    else:
        st.info("No outgoing relations found for this document.")

    if incoming:
        st.subheader("Later documents that cite this one…")
        for rel in incoming:
            label = REL_LABEL.get(rel["rel_type"], rel["rel_type"])
            src_title = rel.get("src_title") or rel.get("src_id") or "?"
            src_date  = fmt_date(rel.get("src_date"))
            conf      = rel.get("confidence", 0)
            with st.expander(f"{label} ← **{src_title}** ({src_date})"):
                if rel.get("evidence"):
                    st.markdown(f"*Evidence:* {rel['evidence']}")
                st.caption(
                    f"Confidence: {conf:.0%} · Method: {rel.get('method', '?')}"
                    + (f" · `{rel['src_id']}`" if rel.get("src_id") else "")
                )
                if rel.get("src_id") and st.button("Open citing document", key=f"inc_{rel['src_id']}"):
                    goto("📄 Document", rel["src_id"])


# ---------------------------------------------------------------------------
# Tab: Document viewer
# ---------------------------------------------------------------------------

def tab_document(conn: sqlite3.Connection):
    doc_id_input = st.text_input(
        "Document ID",
        value=st.session_state.get("doc_id", ""),
        placeholder="e.g. rbi:md:11566",
        key="document_doc_id",
    )

    if not doc_id_input.strip():
        st.caption("Enter a document ID, or click 'Open in Document viewer' from Search, Master Directions, or Change Feed.")
        return

    doc = get_doc(conn, doc_id_input.strip())
    if not doc:
        st.error(f"No document found with id `{doc_id_input.strip()}`.")
        return

    # Header
    badge = STATUS_BADGE.get(doc.get("status", ""), "")
    dtype = DOC_TYPE_LABEL.get(doc.get("doc_type", ""), doc.get("doc_type", ""))
    st.markdown(f"## {badge} {doc['title']}")

    if doc.get("status") in ("superseded", "repealed", "withdrawn"):
        st.warning(
            f"This document is **{doc['status'].upper()}**. "
            "Use the Lineage tab to find the current in-force version.",
            icon="⚠️",
        )

    c1, c2, c3 = st.columns(3)
    c1.caption(f"**Type:** {dtype}")
    c2.caption(f"**Date:** {fmt_date(doc.get('date'))}")
    c3.caption(f"**Department:** {doc.get('department') or '—'}")
    c1.caption(f"**Category:** {doc.get('category') or '—'}")
    c2.caption(f"**Ref:** {doc.get('ref_no') or '—'}")
    c3.caption(f"**Regulator:** {doc.get('regulator', 'RBI')}")

    col_a, col_b = st.columns(2)
    col_a.markdown(f"[View on RBI website]({doc['source_url']})")
    if doc.get("pdf_url"):
        col_b.markdown(f"[Download PDF]({doc['pdf_url']})")

    st.divider()

    subtab_body, subtab_clauses, subtab_revisions = st.tabs(["📄 Body", "🔖 Clauses", "🕒 Revisions"])

    with subtab_body:
        revisions = revisions_for(conn, doc_id_input.strip())
        if len(revisions) > 1:
            def _rev_label(i: int, r: dict) -> str:
                ts = r["captured_at"][:16].replace("T", " ")
                note = "current" if i == 0 else (f"{r['char_delta']:+,} chars" if r.get("char_delta") else "")
                return f"Rev {r['revision_no']} — {ts} ({note})"
            rev_options = {_rev_label(i, r): r["revision_no"] for i, r in enumerate(revisions)}
            chosen_label = st.selectbox("Viewing revision", list(rev_options.keys()))
            chosen_rev = rev_options[chosen_label]
            rev_data = get_revision_body(conn, doc_id_input.strip(), chosen_rev)
            body_text = rev_data["body"] if rev_data else doc.get("body", "")
        else:
            body_text = doc.get("body", "")

        if body_text:
            st.text_area("Document text", body_text, height=500)
        else:
            st.info("Body text not available. The PDF may not have been parsed — check if a PDF URL is present above.")

    with subtab_clauses:
        clauses = clauses_for(conn, doc_id_input.strip())
        if not clauses:
            st.info(
                "No clauses extracted yet. Run:\n\n"
                f"```\n{cmd('chunk', doc_id_input.strip())}\n```"
            )
        else:
            st.caption(f"{len(clauses)} clauses")
            for cl in clauses:
                chapter = f" · {cl['chapter']}" if cl.get("chapter") else ""
                flag = " ⚠️" if cl.get("needs_review") else ""
                with st.expander(f"**{cl['clause_label']}**{chapter}{flag}"):
                    st.write(cl["text"])

    with subtab_revisions:
        if not revisions:
            st.info("No revision history found.")
        else:
            st.caption(f"{len(revisions)} revision(s) stored")
            rdf = pd.DataFrame(revisions)
            rdf["captured_at"] = rdf["captured_at"].str[:16].str.replace("T", " ")
            rdf["char_delta"] = rdf["char_delta"].apply(
                lambda d: f"{'+' if d and d > 0 else ''}{d:,}" if d is not None else "—"
            )
            rdf["body_length"] = rdf["body_length"].apply(lambda n: f"{n:,}" if n else "—")
            st.dataframe(
                rdf[["revision_no", "captured_at", "body_length", "char_delta", "title"]].rename(columns={
                    "revision_no": "Rev", "captured_at": "Captured",
                    "body_length": "Length (chars)", "char_delta": "Delta", "title": "Title at capture"
                }),
                use_container_width=True,
                hide_index=True,
            )


# ---------------------------------------------------------------------------
# Navigation helper — call this instead of setting active_tab + rerun directly
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Compliance layer — requirements, retrieval and grounded Q&A
#
# Ported from the standalone pipeline's app_streamlit.py and 06_query_cli.py,
# repointed from inventory.json at the shared database.
# ---------------------------------------------------------------------------

SEEDED_BANNER = (
    "The **mapping** and **assessment** columns are model-drafted placeholders "
    "with no access to any real policy register. They are illustrative "
    "scaffolding, not evidence of compliance, and must be replaced with actual "
    "policy, process and control artefacts before any compliance use. "
    "Rows marked `reviewed` or `sourced` have been checked by a person; rows "
    "marked `seeded` have not."
)

CLASSIFICATION_BADGE = {
    "Compliant":           "🟢",
    "Partially Compliant": "🟡",
    "Gap":                 "🔴",
    "Not Applicable":      "⚪",
    "To Be Confirmed":     "🔵",
}

SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}

# Weights carried over verbatim from 06_query_cli.py's score_requirement().
# They were tuned against real questions and are the reason keyword hits beat
# incidental matches in the body of a long requirement.
FIELD_WEIGHTS = (
    ("keywords",       4.0),
    ("clause_title",   3.0),
    ("business_area",  3.0),
    ("clause_label",   3.0),
    ("requirement",    1.5),
    ("timeline",       2.0),
    ("finding",        1.0),
    ("policy",         1.0),
    # Not in the original, which had no source text to search: the file
    # pipeline only ever held the model's paraphrase. Scoring the actual clause
    # is what lets a query using the regulation's own vocabulary find the
    # record even when the paraphrase chose different words — and it is the
    # only grounded field in the set. Weighted low because it is long, so a
    # passing mention should not outrank a keyword or title hit.
    ("clause_text",    0.5),
)

STOPWORDS = set(
    "a an the is are was were do does did of for to in on at by with and or if we "
    "our my your it its as be been from that this these those what which who when "
    "where how why should shall must can may need require required".split()
)


def tokenize(q: str) -> list[str]:
    toks = re.sub(r"[^a-z0-9₹\s]", " ", q.lower()).split()
    return [t for t in toks if len(t) > 2 and t not in STOPWORDS]


def requirement_rows(
    conn: sqlite3.Connection,
    doc_id: Optional[str] = None,
    obligation: Optional[str] = None,
    classification: Optional[str] = None,
    severity: Optional[str] = None,
    business_area: Optional[str] = None,
    provenance: Optional[str] = None,
    limit: int = 2000,
) -> list[dict]:
    sql = [
        """SELECT r.id, r.doc_id, r.clause_id, r.clause_title, r.requirement,
                  r.obligation_type, r.branch_relevance, r.applicability, r.timeline,
                  r.keywords, c.clause_label, c.chapter, c.text AS clause_text,
                  d.title AS doc_title,
                  m.business_area, m.policy, m.process, m.control, m.control_type,
                  m.owner_process, m.owner_control, m.evidence_required,
                  m.classification, m.finding, m.recommendation, m.severity, m.provenance,
                  bo.name AS business_area_name,
                  op.role AS owner_process_role, oc.role AS owner_control_role
           FROM requirements r
           JOIN clauses c    ON c.id = r.clause_id
           JOIN documents d  ON d.id = r.doc_id
           LEFT JOIN req_mappings m   ON m.req_id = r.id
           LEFT JOIN business_areas bo ON bo.id = m.business_area
           LEFT JOIN owners op ON op.id = m.owner_process
           LEFT JOIN owners oc ON oc.id = m.owner_control
           WHERE 1=1"""
    ]
    params: list = []
    for col, val in (
        ("r.doc_id", doc_id), ("r.obligation_type", obligation),
        ("m.classification", classification), ("m.severity", severity),
        ("m.business_area", business_area), ("m.provenance", provenance),
    ):
        if val:
            sql.append(f"AND {col} = ?")
            params.append(val)
    sql.append("ORDER BY r.doc_id, c.seq LIMIT ?")
    params.append(limit)
    try:
        return [dict(r) for r in conn.execute(" ".join(sql), params)]
    except sqlite3.OperationalError:
        # Database predates schema v5.
        return []


def _distinct(conn: sqlite3.Connection, sql: str) -> list[str]:
    try:
        return [r[0] for r in conn.execute(sql) if r[0]]
    except sqlite3.OperationalError:
        return []


def _searchable(row: dict) -> dict[str, str]:
    try:
        kw = " ".join(json.loads(row.get("keywords") or "[]"))
    except json.JSONDecodeError:
        kw = ""
    return {
        "keywords": kw,
        "clause_title": row.get("clause_title") or "",
        "business_area": row.get("business_area_name") or "",
        "clause_label": row.get("clause_label") or "",
        "requirement": row.get("requirement") or "",
        "timeline": row.get("timeline") or "",
        "finding": (row.get("finding") or "") + " " + (row.get("recommendation") or ""),
        "policy": " ".join(filter(None, [row.get("policy"), row.get("process"), row.get("control")])),
        "clause_text": row.get("clause_text") or "",
    }


def retrieve(conn: sqlite3.Connection, query: str, k: int = 14) -> list[dict]:
    """Hybrid retrieval: FTS5 for recall, field weights for ranking.

    The original was keyword-only over an in-memory list, which was fine for
    ~200 records in a JSON file. Against a database that will hold tens of
    thousands, FTS5 narrows the candidate set first — using the same index the
    document search already maintains — and the original weighted scorer then
    ranks what comes back. When FTS returns nothing (a degenerate query, or a
    term that only appears in a model-written paraphrase rather than the source
    text), it falls back to scoring everything, so recall never depends on the
    index alone.
    """
    toks = tokenize(query)
    if not toks:
        return []

    candidates: list[dict] = []
    match = escape_fts(query)
    if match:
        try:
            rows = conn.execute(
                """SELECT r.id FROM documents_fts f
                   JOIN documents d ON d.rowid = f.rowid
                   JOIN requirements r ON r.doc_id = d.id
                   WHERE documents_fts MATCH ? LIMIT 4000""",
                (match,),
            ).fetchall()
            ids = {r[0] for r in rows}
            if ids:
                candidates = [x for x in requirement_rows(conn) if x["id"] in ids]
        except sqlite3.OperationalError:
            candidates = []

    if not candidates:
        candidates = requirement_rows(conn)

    scored: list[tuple[float, dict]] = []
    for row in candidates:
        fields = _searchable(row)
        score = 0.0
        for tok in toks:
            for name, weight in FIELD_WEIGHTS:
                if tok in fields[name].lower():
                    score += weight
        if score > 0:
            scored.append((score, row))

    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:k]]


def build_answer_prompt(question: str, hits: list[dict]) -> tuple[str, str]:
    corpus = "\n\n---\n\n".join(
        "\n".join([
            f"[{h['id']}] {h['doc_title']} clause {h['clause_label']} — {h['clause_title'] or '(untitled)'}",
            f"Business area: {h.get('business_area_name') or '?'} | "
            f"Branch relevance: {h.get('branch_relevance') or '?'} | "
            f"Timeline: {h.get('timeline') or 'none'}",
            f"Requirement: {h['requirement']}",
            f"Source clause text: {(h.get('clause_text') or '')[:800]}",
            (f"SEEDED mapping — policy: {h.get('policy')} | process: {h.get('process')} | "
             f"control: {h.get('control')} | evidence: {h.get('evidence_required')}"
             if h.get("policy") else "SEEDED mapping: none"),
            (f"SEEDED assessment — {h.get('classification')} (severity {h.get('severity')}): "
             f"{h.get('finding')} Recommendation: {h.get('recommendation')}"
             if h.get("classification") else "SEEDED assessment: none"),
        ])
        for h in hits
    )
    system = (
        "You answer questions for the Branch Risk Department of an Indian bank, using ONLY "
        "the regulatory records supplied below. Do not use outside knowledge of RBI rules; "
        "if the records do not cover the question, say so plainly and name the closest "
        "records that do exist.\n\n"
        "Cite the record ID and clause inline, like [rbi:md:12798#CHIV-A-106]. "
        "Distinguish sharply between what the regulation requires — the Requirement and "
        "Source clause text lines, which are grounded — and what the SEEDED mapping and "
        "assessment say about the bank's current state, which are placeholders with no "
        "evidence behind them. Flag the seeded layer once per answer, not every sentence.\n\n"
        "Be concise: 2-5 short paragraphs, plain prose, no headings.\n"
        'Return JSON: {"answer": "...", "citations": ["record id", ...]}'
    )
    user = f"RETRIEVED RECORDS\n{corpus}\n\nQUESTION: {question}"
    return system, user


# ---------------------------------------------------------------------------
# Tab: Requirements
# ---------------------------------------------------------------------------

def tab_requirements(conn: sqlite3.Connection):
    st.header("📋 Regulatory Obligations")
    st.caption(
        "One record per clause that places an obligation on the bank — what must be "
        "done, by when, and who owns it. Extracted from the clause text; the internal "
        "mapping alongside it is seeded scaffolding."
    )

    total = 0
    try:
        total = conn.execute("SELECT COUNT(*) FROM requirements").fetchone()[0]
    except sqlite3.OperationalError:
        st.error(
            "This database has no `requirements` table. Run `npm run init` to migrate it.",
            icon="🚨",
        )
        return

    if total == 0:
        st.info(
            "No obligations extracted yet.\n\n"
            "Run these from the `rbi-intel` folder — the one containing "
            "`rbi.py`. Works on Windows, macOS and Linux.\n\n"
            "```\n"
            f"{cmd('chunk', '--all-master-directions')}\n"
            f"{cmd('extract')}\n"
            f"{cmd('scaffold')}\n"
            "```",
            icon="ℹ️",
        )
        return

    st.warning(SEEDED_BANNER, icon="⚠️")

    docs = _distinct(conn, "SELECT DISTINCT doc_id FROM requirements ORDER BY doc_id")
    doc_titles = {
        r[0]: r[1] for r in conn.execute(
            "SELECT id, title FROM documents WHERE id IN "
            "(SELECT DISTINCT doc_id FROM requirements)"
        )
    }

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        doc_pick = st.selectbox(
            "Direction", ["All"] + docs,
            format_func=lambda d: "All" if d == "All" else (doc_titles.get(d, d)[:60]),
        )
    with c2:
        obl = st.selectbox("Obligation type", ["All"] + _distinct(
            conn, "SELECT DISTINCT obligation_type FROM requirements ORDER BY 1"))
    with c3:
        cls = st.selectbox("Assessment", ["All"] + _distinct(
            conn, "SELECT DISTINCT classification FROM req_mappings ORDER BY 1"))
    with c4:
        sev = st.selectbox("Severity", ["All", "High", "Medium", "Low"])

    rows = requirement_rows(
        conn,
        doc_id=None if doc_pick == "All" else doc_pick,
        obligation=None if obl == "All" else obl,
        classification=None if cls == "All" else cls,
        severity=None if sev == "All" else sev,
    )

    text_filter = st.text_input("Filter text", placeholder="e.g. leverage ratio, ICAAP, disclosure")
    if text_filter.strip():
        toks = tokenize(text_filter)
        rows = [
            r for r in rows
            if any(t in " ".join(_searchable(r).values()).lower() for t in toks)
        ]

    st.caption(f"{len(rows):,} of {total:,} obligations")

    if not rows:
        st.info("Nothing matches those filters.")
        return

    m1, m2, m3 = st.columns(3)
    seeded_n = sum(1 for r in rows if (r.get("provenance") or "seeded") == "seeded")
    m1.metric("Shown", f"{len(rows):,}")
    m2.metric("Still seeded", f"{seeded_n:,}")
    m3.metric("High severity", f"{sum(1 for r in rows if r.get('severity') == 'High'):,}")

    table = pd.DataFrame([{
        "Clause": r["clause_label"],
        "Title": r["clause_title"] or "—",
        "Type": r["obligation_type"] or "—",
        "Branch": r["branch_relevance"] or "—",
        "Business area": r.get("business_area_name") or "—",
        "Assessment": f"{CLASSIFICATION_BADGE.get(r.get('classification') or '', '')} "
                      f"{r.get('classification') or '—'}".strip(),
        "Severity": r.get("severity") or "—",
        "Provenance": r.get("provenance") or "—",
    } for r in rows])
    st.dataframe(table, width=True, hide_index=True, height=420)

    st.divider()
    st.subheader("Detail")
    pick = st.selectbox(
        "Obligation",
        rows,
        format_func=lambda r: f"{r['clause_label']} — {r['clause_title'] or '(untitled)'}",
    )
    if not pick:
        return

    left, right = st.columns([3, 2])
    with left:
        st.markdown(f"**{pick['clause_title'] or '(untitled)'}**  ·  `{pick['id']}`")
        st.markdown(pick["requirement"] or "_no requirement text_")
        st.caption(
            f"Obligation: {pick['obligation_type'] or '—'} · "
            f"Branch relevance: {pick['branch_relevance'] or '—'} · "
            f"Timeline: {pick['timeline'] or 'none'} · "
            f"Applies to: {pick['applicability'] or '—'}"
        )
        with st.expander("Source clause text (grounded)"):
            st.text(pick.get("clause_text") or "")
    with right:
        prov = pick.get("provenance")
        if not prov:
            st.info("No internal mapping yet — run `scaffold`.")
        else:
            if prov == "seeded":
                st.warning("SEEDED — placeholder, not evidence.", icon="⚠️")
            else:
                st.success(f"Provenance: {prov}", icon="✅")
            st.markdown(
                f"**Business area** {pick.get('business_area_name') or '—'}  \n"
                f"**Policy** {pick.get('policy') or '—'}  \n"
                f"**Process** {pick.get('process') or '—'}  \n"
                f"**Control** {pick.get('control') or '—'} ({pick.get('control_type') or '—'})  \n"
                f"**Process owner** {pick.get('owner_process_role') or '—'}  \n"
                f"**Control owner** {pick.get('owner_control_role') or '—'}  \n"
                f"**Evidence** {pick.get('evidence_required') or '—'}"
            )
            st.divider()
            st.markdown(
                f"**Assessment** {CLASSIFICATION_BADGE.get(pick.get('classification') or '', '')} "
                f"{pick.get('classification') or '—'} "
                f"(severity {pick.get('severity') or '—'})  \n"
                f"{pick.get('finding') or ''}  \n\n"
                f"_Recommendation:_ {pick.get('recommendation') or '—'}"
            )

    st.download_button(
        "⬇️ Download filtered set (CSV)",
        table.to_csv(index=False).encode("utf-8"),
        file_name="rbi_obligations.csv",
        mime="text/csv",
    )


# ---------------------------------------------------------------------------
# Tab: Ask
# ---------------------------------------------------------------------------

def tab_ask(conn: sqlite3.Connection):
    st.header("💬 Ask")
    st.caption(
        "Retrieval-augmented over the extracted obligations only. Nothing outside "
        "this database is used, and the answer names the records it consulted."
    )

    try:
        total = conn.execute("SELECT COUNT(*) FROM requirements").fetchone()[0]
    except sqlite3.OperationalError:
        total = 0
    if not total:
        st.info(
            "No obligations extracted yet. From the `rbi-intel` folder run:\n\n"
            "```\n"
            f"{cmd('chunk', '--all-master-directions')}\n"
            f"{cmd('extract')}\n"
            "```",
            icon="ℹ️",
        )
        return

    st.warning(SEEDED_BANNER, icon="⚠️")

    question = st.text_input(
        "Question",
        placeholder="e.g. What is the minimum CET1 ratio and when must it be met?",
    )
    c1, c2 = st.columns([1, 3])
    with c1:
        k = st.slider("Records to retrieve", 4, 30, 14)
    with c2:
        provider_name = st.selectbox(
            "Provider", ["auto", "gemini", "anthropic", "stub"],
            help="'stub' returns canned text without calling any API — useful to check "
                 "retrieval quality on its own.",
        )

    if not question.strip():
        return

    hits = retrieve(conn, question, k=k)
    if not hits:
        st.info("No records matched that query.")
        return

    with st.expander(f"Records retrieved ({len(hits)})", expanded=False):
        for h in hits:
            st.markdown(
                f"- `{h['id']}` **{h['clause_title'] or '(untitled)'}** — "
                f"{(h['requirement'] or '')[:160]}"
            )

    if not st.button("Answer", type="primary"):
        return

    try:
        sys.path.insert(0, str(Path(__file__).parent / "python"))
        from rbi_intel.llm import LLMError, QuotaExhausted, get_provider
    except ImportError as e:
        st.error(f"Cannot import the LLM layer: {e}")
        return

    try:
        provider = get_provider(None if provider_name == "auto" else provider_name)
    except Exception as e:
        st.error(str(e), icon="🚨")
        return

    system, user = build_answer_prompt(question, hits)
    with st.spinner(f"Asking {provider.name} ({provider.model})…"):
        try:
            result = provider.json_call(
                system, user,
                response_schema={
                    "type": "object",
                    "properties": {
                        "answer":    {"type": "string"},
                        "citations": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["answer"],
                },
                max_output_tokens=1500,
            )
        except QuotaExhausted as e:
            st.error(f"{e}", icon="🚨")
            return
        except LLMError as e:
            st.error(f"{e}", icon="🚨")
            return

    st.markdown(result.get("answer") or "_no answer returned_")
    cites = result.get("citations") or [h["id"] for h in hits[:8]]
    st.caption("Records consulted: " + ", ".join(f"`{c}`" for c in cites))


PAGES = [
    "📊 Overview",
    "🔍 Search",
    "📋 Master Directions",
    "🔄 Change Feed",
    "🔗 Lineage",
    "📄 Document",
    "📋 Obligations",
    "💬 Ask",
]


def goto(page: str, doc_id: str | None = None) -> None:
    """Navigate to a page, optionally pre-loading a document ID.

    The target is stashed and applied at the top of the next run, before the
    sidebar radio is instantiated. It has to be done that way round: once a
    widget with a `key` exists, Streamlit takes its value from
    `session_state[key]` and ignores the `index=` argument entirely. The
    previous version set an `_page_idx` that `index=` then read, so
    "Open in Document viewer" set a variable, triggered a rerun, and landed
    back on the same page — a button that looked wired up and did nothing.
    """
    st.session_state["_nav_to"] = page
    if doc_id is not None:
        # Lineage reads `doc_id` and syncs itself; it deliberately does NOT get
        # its widget key written here, because that state would be collected
        # before the Lineage page renders.
        st.session_state["doc_id"] = doc_id
    st.rerun()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    mark = logo_data_uri("sber-india-mark.svg")
    st.set_page_config(
        page_title="Sber India · RBI Regulatory Intelligence",
        page_icon=mark or "🏦",
        layout="wide",
    )
    inject_brand_css()

    # ── consume one-shot navigation request before the sidebar renders ──────
    # Writing the radio's own key is what actually moves it; `index=` is only
    # consulted on the very first render.
    nav_to = st.session_state.pop("_nav_to", None)
    if nav_to in PAGES:
        st.session_state["_nav_radio"] = nav_to

    # ── Sidebar ─────────────────────────────────────────────────────────────
    with st.sidebar:
        sidebar_logo = logo_data_uri("sber-india-logo.svg")
        if sidebar_logo:
            st.markdown(
                f'<img src="{sidebar_logo}" alt="Sber India" '
                f'style="width:100%;max-width:210px;margin:2px 0 10px 0;"/>',
                unsafe_allow_html=True,
            )
        else:
            st.title("Sber India")
        st.caption("RBI Regulatory Intelligence")
        db_path = _db_path()
        st.caption(f"Database: `{db_path}`")

        if not db_exists():
            st.error(
                "Database not found. Run `npm run init` in the rbi-intel folder to "
                "create it, then `npm run sync` (or `python rbi.py ingest ...` if "
                "rbi.org.in is not reachable from this machine) to populate it.",
                icon="🚨",
            )
            st.stop()

        try:
            conn = get_conn()
            total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            st.success(f"{total:,} documents indexed", icon="✅")
        except Exception as e:
            st.error(f"Cannot open database: {e}", icon="🚨")
            st.stop()

        st.divider()

        # Radio navigation. No `index=` — the selection lives in
        # session_state["_nav_radio"], which goto() writes directly.
        if st.session_state.get("_nav_radio") not in PAGES:
            st.session_state["_nav_radio"] = PAGES[0]
        page = st.radio("Navigate", PAGES, key="_nav_radio")

        st.divider()
        if st.button("🔄 Refresh data"):
            st.cache_resource.clear()
            st.rerun()

    # ── Page dispatch ────────────────────────────────────────────────────────
    brand_header()

    if page == "📊 Overview":
        tab_overview(conn)
    elif page == "🔍 Search":
        tab_search(conn)
    elif page == "📋 Master Directions":
        tab_master_directions(conn)
    elif page == "🔄 Change Feed":
        tab_change_feed(conn)
    elif page == "🔗 Lineage":
        tab_lineage(conn)
    elif page == "📄 Document":
        tab_document(conn)
    elif page == "📋 Obligations":
        tab_requirements(conn)
    elif page == "💬 Ask":
        tab_ask(conn)

    brand_footer()


if __name__ == "__main__":
    main()
