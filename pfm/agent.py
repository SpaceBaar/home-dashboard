#!/usr/bin/env python3
"""Personal finance agent — orchestrator.

Pipeline
--------
    Kite MCP (get_holdings)                    -> raw broker payload
    portfolio.build_fact_sheet                 -> every number, computed once
    news.collect_articles                      -> deduplicated, attributed news
    news.score_all  (one LLM call per stock)   -> one aggregate score per stock
    report.build_narrative (validated)         -> prose that cannot contradict the data
    report.render_report / write_report        -> markdown on disk
    notify.Telegram                            -> summary push

Everything numeric is deterministic. The local model is used for exactly two
things: rating the news for one stock at a time, and writing the commentary,
which is machine-checked against the computed figures before it is published.

Usage
-----
    python agent.py                 interactive; offers an on-demand run
    python agent.py --daemon        long-running service (systemd)
    python agent.py --once          single analysis run, then exit
    python agent.py --dry-run       run offline against fixture holdings
    python agent.py --preflight     check the LLM runtime and config, then exit
    python agent.py --no-llm        deterministic report only, no model calls
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import schedule

import news as news_mod
import report as report_mod
from llm import LLMClient, LLMUnavailable
from notify import Telegram
from pfm_config import BASE_DIR, CACHE_DIR, REPORT_DIR, STATE_DIR, load_config, setup_logging
from portfolio import build_fact_sheet, extract_holdings_json

log = logging.getLogger("pfm.agent")

CFG = None            # populated in main()
TG: Optional[Telegram] = None
LLM: Optional[LLMClient] = None
_session = None       # mcp.ClientSession, imported lazily so --dry-run needs no MCP install
# Created lazily inside the running loop; a module-level asyncio.Lock() binds to
# the wrong event loop on Python 3.9 and then fails at the first await.
_run_lock: Optional[asyncio.Lock] = None
_OFFSET_FILE = STATE_DIR / "telegram_offset.json"


def _get_run_lock() -> asyncio.Lock:
    global _run_lock
    if _run_lock is None:
        _run_lock = asyncio.Lock()
    return _run_lock


# ---------------------------------------------------------------------------
# Kite session handling
# ---------------------------------------------------------------------------
async def fetch_holdings_text() -> Optional[str]:
    if _session is None:
        return None
    result = await _session.call_tool("get_holdings", arguments={})
    return result.content[0].text if result.content else None


async def probe_session() -> Optional[str]:
    """Return holdings text if the Kite token is still valid, else None.

    Kite MCP answers unauthenticated calls with a plain-text message such as
    "Please log in first using the login tool" instead of raising, so the only
    reliable liveness test is whether the response parses as holdings data.
    """
    try:
        text = await fetch_holdings_text()
    except Exception as exc:
        log.info("get_holdings probe failed (%s); a fresh login is needed.", exc)
        return None
    if text and extract_holdings_json(text) is not None:
        return text
    log.info("Kite replied with non-holdings content; a fresh login is needed.")
    return None


async def send_login_link() -> None:
    if _session is None:
        log.error("No MCP session; cannot generate a login link.")
        return
    try:
        result = await _session.call_tool("login", arguments={})
        url = result.content[0].text
    except Exception as exc:
        log.error("Could not generate the login URL: %s", exc)
        if TG:
            TG.alert(f"Could not generate the Zerodha login URL: {exc}")
        return
    if TG:
        TG.send("Good morning. Zerodha login link for today's analysis:\n\n" + url)
    log.info("Login link sent.")


# ---------------------------------------------------------------------------
# The analysis run
# ---------------------------------------------------------------------------
async def run_analysis(holdings_text: Optional[str] = None, *, use_llm: bool = True) -> Optional[Path]:
    lock = _get_run_lock()
    if lock.locked():
        log.warning("An analysis run is already in progress; skipping this trigger.")
        return None

    async with lock:
        started = datetime.now()
        log.info("=== Analysis run starting ===")

        if holdings_text is None:
            holdings_text = await probe_session()
        if holdings_text is None:
            log.error("No valid Kite session; aborting.")
            if TG:
                TG.alert("Nightly analysis aborted: the Zerodha session has expired. "
                         "Use the morning login link before the 23:00 run.")
            return None

        holdings_raw = extract_holdings_json(holdings_text)
        if not holdings_raw:
            log.error("Could not parse holdings. First 400 chars: %r", holdings_text[:400])
            if TG:
                TG.alert("Nightly analysis aborted: the holdings payload could not be parsed.")
            return None

        # 1. Deterministic portfolio mathematics.
        fact_sheet = build_fact_sheet(
            holdings_raw,
            mismatch_tolerance_pct=float(CFG.portfolio.get("pnl_mismatch_tolerance_pct", 1.0)),
        )
        held = {h.symbol for h in fact_sheet.holdings}
        log.info("Parsed %d holdings. Value Rs %s, P&L Rs %s (%+.1f%%).",
                 len(fact_sheet.holdings), f"{fact_sheet.total_current:,.0f}",
                 f"{fact_sheet.total_pnl:+,.0f}", fact_sheet.total_pnl_pct)

        # 2. News universe = what you hold, plus the labelled watchlist.
        universe: List[str] = sorted(held | set(CFG.watchlist))
        keyword_map = {s: CFG.keywords_for(s) for s in universe}
        keyword_map = {s: kws for s, kws in keyword_map.items() if kws}
        skipped = [s for s in universe if s not in keyword_map]
        if skipped:
            log.info("No usable news keywords for %s; add them to config.json "
                     "tracking.keywords to include them.", ", ".join(skipped))

        feed_stats: dict = {}
        grouped = news_mod.collect_articles(
            CFG.news_sources, list(keyword_map), keyword_map, CFG.exclude_map,
            timeout=float(CFG.news["feed_timeout_seconds"]),
            attempts=int(CFG.news["feed_attempts"]),
            similarity_threshold=float(CFG.news["duplicate_similarity_threshold"]),
            max_per_stock=int(CFG.news["max_articles_per_stock"]),
            stats=feed_stats,
        )

        # 3. One LLM call per stock, all of that stock's headlines together.
        scores = []
        if grouped and use_llm and LLM is not None:
            scores = await news_mod.score_all(
                grouped, LLM,
                max_headline_chars=int(CFG.news["max_headline_chars"]),
                held=held,
            )
        elif grouped:
            log.info("LLM disabled; news will be listed without ratings.")
            from llm import StockScore
            scores = [StockScore(sym, None, "Scoring disabled for this run.",
                                 "unscored", "llm-disabled", len(arts))
                      for sym, arts in grouped.items()]

        news_section = news_mod.render_news_section(grouped, scores, held=held)

        # 4. Commentary — validated against the computed figures.
        narrative, provenance, rejected = await report_mod.build_narrative(
            LLM if use_llm else None, fact_sheet, scores, held, CFG.keyword_map,
            enabled=bool(CFG.narrative.get("enabled", True)) and use_llm,
            max_attempts=int(CFG.narrative.get("max_attempts", 2)),
        )

        # 5. Render and persist.
        content = report_mod.render_report(
            fact_sheet, news_section, narrative, provenance,
            scores=scores,
            model=(LLM.model if LLM else "none"),
            rejected=rejected,
            feed_stats=feed_stats,
        )
        path = report_mod.write_report(content, REPORT_DIR)

        # Structured sidecar for the web view (pfm/web.py).
        report_mod.write_payload(
            report_mod.build_payload(
                fact_sheet, grouped, scores, narrative, provenance,
                held=held, model=(LLM.model if LLM else "none"),
                rejected=rejected, feed_stats=feed_stats,
            ),
            REPORT_DIR,
        )

        if TG:
            TG.send(report_mod.telegram_summary(fact_sheet, scores, path))

        elapsed = (datetime.now() - started).total_seconds()
        rated = sum(1 for s in scores if s.score is not None)
        log.info("=== Analysis run complete in %.0fs — %d/%d stocks rated, provenance: %s ===",
                 elapsed, rated, len(scores), provenance)
        return path


# ---------------------------------------------------------------------------
# Expense listener
# ---------------------------------------------------------------------------
def _load_offset() -> int:
    try:
        return int(json.loads(_OFFSET_FILE.read_text())["offset"])
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    try:
        _OFFSET_FILE.write_text(json.dumps({"offset": offset}))
    except OSError as exc:
        log.warning("Could not persist the Telegram offset: %s", exc)


async def listen_for_expenses() -> None:
    """Log inbound Telegram messages as expenses.

    The offset is persisted so a restart does not replay or lose messages, and
    failures back off instead of spinning silently as the old bare
    ``except: pass`` loop did.
    """
    import requests

    if not TG or not TG.enabled:
        log.info("Expense listener disabled (no Telegram credentials).")
        return

    offset = _load_offset()
    csv_path = BASE_DIR / "daily_expenses.csv"
    if not csv_path.exists():
        csv_path.write_text("timestamp,message\n", encoding="utf-8")

    log.info("Expense listener active (offset %d).", offset)
    backoff = 1
    url = f"https://api.telegram.org/bot{TG.token}/getUpdates"

    while True:
        try:
            resp = await asyncio.to_thread(
                requests.post, url, json={"offset": offset + 1, "timeout": 20}, timeout=30
            )
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("description", "getUpdates not ok"))
            for result in data.get("result", []):
                offset = result["update_id"]
                _save_offset(offset)
                text = (result.get("message") or {}).get("text", "").strip()
                if not text or text.startswith("/"):
                    continue
                with open(csv_path, "a", encoding="utf-8") as fh:
                    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    fh.write(f'{stamp},"{text.replace(chr(34), chr(39))}"\n')
                TG.send(f"Logged: {text}")
            backoff = 1
        except Exception as exc:
            log.warning("Expense listener error (%s); retrying in %ds.", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        await asyncio.sleep(1)


async def mcp_keepalive() -> None:
    """Keep the SSE stream warm; Cloudflare drops idle streams after ~100s."""
    while True:
        await asyncio.sleep(60)
        try:
            if _session:
                await _session.list_tools()
        except Exception as exc:
            log.debug("Keepalive ping failed: %s", exc)


# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------
async def preflight(use_llm: bool = True) -> bool:
    ok = True

    if not CFG.news_sources:
        log.error("config.json defines no news_sources.")
        ok = False
    if not CFG.keyword_map:
        log.warning("config.json defines no tracking.keywords; only symbols of four or "
                    "more characters will be matched, using the ticker itself.")

    if use_llm and LLM is not None:
        try:
            model = await LLM.preflight()
            log.info("LLM preflight OK — using model '%s'.", model)
        except LLMUnavailable as exc:
            log.error("LLM preflight FAILED: %s", exc)
            if TG:
                TG.alert(f"LLM preflight failed: {exc}")
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
async def dry_run(fixture: Optional[str], use_llm: bool) -> int:
    path = Path(fixture) if fixture else BASE_DIR / "tests" / "fixtures" / "holdings.json"
    if not path.exists():
        log.error("Fixture not found: %s", path)
        return 1
    log.info("Dry run against %s", path)
    if use_llm and LLM is not None and not await preflight(use_llm=True):
        log.warning("Continuing the dry run without the LLM.")
        use_llm = False
    result = await run_analysis(path.read_text(encoding="utf-8"), use_llm=use_llm)
    return 0 if result else 1


def _schedule_jobs(loop: asyncio.AbstractEventLoop) -> None:
    login_time = CFG.agent.get("login_time", "09:00")
    analysis_time = CFG.agent.get("analysis_time", "23:00")

    def job(coro_factory, name: str):
        def runner():
            log.info("Scheduler firing: %s", name)
            task = loop.create_task(coro_factory())

            def _done(t: asyncio.Task) -> None:
                exc = t.exception() if not t.cancelled() else None
                if exc:
                    log.exception("Scheduled job '%s' raised", name, exc_info=exc)
                    if TG:
                        TG.alert(f"Scheduled job '{name}' failed: {exc}")

            task.add_done_callback(_done)
        return runner

    schedule.every().day.at(login_time).do(job(send_login_link, "morning login"))
    schedule.every().day.at(analysis_time).do(job(run_analysis, "nightly analysis"))
    log.info("Scheduled: login at %s, analysis at %s (Pi local time).", login_time, analysis_time)


async def main_loop(args: argparse.Namespace) -> int:
    global _session

    use_llm = not args.no_llm

    if args.dry_run:
        return await dry_run(args.fixture, use_llm)

    # Imported here so that --dry-run works on a machine without the MCP client.
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    log.info("Starting the Zerodha Kite MCP bridge...")
    # mcp-remote needs the inherited PATH/HOME to find node and its token cache.
    server_params = StdioServerParameters(
        command=CFG.npx_path,
        args=["-y", "mcp-remote", CFG.kite_mcp_url],
        env=dict(os.environ),
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            _session = session
            log.info("MCP connection established.")

            healthy = await preflight(use_llm=use_llm)
            if args.preflight:
                return 0 if healthy else 1
            if not healthy and use_llm:
                log.warning("Preflight failed; continuing with the LLM disabled so the "
                            "deterministic report is still produced.")
                use_llm = False

            if args.once:
                holdings = await probe_session()
                if holdings is None:
                    await send_login_link()
                    log.error("No valid session. Log in via the Telegram link and re-run.")
                    return 1
                return 0 if await run_analysis(holdings, use_llm=use_llm) else 1

            if not args.daemon:
                answer = input("Run an on-demand analysis now? (y/n): ").strip().lower()
                if answer == "y":
                    holdings = await probe_session()
                    if holdings is None:
                        log.info("Session expired — sending a fresh login link.")
                        await send_login_link()
                        input("Press Enter here once you have completed the Telegram login... ")
                        holdings = await probe_session()
                    if holdings is None:
                        log.error("Still no valid session; skipping the on-demand run.")
                    else:
                        await run_analysis(holdings, use_llm=use_llm)

            _schedule_jobs(asyncio.get_running_loop())
            asyncio.create_task(listen_for_expenses())
            asyncio.create_task(mcp_keepalive())

            log.info("Agent running. Ctrl+C to exit.")
            while True:
                schedule.run_pending()
                await asyncio.sleep(1)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Personal finance AI agent")
    parser.add_argument("--daemon", action="store_true", help="run as a service, no prompts")
    parser.add_argument("--once", action="store_true", help="run one analysis and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="run offline against fixture holdings (no Kite connection)")
    parser.add_argument("--fixture", help="path to a holdings JSON fixture for --dry-run")
    parser.add_argument("--preflight", action="store_true",
                        help="verify the runtime, model and config, then exit")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip all model calls; produce the deterministic report only")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main() -> int:
    global CFG, TG, LLM
    args = parse_args()
    setup_logging(args.verbose)
    CFG = load_config()
    TG = Telegram(CFG.telegram_token, CFG.telegram_chat_id)
    LLM = None if args.no_llm else LLMClient(CFG, cache_dir=CACHE_DIR)
    try:
        return asyncio.run(main_loop(args))
    except KeyboardInterrupt:
        log.info("Interrupted; shutting down.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
