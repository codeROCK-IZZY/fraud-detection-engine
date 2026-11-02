export const dateTime = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";

export const date = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" }) : "—";

export const money = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : v.toLocaleString(undefined, { style: "currency", currency: "USD" });

export const pct = (v: number | null | undefined, digits = 0) =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`;

export const num = (v: number | null | undefined, digits = 0) =>
  v === null || v === undefined ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: digits });

export const humanize = (s: string | null | undefined) => (s ? s.replaceAll("_", " ") : "—");

/** Time left until (or since) a deadline, e.g. "3h left" / "2h overdue". */
export function slaText(dueIso: string, now: Date = new Date()): string {
  const ms = new Date(dueIso).getTime() - now.getTime();
  const h = Math.abs(ms) / 3_600_000;
  const span = h < 1 ? `${Math.round(h * 60)}m` : h < 48 ? `${Math.round(h)}h` : `${Math.round(h / 24)}d`;
  return ms >= 0 ? `${span} left` : `${span} overdue`;
}

export const BAND_TONE = { low: "green", medium: "amber", high: "red", critical: "violet" } as const;
export const STATUS_TONE = { open: "sky", in_review: "amber", escalated: "red", closed: "slate" } as const;
export const PRIORITY_LABEL: Record<number, string> = { 1: "P1 · critical", 2: "P2 · high", 3: "P3 · sample" };
