/**
 * TypeScript half of the document-enrichment taxonomy.
 *
 * Reads the same `seed/taxonomy.json` as `python/rbi_intel/taxonomy.py`.
 * The data is deliberately not duplicated here — a keyword added for one
 * language must not be missing from the other, and `tests/taxonomy.test.ts`
 * plus `tests/test_taxonomy_parity.py` check that the two implementations
 * agree on a shared corpus of titles.
 *
 * Why both languages at all: Python owns analysis and runs `enrich` in bulk,
 * but the Node scraper is what first sees a title, and classifying at write
 * time means a freshly synced document is filterable immediately rather than
 * after someone remembers to run a second command.
 *
 * The three fixes carried over from the Python side are documented in
 * `taxonomy.py`; briefly: lookaround boundaries instead of `\b` (so patterns
 * ending in `)` can match), case-sensitive bare acronyms (so `LAB` stops
 * matching inside "available"), and deterministic tie-breaking.
 */
import { readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

export const GENERIC_INSTITUTION = "Cross-Institution / Generic";
export const UNCLASSIFIED_TOPIC = "Unclassified";

type InstitutionEntry = { name: string; patterns: string[]; case_sensitive?: boolean; source?: string };
type TopicEntry = { group?: string; keywords: string[]; source?: string };
type RawTaxonomy = {
  version: number;
  institution_types: InstitutionEntry[];
  topics: Record<string, TopicEntry>;
  topic_to_business_area?: Record<string, string>;
  applicability: {
    values: string[];
    default: { value: string; rule: string };
    rules: { rule: string; value: string; regex: string; case_sensitive?: boolean }[];
    excluded_institutions: string[];
    exclusion_rule_label: string;
  };
};

export type Classification = {
  institution_type: string;
  matched_pattern: string | null;
  primary_topic: string;
  secondary_topics: string[];
  topic_scores: Record<string, number>;
  topic_group: string | null;
  applicability: string;
  applicability_rule: string;
  business_area_hint: string | null;
};

const here = dirname(fileURLToPath(import.meta.url));
// Resolves from both src/util (tsx) and dist/util (built).
const SEED_DIRS = [join(here, "..", "..", "seed"), join(here, "..", "..", "..", "seed")];

/** Escape a literal for use inside a RegExp. */
function esc(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Compile a surface form with true word boundaries.
 *
 * `\b` is a word/non-word transition, so `\bScheduled Commercial Banks \(SCB\)\b`
 * requires a word character straight after the `)` — which never happens in a
 * real title, making the entry dead. Lookaround says what was actually meant:
 * not preceded or followed by a word character.
 */
function boundary(pattern: string, caseSensitive: boolean): RegExp {
  return new RegExp(`(?<!\\w)${esc(pattern)}(?!\\w)`, caseSensitive ? "" : "i");
}

type InstMatcher = { name: string; surface: string; re: RegExp };
type TopicMatcher = { topic: string; keyword: string; re: RegExp };

export class Taxonomy {
  readonly version: number;
  readonly institutionNames: string[];
  readonly topicNames: string[];
  readonly topicGroups: Record<string, string>;
  readonly topicToBusinessArea: Record<string, string>;
  readonly applicabilityValues: string[];

  private readonly institutions: InstMatcher[];
  private readonly topics: TopicMatcher[];
  private readonly rules: { rule: string; value: string; re: RegExp }[];
  private readonly excluded: Set<string>;
  private readonly exclusionLabel: string;
  private readonly fallback: { value: string; rule: string };

  constructor(raw: RawTaxonomy) {
    this.version = raw.version;

    this.institutions = [];
    for (const entry of raw.institution_types) {
      for (const surface of entry.patterns) {
        this.institutions.push({
          name: entry.name,
          surface,
          re: boundary(surface, entry.case_sensitive === true),
        });
      }
    }
    // Most specific surface form wins. The original relied on the order
    // someone happened to write the list in, so "Commercial Banks" beat
    // "Scheduled Commercial Banks" on a title containing both.
    this.institutions.sort((a, b) =>
      b.surface.length - a.surface.length || a.surface.localeCompare(b.surface)
    );
    this.institutionNames = [...new Set(raw.institution_types.map((e) => e.name))].sort();

    this.topics = [];
    this.topicGroups = {};
    for (const [name, spec] of Object.entries(raw.topics)) {
      this.topicGroups[name] = spec.group ?? "";
      for (const kw of spec.keywords) {
        this.topics.push({ topic: name, keyword: kw, re: boundary(kw, false) });
      }
    }
    this.topicNames = Object.keys(raw.topics).sort();
    this.topicToBusinessArea = raw.topic_to_business_area ?? {};

    const ap = raw.applicability;
    this.applicabilityValues = ap.values;
    this.fallback = ap.default;
    this.rules = ap.rules.map((r) => ({
      rule: r.rule,
      value: r.value,
      re: new RegExp(r.regex, r.case_sensitive ? "" : "i"),
    }));
    this.excluded = new Set(ap.excluded_institutions);
    this.exclusionLabel = ap.exclusion_rule_label;
  }

  institutionType(title?: string | null): { institution_type: string; matched_pattern: string | null } {
    if (!title) return { institution_type: GENERIC_INSTITUTION, matched_pattern: null };
    for (const m of this.institutions) {
      if (m.re.test(title)) return { institution_type: m.name, matched_pattern: m.surface };
    }
    return { institution_type: GENERIC_INSTITUTION, matched_pattern: null };
  }

  /** Every institution named in the title — a title can bind more than one. */
  allInstitutions(title?: string | null): string[] {
    if (!title) return [];
    const found: string[] = [];
    for (const m of this.institutions) {
      if (!found.includes(m.name) && m.re.test(title)) found.push(m.name);
    }
    return found;
  }

  topicsFor(title?: string | null) {
    const empty = {
      primary_topic: UNCLASSIFIED_TOPIC,
      secondary_topics: [] as string[],
      topic_scores: {} as Record<string, number>,
      topic_group: null as string | null,
    };
    if (!title) return empty;

    const scores: Record<string, number> = {};
    const longest: Record<string, number> = {};
    for (const m of this.topics) {
      if (m.re.test(title)) {
        scores[m.topic] = (scores[m.topic] ?? 0) + 1;
        longest[m.topic] = Math.max(longest[m.topic] ?? 0, m.keyword.length);
      }
    }
    const ranked = Object.keys(scores);
    if (!ranked.length) return empty;

    // Score desc, then longest matched keyword desc (more specific evidence),
    // then name. The original sorted on score alone and let equal scores fall
    // out in dictionary order — stable, but arbitrary and invisible.
    ranked.sort((a, b) => scores[b] - scores[a] || longest[b] - longest[a] || a.localeCompare(b));

    const ordered: Record<string, number> = {};
    for (const t of ranked) ordered[t] = scores[t];
    return {
      primary_topic: ranked[0],
      secondary_topics: ranked.slice(1),
      topic_scores: ordered,
      topic_group: this.topicGroups[ranked[0]] ?? null,
    };
  }

  applicability(title?: string | null): { applicability: string; applicability_rule: string } {
    if (!title) return { applicability: this.fallback.value, applicability_rule: this.fallback.rule };
    for (const r of this.rules) {
      if (r.re.test(title)) return { applicability: r.value, applicability_rule: r.rule };
    }
    for (const name of this.allInstitutions(title)) {
      if (this.excluded.has(name)) {
        return {
          applicability: "Not Applicable",
          applicability_rule: `${this.exclusionLabel} (${name})`,
        };
      }
    }
    return { applicability: this.fallback.value, applicability_rule: this.fallback.rule };
  }

  classify(title?: string | null): Classification {
    const inst = this.institutionType(title);
    const topics = this.topicsFor(title);
    const appl = this.applicability(title);
    return {
      ...inst,
      ...topics,
      ...appl,
      business_area_hint: this.topicToBusinessArea[topics.primary_topic] ?? null,
    };
  }
}

let cached: Taxonomy | null = null;

export function loadTaxonomy(): Taxonomy {
  if (cached) return cached;
  const dir = SEED_DIRS.find((d) => existsSync(join(d, "taxonomy.json")));
  if (!dir) throw new Error("seed/taxonomy.json not found next to the package root");
  cached = new Taxonomy(JSON.parse(readFileSync(join(dir, "taxonomy.json"), "utf-8")) as RawTaxonomy);
  return cached;
}
