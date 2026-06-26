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

    async def test_first_cycle_seeds_and_confirms_without_reporting_filings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = f"{tmp}/state.json"
            worker = self._make_worker(state_path)
            subs = _submissions([{"form": "10-K", "date": "2024-11-01", "accession": "0000320193-24-000001"}])
            with patch.object(eng, "load_ticker_map", new=AsyncMock(return_value=_TICKER_MAP)), patch.object(
                eng, "sec_request", new=AsyncMock(return_value=subs)
            ):
                result = await worker.run_cycle()
            # Seeding cycle: no historical filings reported, but a one-time
            # low-priority "monitoring active" confirmation is sent.
            self.assertTrue(result.stats["baseline_seeded"])
            self.assertEqual(result.stats["new_filings"], 0)
            self.assertIsNotNone(result.content)
            self.assertEqual(result.priority, 1)  # PRIORITY_LOW
            self.assertIn("monitoring is now active", result.content.lower())
            self.assertIn("AAPL", result.content)

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
            self.assertEqual(result.priority, 3)  # PRIORITY_HIGH
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


class TestSettings(unittest.TestCase):
    def test_parse_watchlist_dedupes_and_uppercases(self) -> None:
        self.assertEqual(config.parse_watchlist("aapl, MSFT, aapl\nnvda"), ["AAPL", "MSFT", "NVDA"])

    def test_get_watchlist_prefers_app_var_over_env(self) -> None:
        with patch.dict("os.environ", {"SEC_EDGAR_WATCHLIST": "AAPL"}, clear=False), patch.object(
            config, "get_app_var", return_value="NVDA, AMD"
        ):
            self.assertEqual(config.get_watchlist(), ["NVDA", "AMD"])

    def test_get_watchlist_falls_back_to_env_when_app_var_empty(self) -> None:
        with patch.dict("os.environ", {"SEC_EDGAR_WATCHLIST": "AAPL, MSFT"}, clear=False), patch.object(
            config, "get_app_var", return_value=None
        ):
            self.assertEqual(config.get_watchlist(), ["AAPL", "MSFT"])

    def test_set_watchlist_persists_cleaned_list(self) -> None:
        saved: dict[str, str] = {}
        with patch.object(config, "app_vars_enabled", return_value=True), patch.object(
            config, "set_app_var", side_effect=lambda k, v: saved.__setitem__(k, v)
        ):
            result = config.set_watchlist("aapl, nvda , aapl")
        self.assertEqual(result, ["AAPL", "NVDA"])
        self.assertEqual(saved, {config.APP_VAR_WATCHLIST: "AAPL,NVDA"})

    def test_set_watchlist_rejects_empty(self) -> None:
        with patch.object(config, "app_vars_enabled", return_value=True):
            with self.assertRaises(ValueError):
                config.set_watchlist("   ,  ")

    def test_set_watch_forms_persists_uppercased(self) -> None:
        saved: dict[str, str] = {}
        with patch.object(config, "app_vars_enabled", return_value=True), patch.object(
            config, "set_app_var", side_effect=lambda k, v: saved.__setitem__(k, v)
        ):
            result = config.set_watch_forms("8-k, s-1")
        self.assertEqual(result, ["8-K", "S-1"])
        self.assertEqual(saved, {config.APP_VAR_WATCH_FORMS: "8-K,S-1"})

    def test_set_app_var_raises_outside_container(self) -> None:
        with patch.object(config, "app_vars_enabled", return_value=False):
            with self.assertRaises(RuntimeError):
                config.set_app_var("watchlist", "AAPL")


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
