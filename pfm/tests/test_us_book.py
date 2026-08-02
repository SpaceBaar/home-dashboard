#!/usr/bin/env python3
"""Tests for the INDmoney US book: normalisation, multi-currency math, US news.

No network, no MCP server, no model. Every INDmoney response shape lives in
tests/fixtures/indmoney_us_shapes.json.

The point of these tests is not that the guessed field names are right — only a
real capture from tools/probe_indmoney.py can settle that. It is that the
normaliser either understands a row or **refuses it**, and that a refused or
cost-basis-free row can never turn into a fabricated number in the report.

Run with either:
    python tests/test_us_book.py
    pytest tests/test_us_book.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import brokers                                              # noqa: E402
import news as news_mod                                     # noqa: E402
import report as report_mod                                 # noqa: E402
from brokers import BOOK_IND, BOOK_US                       # noqa: E402
from llm import StockScore                                  # noqa: E402
from pfm_config import load_config                          # noqa: E402
from portfolio import (FX_SANITY_RANGE, build_fact_sheet,   # noqa: E402
                       extract_holdings_json, resolve_fx)

FIXTURES = HERE / "fixtures"
SHAPES = json.loads((FIXTURES / "indmoney_us_shapes.json").read_text(encoding="utf-8"))
CFG = load_config(HERE.parent / "config.json")

_failures: List[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
    else:
        print(f"  FAIL  {message}")
        _failures.append(message)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def rows_of(shape_key: str) -> List[dict]:
    return brokers.extract_rows(SHAPES[shape_key]) or []


# ===========================================================================
# 1. Number parsing — the None-vs-zero distinction
# ===========================================================================
def test_number_parsing() -> None:
    section("Number parsing keeps None distinct from zero")

    cases = [
        (1234.5, 1234.5), ("1234.5", 1234.5), ("1,234.50", 1234.5),
        ("$1,138.40", 1138.4), ("₹1,23,456.78", 123456.78), ("+0.62%", 0.62),
        ("-17.33", -17.33), ("unknown", None), ("N/A", None), ("", None),
        (None, None), ("--", None), (True, None), ("abc", None), (0, 0.0),
    ]
    for raw, expected in cases:
        got = brokers._num(raw)
        ok = (got is None and expected is None) or (
            got is not None and expected is not None and abs(got - expected) < 1e-6)
        check(ok, f"_num({raw!r}) -> {got!r} (expected {expected!r})")

    check(brokers._num("unknown") is None and brokers._num(0) == 0.0,
          "'unknown' is None while a real zero stays 0.0 — a missing cost basis "
          "must never become a zero cost basis")


# ===========================================================================
# 2. Normalising the various shapes
# ===========================================================================
def test_normalisation() -> None:
    section("INDmoney rows normalise across plausible field namings")

    holdings, problems = brokers.normalise_indmoney_rows(rows_of("shape_a_snake_case_usd"))
    check(len(holdings) == 2 and not problems, "snake_case USD shape parses both rows")
    aapl = next(h for h in holdings if h["symbol"] == "AAPL")
    check(aapl["currency"] == "USD" and aapl["book"] == BOOK_US,
          "currency and book detected as USD / US")
    check(aapl["quantity"] == 12 and abs(aapl["invested_native"] - 2166.0) < 0.01,
          f"quantity {aapl['quantity']:g}, invested {aapl['invested_native']}")
    check(any("XIRR" in f for f in aapl["flags"]),
          "broker-reported XIRR is retained as a flag, not as a computed figure")

    holdings, problems = brokers.normalise_indmoney_rows(rows_of("shape_b_camel_units_nested"))
    check(len(holdings) == 1 and not problems, "camelCase + nested quote shape parses")
    nvda = holdings[0]
    check(nvda["symbol"] == "NVDA" and nvda["quantity"] == 20,
          "units/avgBuyPrice/marketValue are recognised")
    check(nvda["ltp"] == 121.4 and nvda["day_pct"] == 2.11,
          "values nested one level under 'quote' are found")

    holdings, problems = brokers.normalise_indmoney_rows(rows_of("shape_d_formatted_strings"))
    check(len(holdings) == 1, "currency-formatted strings parse")
    googl = holdings[0]
    check(abs(googl["invested_native"] - 1138.40) < 0.01 and abs(googl["ltp"] - 168.90) < 0.01,
          f"'$1,138.40' -> {googl['invested_native']}, '$168.90' -> {googl['ltp']}")

    holdings, problems = brokers.normalise_indmoney_rows(rows_of("shape_e_mixed_books"))
    check([h["symbol"] for h in holdings] == ["META"],
          "an IND_STOCK row is filtered out of the US book (Kite is authoritative there)")

    holdings, problems = brokers.normalise_indmoney_rows(rows_of("shape_f_unusable"))
    check(not holdings and len(problems) == 1,
          "an uninterpretable row is EXCLUDED, not defaulted to zero")
    check("probe_indmoney" in problems[0],
          "the diagnostic tells you how to capture the real shape")


def test_unknown_cost_basis() -> None:
    section("Missing cost basis is preserved as unknown")

    holdings, problems = brokers.normalise_indmoney_rows(rows_of("shape_c_unknown_cost_basis"))
    check(len(holdings) == 2 and not problems,
          "rows with no invested amount are still kept — the value is real")
    for h in holdings:
        check(h["invested_native"] is None and h["avg_price"] is None,
              f"{h['symbol']}: invested and average price are None, not 0")
        check(any("invested amount not shared" in f for f in h["flags"]),
              f"{h['symbol']}: flagged for the data-quality section")
    msft = next(h for h in holdings if h["symbol"] == "MSFT")
    check(abs(msft["current_native"] - 1720.4) < 0.01,
          "the current value is still used — only the return is unknown")
    check(any("Vested" in f for f in msft["flags"]),
          "the underlying broker is recorded")


# ===========================================================================
# 3. FX handling
# ===========================================================================
def test_fx() -> None:
    section("USD/INR resolution refuses to guess")

    rate, source = resolve_fx(88.5, None)
    check(rate == 88.5 and "config" in source, f"a configured rate is used ({source})")

    rate, source = resolve_fx(None, 87.2)
    check(rate == 87.2 and "implied" in source, f"an implied rate is used ({source})")

    rate, source = resolve_fx(88.0, 87.0)
    check(rate == 88.0, "the configured rate wins over the implied one")

    rate, source = resolve_fx(None, None)
    check(rate is None and source == "unavailable",
          "with no rate available, none is invented")

    for bad in (0.0115, 1.0, 5000.0, -88.0):
        rate, _ = resolve_fx(bad, None)
        check(rate is None, f"an implausible rate {bad} is rejected "
                            f"(sanity band {FX_SANITY_RANGE[0]:g}-{FX_SANITY_RANGE[1]:g})")


# ===========================================================================
# 4. Multi-currency fact sheet
# ===========================================================================
def india_rows() -> List[dict]:
    return extract_holdings_json((FIXTURES / "holdings.json").read_text(encoding="utf-8"))


def test_multi_currency():
    section("Combined fact sheet across two books and two currencies")

    us_rows, _ = brokers.normalise_indmoney_rows(rows_of("shape_a_snake_case_usd"))
    all_rows = india_rows() + us_rows
    fs = build_fact_sheet(all_rows, usd_inr=88.0, fx_source="configured (88.00)")

    check(set(fs.books) == {BOOK_IND, BOOK_US}, "both books are present")
    india, us = fs.books[BOOK_IND], fs.books[BOOK_US]
    check(india.currency == "INR" and us.currency == "USD",
          "each book keeps its own currency")
    check(us.count == 2 and abs(us.current - (2571.0 + 992.0)) < 0.01,
          f"US subtotal is in dollars (${us.current:,.2f})")
    check(abs(us.current_inr - us.current * 88.0) < 0.01,
          f"US subtotal converts to Rs {us.current_inr:,.0f} at 88.00")

    # The invariant: for the costed subset, the arithmetic closes exactly.
    costed = [h for h in fs.holdings if h.has_cost_basis and h.pnl_inr is not None]
    residual = abs(sum(h.pnl_inr for h in costed) - fs.total_pnl)
    check(residual < 0.01,
          f"combined rupee P&L reconciles across both books (residual {residual:.4f})")
    check(abs((fs.total_invested + fs.total_pnl)
              - sum(h.current_inr for h in costed)) < 0.01,
          "invested + P&L equals the costed current value")

    aapl = next(h for h in fs.holdings if h.symbol == "AAPL")
    check(aapl.currency == "USD" and aapl.fx_rate == 88.0,
          "a US holding carries its own FX rate")
    check(abs(aapl.current_native - 2571.0) < 0.01
          and abs(aapl.current_inr - 2571.0 * 88.0) < 0.01,
          f"native ${aapl.current_native:,.0f} and Rs {aapl.current_inr:,.0f} are both kept")
    check(abs(aapl.pnl_pct - 18.7) < 0.5,
          f"the return percentage is currency-independent ({aapl.pnl_pct:+.1f}%)")
    return fs


def test_no_fx_available():
    section("US book with no FX rate is never folded into a rupee total")

    us_rows, _ = brokers.normalise_indmoney_rows(rows_of("shape_a_snake_case_usd"))
    fs = build_fact_sheet(india_rows() + us_rows, usd_inr=None, fx_source="unavailable")

    us_holdings = [h for h in fs.holdings if h.book == BOOK_US]
    check(all(h.current_inr is None for h in us_holdings),
          "US rows have no rupee value")
    check(all(any("no USD/INR rate" in f for f in h.flags) for h in us_holdings),
          "each excluded row says why")
    check(any("not part of the combined rupee total" in q for q in fs.data_quality),
          "the report discloses the exclusion")

    india_only = build_fact_sheet(india_rows())
    check(abs(fs.total_current - india_only.total_current) < 0.01,
          "the combined rupee total equals the India-only total, so no US dollars "
          "were silently added as if they were rupees")
    check(fs.books[BOOK_US].current > 0,
          "the US book still reports its own dollar subtotal")


def test_uncosted_in_fact_sheet():
    section("Holdings with no cost basis suppress returns rather than inventing them")

    us_rows, _ = brokers.normalise_indmoney_rows(rows_of("shape_c_unknown_cost_basis"))
    fs = build_fact_sheet(india_rows() + us_rows, usd_inr=88.0, fx_source="configured")

    msft = next(h for h in fs.holdings if h.symbol == "MSFT")
    check(msft.pnl_pct is None and msft.pnl_native is None,
          "P&L and percentage are None for an uncosted holding")
    check(msft.current_inr is not None and msft.current_inr > 0,
          "its current value still counts toward portfolio value")
    check(set(fs.uncosted) == {"MSFT", "AMZN"}, f"uncosted holdings listed: {fs.uncosted}")
    check(any("No cost basis was shared" in q for q in fs.data_quality),
          "the data-quality section explains it")
    check(fs.books[BOOK_US].uncosted_count == 2,
          "the US book counts its uncosted rows")

    costed = [h for h in fs.holdings if h.has_cost_basis and h.pnl_inr is not None]
    residual = abs(sum(h.pnl_inr for h in costed) - fs.total_pnl)
    check(residual < 0.01,
          f"totals still reconcile with uncosted rows present (residual {residual:.4f})")
    check(fs.total_current > sum(h.current_inr for h in costed),
          "portfolio value exceeds the costed subset, as it should")


# ===========================================================================
# 5. US news and sentiment
# ===========================================================================
def test_us_news():
    section("US news extraction and sentiment mapping")

    rows = brokers.extract_rows(SHAPES["us_details_with_news"]) or []
    details = {}
    for row in rows:
        details[str(row.get("symbol")).upper()] = row
    extracted = brokers.extract_us_news(details)

    check(set(extracted) == {"AAPL", "TSLA", "NVDA"}, "all three tickers extracted")
    check(len(extracted["AAPL"]["articles"]) == 2,
          "both 'title/source/url' and 'headline/publisher/link' namings are read")
    titles = [a["title"] for a in extracted["AAPL"]["articles"]]
    check(any("supply crunch" in t for t in titles), f"headline text preserved: {titles[0][:40]}")
    check(extracted["AAPL"]["articles"][0]["link"].startswith("https://"),
          "article links are preserved")
    check(len(extracted["TSLA"]["articles"]) == 1, "'news_items' is also recognised")
    check(extracted["NVDA"]["articles"] == [] and extracted["NVDA"]["sentiment"] == 8.5,
          "a ticker with sentiment but no headlines is still captured")

    check(extracted["AAPL"]["sentiment"] == 7.0 and "label" in extracted["AAPL"]["sentiment_note"],
          f"the label 'positive' maps to {extracted['AAPL']['sentiment']}/10")
    tsla = extracted["TSLA"]
    check(tsla["sentiment"] is not None and tsla["sentiment"] < 5,
          f"a -0.6 score maps to the bearish half ({tsla['sentiment']}/10)")
    check("-1..+1" in tsla["sentiment_note"],
          f"the assumed scale is recorded rather than presented as fact: "
          f"{tsla['sentiment_note']!r}")

    for value in ("gibberish", None, object()):
        score, _ = brokers._sentiment_to_ten(value)
        check(score is None, f"unmappable sentiment {value!r} yields None")


def test_merge_and_disagreement():
    section("Merging US headlines and flagging sentiment disagreement")

    grouped = {
        "AAPL": [news_mod.Article("AAPL", "Apple warns iPhone and Mac sales face a supply crunch",
                                  "Livemint", "https://example.com/rss-dup")],
    }
    extra = {
        "AAPL": {"articles": [
            {"title": "Apple warns iPhone and Mac sales face a supply crunch",
             "source": "Reuters", "link": "https://example.com/indmoney-dup"},
            {"title": "Apple unveils a new India assembly line",
             "source": "Reuters", "link": "https://example.com/new"},
        ], "sentiment": 7.0, "sentiment_note": "label 'positive'"},
        "MSFT": {"articles": [
            {"title": "Microsoft raises Azure capacity guidance",
             "source": "Bloomberg", "link": "https://example.com/msft"},
        ], "sentiment": None, "sentiment_note": ""},
    }
    merged = news_mod.merge_articles(dict(grouped), extra)

    check(len(merged["AAPL"]) == 2,
          "the story carried by both RSS and INDmoney is not double-counted")
    check(any("India assembly line" in a.title for a in merged["AAPL"]),
          "genuinely new INDmoney headlines are added")
    check("MSFT" in merged and len(merged["MSFT"]) == 1,
          "a ticker with no RSS coverage gains a group from INDmoney")

    scores = [
        StockScore("AAPL", 3, "Supply constraints dominate.", "high", "label", 2),
        StockScore("NVDA", 8, "Demand commentary strong.", "high", "label", 0),
        StockScore("TSLA", 4, "Mixed.", "high", "label", 1),
    ]
    sentiment = {
        "AAPL": {"sentiment": 7.0, "sentiment_note": "label 'positive'"},
        "NVDA": {"sentiment": 8.5, "sentiment_note": "assumed 1..10 scale"},
        "TSLA": {"sentiment": None, "sentiment_note": ""},
    }
    notes = news_mod.sentiment_disagreements(scores, sentiment, threshold=3.0)

    check(len(notes) == 1 and notes[0].startswith("AAPL"),
          f"only the 4-point AAPL gap is flagged: {len(notes)} note(s)")
    check("3/10" in notes[0] and "7.0/10" in notes[0],
          "both scores appear so you can judge which to trust")
    check("label 'positive'" in notes[0],
          "the scale assumption is disclosed in the note")
    check(not any(n.startswith("NVDA") for n in notes),
          "a 0.5-point difference is not noise-flagged")
    check(not any(n.startswith("TSLA") for n in notes),
          "a ticker with no broker sentiment is skipped")


# ===========================================================================
# 6. Report and payload
# ===========================================================================
def test_report_and_payload(fs):
    section("Report and JSON payload carry both books")

    scores = [StockScore("AAPL", 6, "Mixed but constructive.", "high", "label", 2),
              StockScore("TSLA", 3, "China split denial.", "high", "label", 1)]
    held = {h.symbol for h in fs.holdings}
    broker_sentiment = {"AAPL": {"sentiment": 7.0, "sentiment_note": "label 'positive'"}}

    narrative = report_mod.deterministic_narrative(fs, scores, held)
    check("US holdings" in narrative or "US" in narrative,
          "the deterministic narrative mentions the US book")

    allowed_symbols, allowed_aliases = report_mod.build_allowed_names(
        fs, scores, CFG.keyword_map)
    allowed_numbers = report_mod.build_allowed_numbers(fs, scores)
    result = report_mod.validate_narrative(
        narrative, allowed_symbols=allowed_symbols, allowed_aliases=allowed_aliases,
        allowed_numbers=allowed_numbers)
    check(result.ok, f"the multi-currency narrative passes validation "
                     f"({result.violations[:3] if result.violations else 'clean'})")

    content = report_mod.render_report(
        fs, "_no news_", narrative, "deterministic", scores=scores,
        model="fake:1.5b")
    check("### India" in content and "### US" in content,
          "the markdown report has separate India and US holdings tables")
    check("**India total**" in content and "**US total**" in content,
          "each book has its own reconciling total row")
    check("**Combined (Rs):**" in content, "a combined rupee line is present")
    check("USD/INR used" in content and "88.00" in content,
          "the FX rate actually used is disclosed in the report")
    check("$2,571" in content or "$2571" in content,
          "US rows are shown in dollars")

    # With every holding costed, the report may state value against invested directly.
    check("Combined (Rs):** invested" in content,
          "a fully costed portfolio states invested and value together")

    # With uncosted rows, it must not.
    us_uncosted, _ = brokers.normalise_indmoney_rows(rows_of("shape_c_unknown_cost_basis"))
    fs2 = build_fact_sheet(india_rows() + us_uncosted, usd_inr=88.0, fx_source="configured")
    narrative2 = report_mod.deterministic_narrative(fs2, scores, held)
    content2 = report_mod.render_report(fs2, "_none_", narrative2, "deterministic",
                                        scores=scores, model="fake:1.5b")
    check("Cost basis is known for" in content2,
          "with uncosted rows, value and invested are not presented as one pair")
    check("Cost basis is available for" in narrative2,
          "the narrative says which subset the return figures describe")
    check("costed holdings only" in content2,
          "the position table labels the invested and P&L rows as a subset")
    check("* Invested and P&L cover only" in content2,
          "the books table carries the footnote explaining the star")
    result2 = report_mod.validate_narrative(
        narrative2,
        allowed_symbols=report_mod.build_allowed_names(fs2, scores, CFG.keyword_map)[0],
        allowed_aliases=report_mod.build_allowed_names(fs2, scores, CFG.keyword_map)[1],
        allowed_numbers=report_mod.build_allowed_numbers(fs2, scores))
    check(result2.ok, f"the uncosted-aware narrative also validates "
                      f"({result2.violations[:3] if result2.violations else 'clean'})")

    payload = report_mod.build_payload(
        fs, {}, scores, narrative, "deterministic", held=held, model="fake:1.5b",
        broker_sentiment=broker_sentiment)
    check(payload["schema_version"] == 2, "the payload declares schema version 2")
    check(payload["fx"]["usd_inr"] == 88.0, "the FX rate is recorded in the payload")
    check(set(payload["books"]) == {BOOK_IND, BOOK_US}, "per-book subtotals are present")

    us_holding = next(h for h in payload["holdings"] if h["symbol"] == "AAPL")
    check(us_holding["currency"] == "USD" and us_holding["book"] == BOOK_US,
          "holdings carry currency and book")
    check(us_holding["current"] is not None and us_holding["current_inr"] is not None,
          "both native and rupee values are serialised")
    check(us_holding["source"] == "indmoney", "the originating broker is recorded")
    check(payload["news"]["AAPL"]["broker_sentiment"] == 7.0,
          "INDmoney's sentiment is recorded alongside our own score")

    serialised = json.dumps(payload)
    check("NaN" not in serialised and "Infinity" not in serialised,
          "the payload is strict JSON with no NaN or Infinity")


# ===========================================================================
def test_all():
    test_number_parsing()
    test_normalisation()
    test_unknown_cost_basis()
    test_fx()
    fs = test_multi_currency()
    test_no_fx_available()
    test_uncosted_in_fact_sheet()
    test_us_news()
    test_merge_and_disagreement()
    test_report_and_payload(fs)
    assert not _failures, f"{len(_failures)} check(s) failed:\n" + "\n".join(_failures)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
    try:
        test_all()
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
    print("\nAll US book checks passed.")
