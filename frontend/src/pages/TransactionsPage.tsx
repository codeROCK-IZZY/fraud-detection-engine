import { useInfiniteQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Band, Page, TransactionRow } from "../api/types";
import { BandBadge, ScoreBar } from "../components/fraud";
import { Button, Card, Empty, ErrorBox, Field, Input, Loading, PageHeader, Select, Table } from "../components/ui";
import { dateTime, humanize, money } from "../lib/format";

interface Filters {
  band: "" | Band | "flagged";
  channel: string;
  merchant_ref: string;
  customer_ref: string;
  external_id: string;
  min_amount: string;
}
const EMPTY: Filters = { band: "flagged", channel: "", merchant_ref: "", customer_ref: "", external_id: "", min_amount: "" };

function toQuery(f: Filters, cursor?: number) {
  const p = new URLSearchParams({ limit: "50" });
  if (f.band === "flagged") {
    p.append("band", "high");
    p.append("band", "critical");
  } else if (f.band) p.append("band", f.band);
  for (const k of ["channel", "merchant_ref", "customer_ref", "external_id", "min_amount"] as const) if (f[k]) p.set(k, f[k]);
  if (cursor) p.set("before_id", String(cursor));
  return p.toString();
}

export function TransactionsPage() {
  const [draft, setDraft] = useState<Filters>(EMPTY);
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const q = useInfiniteQuery({
    queryKey: ["transactions", filters],
    queryFn: ({ pageParam }) => api<Page<TransactionRow>>(`/transactions?${toQuery(filters, pageParam)}`),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const rows = q.data?.pages.flatMap((p) => p.items) ?? [];
  const set = (k: keyof Filters) => (e: { target: { value: string } }) => setDraft({ ...draft, [k]: e.target.value });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setFilters(draft);
  };

  return (
    <>
      <PageHeader title="Transactions" subtitle="Newest first" />
      <Card className="mb-6">
        <form onSubmit={submit} className="grid grid-cols-2 gap-3 md:grid-cols-7 md:items-end">
          <Field label="Risk band">
            <Select value={draft.band} onChange={set("band")}>
              <option value="flagged">High + critical</option>
              <option value="">Any</option>
              <option value="critical">Critical</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
            </Select>
          </Field>
          <Field label="Channel">
            <Select value={draft.channel} onChange={set("channel")}>
              <option value="">Any</option>
              <option value="pos">In person</option>
              <option value="ecommerce">Online</option>
              <option value="atm">ATM</option>
              <option value="transfer">Transfer</option>
            </Select>
          </Field>
          <Field label="Min amount"><Input type="number" min={0} value={draft.min_amount} onChange={set("min_amount")} /></Field>
          <Field label="Merchant ref"><Input value={draft.merchant_ref} onChange={set("merchant_ref")} /></Field>
          <Field label="Customer ref"><Input value={draft.customer_ref} onChange={set("customer_ref")} /></Field>
          <Field label="Transaction id"><Input value={draft.external_id} onChange={set("external_id")} /></Field>
          <Button type="submit">Search</Button>
        </form>
      </Card>
      <Card>
        {q.isLoading ? <Loading /> : q.error ? <ErrorBox error={q.error} /> : !rows.length ? <Empty>No matching transactions.</Empty> : (
          <>
            <Table>
              <thead>
                <tr><th>Time</th><th>Transaction</th><th>Merchant</th><th>Amount</th><th>Channel</th><th>Card</th><th>Score</th><th>Band</th><th>Case</th></tr>
              </thead>
              <tbody>
                {rows.map((t) => (
                  <tr key={t.id}>
                    <td className="whitespace-nowrap text-xs">{dateTime(t.occurred_at)}</td>
                    <td className="text-xs"><Link to={`/transactions/${t.id}`} className="text-sky-700 hover:underline">{t.external_id}</Link></td>
                    <td className="text-xs">{t.merchant.name}<span className="block text-slate-500">{humanize(t.merchant.category)}</span></td>
                    <td className="tabular-nums">{money(t.amount)}</td>
                    <td className="text-xs">{t.channel}</td>
                    <td className="tabular-nums text-xs">•••• {t.card.last4}</td>
                    <td><ScoreBar score={t.risk_score} /></td>
                    <td><BandBadge band={t.risk_band} /></td>
                    <td className="text-xs">{t.case_id ? <Link to={`/cases/${t.case_id}`} className="text-sky-700 hover:underline">#{t.case_id}</Link> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </Table>
            {q.hasNextPage && (
              <div className="mt-4 text-center">
                <Button variant="secondary" busy={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>Load more</Button>
              </div>
            )}
          </>
        )}
      </Card>
    </>
  );
}
