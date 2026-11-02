import clsx from "clsx";
import type { Band, CaseStatus, Reason } from "../api/types";
import { BAND_TONE, humanize, STATUS_TONE } from "../lib/format";
import { Badge } from "./ui";

export function BandBadge({ band }: { band: Band | null | undefined }) {
  if (!band) return <Badge>unscored</Badge>;
  return <Badge tone={BAND_TONE[band]}>{band}</Badge>;
}

export function StatusBadge({ status }: { status: CaseStatus }) {
  return <Badge tone={STATUS_TONE[status]}>{humanize(status)}</Badge>;
}

/** Labels where a reason came from, so model output is never mistaken for a deterministic rule. */
export function SourceTag({ source }: { source: Reason["source"] }) {
  return (
    <span
      title={source === "model" ? "From the machine-learning model (SHAP explanation)" : "From a deterministic rule"}
      className={clsx(
        "shrink-0 rounded px-1.5 py-0.5 text-[11px] font-medium",
        source === "model" ? "bg-violet-50 text-violet-700" : "bg-slate-100 text-slate-700",
      )}
    >
      {source === "model" ? "Model" : "Rule"}
    </span>
  );
}

export function ReasonList({ reasons, compact }: { reasons: Reason[]; compact?: boolean }) {
  if (!reasons.length) return <p className="text-sm text-slate-500">No notable risk indicators.</p>;
  return (
    <ul className={clsx("space-y-1.5", compact && "text-xs")}>
      {reasons.map((r, i) => (
        <li key={i} className="flex items-start gap-2 text-sm" data-testid="reason">
          <SourceTag source={r.source} />
          <span className="text-slate-800">
            {r.text}
            {r.source === "model" && r.contribution !== undefined && (
              <span className={clsx("ml-1.5 text-xs", r.contribution > 0 ? "text-red-700" : "text-emerald-700")}>
                ({r.contribution > 0 ? "raises" : "lowers"} risk, {r.label?.toLowerCase()})
              </span>
            )}
            {r.source === "rule" && <span className="ml-1.5 text-xs text-slate-500">{r.rule} → at least {r.min_band}</span>}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function ScoreBar({ score }: { score: number | null | undefined }) {
  if (score === null || score === undefined) return <span className="text-xs text-slate-500">rules only</span>;
  const pctValue = Math.max(1, Math.round(score * 100));
  return (
    <span className="inline-flex items-center gap-2" title={`Calibrated fraud probability ${(score * 100).toFixed(1)}%`}>
      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-slate-200">
        <span className={clsx("block h-full", score >= 0.5 ? "bg-violet-600" : score >= 0.1 ? "bg-red-500" : "bg-amber-400")}
          style={{ width: `${pctValue}%` }} />
      </span>
      <span className="tabular-nums text-xs text-slate-700">{(score * 100).toFixed(1)}%</span>
    </span>
  );
}

export function PolicyNote() {
  return (
    <p className="text-xs text-slate-500">
      Scores are risk indicators for human review, not findings. The engine never declines payments or blocks
      accounts. All data is synthetic.
    </p>
  );
}
