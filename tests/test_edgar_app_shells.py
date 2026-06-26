"""App-shell tests: drive the FG/BG apps through the runtime AppHarness."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from truffile.app_runtime import AppHarness

import edgar_engine as eng


class TestForegroundShell(unittest.IsolatedAsyncioTestCase):
    async def test_registered_tool_runs_through_harness(self) -> None:
        from sec_edgar_foreground import app as fg_app

        markdown = "## Company Match\n- Apple Inc. (AAPL) — CIK 320193"
        with patch.object(eng, "edgar_search_company", new=AsyncMock(return_value=markdown)):
            harness = AppHarness(fg_app=fg_app, logger_names=["sec-edgar.foreground"])
            result = await harness.run_fg(calls=[("edgar_search_company", {"query": "Apple"})])

        self.assertTrue(result.success, msg=f"errors: {result.errors}")
        self.assertEqual(result.errors, [])

    async def test_extract_xbrl_requires_cik_or_url(self) -> None:
        from sec_edgar_foreground import app as fg_app

        harness = AppHarness(fg_app=fg_app, logger_names=["sec-edgar.foreground"])
        # Neither cik nor url provided — wrapper returns a guidance error, not a crash.
        result = await harness.run_fg(calls=[("edgar_extract_xbrl", {})])
        self.assertTrue(result.success, msg=f"errors: {result.errors}")


class TestBackgroundShell(unittest.IsolatedAsyncioTestCase):
    async def test_background_cycle_submits_new_filings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {
                    "SEC_EDGAR_WATCHLIST": "AAPL",
                    "SEC_EDGAR_WATCH_FORMS": "10-K",
                    "SEC_EDGAR_STATE_PATH": f"{tmp}/state.json",
                },
                clear=False,
            ):
                from sec_edgar_background import app as bg_app
                from bg_worker import BgRunResult, EdgarBackgroundWorker

                bg_app.reset_for_test()
                run_result = BgRunResult(
                    content="New SEC filings detected: 1.\n- AAPL (Apple Inc.) filed a 10-K on 2024-11-01",
                    uris=["https://www.sec.gov/Archives/edgar/data/320193/x/primary.htm"],
                    stats={"new_filings": 1},
                )
                with patch.object(
                    EdgarBackgroundWorker, "run_cycle", new=AsyncMock(return_value=run_result)
                ):
                    harness = AppHarness(bg_app=bg_app, logger_names=["sec-edgar.background"])
                    result = await harness.run_bg(cycles=1)

        self.assertTrue(result.success, msg=f"errors: {result.errors}")
        self.assertGreaterEqual(len(result.submissions), 1)


if __name__ == "__main__":
    unittest.main()
