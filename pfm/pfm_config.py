"""Configuration loading and path resolution for the personal finance agent.

Every path is resolved relative to this file, so the agent behaves identically
whether it is launched by systemd, by cron, or from an arbitrary shell cwd.
The previous code did ``open('config.json')`` from three different places,
which silently produced different behaviour depending on how it was started.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
CACHE_DIR = BASE_DIR / "cache"
REPORT_DIR = BASE_DIR / "reports"
STATE_DIR = BASE_DIR / "state"

load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("pfm.config")

# ---------------------------------------------------------------------------
# Defaults. Anything missing from config.json falls back to these, so an old
# or partial config file can never crash the agent at 23:00 unattended.
# ---------------------------------------------------------------------------
DEFAULTS: Dict[str, Any] = {
    "agent_settings": {
        "analysis_time": "23:00",
        "login_lead_minutes": 15,
        "auth_grace_minutes": 20,
        "auth_retry_interval_minutes": 2,
        "login_time": None,          # optional extra morning link
        "skip_unchanged_weekends": True,
        "weekend_days": ["saturday", "sunday"],
        "notify_on_skip": True,
        "storage_backend": "markdown",
        "cloud_api_endpoint": None,
    },
    "llm": {
        "host": "http://127.0.0.1:8000",
        "model": "qwen2.5-instruct:1.5b",
        "fallback_models": [],
        "temperature": 0.0,
        "repeat_penalty": 1.05,
        "keep_alive": -1,
        "request_timeout_seconds": 240,
        "max_attempts": 3,
        "score_num_predict": 160,
        "score_chunk_size": 6,
        "narrative_num_predict": 420,
        "cache_ttl_hours": 18,
    },
    "news": {
        "max_headlines_per_call": 6,
        "max_headline_chars": 180,
        "max_articles_per_stock": 12,
        "feed_timeout_seconds": 12,
        "feed_attempts": 2,
        "duplicate_similarity_threshold": 0.9,
    },
    "web": {
        "host": "0.0.0.0",
        "port": 7373,
        "privacy": {
            "blur_by_default": False,
            "blur_on_focus_loss": True,
            "blur_on_tab_hidden": True,
            "blur_on_screenshot_keys": True,
            "idle_seconds": 180,
        },
    },
    "narrative": {"enabled": True, "max_attempts": 2},
    "portfolio": {"pnl_mismatch_tolerance_pct": 1.0, "usd_inr_rate": None},
    "indmoney": {"enabled": True, "mcp_url": "https://mcp.indmoney.com/mcp"},
    "tracking": {"keywords": {}, "exclude_phrases": {}, "watchlist": []},
    "news_sources": [],
}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow-per-section merge: defaults fill gaps, user values win."""
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            merged = dict(out[key])
            merged.update(value)
            out[key] = merged
        else:
            out[key] = value
    return out


def _strip_notes(obj: Any) -> Any:
    """Remove ``_note`` documentation keys so they never leak into prompts."""
    if isinstance(obj, dict):
        return {k: _strip_notes(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_notes(v) for v in obj]
    return obj


class Config:
    """Typed-ish accessor over the merged config dictionary."""

    def __init__(self, raw: Dict[str, Any]):
        self.raw = raw
        self.agent = raw["agent_settings"]
        self.llm = raw["llm"]
        self.news = raw["news"]
        self.narrative = raw["narrative"]
        self.portfolio = raw["portfolio"]
        self.tracking = raw["tracking"]
        self.news_sources: List[Dict[str, str]] = raw["news_sources"]

    # -- tracking helpers ---------------------------------------------------
    @property
    def keyword_map(self) -> Dict[str, List[str]]:
        return {s.upper(): [k.upper() for k in kws]
                for s, kws in self.tracking.get("keywords", {}).items()}

    @property
    def exclude_map(self) -> Dict[str, List[str]]:
        return {s.upper(): [p.upper() for p in phrases]
                for s, phrases in self.tracking.get("exclude_phrases", {}).items()}

    @property
    def watchlist(self) -> List[str]:
        return [s.upper() for s in self.tracking.get("watchlist", [])]

    def keywords_for(self, symbol: str) -> List[str]:
        """Keywords for a symbol, defaulting to the symbol itself.

        This is what makes the agent self-maintaining: a stock bought tomorrow
        appears in Kite holdings and is matched on its own ticker without any
        config edit. Symbols shorter than 4 characters are excluded from the
        implicit fallback because bare 2-3 letter tokens generate far too many
        false positives in news text.
        """
        explicit = self.keyword_map.get(symbol.upper())
        if explicit:
            return explicit
        if len(symbol) >= 4:
            return [symbol.upper()]
        return []

    # -- environment --------------------------------------------------------
    @property
    def telegram_token(self) -> str | None:
        return os.getenv("TELEGRAM_TOKEN")

    @property
    def telegram_chat_id(self) -> str | None:
        return os.getenv("TELEGRAM_CHAT_ID")

    @property
    def npx_path(self) -> str:
        return os.getenv("NPX_PATH", "npx")

    @property
    def kite_mcp_url(self) -> str:
        return os.getenv("KITE_MCP_URL", "https://mcp.kite.trade/mcp")

    @property
    def indmoney_mcp_url(self) -> str:
        configured = (self.raw.get("indmoney", {}) or {}).get("mcp_url")
        return os.getenv("INDMONEY_MCP_URL") or configured or "https://mcp.indmoney.com/mcp"


def _migrate_legacy(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Accept the old ``tracking.stocks`` list shape and convert it in memory."""
    tracking = raw.get("tracking", {})
    legacy = tracking.get("stocks")
    if not legacy:
        return raw
    log.warning("config.json uses the legacy tracking.stocks list; migrating in memory.")
    keywords = dict(tracking.get("keywords", {}))
    for entry in legacy:
        symbol = str(entry.get("symbol", "")).upper()
        if not symbol:
            continue
        # Legacy configs contained padded keywords such as "VI " which broke
        # word-boundary matching; strip them here.
        kws = [str(k).strip().upper() for k in entry.get("keywords", []) if str(k).strip()]
        keywords.setdefault(symbol, kws or [symbol])
    tracking["keywords"] = keywords
    tracking.pop("stocks", None)
    raw["tracking"] = tracking
    return raw


def load_config(path: Path | str | None = None) -> Config:
    path = Path(path) if path else CONFIG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        user_raw = _strip_notes(json.load(fh))
    merged = _merge(DEFAULTS, _migrate_legacy(user_raw))
    for directory in (CACHE_DIR, REPORT_DIR, STATE_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    return Config(merged)


def setup_logging(verbose: bool = False) -> None:
    """Timestamped logging to stdout so journalctl entries are diagnosable."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
