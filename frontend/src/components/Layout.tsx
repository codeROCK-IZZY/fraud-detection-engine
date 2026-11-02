import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { NavLink, Outlet } from "react-router-dom";
import { api } from "../api/client";
import type { Role, SystemInfo } from "../api/types";
import { ADMINS, AUDITING, LEADS, STAFF, useAuth } from "../lib/auth";
import { humanize } from "../lib/format";
import { PolicyNote } from "./fraud";

const NAV: { to: string; label: string; roles: Role[] }[] = [
  { to: "/", label: "Overview", roles: STAFF },
  { to: "/cases", label: "Case queue", roles: STAFF },
  { to: "/transactions", label: "Transactions", roles: STAFF },
  { to: "/monitoring", label: "Monitoring", roles: LEADS },
  { to: "/models", label: "Models", roles: STAFF },
  { to: "/admin", label: "Administration", roles: ADMINS },
  { to: "/audit", label: "Audit log", roles: AUDITING },
];

export function Layout() {
  const { user, logout, can } = useAuth();
  const info = useQuery({ queryKey: ["system-info"], queryFn: () => api<SystemInfo>("/system/info"), staleTime: 60_000 });
  return (
    <div className="min-h-screen md:flex">
      <aside className="border-b border-slate-200 bg-slate-900 text-slate-200 md:min-h-screen md:w-60 md:border-b-0">
        <div className="px-5 py-4">
          <p className="text-sm font-semibold text-white">Fraud Detection Engine</p>
          <p className="text-xs text-slate-400">Investigation console</p>
        </div>
        <nav aria-label="Main" className="flex gap-1 overflow-x-auto px-3 pb-3 md:flex-col md:pb-0">
          {NAV.filter((n) => can(n.roles)).map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"}
              className={({ isActive }) => clsx("whitespace-nowrap rounded-md px-3 py-2 text-sm",
                isActive ? "bg-slate-700 text-white" : "text-slate-300 hover:bg-slate-800 hover:text-white")}>
              {n.label}
            </NavLink>
          ))}
        </nav>
        {info.data && (
          <dl className="hidden space-y-1 px-5 py-4 text-xs text-slate-400 md:block">
            <div>Model: <span className="text-slate-200">{info.data.active_model ? `v${info.data.active_model.version} · ${humanize(info.data.active_model.algorithm)}` : "none (rules only)"}</span></div>
            <div>Rule set: <span className="text-slate-200">v{info.data.rule_set_version}</span></div>
            <div>Streaming: <span className="text-slate-200">{info.data.streaming_enabled ? "on" : "off"}</span></div>
          </dl>
        )}
      </aside>
      <div className="min-w-0 flex-1">
        <header className="flex items-center justify-end gap-3 border-b border-slate-200 bg-white px-6 py-3 text-sm">
          <span className="text-slate-600">{user?.email}</span>
          <span className="rounded bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700">{humanize(user?.role)}</span>
          <button onClick={() => void logout()} className="text-sky-700 hover:underline">Sign out</button>
        </header>
        <main className="mx-auto max-w-7xl p-6">
          <Outlet />
          <footer className="mt-10 border-t border-slate-200 pt-4"><PolicyNote /></footer>
        </main>
      </div>
    </div>
  );
}
