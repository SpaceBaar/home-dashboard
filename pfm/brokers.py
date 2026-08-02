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

# Confirmed: US rows arrive with asset_type 'US_STOCK'. Indian rows arrive with
# asset_type 'STOCK' (not 'IND_STOCK'), which is what keeps them out of the US
# book - important, because INDmoney also mirrors the Zerodha holdings that Kite
# already reports, and counting them twice would inflate the portfolio.
_US_ASSET_HINTS = ("US_STOCK", "US_STOCKS", "USSTOCK", "USSTOCKS",
                   "US EQUITY", "US_EQUITY", "GLOBAL EQUITY", "GLOBAL_EQUITY")

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
    }


_LABEL_STOPWORDS = {"THE", "LTD", "LIMITED", "INC", "CORP", "CORPORATION", "PLC",
                    "CO", "COMPANY", "CLASS", "COMMON", "STOCK", "SHARES", "SHARE",
                    "HOLDINGS", "GROUP", "TECHNOLOGIES", "TECHNOLOGY", "AND", "OF",
                    "A", "B", "C", "ETF", "FUND", "TRUST", "PLC."}


def derive_label(name: str) -> str:
    """A short, stable label for an instrument that has no ticker.

    Deliberately not a guess at the real ticker - it is a display label. The
    accompanying flag says so, and lookup_ind_keys is tried first.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name.upper()) if w]
    keep = [w for w in words if w not in _LABEL_STOPWORDS] or words
    label = "".join(keep[:2])[:12]
    return label or re.sub(r"[^A-Z0-9]", "", name.upper())[:12] or "UNKNOWN"


def is_us_row(item: Dict[str, Any]) -> bool:
    """Heuristic: does this INDmoney row belong to the US book?"""
    flat = _flatten(item)
    asset = (_first_str(flat, _IND_FIELDS["asset_class"]) or "").upper().replace("-", "_")
    if any(hint in asset for hint in _US_ASSET_HINTS):
        return True
    currency = (_first_str(flat, _IND_FIELDS["currency"]) or "").upper()
    return "USD" in currency


def normalise_indmoney_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    us_only: bool = True,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Normalise many rows, returning (holdings, diagnostics)."""
    out: List[Dict[str, Any]] = []
    problems: List[str] = []
    for index, row in enumerate(rows):
        if us_only and not is_us_row(row) and _first_str(_flatten(row), _IND_FIELDS["asset_class"]):
            continue
        normalised = normalise_indmoney(row)
        if normalised is None:
            keys = ", ".join(sorted(str(k) for k in row.keys())[:12])
            problems.append(
                f"INDmoney row {index} could not be interpreted and was excluded "
                f"(fields present: {keys}). Run tools/probe_indmoney.py and update "
                f"_IND_FIELDS in brokers.py."
            )
            log.warning("Unparseable INDmoney row %d: %s", index, keys)
            continue
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
    # get_us_stocks_details takes symbols + segments. The valid segment tokens are
    # not documented; these are tried in order and a rejection is not fatal.
    NEWS_SEGMENT_CANDIDATES = (
        ["news", "analyst"], ["news"], ["news_sentiment"], ["NEWS"], ["all"],
    )

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
                               problems: List[str]) -> None:
        """Turn instrument names into real tickers via lookup_ind_keys.

        networth_holdings supplies only a long name ("Space Exploration
        Technologies Corp. Class A Common Stock") and an internal code, so
        without this step a holding would be labelled with a derived stand-in and
        would not match any news keyword.
        """
        pending = [h for h in holdings
                   if h.get("name") and any("ticker not supplied" in f
                                            for f in h.get("flags", []))]
        if not pending:
            return

        names = [h["name"] for h in pending]
        payload = None
        for args in ({"names": names}, {"names": names, "filter_type": "US_STOCK"}):
            try:
                payload = await self.call("lookup_ind_keys", args)
            except AuthRequired:
                raise
            except Exception as exc:
                log.info("lookup_ind_keys rejected %s (%s)", list(args), exc)
                continue
            if payload:
                break
        if not payload:
            problems.append(
                "Tickers for the US holdings could not be resolved via "
                "lookup_ind_keys, so they are labelled from their instrument names. "
                "News matching for those rows will be unreliable."
            )
            return

        resolved = self._parse_lookup(payload)
        for holding in pending:
            key = _canon(holding["name"])
            ticker = resolved.get(key)
            if not ticker:
                # Try a looser match on the leading words of the name.
                for cand_name, cand_ticker in resolved.items():
                    if cand_name and (cand_name in key or key.startswith(cand_name[:12])):
                        ticker = cand_ticker
                        break
            if ticker:
                holding["flags"] = [f for f in holding["flags"]
                                    if "ticker not supplied" not in f]
                holding["flags"].append(f"ticker {ticker} resolved from the instrument name")
                holding["symbol"] = ticker.upper()

        still = [h["symbol"] for h in pending
                 if any("ticker not supplied" in f for f in h.get("flags", []))]
        if still:
            problems.append(
                "No ticker could be resolved for " + ", ".join(still)
                + "; these are labelled from their instrument names, so news "
                  "matching may miss them. Add an entry to tracking.keywords."
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
            payload = None

            if with_news:
                for segments in self.NEWS_SEGMENT_CANDIDATES:
                    try:
                        payload = await self.call("get_us_stocks_details",
                                                  {"symbols": batch, "segments": segments})
                    except AuthRequired:
                        raise
                    except Exception:
                        continue
                    if payload and _has_news(payload):
                        log.info("get_us_stocks_details returned news with segments=%s",
                                 segments)
                        break
                    payload = payload or None

            if payload is None or not _has_news(payload):
                try:
                    baseline = await self.call("get_us_stocks_details", {"symbols": batch})
                    payload = payload or baseline
                except AuthRequired:
                    raise
                except Exception as exc:
                    log.warning("get_us_stocks_details failed for %s: %s",
                                ", ".join(batch), exc)
                    continue

            if not isinstance(payload, dict):
                continue
            for key, value in payload.items():
                if isinstance(value, dict) and key.upper() in set(batch):
                    collected[key.upper()] = value
        return collected

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
