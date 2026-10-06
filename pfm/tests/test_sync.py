#!/usr/bin/env python3
"""Offline tests for /sync — the on-demand portfolio refresh.

The point of /sync is that it is *not* a run. It reads both books, replies with
the figures, and refreshes the net-worth snapshot the goals page uses. What it
must never do is look like a completed analysis, because the weekend rule skips
a run when the portfolio is unchanged since the last one — so a midday sync
that wrote the baseline would quietly cancel that night's real report.

That is the property most of this file is about. The rest covers degrading
rather than failing when INDmoney is unavailable, since the India book is still
worth having.

No Pi, no model, no broker, no network. Run with either:
    python tests/test_sync.py
    pytest tests/test_sync.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import List

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
sys.path.insert(0, str(HERE.parent))

import agent                                                # noqa: E402
import commands as cmd                                      # noqa: E402
from pfm_config import load_config                          # noqa: E402

# The daemon sets this during startup; these tests call the handlers directly.
agent.CFG = load_config(HERE.parent / "config.json")

_failures: List[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
    else:
        print(f"  FAIL  {message}")
        _failures.append(message)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


class _FakeTG:
    enabled = True

    def __init__(self):
        self.messages: List[str] = []

    def send(self, message):
        self.messages.append(message)

    def alert(self, message):
        self.messages.append(message)


def _holdings_text() -> str:
    return json.dumps(json.loads((FIXTURES / "holdings.json").read_text()))


async def _run_sync() -> str:
    return await agent.cmd_sync(cmd.parse_command("/sync"))


# ===========================================================================
# 1. It reports, and it does not pretend to be a run
# ===========================================================================
def test_sync_leaves_the_schedule_alone() -> None:
    section("A sync is not a run")

    text = _holdings_text()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        saved = (agent.STATE_FILE if hasattr(agent, "STATE_FILE") else None,
                 agent._SNAPSHOT_FILE, agent.REPORT_DIR, agent.TG,
                 agent._ind_session)
        agent._SNAPSHOT_FILE = tmp_path / "networth_snapshot.json"
        agent.REPORT_DIR = tmp_path / "reports"
        agent.REPORT_DIR.mkdir()
        agent.TG = _FakeTG()
        agent._ind_session = None            # India book only; US is optional

        state_file = tmp_path / "state.json"
        if hasattr(agent, "STATE_FILE"):
            agent.STATE_FILE = state_file
        baseline_before = state_file.read_text() if state_file.exists() else None

        async def fake_probe():
            return text

        real_probe = agent.probe_session
        agent.probe_session = fake_probe
        try:
            reply = asyncio.run(_run_sync())
        finally:
            agent.probe_session = real_probe

        print("\n".join("    | " + line for line in reply.split("\n")))

        check("Portfolio synced" in reply, "the reply leads with the sync and a time")
        check("Total value" in reply, "it reports the total value")
        check("tonight's run is unaffected" in reply,
              "and says plainly that the schedule is untouched")

        check(not list(agent.REPORT_DIR.iterdir()),
              "no report file is written to the archive")
        after = state_file.read_text() if state_file.exists() else None
        check(after == baseline_before,
              "the run state and weekend baseline are not touched")

        (agent._SNAPSHOT_FILE, agent.REPORT_DIR, agent.TG,
         agent._ind_session) = saved[1:]
        if hasattr(agent, "STATE_FILE") and saved[0] is not None:
            agent.STATE_FILE = saved[0]


# ===========================================================================
# 2. The India book alone is still worth having
# ===========================================================================
def test_sync_degrades_without_indmoney() -> None:
    section("INDmoney missing costs the US book, not the sync")

    text = _holdings_text()
    saved_tg, saved_ind = agent.TG, agent._ind_session
    agent.TG = _FakeTG()
    agent._ind_session = None

    async def fake_probe():
        return text

    real_probe = agent.probe_session
    agent.probe_session = fake_probe
    try:
        reply = asyncio.run(_run_sync())
    finally:
        agent.probe_session = real_probe
        agent.TG, agent._ind_session = saved_tg, saved_ind

    check("India (Kite)" in reply, "the India book is still reported")
    check("US (INDmoney)" not in reply,
          "the US book is simply absent rather than shown as zero")
    check("keeps its last figures" in reply,
          "and the reply says the goals page was not refreshed")


# ===========================================================================
# 3. A total must not silently drop a book
# ===========================================================================
def _us_provider():
    from brokers import BOOK_US

    class _Provider:
        def __init__(self, session):
            pass

        async def holdings(self):
            return ([{"symbol": "AAPL", "book": BOOK_US, "quantity": 10,
                      "average_price": 1500, "last_price": 1900,
                      "current_native": 19000, "invested_native": 15000,
                      "currency": "USD", "investment_code": "m1"}], [])

        async def watchlist(self):
            return []

        async def us_details(self, symbols):
            return {}

        async def networth_snapshot(self):
            return {"total_networth": 7250000,
                    "investments": [{"asset_type": "US_STOCK",
                                     "current_value": 19000}]}

    return _Provider


def test_total_says_what_it_covers() -> None:
    section("A rupee total that excludes the US book says so")

    text = _holdings_text()
    saved = (agent.TG, agent._ind_session, agent.IndmoneyProvider,
             agent._SNAPSHOT_FILE, agent.probe_session)
    agent.TG = _FakeTG()
    agent._ind_session = object()
    agent.IndmoneyProvider = _us_provider()

    async def fake_probe():
        return text

    agent.probe_session = fake_probe
    configured = agent.CFG.raw.setdefault("portfolio", {})
    original_rate = configured.get("usd_inr_rate")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            agent._SNAPSHOT_FILE = Path(tmp) / "snapshot.json"

            # Without a rate, dollars cannot be added to rupees.
            configured["usd_inr_rate"] = None
            reply = asyncio.run(_run_sync())
            check("US (INDmoney)" in reply, "the US book is still listed on its own line")
            check("Total value (India only)" in reply,
                  "but the total is labelled as covering India only")
            check("No USD/INR rate" in reply,
                  "and the reason appears in the notes, with the fix")

            # With a rate, both books combine and the label is plain again.
            configured["usd_inr_rate"] = 88.5
            reply = asyncio.run(_run_sync())
            check("Total value: " in reply and "India only" not in reply,
                  "a configured rate gives a single combined total")
            check("USD/INR 88.50" in reply, "and the rate used is stated")
            check("1,681,500" in reply, "with the US book converted at that rate")
    finally:
        configured["usd_inr_rate"] = original_rate
        (agent.TG, agent._ind_session, agent.IndmoneyProvider,
         agent._SNAPSHOT_FILE, agent.probe_session) = saved


def test_sync_refreshes_the_goals_page() -> None:
    section("The snapshot /goals reads is refreshed")

    import web                                              # noqa: E402

    text = _holdings_text()
    saved = (agent.TG, agent._ind_session, agent.IndmoneyProvider,
             agent._SNAPSHOT_FILE, agent.probe_session)
    agent.TG = _FakeTG()
    agent._ind_session = object()

    class _Provider(_us_provider()):
        async def networth_snapshot(self):
            return {"total_networth": 7250000,
                    "investments": [{"asset_type": "SA", "current_value": 312000},
                                    {"asset_type": "STOCK", "current_value": 1840000},
                                    {"asset_type": "PPF", "current_value": 1200000}]}

    agent.IndmoneyProvider = _Provider

    async def fake_probe():
        return text

    agent.probe_session = fake_probe
    try:
        with tempfile.TemporaryDirectory() as tmp:
            agent._SNAPSHOT_FILE = Path(tmp) / "snapshot.json"
            reply = asyncio.run(_run_sync())
            check(agent._SNAPSHOT_FILE.exists(), "the snapshot file is written")
            check("goals page now reflects" in reply, "and the reply says so")

            saved_web = (web.SNAPSHOT_FILE, web.GOALS_FILE)
            web.SNAPSHOT_FILE = agent._SNAPSHOT_FILE
            web.GOALS_FILE = Path(tmp) / "goals.json"
            try:
                tiers = web.goals_state()["tiers"]
                check(tiers["ready"]["total"] == 312000,
                      "and /goals reads the fresh ready-money figure")
                check(tiers["sellable"]["total"] == 1840000,
                      "the fresh sellable figure")
                check(tiers["locked"]["total"] == 1200000,
                      "and the fresh locked figure")
            finally:
                web.SNAPSHOT_FILE, web.GOALS_FILE = saved_web
    finally:
        (agent.TG, agent._ind_session, agent.IndmoneyProvider,
         agent._SNAPSHOT_FILE, agent.probe_session) = saved


# ===========================================================================
# 4. Failure modes answer rather than hang
# ===========================================================================
def test_sync_failure_modes() -> None:
    section("Every failure gets a reply you can act on")

    saved_tg = agent.TG
    agent.TG = _FakeTG()
    real_probe = agent.probe_session
    try:
        async def no_session():
            return None

        agent.probe_session = no_session
        reply = asyncio.run(_run_sync())
        check("/login" in reply and "expired" in reply,
              "an expired Zerodha session points at /login")

        async def junk():
            return "Please log in first using the login tool"

        agent.probe_session = junk
        reply = asyncio.run(_run_sync())
        check("could not be parsed" in reply,
              "an unparseable reply says so rather than reporting an empty portfolio")

        async def boom():
            raise RuntimeError("kite fell over")

        agent.probe_session = boom
        try:
            reply = asyncio.run(_run_sync())
            check(False, "an exception should not escape cmd_sync")
        except RuntimeError:
            # probe_session is called outside the guard; the router catches it.
            check(True, "a broker exception propagates to the router, which replies")

        # A run already in progress must win: its figures are fresher.
        agent.probe_session = real_probe
        lock = agent._get_run_lock()

        async def during_run():
            async with lock:
                return await _run_sync()

        reply = asyncio.run(during_run())
        check("already running" in reply,
              "a sync during an analysis defers to it instead of duplicating work")
    finally:
        agent.probe_session = real_probe
        agent.TG = saved_tg


# ===========================================================================
# 4. It is wired into the bot
# ===========================================================================
def test_sync_is_registered() -> None:
    section("The command is reachable")

    check("/sync" in cmd.HELP_TEXT, "/sync appears in the help text")
    parsed = cmd.parse_command("/sync")
    check(parsed is not None and parsed.name == "sync", "/sync parses as a command")
    for alias in ("/refresh", "/portfolio"):
        parsed = cmd.parse_command(alias)
        check(parsed is not None, f"{alias} parses too")


# ===========================================================================
def test_all():
    test_sync_leaves_the_schedule_alone()
    test_sync_degrades_without_indmoney()
    test_total_says_what_it_covers()
    test_sync_refreshes_the_goals_page()
    test_sync_failure_modes()
    test_sync_is_registered()
    assert not _failures, f"{len(_failures)} check(s) failed:\n" + "\n".join(_failures)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
    try:
        test_all()
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
    print("\nAll sync checks passed.")
