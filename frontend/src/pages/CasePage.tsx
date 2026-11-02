import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { CaseDetail, Disposition, TransactionRow, User } from "../api/types";
import { BandBadge, ReasonList, ScoreBar, StatusBadge } from "../components/fraud";
import { Button, Card, Empty, ErrorBox, Field, Loading, PageHeader, Select, Table } from "../components/ui";
import { ANALYSTS, LEADS, useAuth } from "../lib/auth";
import { date, dateTime, humanize, money, num, pct, PRIORITY_LABEL, slaText } from "../lib/format";

export function CasePage() {
  const id = Number(useParams().id);
  const qc = useQueryClient();
  const { user, can } = useAuth();
  const c = useQuery({ queryKey: ["case", id], queryFn: () => api<CaseDetail>(`/cases/${id}`) });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["case", id] });
    void qc.invalidateQueries({ queryKey: ["cases"] });
  };
  const action = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => api(`/cases/${id}/${path}`, { method: "POST", body }),
    onSuccess: refresh,
  });

  if (c.isLoading) return <Loading />;
  if (c.error || !c.data) return <ErrorBox error={c.error} />;
  const k = c.data;
  const isLead = can(LEADS);
  const mine = k.assignee?.id === user?.id;
  const canWork = can(ANALYSTS) && k.status !== "closed" && (mine || isLead);

  return (
    <>
      <PageHeader
        title={`Case #${k.id} · card •••• ${k.card.last4}`}
        subtitle={<span className="flex flex-wrap items-center gap-2">
          <StatusBadge status={k.status} /> <BandBadge band={k.max_band} />
          <span>{PRIORITY_LABEL[k.priority]}</span> ·
          <span className={clsx(k.overdue && "font-semibold text-red-700")}>
            {k.status === "closed" ? `closed ${dateTime(k.closed_at)} by ${k.closed_by}` : `SLA ${slaText(k.sla_due_at)}`}
          </span> ·
          <span>{k.assignee ? `assigned to ${k.assignee.email}` : "unassigned"}</span>
        </span>}
        actions={<Link to="/cases" className="text-sm text-sky-700 hover:underline">← Queue</Link>}
      />
      <ErrorBox error={action.error} />

      {k.status !== "closed" && can(ANALYSTS) && (
        <div className="mb-6 flex flex-wrap items-end gap-2" aria-label="Case actions">
          {k.status === "open" && <Button onClick={() => action.mutate({ path: "claim" })} busy={action.isPending}>Claim case</Button>}
          {canWork && k.status === "in_review" && (
            <>
              <Button variant="secondary" onClick={() => action.mutate({ path: "status", body: { to: "escalated" } })}>Escalate to lead</Button>
              <Button variant="secondary" onClick={() => action.mutate({ path: "status", body: { to: "open" } })}>Release to queue</Button>
            </>
          )}
          {isLead && k.status === "escalated" && (
            <Button variant="secondary" onClick={() => action.mutate({ path: "status", body: { to: "in_review" } })}>Return to analyst</Button>
          )}
          {isLead && <AssignControl onAssign={(assignee_id) => action.mutate({ path: "assign", body: { assignee_id } })} />}
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <Card title={`Flagged transactions (${k.flagged.length})`}>
            <ul className="divide-y divide-slate-100">
              {k.flagged.map((t) => <FlaggedTxn key={t.id} t={t} />)}
            </ul>
          </Card>
          <Card title="Card activity around the flagged transactions">
            <HistoryTable rows={k.history} flagged={new Set(k.flagged.map((t) => t.id))} />
          </Card>
        </div>
        <div className="space-y-6">
          <Card title="Cardholder (pseudonymous)">
            <dl className="grid grid-cols-2 gap-y-1 text-sm">
              <dt className="text-slate-500">Customer</dt><dd>{k.customer.external_ref}</dd>
              <dt className="text-slate-500">Home</dt><dd>{k.customer.home_city ?? "—"}, {k.customer.home_country}</dd>
              <dt className="text-slate-500">Card</dt><dd className="capitalize">{k.card_detail.network} •••• {k.card_detail.last4}</dd>
              <dt className="text-slate-500">Issued</dt><dd>{date(k.card_detail.issued_at)}</dd>
            </dl>
          </Card>
          <Card title="Locations">
            <LocationPlot home={k.customer} rows={k.history} flagged={new Set(k.flagged.map((t) => t.id))} />
          </Card>
          {canWork && k.status !== "open" && (
            <DispositionForm caseDetail={k} isLead={isLead} busy={action.isPending}
              onSubmit={(body) => action.mutate({ path: "disposition", body })} />
          )}
          <Notes k={k} canWrite={canWork} onAdded={refresh} />
          <Card title="Timeline">
            <ol className="space-y-2 text-sm">
              {k.events.map((e, i) => (
                <li key={i} className="flex gap-2">
                  <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-slate-400" />
                  <span><span className="font-medium">{humanize(e.event)}</span>
                    {e.to_status && e.from_status !== e.to_status && <span className="text-slate-600"> → {humanize(e.to_status)}</span>}
                    <span className="block text-xs text-slate-500">{dateTime(e.ts)}{e.actor ? ` · ${e.actor}` : " · system"}</span>
                  </span>
                </li>
              ))}
            </ol>
          </Card>
        </div>
      </div>
    </>
  );
}

function FlaggedTxn({ t }: { t: TransactionRow }) {
  const s = t.score;
  return (
    <li className="py-4 first:pt-0 last:pb-0" data-testid="flagged-txn">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="font-medium">
            {money(t.amount)} · {t.merchant.name}
            <span className="ml-2 text-xs font-normal text-slate-500">{humanize(t.merchant.category)} · {t.merchant.country}</span>
          </p>
          <p className="text-xs text-slate-500">
            {dateTime(t.occurred_at)} · {t.channel} / {t.entry_mode}
            {t.ip_country && ` · IP ${t.ip_country}`}{t.is_transfer && " · transfer"}
            {" · "}<Link to={`/transactions/${t.id}`} className="text-sky-700 hover:underline">{t.external_id}</Link>
          </p>
        </div>
        <div className="flex items-center gap-3"><ScoreBar score={s?.score} /><BandBadge band={t.risk_band} /></div>
      </div>
      {s && (
        <div className="mt-3">
          <ReasonList reasons={s.reasons} />
          <p className="mt-2 text-xs text-slate-500">
            {s.model ? `Model v${s.model.version} (${humanize(s.model.algorithm)})` : "No model (rules only)"} · rule set v{s.rule_set_version}
            {s.anomaly_score !== null && ` · behaviour anomaly percentile ${pct(s.anomaly_score)}`}
            {s.sampled_for_review && " · random quality-control sample"}
          </p>
        </div>
      )}
    </li>
  );
}

function HistoryTable({ rows, flagged }: { rows: TransactionRow[]; flagged: Set<number> }) {
  if (!rows.length) return <Empty>No other activity.</Empty>;
  return (
    <Table>
      <thead><tr><th>Time</th><th>Merchant</th><th>Amount</th><th>Channel</th><th>IP</th><th>Band</th></tr></thead>
      <tbody>
        {rows.map((t) => (
          <tr key={t.id} className={clsx(flagged.has(t.id) && "bg-red-50")}>
            <td className="whitespace-nowrap text-xs">{dateTime(t.occurred_at)}</td>
            <td className="text-xs">{t.merchant.name}<span className="block text-slate-500">{humanize(t.merchant.category)} · {t.merchant.city ?? t.merchant.country}</span></td>
            <td className="tabular-nums">{money(t.amount)}</td>
            <td className="text-xs">{t.channel}{t.entry_mode === "swipe" && " (swipe)"}</td>
            <td className="text-xs">{t.ip_country ?? "—"}</td>
            <td><BandBadge band={t.risk_band} /></td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

/** Equirectangular plot of in-person transactions: no map tiles, so no third-party requests. */
function LocationPlot({ home, rows, flagged }: {
  home: CaseDetail["customer"]; rows: TransactionRow[]; flagged: Set<number>;
}) {
  const pts = rows.filter((r) => r.lat !== null && r.lon !== null).slice().reverse();
  if (!pts.length) return <Empty>No in-person transactions with a location.</Empty>;
  const x = (lon: number) => ((lon + 180) / 360) * 320;
  const y = (lat: number) => ((90 - lat) / 180) * 160;
  return (
    <figure>
      <svg viewBox="0 0 320 160" className="w-full rounded bg-sky-50" role="img"
        aria-label="Positions of in-person transactions relative to the cardholder's home">
        {[-60, -30, 0, 30, 60].map((lat) => <line key={lat} x1={0} x2={320} y1={y(lat)} y2={y(lat)} stroke="#e2e8f0" />)}
        {[-120, -60, 0, 60, 120].map((lon) => <line key={lon} y1={0} y2={160} x1={x(lon)} x2={x(lon)} stroke="#e2e8f0" />)}
        <polyline fill="none" stroke="#94a3b8" strokeWidth={0.6}
          points={pts.map((p) => `${x(p.lon!)},${y(p.lat!)}`).join(" ")} />
        {home.home_lat !== null && home.home_lon !== null && (
          <rect x={x(home.home_lon) - 3} y={y(home.home_lat) - 3} width={6} height={6} fill="#0369a1"><title>Home</title></rect>
        )}
        {pts.map((p) => (
          <circle key={p.id} cx={x(p.lon!)} cy={y(p.lat!)} r={flagged.has(p.id) ? 3.5 : 2}
            fill={flagged.has(p.id) ? "#dc2626" : "#64748b"}>
            <title>{`${p.merchant.city ?? ""} ${dateTime(p.occurred_at)} ${money(p.amount)}`}</title>
          </circle>
        ))}
      </svg>
      <figcaption className="mt-1 text-xs text-slate-500">■ home · <span className="text-red-600">●</span> flagged · ● other in-person</figcaption>
    </figure>
  );
}

function AssignControl({ onAssign }: { onAssign: (id: string) => void }) {
  const users = useQuery({ queryKey: ["directory"], queryFn: () => api<User[]>("/users/directory") });
  const [target, setTarget] = useState("");
  return (
    <span className="flex items-end gap-2">
      <Field label="Assign to">
        <Select value={target} onChange={(e) => setTarget(e.target.value)} aria-label="Assignee">
          <option value="">Choose…</option>
          {users.data?.map((u) => <option key={u.id} value={u.id}>{u.email}</option>)}
        </Select>
      </Field>
      <Button variant="secondary" disabled={!target} onClick={() => onAssign(target)}>Assign</Button>
    </span>
  );
}

function DispositionForm({ caseDetail, isLead, busy, onSubmit }: {
  caseDetail: CaseDetail; isLead: boolean; busy: boolean; onSubmit: (body: unknown) => void;
}) {
  const [disposition, setDisposition] = useState<Disposition>("confirmed_fraud");
  const [note, setNote] = useState("");
  const [overrides, setOverrides] = useState<Record<number, string>>({});
  if (caseDetail.status === "escalated" && !isLead) {
    return <Card title="Close case"><p className="text-sm text-slate-600">Escalated cases are closed by a lead.</p></Card>;
  }
  function submit(e: FormEvent) {
    e.preventDefault();
    const labels = Object.fromEntries(Object.entries(overrides).filter(([, v]) => v));
    onSubmit({ disposition, note, transaction_labels: Object.keys(labels).length ? labels : undefined });
  }
  return (
    <Card title="Close case">
      <form onSubmit={submit} className="space-y-3">
        <Field label="Decision">
          <Select value={disposition} onChange={(e) => setDisposition(e.target.value as Disposition)}>
            <option value="confirmed_fraud">Confirmed fraud</option>
            <option value="legitimate">Legitimate (customer confirmed)</option>
            <option value="inconclusive">Inconclusive</option>
          </Select>
        </Field>
        {caseDetail.flagged.length > 1 && (
          <details className="text-sm">
            <summary className="cursor-pointer text-slate-700">Label transactions individually</summary>
            <ul className="mt-2 space-y-1">
              {caseDetail.flagged.map((t) => (
                <li key={t.id} className="flex items-center justify-between gap-2 text-xs">
                  <span>{money(t.amount)} · {t.merchant.name}</span>
                  <Select className="w-36" value={overrides[t.id] ?? ""}
                    onChange={(e) => setOverrides({ ...overrides, [t.id]: e.target.value })}>
                    <option value="">follow decision</option>
                    <option value="fraud">fraud</option>
                    <option value="legitimate">legitimate</option>
                  </Select>
                </li>
              ))}
            </ul>
          </details>
        )}
        <Field label="Reasoning" hint="Kept with the case. Refer to cards by last 4 digits only.">
          <textarea required minLength={10} rows={3} value={note} onChange={(e) => setNote(e.target.value)}
            className="w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm" />
        </Field>
        <p className="text-xs text-slate-500">Labels from this decision are used the next time the model is retrained ({num(caseDetail.flagged.length)} transactions).</p>
        <Button type="submit" busy={busy} className="w-full">Close case</Button>
      </form>
    </Card>
  );
}

function Notes({ k, canWrite, onAdded }: { k: CaseDetail; canWrite: boolean; onAdded: () => void }) {
  const [body, setBody] = useState("");
  const add = useMutation({
    mutationFn: () => api(`/cases/${k.id}/notes`, { body: { body } }),
    onSuccess: () => { setBody(""); onAdded(); },
  });
  return (
    <Card title={`Notes (${k.notes.length})`}>
      <ul className="space-y-3 text-sm">
        {k.notes.map((n) => (
          <li key={n.id}><p className="whitespace-pre-wrap">{n.body}</p>
            <p className="text-xs text-slate-500">{n.author} · {dateTime(n.created_at)}</p></li>
        ))}
      </ul>
      {canWrite && (
        <form className="mt-3 space-y-2" onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
          <textarea aria-label="New note" rows={2} value={body} onChange={(e) => setBody(e.target.value)} required
            className="w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm" placeholder="Add a note (notes cannot be edited later)" />
          <ErrorBox error={add.error} />
          <Button type="submit" variant="secondary" busy={add.isPending}>Add note</Button>
        </form>
      )}
    </Card>
  );
}
