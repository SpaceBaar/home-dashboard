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

## Daily schedule

Kite access tokens do not survive the day, so the login link is pushed **shortly
before the analysis**, not in the morning:

| Time | What happens |
| --- | --- |
| `22:45` | Probe the Kite session. If it is dead, push the login link with the deadline. If it is still alive, do nothing — no pointless notification. |
| `23:00` | Run the analysis. If the login has not happened yet, poll every 2 minutes for up to 20 minutes rather than losing the night. |

Controlled by `agent_settings`:

| Key | Default | Purpose |
| --- | --- | --- |
| `analysis_time` | `23:00` | when the report runs |
| `login_lead_minutes` | `15` | how far ahead of the run to prompt |
| `auth_grace_minutes` | `20` | how long the run waits for a late login |
| `auth_retry_interval_minutes` | `2` | how often it re-probes while waiting |
| `login_time` | `null` | optional extra morning link; `null` disables it |

`login_lead_minutes` is subtracted from `analysis_time` and wraps correctly over
midnight, so an analysis at `00:10` prompts at `23:55` the previous evening.

### Non-trading days

NSE, BSE and the US markets are all shut at the weekend, so a Saturday or Sunday
run would mostly reproduce the previous report. The run is skipped when **all** of
these hold:

1. today is in `weekend_days`,
2. a previous run succeeded, giving figures to compare against,
3. the last run did not fail — a failure is retried, not skipped,
4. neither the total value **nor** the holdings fingerprint has moved.

Holdings are still fetched, because that is the only way to check condition 4 —
two MCP calls. What is avoided is the RSS scan and the per-stock LLM calls, which
are the fifteen expensive minutes.

Two details worth knowing:

- **Saturday will often still run.** A 23:00 IST Saturday sees Friday's US
  *closing* prices, whereas Friday at 23:00 IST saw that session still open. So
  the US book legitimately moves overnight and a report gets produced. Sunday is
  the reliably flat one. Skipping on the value rather than on the calendar is what
  makes this come out right.
- **The fingerprint is checked as well as the total.** A buy and a sell that
  happen to net out, or T+1 quantities settling over the weekend, leave the total
  unchanged while the positions behind it differ. That runs.

State lives in `state/last_run.json`, which keeps the latest status and the
baseline separately — a Saturday skip must not make Sunday run just because the
most recent *run* was a skip rather than a success.

```bash
python agent.py --show-state   # what is recorded, and tonight's decision
python agent.py --once --force # run anyway
```

| Key | Default | Purpose |
| --- | --- | --- |
| `skip_unchanged_weekends` | `true` | the whole rule; `false` restores nightly runs |
| `weekend_days` | `["saturday","sunday"]` | which days qualify |
| `notify_on_skip` | `true` | one short Telegram line, so silence is never a mystery |

## Environment

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
18,852 per unit only makes sense as ₹ (≈ $214) — $18,852 a share does not.
`get_us_stocks_details`, by contrast, quotes in USD (AAPL at 308.91). Treating
the holdings as dollars would have multiplied the US book by ~95.

**`investment_code` equals `entity_basic.mycroft_id`.** Apple is `118186` in both
the holdings row and the quote reply. That turns ticker resolution into an exact
identifier join rather than a name search, and it doubles as a correctness check:
if a name-derived ticker disagrees with the id, the id wins and the mismatch is
reported.

### Identifiers are never invented

`networth_holdings` carries no ticker — only `investment` (a long name like
`"Space Exploration Technologies Corp. Class A Common Stock"`) and
`investment_code`. There is exactly **one** ticker source:

> INDmoney's own `entity_basic.symbol`, joined on its own `investment_code` ==
> `mycroft_id`. Apple is `118186` in both, so the match is an exact identifier
> join, not a guess.

Where that join finds nothing, the holding keeps **INDmoney's instrument code**
as its identifier and is displayed under **INDmoney's instrument name**. Nothing
is abbreviated, inferred or filled in from a config guess. So SpaceX appears as:

```
| Space Exploration Technologies Corp. Class A Common Stock | 0.0506 | … |
```

and the data-quality section says INDmoney supplied no ticker for it.

Earlier versions invented `SPACEEXPLORA` and `ALPHABETCAPI` as stand-in symbols
and carried a `tracking.instrument_tickers` table of hand-written guesses. Both
are gone: a fabricated symbol looks authoritative, and the first thing it breaks
is your ability to tell whether a holding is actually missing.

**`lookup_ind_keys` is not used at all.** It searches *Indian* instruments: asked
about `"Alphabet"` it returns *Mirae Nifty200Alpha30*, *NIFTY 50* and *Godrej
Consumer Products*; asked about `"Space Exploration Technologies"` it returns
*Space Incubatrics Technologies*. The identifiers it hands back — `INDS02693`,
`INDI00012` — are internal Indian keys, not tickers.

One guard remains as a backstop: any candidate ticker must match
`^[A-Z]{1,5}(\.[A-Z])?$` and must not begin `INDS`/`INDI`/`INDM`, or it is
refused and logged.

Keywords in `tracking.keywords` are **news search terms only**. They never define
an identifier.

Anything still unresolved keeps a label derived from its name, is **flagged as
derived**, and is named in the data-quality section — an unresolved ticker also
means news matching will miss that holding.

**Quote batches are retried per symbol on failure.** `get_us_stocks_details`
takes up to ten symbols, and one unrecognised ticker fails the entire call. That
is how a genuine US holding disappeared: `SPCX` poisoned the batch, `AAPL`'s
`mycroft_id` went with it, and Apple was left labelled `APPLE` rather than
`AAPL` — present in the data but invisible to anyone scanning for the ticker.
Known-Indian tickers are also kept out of the US endpoint entirely.

**`lookup_ind_keys` returns HTTP 414 for long names.** It puts names in a query
string, and a two-name batch containing the 57-character SpaceX name was rejected
with `API returned 414: /v4/global-search/`. Names are therefore stripped of
boilerplate suffixes (`Class A Common Stock`, `Inc.`) and sent one per call.

**An unknown cost basis brings a fake P&L.** `invested_amount` arrives as the
string `"unknown"`, and INDmoney then fills `total_pnl` with the market value and
`pnl_per` with `0`. Taken at face value that reports a 100% gain, so both are
discarded.

**Indian rows carry `asset_type: "STOCK"`**, not `IND_STOCK`. That is what keeps
INDmoney's mirror of your Zerodha holdings out of the US book — otherwise every
Indian position would be counted twice.

### How the book is decided

`asset_type` alone, matched exactly against `_ASSET_TYPE_BOOK` in `brokers.py`.
Two rules, both learned from a real misfiling:

- **`assetclass_l2` is never consulted.** It is a sector-ish label — `Gold`,
  `Global Equity`, `Retirement` — and matching it put the Zerodha Gold ETF
  (`GOLDCASE`) in the US section.
- **An unrecognised or absent `asset_type` is excluded, never assumed to be US.**
  The earlier version defaulted to US whenever the field was missing, which both
  imported Indian holdings into the US book and, because exclusions were silent,
  let a genuine US holding disappear without a word.

Every exclusion now produces a line in the report's data-quality section naming
the instrument and the reason, so a missing holding is visible rather than
inferred. On top of that, `portfolio.build_fact_sheet` refuses to let one symbol
appear in both books: Kite wins, the value is counted once, and the collision is
disclosed.

To see the decision for each row from live data:

```bash
python tools/probe_indmoney.py
```

It prints a per-row table of instrument, `asset_type`, `assetclass_l2`, broker and
resulting book, flags rows for which INDmoney supplies no ticker, and dumps every
row rather than only the first.

**Quotes are keyed by symbol**, not returned as a list: `{"AAPL": {entity_basic:
{…}, entity_stats: {…}}}`.

Field lookup is by canonical key, so `unitPrice`, `unit_price` and `Unit Price`
all resolve together. A row that cannot be interpreted is **excluded with a
diagnostic naming the fields it saw**, never defaulted to zero.

### Currency

Because INDmoney pre-converts, the US book is rupee-denominated and the combined
total needs no FX rate at all.

A rate is still derived, from the data itself: a holding's `unit_price` is in
rupees while the live quote for the same ticker is in dollars, so their ratio is
the rate INDmoney applied. AAPL at 29,476.19 against 308.91 gives 95.42, TSLA
gives 95.47, and the median across every ticker with both figures is recorded in
the report. No external rate source, no configured guess.

`portfolio.usd_inr_rate` overrides that, and matters only for a genuinely
USD-priced row should the API ever start sending one. Rates outside 60–140 are
rejected as a misread field rather than a currency crisis.

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

`get_us_stocks_details` does **not** return headlines in its baseline reply — that
has only `entity_basic` and `entity_stats`. News needs the `segments` parameter,
whose valid tokens are undocumented. Confirmed by sweep:

| `segments` | Result |
| --- | --- |
| `["news","analyst"]` | adds `news` **and** `analyst_forecast` — used |
| `["news"]` | adds `news` — fallback |
| `["NEWS"]`, `["all"]`, `["overview","news"]`, `["news","analyst_consensus"]` | rejected |

`tools/probe_indmoney.py` re-runs that sweep if the API changes. If no value
works, the provider falls back to the baseline quote and records in data quality
that US news came from RSS only.

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

### Hiding amounts

**Screen sharing and screenshots cannot be detected.** Every interface in the
Screen Capture API — `getDisplayMedia`, `CaptureController`, `CropTarget`,
`displaySurface`, `cursor` — exists for the page *doing* the capturing. There is
no inverse, deliberately, because it would be a fingerprinting vector. There is
no screenshot API at all. `FLAG_SECURE` on Android and `UIScreen.isCaptured` on
iOS are native-app only. Any library claiming otherwise is guessing.

So this is a privacy toggle with heuristic triggers, not detection:

| Trigger | Reliability |
| --- | --- |
| The **Hide amounts** button, or pressing `p` | Reliable |
| **Focus loss** — starting a screen share, alt-tabbing into a call, opening a snipping tool | Reliable, and the best available proxy |
| **Tab hidden**, via `visibilitychange` | Reliable |
| **Printing / print-to-PDF**, via `@media print` and `beforeprint` | Reliable, and always applied regardless of the toggle |
| **Idle** for `idle_seconds` | Reliable |
| **Screenshot keys** — `PrintScreen`, `Cmd+Shift+3/4/5`, `Win+Shift+S`, `Ctrl+P` | **Best effort only.** The OS usually swallows these before the page sees them |

Behaviour:

- Hold **Shift** to peek at everything, or press and hold a single figure.
- Money only. Percentages, tickers, quantities and news stay readable, so the
  page is still usable while blurred.
- Your choice persists in `localStorage`; auto-triggers hide amounts without
  overwriting that preference.
- A figure the broker never supplied stays an em dash rather than a blurred
  smudge — blurring it would imply a value exists.
- The blur is a real CSS filter, so a screenshot captures blurred pixels. The
  text is still in the DOM: this defends against shoulder-surfing, screen
  sharing and screenshots, **not** against someone with devtools on your machine.
- Chart tooltips are SVG `<title>` elements, which CSS cannot blur, so each point
  carries an amount-free alternate that is swapped in.

Configure under `web.privacy` in `config.json`. Set `blur_by_default` to `true`
and amounts are hidden server-side on every load, so they never flash visible
before the script runs.

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
| Zerodha Gold ETF (`GOLDCASE`) filed under US stocks | The book was decided by the first populated field among `asset_type`, `asset_class`, `assetclass_l2` — so a row with an empty `asset_type` fell through to `assetclass_l2`, and `GLOBAL EQUITY` was in the US match list. A missing `asset_type` also defaulted to US | Only `asset_type` decides, matched exactly. Unknown or absent means excluded and reported, never assumed. Plus a cross-book guard so one symbol cannot appear in both, with Kite winning |
| Invented symbols (`SPACEEXPLORA`, `ALPHABETCAPI`) and hand-written ticker guesses in config | I filled the gap where INDmoney supplies no ticker instead of reporting it | Removed. The only ticker source is INDmoney's own `entity_basic.symbol` joined on its own `investment_code`; otherwise the holding shows INDmoney's instrument name and the gap is reported |
| A US holding at risk of being labelled `INDS02693` | `lookup_ind_keys` searches *Indian* instruments and returns internal keys, not tickers | That endpoint is not used at all; any candidate must still match a US ticker shape |
| A US holding missing from the report entirely | Rows filtered out by the book check were dropped with a bare `continue` — no log, no data-quality line. A holding could also appear under a derived label (`APPLE`) rather than its ticker (`AAPL`) and read as absent | Every exclusion is named in data quality with its reason; unresolved tickers are called out explicitly; the probe prints all rows and the decision for each, instead of only element `[0]` |
| Commentary published in Chinese | qwen2.5 is a Chinese-origin model and ignores an English instruction now and then | Non-Latin script is a validation failure like any other: retry with a stricter prompt, then fall back to the deterministic template. Score rationales get the same treatment — the number survives, the prose does not. The rejection notice names the script rather than quoting the characters, so the diagnostic cannot reintroduce them |
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
