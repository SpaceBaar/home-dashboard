"""RSS ingestion, symbol attribution, deduplication and per-stock scoring.

Two behavioural changes from the original implementation:

1. Attribution is *multi-symbol and exclusion-aware*. The old loop broke out on
   the first keyword hit, so an article naming both SBI and LIC was filed under
   whichever appeared earlier in config.json. It also matched "SBI" inside "SBI
   Cards" - a different listed company - and matched the padded keyword "VI "
   which cannot work with a word-boundary regex.

2. Scoring is *one LLM call per stock*, with every headline for that stock in
   the same prompt. Previously five stocks shared one call with a token budget
   of ``5 * 45 + 80``, so the model ran out of tokens partway down the list and
   the strict line parser dropped everything - which is why the 2026-08-02
   report showed "Score unavailable" for all seven stocks despite having news.
"""

from __future__ import annotations

import difflib
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Dict, Iterable, List, Optional, Sequence

import requests

from llm import LLMClient, StockScore

log = logging.getLogger("pfm.news")

_UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")
_ATOM = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class Article:
    symbol: str
    title: str
    source: str
    link: str
    published: Optional[float] = None   # epoch seconds, for recency ordering


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def clean_text(raw: Optional[str]) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)          # entities sometimes double-encoded
    return _WS_RE.sub(" ", text).strip()


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def _parse_date(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(value.strip(), fmt))
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------
def _iter_entries(root: ET.Element) -> Iterable[Dict[str, str]]:
    """Yield normalised entries from either an RSS or an Atom document.

    The old code only looked for ``.//item``, so any source that switched to
    Atom would silently contribute zero articles with no error at all.
    """
    items = root.findall(".//item")
    if items:
        for item in items:
            yield {
                "title": clean_text(item.findtext("title")),
                "description": clean_text(item.findtext("description")),
                "link": (item.findtext("link") or "").strip(),
                "date": (item.findtext("pubDate") or item.findtext("date") or "").strip(),
            }
        return

    for entry in root.findall(f".//{_ATOM}entry"):
        link_el = entry.find(f"{_ATOM}link")
        yield {
            "title": clean_text(entry.findtext(f"{_ATOM}title")),
            "description": clean_text(
                entry.findtext(f"{_ATOM}summary") or entry.findtext(f"{_ATOM}content")
            ),
            "link": (link_el.get("href") if link_el is not None else "") or "",
            "date": (entry.findtext(f"{_ATOM}updated")
                     or entry.findtext(f"{_ATOM}published") or "").strip(),
        }


def fetch_feed(source: Dict[str, str], *, timeout: float, attempts: int) -> List[Dict[str, str]]:
    url = source["rss_url"]
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, headers={"User-Agent": _UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"}, timeout=timeout)
            if resp.status_code != 200:
                log.warning("%s: HTTP %s", source["name"], resp.status_code)
            else:
                root = ET.fromstring(resp.content)
                entries = list(_iter_entries(root))
                log.info("%-30s %3d entries", source["name"], len(entries))
                return entries
        except ET.ParseError as exc:
            log.warning("%s: malformed XML (%s)", source["name"], exc)
            return []
        except Exception as exc:
            log.warning("%s: attempt %d failed (%s)", source["name"], attempt, exc)
        if attempt < attempts:
            time.sleep(1.5 * attempt)
    return []


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------
def match_symbols(
    text_upper: str,
    universe: Sequence[str],
    keyword_map: Dict[str, List[str]],
    exclude_map: Dict[str, List[str]],
) -> List[str]:
    """Return EVERY tracked symbol the text refers to.

    Exclusion phrases are deleted from the text before matching, so
    "SBI Cards Q1 profit rises" no longer registers as State Bank of India news
    while "SBI and SBI Cards both report" still correctly matches SBIN.
    """
    hits: List[str] = []
    for symbol in universe:
        keywords = keyword_map.get(symbol) or []
        if not keywords:
            continue
        scoped = text_upper
        for phrase in exclude_map.get(symbol, []):
            if phrase:
                scoped = scoped.replace(phrase, " ")
        for keyword in keywords:
            keyword = keyword.strip()
            if not keyword:
                continue
            # Built from split parts so it is independent of how the installed
            # Python version happens to escape spaces, and tolerant of the
            # variable whitespace that HTML-stripped feed text contains.
            body = r"\s+".join(re.escape(part) for part in keyword.split())
            pattern = r"(?<![A-Z0-9])" + body + r"(?![A-Z0-9])"
            if re.search(pattern, scoped):
                hits.append(symbol)
                break
    return hits


def collect_articles(
    sources: Sequence[Dict[str, str]],
    universe: Sequence[str],
    keyword_map: Dict[str, List[str]],
    exclude_map: Dict[str, List[str]],
    *,
    timeout: float = 12.0,
    attempts: int = 2,
    similarity_threshold: float = 0.9,
    max_per_stock: int = 12,
    stats: Optional[Dict[str, object]] = None,
) -> Dict[str, List[Article]]:
    """Fetch all feeds and group deduplicated articles by symbol.

    ``stats``, if supplied, is filled with feed-level counts so the report can
    distinguish "there was no relevant news" from "every feed was unreachable".
    """
    grouped: Dict[str, List[Article]] = {}
    seen_exact: Dict[str, set] = {}
    feeds_ok, feeds_failed, entries_seen = 0, [], 0

    for source in sources:
        entries = fetch_feed(source, timeout=timeout, attempts=attempts)
        if entries:
            feeds_ok += 1
            entries_seen += len(entries)
        else:
            feeds_failed.append(source["name"])
        for entry in entries:
            title = entry["title"]
            if not title:
                continue
            haystack = f"{title} {entry['description']}".upper()
            for symbol in match_symbols(haystack, universe, keyword_map, exclude_map):
                bucket = grouped.setdefault(symbol, [])
                seen = seen_exact.setdefault(symbol, set())
                norm = _norm_title(title)
                if norm in seen:
                    continue
                # Near-duplicate guard: the same wire story is syndicated across
                # feeds with slightly different slugs, which previously let one
                # story be counted several times and skew the aggregate score.
                if any(difflib.SequenceMatcher(None, norm, _norm_title(a.title)).ratio()
                       >= similarity_threshold for a in bucket):
                    continue
                seen.add(norm)
                bucket.append(Article(
                    symbol=symbol, title=title, source=source["name"],
                    link=entry["link"], published=_parse_date(entry["date"]),
                ))

    for symbol, articles in grouped.items():
        # Most recent first; entries without a date keep feed order at the back.
        articles.sort(key=lambda a: (a.published is not None, a.published or 0), reverse=True)
        grouped[symbol] = articles[:max_per_stock]

    _ = entries_seen
    total = sum(len(v) for v in grouped.values())
    log.info("Attributed %d unique article(s) to %d stock(s) from %d/%d live feed(s).",
             total, len(grouped), feeds_ok, len(sources))
    if feeds_failed:
        log.warning("Feeds returning nothing: %s", ", ".join(feeds_failed))
    if stats is not None:
        stats.update({
            "feeds_total": len(sources),
            "feeds_ok": feeds_ok,
            "feeds_failed": feeds_failed,
            "entries_seen": entries_seen,
            "articles_matched": total,
        })
    return grouped


# ---------------------------------------------------------------------------
# Merging a second news source (INDmoney's US headlines)
# ---------------------------------------------------------------------------
def merge_articles(
    grouped: Dict[str, List[Article]],
    extra: Dict[str, dict],
    *,
    source_label: str = "INDmoney",
    similarity_threshold: float = 0.9,
    max_per_stock: int = 12,
) -> Dict[str, List[Article]]:
    """Fold externally supplied headlines into the RSS-derived groups.

    Indian RSS feeds cover US names thinly, so INDmoney's own US news is the
    better source for those tickers. Deduplication uses the same near-match rule
    as the feed scan, so a story carried by both does not get counted twice and
    skew the aggregate score.
    """
    for symbol, payload in extra.items():
        articles = payload.get("articles") or []
        if not articles:
            continue
        bucket = grouped.setdefault(symbol.upper(), [])
        for item in articles:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            norm = _norm_title(title)
            if any(difflib.SequenceMatcher(None, norm, _norm_title(a.title)).ratio()
                   >= similarity_threshold for a in bucket):
                continue
            bucket.append(Article(
                symbol=symbol.upper(), title=title,
                source=item.get("source") or source_label,
                link=item.get("link") or "", published=None,
            ))
        grouped[symbol.upper()] = bucket[:max_per_stock]
    return grouped


def sentiment_disagreements(
    scores: Sequence[StockScore],
    broker_sentiment: Dict[str, dict],
    *,
    threshold: float = 3.0,
) -> List[str]:
    """Where our local score and the broker's sentiment differ sharply.

    Not treated as an error on either side — the two look at the same headlines
    with different models and possibly different scales. It is surfaced because a
    wide gap is worth a human glance.
    """
    notes: List[str] = []
    for score in scores:
        if score.score is None:
            continue
        entry = broker_sentiment.get(score.symbol)
        if not entry:
            continue
        theirs = entry.get("sentiment")
        if theirs is None:
            continue
        gap = abs(float(theirs) - float(score.score))
        if gap >= threshold:
            note = entry.get("sentiment_note") or ""
            suffix = f" ({note})" if note else ""
            notes.append(
                f"{score.symbol}: local model rated {score.score}/10 but INDmoney's "
                f"sentiment maps to {theirs:.1f}/10{suffix} — a {gap:.1f} point gap."
            )
    return notes


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
async def score_all(
    grouped: Dict[str, List[Article]],
    llm: LLMClient,
    *,
    max_headline_chars: int = 180,
    held: Optional[Sequence[str]] = None,
) -> List[StockScore]:
    """Score each stock in its own LLM call, sequentially.

    Sequential is intentional: the Pi has a single NPU, so firing calls
    concurrently only lengthens the queue and invites timeouts.
    """
    held_set = {s.upper() for s in (held or [])}
    # Held stocks first - they matter more and get the model while it is warm.
    order = sorted(grouped.keys(), key=lambda s: (s not in held_set, s))

    scores: List[StockScore] = []
    for idx, symbol in enumerate(order, 1):
        articles = grouped[symbol]
        headlines = [a.title[:max_headline_chars] for a in articles]
        log.info("[%d/%d] %s - %d headline(s)", idx, len(order), symbol, len(headlines))
        scores.append(await llm.score_stock(symbol, headlines))
    return scores


def render_news_section(
    grouped: Dict[str, List[Article]],
    scores: Sequence[StockScore],
    held: Optional[Sequence[str]] = None,
) -> str:
    """Markdown for the news section: every headline, with its aggregate score."""
    held_set = {s.upper() for s in (held or [])}
    by_symbol = {s.symbol: s for s in scores}
    if not grouped:
        return "_No articles in today's feeds matched the portfolio or watchlist._\n"

    def block(symbol: str) -> str:
        score = by_symbol.get(symbol)
        articles = grouped[symbol]
        if score is None or score.score is None:
            verdict = "**Aggregate sentiment: not scored** - the local model did not return a usable rating."
        else:
            verdict = (f"**Aggregate sentiment: {score.score}/10 ({score.label})** "
                       f"- {score.reason} "
                       f"`confidence={score.confidence}`")
        rows = "\n".join(
            f"- [{a.title}]({a.link}) — _{a.source}_" if a.link else f"- {a.title} — _{a.source}_"
            for a in articles
        )
        return (f"#### {symbol} ({len(articles)} article"
                f"{'s' if len(articles) != 1 else ''})\n\n{verdict}\n\n{rows}\n")

    held_syms = [s for s in grouped if s in held_set]
    watch_syms = [s for s in grouped if s not in held_set]
    out: List[str] = []

    if held_syms:
        out.append("### Holdings\n")
        out.extend(block(s) for s in sorted(held_syms))
    if watch_syms:
        out.append("### Watchlist (not held)\n")
        out.extend(block(s) for s in sorted(watch_syms))
    return "\n".join(out)
