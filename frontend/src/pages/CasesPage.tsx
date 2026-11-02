import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { CaseSummary, Page } from "../api/types";
import { BandBadge, StatusBadge } from "../components/fraud";
import { Button, Card, Empty, ErrorBox, Loading, PageHeader, Table } from "../components/ui";
import { ANALYSTS, useAuth } from "../lib/auth";
import { dateTime, money, PRIORITY_LABEL, slaText } from "../lib/format";

const VIEWS = [
  { key: "mine", label: "My cases", query: "assignee=me&status=in_review&status=escalated" },
  { key: "unassigned", label: "Unassigned", query: "assignee=unassigned&status=open" },
  { key: "open", label: "All open", query: "status=open&status=in_review&status=escalated" },
  { key: "escalated", label: "Escalated", query: "status=escalated" },
  { key: "closed", label: "Closed", query: "status=closed" },
] as const;

export function CasesPage() {
  const { can } = useAuth();
  const [view, setView] = useState<(typeof VIEWS)[number]["key"]>(can(ANALYSTS) ? "unassigned" : "open");
  const q = VIEWS.find((v) => v.key === view)!.query;
  const qc = useQueryClient();
  const navigate = useNavigate();
  const cases = useQuery({ queryKey: ["cases", q], queryFn: () => api<Page<CaseSummary>>(`/cases?${q}&limit=100`) });
  const claim = useMutation({
    mutationFn: (id: number) => api(`/cases/${id}/claim`, { method: "POST" }),
    onSuccess: (_, id) => {
      void qc.invalidateQueries({ queryKey: ["cases"] });
      navigate(`/cases/${id}`);
    },
  });

  return (
    <>
      <PageHeader title="Case queue" subtitle="One case per card. Sorted by priority, then SLA deadline." />
      <div role="tablist" className="mb-4 flex flex-wrap gap-1">
        {VIEWS.filter((v) => v.key !== "mine" || can(ANALYSTS)).map((v) => (
          <button key={v.key} role="tab" aria-selected={view === v.key} onClick={() => setView(v.key)}
            className={clsx("rounded-md px-3 py-1.5 text-sm",
              view === v.key ? "bg-slate-900 text-white" : "bg-white text-slate-700 ring-1 ring-slate-200 hover:bg-slate-50")}>
            {v.label}
          </button>
        ))}
      </div>
      <ErrorBox error={claim.error} />
      <Card>
        {cases.isLoading ? <Loading /> : cases.error ? <ErrorBox error={cases.error} /> : !cases.data?.items.length ? (
          <Empty>No cases in this view.</Empty>
        ) : (
          <Table>
            <thead>
              <tr><th>Case</th><th>Priority</th><th>Band</th><th>Status</th><th>Card</th><th>Txns</th><th>Amount</th>
                <th>SLA</th><th>Assignee</th><th /></tr>
            </thead>
            <tbody>
              {cases.data.items.map((c) => (
                <tr key={c.id} data-testid="case-row">
                  <td><Link to={`/cases/${c.id}`} className="font-medium text-sky-700 hover:underline">#{c.id}</Link>
                    <div className="text-xs text-slate-500">{dateTime(c.opened_at)}</div></td>
                  <td className="whitespace-nowrap text-xs">{PRIORITY_LABEL[c.priority]}</td>
                  <td><BandBadge band={c.max_band} /></td>
                  <td><StatusBadge status={c.status} /></td>
                  <td className="tabular-nums">•••• {c.card.last4}</td>
                  <td className="tabular-nums">{c.transactions}</td>
                  <td className="tabular-nums">{money(c.total_amount)}</td>
                  <td className={clsx("whitespace-nowrap text-xs", c.overdue ? "font-semibold text-red-700" : "text-slate-600")}>
                    {c.status === "closed" ? c.disposition?.replace("_", " ") : slaText(c.sla_due_at)}
                  </td>
                  <td className="text-xs">{c.assignee?.email ?? "—"}</td>
                  <td>
                    {c.status === "open" && !c.assignee && can(ANALYSTS) && (
                      <Button variant="secondary" busy={claim.isPending && claim.variables === c.id}
                        onClick={() => claim.mutate(c.id)}>Claim</Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        {cases.data?.total !== undefined && cases.data.total > cases.data.items.length && (
          <p className="mt-3 text-xs text-slate-500">Showing {cases.data.items.length} of {cases.data.total}.</p>
        )}
      </Card>
    </>
  );
}
