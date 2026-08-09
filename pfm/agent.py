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
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import schedule

import brokers
import news as news_mod
import report as report_mod
from brokers import BOOK_US, AuthRequired, IndmoneyProvider, KiteProvider, ProviderError
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
# Created lazily inside the running loop; a module-level asyncio.Lock() binds to
# the wrong event loop on Python 3.9 and then fails at the first await.
_run_lock: Optional[asyncio.Lock] = None
_OFFSET_FILE = STATE_DIR / "telegram_offset.json"
_LAST_RUN_FILE = STATE_DIR / "last_run.json"


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
    """Keep the streams warm; Cloudflare drops idle streams after ~100s."""
    while True:
        await asyncio.sleep(60)
        for label, session in (("kite", _session), ("indmoney", _ind_session)):
            if session is None:
                continue
            try:
                await session.list_tools()
            except Exception as exc:
                log.debug("Keepalive ping to %s failed: %s", label, exc)


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


@asynccontextmanager
async def _indmoney_session(enabled: bool):
    """Open the INDmoney MCP session, yielding None if it cannot be established.

    Wrapped in its own context manager so that a failure here - an expired OAuth
    token, npx trouble, INDmoney down - degrades to an India-only report instead
    of taking the whole run with it.
    """
    global _ind_session
    if not enabled:
        yield None
        return

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    url = CFG.indmoney_mcp_url
    log.info("Starting the INDmoney MCP bridge (%s)...", url)
    params = StdioServerParameters(
        command=CFG.npx_path, args=["-y", "mcp-remote", url], env=dict(os.environ),
    )
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=180)
                _ind_session = session
                log.info("INDmoney MCP connection established.")
                try:
                    yield session
                finally:
                    _ind_session = None
    except Exception as exc:
        log.warning("INDmoney MCP unavailable (%s). Continuing with the India book only. "
                    "If this is an auth failure, run: python tools/probe_indmoney.py "
                    "--list-only", exc)
        _ind_session = None
        yield None


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

    us_enabled = bool(CFG.raw.get("indmoney", {}).get("enabled", True)) and not args.no_us

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

            # The US book rides alongside the India book. If this session cannot
            # be opened the context manager yields None and the run continues.
            async with _indmoney_session(us_enabled):
                if args.once:
                    holdings = await probe_session()
                    if holdings is None:
                        await send_login_link()
                        log.error("No valid session. Log in via the Telegram link and re-run.")
                        return 1
                    result = await run_analysis(holdings, use_llm=use_llm,
                                                force=args.force)
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
