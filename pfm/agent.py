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
    python agent.py --force         run even on an unchanged non-trading day
    python agent.py --show-state    print the last run and the weekend decision

Scheduling
----------
The Kite login link goes out ``login_lead_minutes`` before ``analysis_time``,
because a token issued in the morning is usually dead by night, and only when the
existing session has actually expired. On a non-trading day the run short-circuits
once it has confirmed the portfolio has not moved, before any news or model work.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import schedule

import brokers
import news as news_mod
import report as report_mod
from bridges import MCPBridge, clear_cached_credentials
from brokers import BOOK_US, AuthRequired, IndmoneyProvider, KiteProvider, ProviderError
from commands import HELP_TEXT, Command, CommandRouter, is_authorised
from llm import LLMClient, LLMUnavailable
from notify import Telegram
from pfm_config import BASE_DIR, CACHE_DIR, REPORT_DIR, STATE_DIR, load_config, setup_logging
from portfolio import build_fact_sheet, extract_holdings_json, resolve_fx

log = logging.getLogger("pfm.agent")

CFG = None            # populated in main()
TG: Optional[Telegram] = None
LLM: Optional[LLMClient] = None
_session = None       # Kite mcp.ClientSession; imported lazily so --dry-run needs no MCP install
_ind_session = None   # INDmoney mcp.ClientSession, or None when the US book is off
KITE_BRIDGE: Optional[MCPBridge] = None    # restartable, so /login can reconnect
IND_BRIDGE: Optional[MCPBridge] = None     # restartable, so /indmoney can re-auth
# Created lazily inside the running loop; a module-level asyncio.Lock() binds to
# the wrong event loop on Python 3.9 and then fails at the first await.
_run_lock: Optional[asyncio.Lock] = None
_OFFSET_FILE = STATE_DIR / "telegram_offset.json"
_LAST_RUN_FILE = STATE_DIR / "last_run.json"
_SNAPSHOT_FILE = STATE_DIR / "networth_snapshot.json"   # feeds the goals view


class _Skipped:
    """Sentinel: the run was deliberately skipped, which is not a failure."""

    def __init__(self, reason: str):
        self.reason = reason

    def __bool__(self) -> bool:      # so `if result:` reads naturally
        return True


def holdings_fingerprint(fact_sheet) -> str:
    """Hash of what is held and at what price.

    Compared alongside the total value, because a total can coincidentally match
    while the positions behind it have changed - a buy and a sell that happen to
    net out, or T+1 quantities settling over the weekend.
    """
    parts = [
        f"{h.symbol}|{h.quantity:.6f}|{(h.ltp or 0):.4f}|{h.current_native:.2f}"
        for h in sorted(fact_sheet.holdings, key=lambda x: x.symbol)
    ]
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def save_last_run(fact_sheet, *, status: str, report: Optional[str] = None) -> None:
    """Record the outcome of a run.

    The comparison baseline is kept separate from the latest status, and is only
    replaced by a successful run. That matters for chaining: after a Saturday skip
    the last *run* was a skip, but the figures to compare Sunday against are still
    Friday's, so Sunday can skip too.
    """
    now = datetime.now()
    state = load_state()
    state["last_run"] = {
        "date": now.strftime("%Y-%m-%d"),
        "at": now.isoformat(timespec="seconds"),
        "weekday": now.strftime("%A"),
        "status": status,
    }
    if status == "success" and fact_sheet is not None:
        state["baseline"] = {
            "date": now.strftime("%Y-%m-%d"),
            "weekday": now.strftime("%A"),
            "total_current": round(fact_sheet.total_current, 2),
            "total_invested": round(fact_sheet.total_invested, 2),
            "holdings_count": len(fact_sheet.holdings),
            "fingerprint": holdings_fingerprint(fact_sheet),
            "report": report,
        }
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _LAST_RUN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(_LAST_RUN_FILE)
    except OSError as exc:
        log.warning("Could not record the run state: %s", exc)


def load_state() -> dict:
    try:
        state = json.loads(_LAST_RUN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def load_last_run() -> dict:
    """The most recent run's outcome, whatever it was."""
    return load_state().get("last_run") or {}


def load_baseline() -> dict:
    """The figures from the most recent *successful* run."""
    return load_state().get("baseline") or {}


def weekend_skip_reason(fact_sheet, *, now: Optional[datetime] = None) -> Optional[str]:
    """Should tonight's run be skipped? Returns the reason, or None to proceed.

    All of these must hold:
      1. today is configured as a non-trading day,
      2. a previous run succeeded, giving us figures to compare against,
      3. the last run did not fail - a failure is retried rather than skipped,
      4. neither the total value nor the holdings fingerprint has moved.

    Only Sunday is reliably flat. A Saturday 23:00 IST run sees Friday's US
    closing prices, whereas the Friday run saw that session still open, so
    Saturday usually does differ and will still produce a report. That is why this
    compares values instead of skipping weekends outright.
    """
    now = now or datetime.now()
    if not CFG.agent.get("skip_unchanged_weekends", True):
        return None

    non_trading = {str(d).strip().lower()
                   for d in (CFG.agent.get("weekend_days")
                             or ["saturday", "sunday"])}
    if now.strftime("%A").lower() not in non_trading:
        return None

    baseline = load_baseline()
    if not baseline or baseline.get("total_current") is None:
        log.info("Weekend, but no successful run is on record to compare against; "
                 "running.")
        return None

    status = (load_last_run() or {}).get("status")
    if status == "failed":
        log.info("Weekend, but the previous run failed; running.")
        return None

    current_total = round(fact_sheet.total_current, 2)
    previous_total = float(baseline["total_current"])
    if abs(current_total - previous_total) >= 0.01:
        log.info("Weekend, but the total moved from %s to %s; running.",
                 f"{previous_total:,.2f}", f"{current_total:,.2f}")
        return None

    if baseline.get("fingerprint") and \
            baseline["fingerprint"] != holdings_fingerprint(fact_sheet):
        log.info("Weekend and the total is unchanged, but the holdings themselves "
                 "differ; running.")
        return None

    return (f"{now:%A} with the markets closed, and the portfolio is unchanged "
            f"since the {baseline.get('weekday', 'previous')} run on "
            f"{baseline.get('date', 'an earlier date')} "
            f"(Rs {current_total:,.0f}).")


def _num_or_none(value) -> Optional[float]:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


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


async def wait_for_kite_session() -> Optional[str]:
    """Probe for a valid Kite session, retrying through a short grace window.

    The login prompt goes out a few minutes before the run, so the token often
    lands moments after the analysis starts. Rather than abort and lose the night,
    poll for ``auth_grace_minutes`` and continue the moment it appears.
    """
    holdings = await probe_session()
    if holdings is not None:
        return holdings

    grace = int(CFG.agent.get("auth_grace_minutes", 20) or 0)
    interval = max(1, int(CFG.agent.get("auth_retry_interval_minutes", 2) or 2))
    if grace <= 0:
        return None

    attempts = max(1, grace // interval)
    log.warning("No Kite session at analysis time; polling every %d min for up to "
                "%d min.", interval, grace)
    if TG:
        TG.send(f"Waiting for your Zerodha login. The analysis will start "
                f"automatically once you have tapped the link, any time in the "
                f"next {grace} minutes.")

    for attempt in range(1, attempts + 1):
        await asyncio.sleep(interval * 60)
        holdings = await probe_session()
        if holdings is not None:
            log.info("Kite session became valid after %d minute(s); continuing.",
                     attempt * interval)
            return holdings
        log.info("Still no session (%d/%d).", attempt, attempts)
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
async def run_analysis(holdings_text: Optional[str] = None, *, use_llm: bool = True,
                       force: bool = False):
    """Run the nightly pipeline.

    Returns the report path on success, a :class:`_Skipped` marker when the
    weekend rule short-circuits it, or None on failure.
    """
    lock = _get_run_lock()
    if lock.locked():
        log.warning("An analysis run is already in progress; skipping this trigger.")
        return None

    async with lock:
        started = datetime.now()
        log.info("=== Analysis run starting ===")

        if holdings_text is None:
            holdings_text = await wait_for_kite_session()
        if holdings_text is None:
            log.error("No valid Kite session; aborting.")
            # Recorded as a failure so a weekend does not skip on the strength of
            # an older successful run.
            save_last_run(None, status="failed")
            if TG:
                TG.alert("Nightly analysis aborted: no valid Zerodha session. "
                         "The login link was sent before the run; tap it and the "
                         "next scheduled run will pick up from there.")
            return None

        holdings_raw = extract_holdings_json(holdings_text)
        if not holdings_raw:
            log.error("Could not parse holdings. First 400 chars: %r", holdings_text[:400])
            save_last_run(None, status="failed")
            if TG:
                TG.alert("Nightly analysis aborted: the holdings payload could not be parsed.")
            return None

        # 1a. US book from INDmoney. Never fatal: a stale OAuth token costs the
        #     US section, not the whole night's report.
        us_rows: List[dict] = []
        us_problems: List[str] = []
        broker_sentiment: dict = {}
        snapshot_fx: Optional[float] = None

        us_quotes: dict = {}
        if _ind_session is not None:
            provider = IndmoneyProvider(_ind_session)
            try:
                us_rows, us_problems = await provider.holdings()
                log.info("INDmoney: %d holding(s) kept for the US book%s.",
                         len(us_rows),
                         f", {len(us_problems)} excluded" if us_problems else "")
                if not us_rows:
                    us_problems.append(
                        "INDmoney returned no usable US holdings. Run "
                        "tools/probe_indmoney.py to see how each row was classified."
                    )

                # Resolve tickers by exact id join before anything else uses the
                # symbols. investment_code equals entity_basic.mycroft_id, so a
                # quote lookup over the candidate pool identifies each holding
                # without any name matching.
                if us_rows:
                    # Candidates come only from places INDmoney or you declared:
                    # your INDmoney watchlist, and tracking.watchlist in
                    # config.json. Nothing is guessed.
                    candidates = set(CFG.watchlist)
                    try:
                        candidates |= set(await provider.watchlist())
                    except Exception as exc:
                        log.info("INDmoney watchlist unavailable: %s", exc)
                    # Anything Kite already reports is Indian; keep it away from a
                    # US endpoint, where an unknown symbol can fail the batch.
                    candidates -= {h.get("symbol") for h in holdings_raw
                                   if isinstance(h, dict) and h.get("symbol")}
                    candidates = {c for c in candidates if brokers.looks_like_us_ticker(c)}

                    details = await provider.us_details(sorted(candidates))

                    # The only ticker source: INDmoney's own entity_basic.symbol,
                    # joined on its own investment_code == mycroft_id.
                    by_code, warnings = brokers.resolve_by_code(
                        us_rows, brokers.build_code_index(details))
                    us_problems.extend(warnings)
                    log.info("Resolved %d US ticker(s) from INDmoney's own quote data.",
                             by_code)

                    # Fetch details for tickers discovered after the first call so
                    # their news and quotes are available too.
                    resolved = {h["symbol"] for h in us_rows if not brokers.needs_ticker(h)}
                    missing = sorted(resolved - set(details))
                    if missing:
                        details.update(await provider.us_details(missing))

                    us_quotes = brokers.extract_us_quotes(details)
                    broker_sentiment = brokers.extract_us_news(details)

                    # INDmoney supplies no ticker for some holdings. Those are shown
                    # under the instrument name it does supply, identified by its
                    # instrument code. Nothing is invented to fill the gap; the
                    # report just says so.
                    unresolved = [f"{h.get('name') or h['symbol']} "
                                  f"(INDmoney code {h.get('investment_code')})"
                                  for h in us_rows if brokers.needs_ticker(h)]
                    if unresolved:
                        us_problems.append(
                            "INDmoney provides no ticker for " + "; ".join(unresolved)
                            + ". They are shown under the instrument name INDmoney "
                              "returned. Add them to a watchlist in the INDmoney app, "
                              "or to tracking.keywords in config.json, for news matching."
                        )

                    # The rate INDmoney itself applied, read back out of the data.
                    snapshot_fx, fx_note = brokers.derive_usd_inr(us_rows, us_quotes)
                    if snapshot_fx:
                        log.info("Implied USD/INR %.2f (%s)", snapshot_fx, fx_note)

                    # One snapshot call serves two purposes: cross-checking the US
                    # total, and giving the goals view something to work from
                    # without needing a live broker session.
                    snapshot = await provider.networth_snapshot()
                    if snapshot:
                        save_networth_snapshot(snapshot)

                    snapshot_total = None
                    for entry in (snapshot or {}).get("investments") or []:
                        if isinstance(entry, dict) and \
                                str(entry.get("asset_type", "")).upper() == "US_STOCK":
                            snapshot_total = brokers._num(entry.get("current_value"))
                            break

                    # The two have been seen to differ by about 1%.
                    row_sum = sum(h.get("current_native") or 0.0 for h in us_rows)
                    if snapshot_total and row_sum:
                        gap = snapshot_total - row_sum
                        if abs(gap) > max(1.0, row_sum * 0.005):
                            us_problems.append(
                                f"INDmoney's US holdings add up to Rs {row_sum:,.0f}, "
                                f"but its own portfolio summary reports "
                                f"Rs {snapshot_total:,.0f} for the same asset class "
                                f"(a difference of Rs {gap:+,.0f}). The row-level sum "
                                f"is used, because that is what the holdings table "
                                f"adds up to. The gap is usually cache freshness or "
                                f"an idle USD cash balance."
                            )
                            log.warning("US total mismatch: rows Rs %s vs snapshot Rs %s",
                                        f"{row_sum:,.0f}", f"{snapshot_total:,.0f}")
            except AuthRequired as exc:
                log.warning("INDmoney needs re-authentication: %s", exc)
                us_problems.append(
                    "The US book was unavailable because the INDmoney session has expired. "
                    "Re-authorise with: python tools/probe_indmoney.py --list-only"
                )
                if TG:
                    TG.send("INDmoney session expired, so tonight's report covers the India "
                            "book only.\n\nRe-authorise on the Pi:\n"
                            "cd ~/Projects/home-dashboard/pfm && "
                            "python tools/probe_indmoney.py --list-only\n\n"
                            "That opens the INDmoney sign-in page; the token is then cached "
                            "for the daemon.")
            except ProviderError as exc:
                log.error("INDmoney holdings failed: %s", exc)
                us_problems.append(f"The US book could not be read from INDmoney: {exc}")

        # 1b. Deterministic portfolio mathematics over both books.
        usd_inr, fx_source = resolve_fx(
            _num_or_none(CFG.portfolio.get("usd_inr_rate")), snapshot_fx)

        fact_sheet = build_fact_sheet(
            list(holdings_raw) + us_rows,
            mismatch_tolerance_pct=float(CFG.portfolio.get("pnl_mismatch_tolerance_pct", 1.0)),
            usd_inr=usd_inr,
            fx_source=fx_source,
        )
        fact_sheet.data_quality.extend(us_problems)
        held = {h.symbol for h in fact_sheet.holdings}

        # networth_holdings has no day-change field for US rows, so take it from
        # the live quote. A percentage move needs no currency conversion.
        filled = 0
        for holding in fact_sheet.holdings:
            quote = us_quotes.get(holding.symbol)
            if holding.book == BOOK_US and holding.day_pct is None and quote:
                if quote.get("day_pct") is not None:
                    holding.day_pct = quote["day_pct"]
                    holding.flags.append("day change from the INDmoney live quote")
                    filled += 1
        if filled:
            log.info("Filled the day change for %d US holding(s) from live quotes.", filled)

        # 1c. Weekend short-circuit. Placed here deliberately: holdings are cheap
        #     to fetch, whereas the news scan and the per-stock LLM calls are the
        #     expensive part, so the decision is made on real figures but before
        #     any of that work is done.
        if not force:
            reason = weekend_skip_reason(fact_sheet)
            if reason:
                log.info("=== Analysis skipped: %s ===", reason)
                # status only; the baseline stays as the last successful run so a
                # Saturday skip does not force Sunday to run.
                save_last_run(None, status="skipped")
                if TG and CFG.agent.get("notify_on_skip", True):
                    TG.send("No analysis tonight: " + reason
                            + "\n\nThe last report is still the current one.")
                return _Skipped(reason)
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

        # 2b. US headlines from INDmoney. Indian RSS covers US names thinly, so
        #     this is the better source for those tickers. We still score them
        #     with the local model, keeping every score on one scale.
        # Headlines were collected alongside the quotes in step 1a; fold them in.
        added = sum(len(v.get("articles") or []) for v in broker_sentiment.values())
        if added:
            log.info("INDmoney supplied %d US headline(s) across %d ticker(s).",
                     added, len(broker_sentiment))
            grouped = news_mod.merge_articles(
                grouped, broker_sentiment,
                similarity_threshold=float(CFG.news["duplicate_similarity_threshold"]),
                max_per_stock=int(CFG.news["max_articles_per_stock"]),
            )
        elif _ind_session is not None and us_rows:
            log.info("INDmoney returned no US headlines; RSS remains the only US source.")
            fact_sheet.data_quality.append(
                "INDmoney returned quotes but no headlines for the US book, so US news "
                "came from the RSS feeds only."
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

        # Where our score and INDmoney's own sentiment disagree sharply, say so
        # rather than silently preferring one of them.
        disagreements = news_mod.sentiment_disagreements(scores, broker_sentiment)
        if disagreements:
            fact_sheet.data_quality.extend(disagreements)

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
                broker_sentiment=broker_sentiment,
            ),
            REPORT_DIR,
        )

        if TG:
            TG.send(report_mod.telegram_summary(fact_sheet, scores, path))

        save_last_run(fact_sheet, status="success", report=path.name)

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


# ---------------------------------------------------------------------------
# Telegram commands
# ---------------------------------------------------------------------------
async def cmd_login(_: Command) -> str:
    """Fresh Zerodha login link, but only when one is actually needed."""
    if KITE_BRIDGE is None or not KITE_BRIDGE.connected:
        if KITE_BRIDGE is not None:
            await KITE_BRIDGE.restart()
            globals()["_session"] = KITE_BRIDGE.session
        if KITE_BRIDGE is None or not KITE_BRIDGE.connected:
            return ("The Kite bridge is not connected and could not be restarted.\n"
                    f"Last error: {getattr(KITE_BRIDGE, 'last_error', 'unknown')}")

    if await probe_session() is not None:
        return ("Your Zerodha session is still valid — no new link needed.\n"
                "Send /run if you want the analysis now.")

    try:
        result = await _session.call_tool("login", arguments={})
        url = result.content[0].text if result.content else None
    except Exception as exc:
        return f"Could not generate a login URL: {exc}"

    if not url:
        return "Kite returned no login URL. Try /login again in a moment."
    return (f"Zerodha login link:\n\n{url}\n\n"
            f"These expire quickly, so open it now. Send /status afterwards to "
            f"confirm, or /run to start the analysis.")


async def cmd_indmoney(command: Command) -> str:
    """Reconnect the US book, optionally clearing cached credentials first."""
    if IND_BRIDGE is None:
        return "The US book is disabled for this run (--no-us or indmoney.enabled=false)."

    force = command.arg in {"force", "-f", "--force", "reauth"}
    notes: List[str] = []

    if force:
        removed = clear_cached_credentials(CFG.indmoney_mcp_url)
        if removed:
            notes.append(f"Cleared {len(removed)} cached credential file(s): "
                         + ", ".join(removed))
        else:
            notes.append("No cached INDmoney credentials were found to clear.")

    ok = await IND_BRIDGE.restart()
    globals()["_ind_session"] = IND_BRIDGE.session

    if not ok:
        notes.append(f"Reconnect failed: {IND_BRIDGE.last_error}")
        tail = IND_BRIDGE.recent_log(4)
        if tail:
            notes.append("Last lines:\n" + "\n".join(tail))
        if not force:
            notes.append("If it keeps failing, send: /indmoney force")
        return "\n\n".join(notes)

    # Connected, but the token may still be stale - prove it with a real call.
    try:
        rows, _ = await IndmoneyProvider(IND_BRIDGE.session).holdings()
        notes.append(f"INDmoney reconnected. {len(rows)} US holding(s) visible.")
    except AuthRequired:
        notes.append("INDmoney reconnected but still reports you are not signed in.")
        if not force:
            notes.append("Send /indmoney force to clear the cached credentials and "
                         "start a fresh sign-in.")
    except Exception as exc:
        notes.append(f"INDmoney reconnected, but reading holdings failed: {exc}")

    return "\n\n".join(notes)


async def cmd_status(_: Command) -> str:
    """Both broker sessions, the last run, and tonight's plan."""
    lines = ["Status", ""]

    kite_live = KITE_BRIDGE is not None and KITE_BRIDGE.connected
    session_ok = await probe_session() is not None if kite_live else False
    lines.append(f"Zerodha Kite: {'bridge up' if kite_live else 'bridge DOWN'}, "
                 f"{'session valid' if session_ok else 'needs login (/login)'}")

    if IND_BRIDGE is None:
        lines.append("INDmoney: disabled for this run")
    elif IND_BRIDGE.connected:
        lines.append("INDmoney: bridge up")
    else:
        lines.append(f"INDmoney: bridge DOWN ({IND_BRIDGE.last_error}) — /indmoney")

    state = load_state()
    last, baseline = state.get("last_run") or {}, state.get("baseline") or {}
    if last:
        lines += ["", f"Last run: {last.get('status')} on {last.get('date')} "
                      f"({last.get('weekday')})"]
    if baseline.get("total_current") is not None:
        lines.append(f"Baseline value: Rs {float(baseline['total_current']):,.0f} "
                     f"from {baseline.get('date')}")
        if baseline.get("report"):
            lines.append(f"Latest report: {baseline['report']}")

    today = datetime.now().strftime("%A")
    non_trading = {str(d).lower() for d in (CFG.agent.get("weekend_days") or [])}
    analysis_time = CFG.agent.get("analysis_time", "23:00")
    if today.lower() in non_trading:
        lines += ["", f"Today is {today}: tonight's {analysis_time} run will be "
                      f"skipped if the portfolio is unchanged. /run overrides."]
    else:
        lines += ["", f"Today is {today}: the {analysis_time} run will go ahead."]
    return "\n".join(lines)


async def cmd_run(_: Command) -> str:
    """Run the analysis now, ignoring the weekend skip."""
    if _get_run_lock().locked():
        return "An analysis is already running. I will send the summary when it finishes."

    holdings = await probe_session()
    if holdings is None:
        return ("Your Zerodha session has expired, so the run cannot start.\n"
                "Send /login, complete it, then /run again.")

    async def _go() -> None:
        try:
            await run_analysis(holdings, use_llm=True, force=True)
        except Exception as exc:
            log.exception("Commanded run failed")
            if TG:
                TG.alert(f"The run you requested failed: {exc}")

    asyncio.create_task(_go())
    return ("Analysis started. This takes around 15 minutes on the Pi; the summary "
            "will arrive here when it is done.")


async def cmd_help(_: Command) -> str:
    return HELP_TEXT


def build_router() -> CommandRouter:
    router = CommandRouter(CFG.telegram_chat_id)
    router.register("login", cmd_login, "kite", "zerodha")
    router.register("indmoney", cmd_indmoney, "us", "ind")
    router.register("status", cmd_status, "state")
    router.register("run", cmd_run, "analyse", "analyze")
    router.register("help", cmd_help, "start", "commands")
    return router


async def listen_for_messages() -> None:
    """Handle Telegram commands, and log anything else as an expense.

    The offset is persisted so a restart does not replay or lose messages, and
    failures back off instead of spinning silently.
    """
    import requests

    if not TG or not TG.enabled:
        log.info("Telegram listener disabled (no credentials).")
        return

    router = build_router()
    offset = _load_offset()
    csv_path = BASE_DIR / "daily_expenses.csv"
    if not csv_path.exists():
        csv_path.write_text("timestamp,message\n", encoding="utf-8")

    log.info("Telegram listener active (offset %d). Commands: %s",
             offset, ", ".join("/" + c for c in router.known()))
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
                message = result.get("message") or {}
                text = (message.get("text") or "").strip()
                chat_id = (message.get("chat") or {}).get("id")
                if not text:
                    continue

                if text.startswith("/"):
                    reply = await router.dispatch(text, chat_id)
                    if reply:
                        TG.send(reply)
                    continue

                # Expense logging is restricted to the configured chat too, so a
                # stranger cannot write lines into your expense file.
                if not is_authorised(chat_id, CFG.telegram_chat_id):
                    log.warning("Ignoring a message from unauthorised chat %s.", chat_id)
                    continue

                with open(csv_path, "a", encoding="utf-8") as fh:
                    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    fh.write(f'{stamp},"{text.replace(chr(34), chr(39))}"\n')
                TG.send(f"Logged: {text}")
            backoff = 1
        except Exception as exc:
            log.warning("Telegram listener error (%s); retrying in %ds.", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        await asyncio.sleep(1)


async def mcp_keepalive(interval: float = 60.0, max_backoff: float = 3600.0,
                        clock=time.monotonic) -> None:
    """Keep the streams warm, and bring a dead bridge back by itself.

    Cloudflare drops idle streams after ~100s, hence the ping. The reconnect
    half matters more: a bridge that failed at startup would otherwise stay
    down until someone noticed and sent /login, which rather defeats the point
    of an unattended overnight agent. Backoff doubles from one minute to an
    hour so a broker that is genuinely down is not hammered, and a reconnect
    needs no alert — nobody wants a Telegram message every retry.

    ``interval``, ``max_backoff`` and ``clock`` are injectable so the recovery
    can be tested in milliseconds rather than hours.
    """
    attempt = {"kite": 0, "indmoney": 0}
    due = {"kite": 0.0, "indmoney": 0.0}

    while True:
        await asyncio.sleep(interval)
        now = clock()

        for label, bridge, global_name in (
                ("kite", KITE_BRIDGE, "_session"),
                ("indmoney", IND_BRIDGE, "_ind_session")):
            session = globals()[global_name]

            if session is not None:
                try:
                    await session.list_tools()
                    attempt[label] = 0
                    continue
                except Exception as exc:
                    log.debug("Keepalive ping to %s failed: %s", label, exc)
                    continue      # a live-but-stalled stream is the ping's job,
                                  # not the reconnector's; /login handles it

            # No session at all: this bridge never started, or was stopped.
            if bridge is None or now < due[label]:
                continue
            attempt[label] += 1
            backoff = min(interval * 2 ** (attempt[label] - 1), max_backoff)
            due[label] = now + backoff
            log.info("Trying to reconnect the %s bridge (attempt %d)...",
                     label, attempt[label])
            try:
                if await bridge.start():
                    globals()[global_name] = bridge.session
                    attempt[label] = 0
                    log.info("The %s bridge is back.", label)
                else:
                    log.info("The %s bridge is still down (%s); next try in %.0fs.",
                             label, bridge.last_error, backoff)
            except Exception as exc:
                log.warning("Reconnecting %s raised: %s", label, exc)


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
async def dry_run(fixture: Optional[str], use_llm: bool,
                  force: bool = False) -> int:
    path = Path(fixture) if fixture else BASE_DIR / "tests" / "fixtures" / "holdings.json"
    if not path.exists():
        log.error("Fixture not found: %s", path)
        return 1
    log.info("Dry run against %s", path)
    if use_llm and LLM is not None and not await preflight(use_llm=True):
        log.warning("Continuing the dry run without the LLM.")
        use_llm = False
    result = await run_analysis(path.read_text(encoding="utf-8"), use_llm=use_llm,
                                force=force)
    return 0 if result else 1


def shift_time(hhmm: str, minutes: int) -> str:
    """Offset an HH:MM clock time, wrapping around midnight.

    Used to place the login prompt shortly before the analysis rather than in the
    morning: a token issued at 09:00 is routinely dead by 23:00.
    """
    hour, minute = (int(part) for part in hhmm.strip().split(":")[:2])
    total = (hour * 60 + minute + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


async def prompt_login_before_analysis() -> None:
    """Ask for a fresh Kite login only if the current session is actually dead.

    Runs a few minutes before the analysis. Probing first means no pointless
    Telegram message on the nights when the token is still good.
    """
    analysis_time = CFG.agent.get("analysis_time", "23:00")
    lead = int(CFG.agent.get("login_lead_minutes", 15) or 15)

    holdings = await probe_session()
    if holdings is not None:
        log.info("Kite session is still valid; no login link needed before the "
                 "%s run.", analysis_time)
        return

    log.info("Kite session is expired; sending the login link %d minutes ahead of "
             "the %s run.", lead, analysis_time)
    if TG:
        grace = int(CFG.agent.get("auth_grace_minutes", 20) or 0)
        deadline = (f" It will keep retrying for up to {grace} minutes after that "
                    f"if you are late." if grace else "")
        TG.send(
            f"Zerodha login needed before tonight's analysis.\n\n"
            f"The run starts at {analysis_time}, in about {lead} minutes.{deadline}\n\n"
            f"Tap the link below, complete the login, and nothing else is required."
        )
    await send_login_link()


def _schedule_jobs(loop: asyncio.AbstractEventLoop) -> None:
    analysis_time = CFG.agent.get("analysis_time", "23:00")
    lead = int(CFG.agent.get("login_lead_minutes", 15) or 15)
    morning_time = CFG.agent.get("login_time") or None

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

    try:
        prompt_time = shift_time(analysis_time, -lead)
    except (ValueError, IndexError):
        log.error("analysis_time %r is not HH:MM; defaulting to 23:00 with a "
                  "22:45 login prompt.", analysis_time)
        analysis_time, prompt_time = "23:00", "22:45"

    schedule.every().day.at(prompt_time).do(
        job(prompt_login_before_analysis, "pre-analysis login prompt"))
    schedule.every().day.at(analysis_time).do(
        job(lambda: run_analysis(force=False), "nightly analysis"))

    # An extra morning link is optional and off unless login_time is set.
    if morning_time:
        schedule.every().day.at(morning_time).do(job(send_login_link, "morning login"))

    log.info("Scheduled (Pi local time): login prompt at %s, analysis at %s%s.",
             prompt_time, analysis_time,
             f", extra morning link at {morning_time}" if morning_time else "")


def save_networth_snapshot(snapshot: dict) -> None:
    """Cache the cross-asset snapshot for the goals view.

    Only the parts the goals page needs are kept, so a file of personal balances
    stays as small as it can be.
    """
    record = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "total_invested": snapshot.get("total_invested"),
        "total_current_value": snapshot.get("total_current_value"),
        "total_networth": snapshot.get("total_networth"),
        "investments": [row for row in (snapshot.get("investments") or [])
                        if isinstance(row, dict)],
        "liabilities": snapshot.get("liabilities") or {},
    }
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _SNAPSHOT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        tmp.replace(_SNAPSHOT_FILE)
        log.info("Net-worth snapshot cached for the goals view (%d asset classes).",
                 len(record["investments"]))
    except OSError as exc:
        log.warning("Could not cache the net-worth snapshot: %s", exc)


def _forward_auth_url(label: str):
    """Push an OAuth sign-in URL to Telegram instead of burying it in the log."""
    def handler(url: str) -> None:
        log.info("%s needs authorisation; sending the link to Telegram.", label)
        if TG:
            TG.send(f"{label} needs you to sign in again.\n\n{url}\n\n"
                    f"Open it, approve, then send /status to confirm.")
    return handler


async def main_loop(args: argparse.Namespace) -> int:
    global _session

    use_llm = not args.no_llm

    if args.show_state:
        state = load_state()
        if not state:
            print(f"No run state recorded yet ({_LAST_RUN_FILE}).")
            return 0
        print(json.dumps(state, indent=2))
        today = datetime.now().strftime("%A")
        non_trading = [str(d).lower() for d in (CFG.agent.get("weekend_days") or [])]
        print(f"\nToday is {today}; non-trading days are {non_trading or 'none'}.")
        if today.lower() in non_trading:
            print("Tonight's run would be skipped if the portfolio still matches the "
                  "baseline above.")
        else:
            print("Tonight's run will go ahead: it is a trading day.")
        return 0

    if args.dry_run:
        return await dry_run(args.fixture, use_llm, force=args.force)

    global KITE_BRIDGE, IND_BRIDGE

    us_enabled = bool(CFG.raw.get("indmoney", {}).get("enabled", True)) and not args.no_us

    # Both bridges are restartable, so a re-login never needs a service restart.
    KITE_BRIDGE = MCPBridge("Zerodha Kite", CFG.kite_mcp_url,
                            npx_path=CFG.npx_path,
                            on_auth_url=_forward_auth_url("Zerodha Kite"))
    IND_BRIDGE = MCPBridge("INDmoney", CFG.indmoney_mcp_url,
                           npx_path=CFG.npx_path,
                           on_auth_url=_forward_auth_url("INDmoney"))

    if not await KITE_BRIDGE.start():
        log.error("Could not open the Kite bridge: %s", KITE_BRIDGE.last_error)
        for line in KITE_BRIDGE.recent_log(8):
            log.error("  [kite] %s", line)
        # A daemon must not exit here. systemd would restart it on a loop, and
        # each restart re-sends the alert and kills the Telegram listener —
        # taking /login with it, which is the one thing that could fix this.
        # So: say it once, carry on, and let the scheduled run or /login
        # recover. Only the foreground modes, where someone is watching and a
        # non-zero exit is useful, still fail hard.
        if not args.daemon:
            if TG:
                TG.alert(f"Could not connect to Zerodha Kite: {KITE_BRIDGE.last_error}")
            return 1
        if TG:
            TG.alert(f"Could not connect to Zerodha Kite: {KITE_BRIDGE.last_error}\n"
                     f"The agent is still running. Send /login for a fresh sign-in "
                     f"link, or /status to see where it stands.")
    _session = KITE_BRIDGE.session

    try:
        healthy = await preflight(use_llm=use_llm)
        if args.preflight:
            return 0 if healthy else 1
        if not healthy and use_llm:
            log.warning("Preflight failed; continuing with the LLM disabled so the "
                        "deterministic report is still produced.")
            use_llm = False

        # The US book rides alongside the India book. A failure here degrades to
        # an India-only report rather than taking the run with it.
        if us_enabled and not await IND_BRIDGE.start():
            log.warning("INDmoney unavailable (%s); continuing with the India book "
                        "only. Send /indmoney to retry.", IND_BRIDGE.last_error)
        _ind_session = IND_BRIDGE.session if us_enabled else None

        if args.once:
            holdings = await probe_session()
            if holdings is None:
                await send_login_link()
                log.error("No valid session. Log in via the Telegram link and re-run.")
                return 1
            result = await run_analysis(holdings, use_llm=use_llm, force=args.force)
            return 0 if result else 1

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
                    await run_analysis(holdings, use_llm=use_llm, force=True)

        _schedule_jobs(asyncio.get_running_loop())
        asyncio.create_task(listen_for_messages())
        asyncio.create_task(mcp_keepalive())

        log.info("Agent running. Ctrl+C to exit. Send /help on Telegram for commands.")
        if TG:
            TG.send("Agent started. Send /help for commands.")
        while True:
            schedule.run_pending()
            await asyncio.sleep(1)
    finally:
        await IND_BRIDGE.stop()
        await KITE_BRIDGE.stop()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Personal finance AI agent")
    parser.add_argument("--daemon", action="store_true", help="run as a service, no prompts")
    parser.add_argument("--once", action="store_true", help="run one analysis and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="run offline against fixture holdings (no Kite connection)")
    parser.add_argument("--show-state", action="store_true",
                        help="print the recorded run state and weekend decision, then exit")
    parser.add_argument("--fixture", help="path to a holdings JSON fixture for --dry-run")
    parser.add_argument("--preflight", action="store_true",
                        help="verify the runtime, model and config, then exit")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip all model calls; produce the deterministic report only")
    parser.add_argument("--no-us", action="store_true",
                        help="skip the INDmoney US book; report Indian holdings only")
    parser.add_argument("--force", action="store_true",
                        help="run even on a non-trading day with an unchanged portfolio")
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
