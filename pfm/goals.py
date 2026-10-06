"""Goal modelling: loan payoff schedules and purchase affordability.

Two kinds of goal:

* **payoff** — an existing loan you want to clear early. Produces a month-by-month
  amortisation schedule, and compares a plan (extra EMIs, EMI hikes, a rate
  change) against the baseline of simply paying the EMI.
* **purchase** — something you want to buy. Works out what a downpayment could be
  drawn from, tiered by how reachable each holding actually is, and what the
  remaining loan would cost.

Everything here is deterministic arithmetic over inputs you supply. No model is
involved, nothing is fetched, and nothing is advice — the module computes
consequences, it does not recommend selling anything.

The amortisation model is the one from EMI Prepayment Calculator.xlsx:

    interest_m    = outstanding × annual_rate / 12
    principal_m   = EMI − interest_m
    outstanding  -= principal_m
    every 12th month: outstanding -= base_EMI × extra_emis_per_year
                      EMI        *= 1 + annual_hike

One deliberate difference from the sheet: in it, ``E14`` subtracts ``F24`` — the
month-12 prepayment applied in month 2 — while every later row correctly uses the
previous row's prepayment. That is a copy error, so the month-2 outstanding in
the spreadsheet is understated. This implements the intended rule.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Guard rails. A schedule that cannot terminate would otherwise spin forever.
MAX_MONTHS = 600                      # 50 years
EPSILON = 0.5                         # a balance under 50 paise is cleared


# ===========================================================================
# Loan mathematics
# ===========================================================================
def monthly_emi(principal: float, annual_rate: float, years: float) -> Optional[float]:
    """The standard EMI for a principal, rate and tenure.

    Returns None when the inputs cannot produce one. A zero rate is handled
    separately because the usual formula divides by zero there.
    """
    if principal <= 0 or years <= 0:
        return None
    months = int(round(years * 12))
    if months <= 0:
        return None
    if annual_rate <= 0:
        return principal / months
    r = annual_rate / 12.0
    factor = (1 + r) ** months
    return principal * r * factor / (factor - 1)


def minimum_emi(principal: float, annual_rate: float) -> float:
    """The EMI below which the balance never falls — one month's interest."""
    return principal * max(annual_rate, 0.0) / 12.0


@dataclass
class ScheduleRow:
    month: int
    emi: float
    principal: float
    interest: float
    prepayment: float
    outstanding: float


@dataclass
class Schedule:
    rows: List[ScheduleRow]
    months: int
    total_interest: float
    total_principal: float
    total_prepaid: float
    cleared: bool
    warnings: List[str] = field(default_factory=list)

    @property
    def years(self) -> float:
        return self.months / 12.0

    @property
    def total_paid(self) -> float:
        return self.total_interest + self.total_principal


def build_schedule(principal: float, annual_rate: float, emi: float, *,
                   extra_emis_per_year: float = 0.0,
                   annual_hike_pct: float = 0.0,
                   lump_sum: float = 0.0,
                   max_months: int = MAX_MONTHS) -> Schedule:
    """Amortise a loan month by month.

    ``extra_emis_per_year`` is a count, matching the spreadsheet: 4 means a lump
    of four times the *base* EMI once every twelve months. ``annual_hike_pct`` is
    a percentage, applied to the EMI after each twelfth month. ``lump_sum`` is a
    one-off payment made immediately, before the first instalment.
    """
    warnings: List[str] = []
    rows: List[ScheduleRow] = []

    if principal <= 0:
        return Schedule([], 0, 0.0, 0.0, 0.0, True,
                        ["There is nothing outstanding to amortise."])

    base_emi = float(emi)
    balance = float(principal)
    total_prepaid = 0.0

    if lump_sum > 0:
        applied = min(lump_sum, balance)
        balance -= applied
        total_prepaid += applied
        if applied < lump_sum:
            warnings.append("The one-off payment was larger than the balance; "
                            "only what was owed has been applied.")

    # An EMI that does not cover the first month's interest can never clear the
    # loan. Say so plainly rather than returning a 600-month schedule.
    floor = minimum_emi(balance, annual_rate)
    if base_emi <= floor and balance > EPSILON and extra_emis_per_year <= 0 \
            and annual_hike_pct <= 0:
        warnings.append(
            f"An EMI of {base_emi:,.0f} does not cover the first month's interest "
            f"of {floor:,.0f}, so the balance would never fall. Increase the EMI, "
            f"add prepayments, or lower the rate."
        )
        return Schedule([], 0, 0.0, 0.0, total_prepaid, False, warnings)

    emi_current = base_emi
    total_interest = total_principal = 0.0
    month = 0

    while balance > EPSILON and month < max_months:
        month += 1
        interest = balance * annual_rate / 12.0
        principal_part = emi_current - interest

        # The final instalment is only ever what is left.
        if principal_part > balance:
            principal_part = balance
        if principal_part < 0:
            principal_part = 0.0

        paid = principal_part + interest
        balance -= principal_part
        total_interest += interest
        total_principal += principal_part

        prepayment = 0.0
        if month % 12 == 0 and balance > EPSILON:
            if extra_emis_per_year > 0:
                prepayment = min(base_emi * extra_emis_per_year, balance)
                balance -= prepayment
                total_prepaid += prepayment
            if annual_hike_pct:
                emi_current *= 1 + annual_hike_pct / 100.0

        rows.append(ScheduleRow(month, round(paid, 2), round(principal_part, 2),
                                round(interest, 2), round(prepayment, 2),
                                round(max(balance, 0.0), 2)))

    cleared = balance <= EPSILON
    if not cleared:
        warnings.append(
            f"Still {balance:,.0f} outstanding after {max_months // 12} years, so "
            f"the schedule was cut short. The EMI is too small for this balance "
            f"and rate."
        )

    return Schedule(rows=rows, months=month,
                    total_interest=round(total_interest, 2),
                    total_principal=round(total_principal + total_prepaid, 2),
                    total_prepaid=round(total_prepaid, 2),
                    cleared=cleared, warnings=warnings)


@dataclass
class PayoffComparison:
    baseline: Schedule
    plan: Schedule
    interest_saved: float
    months_saved: int

    @property
    def years_saved(self) -> float:
        return self.months_saved / 12.0


def compare_payoff(principal: float, annual_rate: float, emi: float, *,
                   extra_emis_per_year: float = 0.0,
                   annual_hike_pct: float = 0.0,
                   lump_sum: float = 0.0,
                   baseline_rate: Optional[float] = None) -> PayoffComparison:
    """Plan against baseline: what the extra effort actually buys.

    The baseline is the same loan paid at its EMI with no prepayments. When the
    plan changes the rate too (a balance transfer, say), ``baseline_rate`` keeps
    the comparison honest by holding the original rate on the baseline side.
    """
    baseline = build_schedule(principal,
                              annual_rate if baseline_rate is None else baseline_rate,
                              emi)
    plan = build_schedule(principal, annual_rate, emi,
                          extra_emis_per_year=extra_emis_per_year,
                          annual_hike_pct=annual_hike_pct,
                          lump_sum=lump_sum)
    return PayoffComparison(
        baseline=baseline, plan=plan,
        interest_saved=round(baseline.total_interest - plan.total_interest, 2),
        months_saved=baseline.months - plan.months,
    )


# ===========================================================================
# Affordability
# ===========================================================================
# How reachable each INDmoney asset class actually is. The point of the tiers is
# that a retirement corpus is not spendable money, and saying so is more useful
# than one flattering total.
TIER_READY = "ready"            # cash and cash-like
TIER_SELLABLE = "sellable"      # can be sold, but there are consequences
TIER_LOCKED = "locked"          # lock-ins and withdrawal rules

TIER_LABELS = {
    TIER_READY: "Readily available",
    TIER_SELLABLE: "Sellable, with consequences",
    TIER_LOCKED: "Locked or restricted",
}

TIER_NOTES = {
    TIER_READY: "Savings and liquid funds. Usable within a day or two.",
    TIER_SELLABLE: "Stocks, mutual funds, gold and US holdings. Selling may trigger "
                   "capital gains tax and exit loads, and the price on the day is "
                   "whatever it is.",
    TIER_LOCKED: "PPF, EPF and NPS. Lock-ins and withdrawal rules apply, so these "
                 "are listed for completeness but excluded from the total by "
                 "default.",
}

# INDmoney asset_type values, from networth_snapshot.
ASSET_TIERS: Dict[str, str] = {
    "SA": TIER_READY,                 # savings account
    "SAVING_ACCOUNT": TIER_READY,
    "US_STOCK_WALLET": TIER_READY,    # idle USD cash
    "LIQUID": TIER_READY,
    "STOCK": TIER_SELLABLE,           # Indian equity and ETFs
    "IND_STOCK": TIER_SELLABLE,
    "US_STOCK": TIER_SELLABLE,
    "MF": TIER_SELLABLE,
    "GOLD": TIER_SELLABLE,
    "SGB": TIER_SELLABLE,
    "BOND": TIER_SELLABLE,
    "CRYPTO": TIER_SELLABLE,
    "PPF": TIER_LOCKED,
    "EPF": TIER_LOCKED,
    "NPS": TIER_LOCKED,
    "INSURANCE": TIER_LOCKED,
    "REAL_ESTATE": TIER_LOCKED,
}


def classify_asset(asset_type: str) -> str:
    """Tier for an INDmoney asset type. Unknown types are treated as locked.

    Fail-closed on purpose: an unrecognised asset class counted as spendable
    would overstate what you can actually reach.
    """
    return ASSET_TIERS.get(str(asset_type or "").strip().upper(), TIER_LOCKED)


@dataclass
class Tier:
    key: str
    label: str
    note: str
    total: float
    items: List[Dict[str, object]] = field(default_factory=list)


def build_liquidity(investments: List[dict]) -> Dict[str, Tier]:
    """Group a networth_snapshot ``investments`` list into liquidity tiers."""
    tiers = {key: Tier(key, TIER_LABELS[key], TIER_NOTES[key], 0.0)
             for key in (TIER_READY, TIER_SELLABLE, TIER_LOCKED)}

    for row in investments or []:
        if not isinstance(row, dict):
            continue
        asset_type = str(row.get("asset_type") or "").upper()
        try:
            value = float(row.get("current_value") or 0.0)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        tier = tiers[classify_asset(asset_type)]
        tier.items.append({"asset_type": asset_type, "current_value": round(value, 2),
                           "unknown_type": asset_type not in ASSET_TIERS})
        tier.total = round(tier.total + value, 2)

    for tier in tiers.values():
        tier.items.sort(key=lambda item: item["current_value"], reverse=True)
    return tiers


@dataclass
class Affordability:
    target: float
    downpayment: float
    loan_needed: float
    tiers: Dict[str, Tier]
    used_tiers: List[str]
    available: float
    shortfall: float
    emi: Optional[float]
    total_interest: Optional[float]
    warnings: List[str] = field(default_factory=list)


def assess_purchase(target_cost: float, investments: List[dict], *,
                    use_tiers: Optional[List[str]] = None,
                    downpayment_cap: Optional[float] = None,
                    loan_rate: float = 0.09,
                    loan_years: float = 7.0,
                    existing_cash: float = 0.0) -> Affordability:
    """What a purchase would take: downpayment from holdings, loan for the rest.

    ``use_tiers`` chooses which liquidity bands count. Locked holdings are
    excluded unless explicitly listed, and even then a warning is attached.
    """
    use_tiers = list(use_tiers or [TIER_READY, TIER_SELLABLE])
    tiers = build_liquidity(investments)
    warnings: List[str] = []

    available = round(sum(tiers[key].total for key in use_tiers if key in tiers)
                      + max(existing_cash, 0.0), 2)

    downpayment = min(available, float(target_cost))
    if downpayment_cap is not None:
        downpayment = min(downpayment, max(float(downpayment_cap), 0.0))

    loan_needed = round(max(float(target_cost) - downpayment, 0.0), 2)
    shortfall = round(max(float(target_cost) - available, 0.0), 2)

    if TIER_LOCKED in use_tiers and tiers[TIER_LOCKED].total > 0:
        warnings.append(
            "Locked holdings (PPF, EPF, NPS) are included in this total. They "
            "have lock-ins and withdrawal rules, so treat that figure as "
            "theoretical rather than money you can reach this month."
        )
    if tiers[TIER_SELLABLE].total > 0 and TIER_SELLABLE in use_tiers:
        warnings.append(
            "Selling investments can trigger capital gains tax and exit loads, "
            "and realises whatever price the market offers that day. The figures "
            "here are gross and ignore both."
        )
    unknown = [item["asset_type"] for tier in tiers.values()
               for item in tier.items if item["unknown_type"]]
    if unknown:
        warnings.append(
            "These asset types are not recognised and were treated as locked, so "
            "they do not inflate the total: " + ", ".join(sorted(set(unknown))) + "."
        )

    emi = monthly_emi(loan_needed, loan_rate, loan_years) if loan_needed > 0 else None
    total_interest = None
    if emi:
        schedule = build_schedule(loan_needed, loan_rate, emi)
        total_interest = schedule.total_interest

    return Affordability(
        target=round(float(target_cost), 2), downpayment=round(downpayment, 2),
        loan_needed=loan_needed, tiers=tiers, used_tiers=use_tiers,
        available=available, shortfall=shortfall,
        emi=round(emi, 2) if emi else None, total_interest=total_interest,
        warnings=warnings,
    )


# ===========================================================================
# Serialisation for the web view
# ===========================================================================
def schedule_payload(schedule: Schedule, *, yearly: bool = True) -> dict:
    """Shrink a schedule for the browser.

    The chart needs a point per month, but a 360-row table does not help anyone,
    so yearly aggregates are sent alongside.
    """
    years: List[dict] = []
    if yearly and schedule.rows:
        for start in range(0, len(schedule.rows), 12):
            chunk = schedule.rows[start:start + 12]
            years.append({
                "year": start // 12 + 1,
                "principal": round(sum(r.principal for r in chunk), 2),
                "interest": round(sum(r.interest for r in chunk), 2),
                "prepayment": round(sum(r.prepayment for r in chunk), 2),
                "closing": chunk[-1].outstanding,
            })

    return {
        "months": schedule.months,
        "years": round(schedule.years, 2),
        "cleared": schedule.cleared,
        "total_interest": schedule.total_interest,
        "total_principal": schedule.total_principal,
        "total_prepaid": schedule.total_prepaid,
        "total_paid": round(schedule.total_paid, 2),
        "warnings": list(schedule.warnings),
        "monthly": [
            {"m": r.month, "p": r.principal, "i": r.interest,
             "pre": r.prepayment, "bal": r.outstanding}
            for r in schedule.rows
        ],
        "yearly": years,
    }


# ===========================================================================
# Storage
# ===========================================================================
# Everything above is pure arithmetic. These few functions are the only part
# that touches disk, kept here because they belong to the same domain rather
# than justifying a module of their own.
GOAL_KINDS = ("payoff", "purchase")


def _clean_number(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(number) or math.isinf(number) else number


def validate_goal(raw: dict) -> tuple[Optional[dict], List[str]]:
    """Normalise one goal from the browser. Returns (goal, errors).

    Validated server-side as well as in the page, because the endpoint is
    reachable by anything on the network, not only by the form.
    """
    errors: List[str] = []
    if not isinstance(raw, dict):
        return None, ["A goal must be an object."]

    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in GOAL_KINDS:
        errors.append(f"kind must be one of {', '.join(GOAL_KINDS)}.")

    name = str(raw.get("name") or "").strip()[:80]
    if not name:
        errors.append("A goal needs a name.")

    goal = {
        "id": str(raw.get("id") or "").strip()[:40] or None,
        "kind": kind,
        "name": name,
        "notes": str(raw.get("notes") or "").strip()[:500],
    }

    if kind == "payoff":
        goal.update({
            "principal": _clean_number(raw.get("principal")),
            "annual_rate": _clean_number(raw.get("annual_rate")),
            "emi": _clean_number(raw.get("emi")),
            "tenure_years": _clean_number(raw.get("tenure_years")),
            "extra_emis_per_year": _clean_number(raw.get("extra_emis_per_year")),
            "annual_hike_pct": _clean_number(raw.get("annual_hike_pct")),
            "lump_sum": _clean_number(raw.get("lump_sum")),
            "lender": str(raw.get("lender") or "").strip()[:60],
        })
        if goal["principal"] <= 0:
            errors.append("The outstanding balance must be greater than zero.")
        if not 0 <= goal["annual_rate"] < 1:
            errors.append("The rate is a decimal fraction, so 0.074 means 7.4%.")
        if goal["emi"] <= 0:
            errors.append("The EMI must be greater than zero.")
    elif kind == "purchase":
        goal.update({
            "target_cost": _clean_number(raw.get("target_cost")),
            "loan_rate": _clean_number(raw.get("loan_rate"), 0.09),
            "loan_years": _clean_number(raw.get("loan_years"), 7.0),
            "existing_cash": _clean_number(raw.get("existing_cash")),
            "downpayment_cap": (None if raw.get("downpayment_cap") in (None, "")
                                else _clean_number(raw.get("downpayment_cap"))),
            "use_tiers": [t for t in (raw.get("use_tiers") or
                                      [TIER_READY, TIER_SELLABLE])
                          if t in TIER_LABELS],
        })
        if goal["target_cost"] <= 0:
            errors.append("The target cost must be greater than zero.")
        if not goal["use_tiers"]:
            errors.append("Choose at least one source of funds.")

    return (None, errors) if errors else (goal, [])


def next_goal_id(goals: List[dict]) -> str:
    used = {str(g.get("id") or "") for g in goals}
    index = 1
    while f"g{index}" in used:
        index += 1
    return f"g{index}"


def load_goals(path) -> List[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    goals = data.get("goals") if isinstance(data, dict) else data
    return [g for g in (goals or []) if isinstance(g, dict)]


def save_goals(path, goals: List[dict]) -> None:
    """Write atomically, so a crash mid-write cannot leave a truncated file."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": datetime.now().isoformat(timespec="seconds"),
               "goals": goals}
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def affordability_payload(result: Affordability) -> dict:
    return {
        "target": result.target,
        "available": result.available,
        "downpayment": result.downpayment,
        "loan_needed": result.loan_needed,
        "shortfall": result.shortfall,
        "emi": result.emi,
        "total_interest": result.total_interest,
        "used_tiers": result.used_tiers,
        "warnings": list(result.warnings),
        "tiers": {
            key: {"label": tier.label, "note": tier.note,
                  "total": tier.total, "items": tier.items}
            for key, tier in result.tiers.items()
        },
    }
