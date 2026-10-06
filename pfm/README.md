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
| `bridges.py` | Restartable MCP connections; forwards OAuth sign-in URLs to Telegram |
| `commands.py` | Telegram command parsing, chat-ID authorisation and routing |
| `goals.py` | Amortisation, prepayment modelling, liquidity tiers, goal storage |
| `web.py` | Standalone report browser and the goals page (own process, own port) |
| `static/` | Stylesheet, table sorting, and the browser-side goals calculator |
| `data/` | Goals you typed. The only directory here that is not regenerable |
| `tests/test_pipeline.py` | Full offline harness — no Pi, no model, no network |
| `tests/test_web.py` | Offline tests for the web view, including live HTTP routes |
| `tests/test_us_book.py` | INDmoney normalisation, multi-currency math, US news |
| `tests/test_commands.py` | Telegram commands, authorisation, auth-URL capture |
| `tests/test_goals.py` | Amortisation against the spreadsheet, affordability, browser parity |
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

### Telegram commands

The login link expires faster than you can always get to it, so the bot takes
commands. **Only the chat in `TELEGRAM_CHAT_ID` is obeyed** — the bot token is a
bearer credential, and these commands start real work and hand out sign-in links.
Anything from another chat is logged and silently ignored, including expense
messages, so a stranger cannot write lines into your expense file either.

| Command | What it does |
| --- | --- |
| `/login` | Probes the Kite session first. Sends a fresh link only if it has actually expired, so you never get a pointless one. Aliases: `/kite`, `/zerodha` |
| `/indmoney` | Reconnects the US book. Aliases: `/us`, `/ind` |
| `/indmoney force` | Also clears the cached INDmoney credentials first, forcing a full OAuth sign-in |
| `/code <address>` | Finishes a sign-in you approved on another device. Aliases: `/callback`, `/auth` |
| `/status` | Both broker sessions, the last run, and whether tonight's run will go ahead or be skipped |
| `/run` | Runs the analysis now, ignoring the weekend skip. Returns immediately; the summary arrives when it finishes |
| `/help` | The list above |

Anything that is not a command is still logged as an expense, as before.

**Sign-in URLs now reach you.** `mcp-remote` prints
`Please authorize this client by visiting: <url>` to stderr, which on a headless
Pi means journalctl — useless if nobody is tailing it. Both bridges tee that
stream and forward any authorisation URL straight to Telegram.

**Approving a sign-in on your phone: use `/code`.** The link arrives on
Telegram, so you will usually open it on a phone — and then the browser is
redirected to `http://localhost:<port>/oauth/callback`, which on a phone means
*the phone itself*, and it refuses to connect. This is not a misconfiguration
and it cannot be fixed by pointing the callback elsewhere: `mcp-remote` binds
its callback server to `127.0.0.1`, hard-coded, and its `--host` flag only
rewrites the `redirect_uri` it registers — so aiming it at the Pi's LAN address
would leave nothing listening there.

The sign-in itself did succeed, though, and the authorisation code is sitting
in the failed page's address bar. Copy that whole address and send it back:

```
/code http://localhost:3335/oauth/callback?code=...&state=...
```

The Pi then makes the request the phone could not, from the one machine where
the callback server exists. A bare code works too. The `state` is matched
against the sign-in in progress, so a code cannot be delivered to the wrong
broker, and the relay refuses any callback address that is not loopback — the
`redirect_uri` comes from a page we did not write, and it is not going to
become a way to make the Pi fetch arbitrary URLs.

If you approve the link in a browser on the Pi itself, none of this applies:
the callback reaches the server directly and `/status` will show the session.

**Both bridges are restartable.** The MCP connections used to live inside nested
`async with` blocks in the main loop, so reconnecting meant restarting the
service. They now sit in an `AsyncExitStack` that can be unwound and rebuilt,
which is what makes an on-request re-login possible at all. A restart takes the
bridge's lock, so a scheduled run cannot catch a half-open session.

**`/indmoney force` is deliberately cautious.** `mcp-remote` names its cached
files by a hash of the server URL, which is not reproducible from here, so the
files are identified by their *contents* mentioning the host — your Kite token
can never be caught in the sweep. They are moved to `~/.mcp-auth/_pfm_cleared/`
rather than deleted, so a wrong match is recoverable, and the reply names every
file that moved.

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
python tests/test_commands.py      # Telegram commands and bridge restart
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
| `/goals` | Loan payoff and purchase planning (see below) |
| `/api/goals` | `GET` the goals and liquidity tiers; `POST` saves the goal list |
| `/healthz` | Liveness probe |

## Goals

Two questions, one page at `/goals`:

- **Pay something off.** What does four extra EMIs a year, or a 10% annual EMI
  hike, or a lump sum actually buy you in months and rupees?
- **Buy something.** What could you put down today without borrowing, and how
  much is left to finance?

### The loan model

`goals.py` builds the amortisation schedule month by month, and the rule is
small enough to state in full:

```
interest  = balance × rate ÷ 12
principal = EMI − interest
every 12th month:  balance −= EMI × extra_emis;  EMI ×= 1 + hike
```

This was modelled on an EMI prepayment spreadsheet, and reproduces it exactly —
`tests/test_goals.py` asserts the figures against it. One divergence is
deliberate and documented in `goals.py`: the sheet's `E14` subtracts the
month-12 prepayment back in month 2, while every later row correctly uses the
previous row's. That is a copy error in the sheet rather than a rule, so the
code does the intended thing and the test encodes the corrected totals.

Guard rails, because a loan calculator that silently lies is worse than none:

- An EMI that does not cover the first month's interest produces a warning and
  no schedule, rather than a balance that quietly never falls.
- The schedule stops at 600 months and says so, rather than looping.
- A lump sum larger than the balance only counts the balance as prepaid.
- Rates are decimal fractions throughout (`0.074`, not `7.4`), validated
  server-side, so an EMI slider cannot be made to model a 740% loan.

The page recalculates in the browser as you drag a slider, so there is no
round trip and no spinner. That means the model exists twice — `goals.py` and
`static/goals.js` — which is a real risk, so `test_js_parity()` runs the
JavaScript copy under `node` across six scenarios and fails if the two disagree
by a single month or a single rupee. If `node` is not installed the check skips
rather than passing silently.

### What a downpayment could come from

Holdings from the last agent run are grouped by how reachable they actually
are, taken from `state/networth_snapshot.json`:

| Tier | Contains | Meaning |
| --- | --- | --- |
| Ready | Savings, US wallet cash | Spendable now |
| Sellable | Stocks, mutual funds, gold | Reachable in days, at a price and a tax cost |
| Locked | PPF, EPF, NPS | Lock-ins and withdrawal rules apply |

Classification is by `asset_type` alone and **fails closed**: an asset type
nobody has taught it about lands in Locked, never in spendable money. A new
INDmoney instrument category can therefore never silently inflate what the page
says you can afford.

The figures are gross. They ignore capital gains tax, exit loads and whatever
price the market offers on the day — the page says so on screen rather than
only here.

### Writing

`/goals` is the only part of the web view that writes anything, and it writes
one file, `data/goals.json`. That directory exists so the systemd unit can
grant write access to exactly one path — `ProtectHome=read-only` plus a single
`ReadWritePaths` line — leaving the source tree the service runs from
read-only. It is also the one directory here whose contents cannot be
regenerated: `cache/`, `reports/` and `state/` all come back on the next run,
your goals do not. Back it up.

Everything posted is re-validated server-side with the
same rules as the page, because the endpoint is reachable by anything on the
network, not only by the form. Cross-site POSTs are refused, and a JSON content
type is required so a plain HTML form cannot reach it either. Set
`web.goals_writable` to `false` in `config.json` to make the whole site
read-only again.

The snapshot the tiers are built from is written by the agent at the end of each
run. Before the first run the page still works — you can model any loan by
typing the numbers in — the affordability figures are simply zero, and the page
tells you why.

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
| A missing `mcp` install crashed the Telegram listener | `MCPBridge.start()` promised never to raise, but imported `mcp` outside its own try block | The import moved inside the guard, so a broken install is reported like any other start failure |
| Every connection failing with `not a real file` | The stderr tee was a Python object with a `write()` method. `stdio_client` passes `errlog` to `anyio.open_process(stderr=...)`, which hands it to the OS when spawning the child — so it needs a genuine file descriptor and never calls `write()`. The unit test poked `write()` directly, so it passed while nothing worked | The tee owns a real `os.pipe()` and drains it on a thread. The test now runs an actual subprocess through it, and a separate check asserts repeated open/close cycles leak no descriptors |
| The daemon crash-looping and spamming Telegram | A failed Kite connection returned `1` from the daemon. systemd restarted it every 15s, re-alerting each time — and each exit killed the Telegram listener, taking `/login` with it, so the documented recovery needed the process that had just died | In daemon mode the failure is reported once and the agent stays up; only the foreground modes still exit non-zero |
| A bridge that failed at startup stayed down all night | The keepalive skipped any bridge with no session, so it only ever pinged healthy ones | It now reconnects a dead bridge with backoff from one minute to an hour, silently — an unattended agent should not need a human to notice |

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

`tests/test_commands.py` covers Telegram command parsing, chat-ID
authorisation (fail-closed when no chat is configured), and the capture of an
OAuth sign-in URL out of `mcp-remote`'s stderr.

`tests/test_goals.py` checks the amortisation against the spreadsheet figures
above, the edges (an EMI below the first month's interest, a loan that never
clears, an interest-free loan, a lump sum larger than the balance), liquidity
classification failing closed, goal validation and atomic storage, the script
escaping of the state the page embeds, and finally that the browser's copy of
the model agrees with the Python one.

Every suite needs no Pi, no model, no broker and no network:

```bash
for t in tests/test_*.py; do python "$t" || break; done
```
