"""Restartable MCP bridges, with OAuth sign-in URLs forwarded to you.

Both brokers are reached through ``mcp-remote``, which handles OAuth and caches
tokens under ``~/.mcp-auth``. Two problems that this module exists to solve:

1. **The sign-in URL only ever reached the log.** When ``mcp-remote`` needs a
   fresh authorisation it prints ``Please authorize this client by visiting:``
   followed by a URL — to stderr, which on the Pi means journalctl. A headless
   daemon nobody is tailing may as well not have printed it. The bridge tees that
   stream and forwards any authorisation URL straight to Telegram.

2. **A session could not be restarted.** The connection used to live inside a
   nested ``async with`` in the main loop, so reconnecting meant restarting the
   whole service. Here it is held in an :class:`~contextlib.AsyncExitStack` that
   can be unwound and rebuilt on demand, which is what makes an on-request
   re-login possible.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger("pfm.bridges")

# mcp-remote prints the authorisation URL on its own line. Matching the URL
# itself rather than the sentence keeps this working if the wording changes.
_AUTH_URL_RE = re.compile(r"https?://\S*/authorize\?\S+")

# Where mcp-remote keeps its cached client registrations and tokens.
MCP_AUTH_DIR = Path(os.path.expanduser("~")) / ".mcp-auth"


class _StderrTee:
    """Watches a subprocess's stderr: logs each line, reports sign-in URLs.

    This owns a **real OS pipe**, which is not an implementation detail we can
    choose. ``stdio_client`` passes its ``errlog`` argument to
    ``anyio.open_process(stderr=...)``, which hands it to the operating system
    when spawning the child — so it must be something with a genuine file
    descriptor. A Python file-like object with a ``write`` method is never
    consulted and fails at ``fileno()``.

    So: ``open()`` returns the write end for the child to inherit, and a reader
    thread drains the read end. A thread rather than a task because the fd is a
    blocking pipe, and because this must keep working while the event loop is
    busy inside ``session.initialize()`` — which is exactly when the
    authorisation URL appears.
    """

    def __init__(self, label: str, on_auth_url: Optional[Callable[[str], None]] = None):
        self.label = label
        self.on_auth_url = on_auth_url
        self.lines: List[str] = []
        self._buffer = ""
        self._seen_urls: set = set()
        self._read_fd: Optional[int] = None
        self._write_file = None
        self._thread: Optional[threading.Thread] = None

    def open(self):
        """Create the pipe and start draining it. Returns the write end."""
        read_fd, write_fd = os.pipe()
        self._read_fd = read_fd
        # Line buffered: the child's stderr reaches us as it is produced, so a
        # sign-in URL is forwarded while it is still valid.
        self._write_file = os.fdopen(write_fd, "w", buffering=1, errors="replace")
        self._thread = threading.Thread(target=self._drain, name=f"{self.label}-stderr",
                                        daemon=True)
        self._thread.start()
        return self._write_file

    def _drain(self) -> None:
        fd = self._read_fd
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break                       # the fd was closed under us: we are done
            if not chunk:
                break                       # EOF: the child exited
            self._feed(chunk.decode("utf-8", "replace"))
        self.flush()

    def _feed(self, text: str) -> None:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._handle(line)

    def flush(self) -> None:
        if self._buffer:
            self._handle(self._buffer)
            self._buffer = ""

    def close(self) -> None:
        """Close both ends. Safe to call twice, and on a tee never opened."""
        if self._write_file is not None:
            try:
                self._write_file.close()
            except OSError:
                pass
            self._write_file = None
        if self._read_fd is not None:
            try:
                os.close(self._read_fd)     # unblocks the reader thread
            except OSError:
                pass
            self._read_fd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _handle(self, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        self.lines = (self.lines + [line])[-200:]
        log.debug("[%s] %s", self.label, line)

        match = _AUTH_URL_RE.search(line)
        if not match:
            return
        url = match.group(0)
        if url in self._seen_urls:
            return
        self._seen_urls.add(url)
        log.info("[%s] authorisation URL detected.", self.label)
        if self.on_auth_url:
            try:
                self.on_auth_url(url)
            except Exception as exc:          # never let a notifier break the bridge
                log.warning("Could not forward the %s auth URL: %s", self.label, exc)


class MCPBridge:
    """One ``mcp-remote`` connection that can be stopped and restarted."""

    def __init__(self, name: str, url: str, *, npx_path: str = "npx",
                 on_auth_url: Optional[Callable[[str], None]] = None,
                 init_timeout: float = 180.0):
        self.name = name
        self.url = url
        self.npx_path = npx_path
        self.on_auth_url = on_auth_url
        self.init_timeout = init_timeout
        self.session = None
        self._stack: Optional[AsyncExitStack] = None
        self._tee: Optional[_StderrTee] = None
        self._lock: Optional[asyncio.Lock] = None
        self.last_error: Optional[str] = None
        self.last_log: List[str] = []       # survives the tee, for /status

    # -- plumbing ----------------------------------------------------------
    def _get_lock(self) -> asyncio.Lock:
        # Created lazily: a module-level asyncio primitive binds to the wrong
        # loop on Python 3.9.
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def connected(self) -> bool:
        return self.session is not None

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> bool:
        """Open the bridge. Returns True on success; never raises."""
        async with self._get_lock():
            return await self._start_unlocked()

    async def _start_unlocked(self) -> bool:
        if self.session is not None:
            return True

        log.info("Starting the %s MCP bridge (%s)...", self.name, self.url)
        stack = AsyncExitStack()
        try:
            # Imported here, inside the guard, so that a missing or broken mcp
            # install is reported like any other start failure rather than
            # taking down the Telegram listener that called us.
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            self._close_tee()
            self._tee = _StderrTee(self.name, self.on_auth_url)
            params = StdioServerParameters(
                command=self.npx_path,
                args=["-y", "mcp-remote", self.url],
                env=dict(os.environ),
            )
            read, write = await stack.enter_async_context(
                stdio_client(params, errlog=self._tee.open()))
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=self.init_timeout)
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("%s MCP bridge failed to start: %s", self.name, exc)
            try:
                await stack.aclose()
            except Exception:
                pass
            self.session, self._stack = None, None
            self._close_tee()
            return False

        self._stack, self.session, self.last_error = stack, session, None
        log.info("%s MCP connection established.", self.name)
        return True

    async def stop(self) -> None:
        async with self._get_lock():
            await self._stop_unlocked()

    def _close_tee(self) -> None:
        """Release the stderr pipe. Leaking these would exhaust the fd table
        after enough /login restarts, which is the whole point of this class.

        The captured lines outlive the pipe, because the moment you most want
        to read them is after a start that failed — and that is precisely when
        the tee gets torn down.
        """
        if self._tee is not None:
            self._tee.close()
            self.last_log = list(self._tee.lines)
            self._tee = None

    async def _stop_unlocked(self) -> None:
        self.session = None
        if self._stack is None:
            self._close_tee()
            return
        try:
            await self._stack.aclose()
        except Exception as exc:
            # A dying subprocess often throws on teardown; it is still gone.
            log.debug("%s bridge teardown raised: %s", self.name, exc)
        finally:
            self._stack = None
            self._close_tee()
            log.info("%s MCP bridge stopped.", self.name)

    async def restart(self) -> bool:
        """Tear the bridge down and build it again, under one lock.

        Taking the lock across both halves matters: a scheduled run must not find
        a half-open session while a re-login is in progress.
        """
        async with self._get_lock():
            await self._stop_unlocked()
            await asyncio.sleep(0.5)       # let the subprocess actually exit
            return await self._start_unlocked()

    def recent_log(self, limit: int = 12) -> List[str]:
        """The subprocess's last stderr lines, live or from the last attempt."""
        lines = self._tee.lines if self._tee is not None else self.last_log
        return list(lines[-limit:])


# ===========================================================================
# Cached credentials
# ===========================================================================
def find_cached_credentials(url: str, auth_dir: Path = MCP_AUTH_DIR) -> List[Path]:
    """Files under ~/.mcp-auth that belong to one server.

    mcp-remote names its files by a hash of the server URL, which is not
    reproducible from here, so the files are identified by their *contents*
    mentioning the host. That way a targeted clear-out can never take the other
    broker's token with it.
    """
    if not auth_dir.is_dir():
        return []

    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    needle = host.split(":")[0]
    prefixes: set = set()
    for path in auth_dir.iterdir():
        if not path.is_file():
            continue
        try:
            if needle in path.read_text(encoding="utf-8", errors="ignore").lower():
                prefixes.add(path.name.split("_")[0])
        except OSError:
            continue

    if not prefixes:
        return []
    return sorted(p for p in auth_dir.iterdir()
                  if p.is_file() and p.name.split("_")[0] in prefixes)


def clear_cached_credentials(url: str, auth_dir: Path = MCP_AUTH_DIR,
                             *, backup: bool = True) -> List[str]:
    """Remove one server's cached credentials, returning what was removed.

    Destructive, so: only files positively identified as belonging to this
    server are touched, and by default they are moved aside rather than deleted,
    which makes the operation reversible if the match was ever wrong.
    """
    targets = find_cached_credentials(url, auth_dir)
    removed: List[str] = []
    if not targets:
        return removed

    graveyard = auth_dir / "_pfm_cleared"
    if backup:
        graveyard.mkdir(parents=True, exist_ok=True)

    for path in targets:
        try:
            if backup:
                shutil.move(str(path), str(graveyard / path.name))
            else:
                path.unlink()
            removed.append(path.name)
        except OSError as exc:
            log.warning("Could not clear %s: %s", path.name, exc)

    log.warning("Cleared %d cached credential file(s) for %s: %s",
                len(removed), url, ", ".join(removed))
    return removed
