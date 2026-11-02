import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { AuditEntry, Page } from "../api/types";
import { Button, Card, Empty, ErrorBox, Field, Input, Loading, PageHeader, Table } from "../components/ui";
import { dateTime } from "../lib/format";

export function AuditPage() {
  const [action, setAction] = useState("");
  const [applied, setApplied] = useState("");
  const q = useInfiniteQuery({
    queryKey: ["audit", applied],
    queryFn: ({ pageParam }) => {
      const p = new URLSearchParams({ limit: "100" });
      if (applied) p.set("action", applied);
      if (pageParam) p.set("before_id", String(pageParam));
      return api<Page<AuditEntry>>(`/audit-log?${p}`);
    },
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const rows = q.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <>
      <PageHeader title="Audit log" subtitle="Append-only: who changed what, and who looked at which case or transaction." />
      <form className="mb-4 flex items-end gap-3" onSubmit={(e) => { e.preventDefault(); setApplied(action); }}>
        <Field label="Action starts with"><Input value={action} placeholder="e.g. case. or model." onChange={(e) => setAction(e.target.value)} /></Field>
        <Button type="submit" variant="secondary">Filter</Button>
      </form>
      <Card>
        {q.isLoading ? <Loading /> : q.error ? <ErrorBox error={q.error} /> : !rows.length ? <Empty>No entries.</Empty> : (
          <>
            <Table>
              <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Entity</th><th>Details</th></tr></thead>
              <tbody>{rows.map((a) => (
                <tr key={a.id}><td className="whitespace-nowrap text-xs">{dateTime(a.ts)}</td><td className="text-xs">{a.user ?? "system"}</td>
                  <td className="font-mono text-xs">{a.action}</td><td className="text-xs">{a.entity} {a.entity_id}</td>
                  <td className="max-w-md truncate text-xs text-slate-600" title={JSON.stringify(a.details)}>{JSON.stringify(a.details)}</td></tr>))}
              </tbody>
            </Table>
            {q.hasNextPage && <div className="mt-4 text-center"><Button variant="secondary" onClick={() => void q.fetchNextPage()}>Load more</Button></div>}
          </>
        )}
      </Card>
    </>
  );
}
