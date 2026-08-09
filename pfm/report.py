"""Report generation with a hard anti-hallucination guarantee.

Every figure in the report is computed by :mod:`portfolio`. The LLM is allowed
to write prose only, over a pre-chewed fact block, and its output then passes
through :func:`validate_narrative`. Any ticker or figure that does not exist in
the source data causes a retry and then a fall back to a deterministic
template, so the published report can never contain an unverifiable claim.

The validator is calibrated against the real 2026-08-02 failure, in which the
model produced ADANIPORTS, IDEAS, TATAPOWERS, LCCI and BSNL (none of which are
held), attributed RBA's -42.3% to LICI, reported "-6.5%" for a stock that was
actually -3.1%, and stated "TATAPOWERS: Down 8644%" by mistaking a rupee
figure for a percentage. See ``tests/test_pipeline.py``.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from brokers import BOOK_IND, BOOK_US
from llm import LLMClient, StockScore
from portfolio import BookTotals, FactSheet, Holding, render_fact_block

_CCY = {"INR": "Rs", "USD": "$"}


# Small formatters. Every one renders an em dash for None, so a figure the broker
# did not supply can never appear as a zero.
def _money(value: Optional[float], unit: str, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    sign = "+" if signed and value >= 0 else ("-" if signed and value < 0 else "")
    return f"{sign}{unit}{abs(value):,.0f}"


def _pct(value: Optional[float], *, decimals: int = 1) -> str:
    return "—" if value is None else f"{value:+.{decimals}f}%"


def _plain(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _round(value: Optional[float], places: int = 2) -> Optional[float]:
    """Round for the JSON payload while preserving None as null."""
    return None if value is None else round(float(value), places)

log = logging.getLogger("pfm.report")

# All-caps tokens that are legitimate English/finance vocabulary rather than
# tickers. Anything else in caps must be a symbol we actually track.
_CAPS_ALLOWED: Set[str] = {
    "AI", "AND", "THE", "FOR", "BUT", "NOT", "ALL", "ANY", "ONE", "TWO", "NEW",
    "P&L", "PNL", "INR", "RS", "USD", "EUR", "GBP", "NAV", "IPO", "FII", "DII",
    "NSE", "BSE", "SEBI", "RBI", "GST", "EPS", "PE", "ROE", "ROI", "YOY", "QOQ",
    "MTM", "LTP", "ETF", "ETFS", "SIP", "AUM", "CAGR", "EBITDA", "PAT", "CEO",
    "CFO", "USA", "US", "UK", "EU", "PPA", "MW", "GW", "KWH", "DOT", "TRAI",
    "AGR", "Q1", "Q2", "Q3", "Q4", "FY", "FY25", "FY26", "H1", "H2", "CY",
    "NOTE", "DATA", "START", "END", "SCORE", "REASON", "STOCK", "SUMMARY",
    "PARAGRAPH", "ANALYSIS", "PORTFOLIO", "NEWS", "HOLDINGS", "TOTAL", "OK",
    "IT", "IS", "IN", "ON", "AT", "TO", "OF", "BY", "AS", "AN", "OR", "SO",
}

# Scripts that must never appear in the report. qwen2.5 is a Chinese-origin model
# and will occasionally answer in Chinese regardless of an English instruction, so
# this is enforced rather than merely requested.
_NON_LATIN_RE = re.compile(
    "["
    "぀-ヿ"      # Hiragana, Katakana
    "㐀-䶿"      # CJK Extension A
    "一-鿿"      # CJK Unified Ideographs
    "豈-﫿"      # CJK Compatibility Ideographs
    "･-ﾟ"      # Halfwidth Katakana
    "가-힯"      # Hangul syllables
    "ᄀ-ᇿ"      # Hangul Jamo
    "Ѐ-ӿ"      # Cyrillic
    "֐-׿"      # Hebrew
    "؀-ۿ"      # Arabic
    "ऀ-ॿ"      # Devanagari
    "฀-๿"      # Thai
    "]"
)


_SCRIPT_RANGES = (
    ("Chinese", 0x4E00, 0x9FFF), ("Chinese", 0x3400, 0x4DBF),
    ("Chinese", 0xF900, 0xFAFF),
    ("Japanese", 0x3040, 0x30FF), ("Japanese", 0xFF66, 0xFF9F),
    ("Korean", 0xAC00, 0xD7AF), ("Korean", 0x1100, 0x11FF),
    ("Cyrillic", 0x0400, 0x04FF), ("Hebrew", 0x0590, 0x05FF),
    ("Arabic", 0x0600, 0x06FF), ("Devanagari", 0x0900, 0x097F),
    ("Thai", 0x0E00, 0x0E7F),
)


def script_name(char: str) -> str:
    code = ord(char)
    for name, low, high in _SCRIPT_RANGES:
        if low <= code <= high:
            return name
    return "non-Latin"


def non_latin_characters(text: str, limit: int = 12) -> List[str]:
    """Characters from a disallowed script, de-duplicated and order-preserving."""
    seen: List[str] = []
    for char in _NON_LATIN_RE.findall(text or ""):
        if char not in seen:
            seen.append(char)
            if len(seen) >= limit:
                break
    return seen


def is_english_only(text: str) -> bool:
    return not non_latin_characters(text, limit=1)


_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_CAPS_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9&\.]{2,14}\b")
_PORTFOLIO_PCT_RE = re.compile(
    r"(?:total|overall|portfolio|aggregate)[^.\n]{0,60}?([-+]?\d[\d,]*(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)


@dataclass
class ValidationResult:
    ok: bool
    violations: List[str]

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.ok


# ---------------------------------------------------------------------------
# Allowed vocabulary and number set
# ---------------------------------------------------------------------------
def _to_float(token: str) -> Optional[float]:
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def build_allowed_numbers(fs: FactSheet, scores: Sequence[StockScore]) -> Set[float]:
    """Every figure the narrative is permitted to mention."""
    values: Set[float] = set()

    def add(value: Optional[float]) -> None:
        if value is None:
            return
        for variant in (value, round(value), round(value, 1), round(value, 2)):
            values.add(abs(float(variant)))

    add(fs.total_invested)
    add(fs.total_current)
    add(fs.total_pnl)
    add(fs.total_pnl_pct)
    add(fs.day_pnl)
    add(fs.concentration_pct)
    add(float(len(fs.holdings)))
    add(float(fs.profitable_count))
    add(float(fs.losing_count))
    # Lakh/crore renderings of the rupee totals, which Indian-market prose uses.
    for base in (fs.total_invested, fs.total_current, abs(fs.total_pnl)):
        add(base / 100_000.0)
        add(base / 10_000_000.0)
    add(fs.usd_inr)
    for totals in fs.books.values():
        for value in (totals.invested, totals.current, totals.costed_current,
                      totals.pnl, totals.pnl_pct, totals.current_inr,
                      totals.invested_inr, totals.pnl_inr, float(totals.count)):
            add(value)
    for holding in fs.holdings:
        # Both the rupee view and the holding's own currency are quotable.
        for value in (holding.pnl, holding.pnl_pct, holding.day_pct,
                      holding.invested, holding.current, holding.quantity,
                      holding.avg_price, holding.ltp, holding.pnl_native,
                      holding.invested_native, holding.current_native):
            add(value)
    for score in scores:
        add(float(score.score) if score.score is not None else None)
        add(float(score.headline_count))
    values.update({0.0, 1.0, 2.0, 3.0, 10.0, 100.0})   # ordinals, "1-10 scale"
    return values


def build_allowed_names(
    fs: FactSheet, scores: Sequence[StockScore], keyword_map: Dict[str, List[str]]
) -> Tuple[Set[str], Set[str]]:
    """Return (allowed_symbols, allowed_alias_tokens)."""
    symbols = {h.symbol.upper() for h in fs.holdings}
    symbols |= {s.symbol.upper() for s in scores}
    # Holdings INDmoney gave no ticker for are referred to by its instrument name,
    # so those words are legitimate vocabulary too.
    for holding in fs.holdings:
        if not holding.has_ticker:
            symbols.add(holding.display.upper())
            for part in re.split(r"[^A-Za-z0-9]+", holding.display.upper()):
                if len(part) >= 3:
                    symbols.add(part)
    aliases: Set[str] = set()
    for symbol in symbols:
        aliases.add(symbol)
        for keyword in keyword_map.get(symbol, []):
            for part in keyword.upper().split():
                if len(part) >= 3:
                    aliases.add(part)
    return symbols, aliases


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_narrative(
    text: str,
    *,
    allowed_symbols: Set[str],
    allowed_aliases: Set[str],
    allowed_numbers: Set[float],
) -> ValidationResult:
    violations: List[str] = []

    # 0. Language, checked before length so a short Chinese reply is diagnosed as
    # non-English rather than merely truncated. Returns immediately: if the model
    # answered in Chinese there is nothing useful to say about its figures.
    foreign = non_latin_characters(text or "")
    if foreign:
        # Describe the script rather than quoting the characters. The violation
        # text is published in the report's data-quality section, so echoing them
        # would put the very characters we rejected back into the report.
        scripts = sorted({script_name(c) for c in foreign})
        return ValidationResult(False, [
            f"narrative is not in English (model replied in "
            f"{', '.join(scripts)}); the deterministic summary was used instead"
        ])

    if not text or len(text.strip()) < 40:
        return ValidationResult(False, ["narrative is empty or too short"])

    # 1. Ticker-like tokens must correspond to something actually held/tracked.
    for token in set(_CAPS_TOKEN_RE.findall(text)):
        bare = token.strip(".").upper()
        if bare in _CAPS_ALLOWED or bare in allowed_symbols or bare in allowed_aliases:
            continue
        if bare.isdigit():
            continue
        violations.append(f"unknown ticker or entity '{token}'")

    # 2. Every figure must exist in the source data (with a rounding tolerance).
    for raw in set(_NUMBER_RE.findall(text)):
        value = _to_float(raw)
        if value is None:
            continue
        magnitude = abs(value)
        if magnitude <= 12 and float(magnitude).is_integer():
            continue          # small counts, scale references, list ordinals
        # 2% relative, with a small absolute floor. A flat 1.0 tolerance was too
        # loose: it let an invented "-6.5%" pass by sitting next to a 6/10 news
        # score in the allowed set.
        tolerance = max(0.05, magnitude * 0.02)
        if not any(abs(magnitude - allowed) <= tolerance for allowed in allowed_numbers):
            violations.append(f"figure '{raw}' does not appear in the source data")

    # 3. An implausible percentage means a rupee figure was read as a percentage.
    for raw in re.findall(r"([-+]?\d[\d,]*(?:\.\d+)?)\s*%", text):
        value = _to_float(raw)
        if value is not None and abs(value) > 1000:
            violations.append(f"implausible percentage '{raw}%'")

    return ValidationResult(not violations, violations)


def validate_portfolio_level_pct(text: str, total_pct: float) -> List[str]:
    """Catch a per-stock percentage being presented as the portfolio return.

    In the 2026-08-02 report the model wrote "a total return of +83.7%", which
    was in fact IDEA's individual gain.
    """
    problems: List[str] = []
    for raw in _PORTFOLIO_PCT_RE.findall(text):
        value = _to_float(raw)
        if value is None:
            continue
        if abs(abs(value) - abs(total_pct)) > max(1.0, abs(total_pct) * 0.05):
            problems.append(
                f"portfolio-level return stated as {raw}% but the computed figure "
                f"is {total_pct:+.1f}%"
            )
    return problems


# ---------------------------------------------------------------------------
# Narrative generation
# ---------------------------------------------------------------------------
def _score_block(scores: Sequence[StockScore], held: Set[str]) -> str:
    if not scores:
        return "No news matched the portfolio today."
    lines = []
    for score in sorted(scores, key=lambda s: (s.symbol not in held, s.symbol)):
        suffix = " (watchlist, not held)" if score.symbol not in held else ""
        if score.score is None:
            lines.append(f"{score.symbol}: no rating available{suffix}")
        else:
            lines.append(f"{score.symbol}: {score.score}/10 {score.label}{suffix} - {score.reason}")
    return "\n".join(lines)


def build_narrative_prompt(fs: FactSheet, scores: Sequence[StockScore], held: Set[str],
                           strict: bool = False) -> str:
    extra = ""
    if strict:
        extra = ("\nYour previous answer was rejected. It either used names or numbers that "
                 "are not in the DATA block, or it was not written in English. Write in "
                 "English using only the Latin alphabet, and copy names and numbers "
                 "character-for-character from the DATA block.\n")
    return f"""You are a financial writer. Write a short portfolio commentary in English.

Hard rules:
- Write in ENGLISH ONLY, using only the Latin alphabet. Do not use Chinese,
  Japanese, Korean, Cyrillic, Devanagari or any other script.
- Use ONLY the company names that appear in the DATA block. Never invent or alter a name.
- Use ONLY the numbers that appear in the DATA block. Copy them exactly.
- Do NOT add, subtract, average or otherwise calculate anything.
- If the DATA block says a stock was not rated, say it was not rated.
- If the DATA block says a cost basis is unavailable for some holdings, do not
  present the invested figure as covering the whole portfolio.
- Write exactly two short paragraphs. No headings, no bullet points, no lists.
- Paragraph 1: overall portfolio position, and which holdings did best and worst.
- Paragraph 2: what the news ratings say, and which stocks they concern.
{extra}
[DATA START]
PORTFOLIO
{render_fact_block(fs)}

NEWS RATINGS (1 = very bad news, 5 = neutral, 10 = very good news)
{_score_block(scores, held)}
[DATA END]

Commentary:"""


def deterministic_narrative(fs: FactSheet, scores: Sequence[StockScore], held: Set[str]) -> str:
    """Template prose built purely from computed facts. Always correct."""
    def listing(items) -> str:
        return ", ".join(f"{h.display} ({h.pnl_pct:+.1f}%)" for h in items) or "none"

    direction = "above" if fs.total_pnl >= 0 else "below"
    top = fs.top_by_value[0].display if fs.top_by_value else "n/a"

    costed_count = sum(1 for h in fs.holdings if h.has_cost_basis)
    if costed_count == len(fs.holdings):
        opening = (f"The portfolio is worth Rs {fs.total_current:,.0f} against "
                   f"Rs {fs.total_invested:,.0f} invested, leaving it "
                   f"Rs {abs(fs.total_pnl):,.0f} {direction} cost ({fs.total_pnl_pct:+.1f}%).")
    else:
        # Do not put value and invested side by side when they cover different
        # sets of holdings - that reads as an arithmetic error.
        opening = (f"The portfolio is worth Rs {fs.total_current:,.0f}. Cost basis is "
                   f"available for {costed_count} of {len(fs.holdings)} holdings, and "
                   f"across those the position is Rs {abs(fs.total_pnl):,.0f} {direction} "
                   f"an invested Rs {fs.total_invested:,.0f} ({fs.total_pnl_pct:+.1f}%).")

    para1 = (
        f"{opening} Of {len(fs.holdings)} holdings, {fs.profitable_count} "
        f"are in profit and {fs.losing_count} are in loss. The strongest performers are "
        f"{listing(fs.winners)}, and the weakest are {listing(fs.losers)}. "
        f"{top} is the largest single position at {fs.concentration_pct:.0f}% of portfolio value."
    )

    india, us = fs.books.get(BOOK_IND), fs.books.get(BOOK_US)
    if india and us:
        us_inr = (f" (Rs {us.current_inr:,.0f})" if us.current_inr is not None else "")
        para1 += (f" The book splits into {india.count} Indian holdings worth "
                  f"Rs {india.current:,.0f} and {us.count} US holdings worth "
                  f"USD {us.current:,.0f}{us_inr}.")
        if us.uncosted_count:
            para1 += (f" {us.uncosted_count} of the US holdings arrived without a cost "
                      f"basis, so their returns are not shown.")

    rated = [s for s in scores if s.score is not None]
    unrated = [s for s in scores if s.score is None]
    if not scores:
        para2 = "No articles in today's feeds matched the portfolio, so there is no news signal to report."
    else:
        positive = [s for s in rated if s.score >= 6]
        negative = [s for s in rated if s.score <= 4]
        neutral = [s for s in rated if s.score == 5]
        bits: List[str] = []
        if positive:
            bits.append("supportive coverage for " + ", ".join(f"{s.symbol} ({s.score}/10)" for s in positive))
        if negative:
            bits.append("negative coverage for " + ", ".join(f"{s.symbol} ({s.score}/10)" for s in negative))
        if neutral:
            bits.append("neutral or routine coverage for " + ", ".join(s.symbol for s in neutral))
        para2 = ("Today's news screen produced " + "; ".join(bits) + "."
                 if bits else "Today's news screen produced no rated coverage.")
        if unrated:
            para2 += (" No usable rating was obtained for "
                      + ", ".join(s.symbol for s in unrated) + ".")
        watch = [s.symbol for s in scores if s.symbol not in held]
        if watch:
            para2 += (" " + ", ".join(sorted(watch))
                      + " appear on the watchlist only and are not held.")
    return f"{para1}\n\n{para2}"


async def build_narrative(
    llm: Optional[LLMClient],
    fs: FactSheet,
    scores: Sequence[StockScore],
    held: Set[str],
    keyword_map: Dict[str, List[str]],
    *,
    enabled: bool = True,
    max_attempts: int = 2,
) -> Tuple[str, str, List[str]]:
    """Return (narrative, provenance, violations_of_final_rejected_attempt)."""
    fallback = deterministic_narrative(fs, scores, held)
    if not enabled or llm is None:
        return fallback, "deterministic (LLM narrative disabled)", []

    allowed_symbols, allowed_aliases = build_allowed_names(fs, scores, keyword_map)
    allowed_numbers = build_allowed_numbers(fs, scores)
    last_violations: List[str] = []

    for attempt in range(1, max_attempts + 1):
        prompt = build_narrative_prompt(fs, scores, held, strict=attempt > 1)
        raw = await llm.generate(
            prompt,
            num_predict=int(llm.cfg["narrative_num_predict"]),
            label=f"narrative {attempt}/{max_attempts}",
        )
        if not raw:
            last_violations = ["model returned nothing"]
            continue

        cleaned = re.sub(r"\n{3,}", "\n\n", raw.strip())
        result = validate_narrative(
            cleaned,
            allowed_symbols=allowed_symbols,
            allowed_aliases=allowed_aliases,
            allowed_numbers=allowed_numbers,
        )
        problems = list(result.violations) + validate_portfolio_level_pct(cleaned, fs.total_pnl_pct)

        if not problems:
            log.info("Narrative accepted on attempt %d.", attempt)
            return cleaned, f"model-written, validated (attempt {attempt})", []

        last_violations = problems
        log.warning("Narrative attempt %d rejected: %s", attempt, "; ".join(problems[:6]))
        foreign = non_latin_characters(cleaned)
        if foreign:
            # Only the log gets the actual characters; useful in journalctl,
            # never published.
            log.warning("  offending characters: %s", " ".join(foreign))

    log.warning("All narrative attempts rejected; using the deterministic template.")
    return fallback, "deterministic fallback (model output failed validation)", last_violations


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------
def render_report(
    fs: FactSheet,
    news_section: str,
    narrative: str,
    provenance: str,
    *,
    scores: Sequence[StockScore],
    model: str,
    rejected: Optional[List[str]] = None,
    feed_stats: Optional[dict] = None,
    generated_at: Optional[datetime] = None,
) -> str:
    stamp = generated_at or datetime.now()
    rated = [s for s in scores if s.score is not None]

    lines: List[str] = [
        f"# Portfolio Analysis — {stamp:%Y-%m-%d}",
        "",
        f"_Generated {stamp:%Y-%m-%d %H:%M} · model `{model}` · "
        f"{len(rated)}/{len(scores)} tracked stocks rated_",
        "",
        "## Position",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Invested | Rs {fs.total_invested:,.2f}"
        + (" _(costed holdings only)_" if fs.uncosted else "") + " |",
        f"| Current value | Rs {fs.total_current:,.2f} |",
        f"| Overall P&L | Rs {fs.total_pnl:+,.2f} ({fs.total_pnl_pct:+.2f}%)"
        + (" _(costed holdings only)_" if fs.uncosted else "") + " |",
    ]
    if fs.day_pnl is not None:
        lines.append(f"| Change today | Rs {fs.day_pnl:+,.2f} |")
    lines += [
        f"| Holdings | {len(fs.holdings)} ({fs.profitable_count} in profit, "
        f"{fs.losing_count} in loss) |",
        f"| Largest position | {fs.top_by_value[0].display if fs.top_by_value else 'n/a'} "
        f"({fs.concentration_pct:.1f}% of value) |",
    ]
    if fs.usd_inr:
        lines.append(f"| USD/INR used | {fs.usd_inr:,.2f} — {fs.fx_source} |")
    lines.append("")

    # -- per-book summary, only when there is more than one book -------------
    # A rupee column is pointless when every book is already in rupees, which is
    # the normal case: INDmoney pre-converts the US book.
    show_inr_column = any(t.currency != "INR" for t in fs.books.values())

    if len(fs.books) > 1:
        inr_head = " Value (Rs) |" if show_inr_column else ""
        inr_rule = " --: |" if show_inr_column else ""
        lines += ["## Books", "",
                  f"| Book | Holdings | Invested | Value | P&L | P&L % |{inr_head}",
                  f"| --- | --: | --: | --: | --: | --: |{inr_rule}"]
        needs_footnote = False
        for key, label in ((BOOK_IND, "India"), (BOOK_US, "US")):
            totals = fs.books.get(key)
            if not totals:
                continue
            unit = _CCY.get(totals.currency, totals.currency)
            # A star marks figures that cover only the holdings with a cost basis.
            star = "*" if totals.uncosted_count else ""
            needs_footnote = needs_footnote or bool(star)
            inr_cell = f" {_money(totals.current_inr, 'Rs')} |" if show_inr_column else ""
            lines.append(
                f"| {label} ({totals.currency}) | {totals.count} | "
                f"{_money(totals.invested, unit)}{star} | {unit}{totals.current:,.0f} | "
                f"{_money(totals.pnl, unit, signed=True)}{star} | "
                f"{_pct(totals.pnl_pct)}{star} |{inr_cell}"
            )
        lines.append("")
        us_book = fs.books.get(BOOK_US)
        if us_book and us_book.currency == "INR":
            lines += ["_The US book is shown in rupees because INDmoney reports US "
                      "positions already converted; no exchange rate is applied here._", ""]
        if needs_footnote:
            lines += [
                "_* Invested and P&L cover only the holdings whose cost basis the broker "
                "shared, so they will not equal Value minus Invested. Value covers every "
                "holding. INDmoney does not pass through the original invested amount for "
                "positions imported from a linked broker._", "",
            ]

    lines += ["## Holdings", ""]
    for key, label in ((BOOK_IND, "India"), (BOOK_US, "US")):
        rows = [h for h in fs.holdings if h.book == key]
        if not rows:
            continue
        if len(fs.books) > 1:
            lines += [f"### {label}", ""]
        inr_head = " Value (Rs) |" if show_inr_column else ""
        inr_rule = " --: |" if show_inr_column else ""
        lines += [f"| Symbol | Qty | Avg | LTP | Invested | Value | P&L | P&L % | "
                  f"Today |{inr_head}",
                  f"| --- | --: | --: | --: | --: | --: | --: | --: | --: |{inr_rule}"]
        unit = _CCY.get(rows[0].currency, rows[0].currency)
        for h in rows:
            inr_cell = f" {_money(h.current_inr, 'Rs')} |" if show_inr_column else ""
            lines.append(
                f"| {h.display} | {h.quantity:g} | {_plain(h.avg_price)} | {_plain(h.ltp)} | "
                f"{_money(h.invested_native, unit)} | {unit}{h.current_native:,.0f} | "
                f"{_money(h.pnl_native, unit, signed=True)} | {_pct(h.pnl_pct)} | "
                f"{_pct(h.day_pct, decimals=2)} |{inr_cell}"
            )
        totals = fs.books.get(key)
        if totals:
            inr_cell = f" **{_money(totals.current_inr, 'Rs')}** |" if show_inr_column else ""
            lines.append(
                f"| **{label} total** | | | | **{_money(totals.invested, unit)}** | "
                f"**{unit}{totals.current:,.0f}** | **{_money(totals.pnl, unit, signed=True)}** | "
                f"**{_pct(totals.pnl_pct)}** | |{inr_cell}"
            )
        lines.append("")

    costed_count = sum(1 for h in fs.holdings if h.has_cost_basis)
    if costed_count == len(fs.holdings):
        lines += [f"**Combined (Rs):** invested {fs.total_invested:,.0f} · "
                  f"value {fs.total_current:,.0f} · "
                  f"P&L {fs.total_pnl:+,.0f} ({fs.total_pnl_pct:+.1f}%)", ""]
    else:
        lines += [f"**Combined (Rs):** value {fs.total_current:,.0f} across "
                  f"{len(fs.holdings)} holdings. Cost basis is known for "
                  f"{costed_count} of them: invested {fs.total_invested:,.0f}, "
                  f"P&L {fs.total_pnl:+,.0f} ({fs.total_pnl_pct:+.1f}%). The return "
                  f"figures therefore describe that subset, not the whole portfolio.", ""]

    lines += ["", "## News and sentiment", "", news_section.rstrip(), "",
              "## Commentary", "", narrative.strip(), "",
              f"_Provenance: {provenance}._", ""]

    unscored = [s.symbol for s in scores if s.score is None]
    quality: List[str] = list(fs.data_quality)
    if feed_stats:
        ok, total = feed_stats.get("feeds_ok", 0), feed_stats.get("feeds_total", 0)
        failed = feed_stats.get("feeds_failed") or []
        if total and ok == 0:
            quality.append(
                "None of the news feeds could be reached, so the absence of news below "
                "reflects a fetch failure, not a quiet news day. Failed: "
                + ", ".join(failed) + "."
            )
        elif failed:
            quality.append(f"{ok}/{total} news feeds responded. No data from: "
                           + ", ".join(failed) + ".")
        else:
            quality.append(f"All {total} news feeds responded "
                           f"({feed_stats.get('entries_seen', 0)} items scanned, "
                           f"{feed_stats.get('articles_matched', 0)} relevant).")
    if unscored:
        quality.append("No usable model rating for: " + ", ".join(unscored) + ".")
    low_conf = [s.symbol for s in scores if s.score is not None and s.confidence == "low"]
    if low_conf:
        quality.append("Low-confidence ratings (headline groups disagreed): "
                       + ", ".join(low_conf) + ".")
    if rejected:
        quality.append("Model commentary was rejected by validation: "
                       + "; ".join(rejected[:6]) + ".")

    lines += ["## Data quality", ""]
    if quality:
        lines += [f"- {item}" for item in quality]
    else:
        lines.append("- No issues detected. All figures reconcile and every tracked stock was rated.")
    lines += ["",
              "_All figures in this report are computed directly from the broker payload. "
              "The commentary section is the only model-written text, and it is validated "
              "against those figures before publication._", ""]

    return "\n".join(lines)


# v2 adds the US book: per-holding currency and native values, per-book
# subtotals, the USD/INR rate actually used, and broker sentiment cross-checks.
SCHEMA_VERSION = 2


def build_payload(
    fs: FactSheet,
    grouped: dict,
    scores: Sequence[StockScore],
    narrative: str,
    provenance: str,
    *,
    held: Set[str],
    model: str,
    rejected: Optional[List[str]] = None,
    feed_stats: Optional[dict] = None,
    broker_sentiment: Optional[Dict[str, dict]] = None,
    generated_at: Optional[datetime] = None,
) -> dict:
    """Structured sidecar consumed by the web view.

    The web layer reads this rather than scraping the markdown, so the browser
    gets the same computed figures the report does - no second parsing step that
    could drift away from the source of truth.
    """
    stamp = generated_at or datetime.now()
    by_symbol = {s.symbol: s for s in scores}
    us_symbols = {h.symbol for h in fs.holdings if h.book == BOOK_US}
    broker_sentiment = broker_sentiment or {}

    news: dict = {}
    for symbol in sorted(set(grouped) | set(by_symbol)):
        score = by_symbol.get(symbol)
        articles = grouped.get(symbol, [])
        entry = {
            "held": symbol in held,
            "book": BOOK_US if symbol in us_symbols else BOOK_IND,
            "score": score.score if score else None,
            "label": score.label if score else "unscored",
            "reason": score.reason if score else "",
            "confidence": score.confidence if score else "unscored",
            "method": score.method if score else "",
            "headline_count": len(articles),
            "chunk_scores": list(score.chunk_scores) if score else [],
            "articles": [
                {"title": a.title, "source": a.source, "link": a.link}
                for a in articles
            ],
        }
        sentiment = broker_sentiment.get(symbol)
        if sentiment:
            entry["broker_sentiment"] = sentiment.get("sentiment")
            entry["broker_sentiment_note"] = sentiment.get("sentiment_note", "")
        news[symbol] = entry

    return {
        "schema_version": SCHEMA_VERSION,
        "date": f"{stamp:%Y-%m-%d}",
        "generated_at": stamp.isoformat(timespec="seconds"),
        "model": model,
        "totals": {
            "invested": round(fs.total_invested, 2),
            "current": round(fs.total_current, 2),
            "pnl": round(fs.total_pnl, 2),
            "pnl_pct": round(fs.total_pnl_pct, 2),
            "day_pnl": round(fs.day_pnl, 2) if fs.day_pnl is not None else None,
            "holdings_count": len(fs.holdings),
            "profitable_count": fs.profitable_count,
            "losing_count": fs.losing_count,
            "concentration_pct": round(fs.concentration_pct, 2),
            "largest_position": fs.top_by_value[0].symbol if fs.top_by_value else None,
        },
        "fx": {
            "usd_inr": fs.usd_inr,
            "source": fs.fx_source,
        },
        "books": {
            key: {
                "currency": t.currency,
                "count": t.count,
                "uncosted_count": t.uncosted_count,
                "invested": _round(t.invested),
                "current": _round(t.current),
                "pnl": _round(t.pnl),
                "pnl_pct": _round(t.pnl_pct),
                "invested_inr": _round(t.invested_inr),
                "current_inr": _round(t.current_inr),
                "pnl_inr": _round(t.pnl_inr),
            }
            for key, t in fs.books.items()
        },
        "holdings": [
            {
                "symbol": h.symbol,
                "display": h.display,
                "has_ticker": h.has_ticker,
                "name": h.name,
                "exchange": h.exchange,
                "book": h.book,
                "currency": h.currency,
                "source": h.source,
                "quantity": h.quantity,
                "avg_price": _round(h.avg_price),
                "ltp": _round(h.ltp),
                # Native currency, then the rupee view of the same row.
                "invested": _round(h.invested_native),
                "current": _round(h.current_native),
                "pnl": _round(h.pnl_native),
                "pnl_pct": _round(h.pnl_pct),
                "invested_inr": _round(h.invested_inr),
                "current_inr": _round(h.current_inr),
                "pnl_inr": _round(h.pnl_inr),
                "day_pct": _round(h.day_pct),
                "has_cost_basis": h.has_cost_basis,
                "flags": list(h.flags),
            }
            for h in fs.holdings
        ],
        "uncosted": list(fs.uncosted),
        "winners": [h.symbol for h in fs.winners],
        "losers": [h.symbol for h in fs.losers],
        "news": news,
        "commentary": {"text": narrative, "provenance": provenance},
        "rejected_commentary": list(rejected or []),
        "data_quality": list(fs.data_quality),
        "feed_stats": dict(feed_stats or {}),
    }


def write_payload(payload: dict, report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"portfolio_analysis_{payload['date']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)   # atomic, so the web view never reads a half-written file
    log.info("Report data written to %s", path)
    return path


def write_report(content: str, report_dir: Path, stamp: Optional[datetime] = None) -> Path:
    stamp = stamp or datetime.now()
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"portfolio_analysis_{stamp:%Y-%m-%d}.md"
    path.write_text(content, encoding="utf-8")
    log.info("Report written to %s", path)
    return path


def telegram_summary(fs: FactSheet, scores: Sequence[StockScore], path: Path) -> str:
    rated = [s for s in scores if s.score is not None]
    best = max(rated, key=lambda s: s.score, default=None)
    worst = min(rated, key=lambda s: s.score, default=None)
    parts = [
        "Daily portfolio analysis",
        f"Value Rs {fs.total_current:,.0f} | P&L Rs {fs.total_pnl:+,.0f} ({fs.total_pnl_pct:+.1f}%)",
        f"{fs.profitable_count} up / {fs.losing_count} down across {len(fs.holdings)} holdings",
        f"News rated for {len(rated)}/{len(scores)} tracked stocks",
    ]
    if best:
        parts.append(f"Best news: {best.symbol} {best.score}/10")
    if worst and worst is not best:
        parts.append(f"Worst news: {worst.symbol} {worst.score}/10")
    parts.append(f"Report: {path.name}")
    return "\n".join(parts)
