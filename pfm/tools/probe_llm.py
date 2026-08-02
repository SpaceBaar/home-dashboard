#!/usr/bin/env python3
"""Diagnose the hailo-ollama runtime and the scoring prompt end to end.

Run this on the Pi whenever scores go missing. It prints, in order:
  1. what /api/tags advertises,
  2. the resolved model,
  3. the exact prompt sent for one stock,
  4. the RAW model output,
  5. what the tiered parser made of it, and how long it took.

Usage:
    python tools/probe_llm.py
    python tools/probe_llm.py --symbol TATAPOWER
    python tools/probe_llm.py --model llama3.2:3b
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm import LLMClient, LLMUnavailable, build_score_prompt, parse_score  # noqa: E402
from pfm_config import CACHE_DIR, load_config, setup_logging                # noqa: E402

SAMPLE = {
    "TATAPOWER": [
        "Juniper Green Energy, Tata Power sign PPA for 85 MW hybrid project in Maharashtra",
        "Tata Power commissions 200 MW solar capacity in Rajasthan",
    ],
    "IDEA": [
        "Voda Idea receives Rs 26.8 crore notice from DoT over spectrum rollout; "
        "telco says operations remain unaffected",
    ],
    "SBIN": [
        "Q1 Results This Week: SBI, Airtel, LIC, Titan and 550+ companies to report earnings",
    ],
}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=None, help="probe a single symbol")
    parser.add_argument("--model", default=None, help="override the configured model")
    args = parser.parse_args()

    setup_logging(verbose=True)
    cfg = load_config()
    if args.model:
        cfg.llm["model"] = args.model

    client = LLMClient(cfg, cache_dir=CACHE_DIR)
    print(f"\nHost: {client.host}")
    print(f"Configured model: {cfg.llm['model']}")

    try:
        resolved = await client.preflight()
    except LLMUnavailable as exc:
        print(f"\nPREFLIGHT FAILED: {exc}")
        print("\nChecklist:")
        print("  systemctl status hailo-ollama")
        print(f"  curl {client.host}/api/tags")
        print("  then set llm.model in config.json to a name that appears there")
        return 1
    print(f"Resolved model: {resolved}\n")

    targets = {args.symbol: SAMPLE.get(args.symbol, ["Placeholder headline for a probe."])} \
        if args.symbol else SAMPLE

    failures = 0
    for symbol, headlines in targets.items():
        print("=" * 72)
        prompt = build_score_prompt(symbol, headlines)
        print(f"--- PROMPT for {symbol} ({len(prompt)} chars) ---\n{prompt}")

        started = time.time()
        raw = await client.generate(prompt, num_predict=int(cfg.llm["score_num_predict"]),
                                   label=f"probe {symbol}")
        elapsed = time.time() - started

        print(f"--- RAW OUTPUT ({elapsed:.1f}s, {len(raw)} chars) ---\n{raw!r}\n")
        score, reason, method = parse_score(raw)
        print(f"--- PARSED --- score={score} method={method}\nreason={reason!r}\n")
        if score is None:
            failures += 1
            print("This is the failure mode to investigate. The parser accepts "
                  "'SCORE: n', 'n/10', 'Rating: n out of 10', a bare integer, and "
                  "plain bullish/bearish wording. If none of those appear above, the "
                  "model is ignoring the format entirely - try a larger model via "
                  "--model, or raise llm.score_num_predict.\n")

    print("=" * 72)
    print(f"{len(targets) - failures}/{len(targets)} probes produced a usable score.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
