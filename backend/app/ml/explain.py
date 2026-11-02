"""Turn SHAP contributions into analyst-readable reasons.

SHAP values are in log-odds: positive values push the fraud risk up, negative
values push it down. Contributions are **grouped by signal family** (velocity,
amount, device, …) because features within a family are correlated and a model
can give them large offsetting values; the family total is what the evidence says.

The sentences come from fixed templates filled in with the transaction's actual
values. They are deterministic, not written by an AI model.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from app.ml.features import FAMILY_OF

FAMILY_LABEL = {
    "velocity": "Transaction velocity",
    "amount": "Amount",
    "device": "Device",
    "location": "Location",
    "time": "Time of day",
    "merchant": "Merchant",
    "channel": "Channel",
    "account": "Account history",
}


def _ok(v: float | None) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def describe(feature: str, v: float | None, f: Mapping[str, float]) -> str | None:
    """A sentence for one feature value, or None if there is nothing meaningful to say."""
    if feature.startswith("cat_"):
        return f"Merchant category: {feature[4:].replace('_', ' ')}" if v == 1 else None
    if feature.startswith("ch_"):
        return f"Channel: {feature[3:]}" if v == 1 else None
    if not _ok(v):
        return None
    assert v is not None
    # Lambdas: only the matching template is evaluated (others may not make sense for this value).
    templates: dict[str, Callable[[float], str]] = {
        "cnt_10m": lambda v: f"{v:.0f} earlier transactions on this card in the previous 10 minutes",
        "cnt_1h": lambda v: f"{v:.0f} earlier transactions on this card in the previous hour",
        "cnt_24h": lambda v: f"{v:.0f} earlier transactions on this card in the previous 24 hours",
        "sum_1h": lambda v: f"${v:,.2f} spent on this card in the previous hour",
        "sum_24h": lambda v: f"${v:,.2f} spent on this card in the previous 24 hours",
        "small_cnt_10m": lambda v: f"{v:.0f} tiny (< $5) transactions in the previous 10 minutes",
        "log_secs_since_prev": lambda v: f"{_duration(math.expm1(v))} since this card's previous transaction",
        "amount": lambda v: f"Amount ${v:,.2f}",
        "log_amount": lambda v: f"Amount ${math.expm1(v):,.2f}",
        "amount_z": lambda v: (
            f"Amount is {abs(v):.1f} standard deviations {'above' if v >= 0 else 'below'} this card's usual"
        ),
        "amount_ratio_max": lambda v: f"Amount is {v:.1f}× this card's largest purchase in the last 90 days",
        "new_device": lambda v: (
            "Device not seen with this card in the last 90 days" if v == 1 else "Device used with this card before"
        ),
        "device_age_days": lambda v: (
            "Device in use for 90+ days" if v >= 90 else f"Device first seen {v:,.0f} days ago"
        ),
        "has_device": lambda v: "A device fingerprint was provided" if v == 1 else "No device involved (in person)",
        "km_from_home": lambda v: f"{v:,.0f} km from the cardholder's home city",
        "speed_kmh": lambda v: f"Implied travel speed {v:,.0f} km/h since the previous in-person transaction",
        "ip_mismatch": lambda v: (
            "IP country differs from the cardholder's home country" if v == 1 else "IP country matches home country"
        ),
        "foreign_merchant": lambda v: (
            "Merchant is outside the cardholder's home country" if v == 1 else "Merchant is in the home country"
        ),
        "hour_familiarity": lambda v: (
            f"{v:.0%} of this card's transactions in the last 90 days were at this time of day"
        ),
        "is_night": lambda v: "Made between midnight and 6 am (UTC)" if v == 1 else "Made during the day",
        "merchant_fraud_rate": lambda v: f"Merchant's reported fraud rate so far: {v:.2%}",
        "em_swipe": lambda v: (
            "Card magnetic stripe was swiped (no chip)" if v == 1 else "Chip, contactless or online entry"
        ),
        "em_manual": lambda v: "Card number keyed in manually" if v == 1 else "Card number not keyed in",
        "card_age_days": lambda v: f"Card issued {v:,.0f} days ago",
        "new_payee": lambda v: "Transfer to a payee not seen in the last 90 days" if v == 1 else "Known payee",
        "cnt_90d": lambda v: f"{v:.0f} transactions on this card in the last 90 days",
    }
    t = templates.get(feature)
    return t(v) if t else None


def _duration(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f} seconds"
    if seconds < 7200:
        return f"{seconds / 60:.0f} minutes"
    if seconds < 172_800:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.0f} days"


def top_reasons(
    feature_names: Sequence[str], values: Sequence[float], contributions: Sequence[float], k: int = 4
) -> list[dict[str, Any]]:
    """Top families by absolute total contribution, each with a sentence from its strongest
    same-direction feature that has a meaningful value."""
    f = dict(zip(feature_names, values, strict=True))
    groups: dict[str, list[int]] = {}
    for i, name in enumerate(feature_names):
        groups.setdefault(FAMILY_OF.get(name, "other"), []).append(i)
    totals = {g: sum(contributions[i] for i in idx) for g, idx in groups.items()}
    out: list[dict[str, Any]] = []
    for g in sorted(totals, key=lambda g: abs(totals[g]), reverse=True):
        total = totals[g]
        if len(out) >= k or abs(total) < 0.05:
            break
        candidates = sorted(
            (i for i in groups[g] if contributions[i] * total > 0), key=lambda i: abs(contributions[i]), reverse=True
        )
        text = None
        lead = None
        for i in candidates:
            text = describe(feature_names[i], values[i], f)
            if text:
                lead = i
                break
        if text is None or lead is None:
            continue
        v = values[lead]
        out.append(
            {
                "source": "model",
                "signal": g,
                "label": FAMILY_LABEL.get(g, g),
                "feature": feature_names[lead],
                "value": round(float(v), 4) if _ok(v) else None,
                "contribution": round(float(total), 4),
                "effect": "increases risk" if total > 0 else "decreases risk",
                "text": text,
            }
        )
    return out
