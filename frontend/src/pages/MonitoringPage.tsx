import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api/client";
import type { DriftReport, Performance } from "../api/types";
import { Badge, Button, Card, Empty, ErrorBox, Loading, PageHeader, Stat, Table } from "../components/ui";
import { ADMINS, useAuth } from "../lib/auth";
import { date, dateTime, num, pct } from "../lib/format";

const DRIFT_TONE = { ok: "green", warning: "amber", alert: "red" } as const;

export function MonitoringPage() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const drift = useQuery({ queryKey: ["drift"], queryFn: () => api<DriftReport[]>("/monitoring/drift") });
  const perf = useQuery({ queryKey: ["performance"], queryFn: () => api<Performance>("/monitoring/performance?weeks=16") });
  const latency = useQuery({
    queryKey: ["latency"],
    queryFn: () => api<{ scores_24h: number; mean_ms: number | null; p50_ms: number | null; p95_ms: number | null; note: string }>("/monitoring/latency"),
  });
  const run = useMutation({
    mutationFn: () => api<{ job_id: number }>("/monitoring/drift?days=7", { method: "POST" }),
    onSuccess: () => setTimeout(() => void qc.invalidateQueries({ queryKey: ["drift"] }), 4000),
  });
  const latest = drift.data?.[0];
  const psiRows = latest ? Object.entries(latest.feature_psi).map(([feature, psi]) => ({ feature, psi })).sort((a, b) => b.psi - a.psi) : [];

  return (
    <>
      <PageHeader title="Model monitoring"
        subtitle="Drift needs no labels; performance waits for labels to mature."
        actions={can(ADMINS) && <Button variant="secondary" busy={run.isPending} onClick={() => run.mutate()}>Run drift check (7 days)</Button>} />
      <ErrorBox error={run.error} />
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Latest drift status" value={latest ? <Badge tone={DRIFT_TONE[latest.status]}>{latest.status}</Badge> : "—"}
          sub={latest ? `${dateTime(latest.window_start)} → ${dateTime(latest.window_end)}` : "no report yet"} />
        <Stat label="Score PSI" value={latest?.score_psi?.toFixed(3) ?? "—"} sub="< 0.1 stable · > 0.25 alert" />
        <Stat label="Scoring latency p95 (24 h)" value={latency.data?.p95_ms ? `${latency.data.p95_ms.toFixed(1)} ms` : "—"}
          sub={`${num(latency.data?.scores_24h)} live scores · p50 ${latency.data?.p50_ms?.toFixed(1) ?? "—"} ms`} />
        <Stat label="Label maturity" value={`${perf.data?.maturity_days ?? "—"} days`} sub="weeks younger than this are not judged" />
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card title="Feature drift (latest report, PSI)">
          {drift.isLoading ? <Loading /> : !psiRows.length ? <Empty>No drift report yet.</Empty> : (
            <div className="h-80" role="img" aria-label="PSI per monitored feature">
              <ResponsiveContainer>
                <BarChart data={psiRows} layout="vertical" margin={{ left: 60 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis type="number" fontSize={12} />
                  <YAxis type="category" dataKey="feature" fontSize={11} width={120} />
                  <Tooltip formatter={(v: number) => v.toFixed(3)} />
                  <ReferenceLine x={0.1} stroke="#d97706" strokeDasharray="4 4" />
                  <ReferenceLine x={0.25} stroke="#dc2626" strokeDasharray="4 4" />
                  <Bar dataKey="psi" fill="#0369a1" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </Card>
        <Card title="Weekly alert quality (labels known so far)">
          {perf.isLoading ? <Loading /> : perf.error ? <ErrorBox error={perf.error} /> : !perf.data?.weeks.length ? <Empty>No scored weeks.</Empty> : (
            <div className="h-80" role="img" aria-label="Weekly alert precision and recall">
              <ResponsiveContainer>
                <LineChart data={perf.data.weeks.map((w) => ({ ...w, label: date(w.week_start) }))}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="label" fontSize={11} />
                  <YAxis domain={[0, 1]} tickFormatter={(v: number) => pct(v)} fontSize={12} />
                  <Tooltip formatter={(v: number) => pct(v, 1)} />
                  <Legend />
                  <Line dataKey="alert_precision" name="Alert precision" stroke="#0369a1" connectNulls />
                  <Line dataKey="recall" name="Recall" stroke="#dc2626" connectNulls />
                  <Line dataKey="pr_auc" name="PR-AUC" stroke="#7c3aed" connectNulls />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </Card>
      </div>

      <Card title="Weekly detail" className="mt-6">
        {perf.data?.weeks.length ? (
          <Table>
            <thead><tr><th>Week</th><th>Labels</th><th>Transactions</th><th>Flagged</th><th>Reported fraud</th><th>Precision</th><th>Recall</th><th>$ recall</th><th>PR-AUC</th></tr></thead>
            <tbody>
              {perf.data.weeks.map((w) => (
                <tr key={w.week_start}>
                  <td>{date(w.week_start)}</td>
                  <td>{w.mature ? <Badge tone="green">mature</Badge> : <Badge tone="amber">immature</Badge>}</td>
                  <td className="tabular-nums">{num(w.transactions)}</td>
                  <td className="tabular-nums">{num(w.flagged)}</td>
                  <td className="tabular-nums">{num(w.reported_fraud)}</td>
                  <td className="tabular-nums">{pct(w.alert_precision, 1)}</td>
                  <td className="tabular-nums">{pct(w.recall, 1)}</td>
                  <td className="tabular-nums">{pct(w.dollar_recall, 1)}</td>
                  <td className="tabular-nums">{w.pr_auc?.toFixed(3) ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : <Empty>No data.</Empty>}
        <p className="mt-3 text-xs text-slate-500">
          Immature weeks look better than they are: fraud on those days has mostly not been reported yet.
        </p>
      </Card>

      {drift.data && drift.data.length > 1 && (
        <Card title="Drift report history" className="mt-6">
          <Table>
            <thead><tr><th>Created</th><th>Window</th><th>Transactions</th><th>Score PSI</th><th>Max feature PSI</th><th>Status</th></tr></thead>
            <tbody>{drift.data.map((r) => (
              <tr key={r.id}><td className="text-xs">{dateTime(r.created_at)}</td><td className="text-xs">{date(r.window_start)} – {date(r.window_end)}</td>
                <td className="tabular-nums">{num(r.n_transactions)}</td><td className="tabular-nums">{r.score_psi?.toFixed(3) ?? "—"}</td>
                <td className="tabular-nums">{Math.max(...Object.values(r.feature_psi)).toFixed(3)}</td>
                <td><Badge tone={DRIFT_TONE[r.status]}>{r.status}</Badge></td></tr>))}
            </tbody>
          </Table>
        </Card>
      )}
    </>
  );
}
