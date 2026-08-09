#!/usr/bin/env python3
"""Standalone read-only web view for the portfolio reports.

Deliberately independent of the home-dashboard Node app: its own process, its
own port, its own systemd unit. Nothing here imports from or writes to that
application.

Zero extra dependencies - stdlib ``http.server`` only. The surface is read-only
GET, intended for a LAN or VPN address, so a threaded stdlib server is the right
amount of machinery.

    python web.py                 # serve on 0.0.0.0:7373
    python web.py --port 8080
    python web.py --once /        # render one route to stdout (for testing)

Routes
    /                     latest report
    /r/<date>             a specific date
    /raw/<date>.md        the markdown source
    /api/reports          index of every available date
    /api/reports/<date>   the structured payload
    /healthz              liveness probe
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import socket
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from pfm_config import BASE_DIR, REPORT_DIR, load_config, setup_logging

log = logging.getLogger("pfm.web")

STATIC_DIR = BASE_DIR / "static"

# Privacy defaults, overridden from config.json in main(). Held module-level so
# the request handler can reach them without a per-request config read.
PRIVACY: Dict[str, object] = {
    "blur_by_default": False,
    "blur_on_focus_loss": True,
    "blur_on_tab_hidden": True,
    "blur_on_screenshot_keys": True,
    "idle_seconds": 180,
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REPORT_NAME_RE = re.compile(r"^portfolio_analysis_(\d{4}-\d{2}-\d{2})\.(md|json)$")

# Legacy reports were written to pfm/ itself before reports/ existed.
SEARCH_DIRS = (REPORT_DIR, BASE_DIR)


# ===========================================================================
# Report discovery
# ===========================================================================
def discover_reports() -> Dict[str, Dict[str, Path]]:
    """Map date -> {"md": path, "json": path}, scanning newest location first."""
    found: Dict[str, Dict[str, Path]] = {}
    for directory in SEARCH_DIRS:
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            match = _REPORT_NAME_RE.match(entry.name)
            if not match or not entry.is_file():
                continue
            date, kind = match.group(1), match.group(2)
            # reports/ is scanned first, so it wins over a stale copy in pfm/.
            found.setdefault(date, {}).setdefault(kind, entry)
    return found


def load_payload(date: str) -> Optional[dict]:
    paths = discover_reports().get(date, {})
    if "json" not in paths:
        return None
    try:
        return json.loads(paths["json"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Unreadable payload for %s: %s", date, exc)
        return None


def load_markdown(date: str) -> Optional[str]:
    paths = discover_reports().get(date, {})
    if "md" not in paths:
        return None
    try:
        return paths["md"].read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Unreadable markdown for %s: %s", date, exc)
        return None


def build_index() -> List[dict]:
    """Newest-first summary of every report, for the sidebar and the chart."""
    index: List[dict] = []
    for date, paths in discover_reports().items():
        entry = {"date": date, "has_data": "json" in paths, "has_markdown": "md" in paths,
                 "current": None, "pnl": None, "pnl_pct": None, "rated": None, "tracked": None}
        if "json" in paths:
            payload = load_payload(date) or {}
            totals = payload.get("totals", {})
            news = payload.get("news", {})
            entry.update({
                "current": totals.get("current"),
                "pnl": totals.get("pnl"),
                "pnl_pct": totals.get("pnl_pct"),
                "rated": sum(1 for n in news.values() if n.get("score") is not None),
                "tracked": len(news),
            })
        index.append(entry)
    index.sort(key=lambda e: e["date"], reverse=True)
    return index


# ===========================================================================
# Minimal markdown rendering, used only for legacy reports with no sidecar
# ===========================================================================
def render_markdown(text: str) -> str:
    """Enough markdown for the reports this agent produces.

    Only reached for pre-sidecar reports; current reports are rendered from
    structured data instead.
    """
    def inline(chunk: str) -> str:
        out = html.escape(chunk)
        out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                     lambda m: f'<a href="{html.escape(m.group(2), quote=True)}" '
                               f'rel="noopener noreferrer" target="_blank">{m.group(1)}</a>',
                     out)
        out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
        out = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<em>\1</em>", out)
        out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
        return out

    lines = text.splitlines()
    out: List[str] = []
    i, in_list = 0, False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        if not stripped:
            close_list()
            i += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            close_list()
            level = min(len(heading.group(1)) + 1, 6)   # h1 is the page title
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            i += 1
            continue

        # Table: a header row followed by a separator row.
        if stripped.startswith("|") and i + 1 < len(lines) and \
                re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            close_list()
            def cells(row: str) -> List[str]:
                return [c.strip() for c in row.strip().strip("|").split("|")]
            head = cells(stripped)
            out.append('<div class="table-wrap"><table><thead><tr>'
                       + "".join(f"<th>{inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>")
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>"
                                            for c in cells(lines[i])) + "</tr>")
                i += 1
            out.append("</tbody></table></div>")
            continue

        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(bullet.group(1))}</li>")
            i += 1
            continue

        close_list()
        out.append(f"<p>{inline(stripped)}</p>")
        i += 1

    close_list()
    return "\n".join(out)


# ===========================================================================
# Formatting helpers
# ===========================================================================
def rupees(value: Optional[float], *, signed: bool = False, decimals: int = 0) -> str:
    if value is None:
        return "—"
    sign = "+" if signed and value >= 0 else ("-" if signed and value < 0 else "")
    return f"{sign}₹{abs(value):,.{decimals}f}"


def percent(value: Optional[float], *, signed: bool = True, decimals: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:+.{decimals}f}%" if signed else f"{value:.{decimals}f}%"


def tone(value: Optional[float]) -> str:
    if value is None:
        return "flat"
    return "up" if value > 0 else ("down" if value < 0 else "flat")


def score_tone(score: Optional[int]) -> str:
    if score is None:
        return "none"
    if score >= 6:
        return "up"
    if score <= 4:
        return "down"
    return "flat"


def pretty_date(date: str) -> str:
    try:
        return f"{datetime.strptime(date, '%Y-%m-%d'):%a %d %b %Y}"
    except ValueError:
        return date


# ===========================================================================
# SVG trend chart, rendered server-side so it works with JavaScript disabled
# ===========================================================================
def render_chart(index: List[dict], width: int = 720, height: int = 190) -> str:
    points = [e for e in reversed(index) if e.get("current") is not None]
    if not points:
        return ('<p class="empty">No structured report data yet — the trend chart appears '
                'once the agent has written at least one JSON sidecar.</p>')
    if len(points) == 1:
        only = points[0]
        return (f'<p class="empty">Only one report so far ({pretty_date(only["date"])}, '
                f'{amt_text(rupees(only["current"]))}). '
                f'The trend chart needs at least two.</p>')

    pad_l, pad_r, pad_t, pad_b = 8, 8, 14, 24
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    values = [p["current"] for p in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or max(abs(hi), 1.0) * 0.02

    def x(i: int) -> float:
        return pad_l + (plot_w * i / (len(points) - 1))

    def y(value: float) -> float:
        return pad_t + plot_h - ((value - lo) / span * plot_h)

    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    area = f"{pad_l},{pad_t + plot_h} {line} {pad_l + plot_w},{pad_t + plot_h}"

    # SVG <title> text cannot be blurred by CSS, so each point carries both a
    # full tooltip and an amount-free one, and app.js swaps them as the toggle
    # changes. The initial <title> matches the configured default so that no
    # amount is exposed before the script runs - or at all, if JS is off.
    hidden_by_default = bool(PRIVACY.get("blur_by_default"))
    dot_parts: List[str] = []
    for index, value in enumerate(values):
        when = pretty_date(points[index]["date"])
        pct = percent(points[index].get("pnl_pct"))
        full = f"{when}: {rupees(value)} ({pct})"
        safe = f"{when}: amount hidden ({pct})"
        dot_parts.append(
            f'<circle class="dot" cx="{x(index):.1f}" cy="{y(value):.1f}" r="3"'
            f' data-full="{html.escape(full, quote=True)}"'
            f' data-safe="{html.escape(safe, quote=True)}">'
            f"<title>{html.escape(safe if hidden_by_default else full)}</title>"
            f"</circle>"
        )
    dots = "".join(dot_parts)

    first, last = points[0], points[-1]
    delta = last["current"] - first["current"]
    change_pct = (delta / first["current"] * 100) if first["current"] else 0.0

    return f"""<figure class="chart">
  <svg viewBox="0 0 {width} {height}" role="img" preserveAspectRatio="none"
       aria-label="Portfolio value from {first['date']} to {last['date']}">
    <polygon class="chart-area" points="{area}"/>
    <polyline class="chart-line" points="{line}"/>
    {dots}
  </svg>
  <figcaption>
    <span>{pretty_date(first['date'])}</span>
    <span class="chart-delta {tone(delta)}">{amt_text(rupees(delta, signed=True))}
      ({percent(change_pct)}) over {len(points)} reports</span>
    <span>{pretty_date(last['date'])}</span>
  </figcaption>
</figure>"""


# ===========================================================================
# Page rendering
# ===========================================================================
def page(title: str, body: str, *, active_date: Optional[str], index: List[dict],
         privacy: Optional[dict] = None) -> str:
    items = []
    for entry in index:
        classes = ["archive-item"]
        if entry["date"] == active_date:
            classes.append("is-active")
        if not entry["has_data"]:
            classes.append("is-legacy")
        meta = (f'<span class="archive-value">{amt_text(rupees(entry["current"]))}</span>'
                f'<span class="pill {tone(entry["pnl"])}">{percent(entry["pnl_pct"])}</span>'
                if entry["has_data"] else '<span class="pill none">legacy</span>')
        items.append(
            f'<li><a class="{" ".join(classes)}" href="/r/{entry["date"]}">'
            f'<span class="archive-date">{pretty_date(entry["date"])}</span>'
            f'<span class="archive-meta">{meta}</span></a></li>'
        )
    archive = "\n".join(items) or '<li class="empty">No reports found yet.</li>'

    # Privacy settings are rendered as data attributes so the stylesheet can blur
    # before any script runs - otherwise amounts would flash visible on load.
    cfg = privacy or {}
    default_on = "1" if cfg.get("blur_by_default") else "0"
    body_class = "privacy-on" if cfg.get("blur_by_default") else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body class="{body_class}"
      data-privacy-default="{default_on}"
      data-privacy-blur-on-blur="{1 if cfg.get('blur_on_focus_loss', True) else 0}"
      data-privacy-blur-on-hidden="{1 if cfg.get('blur_on_tab_hidden', True) else 0}"
      data-privacy-blur-on-keys="{1 if cfg.get('blur_on_screenshot_keys', True) else 0}"
      data-privacy-idle-seconds="{int(cfg.get('idle_seconds', 0) or 0)}">
<header class="topbar">
  <a class="brand" href="/">Portfolio reports</a>
  <span class="brand-sub">{len(index)} report{"s" if len(index) != 1 else ""} archived</span>
  <button type="button" id="privacy-toggle" class="privacy-btn"
          aria-pressed="false" title="Hide amounts (p). Hold Shift to peek.">
    <span class="privacy-label">Hide amounts</span>
  </button>
</header>
<div id="privacy-flash" class="privacy-flash" role="status" aria-live="polite" hidden></div>
<div class="layout">
  <aside class="sidebar">
    <h2 class="sidebar-title">Archive</h2>
    <ul class="archive">{archive}</ul>
  </aside>
  <main class="content">{body}</main>
</div>
<script src="/static/app.js" defer></script>
</body>
</html>"""


_CCY_SYMBOL = {"INR": "₹", "USD": "$"}
_BOOK_LABEL = {"IND": "India", "US": "US"}


def money(value: Optional[float], currency: str = "INR", *,
          signed: bool = False, decimals: int = 0) -> str:
    """Plain-text money. Use :func:`amt` for anything rendered into the page."""
    if value is None:
        return "—"
    unit = _CCY_SYMBOL.get(currency, currency + " ")
    sign = "+" if signed and value >= 0 else ("-" if signed and value < 0 else "")
    return f"{sign}{unit}{abs(value):,.{decimals}f}"


def amt(value: Optional[float], currency: str = "INR", **kwargs) -> str:
    """Money wrapped so privacy mode can blur it.

    Every monetary figure in the page goes through here. An em dash for a missing
    figure is left unwrapped - there is nothing to hide, and blurring it would
    imply a value exists.
    """
    text = money(value, currency, **kwargs)
    return text if text == "—" else f'<span class="amt">{text}</span>'


def amt_text(text: str) -> str:
    """Wrap pre-formatted money text (e.g. from :func:`rupees`)."""
    return text if text in ("—", "") else f'<span class="amt">{text}</span>'


def render_holdings_table(rows: List[dict], book: str, totals: Optional[dict],
                          *, show_inr: bool = False) -> str:
    if not rows:
        return ""
    currency = rows[0].get("currency", "INR")
    # A rupee column only earns its space when the book is priced in something
    # else. INDmoney pre-converts the US book, so normally it does not.
    is_us = show_inr and currency != "INR"

    cells = []
    for h in rows:
        flags = "".join(
            f'<span class="flag" title="{html.escape(f, quote=True)}">!</span>'
            for f in h.get("flags", [])
        )
        # A holding with no cost basis shows an em dash, never a zero.
        inr_cell = (f'<td data-sort="{h.get("current_inr") or 0}">'
                    f'{amt(h.get("current_inr"), "INR")}</td>') if is_us else ""
        cells.append(f"""<tr>
<td class="sym">{html.escape(h.get("display") or h["symbol"])}{flags}</td>
<td data-sort="{h["quantity"]}">{h["quantity"]:g}</td>
<td data-sort="{h.get("avg_price") or 0}">{amt(h.get("avg_price"), currency, decimals=2)}</td>
<td data-sort="{h.get("ltp") or 0}">{amt(h.get("ltp"), currency, decimals=2)}</td>
<td data-sort="{h.get("invested") or 0}">{amt(h.get("invested"), currency)}</td>
<td data-sort="{h.get("current") or 0}">{amt(h.get("current"), currency)}</td>
<td data-sort="{h.get("pnl") or 0}" class="{tone(h.get("pnl"))}">{amt(h.get("pnl"), currency, signed=True)}</td>
<td data-sort="{h.get("pnl_pct") or 0}" class="{tone(h.get("pnl_pct"))}">{percent(h.get("pnl_pct"))}</td>
<td data-sort="{h.get("day_pct") or 0}" class="{tone(h.get("day_pct"))}">{percent(h.get("day_pct"), decimals=2)}</td>
{inr_cell}</tr>""")

    foot = ""
    if totals:
        foot_inr = f'<td>{amt(totals.get("current_inr"), "INR")}</td>' if is_us else ""
        foot = f"""<tfoot><tr>
<td>Total</td><td></td><td></td><td></td>
<td>{amt(totals.get("invested"), currency)}</td>
<td>{amt(totals.get("current"), currency)}</td>
<td class="{tone(totals.get("pnl"))}">{amt(totals.get("pnl"), currency, signed=True)}</td>
<td class="{tone(totals.get("pnl_pct"))}">{percent(totals.get("pnl_pct"))}</td>
<td></td>{foot_inr}</tr></tfoot>"""

    inr_head = "<th>Value (₹)</th>" if is_us else ""
    return f"""<div class="table-wrap">
<table class="holdings sortable">
<thead><tr>
<th data-type="text">Symbol</th><th>Qty</th><th>Avg</th><th>LTP</th>
<th>Invested</th><th>Value</th><th>P&amp;L</th><th>P&amp;L %</th><th>Today</th>{inr_head}
</tr></thead>
<tbody>{"".join(cells)}</tbody>
{foot}
</table></div>"""


def render_books_card(payload: dict) -> str:
    """Side-by-side book summary, shown only when there is more than one book."""
    books = payload.get("books") or {}
    if len(books) < 2:
        return ""
    fx = payload.get("fx") or {}
    tiles = []
    for key in ("IND", "US"):
        totals = books.get(key)
        if not totals:
            continue
        currency = totals.get("currency", "INR")
        extra = ""
        if key == "US" and totals.get("current_inr") is not None:
            extra = f'<span class="book-inr">{amt(totals["current_inr"], "INR")}</span>'
        uncosted = totals.get("uncosted_count") or 0
        # Say plainly that invested and P&L cover a subset, so the three numbers
        # in this tile are not expected to subtract to each other.
        note = (f'<span class="pill none" title="Invested and P&amp;L cover only the '
                f'holdings whose cost basis the broker shared, so they will not equal '
                f'Value minus Invested.">{uncosted} without cost basis</span>'
                if uncosted else "")
        tiles.append(f"""<div class="book-tile">
<div class="book-head"><h3>{_BOOK_LABEL.get(key, key)}</h3>
<span class="pill {tone(totals.get("pnl"))}">{percent(totals.get("pnl_pct"))}</span></div>
<p class="book-value">{amt(totals.get("current"), currency)}{extra}</p>
<p class="book-meta">{totals.get("count", 0)} holdings ·
invested {amt(totals.get("invested"), currency)} ·
P&amp;L {amt(totals.get("pnl"), currency, signed=True)}</p>
{note}</div>""")

    us_currency = (books.get("US") or {}).get("currency", "INR")
    if us_currency == "INR":
        fx_line = ('<p class="subtle">The US book is shown in rupees because INDmoney '
                   'reports US positions already converted. No exchange rate is '
                   'applied.</p>')
    elif fx.get("usd_inr"):
        fx_line = (f'<p class="subtle">Combined rupee figures use USD/INR '
                   f'{fx["usd_inr"]:,.2f} — {html.escape(str(fx.get("source", "")))}.</p>')
    else:
        fx_line = ('<p class="subtle">No USD/INR rate was available, so the US book is '
                   'shown in its own currency only and is not included in the combined '
                   'total.</p>')
    return (f'<section class="card"><h2>Books</h2>'
            f'<div class="book-grid">{"".join(tiles)}</div>{fx_line}</section>')


def render_report_page(payload: dict) -> str:
    totals = payload.get("totals", {})
    date = payload.get("date", "")
    news = payload.get("news", {})
    rated = sum(1 for n in news.values() if n.get("score") is not None)

    def stat(label: str, value: str, cls: str = "") -> str:
        return (f'<div class="stat"><span class="stat-label">{label}</span>'
                f'<span class="stat-value {cls}">{value}</span></div>')

    stats = [
        stat("Invested", amt_text(rupees(totals.get("invested")))),
        stat("Current value", amt_text(rupees(totals.get("current")))),
        stat("Overall P&amp;L",
             f'{amt_text(rupees(totals.get("pnl"), signed=True))} '
             f'<small>{percent(totals.get("pnl_pct"))}'
             + (" · costed only" if payload.get("uncosted") else "")
             + "</small>",
             tone(totals.get("pnl"))),
    ]
    if totals.get("day_pnl") is not None:
        stats.append(stat("Change today",
                          amt_text(rupees(totals["day_pnl"], signed=True)),
                          tone(totals["day_pnl"])))
    stats.append(stat("Holdings",
                      f'{totals.get("holdings_count", 0)} '
                      f'<small>{totals.get("profitable_count", 0)} up / '
                      f'{totals.get("losing_count", 0)} down</small>'))
    if totals.get("largest_position"):
        stats.append(stat("Largest position",
                          f'{html.escape(str(totals["largest_position"]))} '
                          f'<small>{percent(totals.get("concentration_pct"), signed=False)} '
                          f'of value</small>'))

    # --- holdings, split by book (sortable; data-sort carries raw numerics) ---
    all_rows = payload.get("holdings", [])
    books = payload.get("books") or {}
    if not all_rows:
        holdings_table = '<p class="empty">No holdings in this report.</p>'
    elif len(books) > 1:
        show_inr = any((b or {}).get("currency", "INR") != "INR" for b in books.values())
        parts = []
        for key in ("IND", "US"):
            rows = [h for h in all_rows if h.get("book", "IND") == key]
            if not rows:
                continue
            currency = rows[0].get("currency", "INR")
            parts.append(f'<h3 class="subhead">{_BOOK_LABEL.get(key, key)} '
                         f'<span class="pill none">{currency}</span></h3>'
                         + render_holdings_table(rows, key, books.get(key),
                                                 show_inr=show_inr))
        holdings_table = "".join(parts)
    else:
        only = next(iter(books), "IND")
        holdings_table = render_holdings_table(all_rows, only, books.get(only))

    # --- news, held first then watchlist ---
    def news_card(symbol: str, item: dict) -> str:
        score = item.get("score")
        badge = (f'<span class="score {score_tone(score)}">{score}<small>/10</small></span>'
                 if score is not None else '<span class="score none">n/a</span>')
        label = html.escape(str(item.get("label", "")))
        conf = html.escape(str(item.get("confidence", "")))
        reason = html.escape(str(item.get("reason") or ""))
        # INDmoney's own sentiment, when it supplied one, shown next to ours
        # rather than blended into it.
        broker = item.get("broker_sentiment")
        broker_badge = ""
        if broker is not None:
            note = html.escape(str(item.get("broker_sentiment_note") or ""))
            gap = abs(float(broker) - score) if score is not None else 0.0
            cls = "down" if gap >= 3.0 else "none"
            broker_badge = (f'<span class="pill {cls}" title="INDmoney sentiment'
                            f'{" — " + note if note else ""}">INDmoney '
                            f'{float(broker):.1f}/10</span>')
        links = "".join(
            f'<li><a href="{html.escape(a["link"], quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{html.escape(a["title"])}</a>'
            f'<span class="source">{html.escape(a["source"])}</span></li>'
            if a.get("link") else
            f'<li>{html.escape(a["title"])}<span class="source">{html.escape(a["source"])}</span></li>'
            for a in item.get("articles", [])
        )
        count = item.get("headline_count", 0)
        return f"""<article class="news-card">
<header>{badge}<div><h4>{html.escape(symbol)}</h4>
<p class="news-meta">{label} · {count} article{"s" if count != 1 else ""} · confidence {conf}</p>
{broker_badge}</div></header>
{f'<p class="news-reason">{reason}</p>' if reason else ''}
<ul class="news-links">{links}</ul>
</article>"""

    held_cards = [news_card(s, n) for s, n in sorted(news.items()) if n.get("held")]
    watch_cards = [news_card(s, n) for s, n in sorted(news.items()) if not n.get("held")]

    news_html = ""
    if held_cards:
        news_html += '<h3 class="subhead">Holdings</h3><div class="news-grid">' \
                     + "".join(held_cards) + "</div>"
    if watch_cards:
        news_html += '<h3 class="subhead">Watchlist <span class="pill none">not held</span></h3>' \
                     '<div class="news-grid">' + "".join(watch_cards) + "</div>"
    if not news_html:
        news_html = '<p class="empty">No articles matched the portfolio or watchlist.</p>'

    commentary = payload.get("commentary", {}) or {}
    paragraphs = "".join(f"<p>{html.escape(p.strip())}</p>"
                         for p in (commentary.get("text") or "").split("\n\n") if p.strip())

    quality = payload.get("data_quality") or []
    rejected = payload.get("rejected_commentary") or []
    quality_items = "".join(f"<li>{html.escape(q)}</li>" for q in quality)
    if rejected:
        quality_items += ("<li>Model commentary was rejected by validation: "
                          + html.escape("; ".join(rejected[:6])) + ".</li>")
    quality_html = (f"<ul>{quality_items}</ul>" if quality_items else
                    '<p class="ok">No issues detected. All figures reconcile and every '
                    'tracked stock was rated.</p>')

    generated = html.escape(str(payload.get("generated_at", "")))
    model = html.escape(str(payload.get("model", "unknown")))

    return f"""<div class="page-head">
<div><h1>{pretty_date(date)}</h1>
<p class="subtle">Generated {generated} · model <code>{model}</code> ·
{rated}/{len(news)} tracked stocks rated</p></div>
<a class="btn" href="/raw/{date}.md">View markdown</a>
</div>

<section class="card"><div class="stats">{"".join(stats)}</div></section>

{render_books_card(payload)}

<section class="card"><h2>Holdings</h2>
<p class="hint">Click a column heading to sort.</p>
{holdings_table}</section>

<section class="card"><h2>News and sentiment</h2>{news_html}</section>

<section class="card"><h2>Commentary</h2>
{paragraphs or '<p class="empty">No commentary in this report.</p>'}
<p class="subtle">Provenance: {html.escape(str(commentary.get("provenance", "unknown")))}</p>
</section>

<section class="card"><h2>Data quality</h2>{quality_html}
<p class="subtle">Every figure above is computed from the broker payload. The commentary
is the only model-written text and is validated against those figures before publication.</p>
</section>"""


def render_legacy_page(date: str, markdown: str) -> str:
    return f"""<div class="page-head">
<div><h1>{pretty_date(date)}</h1>
<p class="subtle">Legacy report — no structured data, rendered from markdown</p></div>
<a class="btn" href="/raw/{date}.md">View markdown</a>
</div>
<section class="card legacy">{render_markdown(markdown)}</section>"""


def render_home(index: List[dict]) -> Tuple[str, Optional[str]]:
    """Latest report with the trend chart on top. Returns (body, active_date)."""
    if not index:
        return ("""<div class="page-head"><div><h1>No reports yet</h1>
<p class="subtle">Nothing has been generated in <code>pfm/reports/</code>.</p></div></div>
<section class="card"><h2>Get started</h2>
<p>Run the agent once to produce a report:</p>
<pre>cd pfm
python agent.py --once           # real run against Kite
python agent.py --dry-run --no-llm   # offline, using fixture holdings</pre>
<p>Reports appear here automatically — this page reads the directory on every request,
so no restart is needed.</p></section>""", None)

    latest = index[0]
    chart = render_chart(index)
    body = f'<section class="card chart-card"><h2>Portfolio value over time</h2>{chart}</section>'

    payload = load_payload(latest["date"])
    if payload:
        return body + render_report_page(payload), latest["date"]
    markdown = load_markdown(latest["date"]) or ""
    return body + render_legacy_page(latest["date"], markdown), latest["date"]


# ===========================================================================
# HTTP handler
# ===========================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "pfm-web/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:      # route through logging
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str,
              *, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, body.encode("utf-8"), "text/html; charset=utf-8")

    def _json(self, obj, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _text(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, body.encode("utf-8"), "text/plain; charset=utf-8")

    def _not_found(self, message: str = "Not found") -> None:
        index = build_index()
        self._html(page("Not found", f"""<div class="page-head"><div><h1>Not found</h1>
<p class="subtle">{html.escape(message)}</p></div></div>
<section class="card"><p><a href="/">Back to the latest report</a></p></section>""",
                        active_date=None, index=index), HTTPStatus.NOT_FOUND)

    # -- routing ----------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        try:
            self.route(path)
        except BrokenPipeError:
            pass
        except Exception:
            log.exception("Error serving %s", path)
            try:
                self._text("Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:
                pass

    def route(self, path: str) -> None:
        if path in ("/healthz", "/healthz/"):
            reports = build_index()
            self._json({"ok": True, "reports": len(reports),
                        "latest": reports[0]["date"] if reports else None})
            return

        if path.startswith("/static/"):
            self.serve_static(path[len("/static/"):])
            return

        if path in ("/", "/index.html"):
            index = build_index()
            body, active = render_home(index)
            self._html(page("Portfolio reports", body, active_date=active, index=index, privacy=PRIVACY))
            return

        if path == "/api/reports":
            self._json({"reports": build_index()})
            return

        api = re.match(r"^/api/reports/(.+)$", path)
        if api:
            date = api.group(1)
            if not DATE_RE.match(date):
                self._json({"error": "date must be YYYY-MM-DD"}, HTTPStatus.BAD_REQUEST)
                return
            payload = load_payload(date)
            if payload is None:
                self._json({"error": f"no structured data for {date}"}, HTTPStatus.NOT_FOUND)
                return
            self._json(payload)
            return

        raw = re.match(r"^/raw/(.+?)(?:\.md)?$", path)
        if raw and path.startswith("/raw/"):
            date = raw.group(1)
            if not DATE_RE.match(date):
                self._text("date must be YYYY-MM-DD", HTTPStatus.BAD_REQUEST)
                return
            markdown = load_markdown(date)
            if markdown is None:
                self._text(f"No markdown report for {date}", HTTPStatus.NOT_FOUND)
                return
            self._text(markdown)
            return

        report = re.match(r"^/r/(.+?)/?$", path)
        if report:
            date = report.group(1)
            if not DATE_RE.match(date):
                self._not_found("Report dates look like /r/2026-08-02.")
                return
            index = build_index()
            payload = load_payload(date)
            if payload:
                body = render_report_page(payload)
            else:
                markdown = load_markdown(date)
                if markdown is None:
                    self._not_found(f"No report for {date}.")
                    return
                body = render_legacy_page(date, markdown)
            self._html(page(f"Portfolio report {date}", body, active_date=date, index=index, privacy=PRIVACY))
            return

        self._not_found(f"No route for {path}")

    def serve_static(self, rel: str) -> None:
        """Serve pfm/static/. Rejects anything that escapes the directory."""
        types = {".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".svg": "image/svg+xml", ".ico": "image/x-icon",
                 ".png": "image/png", ".woff2": "font/woff2"}
        candidate = (STATIC_DIR / rel).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._text("Forbidden", HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            self._text("Not found", HTTPStatus.NOT_FOUND)
            return
        self._send(HTTPStatus.OK, candidate.read_bytes(),
                   types.get(candidate.suffix, "application/octet-stream"),
                   cache="public, max-age=300")


# ===========================================================================
# Entry point
# ===========================================================================
def _lan_ip() -> str:
    """Best-effort LAN address, purely so the startup log is useful."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.2)
            sock.connect(("192.168.1.1", 1))
            return sock.getsockname()[0]
    except Exception:
        return "0.0.0.0"


def main() -> int:
    parser = argparse.ArgumentParser(description="Portfolio report web view")
    parser.add_argument("--host", default=None, help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="port (default 7373)")
    parser.add_argument("--once", metavar="PATH",
                        help="render a single route to stdout and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    cfg = load_config()
    web_cfg = cfg.raw.get("web", {}) or {}
    host = args.host or web_cfg.get("host", "0.0.0.0")
    port = args.port or int(web_cfg.get("port", 7373))

    configured_privacy = web_cfg.get("privacy") or {}
    PRIVACY.update({k: v for k, v in configured_privacy.items()
                    if not str(k).startswith("_")})
    log.info("Privacy: amounts %s by default; auto-hide on "
             "focus loss=%s, tab hidden=%s, screenshot keys=%s, idle=%ss. "
             "Printing is always hidden.",
             "hidden" if PRIVACY.get("blur_by_default") else "visible",
             PRIVACY.get("blur_on_focus_loss"), PRIVACY.get("blur_on_tab_hidden"),
             PRIVACY.get("blur_on_screenshot_keys"), PRIVACY.get("idle_seconds"))

    if args.once:
        index = build_index()
        if args.once in ("/", "/index.html"):
            body, active = render_home(index)
            print(page("Portfolio reports", body, active_date=active, index=index, privacy=PRIVACY))
        elif args.once == "/api/reports":
            print(json.dumps({"reports": index}, indent=2))
        else:
            match = re.match(r"^/r/(.+)$", args.once)
            if not match:
                print(f"Cannot render {args.once} offline.")
                return 1
            payload = load_payload(match.group(1))
            body = (render_report_page(payload) if payload
                    else render_legacy_page(match.group(1),
                                            load_markdown(match.group(1)) or ""))
            print(page("Portfolio report", body, active_date=match.group(1), index=index, privacy=PRIVACY))
        return 0

    reports = build_index()
    log.info("Serving %d report(s) from %s", len(reports), REPORT_DIR)
    log.info("Local:   http://127.0.0.1:%d/", port)
    log.info("Network: http://%s:%d/", _lan_ip(), port)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
