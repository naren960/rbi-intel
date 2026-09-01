"""Ingest a document from local disk into the shared database.

The Node scrapers are the right way to get documents in — when rbi.org.in is
reachable. On the network this system actually runs on, it is not: the
corporate firewall blocks it outright, and every code path in the package
began at `npm run sync`. So the entire pipeline was unusable on the one
machine that mattered.

This is the offline door. Point it at a PDF, DOCX or TXT that was obtained
some other way and it creates a `documents` row indistinguishable from a
scraped one except in the way that matters: `source = 'local'`, so nobody
later mistakes a hand-placed file for something verified against RBI's site.

    python -m rbi_intel ingest --file "Capital Adequacy MD.pdf" \
        --doc-id rbi:md:12798 \
        --title "Reserve Bank of India (Commercial Banks - Prudential Norms on Capital Adequacy) Directions, 2025" \
        --date 2025-11-28 --category "Commercial Banks" \
        --url https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12798

Text extraction and header stripping are ported from `00_ingest_local.py`.
Revision handling is ported from the Node side: re-ingesting an edited copy
of the same document appends a `document_revisions` row rather than
overwriting the previous text, so the amendment history survives.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

VALID_TYPES = (
    "master_direction", "master_circular", "amendment_direction",
    "standalone_circular", "notification", "guidance_note", "other",
)


# ── Text extraction ──────────────────────────────────────────────────────────

def extract_pdf(path: pathlib.Path) -> str:
    try:
        import pdfplumber
    except ImportError as e:
        raise SystemExit(
            "pdfplumber is required for PDF ingestion.  pip install pdfplumber\n"
            "(It handles RBI's Basel-style capital tables far better than pdf-parse.)"
        ) from e
    pages: list[str] = []
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            pages.append(page.extract_text() or "")
            if (i + 1) % 20 == 0:
                print(f"[ingest]   ...{i + 1}/{total} pages", file=sys.stderr)
    return "\n".join(pages)


def extract_docx(path: pathlib.Path) -> str:
    try:
        import docx
    except ImportError as e:
        raise SystemExit("python-docx is required for .docx ingestion.  pip install python-docx") from e
    return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)


def extract_txt(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def extract(path: pathlib.Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".docx":
        return extract_docx(path)
    if suffix in (".txt", ".text", ".md"):
        return extract_txt(path)
    raise SystemExit(f"unsupported file type: {suffix} (supported: .pdf, .docx, .txt, .md)")


# ── Header / footer stripping ────────────────────────────────────────────────

def clean_text(raw: str) -> tuple[str, int]:
    """Strip running headers and footers. Returns (cleaned_text, lines_stripped).

    RBI PDFs carry the same chrome on every page — the document title, page
    numbers, 'RESERVE BANK OF INDIA'. Left in, those lines repeat hundreds of
    times and the chunker has no way to know they are not clause text.

    The detector is frequency-based: a short line that appears more than six
    times AND on more than 0.5% of all lines is chrome, because real directive
    text does not repeat verbatim. Headers that embed the page number vary per
    page and so escape this — that is a known limitation, not a bug, and it is
    harmless because the clause classifier rejects them downstream.
    """
    lines = [l.strip() for l in raw.split("\n")]
    non_empty = [l for l in lines if l and len(l) < 100]
    freq = Counter(non_empty)
    total = max(len(lines), 1)
    noisy = {l for l, c in freq.items() if c > 6 and c / total > 0.005}
    return "\n".join(l for l in lines if l not in noisy), len(noisy)


# ── Metadata inference ───────────────────────────────────────────────────────

RE_UPDATED = re.compile(
    r"\(\s*updated\s+as\s+on\s+([A-Za-z0-9 ,\-/]+?)\s*\)", re.I
)
RE_REF_NO = re.compile(
    r"\b((?:DOR|DBOD|DBR|DPSS|DCBR|DNBR|DBS|FMRD|FIDD|IDMD|CO\.DPSS|RPCD|MPD|DGBA|CEPD)"
    r"[.\w]*(?:No)?[.\w]*\.?\s*[\w./-]*\d{4}-\d{2,4})\b",
    re.I,
)
RE_RBI_NO = re.compile(r"\bRBI/(?:[A-Z]+/)?\d{4}-\d{2,4}/\d+\b")

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def parse_loose_date(s: str) -> str | None:
    """Parse RBI's several date shapes to ISO, component-wise.

    Deliberately NOT via `datetime.strptime(...).isoformat()` on a naive local
    date: the Node side had exactly that bug, where every date shifted back a
    day in IST because a local midnight was converted to UTC. Chronology is
    load-bearing here — the relation extractor rejects edges where a document
    supersedes something published after it — so an off-by-one silently
    corrupts the graph.
    """
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.match(r"^(\d{1,2})[ \-/]+([A-Za-z]+)[ \-/,]+(\d{4})$", s)
    if m:
        d, mon, y = m.group(1), m.group(2).lower()[:3], m.group(3)
        for name, num in MONTHS.items():
            if name.startswith(mon):
                return f"{int(y):04d}-{num:02d}-{int(d):02d}"
    m = re.match(r"^([A-Za-z]+)[ ]+(\d{1,2})[ ,]+(\d{4})$", s)
    if m:
        mon, d, y = m.group(1).lower()[:3], m.group(2), m.group(3)
        for name, num in MONTHS.items():
            if name.startswith(mon):
                return f"{int(y):04d}-{num:02d}-{int(d):02d}"
    return None


def sniff_metadata(text: str, title: str | None) -> dict:
    """Pull ref_no, RBI number and '(Updated as on ...)' out of the masthead.

    Only the first 3000 characters are scanned. Scanning the whole body would
    pick up every *cited* circular number and attribute it to the citing
    document — the same masthead-only discipline the relation resolver uses,
    and for the same reason.
    """
    head = text[:3000]
    ref = RE_REF_NO.search(head)
    rbi = RE_RBI_NO.search(head)
    upd = RE_UPDATED.search(title or "") or RE_UPDATED.search(head)
    return {
        "ref_no": (ref.group(1).strip() if ref else None) or (rbi.group(0) if rbi else None),
        "updated_date": parse_loose_date(upd.group(1)) if upd else None,
    }


def normalise_title(title: str) -> tuple[str, str | None]:
    """Split '<Title> (Updated as on 01-Jul-2026)' into title and update date.

    Port of `normalize_title()` (gap P1). Roughly 40% of Master Direction
    titles carry this suffix, and storing it verbatim means the same document
    looks like a different one after every RBI edit.
    """
    m = RE_UPDATED.search(title or "")
    if not m:
        return (title or "").strip(), None
    cleaned = (title[: m.start()] + title[m.end():]).strip().rstrip("-–—,").strip()
    return cleaned, parse_loose_date(m.group(1))


# ── Database write ───────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def store(
    conn: sqlite3.Connection,
    doc_id: str,
    *,
    title: str,
    body: str,
    date: str,
    doc_type: str,
    category: str | None,
    source_url: str,
    ref_no: str | None,
    updated_date: str | None,
) -> dict:
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    now = _now()
    prev = conn.execute(
        "SELECT body_hash, body, title FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()

    if prev and prev["body_hash"] == body_hash:
        conn.execute("UPDATE documents SET indexed_at = ? WHERE id = ?", (now, doc_id))
        conn.commit()
        return {"doc_id": doc_id, "action": "unchanged", "chars": len(body), "revision": None}

    if prev:
        # Never overwrite in place. The prior text is the only evidence that an
        # amendment happened at all.
        rev_no = (conn.execute(
            "SELECT COALESCE(MAX(revision_no), 0) AS n FROM document_revisions WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()["n"]) + 1
        conn.execute(
            "INSERT INTO document_revisions (doc_id, revision_no, body_hash, body, title, captured_at, char_delta) "
            "VALUES (?,?,?,?,?,?,?)",
            (doc_id, rev_no, body_hash, body, title, now, len(body) - len(prev["body"] or "")),
        )
        conn.execute(
            "UPDATE documents SET title=?, body=?, body_hash=?, date=?, category=COALESCE(?,category), "
            "ref_no=COALESCE(?,ref_no), source_url=?, last_changed=?, indexed_at=?, source='local' WHERE id=?",
            (title, body, body_hash, date, category, ref_no, source_url, now, now, doc_id),
        )
        action = "amended"
    else:
        conn.execute(
            "INSERT INTO documents (id, regulator, doc_type, title, date, department, source_url, "
            "pdf_url, body, indexed_at, body_hash, status, category, ref_no, first_seen, last_changed, source) "
            "VALUES (?,'RBI',?,?,?,?,?,?,?,?,?,'active',?,?,?,?,'local')",
            (doc_id, doc_type, title, date, category, source_url, None, body, now,
             body_hash, category, ref_no, now, now),
        )
        rev_no = 1
        conn.execute(
            "INSERT INTO document_revisions (doc_id, revision_no, body_hash, body, title, captured_at, char_delta) "
            "VALUES (?,?,?,?,?,?,?)",
            (doc_id, rev_no, body_hash, body, title, now, len(body)),
        )
        action = "new"

    if updated_date:
        # Schema v6 gave this a real column. Keep writing sync_meta too so a
        # database that has not been migrated yet still records it somewhere.
        try:
            conn.execute(
                "UPDATE documents SET updated_date = ?, has_update = 1 WHERE id = ?",
                (updated_date, doc_id),
            )
        except sqlite3.OperationalError:
            pass
        from .db import set_meta
        set_meta(conn, f"updated_date:{doc_id}", updated_date)

    conn.commit()
    return {"doc_id": doc_id, "action": action, "chars": len(body), "revision": rev_no}


def ingest_file(
    conn: sqlite3.Connection,
    path: pathlib.Path,
    *,
    doc_id: str,
    title: str | None = None,
    date: str | None = None,
    doc_type: str = "master_direction",
    category: str | None = None,
    source_url: str | None = None,
    clean: bool = True,
) -> dict:
    if not path.exists():
        raise SystemExit(f"file not found: {path}")
    if doc_type not in VALID_TYPES:
        raise SystemExit(f"--type must be one of: {', '.join(VALID_TYPES)}")

    print(f"[ingest] reading {path.name}", file=sys.stderr)
    raw = extract(path)
    print(f"[ingest] extracted {len(raw):,} raw chars", file=sys.stderr)

    if clean:
        body, stripped = clean_text(raw)
        print(f"[ingest] stripped {stripped} repeated header/footer line(s) "
              f"-> {len(body):,} chars", file=sys.stderr)
    else:
        body = raw

    raw_title = title or path.stem
    display_title, title_updated = normalise_title(raw_title)
    sniffed = sniff_metadata(body, raw_title)
    updated_date = title_updated or sniffed["updated_date"]

    iso_date = parse_loose_date(date) if date else None
    if date and not iso_date:
        raise SystemExit(f"could not parse --date {date!r} (try YYYY-MM-DD)")
    if not iso_date:
        iso_date = updated_date or datetime.now(timezone.utc).date().isoformat()
        print(f"[ingest] no --date given; using {iso_date}", file=sys.stderr)

    result = store(
        conn, doc_id,
        title=display_title,
        body=body,
        date=iso_date,
        doc_type=doc_type,
        category=category,
        source_url=source_url or f"local:{path.name}",
        ref_no=sniffed["ref_no"],
        updated_date=updated_date,
    )
    result.update({
        "title": display_title,
        "ref_no": sniffed["ref_no"],
        "updated_date": updated_date,
        "date": iso_date,
        "source": "local",
    })
    return result
