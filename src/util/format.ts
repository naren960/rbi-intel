export const DISCLAIMER =
  "Source: official RBI publications (rbi.org.in). Primary-source retrieval, not legal advice. " +
  "Relationship edges are machine-extracted — verify against the linked official document before relying on them.";

export function ok(data: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
}

export function err(m: string) {
  return { content: [{ type: "text" as const, text: `Error: ${m}` }], isError: true };
}

export function emptyDbMsg() {
  return ok({
    message:
      "The regulatory index is empty. Run `npm run sync` (or call sync_latest) to populate it, " +
      "then `python -m rbi_intel relations` to build the relationship graph.",
    disclaimer: DISCLAIMER,
  });
}
