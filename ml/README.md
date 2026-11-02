# Synthetic data and evaluation

**No real customer, card or banking data is used anywhere in this project.** All data comes from
`backend/app/ml/synthetic.py`, which is seeded and deterministic.

## The simulated world

| Entity | How it is generated |
|---|---|
| Customers | 33 world cities, weighted toward the US. Each has a home city, activity rate (log-normal, ~1.2 transactions/day), spend scale, category preferences, a daily rhythm, 1–2 devices, 0–3 known transfer payees. No names or demographics. |
| Cards | One per customer, tokenised (`tok_…`). ~4% are issued during the simulated year. |
| Merchants | ~950: local merchants per city per category, plus online merchants (75% domestic). |
| Legitimate behaviour | Mostly favourite local merchants; online shopping from own devices; ~1.5 trips/year to other cities (hard negatives: far from home but genuine); 25% change phone once; 2% of online transactions come from a VPN/foreign IP; 8% of transfers go to a new payee. |
| Drift | From `drift_start_day` (≈ month 11), 35% of customers start digital-wallet top-ups, a new legitimate behaviour. |

## Fraud scenarios

| Scenario | Rate (episodes per 1,000 customers per year) | Signature |
|---|---|---|
| `card_testing` | 60 | 3–8 tiny ($0.50–$3) online authorisations within minutes, then 1–2 large electronics/gift-card purchases |
| `cnp_stolen` | 150 | 1–5 online purchases at 2–6× the customer's typical amount, unfamiliar device (85%), foreign IP (65%) |
| `account_takeover` | 60 | 1–3 transfers of $500–$4,000 to new payees, unfamiliar device (90%) |
| `cloned_card` | 70 | 2–6 in-person purchases in another country (often magstripe "swipe") while the real card keeps being used at home |
| `device_compromise` | 300 (only after drift start) | Moderate digital-wallet top-ups from the customer's *own* device and country: deliberately hard, and never seen in training |

Fraud devices come from a shared pool of 40 (a fraud ring). Fraud IP and cloned-card countries are drawn
uniformly at random, so the data can teach "different from home" but never "country X is risky".

## Labels

- 90% of fraud episodes are eventually reported, all transactions of an episode at once, with a
  log-normal delay (median 4–18 days by scenario, capped at 60). The other 10% are never reported.
- In card testing, the tiny transactions are reported only half the time (customers notice the big ones).
- 0.03% of legitimate online transactions are wrongly disputed (label noise).
- The database only receives labels reported by "now"; later ones stay unknown, as in production.

## Reproducing the evaluation

```bash
cd backend
python -m app.cli evaluate --out ../ml/reports            # 2,000 customers, 1 year, seed 42
python -m app.cli evaluate --customers 500 --out /tmp/r    # quicker
```

`reports/evaluation.json` holds every number; `reports/evaluation.md` is the readable version.
These results measure how learnable the simulator's patterns are. They are **not** evidence of
performance on real transactions.
