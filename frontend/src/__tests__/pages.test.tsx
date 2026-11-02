import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { setAccessToken } from "../api/client";
import { AuthProvider } from "../lib/auth";
import { CasePage } from "../pages/CasePage";
import { LoginPage } from "../pages/LoginPage";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function wrap(ui: ReactNode, path = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <AuthProvider>{ui}</AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const CASE = {
  id: 7, status: "open", priority: 1, max_band: "critical", reason: "critical", card: { id: 3, last4: "4242" },
  assignee: null, disposition: null, opened_at: "2026-11-01T10:00:00Z", sla_due_at: "2099-01-01T00:00:00Z",
  overdue: false, closed_at: null, transactions: 1, total_amount: 899,
  customer: { external_ref: "CUST-000001", home_city: "Boston", home_country: "US", home_lat: 42.36, home_lon: -71.06 },
  card_detail: { id: 3, last4: "4242", network: "visa", issued_at: "2023-01-01T00:00:00Z" },
  flagged: [{
    id: 99, external_id: "TXN-99", occurred_at: "2026-11-01T09:00:00Z", amount: 899, currency: "USD", channel: "ecommerce",
    entry_mode: "online", ip_country: "ZZ", lat: null, lon: null, has_device: true, is_transfer: false,
    merchant: { id: 1, external_ref: "M-1", name: "TV Online 1", category: "electronics", country: "US", city: null, is_online: true },
    card: { id: 3, last4: "4242" }, risk_score: 0.81, risk_band: "critical", case_id: 7, source: "api",
    score: { id: 1, score: 0.81, risk_band: "critical", model_band: "critical", anomaly_score: 0.97, recommended_action: "review",
      sampled_for_review: false, rules_fired: [], features: null, rule_set_version: 1, scored_at: "2026-11-01T09:00:01Z", latency_ms: 12,
      model: { id: 1, name: "fraud-txn", version: 2, algorithm: "xgboost" },
      reasons: [{ source: "model", text: "Device not seen with this card in the last 90 days", contribution: 0.9, label: "Device" }] },
  }],
  history: [], notes: [], events: [{ event: "opened", actor: null, from_status: null, to_status: "open", details: {}, ts: "2026-11-01T10:00:00Z" }],
  closed_by: null,
};

describe("pages", () => {
  afterEach(() => { vi.restoreAllMocks(); setAccessToken(null); });

  it("shows the login error from the API", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) =>
      String(url).endsWith("/auth/refresh") ? json({ detail: "no" }, 401) : json({ detail: "Invalid email or password" }, 401));
    wrap(<LoginPage />);
    await userEvent.type(screen.getByLabelText("Email"), "x@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid email or password");
  });

  it("renders a case with explanations and offers claim only to analysts", async () => {
    const me = { id: "u1", email: "auditor@example.com", full_name: "", role: "auditor", is_active: true, created_at: "" };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      const u = String(url);
      if (u.endsWith("/auth/refresh")) return json({ access_token: "t" });
      if (u.endsWith("/auth/me")) return json(me);
      if (u.endsWith("/cases/7")) return json(CASE);
      return json({}, 404);
    });
    wrap(<Routes><Route path="/cases/:id" element={<CasePage />} /></Routes>, "/cases/7");
    expect(await screen.findByText(/Case #7/)).toBeInTheDocument();
    expect(screen.getByText(/Device not seen with this card/)).toBeInTheDocument();
    expect(screen.getByText(/Model v2/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Claim case" })).not.toBeInTheDocument());
  });
});
