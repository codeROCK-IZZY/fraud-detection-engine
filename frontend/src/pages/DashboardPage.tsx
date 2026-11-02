import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api/client";
import type { Summary, Trends } from "../api/types";
import { Card, Empty, ErrorBox, Loading, PageHeader, Stat, Table } from "../components/ui";
import { dateTime, humanize, money, num, pct } from "../lib/format";

const shortDay = (iso: string) => new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });

export function DashboardPage() {
  const summary = useQuery({ queryKey: ["summary"], queryFn: () => api<Summary>("/analytics/summary") });
  const trends = useQuery({ queryKey: ["trends", 30], queryFn: () => api<Trends>("/analytics/trends?days=30") });
  if (summary.isLoading) return <Loading />;
  if (summary.error) return <ErrorBox error={summary.error} />;
  const s = summary.data;
  if (!s?.as_of) return <Empty>No transactions yet. Load the demo data or send transactions to the API.</Empty>;

  const bandTotal = Object.values(trends.data?.bands ?? {}).reduce((a, b) => a + b, 0) || 1;
  return (
    <>
      <PageHeader title="Overview" subtitle={`Activity up to ${dateTime(s.as_of)}`} />
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Transactions (24 h)" value={num(s.last_24h.transactions)} sub={money(s.last_24h.amount)} />
        <Stat label="Flagged for review (24 h)" value={num(s.last_24h.flagged)}
          sub={`${pct(s.last_24h.flagged / Math.max(s.last_24h.transactions, 1), 2)} of volume · ${s.last_24h.critical} critical`} />
        <Stat label="Open cases" value={<Link to="/cases" className="hover:underline">{num(s.cases.open)}</Link>}
          sub={`${s.cases.unassigned} unassigned · ${s.cases.overdue} past SLA · ${s.cases.escalated} escalated`} />
        <Stat label="Analyst-confirmed fraud (30 d)" value={pct(s.analyst_confirmed_rate_30d)}
          sub={`of decided cases · ${num(s.fraud_reported_30d.transactions)} frauds reported (${money(s.fraud_reported_30d.amount)})`} />
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-3">
        <Card title="Daily volume and flags (30 days)" className="lg:col-span-2">
          {trends.data?.daily.length ? (
            <div className="h-72" role="img" aria-label="Daily transactions, flagged transactions and reported fraud">
              <ResponsiveContainer>
                <LineChart data={trends.data.daily}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="day" tickFormatter={shortDay} fontSize={12} />
                  <YAxis yAxisId="left" fontSize={12} />
                  <YAxis yAxisId="right" orientation="right" fontSize={12} />
                  <Tooltip labelFormatter={(v) => shortDay(String(v))} />
                  <Legend />
                  <Line yAxisId="left" dataKey="transactions" name="Transactions" stroke="#0369a1" dot={false} />
                  <Line yAxisId="right" dataKey="flagged" name="Flagged" stroke="#dc2626" dot={false} />
                  <Line yAxisId="right" dataKey="reported_fraud" name="Reported fraud (so far)" stroke="#7c3aed" dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          ) : <Empty>No data.</Empty>}
          <p className="mt-2 text-xs text-slate-500">
            Reported fraud for recent days is incomplete: chargebacks arrive days to weeks later.
          </p>
        </Card>
        <Card title="Risk bands (30 days)">
          <ul className="space-y-2 text-sm">
            {(["low", "medium", "high", "critical", "unscored"] as const).map((b) => (
              <li key={b} className="flex items-center justify-between">
                <span className="capitalize">{b}</span>
                <span className="tabular-nums text-slate-700">
                  {num(trends.data?.bands[b] ?? 0)} <span className="text-xs text-slate-500">({pct((trends.data?.bands[b] ?? 0) / bandTotal, 2)})</span>
                </span>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <Card title="Flags and reported fraud by merchant category (30 days)" className="mt-6">
        {trends.data?.by_category.length ? (
          <div className="grid gap-6 lg:grid-cols-2">
            <div className="h-64" role="img" aria-label="Flagged and reported fraud by category">
              <ResponsiveContainer>
                <BarChart data={trends.data.by_category.slice(0, 8)} layout="vertical" margin={{ left: 40 }}>
                  <XAxis type="number" fontSize={12} />
                  <YAxis type="category" dataKey="category" tickFormatter={humanize} fontSize={12} width={90} />
                  <Tooltip />
                  <Legend />
                  <Bar dataKey="flagged" name="Flagged" fill="#f87171" />
                  <Bar dataKey="reported_fraud" name="Reported fraud" fill="#8b5cf6" />
                </BarChart>
              </ResponsiveContainer>
            </div>
            <Table>
              <thead><tr><th>Category</th><th>Transactions</th><th>Flagged</th><th>Reported fraud</th></tr></thead>
              <tbody>
                {trends.data.by_category.map((c) => (
                  <tr key={c.category}>
                    <td className="capitalize">{humanize(c.category)}</td>
                    <td className="tabular-nums">{num(c.transactions)}</td>
                    <td className="tabular-nums">{num(c.flagged)}</td>
                    <td className="tabular-nums">{num(c.reported_fraud)}</td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
        ) : <Empty>No data.</Empty>}
      </Card>
    </>
  );
}
