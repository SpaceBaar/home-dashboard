# pfm — Personal Finance Manager agent

A nightly agent that reads your Indian holdings over the **Zerodha Kite** MCP
bridge and your US holdings over the **INDmoney** MCP bridge, screens the news
for the stocks you actually own, rates that news with a local LLM on a
Raspberry Pi 5 + AI HAT+ (hailo-ollama), and writes a markdown report, a JSON
sidecar, a browsable web view and a Telegram summary.

## Design principle

**Numbers are computed. Only prose is generated.**

The model is used for exactly two things:

1. Rating the news for **one stock at a time**, given all of that stock's
   headlines in a single prompt.
2. Writing a two-paragraph commentary — which is then machine-validated against
   the computed figures and discarded if it does not match.

Everything else — totals, per-holding P&L, percentages, winners, losers,
concentration, aggregation of chunk scores — is plain Python. A 1.5B model is
never asked to do arithmetic, because that is precisely where it invents things.

## Pipeline

```
Kite MCP  get_holdings         ─┐  India book, INR
INDmoney  networth_holdings    ─┤  US book, USD
                                │
                                ▼
brokers.normalise_* ───────────── one common holding shape, per-row currency
        │
        ▼
portfolio.build_fact_sheet ──────── every figure, computed once, totals reconcile
        │
        ▼
news.collect_articles ───────────── RSS + Atom, exclusion-aware attribution,
        │                           near-duplicate collapse
        ▼
news.score_all ──────────────────── ONE llm call per stock → one aggregate score
        │
        ▼
report.build_narrative ──────────── prose, validated, else deterministic template
        │
        ▼
report.render_report / write_report → reports/portfolio_analysis_YYYY-MM-DD.md
report.build_payload / write_payload → reports/portfolio_analysis_YYYY-MM-DD.json
        │                                        │
        ▼                                        ▼
notify.Telegram ─────────────────── chunked   web.py ── browser, by date
                                    summary
```

Each run writes two files: the markdown report for reading, and a JSON sidecar
with the same computed figures in structured form. The web view reads the JSON,
so the browser and the report can never disagree.

## Files

| File | Responsibility |
| --- | --- |
| `agent.py` | Orchestrator, both MCP sessions, scheduler, CLI, expense listener |
| `brokers.py` | Kite and INDmoney providers, holdings normalisation, US news extraction |
| `pfm_config.py` | Config load/merge/validate, path resolution, logging |
| `portfolio.py` | Holdings parsing and all portfolio mathematics |
| `news.py` | Feed fetching, symbol attribution, dedup, per-stock scoring |
| `llm.py` | hailo-ollama client, preflight, tiered score parser, score cache |
| `report.py` | Fact sheet → markdown, narrative validation, fallback template |
| `notify.py` | Telegram with chunking, retries and token redaction |
| `web.py` | Standalone read-only report browser (own process, own port) |
| `static/` | Stylesheet and table-sorting script for the web view |
| `tests/test_pipeline.py` | Full offline harness — no Pi, no model, no network |
| `tests/test_web.py` | Offline tests for the web view, including live HTTP routes |
| `tests/test_us_book.py` | INDmoney normalisation, multi-currency math, US news |
| `tools/probe_indmoney.py` | Capture INDmoney's real response shapes |
| `tools/probe_llm.py` | On-Pi diagnosis of the runtime and the scoring prompt |
| `tools/check_telegram.py` | Credential check |

## Setup

```bash
cd pfm
pip install -r requirements.txt
cp ../.env.example .env   # then set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID
```

Optional environment variables (`pfm/.env`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | — | bot token |
| `TELEGRAM_CHAT_ID` | — | destination chat |
| `NPX_PATH` | `npx` | absolute path to npx for the MCP bridges |
| `KITE_MCP_URL` | `https://mcp.kite.trade/mcp` | Kite MCP endpoint |
| `INDMONEY_MCP_URL` | `https://mcp.indmoney.com/mcp` | INDmoney MCP endpoint |

## Running

```bash
python agent.py --preflight        # check runtime, model and config, then exit
python agent.py --dry-run --no-llm # offline report from fixture holdings
python agent.py --dry-run          # offline holdings, real model, real feeds
python agent.py --once             # one real run against both brokers, then exit
python agent.py --once --no-us     # India book only
python agent.py --daemon           # service mode (systemd)
python tests/test_pipeline.py      # full offline test harness
python tests/test_web.py           # offline tests for the web view
python tests/test_us_book.py       # INDmoney normalisation and multi-currency math
python tools/probe_llm.py          # why is the model not scoring?
python tools/probe_indmoney.py     # what does INDmoney actually return?
```

Install the services:

```bash
sudo cp finance-agent.service pfm-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now finance-agent pfm-web
journalctl -u finance-agent -f
journalctl -u pfm-web -f
```

## US book via INDmoney

INDmoney publishes a read-only MCP server at `https://mcp.indmoney.com/mcp`
(OAuth 2.1 + PKCE, 14 tools, no write capability anywhere in it). This project
uses three of those tools:

| Tool | Purpose here |
| --- | --- |
| `networth_holdings` | Per-position US rows: units, price, invested, value, P&L, XIRR |
| `get_us_stocks_details` | US quotes and, importantly, headlines with INDmoney's own sentiment |
| `user_watchlist` | Optional source for watchlist tickers |

### First-time sign-in

`mcp-remote` runs the OAuth flow and caches the token under `~/.mcp-auth`, the
same mechanism the Kite bridge already uses. On a headless Pi it prints a URL:

```bash
cd pfm
python tools/probe_indmoney.py --list-only
```

Open the printed URL on your phone or laptop, complete OTP + MPIN **on
INDmoney's own page**, and approve the consent screen. Your credentials never
pass through this code. Once cached, the daemon refreshes silently.

### What the API actually returns

Captured from the live server on 2026-08-02. These findings are encoded in
`brokers.py`; the structures live in `tests/fixtures/indmoney_us_shapes.json`
with the figures replaced.

**US holdings are already in rupees.** This is the opposite of the obvious
assumption and the single most dangerous detail. For the captured SpaceX row,
`0.05061407 units × 10340.67 = 523.38 market_value`, and the implied average of
18,852 per unit only makes sense as ₹ (≈ $214 at 88) — $18,852 a share does not.
`get_us_stocks_details`, by contrast, quotes in USD (AAPL at 308.91). Treating
the holdings as dollars would have multiplied the US book by about 88.

**There is no ticker field.** Rows carry only `investment` (a long name like
`"Space Exploration Technologies Corp. Class A Common Stock"`) and
`investment_code`. Tickers are resolved through `lookup_ind_keys`; if that fails,
a label is derived from the name, **flagged as derived**, and the failure appears
in the data-quality section — because an unresolved ticker also means news
matching will miss that holding.

**An unknown cost basis brings a fake P&L.** `invested_amount` arrives as the
string `"unknown"`, and INDmoney then fills `total_pnl` with the market value and
`pnl_per` with `0`. Taken at face value that reports a 100% gain, so both are
discarded.

**Indian rows carry `asset_type: "STOCK"`**, not `IND_STOCK`. That is what keeps
INDmoney's mirror of your Zerodha holdings out of the US book — otherwise every
Indian position would be counted twice.

**Quotes are keyed by symbol**, not returned as a list: `{"AAPL": {entity_basic:
{…}, entity_stats: {…}}}`.

Field lookup is by canonical key, so `unitPrice`, `unit_price` and `Unit Price`
all resolve together. A row that cannot be interpreted is **excluded with a
diagnostic naming the fields it saw**, never defaulted to zero.

### Currency

Because INDmoney pre-converts, the US book is rupee-denominated and the combined
total needs no FX rate at all. `portfolio.usd_inr_rate` stays relevant only for a
genuinely USD-priced row, should the API ever start sending one — and rates
outside 60–140 are rejected as a misread field rather than a currency crisis.

### Holdings with no cost basis

Rows without an invested amount:

- show `—` for invested, average price, P&L and P&L %, never `0`;
- still count their full current value toward portfolio value;
- mark book-level invested and P&L with `*` and a footnote, since those cover
  only the costed subset and so will not equal value minus invested;
- are listed in the data-quality section.

The narrative changes shape too: with uncosted rows it says *"Cost basis is
available for 20 of 22 holdings"* rather than putting value and invested side by
side as though they described the same set.

### News and sentiment

`get_us_stocks_details` does **not** return headlines in its baseline reply — the
confirmed response has only `entity_basic` and `entity_stats`. It takes a
`segments` parameter whose valid tokens are undocumented, so the provider tries
several, falls back to the baseline quote, and records in data quality that US
news came from RSS only. `tools/probe_indmoney.py` sweeps candidate `segments`
values and tells you which one works; put it at the head of
`IndmoneyProvider.NEWS_SEGMENT_CANDIDATES`.

The quote reply is still useful: `networth_holdings` has no day-change field for
US rows, so `day_change_percentage` is taken from the live quote. A percentage
move is currency-agnostic, so it attaches to a rupee-denominated holding without
conversion.

When headlines do arrive they are merged with the same near-duplicate guard, then
scored by **your local model** so every score in the report shares one scale.
INDmoney's own sentiment is recorded beside ours, never blended in. A gap of 3
points or more is surfaced in data quality along with the scale assumption made —
a label maps cleanly, but a bare number is only converted when its range is
unambiguous.

### When the token expires

A stale INDmoney token never blocks the run. The India book reports as normal,
the US section is marked unavailable in data quality, and Telegram gets one
message with the re-auth command. Pass `--no-us`, or set `indmoney.enabled` to
`false`, to skip the US book entirely.

## Web view

A separate, read-only process that serves the report archive over HTTP. It has
nothing to do with the Node home-dashboard app — different process, different
port, no shared code or assets. Stdlib only, so there is nothing extra to
install.

```bash
python web.py                 # http://<pi>:7373/
python web.py --port 8080
python web.py --once /        # render one route to stdout, for debugging
```

| Route | Purpose |
| --- | --- |
| `/` | Latest report, with the value-over-time chart |
| `/r/<date>` | A specific date, e.g. `/r/2026-08-02` |
| `/raw/<date>.md` | The markdown source |
| `/api/reports` | JSON index of every report |
| `/api/reports/<date>` | The full structured payload |
| `/healthz` | Liveness probe |

Notes:

- The reports directory is read on **every request**, so a new report appears
  without restarting anything.
- Reports written before the JSON sidecar existed still show up. They render
  from their markdown and are marked `legacy` in the archive list.
- Legacy reports left in `pfm/` itself (rather than `pfm/reports/`) are also
  picked up, so nothing already on the Pi is lost.
- Payloads are written atomically via a temporary file, so the browser never
  reads a half-written report.
- There is **no authentication**. Bind it to your LAN or VPN only — do not port
  forward it. Set `web.host` in `config.json` to `127.0.0.1` if you would rather
  reach it exclusively through an SSH tunnel or a reverse proxy.
- The holdings table sorts client-side. Without JavaScript the page is still
  fully readable, just in the server's default order (largest position first).

## Configuration notes

`config.json` sections that matter most:

- **`llm.repeat_penalty`** — keep this near `1.0`. A high value penalises the
  model for re-emitting the literal `SCORE:` / `REASON:` tokens the format
  requires, which suppresses the very output the parser needs.
- **`llm.model` / `llm.fallback_models`** — verified against `/api/tags` at
  startup. If the configured model is absent, the first available fallback is
  used and a warning is logged.
- **`llm.cache_ttl_hours`** — scores are cached on a hash of
  `(model, symbol, headline set)`, so re-running the same day is free and
  produces identical output.
- **`tracking.keywords`** — a symbol you hold with no entry here is matched on
  its own ticker, provided the ticker is at least four characters. Shorter
  tickers need explicit keywords or they generate too many false positives.
- **`tracking.exclude_phrases`** — phrases stripped from article text before
  matching, so an *SBI Cards* story is not filed as SBIN news.
- **`tracking.watchlist`** — symbols you do not hold. Their news is still
  gathered and scored, but under a clearly labelled "not held" heading.

## What changed, and why

The 2026-08-02 report showed `Score unavailable` for all seven stocks that had
news, and a commentary section containing companies that are not in the
portfolio. Root causes and fixes:

| Defect | Cause | Fix |
| --- | --- | --- |
| Every stock unscored despite having news | Five stocks shared one prompt with a `5 × 45 + 80` token budget, so output was truncated; the parser also required a `Stock: X` line immediately followed by `Score: N` and dropped everything else | One LLM call per stock; a six-tier permissive parser; a number-only retry; an explicit `unscored` sentinel when both fail |
| Format tokens suppressed | `repeat_penalty: 1.3` penalised repeating `SCORE:` / `REASON:` | Lowered to `1.05` |
| Invented tickers (`ADANIPORTS`, `IDEAS`, `TATAPOWERS`, `LCCI`, `BSNL`) | Free-form synthesis over a model-written intermediate summary | Removed the map/reduce summarisation of holdings; the model now sees a small computed fact sheet and its output is validated against the allowed symbol set |
| `TATAPOWERS: Down 8644%` | A rupee P&L figure read as a percentage | Any percentage above 1000 in magnitude is rejected outright |
| `total return of +83.7%` | A per-stock gain presented as the portfolio return | Portfolio-level percentages are cross-checked against the computed total |
| RBA's `-42.3%` attributed to LICI | Numbers restated by the model | The report's figures never pass through the model at all |
| Percentages that did not reconcile with rupee P&L | Broker `pnl` mixed with derived percentages | One consistent derived basis; broker disagreements are flagged in a data-quality section |
| Pledged and T+1 holdings shown as zero | Only `quantity` was read | `quantity + t1_quantity + collateral_quantity`, with audit flags |
| The same wire story counted several times | No deduplication | Exact and 90%-similarity title dedup per symbol |
| `SBI Cards` counted as SBIN | Substring matching, first-match-wins | Word-boundary matching, exclusion phrases, multi-symbol attribution |
| AAPL/TSLA news scored despite not being held | News driven by `config.json` alone | Driven by live holdings; the watchlist is separate and labelled |
| Feeds silently contributing nothing | Only `<item>` was parsed; failures were swallowed | Atom support, retries, and a feed-health line in the data-quality section |
| Bot token written to `journalctl` | Exception text contains the request URL | Redacted before logging |
| Overlapping scheduled runs, swallowed exceptions | `asyncio.create_task` with no guard or error handling | Run lock, done-callbacks, Telegram alerts on failure |
| Config read from the current working directory | Relative `open('config.json')` | All paths resolved from `__file__` |

## Verification

`tests/test_pipeline.py` runs the whole pipeline with a fake model and fixture
data. Each assertion maps to a defect above, including a case that feeds the
verbatim hallucinated paragraph from the 2026-08-02 report through the validator
and asserts it never reaches the published output.

`tests/test_web.py` builds a temporary archive of several JSON reports plus one
legacy markdown-only report, then exercises every route against a real HTTP
server on a loopback port — including payload arithmetic, the single-data-point
chart case, HTML escaping of report content, and static path traversal.

`tests/test_us_book.py` covers the INDmoney path: several plausible response
shapes including camelCase keys, nested quote objects, currency-formatted
strings and unknown cost bases; USD/INR sanity rejection; the guarantee that a
dollar figure is never added to a rupee total; and sentiment-disagreement
flagging.

All three suites need no Pi, no model, no broker and no network:

```bash
python tests/test_pipeline.py && python tests/test_web.py && python tests/test_us_book.py
```
