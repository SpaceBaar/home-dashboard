#!/usr/bin/env python3
"""Tests for the INDmoney US book: normalisation, currency, US news.

No network, no MCP server, no model. Response structures in
tests/fixtures/indmoney_us_shapes.json were captured from the live INDmoney MCP
server on 2026-08-02 with tools/probe_indmoney.py; the figures were replaced with
representative values so no real holdings live in the repository.

Two live findings drive most of these tests:

1. ``networth_holdings`` reports US positions **already converted to rupees**,
   while ``get_us_stocks_details`` quotes in USD. Treating the holdings as USD
   would multiply the US book by roughly 88.
2. When the cost basis is unknown, INDmoney sends the string ``"unknown"`` and
   then fills ``total_pnl`` with the market value and ``pnl_per`` with 0. Taking
   that at face value would report a 100% gain on the position.

Run with either:
    python tests/test_us_book.py
    pytest tests/test_us_book.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import brokers                                              # noqa: E402
import news as news_mod                                     # noqa: E402
import report as report_mod                                 # noqa: E402
from brokers import BOOK_IND, BOOK_US, IndmoneyProvider     # noqa: E402
from llm import StockScore                                  # noqa: E402
from pfm_config import load_config                          # noqa: E402
from portfolio import (FX_SANITY_RANGE, build_fact_sheet,    # noqa: E402
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
    return brokers.extract_rows(SHAPES[shape_key], hint_keys=("holdings",)) or []


def us_holdings() -> List[dict]:
    holdings, _ = brokers.normalise_indmoney_rows(rows_of("real_networth_holdings_us"))
    return holdings


def india_rows() -> List[dict]:
    return extract_holdings_json((FIXTURES / "holdings.json").read_text(encoding="utf-8"))


# ===========================================================================
# 1. Number parsing
# ===========================================================================
def test_number_parsing() -> None:
    section("Number parsing keeps None distinct from zero")

    cases = [
        (1234.5, 1234.5), ("1234.5", 1234.5), ("1,234.50", 1234.5),
        ("$1,138.40", 1138.4), ("₹1,23,456.78", 123456.78), ("+0.62%", 0.62),
        ("unknown", None), ("N/A", None), ("", None), (None, None),
        ("--", None), (True, None), ("abc", None), (0, 0.0),
    ]
    for raw, expected in cases:
        got = brokers._num(raw)
        ok = (got is None and expected is None) or (
            got is not None and expected is not None and abs(got - expected) < 1e-6)
        check(ok, f"_num({raw!r}) -> {got!r} (expected {expected!r})")

    check(brokers._num("unknown") is None and brokers._num(0) == 0.0,
          "INDmoney's literal 'unknown' is None while a real zero stays 0.0")

    check(brokers._canon("unitPrice") == brokers._canon("unit_price")
          == brokers._canon("Unit Price") == "unitprice",
          "field lookup is by canonical key, so naming convention does not matter")


# ===========================================================================
# 2. The real captured shape
# ===========================================================================
def test_real_us_shape() -> None:
    section("Live networth_holdings shape normalises correctly")

    holdings, problems = brokers.normalise_indmoney_rows(rows_of("real_networth_holdings_us"))
    check(len(holdings) == 3, f"all three US rows parse ({len(holdings)})")

    spacex = next(h for h in holdings if "Space Exploration" in (h.get("name") or ""))
    check(abs(spacex["quantity"] - 0.05061407) < 1e-9,
          "fractional units are preserved exactly")
    check(abs(spacex["current_native"] - 523.38) < 0.01
          and abs(spacex["invested_native"] - 954.2) < 0.01,
          "market_value and invested_amount are read")
    check(abs(spacex["quantity"] * spacex["ltp"] - spacex["current_native"]) < 0.05,
          "units x unit_price reconciles with market_value")

    # The finding that matters most.
    check(spacex["currency"] == "INR",
          "US holdings are treated as RUPEES — INDmoney has already converted them")
    check(all(h["currency"] == "INR" for h in holdings),
          "every US holding row is rupee-denominated")

    check(spacex["symbol"] and spacex["symbol"] != "UNKNOWN",
          f"a label is derived when no ticker is supplied ({spacex['symbol']})")
    check(any("ticker not supplied" in f for f in spacex["flags"]),
          "the derived label is flagged, so it is never mistaken for a real ticker")
    check(spacex["name"] == "Space Exploration Technologies Corp. Class A Common Stock",
          "the full instrument name is retained for lookup and display")

    apple = next(h for h in holdings if h["name"].startswith("Apple"))
    check(apple["symbol"].startswith("APPLE"),
          f"a name yields a sensible stand-in label ({apple['symbol']})")
    check(apple["investment_code"] == "118186",
          "investment_code is carried through for the id join")
    check(abs(apple["quantity"] * apple["ltp"] - apple["current_native"]) < 1.0,
          "Apple's units x unit_price also reconciles with market_value")


def test_placeholder_pnl_discarded() -> None:
    section("The unknown-cost-basis placeholder P&L is discarded")

    holdings, _ = brokers.normalise_indmoney_rows(rows_of("real_networth_holdings_us"))
    tesla = next(h for h in holdings if h["name"].startswith("Tesla"))

    check(tesla["invested_native"] is None and tesla["avg_price"] is None,
          "invested and average price are None for the 'unknown' row")
    check(tesla["broker_pnl_native"] is None,
          "total_pnl of 82000 — equal to market_value — is discarded as a placeholder")
    check(any("placeholder" in f for f in tesla["flags"]),
          "the discard is recorded in the flags")
    check(abs(tesla["current_native"] - 82000.0) < 0.01,
          "the market value itself is still trusted and used")

    fs = build_fact_sheet([tesla])
    check(fs.total_pnl == 0 and fs.total_invested == 0,
          "the placeholder cannot leak into portfolio P&L")
    check(fs.total_current == 0 or fs.holdings[0].pnl_pct is None,
          "no return percentage is produced for it")


def test_indian_rows_excluded() -> None:
    section("INDmoney's Indian rows stay out of the US book")

    rows = brokers.extract_rows(SHAPES["real_networth_holdings_ind"],
                                hint_keys=("holdings",)) or []
    check(len(rows) == 1, "the Indian envelope is unwrapped past positions/open_orders")
    check(rows[0]["asset_type"] == "STOCK",
          "Indian rows carry asset_type 'STOCK', not 'IND_STOCK'")
    check(not brokers.is_us_row(rows[0]),
          "so they are not identified as US rows")

    holdings, _ = brokers.normalise_indmoney_rows(rows, us_only=True)
    check(not holdings,
          "the Zerodha Gold ETF is excluded — Kite already reports it, and counting "
          "it twice would inflate the portfolio")


def test_alternative_shapes() -> None:
    section("Fallback namings still parse if the API shifts")

    holdings, _ = brokers.normalise_indmoney_rows(rows_of("shape_b_camel_units_nested"))
    check(len(holdings) == 1 and holdings[0]["symbol"] == "NVDA",
          "camelCase keys with a nested quote object parse")
    check(holdings[0]["currency"] == "USD",
          "an explicit currencyCode of USD overrides the rupee default")
    check(holdings[0]["ltp"] == 121.4 and holdings[0]["day_pct"] == 2.11,
          "values nested under 'quote' are found")

    holdings, _ = brokers.normalise_indmoney_rows(
        brokers.extract_rows(SHAPES["shape_d_formatted_strings"],
                             hint_keys=("positions",)) or [])
    check(len(holdings) == 1 and abs(holdings[0]["invested_native"] - 1138.40) < 0.01,
          "currency-formatted strings parse")

    holdings, problems = brokers.normalise_indmoney_rows(rows_of("shape_f_unusable"))
    check(not holdings and len(problems) == 1,
          "an uninterpretable row is EXCLUDED, not defaulted to zero")
    check("probe_indmoney" in problems[0],
          "the diagnostic says how to capture the real shape")


# ===========================================================================
# 3. Ticker resolution, watchlist, quotes
# ===========================================================================
class FakeSession:
    """Minimal MCP session stub returning fixture payloads."""

    def __init__(self, replies: dict):
        self.replies = replies
        self.calls: List[tuple] = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        value = self.replies.get(name)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RuntimeError(f"no stub for {name}")

        class Block:
            def __init__(self, text): self.text = text

        class Result:
            def __init__(self, text): self.content = [Block(text)]

        return Result(json.dumps(value))


def test_ticker_resolution_by_id() -> None:
    section("Tickers resolved by joining investment_code to mycroft_id")

    index = brokers.build_code_index(SHAPES["real_us_stocks_details"])
    check(index.get("118186") == "AAPL" and index.get("116683") == "TSLA",
          f"mycroft_id maps to ticker: {index}")

    holdings, _ = brokers.normalise_indmoney_rows(rows_of("real_networth_holdings_us"))
    check(all(h.get("investment_code") for h in holdings),
          "investment_code survives normalisation so the join is possible")
    check(all(brokers.needs_ticker(h) for h in holdings),
          "no row has a real ticker before resolution")

    filled, warnings = brokers.resolve_by_code(holdings, index)
    check(filled == 2 and not warnings, f"two tickers filled by exact id match ({filled})")
    symbols = {h["symbol"] for h in holdings}
    check("AAPL" in symbols and "TSLA" in symbols,
          f"Apple and Tesla resolved without any name matching ({symbols})")
    aapl = next(h for h in holdings if h["symbol"] == "AAPL")
    check(any("instrument id" in f for f in aapl["flags"]),
          "the resolution method is recorded")
    check(not brokers.needs_ticker(aapl),
          "the derived-label warning is cleared once a real ticker is known")
    check(any(brokers.needs_ticker(h) for h in holdings),
          "SPCX has no quote in this fixture, so it stays unresolved rather than guessed")

    # A name-derived ticker that contradicts the id must be corrected loudly.
    holdings2, _ = brokers.normalise_indmoney_rows(rows_of("real_networth_holdings_us"))
    wrong = next(h for h in holdings2 if h["investment_code"] == "118186")
    brokers._apply_ticker(wrong, "MSFT", "guessed from the name")
    filled, warnings = brokers.resolve_by_code(holdings2, index)
    check(wrong["symbol"] == "AAPL" and warnings,
          "a wrong name-derived ticker is corrected and the mismatch reported")
    check("belongs to AAPL" in warnings[0], f"the warning names both: {warnings[0][:70]}")


def test_lookup_fallback() -> None:
    section("lookup_ind_keys fallback avoids the HTTP 414 that long names cause")

    long_name = "Space Exploration Technologies Corp. Class A Common Stock"
    short = brokers.shorten_for_lookup(long_name)
    check(len(short) < len(long_name) and "Common Stock" not in short,
          f"boilerplate suffixes are stripped: {short!r} ({len(short)} chars)")
    check(short == "Space Exploration Technologies", f"expected name kept: {short!r}")
    check(brokers.shorten_for_lookup("Apple Inc. Common Stock") == "Apple",
          f"'Apple Inc. Common Stock' -> "
          f"{brokers.shorten_for_lookup('Apple Inc. Common Stock')!r}")

    for label, payload in (("dict keyed by name", SHAPES["lookup_dict_keyed"]),
                           ("list of records", SHAPES["lookup_list_shaped"])):
        check(bool(IndmoneyProvider._parse_lookup(payload)),
              f"a lookup reply shaped as a {label} is parsed")

    session = FakeSession({
        "networth_holdings": SHAPES["real_networth_holdings_us"],
        "lookup_ind_keys": SHAPES["lookup_dict_keyed"],
    })
    holdings, problems = asyncio.run(IndmoneyProvider(session).holdings())
    symbols = {h["symbol"] for h in holdings}
    check("SPCX" in symbols, f"SpaceX resolves via the shortened name ({symbols})")

    lookup_calls = [c for c in session.calls if c[0] == "lookup_ind_keys"]
    check(all(len(c[1].get("names", [])) <= 1 for c in lookup_calls if
              isinstance(c[1].get("names"), list)),
          "names are sent one per call, which is what avoids the 414")
    check(all(len(str(c[1].get("names"))) < 60 for c in lookup_calls),
          "and each request stays short")

    # The real 414 error payload must not be mistaken for a result.
    session = FakeSession({
        "networth_holdings": SHAPES["real_networth_holdings_us"],
        "lookup_ind_keys": SHAPES["lookup_uri_too_long_error"],
    })
    holdings, problems = asyncio.run(IndmoneyProvider(session).holdings())
    check(len(holdings) == 3, "holdings survive a lookup failure")
    check(any("could not be resolved" in p for p in problems),
          "the failure is reported in data quality rather than hidden")
    check(all(brokers.needs_ticker(h) for h in holdings),
          "an error payload yields no tickers, rather than bogus ones")

    session = FakeSession({
        "networth_holdings": SHAPES["real_networth_holdings_us"],
        "lookup_ind_keys": RuntimeError("tool unavailable"),
    })
    holdings, problems = asyncio.run(IndmoneyProvider(session).holdings())
    check(len(holdings) == 3 and any("could not be resolved" in p for p in problems),
          "an exception is handled the same way")


def test_fx_derivation() -> None:
    section("USD/INR derived from INDmoney's own rupee prices vs its USD quotes")

    holdings, _ = brokers.normalise_indmoney_rows(rows_of("real_networth_holdings_us"))
    brokers.resolve_by_code(holdings, brokers.build_code_index(SHAPES["real_us_stocks_details"]))
    quotes = brokers.extract_us_quotes(SHAPES["real_us_stocks_details"])

    rate, note = brokers.derive_usd_inr(holdings, quotes)
    check(rate is not None and 90 < rate < 100,
          f"a plausible rate is derived with no external source ({rate:.2f})")
    check(abs(rate - 29476.19 / 308.91) < 1.0,
          f"AAPL's rupee unit price over its USD quote gives {29476.19 / 308.91:.2f}")
    check("derived from INDmoney" in note and "AAPL" in note,
          f"the derivation is explained: {note[:60]}...")

    check(brokers.derive_usd_inr([], quotes)[0] is None, "no holdings yields no rate")
    check(brokers.derive_usd_inr(holdings, {})[0] is None, "no quotes yields no rate")

    resolved, _ = resolve_fx(None, rate)
    check(resolved is not None, "the derived rate passes the sanity band and is accepted")


def test_watchlist() -> None:
    section("Watchlist tickers from nested watchlists[].stocks[]")

    session = FakeSession({"user_watchlist": SHAPES["real_user_watchlist"]})
    symbols = asyncio.run(IndmoneyProvider(session).watchlist())
    check(symbols == ["MSFT", "NVDA", "SPCX"], f"all tickers across lists: {symbols}")
    check(session.calls[0][1] == {"type": "all"},
          "the required 'type' parameter is supplied")

    session = FakeSession({"user_watchlist": RuntimeError("nope")})
    check(asyncio.run(IndmoneyProvider(session).watchlist()) == [],
          "a watchlist failure returns empty rather than raising")


def test_us_quotes() -> None:
    section("US quotes from the symbol-keyed get_us_stocks_details reply")

    details = SHAPES["real_us_stocks_details"]
    quotes = brokers.extract_us_quotes(details)

    check(set(quotes) == {"AAPL", "TSLA"}, "both tickers extracted from a dict-keyed reply")
    check(quotes["AAPL"]["live_price_usd"] == 308.91,
          "live_price is read from under entity_stats")
    check(quotes["AAPL"]["day_pct"] == -7.35,
          "day_change_percentage is read — networth_holdings has no day change at all")
    check(quotes["AAPL"]["name"] == "Apple Inc." and quotes["AAPL"]["sector"],
          "identity fields come from entity_basic")
    check(quotes["TSLA"]["week52_high"] == 498.83, "52-week fields parse despite the digit prefix")

    session = FakeSession({"get_us_stocks_details": details})
    collected = asyncio.run(IndmoneyProvider(session).us_details(["AAPL", "TSLA"]))
    check(set(collected) == {"AAPL", "TSLA"},
          "the provider returns the per-symbol dicts, not a flattened list")
    check(any(call[1].get("segments") for call in session.calls),
          "a segments value is attempted, since news is not in the baseline reply")
    check(any("symbols" in call[1] for call in session.calls),
          "the confirmed 'symbols' parameter name is used")
    check(IndmoneyProvider.NEWS_SEGMENT_CANDIDATES[0] == ["news", "analyst"],
          "the segments value confirmed by the sweep is tried first")
    check(all(seg in (["news"], ["news", "analyst"])
              for seg in IndmoneyProvider.NEWS_SEGMENT_CANDIDATES),
          "the tokens the server rejected are no longer attempted")


def test_us_news() -> None:
    section("US news extraction and sentiment mapping")

    check(not brokers._has_news(SHAPES["real_us_stocks_details"]),
          "the baseline quote reply carries no headlines, and that is detected")
    check(brokers._has_news(SHAPES["real_us_stocks_details_with_news"]),
          "a reply that does carry headlines is detected")

    extracted = brokers.extract_us_news(SHAPES["real_us_stocks_details_with_news"])
    check(len(extracted["AAPL"]["articles"]) == 2,
          "both 'title/source/url' and 'headline/publisher/link' namings are read")
    check(extracted["AAPL"]["articles"][0]["link"].startswith("https://"),
          "article links are preserved")
    check(len(extracted["TSLA"]["articles"]) == 1,
          "headlines nested under entity_news are found")

    check(extracted["AAPL"]["sentiment"] == 3.0
          and "label" in extracted["AAPL"]["sentiment_note"],
          f"the label 'negative' maps to {extracted['AAPL']['sentiment']}/10")
    tsla = extracted["TSLA"]
    check(tsla["sentiment"] is not None and tsla["sentiment"] < 5,
          f"a -0.6 score maps to the bearish half ({tsla['sentiment']}/10)")
    check("-1..+1" in tsla["sentiment_note"],
          f"the assumed scale is disclosed rather than presented as fact: "
          f"{tsla['sentiment_note']!r}")

    for value in ("gibberish", None, object()):
        score, _ = brokers._sentiment_to_ten(value)
        check(score is None, f"unmappable sentiment {value!r} yields None")


# ===========================================================================
# 4. FX and the combined fact sheet
# ===========================================================================
def test_fx() -> None:
    section("USD/INR resolution refuses to guess")

    rate, source = resolve_fx(88.5, None)
    check(rate == 88.5 and "config" in source, f"a configured rate is used ({source})")
    rate, _ = resolve_fx(None, 87.2)
    check(rate == 87.2, "an implied rate is used when there is no configured one")
    rate, _ = resolve_fx(88.0, 87.0)
    check(rate == 88.0, "the configured rate wins")
    rate, source = resolve_fx(None, None)
    check(rate is None and source == "unavailable", "no rate is ever invented")
    for bad in (0.0115, 1.0, 5000.0, -88.0):
        check(resolve_fx(bad, None)[0] is None,
              f"an implausible rate {bad} is rejected (band "
              f"{FX_SANITY_RANGE[0]:g}-{FX_SANITY_RANGE[1]:g})")


def test_combined_fact_sheet():
    section("Combined fact sheet across both books")

    us = us_holdings()
    fs = build_fact_sheet(india_rows() + us)

    check(set(fs.books) == {BOOK_IND, BOOK_US}, "both books are present")
    india, usb = fs.books[BOOK_IND], fs.books[BOOK_US]
    check(usb.currency == "INR",
          "the US book is rupee-denominated, so no FX conversion is applied")
    check(usb.count == 3 and usb.uncosted_count == 1,
          f"US book: {usb.count} holdings, {usb.uncosted_count} without cost basis")

    expected = 523.38 + 208889.42 + 82000.0
    check(abs(usb.current - expected) < 0.05,
          f"US subtotal is the plain sum of rupee values ({usb.current:,.2f})")
    check(abs(fs.total_current - (india.current + usb.current)) < 0.05,
          "the combined total is the sum of both books, with no rate applied")

    costed = [h for h in fs.holdings if h.has_cost_basis and h.pnl_inr is not None]
    residual = abs(sum(h.pnl_inr for h in costed) - fs.total_pnl)
    check(residual < 0.01, f"P&L reconciles across both books (residual {residual:.4f})")

    # A USD-priced row still converts correctly, so the mixed case works.
    usd_row, _ = brokers.normalise_indmoney_rows(rows_of("shape_b_camel_units_nested"))
    mixed = build_fact_sheet(india_rows() + us + usd_row, usd_inr=88.0,
                            fx_source="configured (88.00)")
    nvda = next(h for h in mixed.holdings if h.symbol == "NVDA")
    check(nvda.currency == "USD" and nvda.fx_rate == 88.0,
          "a genuinely USD-priced row is converted at the configured rate")
    check(abs(nvda.current_inr - 2428.0 * 88.0) < 0.01,
          f"NVDA converts to Rs {nvda.current_inr:,.0f}")
    spcx = next(h for h in mixed.holdings if h.name and "Space" in h.name)
    check(spcx.fx_rate == 1.0,
          "while the rupee-denominated INDmoney rows are left alone")
    return fs


def test_no_fx_needed_for_indmoney():
    section("A missing FX rate does not break the INDmoney US book")

    fs = build_fact_sheet(india_rows() + us_holdings(), usd_inr=None)
    us_rows = [h for h in fs.holdings if h.book == BOOK_US]
    check(all(h.current_inr is not None for h in us_rows),
          "US holdings still have rupee values without any configured rate — "
          "because INDmoney supplied them in rupees")
    check(not any("no USD/INR rate" in q for q in fs.data_quality),
          "no FX warning is raised when none is needed")

    usd_row, _ = brokers.normalise_indmoney_rows(rows_of("shape_b_camel_units_nested"))
    fs2 = build_fact_sheet(india_rows() + usd_row, usd_inr=None)
    nvda = next(h for h in fs2.holdings if h.symbol == "NVDA")
    check(nvda.current_inr is None,
          "a genuinely USD row with no rate is excluded from the rupee total")
    check(any("not part of the combined rupee total" in q for q in fs2.data_quality),
          "and the exclusion is disclosed")


def test_uncosted_reporting():
    section("Uncosted holdings suppress returns rather than inventing them")

    fs = build_fact_sheet(india_rows() + us_holdings())
    tesla = next(h for h in fs.holdings if (h.name or "").startswith("Tesla"))

    check(tesla.pnl_pct is None and tesla.pnl_native is None,
          "P&L and percentage are None, not a 100% gain")
    check(tesla.current_inr and tesla.current_inr > 0,
          "its value still counts toward portfolio value")
    check(len(fs.uncosted) == 1, f"uncosted holdings listed: {fs.uncosted}")
    check(any("No cost basis was shared" in q for q in fs.data_quality),
          "the data-quality section explains it")

    costed = [h for h in fs.holdings if h.has_cost_basis and h.pnl_inr is not None]
    check(abs(sum(h.pnl_inr for h in costed) - fs.total_pnl) < 0.01,
          "totals still reconcile with an uncosted row present")
    check(fs.total_current > sum(h.current_inr for h in costed),
          "portfolio value exceeds the costed subset, as it must")


# ===========================================================================
# 5. News merge, disagreement, report
# ===========================================================================
def test_merge_and_disagreement() -> None:
    section("Merging US headlines and flagging sentiment disagreement")

    grouped = {
        "AAPL": [news_mod.Article("AAPL", "Apple warns iPhone and Mac sales face a supply crunch",
                                  "Livemint", "https://example.com/rss-dup")],
    }
    extra = brokers.extract_us_news(SHAPES["real_us_stocks_details_with_news"])
    merged = news_mod.merge_articles(dict(grouped), extra)

    check(len(merged["AAPL"]) == 2,
          "the story carried by both RSS and INDmoney is not double-counted")
    check(any("India growth" in a.title for a in merged["AAPL"]),
          "genuinely new INDmoney headlines are added")
    check("TSLA" in merged, "a ticker with no RSS coverage gains a group from INDmoney")

    scores = [StockScore("AAPL", 8, "Reads constructive.", "high", "label", 2),
              StockScore("TSLA", 4, "Mixed.", "high", "label", 1)]
    notes = news_mod.sentiment_disagreements(scores, extra, threshold=3.0)
    check(len(notes) == 1 and notes[0].startswith("AAPL"),
          f"only the wide AAPL gap is flagged ({len(notes)} note(s))")
    check("8/10" in notes[0] and "3.0/10" in notes[0],
          "both scores appear so you can judge which to trust")
    check("label" in notes[0], "the scale assumption is disclosed")


def test_report_and_payload(fs):
    section("Report and JSON payload carry both books")

    scores = [StockScore("AAPL", 6, "Mixed but constructive.", "high", "label", 2)]
    held = {h.symbol for h in fs.holdings}
    narrative = report_mod.deterministic_narrative(fs, scores, held)

    allowed_symbols, allowed_aliases = report_mod.build_allowed_names(
        fs, scores, CFG.keyword_map)
    result = report_mod.validate_narrative(
        narrative, allowed_symbols=allowed_symbols, allowed_aliases=allowed_aliases,
        allowed_numbers=report_mod.build_allowed_numbers(fs, scores))
    check(result.ok, f"the two-book narrative validates "
                     f"({result.violations[:3] if result.violations else 'clean'})")
    check("Cost basis is available for" in narrative,
          "the narrative says which subset the return figures describe")

    content = report_mod.render_report(fs, "_none_", narrative, "deterministic",
                                       scores=scores, model="fake:1.5b")
    check("### India" in content and "### US" in content,
          "separate India and US holdings tables")
    check("**US total**" in content, "the US book has its own reconciling total row")
    check("* Invested and P&L cover only" in content,
          "the footnote explaining the star is present")
    check("costed holdings only" in content,
          "the position table labels invested and P&L as a subset")

    payload = report_mod.build_payload(
        fs, {}, scores, narrative, "deterministic", held=held, model="fake:1.5b",
        broker_sentiment=brokers.extract_us_news(SHAPES["real_us_stocks_details_with_news"]))
    check(payload["schema_version"] == 2, "the payload declares schema version 2")
    check(set(payload["books"]) == {BOOK_IND, BOOK_US}, "per-book subtotals are present")
    us_row = next(h for h in payload["holdings"] if h["book"] == BOOK_US)
    check(us_row["currency"] == "INR" and us_row["source"] == "indmoney",
          "US rows record their currency and originating broker")
    check(any(h["pnl"] is None for h in payload["holdings"]),
          "an uncosted holding serialises P&L as null, not 0")
    check(payload["news"]["AAPL"]["broker_sentiment"] == 3.0,
          "INDmoney's sentiment is recorded next to our own score")

    serialised = json.dumps(payload)
    check("NaN" not in serialised and "Infinity" not in serialised,
          "the payload is strict JSON")


# ===========================================================================
def test_all():
    test_number_parsing()
    test_real_us_shape()
    test_placeholder_pnl_discarded()
    test_indian_rows_excluded()
    test_alternative_shapes()
    test_ticker_resolution_by_id()
    test_lookup_fallback()
    test_fx_derivation()
    test_watchlist()
    test_us_quotes()
    test_us_news()
    test_fx()
    fs = test_combined_fact_sheet()
    test_no_fx_needed_for_indmoney()
    test_uncosted_reporting()
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
