"""CLI for the Python analysis layer.

    python -m rbi_intel init                      (use `npm run init` — creates the DB)
    python -m rbi_intel ingest --file x.pdf ...   index a local PDF/DOCX/TXT
    python -m rbi_intel relations                 build the relationship graph
    python -m rbi_intel lineage rbi:md:13136      what replaced / was replaced
    python -m rbi_intel diagram rbi:md:13136      Mermaid diagram
    python -m rbi_intel diagram --category NBFC   category-wide diagram
    python -m rbi_intel chunk rbi:md:13136        clause-level breakdown
    python -m rbi_intel extract                   clauses -> requirements (LLM)
    python -m rbi_intel scaffold                  requirements -> seeded internal mapping (LLM)
    python -m rbi_intel enrich                    classify institution / topic / applicability
    python -m rbi_intel validate                  data-integrity report
    python -m rbi_intel export -o inventory.json  legacy inventory.json shape
    python -m rbi_intel stats
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pathlib as _pathlib

from . import chunk as chunk_mod
from . import enrich as enrich_mod
from . import export as export_mod
from . import graph as graph_mod
from . import ingest as ingest_mod
from . import relations as rel_mod
from . import requirements as req_mod
from . import scaffold as scaffold_mod
from . import taxonomy as taxonomy_mod
from . import validate as validate_mod
from .db import connect, db_path
from .llm import LLMError, get_provider


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rbi_intel", description="RBI regulatory intelligence — analysis layer")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_rel = sub.add_parser("relations", help="Extract lineage edges from document text")
    p_rel.add_argument("--min-confidence", type=float, default=0.3)

    p_lin = sub.add_parser("lineage", help="Show what a document replaced and what replaced it")
    p_lin.add_argument("doc_id")
    p_lin.add_argument("--depth", type=int, default=4)
    p_lin.add_argument("--min-confidence", type=float, default=0.6)

    p_dia = sub.add_parser("diagram", help="Render a Mermaid relationship diagram")
    p_dia.add_argument("doc_id", nargs="?")
    p_dia.add_argument("--category")
    p_dia.add_argument("--depth", type=int, default=2)
    p_dia.add_argument("--min-confidence", type=float, default=0.6)
    p_dia.add_argument("--max-nodes", type=int, default=60)
    p_dia.add_argument("-o", "--out", help="Write to file instead of stdout")
    p_dia.add_argument("--html", action="store_true", help="Wrap in a self-contained renderable HTML page")

    p_ch = sub.add_parser("chunk", help="Split a document into numbered clauses")
    p_ch.add_argument("doc_id", nargs="?")
    p_ch.add_argument("--all-master-directions", action="store_true")

    p_ing = sub.add_parser("ingest", help="Index a local PDF/DOCX/TXT (use when rbi.org.in is unreachable)")
    p_ing.add_argument("--file", required=True)
    p_ing.add_argument("--doc-id", required=True,
                       help="e.g. rbi:md:12798 — must match RBI's own id to line up with scraped data")
    p_ing.add_argument("--title")
    p_ing.add_argument("--date", help="YYYY-MM-DD, or 'November 28, 2025'")
    p_ing.add_argument("--type", default="master_direction", dest="doc_type",
                       choices=list(ingest_mod.VALID_TYPES))
    p_ing.add_argument("--category", help="RBI's regulated-entity grouping, e.g. 'Commercial Banks'")
    p_ing.add_argument("--url", dest="source_url")
    p_ing.add_argument("--no-clean", action="store_true", help="skip header/footer stripping")
    p_ing.add_argument("--chunk", action="store_true", help="chunk into clauses immediately after")

    p_ext = sub.add_parser("extract", help="Clause text -> structured requirements (uses an LLM)")
    p_ext.add_argument("doc_id", nargs="?")
    p_ext.add_argument("--provider", choices=["gemini", "anthropic", "stub"])
    p_ext.add_argument("--model")
    p_ext.add_argument("--limit", type=int, help="first N clauses only — use for a test batch")
    p_ext.add_argument("--sleep", type=float, help="seconds between calls (default: per-provider)")
    p_ext.add_argument("--force", action="store_true", help="re-extract clauses already done")
    p_ext.add_argument("--include-flagged", action="store_true",
                       help="also send clauses the chunker flagged needs_review")

    p_sc = sub.add_parser("scaffold", help="Requirements -> SEEDED internal mapping and assessment")
    p_sc.add_argument("doc_id", nargs="?")
    p_sc.add_argument("--provider", choices=["gemini", "anthropic", "stub"])
    p_sc.add_argument("--model")
    p_sc.add_argument("--limit", type=int)
    p_sc.add_argument("--sleep", type=float)
    p_sc.add_argument("--force", action="store_true",
                      help="redo seeded mappings (never touches provenance='reviewed'/'sourced')")

    sub.add_parser("validate", help="Referential, enum and distribution checks over the database")

    p_exp = sub.add_parser("export", help="Write the legacy inventory.json shape")
    p_exp.add_argument("-o", "--out", required=True)
    p_exp.add_argument("--doc-id", action="append",
                       help="restrict to these documents (repeatable)")

    p_en = sub.add_parser("enrich", help="Classify documents by institution type, topic and applicability")
    p_en.add_argument("doc_id", nargs="?")
    p_en.add_argument("--force", action="store_true", help="reclassify everything")
    p_en.add_argument("--rewrite-titles", action="store_true",
                      help="also strip '(Updated as on ...)' from documents.title")
    p_en.add_argument("--report-only", action="store_true", help="print coverage without reclassifying")
    p_en.add_argument("-v", "--verbose", action="store_true")

    p_tx = sub.add_parser("taxonomy", help="Inspect the enrichment taxonomy, or classify a title")
    p_tx.add_argument("title", nargs="?", help="classify this title and exit")
    p_tx.add_argument("--list", choices=["institutions", "topics", "groups"])

    sub.add_parser("stats", help="Relationship and status counts")

    a = p.parse_args(argv)

    if a.cmd == "relations":
        with connect() as conn:
            rel_mod.build(conn, min_confidence=a.min_confidence)
        return 0

    if a.cmd == "lineage":
        with connect(readonly=True) as conn:
            print(json.dumps(graph_mod.lineage(conn, a.doc_id, a.depth, a.min_confidence), indent=2))
        return 0

    if a.cmd == "diagram":
        if not a.doc_id and not a.category:
            p.error("pass a document id or --category")
        with connect(readonly=True) as conn:
            src = graph_mod.mermaid(
                conn, doc_id=a.doc_id, category=a.category,
                depth=a.depth, min_confidence=a.min_confidence, max_nodes=a.max_nodes,
            )
        out = _wrap_html(src, a.doc_id or a.category or "RBI") if a.html else src
        if a.out:
            Path(a.out).write_text(out, encoding="utf-8")
            print(f"wrote {a.out}", file=sys.stderr)
        else:
            print(out)
        return 0

    if a.cmd == "chunk":
        with connect() as conn:
            if a.all_master_directions:
                rows = conn.execute(
                    "SELECT id FROM documents WHERE doc_type IN ('master_direction','master_circular') "
                    "AND body IS NOT NULL AND LENGTH(body) > 2000"
                ).fetchall()
                total = 0
                for r in rows:
                    res = chunk_mod.chunk_document(conn, r["id"])
                    total += res["clauses"]
                    print(json.dumps(res))
                print(json.dumps({"documents": len(rows), "clauses_total": total}, indent=2))
            elif a.doc_id:
                print(json.dumps(chunk_mod.chunk_document(conn, a.doc_id), indent=2))
            else:
                p.error("pass a document id or --all-master-directions")
        return 0

    if a.cmd == "ingest":
        with connect() as conn:
            res = ingest_mod.ingest_file(
                conn, _pathlib.Path(a.file), doc_id=a.doc_id, title=a.title, date=a.date,
                doc_type=a.doc_type, category=a.category, source_url=a.source_url,
                clean=not a.no_clean,
            )
            res["enrichment"] = enrich_mod.run(conn, doc_id=a.doc_id, force=True)
            res["enrichment"].pop("unclassified_examples", None)
            if a.chunk:
                res["chunking"] = chunk_mod.chunk_document(conn, a.doc_id)
            print(json.dumps(res, indent=2))
        return 0

    if a.cmd in ("extract", "scaffold"):
        try:
            provider = get_provider(a.provider, a.model)
        except LLMError as e:
            print(str(e), file=sys.stderr)
            return 2
        if provider.name == "stub":
            print("[warn] stub provider: output is canned placeholder text, not extraction.",
                  file=sys.stderr)
        with connect() as conn:
            if a.cmd == "extract":
                res = req_mod.extract(
                    conn, provider, doc_id=a.doc_id, force=a.force,
                    include_flagged=a.include_flagged, limit=a.limit, sleep=a.sleep,
                )
            else:
                res = scaffold_mod.scaffold(
                    conn, provider, doc_id=a.doc_id, force=a.force,
                    limit=a.limit, sleep=a.sleep,
                )
            print(json.dumps(res, indent=2))
        # A run cut short by quota is not a success — signal it so a cron job
        # or a `&&` chain does not carry on as though the data were complete.
        return 3 if res.get("stopped") else 0

    if a.cmd == "enrich":
        if a.report_only:
            with connect(readonly=True) as conn:
                print(enrich_mod.format_report(enrich_mod.report(conn)))
            return 0
        with connect() as conn:
            res = enrich_mod.run(conn, doc_id=a.doc_id, force=a.force,
                                 rewrite_titles=a.rewrite_titles, quiet=not a.verbose)
        print(enrich_mod.format_report(res))
        return 0

    if a.cmd == "taxonomy":
        tax = taxonomy_mod.load()
        if a.title:
            print(json.dumps(tax.classify(a.title), indent=2, ensure_ascii=False))
        elif a.list == "institutions":
            print("\n".join(tax.institution_names))
        elif a.list == "topics":
            for t in tax.topic_names:
                print(f"{tax.topic_groups.get(t, ''):<38} {t}")
        elif a.list == "groups":
            groups: dict[str, int] = {}
            for g in tax.topic_groups.values():
                groups[g] = groups.get(g, 0) + 1
            for g, n in sorted(groups.items(), key=lambda x: -x[1]):
                print(f"{n:>4}  {g}")
        else:
            print(json.dumps({
                "version": tax.version,
                "institution_types": len(tax.institution_names),
                "topics": len(tax.topic_names),
                "applicability_values": tax.applicability_values,
                "topic_to_business_area": len(tax.topic_to_business_area),
            }, indent=2))
        return 0

    if a.cmd == "validate":
        with connect(readonly=True) as conn:
            res = validate_mod.run(conn)
        print(validate_mod.report(res))
        return 0 if res["ok"] else 1

    if a.cmd == "export":
        with connect(readonly=True) as conn:
            res = export_mod.write_inventory(conn, _pathlib.Path(a.out), doc_ids=a.doc_id)
        print(json.dumps(res, indent=2))
        return 0

    if a.cmd == "stats":
        with connect(readonly=True) as conn:
            print(json.dumps({"db": str(db_path()), **graph_mod.stats(conn)}, indent=2))
        return 0

    return 1


def _wrap_html(mermaid_src: str, title: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RBI lineage — {title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
         background:#fafaf9; color:#1c1917; }}
  header {{ padding:20px 24px; border-bottom:1px solid #e7e5e4; background:#fff; }}
  h1 {{ margin:0; font-size:17px; font-weight:600; letter-spacing:-0.01em; }}
  p.sub {{ margin:4px 0 0; font-size:13px; color:#78716c; }}
  .legend {{ display:flex; gap:18px; flex-wrap:wrap; padding:12px 24px; font-size:12px;
             color:#57534e; border-bottom:1px solid #e7e5e4; background:#fff; }}
  .legend span {{ display:inline-flex; align-items:center; gap:6px; }}
  .sw {{ width:12px; height:12px; border-radius:3px; display:inline-block; }}
  main {{ padding:24px; overflow:auto; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background:#0c0a09; color:#e7e5e4; }}
    header,.legend {{ background:#1c1917; border-color:#292524; }}
    p.sub,.legend {{ color:#a8a29e; }}
  }}
</style></head><body>
<header>
  <h1>RBI regulatory lineage — {title}</h1>
  <p class="sub">Machine-extracted from document text. Verify against the official source before relying on any edge.</p>
</header>
<div class="legend">
  <span><i class="sw" style="background:#ecfdf5;border:1px solid #047857"></i> in force</span>
  <span><i class="sw" style="background:#fffbeb;border:1px solid #b45309"></i> superseded</span>
  <span><i class="sw" style="background:#fef2f2;border:1px solid #b91c1c"></i> repealed / withdrawn</span>
  <span>double border = Master Direction</span>
  <span>solid arrow = replaces &nbsp; dashed = amends / refers</span>
</div>
<main><pre class="mermaid">
{mermaid_src}
</pre></main>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  mermaid.initialize({{ startOnLoad: true, securityLevel: "loose",
    theme: window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "default" }});
</script>
</body></html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
