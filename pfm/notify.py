"""Telegram notifications.

Adds the things the original helper was missing: the 4096-character limit is
respected, failures are logged rather than swallowed, and sends are retried.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

import requests

log = logging.getLogger("pfm.notify")

_LIMIT = 3900   # Telegram's hard limit is 4096; leave room for the part marker


class Telegram:
    def _redact(self, text: str) -> str:
        """Keep the bot token out of the logs.

        requests embeds the full request URL in its exception messages, which
        would otherwise write the token into journalctl on every network blip.
        """
        return text.replace(self.token, "<token>") if self.token else text

    def __init__(self, token: Optional[str], chat_id: Optional[str]):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        if not self.enabled:
            log.warning("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID missing; notifications disabled.")

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    @staticmethod
    def _chunk(text: str) -> List[str]:
        if len(text) <= _LIMIT:
            return [text]
        chunks, current = [], ""
        for line in text.splitlines(keepends=True):
            if len(current) + len(line) > _LIMIT:
                chunks.append(current)
                current = ""
            current += line
        if current:
            chunks.append(current)
        return chunks

    def send(self, text: str, *, attempts: int = 3) -> bool:
        if not self.enabled or not text.strip():
            return False
        parts = self._chunk(text)
        ok = True
        for index, part in enumerate(parts, 1):
            prefix = f"({index}/{len(parts)})\n" if len(parts) > 1 else ""
            ok &= self._post(prefix + part, attempts=attempts)
        return ok

    def _post(self, text: str, *, attempts: int) -> bool:
        for attempt in range(1, attempts + 1):
            try:
                resp = requests.post(
                    self._url("sendMessage"),
                    json={"chat_id": self.chat_id, "text": text,
                          "disable_web_page_preview": True},
                    timeout=20,
                )
                if resp.status_code == 200:
                    return True
                log.warning("Telegram send failed (HTTP %s): %s",
                            resp.status_code, self._redact(resp.text[:200]))
                if resp.status_code == 429:
                    retry_after = 5
                    try:
                        retry_after = int(resp.json()["parameters"]["retry_after"])
                    except Exception:
                        pass
                    time.sleep(retry_after)
                    continue
            except Exception as exc:
                log.warning("Telegram send attempt %d errored: %s", attempt, self._redact(str(exc)))
            if attempt < attempts:
                time.sleep(2 * attempt)
        return False

    def alert(self, text: str) -> None:
        self.send(f"[pfm alert] {text}")
