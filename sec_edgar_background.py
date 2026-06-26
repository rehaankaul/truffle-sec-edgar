"""Background app: periodically surface newly filed SEC documents.

On each scheduled cycle the worker checks the configured watchlist of tickers for
new 8-K/10-K/10-Q filings and submits a context note to the proactivity agent so
it can decide whether to alert the user or analyze the filing. The worker's
run_cycle is async (it makes rate-limited SEC HTTP calls), so we drive it through
asyncio.run here.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Coroutine

from truffile.app_runtime import BackgroundWorkerApp

from bg_worker import BgRunResult, EdgarBackgroundWorker


def _run_coro(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run an async coroutine to completion from a sync context.

    The background runtime (and the test harness) may invoke run_cycle from
    inside an already-running event loop, where asyncio.run() would raise.
    Detect that case and execute the coroutine on a dedicated worker thread
    with its own loop; otherwise run it directly.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()

class EdgarBackgroundApp(BackgroundWorkerApp[EdgarBackgroundWorker, BgRunResult]):
    def __init__(self) -> None:
        super().__init__("sec-edgar", logger_name="sec-edgar.background")

    def build_worker(self) -> EdgarBackgroundWorker:
        return EdgarBackgroundWorker()

    def verify_worker(self, worker: EdgarBackgroundWorker) -> tuple[bool, str]:
        return worker.verify()

    def run_cycle(self, worker: EdgarBackgroundWorker) -> BgRunResult:
        return _run_coro(worker.run_cycle())

    def handle_cycle_result(self, ctx: object, result: BgRunResult) -> None:
        if result.error:
            self.logger.error(
                "SEC EDGAR background cycle failed", extra={"error": result.error}
            )
            return
        if not result.content:
            self.logger.info(
                "SEC EDGAR background cycle produced no new filings",
                extra={"stats": result.stats},
            )
            return
        self.submit_text(
            ctx,
            content=result.content,
            uris=result.uris,
            priority=result.priority,
        )


app = EdgarBackgroundApp()


def verify() -> int:
    worker = app.build_worker()
    ok, message = worker.verify()
    print(message, flush=True)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="SEC EDGAR background app")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify the background watchlist configuration, then exit.",
    )
    args = parser.parse_args()
    if args.verify:
        return verify()
    app.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
