import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api/client";
import type { ApiKey, Role, RiskSettings, RulesResponse, User } from "../api/types";
import { Badge, Button, Card, ErrorBox, Field, Input, Loading, PageHeader, Select, Table } from "../components/ui";
import { dateTime, humanize } from "../lib/format";

export function AdminPage() {
  return (
    <>
      <PageHeader title="Administration" subtitle="Every change here is written to the audit log." />
      <div className="space-y-6">
        <UsersCard />
        <RiskSettingsCard />
        <RulesCard />
        <ApiKeysCard />
        <CsvImportCard />
      </div>
    </>
  );
}

function UsersCard() {
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<User[]>("/users") });
  const [form, setForm] = useState({ email: "", full_name: "", password: "", role: "analyst" as Role });
  const create = useMutation({
    mutationFn: () => api("/users", { body: form }),
    onSuccess: () => { setForm({ ...form, email: "", full_name: "", password: "" }); void qc.invalidateQueries({ queryKey: ["users"] }); },
  });
  const toggle = useMutation({
    mutationFn: (u: User) => api(`/users/${u.id}`, { method: "PATCH", body: { is_active: !u.is_active } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["users"] }),
  });
  return (
    <Card title="Users">
      {users.isLoading ? <Loading /> : (
        <Table>
          <thead><tr><th>Email</th><th>Name</th><th>Role</th><th>Status</th><th /></tr></thead>
          <tbody>{users.data?.map((u) => (
            <tr key={u.id}><td>{u.email}</td><td>{u.full_name}</td><td>{humanize(u.role)}</td>
              <td>{u.is_active ? <Badge tone="green">active</Badge> : <Badge>inactive</Badge>}</td>
              <td><Button variant="secondary" onClick={() => toggle.mutate(u)}>{u.is_active ? "Deactivate" : "Reactivate"}</Button></td></tr>))}
          </tbody>
        </Table>
      )}
      <ErrorBox error={toggle.error} />
      <form className="mt-4 grid gap-3 md:grid-cols-5 md:items-end" onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
        <Field label="Email"><Input type="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></Field>
        <Field label="Name"><Input value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} /></Field>
        <Field label="Initial password" hint="At least 12 characters">
          <Input type="password" minLength={12} required autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
        </Field>
        <Field label="Role">
          <Select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
            <option value="analyst">Analyst</option><option value="lead_analyst">Lead analyst</option>
            <option value="auditor">Auditor</option><option value="admin">Admin</option>
          </Select>
        </Field>
        <Button type="submit" busy={create.isPending}>Add user</Button>
      </form>
      <ErrorBox error={create.error} />
    </Card>
  );
}

function RiskSettingsCard() {
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["risk-settings"], queryFn: () => api<RiskSettings>("/settings/risk") });
  const [draft, setDraft] = useState<RiskSettings | null>(null);
  useEffect(() => { if (s.data) setDraft(s.data); }, [s.data]);
  const save = useMutation({
    mutationFn: (body: RiskSettings) => api<RiskSettings>("/settings/risk", { method: "PUT", body }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["risk-settings"] }),
  });
  if (!draft) return <Card title="Risk settings"><Loading /></Card>;
  const override = draft.threshold_overrides;
  return (
    <Card title="Risk settings">
      <form className="grid gap-4 md:grid-cols-3" onSubmit={(e: FormEvent) => { e.preventDefault(); save.mutate(draft); }}>
        <Field label="Random review sample" hint="Share of low/medium transactions sent for review (unbiased labels).">
          <Input type="number" step="0.0001" min={0} max={0.05} value={draft.review_sample_rate}
            onChange={(e) => setDraft({ ...draft, review_sample_rate: Number(e.target.value) })} />
        </Field>
        <Field label="Analyst capacity (reviews/day)" hint="Used the next time a model is trained.">
          <Input type="number" min={1} value={draft.capacity_per_day} onChange={(e) => setDraft({ ...draft, capacity_per_day: Number(e.target.value) })} />
        </Field>
        <Field label="SLA hours (P1 / P2 / P3)">
          <div className="flex gap-2">{(["1", "2", "3"] as const).map((p) => (
            <Input key={p} type="number" min={1} aria-label={`SLA hours P${p}`} value={draft.sla_hours[p]}
              onChange={(e) => setDraft({ ...draft, sla_hours: { ...draft.sla_hours, [p]: Number(e.target.value) } })} />))}
          </div>
        </Field>
        <fieldset className="md:col-span-3">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={!!override}
              onChange={(e) => setDraft({ ...draft, threshold_overrides: e.target.checked ? { medium: 0.05, high: 0.2, critical: 0.5 } : null })} />
            Override the model's band thresholds (otherwise they come from analyst capacity at training time)
          </label>
          {override && (
            <div className="mt-2 grid gap-3 md:grid-cols-3">
              {(["medium", "high", "critical"] as const).map((b) => (
                <Field key={b} label={`${b} ≥`}><Input type="number" step="0.001" min={0} max={1} value={override[b]}
                  onChange={(e) => setDraft({ ...draft, threshold_overrides: { ...override, [b]: Number(e.target.value) } })} /></Field>
              ))}
            </div>
          )}
        </fieldset>
        <div><Button type="submit" busy={save.isPending}>Save settings</Button></div>
      </form>
      <ErrorBox error={save.error} />
    </Card>
  );
}

function RulesCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["rules"], queryFn: () => api<RulesResponse>("/rules") });
  const [draft, setDraft] = useState<RulesResponse["rules"] | null>(null);
  const [comment, setComment] = useState("");
  useEffect(() => { if (q.data) setDraft(q.data.rules); }, [q.data]);
  const save = useMutation({
    mutationFn: () => api("/rules", { method: "PUT", body: { rules: draft?.map((r) => ({ code: r.code, enabled: r.enabled, min_band: r.min_band, params: r.params })), comment } }),
    onSuccess: () => { setComment(""); void qc.invalidateQueries({ queryKey: ["rules"] }); void qc.invalidateQueries({ queryKey: ["system-info"] }); },
  });
  if (!draft || !q.data) return <Card title="Rules"><Loading /></Card>;
  const update = (i: number, patch: Partial<RulesResponse["rules"][number]>) => setDraft(draft.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <Card title={`Deterministic rules (active version ${q.data.active_version})`}>
      <p className="mb-3 text-sm text-slate-600">Rules can only raise a transaction's band. Saving creates a new version; each score records the version used.</p>
      <Table>
        <thead><tr><th>On</th><th>Rule</th><th>Minimum band</th><th>Parameters</th></tr></thead>
        <tbody>{draft.map((r, i) => (
          <tr key={r.code}>
            <td><input type="checkbox" aria-label={`Enable ${r.code}`} checked={r.enabled} onChange={(e) => update(i, { enabled: e.target.checked })} /></td>
            <td><span className="font-mono text-xs">{r.code}</span><span className="block text-xs text-slate-500">{r.description}</span></td>
            <td><Select value={r.min_band} onChange={(e) => update(i, { min_band: e.target.value as typeof r.min_band })}>
              <option value="medium">medium</option><option value="high">high</option><option value="critical">critical</option></Select></td>
            <td className="space-y-1">{Object.entries(r.params).map(([k, v]) => (
              <label key={k} className="flex items-center gap-2 text-xs"><span className="w-28 text-slate-600">{humanize(k)}</span>
                {Array.isArray(v) ? (
                  <Input value={v.join(", ")} placeholder="comma-separated merchant refs"
                    onChange={(e) => update(i, { params: { ...r.params, [k]: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) } })} />
                ) : (
                  <Input type="number" min={0} value={v} onChange={(e) => update(i, { params: { ...r.params, [k]: Number(e.target.value) } })} />
                )}</label>))}
            </td>
          </tr>))}
        </tbody>
      </Table>
      <form className="mt-4 flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <Field label="Change note"><Input required minLength={3} value={comment} onChange={(e) => setComment(e.target.value)} /></Field>
        <Button type="submit" busy={save.isPending}>Save as new version</Button>
      </form>
      <ErrorBox error={save.error} />
    </Card>
  );
}

function ApiKeysCard() {
  const qc = useQueryClient();
  const keys = useQuery({ queryKey: ["api-keys"], queryFn: () => api<ApiKey[]>("/api-keys") });
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["transactions:write"]);
  const [created, setCreated] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: () => api<ApiKey>("/api-keys", { body: { name, scopes } }),
    onSuccess: (k) => { setCreated(k.key ?? null); setName(""); void qc.invalidateQueries({ queryKey: ["api-keys"] }); },
  });
  const revoke = useMutation({
    mutationFn: (id: string) => api(`/api-keys/${id}`, { method: "DELETE" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["api-keys"] }),
  });
  return (
    <Card title="API keys (machine clients)">
      {created && (
        <div role="alert" className="mb-4 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm">
          <p className="font-medium">Copy this key now. It will not be shown again.</p>
          <code className="mt-1 block break-all text-xs">{created}</code>
        </div>
      )}
      <Table>
        <thead><tr><th>Name</th><th>Prefix</th><th>Scopes</th><th>Last used</th><th>Status</th><th /></tr></thead>
        <tbody>{keys.data?.map((k) => (
          <tr key={k.id}><td>{k.name}</td><td className="font-mono text-xs">{k.key_prefix}…</td><td className="text-xs">{k.scopes.join(", ")}</td>
            <td className="text-xs">{dateTime(k.last_used_at)}</td><td>{k.revoked_at ? <Badge>revoked</Badge> : <Badge tone="green">active</Badge>}</td>
            <td>{!k.revoked_at && <Button variant="danger" onClick={() => revoke.mutate(k.id)}>Revoke</Button>}</td></tr>))}
        </tbody>
      </Table>
      <form className="mt-4 flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
        <Field label="Name"><Input required value={name} onChange={(e) => setName(e.target.value)} /></Field>
        {["transactions:write", "reference:write", "labels:write"].map((s) => (
          <label key={s} className="flex items-center gap-1 text-sm">
            <input type="checkbox" checked={scopes.includes(s)} onChange={(e) => setScopes(e.target.checked ? [...scopes, s] : scopes.filter((x) => x !== s))} />{s}
          </label>
        ))}
        <Button type="submit" busy={create.isPending} disabled={!scopes.length}>Create key</Button>
      </form>
      <ErrorBox error={create.error ?? revoke.error} />
    </Card>
  );
}

function CsvImportCard() {
  const [file, setFile] = useState<File | null>(null);
  const upload = useMutation({
    mutationFn: () => { const form = new FormData(); form.append("file", file!); return api<{ id: string; status: string }>("/ingestion/csv", { form }); },
  });
  const batch = useQuery({
    queryKey: ["batch", upload.data?.id],
    queryFn: () => api<{ status: string; rows_total: number; rows_accepted: number; rows_rejected: number; rows_duplicate: number; errors: { row: number; error: string }[] }>(`/ingestion/batches/${upload.data!.id}`),
    enabled: !!upload.data,
    refetchInterval: (q) => (q.state.data && ["completed", "failed"].includes(q.state.data.status) ? false : 2000),
  });
  return (
    <Card title="Import transactions from CSV">
      <p className="mb-3 text-sm text-slate-600">Same columns as <code>POST /api/v1/transactions</code>. Every row is validated, then scored by the background worker.</p>
      <form className="flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); upload.mutate(); }}>
        <input type="file" accept=".csv" aria-label="CSV file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <Button type="submit" disabled={!file} busy={upload.isPending}>Upload</Button>
      </form>
      <ErrorBox error={upload.error} />
      {batch.data && (
        <div className="mt-3 text-sm" role="status">
          <p>Status: <strong>{batch.data.status}</strong> · {batch.data.rows_accepted} accepted · {batch.data.rows_rejected} rejected · {batch.data.rows_duplicate} duplicates</p>
          {batch.data.errors.length > 0 && (
            <ul className="mt-1 list-disc pl-5 text-xs text-red-800">{batch.data.errors.slice(0, 10).map((e, i) => <li key={i}>Row {e.row}: {e.error}</li>)}</ul>
          )}
        </div>
      )}
    </Card>
  );
}
