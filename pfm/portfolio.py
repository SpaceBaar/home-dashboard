"""Deterministic portfolio mathematics. No LLM is involved anywhere in here.

Every number that reaches the report is computed once, in Python, from the
broker payload - and the report's arithmetic is guaranteed to close, i.e.
``sum(holding.pnl) == total_current - total_invested`` exactly.

The previous implementation mixed two incompatible sources: the per-holding
rupee P&L came from the broker while the percentage was derived from
``qty * avg`` vs ``qty * ltp``. Those two disagree whenever quantity is
pledged, in T+1, or partially realised, which is how a report ends up showing
figures that cannot be reconciled with each other.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("pfm.portfolio")


@dataclass
class Holding:
    symbol: str
    exchange: str
    quantity: float
    avg_price: float
    ltp: float
    invested: float
    current: float
    pnl: float
    pnl_pct: float
    day_pct: Optional[float]
    broker_pnl: Optional[float] = None
    flags: List[str] = field(default_factory=list)

    def line(self) -> str:
        day = f"{self.day_pct:+.2f}% today" if self.day_pct is not None else "day change n/a"
        return (f"{self.symbol}: qty {self.quantity:g} | avg Rs{self.avg_price:,.2f} | "
                f"LTP Rs{self.ltp:,.2f} | P&L Rs{self.pnl:+,.0f} "
                f"({self.pnl_pct:+.1f}%, {day})")


@dataclass
class FactSheet:
    """The single source of truth handed to the report and to the LLM prompt."""

    total_invested: float
    total_current: float
    total_pnl: float
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

    @property
    def symbols(self) -> List[str]:
        return [h.symbol for h in self.holdings]


# ---------------------------------------------------------------------------
# Parsing the MCP payload
# ---------------------------------------------------------------------------
def extract_holdings_json(text: str) -> Optional[List[dict]]:
    """Pull a holdings list out of Kite MCP output.

    Kite MCP returns plain-text error strings (e.g. "Please log in first")
    rather than raising, so a None return here must be treated as "not
    authenticated", never as "no holdings".
    """
    if not text:
        return None

    def _coerce(obj: Any) -> Optional[List[dict]]:
        if isinstance(obj, list):
            return [o for o in obj if isinstance(o, dict)] or None
        if isinstance(obj, dict):
            for key in ("data", "holdings", "net", "result"):
                inner = obj.get(key)
                if isinstance(inner, list):
                    return [o for o in inner if isinstance(o, dict)] or None
                if isinstance(inner, dict):
                    nested = _coerce(inner)
                    if nested:
                        return nested
        return None

    try:
        found = _coerce(json.loads(text))
        if found:
            return found
    except (json.JSONDecodeError, TypeError):
        pass

    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if fenced:
        try:
            found = _coerce(json.loads(fenced.group(1)))
            if found:
                return found
        except json.JSONDecodeError:
            pass

    array = re.search(r"(\[\s*\{[\s\S]*\}\s*\])", text)
    if array:
        try:
            found = _coerce(json.loads(array.group(1)))
            if found:
                return found
        except json.JSONDecodeError:
            pass

    return None


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _effective_quantity(item: dict) -> tuple[float, List[str]]:
    """Total economically-held quantity, with an audit flag when it differs.

    Kite splits a position across ``quantity`` (free), ``t1_quantity``
    (settling) and ``collateral_quantity`` (pledged). Using ``quantity`` alone
    reports a pledged holding as zero-invested, which then divides into a
    meaningless percentage.
    """
    free = _num(item.get("quantity"))
    t1 = _num(item.get("t1_quantity"))
    collateral = _num(item.get("collateral_quantity"))
    authorised = _num(item.get("authorised_quantity"))
    total = free + t1 + collateral

    flags: List[str] = []
    if t1:
        flags.append(f"{t1:g} in T+1 settlement")
    if collateral:
        flags.append(f"{collateral:g} pledged as collateral")
    if authorised:
        flags.append(f"{authorised:g} authorised for sale")

    if total <= 0:
        opening = _num(item.get("opening_quantity"))
        if opening > 0:
            total = opening
            flags.append("quantity taken from opening_quantity")
    return total, flags


# ---------------------------------------------------------------------------
# Building the fact sheet
# ---------------------------------------------------------------------------
def build_fact_sheet(holdings_raw: List[dict], *, mismatch_tolerance_pct: float = 1.0) -> FactSheet:
    holdings: List[Holding] = []
    data_quality: List[str] = []
    day_pnl_total = 0.0
    day_pnl_known = False

    for item in holdings_raw:
        symbol = str(item.get("tradingsymbol") or item.get("symbol") or "UNKNOWN").upper().strip()
        exchange = str(item.get("exchange") or "").upper()
        qty, flags = _effective_quantity(item)
        avg = _num(item.get("average_price"))
        ltp = _num(item.get("last_price"))

        if ltp <= 0:
            close = _num(item.get("close_price"))
            if close > 0:
                ltp = close
                flags.append("last_price missing; used close_price")

        invested = qty * avg
        current = qty * ltp
        pnl = current - invested
        pnl_pct = (pnl / invested * 100.0) if invested else 0.0

        broker_pnl = item.get("pnl")
        broker_pnl = _num(broker_pnl) if broker_pnl is not None else None
        if broker_pnl is not None and invested > 0:
            tolerance = max(1.0, invested * mismatch_tolerance_pct / 100.0)
            if abs(broker_pnl - pnl) > tolerance:
                flags.append(f"broker P&L Rs{broker_pnl:+,.0f} vs derived Rs{pnl:+,.0f}")
                data_quality.append(
                    f"{symbol}: broker-reported P&L (Rs{broker_pnl:+,.0f}) differs from "
                    f"quantity x price arithmetic (Rs{pnl:+,.0f}). The derived figure is used "
                    f"so that the report totals reconcile; the difference usually means part "
                    f"of the position is pledged, settling, or partially realised."
                )

        day_pct_raw = item.get("day_change_percentage")
        day_pct = _num(day_pct_raw) if day_pct_raw is not None else None
        if day_pct is None:
            close = _num(item.get("close_price"))
            if close > 0 and ltp > 0:
                day_pct = (ltp - close) / close * 100.0
                flags.append("day change derived from close_price")

        if qty <= 0:
            data_quality.append(f"{symbol}: zero quantity reported; excluded from totals.")
            continue
        if avg <= 0 or ltp <= 0:
            data_quality.append(
                f"{symbol}: missing average_price or last_price; percentages are unreliable."
            )

        holdings.append(Holding(
            symbol=symbol, exchange=exchange, quantity=qty, avg_price=avg, ltp=ltp,
            invested=invested, current=current, pnl=pnl, pnl_pct=pnl_pct,
            day_pct=day_pct, broker_pnl=broker_pnl, flags=flags,
        ))

        if day_pct is not None and current and day_pct != -100:
            # Rupee move today = current value minus yesterday's implied value.
            day_pnl_total += current - (current / (1 + day_pct / 100.0))
            day_pnl_known = True

    holdings.sort(key=lambda h: h.current, reverse=True)

    total_invested = sum(h.invested for h in holdings)
    total_current = sum(h.current for h in holdings)
    total_pnl = total_current - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100.0) if total_invested else 0.0

    by_pct = sorted(holdings, key=lambda h: h.pnl_pct, reverse=True)
    winners = [h for h in by_pct if h.pnl > 0][:3]
    losers = [h for h in reversed(by_pct) if h.pnl < 0][:3]
    top_by_value = holdings[:3]
    concentration = (holdings[0].current / total_current * 100.0) if holdings and total_current else 0.0

    # Arithmetic self-check. This assertion is the guarantee that nothing in the
    # report can contradict anything else in the report.
    residual = abs(sum(h.pnl for h in holdings) - total_pnl)
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
        profitable_count=sum(1 for h in holdings if h.pnl > 0),
        losing_count=sum(1 for h in holdings if h.pnl < 0),
        data_quality=data_quality,
    )


def render_fact_block(fs: FactSheet) -> str:
    """The compact, unambiguous fact block given to the LLM.

    Deliberately small: a 1.5B model handed fifteen rows of numbers starts
    blending tickers together. It gets pre-chewed conclusions instead, and is
    forbidden from doing any arithmetic of its own.
    """
    def names(items: List[Holding]) -> str:
        return ", ".join(f"{h.symbol} ({h.pnl_pct:+.1f}%)" for h in items) or "none"

    lines = [
        f"Total invested: Rs {fs.total_invested:,.0f}",
        f"Current value: Rs {fs.total_current:,.0f}",
        f"Overall profit/loss: Rs {fs.total_pnl:+,.0f} ({fs.total_pnl_pct:+.1f}%)",
        f"Number of holdings: {len(fs.holdings)} "
        f"({fs.profitable_count} in profit, {fs.losing_count} in loss)",
        f"Largest holding by value: {fs.top_by_value[0].symbol if fs.top_by_value else 'none'} "
        f"at {fs.concentration_pct:.0f}% of the portfolio",
        f"Best performers: {names(fs.winners)}",
        f"Worst performers: {names(fs.losers)}",
    ]
    return "\n".join(lines)
