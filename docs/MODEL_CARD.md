# Model card — `fraud-txn`

## Intended use
Rank card and account transactions by fraud risk so a team of human analysts can review the riskiest
ones first. Output is a calibrated probability, a band (low/medium/high/critical), reasons, and an
advisory action (`allow` / `review`).

**Not intended for:** declining payments, blocking accounts, judging people, credit or eligibility
decisions, or any use on real data without re-training, re-validation and a fairness review.

## Model
- Gradient-boosted trees (XGBoost) or logistic regression: whichever has the better 5-fold
  cross-validated PR-AUC (grouped by card) on the training window. XGBoost won in the reference evaluation.
- Platt-calibrated on a later, untouched window. Training uses all frauds plus a 25% weighted sample of
  legitimate transactions.
- 42 features in 8 families: velocity, amount vs. the card's own history, device novelty, location
  (distance from home, implied travel speed, IP mismatch), time-of-day familiarity, merchant
  (category, point-in-time fraud rate), channel/entry mode, account history. All are computed from the
  card's last 90 days only, strictly before the transaction.
- Separate unsupervised Isolation Forest anomaly score, shown to analysts, not mixed into the risk score.
- Deterministic rules run alongside and can only raise a band.

## Data
Synthetic only (see [ml/README.md](../ml/README.md)): 2,000 customers, one year, ~0.3% fraud across five
scenarios, delayed and incomplete labels, label noise, and a drift scenario in the last two months.
**No protected attributes exist in the data.** Customers have only a pseudonymous reference and a home
location. Fraud countries in the simulator are uniformly random, so the model learns "differs from the
customer's home", not anything about specific countries.

## Evaluation
Out-of-time: trained on data more than 60 days before a deployment date, tested on the three months
after it against full ground truth. Metrics: PR-AUC (primary), ROC-AUC, precision/recall/F1 at the
operating thresholds, recall and dollar-recall at analyst capacity, Brier score, reliability table,
per-scenario recall, weekly performance, PSI drift.

Full results: [ml/reports/evaluation.md](../ml/reports/evaluation.md). Headline numbers are repeated in
the README; all come from that report.

## Thresholds
`high` is set so the expected number of reviews per day matches analyst capacity (default 25/day).
`critical` = probability ≥ 0.5. Admins can override thresholds; overrides are audit-logged.

## Known weaknesses
- A fraud pattern that looks like the customer's normal behaviour (`device_compromise`: own device, own
  country, moderate amounts) is caught far less often, and the anomaly score does not help with it.
  Monitoring flags the shift (merchant-category mix PSI), which is the cue to collect labels and retrain.
- Under-calibrated against full ground truth because ~10% of fraud is never reported and some is
  reported late. The model learns what gets reported.
- Analyst feedback is biased toward flagged transactions; a 0.1% random review sample mitigates this.
- Synthetic results do not transfer to real data.

## Responsible use
Scores are risk indicators about *transactions*. Every decision is made and recorded by an authorised
analyst who sees the reasons (labelled *Model* or *Rule*) and can disagree. No automated adverse action.
