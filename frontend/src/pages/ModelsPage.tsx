import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api/client";
import type { Job, ModelDetail, ModelSummary } from "../api/types";
import { Badge, Button, Card, Empty, ErrorBox, Loading, PageHeader, Table } from "../components/ui";
import { ADMINS, useAuth } from "../lib/auth";
import { dateTime, humanize, num } from "../lib/format";

export function ModelsPage() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const models = useQuery({ queryKey: ["models"], queryFn: () => api<ModelSummary[]>("/models") });
  const active = models.data?.find((m) => m.is_active) ?? models.data?.[0];
  const [selected, setSelected] = useState<number | null>(null);
  const shown = selected ?? active?.id ?? null;
  const detail = useQuery({
    queryKey: ["model", shown],
    queryFn: () => api<ModelDetail>(`/models/${shown}`),
    enabled: shown !== null,
  });
  const [jobId, setJobId] = useState<number | null>(null);
  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api<Job>(`/jobs/${jobId}`),
    enabled: jobId !== null,
    refetchInterval: (q) => (q.state.data && ["succeeded", "failed"].includes(q.state.data.status) ? false : 3000),
  });
  const done = () => void qc.invalidateQueries({ queryKey: ["models"] });
  const train = useMutation({
    mutationFn: () => api<{ job_id: number }>("/models/train", { body: { capacity_per_day: 25, backfill_days: 14 } }),
    onSuccess: (r) => setJobId(r.job_id),
  });
  const activate = useMutation({ mutationFn: (id: number) => api(`/models/${id}/activate`, { method: "POST" }), onSuccess: done });
  const rollback = useMutation({ mutationFn: () => api("/models/rollback", { method: "POST" }), onSuccess: done });
  const jobStatus = job.data?.status;
  useEffect(() => {
    if (jobStatus === "succeeded") void qc.invalidateQueries({ queryKey: ["models"] });
  }, [jobStatus, qc]);

  if (models.isLoading) return <Loading />;
  if (models.error) return <ErrorBox error={models.error} />;
  const d = detail.data;

  return (
    <>
      <PageHeader title="Models" subtitle="Each version records its data window, validation results and thresholds."
        actions={can(ADMINS) && (
          <>
            <Button variant="secondary" busy={rollback.isPending} onClick={() => rollback.mutate()}>Roll back</Button>
            <Button busy={train.isPending || job.data?.status === "running" || job.data?.status === "queued"} onClick={() => train.mutate()}>Train new version</Button>
          </>
        )} />
      <ErrorBox error={train.error ?? activate.error ?? rollback.error} />
      {job.data && (
        <p className="mb-4 text-sm text-slate-600" role="status">
          Training job #{job.data.id}: <strong>{job.data.status}</strong>
          {job.data.error && <span className="text-red-700"> — {job.data.error}</span>}
        </p>
      )}
      <Card title="Versions">
        {!models.data?.length ? <Empty>No model trained yet. Until one is, transactions are assessed by rules only.</Empty> : (
          <Table>
            <thead><tr><th>Version</th><th>Algorithm</th><th>Trained</th><th>Calibration PR-AUC</th><th>High threshold</th><th>Status</th><th /></tr></thead>
            <tbody>
              {models.data.map((m) => (
                <tr key={m.id} className={m.id === shown ? "bg-sky-50" : undefined}>
                  <td><button className="font-medium text-sky-700 hover:underline" onClick={() => setSelected(m.id)}>v{m.version}</button></td>
                  <td>{humanize(m.algorithm)}</td>
                  <td className="text-xs">{dateTime(m.trained_at)}</td>
                  <td className="tabular-nums">{m.calibration_pr_auc?.toFixed(3) ?? "—"}</td>
                  <td className="tabular-nums">{m.thresholds.high.toFixed(4)}</td>
                  <td>{m.is_active ? <Badge tone="green">active</Badge> : <Badge>inactive</Badge>}</td>
                  <td>{can(ADMINS) && !m.is_active && <Button variant="secondary" onClick={() => activate.mutate(m.id)}>Activate</Button>}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {d && (
        <div className="mt-6 grid gap-6 lg:grid-cols-2">
          <Card title={`v${d.version} · validation`}>
            <p className="mb-3 text-sm text-slate-700">{d.metrics.algorithm_choice}</p>
            <Table>
              <thead><tr><th>Model</th><th>Cross-validated PR-AUC (grouped by card)</th></tr></thead>
              <tbody>{Object.entries(d.metrics.cross_validation).map(([name, cv]) => (
                <tr key={name}><td>{humanize(name)}</td><td className="tabular-nums">{cv.pr_auc_mean.toFixed(3)} ± {cv.pr_auc_std.toFixed(3)}</td></tr>))}
              </tbody>
            </Table>
            <dl className="mt-4 grid grid-cols-2 gap-y-1 text-sm">
              <dt className="text-slate-500">Calibration-window PR-AUC</dt><dd>{d.metrics.calibration_window.pr_auc?.toFixed(3) ?? "—"}</dd>
              <dt className="text-slate-500">Thresholds</dt>
              <dd className="text-xs">medium {d.thresholds.medium.toFixed(4)} · high {d.thresholds.high.toFixed(4)} · critical {d.thresholds.critical.toFixed(2)}</dd>
              <dt className="text-slate-500">Set for capacity</dt><dd>{num(d.thresholds.capacity_per_day)} reviews/day</dd>
              {Object.entries(d.metrics.data).map(([k, v]) => (
                <div key={k} className="contents"><dt className="text-slate-500">{humanize(k)}</dt><dd className="text-xs">{String(v)}</dd></div>
              ))}
            </dl>
            <p className="mt-3 text-xs text-slate-500">{d.metrics.calibration_window.note} Results on synthetic data do not predict real-world performance.</p>
          </Card>
          <Card title="Most influential features (mean |SHAP|)">
            {d.metrics.global_importance.length ? (
              <div className="h-96" role="img" aria-label="Global feature importance">
                <ResponsiveContainer>
                  <BarChart data={d.metrics.global_importance} layout="vertical" margin={{ left: 60 }}>
                    <XAxis type="number" fontSize={12} />
                    <YAxis type="category" dataKey="feature" fontSize={11} width={130} />
                    <Tooltip formatter={(v: number) => v.toFixed(3)} />
                    <Bar dataKey="mean_abs_shap" fill="#7c3aed" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            ) : <Empty>No importance data.</Empty>}
          </Card>
        </div>
      )}
    </>
  );
}
