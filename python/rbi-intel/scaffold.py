"""Seeded internal-compliance scaffolding for extracted requirements.

Port of `04_scaffold_internal_layer.py`, switched off its dead `import
anthropic` and onto the provider-agnostic `llm` module.

THIS STEP PRODUCES FICTION, ON PURPOSE.

There is no policy register behind it. It sketches the mapping and gap
assessment a consultant would draft before evidence exists — useful as
scaffolding, dangerous if mistaken for a finding. Two structural guards carry
that warning further than a comment can:

  * rows land in `req_mappings`, a table separate from `requirements`, so a
    query cannot pick up an assessment without also selecting the table it
    came from;
  * every row carries `provenance`, defaulting to `'seeded'`. Promoting a row
    to `'reviewed'` or `'sourced'` is a deliberate act by whoever checked it.

Owner names are mapped back to IDs against the `owners` table. An unmatched
name becomes `OWN-UNMAPPED` rather than being silently dropped — a visible
placeholder that `validate` counts and reports.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone

from .llm import LLMError, Provider, QuotaExhausted, default_sleep

SYSTEM_PROMPT_TMPL = """You are drafting SEEDED, illustrative scaffolding for a bank's internal
compliance mapping. No real policy documents exist yet for this exercise — you are producing a
first-draft placeholder that a compliance officer will later overwrite with the bank's actual
artefacts. Do not present anything you write as fact about a real institution.

Given a regulatory requirement, produce:
1. A plausible mapping: what a reasonably well-run Indian commercial bank's branch risk function
   would typically have in place for this kind of obligation (policy name, process, control,
   control_type, evidence_required). Use realistic but generic naming, not invented specifics you
   could not know.
2. Assign process and control owners from this fixed role list only — pick the single best fit
   for each: {owner_list}
3. Pick the single best-fitting business area from this fixed list only: {area_list}
4. A seeded assessment: classification (Compliant / Partially Compliant / Gap / Not Applicable /
   To Be Confirmed), a one-to-three sentence finding, a one-to-two sentence recommendation, and a
   severity (Low/Medium/High).

Calibration for the classification you assign (do not cluster everything in one bucket):
- Compliant: mechanical, easily automated, low-judgment obligations.
- Partially Compliant: the most common real-world outcome — a control exists but has a specific,
  named blind spot. This should be your most-used classification.
- Gap: something plausibly absent or systemically broken, not just imperfect. Use sparingly.
- To Be Confirmed: use when verifying compliance would require evidence (vendor attestations,
  log samples) that a first-pass review would not yet have gathered.
- Not Applicable: only if the clause plausibly does not apply to this bank's current business.

The finding must name a SPECIFIC, plausible gap mechanism — not a generic "may not be fully
compliant." Study the worked style: "Dedupe logic exists at onboarding but historical duplicate
UCICs remain in the book" is good; "the process needs improvement" is not."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "business_area":       {"type": "string"},
        "business_area_guess": {"type": "string"},
        "mapping": {
            "type": "object",
            "properties": {
                "policy":            {"type": "string"},
                "process":           {"type": "string"},
                "control":           {"type": "string"},
                "control_type":      {"type": "string", "enum": ["Preventive", "Detective", "Corrective"]},
                "owner_process":     {"type": "string"},
                "owner_control":     {"type": "string"},
                "evidence_required": {"type": "string"},
            },
        },
        "assessment": {
            "type": "object",
            "properties": {
                "classification": {"type": "string",
                                   "enum": ["Compliant", "Partially Compliant", "Gap",
                                            "Not Applicable", "To Be Confirmed"]},
                "finding":        {"type": "string"},
                "recommendation": {"type": "string"},
                "severity":       {"type": "string", "enum": ["Low", "Medium", "High"]},
            },
        },
    },
    "required": ["mapping", "assessment"],
}

CLASSIFICATIONS = {"Compliant", "Partially Compliant", "Gap", "Not Applicable", "To Be Confirmed"}
SEVERITIES = {"Low", "Medium", "High"}
CONTROL_TYPES = {"Preventive", "Detective", "Corrective"}
UNMAPPED_OWNER = "OWN-UNMAPPED"
UNMAPPED_AREA = "BA-99"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reference(conn: sqlite3.Connection) -> tuple[dict[str, str], dict[str, str]]:
    owners = {
        r["role"].strip().lower(): r["id"]
        for r in conn.execute("SELECT id, role FROM owners WHERE id <> ?", (UNMAPPED_OWNER,))
    }
    areas = {
        r["name"].strip().lower(): r["id"]
        for r in conn.execute("SELECT id, name FROM business_areas WHERE id <> ?", (UNMAPPED_AREA,))
    }
    return owners, areas


def _resolve(name, table: dict[str, str], fallback: str) -> tuple[str, bool]:
    """Exact match, then a forgiving contains-match, then the flagged fallback."""
    key = (name or "").strip().lower()
    if not key:
        return fallback, False
    if key in table:
        return table[key], True
    for known, ident in table.items():
        if key in known or known in key:
            return ident, True
    return fallback, False


def pending_requirements(
    conn: sqlite3.Connection, doc_id: str | None = None,
    force: bool = False, limit: int | None = None,
) -> list[sqlite3.Row]:
    sql = [
        "SELECT r.id, r.doc_id, r.clause_id, r.clause_title, r.requirement, r.obligation_type,",
        "       r.branch_relevance, r.timeline, c.clause_label,",
        "       d.title AS doc_title, d.primary_topic, d.institution_type",
        "FROM requirements r JOIN clauses c ON c.id = r.clause_id",
        "JOIN documents d ON d.id = r.doc_id",
        "WHERE 1=1",
    ]
    params: list = []
    if doc_id:
        sql.append("AND r.doc_id = ?")
        params.append(doc_id)
    if not force:
        # Never silently overwrite a mapping a human has reviewed.
        sql.append("AND NOT EXISTS (SELECT 1 FROM req_mappings m WHERE m.req_id = r.id)")
    else:
        sql.append("AND NOT EXISTS (SELECT 1 FROM req_mappings m WHERE m.req_id = r.id "
                   "AND m.provenance <> 'seeded')")
    sql.append("ORDER BY r.doc_id, c.seq")
    if limit:
        sql.append("LIMIT ?")
        params.append(limit)
    return conn.execute(" ".join(sql), params).fetchall()


def _enum(value, allowed: set[str]) -> str | None:
    v = (value or "").strip()
    return v if v in allowed else None


def store_mapping(conn: sqlite3.Connection, req: sqlite3.Row, result: dict,
                  owners: dict, areas: dict, model: str,
                  area_hint: str | None = None) -> bool:
    m = result.get("mapping") or {}
    a = result.get("assessment") or {}

    owner_process, ok_p = _resolve(m.get("owner_process"), owners, UNMAPPED_OWNER)
    owner_control, ok_c = _resolve(m.get("owner_control"), owners, UNMAPPED_OWNER)
    area, ok_a = _resolve(result.get("business_area"), areas, UNMAPPED_AREA)
    if not ok_a and area_hint:
        # The model named an area outside the fixed list. The taxonomy's own
        # topic mapping is a better answer than BA-99, and it is traceable.
        area, ok_a = area_hint, True

    conn.execute(
        "INSERT INTO req_mappings (req_id, business_area, business_area_guess, policy, process, "
        "control, control_type, owner_process, owner_control, evidence_required, classification, "
        "finding, recommendation, severity, provenance, model, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'seeded',?,?) "
        "ON CONFLICT(req_id) DO UPDATE SET business_area=excluded.business_area, "
        "business_area_guess=excluded.business_area_guess, policy=excluded.policy, "
        "process=excluded.process, control=excluded.control, control_type=excluded.control_type, "
        "owner_process=excluded.owner_process, owner_control=excluded.owner_control, "
        "evidence_required=excluded.evidence_required, classification=excluded.classification, "
        "finding=excluded.finding, recommendation=excluded.recommendation, "
        "severity=excluded.severity, model=excluded.model, created_at=excluded.created_at",
        (
            req["id"], area,
            (result.get("business_area_guess") or result.get("business_area") or "").strip() or None,
            (m.get("policy") or "").strip() or None,
            (m.get("process") or "").strip() or None,
            (m.get("control") or "").strip() or None,
            _enum(m.get("control_type"), CONTROL_TYPES),
            owner_process, owner_control,
            (m.get("evidence_required") or "").strip() or None,
            _enum(a.get("classification"), CLASSIFICATIONS),
            (a.get("finding") or "").strip() or None,
            (a.get("recommendation") or "").strip() or None,
            _enum(a.get("severity"), SEVERITIES),
            model, _now(),
        ),
    )
    conn.commit()
    return ok_p and ok_c and ok_a


def scaffold(
    conn: sqlite3.Connection, provider: Provider, doc_id: str | None = None,
    force: bool = False, limit: int | None = None, sleep: float | None = None,
    quiet: bool = False,
) -> dict:
    rows = pending_requirements(conn, doc_id, force, limit)
    if not rows:
        return {"requirements": 0, "mapped": 0,
                "note": "nothing pending — run `extract` first, or pass --force"}

    owners, areas = _reference(conn)
    if not owners or not areas:
        raise SystemExit(
            "owners / business_areas tables are empty. Run `npm run init` to seed them."
        )

    # Topic -> business-area priors from seed/taxonomy.json.
    try:
        from .taxonomy import load as _load_tax
        topic_hints = _load_tax().topic_to_business_area
    except SystemExit:
        topic_hints = {}
    area_names = {
        r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM business_areas")
    }

    owner_list = ", ".join(sorted(r for r in
        (x["role"] for x in conn.execute("SELECT role FROM owners WHERE id <> ?", (UNMAPPED_OWNER,)))))
    area_list = ", ".join(sorted(r for r in
        (x["name"] for x in conn.execute("SELECT name FROM business_areas WHERE id <> ?", (UNMAPPED_AREA,)))))
    sys_prompt = SYSTEM_PROMPT_TMPL.format(owner_list=owner_list, area_list=area_list)

    pause = default_sleep(provider) if sleep is None else sleep
    mapped = failed = unmapped = 0
    stopped: str | None = None

    for i, r in enumerate(rows, 1):
        if not quiet:
            print(f"[scaffold] {i}/{len(rows)}  {r['id']}", file=sys.stderr)
        # Give the model the parent document's context. Without it the model
        # sees one clause and a list of 36 area names and has to guess which
        # part of the bank owns it — a clause about "the Board shall review
        # the policy annually" is unassignable in isolation but obvious once
        # you know it came from a Capital Adequacy Master Direction.
        #
        # `hint` is the taxonomy's own topic -> business-area mapping. It is
        # offered as a suggestion, not imposed: the model still chooses, and
        # a clause that genuinely belongs elsewhere can still go elsewhere.
        topic = r["primary_topic"] if "primary_topic" in r.keys() else None
        hint_id = topic_hints.get(topic) if topic else None
        hint_name = area_names.get(hint_id) if hint_id else None

        context = [
            f"Source document: {r['doc_title']}",
        ]
        if topic and topic != "Unclassified":
            context.append(f"Document topic: {topic}")
        if r["institution_type"]:
            context.append(f"Applies to: {r['institution_type']}")
        if hint_name:
            context.append(
                f"Suggested business area (from the document's topic; override if the "
                f"clause clearly belongs elsewhere): {hint_name}"
            )

        user = (
            "\n".join(context) + "\n\n"
            f"Requirement [{r['id']}] {r['clause_title'] or '(untitled)'} "
            f"(clause {r['clause_label']}):\n{r['requirement']}\n\n"
            f"Obligation type: {r['obligation_type'] or 'unknown'} | "
            f"Branch relevance: {r['branch_relevance'] or 'unknown'} | "
            f"Timeline: {r['timeline'] or 'none'}"
        )
        try:
            result = provider.json_call(sys_prompt, user, RESPONSE_SCHEMA, max_output_tokens=1000)
        except QuotaExhausted as e:
            stopped = str(e)
            print(f"[scaffold] STOPPED at {i}/{len(rows)}: {e}", file=sys.stderr)
            break
        except LLMError as e:
            failed += 1
            print(f"           !! {e}", file=sys.stderr)
            time.sleep(pause)
            continue

        clean = store_mapping(conn, r, result, owners, areas, provider.model,
                              area_hint=hint_id)
        mapped += 1
        if not clean:
            unmapped += 1
            if not quiet:
                print("           WARNING: owner or business area did not match the fixed list "
                      "— flagged for manual fix", file=sys.stderr)
        time.sleep(pause)

    out = {
        "requirements": len(rows), "mapped": mapped, "failed": failed,
        "needs_manual_fix": unmapped, "remaining": len(rows) - mapped - failed,
        "provider": provider.name, "model": provider.model,
        "provenance": "seeded — NOT evidence of compliance",
    }
    if stopped:
        out["stopped"] = stopped
    return out
