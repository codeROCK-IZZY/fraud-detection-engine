import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { TransactionDetail } from "../api/types";
import { BandBadge, ReasonList, ScoreBar, StatusBadge } from "../components/fraud";
import { Card, Empty, ErrorBox, Loading, PageHeader, Table } from "../components/ui";
import { ANALYSTS, useAuth } from "../lib/auth";
import { dateTime, humanize, money, num, pct } from "../lib/format";

export function TransactionPage() {
  const id = Number(useParams().id);
  const { can } = useAuth();
  const allowed = can(ANALYSTS);
  const q = useQuery({
    queryKey: ["transaction", id],
    queryFn: () => api<TransactionDetail>(`/transactions/${id}`),
    enabled: allowed,
  });
  if (!allowed) return <p className="text-sm text-slate-600">Transaction detail is available to analysts.</p>;
  if (q.isLoading) return <Loading />;
  if (q.error || !q.data) return <ErrorBox error={q.error} />;
  const t = q.data;
  const s = t.score;
  const features = Object.entries(s?.features ?? {}).filter(([k, v]) => v !== null && !(/^(cat|ch|em)_/.test(k) && v === 0));
  return (
    <>
      <PageHeader
        title={`${money(t.amount)} at ${t.merchant.name}`}
        subtitle={`${t.external_id} · ${dateTime(t.occurred_at)} · card •••• ${t.card.last4}`}
      />
      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <Card title="Current assessment" actions={<><ScoreBar score={s?.score} /><BandBadge band={t.risk_band} /></>}>
            {s ? (
              <>
                <p className="mb-3 text-sm">
                  Recommended action: <strong>{s.recommended_action}</strong>
                  {s.model_band && s.model_band !== s.risk_band && (
                    <span className="text-slate-600"> (model said {s.model_band}; a rule raised it)</span>
                  )}
                </p>
                <ReasonList reasons={s.reasons} />
              </>
            ) : (
              <Empty>Not scored.</Empty>
            )}
          </Card>
          {features.length > 0 && (
            <Card title="Model inputs (as computed at scoring time)">
              <div className="grid grid-cols-2 gap-x-6 text-xs md:grid-cols-3">
                {features.map(([k, v]) => (
                  <div key={k} className="flex justify-between border-b border-slate-100 py-1">
                    <span className="text-slate-600">{k}</span>
                    <span className="tabular-nums">{num(v, 3)}</span>
                  </div>
                ))}
              </div>
            </Card>
          )}
          <Card title="Scoring history">
            <Table>
              <thead><tr><th>Scored</th><th>Model</th><th>Rules</th><th>Score</th><th>Band</th><th>Action</th></tr></thead>
              <tbody>
                {t.score_history.map((h) => (
                  <tr key={h.id}>
                    <td className="text-xs">{dateTime(h.scored_at)}</td>
                    <td className="text-xs">{h.model ? `v${h.model.version} ${humanize(h.model.algorithm)}` : "none"}</td>
                    <td className="text-xs">v{h.rule_set_version}</td>
                    <td><ScoreBar score={h.score} /></td>
                    <td><BandBadge band={h.risk_band} /></td>
                    <td className="text-xs">{h.recommended_action}</td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Card>
        </div>
        <div className="space-y-6">
          <Card title="Details">
            <dl className="grid grid-cols-2 gap-y-1 text-sm">
              <dt className="text-slate-500">Channel</dt><dd>{t.channel} / {t.entry_mode}</dd>
              <dt className="text-slate-500">Merchant</dt>
              <dd>{humanize(t.merchant.category)} · {t.merchant.city ?? "online"}, {t.merchant.country}</dd>
              <dt className="text-slate-500">IP country</dt><dd>{t.ip_country ?? "—"}</dd>
              <dt className="text-slate-500">Device</dt><dd>{t.has_device ? "fingerprint provided" : "—"}</dd>
              <dt className="text-slate-500">Source</dt><dd>{t.source}</dd>
              {s?.anomaly_score !== null && s?.anomaly_score !== undefined && (
                <><dt className="text-slate-500">Anomaly</dt><dd>{pct(s.anomaly_score)} percentile</dd></>
              )}
            </dl>
          </Card>
          <Card title="Labels">
            {t.labels.length ? (
              <ul className="space-y-1 text-sm">
                {t.labels.map((l, i) => (
                  <li key={i}><strong>{l.label}</strong> · {humanize(l.source)} · {dateTime(l.reported_at)}</li>
                ))}
              </ul>
            ) : (
              <Empty>No chargeback or analyst label yet.</Empty>
            )}
          </Card>
          <Card title="Cases">
            {t.cases.length ? (
              <ul className="space-y-1 text-sm">
                {t.cases.map((c) => (
                  <li key={c.id} className="flex items-center gap-2">
                    <Link to={`/cases/${c.id}`} className="text-sky-700 hover:underline">#{c.id}</Link>
                    <StatusBadge status={c.status} />
                    {c.disposition && humanize(c.disposition)}
                  </li>
                ))}
              </ul>
            ) : (
              <Empty>Not part of a case.</Empty>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}
