#!/usr/bin/env python3
"""Discover the real response shapes of the INDmoney MCP server.

Why this exists
---------------
INDmoney publishes an MCP server at https://mcp.indmoney.com/mcp, but not a
field-level schema for what its tools return. Writing a parser against a guess
is how you end up with a report full of confident, wrong numbers - exactly the
failure this project already fixed once. So: capture the real shapes first,
then write the parser against them.

Authentication
--------------
The server uses OAuth 2.1 with PKCE. ``mcp-remote`` handles the flow and caches
tokens under ~/.mcp-auth. On a headless Pi it prints a URL - open it on your
phone or laptop, complete OTP + MPIN on INDmoney's own page, approve the consent
screen, and this script continues. Your credentials go to INDmoney directly;
nothing in this repository ever sees them.

Usage
-----
    python tools/probe_indmoney.py                 # list tools, then probe all
    python tools/probe_indmoney.py --list-only     # just the tool inventory
    python tools/probe_indmoney.py --tool networth_holdings --args '{"asset_type":"US_STOCK"}'
    python tools/probe_indmoney.py --no-redact     # keep values as-is (careful)

Output goes to pfm/cache/indmoney_probe_<timestamp>.json and a readable summary
is printed. Numbers are preserved; obvious identifiers are masked by default so
the file is safe to paste into a chat or an issue.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pfm_config import CACHE_DIR, load_config, setup_logging  # noqa: E402

log = logging.getLogger("pfm.probe")

INDMONEY_MCP_URL = "https://mcp.indmoney.com/mcp"

# Tools this pipeline could plausibly use. Everything else INDmoney exposes
# (option chains, greeks, mutual-fund research) is out of scope here.
PROBES: List[Dict[str, Any]] = [
    {"tool": "networth_snapshot", "args": {},
     "why": "cross-asset totals; confirms values arrive in INR"},
    {"tool": "networth_holdings", "args": {"asset_type": "US_STOCK"},
     "why": "THE important one - per-position US rows"},
    {"tool": "networth_holdings", "args": {"asset_type": "IND_STOCK"},
     "why": "confirm rows come back as asset_type STOCK so they stay out of the US book"},
    {"tool": "networth_allocation_breakdown",
     "args": {"asset_type": "US_STOCK", "breakdown_by": "sector"},
     "why": "asset-class US subtotal; breakdown_by is required"},
    {"tool": "user_watchlist", "args": {"type": "all"},
     "why": "watchlist tickers; type is required"},
    {"tool": "get_us_stocks_details", "args": {"symbols": ["AAPL", "TSLA"]},
     "why": "baseline US quotes (confirmed: no news in the baseline reply)"},
    {"tool": "lookup_ind_keys",
     "args": {"names": ["Space Exploration Technologies Corp. Class A Common Stock",
                        "Apple Inc."]},
     "why": "resolve instrument names to tickers - networth_holdings has no ticker field"},
]

# The `segments` tokens that turn on news and analyst data are not documented.
# This sweep finds a working one; whichever returns headlines is the answer.
SEGMENT_CANDIDATES = [
    ["news"], ["news", "analyst"], ["news_sentiment"], ["NEWS"],
    ["analyst"], ["all"], ["news", "analyst_consensus"], ["overview", "news"],
]

# Field names worth masking. Values are replaced, keys are kept, and numbers are
# always preserved - the point of the probe is to learn structure and units.
_SENSITIVE_KEY_RE = re.compile(
    r"(pan|aadha|email|mobile|phone|client_?id|user_?id|account|folio|dp_?id|"
    r"demat|bank|ifsc|address|dob|name_?on|customer)",
    re.IGNORECASE,
)


def redact(obj: Any, *, enabled: bool = True, depth: int = 0) -> Any:
    """Mask identifier-ish string values, keep every number and key intact."""
    if not enabled or depth > 12:
        return obj
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if _SENSITIVE_KEY_RE.search(str(key)) and isinstance(value, str) and value:
                out[key] = f"<redacted:{len(value)} chars>"
            else:
                out[key] = redact(value, enabled=enabled, depth=depth + 1)
        return out
    if isinstance(obj, list):
        return [redact(v, enabled=enabled, depth=depth + 1) for v in obj]
    return obj


def describe(obj: Any, *, prefix: str = "", depth: int = 0, max_depth: int = 5,
             max_items: int = 8) -> List[str]:
    """Render a compact type/shape outline - the part you actually need to read.

    Lists show up to ``max_items`` elements, not just the first. Only printing
    element [0] previously hid the very rows that were being misclassified.
    """
    lines: List[str] = []
    if depth > max_depth:
        return [f"{prefix}: ..."]

    if isinstance(obj, dict):
        for key, value in list(obj.items())[:40]:
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, (dict, list)):
                lines.extend(describe(value, prefix=path, depth=depth + 1,
                                      max_depth=max_depth, max_items=max_items))
            else:
                kind = type(value).__name__
                shown = repr(value)
                if len(shown) > 60:
                    shown = shown[:57] + "..."
                lines.append(f"{path}: {kind} = {shown}")
    elif isinstance(obj, list):
        lines.append(f"{prefix}: list[{len(obj)}]")
        for index, item in enumerate(obj[:max_items]):
            lines.extend(describe(item, prefix=f"{prefix}[{index}]", depth=depth + 1,
                                  max_depth=max_depth, max_items=max_items))
        if len(obj) > max_items:
            lines.append(f"{prefix}: ... and {len(obj) - max_items} more")
    else:
        lines.append(f"{prefix}: {type(obj).__name__} = {obj!r}")
    return lines


def classification_table(payload: Any) -> List[str]:
    """Show, per holding row, which book it lands in and on what evidence.

    This is the answer to "why is X in the wrong section" or "where did Y go".
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import brokers
    except ImportError:
        return []

    rows = brokers.extract_rows(payload, hint_keys=("holdings",))
    if not rows:
        return []

    lines = ["", f"  --- book classification for {len(rows)} row(s) ---",
             f"  {'instrument':<40} {'asset_type':<14} {'l2':<14} {'broker':<10} book"]
    kept = 0
    for row in rows:
        flat = brokers._flatten(row)
        name = (brokers._first_str(flat, brokers._IND_FIELDS["name"]) or "?")[:38]
        asset = (brokers._first_str(flat, ["asset_type", "assetType"]) or "-")[:12]
        l2 = (brokers._first_str(flat, ["assetclass_l2"]) or "-")[:12]
        broker_name = (brokers._first_str(flat, ["broker"]) or "-")[:8]
        book, reason = brokers.classify_book(row)
        if book == brokers.BOOK_US:
            kept += 1
        marker = {"US": "US  <- kept", "IND": "IND (Kite owns it)"}.get(book, "?? EXCLUDED")
        lines.append(f"  {name:<40} {asset:<14} {l2:<14} {broker_name:<10} {marker}")
        if book is None:
            lines.append(f"      reason: {reason}")

    normalised, problems = brokers.normalise_indmoney_rows(rows)
    lines.append(f"  => {kept} row(s) classified US, {len(normalised)} survived "
                 f"normalisation")
    for problem in problems:
        lines.append(f"     ! {problem}")
    if normalised:
        lines.append("  symbols after normalisation (before ticker resolution):")
        for holding in normalised:
            flag = " [derived label]" if brokers.needs_ticker(holding) else ""
            lines.append(f"     {holding['symbol']:<16} {holding.get('name', '')[:44]}{flag}")
    return lines


def parse_tool_text(result: Any) -> Any:
    """MCP tool results wrap the payload in content blocks; unwrap to JSON."""
    content = getattr(result, "content", None) or []
    texts = [getattr(block, "text", None) for block in content]
    texts = [t for t in texts if t]
    if not texts:
        return {"_note": "tool returned no text content",
                "_repr": repr(result)[:500]}
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        # Some servers return prose or fenced JSON.
        fenced = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", joined)
        if fenced:
            try:
                return json.loads(fenced.group(1))
            except json.JSONDecodeError:
                pass
        return {"_raw_text": joined[:4000]}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Probe the INDmoney MCP server")
    parser.add_argument("--url", default=None, help=f"default {INDMONEY_MCP_URL}")
    parser.add_argument("--list-only", action="store_true", help="only list tools")
    parser.add_argument("--tool", help="probe a single tool by name")
    parser.add_argument("--args", help="JSON arguments for --tool")
    parser.add_argument("--no-redact", action="store_true",
                        help="do not mask identifier fields")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    cfg = load_config()
    url = args.url or os.getenv("INDMONEY_MCP_URL") or INDMONEY_MCP_URL
    do_redact = not args.no_redact

    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        print("The 'mcp' package is required: pip install mcp")
        return 1

    print(f"\nConnecting to {url} via mcp-remote...")
    print("If this is your first connection, a sign-in URL will be printed below.")
    print("Open it, complete OTP + MPIN on INDmoney, and approve the consent screen.\n")

    server_params = StdioServerParameters(
        command=cfg.npx_path,
        args=["-y", "mcp-remote", url],
        env=dict(os.environ),
    )

    captured: Dict[str, Any] = {
        "probed_at": datetime.now().isoformat(timespec="seconds"),
        "url": url,
        "redacted": do_redact,
        "tools": [],
        "results": {},
    }

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=300)
            print("Connected.\n")

            listed = await session.list_tools()
            print("=" * 72)
            print(f"TOOLS ({len(listed.tools)})")
            print("=" * 72)
            for tool in listed.tools:
                schema = getattr(tool, "inputSchema", None) or {}
                props = list((schema.get("properties") or {}).keys())
                required = schema.get("required") or []
                captured["tools"].append({
                    "name": tool.name,
                    "description": (tool.description or "")[:500],
                    "input_schema": schema,
                })
                print(f"\n{tool.name}")
                print(f"  {(tool.description or '').strip()[:200]}")
                if props:
                    marked = [f"{p}*" if p in required else p for p in props]
                    print(f"  params: {', '.join(marked)}   (* = required)")

            if args.list_only:
                probes: List[Dict[str, Any]] = []
            elif args.tool:
                probes = [{"tool": args.tool,
                           "args": json.loads(args.args) if args.args else {},
                           "why": "requested on the command line"}]
            else:
                available = {t.name for t in listed.tools}
                probes = [p for p in PROBES if p["tool"] in available]
                skipped = [p["tool"] for p in PROBES if p["tool"] not in available]
                if skipped:
                    print(f"\nNot offered by the server, skipping: {', '.join(skipped)}")

            for probe in probes:
                name, tool_args = probe["tool"], probe["args"]
                key = f"{name}({json.dumps(tool_args, sort_keys=True)})"
                print("\n" + "=" * 72)
                print(f"CALL {key}")
                print(f"     {probe['why']}")
                print("=" * 72)
                try:
                    result = await asyncio.wait_for(
                        session.call_tool(name, arguments=tool_args), timeout=180)
                    payload = parse_tool_text(result)
                except Exception as exc:
                    print(f"  FAILED: {exc}")
                    captured["results"][key] = {"error": str(exc)}
                    continue

                safe = redact(payload, enabled=do_redact)
                captured["results"][key] = safe
                for line in describe(safe)[:160]:
                    print(f"  {line}")
                if name == "networth_holdings":
                    for line in classification_table(payload):
                        print(line)

            # --- sweep for the segments value that enables news ------------
            available = {t.name for t in listed.tools}
            if not args.tool and not args.list_only and "get_us_stocks_details" in available:
                print("\n" + "=" * 72)
                print("SWEEP get_us_stocks_details segments= (looking for news)")
                print("=" * 72)
                winners = []
                for segments in SEGMENT_CANDIDATES:
                    label = json.dumps(segments)
                    try:
                        result = await asyncio.wait_for(
                            session.call_tool("get_us_stocks_details",
                                              arguments={"symbols": ["AAPL"],
                                                         "segments": segments}),
                            timeout=120)
                        payload = parse_tool_text(result)
                    except Exception as exc:
                        print(f"  segments={label:<28} rejected: {str(exc)[:90]}")
                        continue

                    text = json.dumps(payload).lower()
                    has_news = any(k in text for k in
                                   ('"news', '"headline', '"article', 'sentiment'))
                    keys = sorted((payload.get("AAPL") or {}).keys()) \
                        if isinstance(payload, dict) else []
                    print(f"  segments={label:<28} "
                          f"{'NEWS FOUND' if has_news else 'no news'}  keys={keys}")
                    captured["results"][f"segments={label}"] = redact(payload, enabled=do_redact)
                    if has_news:
                        winners.append(segments)

                if winners:
                    print(f"\n  Working segments values: {winners}")
                    print("  Put the first of these at the head of "
                          "IndmoneyProvider.NEWS_SEGMENT_CANDIDATES in brokers.py.")
                else:
                    print("\n  No segments value produced news. US headlines will keep "
                          "coming from the RSS feeds; the pipeline handles that.")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"indmoney_probe_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(captured, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 72)
    print(f"Saved to {out}")
    print("=" * 72)
    print("""
Already confirmed from the 2026-08-02 capture, and encoded in brokers.py:

  * networth_holdings US rows are in RUPEES, not dollars. INDmoney has already
    converted them (units x unit_price == market_value, and the implied average
    only makes sense as INR). get_us_stocks_details, by contrast, quotes in USD.
  * There is no ticker field - only `investment` (long name) and
    `investment_code`. Tickers are resolved via lookup_ind_keys.
  * An unknown cost basis arrives as the string "unknown", and total_pnl is then
    filled with the market value while pnl_per is 0. Both are placeholders and
    are discarded.
  * Indian rows come back with asset_type 'STOCK', which is what keeps INDmoney's
    mirror of your Zerodha holdings out of the US book.
  * get_us_stocks_details is keyed by symbol at the top level, with numbers under
    entity_stats and identity under entity_basic.

Worth re-checking if something looks wrong:

  1. Whether a `segments` value now returns news (see the sweep above).
  2. Whether lookup_ind_keys resolved every holding name to a ticker.
  3. Whether any new asset_type values need adding to _US_ASSET_HINTS.

Paste the file contents back if the parser needs adjusting.
""")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130)
