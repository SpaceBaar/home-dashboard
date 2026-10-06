"""Telegram command handling.

Commands are only accepted from the configured chat. The bot token is a bearer
credential: anyone who learns it can message the bot, and these commands start
real work and hand out sign-in links, so an unauthorised sender is refused and
logged rather than quietly ignored.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("pfm.commands")

HELP_TEXT = """Commands

/login — new Zerodha login link
   Checks the session first; only sends a link if it has actually expired.

/indmoney — reconnect the US book
   Reopens the INDmoney bridge. Any sign-in URL is forwarded here.
   /indmoney force — also clear the cached INDmoney credentials first.

/code <address> — finish a sign-in approved on your phone
   After approving, the browser is sent to localhost and refuses to connect.
   That is expected: the callback server runs on the Pi, not your phone.
   Copy the whole failed address and send it here, and the Pi completes it.

/status — both broker sessions, the last run, and tonight's plan

/run — run the analysis now, ignoring the weekend skip

/help — this message

Anything else you send is logged as an expense."""


@dataclass
class Command:
    name: str
    args: List[str]
    raw: str

    @property
    def arg(self) -> str:
        """First argument, lowercased — for keywords like ``force``."""
        return self.args[0].lower() if self.args else ""

    @property
    def text(self) -> str:
        """Everything after the command name, exactly as it was typed.

        Anything case-sensitive must use this rather than ``arg``. An OAuth
        authorisation code lowercased is simply a different, invalid code —
        and the failure is silent, because the callback server accepts it and
        only the later token exchange rejects it.
        """
        return " ".join(self.args)


def parse_command(text: str) -> Optional[Command]:
    """Parse a Telegram command, or None if the text is not one.

    Handles the ``/cmd@BotName`` form Telegram uses in groups.
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split()
    name = parts[0][1:].split("@", 1)[0].lower()
    # A command name is letters, digits and underscores - anything else ("//",
    # "/-", a bare slash) is ordinary text that happens to begin with a slash.
    if not name or not re.fullmatch(r"[a-z0-9_]+", name):
        return None
    return Command(name=name, args=parts[1:], raw=stripped)


def is_authorised(chat_id, allowed_chat_id) -> bool:
    """Only the configured chat may issue commands.

    Compared as strings because Telegram sends the id as a number while the
    environment holds it as text.
    """
    if allowed_chat_id in (None, ""):
        return False
    return str(chat_id).strip() == str(allowed_chat_id).strip()


Handler = Callable[[Command], Awaitable[str]]


class CommandRouter:
    """Maps a command name to a coroutine returning the reply text."""

    def __init__(self, allowed_chat_id):
        self.allowed_chat_id = allowed_chat_id
        self._handlers: Dict[str, Handler] = {}
        self._aliases: Dict[str, str] = {}
        self.rejected: List[Tuple[str, str]] = []

    def register(self, name: str, handler: Handler, *aliases: str) -> None:
        self._handlers[name] = handler
        for alias in aliases:
            self._aliases[alias] = name

    def resolve(self, name: str) -> Optional[Handler]:
        return self._handlers.get(self._aliases.get(name, name))

    def known(self) -> List[str]:
        return sorted(set(self._handlers) | set(self._aliases))

    async def dispatch(self, text: str, chat_id) -> Optional[str]:
        """Run a command. Returns the reply, or None when the text is not one."""
        command = parse_command(text)
        if command is None:
            return None

        if not is_authorised(chat_id, self.allowed_chat_id):
            self.rejected.append((str(chat_id), command.name))
            log.warning("Ignoring /%s from unauthorised chat %s.",
                        command.name, chat_id)
            return None          # stay silent; do not confirm the bot exists

        handler = self.resolve(command.name)
        if handler is None:
            return (f"Unknown command /{command.name}.\n\n"
                    f"Try: {', '.join('/' + c for c in self.known())}")

        # Hand the handler the canonical name, so /us behaves exactly like
        # /indmoney rather than subtly differently.
        canonical = self._aliases.get(command.name, command.name)
        if canonical != command.name:
            command = Command(name=canonical, args=command.args, raw=command.raw)

        log.info("Running command /%s %s", command.name, " ".join(command.args))
        try:
            return await handler(command)
        except Exception as exc:
            log.exception("Command /%s failed", command.name)
            return f"/{command.name} failed: {exc}"
