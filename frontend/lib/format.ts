export const usd = (x: number | null | undefined, digits = 2) =>
  x == null ? "–" : x.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits });

export const pct = (x: number | null | undefined, digits = 1, sign = true) =>
  x == null ? "–" : `${sign && x > 0 ? "+" : ""}${x.toFixed(digits)}%`;

const ACRONYMS: Record<string, string> = { fda: "FDA", m: "M", a: "A", sec: "SEC", eps: "EPS", ipo: "IPO" };
const LABELS: Record<string, string> = {
  fda_approval: "FDA approval",
  m_and_a_target: "M&A target",
  earnings_beat: "Earnings beat",
  guidance_raise: "Guidance raise",
  positive_trial: "Positive trial",
  contract_win: "Contract win",
  insider_buy_cluster: "Insider buying",
  upgrade: "Analyst upgrade",
  signal_decay: "Signal decay",
  time_stop: "Time stop",
};

export const title = (s: string) =>
  LABELS[s] ??
  s
    .split("_")
    .map((w) => ACRONYMS[w] ?? w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");

export function ago(hours: number | null | undefined): string {
  if (hours == null) return "–";
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} min ago`;
  if (hours < 48) return `${hours.toFixed(hours < 10 ? 1 : 0)} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

export const capBucket = (cap: number | null) =>
  cap == null ? "unknown" : cap >= 10e9 ? "large" : cap >= 2e9 ? "mid" : cap >= 300e6 ? "small" : "micro";
