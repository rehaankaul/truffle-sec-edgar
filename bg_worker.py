"""Background worker: watch a list of tickers for newly filed SEC documents.

Each cycle resolves every watchlist ticker to a CIK, pulls its most recent
submissions, and detects filings (of the configured form types) that we have not
seen before. The first cycle seeds state without submitting, so the agent is not
flooded with a backlog of historical filings on install. Seen accession numbers
are persisted to disk and bounded to keep memory/state size in check.

The SEC fetch primitives are reused from edgar_engine so behavior (rate limiting,
User-Agent, error handling) matches the foreground tools exactly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config
import edgar_engine as eng

logger = logging.getLogger("sec-edgar.bg_worker")
logger.setLevel(logging.INFO)


@dataclass
class NewFiling:
    ticker: str
    company: str
    cik: str
    form: str
    date: str
    accession: str
    description: str
    url: str


@dataclass
class BgRunResult:
    content: str | None = None
    uris: list[str] = field(default_factory=list)
    error: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)


class EdgarBackgroundWorker:
    def __init__(self) -> None:
        self._watchlist = config.get_watchlist()
        self._forms = set(config.get_watch_forms())
        self._lookback = config.get_submissions_lookback()
        self._max_new = config.get_max_new_filings_per_run()
        self._state_path = Path(config.get_state_path())

    # ------------------------------------------------------------------ verify
    def verify(self) -> tuple[bool, str]:
        if not self._watchlist:
            return (
                False,
                "No tickers configured. Set a comma-separated watchlist (e.g. "
                "AAPL,MSFT,TSLA) to monitor for new SEC filings.",
            )
        return (
            True,
            f"Monitoring {len(self._watchlist)} ticker(s) "
            f"({', '.join(self._watchlist)}) for new {', '.join(sorted(self._forms))} filings.",
        )

    # --------------------------------------------------------------- run cycle
    async def run_cycle(self) -> BgRunResult:
        if not self._watchlist:
            return BgRunResult(error="no_watchlist")

        state = self._load_state()
        seen: set[str] = set(state.get("seen_accessions") or [])
        seeded = bool(state.get("initialized_at"))

        try:
            ticker_map = await eng.load_ticker_map()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load ticker map: %s", exc)
            return BgRunResult(error=f"ticker_map_failed: {exc}")

        new_filings: list[NewFiling] = []
        scanned = 0
        unresolved: list[str] = []

        for ticker in self._watchlist:
            entry = ticker_map.get(ticker.upper())
            if not entry or not entry.get("cik"):
                unresolved.append(ticker)
                continue
            try:
                filings = await self._recent_filings(ticker, entry)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to fetch submissions for %s: %s", ticker, exc)
                continue

            for filing in filings:
                scanned += 1
                if filing.accession in seen:
                    continue
                seen.add(filing.accession)
                # During the first cycle we only seed state; nothing is submitted.
                if seeded and len(new_filings) < self._max_new:
                    new_filings.append(filing)

        # Persist bounded state.
        state["seen_accessions"] = sorted(seen)[-config.MAX_SEEN_ACCESSIONS :]
        if not seeded:
            state["initialized_at"] = "seeded"
        self._save_state(state)

        stats = {
            "baseline_seeded": not seeded,
            "tickers": len(self._watchlist),
            "unresolved": unresolved,
            "filings_scanned": scanned,
            "new_filings": len(new_filings),
        }

        if not new_filings:
            return BgRunResult(content=None, stats=stats)

        return BgRunResult(
            content=self._build_context(new_filings),
            uris=[f.url for f in new_filings if f.url],
            stats=stats,
        )

    # ---------------------------------------------------------------- helpers
    async def _recent_filings(self, ticker: str, entry: dict[str, Any]) -> list[NewFiling]:
        cik_raw = str(entry.get("cik", ""))
        cik_padded = eng.pad_cik(cik_raw)
        url = f"{eng.SEC_BASE_URL}/submissions/CIK{cik_padded}.json"
        data = await eng.sec_request(url)

        company = data.get("name", entry.get("name", ticker))
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        descriptions = recent.get("primaryDocDescription", [])

        results: list[NewFiling] = []
        for i in range(min(len(forms), self._lookback)):
            form = forms[i]
            if form not in self._forms:
                continue
            accession = accessions[i] if i < len(accessions) else ""
            if not accession:
                continue
            primary = primary_docs[i] if i < len(primary_docs) else ""
            accession_clean = accession.replace("-", "")
            doc_url = (
                f"{eng.SEC_WWW_URL}/Archives/edgar/data/{cik_raw.lstrip('0')}"
                f"/{accession_clean}/{primary}"
                if primary
                else f"{eng.SEC_WWW_URL}/Archives/edgar/data/{cik_raw.lstrip('0')}/{accession_clean}/"
            )
            results.append(
                NewFiling(
                    ticker=ticker.upper(),
                    company=company,
                    cik=cik_raw,
                    form=form,
                    date=dates[i] if i < len(dates) else "",
                    accession=accession,
                    description=descriptions[i] if i < len(descriptions) else "",
                    url=doc_url,
                )
            )
        return results

    def _build_context(self, items: list[NewFiling]) -> str:
        lines = [f"New SEC filings detected: {len(items)}."]
        for f in items:
            desc = f" — {f.description}" if f.description else ""
            lines.append(
                f"- {f.ticker} ({f.company}) filed a {f.form} on {f.date}{desc}"
            )
            lines.append(
                f"  analyze: edgar_get_submissions cik=\"{f.cik}\" form_type=\"{f.form}\" "
                f"| edgar_extract_xbrl cik=\"{f.cik}\" form_type=\"{f.form}\""
            )
            if f.url:
                lines.append(f"  url: {f.url}")
        return "\n".join(lines)

    def _load_state(self) -> dict[str, Any]:
        try:
            if not self._state_path.exists():
                return {"seen_accessions": []}
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load BG state: %s", exc)
        return {"seen_accessions": []}

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to save BG state: %s", exc)
