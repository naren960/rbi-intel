"""Extract regulatory lineage edges between RBI documents.

RBI states lineage in prose, not metadata. A circular says things like

    "In supersession of our circular DBOD.No.BP.BC.1/21.06.201/2015-16
     dated July 1, 2015, it has been decided that ..."
    "The instructions contained in the circulars listed in the Annex
     stand repealed with effect from ..."
    "In partial modification of our circular DOR.AUT.REC.No.12/24.01.001/
     2025-26 dated August 1, 2025 ..."

So the extraction is two stages, deliberately kept separate:

  1. TRIGGER   — a phrase that establishes a relationship *type*
                 ("in supersession of", "stand repealed", "in modification of")
  2. REFERENCE — a document identifier near that trigger
                 (an RBI reference number, an RBI/YYYY-YY/NN number, or a
                  "circular ... dated <date>" phrase)

Stage 2 is then resolved against documents already in the index. References
that cannot be resolved are still written, with dst_id NULL — those are the
backlog of documents worth fetching, and throwing them away would hide the
gap. This is regex-first by design: RBI's phrasing is formulaic, the rules
are auditable, and every edge carries the sentence it came from as evidence.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

# --------------------------------------------------------------------------
# Relationship triggers
# --------------------------------------------------------------------------
# Ordered most-specific first. `weight` is the base confidence for an edge
# found via this trigger, before reference-resolution quality is factored in.

TRIGGERS: list[tuple[str, re.Pattern[str], float]] = [
    ("supersedes", re.compile(r"\bin\s+supersession\s+of\b", re.I), 0.95),
    ("supersedes", re.compile(r"\bsupersede[sd]?\b", re.I), 0.85),
    ("repeals",    re.compile(r"\b(?:stands?|shall\s+stand|are|is|hereby)\s+repealed\b", re.I), 0.95),
    ("repeals",    re.compile(r"\bhereby\s+repeals?\b", re.I), 0.9),
    ("repeals",    re.compile(r"\brepealed\b", re.I), 0.7),
    ("withdraws",  re.compile(r"\b(?:stands?|shall\s+stand|are|is|hereby)\s+withdrawn\b", re.I), 0.9),
    ("withdraws",  re.compile(r"\bwithdrawn\s+with\s+effect\s+from\b", re.I), 0.9),
    ("amends",     re.compile(r"\bin\s+(?:partial\s+)?modification\s+of\b", re.I), 0.9),
    ("amends",     re.compile(r"\bamendments?\s+to\b", re.I), 0.8),
    ("amends",     re.compile(r"\b(?:is|are|stands?|shall\s+be)\s+(?:hereby\s+)?(?:amended|substituted)\b", re.I), 0.8),
    ("consolidates", re.compile(r"\bconsolidat\w+\s+(?:the\s+)?(?:instructions|guidelines|circulars)\b", re.I), 0.85),
    ("consolidates", re.compile(r"\bincorporates?\s+the\s+instructions\b", re.I), 0.75),
    ("references", re.compile(r"\b(?:please\s+refer\s+to|reference\s+is\s+invited\s+to|in\s+terms\s+of|read\s+with|vide)\b", re.I), 0.45),
]

# --------------------------------------------------------------------------
# Reference forms
# --------------------------------------------------------------------------

# "RBI/2025-26/45", "RBI/DOR/2024-25/118"
RE_RBI_NO = re.compile(r"\bRBI\s*/\s*(?:[A-Z]{2,6}\s*/\s*)?(\d{4}\s*-\s*\d{2,4})\s*/\s*(\d{1,4})\b", re.I)

# Departmental reference numbers. The shape is
#   <dotted alpha/numeric prefix> / <dotted numeric file code> / <year range>
# and crucially "No." can appear anywhere inside the prefix, not just at its
# end — all of these are real and all must match one pattern:
#   DBOD.No.BP.BC.9/21.04.048/2014-15
#   DOR.AUT.REC.No.12/24.01.001/2025-26
#   DOR.AML.REC.44/14.01.001/2026-27
#   DPSS.CO.PD.No.1810/02.14.008/2019-20
RE_DEPT_REF = re.compile(
    r"\b([A-Z]{2,8}(?:\.[A-Za-z0-9()]{1,14}){1,8}"   # DBOD.No.BP.BC.9
    r"\s*/\s*\d{1,4}(?:\.\d{1,4}){0,4}"              # /21.04.048
    r"\s*/\s*\d{4}\s*-\s*\d{2,4})"                   # /2014-15
)
# Reference given without the trailing year range. The lookahead must reject
# a following digit as well as a slash: without it this pattern happily
# matched "DBOD.AML.BC.No.1" out of "DBOD.AML.BC.No.15/14.01.001/2013-14"
# and emitted a phantom unresolved reference alongside the real one.
RE_DEPT_REF_LOOSE = re.compile(
    r"\b([A-Z]{2,8}(?:\.[A-Za-z0-9()]{1,14}){1,8}\.No\.?\s*\d{1,5})(?![\d/]|\s*/)"
)

MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
# "circular DBOD.No.BP.BC.9/21.04.048/2014-15 dated July 15, 2014"
# The middle span must allow periods — every RBI reference number is full of
# them, and excluding '.' here made this pattern fail on exactly the citations
# it was written to catch.
RE_DATED_DOC = re.compile(
    r"\b(circulars?|notifications?|master\s+directions?|master\s+circulars?|directions?)\b"
    r"(?P<mid>[^;:\n]{0,200}?)"
    r"\bdated\s+(?P<date>" + MONTH + r"\s+\d{1,2},?\s+\d{4})",
    re.I,
)

RE_MD_TITLE = re.compile(
    r"\bMaster\s+(?:Direction|Circular)\s*[–—\-:]\s*(?P<title>[^\n.;]{8,140})", re.I
)

MONTH_NUM = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

HEADER_WINDOW = 400   # chars of masthead scanned for a document's own identifiers

STOPWORDS = {
    "the", "of", "and", "on", "for", "to", "in", "a", "an", "by", "with", "rbi",
    "reserve", "bank", "india", "circular", "master", "direction", "directions",
    "notification", "no", "dated", "our", "this", "these", "shall", "as",
}


@dataclass
class Reference:
    raw: str
    kind: str                      # rbi_no | dept_ref | dated | md_title
    key: str | None = None         # normalised lookup key
    date: str | None = None        # ISO, when the reference carried one
    title_hint: str | None = None


@dataclass
class Edge:
    src_id: str
    rel_type: str
    dst_id: str | None
    dst_ref_text: str
    evidence: str
    confidence: float
    method: str = "regex"


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

def norm_ref(s: str) -> str:
    """Collapse an RBI reference number to a comparable key."""
    return re.sub(r"[\s]", "", s or "").upper().rstrip(".")


def norm_rbi_no(year: str, num: str) -> str:
    clean_year = re.sub(r"\s", "", year)
    return f"RBI/{clean_year}/{int(num)}"


def to_iso(datestr: str) -> str | None:
    m = re.match(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", (datestr or "").strip())
    if not m:
        return None
    mon = MONTH_NUM.get(m.group(1)[:3].lower())
    if not mon:
        return None
    return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"


def title_tokens(s: str) -> set[str]:
    words = re.findall(r"[a-z]{3,}", (s or "").lower())
    return {w for w in words if w not in STOPWORDS}


# --------------------------------------------------------------------------
# Reference index — resolves a Reference to a document id
# --------------------------------------------------------------------------

class ReferenceIndex:
    def __init__(self, rows: Iterable[sqlite3.Row]):
        self.by_ref: dict[str, str] = {}
        self.by_rbi_no: dict[str, str] = {}
        self.by_date: dict[str, list[tuple[str, set[str]]]] = {}
        self.by_title: list[tuple[str, set[str], str]] = []
        self.date_of: dict[str, str] = {}

        for r in rows:
            doc_id = r["id"]
            title = r["title"] or ""

            # Only harvest aliases from the document's own masthead.
            #
            # An RBI document opens with its own identifiers:
            #   "RBI/2014-15/103 DBOD.No.BP.BC.9/21.04.048/2014-15 dated July 15, 2014"
            # and then goes on to *cite* other documents' reference numbers in
            # the body. Scanning the whole body for aliases therefore registers
            # every cited circular as an alias for the citing one, and the
            # resolver then confidently points edges at the wrong document.
            # Restricting to the masthead, and taking only the first match of
            # each kind, keeps an alias meaning "this document is X".
            head = ((r["body"] or "")[:HEADER_WINDOW]) if "body" in r.keys() else ""

            if r["ref_no"]:
                self.by_ref.setdefault(norm_ref(r["ref_no"]), doc_id)
            m = RE_DEPT_REF.search(head)
            if m:
                self.by_ref.setdefault(norm_ref(m.group(1)), doc_id)
            m = RE_RBI_NO.search(head)
            if m:
                self.by_rbi_no.setdefault(norm_rbi_no(m.group(1), m.group(2)), doc_id)

            self.date_of[doc_id] = r["date"] or ""
            toks = title_tokens(title)
            if r["date"]:
                self.by_date.setdefault(r["date"], []).append((doc_id, toks))
            self.by_title.append((doc_id, toks, r["doc_type"]))

    def resolve(self, ref: Reference, src_date: str = "") -> tuple[str | None, float]:
        """Return (doc_id, resolution_quality 0..1).

        `src_date` enables a chronology guard: a document cannot supersede,
        repeal or amend something published after it. Any candidate dated
        later than the referring document is rejected outright.
        """
        def ok(doc_id: str | None) -> bool:
            if not doc_id:
                return False
            if not src_date:
                return True
            other = self.date_of.get(doc_id, "")
            return not (other and other > src_date)

        if ref.kind == "dept_ref" and ref.key:
            hit = self.by_ref.get(ref.key)
            if ok(hit):
                return hit, 1.0
        if ref.kind == "rbi_no" and ref.key:
            hit = self.by_rbi_no.get(ref.key)
            if ok(hit):
                return hit, 0.95

        # Dated reference: same publication date, best title-token overlap.
        if ref.date and ref.date in self.by_date:
            want = title_tokens(ref.title_hint or "")
            best, best_score = None, 0.0
            for doc_id, toks in self.by_date[ref.date]:
                if not toks:
                    continue
                inter = len(want & toks)
                if not inter:
                    continue
                score = inter / max(len(want | toks), 1)
                if score > best_score:
                    best, best_score = doc_id, score
            if ok(best) and best_score >= 0.20:
                return best, 0.55 + min(best_score, 0.4)
            # Date matched but nothing else — only one candidate is still useful.
            if len(self.by_date[ref.date]) == 1 and not want and ok(self.by_date[ref.date][0][0]):
                return self.by_date[ref.date][0][0], 0.5

        # Master Direction referred to by name.
        if ref.kind == "md_title" and ref.title_hint:
            want = title_tokens(ref.title_hint)
            best, best_score = None, 0.0
            for doc_id, toks, dtype in self.by_title:
                if dtype not in ("master_direction", "master_circular"):
                    continue
                if not toks or not want:
                    continue
                score = len(want & toks) / max(len(want | toks), 1)
                if score > best_score:
                    best, best_score = doc_id, score
            if ok(best) and best_score >= 0.45:
                return best, 0.5 + min(best_score, 0.45)

        return None, 0.0


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def split_sentences(text: str) -> list[str]:
    """Cheap sentence split that does not break on RBI reference numbers.

    A naive split on '.' shreds 'DOR.AUT.REC.No.12/24.01.001/2025-26'. We only
    break on a period that is followed by whitespace and an uppercase letter or
    digit-free word start, and never when the period sits inside a token that
    looks like a reference.
    """
    text = re.sub(r"\s+", " ", text or "")
    parts = re.split(r"(?<=[.;:])\s+(?=[A-Z(])", text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # Re-join fragments that end mid-reference.
        if out and re.search(r"[A-Z]{2,8}(\.[A-Z0-9]{1,10})*\.$", out[-1]):
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return out


def find_references(text: str) -> list[Reference]:
    refs: list[Reference] = []
    seen: set[str] = set()

    for m in RE_DEPT_REF.finditer(text):
        raw = m.group(1)
        k = norm_ref(raw)
        if k not in seen:
            seen.add(k)
            refs.append(Reference(raw=raw, kind="dept_ref", key=k))
    for m in RE_DEPT_REF_LOOSE.finditer(text):
        raw = m.group(1)
        k = norm_ref(raw)
        if k not in seen:
            seen.add(k)
            refs.append(Reference(raw=raw, kind="dept_ref", key=k))
    for m in RE_RBI_NO.finditer(text):
        k = norm_rbi_no(m.group(1), m.group(2))
        if k not in seen:
            seen.add(k)
            refs.append(Reference(raw=m.group(0), kind="rbi_no", key=k))
    for m in RE_DATED_DOC.finditer(text):
        iso = to_iso(m.group("date"))
        raw = m.group(0)
        if raw not in seen:
            seen.add(raw)
            refs.append(Reference(raw=raw, kind="dated", date=iso, title_hint=m.group("mid")))
    for m in RE_MD_TITLE.finditer(text):
        raw = m.group(0)
        if raw not in seen:
            seen.add(raw)
            refs.append(Reference(raw=raw, kind="md_title", title_hint=m.group("title")))

    return refs


def extract_edges(
    doc_id: str, body: str, index: ReferenceIndex, self_title: str = "", self_date: str = ""
) -> list[Edge]:
    """Find lineage edges stated in one document's text."""
    edges: list[Edge] = []
    sentences = split_sentences(body or "")
    self_toks = title_tokens(self_title)

    for i, sent in enumerate(sentences):
        fired: list[tuple[str, float]] = []
        for rel_type, pat, weight in TRIGGERS:
            if pat.search(sent):
                fired.append((rel_type, weight))
        if not fired:
            continue

        # Strongest trigger wins; "references" only if nothing stronger fired.
        fired.sort(key=lambda x: -x[1])
        strong = [f for f in fired if f[0] != "references"]
        rel_type, weight = (strong[0] if strong else fired[0])

        # Look in this sentence, and the next one when the trigger introduces
        # a list ("the following circulars stand repealed:").
        window = sent
        if sent.rstrip().endswith(":") and i + 1 < len(sentences):
            window = sent + " " + sentences[i + 1]

        for ref in find_references(window):
            dst_id, quality = index.resolve(ref, self_date)
            if dst_id == doc_id:
                continue  # a document referring to itself is noise
            # A "reference" edge to something with no title overlap is very
            # weak signal; keep it but score it low.
            conf = round(min(0.99, weight * (0.55 + 0.45 * quality)), 3)
            edges.append(
                Edge(
                    src_id=doc_id,
                    rel_type=rel_type,
                    dst_id=dst_id,
                    dst_ref_text=ref.raw.strip()[:300],
                    evidence=sent[:600],
                    confidence=conf,
                )
            )

    return dedupe(edges)


def dedupe(edges: list[Edge]) -> list[Edge]:
    """Collapse edges that describe the same relationship.

    One sentence usually yields the same target twice: once as a bare
    reference number and once as the longer "circular <number> dated <date>"
    phrase that contains it. Keeping both inflates every count and shows the
    user a duplicate. So after keying, we drop any unresolved edge whose
    reference text merely wraps a reference that already resolved for the
    same source and relation type.
    """
    best: dict[tuple, Edge] = {}
    for e in edges:
        k = (e.src_id, e.rel_type, e.dst_id or "", "" if e.dst_id else e.dst_ref_text)
        cur = best.get(k)
        if cur is None or e.confidence > cur.confidence:
            best[k] = e

    kept = list(best.values())
    resolved_refs: dict[tuple[str, str], list[str]] = {}
    for e in kept:
        if e.dst_id:
            resolved_refs.setdefault((e.src_id, e.rel_type), []).append(norm_ref(e.dst_ref_text))

    # Group unresolved edges so we can collapse ones that wrap each other,
    # e.g. "DBOD.No.BP.BC.31/21.04.048/2012-13" and the longer
    # "circular DBOD.No.BP.BC.31/21.04.048/2012-13 dated August 2, 2012".
    unresolved_by_key: dict[tuple[str, str], list[str]] = {}
    for e in kept:
        if not e.dst_id:
            unresolved_by_key.setdefault((e.src_id, e.rel_type), []).append(norm_ref(e.dst_ref_text))

    out: list[Edge] = []
    for e in kept:
        if not e.dst_id:
            norm = norm_ref(e.dst_ref_text)
            # already covered by a resolved edge from the same source
            if any(r and r in norm for r in resolved_refs.get((e.src_id, e.rel_type), [])):
                continue
            # a shorter sibling reference says the same thing more precisely
            siblings = unresolved_by_key.get((e.src_id, e.rel_type), [])
            if any(other and other != norm and other in norm for other in siblings):
                continue
        out.append(e)
    return out


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def build(conn: sqlite3.Connection, min_confidence: float = 0.3, verbose: bool = True) -> dict:
    rows = conn.execute(
        "SELECT id, doc_type, title, date, ref_no, body FROM documents ORDER BY date"
    ).fetchall()
    if not rows:
        raise SystemExit("No documents in the index. Run `npm run sync` first.")

    index = ReferenceIndex(rows)
    all_edges: list[Edge] = []
    for r in rows:
        if not r["body"]:
            continue
        all_edges.extend(extract_edges(r["id"], r["body"], index, r["title"] or "", r["date"] or ""))

    kept = [e for e in all_edges if e.confidence >= min_confidence]

    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM relations WHERE method = 'regex'")
    conn.executemany(
        "INSERT OR REPLACE INTO relations "
        "(src_id, dst_id, dst_ref_text, rel_type, evidence, confidence, method, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(e.src_id, e.dst_id, e.dst_ref_text, e.rel_type, e.evidence, e.confidence, e.method, now) for e in kept],
    )

    # Mark documents that a later document supersedes, repeals or withdraws.
    conn.execute("UPDATE documents SET status = 'active' WHERE status <> 'active'")
    conn.execute(
        """
        UPDATE documents SET status = (
          SELECT CASE r.rel_type
                   WHEN 'repeals'    THEN 'repealed'
                   WHEN 'withdraws'  THEN 'withdrawn'
                   ELSE 'superseded'
                 END
          FROM relations r
          WHERE r.dst_id = documents.id
            AND r.rel_type IN ('supersedes','repeals','withdraws')
            AND r.confidence >= 0.7
          ORDER BY r.confidence DESC LIMIT 1
        )
        WHERE EXISTS (
          SELECT 1 FROM relations r
          WHERE r.dst_id = documents.id
            AND r.rel_type IN ('supersedes','repeals','withdraws')
            AND r.confidence >= 0.7
        )
        """
    )

    from .db import set_meta
    set_meta(conn, "relations_built_at", now)
    conn.commit()

    by_type: dict[str, dict[str, int]] = {}
    for e in kept:
        d = by_type.setdefault(e.rel_type, {"resolved": 0, "unresolved": 0})
        d["resolved" if e.dst_id else "unresolved"] += 1

    stats = {
        "documents_scanned": len(rows),
        "edges_found": len(all_edges),
        "edges_kept": len(kept),
        "by_type": by_type,
        "unresolved_examples": [e.dst_ref_text for e in kept if not e.dst_id][:10],
    }
    if verbose:
        print(json.dumps(stats, indent=2))
    return stats
