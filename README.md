# SEC EDGAR — Truffle App

<p align="center">
  <img src="./icon.png" alt="SEC EDGAR" width="140" />
</p>

A [Truffle](https://docs.truffle.net) app that gives your on-device Truffle agent
direct access to **SEC EDGAR** financial data — company filings, XBRL financial
statements, cross-company comparisons, and full-text search — plus a background
monitor that proactively surfaces newly filed 8-Ks, 10-Ks, and 10-Qs for the
companies you care about.

All data comes from the SEC's free, public APIs. **No API key required.**

---

## What it does

**12 foreground tools** the agent can call during a conversation:

| Tool | Purpose |
|------|---------|
| `edgar_search_company` | Find a company's CIK by ticker or name (call this first) |
| `edgar_get_submissions` | Filing history (10-K, 10-Q, 8-K, …) with document links |
| `edgar_get_company_facts` | All XBRL facts a company reports (discover exact tags) |
| `edgar_get_company_concept` | Full historical time series for one XBRL concept |
| `edgar_get_frames` | One metric across all companies for a period (peer benchmarking) |
| `edgar_search_filings` | Quick full-text search across all filings since 2001 |
| `edgar_get_filing_document` | Fetch and read a specific filing document |
| `edgar_search_filing_content` | Keyword-search *within* a large filing, returning only relevant sections |
| `edgar_get_financial_statements` | Structured income statement / balance sheet / cash flow |
| `edgar_extract_xbrl` | **Primary modeling tool** — parses iXBRL from the actual filing, capturing custom tags and segment detail |
| `edgar_raw_query` | Generic passthrough to any SEC EDGAR API endpoint |
| `edgar_full_text_search` | Advanced full-text search with ticker/CIK filtering, pagination, and snippets |

Plus three **settings tools** the agent uses to reconfigure the background
monitor from chat: `edgar_get_settings`, `edgar_set_watchlist`, and
`edgar_set_watched_forms`.

**Background monitor:** on a schedule (every 2 minutes in dev, 30 minutes in
production) it checks your watchlist of tickers and submits a note to the
proactivity agent when a new filing (of the configured form types) appears — so
you hear about a fresh 8-K or 10-K without asking. The very first cycle after
install seeds its baseline (so you aren't flooded with historical filings) and
sends a one-time low-priority "monitoring is now active" confirmation.

---

## Prerequisites

- A Truffle device onboarded through the [Symphony desktop client](https://docs.truffle.net/client/overview).
- The `truffile` CLI ([SDK installation](https://docs.truffle.net/sdk/installation)):

  ```bash
  # Python 3.12+ required. With uv (recommended):
  uv venv --python 3.13 .venv && source .venv/bin/activate
  uv pip install truffile
  # …or with plain pip:
  python3 -m venv .venv && source .venv/bin/activate && pip install truffile
  ```

- A connected device:

  ```bash
  truffile scan
  truffile connect <device-name> --user-id <your-user-id>   # User ID from Symphony → Settings → About
  ```

## Install the app

```bash
git clone https://github.com/rehaankaul/truffle-sec-edgar.git
cd truffle-sec-edgar
truffile validate .          # optional: local checks, no device needed
truffile deploy .
```

Install presents three separate configuration steps, so each can be edited on
its own later:

1. **SEC contact email** *(required)* — the SEC requires every API client to
   identify itself with a name and contact email, e.g. `Jane Doe jane@example.com`.
   No parentheses or browser-style formatting (the SEC rejects those).
2. **Watchlist tickers** *(required)* — comma-separated tickers to monitor for new
   filings, e.g. `AAPL, MSFT, TSLA`.
3. **Watched documents** *(optional)* — comma-separated SEC form types; defaults
   to `8-K, 10-K, 10-Q`. Add e.g. `S-1` for IPO registrations or `4` for
   insider-transaction reports.

The 12 tools work for **any** public company regardless of the watchlist — the
watchlist only drives proactive background alerts.

### Changing settings later

**Watchlist and watched forms — just ask the agent (no redeploy):** these are
stored as runtime app variables, so you can change them right from chat. The
background monitor picks up the change on its next cycle.

```bash
truffile chat --app sec-edgar "show my EDGAR settings"
truffile chat --app sec-edgar "set my EDGAR watchlist to AAPL, NVDA, MSFT"
truffile chat --app sec-edgar "also watch S-1 filings"
```

Backing tools: `edgar_get_settings`, `edgar_set_watchlist`, `edgar_set_watched_forms`.

**SEC contact email** is set at install time. To change it (or to re-enter
everything from scratch), re-run setup from the CLI — it re-prompts each of the
three configuration screens:

```bash
truffile deploy . --replace
```

---

## Try it

Once deployed, chat with your Truffle agent:

```bash
truffile chat --app sec-edgar "Pull Apple's revenue history from EDGAR for the last 5 years"
truffile chat --app sec-edgar "Build a 3-statement model for Netflix from its latest 10-K"
truffile chat --app sec-edgar "Find 8-Ks mentioning goodwill impairment for MSFT in 2024"
```

## Configuration reference

These are set during install but can be overridden via the app's environment:

| Env var | Default | Meaning |
|---------|---------|---------|
| `SEC_EDGAR_USER_AGENT` | — | SEC contact User-Agent (`Name email@example.com`) |
| `SEC_EDGAR_WATCHLIST` | — | Comma-separated tickers for the background monitor |
| `SEC_EDGAR_WATCH_FORMS` | `8-K,10-K,10-Q` | Form types treated as noteworthy |
| `SEC_EDGAR_SUBMISSIONS_LOOKBACK` | `30` | Most-recent filings scanned per ticker each cycle |
| `SEC_EDGAR_MAX_NEW_FILINGS_PER_RUN` | `8` | Cap on filings surfaced per cycle |

## Development

```bash
pip install pytest
python -m pytest tests/ -v          # unit + AppHarness shell tests (no network)

# Verify connectivity / configuration locally:
SEC_EDGAR_USER_AGENT="Jane Doe jane@example.com" python sec_edgar_foreground.py --verify
SEC_EDGAR_WATCHLIST="AAPL,MSFT" python sec_edgar_background.py --verify
```

### Layout

```
truffile.yaml              # hybrid app manifest (foreground + background + install steps)
edgar_engine.py            # SEC EDGAR engine: the 12 tool implementations + iXBRL parsing
edgar_tools.py             # agent-facing tool metadata (name/description/icon)
config.py                  # environment-driven configuration
sec_edgar_foreground.py    # ForegroundApp: registers the 12 tools
bg_worker.py               # background filing monitor (watchlist → new filings)
sec_edgar_background.py    # BackgroundWorkerApp entrypoint
tests/                     # unit + app-shell tests
```

## SEC compliance & rate limits

The SEC requires a descriptive `User-Agent` header and limits clients to ~10
requests/second. This app sends your configured contact User-Agent on every
request and self-throttles to stay within SEC limits. Please use a real contact
email so the SEC can reach you if needed.

## Credits

Built with the [Truffle SDK](https://docs.truffle.net/sdk/building-apps). Financial
data from the U.S. Securities and Exchange Commission's public
[EDGAR APIs](https://www.sec.gov/edgar/sec-api-documentation). Not affiliated with
or endorsed by the SEC.

## License

[MIT](./LICENSE)
