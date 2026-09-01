"""Clause-level chunking of RBI directive text.

This is a port of the chunker validated in the earlier Python pipeline
(02_chunk_clauses.py + 02b_clean_chunks.py), operating on document bodies
already in the shared database instead of on local PDFs.

Three things it has to get right, all learned the hard way on real documents:

  1. CHAPTER and ANNEX must produce distinct tags. When they shared one
     pattern, every Annex after the last chapter silently inherited that
     chapter's tag and produced duplicate labels.

  2. Table rows and worked examples ("AAA to AA All 0.00") open with a
     number and a period — structurally identical to a real clause. They
     are separated by classifying the *content*, not the shape: a real
     clause has enough words, a high enough alphabetic ratio, and at least
     one directive keyword. Fragments are merged into the preceding
     confirmed clause rather than dropped, so no text is lost.

  3. Modern RBI 2025 Master Directions (UCB series and others) use
     parenthesized clause numbering — (1), (2), (3) — instead of the bare
     "38." style used in older directions. The inline-break normaliser and
     clause-boundary patterns must handle both forms. PART I / PART II
     structural markers are also common in newer directions and must be
     tracked the same way as CHAPTER headings.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

# ── Boundary patterns, highest priority first ────────────────────────────────

RE_DEEP   = re.compile(r"^\s*(\d+(?:\.\d+){2,})\.?\s+(?=\S)")      # 2.1.1.1
RE_DOTTED = re.compile(r"^\s*(\d+\.\d+)\.?\s+(?=\S)")               # 2.1
RE_BARE   = re.compile(r"^\s*(\d{1,3}[A-Z]?)\.\s+(?=\S)")           # 38.  or  3A.

# Parenthesized numbers: (1), (2) … very common in 2025 UCB/RCB directions.
# Lower priority than bare numbers so "1. Clause (1) sub-item" doesn't
# accidentally split at the sub-item if the line doesn't start with it.
RE_PAREN  = re.compile(r"^\s*\((\d{1,3})\)\s+(?=\S)")               # (1), (2)

# Structural markers (reset chapter context; do not produce a clause chunk).
RE_CHAPTER = re.compile(r"^\s*CHAPTER\s+([IVXLC]+|\d+)\b", re.I)
RE_ANNEX   = re.compile(r"^\s*(?:ANNEX|ANNEXURE|APPENDIX)\s*[-–—]?\s*([IVXLC]+|\d+)?\b", re.I)
# PART I / PART II — used in modern directions as an alternative to CHAPTER.
RE_PART    = re.compile(r"^\s*PART\s+([IVXLC]+|\d+)\b", re.I)

# Lettered sub-section heading inside a chapter: "B Applicability",
# "C. Perpetual Non-Cumulative Preference Shares". RBI restarts its
# parenthesised numbering under each of these, so without tracking them the
# chapter is too coarse a namespace and (1), (2), (3) collide repeatedly.
# Requires a title in title/sentence case after the letter, so it does not
# fire on a stray "A" in running text.
RE_SECTION = re.compile(r"^\s*([A-Z])\.?\s+([A-Z][A-Za-z][^.]{2,90})$")

# ── Real-clause classification ────────────────────────────────────────────────
#
# Sourced from 02b_clean_chunks.py (original pipeline) plus additions found
# by inspecting real UCB / RCB 2025 direction text that produced zero clauses
# with the original keyword list.
DIRECTIVE_KEYWORDS = (
    # Modal / obligation verbs
    "shall", "should", "must", "may not", "may ",
    # Definitional
    "means", "is defined", "is defined as", "defined as",
    # Applicability
    "these directions", "this direction", "applicable to", "apply to",
    "shall apply", "applies to", "shall be applicable",
    # Repeal / commencement
    "stand repealed", "stands repealed", "shall stand", "repealed",
    "come into effect", "come into force", "in force", "with effect from",
    # Obligation / permission
    "is required", "are required", "shall be required", "required to",
    "permitted", "prohibited", "not exceed", "shall not exceed",
    "at least", "not less than", "not more than",
    # Operational
    "ensure", "maintain", "report", "disclose", "submit", "obtain",
    "comply", "furnish", "notify", "intimate",
    # Qualification
    "eligible", "provided that", "subject to", "in respect of",
    "for the purpose", "for the purposes",
    # Declarative register
    "reserve bank", "in exercise", "in terms of",
    "under section", "under sub-section",
    "explanation",
)

ROMAN = "I II III IV V VI VII VIII IX X XI XII XIII XIV XV XVI XVII XVIII XIX XX".split()

# ── Markdown stripping ────────────────────────────────────────────────────────
#
# TurndownService (HTML → MD) wraps clause numbers in bold: **1.**, *1.*
# Strip all inline Markdown so that RE_BARE / RE_PAREN still fire.
RE_MD_BOLD_ITALIC = re.compile(r"\*{1,3}((?:[^*]|\*(?!\*))+?)\*{1,3}")
RE_MD_HEADER      = re.compile(r"^#{1,6}\s+", re.M)
RE_MD_CODE_INLINE = re.compile(r"`[^`]+`")
RE_MD_LINK        = re.compile(r"\[([^\]]+)\]\([^)]+\)")
RE_MD_HORIZ_RULE  = re.compile(r"^[-*_]{3,}\s*$", re.M)


def strip_markdown(text: str) -> str:
    """Remove Markdown inline markup so clause-number regexes match cleanly."""
    text = RE_MD_HEADER.sub("", text)           # ## Heading → (blank line)
    text = RE_MD_BOLD_ITALIC.sub(r"\1", text)   # **text** / *text* → text
    text = RE_MD_CODE_INLINE.sub("", text)      # `code` → (removed)
    text = RE_MD_LINK.sub(r"\1", text)          # [label](url) → label
    text = RE_MD_HORIZ_RULE.sub("", text)       # --- / *** → (removed)
    return text


@dataclass
class Chunk:
    label: str
    chapter: str | None
    seq: int
    text: str
    needs_review: bool = False
    section: str | None = None
    real: bool = True


def _is_real_clause(text: str) -> bool:
    """Four gates, all must pass — word count, alpha ratio, directive keyword,
    and minimum prose-word count (filters table rows with short technical terms).
    """
    body = text.strip()
    words = body.split()

    # Gate 1: minimum word count.
    if len(words) < 5:
        return False

    # Gate 2: alphabetic-character ratio (filters numeric tables / worked examples).
    alpha = sum(c.isalpha() or c.isspace() for c in body)
    if not body or alpha / len(body) < 0.50:
        return False

    # Gate 3: at least one directive/regulatory keyword.
    low = body.lower()
    if not any(k in low for k in DIRECTIVE_KEYWORDS):
        return False

    # Gate 4: at least 3 "prose words" — words of 4+ chars with a lowercase
    # letter (filters rows that are all-caps acronym lists or decimal tables).
    prose = [w for w in words if len(w) >= 4 and any(c.islower() for c in w)]
    return len(prose) >= 3


def trim_front_matter(text: str) -> str:
    """Drop the table of contents and front preamble.

    ToC lines like '4. Capital charge for credit risk ... 95' fire the bare
    clause-boundary pattern and would otherwise produce spurious empty chunks.
    """
    markers = [
        re.compile(r"^\s*Introduction\s*$", re.I | re.M),
        re.compile(r"In exercise of the powers conferred", re.I),
        re.compile(r"^\s*CHAPTER\s+I\b", re.I | re.M),
        re.compile(r"^\s*PART\s+I\b", re.I | re.M),      # 2025 directions start with PART I
        re.compile(r"^\s*\(1\)\s+Short title", re.I),     # (1) Short title and commencement
    ]
    for pat in markers:
        m = pat.search(text)
        # First occurrence only — a stray later sub-heading named 'Introduction'
        # once truncated a whole document to 61 lines.
        if m and m.start() > 0:
            return text[m.start():]
    return text


# ── Inline-break normalisation ────────────────────────────────────────────────
#
# PDF and HTML extraction do not reliably preserve RBI's paragraph breaks —
# a whole chapter can arrive as one run-on line. Re-introducing the breaks
# here keeps the chunker working on either shape.

# Bare-number clauses that appear mid-line: "...text. 2. Next clause."
# Lookbehind accepts . ? ! ; : as sentence terminators (colon-list items
# often introduce the next numbered clause without a full stop).
RE_INLINE_CLAUSE = re.compile(
    r"(?<=[.?!;:])\s+(?=(\d{1,3}[A-Z]?(?:\.\d{1,3})*)\.\s+[A-Z])"
)

# Parenthesized-number clauses that appear mid-line: "...text. (2) Next."
RE_INLINE_PAREN = re.compile(
    r"(?<=[.?!;:])\s+(?=\(\d{1,3}\)\s+[A-Z])"
)

# Structural markers (CHAPTER / PART / ANNEX / APPENDIX) appearing mid-line.
RE_INLINE_STRUCT = re.compile(
    r"\s+(?=(?:CHAPTER|PART|ANNEX|ANNEXURE|APPENDIX)\s+[IVXLC0-9])",
    re.I,
)

# A structural marker followed on the same line by the first clause of that
# unit: "CHAPTER I 1. Short title." → break after the marker so the clause
# is detected as its own line.
RE_STRUCT_TAIL = re.compile(
    r"((?:CHAPTER|PART|ANNEX|ANNEXURE|APPENDIX)\s+(?:[IVXLC]+|\d+))\s+"
    r"(?=(?:\d{1,3}[A-Z]?(?:\.\d{1,3})*\.\s|\(\d{1,3}\)\s))",
    re.I,
)


def normalise_breaks(text: str) -> str:
    """Insert line breaks at clause boundaries that appear mid-line."""
    text = text or ""
    text = RE_INLINE_STRUCT.sub("\n", text)
    text = RE_STRUCT_TAIL.sub(r"\1\n", text)
    text = RE_INLINE_CLAUSE.sub("\n", text)
    text = RE_INLINE_PAREN.sub("\n", text)
    return text


def chunk_text(text: str) -> list[Chunk]:
    """Parse body text into a list of Chunks.

    Tries all clause-boundary patterns in priority order:
      RE_DEEP → RE_DOTTED → RE_BARE → RE_PAREN

    When the entire document produces zero raw chunks (uncommon but possible
    when numbering is absent), falls back to a paragraph-level split so the
    document still yields something reviewable rather than zero entries.
    """
    processed = normalise_breaks(trim_front_matter(strip_markdown(text or "")))
    lines = processed.splitlines()
    raw: list[Chunk] = []
    current_chapter: str | None = None
    current_section: str | None = None
    cur: Chunk | None = None
    seq = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if cur:
                cur.text += "\n"
            continue

        # ── Structural markers (update chapter context; no chunk produced) ──
        m_ch = RE_CHAPTER.match(stripped)
        m_pt = RE_PART.match(stripped) if not m_ch else None
        m_ax = RE_ANNEX.match(stripped) if not m_ch and not m_pt else None

        if m_ch:
            current_chapter = "CH" + (m_ch.group(1) or "").upper()
            current_section = None
            continue
        if m_pt:
            # Use "PT" prefix so PART and CHAPTER labels remain distinct.
            current_chapter = "PT" + (m_pt.group(1) or "").upper()
            current_section = None
            continue
        if m_ax:
            current_chapter = "ANX" + (m_ax.group(1) or "").upper()
            current_section = None
            continue

        m_sec = RE_SECTION.match(stripped)
        if m_sec:
            current_section = m_sec.group(1).upper()
            continue

        # ── Clause-boundary detection ─────────────────────────────────────
        #
        # The label must record which numbering FORM matched, not just the
        # digits. RBI runs two independent sequences inside a single chapter:
        # bare numbers (1., 2., 3.) for paragraphs and parenthesised numbers
        # ((1), (2), (3)) for definitions and sub-items. Reducing both to "1"
        # made them collide on the "{chapter}-{label}" dedup key, so every
        # clause of the second sequence was suffixed -dup2 and flagged
        # needs_review. On the Capital Adequacy MD that mislabelled 296 of 729
        # clauses — 41% of the document, including nearly every definition —
        # and `extract` skips flagged clauses, so they would have been dropped
        # from the requirements layer without a word.
        #
        # Keeping the parentheses in the label also matches how the clause
        # would actually be cited.
        label = None
        for pat in (RE_DEEP, RE_DOTTED, RE_BARE, RE_PAREN):
            m = pat.match(stripped)
            if m:
                label = f"({m.group(1)})" if pat is RE_PAREN else m.group(1)
                break

        if label:
            seq += 1
            cur = Chunk(label=label, chapter=current_chapter, seq=seq, text=stripped,
                        section=current_section)
            raw.append(cur)
        elif cur:
            cur.text += " " + stripped

    # ── Paragraph fallback ────────────────────────────────────────────────────
    # If no clause boundary patterns fired at all, split by double-newline so
    # the document is not silently dropped. Each paragraph gets a synthetic
    # label P1, P2 … and is flagged for review.
    if not raw:
        paras = [p.strip() for p in re.split(r"\n{2,}", processed) if p.strip()]
        for i, para in enumerate(paras, start=1):
            if len(para.split()) >= 5:
                raw.append(Chunk(
                    label=f"P{i}",
                    chapter=None,
                    seq=i,
                    text=para,
                    needs_review=True,
                ))

    return _clean(raw)


def _clean(chunks: list[Chunk]) -> list[Chunk]:
    """Merge fragments into the preceding real clause, then de-duplicate labels.

    `needs_review` marks content the downstream requirement extractor should
    not be asked to interpret. It is deliberately NOT set for a label that
    merely collides.

    That distinction was originally absent, and it mattered: RBI restarts its
    (1), (2), (3) numbering under every lettered sub-heading, so a chapter-wide
    namespace produces dozens of legitimate collisions. Flagging those marked
    36% of the Capital Adequacy MD — nearly all of its definitions and its
    entire AT1/Tier-2 instrument criteria — as suspect, and `extract` skips
    flagged clauses by default. Real, substantive text would have vanished
    from the requirements layer with no error anywhere.

    So the two questions are answered separately:
      * is the TEXT usable?   -> the four content gates, recorded as `real`
      * is the LABEL unique?  -> suffixing, which never implies bad content
    """
    merged: list[Chunk] = []
    for c in chunks:
        if _is_real_clause(c.text):
            c.real = True
            merged.append(c)
        elif merged:
            # A fragment (table row, worked example) belongs to the clause above
            # it. Merging preserves the text; dropping would lose it.
            merged[-1].text += " " + c.text
        else:
            # Nothing to merge into — keep it, but say it is not trusted.
            c.real = False
            c.needs_review = True
            merged.append(c)

    # Namespace: chapter -> lettered section -> label. The section level is
    # what stops the restarted (1)/(2)/(3) sequences from colliding.
    counts: dict[str, int] = {}
    for c in merged:
        parts = [p for p in (c.chapter, c.section) if p]
        key = "-".join(parts + [c.label]) if parts else c.label
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > 1:
            # Still colliding: an unlettered sub-heading, or genuinely repeated
            # content such as two worked-example tables in one chapter.
            c.label = f"{key}-r{counts[key]}"
            # Only suspect if the CONTENT also failed the gates.
            if not c.real:
                c.needs_review = True
        else:
            c.label = key

    for i, c in enumerate(merged, start=1):
        c.seq = i
    return merged


def store(conn: sqlite3.Connection, doc_id: str, chunks: list[Chunk]) -> int:
    conn.execute("DELETE FROM clauses WHERE doc_id = ?", (doc_id,))
    conn.executemany(
        "INSERT INTO clauses (id, doc_id, clause_label, chapter, seq, text, needs_review) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (f"{doc_id}#{c.label}", doc_id, c.label, c.chapter, c.seq, c.text.strip(), int(c.needs_review))
            for c in chunks
        ],
    )
    conn.commit()
    return len(chunks)


def chunk_document(conn: sqlite3.Connection, doc_id: str) -> dict:
    row = conn.execute("SELECT id, title, body FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        raise SystemExit(f"Unknown document id: {doc_id}")
    if not row["body"]:
        raise SystemExit(f"{doc_id} has no stored body text.")
    chunks = chunk_text(row["body"])
    n = store(conn, doc_id, chunks)
    flagged = sum(c.needs_review for c in chunks)
    return {"doc_id": doc_id, "title": row["title"], "clauses": n, "needs_review": flagged}
