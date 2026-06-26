"""Canonical foreground tool metadata for the SEC EDGAR app.

Each entry maps one of the 12 EDGAR engine functions to the name, title,
description, icon, and annotations the Truffle runtime registers. All tools are
read-only (they only fetch public SEC data), so every annotation sets
readOnlyHint=True / destructiveHint=False.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    title: str
    description: str
    icon: str
    annotations: dict[str, bool]


_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False}


TOOLS = [
    ToolDefinition(
        name="edgar_search_company",
        title="Search Company",
        description=(
            "Find a public company's CIK (SEC identifier) by ticker symbol or "
            "company name. Always call this first — every other tool needs a CIK.\n"
            "Parameters:\n"
            "- query (required): ticker (e.g. 'AAPL') or company name (e.g. 'Apple').\n"
            "- limit (optional): max matches to return; default 10.\n"
            "Example: edgar_search_company(query=\"Tesla\")"
        ),
        icon="magnifying-glass",
        annotations=_READ_ONLY,
    ),
    ToolDefinition(
        name="edgar_get_submissions",
        title="Filing History",
        description=(
            "Get a company's filing history (10-K, 10-Q, 8-K, proxy statements, "
            "etc.) with dates, accession numbers, and document links.\n"
            "Parameters:\n"
            "- cik (required): company CIK from edgar_search_company.\n"
            "- form_type (optional): filter to one form, e.g. '10-K'.\n"
            "- limit (optional): max filings; default 20.\n"
            "- response_format (optional): 'markdown' (default) or 'json'.\n"
            "Example: edgar_get_submissions(cik=\"320193\", form_type=\"10-K\", limit=5)"
        ),
        icon="files",
        annotations=_READ_ONLY,
    ),
    ToolDefinition(
        name="edgar_get_company_facts",
        title="Company Facts",
        description=(
            "Get all XBRL financial facts a company reports. Use without a "
            "concept_filter to discover which exact XBRL tags a company uses, "
            "then pull specifics with edgar_get_company_concept.\n"
            "Parameters:\n"
            "- cik (required): company CIK.\n"
            "- taxonomy (optional): e.g. 'us-gaap', 'dei'.\n"
            "- concept_filter (optional): keyword to narrow concepts, e.g. 'Revenue'.\n"
            "- response_format (optional): 'markdown' (default) or 'json'.\n"
            "Example: edgar_get_company_facts(cik=\"320193\", concept_filter=\"Revenue\")"
        ),
        icon="table",
        annotations=_READ_ONLY,
    ),
    ToolDefinition(
        name="edgar_get_company_concept",
        title="Concept Time Series",
        description=(
            "Get the full historical time series for a single XBRL concept for one "
            "company (e.g. Revenues, NetIncomeLoss, Assets, EarningsPerShareDiluted).\n"
            "Parameters:\n"
            "- cik (required): company CIK.\n"
            "- concept (required): XBRL concept tag, e.g. 'Revenues'.\n"
            "- taxonomy (optional): default 'us-gaap'.\n"
            "- response_format (optional): 'markdown' (default) or 'json'.\n"
            "Example: edgar_get_company_concept(cik=\"320193\", concept=\"NetIncomeLoss\")"
        ),
        icon="chart-line",
        annotations=_READ_ONLY,
    ),
    ToolDefinition(
        name="edgar_get_frames",
        title="Cross-Company Frame",
        description=(
            "Get one XBRL metric across all reporting companies for a single period "
            "— for peer benchmarking. Returns many companies' values for the metric.\n"
            "Parameters:\n"
            "- concept (required): XBRL concept tag, e.g. 'Revenues'.\n"
            "- period (required): a frame period, e.g. 'CY2023' or 'CY2023Q4I'.\n"
            "- unit (optional): default 'USD'.\n"
            "- limit (optional): max companies; default 50.\n"
            "- response_format (optional): 'markdown' (default) or 'json'.\n"
            "Example: edgar_get_frames(concept=\"Revenues\", period=\"CY2023\")"
        ),
        icon="chart-bar",
        annotations=_READ_ONLY,
    ),
    ToolDefinition(
        name="edgar_search_filings",
        title="Search Filings",
        description=(
            "Quick full-text search across all SEC filings since 2001. Use for "
            "simple keyword lookups; use edgar_full_text_search for company/ticker "
            "filtering, pagination, and match snippets.\n"
            "Parameters:\n"
            "- query (required): search phrase.\n"
            "- forms (optional): comma-separated form filter, e.g. '10-K,8-K'.\n"
            "- date_start / date_end (optional): YYYY-MM-DD bounds.\n"
            "- limit (optional): max results; default 10.\n"
            "Example: edgar_search_filings(query=\"artificial intelligence\", forms=\"10-K\")"
        ),
        icon="text-magnifying-glass",
        annotations=_READ_ONLY,
    ),
    ToolDefinition(
        name="edgar_get_filing_document",
        title="Read Filing",
        description=(
            "Fetch and read the text of a specific SEC filing document by URL. For "
            "large filings, prefer edgar_search_filing_content to jump to sections "
            "or edgar_extract_xbrl for structured financials — do NOT re-fetch with "
            "different offsets.\n"
            "Parameters:\n"
            "- url (required): filing document URL (from edgar_get_submissions).\n"
            "- max_length (optional): max characters to return.\n"
            "Example: edgar_get_filing_document(url=\"https://www.sec.gov/Archives/...\")"
        ),
        icon="file-text",
        annotations=_READ_ONLY,
    ),
    ToolDefinition(
        name="edgar_search_filing_content",
        title="Search Within Filing",
        description=(
            "Keyword-search WITHIN a single large filing and return only the "
            "matching sections with surrounding context. Fetches the entire filing "
            "(even 4MB+) but returns just the relevant windows — ideal for revenue "
            "tables, risk factors, segment data, or compensation.\n"
            "Parameters:\n"
            "- url (required): filing document URL.\n"
            "- search_terms (required): comma-separated terms, e.g. 'segment revenue,disaggregation'.\n"
            "- context_chars (optional): characters of context per match.\n"
            "- max_matches (optional): max matches to return.\n"
            "Example: edgar_search_filing_content(url=\"...\", search_terms=\"risk factors\")"
        ),
        icon="crosshair",
        annotations=_READ_ONLY,
    ),
    ToolDefinition(
        name="edgar_get_financial_statements",
        title="Financial Statements",
        description=(
            "Quick structured income statement, balance sheet, or cash flow built "
            "from the standard XBRL API. Use for a high-level GAAP overview. For "
            "modeling, segment detail, or custom-tag line items, use "
            "edgar_extract_xbrl instead.\n"
            "Parameters:\n"
            "- cik (required): company CIK.\n"
            "- statement (optional): 'income_statement', 'balance_sheet', or 'cash_flow'.\n"
            "- period_type (optional): 'annual' (default) or 'quarterly'.\n"
            "- years (optional): number of periods; default 5.\n"
            "- response_format (optional): 'markdown' (default) or 'json'.\n"
            "Example: edgar_get_financial_statements(cik=\"320193\", statement=\"income_statement\")"
        ),
        icon="calculator",
        annotations=_READ_ONLY,
    ),
    ToolDefinition(
        name="edgar_extract_xbrl",
        title="Extract XBRL (Modeling)",
        description=(
            "The primary tool for financial modeling. Parses iXBRL directly from an "
            "actual 10-K/10-Q filing, capturing custom taxonomy extensions, segment "
            "breakdowns, and line-item detail the standard XBRL API misses. Use this "
            "for 3-statement models, DCFs, and any detailed financial breakdown.\n"
            "Parameters:\n"
            "- cik (optional): company CIK (or pass url directly).\n"
            "- url (optional): a specific filing URL to parse.\n"
            "- form_type (optional): default '10-K'.\n"
            "- filing_date (optional): YYYY-MM-DD to target a specific year's filing.\n"
            "- filter (optional): keyword to narrow facts, e.g. 'Revenue'.\n"
            "- axis_filter (optional): segment axis, e.g. 'ProductOrServiceAxis', "
            "'StatementGeographicalAxis', 'StatementBusinessSegmentsAxis'.\n"
            "- include_standard (optional): include standard us-gaap facts; default true.\n"
            "- response_format (optional): 'markdown' (default) or 'json'.\n"
            "Example: edgar_extract_xbrl(cik=\"320193\", axis_filter=\"ProductOrServiceAxis\")"
        ),
        icon="brackets-curly",
        annotations=_READ_ONLY,
    ),
    ToolDefinition(
        name="edgar_raw_query",
        title="Raw SEC Query",
        description=(
            "Generic passthrough to any SEC EDGAR API endpoint not covered by the "
            "other tools (mutual fund data, insider feeds, custom XBRL queries). "
            "Only data.sec.gov, efts.sec.gov, and www.sec.gov URLs are allowed.\n"
            "Parameters:\n"
            "- url (required): full SEC API URL.\n"
            "- max_length (optional): max characters to return.\n"
            "- response_format (optional): 'markdown' (default) or 'json'.\n"
            "Example: edgar_raw_query(url=\"https://data.sec.gov/submissions/CIK0000320193.json\")"
        ),
        icon="terminal",
        annotations=_READ_ONLY,
    ),
    ToolDefinition(
        name="edgar_full_text_search",
        title="Advanced Full-Text Search",
        description=(
            "Advanced full-text search with ticker/CIK filtering (auto-resolves "
            "tickers to CIKs), pagination, company-name filtering, and match "
            "snippets. Use for research like 'find all 8-Ks mentioning goodwill "
            "impairment for MSFT in 2024'.\n"
            "Parameters:\n"
            "- query (required): search phrase.\n"
            "- tickers (optional): comma-separated tickers, e.g. 'MSFT,AAPL'.\n"
            "- entity_name / ciks (optional): filter by company name or CIK.\n"
            "- forms (optional): comma-separated form filter, e.g. '8-K'.\n"
            "- date_start / date_end (optional): YYYY-MM-DD filing-date bounds.\n"
            "- limit (optional): page size; start_from (optional): pagination offset.\n"
            "- include_snippets (optional): include matching text snippets; default true.\n"
            "Example: edgar_full_text_search(query=\"goodwill impairment\", tickers=\"MSFT\", forms=\"8-K\")"
        ),
        icon="binoculars",
        annotations=_READ_ONLY,
    ),
]

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}
