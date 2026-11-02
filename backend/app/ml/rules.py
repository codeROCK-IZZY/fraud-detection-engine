"""Deterministic fraud rules.

Rules sit beside the model, not inside it. Each rule can only *raise* a
transaction's risk band (to its ``min_band``), never lower it, and every rule
that fires is reported with ``source: "rule"`` so analysts can tell rules from
model output.

The rule *logic* lives here in code (reviewed and tested). Which rules are
enabled, their thresholds and the merchant blocklist are *data*: a versioned
``rule_sets`` row that admins edit through the API. Every score records the rule
set version that produced it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

BAND_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
BANDS = ("low", "medium", "high", "critical")


@dataclass(frozen=True)
class RuleSpec:
    code: str
    description: str
    default_params: dict[str, Any]
    default_band: str
    check: Callable[[Mapping[str, float | None], Mapping[str, Any], dict[str, Any]], str | None]


def _num(f: Mapping[str, float | None], k: str) -> float:
    v = f.get(k)
    return float("nan") if v is None else float(v)


def _velocity(f: Mapping[str, float | None], _: Mapping[str, Any], p: dict[str, Any]) -> str | None:
    n = _num(f, "cnt_10m")
    if n >= p["min_count_10m"]:
        return f"{n:.0f} earlier transactions on this card in the last 10 minutes (rule threshold {p['min_count_10m']})"
    return None


def _card_testing(f: Mapping[str, float | None], _: Mapping[str, Any], p: dict[str, Any]) -> str | None:
    small, amount = _num(f, "small_cnt_10m"), _num(f, "amount")
    if small >= p["min_small_10m"] and amount >= p["min_amount"]:
        return f"${amount:,.2f} purchase right after {small:.0f} tiny (< $5) transactions in 10 minutes"
    return None


def _impossible_travel(f: Mapping[str, float | None], _: Mapping[str, Any], p: dict[str, Any]) -> str | None:
    speed = _num(f, "speed_kmh")
    if speed >= p["min_speed_kmh"]:
        return f"Implied travel speed of {speed:,.0f} km/h since the previous in-person transaction"
    return None


def _new_device_amount(f: Mapping[str, float | None], _: Mapping[str, Any], p: dict[str, Any]) -> str | None:
    if _num(f, "new_device") == 1 and _num(f, "amount") >= p["min_amount"]:
        return f"${_num(f, 'amount'):,.2f} from a device not seen with this card in the last 90 days"
    return None


def _new_payee_transfer(f: Mapping[str, float | None], _: Mapping[str, Any], p: dict[str, Any]) -> str | None:
    if _num(f, "new_payee") == 1 and _num(f, "amount") >= p["min_amount"]:
        return f"${_num(f, 'amount'):,.2f} transfer to a payee not seen in the last 90 days"
    return None


def _blocked_merchant(_: Mapping[str, float | None], ctx: Mapping[str, Any], p: dict[str, Any]) -> str | None:
    if ctx.get("merchant_ref") in set(p.get("merchant_refs", [])):
        return f"Merchant {ctx.get('merchant_ref')} is on the blocklist"
    return None


RULES: dict[str, RuleSpec] = {
    r.code: r
    for r in (
        RuleSpec(
            "VELOCITY_BURST", "Many transactions on one card within minutes", {"min_count_10m": 5}, "high", _velocity
        ),
        RuleSpec(
            "CARD_TESTING",
            "Large purchase right after a burst of tiny ones",
            {"min_small_10m": 3, "min_amount": 100},
            "high",
            _card_testing,
        ),
        RuleSpec(
            "IMPOSSIBLE_TRAVEL",
            "In-person transactions too far apart to travel between",
            {"min_speed_kmh": 900},
            "high",
            _impossible_travel,
        ),
        RuleSpec(
            "NEW_DEVICE_HIGH_AMOUNT",
            "High amount from an unfamiliar device",
            {"min_amount": 1000},
            "medium",
            _new_device_amount,
        ),
        RuleSpec(
            "NEW_PAYEE_LARGE_TRANSFER",
            "Large transfer to a new payee",
            {"min_amount": 2000},
            "medium",
            _new_payee_transfer,
        ),
        RuleSpec(
            "BLOCKED_MERCHANT", "Merchant on the admin blocklist", {"merchant_refs": []}, "critical", _blocked_merchant
        ),
    )
}


def default_rule_config() -> list[dict[str, Any]]:
    return [
        {"code": r.code, "enabled": True, "min_band": r.default_band, "params": dict(r.default_params)}
        for r in RULES.values()
    ]


def validate_rule_config(config: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Checks codes, bands and parameter names/types; returns a normalised copy."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in config:
        code = item.get("code")
        if code not in RULES:
            raise ValueError(f"unknown rule code {code!r}")
        if code in seen:
            raise ValueError(f"rule {code} listed twice")
        seen.add(code)
        band = item.get("min_band", RULES[code].default_band)
        if band not in BAND_ORDER or band == "low":
            raise ValueError(f"rule {code}: min_band must be medium, high or critical")
        params = {**RULES[code].default_params, **(item.get("params") or {})}
        unknown = set(params) - set(RULES[code].default_params)
        if unknown:
            raise ValueError(f"rule {code}: unknown parameters {sorted(unknown)}")
        for k, default in RULES[code].default_params.items():
            if isinstance(default, list):
                if not isinstance(params[k], list) or not all(isinstance(x, str) for x in params[k]):
                    raise ValueError(f"rule {code}: {k} must be a list of strings")
            elif not isinstance(params[k], int | float) or params[k] < 0:
                raise ValueError(f"rule {code}: {k} must be a non-negative number")
        out.append({"code": code, "enabled": bool(item.get("enabled", True)), "min_band": band, "params": params})
    return out


def evaluate(
    features: Mapping[str, float | None], context: Mapping[str, Any], config: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rules that fire for one transaction: ``[{source, rule, min_band, text}]``."""
    fired: list[dict[str, Any]] = []
    for item in config:
        if not item.get("enabled", True):
            continue
        spec = RULES[item["code"]]
        text = spec.check(features, context, item["params"])
        if text:
            fired.append({"source": "rule", "rule": spec.code, "min_band": item["min_band"], "text": text})
    return fired


def floor_band(fired: list[dict[str, Any]]) -> str:
    """The highest ``min_band`` among fired rules (``low`` if none)."""
    return max((f["min_band"] for f in fired), key=lambda b: BAND_ORDER[b], default="low")


def max_band(*bands: str | None) -> str:
    return max((b for b in bands if b), key=lambda b: BAND_ORDER[b], default="low")
