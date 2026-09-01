"""Tests for the document-enrichment taxonomy (gaps D.4-D.6 / P2-P3).

Three of these are regressions for defects in the original
`RBI_CIRCULARS_UPDATE_PAGE.py` classifiers. They are written against the
behaviour that was wrong, so a future "simplification" back to `\\b` and
IGNORECASE substring matching fails loudly rather than silently dropping
documents out of the applicable set again.

Run: PYTHONPATH=python python3 tests/test_taxonomy.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from rbi_intel.taxonomy import GENERIC_INSTITUTION, UNCLASSIFIED_TOPIC, load

TAX = load()
TITLES = json.loads((ROOT / "tests" / "fixtures_titles.json").read_text(encoding="utf-8"))


# ==========================================================================
# Regressions
# ==========================================================================

def test_bare_acronyms_do_not_match_inside_ordinary_words():
    """REGRESSION.

    The exclusion list was applied as `re.search(re.escape(p), title, IGNORECASE)`
    with no word boundary at all. `LAB` (Local Area Banks) therefore matched
    inside "avai**lab**le", "col**lab**orative" and "**Lab**our", and every
    such document was classified Not Applicable and dropped from the
    applicable set with no error anywhere.

    Three real title shapes that were silently excluded:
    """
    for title in [
        "Prudential Norms on Available for Sale and Held for Trading Categories",
        "Guidelines on Collaborative Lending Arrangements between Banks",
        "Master Circular on Labour Welfare and staff matters in banks",
    ]:
        got = TAX.applicability(title)
        assert got["applicability"] != "Not Applicable", \
            "%r wrongly excluded via %s" % (title, got["applicability_rule"])


def test_acronym_still_matches_when_it_is_actually_an_acronym():
    """The fix must not overshoot - a real LAB/UCB/RRB reference still excludes."""
    for title, expect in [
        ("Master Direction applicable to LAB and other entities", "Local Area Banks"),
        ("Prudential norms for UCBs", "Urban Co-operative Banks"),
        ("Capital adequacy for RRBs", "Regional Rural Banks"),
    ]:
        got = TAX.applicability(title)
        assert got["applicability"] == "Not Applicable", "%s -> %s" % (title, got)
        assert expect in got["applicability_rule"], got["applicability_rule"]


def test_lowercase_acronym_lookalikes_do_not_match():
    """Acronyms are case-sensitive, so 'lab' in prose is not Local Area Banks."""
    got = TAX.applicability("Guidelines on the lab testing of currency note substrates")
    assert got["applicability"] != "Not Applicable", got


def test_pattern_ending_in_a_bracket_can_match():
    """REGRESSION.

    `\\b` is a word/non-word transition, so `\\bScheduled Commercial Banks \\(SCB\\)\\b`
    needed a word character straight after the `)`. In every real title the
    next character is a space, so that entry could never fire.
    """
    title = "Master Direction for Scheduled Commercial Banks (SCB) on Leverage Ratio"
    got = TAX.institution_type(title)
    assert got["institution_type"] == "Scheduled Commercial Banks", got
    assert got["matched_pattern"] == "Scheduled Commercial Banks (SCB)", got


def test_most_specific_institution_wins_not_list_order():
    """REGRESSION.

    Matching walked the list in the order it happened to be written and
    returned the first hit, so 'Commercial Banks' beat 'Scheduled Commercial
    Banks' on a title containing both. Specificity is now explicit.
    """
    got = TAX.institution_type("Circular to All Scheduled Commercial Banks and other banks")
    assert got["institution_type"] == "Scheduled Commercial Banks", got


def test_topic_ties_break_deterministically():
    """Ranking sorted on score alone, leaving equal scores in dict insertion
    order - stable, but arbitrary and invisible. Repeated runs must agree."""
    title = "Master Direction on Operational Risk and Cyber Security"
    first = TAX.topics_for(title)
    for _ in range(5):
        assert TAX.topics_for(title) == first


# ==========================================================================
# Behaviour carried over from the original, which must not change
# ==========================================================================

def test_applicability_rule_order_is_preserved():
    """A Fraud Risk MD naming Commercial Banks, RRBs and AIFIs is Applicable:
    Rule 2 fires before the Rule 4 exclusions. That ordering is the original's
    and it is correct - the document does bind commercial banks."""
    title = ("Master Directions on Fraud Risk Management in Commercial Banks "
             "(including Regional Rural Banks) and All India Financial Institutions")
    got = TAX.applicability(title)
    assert got["applicability"] == "Applicable", got
    assert "Rule 2" in got["applicability_rule"], got


def test_guidance_notes_are_applicable():
    for t in ["Guidance Note on Market Risk Management",
              "Guidance Notes on Operational Resilience"]:
        got = TAX.applicability(t)
        assert got["applicability"] == "Applicable", got
        assert "Rule 1" in got["applicability_rule"], got


def test_institution_specific_directions_are_excluded():
    cases = [
        ("Master Direction - Reserve Bank of India (Small Finance Banks - Prudential Norms) Directions, 2024",
         "Small Finance Banks"),
        ("Master Direction - Housing Finance Company (Reserve Bank) Directions, 2021",
         "Housing Finance Companies"),
        ("Master Direction - Asset Reconstruction Companies (Reserve Bank) Directions, 2024",
         "Asset Reconstruction Companies"),
    ]
    for title, inst in cases:
        got = TAX.applicability(title)
        assert got["applicability"] == "Not Applicable", "%s -> %s" % (title, got)
        assert inst in got["applicability_rule"], got


def test_unknown_title_falls_through_to_the_default():
    got = TAX.applicability("Some entirely unremarkable circular about nothing")
    assert got["applicability"] == "Likely Applicable", got
    assert got["applicability_rule"] == "Default", got


def test_none_and_empty_titles_are_handled():
    for bad in (None, ""):
        assert TAX.institution_type(bad)["institution_type"] == GENERIC_INSTITUTION
        assert TAX.topics_for(bad)["primary_topic"] == UNCLASSIFIED_TOPIC
        assert TAX.applicability(bad)["applicability"] == "Likely Applicable"


def test_topic_classification_is_multi_label_and_scored():
    got = TAX.topics_for("Basel III Capital Regulations - Countercyclical Capital Buffer")
    assert got["primary_topic"] == "Capital Adequacy", got
    assert got["topic_scores"]["Capital Adequacy"] >= 3, got["topic_scores"]


def test_sber_topics_are_all_present():
    """The 60 topics from the Confluence dictionary must survive the merge."""
    for t in ["Capital Adequacy", "Asset Liability Management (ALM)", "Credit Risk Management",
              "Concentration Risk Management", "Investment Portfolio", "Securitisation",
              "KYC / AML", "Governance", "Wilful Defaulters", "Foreign Exchange (FEMA)",
              "Payment Systems", "Priority Sector Lending", "Cyber Resilience",
              "Climate Risk", "Cash Reserve Ratio (CRR)", "Miscellaneous"]:
        assert t in TAX.topic_names, "topic lost in the merge: %s" % t


def test_every_topic_has_keywords_and_a_group():
    for name, spec in TAX.raw["topics"].items():
        assert spec["keywords"], "%s has no keywords" % name
        assert spec.get("group"), "%s has no group" % name


def test_business_area_hints_resolve_to_real_areas():
    areas = set(a["id"] for a in json.loads(
        (ROOT / "seed" / "business_areas.json").read_text(encoding="utf-8")))
    for topic, area in TAX.topic_to_business_area.items():
        assert topic in TAX.topic_names, "hint for unknown topic %s" % topic
        assert area in areas, "hint for %s points at unknown area %s" % (topic, area)


# ==========================================================================
# Corpus coverage
# ==========================================================================

def test_corpus_coverage_stays_high():
    """A coverage floor, not a target. If RBI's vocabulary drifts this fails,
    and the fix is to add keywords - which is what the coverage report in
    `enrich` exists to tell you."""
    unclassified = sum(
        1 for t in TITLES if TAX.topics_for(t)["primary_topic"] == UNCLASSIFIED_TOPIC
    )
    pct = 100.0 * unclassified / len(TITLES)
    assert pct <= 10, "%d/%d (%.0f%%) titles unclassified" % (unclassified, len(TITLES), pct)


def test_capital_adequacy_md_classifies_end_to_end():
    t = ("Reserve Bank of India (Commercial Banks - Prudential Norms on Capital Adequacy) "
         "Directions, 2025 (Updated as on July 01, 2026)")
    c = TAX.classify(t)
    assert c["applicability"] == "Applicable", c
    assert c["institution_type"] == "Commercial Banks", c
    assert c["primary_topic"] == "Capital Adequacy", c
    assert c["business_area_hint"] == "BA-24", c


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("  PASS  %s" % name)
                passed += 1
            except AssertionError as e:
                print("  FAIL  %s - %s" % (name, e))
                failed += 1
            except Exception as e:
                print("  ERROR %s - %s: %s" % (name, type(e).__name__, e))
                failed += 1
    print("\n%d passed, %d failed" % (passed, failed))
    raise SystemExit(1 if failed else 0)
