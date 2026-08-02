#!/usr/bin/env python3
"""Verify the Telegram credentials in pfm/.env."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notify import Telegram                        # noqa: E402
from pfm_config import load_config, setup_logging  # noqa: E402

if __name__ == "__main__":
    setup_logging()
    cfg = load_config()
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    if not tg.enabled:
        print("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID missing from pfm/.env")
        sys.exit(1)
    ok = tg.send("pfm connection test: OK")
    print("Sent." if ok else "Send failed - see the log above.")
    sys.exit(0 if ok else 1)
