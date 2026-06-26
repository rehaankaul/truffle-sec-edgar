"""Unit tests for SEC EDGAR app logic — no network, auth, or device required."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import edgar_engine as eng
from bg_worker import EdgarBackgroundWorker, NewFiling


def _submissions(filings: list[dict]) -> dict:
    return {
        "name": "Apple Inc.",
        "filings": {
            "recent": {
                "form": [f["form"] for f in filings],
                "filingDate": [f["date"] for f in filings],
                "accessionNumber": [f["accession"] for f in filings],
                "primaryDocument": [f.get("doc", "primary.htm") for f in filings],
                "primaryDocDescription": [f.get("desc", f["form"]) for f in filings],
            }
        },
    }


_TICKER_MAP = {"AAPL": {"cik": "320193", "ticker": "AAPL", "name": "Apple Inc."}}


class TestConfig(unittest.TestCase):
    def test_watchlist_parses_dedups_and_uppercases(self) -> None:
        with patch.dict("os.environ", {"SEC_EDGAR_WATCHLIST": "aapl, MSFT,aapl\ntsla"}, clear=False):
            self.assertEqual(config.get_watchlist(), ["AAPL", "MSFT", "TSLA"])

    def test_watchlist_empty_returns_empty_list(self) -> None:
        with patch.dict("os.environ", {"SEC_EDGAR_WATCHLIST": "   "}, clear=False):
            self.assertEqual(config.get_watchlist(), [])

    def test_watch_forms_default(self) -> None:
        with patch.dict("os.environ", {"SEC_EDGAR_WATCH_FORMS": ""}, clear=False):
            self.assertEqual(config.get_watch_forms(), ["8-K", "10-K", "10-Q"])

    def test_watch_forms_custom(self) -> None:
        with patch.dict("os.environ", {"SEC_EDGAR_WATCH_FORMS": "8-k, s-1"}, clear=False):
            self.assertEqual(config.get_watch_forms(), ["8-K", "S-1"])

    def test_user_agent_falls_back(self) -> None:
        with patch.dict("os.environ", {"SEC_EDGAR_USER_AGENT": ""}, clear=False):
            self.assertEqual(config.get_user_agent(), config.DEFAULT_USER_AGENT)


class TestBackgroundWorker(unittest.IsolatedAsyncioTestCase):
    def _make_worker(self, state_path: str) -> EdgarBackgroundWorker:
        self._env = patch.dict(
            "os.environ",
            {
                "SEC_EDGAR_WATCHLIST": "AAPL",
                "SEC_EDGAR_WATCH_FORMS": "10-K",
                "SEC_EDGAR_STATE_PATH": state_path,
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        return EdgarBackgroundWorker()

    async def test_first_cycle_seeds_without_submitting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = f"{tmp}/state.json"
            worker = self._make_worker(state_path)
            subs = _submissions([{"form": "10-K", "date": "2024-11-01", "accession": "0000320193-24-000001"}])
            with patch.object(eng, "load_ticker_map", new=AsyncMock(return_value=_TICKER_MAP)), patch.object(
                eng, "sec_request", new=AsyncMock(return_value=subs)
            ):
                result = await worker.run_cycle()
            # Seeding cycle: nothing submitted, but state records the accession.
            self.assertIsNone(result.content)
            self.assertTrue(result.stats["baseline_seeded"])
            self.assertEqual(result.stats["new_filings"], 0)

    async def test_second_cycle_detects_new_filing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = f"{tmp}/state.json"
            old = {"form": "10-K", "date": "2023-11-01", "accession": "0000320193-23-000001"}
            new = {"form": "10-K", "date": "2024-11-01", "accession": "0000320193-24-000099"}

            # Cycle 1 seeds with the old filing only.
            worker1 = self._make_worker(state_path)
            with patch.object(eng, "load_ticker_map", new=AsyncMock(return_value=_TICKER_MAP)), patch.object(
                eng, "sec_request", new=AsyncMock(return_value=_submissions([old]))
            ):
                await worker1.run_cycle()

            # Cycle 2 sees old + new; only the new one is reported.
            worker2 = EdgarBackgroundWorker()
            with patch.object(eng, "load_ticker_map", new=AsyncMock(return_value=_TICKER_MAP)), patch.object(
                eng, "sec_request", new=AsyncMock(return_value=_submissions([new, old]))
            ):
                result = await worker2.run_cycle()

            self.assertIsNotNone(result.content)
            self.assertEqual(result.stats["new_filings"], 1)
            self.assertIn("AAPL", result.content)
            self.assertIn("0000320193-24-000099".replace("-", ""), "".join(result.uris))

    async def test_unconfigured_watchlist_fails_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {"SEC_EDGAR_WATCHLIST": "", "SEC_EDGAR_STATE_PATH": f"{tmp}/s.json"},
                clear=False,
            ):
                worker = EdgarBackgroundWorker()
                ok, _ = worker.verify()
                self.assertFalse(ok)


class TestContextFormatting(unittest.TestCase):
    def test_build_context_includes_actionable_hints(self) -> None:
        with patch.dict("os.environ", {"SEC_EDGAR_WATCHLIST": "AAPL"}, clear=False):
            worker = EdgarBackgroundWorker()
        filing = NewFiling(
            ticker="AAPL",
            company="Apple Inc.",
            cik="320193",
            form="10-K",
            date="2024-11-01",
            accession="0000320193-24-000099",
            description="Annual report",
            url="https://www.sec.gov/Archives/edgar/data/320193/x/primary.htm",
        )
        text = worker._build_context([filing])
        self.assertIn("New SEC filings detected: 1", text)
        self.assertIn("edgar_extract_xbrl", text)
        self.assertIn("Apple Inc.", text)


if __name__ == "__main__":
    unittest.main()
