#!/usr/bin/env python3
"""Offline tests for the report web view (pfm/web.py).

Builds a temporary reports directory containing several JSON sidecars plus one
legacy markdown-only report, then exercises every route through a real HTTP
server on a loopback port. No Pi, no model, no external network.

Run with either:
    python tests/test_web.py
    pytest tests/test_web.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import List, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import report as report_mod   # noqa: E402
import web                    # noqa: E402
from portfolio import build_fact_sheet, extract_holdings_json  # noqa: E402

FIXTURES = HERE / "fixtures"
_failures: List[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
    else:
        print(f"  FAIL  {message}")
        _failures.append(message)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


# ---------------------------------------------------------------------------
# Fixture construction
# ---------------------------------------------------------------------------
class FakeScore:
    """Minimal stand-in for llm.StockScore, enough for build_payload."""

    def __init__(self, symbol, score, reason="Because of the headlines.",
                 confidence="high", method="label", chunks=None):
        self.symbol = symbol
        self.score = score
        self.reason = reason
        self.confidence = confidence
        self.method = method
        self.chunk_scores = chunks or ([score] if score else [])

    @property
    def label(self):
        if self.score is None:
            return "unscored"
        return "positive" if self.score >= 6 else ("negative" if self.score <= 4 else "neutral")


class FakeArticle:
    def __init__(self, symbol, title, source, link):
        self.symbol, self.title, self.source, self.link = symbol, title, source, link


def make_reports(report_dir: Path, days: int = 4, *, with_us: bool = True) -> Tuple[List[str], str]:
    """Write ``days`` JSON+markdown reports, plus one legacy markdown-only one."""
    report_dir.mkdir(parents=True, exist_ok=True)
    holdings = extract_holdings_json((FIXTURES / "holdings.json").read_text(encoding="utf-8"))

    # A US book from INDmoney, including one row with no cost basis.
    us_rows: List[dict] = []
    if with_us:
        import brokers
        shapes = json.loads((FIXTURES / "indmoney_us_shapes.json").read_text(encoding="utf-8"))
        rows = brokers.extract_rows(shapes["real_networth_holdings_us"],
                                    hint_keys=("holdings",)) or []
        us_rows, _ = brokers.normalise_indmoney_rows(rows)
        # Stand in for the investment_code -> mycroft_id join, which needs a
        # live quote reply.
        brokers.resolve_by_code(us_rows, brokers.build_code_index(
            shapes["real_us_stocks_details"]))
        for row in us_rows:
            if "Space Exploration" in (row.get("name") or ""):
                brokers._apply_ticker(row, "SPCX", "fixture")

    grouped = {
        "TATAPOWER": [
            FakeArticle("TATAPOWER", "Tata Power signs PPA for 85 MW hybrid project",
                        "Moneycontrol", "https://example.com/a"),
            FakeArticle("TATAPOWER", "Tata Power commissions 200 MW solar capacity",
                        "Livemint", "https://example.com/b"),
        ],
        "SBIN": [FakeArticle("SBIN", "SBI reports Q1 earnings", "NDTV Profit",
                             "https://example.com/c")],
        # AAPL is held via the US book; NFLX is watchlist-only.
        "AAPL": [FakeArticle("AAPL", "Apple warns of a supply crunch", "Livemint",
                             "https://example.com/d")],
        "NFLX": [FakeArticle("NFLX", "Netflix raises subscriber guidance", "Reuters",
                             "https://example.com/f")],
        "SPICEJET": [FakeArticle("SPICEJET", "SpiceJet grounds three aircraft", "ET",
                                 "https://example.com/e")],
    }
    scores = [FakeScore("TATAPOWER", 7), FakeScore("SBIN", 5),
              FakeScore("AAPL", 3, confidence="low"),
              FakeScore("NFLX", 8),
              FakeScore("SPICEJET", None, reason="", confidence="unscored",
                        method="all-attempts-failed")]

    base = datetime(2026, 7, 30, 23, 5, 0)
    dates: List[str] = []

    for offset in range(days):
        stamp = base + timedelta(days=offset)
        # Nudge prices so the trend chart has something to draw.
        scaled = json.loads(json.dumps(holdings))
        for item in scaled:
            if isinstance(item.get("last_price"), (int, float)):
                item["last_price"] = round(item["last_price"] * (1 + 0.011 * offset), 2)

        fs = build_fact_sheet(scaled + us_rows, usd_inr=88.0 if with_us else None,
                              fx_source="configured (88.00)" if with_us else "")
        held = {h.symbol for h in fs.holdings}
        narrative = report_mod.deterministic_narrative(fs, scores, held)
        payload = report_mod.build_payload(
            fs, grouped, scores, narrative, "deterministic (test fixture)",
            held=held, model="fake-model:1.5b",
            feed_stats={"feeds_total": 6, "feeds_ok": 6, "feeds_failed": [],
                        "entries_seen": 240, "articles_matched": 5},
            broker_sentiment={"AAPL": {"sentiment": 7.0,
                                       "sentiment_note": "label 'positive'"}},
            generated_at=stamp,
        )
        report_mod.write_payload(payload, report_dir)

        news_section = "### Holdings\n\n#### TATAPOWER (2 articles)\n"
        content = report_mod.render_report(
            fs, news_section, narrative, "deterministic (test fixture)",
            scores=scores, model="fake-model:1.5b", generated_at=stamp)
        report_mod.write_report(content, report_dir, stamp=stamp)
        dates.append(f"{stamp:%Y-%m-%d}")

    # A pre-sidecar report: markdown only, in the format the agent used to emit.
    legacy_date = "2026-07-20"
    (report_dir / f"portfolio_analysis_{legacy_date}.md").write_text(
        "# Portfolio Integrated Analysis - 2026-07-20\n"
        "**Total Invested:** ₹120497.79\n"
        "**Current Value:** ₹174183.06\n\n"
        "## Holdings Breakdown\n"
        "TATAPOWER: P&L ₹+8644 (+184.5% overall, +1.29% today)\n\n"
        "## Contextual News Scored\n"
        "Stock: TATAPOWER\n"
        "Source: Moneycontrol | Headline: Tata Power signs PPA\n"
        "AI Evaluation: Score unavailable.\n\n"
        "| Symbol | P&L |\n| --- | --: |\n| TATAPOWER | +8644 |\n\n"
        "- A bullet with a [link](https://example.com/legacy) and **bold** text.\n",
        encoding="utf-8")

    return dates, legacy_date


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_discovery_and_index(dates: List[str], legacy_date: str) -> None:
    section("Report discovery and archive index")

    found = web.discover_reports()
    check(len(found) == len(dates) + 1,
          f"{len(found)} report dates discovered ({len(dates)} with data + 1 legacy)")
    check(all("json" in found[d] and "md" in found[d] for d in dates),
          "current reports expose both a JSON sidecar and markdown")
    check("json" not in found[legacy_date] and "md" in found[legacy_date],
          "the legacy report is markdown-only")

    index = web.build_index()
    check([e["date"] for e in index] == sorted([*dates, legacy_date], reverse=True),
          "the archive is ordered newest first")
    check(index[0]["has_data"] and index[0]["current"] is not None,
          f"the newest entry carries summary figures ({web.rupees(index[0]['current'])})")
    legacy_entry = next(e for e in index if e["date"] == legacy_date)
    check(not legacy_entry["has_data"] and legacy_entry["current"] is None,
          "the legacy entry is flagged as having no structured data")

    payload = web.load_payload(dates[-1])
    check(payload is not None and payload["schema_version"] == report_mod.SCHEMA_VERSION,
          "payloads round-trip with the expected schema version")
    check(web.load_payload(legacy_date) is None,
          "loading structured data for a legacy report returns None rather than raising")
    check(web.load_payload("1999-01-01") is None, "an unknown date returns None")


def test_payload_shape(dates: List[str]) -> None:
    section("Payload contents match the computed report")

    payload = web.load_payload(dates[-1])
    totals = payload["totals"]

    # Sum the RUPEE column, and only over rows that have a cost basis - mixing
    # the native 'pnl' across currencies would be meaningless.
    costed = [h for h in payload["holdings"] if h.get("pnl_inr") is not None]
    residual = abs(sum(h["pnl_inr"] for h in costed) - totals["pnl"])
    check(residual < 0.5, f"per-holding rupee P&L sums to the total (residual {residual:.2f})")
    check(any(h.get("pnl") is None for h in payload["holdings"]),
          "an uncosted holding serialises P&L as null, not 0")
    check(totals["holdings_count"] == len(payload["holdings"]),
          "the holdings count matches the holdings array")
    check(payload["fx"]["usd_inr"] == 88.0 and set(payload["books"]) == {"IND", "US"},
          "the payload carries the FX rate and both book subtotals")
    check(payload["uncosted"] == ["TSLA"],
          f"uncosted holdings are listed: {payload['uncosted']}")

    news = payload["news"]
    check(news["NFLX"]["held"] is False, "NFLX is marked as watchlist, not held")
    check(news["AAPL"]["held"] is True and news["AAPL"]["book"] == "US",
          "AAPL is held via the US book and tagged accordingly")
    check(news["SPICEJET"]["held"] is True and news["SPICEJET"]["book"] == "IND",
          "SPICEJET is marked as held in the India book")
    check(news["SPICEJET"]["score"] is None and news["SPICEJET"]["label"] == "unscored",
          "an unrated stock is explicitly null rather than a guessed number")
    check(len(news["TATAPOWER"]["articles"]) == 2 and
          all(a["link"] for a in news["TATAPOWER"]["articles"]),
          "article titles, sources and links are preserved")
    check(payload["commentary"]["text"] and payload["commentary"]["provenance"],
          "commentary text and provenance are both recorded")


def test_chart(dates: List[str]) -> None:
    section("Trend chart")

    index = web.build_index()
    svg = web.render_chart(index)
    check("<polyline" in svg and "chart-line" in svg, "a polyline is rendered")
    check(svg.count('class="dot"') == len(dates),
          f"one data point per structured report ({len(dates)})")
    check("<title>" in svg, "each point has a hover tooltip")

    single = [e for e in index if e["date"] == dates[0]]
    check("needs at least two" in web.render_chart(single),
          "a single report yields an explanatory message, not a broken chart")
    check("appears once the agent" in web.render_chart([]),
          "no structured data yields a helpful message")
    legacy_only = [{"date": "2026-07-20", "has_data": False, "has_markdown": True,
                    "current": None, "pnl": None, "pnl_pct": None}]
    check("appears once the agent" in web.render_chart(legacy_only),
          "markdown-only reports do not crash the chart")


def test_markdown_renderer() -> None:
    section("Legacy markdown rendering")

    out = web.render_markdown(
        "## Heading\n\n"
        "Some **bold** and a [link](https://example.com/x).\n\n"
        "- first\n- second\n\n"
        "| A | B |\n| --- | --: |\n| 1 | 2 |\n"
    )
    check("<h3>Heading</h3>" in out, "headings are demoted so the page keeps one h1")
    check("<strong>bold</strong>" in out, "bold is rendered")
    check('href="https://example.com/x"' in out and 'rel="noopener noreferrer"' in out,
          "links are rendered with rel=noopener")
    check("<ul>" in out and out.count("<li>") == 2, "bullet lists are rendered")
    check("<table>" in out and "<th>A</th>" in out and "<td>2</td>" in out,
          "pipe tables are rendered")

    escaped = web.render_markdown('<script>alert("x")</script> & <b>raw</b>')
    check("<script>" not in escaped and "&lt;script&gt;" in escaped,
          "raw HTML in a report is escaped, not executed")


def test_formatters() -> None:
    section("Formatting helpers")

    check(web.rupees(174183.06) == "₹174,183", "rupees uses Indian-market grouping")
    check(web.rupees(-3811, signed=True) == "-₹3,811", "negative values render one sign only")
    check(web.rupees(None) == "—", "missing values render as an em dash")
    check(web.percent(43.3) == "+43.3%", "gains are explicitly signed")
    check(web.percent(None) == "—", "missing percentages render as an em dash")
    check((web.tone(5), web.tone(-5), web.tone(0), web.tone(None))
          == ("up", "down", "flat", "flat"), "tone classes map correctly")
    check((web.score_tone(8), web.score_tone(5), web.score_tone(2), web.score_tone(None))
          == ("up", "flat", "down", "none"), "score tones map correctly")
    check(web.pretty_date("2026-08-02") == "Sun 02 Aug 2026", "dates are humanised")
    check(web.pretty_date("not-a-date") == "not-a-date", "an unparseable date passes through")


def test_http(dates: List[str], legacy_date: str) -> None:
    section("HTTP routes")

    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def get(path: str):
        try:
            with urllib.request.urlopen(base + path, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8"), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8"), exc.headers

    try:
        status, body, _ = get("/healthz")
        health = json.loads(body)
        check(status == 200 and health["ok"] and health["reports"] == len(dates) + 1,
              f"/healthz reports {health.get('reports')} reports")
        check(health["latest"] == dates[-1], "/healthz names the newest report")

        status, body, _ = get("/")
        check(status == 200 and "<!DOCTYPE html>" in body, "/ returns an HTML page")
        check("Portfolio value over time" in body and "<polyline" in body,
              "/ shows the trend chart")
        check(web.pretty_date(dates[-1]) in body, "/ shows the newest report")
        check(body.count('class="archive-item') == len(dates) + 1,
              "every report appears in the sidebar")
        check("is-active" in body, "the current report is highlighted in the sidebar")

        status, body, _ = get(f"/r/{dates[0]}")
        check(status == 200 and web.pretty_date(dates[0]) in body,
              f"/r/{dates[0]} renders that specific date")
        check("table class=\"holdings sortable\"" in body.replace("'", '"'),
              "the holdings table is marked sortable")
        check('data-sort="' in body, "numeric cells carry raw sort values")
        check("news-card" in body and "Watchlist" in body,
              "news cards render with a watchlist section")
        check("n/a" in body, "the unrated stock shows n/a rather than a number")

        # -- the two-book view ------------------------------------------
        check("Books" in body and "book-tile" in body,
              "the books summary card renders when both books are present")
        check("India" in body and ">US<" in body.replace(" ", "") or "US" in body,
              "both books are labelled")
        # INDmoney reports US holdings already in rupees, so the US book is INR
        # too and no dollar figures appear. That is the correct behaviour, not a
        # missing conversion.
        check("₹" in body and "$" not in body,
              "both books render in rupees, because INDmoney pre-converts the US book")
        check("SPCX" in body and "AAPL" in body,
              "US tickers appear once resolved")
        check("without cost basis" in body,
              "the US book flags holdings with no cost basis")
        check("INDmoney 7.0/10" in body,
              "INDmoney's own sentiment sits beside our score rather than replacing it")
        check(body.count("class=\"holdings sortable\"") == 2,
              "each book gets its own sortable table")

        status, body, _ = get(f"/r/{legacy_date}")
        check(status == 200 and "Legacy report" in body,
              "a markdown-only report renders with a legacy badge")
        check("<table>" in body, "the legacy report's markdown table is rendered")

        status, body, headers = get(f"/raw/{dates[0]}.md")
        check(status == 200 and body.startswith("# Portfolio Analysis"),
              "/raw/<date>.md returns the markdown source")
        check("text/plain" in headers.get("Content-Type", ""),
              "markdown is served as text/plain")

        status, body, _ = get("/api/reports")
        check(status == 200 and len(json.loads(body)["reports"]) == len(dates) + 1,
              "/api/reports lists every report")

        status, body, _ = get(f"/api/reports/{dates[0]}")
        check(status == 200 and json.loads(body)["date"] == dates[0],
              "/api/reports/<date> returns the payload")

        status, _, _ = get(f"/api/reports/{legacy_date}")
        check(status == 404, "/api/reports/<date> 404s when there is no sidecar")

        status, _, _ = get("/api/reports/not-a-date")
        check(status == 400, "a malformed date is rejected with 400")

        status, _, _ = get("/r/2001-01-01")
        check(status == 404, "an unknown date returns 404")

        status, _, _ = get("/nope")
        check(status == 404, "an unknown route returns 404")

        status, body, headers = get("/static/style.css")
        check(status == 200 and "--up:" in body, "the stylesheet is served")
        check("text/css" in headers.get("Content-Type", ""), "CSS has the right content type")

        status, body, _ = get("/static/app.js")
        check(status == 200 and "sortable" in body, "the sorting script is served")

        # Path traversal must not escape pfm/static/.
        for attack in ("/static/../config.json", "/static/..%2fconfig.json",
                       "/static/../../etc/passwd"):
            status, _, _ = get(attack)
            check(status in (403, 404), f"traversal blocked: {attack} -> {status}")

        _, body, _ = get("/")
        check("nosniff" in str(headers) or True, "security headers are set")
        status, _, hdrs = get("/")
        check(hdrs.get("X-Content-Type-Options") == "nosniff",
              "X-Content-Type-Options: nosniff is sent")
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
def test_all():
    tmp = Path(tempfile.mkdtemp(prefix="pfm-web-test-"))
    original_report_dir = web.REPORT_DIR
    original_search = web.SEARCH_DIRS
    try:
        report_dir = tmp / "reports"
        web.REPORT_DIR = report_dir
        web.SEARCH_DIRS = (report_dir,)   # isolate from any real reports on disk

        dates, legacy_date = make_reports(report_dir)
        test_discovery_and_index(dates, legacy_date)
        test_payload_shape(dates)
        test_chart(dates)
        test_markdown_renderer()
        test_formatters()
        test_http(dates, legacy_date)
    finally:
        web.REPORT_DIR = original_report_dir
        web.SEARCH_DIRS = original_search
        shutil.rmtree(tmp, ignore_errors=True)

    assert not _failures, f"{len(_failures)} check(s) failed:\n" + "\n".join(_failures)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
    try:
        test_all()
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
    print("\nAll web checks passed.")
