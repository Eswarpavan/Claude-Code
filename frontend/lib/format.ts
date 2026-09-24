export const usd = (x: number | null | undefined, digits = 2) =>
  x == null ? "–" : x.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits });

export const pct = (x: number | null | undefined, digits = 1, sign = true) =>
  x == null ? "–" : `${sign && x > 0 ? "+" : ""}${x.toFixed(digits)}%`;

export const title = (s: string) => s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

export function ago(hours: number | null | undefined): string {
  if (hours == null) return "–";
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} min ago`;
  if (hours < 48) return `${hours.toFixed(hours < 10 ? 1 : 0)} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

export const capBucket = (cap: number | null) =>
  cap == null ? "unknown" : cap >= 10e9 ? "large" : cap >= 2e9 ? "mid" : cap >= 300e6 ? "small" : "micro";
