#!/usr/bin/env python3
"""Offline tests for the goals engine, its store, and the one write endpoint.

The amortisation maths is checked against the EMI prepayment spreadsheet this
feature was modelled on, because that sheet is the thing the figures will be
compared against. The reference case is its Calculations tab:

    B1 principal 4,512,463      B5 EMI 49,524
    B2 rate 7.4% a year         B8 four extra EMIs a year
    B3 tenure 11 years          B9 no annual hike

One deliberate divergence from the sheet is documented in goals.py: its ``E14``
subtracts the month-12 prepayment in month 2, while every later row correctly
uses the previous row's. That is a copy error in the sheet, not a rule, so the
totals below are the intended ones.

The browser recalculates with its own copy of this model in static/goals.js so
a slider responds without a round trip. test_js_parity() runs that copy under
node, when node is available, and insists the two agree — a silent drift there
would show the user one set of numbers and save another.

Run with either:
    python tests/test_goals.py
    pytest tests/test_goals.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import goals                                                # noqa: E402
import web                                                  # noqa: E402

_failures: List[str] = []

# The spreadsheet's inputs, named so the assertions below read like the sheet.
SHEET = dict(principal=4512463.0, rate=0.074, emi=49524.0, extra_emis=4.0)


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
    else:
        print(f"  FAIL  {message}")
        _failures.append(message)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def close(got: float, want: float, tol: float = 1.0) -> bool:
    return abs(float(got) - float(want)) <= tol


# ===========================================================================
# 1. Against the spreadsheet
# ===========================================================================
def test_against_spreadsheet() -> None:
    section("Amortisation against the spreadsheet")

    baseline = goals.build_schedule(SHEET["principal"], SHEET["rate"], SHEET["emi"])
    plan = goals.build_schedule(SHEET["principal"], SHEET["rate"], SHEET["emi"],
                                extra_emis_per_year=SHEET["extra_emis"])

    check(baseline.months == 135,
          f"EMI alone clears the loan in 135 months (got {baseline.months})")
    check(close(baseline.total_interest, 2135710),
          f"paying Rs 2,135,710 of interest (got {baseline.total_interest:,.0f})")

    check(plan.months == 92,
          f"four extra EMIs a year clears it in 92 months (got {plan.months})")
    check(close(plan.total_interest, 1429891),
          f"paying Rs 1,429,891 of interest (got {plan.total_interest:,.0f})")

    # Sheet D13: the first month's interest is simply B1 * B2 / 12.
    first = plan.rows[0]
    check(close(first.interest, SHEET["principal"] * SHEET["rate"] / 12, 0.01),
          f"month 1 interest matches sheet D13 ({first.interest:,.2f})")
    check(close(first.principal, SHEET["emi"] - first.interest, 0.01),
          "month 1 principal is the EMI less that interest")

    # Sheet F24: the month-12 prepayment is $B$5 * $B$8.
    check(close(plan.rows[11].prepayment, SHEET["emi"] * SHEET["extra_emis"]),
          f"month 12 prepays Rs {SHEET['emi'] * SHEET['extra_emis']:,.0f}, as sheet F24")
    check(all(row.prepayment == 0 for row in plan.rows[:11]),
          "and nothing is prepaid before the first anniversary")

    check(close(plan.total_principal, SHEET["principal"], 1.0),
          "principal repaid plus prepayments equals the loan exactly")
    check(close(plan.rows[-1].outstanding, 0.0, goals.EPSILON),
          "the final row leaves nothing outstanding")

    saved = baseline.total_interest - plan.total_interest
    check(close(saved, 705819, 2.0),
          f"the plan saves Rs 705,819 of interest (got {saved:,.0f})")
    check(baseline.months - plan.months == 43,
          "and 43 months, which is the headline the page shows")


# ===========================================================================
# 2. The levers, and the edges
# ===========================================================================
def test_levers() -> None:
    section("Rate, hike, lump sum and the edges")

    cases = {
        "a 10% annual EMI hike alone": (dict(annual_hike_pct=10), 89),
        "a Rs 10L lump sum alone": (dict(lump_sum=1000000), 94),
        "extra EMIs plus the hike": (dict(extra_emis_per_year=4, annual_hike_pct=10), 72),
        "extra EMIs plus the lump sum": (dict(extra_emis_per_year=4, lump_sum=1000000), 68),
    }
    for label, (kwargs, months) in cases.items():
        schedule = goals.build_schedule(SHEET["principal"], SHEET["rate"],
                                        SHEET["emi"], **kwargs)
        check(schedule.months == months,
              f"{label} clears in {months} months (got {schedule.months})")
        check(close(schedule.rows[-1].outstanding, 0.0, goals.EPSILON),
              f"  and finishes at zero, not a rounding crumb")

    zero = goals.build_schedule(1200000, 0.0, 10000)
    check(zero.months == 120 and close(zero.total_interest, 0.0),
          "an interest-free loan is just principal divided by the EMI")

    starved = goals.build_schedule(5000000, 0.09, 10000)
    check(not starved.rows and starved.warnings,
          "an EMI below the first month's interest is refused with a warning")
    check(not starved.cleared, "and is not reported as cleared")
    check("never" in starved.warnings[0].lower() or "interest" in starved.warnings[0].lower(),
          "the warning explains why rather than just failing")

    stalled = goals.build_schedule(5000000, 0.09, 37600)
    check(not stalled.cleared and stalled.warnings,
          "an EMI that runs past the 600-month cap warns instead of looping")
    check(stalled.months <= goals.MAX_MONTHS, "and stops at the cap")

    lump_clears = goals.build_schedule(500000, 0.09, 10000, lump_sum=600000)
    check(lump_clears.cleared and lump_clears.months == 0,
          "a lump sum larger than the balance clears it with no instalments")
    check(close(lump_clears.total_prepaid, 500000),
          "and only the balance is counted as prepaid, not the whole cheque")

    # monthly_emi() is only a hint on the page: the real EMI is whatever the
    # bank charges, which is why it stays an input. So the test that matters is
    # that the formula is self-consistent, not that it reproduces the sheet's
    # Rs 49,524 — that figure comes from the original loan, not this balance.
    emi = goals.monthly_emi(SHEET["principal"], SHEET["rate"], 11)
    check(emi is not None and close(emi, 50066, 1),
          f"the EMI formula gives Rs 50,066 for this balance over 11 years (got {emi:,.0f})")
    check(goals.build_schedule(SHEET["principal"], SHEET["rate"], emi).months == 132,
          "and paying exactly that clears the loan in the 132 months it promises")
    check(goals.monthly_emi(100000, 0.09, 0) is None,
          "a zero tenure has no EMI rather than a division by zero")


# ===========================================================================
# 3. Liquidity and affordability
# ===========================================================================
def test_affordability() -> None:
    section("Liquidity tiers and affordability")

    investments = [
        {"asset_type": "SA", "current_value": 250000},
        {"asset_type": "STOCK", "current_value": 800000},
        {"asset_type": "MF", "current_value": 400000},
        {"asset_type": "PPF", "current_value": 900000},
        {"asset_type": "SOMETHING_NEW", "current_value": 100000},
    ]
    tiers = goals.build_liquidity(investments)
    check(close(tiers[goals.TIER_READY].total, 250000), "ready money is the savings balance")
    check(close(tiers[goals.TIER_SELLABLE].total, 1200000),
          "sellable is stocks plus mutual funds")
    check(close(tiers[goals.TIER_LOCKED].total, 1000000),
          "an unrecognised asset type lands in locked, not in spendable money")
    check(goals.classify_asset("SOMETHING_NEW") == goals.TIER_LOCKED,
          "classification fails closed, so a new INDmoney type never inflates a downpayment")

    result = goals.assess_purchase(2000000, investments,
                                   use_tiers=[goals.TIER_READY, goals.TIER_SELLABLE],
                                   loan_rate=0.09, loan_years=7)
    check(close(result.available, 1450000), "available is ready plus sellable")
    check(close(result.downpayment, 1450000), "all of which can go to the downpayment")
    check(close(result.loan_needed, 550000), "leaving Rs 550,000 to borrow")
    check(result.emi and result.emi > 0, "with an EMI computed for that balance")

    capped = goals.assess_purchase(2000000, investments,
                                   use_tiers=[goals.TIER_READY, goals.TIER_SELLABLE],
                                   downpayment_cap=500000)
    check(close(capped.downpayment, 500000) and close(capped.loan_needed, 1500000),
          "a downpayment cap is respected and the rest becomes the loan")

    affordable = goals.assess_purchase(200000, investments,
                                       use_tiers=[goals.TIER_READY])
    check(close(affordable.loan_needed, 0.0),
          "something you can already afford needs no loan")

    cash = goals.assess_purchase(300000, [], use_tiers=[goals.TIER_READY],
                                 existing_cash=300000)
    check(close(cash.downpayment, 300000),
          "cash held outside INDmoney counts towards the downpayment")


# ===========================================================================
# 4. Validation and the store
# ===========================================================================
def test_validation_and_store() -> None:
    section("Goal validation and storage")

    good = {"kind": "payoff", "name": "Home loan", "principal": 4512463,
            "annual_rate": 0.074, "emi": 49524, "extra_emis_per_year": 4}
    goal, errors = goals.validate_goal(good)
    check(goal is not None and not errors, "a well-formed payoff goal validates")

    bad_cases = {
        "a goal with no name": {"kind": "payoff", "principal": 1, "emi": 1},
        "an unknown kind": {"kind": "retire", "name": "x"},
        "a zero principal": {"kind": "payoff", "name": "x", "principal": 0, "emi": 1},
        "a rate given as 7.4 rather than 0.074":
            {"kind": "payoff", "name": "x", "principal": 1, "annual_rate": 7.4, "emi": 1},
        "a purchase with no cost": {"kind": "purchase", "name": "x", "target_cost": 0},
        "a goal that is not an object": "payoff",
    }
    for label, raw in bad_cases.items():
        goal, errors = goals.validate_goal(raw)
        check(goal is None and errors, f"{label} is rejected with a reason")

    nonsense = goals.validate_goal({"kind": "payoff", "name": "x", "principal": "NaN",
                                    "annual_rate": 0.08, "emi": float("inf")})
    check(nonsense[0] is None, "NaN and infinity never reach the schedule")

    bogus_tier = goals.validate_goal({"kind": "purchase", "name": "Car",
                                      "target_cost": 100000,
                                      "use_tiers": ["ready", "magic"]})
    check(bogus_tier[0] is not None and bogus_tier[0]["use_tiers"] == ["ready"],
          "an invented tier name is dropped rather than trusted")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "goals.json"
        check(goals.load_goals(path) == [], "a missing goals file reads as empty")
        path.write_text("{not json", encoding="utf-8")
        check(goals.load_goals(path) == [], "a corrupt goals file reads as empty, not a crash")

        saved = [goals.validate_goal(good)[0]]
        saved[0]["id"] = goals.next_goal_id([])
        goals.save_goals(path, saved)
        check(goals.load_goals(path)[0]["name"] == "Home loan", "goals round-trip through disk")
        check(not list(Path(tmp).glob("*.tmp")), "and the atomic write leaves no .tmp behind")


# ===========================================================================
# 5. The web layer
# ===========================================================================
def test_web_layer() -> None:
    section("The goals page and its endpoint")

    block = web.json_block({"evil": "</script><img src=x onerror=alert(1)>"})
    check("</script>" not in block and "\\u003c" in block,
          "the embedded state cannot close its own script element")
    check(json.loads(block)["evil"].startswith("</script>"),
          "while still parsing back to the original string")

    state = web.goals_state()
    check(set(state) >= {"goals", "tiers", "liabilities"},
          "goals_state carries the goals, the tiers and any known loans")
    check(set(state["tiers"]) == {"ready", "sellable", "locked"},
          "with all three liquidity bands present even before the first run")

    html = web.page("Goals", web.render_goals_page(state), active_date=None,
                    index=[], privacy=web.PRIVACY, goals_active=True, sidebar=False)
    check('id="goals-state"' in html, "the page ships its state to the browser")
    check("/static/goals.js" in html, "and loads the calculator")
    check('class="amt"' in html, "amounts are wrapped so privacy blur still covers them")
    check("<aside" not in html, "the goals page drops the report archive sidebar")


# ===========================================================================
# 6. Browser and server must agree
# ===========================================================================
def test_js_parity() -> None:
    section("The browser's copy of the model")

    node = shutil.which("node")
    if not node:
        print("  SKIP  node is not installed, so JS parity was not checked")
        return

    source = (HERE.parent / "static" / "goals.js").read_text(encoding="utf-8")
    match = re.search(r"function buildSchedule.*?(?=\n  function monthlyEmi)",
                      source, re.S)
    if not match:
        check(False, "buildSchedule could be located in static/goals.js")
        return

    harness = f"""
      var MAX_MONTHS = 600, EPSILON = 0.5;
      function rupees(v) {{ return String(Math.round(v)); }}
      {match.group(0)}
      var out = JSON.parse(process.argv[1]).map(function (c) {{
        var s = buildSchedule(c[0], c[1], c[2], c[3]);
        return [s.months, Math.round(s.totalInterest)];
      }});
      console.log(JSON.stringify(out));
    """

    cases = [
        ({}, {}),
        ({"extra_emis_per_year": 4}, {"extraEmis": 4}),
        ({"annual_hike_pct": 10}, {"hikePct": 10}),
        ({"lump_sum": 1000000}, {"lumpSum": 1000000}),
        ({"extra_emis_per_year": 4, "annual_hike_pct": 10},
         {"extraEmis": 4, "hikePct": 10}),
        ({"extra_emis_per_year": 4, "lump_sum": 1000000},
         {"extraEmis": 4, "lumpSum": 1000000}),
    ]
    payload = [[SHEET["principal"], SHEET["rate"], SHEET["emi"], js] for _, js in cases]

    try:
        proc = subprocess.run([node, "-e", harness, json.dumps(payload)],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  SKIP  node could not be run ({exc})")
        return
    if proc.returncode != 0:
        check(False, f"the browser model runs under node ({proc.stderr.strip()[:120]})")
        return

    got = json.loads(proc.stdout)
    for (py_kwargs, _), (months, interest) in zip(cases, got):
        expected = goals.build_schedule(SHEET["principal"], SHEET["rate"],
                                        SHEET["emi"], **py_kwargs)
        label = ", ".join(f"{k}={v}" for k, v in py_kwargs.items()) or "EMI only"
        check(months == expected.months and close(interest, expected.total_interest, 1.0),
              f"browser and server agree on {label} "
              f"({months} months, Rs {interest:,})")


# ===========================================================================
def test_all():
    test_against_spreadsheet()
    test_levers()
    test_affordability()
    test_validation_and_store()
    test_web_layer()
    test_js_parity()
    assert not _failures, f"{len(_failures)} check(s) failed:\n" + "\n".join(_failures)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
    try:
        test_all()
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
    print("\nAll goal checks passed.")
