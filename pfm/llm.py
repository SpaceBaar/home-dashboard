"""Hardened LLM client for hailo-ollama on the Raspberry Pi 5 + AI HAT+.

Design constraints that drove this module
-----------------------------------------
1. There is exactly ONE NPU. Concurrent ``generate`` calls do not run in
   parallel; they queue inside the runtime and multiply the chance of a
   timeout. Every call therefore goes through a single-slot semaphore.
2. The model is small (1.5B). It cannot reliably hold a multi-stock schema in
   its head. Each scoring call is about ONE stock only, so there is no way for
   the model to attribute a score to the wrong ticker.
3. Small models drift in format. Parsing is therefore tiered and permissive,
   with a deliberately dumber retry prompt, and finally an explicit
   "unscored" sentinel instead of a silently-wrong number.
4. Runs must be reproducible. temperature defaults to 0.0 and scores are
   cached on a hash of (model, symbol, headline set) so re-running the same day
   yields byte-identical output.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import ollama
import requests

log = logging.getLogger("pfm.llm")

# Sentinel used everywhere instead of the old free-text "Score unavailable."
UNSCORED = None

# qwen2.5 is a Chinese-origin model and sometimes answers in Chinese regardless of
# an English instruction. A score is a number and stays valid either way, but a
# non-English rationale must never reach the report, so it is dropped.
_NON_LATIN_RE = re.compile(
    "[぀-ヿ㐀-䶿一-鿿豈-﫿･-ﾟ가-힯ᄀ-ᇿЀ-ӿ֐-׿؀-ۿऀ-ॿ฀-๿]"
)


def is_english_only(text: Optional[str]) -> bool:
    return not _NON_LATIN_RE.search(text or "")


class LLMUnavailable(RuntimeError):
    """Raised when the runtime is unreachable or the model is not installed."""


@dataclass
class StockScore:
    """One aggregate sentiment score for one stock, over all of its news."""

    symbol: str
    score: Optional[int]           # 1-10, or None when the model could not be parsed
    reason: str
    confidence: str                # "high" | "low" | "unscored"
    method: str                    # how the number was obtained (audit trail)
    headline_count: int = 0
    chunk_scores: List[int] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.score is None:
            return "unscored"
        if self.score >= 8:
            return "strongly positive"
        if self.score >= 6:
            return "positive"
        if self.score == 5:
            return "neutral"
        if self.score >= 3:
            return "negative"
        return "strongly negative"

    def compact(self) -> str:
        if self.score is None:
            return f"{self.symbol}: no reliable score ({self.headline_count} headlines)"
        return f"{self.symbol}: {self.score}/10 ({self.label}) - {self.reason}"


# ---------------------------------------------------------------------------
# Score parsing - the part that was silently failing before
# ---------------------------------------------------------------------------
_SENTIMENT_WORDS = [
    (r"\bvery\s+bullish\b|\bstrongly\s+(?:positive|bullish)\b", 9),
    (r"\bbullish\b|\bpositive\b|\boptimistic\b", 7),
    (r"\bneutral\b|\bmixed\b|\bunclear\b", 5),
    (r"\bbearish\b|\bnegative\b|\bpessimistic\b", 3),
    (r"\bvery\s+bearish\b|\bstrongly\s+(?:negative|bearish)\b", 2),
]


def parse_score(raw: str) -> tuple[Optional[int], Optional[str], str]:
    """Extract (score, reason, method) from arbitrary model output.

    Tiers are tried in decreasing order of confidence. The previous
    implementation only understood a strict ``Score: N`` line anchored to a
    preceding ``Stock: X`` line; the moment the model emitted a numbered list
    or put the score on the same line as the ticker, everything fell through
    and every stock was reported as unavailable.
    """
    if not raw or not raw.strip():
        return None, None, "empty-response"

    text = raw.strip()
    # Normalise: drop markdown emphasis and bullets that break anchored regexes.
    flat = re.sub(r"[*_`#>]+", " ", text)

    score: Optional[int] = None
    method = "none"

    # Tier 1: explicit SCORE label.
    m = re.search(r"\bscores?\b\s*[:=\-]?\s*\**\s*(10|[1-9])\b", flat, re.IGNORECASE)
    if m:
        score, method = int(m.group(1)), "label"

    # Tier 2: N/10 anywhere.
    if score is None:
        m = re.search(r"\b(10|[1-9])\s*(?:/|out of)\s*10\b", flat, re.IGNORECASE)
        if m:
            score, method = int(m.group(1)), "fraction"

    # Tier 3: rating/sentiment word followed by a number nearby.
    if score is None:
        m = re.search(r"\b(?:rating|sentiment|value)\b[^0-9\n]{0,24}(10|[1-9])\b",
                      flat, re.IGNORECASE)
        if m:
            score, method = int(m.group(1)), "nearby"

    # Tier 4: the response is essentially just a number (the retry prompt path).
    if score is None:
        m = re.fullmatch(r"[^0-9]{0,12}(10|[1-9])[^0-9]{0,12}", flat, re.DOTALL)
        if m:
            score, method = int(m.group(1)), "bare"

    # Tier 5: first standalone integer in 1..10 anywhere in the text.
    if score is None:
        for candidate in re.findall(r"(?<![\d.%])\b(10|[1-9])\b(?![\d.%])", flat):
            score, method = int(candidate), "first-int"
            break

    # Tier 6: qualitative wording only.
    if score is None:
        for pattern, value in _SENTIMENT_WORDS:
            if re.search(pattern, flat, re.IGNORECASE):
                score, method = value, "sentiment-word"
                break

    if score is not None and not 1 <= score <= 10:
        score, method = None, "out-of-range"

    # -- reason ------------------------------------------------------------
    reason = None
    m = re.search(r"\b(?:reason|justification|because|rationale)\b\s*[:=\-]?\s*(.+)",
                  flat, re.IGNORECASE | re.DOTALL)
    if m:
        reason = m.group(1)
    else:
        # Fall back to the first sentence that is not just the score line.
        for line in flat.splitlines():
            line = line.strip(" -*\t")
            if len(line) < 12:
                continue
            if re.fullmatch(r"(?i)\s*scores?\s*[:=\-]?\s*(10|[1-9])\s*(?:/\s*10)?\s*", line):
                continue
            reason = line
            break

    if reason:
        reason = re.sub(r"\s+", " ", reason).strip()
        reason = reason.split("Stock:")[0].strip()
        # Strip list markers and any leading score fragment the model glued on,
        # e.g. "1. Score: 3/10 - Regulatory notice weighs on the stock."
        reason = re.sub(r"^\d+\s*[.)]\s*", "", reason)
        reason = re.sub(r"^(?:overall\s+)?(?:scores?|ratings?)\s*[:=]?\s*(?:10|[1-9])"
                        r"(?:\s*/\s*10)?\s*(?:out of 10)?\s*[-–—:,.]*\s*", "",
                        reason, flags=re.IGNORECASE)
        reason = reason.strip(" -–—:,.").strip()
        # Keep the first sentence, but never a fragment so short it says nothing.
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", reason) if p.strip()]
        chosen = next((p for p in parts if len(p) >= 15), reason)
        reason = chosen[:220].strip()
        if reason and not reason.endswith((".", "!", "?")):
            reason += "."
        # The number survives a non-English answer; the prose does not.
        if not is_english_only(reason):
            reason = None
            method = f"{method}+non-english-reason-dropped"

    return score, (reason or None), method


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
def build_score_prompt(symbol: str, headlines: Sequence[str], display_name: str | None = None) -> str:
    name = display_name or symbol
    numbered = "\n".join(f"{i}. {h}" for i, h in enumerate(headlines, 1))
    return f"""You are a financial news sentiment rater. You rate ONE company at a time.

Rating scale:
1-2 = very bad news for the share price
3-4 = mildly bad news
5   = neutral, routine, or unclear news
6-7 = mildly good news
8-10 = very good news for the share price

Rules:
- Consider ALL of the headlines together and give ONE overall rating.
- Base the rating only on the headlines given. Do not use outside knowledge.
- Write in ENGLISH ONLY, using only the Latin alphabet. No Chinese characters.
- Answer in exactly two lines, using this format:
SCORE: <single integer 1 to 10>
REASON: <one short sentence>

Example
Company: RELIANCE
Headlines:
1. Jio adds 5G in 50 new cities
2. Retail arm posts 20% profit growth
Answer:
SCORE: 8
REASON: Network expansion and strong retail profit growth both support earnings.

Now rate this company.
Company: {name}
Headlines:
{numbered}
Answer:
"""


def build_retry_prompt(symbol: str, headlines: Sequence[str], display_name: str | None = None) -> str:
    """Deliberately dumber second attempt: one integer, nothing else."""
    name = display_name or symbol
    joined = " | ".join(headlines)
    return f"""News about {name}: {joined}

Is this news good or bad for the {name} share price?
Reply with ONLY one integer from 1 to 10 (1 = very bad, 5 = neutral, 10 = very good).
Do not write any words.
Answer:"""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class LLMClient:
    def __init__(self, cfg, cache_dir: Path | None = None):
        self.cfg = cfg.llm
        self.host = self.cfg["host"]
        self.model = self.cfg["model"]
        self.timeout = float(self.cfg["request_timeout_seconds"])
        self.max_attempts = int(self.cfg["max_attempts"])
        self._client = ollama.AsyncClient(host=self.host)
        # ONE NPU -> one in-flight request. Never remove this.
        # Created lazily: on Python 3.9 an asyncio primitive built outside a
        # running loop binds to the wrong loop and later raises
        # "future attached to a different loop".
        self._slot: Optional[asyncio.Semaphore] = None
        self._cache_path = (cache_dir or Path(".")) / "news_scores.json"
        self._cache = self._load_cache()
        self.available_models: List[str] = []

    # -- cache -------------------------------------------------------------
    def _load_cache(self) -> Dict[str, dict]:
        try:
            with open(self._cache_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._cache, fh, indent=2)
            tmp.replace(self._cache_path)
        except OSError as exc:
            log.warning("Could not persist score cache: %s", exc)

    def _cache_key(self, symbol: str, headlines: Sequence[str]) -> str:
        payload = json.dumps(
            {"m": self.model, "s": symbol.upper(), "h": sorted(h.strip().lower() for h in headlines)},
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> Optional[StockScore]:
        entry = self._cache.get(key)
        if not entry:
            return None
        ttl = float(self.cfg["cache_ttl_hours"]) * 3600
        if time.time() - entry.get("_ts", 0) > ttl:
            return None
        data = {k: v for k, v in entry.items() if not k.startswith("_")}
        try:
            return StockScore(**data)
        except TypeError:
            return None

    def _cache_put(self, key: str, score: StockScore) -> None:
        entry = asdict(score)
        entry["_ts"] = time.time()
        self._cache[key] = entry
        self._save_cache()

    # -- preflight ---------------------------------------------------------
    async def preflight(self) -> str:
        """Verify the runtime is up and resolve an installed model name.

        Fails loudly here rather than producing a report full of
        "Score unavailable" three hours later.
        """
        def _tags():
            return requests.get(f"{self.host}/api/tags", timeout=10)

        try:
            resp = await asyncio.to_thread(_tags)
        except Exception as exc:
            raise LLMUnavailable(f"hailo-ollama unreachable at {self.host}: {exc}") from exc

        if resp.status_code != 200:
            raise LLMUnavailable(f"{self.host}/api/tags returned HTTP {resp.status_code}")

        try:
            models = resp.json().get("models", []) or []
            names = [m.get("name") or m.get("model") for m in models]
            self.available_models = [n for n in names if n]
        except ValueError:
            self.available_models = []

        log.info("Runtime reachable. Models advertised: %s",
                 ", ".join(self.available_models) or "(none reported)")

        if not self.available_models:
            # Some hailo-ollama builds return an empty/absent tag list. Probe
            # the configured model directly instead of refusing to start.
            log.warning("Runtime reported no models; probing '%s' directly.", self.model)
            if await self._probe(self.model):
                return self.model
            raise LLMUnavailable(f"Model '{self.model}' did not respond to a probe request.")

        candidates = [self.model] + list(self.cfg.get("fallback_models") or [])
        for candidate in candidates:
            match = self._match_model(candidate)
            if match:
                if match != self.model:
                    log.warning("Configured model '%s' unavailable; using '%s'.", self.model, match)
                self.model = match
                return match

        raise LLMUnavailable(
            f"None of {candidates} are installed. Available: {self.available_models}"
        )

    def _match_model(self, wanted: str) -> Optional[str]:
        if wanted in self.available_models:
            return wanted
        base = wanted.split(":")[0]
        for name in self.available_models:
            if name == wanted or name.startswith(f"{wanted}:") or name.split(":")[0] == base:
                return name
        return None

    async def _probe(self, model: str) -> bool:
        try:
            out = await self._generate_raw(model, "Reply with the single word OK.", num_predict=8)
            return bool(out and out.strip())
        except Exception as exc:
            log.error("Probe of '%s' failed: %s", model, exc)
            return False

    # -- generation --------------------------------------------------------
    def _get_slot(self) -> asyncio.Semaphore:
        if self._slot is None:
            self._slot = asyncio.Semaphore(1)
        return self._slot

    async def _generate_raw(self, model: str, prompt: str, *, num_predict: int) -> str:
        async with self._get_slot():
            response = await asyncio.wait_for(
                self._client.generate(
                    model=model,
                    prompt=prompt,
                    keep_alive=self.cfg["keep_alive"],
                    options={
                        "temperature": float(self.cfg["temperature"]),
                        "repeat_penalty": float(self.cfg["repeat_penalty"]),
                        "num_predict": int(num_predict),
                    },
                    stream=False,
                ),
                timeout=self.timeout,
            )
        return (response.get("response") or "").strip()

    async def generate(self, prompt: str, *, num_predict: int, label: str = "") -> str:
        """Generate with retry/backoff. Returns "" if every attempt fails."""
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                out = await self._generate_raw(self.model, prompt, num_predict=num_predict)
                if out:
                    return out
                log.warning("[%s] attempt %d returned an empty response.", label, attempt)
            except asyncio.TimeoutError as exc:
                last_error = exc
                log.warning("[%s] attempt %d timed out after %.0fs.", label, attempt, self.timeout)
            except Exception as exc:
                last_error = exc
                log.warning("[%s] attempt %d failed: %s", label, attempt, exc)
            if attempt < self.max_attempts:
                await asyncio.sleep(min(2 ** attempt, 15))
        if last_error:
            log.error("[%s] all %d attempts failed: %s", label, self.max_attempts, last_error)
        return ""

    # -- the public scoring entry point ------------------------------------
    async def score_stock(
        self,
        symbol: str,
        headlines: Sequence[str],
        *,
        display_name: str | None = None,
        use_cache: bool = True,
    ) -> StockScore:
        """Aggregate-score ONE stock across ALL of its headlines.

        Headlines beyond ``max_headlines_per_call`` are split into chunks. Each
        chunk is scored by the model and the chunk scores are averaged *in
        Python* - the model is never asked to do arithmetic, because that is
        exactly where a 1.5B model invents numbers.
        """
        headlines = [h for h in (h.strip() for h in headlines) if h]
        if not headlines:
            return StockScore(symbol, None, "No headlines matched this stock.",
                              "unscored", "no-input", 0)

        key = self._cache_key(symbol, headlines)
        if use_cache:
            cached = self._cache_get(key)
            if cached:
                log.info("  %-11s cached -> %s", symbol, cached.compact())
                return cached

        chunk_size = max(1, int(self.cfg.get("score_chunk_size") or 6))
        chunks = [list(headlines[i:i + chunk_size]) for i in range(0, len(headlines), chunk_size)]

        chunk_scores: List[int] = []
        reasons: List[str] = []
        methods: List[str] = []

        for idx, chunk in enumerate(chunks, 1):
            label = f"score {symbol} {idx}/{len(chunks)}"
            raw = await self.generate(
                build_score_prompt(symbol, chunk, display_name),
                num_predict=int(self.cfg["score_num_predict"]),
                label=label,
            )
            score, reason, method = parse_score(raw)
            if raw:
                log.debug("[%s] raw: %s", label, raw.replace("\n", " | ")[:300])

            if score is None:
                # Second, dumber attempt: ask for a bare integer only.
                log.info("  %-11s primary parse failed (%s); retrying number-only.", symbol, method)
                raw2 = await self.generate(
                    build_retry_prompt(symbol, chunk, display_name),
                    num_predict=12,
                    label=label + " retry",
                )
                score, reason2, method2 = parse_score(raw2)
                if score is not None:
                    method = f"retry:{method2}"
                    # The primary attempt's "reason" is unusable noise when it
                    # failed to produce a score at all - do not carry it over.
                    reason = reason2 or ("Rated from the headlines on a follow-up "
                                         "attempt; the model returned no rationale.")

            if score is not None:
                chunk_scores.append(score)
                methods.append(method)
                if reason:
                    reasons.append(reason)

        if not chunk_scores:
            result = StockScore(
                symbol, None,
                "The local model did not return a parseable rating for these headlines.",
                "unscored", "all-attempts-failed", len(headlines),
            )
        else:
            # Deterministic aggregation in Python. Never delegated to the LLM.
            aggregate = round(sum(chunk_scores) / len(chunk_scores))
            aggregate = min(10, max(1, aggregate))
            spread = max(chunk_scores) - min(chunk_scores)
            confidence = "high" if (len(chunk_scores) == len(chunks) and spread <= 2) else "low"
            usable = [r for r in reasons if is_english_only(r)]
            reason = (usable[0] if usable
                      else f"Aggregate of {len(chunk_scores)} rated headline group(s); "
                           f"no usable English rationale was returned.")
            result = StockScore(
                symbol, aggregate, reason, confidence,
                "+".join(dict.fromkeys(methods)), len(headlines), chunk_scores,
            )

        log.info("  %-11s -> %s", symbol, result.compact())
        if use_cache:
            self._cache_put(key, result)
        return result
