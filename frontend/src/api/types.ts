export type Role = "admin" | "lead_analyst" | "analyst" | "auditor";
export type Band = "low" | "medium" | "high" | "critical";
export type CaseStatus = "open" | "in_review" | "escalated" | "closed";
export type Disposition = "confirmed_fraud" | "legitimate" | "inconclusive";

export interface User {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  created_at: string;
}

export interface Reason {
  source: "model" | "rule";
  text: string;
  signal?: string;
  label?: string;
  feature?: string;
  value?: number | null;
  contribution?: number;
  effect?: string;
  rule?: string;
  min_band?: Band;
}

export interface ModelRef {
  id?: number;
  name: string;
  version: number;
  algorithm: string;
}

export interface ScoreView {
  id: number;
  score: number | null;
  risk_band: Band;
  model_band: Band | null;
  anomaly_score: number | null;
  recommended_action: "allow" | "review";
  sampled_for_review: boolean;
  reasons: Reason[];
  rules_fired: Reason[];
  features: Record<string, number | null> | null;
  rule_set_version: number | null;
  model: ModelRef | null;
  scored_at: string;
  latency_ms: number | null;
}

export interface TransactionRow {
  id: number;
  external_id: string;
  occurred_at: string;
  amount: number;
  currency: string;
  channel: string;
  entry_mode: string;
  ip_country: string | null;
  lat: number | null;
  lon: number | null;
  has_device: boolean;
  is_transfer: boolean;
  merchant: { id: number; external_ref: string; name: string; category: string; country: string; city: string | null; is_online: boolean };
  card: { id: number; last4: string };
  risk_score: number | null;
  risk_band: Band | null;
  case_id: number | null;
  source: string;
  score?: ScoreView | null;
}

export interface TransactionDetail extends TransactionRow {
  score: ScoreView | null;
  score_history: ScoreView[];
  labels: { label: string; source: string; reported_at: string; case_id: number | null }[];
  cases: { id: number; status: CaseStatus; priority: number; disposition: Disposition | null }[];
}

export interface CaseSummary {
  id: number;
  status: CaseStatus;
  priority: number;
  max_band: Band;
  reason: string;
  card: { id: number; last4: string | null };
  assignee: { id: string; email: string; full_name: string } | null;
  disposition: Disposition | null;
  opened_at: string;
  sla_due_at: string;
  overdue: boolean;
  closed_at: string | null;
  transactions: number;
  total_amount: number;
}

export interface CaseDetail extends CaseSummary {
  customer: { external_ref: string; home_city: string | null; home_country: string; home_lat: number | null; home_lon: number | null };
  card_detail: { id: number; last4: string; network: string; issued_at: string };
  flagged: TransactionRow[];
  history: TransactionRow[];
  notes: { id: number; author: string | null; body: string; created_at: string }[];
  events: { event: string; actor: string | null; from_status: string | null; to_status: string | null; details: Record<string, unknown>; ts: string }[];
  closed_by: string | null;
}

export interface Page<T> {
  items: T[];
  next_cursor?: number | null;
  total?: number;
}

export interface Summary {
  as_of: string | null;
  last_24h: { transactions: number; amount: number; flagged: number; critical: number; medium: number };
  cases: { open: number; unassigned: number; overdue: number; escalated: number; critical_open: number };
  outcomes_30d: Record<string, number>;
  analyst_confirmed_rate_30d: number | null;
  fraud_reported_30d: { transactions: number; amount: number };
}

export interface Trends {
  as_of: string | null;
  days: number;
  daily: { day: string; transactions: number; flagged: number; reported_fraud: number; fraud_amount: number }[];
  by_category: { category: string; transactions: number; flagged: number; reported_fraud: number }[];
  by_channel: { channel: string; transactions: number; flagged: number; reported_fraud: number }[];
  bands: Record<string, number>;
}

export interface ModelSummary {
  id: number;
  name: string;
  version: number;
  algorithm: string;
  is_active: boolean;
  trained_at: string;
  activated_at: string | null;
  thresholds: { medium: number; high: number; critical: number; capacity_per_day: number };
  calibration_pr_auc: number | null;
  data: Record<string, unknown> | null;
}

export interface ModelDetail extends ModelSummary {
  metrics: {
    cross_validation: Record<string, { pr_auc_mean: number; pr_auc_std: number; folds: number[] }>;
    algorithm_choice: string;
    calibration_window: { pr_auc: number | null; roc_auc: number | null; best_f1: number | null; note: string };
    global_importance: { feature: string; mean_abs_shap: number }[];
    data: Record<string, unknown>;
  };
  feature_names: string[];
  monitored_features: string[];
}

export interface DriftReport {
  id: number;
  model_version_id: number;
  window_start: string;
  window_end: string;
  n_transactions: number;
  feature_psi: Record<string, number>;
  score_psi: number | null;
  status: "ok" | "warning" | "alert";
  created_at: string;
}

export interface Performance {
  maturity_days: number;
  weeks: {
    week_start: string;
    mature: boolean;
    transactions: number;
    flagged: number;
    reported_fraud: number;
    alert_precision: number | null;
    recall: number | null;
    dollar_recall: number | null;
    pr_auc: number | null;
  }[];
}

export interface RiskSettings {
  review_sample_rate: number;
  threshold_overrides: { medium: number; high: number; critical: number } | null;
  capacity_per_day: number;
  sla_hours: Record<string, number>;
}

export interface RuleItem {
  code: string;
  enabled: boolean;
  min_band: Band;
  params: Record<string, number | string[]>;
  description: string;
}

export interface RulesResponse {
  active_version: number;
  rules: RuleItem[];
  versions: { version: number; comment: string; created_at: string; is_active: boolean }[];
}

export interface ApiKey {
  id: string;
  name: string;
  key_prefix: string;
  scopes: string[];
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  key?: string;
}

export interface SystemInfo {
  streaming_enabled: boolean;
  active_model: { id: number; name: string; version: number; algorithm: string; trained_at: string } | null;
  rule_set_version: number;
  decision_policy: string;
}

export interface Job {
  id: number;
  type: string;
  status: "queued" | "running" | "succeeded" | "failed";
  attempts: number;
  result: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface AuditEntry {
  id: number;
  ts: string;
  user: string | null;
  action: string;
  entity: string;
  entity_id: string | null;
  details: Record<string, unknown>;
}
