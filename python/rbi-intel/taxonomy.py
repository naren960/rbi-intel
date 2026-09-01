"""Load and compile seed/taxonomy.json.

The taxonomy is data, not code, and it is read by both languages from the same
file. That is the point: `src/util/taxonomy.ts` compiles the identical JSON,
and `tests/test_taxonomy_parity.py` asserts the two agree on a shared corpus of
titles. A keyword added for the dashboard cannot then go missing from the MCP
server.

Three deviations from the original `RBI_CIRCULARS_UPDATE_PAGE.py` matchers,
each fixing a defect demonstrated against real RBI title shapes:

1. **Boundaries are `(?<!\\w)` / `(?!\\w)`, not `\\b`.**
   `\\b` is a transition between a word and a non-word character, so a pattern
   ending in a bracket — `Scheduled Commercial Banks (SCB)` — needs a word
   character immediately after the `)` to match. In every real title the next
   character is a space, so that entry could never fire. The lookaround form
   asserts "not followed by a word character", which is what was meant.

2. **Bare acronyms match case-sensitively.**
   The exclusion list was applied with `re.escape(pattern)` and no boundary at
   all, under `IGNORECASE`. `LAB` therefore matched inside "avai**lab**le",
   "col**lab**orative" and "**Lab**our — so a Master Direction on
   "Available for Sale" categories was classified **Not Applicable**, silently
   removing it from the applicable set. Acronyms now require boundaries and
   exact case.

3. **Ties break deterministically.**
   Topic ranking sorted on score alone, leaving equal scores in dict insertion
   order — stable, but arbitrary and invisible. Ties now resolve by longest
   matched keyword (the more specific evidence), then alphabetically.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# seed/ sits next to python/ in the package root.
TAXONOMY_PATH = Path(__file__).resolve().parents[2] / "seed" / "taxonomy.json"

GENERIC_INSTITUTION = "Cross-Institution / Generic"
UNCLASSIFIED_TOPIC = "Unclassified"


def _boundary(pattern: str, case_sensitive: bool) -> re.Pattern[str]:
    """Compile a surface form with true word boundaries.

    Lookaround rather than `\\b` so patterns ending in `)` or `.` still anchor.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(r"(?<!\w)" + re.escape(pattern) + r"(?!\w)", flags)


@dataclass(frozen=True)
class InstitutionMatcher:
    name: str
    pattern: re.Pattern[str]
    surface: str
    source: str


@dataclass(frozen=True)
class TopicMatcher:
    topic: str
    group: str
    pattern: re.Pattern[str]
    keyword: str
    source: str


class Taxonomy:
    def __init__(self, raw: dict):
        self.raw = raw
        self.version = raw.get("version", 0)

        # Institutions: match the most specific surface form first. The
        # original relied on list order, so "Commercial Banks" — first in the
        # list — beat "Scheduled Commercial Banks" on a title containing both.
        # Sorting by pattern length makes specificity explicit and removes the
        # dependence on how someone happened to order a literal.
        self.institutions: list[InstitutionMatcher] = []
        for entry in raw["institution_types"]:
            for surface in entry["patterns"]:
                self.institutions.append(InstitutionMatcher(
                    name=entry["name"],
                    pattern=_boundary(surface, entry.get("case_sensitive", False)),
                    surface=surface,
                    source=entry.get("source", "sber"),
                ))
        self.institutions.sort(key=lambda m: (-len(m.surface), m.surface))

        self.institution_names: list[str] = sorted(
            {e["name"] for e in raw["institution_types"]}
        )

        self.topics: list[TopicMatcher] = []
        for name, spec in raw["topics"].items():
            for kw in spec["keywords"]:
                self.topics.append(TopicMatcher(
                    topic=name, group=spec.get("group", ""),
                    pattern=_boundary(kw, False), keyword=kw,
                    source=spec.get("source", "sber"),
                ))

        self.topic_names: list[str] = sorted(raw["topics"])
        self.topic_groups: dict[str, str] = {
            n: s.get("group", "") for n, s in raw["topics"].items()
        }

        ap = raw["applicability"]
        self.applicability_values: list[str] = ap["values"]
        self.applicability_default = ap["default"]
        self.applicability_rules = [
            {
                "rule": r["rule"], "value": r["value"],
                "pattern": re.compile(r["regex"], 0 if r.get("case_sensitive") else re.IGNORECASE),
            }
            for r in ap["rules"]
        ]
        self.excluded_institutions: set[str] = set(ap["excluded_institutions"])
        self.exclusion_rule_label: str = ap["exclusion_rule_label"]

        self.topic_to_business_area: dict[str, str] = raw.get("topic_to_business_area", {})

    # ── Classifiers ───────────────────────────────────────────────────────

    def institution_type(self, title: str | None) -> dict:
        """Port of `extract_institution_type`."""
        if not title:
            return {"institution_type": GENERIC_INSTITUTION, "matched_pattern": None}
        for m in self.institutions:
            if m.pattern.search(title):
                return {"institution_type": m.name, "matched_pattern": m.surface}
        return {"institution_type": GENERIC_INSTITUTION, "matched_pattern": None}

    def all_institutions(self, title: str | None) -> list[str]:
        """Every institution named in the title, not just the most specific.

        `institution_type` answers "whose rule is this", which a single value
        can express. A title reading "applicable to Commercial Banks and Urban
        Co-operative Banks" names two, and the applicability logic needs both.
        """
        if not title:
            return []
        found: list[str] = []
        for m in self.institutions:
            if m.name not in found and m.pattern.search(title):
                found.append(m.name)
        return found

    def topics_for(self, title: str | None) -> dict:
        """Port of `classify_regulatory_topics` — multi-label, scored."""
        empty = {
            "primary_topic": UNCLASSIFIED_TOPIC, "secondary_topics": [],
            "matched_topics": [], "topic_scores": {}, "topic_group": None,
        }
        if not title:
            return empty

        scores: dict[str, int] = {}
        longest: dict[str, int] = {}
        for m in self.topics:
            if m.pattern.search(title):
                scores[m.topic] = scores.get(m.topic, 0) + 1
                longest[m.topic] = max(longest.get(m.topic, 0), len(m.keyword))
        if not scores:
            return empty

        ranked = sorted(
            scores,
            key=lambda t: (-scores[t], -longest[t], t),
        )
        return {
            "primary_topic": ranked[0],
            "secondary_topics": ranked[1:],
            "matched_topics": ranked,
            "topic_scores": {t: scores[t] for t in ranked},
            "topic_group": self.topic_groups.get(ranked[0]),
        }

    def applicability(self, title: str | None) -> dict:
        """Port of `classify_applicability`. Rule order preserved exactly."""
        if not title:
            return {"applicability": self.applicability_default["value"],
                    "applicability_rule": self.applicability_default["rule"]}

        for rule in self.applicability_rules:
            if rule["pattern"].search(title):
                return {"applicability": rule["value"], "applicability_rule": rule["rule"]}

        for name in self.all_institutions(title):
            if name in self.excluded_institutions:
                return {"applicability": "Not Applicable",
                        "applicability_rule": f"{self.exclusion_rule_label} ({name})"}

        return {"applicability": self.applicability_default["value"],
                "applicability_rule": self.applicability_default["rule"]}

    def classify(self, title: str | None) -> dict:
        out: dict = {}
        out.update(self.institution_type(title))
        out.update(self.topics_for(title))
        out.update(self.applicability(title))
        out["business_area_hint"] = self.topic_to_business_area.get(out["primary_topic"])
        return out


@lru_cache(maxsize=1)
def load(path: str | None = None) -> Taxonomy:
    p = Path(path) if path else TAXONOMY_PATH
    if not p.exists():
        raise SystemExit(
            f"taxonomy not found at {p}. It ships in seed/taxonomy.json — "
            f"re-extract the package or set the path explicitly."
        )
    return Taxonomy(json.loads(p.read_text(encoding="utf-8")))
