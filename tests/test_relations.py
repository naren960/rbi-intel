"""Unit tests for the relation extractor.

Run: PYTHONPATH=python python3 -m pytest tests/test_relations.py -q
     (or: PYTHONPATH=python python3 tests/test_relations.py)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from rbi_intel import relations as R
from rbi_intel import chunk as C


# --------------------------------------------------------------------------
# Reference recognition
# --------------------------------------------------------------------------

REAL_REFS = [
    "DBOD.No.BP.BC.9/21.04.048/2014-15",
    "DOR.AUT.REC.No.12/24.01.001/2025-26",
    "DOR.AML.REC.44/14.01.001/2026-27",
    "DPSS.CO.PD.No.1810/02.14.008/2019-20",
    "DBOD.AML.BC.No.15/14.01.001/2013-14",
    "FMRD.DIRD.10/14.03.002/2019-20",
    "DNBR.PD.007/03.10.119/2016-17",
]


def test_department_reference_formats():
    for ref in REAL_REFS:
        m = R.RE_DEPT_REF.search(ref)
        assert m and m.group(1) == ref, f"failed to match {ref}"


def test_loose_pattern_does_not_truncate_a_full_reference():
    # Regression: "DBOD.AML.BC.No.1" was being sliced out of the full ref.
    refs = R.find_references("supersedes DBOD.AML.BC.No.15/14.01.001/2013-14 issued earlier")
    keys = {r.key for r in refs if r.kind == "dept_ref"}
    assert keys == {"DBOD.AML.BC.NO.15/14.01.001/2013-14"}, keys


def test_rbi_circular_number():
    m = R.RE_RBI_NO.search("RBI/2025-26/74 DOR.CRE.REC")
    assert m and R.norm_rbi_no(m.group(1), m.group(2)) == "RBI/2025-26/74"


def test_dated_citation_tolerates_periods_in_the_reference():
    # Regression: excluding '.' from the middle span made this pattern fail
    # on every citation containing a reference number.
    s = "circular DBOD.No.BP.BC.9/21.04.048/2014-15 dated July 15, 2014"
    kinds = {r.kind for r in R.find_references(s)}
    assert "dated" in kinds and "dept_ref" in kinds


def test_date_conversion():
    assert R.to_iso("July 1, 2015") == "2015-07-01"
    assert R.to_iso("Aug 14, 2026") == "2026-08-14"
    assert R.to_iso("nonsense") is None


# --------------------------------------------------------------------------
# Trigger detection
# --------------------------------------------------------------------------

def _types(sentence):
    return {t for t, p, _ in R.TRIGGERS if p.search(sentence)}


def test_all_four_lineage_verbs_fire_on_real_phrasing():
    assert "supersedes" in _types("In supersession of the Master Circular dated July 1, 2013,")
    assert "repeals" in _types("The instructions contained in the following circulars stand repealed")
    assert "withdraws" in _types("the said circular stands withdrawn with immediate effect")
    assert "amends" in _types("In partial modification of our circular dated August 1, 2025")
    assert "consolidates" in _types("This Master Direction consolidates the instructions contained in")


def test_sentence_split_does_not_shred_reference_numbers():
    text = ("These Directions apply to all banks. The circular "
            "DBOD.No.BP.BC.9/21.04.048/2014-15 dated July 15, 2014 stands repealed.")
    sents = R.split_sentences(text)
    assert any("DBOD.No.BP.BC.9/21.04.048/2014-15" in s for s in sents), sents


# --------------------------------------------------------------------------
# Resolution safety
# --------------------------------------------------------------------------

class FakeRow(dict):
    def keys(self):
        return list(super().keys())

    def __getitem__(self, k):
        return super().__getitem__(k)


def _rows():
    return [
        FakeRow(id="rbi:nt:1", doc_type="circular", title="Old Exposure Norms",
                date="2014-07-15", ref_no="DBOD.No.BP.BC.9/21.04.048/2014-15",
                body="RBI/2014-15/103 DBOD.No.BP.BC.9/21.04.048/2014-15 dated July 15, 2014. "
                     "Please refer to our circular DBOD.No.BP.BC.31/21.04.048/2012-13 dated August 2, 2012."),
        FakeRow(id="rbi:md:2", doc_type="master_direction", title="Credit Risk Directions 2025",
                date="2025-11-28", ref_no="DOR.CRE.REC.No.62/21.04.048/2025-26",
                body="RBI/2025-26/74 DOR.CRE.REC.No.62/21.04.048/2025-26 dated November 28, 2025."),
    ]


def test_index_does_not_alias_a_document_by_references_it_merely_cites():
    """Regression: scanning the whole body registered every cited circular as
    an alias for the citing document, so edges resolved to the wrong target."""
    idx = R.ReferenceIndex(_rows())
    cited = R.norm_ref("DBOD.No.BP.BC.31/21.04.048/2012-13")
    assert cited not in idx.by_ref, "a cited reference must not become an alias"
    own = R.norm_ref("DBOD.No.BP.BC.9/21.04.048/2014-15")
    assert idx.by_ref[own] == "rbi:nt:1"


def test_chronology_guard_rejects_a_later_document():
    idx = R.ReferenceIndex(_rows())
    ref = R.Reference(raw="x", kind="dept_ref", key=R.norm_ref("DOR.CRE.REC.No.62/21.04.048/2025-26"))
    # A 2014 document cannot supersede a 2025 one.
    assert idx.resolve(ref, src_date="2014-07-15")[0] is None
    assert idx.resolve(ref, src_date="2026-01-01")[0] == "rbi:md:2"


def test_unresolved_duplicates_collapse():
    edges = [
        R.Edge("a", "withdraws", None, "DBOD.No.BP.BC.31/21.04.048/2012-13", "e", 0.5),
        R.Edge("a", "withdraws", None, "circular DBOD.No.BP.BC.31/21.04.048/2012-13 dated August 2, 2012", "e", 0.5),
    ]
    assert len(R.dedupe(edges)) == 1


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------

def test_chapter_and_annex_produce_distinct_tags():
    text = ("CHAPTER I\n1. Short title. These Directions shall be called the Test Directions.\n"
            "ANNEX I\n1. Illustrative computation. Regulated Entities shall apply the method shown here.")
    chunks = C.chunk_text(text)
    labels = [c.label for c in chunks]
    assert labels == ["CHI-1", "ANXI-1"], labels


def test_table_fragments_are_merged_not_turned_into_clauses():
    text = ("CHAPTER II\n"
            "4. Exposure limits. Banks shall ensure that exposure does not exceed twenty per cent of capital.\n"
            "1. AAA to AA All 0.00\n"
            "2. Exposure MV 1050 Haircut 0\n")
    chunks = C.chunk_text(text)
    assert len(chunks) == 1, [c.label for c in chunks]
    assert "AAA to AA" in chunks[0].text, "fragment text must be preserved, not dropped"


def test_run_on_text_is_still_chunked():
    text = ("CHAPTER I 1. Short title. These Directions shall be called the Test Directions, 2025. "
            "2. Applicability. These Directions shall apply to all Commercial Banks in India.")
    chunks = C.chunk_text(text)
    assert len(chunks) == 2, [c.label for c in chunks]


def test_front_matter_trim_uses_the_first_marker():
    # Regression: using the last occurrence of 'Introduction' truncated a
    # whole document down to its final section.
    text = "Table of Contents\n1. Something 5\nIntroduction\nThese Directions apply.\nIntroduction\nlater stray heading"
    out = C.trim_front_matter(text)
    assert out.startswith("Introduction\nThese Directions apply.")


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
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
