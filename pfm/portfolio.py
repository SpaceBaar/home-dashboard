"""Deterministic portfolio mathematics. No LLM is involved anywhere in here.

Every number that reaches the report is computed once, in Python, from the
broker payloads.

Two invariants hold no matter what the brokers send:

1. **Arithmetic closes.** For the subset of holdings that have a known cost
   basis, ``sum(pnl) == costed_current - total_invested`` exactly.
2. **Nothing is invented.** A holding with no cost basis (INDmoney does not
   share the invested amount for broker-imported rows) reports ``None`` for
   return figures rather than a percentage computed against a zero. A USD
   holding with no FX rate available is never folded into a rupee total.

The earlier version mixed the broker's rupee P&L with locally derived
percentages, which disagree whenever quantity is pledged, in T+1, or partially
realised — that is how a report ends up with figures that cannot be reconciled
against each other.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from brokers import BOOK_IND, BOOK_US, extract_rows, normalise_kite

log = logging.getLogger("pfm.portfolio")

# A USD/INR rate outside this band means we have misread a field, not that the
# rupee has moved. Better to refuse the conversion than to publish nonsense.
FX_SANITY_RANGE = (60.0, 140.0)


@dataclass
class Holding:
    symbol: str
    exchange: str
    book: str                      # BOOK_IND | BOOK_US
    currency: str                  # "INR" | "USD"
    quantity: float
    avg_price: Optional[float]
    ltp: Optional[float]
    invested_native: Optional[float]
    current_native: float
    pnl_native: Optional[float]
    pnl_pct: Optional[float]
    fx_rate: float                 # native -> INR (1.0 when already INR)
    invested_inr: Optional[float]
    current_inr: Optional[float]   # None when a USD row has no usable FX rate
    pnl_inr: Optional[float]
    day_pct: Optional[float]
    broker_pnl_native: Optional[float] = None
    source: str = ""
    name: Optional[str] = None
    flags: List[str] = field(default_factory=list)

    @property
    def has_cost_basis(self) -> bool:
        return self.invested_native is not None and self.invested_native > 0

    # The bare names are always RUPEES, so report code that adds them together
    # cannot accidentally mix currencies. Use the *_native fields to show a
    # holding in its own currency.
    @property
    def invested(self) -> Optional[float]:
        return self.invested_inr

    @property
    def current(self) -> Optional[float]:
        return self.current_inr

    @property
    def pnl(self) -> Optional[float]:
        return self.pnl_inr

    @property
    def has_ticker(self) -> bool:
        """False when the identifier is INDmoney's instrument code, not a ticker."""
        return bool(re.match(r"^[A-Z]{1,5}(?:\.[A-Z])?$", self.symbol or ""))

    @property
    def display(self) -> str:
        """What to print. INDmoney's own name when it gave us no ticker.

        Never a fabricated abbreviation: if INDmoney supplies no ticker, the
        report shows the instrument name INDmoney actually returned.
        """
        if self.has_ticker:
            return self.symbol
        return (self.name or self.symbol or "").strip() or self.symbol


@dataclass
class BookTotals:
    """Subtotals for one book, in its own currency and in rupees."""

    book: str
    currency: str
    invested: Optional[float]          # native, costed rows only
    current: float                     # native, all rows
    costed_current: Optional[float]    # native, costed rows only
    pnl: Optional[float]
    pnl_pct: Optional[float]
    current_inr: Optional[float]
    invested_inr: Optional[float]
    pnl_inr: Optional[float]
    count: int
    uncosted_count: int


@dataclass
class FactSheet:
    """The single source of truth handed to the report and to the LLM prompt."""

    total_invested: float              # INR, costed rows only
    total_current: float               # INR, all rows with a usable rate
    total_pnl: float                   # INR
    total_pnl_pct: float
    day_pnl: Optional[float]
    holdings: List[Holding]
    winners: List[Holding]
    losers: List[Holding]
    top_by_value: List[Holding]
    concentration_pct: float
    profitable_count: int
    losing_count: int
    data_quality: List[str]
    books: Dict[str, BookTotals] = field(default_factory=dict)
    usd_inr: Optional[float] = None
    fx_source: str = ""
    uncosted: List[str] = field(default_factory=list)
    excluded_value_inr: float = 0.0

    @property
    def symbols(self) -> List[str]:
        return [h.symbol for h in self.holdings]

    @property
    def has_us_book(self) -> bool:
        return BOOK_US in self.books


# ---------------------------------------------------------------------------
# Parsing the MCP payload (kept for the Kite path and for tests)
# ---------------------------------------------------------------------------
def extract_holdings_json(text: str) -> Optional[List[dict]]:
    """Pull a holdings list out of MCP tool output.

    Both servers answer unauthenticated calls with plain-text messages rather
    than raising, so a None return must be read as "not authenticated", never
    as "no holdings".
    """
    if not text:
        return None
    try:
        return extract_rows(json.loads(text), hint_keys=("holdings", "net", "data"))
    except (json.JSONDecodeError, TypeError):
        pass

    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if fenced:
        try:
            found = extract_rows(json.loads(fenced.group(1)))
            if found:
                return found
        except json.JSONDecodeError:
            pass

    array = re.search(r"(\[\s*\{[\s\S]*\}\s*\])", text)
    if array:
        try:
            found = extract_rows(json.loads(array.group(1)))
            if found:
                return found
        except json.JSONDecodeError:
            pass
    return None


def _is_normalised(item: Dict[str, Any]) -> bool:
    return "book" in item and "current_native" in item


# ---------------------------------------------------------------------------
# FX
# ---------------------------------------------------------------------------
def resolve_fx(
    configured: Optional[float],
    snapshot_derived: Optional[float],
) -> tuple[Optional[float], str]:
    """Pick a USD/INR rate and say where it came from.

    Preference order: an explicit config value, then a rate implied by
    INDmoney's own rupee totals. No rate is ever assumed — without one, the US
    book is reported in dollars only and no combined figure is published.
    """
    for value, source in ((configured, "configured in config.json"),
                          (snapshot_derived, "implied by INDmoney rupee totals")):
        if value is None:
            continue
        if FX_SANITY_RANGE[0] <= value <= FX_SANITY_RANGE[1]:
            return float(value), f"{source} ({value:.2f})"
        log.warning("Ignoring implausible USD/INR rate %.4f from %s", value, source)
    return None, "unavailable"


# ---------------------------------------------------------------------------
# Building the fact sheet
# ---------------------------------------------------------------------------
def build_fact_sheet(
    holdings_raw: Sequence[Dict[str, Any]],
    *,
    mismatch_tolerance_pct: float = 1.0,
    usd_inr: Optional[float] = None,
    fx_source: str = "",
) -> FactSheet:
    """Compute every reported figure from normalised (or raw Kite) holding rows."""
    holdings: List[Holding] = []
    data_quality: List[str] = []
    uncosted: List[str] = []
    day_pnl_total = 0.0
    day_pnl_known = False
    excluded_value_inr = 0.0

    for item in holdings_raw:
        # Accept raw Kite rows directly so existing callers and fixtures work.
        row = item if _is_normalised(item) else normalise_kite(item)
        if row is None:
            continue

        symbol = str(row.get("symbol") or "UNKNOWN").upper()
        book = row.get("book") or BOOK_IND
        currency = (row.get("currency") or "INR").upper()
        quantity = float(row.get("quantity") or 0.0)
        avg = row.get("avg_price")
        ltp = row.get("ltp")
        invested = row.get("invested_native")
        current = row.get("current_native")
        flags = list(row.get("flags") or [])

        if not current or current <= 0:
            data_quality.append(f"{symbol}: no current value reported; excluded from totals.")
            continue
        if quantity <= 0:
            data_quality.append(f"{symbol}: zero quantity reported; excluded from totals.")
            continue

        # Native P&L, and the percentage only when a cost basis exists.
        if invested is not None and invested > 0:
            pnl_native: Optional[float] = current - invested
            pnl_pct: Optional[float] = pnl_native / invested * 100.0
        else:
            pnl_native, pnl_pct = None, None
            uncosted.append(symbol)

        # Broker-reported P&L is a cross-check, never the reported figure.
        broker_pnl = row.get("broker_pnl_native")
        if broker_pnl is not None and invested and pnl_native is not None:
            tolerance = max(1.0, invested * mismatch_tolerance_pct / 100.0)
            if abs(broker_pnl - pnl_native) > tolerance:
                unit = "$" if currency == "USD" else "Rs"
                flags.append(f"broker P&L {unit}{broker_pnl:+,.0f} vs derived {unit}{pnl_native:+,.0f}")
                data_quality.append(
                    f"{symbol}: broker-reported P&L ({unit}{broker_pnl:+,.0f}) differs from "
                    f"quantity x price arithmetic ({unit}{pnl_native:+,.0f}). The derived figure "
                    f"is used so the report totals reconcile; the difference usually means part "
                    f"of the position is pledged, settling, or partially realised."
                )

        # Convert to rupees. USD rows without a rate stay out of every INR total.
        if currency == "INR":
            fx_rate = 1.0
        elif usd_inr:
            fx_rate = float(usd_inr)
        else:
            fx_rate = 0.0

        if fx_rate:
            current_inr: Optional[float] = current * fx_rate
            invested_inr: Optional[float] = invested * fx_rate if invested is not None else None
            pnl_inr: Optional[float] = pnl_native * fx_rate if pnl_native is not None else None
        else:
            current_inr = invested_inr = pnl_inr = None
            flags.append("no USD/INR rate available; excluded from the combined rupee total")

        holdings.append(Holding(
            symbol=symbol, exchange=str(row.get("exchange") or ""), book=book,
            currency=currency, quantity=quantity, avg_price=avg, ltp=ltp,
            invested_native=invested, current_native=current,
            pnl_native=pnl_native, pnl_pct=pnl_pct,
            fx_rate=fx_rate, invested_inr=invested_inr, current_inr=current_inr,
            pnl_inr=pnl_inr, day_pct=row.get("day_pct"),
            broker_pnl_native=broker_pnl, source=str(row.get("source") or ""),
            name=row.get("name"), flags=flags,
        ))

    # -- cross-book duplicate guard ----------------------------------------
    # INDmoney aggregates the same Zerodha account Kite reports directly, so a
    # misclassified row would show one holding in both books and double-count it.
    # Kite wins for anything Indian: it is the primary source, with a real ticker
    # and a real cost basis.
    by_symbol: Dict[str, List[Holding]] = {}
    for holding in holdings:
        by_symbol.setdefault(holding.symbol, []).append(holding)

    duplicates = {sym: rows for sym, rows in by_symbol.items() if len(rows) > 1}
    if duplicates:
        keep: List[Holding] = []
        for holding in holdings:
            rows = duplicates.get(holding.symbol)
            if not rows:
                keep.append(holding)
                continue
            # Prefer Kite, then the row that actually has a cost basis.
            best = min(rows, key=lambda h: (h.source != "kite", not h.has_cost_basis))
            if holding is best:
                keep.append(holding)
        for symbol, rows in duplicates.items():
            sources = ", ".join(sorted({f"{h.source or 'unknown'}/{h.book}" for h in rows}))
            data_quality.append(
                f"{symbol} was reported by more than one source ({sources}). Only the "
                f"Zerodha/Kite row is counted, so the holding is not double-counted "
                f"and does not appear under the wrong book."
            )
            log.warning("Duplicate holding %s across books (%s); kept the Kite row.",
                        symbol, sources)
        holdings = keep

    # Sort by rupee value where known, otherwise push to the end.
    holdings.sort(key=lambda h: (h.current_inr if h.current_inr is not None else -1),
                  reverse=True)

    # -- per-book subtotals -------------------------------------------------
    books: Dict[str, BookTotals] = {}
    for book in (BOOK_IND, BOOK_US):
        rows = [h for h in holdings if h.book == book]
        if not rows:
            continue
        costed = [h for h in rows if h.has_cost_basis]
        invested_native = sum(h.invested_native or 0.0 for h in costed) or None
        costed_current = sum(h.current_native for h in costed) if costed else None
        pnl_native = (costed_current - invested_native
                      if (invested_native and costed_current is not None) else None)
        books[book] = BookTotals(
            book=book,
            currency=rows[0].currency,
            invested=invested_native,
            current=sum(h.current_native for h in rows),
            costed_current=costed_current,
            pnl=pnl_native,
            pnl_pct=(pnl_native / invested_native * 100.0)
                    if (pnl_native is not None and invested_native) else None,
            current_inr=(sum(h.current_inr for h in rows if h.current_inr is not None)
                         if any(h.current_inr is not None for h in rows) else None),
            invested_inr=(sum(h.invested_inr for h in costed if h.invested_inr is not None)
                          if any(h.invested_inr is not None for h in costed) else None),
            pnl_inr=(sum(h.pnl_inr for h in costed if h.pnl_inr is not None)
                     if any(h.pnl_inr is not None for h in costed) else None),
            count=len(rows),
            uncosted_count=len(rows) - len(costed),
        )

    # -- combined rupee totals ---------------------------------------------
    convertible = [h for h in holdings if h.current_inr is not None]
    costed_inr = [h for h in convertible if h.has_cost_basis and h.invested_inr is not None]

    total_invested = sum(h.invested_inr or 0.0 for h in costed_inr)
    costed_current = sum(h.current_inr or 0.0 for h in costed_inr)
    total_current = sum(h.current_inr or 0.0 for h in convertible)
    # Invariant: this equals sum(h.pnl_inr) over the costed set, exactly.
    total_pnl = costed_current - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100.0) if total_invested else 0.0

    excluded = [h for h in holdings if h.current_inr is None]
    if excluded:
        excluded_value_inr = 0.0
        data_quality.append(
            "No USD/INR rate was available, so "
            + ", ".join(sorted(h.symbol for h in excluded))
            + " are reported in dollars only and are not part of the combined rupee total. "
              "Set portfolio.usd_inr_rate in config.json to include them."
        )

    for holding in holdings:
        if holding.day_pct is not None and holding.current_inr and holding.day_pct != -100:
            day_pnl_total += holding.current_inr - (holding.current_inr / (1 + holding.day_pct / 100.0))
            day_pnl_known = True

    by_pct = sorted((h for h in holdings if h.pnl_pct is not None),
                    key=lambda h: h.pnl_pct, reverse=True)
    winners = [h for h in by_pct if (h.pnl_native or 0) > 0][:3]
    losers = [h for h in reversed(by_pct) if (h.pnl_native or 0) < 0][:3]
    top_by_value = convertible[:3] or holdings[:3]
    concentration = ((convertible[0].current_inr / total_current * 100.0)
                     if convertible and total_current else 0.0)

    if uncosted:
        data_quality.append(
            "No cost basis was shared for " + ", ".join(sorted(set(uncosted)))
            + ", so their return figures are shown as unavailable rather than estimated. "
              "INDmoney does not pass through the original invested amount for holdings "
              "imported from a linked broker."
        )

    residual = abs(sum(h.pnl_inr or 0.0 for h in costed_inr) - total_pnl)
    if residual > 0.01:
        data_quality.append(f"Internal arithmetic residual of Rs{residual:.2f} detected.")

    return FactSheet(
        total_invested=total_invested,
        total_current=total_current,
        total_pnl=total_pnl,
        total_pnl_pct=total_pnl_pct,
        day_pnl=day_pnl_total if day_pnl_known else None,
        holdings=holdings,
        winners=winners,
        losers=losers,
        top_by_value=top_by_value,
        concentration_pct=concentration,
        profitable_count=sum(1 for h in holdings if (h.pnl_native or 0) > 0),
        losing_count=sum(1 for h in holdings if (h.pnl_native or 0) < 0),
        data_quality=data_quality,
        books=books,
        usd_inr=usd_inr,
        fx_source=fx_source,
        uncosted=sorted(set(uncosted)),
        excluded_value_inr=excluded_value_inr,
    )


def render_fact_block(fs: FactSheet) -> str:
    """The compact, unambiguous fact block given to the LLM.

    Deliberately small: a 1.5B model handed twenty rows of numbers starts
    blending tickers together. It gets pre-chewed conclusions instead, and is
    forbidden from doing any arithmetic of its own.
    """
    def names(items: List[Holding]) -> str:
        return ", ".join(f"{h.display} ({h.pnl_pct:+.1f}%)" for h in items) or "none"

    lines = [
        f"Total invested: Rs {fs.total_invested:,.0f}",
        f"Current value: Rs {fs.total_current:,.0f}",
        f"Overall profit/loss: Rs {fs.total_pnl:+,.0f} ({fs.total_pnl_pct:+.1f}%)",
        f"Number of holdings: {len(fs.holdings)} "
        f"({fs.profitable_count} in profit, {fs.losing_count} in loss)",
        f"Largest holding by value: {fs.top_by_value[0].display if fs.top_by_value else 'none'} "
        f"at {fs.concentration_pct:.0f}% of the portfolio",
        f"Best performers: {names(fs.winners)}",
        f"Worst performers: {names(fs.losers)}",
    ]

    india = fs.books.get(BOOK_IND)
    us = fs.books.get(BOOK_US)
    if india and us:
        lines.append(f"Indian holdings: {india.count} worth Rs {india.current:,.0f}")
        if us.current_inr is not None:
            lines.append(f"US holdings: {us.count} worth USD {us.current:,.0f} "
                         f"(Rs {us.current_inr:,.0f})")
        else:
            lines.append(f"US holdings: {us.count} worth USD {us.current:,.0f}")
    if fs.uncosted:
        lines.append("No cost basis available for: " + ", ".join(fs.uncosted))
    return "\n".join(lines)
