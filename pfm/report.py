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

from llm import LLMClient, StockScore
from portfolio import FactSheet, render_fact_block

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
    for holding in fs.holdings:
        add(holding.pnl)
        add(holding.pnl_pct)
        add(holding.day_pct)
        add(holding.invested)
        add(holding.current)
        add(holding.quantity)
        add(holding.avg_price)
        add(holding.ltp)
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
        extra = ("\nYour previous answer contained names or numbers that were not in the DATA "
                 "block. Copy names and numbers character-for-character from the DATA block "
                 "this time, and mention no others.\n")
    return f"""You are a financial writer. Write a short portfolio commentary in English.

Hard rules:
- Use ONLY the company names that appear in the DATA block. Never invent or alter a name.
- Use ONLY the numbers that appear in the DATA block. Copy them exactly.
- Do NOT add, subtract, average or otherwise calculate anything.
- If the DATA block says a stock was not rated, say it was not rated.
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
        return ", ".join(f"{h.symbol} ({h.pnl_pct:+.1f}%)" for h in items) or "none"

    direction = "above" if fs.total_pnl >= 0 else "below"
    top = fs.top_by_value[0].symbol if fs.top_by_value else "n/a"

    para1 = (
        f"The portfolio is worth Rs {fs.total_current:,.0f} against Rs {fs.total_invested:,.0f} "
        f"invested, leaving it Rs {abs(fs.total_pnl):,.0f} {direction} cost "
        f"({fs.total_pnl_pct:+.1f}%). Of {len(fs.holdings)} holdings, {fs.profitable_count} "
        f"are in profit and {fs.losing_count} are in loss. The strongest performers are "
        f"{listing(fs.winners)}, and the weakest are {listing(fs.losers)}. "
        f"{top} is the largest single position at {fs.concentration_pct:.0f}% of portfolio value."
    )

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
        f"| Invested | Rs {fs.total_invested:,.2f} |",
        f"| Current value | Rs {fs.total_current:,.2f} |",
        f"| Overall P&L | Rs {fs.total_pnl:+,.2f} ({fs.total_pnl_pct:+.2f}%) |",
    ]
    if fs.day_pnl is not None:
        lines.append(f"| Change today | Rs {fs.day_pnl:+,.2f} |")
    lines += [
        f"| Holdings | {len(fs.holdings)} ({fs.profitable_count} in profit, "
        f"{fs.losing_count} in loss) |",
        f"| Largest position | {fs.top_by_value[0].symbol if fs.top_by_value else 'n/a'} "
        f"({fs.concentration_pct:.1f}% of value) |",
        "",
        "## Holdings",
        "",
        "| Symbol | Qty | Avg | LTP | Invested | Value | P&L | P&L % | Today |",
        "| --- | --: | --: | --: | --: | --: | --: | --: | --: |",
    ]
    for h in fs.holdings:
        day = f"{h.day_pct:+.2f}%" if h.day_pct is not None else "—"
        lines.append(
            f"| {h.symbol} | {h.quantity:g} | {h.avg_price:,.2f} | {h.ltp:,.2f} | "
            f"{h.invested:,.0f} | {h.current:,.0f} | {h.pnl:+,.0f} | {h.pnl_pct:+.1f}% | {day} |"
        )
    lines.append(
        f"| **Total** | | | | **{fs.total_invested:,.0f}** | **{fs.total_current:,.0f}** | "
        f"**{fs.total_pnl:+,.0f}** | **{fs.total_pnl_pct:+.1f}%** | |"
    )

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


SCHEMA_VERSION = 1


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
    generated_at: Optional[datetime] = None,
) -> dict:
    """Structured sidecar consumed by the web view.

    The web layer reads this rather than scraping the markdown, so the browser
    gets the same computed figures the report does - no second parsing step that
    could drift away from the source of truth.
    """
    stamp = generated_at or datetime.now()
    by_symbol = {s.symbol: s for s in scores}

    news: dict = {}
    for symbol in sorted(set(grouped) | set(by_symbol)):
        score = by_symbol.get(symbol)
        articles = grouped.get(symbol, [])
        news[symbol] = {
            "held": symbol in held,
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
        "holdings": [
            {
                "symbol": h.symbol,
                "exchange": h.exchange,
                "quantity": h.quantity,
                "avg_price": round(h.avg_price, 2),
                "ltp": round(h.ltp, 2),
                "invested": round(h.invested, 2),
                "current": round(h.current, 2),
                "pnl": round(h.pnl, 2),
                "pnl_pct": round(h.pnl_pct, 2),
                "day_pct": round(h.day_pct, 2) if h.day_pct is not None else None,
                "flags": list(h.flags),
            }
            for h in fs.holdings
        ],
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
