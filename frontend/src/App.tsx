import type { ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import type { Role } from "./api/types";
import { Layout } from "./components/Layout";
import { Loading } from "./components/ui";
import { ADMINS, AUDITING, LEADS, useAuth } from "./lib/auth";
import { AdminPage } from "./pages/AdminPage";
import { AuditPage } from "./pages/AuditPage";
import { CasePage } from "./pages/CasePage";
import { CasesPage } from "./pages/CasesPage";
import { DashboardPage } from "./pages/DashboardPage";
import { LoginPage } from "./pages/LoginPage";
import { ModelsPage } from "./pages/ModelsPage";
import { MonitoringPage } from "./pages/MonitoringPage";
import { TransactionPage } from "./pages/TransactionPage";
import { TransactionsPage } from "./pages/TransactionsPage";

function RequireRole({ roles, children }: { roles: Role[]; children: ReactNode }) {
  const { can } = useAuth();
  return can(roles) ? <>{children}</> : <p className="text-sm text-slate-600">You don't have access to this page.</p>;
}

export function App() {
  const { user, loading } = useAuth();
  if (loading) return <Loading />;
  if (!user) return <Routes><Route path="*" element={<LoginPage />} /></Routes>;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DashboardPage />} />
        <Route path="cases" element={<CasesPage />} />
        <Route path="cases/:id" element={<CasePage />} />
        <Route path="transactions" element={<TransactionsPage />} />
        <Route path="transactions/:id" element={<TransactionPage />} />
        <Route path="monitoring" element={<RequireRole roles={LEADS}><MonitoringPage /></RequireRole>} />
        <Route path="models" element={<ModelsPage />} />
        <Route path="admin" element={<RequireRole roles={ADMINS}><AdminPage /></RequireRole>} />
        <Route path="audit" element={<RequireRole roles={AUDITING}><AuditPage /></RequireRole>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
