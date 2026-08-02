#!/usr/bin/env python3
"""Offline test harness for the personal finance agent.

Runs the entire pipeline with no Raspberry Pi, no hailo-ollama, no Kite and no
network. Every case here corresponds to a defect observed in the real
portfolio_analysis_2026-08-02.md report.

Run with either:
    python tests/test_pipeline.py
    pytest tests/test_pipeline.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Sequence

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import news as news_mod                                     # noqa: E402
import report as report_mod                                 # noqa: E402
from llm import LLMClient, StockScore, parse_score          # noqa: E402
from pfm_config import load_config                          # noqa: E402
from portfolio import build_fact_sheet, extract_holdings_json  # noqa: E402

FIXTURES = HERE / "fixtures"
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


# ===========================================================================
# 1. Score parsing. The old parser required a "Stock: X" line immediately
#    followed by "Score: N"; anything else produced "Score unavailable."
# ===========================================================================
def test_score_parsing() -> None:
    section("Score parsing tolerates real small-model output")

    cases = [
        ("SCORE: 8\nREASON: Strong order book.", 8),
        ("Score: 8/10\nReason: Strong order book.", 8),
        ("**SCORE:** 7\n**REASON:** Mildly positive.", 7),
        ("1. TATAPOWER - Score 6/10 - new PPA signed", 6),        # numbered list
        ("Rating: 3 out of 10. Regulatory notice is negative.", 3),
        ("The sentiment here is 9", 9),
        ("4", 4),                                                  # bare integer
        ("I would say this is bearish for the stock.", 3),          # words only
        ("SCORE: 10\nREASON: Upgrade.", 10),
        ("Score: 0", None),                                        # out of range
        ("", None),
        ("No opinion available.", None),
    ]
    for raw, expected in cases:
        score, _reason, method = parse_score(raw)
        check(score == expected,
              f"{raw[:44]!r:48} -> {score} (expected {expected}, via {method})")

    # A reason is extracted even when the label is missing.
    _s, reason, _m = parse_score("SCORE: 7\nThe new PPA adds contracted capacity.")
    check(bool(reason) and "PPA" in reason, f"reason recovered without a label: {reason!r}")


# ===========================================================================
# 2. Portfolio arithmetic
# ===========================================================================
def test_portfolio_math():
    section("Portfolio arithmetic is internally consistent")

    raw = (FIXTURES / "holdings.json").read_text(encoding="utf-8")
    holdings = extract_holdings_json(raw)
    check(holdings is not None and len(holdings) == 18,
          f"holdings parsed from JSON payload ({len(holdings or [])} rows)")

    fs = build_fact_sheet(holdings, mismatch_tolerance_pct=1.0)

    residual = abs(sum(h.pnl for h in fs.holdings) - fs.total_pnl)
    check(residual < 0.01, f"sum of per-holding P&L equals the total (residual {residual:.4f})")

    derived_pct = fs.total_pnl / fs.total_invested * 100
    check(abs(derived_pct - fs.total_pnl_pct) < 1e-9, "total percentage is derived, not asserted")

    symbols = {h.symbol for h in fs.holdings}
    check("PLEDGEDCO" in symbols,
          "a fully pledged holding (quantity=0, collateral=50) is still counted")
    pledged = next(h for h in fs.holdings if h.symbol == "PLEDGEDCO")
    check(pledged.quantity == 50 and abs(pledged.invested - 5000) < 0.01,
          f"pledged quantity resolved to {pledged.quantity:g}, invested {pledged.invested:,.0f}")

    t1 = next(h for h in fs.holdings if h.symbol == "T1STOCK")
    check(t1.quantity == 10 and any("T+1" in f for f in t1.flags),
          "T+1 settling quantity is counted and flagged")

    mismatch = next(h for h in fs.holdings if h.symbol == "MISMATCH")
    check(any("broker P&L" in f for f in mismatch.flags),
          "broker P&L that disagrees with quantity x price is flagged, not silently mixed in")
    check(any("MISMATCH" in q for q in fs.data_quality),
          "the disagreement is surfaced in the data-quality section")

    check(fs.profitable_count + fs.losing_count <= len(fs.holdings),
          f"{fs.profitable_count} in profit / {fs.losing_count} in loss of {len(fs.holdings)}")
    check(all(h.pnl_pct >= fs.winners[-1].pnl_pct for h in fs.winners),
          "winners are ordered by percentage gain")
    check(0 <= fs.concentration_pct <= 100,
          f"concentration is a valid percentage ({fs.concentration_pct:.1f}%)")

    print(f"        invested Rs {fs.total_invested:,.2f} | value Rs {fs.total_current:,.2f} "
          f"| P&L Rs {fs.total_pnl:+,.2f} ({fs.total_pnl_pct:+.2f}%)")
    return fs


# ===========================================================================
# 3. News attribution and deduplication
# ===========================================================================
def _fixture_feeds(monkeypatched_universe: Sequence[str]) -> Dict[str, List[news_mod.Article]]:
    """Run collect_articles against local XML instead of the network."""
    original = news_mod.fetch_feed

    def fake_fetch(source, *, timeout, attempts):
        path = FIXTURES / source["rss_url"]
        root = ET.fromstring(path.read_bytes())
        return list(news_mod._iter_entries(root))

    news_mod.fetch_feed = fake_fetch
    try:
        keyword_map = {s: CFG.keywords_for(s) for s in monkeypatched_universe}
        keyword_map = {s: k for s, k in keyword_map.items() if k}
        return news_mod.collect_articles(
            [{"name": "RSS Fixture", "rss_url": "feed_rss.xml"},
             {"name": "Atom Fixture", "rss_url": "feed_atom.xml"}],
            list(keyword_map), keyword_map, CFG.exclude_map,
            similarity_threshold=0.9, max_per_stock=12,
        )
    finally:
        news_mod.fetch_feed = original


def test_news_attribution():
    section("News attribution, exclusions and deduplication")

    universe = ["TATAPOWER", "SBIN", "LICI", "IDEA", "YESBANK", "COALINDIA",
                "AAPL", "TSLA", "NVDA", "PAYTM"]
    grouped = _fixture_feeds(universe)
    for symbol in sorted(grouped):
        print(f"        {symbol:<11} {len(grouped[symbol])} article(s)")

    tata = grouped.get("TATAPOWER", [])
    check(len(tata) == 2,
          f"the syndicated duplicate Tata Power story is collapsed (kept {len(tata)}: "
          f"the PPA story once, plus the Coal India joint venture)")

    sbin_titles = [a.title for a in grouped.get("SBIN", [])]
    check(not any("SBI Cards" in t for t in sbin_titles),
          "an SBI Cards story is NOT attributed to SBIN")
    check(any("Q1 Results This Week" in t for t in sbin_titles),
          "a genuine State Bank of India story IS attributed to SBIN")

    lici_titles = [a.title for a in grouped.get("LICI", [])]
    check(not any("Publicis" in t for t in lici_titles),
          "'Publicis'/'police'/'polyclinic' do not match the LIC keyword")
    check(any("Q1 Results This Week" in t for t in lici_titles),
          "the same multi-company story is attributed to LICI as well as SBIN")

    coal = [a.title for a in grouped.get("COALINDIA", [])]
    check(any("joint pithead solar" in t for t in coal),
          "Atom feed entries are parsed (the old code only looked for <item>)")

    check("NVDA" not in grouped, "a watchlist symbol with no news is simply absent")
    check("PAYTM" not in grouped, "a held symbol with no news is simply absent")
    check(len(grouped.get("AAPL", [])) == 2, "both distinct Apple stories are kept")
    return grouped


# ===========================================================================
# 4. Scoring: one call per stock, nothing left unscored
# ===========================================================================
class FakeLLM(LLMClient):
    """Mimics a small local model: inconsistent formats, occasional silence."""

    def __init__(self, cfg, tmp_dir: Path, behaviour: str = "messy"):
        super().__init__(cfg, cache_dir=tmp_dir)
        self.behaviour = behaviour
        self.calls: List[str] = []
        self.model = "fake-model:1.5b"

    async def generate(self, prompt: str, *, num_predict: int, label: str = "") -> str:
        self.calls.append(label)

        if self.behaviour == "silent":
            return ""                                      # models the runtime being down

        if "Reply with ONLY one integer" in prompt:
            return "6"                                     # the retry path

        if "Commentary:" in prompt:
            return self._narrative(prompt)

        if self.behaviour in ("chinese", "chinese_always"):
            # A valid score with a Chinese rationale: the number must survive,
            # the prose must not.
            return "SCORE: 7\nREASON: 该公司订单强劲，收入增长良好。"

        # Rotate through the malformed shapes a 1.5B model actually emits.
        shapes = [
            "SCORE: 8\nREASON: Contracted capacity additions support earnings.",
            "Score: 3/10 - Regulatory notice weighs on the stock.",
            "**Rating:** 5 out of 10. Routine earnings-calendar mention.",
            "1. Overall score 7 — demand commentary is constructive.",
            "This looks broadly neutral for the share price.",
            "I cannot rate this.",                          # forces the retry path
        ]
        return shapes[len(self.calls) % len(shapes)]

    def _narrative(self, prompt: str) -> str:
        if self.behaviour == "chinese":
            # qwen2.5 is a Chinese-origin model and does this occasionally. If the
            # retry prompt is in play, answer properly the second time.
            if "not written in English" in prompt:
                return ("The portfolio is showing a gain overall, led by the "
                        "top-rated holdings.\n\nNews ratings were mixed today.")
            return ("本投资组合价值为 700,485 卢比，较成本高出 72,473 卢比，"
                    "表现最好的是 COALINDIA。\n\n今日新闻评级参差不齐。")
        if self.behaviour == "chinese_always":
            return "本投资组合价值为 700,485 卢比。\n\n今日新闻评级参差不齐。"
        if self.behaviour == "hallucinate":
            # Verbatim shape of the real 2026-08-02 failure.
            return ("The portfolio had a total return of +83.7% over the past year, driven by "
                    "ADANIPORTS (up 23.2%), LIQUIDBEES (up 40.1%) and IDEAS. However it also "
                    "suffered losses from PAYTM (-6.5%) and RBA (-42.3%).\n\n"
                    "Key losers were TATAPOWERS, down 8644%, and LCCI, down 42.3%. "
                    "BSNL coverage was unscored.")
        return ("The portfolio is showing a gain overall, with the strongest contributions "
                "coming from the top-rated holdings and the weakest from the names listed as "
                "worst performers in the data.\n\n"
                "News ratings were mixed across the stocks that had coverage today, and the "
                "stocks marked as not rated should be treated as having no signal.")

    async def preflight(self) -> str:
        return self.model


def test_scoring(grouped, tmp_dir: Path):
    section("Scoring: one LLM call per stock, aggregated in Python")

    llm = FakeLLM(CFG, tmp_dir, behaviour="messy")
    scores = asyncio.run(news_mod.score_all(grouped, llm, held=["TATAPOWER", "SBIN", "LICI",
                                                               "IDEA", "YESBANK", "COALINDIA"]))

    unscored = [s.symbol for s in scores if s.score is None]
    check(not unscored,
          f"every stock with news received a score (unscored: {unscored or 'none'})")
    check(len(scores) == len(grouped),
          f"one score per stock ({len(scores)} scores for {len(grouped)} stocks)")
    check(all(1 <= s.score <= 10 for s in scores if s.score is not None),
          "all scores are within 1-10")

    # One prompt per stock (plus retries), never a shared multi-stock prompt.
    primary = [c for c in llm.calls if "retry" not in c]
    check(len(primary) >= len(grouped),
          f"{len(primary)} primary scoring calls for {len(grouped)} stocks — one stock per call")
    check(any("retry" in c for c in llm.calls),
          "the number-only retry fires when the primary parse fails")

    for s in scores:
        print(f"        {s.compact()[:100]}  [{s.method}, {s.confidence}]")

    # Caching makes a same-day re-run byte-identical and free.
    before = len(llm.calls)
    again = asyncio.run(news_mod.score_all(grouped, llm, held=[]))
    check(len(llm.calls) == before, "a second run is served entirely from cache (0 new calls)")
    check({s.symbol: s.score for s in again} == {s.symbol: s.score for s in scores},
          "cached re-run reproduces identical scores")

    # A totally unresponsive model must yield an explicit "unscored", not a guess.
    silent = FakeLLM(CFG, tmp_dir / "silent", behaviour="silent")
    silent_scores = asyncio.run(news_mod.score_all({"TATAPOWER": grouped["TATAPOWER"]}, silent))
    check(silent_scores[0].score is None and silent_scores[0].confidence == "unscored",
          "a silent model yields an explicit unscored result rather than an invented number")
    return scores


# ===========================================================================
# 5. The anti-hallucination validator
# ===========================================================================
def test_validator(fs, scores):
    section("Narrative validation catches the real 2026-08-02 hallucinations")

    held = {h.symbol for h in fs.holdings}
    allowed_symbols, allowed_aliases = report_mod.build_allowed_names(fs, scores, CFG.keyword_map)
    allowed_numbers = report_mod.build_allowed_numbers(fs, scores)

    bad = ("The portfolio had a total return of +83.7% over the past year, driven by "
           "ADANIPORTS (up 23.2%), LIQUIDBEES (up 40.1%) and IDEAS. It also suffered losses "
           "from PAYTM (-6.5%) and RBA (-42.3%). Key losers: TATAPOWERS down 8644%, "
           "LCCI down 42.3%. BSNL was unscored.")
    result = report_mod.validate_narrative(
        bad, allowed_symbols=allowed_symbols, allowed_aliases=allowed_aliases,
        allowed_numbers=allowed_numbers)
    check(not result.ok, "the real hallucinated paragraph is rejected")
    joined = " | ".join(result.violations)
    for ghost in ("ADANIPORTS", "IDEAS", "TATAPOWERS", "LCCI", "BSNL"):
        check(ghost in joined, f"invented ticker '{ghost}' is named in the violations")
    check(any("8644" in v for v in result.violations),
          "the rupee figure presented as '8644%' is caught as an implausible percentage")
    check(any("6.5" in v for v in result.violations),
          "the invented '-6.5%' for PAYTM is caught as a figure absent from the data")

    pct_problems = report_mod.validate_portfolio_level_pct(bad, fs.total_pnl_pct)
    check(bool(pct_problems),
          f"a per-stock percentage sold as the portfolio return is caught: {pct_problems[:1]}")

    good = report_mod.deterministic_narrative(fs, scores, held)
    result = report_mod.validate_narrative(
        good, allowed_symbols=allowed_symbols, allowed_aliases=allowed_aliases,
        allowed_numbers=allowed_numbers)
    check(result.ok,
          f"the deterministic narrative passes its own validator "
          f"({result.violations[:3] if result.violations else 'no violations'})")
    check(not report_mod.validate_portfolio_level_pct(good, fs.total_pnl_pct),
          "the deterministic narrative states the correct portfolio-level return")


# ===========================================================================
# 5b. Non-English output can never reach the report
# ===========================================================================
def test_language_guard(fs, grouped, tmp_dir: Path):
    section("Chinese output is rejected before it can be published")

    held = {h.symbol for h in fs.holdings}
    chinese = ("本投资组合价值为 700,485 卢比，较成本高出 72,473 卢比。\n\n"
               "今日新闻评级参差不齐。")

    found = report_mod.non_latin_characters(chinese)
    check(bool(found), f"Chinese characters are detected ({' '.join(found[:4])})")
    check(not report_mod.is_english_only(chinese), "the text is not English-only")
    check(report_mod.is_english_only("Portfolio worth Rs 700,485, up Rs 72,473 (+11.5%)."),
          "an ordinary English sentence with rupee figures passes")
    check(not report_mod.is_english_only("Portfolio 表现良好 overall"),
          "a single Chinese word in otherwise English prose is caught")
    for sample, script in (("Портфель вырос", "Cyrillic"),
                           ("ポートフォリオ", "Japanese"),
                           ("포트폴리오", "Korean"),
                           ("पोर्टफोलियो", "Devanagari")):
        check(not report_mod.is_english_only(sample), f"{script} is also rejected")

    allowed_symbols, allowed_aliases = report_mod.build_allowed_names(fs, [], CFG.keyword_map)
    result = report_mod.validate_narrative(
        chinese, allowed_symbols=allowed_symbols, allowed_aliases=allowed_aliases,
        allowed_numbers=report_mod.build_allowed_numbers(fs, []))
    check(not result.ok, "the validator rejects it")
    check("not in English" in result.violations[0],
          f"and says why: {result.violations[0][:52]}")

    # A model that recovers on the retry is allowed through.
    recovers = FakeLLM(CFG, tmp_dir / "cn1", behaviour="chinese")
    scores = asyncio.run(news_mod.score_all(grouped, recovers, held=held))
    narrative, provenance, _ = asyncio.run(report_mod.build_narrative(
        recovers, fs, scores, held, CFG.keyword_map, enabled=True, max_attempts=2))
    check(report_mod.is_english_only(narrative), "the published narrative is English")
    check("validated" in provenance,
          f"a model that recovers on the retry is accepted ({provenance})")

    # A model that never recovers falls back to the deterministic template.
    stubborn = FakeLLM(CFG, tmp_dir / "cn2", behaviour="chinese_always")
    scores2 = asyncio.run(news_mod.score_all(grouped, stubborn, held=held))
    narrative2, provenance2, rejected = asyncio.run(report_mod.build_narrative(
        stubborn, fs, scores2, held, CFG.keyword_map, enabled=True, max_attempts=2))
    check(report_mod.is_english_only(narrative2),
          "persistent Chinese output still yields an English report")
    check("deterministic fallback" in provenance2,
          f"because it falls back to the template ({provenance2})")
    check(any("not in English" in r for r in rejected),
          "and the reason is recorded for the data-quality section")

    # Scores are numbers, so they survive; the rationale does not.
    check(all(s.score == 7 for s in scores2),
          "a Chinese-language answer still yields its numeric score")
    check(all(report_mod.is_english_only(s.reason) for s in scores2),
          "but no Chinese rationale reaches the news section")
    check(any("non-english-reason-dropped" in s.method for s in scores2),
          "the drop is recorded in the score's audit trail")

    content = report_mod.render_report(
        fs, news_mod.render_news_section(grouped, scores2, held=held),
        narrative2, provenance2, scores=scores2, model="fake:1.5b", rejected=rejected)
    check(report_mod.is_english_only(content),
          "the entire rendered report contains no non-Latin script")

    payload = report_mod.build_payload(fs, grouped, scores2, narrative2, provenance2,
                                       held=held, model="fake:1.5b", rejected=rejected)
    check(report_mod.is_english_only(json.dumps(payload, ensure_ascii=False)),
          "and neither does the JSON sidecar the web view reads")


# ===========================================================================
# 6. End-to-end report
# ===========================================================================
def test_end_to_end(fs, grouped, tmp_dir: Path):
    section("End-to-end report generation")

    held = {h.symbol for h in fs.holdings}

    # A model that hallucinates must not be able to reach the report.
    liar = FakeLLM(CFG, tmp_dir / "liar", behaviour="hallucinate")
    scores = asyncio.run(news_mod.score_all(grouped, liar, held=held))
    narrative, provenance, rejected = asyncio.run(report_mod.build_narrative(
        liar, fs, scores, held, CFG.keyword_map, enabled=True, max_attempts=2))
    check("deterministic fallback" in provenance,
          f"hallucinated commentary is replaced by the deterministic template ({provenance})")
    check(bool(rejected), "the rejection reasons are recorded for the report")
    for ghost in ("ADANIPORTS", "TATAPOWERS", "LCCI", "BSNL", "IDEAS"):
        check(ghost not in narrative, f"'{ghost}' never reaches the published narrative")

    # A well-behaved model is allowed through.
    honest = FakeLLM(CFG, tmp_dir / "honest", behaviour="messy")
    scores2 = asyncio.run(news_mod.score_all(grouped, honest, held=held))
    narrative2, provenance2, _ = asyncio.run(report_mod.build_narrative(
        honest, fs, scores2, held, CFG.keyword_map, enabled=True, max_attempts=2))
    check("validated" in provenance2 or "deterministic" in provenance2,
          f"provenance is always explicit ({provenance2})")

    news_section = news_mod.render_news_section(grouped, scores2, held=held)
    content = report_mod.render_report(
        fs, news_section, narrative2, provenance2,
        scores=scores2, model=honest.model, rejected=None)

    check("Score unavailable" not in content,
          "the phrase 'Score unavailable' no longer appears anywhere in the report")
    check("Watchlist (not held)" in content,
          "watchlist stocks are labelled as not held")
    check(f"{fs.total_current:,.2f}" in content, "the computed current value appears verbatim")
    check("**India total**" in content and "**Combined (Rs):**" in content,
          "the holdings table carries a reconciling total row")
    check("## Data quality" in content, "a data-quality section is always present")
    for symbol in grouped:
        check(f"#### {symbol}" in content, f"news block rendered for {symbol}")

    out = report_mod.write_report(content, tmp_dir)
    check(out.exists() and out.stat().st_size > 800, f"report written ({out.stat().st_size} bytes)")
    return out


# ===========================================================================
# pytest-compatible wrappers
# ===========================================================================
def test_all():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_score_parsing()
        fs = test_portfolio_math()
        grouped = test_news_attribution()
        scores = test_scoring(grouped, tmp / "cache")
        test_validator(fs, scores)
        test_language_guard(fs, grouped, tmp / "lang")
        report_path = test_end_to_end(fs, grouped, tmp / "reports")
        preview = report_path.read_text(encoding="utf-8")
    assert not _failures, f"{len(_failures)} check(s) failed:\n" + "\n".join(_failures)
    return preview


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        preview = test_all()
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
    print(f"\nAll checks passed.\n")
    print("=" * 72)
    print(preview)
