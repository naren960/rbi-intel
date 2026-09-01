/**
 * RBI serves dates as "Aug 14, 2026".
 *
 * The original package did `new Date(s).toISOString().split("T")[0]`, which
 * parses to LOCAL midnight and then converts to UTC. In IST (UTC+5:30) that
 * shifts every date back one day: "Aug 14, 2026" -> "2026-08-13". Silent,
 * systematic, and it corrupts any chronological reasoning about which
 * circular superseded which. We parse the components directly instead.
 */
const MONTHS: Record<string, number> = {
  jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6,
  jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12,
};

export function toISODate(raw: string): string {
  const s = raw.trim();

  // "Aug 14, 2026" / "August 14, 2026" / "Aug 14 2026"
  let m = s.match(/^([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})$/);
  if (m) return build(m[3], MONTHS[m[1].slice(0, 3).toLowerCase()], m[2]);

  // "14 Aug 2026"
  m = s.match(/^(\d{1,2})\s+([A-Za-z]{3,9}),?\s+(\d{4})$/);
  if (m) return build(m[3], MONTHS[m[2].slice(0, 3).toLowerCase()], m[1]);

  // "14/08/2026" or "14-08-2026" (RBI uses day-first)
  m = s.match(/^(\d{1,2})[\/-](\d{1,2})[\/-](\d{4})$/);
  if (m) return build(m[3], Number(m[2]), m[1]);

  // Already ISO
  m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (m) return `${m[1]}-${m[2]}-${m[3]}`;

  return "";
}

function build(year: string, month: number | undefined, day: string): string {
  if (!month || Number.isNaN(month)) return "";
  const d = Number(day);
  if (d < 1 || d > 31) return "";
  return `${year}-${String(month).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

export function isoDaysAgo(days: number): string {
  const d = new Date(Date.now() - days * 86_400_000);
  return d.toISOString();
}
