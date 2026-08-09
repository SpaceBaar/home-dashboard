"""Broker providers and holdings normalisation.

Two sources feed the pipeline:

* **Zerodha Kite** (``mcp.kite.trade``) — Indian equity, priced in INR. Field
  shapes are documented and stable.
* **INDmoney** (``mcp.indmoney.com``) — used here for the US book. Read-only,
  OAuth 2.1 + PKCE, so ``mcp-remote`` handles sign-in and token refresh.

Everything downstream consumes one normalised shape, so adding a third broker
later means writing one ``normalise_*`` function and nothing else.

A deliberate note on INDmoney
-----------------------------
INDmoney does not publish a field-level schema, so the normaliser accepts a
range of plausible namings and — critically — **refuses to guess** when a row is
unintelligible. An unparseable row is dropped with a diagnostic rather than
contributing a zero to your totals. Run ``tools/probe_indmoney.py`` to capture
the real shapes and tighten ``_IND_FIELDS`` accordingly.

INDmoney documents that holdings imported from a linked broker may not expose
the original invested amount. Those rows arrive with ``invested_native = None``,
which propagates as a suppressed percentage rather than a fabricated one.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger("pfm.brokers")

KITE_MCP_URL = "https://mcp.kite.trade/mcp"
INDMONEY_MCP_URL = "https://mcp.indmoney.com/mcp"

# Books the report separates. Currency is a property of the holding, not the book,
# but in practice IND is INR and US is USD.
BOOK_IND = "IND"
BOOK_US = "US"


# ===========================================================================
# Helpers
# ===========================================================================
def _num(value: Any) -> Optional[float]:
    """Parse a number from int/float/str, returning None for anything unusable.

    Returns None rather than 0.0 on failure. That distinction is the whole point:
    a missing cost basis must not become a zero cost basis.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.lower() in {"unknown", "n/a", "na", "null", "none", "-", "--"}:
        return None
    # Tolerate "₹1,23,456.78", "$1,234", "1.23%", "12,345 USD"
    cleaned = re.sub(r"[^\d.\-+eE]", "", text.replace(",", ""))
    if cleaned in {"", "-", "+", ".", "-.", "+."}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _canon(name: str) -> str:
    """Canonical key form: lowercase, alphanumerics only.

    Makes ``unitPrice``, ``unit_price`` and ``Unit Price`` the same key, so the
    candidate lists below need one spelling per concept rather than one per
    naming convention.
    """
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _lookup(item: Dict[str, Any], name: str) -> Any:
    if name in item:
        return item[name]
    target = _canon(name)
    for key, value in item.items():
        if _canon(key) == target:
            return value
    return None


def _first(item: Dict[str, Any], names: Sequence[str]) -> Optional[float]:
    """First numerically-parseable value among candidate field names."""
    for name in names:
        parsed = _num(_lookup(item, name))
        if parsed is not None:
            return parsed
    return None


def _first_str(item: Dict[str, Any], names: Sequence[str]) -> Optional[str]:
    for name in names:
        value = _lookup(item, name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _flatten(item: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten one level of nesting so ``{"quote": {"ltp": 1}}`` exposes ``ltp``.

    Outer keys win, so a top-level field is never shadowed by a nested one.
    """
    flat: Dict[str, Any] = {}
    for key, value in item.items():
        if isinstance(value, dict):
            for inner_key, inner_value in value.items():
                if not isinstance(inner_value, (dict, list)):
                    flat.setdefault(inner_key, inner_value)
                    flat.setdefault(f"{key}_{inner_key}", inner_value)
        else:
            flat[key] = value
    for key, value in item.items():
        if not isinstance(value, dict):
            flat[key] = value
    return flat


def extract_rows(payload: Any, *, hint_keys: Sequence[str] = ()) -> Optional[List[dict]]:
    """Find the list of holding dicts inside an arbitrary MCP payload.

    Handles a bare list, a wrapper dict, and one or two levels of nesting, which
    covers every shape these servers have been observed to use.
    """
    if payload is None:
        return None
    if isinstance(payload, list):
        rows = [r for r in payload if isinstance(r, dict)]
        return rows or None

    if isinstance(payload, dict):
        candidates = list(hint_keys) + [
            "holdings", "data", "rows", "items", "positions", "net",
            "result", "results", "instruments", "stocks", "securities", "list",
        ]
        for key in candidates:
            if key in payload:
                found = extract_rows(payload[key], hint_keys=hint_keys)
                if found:
                    return found
        # Last resort: any list-of-dicts value anywhere in the object.
        for value in payload.values():
            if isinstance(value, list):
                rows = [r for r in value if isinstance(r, dict)]
                if rows:
                    return rows
            elif isinstance(value, dict):
                found = extract_rows(value, hint_keys=hint_keys)
                if found:
                    return found
    return None


def parse_tool_payload(text: Optional[str]) -> Any:
    """Parse an MCP tool's text block into JSON, tolerating fences and prose."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    for pattern in (r"(\[\s*[\{\[][\s\S]*[\}\]]\s*\])", r"(\{[\s\S]*\})"):
        match = re.search(pattern, text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
    return None


def looks_like_auth_error(text: Optional[str]) -> bool:
    """True when a tool result is really an authentication complaint.

    Both servers answer unauthenticated calls with prose instead of raising, so
    this is the only reliable liveness test.
    """
    if not text:
        return False
    if len(text) > 2000:          # a real payload, not an error sentence
        return False
    lowered = text.lower()
    signals = ("log in", "login", "unauthenticated", "unauthorized", "unauthorised",
               "authenticate", "session expired", "token expired", "not connected",
               "invalid token", "please connect", "re-auth", "403", "401")
    return any(signal in lowered for signal in signals)


# ===========================================================================
# Kite normalisation (INR, Indian book)
# ===========================================================================
def normalise_kite(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    symbol = _first_str(item, ["tradingsymbol", "symbol", "trading_symbol"])
    if not symbol:
        return None

    free = _num(item.get("quantity")) or 0.0
    t1 = _num(item.get("t1_quantity")) or 0.0
    collateral = _num(item.get("collateral_quantity")) or 0.0
    authorised = _num(item.get("authorised_quantity")) or 0.0
    quantity = free + t1 + collateral

    flags: List[str] = []
    if t1:
        flags.append(f"{t1:g} in T+1 settlement")
    if collateral:
        flags.append(f"{collateral:g} pledged as collateral")
    if authorised:
        flags.append(f"{authorised:g} authorised for sale")
    if quantity <= 0:
        opening = _num(item.get("opening_quantity")) or 0.0
        if opening > 0:
            quantity = opening
            flags.append("quantity taken from opening_quantity")

    avg = _num(item.get("average_price"))
    ltp = _num(item.get("last_price"))
    close = _num(item.get("close_price"))
    if not ltp and close:
        ltp = close
        flags.append("last_price missing; used close_price")

    day_pct = _num(item.get("day_change_percentage"))
    if day_pct is None and close and ltp:
        day_pct = (ltp - close) / close * 100.0
        flags.append("day change derived from close_price")

    return {
        "symbol": symbol.upper(),
        "exchange": (_first_str(item, ["exchange"]) or "").upper(),
        "book": BOOK_IND,
        "currency": "INR",
        "quantity": quantity,
        "avg_price": avg,
        "ltp": ltp,
        "invested_native": (quantity * avg) if (avg and quantity) else None,
        "current_native": (quantity * ltp) if (ltp and quantity) else None,
        "broker_pnl_native": _num(item.get("pnl")),
        "day_pct": day_pct,
        "source": "kite",
        "flags": flags,
    }


# ===========================================================================
# INDmoney normalisation (USD or INR, US book)
# ===========================================================================
# Field names confirmed against a real networth_holdings capture (2026-08-02):
#
#   investment_code  '203532'          investment    'Space Exploration Technologies…'
#   asset_type       'US_STOCK'        assetclass_l2 'Global Equity'
#   invested_amount  954.1999…         market_value  523.3831…
#   total_pnl        -430.8168…        pnl_per       -45.1495…
#   total_units      0.05061407        unit_price    10340.665…
#   broker           'INDmoney'        xirr          0
#
# Alternative spellings are kept as fallbacks in case the API shifts; lookups are
# by canonical key, so one spelling per concept covers every naming convention.
_IND_FIELDS = {
    # networth_holdings carries NO ticker field, only a long instrument name and
    # an internal code. Tickers are resolved separately via lookup_ind_keys.
    "symbol": ["symbol", "ticker", "ind_key", "tradingsymbol", "scrip"],
    "name": ["investment", "name", "display_name", "company_name",
             "instrument_name", "scheme_name", "short_name"],
    "code": ["investment_code", "instrument_code", "entity_id"],
    "quantity": ["total_units", "quantity", "units", "qty", "shares", "no_of_units"],
    "avg_price": ["average_price", "avg_price", "avg_buy_price", "buy_price",
                  "cost_price", "purchase_price"],
    "ltp": ["unit_price", "live_price", "last_price", "ltp", "current_price",
            "market_price", "price", "nav", "close_price"],
    "invested": ["invested_amount", "invested_value", "invested", "cost_value",
                 "buy_value", "amount_invested", "total_invested"],
    "current": ["market_value", "current_value", "present_value",
                "holding_value", "total_value", "value"],
    "pnl": ["total_pnl", "pnl", "profit_loss", "gain_loss", "unrealised_pnl",
            "returns", "total_returns", "gain"],
    "pnl_pct": ["pnl_per", "pnl_percentage", "returns_percentage",
                "return_percent", "gain_percentage"],
    "day_pct": ["day_change_percentage", "day_change_percent", "change_percent",
                "percent_change", "todays_change_percent"],
    "currency": ["currency", "ccy", "currency_code", "trade_currency"],
    "asset_class": ["asset_type", "asset_class", "assetclass_l2", "category", "type"],
    "broker": ["broker", "broker_name", "platform"],
    "xirr": ["xirr", "annualised_return", "annualized_return"],
}

# The book is decided by asset_type ALONE, matched exactly against this table.
#
# Two rules learned the hard way:
#   * Never fall back to assetclass_l2. It is a sector-ish label - 'Gold',
#     'Global Equity', 'Retirement' - and matching it put an Indian gold ETF in
#     the US book.
#   * Never default to US. An unrecognised asset_type is excluded and reported,
#     not guessed at. The previous version classified any row lacking an
#     asset_type as US with no evidence at all.
#
# Confirmed values: US holdings arrive as 'US_STOCK'; Indian equity and ETFs
# arrive as 'STOCK' (not 'IND_STOCK'). That distinction is what keeps INDmoney's
# mirror of the Zerodha holdings - which Kite already reports - out of the US book.
_ASSET_TYPE_BOOK = {
    # United States
    "US_STOCK": BOOK_US, "US_STOCKS": BOOK_US, "USSTOCK": BOOK_US,
    "US_ETF": BOOK_US, "US_EQUITY": BOOK_US, "USEQUITY": BOOK_US,
    # India, and everything else INDmoney aggregates. Kite is authoritative for
    # Indian equity, so these are deliberately excluded from the US book.
    "STOCK": BOOK_IND, "IND_STOCK": BOOK_IND, "INDSTOCK": BOOK_IND,
    "EQUITY": BOOK_IND, "ETF": BOOK_IND, "MF": BOOK_IND, "MUTUAL_FUND": BOOK_IND,
    "BOND": BOOK_IND, "NPS": BOOK_IND, "EPF": BOOK_IND, "PPF": BOOK_IND,
    "FD": BOOK_IND, "RD": BOOK_IND, "SAVINGS": BOOK_IND, "GOLD": BOOK_IND,
    "SGB": BOOK_IND, "REAL_ESTATE": BOOK_IND, "PMS": BOOK_IND, "AIF": BOOK_IND,
    "CRYPTO": BOOK_IND, "INSURANCE": BOOK_IND, "COMMODITY": BOOK_IND,
    # Cash balances, seen in networth_snapshot. Recognised so they never trigger
    # an "unrecognised asset_type" diagnostic; they carry no holdings rows.
    "US_STOCK_WALLET": BOOK_US, "SA": BOOK_IND, "SAVING_ACCOUNT": BOOK_IND,
}

_US_ASSET_HINTS = tuple(k for k, v in _ASSET_TYPE_BOOK.items() if v == BOOK_US)

# CONFIRMED, and the opposite of the obvious assumption: networth_holdings
# reports US positions ALREADY CONVERTED TO RUPEES. For the captured SpaceX row,
# 0.05061407 units x 10340.67 == 523.38 market_value, and an implied average of
# 18,852 per unit is ~$214 at 88 INR/USD - plausible for SpaceX secondaries,
# whereas $18,852 per share is not. get_us_stocks_details, by contrast, quotes in
# USD (AAPL live_price 308.91). Treating the holdings as USD would multiply the
# US book by ~88.
_INDMONEY_HOLDINGS_CURRENCY = "INR"


def _detect_currency(flat: Dict[str, Any], default: str) -> str:
    explicit = _first_str(flat, _IND_FIELDS["currency"])
    if explicit:
        upper = explicit.upper()
        if "USD" in upper or upper == "$":
            return "USD"
        if "INR" in upper or upper in {"₹", "RS", "RS."}:
            return "INR"
    return default


def normalise_indmoney(
    item: Dict[str, Any],
    *,
    book: str = BOOK_US,
    default_currency: str = _INDMONEY_HOLDINGS_CURRENCY,
) -> Optional[Dict[str, Any]]:
    """Normalise one INDmoney holding row.

    Returns None when the row cannot be understood well enough to be trusted;
    the caller logs it rather than letting a zero into the totals.
    """
    flat = _flatten(item)

    flags: List[str] = []
    symbol = _first_str(flat, _IND_FIELDS["symbol"])
    name = _first_str(flat, _IND_FIELDS["name"])

    if not symbol and name:
        # networth_holdings has no ticker field, so fall back to a label derived
        # from the instrument name. IndmoneyProvider.holdings() tries to replace
        # this with a real ind_key via lookup_ind_keys before the row is used.
        bracketed = re.search(r"\(([A-Z]{2,6})\)", name)
        if bracketed:
            symbol = bracketed.group(1)
        else:
            symbol = derive_label(name)
            flags.append(f"ticker not supplied by INDmoney; label derived from "
                         f"\"{name}\"")
    if not symbol:
        return None

    currency = _detect_currency(flat, default_currency)

    quantity = _first(flat, _IND_FIELDS["quantity"])
    ltp = _first(flat, _IND_FIELDS["ltp"])
    invested = _first(flat, _IND_FIELDS["invested"])
    current = _first(flat, _IND_FIELDS["current"])
    avg = _first(flat, _IND_FIELDS["avg_price"])
    broker_pnl = _first(flat, _IND_FIELDS["pnl"])
    day_pct = _first(flat, _IND_FIELDS["day_pct"])
    xirr = _first(flat, _IND_FIELDS["xirr"])

    # Fill gaps only where the arithmetic is unambiguous.
    if current is None and quantity and ltp:
        current = quantity * ltp
    if invested is None and quantity and avg:
        invested = quantity * avg
    if avg is None and invested and quantity:
        avg = invested / quantity
    if ltp is None and current and quantity:
        ltp = current / quantity
    if quantity is None and current and ltp and ltp:
        quantity = current / ltp
        flags.append("quantity derived from value / price")

    if current is None:
        return None      # without a current value the row is worthless

    # INDmoney documents that broker-imported rows may omit the cost basis; it
    # sends the string "unknown" rather than null. Observed alongside that: it
    # then fills total_pnl with the market value itself and pnl_per with 0, which
    # is a placeholder, not a P&L. Both must be discarded or the report would show
    # a 100%-gain position.
    if invested is None or invested <= 0:
        invested = None
        avg = None
        flags.append("invested amount not shared by the broker; "
                     "return figures suppressed for this holding")
        if broker_pnl is not None and current is not None and \
                abs(broker_pnl - current) < max(0.01, abs(current) * 0.001):
            broker_pnl = None
            flags.append("broker P&L equalled the market value, so it was a "
                         "placeholder and has been discarded")
        else:
            broker_pnl = None

    broker = _first_str(flat, _IND_FIELDS["broker"])
    if broker:
        flags.append(f"held via {broker}")
    if xirr is not None:
        flags.append(f"XIRR {xirr:+.1f}% (broker-reported)")

    asset_class = _first_str(flat, _IND_FIELDS["asset_class"]) or ""

    return {
        "symbol": symbol.upper(),
        "exchange": asset_class.upper() or ("NASDAQ/NYSE" if book == BOOK_US else ""),
        "book": book,
        "currency": currency,
        "quantity": quantity or 0.0,
        "avg_price": avg,
        "ltp": ltp,
        "invested_native": invested,
        "current_native": current,
        "broker_pnl_native": broker_pnl,
        "day_pct": day_pct,
        "source": "indmoney",
        "flags": flags,
        "name": name,
        # Kept so tickers can be resolved by an exact id join later.
        "investment_code": _first_str(flat, _IND_FIELDS["code"]),
    }


_LABEL_STOPWORDS = {"THE", "LTD", "LIMITED", "INC", "CORP", "CORPORATION", "PLC",
                    "CO", "COMPANY", "CLASS", "COMMON", "STOCK", "SHARES", "SHARE",
                    "HOLDINGS", "GROUP", "TECHNOLOGIES", "TECHNOLOGY", "AND", "OF",
                    "A", "B", "C", "ETF", "FUND", "TRUST", "PLC.", "CAPITAL",
                    "ORDINARY", "DEPOSITARY", "RECEIPTS", "ADR"}


_LOOKUP_SUFFIXES = re.compile(
    r"(?:,?\s+class\s+[a-z])?"
    r"(?:\s+(?:common\s+stock|capital\s+stock|ordinary\s+shares?|common\s+shares?|"
    r"depositary\s+receipts?|adr|shares?|stock))+\s*$", re.IGNORECASE)


def shorten_for_lookup(name: str, limit: int = 32) -> str:
    """Trim an instrument name to something a search endpoint will accept.

    ``lookup_ind_keys`` puts names in a query string and answered HTTP 414 (URI
    Too Long) for the full 57-character SpaceX name, so boilerplate suffixes are
    stripped and the result is capped at whole words.
    """
    trimmed = _LOOKUP_SUFFIXES.sub("", name.strip())
    trimmed = re.sub(r"\s+(?:inc|corp|corporation|ltd|limited|plc|co)\.?\s*$", "",
                     trimmed, flags=re.IGNORECASE).strip(" .,")
    if len(trimmed) <= limit:
        return trimmed or name[:limit]
    words = trimmed.split()
    out: List[str] = []
    for word in words:
        if len(" ".join(out + [word])) > limit:
            break
        out.append(word)
    return " ".join(out) or trimmed[:limit]


def needs_ticker(holding: Dict[str, Any]) -> bool:
    return any("ticker not supplied" in f for f in holding.get("flags", []))


# A US ticker is one to five letters, optionally with a class suffix (BRK.B).
# INDmoney's own instrument keys look like INDS02693 or INDI00012 and must never
# be mistaken for one - lookup_ind_keys searches INDIAN instruments, so asking it
# about "Alphabet" returns Mirae Nifty200Alpha30 and friends.
_US_TICKER_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")


def looks_like_us_ticker(value: Optional[str]) -> bool:
    if not value:
        return False
    candidate = str(value).strip().upper()
    if candidate.startswith(("INDS", "INDI", "INDM")):
        return False
    return bool(_US_TICKER_RE.match(candidate))


def _apply_ticker(holding: Dict[str, Any], ticker: str, how: str) -> bool:
    """Attach a ticker, refusing anything that is not shaped like one.

    Returns True when applied. A wrong ticker is worse than a derived label: the
    label is visibly provisional, whereas 'INDS02693' looks authoritative.
    """
    if not looks_like_us_ticker(ticker):
        log.warning("Refusing to label %r with %r - not a US ticker (%s).",
                    holding.get("name") or holding.get("symbol"), ticker, how)
        return False
    holding["flags"] = [f for f in holding.get("flags", [])
                        if "ticker not supplied" not in f]
    holding["flags"].append(f"ticker {ticker.upper()} {how}")
    holding["symbol"] = ticker.upper()
    return True


def build_code_index(details: Dict[str, dict]) -> Dict[str, str]:
    """Map INDmoney's internal instrument id to its ticker.

    ``entity_basic.mycroft_id`` in a quote reply equals ``investment_code`` in a
    holdings row - AAPL is 118186 in both. That makes ticker resolution an exact
    identifier join rather than a fuzzy name search.
    """
    index: Dict[str, str] = {}
    for symbol, row in details.items():
        flat = _flatten(row)
        for key in ("mycroft_id", "investment_code", "entity_id", "instrument_id"):
            value = _lookup(flat, key)
            if value not in (None, ""):
                index[str(value).strip()] = symbol.upper()
    return index


def resolve_by_code(holdings: List[Dict[str, Any]],
                    code_index: Dict[str, str]) -> Tuple[int, List[str]]:
    """Fill in tickers by joining investment_code to mycroft_id.

    Also verifies rows whose ticker came from elsewhere: a mismatch means the
    name lookup attached the wrong company, which is worth knowing loudly.
    """
    filled, warnings = 0, []
    for holding in holdings:
        code = str(holding.get("investment_code") or "").strip()
        if not code:
            continue
        ticker = code_index.get(code)
        if not ticker:
            continue
        if needs_ticker(holding):
            if _apply_ticker(holding, ticker, "matched on INDmoney's instrument id"):
                filled += 1
        elif holding.get("symbol") != ticker:
            warnings.append(
                f"{holding.get('symbol')} was matched by name but INDmoney's "
                f"instrument id {code} belongs to {ticker}; using {ticker}."
            )
            _apply_ticker(holding, ticker, "corrected via INDmoney's instrument id")
    return filled, warnings


def resolve_from_config(holdings: List[Dict[str, Any]],
                        mapping: Dict[str, str]) -> int:
    """Apply manual name-fragment -> ticker overrides from config.json.

    The escape hatch for instruments the API cannot resolve. SpaceX, for example,
    is a private-market holding that ``get_us_stocks_details`` does not know, so
    no automatic path will ever find its ticker.
    """
    if not mapping:
        return 0
    filled = 0
    for holding in holdings:
        if not needs_ticker(holding):
            continue
        haystack = f"{holding.get('name') or ''} {holding.get('symbol') or ''}".upper()
        for fragment, ticker in mapping.items():
            if fragment and str(fragment).upper() in haystack:
                if _apply_ticker(holding, str(ticker),
                                 "set by tracking.instrument_tickers"):
                    filled += 1
                break
    return filled


def resolve_by_name(holdings: List[Dict[str, Any]],
                    details: Dict[str, dict]) -> Tuple[int, List[str]]:
    """Fill in tickers by matching instrument names against the quote reply.

    A second, independent path to the same answer, using data already fetched.
    ``"Apple Inc. Common Stock"`` and ``entity_basic.name`` of ``"Apple Inc."``
    both reduce to ``"Apple"``, so the two join without another network call and
    without depending on ``mycroft_id`` being present.
    """
    index: Dict[str, str] = {}
    for symbol, row in details.items():
        flat = _flatten(row)
        for key in ("name", "display_name", "short_name"):
            value = _first_str(flat, [key])
            if value:
                index.setdefault(_canon(shorten_for_lookup(value)), symbol.upper())

    filled, warnings = 0, []
    for holding in holdings:
        if not needs_ticker(holding) or not holding.get("name"):
            continue
        key = _canon(shorten_for_lookup(holding["name"]))
        ticker = index.get(key)
        if not ticker:
            for cand_key, cand_ticker in index.items():
                if cand_key and len(cand_key) >= 4 and (
                        key.startswith(cand_key) or cand_key.startswith(key)):
                    ticker = cand_ticker
                    break
        if ticker and _apply_ticker(holding, ticker, "matched on the instrument name"):
            filled += 1
    return filled, warnings


def derive_usd_inr(holdings: List[Dict[str, Any]],
                   quotes: Dict[str, dict]) -> Tuple[Optional[float], str]:
    """Derive USD/INR from the data itself, with no external rate source.

    A US holding's ``unit_price`` is in rupees while the live quote for the same
    ticker is in dollars, so their ratio is the rate INDmoney applied. AAPL at
    29,476.19 against 308.91 implies 95.42. The median across every ticker with
    both figures is used, so one stale quote cannot skew it.
    """
    ratios: List[Tuple[str, float]] = []
    for holding in holdings:
        if holding.get("currency") != "INR" or holding.get("book") != BOOK_US:
            continue
        inr_price = holding.get("ltp")
        quote = quotes.get(str(holding.get("symbol", "")).upper())
        usd_price = (quote or {}).get("live_price_usd")
        if inr_price and usd_price and usd_price > 0:
            ratios.append((holding["symbol"], inr_price / usd_price))

    if not ratios:
        return None, "unavailable"

    values = sorted(r for _, r in ratios)
    mid = len(values) // 2
    median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    sources = ", ".join(f"{sym} {ratio:.2f}" for sym, ratio in ratios[:4])
    return median, (f"derived from INDmoney's own rupee prices vs its USD quotes "
                    f"({sources})")


def derive_label(name: str) -> str:
    """A short, stable label for an instrument that has no ticker.

    Deliberately not a guess at the real ticker - it is a display label. The
    accompanying flag says so, and lookup_ind_keys is tried first.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name.upper()) if w]
    keep = [w for w in words if w not in _LABEL_STOPWORDS] or words
    label = "".join(keep[:2])[:12]
    return label or re.sub(r"[^A-Z0-9]", "", name.upper())[:12] or "UNKNOWN"


def classify_book(item: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Which book does this INDmoney row belong to, and on what evidence?

    Returns ``(book, reason)`` where book is ``BOOK_US``, ``BOOK_IND`` or None.
    None means the row could not be classified and must be excluded rather than
    assumed - a wrong book puts an Indian holding into the US section, or hides a
    US holding entirely.
    """
    flat = _flatten(item)
    raw = _first_str(flat, ["asset_type", "assetType", "asset_class"])
    label = _first_str(flat, _IND_FIELDS["name"]) or "unnamed row"

    if raw:
        key = re.sub(r"[^A-Z0-9]", "_", raw.upper()).strip("_")
        book = _ASSET_TYPE_BOOK.get(key)
        if book:
            return book, f"asset_type {raw!r}"
        # An unknown asset_type is a data question, not something to guess at.
        return None, (f"unrecognised asset_type {raw!r} on {label!r}; add it to "
                      f"_ASSET_TYPE_BOOK in brokers.py")

    # No asset_type at all. Currency is weak but unambiguous evidence when USD.
    currency = (_first_str(flat, _IND_FIELDS["currency"]) or "").upper()
    if "USD" in currency:
        return BOOK_US, "no asset_type, but the row is priced in USD"
    if "INR" in currency:
        return BOOK_IND, "no asset_type, but the row is priced in INR"

    return None, (f"no asset_type and no currency on {label!r}, so the book cannot "
                  f"be determined")


def is_us_row(item: Dict[str, Any]) -> bool:
    """True only when the row is positively identified as a US holding."""
    return classify_book(item)[0] == BOOK_US


def normalise_indmoney_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    us_only: bool = True,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Normalise many rows, returning (holdings, diagnostics).

    Fail-closed: a row is kept for the US book only when it is positively
    identified as one. Rows belonging to another book are dropped silently (that
    is routine), but rows that cannot be classified at all are dropped *loudly*,
    because silence there is how a holding goes missing from the report.
    """
    out: List[Dict[str, Any]] = []
    problems: List[str] = []

    for index, row in enumerate(rows):
        book, reason = classify_book(row)
        name = _first_str(_flatten(row), _IND_FIELDS["name"]) or f"row {index}"

        if us_only:
            if book is None:
                problems.append(
                    f"INDmoney holding {name!r} was excluded because its book could "
                    f"not be determined: {reason}."
                )
                log.warning("Unclassifiable INDmoney row %d (%s): %s", index, name, reason)
                continue
            if book != BOOK_US:
                # Routine: Kite is authoritative for the Indian book.
                log.debug("Skipping non-US INDmoney row %s (%s)", name, reason)
                continue

        normalised = normalise_indmoney(row, book=book or BOOK_US)
        if normalised is None:
            keys = ", ".join(sorted(str(k) for k in row.keys())[:12])
            problems.append(
                f"INDmoney holding {name!r} could not be interpreted and was excluded "
                f"(fields present: {keys}). Run tools/probe_indmoney.py and update "
                f"_IND_FIELDS in brokers.py."
            )
            log.warning("Unparseable INDmoney row %d: %s", index, keys)
            continue

        normalised["book_reason"] = reason
        out.append(normalised)

    return out, problems


# ===========================================================================
# Provider objects
# ===========================================================================
class ProviderError(RuntimeError):
    pass


class AuthRequired(ProviderError):
    """Raised when a provider needs an interactive sign-in."""


class BrokerProvider:
    """One MCP server, held open for the life of the process."""

    name = "provider"
    book = BOOK_IND
    url = ""
    holdings_tool = "get_holdings"

    def __init__(self, session, *, url: str = ""):
        self.session = session
        if url:
            self.url = url

    async def call(self, tool: str, arguments: Optional[dict] = None) -> Any:
        result = await self.session.call_tool(tool, arguments=arguments or {})
        text = result.content[0].text if getattr(result, "content", None) else None
        if looks_like_auth_error(text):
            raise AuthRequired(f"{self.name}: {(text or '').strip()[:160]}")
        return parse_tool_payload(text) if text else None

    async def holdings(self) -> Tuple[List[Dict[str, Any]], List[str]]:
        raise NotImplementedError


class KiteProvider(BrokerProvider):
    name = "kite"
    book = BOOK_IND
    url = KITE_MCP_URL

    async def holdings(self) -> Tuple[List[Dict[str, Any]], List[str]]:
        payload = await self.call("get_holdings")
        rows = extract_rows(payload, hint_keys=("holdings", "net", "data"))
        if not rows:
            raise ProviderError("Kite returned no parseable holdings list.")
        out, problems = [], []
        for index, row in enumerate(rows):
            normalised = normalise_kite(row)
            if normalised is None or not normalised.get("current_native"):
                problems.append(f"Kite row {index} skipped: missing symbol, quantity or price.")
                continue
            out.append(normalised)
        return out, problems

    async def login_url(self) -> Optional[str]:
        result = await self.session.call_tool("login", arguments={})
        return result.content[0].text if getattr(result, "content", None) else None


class IndmoneyProvider(BrokerProvider):
    """US book via INDmoney. Read-only; INDmoney exposes no write capability.

    ``networth_holdings`` is the per-position tool. Its parameter name is not
    documented publicly, so several spellings are attempted before giving up.
    """

    name = "indmoney"
    book = BOOK_US
    url = INDMONEY_MCP_URL
    holdings_tool = "networth_holdings"

    ASSET_TYPE_VALUE = "US_STOCK"
    # Confirmed from the tool schema: networth_holdings takes asset_type (required).
    ASSET_TYPE_KEYS = ("asset_type", "assetType", "asset_class", "type", "category")
    # CONFIRMED by sweep on 2026-08-02: segments=["news"] adds a top-level "news"
    # key, and ["news","analyst"] adds "analyst_forecast" as well. ["NEWS"],
    # ["all"], ["overview","news"] and ["news","analyst_consensus"] are rejected.
    NEWS_SEGMENT_CANDIDATES = (["news", "analyst"], ["news"])

    async def holdings(self, *, resolve_tickers: bool = True
                       ) -> Tuple[List[Dict[str, Any]], List[str]]:
        payload, attempted = None, []
        for key in self.ASSET_TYPE_KEYS:
            try:
                payload = await self.call(self.holdings_tool, {key: self.ASSET_TYPE_VALUE})
            except AuthRequired:
                raise
            except Exception as exc:
                attempted.append(f"{key}={exc.__class__.__name__}")
                continue
            if payload:
                break
        if payload is None:
            try:
                payload = await self.call(self.holdings_tool, {})
            except AuthRequired:
                raise
            except Exception as exc:
                raise ProviderError(
                    f"{self.holdings_tool} rejected every argument spelling "
                    f"({', '.join(attempted) or 'none'}) and also the empty call: {exc}"
                ) from exc

        rows = extract_rows(payload, hint_keys=("holdings", "data", "rows", "positions"))
        if not rows:
            raise ProviderError(
                f"{self.holdings_tool} returned no recognisable list of positions. "
                f"Run tools/probe_indmoney.py to capture the real shape."
            )
        holdings, problems = normalise_indmoney_rows(rows, us_only=True)

        if resolve_tickers:
            await self._resolve_tickers(holdings, problems)
        return holdings, problems

    async def _resolve_tickers(self, holdings: List[Dict[str, Any]],
                               problems: List[str], *, enabled: bool = False) -> None:
        """Resolve instrument names to tickers via lookup_ind_keys.

        **Off by default, and for good reason.** ``lookup_ind_keys`` searches
        INDIAN instruments. Asked about "Space Exploration Technologies" it
        returns Space Incubatrics Technologies and Paras Defence; asked about
        "Alphabet" it returns Mirae Nifty200Alpha30 and NIFTY 50. The keys it
        hands back (``INDS02693``, ``INDI00012``) are not tickers at all, so a
        loose match here would stamp an Indian instrument key onto a US holding -
        worse than leaving the derived label, which at least looks provisional.

        It is kept because a future ``filter_type`` may make it useful, but every
        result must survive an exact-or-near name match AND the US ticker shape
        check in :func:`_apply_ticker`.

        Names are shortened and sent ONE PER CALL: the endpoint puts them in a
        query string and returned HTTP 414 for the full SpaceX name.
        """
        if not enabled:
            return
        pending = [h for h in holdings if needs_ticker(h) and h.get("name")]
        if not pending:
            return

        resolved: Dict[str, str] = {}
        for holding in pending:
            short = shorten_for_lookup(holding["name"])
            payload = None
            for args in ({"names": [short]}, {"names": short}):
                try:
                    payload = await self.call("lookup_ind_keys", args)
                except AuthRequired:
                    raise
                except Exception as exc:
                    log.info("lookup_ind_keys(%r) failed: %s", short, exc)
                    continue
                if payload and not (isinstance(payload, dict) and payload.get("error")):
                    break
                payload = None
            if payload:
                resolved.update(self._parse_lookup(payload))

        for holding in pending:
            key = _canon(holding["name"])
            short_key = _canon(shorten_for_lookup(holding["name"]))
            ticker = resolved.get(key) or resolved.get(short_key)
            if not ticker:
                # Near-exact only. Prefix matching previously accepted wildly
                # different companies that merely began with the same letters.
                best, best_ratio = None, 0.0
                for cand_name, cand_ticker in resolved.items():
                    ratio = difflib.SequenceMatcher(None, short_key, cand_name).ratio()
                    if ratio > best_ratio:
                        best, best_ratio = cand_ticker, ratio
                if best and best_ratio >= 0.90:
                    ticker = best
                elif best:
                    log.info("Best lookup match for %r was only %.0f%% similar; "
                             "ignoring it.", holding["name"], best_ratio * 100)
            if ticker:
                _apply_ticker(holding, ticker, "resolved from the instrument name")

        still = [h.get("name") or h["symbol"] for h in pending if needs_ticker(h)]
        if still:
            problems.append(
                "A ticker could not be resolved for " + ", ".join(still)
                + ". These are labelled from their instrument names, so news matching "
                  "may miss them; add an entry to tracking.keywords in config.json."
            )

    @staticmethod
    def _parse_lookup(payload: Any) -> Dict[str, str]:
        """Map canonical instrument name -> ticker from a lookup_ind_keys reply.

        The reply shape is undocumented, so both a dict keyed by name and a list
        of records are accepted.
        """
        out: Dict[str, str] = {}

        def record(name: Any, ticker: Any) -> None:
            if isinstance(name, str) and isinstance(ticker, str) and name and ticker:
                out[_canon(name)] = ticker.strip().upper()

        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(value, dict):
                    flat = _flatten(value)
                    ticker = _first_str(flat, ["ind_key", "symbol", "ticker"])
                    name = _first_str(flat, _IND_FIELDS["name"]) or key
                    record(name, ticker or key)
                    record(key, ticker or key)
                elif isinstance(value, str):
                    record(key, value)

        rows = extract_rows(payload, hint_keys=("results", "matches", "data", "keys"))
        for row in rows or []:
            flat = _flatten(row)
            record(_first_str(flat, _IND_FIELDS["name"]),
                   _first_str(flat, ["ind_key", "symbol", "ticker"]))
        return out

    async def us_details(self, symbols: Sequence[str], *, with_news: bool = True
                         ) -> Dict[str, dict]:
        """Live US quotes, and headlines when a working ``segments`` value exists.

        The reply is keyed BY SYMBOL at the top level - ``{"AAPL": {...}}`` - with
        the numbers under ``entity_stats`` and the identity under
        ``entity_basic``. It is not a list, which is why a generic list scan finds
        nothing here.
        """
        collected: Dict[str, dict] = {}
        symbols = [s.upper() for s in symbols]

        for start in range(0, len(symbols), 10):        # documented cap: 10 per call
            batch = symbols[start:start + 10]
            payload = await self._fetch_us_batch(batch, with_news=with_news)

            if payload is None:
                # One unrecognised ticker can fail the whole batch, and losing the
                # batch loses the mycroft_id that identifies a real holding - which
                # is how a US holding ends up labelled from its name and looks
                # missing. Retry one at a time so a bad symbol costs only itself.
                log.warning("Batch of %d failed; retrying %s individually.",
                            len(batch), ", ".join(batch))
                for symbol in batch:
                    single = await self._fetch_us_batch([symbol], with_news=with_news)
                    if single is None:
                        log.info("  %s is not recognised by get_us_stocks_details.", symbol)
                        continue
                    self._absorb(single, [symbol], collected)
                continue

            self._absorb(payload, batch, collected)

        missing = [s for s in symbols if s not in collected]
        if missing:
            log.info("No US quote for: %s", ", ".join(missing))
        return collected

    async def _fetch_us_batch(self, batch: Sequence[str], *, with_news: bool):
        """One get_us_stocks_details call, or None if every attempt failed."""
        payload = None
        if with_news:
            for segments in self.NEWS_SEGMENT_CANDIDATES:
                try:
                    payload = await self.call("get_us_stocks_details",
                                              {"symbols": list(batch), "segments": segments})
                except AuthRequired:
                    raise
                except Exception:
                    continue
                if payload and _has_news(payload):
                    return payload
                payload = payload or None

        if payload is None:
            try:
                payload = await self.call("get_us_stocks_details", {"symbols": list(batch)})
            except AuthRequired:
                raise
            except Exception as exc:
                log.debug("get_us_stocks_details failed for %s: %s", ", ".join(batch), exc)
                return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _absorb(payload: dict, batch: Sequence[str], collected: Dict[str, dict]) -> None:
        wanted = {s.upper() for s in batch}
        for key, value in payload.items():
            if isinstance(value, dict) and key.upper() in wanted:
                collected[key.upper()] = value

    async def watchlist(self) -> List[str]:
        """Tickers from the user's INDmoney watchlists.

        Shape is nested two levels: ``watchlists[].stocks[].ticker``.
        """
        payload = None
        for args in ({"type": "all"}, {}, {"type": "us"}):
            try:
                payload = await self.call("user_watchlist", args)
            except AuthRequired:
                raise
            except Exception as exc:
                log.info("user_watchlist rejected %s (%s)", args, exc)
                continue
            if payload:
                break
        if not payload:
            return []

        symbols: List[str] = []
        lists = extract_rows(payload, hint_keys=("watchlists", "watchlist", "data"))
        for entry in lists or []:
            stocks = entry.get("stocks") if isinstance(entry, dict) else None
            if isinstance(stocks, list):
                for stock in stocks:
                    if isinstance(stock, dict):
                        ticker = _first_str(_flatten(stock),
                                            ["ticker", "ind_key", "symbol"])
                        if ticker:
                            symbols.append(ticker.upper())
            elif isinstance(entry, dict):
                ticker = _first_str(_flatten(entry), ["ticker", "ind_key", "symbol"])
                if ticker:
                    symbols.append(ticker.upper())
        return sorted(set(symbols))


def _has_news(payload: Any) -> bool:
    """Does a get_us_stocks_details reply carry any headlines?"""
    if not isinstance(payload, dict):
        return False
    for value in payload.values():
        if not isinstance(value, dict):
            continue
        for key in _NEWS_KEYS:
            blob = value.get(key)
            if isinstance(blob, list) and blob:
                return True
            if isinstance(blob, dict) and blob:
                return True
        for inner in value.values():
            if isinstance(inner, dict):
                for key in _NEWS_KEYS:
                    if inner.get(key):
                        return True
    return False


# ===========================================================================
# US news extraction from get_us_stocks_details
# ===========================================================================
_NEWS_KEYS = ("news", "news_items", "articles", "headlines", "news_and_sentiment",
              "recent_news", "newsItems")
_TITLE_KEYS = ("title", "headline", "heading", "text", "summary", "description")
_LINK_KEYS = ("link", "url", "article_url", "source_url", "href")
_SOURCE_KEYS = ("source", "publisher", "provider", "source_name", "site")
_SENTIMENT_KEYS = ("sentiment", "sentiment_label", "sentiment_score", "score",
                   "tone", "polarity")

# INDmoney's sentiment scale is undocumented. Labels map cleanly; bare numbers are
# only trusted when they sit in a range we can interpret without guessing.
_SENTIMENT_LABELS = {
    "very positive": 9, "strongly positive": 9, "very bullish": 9,
    "positive": 7, "bullish": 7, "buy": 7,
    "neutral": 5, "mixed": 5, "hold": 5,
    "negative": 3, "bearish": 3, "sell": 3,
    "very negative": 2, "strongly negative": 2, "very bearish": 2,
}


def _sentiment_to_ten(value: Any) -> Tuple[Optional[float], str]:
    """Best-effort map of INDmoney sentiment onto our 1-10 scale.

    Returns (score, note). The note records the assumption made, so the report
    can disclose it instead of presenting a converted number as a fact.
    """
    if value is None:
        return None, ""
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _SENTIMENT_LABELS:
            return float(_SENTIMENT_LABELS[key]), f"label '{value.strip()}'"
        parsed = _num(value)
        if parsed is None:
            return None, ""
        value = parsed
    number = _num(value)
    if number is None:
        return None, ""
    if -1.0 <= number <= 1.0:
        return round((number + 1) * 4.5 + 1, 1), "assumed -1..+1 scale"
    if 0.0 <= number <= 10.0:
        return round(number, 1), "assumed 1..10 scale"
    if 0.0 <= number <= 100.0:
        return round(number / 10.0, 1), "assumed 0..100 scale"
    return None, ""


def extract_us_quotes(details: Dict[str, dict]) -> Dict[str, dict]:
    """Live USD quote and day change per ticker from get_us_stocks_details.

    Worth pulling because ``networth_holdings`` carries no day-change field for
    US rows. A percentage move is currency-agnostic, so a USD-derived day change
    can be attached to a rupee-denominated holding without conversion.
    """
    out: Dict[str, dict] = {}
    for symbol, row in details.items():
        flat = _flatten(row)
        entry = {
            "live_price_usd": _first(flat, ["live_price", "ltp", "last_price", "price"]),
            "day_pct": _first(flat, ["day_change_percentage", "day_change_percent"]),
            "day_change_usd": _first(flat, ["day_change"]),
            "prev_close_usd": _first(flat, ["prev_close"]),
            "week52_high": _first(flat, ["52week_high", "week52_high"]),
            "week52_low": _first(flat, ["52week_low", "week52_low"]),
            "name": _first_str(flat, ["name", "display_name", "short_name"]),
            "sector": _first_str(flat, ["sector"]),
            "last_updated": _first_str(flat, ["last_updated"]),
        }
        if any(v is not None for v in entry.values()):
            out[symbol.upper()] = entry
    return out


def extract_us_news(details: Dict[str, dict]) -> Dict[str, dict]:
    """Pull headlines and any broker sentiment out of get_us_stocks_details.

    Returns ``{SYMBOL: {"articles": [{title, source, link}], "sentiment": float|None,
    "sentiment_note": str}}``.
    """
    out: Dict[str, dict] = {}
    for symbol, row in details.items():
        articles: List[dict] = []
        news_blob: Any = None
        for key in _NEWS_KEYS:
            if key in row:
                news_blob = row[key]
                break
        if news_blob is None:
            # The real reply nests everything under entity_* containers, so look
            # one level down as well.
            for value in row.values():
                if not isinstance(value, dict):
                    continue
                for key in _NEWS_KEYS:
                    if value.get(key):
                        news_blob = value[key]
                        break
                if news_blob is not None:
                    break

        entries: List[Any] = []
        if isinstance(news_blob, list):
            entries = news_blob
        elif isinstance(news_blob, dict):
            inner = extract_rows(news_blob, hint_keys=_NEWS_KEYS)
            entries = inner or []

        for entry in entries:
            if isinstance(entry, str):
                articles.append({"title": entry, "source": "INDmoney", "link": ""})
                continue
            if not isinstance(entry, dict):
                continue
            flat = _flatten(entry)
            title = _first_str(flat, _TITLE_KEYS)
            if not title:
                continue
            articles.append({
                "title": title,
                "source": _first_str(flat, _SOURCE_KEYS) or "INDmoney",
                "link": _first_str(flat, _LINK_KEYS) or "",
            })

        sentiment, note = None, ""
        flat_row = _flatten(row)
        for key in _SENTIMENT_KEYS:
            if key in flat_row:
                sentiment, note = _sentiment_to_ten(flat_row[key])
                if sentiment is not None:
                    break

        if articles or sentiment is not None:
            out[symbol.upper()] = {"articles": articles,
                                   "sentiment": sentiment,
                                   "sentiment_note": note}
    return out
