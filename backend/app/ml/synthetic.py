"""Synthetic financial-transaction generator.

**No real customer or banking data is used anywhere in this project.** This
module simulates customers, cards, merchants and devices, a year of legitimate
spending, and injected fraud episodes with realistic signatures. Every parameter
is documented in ``ml/README.md``.

Legitimate behaviour (per customer): home city, activity rate, spend scale,
category preferences, favourite local merchants, a daily rhythm, 1–2 personal
devices (one phone change during the year for some), known transfer payees,
occasional trips to other cities (hard negatives: far from home, but legitimate).

Fraud scenarios, each with a distinct signature:

=====================  ===========================================================
card_testing           burst of tiny online authorisations, then large purchases
cnp_stolen             card-not-present purchases from an unfamiliar device, often
                       a foreign IP, at electronics/gift-card/digital merchants
account_takeover       transfers to new payees from an unfamiliar device
cloned_card            card-present (often magstripe "swipe") purchases in another
                       country while the real card is still used at home
device_compromise      (drift scenario, from ``drift_start_day``) moderate digital
                       wallet top-ups from the customer's *own* device and country
=====================  ===========================================================

Countries for fraud IPs and cloned-card locations are drawn uniformly at random,
so the data can teach "differs from the customer's home" but never "country X is
suspicious".

Labels: fraud is reported with a delay (chargebacks, days to weeks later); some
fraud is never reported; a few legitimate transactions are disputed (label noise).
Ground truth (``is_fraud``, ``scenario``) is kept separately for evaluation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

# (city, country, lat, lon, weight as a customer home)
CITIES: tuple[tuple[str, str, float, float, float], ...] = (
    ("New York", "US", 40.71, -74.01, 10),
    ("Los Angeles", "US", 34.05, -118.24, 8),
    ("Chicago", "US", 41.88, -87.63, 6),
    ("Houston", "US", 29.76, -95.37, 5),
    ("Phoenix", "US", 33.45, -112.07, 4),
    ("Philadelphia", "US", 39.95, -75.17, 4),
    ("San Antonio", "US", 29.42, -98.49, 3),
    ("San Diego", "US", 32.72, -117.16, 3),
    ("Dallas", "US", 32.78, -96.80, 4),
    ("Seattle", "US", 47.61, -122.33, 3),
    ("Denver", "US", 39.74, -104.99, 3),
    ("Boston", "US", 42.36, -71.06, 3),
    ("Atlanta", "US", 33.75, -84.39, 3),
    ("Miami", "US", 25.76, -80.19, 3),
    ("Minneapolis", "US", 44.98, -93.27, 2),
    ("Toronto", "CA", 43.65, -79.38, 3),
    ("Vancouver", "CA", 49.28, -123.12, 2),
    ("London", "GB", 51.51, -0.13, 3),
    ("Manchester", "GB", 53.48, -2.24, 1),
    ("Mexico City", "MX", 19.43, -99.13, 0.5),
    ("Paris", "FR", 48.86, 2.35, 0.5),
    ("Berlin", "DE", 52.52, 13.40, 0.5),
    ("Madrid", "ES", 40.42, -3.70, 0.5),
    ("Rome", "IT", 41.90, 12.50, 0.5),
    ("Amsterdam", "NL", 52.37, 4.90, 0.5),
    ("Sao Paulo", "BR", -23.55, -46.63, 0.5),
    ("Johannesburg", "ZA", -26.20, 28.05, 0.5),
    ("Mumbai", "IN", 19.08, 72.88, 0.5),
    ("Singapore", "SG", 1.35, 103.82, 0.5),
    ("Tokyo", "JP", 35.68, 139.69, 0.5),
    ("Sydney", "AU", -33.87, 151.21, 0.5),
    ("Dubai", "AE", 25.20, 55.27, 0.5),
    ("Seoul", "KR", 37.57, 126.98, 0.5),
)
COUNTRIES: tuple[str, ...] = tuple(sorted({c[1] for c in CITIES}))


@dataclass(frozen=True)
class Category:
    code: str
    mcc: str
    median: float  # median amount (USD) for an average customer
    sigma: float  # log-normal spread
    weight: float  # base share of a customer's transactions
    pos_share: float  # share made in person (rest online)
    local_merchants: int  # merchants per city (card-present)
    online_merchants: int


CATEGORIES: tuple[Category, ...] = (
    Category("grocery", "5411", 45, 0.6, 26, 0.95, 6, 2),
    Category("restaurant", "5812", 28, 0.6, 20, 0.97, 8, 1),
    Category("fuel", "5541", 40, 0.4, 9, 1.0, 3, 0),
    Category("retail", "5311", 60, 0.8, 12, 0.6, 4, 12),
    Category("electronics", "5732", 160, 0.9, 3, 0.4, 2, 8),
    Category("travel", "4511", 340, 0.8, 1.5, 0.05, 1, 6),
    Category("digital_goods", "5815", 15, 0.8, 7, 0.0, 0, 12),
    Category("gift_cards", "5947", 60, 0.6, 1, 0.3, 1, 6),
    Category("utilities", "4900", 90, 0.4, 4, 0.0, 0, 8),
    Category("atm_cash", "6011", 120, 0.5, 4, 1.0, 2, 0),
    Category("digital_wallet", "6540", 80, 0.6, 0, 0.0, 0, 5),  # adopted from drift_start_day
    Category("transfer", "4829", 220, 0.8, 0, 0.0, 0, 1),  # P2P transfers (separate process)
)
CATEGORY_BY_CODE = {c.code: c for c in CATEGORIES}
NS_DAY = 86_400 * 10**9
CATEGORY_CODES: tuple[str, ...] = tuple(c.code for c in CATEGORIES)
SCENARIOS = ("card_testing", "cnp_stolen", "account_takeover", "cloned_card", "device_compromise")


@dataclass(frozen=True)
class SimConfig:
    n_customers: int = 2000
    days: int = 365
    end: datetime = datetime(2026, 11, 1, tzinfo=UTC)
    seed: int = 42
    # Fraud episodes per 1,000 customers per year (device_compromise: per 1,000 per year *while active*).
    episode_rates: dict[str, float] = field(
        default_factory=lambda: {
            "card_testing": 60,
            "cnp_stolen": 150,
            "account_takeover": 60,
            "cloned_card": 70,
            "device_compromise": 300,
        }
    )
    drift_start_day: int | None = 300  # ≈ month 11; None disables the drift scenario
    wallet_adoption: float = 0.35  # share of customers who start digital-wallet top-ups at drift start
    report_prob: float = 0.9  # share of fraud episodes the customer ever reports
    dispute_rate: float = 0.0003  # share of legitimate online transactions wrongly disputed

    @property
    def start(self) -> datetime:
        return self.end - timedelta(days=self.days)


@dataclass
class SimData:
    config: SimConfig
    customers: pd.DataFrame
    cards: pd.DataFrame
    merchants: pd.DataFrame
    transactions: pd.DataFrame  # includes ground truth columns is_fraud, scenario, episode
    labels: pd.DataFrame  # external_id, label, source, reported_at (may be after config.end)

    def summary(self) -> dict[str, object]:
        tx = self.transactions
        return {
            "customers": len(self.customers),
            "merchants": len(self.merchants),
            "transactions": len(tx),
            "fraud_transactions": int(tx.is_fraud.sum()),
            "fraud_rate": round(float(tx.is_fraud.mean()), 5),
            "fraud_by_scenario": {k: int(v) for k, v in tx[tx.is_fraud].scenario.value_counts().items()},
            "fraud_amount_share": round(float(tx.amount[tx.is_fraud].sum() / tx.amount.sum()), 5),
            "labels": len(self.labels),
            "start": self.config.start.isoformat(),
            "end": self.config.end.isoformat(),
            "seed": self.config.seed,
        }


def _fp(rng: np.random.Generator, prefix: str) -> str:
    return prefix + rng.bytes(12).hex()


def _token(i: int, seed: int) -> str:
    return "tok_" + hashlib.sha256(f"card-{seed}-{i}".encode()).hexdigest()[:24]


def _merchants(rng: np.random.Generator) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for cat in CATEGORIES:
        for city, country, lat, lon, _ in CITIES:
            for k in range(cat.local_merchants):
                rows.append(
                    {
                        "external_ref": f"M-{cat.code.upper()}-{city.upper().replace(' ', '')}-{k:02d}",
                        "name": f"{city} {cat.code.replace('_', ' ').title()} {k + 1}",
                        "mcc": cat.mcc,
                        "category": cat.code,
                        "country": country,
                        "city": city,
                        "lat": lat + rng.normal(0, 0.05),
                        "lon": lon + rng.normal(0, 0.05),
                        "is_online": False,
                    }
                )
        for k in range(cat.online_merchants):
            # Most online merchants are domestic (US); some are abroad.
            country = "US" if rng.random() < 0.75 else str(rng.choice(COUNTRIES))
            rows.append(
                {
                    "external_ref": f"M-{cat.code.upper()}-WEB-{k:02d}",
                    "name": f"{cat.code.replace('_', ' ').title()} Online {k + 1}",
                    "mcc": cat.mcc,
                    "category": cat.code,
                    "country": country,
                    "city": None,
                    "lat": None,
                    "lon": None,
                    "is_online": True,
                }
            )
    return pd.DataFrame(rows)


class _Builder:
    """Accumulates transaction rows as column lists (fast to build, one DataFrame at the end)."""

    COLS = (
        "card_idx",
        "merchant_idx",
        "device",
        "amount",
        "channel",
        "entry_mode",
        "payee",
        "ip_country",
        "lat",
        "lon",
        "occurred_at",
        "is_fraud",
        "scenario",
        "episode",
    )

    def __init__(self) -> None:
        self.cols: dict[str, list[object]] = {c: [] for c in self.COLS}

    def add(self, **row: object) -> None:
        for c in self.COLS:
            self.cols[c].append(row.get(c))

    def add_many(self, **cols: object) -> None:
        """Append a block of rows; scalar values are broadcast to the block length."""
        n = len(next(v for v in cols.values() if isinstance(v, np.ndarray)))
        for c in self.COLS:
            v = cols.get(c)
            self.cols[c].extend(v.tolist() if isinstance(v, np.ndarray) else [v] * n)

    def frame(self) -> pd.DataFrame:
        df = pd.DataFrame(self.cols)
        # Whole seconds, like real authorisation timestamps (and PostgreSQL's precision).
        df["occurred_at"] = pd.to_datetime(
            [t.value if isinstance(t, pd.Timestamp) else int(t) for t in df["occurred_at"]], utc=True
        ).floor("s")
        return df


def generate(cfg: SimConfig = SimConfig()) -> SimData:
    rng = np.random.default_rng(cfg.seed)
    start = pd.Timestamp(cfg.start)
    merchants = _merchants(rng)
    m_by_cat_city: dict[tuple[str, str | None], np.ndarray] = {
        (str(cat), None if pd.isna(city) else str(city)): idx.to_numpy()
        for (cat, city), idx in merchants.groupby(["category", merchants.city.fillna("")]).groups.items()
    }
    m_by_cat_city = {(k[0], k[1] or None): v for k, v in m_by_cat_city.items()}
    m_lat = merchants.lat.to_numpy(dtype=float)
    m_lon = merchants.lon.to_numpy(dtype=float)

    def local(cat: str, city: str) -> np.ndarray:
        return m_by_cat_city.get((cat, city), np.array([], dtype=int))

    def online(cat: str) -> np.ndarray:
        return m_by_cat_city.get((cat, None), np.array([], dtype=int))

    city_w = np.array([c[4] for c in CITIES], dtype=float)
    home_idx = rng.choice(len(CITIES), size=cfg.n_customers, p=city_w / city_w.sum())

    customers = pd.DataFrame(
        {
            "external_ref": [f"CUST-{i:06d}" for i in range(cfg.n_customers)],
            "home_city": [CITIES[h][0] for h in home_idx],
            "home_country": [CITIES[h][1] for h in home_idx],
            "home_lat": [CITIES[h][2] for h in home_idx],
            "home_lon": [CITIES[h][3] for h in home_idx],
        }
    )
    issued_days_before = rng.uniform(30, 3000, cfg.n_customers)
    # ~4% of cards are issued during the simulated year (new customers).
    new_mask = rng.random(cfg.n_customers) < 0.04
    issued_days_before[new_mask] = -rng.uniform(0, cfg.days * 0.8, new_mask.sum())
    cards = pd.DataFrame(
        {
            "token": [_token(i, cfg.seed) for i in range(cfg.n_customers)],
            "customer_ref": customers.external_ref,
            "last4": [f"{x:04d}" for x in rng.integers(0, 10_000, cfg.n_customers)],
            "network": rng.choice(["visa", "mastercard", "amex"], cfg.n_customers, p=[0.55, 0.35, 0.10]),
            "issued_at": [(start - pd.Timedelta(days=float(d))).floor("s") for d in issued_days_before],
        }
    )

    b = _Builder()
    base_w = np.array([c.weight for c in CATEGORIES], dtype=float)
    cats_regular = [i for i, c in enumerate(CATEGORIES) if c.weight > 0]
    fraud_device_pool = [_fp(rng, "fd-") for _ in range(40)]
    fraud_payee_pool = [f"PAYEE-X{k:04d}" for k in range(60)]
    profiles: list[dict[str, Any]] = []
    drift_t = start + pd.Timedelta(days=cfg.drift_start_day) if cfg.drift_start_day is not None else None

    start_ns, end_ns = start.value, pd.Timestamp(cfg.end).value
    cat_pos = np.array([c.pos_share for c in CATEGORIES])
    cat_median = np.array([c.median for c in CATEGORIES])
    cat_sigma = np.array([c.sigma for c in CATEGORIES])
    atm_idx = CATEGORY_CODES.index("atm_cash")
    for i in range(cfg.n_customers):
        home = CITIES[home_idx[i]]
        card_start = max(start, cards.issued_at[i])
        active_days = (pd.Timestamp(cfg.end) - card_start).total_seconds() / 86400
        if active_days <= 1:
            profiles.append({})
            continue
        rate = float(np.clip(rng.lognormal(0.1, 0.45), 0.25, 4.0))
        scale = float(rng.lognormal(0, 0.4))
        prefs = rng.dirichlet(base_w[cats_regular] * 2.0)
        peak = float(np.clip(rng.normal(14, 2.5), 9, 20))
        devices = [_fp(rng, "d-")]
        if rng.random() < 0.3:
            devices.append(_fp(rng, "d-"))
        phone_change_ns = (
            card_start.value + int(rng.uniform(0, active_days) * NS_DAY) if rng.random() < 0.25 else end_ns + 1
        )
        new_phone = _fp(rng, "d-")
        payees = [f"PAYEE-{i:06d}-{k}" for k in range(int(rng.integers(0, 4)))]
        transfer_rate = float(rng.uniform(0.02, 0.12)) if payees else 0.0
        favourites = {
            c: rng.choice(m, size=min(len(m), 2), replace=False)
            for c in cats_regular
            if len(m := local(CATEGORIES[c].code, home[0]))
        }
        typical = 50.0 * scale
        profiles.append({"devices": devices, "typical": typical, "payees": payees})
        trips: list[tuple[int, int, int]] = []
        for _ in range(rng.poisson(1.5 * active_days / 365)):
            t0 = card_start.value + int(rng.uniform(0, active_days) * NS_DAY)
            dest = int(rng.choice([k for k in range(len(CITIES)) if k != home_idx[i]]))
            trips.append((t0, t0 + int(rng.uniform(3, 9) * NS_DAY), dest))

        def location(
            tns: np.ndarray, trips: list[tuple[int, int, int]] = trips, h: int = int(home_idx[i])
        ) -> np.ndarray:
            loc = np.full(len(tns), h)
            for t0, t1, dest in trips:
                loc[(tns >= t0) & (tns < t1)] = dest
            return loc

        def device_for(
            tns: np.ndarray, devices: list[str] = devices, pc: int = phone_change_ns, newp: str = new_phone
        ) -> np.ndarray:
            out = np.array(devices, dtype=object)[rng.integers(0, len(devices), len(tns))]
            out[tns >= pc] = newp
            return out

        # --- regular spending (vectorised per customer)
        n = rng.poisson(rate * active_days)
        day = np.floor(rng.uniform(0, active_days, n))
        hours = np.clip(rng.normal(peak, 3.2, n), 6, 23.9)
        tns = card_start.normalize().value + (day * NS_DAY + hours * 3600e9).astype(np.int64)
        cat = rng.choice(cats_regular, size=n, p=prefs)
        keep = (tns >= start_ns) & (tns < end_ns)
        tns, cat = tns[keep], cat[keep]
        n = len(tns)
        loc = location(tns)
        in_person = rng.random(n) < cat_pos[cat]
        amount = np.round(np.clip(rng.lognormal(np.log(cat_median[cat] * scale), cat_sigma[cat]), 0.5, 20_000), 2)
        merchant = np.full(n, -1)
        for c, city in set(zip(cat[in_person].tolist(), loc[in_person].tolist(), strict=True)):
            rows = np.flatnonzero(in_person & (cat == c) & (loc == city))
            pool = local(CATEGORIES[c].code, CITIES[city][0])
            if not len(pool):
                continue
            pick = rng.choice(pool, size=len(rows))
            if city == home_idx[i] and c in favourites:
                fav = rng.random(len(rows)) < 0.75
                pick[fav] = rng.choice(favourites[c], size=int(fav.sum()))
            merchant[rows] = pick
        for c in set(cat[~in_person].tolist()):
            rows = np.flatnonzero(~in_person & (cat == c))
            pool = online(CATEGORIES[c].code)
            if len(pool):
                merchant[rows] = rng.choice(pool, size=len(rows))
        ok = merchant >= 0
        tns, cat, loc, in_person, amount, merchant = tns[ok], cat[ok], loc[ok], in_person[ok], amount[ok], merchant[ok]
        n = len(tns)
        if n:
            is_atm = cat == atm_idx
            channel = np.where(in_person, np.where(is_atm, "atm", "pos"), "ecommerce").astype(object)
            entry = np.where(
                in_person,
                np.where(is_atm, "chip", rng.choice(["chip", "contactless", "swipe"], size=n, p=[0.5, 0.46, 0.04])),
                "online",
            ).astype(object)
            loc_country = np.array([CITIES[k][1] for k in loc], dtype=object)
            vpn = rng.random(n) < 0.02  # legitimate VPN / roaming noise
            ip = np.where(vpn, rng.choice(COUNTRIES, size=n), loc_country).astype(object)
            ip[in_person] = None
            dev = device_for(tns)
            dev[in_person] = None
            b.add_many(
                card_idx=i,
                merchant_idx=merchant,
                device=dev,
                amount=amount,
                channel=channel,
                entry_mode=entry,
                payee=None,
                ip_country=ip,
                lat=np.where(in_person, m_lat[merchant], np.nan),
                lon=np.where(in_person, m_lon[merchant], np.nan),
                occurred_at=tns,
                is_fraud=False,
                scenario="legit",
                episode=-1,
            )

        # --- transfers to known (occasionally new) payees
        if payees:
            k = rng.poisson(transfer_rate * active_days)
            tt = card_start.value + (rng.uniform(0, active_days, k) * NS_DAY).astype(np.int64)
            tt = tt[(tt >= start_ns) & (tt < end_ns)]
            k = len(tt)
            if k:
                known = np.array(payees, dtype=object)[rng.integers(0, len(payees), k)]
                fresh = np.array([f"PAYEE-{i:06d}-n{x}" for x in rng.integers(0, 1_000_000, k)], dtype=object)
                payee = np.where(rng.random(k) < 0.08, fresh, known)
                amt = np.round(np.clip(rng.lognormal(np.log(220 * scale), 0.8, k), 5, 20_000), 2)
                b.add_many(
                    card_idx=i,
                    merchant_idx=np.full(k, int(online("transfer")[0])),
                    device=device_for(tt),
                    amount=amt,
                    channel="transfer",
                    entry_mode="online",
                    payee=payee,
                    ip_country=np.array([CITIES[x][1] for x in location(tt)], dtype=object),
                    lat=np.nan,
                    lon=np.nan,
                    occurred_at=tt,
                    is_fraud=False,
                    scenario="legit",
                    episode=-1,
                )

        # --- digital wallet adoption (a legitimate behaviour change that starts with the drift period)
        if drift_t is not None and rng.random() < cfg.wallet_adoption:
            remaining = (pd.Timestamp(cfg.end) - drift_t).total_seconds() / 86400
            k = rng.poisson(remaining / 6)
            tt = drift_t.normalize().value + (
                np.floor(rng.uniform(0, remaining, k)) * NS_DAY + np.clip(rng.normal(peak, 3, k), 7, 23) * 3600e9
            ).astype(np.int64)
            tt = tt[(tt >= drift_t.value) & (tt < end_ns) & (tt >= card_start.value)]
            k = len(tt)
            if k:
                amt = np.round(np.clip(rng.lognormal(np.log(80 * scale), 0.6, k), 5, 5000), 2)
                b.add_many(
                    card_idx=i,
                    merchant_idx=rng.choice(online("digital_wallet"), size=k),
                    device=device_for(tt),
                    amount=amt,
                    channel="ecommerce",
                    entry_mode="online",
                    payee=None,
                    ip_country=home[1],
                    lat=np.nan,
                    lon=np.nan,
                    occurred_at=tt,
                    is_fraud=False,
                    scenario="legit",
                    episode=-1,
                )

    # ------------------------------------------------------------------ fraud episodes
    episode = 0
    report_delay_median: dict[str, float] = {
        "card_testing": 6,
        "cnp_stolen": 7,
        "account_takeover": 4,
        "cloned_card": 9,
        "device_compromise": 18,
    }
    episodes: list[tuple[int, str, pd.Timestamp]] = []
    for scen in SCENARIOS:
        rate = cfg.episode_rates.get(scen, 0) / 1000
        if scen == "device_compromise":
            if drift_t is None:
                continue
            window = (pd.Timestamp(cfg.end) - drift_t).total_seconds() / 86400
            t_from = drift_t
        else:
            window = float(cfg.days)
            t_from = start
        n_ep = rng.poisson(rate * cfg.n_customers * window / 365)
        for _ in range(n_ep):
            i = int(rng.integers(0, cfg.n_customers))
            t0 = t_from + pd.Timedelta(days=float(rng.uniform(0, window)))
            if not profiles[i] or t0 < cards.issued_at[i]:
                continue
            episodes.append((i, scen, t0))

    for i, scen, t0 in episodes:
        prof = profiles[i]
        typical = float(prof["typical"])
        home = CITIES[home_idx[i]]
        fdev = str(rng.choice(fraud_device_pool)) if rng.random() < 0.85 else _fp(rng, "fd-")
        foreign = str(rng.choice([c for c in COUNTRIES if c != home[1]]))
        events: list[dict[str, Any]] = []
        if scen == "card_testing":
            fip = foreign if rng.random() < 0.8 else home[1]
            ft = t0
            for _ in range(int(rng.integers(3, 9))):
                ft += pd.Timedelta(seconds=float(rng.uniform(15, 120)))
                fm = int(rng.choice(np.concatenate([online("digital_goods"), online("retail")])))
                events.append(
                    {
                        "merchant_idx": fm,
                        "amount": round(float(rng.uniform(0.5, 3.0)), 2),
                        "device": fdev,
                        "channel": "ecommerce",
                        "entry_mode": "online",
                        "ip_country": fip,
                        "occurred_at": ft,
                    }
                )
            for _ in range(int(rng.integers(1, 3))):
                ft += pd.Timedelta(minutes=float(rng.uniform(3, 40)))
                fm = int(rng.choice(np.concatenate([online("electronics"), online("gift_cards")])))
                events.append(
                    {
                        "merchant_idx": fm,
                        "amount": round(float(rng.uniform(250, 1500)), 2),
                        "device": fdev,
                        "channel": "ecommerce",
                        "entry_mode": "online",
                        "ip_country": fip,
                        "occurred_at": ft,
                    }
                )
        elif scen == "cnp_stolen":
            edev = fdev if rng.random() < 0.85 else str(rng.choice(prof["devices"]))
            fip = foreign if rng.random() < 0.65 else home[1]
            for k in range(int(rng.integers(1, 6))):
                ft = t0 + pd.Timedelta(hours=float(rng.uniform(0, 6))) if k else t0
                fcat = str(
                    rng.choice(["electronics", "gift_cards", "digital_goods", "retail"], p=[0.4, 0.3, 0.15, 0.15])
                )
                amount = round(float(np.clip(typical * rng.uniform(2, 6), 80, 5000)), 2)
                events.append(
                    {
                        "merchant_idx": int(rng.choice(online(fcat))),
                        "amount": amount,
                        "device": edev,
                        "channel": "ecommerce",
                        "entry_mode": "online",
                        "ip_country": fip,
                        "occurred_at": ft,
                    }
                )
        elif scen == "account_takeover":
            edev = fdev if rng.random() < 0.9 else str(rng.choice(prof["devices"]))
            fip = foreign if rng.random() < 0.5 else home[1]
            transfer_m = int(online("transfer")[0])
            for k in range(int(rng.integers(1, 4))):
                ft = t0 + pd.Timedelta(hours=float(rng.uniform(0, 24))) if k else t0
                events.append(
                    {
                        "merchant_idx": transfer_m,
                        "amount": round(float(rng.uniform(500, 4000)), 2),
                        "device": edev,
                        "channel": "transfer",
                        "entry_mode": "online",
                        "ip_country": fip,
                        "payee": str(rng.choice(fraud_payee_pool)),
                        "occurred_at": ft,
                    }
                )
        elif scen == "cloned_card":
            ffar = [c for c in CITIES if c[1] != home[1]]
            fdest = ffar[int(rng.integers(0, len(ffar)))]
            for k in range(int(rng.integers(2, 7))):
                ft = t0 + pd.Timedelta(hours=float(rng.uniform(0, 36))) if k else t0
                fcat = str(rng.choice(["electronics", "retail", "atm_cash", "gift_cards"], p=[0.3, 0.3, 0.3, 0.1]))
                fpool = local(fcat, fdest[0])
                if not len(fpool):
                    continue
                fm = int(rng.choice(fpool))
                events.append(
                    {
                        "merchant_idx": fm,
                        "amount": round(float(np.clip(rng.lognormal(np.log(220), 0.7), 20, 5000)), 2),
                        "device": None,
                        "channel": "atm" if fcat == "atm_cash" else "pos",
                        "entry_mode": "swipe" if rng.random() < 0.8 else "chip",
                        "ip_country": None,
                        "lat": m_lat[fm],
                        "lon": m_lon[fm],
                        "occurred_at": ft,
                    }
                )
        elif scen == "device_compromise":
            own = str(rng.choice(prof["devices"]))
            for k in range(int(rng.integers(2, 7))):
                ft = t0 + pd.Timedelta(days=float(rng.uniform(0, 10))) if k else t0
                fcat = "digital_wallet" if rng.random() < 0.7 else "digital_goods"
                amount = round(float(np.clip(typical * rng.uniform(1.0, 2.5), 20, 2000)), 2)
                events.append(
                    {
                        "merchant_idx": int(rng.choice(online(fcat))),
                        "amount": amount,
                        "device": own,
                        "channel": "ecommerce",
                        "entry_mode": "online",
                        "ip_country": home[1],
                        "occurred_at": ft,
                    }
                )
        for r in events:
            if r["occurred_at"] >= pd.Timestamp(cfg.end):
                continue
            b.add(
                card_idx=i,
                payee=r.get("payee"),
                lat=r.get("lat"),
                lon=r.get("lon"),
                is_fraud=True,
                scenario=scen,
                episode=episode,
                **{k: v for k, v in r.items() if k not in ("payee", "lat", "lon")},
            )
        episode += 1

    tx = b.frame()
    tx = tx.sort_values(["occurred_at", "card_idx"], kind="mergesort").reset_index(drop=True)
    tx["occurred_at"] = pd.to_datetime(tx["occurred_at"], utc=True)
    tx["external_id"] = [f"TXN-{cfg.seed}-{k:08d}" for k in range(len(tx))]
    tx["card_token"] = cards.token.to_numpy()[tx.card_idx.to_numpy()]
    tx["merchant_ref"] = merchants.external_ref.to_numpy()[tx.merchant_idx.to_numpy()]
    tx["amount"] = tx.amount.astype(float)
    tx["lat"] = tx.lat.astype(float)
    tx["lon"] = tx.lon.astype(float)
    tx["is_fraud"] = tx.is_fraud.astype(bool)
    tx["episode"] = tx.episode.astype(int)

    labels = _labels(tx, cfg, rng, report_delay_median)
    return SimData(cfg, customers, cards, merchants, tx, labels)


def _labels(tx: pd.DataFrame, cfg: SimConfig, rng: np.random.Generator, delay_median: dict[str, float]) -> pd.DataFrame:
    """Chargeback-style labels with realistic delay, missing reports and noise."""
    out: list[pd.DataFrame] = []
    fraud = tx[tx.is_fraud]
    for _ep, g in fraud.groupby("episode"):
        scen = str(g.scenario.iloc[0])
        if rng.random() > cfg.report_prob:
            continue  # never reported: remains a hidden positive
        delay = float(np.clip(rng.lognormal(np.log(delay_median[scen]), 0.7), 0.5, 60))
        reported = g.occurred_at.max() + pd.Timedelta(days=delay)
        keep = g
        if scen == "card_testing":
            # Customers often only notice the large purchases, not the $1 tests.
            small = g.amount < 5
            keep = g[~small | (rng.random(len(g)) < 0.5)]
        out.append(
            pd.DataFrame(
                {"external_id": keep.external_id, "label": "fraud", "source": "chargeback", "reported_at": reported}
            )
        )
    legit_online = tx[(~tx.is_fraud) & (tx.channel == "ecommerce")]
    disputed = legit_online[rng.random(len(legit_online)) < cfg.dispute_rate]
    if len(disputed):
        delays = pd.to_timedelta(np.clip(rng.lognormal(np.log(20), 0.6, len(disputed)), 2, 60), unit="D")
        reported = disputed.occurred_at.reset_index(drop=True) + pd.Series(delays)
        out.append(
            pd.DataFrame(
                {
                    "external_id": disputed.external_id.to_numpy(),
                    "label": "fraud",
                    "source": "chargeback",
                    "reported_at": reported.to_numpy(),
                }
            )
        )
    if not out:
        return pd.DataFrame(columns=["external_id", "label", "source", "reported_at"])
    labels = pd.concat(out, ignore_index=True)
    labels["reported_at"] = pd.to_datetime(labels.reported_at, utc=True).dt.floor("s")
    return labels.sort_values("reported_at", kind="mergesort").reset_index(drop=True)
