"""Foreground app: exposes the 12 SEC EDGAR tools to the on-device agent.

The SEC logic lives in edgar_engine.py (ported from the original FastMCP server).
Each engine function takes a Pydantic input model and returns a text string; the
wrappers below give the runtime explicit keyword signatures (so it can build tool
input schemas), construct the model, call the engine, and return the text as a
CallToolResult. All tools are read-only — they only fetch free, public SEC data.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from mcp.types import CallToolResult, TextContent
from truffile.app_runtime import ForegroundApp, ToolSpec

import config
import edgar_engine as eng
from edgar_engine import (
    EdgarSearchCompanyInput,
    EdgarGetSubmissionsInput,
    EdgarGetCompanyFactsInput,
    EdgarGetCompanyConceptInput,
    EdgarGetFramesInput,
    EdgarSearchFilingsInput,
    EdgarGetFilingDocumentInput,
    EdgarSearchFilingContentInput,
    EdgarGetFinancialStatementsInput,
    EdgarExtractXBRLInput,
    EdgarRawQueryInput,
    EdgarFullTextSearchInput,
    ResponseFormat,
)
from edgar_tools import TOOLS_BY_NAME


def _text_result(text: str, *, is_error: bool = False) -> CallToolResult:
    body = (text or "").strip() or "SEC EDGAR returned no visible content."
    return CallToolResult(
        content=[TextContent(type="text", text=body)],
        isError=is_error,
    )


def _clean(**kwargs: Any) -> dict[str, Any]:
    """Drop None values so each Pydantic model falls back to its own defaults."""
    return {key: value for key, value in kwargs.items() if value is not None}


def _coerce_format(response_format: str | None) -> ResponseFormat | None:
    if response_format is None:
        return None
    try:
        return ResponseFormat(response_format.lower())
    except ValueError:
        return ResponseFormat.MARKDOWN


def _spec(name: str) -> ToolSpec:
    tool = TOOLS_BY_NAME[name]
    return ToolSpec(
        name=tool.name,
        title=tool.title,
        description=tool.description,
        icon=tool.icon,
        annotations=dict(tool.annotations),
    )


class EdgarForegroundApp(ForegroundApp):
    def __init__(self) -> None:
        super().__init__("sec-edgar", logger_name="sec-edgar.foreground")
        self._register_tools()
        self._register_user_info()

    def _register_user_info(self) -> None:
        @self.user_info_resource()
        async def edgar_user_info() -> str:
            lines = [
                "# SEC EDGAR",
                f"User-Agent: {config.get_user_agent()}",
                "Data: free public SEC EDGAR APIs (no API key).",
                "Tools: 12 (search, filings, XBRL facts, financial statements, "
                "extract-XBRL modeling, full-text search).",
            ]
            watchlist = config.get_watchlist()
            if watchlist:
                lines.append(
                    f"Background watchlist: {', '.join(watchlist)} "
                    f"(forms: {', '.join(config.get_watch_forms())})."
                )
            else:
                lines.append("Background watchlist: not configured.")
            return "\n".join(lines)

    def _register_tools(self) -> None:
        @self.tool(_spec("edgar_search_company"))
        async def edgar_search_company(query: str, limit: int | None = None) -> CallToolResult:
            return await self._run(
                eng.edgar_search_company,
                EdgarSearchCompanyInput(**_clean(query=query, limit=limit)),
            )

        @self.tool(_spec("edgar_get_submissions"))
        async def edgar_get_submissions(
            cik: str,
            form_type: str | None = None,
            limit: int | None = None,
            response_format: str | None = None,
        ) -> CallToolResult:
            return await self._run(
                eng.edgar_get_submissions,
                EdgarGetSubmissionsInput(
                    **_clean(
                        cik=cik,
                        form_type=form_type,
                        limit=limit,
                        response_format=_coerce_format(response_format),
                    )
                ),
            )

        @self.tool(_spec("edgar_get_company_facts"))
        async def edgar_get_company_facts(
            cik: str,
            taxonomy: str | None = None,
            concept_filter: str | None = None,
            response_format: str | None = None,
        ) -> CallToolResult:
            return await self._run(
                eng.edgar_get_company_facts,
                EdgarGetCompanyFactsInput(
                    **_clean(
                        cik=cik,
                        taxonomy=taxonomy,
                        concept_filter=concept_filter,
                        response_format=_coerce_format(response_format),
                    )
                ),
            )

        @self.tool(_spec("edgar_get_company_concept"))
        async def edgar_get_company_concept(
            cik: str,
            concept: str,
            taxonomy: str | None = None,
            response_format: str | None = None,
        ) -> CallToolResult:
            return await self._run(
                eng.edgar_get_company_concept,
                EdgarGetCompanyConceptInput(
                    **_clean(
                        cik=cik,
                        concept=concept,
                        taxonomy=taxonomy,
                        response_format=_coerce_format(response_format),
                    )
                ),
            )

        @self.tool(_spec("edgar_get_frames"))
        async def edgar_get_frames(
            concept: str,
            period: str,
            taxonomy: str | None = None,
            unit: str | None = None,
            limit: int | None = None,
            response_format: str | None = None,
        ) -> CallToolResult:
            return await self._run(
                eng.edgar_get_frames,
                EdgarGetFramesInput(
                    **_clean(
                        concept=concept,
                        period=period,
                        taxonomy=taxonomy,
                        unit=unit,
                        limit=limit,
                        response_format=_coerce_format(response_format),
                    )
                ),
            )

        @self.tool(_spec("edgar_search_filings"))
        async def edgar_search_filings(
            query: str,
            forms: str | None = None,
            date_start: str | None = None,
            date_end: str | None = None,
            limit: int | None = None,
        ) -> CallToolResult:
            return await self._run(
                eng.edgar_search_filings,
                EdgarSearchFilingsInput(
                    **_clean(
                        query=query,
                        forms=forms,
                        date_start=date_start,
                        date_end=date_end,
                        limit=limit,
                    )
                ),
            )

        @self.tool(_spec("edgar_get_filing_document"))
        async def edgar_get_filing_document(
            url: str, max_length: int | None = None
        ) -> CallToolResult:
            return await self._run(
                eng.edgar_get_filing_document,
                EdgarGetFilingDocumentInput(**_clean(url=url, max_length=max_length)),
            )

        @self.tool(_spec("edgar_search_filing_content"))
        async def edgar_search_filing_content(
            url: str,
            search_terms: str,
            context_chars: int | None = None,
            max_matches: int | None = None,
        ) -> CallToolResult:
            return await self._run(
                eng.edgar_search_filing_content,
                EdgarSearchFilingContentInput(
                    **_clean(
                        url=url,
                        search_terms=search_terms,
                        context_chars=context_chars,
                        max_matches=max_matches,
                    )
                ),
            )

        @self.tool(_spec("edgar_get_financial_statements"))
        async def edgar_get_financial_statements(
            cik: str,
            statement: str | None = None,
            period_type: str | None = None,
            years: int | None = None,
            response_format: str | None = None,
        ) -> CallToolResult:
            return await self._run(
                eng.edgar_get_financial_statements,
                EdgarGetFinancialStatementsInput(
                    **_clean(
                        cik=cik,
                        statement=statement,
                        period_type=period_type,
                        years=years,
                        response_format=_coerce_format(response_format),
                    )
                ),
            )

        @self.tool(_spec("edgar_extract_xbrl"))
        async def edgar_extract_xbrl(
            cik: str | None = None,
            url: str | None = None,
            form_type: str | None = None,
            filing_date: str | None = None,
            filter: str | None = None,
            axis_filter: str | None = None,
            include_standard: bool | None = None,
            response_format: str | None = None,
        ) -> CallToolResult:
            if not cik and not url:
                return _text_result(
                    "Provide either cik or url for edgar_extract_xbrl.", is_error=True
                )
            return await self._run(
                eng.edgar_extract_xbrl,
                EdgarExtractXBRLInput(
                    **_clean(
                        cik=cik,
                        url=url,
                        form_type=form_type,
                        filing_date=filing_date,
                        filter=filter,
                        axis_filter=axis_filter,
                        include_standard=include_standard,
                        response_format=_coerce_format(response_format),
                    )
                ),
            )

        @self.tool(_spec("edgar_raw_query"))
        async def edgar_raw_query(
            url: str,
            max_length: int | None = None,
            response_format: str | None = None,
        ) -> CallToolResult:
            return await self._run(
                eng.edgar_raw_query,
                EdgarRawQueryInput(
                    **_clean(
                        url=url,
                        max_length=max_length,
                        response_format=_coerce_format(response_format),
                    )
                ),
            )

        @self.tool(_spec("edgar_full_text_search"))
        async def edgar_full_text_search(
            query: str,
            entity_name: str | None = None,
            ciks: str | None = None,
            tickers: str | None = None,
            forms: str | None = None,
            date_start: str | None = None,
            date_end: str | None = None,
            filed_start: str | None = None,
            filed_end: str | None = None,
            limit: int | None = None,
            start_from: int | None = None,
            include_snippets: bool | None = None,
        ) -> CallToolResult:
            return await self._run(
                eng.edgar_full_text_search,
                EdgarFullTextSearchInput(
                    **_clean(
                        query=query,
                        entity_name=entity_name,
                        ciks=ciks,
                        tickers=tickers,
                        forms=forms,
                        date_start=date_start,
                        date_end=date_end,
                        filed_start=filed_start,
                        filed_end=filed_end,
                        limit=limit,
                        start_from=start_from,
                        include_snippets=include_snippets,
                    )
                ),
            )

    async def _run(self, fn: Any, params: Any) -> CallToolResult:
        try:
            text = await fn(params)
        except Exception as exc:  # noqa: BLE001 — never let it propagate to the runtime
            self.logger.exception("EDGAR tool failed")
            return _text_result(f"SEC EDGAR request failed: {exc}", is_error=True)
        is_error = isinstance(text, str) and text.lstrip().lower().startswith("error")
        return _text_result(text if isinstance(text, str) else str(text), is_error=is_error)


app = EdgarForegroundApp()


async def _verify_async() -> int:
    """Confirm the SEC accepts our User-Agent and the ticker map loads."""
    try:
        ticker_map = await eng.load_ticker_map()
    except Exception as exc:  # noqa: BLE001
        print(f"SEC EDGAR verification failed: {exc}", flush=True)
        return 1
    finally:
        client = eng._client
        if client is not None and not client.is_closed:
            await client.aclose()
    count = len(ticker_map or {})
    if count == 0:
        print("SEC EDGAR verification failed: empty ticker map.", flush=True)
        return 1
    print(
        f"SEC EDGAR reachable. Loaded {count} ticker mappings "
        f"using User-Agent '{config.get_user_agent()}'.",
        flush=True,
    )
    return 0


def verify() -> int:
    return asyncio.run(_verify_async())


def main() -> int:
    parser = argparse.ArgumentParser(description="SEC EDGAR foreground app")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify SEC reachability and the configured User-Agent, then exit.",
    )
    args = parser.parse_args()
    if args.verify:
        return verify()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
