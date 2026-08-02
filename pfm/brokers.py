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
# Candidate field names, most specific first. Tighten these once
# tools/probe_indmoney.py has shown what the server actually sends.
_IND_FIELDS = {
    "symbol": ["symbol", "ticker", "tradingsymbol", "trading_symbol", "scrip",
               "instrument", "code", "isin_symbol"],
    "name": ["name", "company_name", "instrument_name", "display_name", "scheme_name"],
    "quantity": ["quantity", "units", "qty", "shares", "holding_units", "no_of_units"],
    "avg_price": ["average_price", "avg_price", "buy_price", "avg_buy_price",
                  "average_buy_price", "cost_price", "avg_cost", "purchase_price"],
    "ltp": ["last_price", "unit_price", "ltp", "current_price", "price",
            "market_price", "nav", "close_price"],
    "invested": ["invested_amount", "invested", "investment", "invested_value",
                 "total_invested", "cost_value", "buy_value", "amount_invested"],
    "current": ["current_value", "market_value", "present_value", "value",
                "current_amount", "total_value", "holding_value"],
    "pnl": ["pnl", "profit_loss", "gain_loss", "unrealised_pnl", "unrealized_pnl",
            "returns", "total_returns", "absolute_returns", "gain"],
    "pnl_pct": ["pnl_percentage", "pnl_percent", "returns_percentage",
                "return_percent", "gain_percentage", "absolute_return_percentage"],
    "day_pct": ["day_change_percentage", "day_change_percent", "change_percent",
                "percent_change", "day_change_pct", "todays_change_percent"],
    "currency": ["currency", "ccy", "currency_code", "trade_currency"],
    "asset_class": ["asset_class", "asset_type", "assetType", "category", "type"],
    "broker": ["broker", "broker_name", "source", "platform"],
    "xirr": ["xirr", "XIRR", "annualised_return", "annualized_return"],
}

# US_STOCK is INDmoney's own asset-type token; the rest are defensive.
_US_ASSET_HINTS = ("US_STOCK", "US_STOCKS", "USSTOCK", "US EQUITY", "US_EQUITY",
                   "USSTOCKS", "GLOBAL_STOCK", "US")


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
    default_currency: str = "USD",
) -> Optional[Dict[str, Any]]:
    """Normalise one INDmoney holding row.

    Returns None when the row cannot be understood well enough to be trusted;
    the caller logs it rather than letting a zero into the totals.
    """
    flat = _flatten(item)

    symbol = _first_str(flat, _IND_FIELDS["symbol"])
    name = _first_str(flat, _IND_FIELDS["name"])
    if not symbol and name:
        # Fall back to a ticker embedded in the display name, e.g. "Apple Inc (AAPL)".
        bracketed = re.search(r"\(([A-Z]{1,6})\)\s*$", name)
        symbol = bracketed.group(1) if bracketed else None
    if not symbol:
        return None

    flags: List[str] = []
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

    # INDmoney documents that broker-imported rows may omit the cost basis.
    if invested is None or invested <= 0:
        invested = None
        avg = None
        flags.append("invested amount not shared by the broker; "
                     "return figures suppressed for this holding")

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

    ASSET_TYPE_KEYS = ("asset_type", "assetType", "asset_class", "type", "category")
    ASSET_TYPE_VALUE = "US_STOCK"

    async def holdings(self) -> Tuple[List[Dict[str, Any]], List[str]]:
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
            # Some servers return everything and expect the client to filter.
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
        return normalise_indmoney_rows(rows, us_only=True)

    async def us_details(self, symbols: Sequence[str]) -> Dict[str, dict]:
        """Live data plus news and sentiment for up to 10 tickers per call."""
        collected: Dict[str, dict] = {}
        symbols = [s.upper() for s in symbols]
        for start in range(0, len(symbols), 10):        # documented cap: 10 per call
            batch = symbols[start:start + 10]
            payload = None
            for key in ("symbols", "tickers", "instruments", "symbol"):
                arg: Any = batch if key != "symbol" else ",".join(batch)
                try:
                    payload = await self.call("get_us_stocks_details",
                                              {key: arg, "include_news": True,
                                               "include_analyst_consensus": True})
                except AuthRequired:
                    raise
                except Exception:
                    try:
                        payload = await self.call("get_us_stocks_details", {key: arg})
                    except Exception:
                        continue
                if payload:
                    break
            if not payload:
                log.warning("get_us_stocks_details returned nothing for %s", ", ".join(batch))
                continue
            rows = extract_rows(payload, hint_keys=("stocks", "data", "details", "results"))
            for row in rows or []:
                flat = _flatten(row)
                symbol = (_first_str(flat, _IND_FIELDS["symbol"]) or "").upper()
                if symbol:
                    collected[symbol] = row
        return collected

    async def watchlist(self) -> List[str]:
        try:
            payload = await self.call("user_watchlist", {})
        except AuthRequired:
            raise
        except Exception as exc:
            log.info("user_watchlist unavailable (%s); using config.json watchlist.", exc)
            return []
        rows = extract_rows(payload, hint_keys=("watchlist", "instruments", "data"))
        symbols: List[str] = []
        for row in rows or []:
            flat = _flatten(row)
            symbol = _first_str(flat, _IND_FIELDS["symbol"])
            if symbol:
                symbols.append(symbol.upper())
        return symbols


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
            nested = _flatten(row)
            for key in _NEWS_KEYS:
                if key in nested:
                    news_blob = nested[key]
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
