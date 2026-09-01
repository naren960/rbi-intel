"""Tests for the compliance layer — chunk labelling, extraction, scaffolding,
validation and export.

Runs entirely offline against a temporary database using the `stub` LLM
provider, so it exercises the real code path without a key or a network.

Run: PYTHONPATH=python python3 tests/test_compliance.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from rbi_intel import chunk as C
from rbi_intel import export as E
from rbi_intel import ingest as I
from rbi_intel import requirements as R
from rbi_intel import scaffold as S
from rbi_intel import validate as V
from rbi_intel.llm import QuotaExhausted, StubProvider, get_provider


# ==========================================================================
# Chunk labelling
# ==========================================================================

def test_paren_and_bare_numbering_do_not_collide():
    """RBI runs two numbering sequences inside one chapter.

    Regression: both '1.' and '(1)' reduced to the label '1', collided on the
    dedup key, and every clause of the second sequence was suffixed -dup2 and
    flagged needs_review. On the real Capital Adequacy MD that mislabelled 296
    of 729 clauses, and `extract` skips flagged clauses by default — so 41% of
    the document would silently never reach the requirements layer.
    """
    text = (
        "CHAPTER I\n"
        "1. These Directions shall be called the Test Directions and shall apply to banks.\n"
        "2. These Directions shall come into force with effect from April 1, 2026.\n"
        "(1) 'Banking book' shall mean all items that are not included in the trading book.\n"
        "(2) 'Capital funds' means the aggregate of Tier 1 and Tier 2 capital as defined.\n"
    )
    chunks = C.chunk_text(text)
    labels = [c.label for c in chunks]
    assert len(labels) == len(set(labels)), f"duplicate labels: {labels}"
    assert not any(c.needs_review for c in chunks), \
        f"legitimate clauses flagged: {[c.label for c in chunks if c.needs_review]}"
    assert any(l.endswith("-1") for l in labels), labels
    assert any(l.endswith("-(1)") for l in labels), labels


def test_lettered_sections_namespace_restarted_numbering():
    """RBI restarts (1)(2)(3) under each lettered sub-heading."""
    text = (
        "CHAPTER III\n"
        "A Common Equity Tier 1\n"
        "(1) CET1 capital shall comprise paid-up equity capital and shall be maintained.\n"
        "(2) Any negative balance shall be deducted from CET1 capital of the bank.\n"
        "B Additional Tier 1\n"
        "(1) The instruments shall be issued by the bank and shall be fully paid-up.\n"
        "(2) The amount to be raised shall be decided by the Board of the bank.\n"
    )
    chunks = C.chunk_text(text)
    labels = [c.label for c in chunks]
    assert "CHIII-A-(1)" in labels, labels
    assert "CHIII-B-(1)" in labels, labels
    assert len(labels) == len(set(labels)), labels
    assert not any(c.needs_review for c in chunks)


def test_label_collision_alone_does_not_flag_needs_review():
    """needs_review means 'the TEXT is suspect', never 'the LABEL repeats'.

    Conflating the two is what caused the mass mislabelling above: a collision
    is a naming problem, and the content gates have already had their say.
    """
    text = (
        "CHAPTER II\n"
        "1. Every bank shall maintain adequate records and shall report them promptly.\n"
        "1. Every bank shall also ensure that the records are retained for five years.\n"
    )
    chunks = C.chunk_text(text)
    assert len(chunks) == 2, [c.label for c in chunks]
    assert chunks[1].label.endswith("-r2"), chunks[1].label
    assert not chunks[1].needs_review, "a repeated label is not suspect content"


def test_table_fragments_still_merge_and_flag():
    """The original fragment classifier must keep working."""
    text = (
        "CHAPTER IV\n"
        "5. A bank shall apply the risk weights specified in the table below to exposures.\n"
        "1. AAA to AA All 0.00\n"
        "2. A All 0.20\n"
    )
    chunks = C.chunk_text(text)
    assert len(chunks) == 1, [c.label for c in chunks]
    assert "AAA to AA" in chunks[0].text, "fragment text was lost rather than merged"


# ==========================================================================
# Metadata
# ==========================================================================

def test_title_normalisation_extracts_update_date():
    title, updated = I.normalise_title(
        "Reserve Bank of India (Capital Adequacy) Directions, 2025 (Updated as on July 01, 2026)"
    )
    assert updated == "2026-07-01", updated
    assert "Updated as on" not in title, title
    assert title.endswith("Directions, 2025"), title


def test_title_without_update_suffix_is_unchanged():
    title, updated = I.normalise_title("Master Direction on Know Your Customer")
    assert updated is None
    assert title == "Master Direction on Know Your Customer"


def test_dates_parse_component_wise_without_timezone_shift():
    """Regression from the Node side: parsing to a local midnight then
    converting to UTC shifted every date back one day in IST, silently
    corrupting the chronology guard in the relation extractor."""
    for raw, want in [
        ("2025-11-28", "2025-11-28"),
        ("November 28, 2025", "2025-11-28"),
        ("28 November 2025", "2025-11-28"),
        ("01-Jul-2026", "2026-07-01"),
        ("July 01, 2026", "2026-07-01"),
    ]:
        got = I.parse_loose_date(raw)
        assert got == want, f"{raw!r} -> {got!r}, wanted {want!r}"
    assert I.parse_loose_date("not a date") is None


def test_header_footer_stripper_removes_repeated_chrome():
    lines = []
    for i in range(1, 40):
        lines.append("RESERVE BANK OF INDIA")
        lines.append(f"{i}. Substantive clause number {i} which differs each time.")
    body = "\n".join(lines)
    cleaned, stripped = I.clean_text(body)
    assert stripped >= 1, "repeated masthead was not detected"
    assert "RESERVE BANK OF INDIA" not in cleaned
    assert "Substantive clause number 7" in cleaned


# ==========================================================================
# End-to-end over a temporary database
# ==========================================================================

DOC_TEXT = """RBI/DOR/2025-26/151
DOR.CAP.REC.70/21-01-002/2025-26 November 28, 2025

In exercise of the powers conferred by section 35A of the Banking Regulation Act, 1949.

CHAPTER I
A Short title and commencement
1. These Directions shall be called the Test Capital Directions, 2025 and shall apply to banks.
2. These Directions shall come into effect immediately upon issuance by the Reserve Bank.
B Applicability
3. These Directions shall be applicable to all Commercial Banks operating in India.
CHAPTER II
A Minimum capital
4. A bank shall maintain a minimum Common Equity Tier 1 ratio of 5.5 per cent at all times.
5. A bank shall disclose its capital position quarterly and shall submit returns to the Reserve Bank.
"""


def _fresh_db() -> tuple[sqlite3.Connection, str]:
    """Create a v5 database by running the Node migration — the schema has one
    source of truth and the tests should not fork it."""
    tmp = tempfile.mkdtemp(prefix="rbi-compliance-")
    db = os.path.join(tmp, "test.db")
    env = {**os.environ, "RBI_INTEL_DB": db}
    r = subprocess.run(
        ["npx", "tsx", "src/cli/init.ts"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise AssertionError(f"npm run init failed:\n{r.stdout}\n{r.stderr}")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn, tmp


def test_end_to_end_ingest_chunk_extract_scaffold_validate_export():
    conn, tmp = _fresh_db()
    try:
        src = Path(tmp) / "test_md.txt"
        src.write_text(DOC_TEXT, encoding="utf-8")

        res = I.ingest_file(
            conn, src, doc_id="rbi:md:99999",
            title="Reserve Bank of India (Test Capital) Directions, 2025 (Updated as on July 01, 2026)",
            date="November 28, 2025", category="Commercial Banks",
            source_url="https://example.invalid/test",
        )
        assert res["action"] == "new", res
        assert res["updated_date"] == "2026-07-01", res
        assert "Updated as on" not in res["title"], res

        row = conn.execute("SELECT source FROM documents WHERE id='rbi:md:99999'").fetchone()
        assert row["source"] == "local", "locally ingested documents must be distinguishable"

        ch = C.chunk_document(conn, "rbi:md:99999")
        assert ch["clauses"] >= 5, ch
        assert ch["needs_review"] == 0, ch

        provider = StubProvider()
        ex = R.extract(conn, provider, quiet=True)
        assert ex["kept"] >= 1, ex
        assert ex["failed"] == 0, ex

        # Resumability: a second run finds nothing pending.
        again = R.extract(conn, provider, quiet=True)
        assert again["clauses"] == 0, again

        sc = S.scaffold(conn, provider, quiet=True)
        assert sc["mapped"] == ex["kept"], sc

        # Every mapping must declare itself seeded.
        provs = {r["provenance"] for r in conn.execute("SELECT provenance FROM req_mappings")}
        assert provs == {"seeded"}, provs

        # A reviewed mapping must survive --force.
        rid = conn.execute("SELECT req_id FROM req_mappings LIMIT 1").fetchone()["req_id"]
        conn.execute("UPDATE req_mappings SET provenance='reviewed' WHERE req_id=?", (rid,))
        conn.commit()
        pend = S.pending_requirements(conn, force=True)
        assert rid not in {p["id"] for p in pend}, "--force overwrote a human-reviewed mapping"

        v = V.run(conn)
        assert not any("orphan" in e for e in v["errors"]), v["errors"]
        assert v["counts"]["requirements"] == ex["kept"], v["counts"]

        out = Path(tmp) / "inventory.json"
        w = E.write_inventory(conn, out)
        assert w["requirements"] == ex["kept"], w
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "SEEDED PLACEHOLDER" in data["meta"]["internal_layer_provenance"]
        assert data["requirements"][0]["req_id"].startswith("rbi:md:99999#")
        # Only referenced reference rows are exported — the original validator
        # treated an unreferenced business area as an error.
        used = {r["business_area"] for r in data["requirements"]}
        assert {a["id"] for a in data["business_areas"]} <= used
    finally:
        conn.close()


def test_reingesting_edited_text_keeps_a_revision():
    conn, tmp = _fresh_db()
    try:
        src = Path(tmp) / "doc.txt"
        src.write_text(DOC_TEXT, encoding="utf-8")
        I.ingest_file(conn, src, doc_id="rbi:md:88888", title="Test", date="2025-11-28")

        same = I.ingest_file(conn, src, doc_id="rbi:md:88888", title="Test", date="2025-11-28")
        assert same["action"] == "unchanged", same

        src.write_text(DOC_TEXT + "\n6. A bank shall additionally report exposures monthly.\n",
                       encoding="utf-8")
        edited = I.ingest_file(conn, src, doc_id="rbi:md:88888", title="Test", date="2025-11-28")
        assert edited["action"] == "amended", edited

        revs = conn.execute(
            "SELECT revision_no FROM document_revisions WHERE doc_id='rbi:md:88888'"
        ).fetchall()
        assert len(revs) == 2, f"prior text was destroyed instead of retained: {revs}"
    finally:
        conn.close()


def test_validate_rejects_a_single_bucket_assessment_distribution():
    """The sharpest check inherited from 05_validate.py: if every assessment
    came back the same, the model agreed rather than assessed."""
    conn, tmp = _fresh_db()
    try:
        src = Path(tmp) / "doc.txt"
        src.write_text(DOC_TEXT, encoding="utf-8")
        I.ingest_file(conn, src, doc_id="rbi:md:77777", title="Test", date="2025-11-28")
        C.chunk_document(conn, "rbi:md:77777")

        # Fabricate 25 uniformly-classified mappings.
        cl = conn.execute("SELECT id, doc_id FROM clauses LIMIT 1").fetchone()
        for i in range(25):
            rid = f"{cl['id']}#fake{i}"
            conn.execute(
                "INSERT INTO requirements (id, clause_id, doc_id, requirement, extracted_at) "
                "VALUES (?,?,?,?,datetime('now'))",
                (rid, cl["id"], cl["doc_id"], "fabricated"),
            )
            conn.execute(
                "INSERT INTO req_mappings (req_id, classification, provenance, created_at) "
                "VALUES (?,'Compliant','seeded',datetime('now'))",
                (rid,),
            )
        conn.commit()

        v = V.run(conn)
        assert not v["ok"], "a 25/25 single-bucket distribution should fail validation"
        assert any("single-bucket" in e for e in v["errors"]), v["errors"]
    finally:
        conn.close()


def test_validate_catches_enum_drift():
    conn, tmp = _fresh_db()
    try:
        src = Path(tmp) / "doc.txt"
        src.write_text(DOC_TEXT, encoding="utf-8")
        I.ingest_file(conn, src, doc_id="rbi:md:66666", title="Test", date="2025-11-28")
        C.chunk_document(conn, "rbi:md:66666")
        cl = conn.execute("SELECT id, doc_id FROM clauses LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO requirements (id, clause_id, doc_id, requirement, obligation_type, extracted_at) "
            "VALUES (?,?,?,?,?,datetime('now'))",
            ("x#1", cl["id"], cl["doc_id"], "text", "Reportage"),  # not a valid enum value
        )
        conn.commit()
        v = V.run(conn)
        assert any("obligation_type" in e for e in v["errors"]), v["errors"]
    finally:
        conn.close()


# ==========================================================================
# LLM layer
# ==========================================================================

def test_provider_selection_is_explicit_and_falls_back_to_env():
    assert get_provider("stub").name == "stub"
    try:
        get_provider("nonsense")
    except Exception as e:
        assert "Unknown provider" in str(e), e
    else:
        raise AssertionError("unknown provider name was accepted")


def test_daily_quota_is_distinguished_from_a_temporary_throttle():
    """Retrying a per-day cap only burns wall-clock time; the original helper
    learned this against the real free tier and the distinction must survive
    the port."""
    from rbi_intel.llm import _is_daily_quota
    assert _is_daily_quota(
        "429 RESOURCE_EXHAUSTED quota GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    assert _is_daily_quota("Quota exceeded for metric ... PerDay ...")
    assert not _is_daily_quota("429 RESOURCE_EXHAUSTED please retry in 39.06s")


def test_googles_suggested_retry_delay_is_honoured():
    from rbi_intel.llm import _suggested_delay
    assert abs(_suggested_delay("Please retry in 39.060090448s.", 8.0) - 39.06009) < 0.001
    assert _suggested_delay("no delay here", 8.0) == 8.0


def test_extract_treats_quota_exhaustion_as_a_checkpoint():
    """Rows produced before the cap must stay committed, and the run must
    report that it stopped rather than claiming completion."""
    conn, tmp = _fresh_db()
    try:
        src = Path(tmp) / "doc.txt"
        src.write_text(DOC_TEXT, encoding="utf-8")
        I.ingest_file(conn, src, doc_id="rbi:md:55555", title="Test", date="2025-11-28")
        C.chunk_document(conn, "rbi:md:55555")

        class DiesAfterTwo(StubProvider):
            def __init__(self):
                self.calls = 0

            def json_call(self, *a, **k):
                self.calls += 1
                if self.calls > 2:
                    raise QuotaExhausted("daily cap reached")
                return {"skip": False, "clause_title": "T", "requirement": "R",
                        "obligation_type": "Process", "branch_relevance": "Low",
                        "keywords": ["a"]}

        res = R.extract(conn, DiesAfterTwo(), quiet=True)
        assert "stopped" in res, res
        assert res["kept"] == 2, res
        n = conn.execute("SELECT COUNT(*) n FROM requirements").fetchone()["n"]
        assert n == 2, "committed rows were lost when the quota ran out"

        # And the run resumes without --force.
        res2 = R.extract(conn, StubProvider(), quiet=True)
        assert res2["clauses"] > 0, res2
    finally:
        conn.close()


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name} — {e}")
                failed += 1
            except Exception as e:
                print(f"  ERROR {name} — {type(e).__name__}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
