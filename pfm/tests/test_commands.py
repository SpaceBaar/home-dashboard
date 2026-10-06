#!/usr/bin/env python3
"""Offline tests for the Telegram commands and the restartable MCP bridges.

No network, no Telegram, no MCP server. Covers the parts that matter when you
are standing in a shop realising the login link expired twenty minutes ago:
can you get a new one, is anyone else able to ask for it, and does a sign-in URL
actually reach you rather than the log.

Run with either:
    python tests/test_commands.py
    pytest tests/test_commands.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import bridges                                              # noqa: E402
import commands as cmd                                      # noqa: E402

_failures: List[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
    else:
        print(f"  FAIL  {message}")
        _failures.append(message)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


# ===========================================================================
# 1. Parsing
# ===========================================================================
def test_parsing() -> None:
    section("Command parsing")

    cases = [
        ("/login", "login", []),
        ("/login   ", "login", []),
        ("/indmoney force", "indmoney", ["force"]),
        ("/INDMONEY Force", "indmoney", ["Force"]),
        ("/status@MyFinanceBot", "status", []),      # the group form
        ("/run now please", "run", ["now", "please"]),
    ]
    for text, name, args in cases:
        parsed = cmd.parse_command(text)
        check(parsed is not None and parsed.name == name and parsed.args == args,
              f"{text!r} -> /{name} {args}")

    for text in ["", "   ", "coffee 250", "not /a command", "//", None]:
        check(cmd.parse_command(text) is None, f"{text!r} is not a command")

    parsed = cmd.parse_command("/indmoney FORCE")
    check(parsed.arg == "force", "the first argument is lowercased for matching")


# ===========================================================================
# 2. Authorisation
# ===========================================================================
def test_authorisation() -> None:
    section("Only the configured chat may issue commands")

    check(cmd.is_authorised(123456, "123456"), "a numeric id matches its string form")
    check(cmd.is_authorised("123456", 123456), "and the other way round")
    check(cmd.is_authorised(" 123456 ", "123456"), "whitespace is tolerated")
    check(not cmd.is_authorised(999, "123456"), "a different chat is refused")
    check(not cmd.is_authorised(None, "123456"), "a missing chat id is refused")
    check(not cmd.is_authorised(123456, None),
          "with no configured chat, nothing is authorised — fail closed")
    check(not cmd.is_authorised(123456, ""), "an empty configured chat is not a wildcard")

    calls: List[str] = []

    async def handler(command: cmd.Command) -> str:
        calls.append(command.name)
        return "ran"

    router = cmd.CommandRouter("123456")
    router.register("login", handler)

    check(asyncio.run(router.dispatch("/login", "123456")) == "ran",
          "the owner's command runs")
    check(calls == ["login"], "and the handler was actually invoked")

    reply = asyncio.run(router.dispatch("/login", 999))
    check(reply is None, "a stranger gets no reply at all")
    check(calls == ["login"], "and the handler is never reached")
    check(router.rejected == [("999", "login")], "the rejection is recorded")

    check(asyncio.run(router.dispatch("coffee 250", "123456")) is None,
          "ordinary text is not treated as a command")


# ===========================================================================
# 3. Routing
# ===========================================================================
def test_routing() -> None:
    section("Routing, aliases and failures")

    seen: List[str] = []

    async def ok(command: cmd.Command) -> str:
        seen.append(command.raw)
        return f"did {command.name}"

    async def boom(_: cmd.Command) -> str:
        raise RuntimeError("the bridge is on fire")

    router = cmd.CommandRouter("1")
    router.register("indmoney", ok, "us", "ind")
    router.register("explode", boom)

    for alias in ("/indmoney", "/us", "/ind"):
        check(asyncio.run(router.dispatch(alias, "1")) == "did indmoney",
              f"{alias} routes to the same handler")

    reply = asyncio.run(router.dispatch("/nonsense", "1"))
    check(reply and "Unknown command" in reply, "an unknown command is reported")
    check(reply and "/indmoney" in reply, "and the known commands are listed")

    reply = asyncio.run(router.dispatch("/explode", "1"))
    check(reply and "on fire" in reply,
          "a handler that raises returns the error rather than killing the listener")

    check("force" in cmd.HELP_TEXT and "/login" in cmd.HELP_TEXT,
          "the help text documents the commands")


# ===========================================================================
# 4. Auth URL capture
# ===========================================================================
def _run_through_tee(tee, script: str, timeout: float = 10.0) -> None:
    """Run a real child process with its stderr wired to the tee's pipe.

    Deliberately a subprocess rather than a direct call. The first version of
    this test poked the tee's write() method, which passed happily while the
    real thing was broken: stdio_client hands errlog to the OS as the child's
    stderr, so it needs a genuine file descriptor and never calls write() at
    all. Spawning a child is the only way to exercise what actually happens.
    """
    handle = tee.open()
    proc = subprocess.Popen([sys.executable, "-u", "-c", script], stderr=handle)
    proc.wait(timeout=timeout)
    deadline = time.time() + 2.0
    while time.time() < deadline and not tee.lines:
        time.sleep(0.01)
    time.sleep(0.15)                      # let the reader thread drain the tail


def test_auth_url_capture() -> None:
    section("mcp-remote's sign-in URL is forwarded, not buried in the log")

    captured: List[str] = []
    tee = bridges._StderrTee("INDmoney", captured.append)

    # Verbatim shape of what mcp-remote prints, from a real child process.
    url = ("https://mcp.indmoney.com/authorize?response_type=code"
           "&client_id=73cf84c9&code_challenge=BY6S0x&state=6038813d")
    _run_through_tee(tee, textwrap.dedent(f"""
        import sys
        sys.stderr.write("[21729] Connecting to remote server...\\n")
        sys.stderr.write("[21729] \\nPlease authorize this client by visiting:\\n")
        sys.stderr.write("{url}\\n")
        sys.stderr.write("[21729] Browser opened automatically.\\n")
        sys.stderr.write("{url}\\n")
    """))

    check(tee.lines, "a real subprocess's stderr reaches the tee at all")
    check(len(captured) == 1, f"exactly one URL captured ({len(captured)})")
    check(captured and captured[0].startswith("https://mcp.indmoney.com/authorize?"),
          "and it is the authorisation URL")
    check(captured and "state=6038813d" in captured[0],
          "with the full query string intact — a truncated URL would not work")
    check(len(captured) == 1, "the same URL printed twice is forwarded once")
    check(any("Browser opened" in line for line in tee.lines),
          "ordinary output is still retained for diagnostics")
    tee.close()

    # A URL split across two unflushed writes must still be caught.
    captured.clear()
    tee2 = bridges._StderrTee("Kite", captured.append)
    _run_through_tee(tee2, textwrap.dedent("""
        import sys, time
        sys.stderr.write("visit https://mcp.kite.trade/authorize?code_cha")
        sys.stderr.flush(); time.sleep(0.05)
        sys.stderr.write("llenge=abc&state=xyz\\n")
    """))
    check(len(captured) == 1 and captured and "state=xyz" in captured[0],
          "a URL split across two reads is reassembled")
    tee2.close()

    # A notifier that throws must not break the bridge.
    def angry(_: str) -> None:
        raise RuntimeError("telegram down")

    tee3 = bridges._StderrTee("Kite", angry)
    try:
        _run_through_tee(tee3, 'import sys; sys.stderr.write('
                               '"https://mcp.kite.trade/authorize?x=1\\n")')
        check(True, "a failing notifier is swallowed rather than killing the bridge")
    except Exception as exc:
        check(False, f"a failing notifier escaped: {exc}")
    tee3.close()

    # Closing must release both ends, or enough /login restarts exhaust the
    # process's file descriptors and the bridge stops coming back.
    before = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    for _ in range(25):
        spare = bridges._StderrTee("churn")
        spare.open()
        spare.close()
    if before is not None:
        after = len(os.listdir("/proc/self/fd"))
        check(after <= before + 2,
              f"25 open/close cycles leak no descriptors ({before} -> {after})")
    check(bridges._StderrTee("never-opened").close() is None,
          "closing a tee that was never opened is harmless")


# ===========================================================================
# 5. Cached credentials
# ===========================================================================
def test_credential_clearing() -> None:
    section("Clearing credentials touches only the server you asked for")

    with tempfile.TemporaryDirectory() as td:
        auth = Path(td)
        # mcp-remote names files by a hash of the URL, so identification is by
        # content. Two servers, two hashes.
        (auth / "aaa111_client_info.json").write_text(
            '{"server_url":"https://mcp.indmoney.com/mcp","client_id":"x"}')
        (auth / "aaa111_tokens.json").write_text('{"access_token":"ind-token"}')
        (auth / "bbb222_client_info.json").write_text(
            '{"server_url":"https://mcp.kite.trade/mcp","client_id":"y"}')
        (auth / "bbb222_tokens.json").write_text('{"access_token":"kite-token"}')

        found = bridges.find_cached_credentials("https://mcp.indmoney.com/mcp", auth)
        names = sorted(p.name for p in found)
        check(names == ["aaa111_client_info.json", "aaa111_tokens.json"],
              f"both INDmoney files found by hash prefix ({names})")
        check(not any("bbb222" in n for n in names),
              "and no Kite file is included")

        removed = bridges.clear_cached_credentials(
            "https://mcp.indmoney.com/mcp", auth, backup=True)
        check(sorted(removed) == ["aaa111_client_info.json", "aaa111_tokens.json"],
              f"both are cleared and reported ({removed})")
        check(not (auth / "aaa111_tokens.json").exists(),
              "the INDmoney token is gone from its original place")
        check((auth / "bbb222_tokens.json").exists(),
              "the KITE token is untouched — this is the whole point")
        check((auth / "_pfm_cleared" / "aaa111_tokens.json").exists(),
              "and it was moved aside rather than destroyed, so a wrong match is "
              "recoverable")

        check(bridges.clear_cached_credentials("https://mcp.indmoney.com/mcp", auth) == [],
              "clearing twice is a no-op rather than an error")
        check(bridges.find_cached_credentials("https://example.com/mcp", auth) == [],
              "an unknown server matches nothing")
        check(bridges.find_cached_credentials(
            "https://mcp.indmoney.com/mcp", auth / "nope") == [],
            "a missing auth directory is handled")


# ===========================================================================
# 6. Bridge lifecycle
# ===========================================================================
def test_bridge_lifecycle() -> None:
    section("Bridge restart")

    bridge = bridges.MCPBridge("Test", "https://example.com/mcp", npx_path="npx")
    check(not bridge.connected, "a new bridge starts disconnected")
    check(bridge.recent_log() == [], "with no log yet")

    # No MCP server here, so start() must fail cleanly rather than raise.
    started = asyncio.run(bridge.start())
    check(started is False, "start() returns False instead of raising")
    check(bridge.last_error, f"and records why ({str(bridge.last_error)[:40]})")
    check(not bridge.connected, "the bridge stays disconnected")

    check(asyncio.run(bridge.restart()) is False,
          "restart() on a dead bridge also fails cleanly")
    asyncio.run(bridge.stop())
    check(not bridge.connected, "stop() is safe on a bridge that never started")


# ===========================================================================
# 7. Unattended recovery
# ===========================================================================
class _FlakyBridge:
    def __init__(self, fail_times: int):
        self.n = 0
        self.fail = fail_times
        self.session = None
        self.last_error = "connection refused"

    async def start(self) -> bool:
        self.n += 1
        if self.n <= self.fail:
            return False
        self.session = object()
        return True


def test_unattended_recovery() -> None:
    section("A bridge that fails at startup comes back without a restart")

    import agent                                            # noqa: E402

    saved = (agent.KITE_BRIDGE, agent.IND_BRIDGE, agent._session, agent._ind_session)
    try:
        bridge = _FlakyBridge(fail_times=2)
        agent.KITE_BRIDGE, agent.IND_BRIDGE = bridge, None
        agent._session = agent._ind_session = None

        clock = {"t": 0.0}

        async def drive():
            task = asyncio.create_task(
                agent.mcp_keepalive(interval=0.01, max_backoff=0.32,
                                    clock=lambda: clock["t"]))
            for _ in range(200):
                clock["t"] += 0.05
                await asyncio.sleep(0.005)
                if agent._session is not None:
                    break
            task.cancel()

        asyncio.run(drive())
        check(agent._session is not None,
              "the Kite bridge reconnects itself after failing twice")
        check(bridge.n == 3, f"taking exactly three attempts (got {bridge.n})")
        check(agent.IND_BRIDGE is None,
              "and a bridge that was never created is skipped, not crashed on")
    finally:
        agent.KITE_BRIDGE, agent.IND_BRIDGE, agent._session, agent._ind_session = saved


# ===========================================================================
def test_all():
    test_parsing()
    test_authorisation()
    test_routing()
    test_auth_url_capture()
    test_credential_clearing()
    test_bridge_lifecycle()
    test_unattended_recovery()
    assert not _failures, f"{len(_failures)} check(s) failed:\n" + "\n".join(_failures)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
    try:
        test_all()
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
    print("\nAll command checks passed.")
