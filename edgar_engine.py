"""
SEC EDGAR MCP Server
====================
An MCP server that provides access to the SEC EDGAR database,
including company filings, XBRL financial data, and full-text search.

All APIs used are free and require no authentication.
SEC requires a User-Agent header with company name and contact email.
Rate limit: 10 requests/second.
"""

import json
import os
import re
import asyncio
from typing import Optional, List, Dict, Any
from enum import Enum
from collections import defaultdict

import httpx
from pydantic import BaseModel, Field, field_validator, ConfigDict
from mcp.server.fastmcp import FastMCP

# =============================================================================
# Configuration
# =============================================================================

# SEC EDGAR requires a User-Agent header identifying who is making requests
# (format: "Sample Company Name AdminContact@example.com"). On a Truffle device
# this is collected during install and provided via the SEC_EDGAR_USER_AGENT env
# var; see config.py. The fallback keeps local imports/tests working.
USER_AGENT = os.environ.get(
    "SEC_EDGAR_USER_AGENT", "Truffle EDGAR App admin@example.com"
).strip() or "Truffle EDGAR App admin@example.com"

SEC_BASE_URL = "https://data.sec.gov"
EFTS_BASE_URL = "https://efts.sec.gov/LATEST"
SEC_WWW_URL = "https://www.sec.gov"

# Rate limiting: SEC allows 10 req/sec; we stay conservative
REQUEST_DELAY = 0.15  # seconds between requests

# =============================================================================
# Server Initialization
# =============================================================================

mcp = FastMCP("edgar_mcp")

# =============================================================================
# Shared HTTP Client & Helpers
# =============================================================================

_client: Optional[httpx.AsyncClient] = None
_last_request_time: float = 0


async def get_client() -> httpx.AsyncClient:
    """Get or create a shared async HTTP client with SEC-required headers."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=90.0,
            follow_redirects=True,
        )
    return _client


async def sec_request(url: str) -> Dict[str, Any]:
    """Make a rate-limited request to SEC APIs with error handling."""
    global _last_request_time

    # Rate limiting
    now = asyncio.get_event_loop().time()
    elapsed = now - _last_request_time
    if elapsed < REQUEST_DELAY:
        await asyncio.sleep(REQUEST_DELAY - elapsed)

    client = await get_client()
    _last_request_time = asyncio.get_event_loop().time()

    response = await client.get(url)
    response.raise_for_status()
    return response.json()


def pad_cik(cik: str) -> str:
    """Pad a CIK number to 10 digits with leading zeros."""
    return cik.strip().lstrip("0").zfill(10)


def handle_api_error(e: Exception, context: str = "") -> str:
    """Consistent error formatting across all tools."""
    prefix = f"Error ({context}): " if context else "Error: "
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 404:
            return f"{prefix}Resource not found. Check that the CIK, ticker, or concept is correct."
        elif status == 403:
            return f"{prefix}Access denied. The SEC may be rate-limiting requests. Wait a moment and retry."
        elif status == 429:
            return f"{prefix}Rate limit exceeded. Wait 10+ seconds before retrying."
        return f"{prefix}HTTP {status} from SEC API."
    elif isinstance(e, httpx.TimeoutException):
        return f"{prefix}Request timed out. The SEC servers may be under heavy load. Retry shortly."
    return f"{prefix}{type(e).__name__}: {str(e)}"


# =============================================================================
# Ticker / CIK Lookup Cache
# =============================================================================

_ticker_map: Optional[Dict[str, Dict[str, Any]]] = None


async def load_ticker_map() -> Dict[str, Dict[str, Any]]:
    """Load and cache the SEC ticker-to-CIK mapping file."""
    global _ticker_map
    if _ticker_map is not None:
        return _ticker_map

    url = f"{SEC_WWW_URL}/files/company_tickers.json"
    data = await sec_request(url)

    # Build lookup by ticker (uppercase) and by company name (lowercase)
    _ticker_map = {}
    for _key, entry in data.items():
        ticker = entry.get("ticker", "").upper()
        _ticker_map[ticker] = {
            "cik": str(entry.get("cik_str", "")),
            "ticker": ticker,
            "name": entry.get("title", ""),
        }
    return _ticker_map


# =============================================================================
# Financial Statement Concept Mappings
# =============================================================================

FINANCIAL_STATEMENT_CONCEPTS = {
    "income_statement": {
        "Revenue": [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ],
        "CostOfRevenue": [
            "CostOfGoodsAndServicesSold",
            "CostOfGoodsSold",
            "CostOfRevenue",
        ],
        "GrossProfit": [
            "GrossProfit",
        ],
        "ResearchAndDevelopment": [
            "ResearchAndDevelopmentExpense",
        ],
        "SellingGeneralAndAdmin": [
            "SellingGeneralAndAdministrativeExpense",
        ],
        "OperatingExpenses": [
            "OperatingExpenses",
        ],
        "OperatingIncome": [
            "OperatingIncomeLoss",
        ],
        "InterestExpense": [
            "InterestExpense",
            "InterestExpenseDebt",
        ],
        "InterestIncome": [
            "InvestmentIncomeInterest",
            "InterestIncomeExpenseNet",
        ],
        "OtherIncomeExpense": [
            "OtherNonoperatingIncomeExpense",
            "NonoperatingIncomeExpense",
        ],
        "IncomeBeforeTax": [
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        ],
        "IncomeTaxExpense": [
            "IncomeTaxExpenseBenefit",
        ],
        "NetIncome": [
            "NetIncomeLoss",
        ],
        "EPSBasic": [
            "EarningsPerShareBasic",
        ],
        "EPSDiluted": [
            "EarningsPerShareDiluted",
        ],
        "SharesOutstandingBasic": [
            "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
            "WeightedAverageNumberOfSharesOutstandingBasic",
        ],
        "SharesOutstandingDiluted": [
            "WeightedAverageNumberOfDilutedSharesOutstanding",
        ],
    },
    "balance_sheet": {
        "CashAndEquivalents": [
            "CashAndCashEquivalentsAtCarryingValue",
            "Cash",
        ],
        "ShortTermInvestments": [
            "ShortTermInvestments",
            "MarketableSecuritiesCurrent",
            "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        ],
        "AccountsReceivable": [
            "AccountsReceivableNetCurrent",
            "AccountsReceivableNet",
        ],
        "Inventory": [
            "InventoryNet",
        ],
        "OtherCurrentAssets": [
            "OtherAssetsCurrent",
        ],
        "TotalCurrentAssets": [
            "AssetsCurrent",
        ],
        "PropertyPlantEquipment": [
            "PropertyPlantAndEquipmentNet",
        ],
        "Goodwill": [
            "Goodwill",
        ],
        "IntangibleAssets": [
            "IntangibleAssetsNetExcludingGoodwill",
        ],
        "LongTermInvestments": [
            "LongTermInvestments",
            "MarketableSecuritiesNoncurrent",
            "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
        ],
        "OtherNonCurrentAssets": [
            "OtherAssetsNoncurrent",
        ],
        "TotalAssets": [
            "Assets",
        ],
        "AccountsPayable": [
            "AccountsPayableCurrent",
        ],
        "ShortTermDebt": [
            "ShortTermBorrowings",
            "CommercialPaper",
        ],
        "CurrentPortionLongTermDebt": [
            "LongTermDebtCurrent",
        ],
        "DeferredRevenueCurrent": [
            "ContractWithCustomerLiabilityCurrent",
            "DeferredRevenueCurrent",
        ],
        "OtherCurrentLiabilities": [
            "OtherLiabilitiesCurrent",
        ],
        "TotalCurrentLiabilities": [
            "LiabilitiesCurrent",
        ],
        "LongTermDebt": [
            "LongTermDebtNoncurrent",
            "LongTermDebt",
        ],
        "DeferredRevenueNonCurrent": [
            "ContractWithCustomerLiabilityNoncurrent",
            "DeferredRevenueNoncurrent",
        ],
        "OtherNonCurrentLiabilities": [
            "OtherLiabilitiesNoncurrent",
        ],
        "TotalLiabilities": [
            "Liabilities",
        ],
        "CommonStock": [
            "CommonStocksIncludingAdditionalPaidInCapital",
            "CommonStockValue",
        ],
        "RetainedEarnings": [
            "RetainedEarningsAccumulatedDeficit",
        ],
        "AccumulatedOtherComprehensiveIncome": [
            "AccumulatedOtherComprehensiveIncomeLossNetOfTax",
        ],
        "TotalStockholdersEquity": [
            "StockholdersEquity",
        ],
        "TotalLiabilitiesAndEquity": [
            "LiabilitiesAndStockholdersEquity",
        ],
    },
    "cash_flow": {
        "NetIncome": [
            "NetIncomeLoss",
        ],
        "DepreciationAmortization": [
            "DepreciationDepletionAndAmortization",
            "DepreciationAndAmortization",
            "Depreciation",
        ],
        "StockBasedCompensation": [
            "ShareBasedCompensation",
        ],
        "DeferredIncomeTax": [
            "DeferredIncomeTaxExpenseBenefit",
        ],
        "ChangesInAccountsReceivable": [
            "IncreaseDecreaseInAccountsReceivable",
        ],
        "ChangesInInventory": [
            "IncreaseDecreaseInInventories",
        ],
        "ChangesInAccountsPayable": [
            "IncreaseDecreaseInAccountsPayable",
        ],
        "ChangesInDeferredRevenue": [
            "IncreaseDecreaseInContractWithCustomerLiability",
            "IncreaseDecreaseInDeferredRevenue",
        ],
        "OperatingCashFlow": [
            "NetCashProvidedByUsedInOperatingActivities",
        ],
        "CapitalExpenditures": [
            "PaymentsToAcquirePropertyPlantAndEquipment",
        ],
        "AcquisitionsNet": [
            "PaymentsToAcquireBusinessesNetOfCashAcquired",
        ],
        "PurchaseOfInvestments": [
            "PaymentsToAcquireInvestments",
            "PaymentsToAcquireAvailableForSaleSecuritiesDebt",
        ],
        "SaleOfInvestments": [
            "ProceedsFromSaleAndMaturityOfMarketableSecurities",
            "ProceedsFromMaturitiesPrepaymentsAndCallsOfAvailableForSaleSecurities",
        ],
        "InvestingCashFlow": [
            "NetCashProvidedByUsedInInvestingActivities",
        ],
        "DebtIssuance": [
            "ProceedsFromIssuanceOfLongTermDebt",
        ],
        "DebtRepayment": [
            "RepaymentsOfLongTermDebt",
        ],
        "ShareRepurchases": [
            "PaymentsForRepurchaseOfCommonStock",
        ],
        "DividendsPaid": [
            "PaymentsOfDividends",
            "PaymentsOfDividendsCommonStock",
        ],
        "FinancingCashFlow": [
            "NetCashProvidedByUsedInFinancingActivities",
        ],
    },
}


# =============================================================================
# iXBRL Parsing Helpers
# =============================================================================

async def resolve_filing_url(cik: str, form_type: str = "10-K", filing_date: Optional[str] = None) -> Dict[str, str]:
    """Resolve a CIK + form type to the primary document URL of the matching filing."""
    cik_padded = pad_cik(cik)
    url = f"{SEC_BASE_URL}/submissions/CIK{cik_padded}.json"
    data = await sec_request(url)

    company_name = data.get("name", "Unknown")
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    cik_clean = cik.strip().lstrip("0")

    for i in range(len(forms)):
        if forms[i] != form_type:
            continue
        if filing_date and dates[i] != filing_date:
            continue
        if i >= len(primary_docs) or not primary_docs[i]:
            continue

        accession_clean = accessions[i].replace("-", "")
        doc_url = (
            f"{SEC_WWW_URL}/Archives/edgar/data/{cik_clean}"
            f"/{accession_clean}/{primary_docs[i]}"
        )
        return {
            "url": doc_url,
            "form_type": forms[i],
            "filing_date": dates[i],
            "company_name": company_name,
        }

    raise ValueError(
        f"No {form_type} filing found for CIK {cik}"
        + (f" on date {filing_date}" if filing_date else "")
    )


def parse_ixbrl_contexts(html: str) -> Dict[str, Dict[str, Any]]:
    """Parse xbrli:context elements from iXBRL HTML."""
    contexts = {}
    ctx_pattern = re.compile(
        r'<xbrli:context\s+id="([^"]+)">(.*?)</xbrli:context>',
        re.DOTALL,
    )
    for match in ctx_pattern.finditer(html):
        ctx_id = match.group(1)
        body = match.group(2)

        period = {}
        start_match = re.search(r'<xbrli:startDate>([^<]+)</xbrli:startDate>', body)
        end_match = re.search(r'<xbrli:endDate>([^<]+)</xbrli:endDate>', body)
        instant_match = re.search(r'<xbrli:instant>([^<]+)</xbrli:instant>', body)

        if start_match and end_match:
            period = {"start": start_match.group(1), "end": end_match.group(1)}
        elif instant_match:
            period = {"instant": instant_match.group(1)}

        dimensions = {}
        dim_pattern = re.compile(
            r'<xbrldi:explicitMember\s+dimension="([^"]+)">([^<]+)</xbrldi:explicitMember>'
        )
        for dim_match in dim_pattern.finditer(body):
            axis = dim_match.group(1)
            member = dim_match.group(2)
            axis_clean = axis.split(":")[-1] if ":" in axis else axis
            dimensions[axis_clean] = member

        contexts[ctx_id] = {"period": period, "dimensions": dimensions}

    return contexts


def parse_ixbrl_units(html: str) -> Dict[str, str]:
    """Parse xbrli:unit elements to map unit IDs to human-readable names."""
    units = {}
    unit_pattern = re.compile(
        r'<xbrli:unit\s+id="([^"]+)">(.*?)</xbrli:unit>',
        re.DOTALL,
    )
    for match in unit_pattern.finditer(html):
        unit_id = match.group(1)
        body = match.group(2)
        measure_match = re.search(r'<xbrli:measure>([^<]+)</xbrli:measure>', body)
        if measure_match:
            measure = measure_match.group(1)
            if "USD" in measure:
                units[unit_id] = "USD"
            elif "shares" in measure.lower():
                units[unit_id] = "shares"
            else:
                units[unit_id] = measure
        else:
            num_match = re.search(r'<xbrli:unitNumerator>.*?<xbrli:measure>([^<]+)</xbrli:measure>', body, re.DOTALL)
            den_match = re.search(r'<xbrli:unitDenominator>.*?<xbrli:measure>([^<]+)</xbrli:measure>', body, re.DOTALL)
            if num_match and den_match:
                units[unit_id] = f"{num_match.group(1).split(':')[-1]}/{den_match.group(1).split(':')[-1]}"
            else:
                units[unit_id] = unit_id

    return units


def parse_ixbrl_facts(html: str, contexts: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Parse ix:nonFraction elements from iXBRL HTML."""
    facts = []
    fact_pattern = re.compile(
        r'<ix:nonFraction\s+([^>]*)>([^<]*)</ix:nonFraction>',
        re.DOTALL,
    )

    for match in fact_pattern.finditer(html):
        attrs_str = match.group(1)
        raw_value = match.group(2).strip()

        attrs = {}
        for attr_match in re.finditer(r'(\w+)="([^"]*)"', attrs_str):
            attrs[attr_match.group(1)] = attr_match.group(2)

        name = attrs.get("name", "")
        context_ref = attrs.get("contextRef", "")
        unit_ref = attrs.get("unitRef", "")
        scale = int(attrs.get("scale", "0"))

        if not name or not context_ref:
            continue

        clean_value = raw_value.replace(",", "").replace(" ", "")
        is_negative = False
        if clean_value.startswith("(") and clean_value.endswith(")"):
            clean_value = clean_value[1:-1]
            is_negative = True
        if clean_value.startswith("-"):
            clean_value = clean_value[1:]
            is_negative = True

        if attrs.get("sign") == "-":
            is_negative = not is_negative

        try:
            numeric_value = float(clean_value)
        except (ValueError, TypeError):
            continue

        if is_negative:
            numeric_value = -numeric_value

        if scale != 0:
            numeric_value = numeric_value * (10 ** scale)

        if numeric_value == int(numeric_value):
            numeric_value = int(numeric_value)

        if ":" in name:
            taxonomy, concept = name.split(":", 1)
        else:
            taxonomy, concept = "", name

        ctx = contexts.get(context_ref, {})
        period = ctx.get("period", {})
        dimensions = ctx.get("dimensions", {})

        facts.append({
            "concept": name,
            "taxonomy": taxonomy,
            "concept_name": concept,
            "value": numeric_value,
            "unit": unit_ref,
            "period": period,
            "dimensions": dimensions,
            "context_ref": context_ref,
        })

    return facts


# =============================================================================
# Enums & Input Models
# =============================================================================

class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


class EdgarSearchCompanyInput(BaseModel):
    """Input for searching companies by ticker or name."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description="Company ticker symbol (e.g., 'AAPL') or partial company name (e.g., 'Apple').",
        min_length=1,
        max_length=200,
    )
    limit: int = Field(
        default=10,
        description="Maximum number of results to return.",
        ge=1,
        le=50,
    )


class EdgarGetSubmissionsInput(BaseModel):
    """Input for retrieving a company's filing history."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    cik: str = Field(
        ...,
        description="Company CIK number (e.g., '320193' for Apple). Use edgar_search_company to find this.",
        min_length=1,
        max_length=20,
    )
    form_type: Optional[str] = Field(
        default=None,
        description="Filter by form type (e.g., '10-K', '10-Q', '8-K', 'DEF 14A'). Leave empty for all types.",
    )
    limit: int = Field(
        default=20,
        description="Maximum number of recent filings to return.",
        ge=1,
        le=100,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data.",
    )


class EdgarGetCompanyFactsInput(BaseModel):
    """Input for retrieving all XBRL financial facts for a company."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    cik: str = Field(
        ...,
        description="Company CIK number (e.g., '320193' for Apple).",
        min_length=1,
        max_length=20,
    )
    taxonomy: Optional[str] = Field(
        default=None,
        description="Filter by taxonomy: 'us-gaap', 'ifrs-full', 'dei', 'srt'. Leave empty for all.",
    )
    concept_filter: Optional[str] = Field(
        default=None,
        description=(
            "Filter facts by concept keyword (case-insensitive substring match). "
            "Examples: 'Revenue', 'Assets', 'NetIncome', 'EarningsPerShare'. "
            "Leave empty to return a summary of all available concepts."
        ),
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


class EdgarGetCompanyConceptInput(BaseModel):
    """Input for retrieving a specific XBRL concept across time for a company."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    cik: str = Field(
        ...,
        description="Company CIK number (e.g., '320193' for Apple).",
        min_length=1,
        max_length=20,
    )
    taxonomy: str = Field(
        default="us-gaap",
        description="Taxonomy: 'us-gaap', 'ifrs-full', 'dei', or 'srt'.",
    )
    concept: str = Field(
        ...,
        description=(
            "XBRL concept tag name. Common examples: "
            "'Revenues', 'RevenueFromContractWithCustomerExcludingAssessedTax', "
            "'NetIncomeLoss', 'Assets', 'Liabilities', 'StockholdersEquity', "
            "'EarningsPerShareBasic', 'OperatingIncomeLoss', 'CashAndCashEquivalentsAtCarryingValue'."
        ),
        min_length=1,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


class EdgarGetFramesInput(BaseModel):
    """Input for cross-company comparison of a financial metric in a specific period."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    taxonomy: str = Field(
        default="us-gaap",
        description="Taxonomy: 'us-gaap', 'ifrs-full', 'dei', or 'srt'.",
    )
    concept: str = Field(
        ...,
        description="XBRL concept tag (e.g., 'Revenues', 'NetIncomeLoss', 'Assets').",
        min_length=1,
    )
    unit: str = Field(
        default="USD",
        description="Unit of measure (e.g., 'USD', 'shares', 'USD-per-shares', 'pure').",
    )
    period: str = Field(
        ...,
        description=(
            "Calendar period. Format: 'CY2024' for annual, 'CY2024Q1' for quarterly duration, "
            "'CY2024Q1I' for instantaneous (balance sheet). Example: 'CY2023' for full-year 2023."
        ),
        min_length=4,
    )
    limit: int = Field(
        default=20,
        description="Number of top companies to return (sorted by value descending).",
        ge=1,
        le=100,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


class EdgarSearchFilingsInput(BaseModel):
    """Input for full-text search across SEC filings."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description=(
            "Search query. Supports boolean operators: AND (default between terms), "
            "OR, NOT, exact phrases in quotes. "
            "Examples: 'artificial intelligence', '\"climate risk\" AND 10-K', 'SpaceX'."
        ),
        min_length=1,
        max_length=500,
    )
    forms: Optional[str] = Field(
        default=None,
        description="Comma-separated form types to filter (e.g., '10-K,10-Q,8-K'). Leave empty for all.",
    )
    date_start: Optional[str] = Field(
        default=None,
        description="Start date for filing date range (YYYY-MM-DD format). Example: '2024-01-01'.",
    )
    date_end: Optional[str] = Field(
        default=None,
        description="End date for filing date range (YYYY-MM-DD format). Example: '2024-12-31'.",
    )
    limit: int = Field(
        default=10,
        description="Maximum number of results to return.",
        ge=1,
        le=50,
    )


class EdgarGetFilingDocumentInput(BaseModel):
    """Input for fetching the text content of a specific filing document."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url: str = Field(
        ...,
        description=(
            "Full URL of the filing document on sec.gov. "
            "Get this from edgar_get_submissions or edgar_search_filings results. "
            "Example: 'https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm'"
        ),
        min_length=10,
    )
    max_length: int = Field(
        default=50000,
        description="Maximum number of characters to return from the document. Filings can be very large.",
        ge=1000,
        le=200000,
    )


class EdgarSearchFilingContentInput(BaseModel):
    """Input for searching within a specific SEC filing document for relevant sections."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url: str = Field(
        ...,
        description=(
            "Full URL of the filing document on sec.gov. "
            "Get this from edgar_get_submissions or edgar_search_filings results."
        ),
        min_length=10,
    )
    search_terms: str = Field(
        ...,
        description=(
            "Comma-separated keywords to search for within the filing. "
            "Returns surrounding context (up to 3000 chars) for each match. "
            "Examples: 'segment revenue,disaggregation' or 'goodwill,impairment' "
            "or 'risk factors,cybersecurity'"
        ),
        min_length=1,
    )
    context_chars: int = Field(
        default=3000,
        description="Characters of context to return around each match. Default: 3000.",
        ge=500,
        le=10000,
    )
    max_matches: int = Field(
        default=10,
        description="Maximum number of matching sections to return. Default: 10.",
        ge=1,
        le=30,
    )


class EdgarGetFinancialStatementsInput(BaseModel):
    """Input for retrieving structured financial statements."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    cik: str = Field(
        ...,
        description="Company CIK number (e.g., '320193' for Apple).",
        min_length=1,
        max_length=20,
    )
    statement: str = Field(
        default="all",
        description=(
            "Which statement to return: 'income', 'balance_sheet', 'cash_flow', or 'all'. "
            "Default: 'all' returns all three statements."
        ),
    )
    period_type: str = Field(
        default="annual",
        description="Period type: 'annual' for yearly data or 'quarterly'. Default: 'annual'.",
    )
    years: int = Field(
        default=5,
        description="Number of years of data to return. Default: 5, max: 10.",
        ge=1,
        le=10,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


class EdgarExtractXBRLInput(BaseModel):
    """Input for extracting iXBRL facts from a filing document."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url: Optional[str] = Field(
        default=None,
        description=(
            "Direct URL to a filing document on sec.gov. "
            "If provided, this URL is used directly. "
            "If omitted, provide cik to auto-resolve the latest filing."
        ),
    )
    cik: Optional[str] = Field(
        default=None,
        description="Company CIK number. Used with form_type to auto-resolve the filing URL.",
    )
    form_type: str = Field(
        default="10-K",
        description="Filing form type to look up when using cik. Default: '10-K'.",
    )
    filing_date: Optional[str] = Field(
        default=None,
        description="Specific filing date (YYYY-MM-DD). If omitted, uses the most recent filing.",
    )
    filter: Optional[str] = Field(
        default=None,
        description=(
            "Keyword filter for concept names (case-insensitive). "
            "Examples: 'iPhone', 'Revenue', 'Segment', 'Geographic'."
        ),
    )
    axis_filter: Optional[str] = Field(
        default=None,
        description=(
            "Filter by XBRL dimension axis name (case-insensitive substring match). "
            "Examples: 'ProductOrServiceAxis', 'StatementBusinessSegmentsAxis', 'StatementGeographicalAxis'."
        ),
    )
    include_standard: bool = Field(
        default=False,
        description="If true, include standard us-gaap/dei/srt facts. Default: false (only custom extension facts).",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


# =============================================================================
# Tool Implementations
# =============================================================================

@mcp.tool(
    name="edgar_search_company",
    annotations={
        "title": "Search SEC EDGAR for Companies",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def edgar_search_company(params: EdgarSearchCompanyInput) -> str:
    """Search for a company by ticker symbol or name to find its CIK number.

    The CIK (Central Index Key) is required for most other EDGAR tools.
    Searches the SEC's official ticker-to-company mapping.

    Args:
        params (EdgarSearchCompanyInput): Search parameters containing:
            - query (str): Ticker symbol or partial company name
            - limit (int): Max results to return (default: 10)

    Returns:
        str: Markdown-formatted list of matching companies with CIK, ticker, and name.
    """
    try:
        ticker_map = await load_ticker_map()
    except Exception as e:
        return handle_api_error(e, "loading company data")

    query_upper = params.query.upper()
    query_lower = params.query.lower()
    matches = []

    # Exact ticker match first
    if query_upper in ticker_map:
        entry = ticker_map[query_upper]
        matches.append(entry)

    # Then partial matches on ticker and name
    for ticker, entry in ticker_map.items():
        if entry in matches:
            continue
        if query_upper in ticker or query_lower in entry["name"].lower():
            matches.append(entry)
        if len(matches) >= params.limit:
            break

    if not matches:
        return f"No companies found matching '{params.query}'. Try a different ticker or name."

    lines = [f"## SEC EDGAR Company Search: '{params.query}'\n"]
    lines.append(f"Found {len(matches)} result(s):\n")
    for m in matches:
        lines.append(f"- **{m['name']}** — Ticker: `{m['ticker']}`, CIK: `{m['cik']}`")

    lines.append(
        "\n*Use the CIK number with other EDGAR tools to retrieve filings and financial data.*"
    )
    return "\n".join(lines)


@mcp.tool(
    name="edgar_get_submissions",
    annotations={
        "title": "Get Company Filing History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def edgar_get_submissions(params: EdgarGetSubmissionsInput) -> str:
    """Retrieve the filing history (submissions) for a company from SEC EDGAR.

    Returns recent filings with form type, filing date, accession number, and
    links to the filing documents. Use edgar_search_company first to find the CIK.

    Args:
        params (EdgarGetSubmissionsInput): Parameters containing:
            - cik (str): Company CIK number
            - form_type (Optional[str]): Filter by form type (e.g., '10-K')
            - limit (int): Max filings to return (default: 20)
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: Filing history in the requested format.
    """
    cik_padded = pad_cik(params.cik)
    url = f"{SEC_BASE_URL}/submissions/CIK{cik_padded}.json"

    try:
        data = await sec_request(url)
    except Exception as e:
        return handle_api_error(e, "fetching submissions")

    company_name = data.get("name", "Unknown")
    tickers = data.get("tickers", [])
    recent = data.get("filings", {}).get("recent", {})

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])

    # Build filing records
    filings = []
    for i in range(len(forms)):
        if params.form_type and forms[i] != params.form_type:
            continue

        accession_clean = accessions[i].replace("-", "")
        doc_url = (
            f"{SEC_WWW_URL}/Archives/edgar/data/{params.cik.lstrip('0')}"
            f"/{accession_clean}/{primary_docs[i]}"
        ) if i < len(primary_docs) and primary_docs[i] else ""

        filings.append({
            "form": forms[i],
            "date": dates[i],
            "accession": accessions[i],
            "description": descriptions[i] if i < len(descriptions) else "",
            "url": doc_url,
        })

        if len(filings) >= params.limit:
            break

    if params.response_format == ResponseFormat.JSON:
        return json.dumps({
            "company": company_name,
            "cik": params.cik,
            "tickers": tickers,
            "total_filings_returned": len(filings),
            "filings": filings,
        }, indent=2)

    # Markdown
    ticker_str = ", ".join(tickers) if tickers else "N/A"
    lines = [
        f"## Filing History: {company_name}",
        f"**CIK:** {params.cik} | **Ticker(s):** {ticker_str}",
        f"**Showing:** {len(filings)} filings"
        + (f" (filtered to {params.form_type})" if params.form_type else ""),
        "",
    ]

    for f in filings:
        link = f" — [View]({f['url']})" if f["url"] else ""
        lines.append(f"- **{f['form']}** ({f['date']}): {f['description']}{link}")

    return "\n".join(lines)


@mcp.tool(
    name="edgar_get_company_facts",
    annotations={
        "title": "Get Company XBRL Financial Facts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def edgar_get_company_facts(params: EdgarGetCompanyFactsInput) -> str:
    """Retrieve all XBRL financial facts reported by a company to the SEC.

    This returns structured financial data extracted from 10-K, 10-Q, 8-K, and
    other filings. Facts include revenue, net income, assets, liabilities, EPS,
    and hundreds of other standard financial concepts.

    Use concept_filter to search for specific metrics, or leave empty to get
    a summary of all available concepts.

    Args:
        params (EdgarGetCompanyFactsInput): Parameters containing:
            - cik (str): Company CIK number
            - taxonomy (Optional[str]): Filter by taxonomy
            - concept_filter (Optional[str]): Keyword filter for concept names
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: Financial facts in the requested format.
    """
    cik_padded = pad_cik(params.cik)
    url = f"{SEC_BASE_URL}/api/xbrl/companyfacts/CIK{cik_padded}.json"

    try:
        data = await sec_request(url)
    except Exception as e:
        return handle_api_error(e, "fetching company facts")

    company_name = data.get("entityName", "Unknown")
    facts = data.get("facts", {})

    # If no filter, return a summary of available concepts
    if not params.concept_filter:
        summary = {}
        for taxonomy_name, concepts in facts.items():
            if params.taxonomy and taxonomy_name != params.taxonomy:
                continue
            concept_names = sorted(concepts.keys())
            summary[taxonomy_name] = {
                "count": len(concept_names),
                "concepts": concept_names[:50],  # First 50 for brevity
                "has_more": len(concept_names) > 50,
            }

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"company": company_name, "cik": params.cik, "taxonomies": summary}, indent=2)

        lines = [f"## Available Financial Concepts: {company_name}\n"]
        for tax, info in summary.items():
            lines.append(f"### {tax} ({info['count']} concepts)")
            lines.append(", ".join(f"`{c}`" for c in info["concepts"]))
            if info["has_more"]:
                lines.append(f"\n*...and {info['count'] - 50} more. Use concept_filter to search.*")
            lines.append("")

        lines.append(
            "*Use `concept_filter` to search for specific metrics "
            "(e.g., 'Revenue', 'NetIncome', 'Assets').*"
        )
        return "\n".join(lines)

    # Filter concepts by keyword
    filter_lower = params.concept_filter.lower()
    matched = {}
    for taxonomy_name, concepts in facts.items():
        if params.taxonomy and taxonomy_name != params.taxonomy:
            continue
        for concept_name, concept_data in concepts.items():
            if filter_lower in concept_name.lower():
                label = concept_data.get("label", concept_name)
                description = concept_data.get("description", "")
                units = concept_data.get("units", {})

                # Get the most recent values
                recent_values = []
                for unit_name, unit_facts in units.items():
                    for fact in sorted(unit_facts, key=lambda x: x.get("end", ""), reverse=True)[:5]:
                        recent_values.append({
                            "value": fact.get("val"),
                            "unit": unit_name,
                            "period_end": fact.get("end", ""),
                            "period_start": fact.get("start", ""),
                            "form": fact.get("form", ""),
                            "filed": fact.get("filed", ""),
                            "fiscal_year": fact.get("fy", ""),
                            "fiscal_period": fact.get("fp", ""),
                        })

                matched[f"{taxonomy_name}/{concept_name}"] = {
                    "label": label,
                    "description": description,
                    "recent_values": recent_values[:10],
                }

    if not matched:
        return (
            f"No concepts matching '{params.concept_filter}' found for {company_name}. "
            "Try a broader search term or check available concepts without a filter."
        )

    if params.response_format == ResponseFormat.JSON:
        return json.dumps({
            "company": company_name,
            "cik": params.cik,
            "filter": params.concept_filter,
            "matched_concepts": matched,
        }, indent=2)

    lines = [f"## Financial Facts: {company_name}", f"**Filter:** '{params.concept_filter}'\n"]
    for key, info in matched.items():
        lines.append(f"### {key}")
        lines.append(f"*{info['label']}*")
        if info["description"]:
            lines.append(f"> {info['description'][:200]}")
        lines.append("")
        for v in info["recent_values"]:
            val = v["value"]
            if isinstance(val, (int, float)) and abs(val) >= 1_000_000:
                val_str = f"${val:,.0f}" if v["unit"] == "USD" else f"{val:,.0f}"
            else:
                val_str = str(val)
            period = f"{v['period_start']} to {v['period_end']}" if v["period_start"] else v["period_end"]
            lines.append(
                f"- {val_str} ({v['unit']}) — {period} [{v['form']} filed {v['filed']}]"
            )
        lines.append("")

    return "\n".join(lines)


@mcp.tool(
    name="edgar_get_company_concept",
    annotations={
        "title": "Get Specific Financial Concept Over Time",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def edgar_get_company_concept(params: EdgarGetCompanyConceptInput) -> str:
    """Retrieve all historical values for a specific XBRL financial concept for a company.

    Returns a time series of a single financial metric (e.g., Revenue or Net Income)
    across all periods the company has reported it. Useful for building trend analyses
    and financial models.

    Args:
        params (EdgarGetCompanyConceptInput): Parameters containing:
            - cik (str): Company CIK number
            - taxonomy (str): Taxonomy (default: 'us-gaap')
            - concept (str): XBRL concept tag name (e.g., 'Revenues')
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: Time series of the financial concept in the requested format.
    """
    cik_padded = pad_cik(params.cik)
    url = (
        f"{SEC_BASE_URL}/api/xbrl/companyconcept"
        f"/CIK{cik_padded}/{params.taxonomy}/{params.concept}.json"
    )

    try:
        data = await sec_request(url)
    except Exception as e:
        return handle_api_error(e, "fetching company concept")

    company_name = data.get("entityName", "Unknown")
    label = data.get("label", params.concept)
    description = data.get("description", "")
    units = data.get("units", {})

    if params.response_format == ResponseFormat.JSON:
        return json.dumps({
            "company": company_name,
            "cik": params.cik,
            "concept": params.concept,
            "taxonomy": params.taxonomy,
            "label": label,
            "description": description,
            "units": units,
        }, indent=2)

    lines = [
        f"## {label}: {company_name}",
        f"**Concept:** `{params.taxonomy}/{params.concept}`",
    ]
    if description:
        lines.append(f"> {description[:300]}")
    lines.append("")

    for unit_name, facts in units.items():
        lines.append(f"### Unit: {unit_name}\n")

        # Sort by end date descending, deduplicate by period
        sorted_facts = sorted(facts, key=lambda x: x.get("end", ""), reverse=True)
        seen_periods = set()
        for f in sorted_facts:
            period_key = f"{f.get('start', '')}-{f.get('end', '')}-{f.get('form', '')}"
            if period_key in seen_periods:
                continue
            seen_periods.add(period_key)

            val = f.get("val")
            if isinstance(val, (int, float)) and abs(val) >= 1_000_000:
                val_str = f"${val:,.0f}" if "USD" in unit_name.upper() else f"{val:,.0f}"
            else:
                val_str = str(val)

            period = (
                f"{f.get('start', '?')} to {f.get('end', '?')}"
                if f.get("start")
                else f"as of {f.get('end', '?')}"
            )
            lines.append(
                f"- {val_str} — {period} "
                f"[{f.get('form', '?')}, FY{f.get('fy', '?')}{f.get('fp', '')} "
                f"filed {f.get('filed', '?')}]"
            )

            if len(seen_periods) >= 20:
                lines.append(f"\n*Showing 20 of {len(facts)} data points. Use JSON format for complete data.*")
                break

        lines.append("")

    return "\n".join(lines)


@mcp.tool(
    name="edgar_get_frames",
    annotations={
        "title": "Cross-Company Financial Comparison",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def edgar_get_frames(params: EdgarGetFramesInput) -> str:
    """Compare a financial metric across all reporting companies for a specific period.

    Uses the XBRL Frames API to get the same concept (e.g., Revenue) for many
    companies in the same calendar period. Useful for peer comparison and
    industry benchmarking.

    Args:
        params (EdgarGetFramesInput): Parameters containing:
            - taxonomy (str): Taxonomy (default: 'us-gaap')
            - concept (str): XBRL concept tag (e.g., 'Revenues')
            - unit (str): Unit (default: 'USD')
            - period (str): Calendar period (e.g., 'CY2023')
            - limit (int): Number of top companies (default: 20)
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: Ranked list of companies by the specified metric.
    """
    url = (
        f"{SEC_BASE_URL}/api/xbrl/frames"
        f"/{params.taxonomy}/{params.concept}/{params.unit}/{params.period}.json"
    )

    try:
        data = await sec_request(url)
    except Exception as e:
        return handle_api_error(e, "fetching frames data")

    label = data.get("label", params.concept)
    description = data.get("description", "")
    frame_data = data.get("data", [])

    # Sort by value descending
    sorted_data = sorted(frame_data, key=lambda x: abs(x.get("val", 0)), reverse=True)
    top = sorted_data[: params.limit]

    if params.response_format == ResponseFormat.JSON:
        return json.dumps({
            "concept": params.concept,
            "taxonomy": params.taxonomy,
            "unit": params.unit,
            "period": params.period,
            "label": label,
            "total_companies": len(frame_data),
            "top_companies": top,
        }, indent=2)

    lines = [
        f"## {label} — {params.period}",
        f"**Concept:** `{params.taxonomy}/{params.concept}` | **Unit:** {params.unit}",
        f"**Total reporting companies:** {len(frame_data)}\n",
        f"### Top {len(top)} by value:\n",
    ]

    for i, entry in enumerate(top, 1):
        val = entry.get("val", 0)
        if isinstance(val, (int, float)) and abs(val) >= 1_000_000:
            val_str = f"${val / 1e9:,.2f}B" if abs(val) >= 1e9 else f"${val / 1e6:,.1f}M"
        else:
            val_str = str(val)
        name = entry.get("entityName", "Unknown")
        cik = entry.get("cik", "")
        lines.append(f"{i}. **{name}** (CIK: {cik}) — {val_str}")

    return "\n".join(lines)


@mcp.tool(
    name="edgar_search_filings",
    annotations={
        "title": "Full-Text Search SEC Filings",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def edgar_search_filings(params: EdgarSearchFilingsInput) -> str:
    """Search the full text of all SEC EDGAR filings since 2001.

    Searches across the content of filings including exhibits and attachments.
    Supports boolean operators (AND, OR, NOT) and exact phrase matching with quotes.
    Returns metadata and links for matching filings.

    Args:
        params (EdgarSearchFilingsInput): Parameters containing:
            - query (str): Search query with optional boolean operators
            - forms (Optional[str]): Comma-separated form types to filter
            - date_start (Optional[str]): Start date (YYYY-MM-DD)
            - date_end (Optional[str]): End date (YYYY-MM-DD)
            - limit (int): Max results (default: 10)

    Returns:
        str: Markdown-formatted search results with filing metadata and links.
    """
    # Build EFTS query parameters
    query_params: Dict[str, Any] = {
        "q": params.query,
        "from": 0,
        "size": params.limit,
    }

    if params.forms:
        query_params["forms"] = params.forms
    if params.date_start or params.date_end:
        start = params.date_start or "2001-01-01"
        end = params.date_end or "2099-12-31"
        query_params["dateRange"] = "custom"
        query_params["startdt"] = start
        query_params["enddt"] = end

    # Build URL with query params
    param_str = "&".join(f"{k}={v}" for k, v in query_params.items())
    url = f"{EFTS_BASE_URL}/search-index?{param_str}"

    try:
        data = await sec_request(url)
    except Exception as e:
        return handle_api_error(e, "searching filings")

    hits = data.get("hits", {})
    total = hits.get("total", {}).get("value", 0)
    results = hits.get("hits", [])

    if not results:
        return f"No filings found matching '{params.query}'. Try broader search terms or different date range."

    lines = [
        f"## EDGAR Full-Text Search: '{params.query}'",
        f"**Total matches:** {total:,} | **Showing:** {len(results)}\n",
    ]

    for r in results:
        source = r.get("_source", {})
        file_date = source.get("file_date", "N/A")
        form_type = source.get("form_type", "N/A")
        entity_name = source.get("entity_name", "Unknown")
        file_num = source.get("file_num", "")
        display_names = source.get("display_names", [])

        # Build filing URL from accession number
        file_url = ""
        accession = r.get("_id", "")
        if accession:
            filing_page = f"{SEC_WWW_URL}/cgi-bin/browse-edgar?action=getcompany&filenum={file_num}&type=&dateb=&owner=include&count=40"
            file_url = f"{SEC_WWW_URL}/Archives/edgar/data/{source.get('file_num', '')}"

        names = ", ".join(display_names) if display_names else entity_name
        lines.append(f"- **{form_type}** ({file_date}) — {names}")
        if accession:
            lines.append(f"  Accession: `{accession}`")

    return "\n".join(lines)


@mcp.tool(
    name="edgar_get_filing_document",
    annotations={
        "title": "Fetch Filing Document Content",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def edgar_get_filing_document(params: EdgarGetFilingDocumentInput) -> str:
    """Fetch and return the text content of a specific SEC filing document.

    Retrieves the raw HTML/text content of a filing and strips HTML tags
    for readability. Use URLs from edgar_get_submissions or edgar_search_filings.

    Args:
        params (EdgarGetFilingDocumentInput): Parameters containing:
            - url (str): Full URL of the filing document on sec.gov
            - max_length (int): Max characters to return (default: 50000)

    Returns:
        str: Text content of the filing document (HTML tags stripped).
    """
    if not params.url.startswith("https://www.sec.gov") and not params.url.startswith("https://sec.gov"):
        return "Error: URL must be a sec.gov URL. Get filing URLs from edgar_get_submissions or edgar_search_filings."

    try:
        client = await get_client()
        response = await client.get(params.url)
        response.raise_for_status()
        content = response.text
    except Exception as e:
        return handle_api_error(e, "fetching filing document")

    # Strip HTML tags for readability
    text = re.sub(r"<style[^>]*>.*?</style>", "", content, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > params.max_length:
        text = text[: params.max_length] + f"\n\n[TRUNCATED — showing {params.max_length:,} of {len(content):,} characters]"

    return f"## Filing Document Content\n**Source:** {params.url}\n\n{text}"


@mcp.tool(
    name="edgar_search_filing_content",
    annotations={
        "title": "Search Within a Filing Document",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def edgar_search_filing_content(params: EdgarSearchFilingContentInput) -> str:
    """Search within a specific SEC filing for sections matching keywords.

    Instead of returning the first N characters of a filing (which is usually
    just the cover page), this tool searches the ENTIRE filing for your keywords
    and returns the surrounding context for each match. This is the best way to
    find specific tables, sections, or disclosures in large filings (10-Ks can
    be 4MB+).

    Use cases:
    - Find revenue segment tables: search_terms='segment revenue,disaggregation of revenue'
    - Find risk factors: search_terms='risk factors,cybersecurity'
    - Find executive compensation: search_terms='compensation,salary,bonus'
    - Find goodwill details: search_terms='goodwill,impairment'
    - Find debt schedule: search_terms='long-term debt,maturities'
    - Find lease obligations: search_terms='operating lease,right-of-use'

    Args:
        params (EdgarSearchFilingContentInput): Parameters containing:
            - url (str): Full sec.gov URL of the filing document
            - search_terms (str): Comma-separated keywords to find
            - context_chars (int): Chars of context around each match (default: 3000)
            - max_matches (int): Max matching sections to return (default: 10)

    Returns:
        str: Matching sections with surrounding context, or a message if no matches found.
    """
    if not params.url.startswith("https://www.sec.gov") and not params.url.startswith("https://sec.gov"):
        return "Error: URL must be a sec.gov URL. Get filing URLs from edgar_get_submissions or edgar_search_filings."

    try:
        client = await get_client()
        response = await client.get(params.url)
        response.raise_for_status()
        content = response.text
    except Exception as e:
        return handle_api_error(e, "fetching filing document")

    # Strip HTML to searchable text, preserving some structure
    text = re.sub(r"<style[^>]*>.*?</style>", "", content, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    # Convert table cells and line breaks to whitespace with markers
    text = re.sub(r"</tr>", "\n", text)
    text = re.sub(r"</td>", " | ", text)
    text = re.sub(r"</th>", " | ", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    # Collapse whitespace but preserve newlines
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    terms = [t.strip().lower() for t in params.search_terms.split(",") if t.strip()]
    if not terms:
        return "Error: Provide at least one search term."

    text_lower = text.lower()
    half_ctx = params.context_chars // 2

    # Find all match positions, dedup overlapping ranges
    match_positions = []
    for term in terms:
        start = 0
        while True:
            idx = text_lower.find(term, start)
            if idx == -1:
                break
            match_positions.append((idx, term))
            start = idx + len(term)

    if not match_positions:
        return (
            f"## No Matches Found\n\n"
            f"**Source:** {params.url}\n"
            f"**Search terms:** {', '.join(terms)}\n\n"
            f"The filing ({len(text):,} characters) did not contain these terms. "
            f"Try different keywords or use edgar_extract_xbrl for structured financial data."
        )

    # Sort by position and deduplicate overlapping ranges
    match_positions.sort(key=lambda x: x[0])
    sections = []
    for pos, term in match_positions:
        range_start = max(0, pos - half_ctx)
        range_end = min(len(text), pos + half_ctx)

        # Merge with previous section if overlapping
        if sections and range_start <= sections[-1]["end"]:
            sections[-1]["end"] = max(sections[-1]["end"], range_end)
            sections[-1]["terms"].add(term)
        else:
            sections.append({
                "start": range_start,
                "end": range_end,
                "terms": {term},
            })

        if len(sections) >= params.max_matches:
            break

    # Format output
    lines = [
        f"## Filing Content Search Results",
        f"**Source:** {params.url}",
        f"**Filing size:** {len(text):,} characters",
        f"**Search terms:** {', '.join(terms)}",
        f"**Matches found:** {len(match_positions)} occurrences across {len(sections)} sections",
        "",
    ]

    for i, section in enumerate(sections, 1):
        excerpt = text[section["start"]:section["end"]].strip()
        matched = ", ".join(sorted(section["terms"]))
        pct = (section["start"] / len(text)) * 100
        lines.append(f"### Section {i} (at {pct:.0f}% through filing, matched: {matched})\n")
        lines.append(f"```\n{excerpt}\n```\n")

    return "\n".join(lines)


@mcp.tool(
    name="edgar_get_financial_statements",
    annotations={
        "title": "Get Structured Financial Statements",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def edgar_get_financial_statements(params: EdgarGetFinancialStatementsInput) -> str:
    """Retrieve structured financial statements (Income Statement, Balance Sheet, Cash Flow) for a company.

    Returns data organized by statement and line item, across multiple periods.
    Uses the XBRL company facts API for fast retrieval of standard US-GAAP concepts.

    NOTE: This tool provides a quick high-level overview using standard US-GAAP concepts only.
    For detailed financial modeling, 3-statement models, or segment/custom breakdowns, use
    edgar_extract_xbrl instead — it parses actual filing iXBRL and captures company-specific
    line items, product segments, and geographic detail that this tool cannot access.

    Args:
        params (EdgarGetFinancialStatementsInput): Parameters containing:
            - cik (str): Company CIK number
            - statement (str): 'income', 'balance_sheet', 'cash_flow', or 'all'
            - period_type (str): 'annual' or 'quarterly'
            - years (int): Number of years of data (default: 5)
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: Structured financial statements in the requested format.
    """
    cik_padded = pad_cik(params.cik)
    url = f"{SEC_BASE_URL}/api/xbrl/companyfacts/CIK{cik_padded}.json"

    try:
        data = await sec_request(url)
    except Exception as e:
        return handle_api_error(e, "fetching company facts")

    company_name = data.get("entityName", "Unknown")
    facts = data.get("facts", {})
    gaap_facts = facts.get("us-gaap", {})
    dei_facts = facts.get("dei", {})

    # --- Detect fiscal year end and currency ---
    fiscal_year_end = None
    # Try to get fiscal year end from dei:DocumentFiscalYearFocus and dei:DocumentFiscalPeriodFocus
    # or infer from filing dates in the data
    fy_concept = dei_facts.get("DocumentFiscalYearFocus", {})
    fpe_concept = dei_facts.get("DocumentFiscalPeriodFocus", {})

    # Try to detect fiscal year end from the actual period end dates in revenue data
    revenue_candidates = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"]
    for rc in revenue_candidates:
        if rc in gaap_facts:
            for unit_facts in gaap_facts[rc].get("units", {}).values():
                for fact in unit_facts:
                    end_date = fact.get("end")
                    frame = fact.get("frame", "")
                    if end_date and re.match(r'^CY\d{4}$', frame):
                        # This is an annual period — the end date tells us fiscal year end
                        month_day = end_date[5:]  # e.g., "09-28" or "12-31"
                        fiscal_year_end = month_day
                        break
                if fiscal_year_end:
                    break
        if fiscal_year_end:
            break

    is_calendar_year = fiscal_year_end is None or fiscal_year_end.startswith("12-")

    # Detect currency from unit keys
    currency = "USD"
    for concept_data in list(gaap_facts.values())[:5]:
        for unit_key in concept_data.get("units", {}).keys():
            if unit_key in ("USD", "EUR", "GBP", "JPY", "CNY", "CHF", "CAD"):
                currency = unit_key
                break

    # Detect reporting scale from values (thousands, millions, etc.)
    reporting_scale = None
    for rc in revenue_candidates:
        if rc in gaap_facts:
            for unit_facts in gaap_facts[rc].get("units", {}).values():
                for fact in unit_facts:
                    val = fact.get("val")
                    if val and isinstance(val, (int, float)):
                        if val > 1e12:
                            reporting_scale = "units"
                        elif val > 1e9:
                            reporting_scale = "units"
                        else:
                            reporting_scale = "units"
                        break
                break
        if reporting_scale:
            break
    if not reporting_scale:
        reporting_scale = "units"

    # Determine which statements to process
    statement_map = {
        "income": "income_statement",
        "balance_sheet": "balance_sheet",
        "cash_flow": "cash_flow",
    }
    if params.statement == "all":
        statements_to_process = list(FINANCIAL_STATEMENT_CONCEPTS.keys())
    else:
        key = statement_map.get(params.statement, params.statement)
        if key not in FINANCIAL_STATEMENT_CONCEPTS:
            return f"Error: Unknown statement type '{params.statement}'. Use 'income', 'balance_sheet', 'cash_flow', or 'all'."
        statements_to_process = [key]

    # Frame patterns:
    # Duration (income/cash flow): CY2024 (annual) or CY2024Q1 (quarterly)
    # Instant (balance sheet):     CY2024Q4I (annual, end-of-year) or CY2024Q1I (quarterly)
    if params.period_type == "annual":
        duration_pattern = re.compile(r'^CY(\d{4})$')
        instant_pattern = re.compile(r'^CY(\d{4})Q4I$')
    else:
        duration_pattern = re.compile(r'^CY(\d{4})Q([1-4])$')
        instant_pattern = re.compile(r'^CY(\d{4})Q([1-4])I$')

    # Collect available periods by type
    duration_periods = set()
    instant_periods = set()
    for concept_data in gaap_facts.values():
        for unit_facts in concept_data.get("units", {}).values():
            for fact in unit_facts:
                frame = fact.get("frame", "")
                if duration_pattern.match(frame):
                    duration_periods.add(frame)
                elif instant_pattern.match(frame):
                    instant_periods.add(frame)

    def limit_periods(periods_set, max_years, period_type):
        sorted_p = sorted(periods_set, reverse=True)
        if period_type == "annual":
            seen_years = set()
            result = []
            for p in sorted_p:
                m = re.match(r'CY(\d{4})', p)
                if m and m.group(1) not in seen_years:
                    seen_years.add(m.group(1))
                    result.append(p)
                if len(seen_years) >= max_years:
                    break
            return result
        else:
            return sorted_p[: max_years * 4]

    sorted_duration = limit_periods(duration_periods, params.years, params.period_type)
    sorted_instant = limit_periods(instant_periods, params.years, params.period_type)

    # Extract data for each statement
    result_statements = {}
    for stmt_key in statements_to_process:
        # Balance sheet uses instant frames; income/cash flow use duration frames
        is_balance_sheet = (stmt_key == "balance_sheet")
        stmt_periods = sorted_instant if is_balance_sheet else sorted_duration

        line_items_config = FINANCIAL_STATEMENT_CONCEPTS[stmt_key]
        line_items_data = {}

        for line_name, concept_candidates in line_items_config.items():
            values = {}

            for concept_name in concept_candidates:
                if concept_name not in gaap_facts:
                    continue

                concept_data = gaap_facts[concept_name]
                for unit_name, unit_facts in concept_data.get("units", {}).items():
                    for fact in unit_facts:
                        frame = fact.get("frame", "")
                        if frame in stmt_periods:
                            if frame not in values:
                                values[frame] = fact.get("val")

                if values:
                    break

            if values:
                line_items_data[line_name] = {
                    period: values.get(period) for period in stmt_periods
                }

        result_statements[stmt_key] = {
            "periods": stmt_periods,
            "line_items": line_items_data,
        }

    # --- Enhancement: D&A fallback ---
    # If income statement has no DepreciationAmortization but cash flow does, copy it
    if "income_statement" in result_statements and "cash_flow" in result_statements:
        is_items = result_statements["income_statement"]["line_items"]
        cf_items = result_statements["cash_flow"]["line_items"]
        if "DepreciationAmortization" not in is_items and "DepreciationAmortization" in cf_items:
            is_items["DepreciationAmortization (from CF)"] = cf_items["DepreciationAmortization"]

    # --- Enhancement: Cross-statement integrity checks ---
    integrity_checks = []

    # Check 1: IS Net Income == CF starting Net Income
    if "income_statement" in result_statements and "cash_flow" in result_statements:
        is_ni = result_statements["income_statement"]["line_items"].get("NetIncome", {})
        cf_ni = result_statements["cash_flow"]["line_items"].get("NetIncome", {})
        duration_periods = result_statements["income_statement"]["periods"]
        for p in duration_periods:
            is_val = is_ni.get(p)
            cf_val = cf_ni.get(p)
            if is_val is not None and cf_val is not None:
                if is_val != cf_val:
                    integrity_checks.append(
                        f"WARNING ({p}): IS Net Income (${is_val:,.0f}) != CF Net Income (${cf_val:,.0f})"
                    )

    # Check 2: BS Assets == Liabilities + Equity
    if "balance_sheet" in result_statements:
        bs_items = result_statements["balance_sheet"]["line_items"]
        bs_periods = result_statements["balance_sheet"]["periods"]
        assets = bs_items.get("TotalAssets", {})
        liab = bs_items.get("TotalLiabilities", {})
        equity = bs_items.get("TotalStockholdersEquity", {})
        for p in bs_periods:
            a = assets.get(p)
            l = liab.get(p)
            e = equity.get(p)
            if a is not None and l is not None and e is not None:
                if abs(a - (l + e)) > 1:
                    integrity_checks.append(
                        f"WARNING ({p}): Assets (${a:,.0f}) != Liabilities (${l:,.0f}) + Equity (${e:,.0f})"
                    )

    # --- Build fiscal year period labels ---
    def make_period_label(frame: str) -> str:
        """Convert XBRL frame to human-readable fiscal year label."""
        m = re.match(r'^CY(\d{4})$', frame)
        if m:
            year = m.group(1)
            if is_calendar_year:
                return f"FY{year}"
            else:
                return f"FY{year} (ends {fiscal_year_end})"
        m = re.match(r'^CY(\d{4})Q(\d)I?$', frame)
        if m:
            return f"FY{m.group(1)} Q{m.group(2)}"
        return frame

    # --- Build metadata ---
    metadata = {
        "currency": currency,
        "reporting_scale": reporting_scale,
        "fiscal_year_end": fiscal_year_end or "12-31",
        "is_calendar_year": is_calendar_year,
    }

    if params.response_format == ResponseFormat.JSON:
        return json.dumps({
            "company": company_name,
            "cik": params.cik,
            "period_type": params.period_type,
            "years": params.years,
            "metadata": metadata,
            "integrity_checks": integrity_checks,
            "statements": result_statements,
        }, indent=2)

    # Markdown output
    lines = [
        f"## Financial Statements: {company_name}",
        f"**CIK:** {params.cik} | **Period:** {params.period_type} | **Years:** {params.years}",
        f"**Currency:** {currency} | **Fiscal Year End:** {fiscal_year_end or '12-31'}"
        + (" (calendar year)" if is_calendar_year else " (non-calendar)"),
        "",
    ]

    if integrity_checks:
        lines.append("### Integrity Checks\n")
        for check in integrity_checks:
            lines.append(f"- {check}")
        lines.append("")

    statement_titles = {
        "income_statement": "Income Statement",
        "balance_sheet": "Balance Sheet",
        "cash_flow": "Cash Flow Statement",
    }

    for stmt_key, stmt_data in result_statements.items():
        periods = stmt_data["periods"]
        line_items = stmt_data["line_items"]
        period_labels = [make_period_label(p) for p in periods]

        lines.append(f"### {statement_titles.get(stmt_key, stmt_key)}\n")

        if not line_items:
            lines.append("*No data available for this statement.*\n")
            continue

        header = "| Line Item | " + " | ".join(period_labels) + " |"
        separator = "|---|" + "|".join(["---:"] * len(periods)) + "|"
        lines.append(header)
        lines.append(separator)

        for line_name, period_values in line_items.items():
            row_values = []
            for p in periods:
                val = period_values.get(p)
                if val is None:
                    row_values.append("—")
                elif isinstance(val, (int, float)):
                    if abs(val) >= 1e9:
                        row_values.append(f"${val/1e9:,.1f}B")
                    elif abs(val) >= 1e6:
                        row_values.append(f"${val/1e6:,.0f}M")
                    elif abs(val) < 100:
                        row_values.append(f"${val:,.2f}")
                    else:
                        row_values.append(f"${val:,.0f}")
                else:
                    row_values.append(str(val))
            lines.append(f"| {line_name} | " + " | ".join(row_values) + " |")

        lines.append("")

    lines.append("---")
    lines.append("*For detailed company-specific line items, segment breakdowns, and custom metrics, use `edgar_extract_xbrl` instead.*")

    return "\n".join(lines)


@mcp.tool(
    name="edgar_extract_xbrl",
    annotations={
        "title": "Extract iXBRL Facts from Filing",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def edgar_extract_xbrl(params: EdgarExtractXBRLInput) -> str:
    """Extract XBRL facts from an iXBRL filing document, including custom taxonomy extensions.

    PRIMARY TOOL for building 3-statement financial models, pulling detailed financials,
    or getting any financial data breakdowns. This tool parses the actual SEC filing HTML
    and captures EVERY tagged financial fact — including company-specific custom line items,
    product segment revenue, geographic breakdowns, and detail that the standard XBRL API misses.

    ALWAYS use this tool (not edgar_get_financial_statements) when:
    - Building 3-statement financial models (Income Statement, Balance Sheet, Cash Flow)
    - The user asks for financial data, revenue breakdowns, or detailed financials
    - Segment data is needed (product revenue, geographic splits, business segments)
    - Company-specific metrics are needed (e.g., iPhone revenue, subscriber counts)

    Use with include_standard=true to get the full picture (standard + custom facts).
    Use axis_filter to target specific dimensions like 'ProductOrServiceAxis' or 'StatementGeographicalAxis'.

    Provide either a direct URL or a CIK number to auto-resolve the latest filing.

    Args:
        params (EdgarExtractXBRLInput): Parameters containing:
            - url (Optional[str]): Direct filing document URL
            - cik (Optional[str]): Company CIK to auto-resolve filing
            - form_type (str): Form type when using CIK (default: '10-K')
            - filing_date (Optional[str]): Specific filing date
            - filter (Optional[str]): Keyword filter for concept names
            - axis_filter (Optional[str]): Filter by dimension axis
            - include_standard (bool): Include standard us-gaap facts (default: false)
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: Extracted XBRL facts in the requested format.
    """
    filing_info: Dict[str, str] = {"url": "", "form_type": "", "filing_date": "", "company_name": ""}

    if params.url:
        filing_info["url"] = params.url
        filing_info["form_type"] = params.form_type
    elif params.cik:
        try:
            filing_info = await resolve_filing_url(
                params.cik, params.form_type, params.filing_date
            )
        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            return handle_api_error(e, "resolving filing URL")
    else:
        return "Error: Provide either 'url' or 'cik' to identify the filing."

    doc_url = filing_info["url"]
    if not doc_url.startswith("https://www.sec.gov") and not doc_url.startswith("https://sec.gov"):
        return "Error: URL must be a sec.gov URL."

    # Fetch the raw iXBRL HTML
    try:
        client = await get_client()
        response = await client.get(doc_url)
        response.raise_for_status()
        html = response.text
    except Exception as e:
        return handle_api_error(e, "fetching filing document")

    # Parse contexts, units, and facts
    contexts = parse_ixbrl_contexts(html)
    units = parse_ixbrl_units(html)
    all_facts = parse_ixbrl_facts(html, contexts)

    # Resolve unit names
    for fact in all_facts:
        fact["unit"] = units.get(fact["unit"], fact["unit"])

    # --- Detect fiscal year end from period end dates ---
    fiscal_year_end = None
    for fact in all_facts:
        period = fact.get("period", {})
        end = period.get("end")
        # Look for annual-length periods (>300 days) to detect FY end
        if end and period.get("start"):
            from datetime import date as dt_date
            try:
                s = dt_date.fromisoformat(period["start"])
                e = dt_date.fromisoformat(end)
                if (e - s).days > 300:
                    fiscal_year_end = end[5:]  # e.g., "09-27"
                    break
            except (ValueError, TypeError):
                pass

    # Detect primary currency
    currency = "USD"
    unit_counts: Dict[str, int] = defaultdict(int)
    for fact in all_facts:
        if fact["unit"] in ("USD", "EUR", "GBP", "JPY", "CNY", "CHF", "CAD"):
            unit_counts[fact["unit"]] += 1
    if unit_counts:
        currency = max(unit_counts, key=unit_counts.get)

    is_calendar_year = fiscal_year_end is None or fiscal_year_end.startswith("12-")

    # Apply filters
    filtered_facts = []
    standard_taxonomies = {"us-gaap", "dei", "srt", "xbrli"}

    for fact in all_facts:
        if not params.include_standard and fact["taxonomy"] in standard_taxonomies:
            if not fact["dimensions"]:
                continue

        if params.filter:
            filter_lower = params.filter.lower()
            concept_lower = fact["concept"].lower()
            member_values = " ".join(str(v).lower() for v in fact["dimensions"].values())
            if filter_lower not in concept_lower and filter_lower not in member_values:
                continue

        if params.axis_filter:
            axis_lower = params.axis_filter.lower()
            if not any(axis_lower in k.lower() for k in fact["dimensions"].keys()):
                continue

        filtered_facts.append(fact)

    # Sort by concept name, then by period end date
    def sort_key(f):
        period = f.get("period", {})
        end = period.get("end", period.get("instant", ""))
        return (f["concept"], end)

    filtered_facts.sort(key=sort_key)

    metadata = {
        "currency": currency,
        "fiscal_year_end": fiscal_year_end or "12-31",
        "is_calendar_year": is_calendar_year,
    }

    if params.response_format == ResponseFormat.JSON:
        output_facts = []
        for f in filtered_facts:
            output_facts.append({
                "concept": f["concept"],
                "taxonomy": f["taxonomy"],
                "value": f["value"],
                "unit": f["unit"],
                "period": f["period"],
                "dimensions": f["dimensions"],
            })

        return json.dumps({
            "company": filing_info.get("company_name", ""),
            "filing": f"{filing_info.get('form_type', '')} filed {filing_info.get('filing_date', '')}",
            "source_url": doc_url,
            "metadata": metadata,
            "total_facts": len(output_facts),
            "facts": output_facts,
        }, indent=2)

    # Markdown output
    lines = [
        f"## iXBRL Facts: {filing_info.get('company_name', 'Filing')}",
        f"**Filing:** {filing_info.get('form_type', '')} filed {filing_info.get('filing_date', '')}",
        f"**Source:** {doc_url}",
        f"**Currency:** {currency} | **Fiscal Year End:** {fiscal_year_end or '12-31'}"
        + (" (calendar year)" if is_calendar_year else " (non-calendar)"),
        f"**Facts found:** {len(filtered_facts)}",
    ]
    if params.filter:
        lines.append(f"**Filter:** '{params.filter}'")
    if params.axis_filter:
        lines.append(f"**Axis filter:** '{params.axis_filter}'")
    lines.append("")

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for f in filtered_facts:
        grouped[f["concept"]].append(f)

    for concept, concept_facts in grouped.items():
        lines.append(f"### {concept}\n")
        for f in concept_facts:
            val = f["value"]
            if isinstance(val, (int, float)) and abs(val) >= 1e6 and f["unit"] == "USD":
                val_str = f"${val / 1e9:,.2f}B" if abs(val) >= 1e9 else f"${val / 1e6:,.0f}M"
            elif isinstance(val, (int, float)):
                val_str = f"{val:,.2f}" if isinstance(val, float) and val != int(val) else f"{val:,}"
            else:
                val_str = str(val)

            period = f["period"]
            if "start" in period and "end" in period:
                period_str = f"{period['start']} to {period['end']}"
            elif "instant" in period:
                period_str = f"as of {period['instant']}"
            else:
                period_str = "unknown period"

            dim_str = ""
            if f["dimensions"]:
                dim_parts = [f"{k.split(':')[-1]}={v.split(':')[-1]}" for k, v in f["dimensions"].items()]
                dim_str = f" [{', '.join(dim_parts)}]"

            lines.append(f"- {val_str} ({f['unit']}) — {period_str}{dim_str}")

        lines.append("")

    if not filtered_facts:
        lines.append("*No matching facts found. Try broadening your filter or setting include_standard=true.*")

    return "\n".join(lines)


# =============================================================================
# Tool 10: Raw SEC API Query (Generic Passthrough)
# =============================================================================

ALLOWED_SEC_HOSTS = {
    "data.sec.gov",
    "efts.sec.gov",
    "www.sec.gov",
}


class EdgarRawQueryInput(BaseModel):
    """Input for making a raw request to any SEC EDGAR API endpoint."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url: str = Field(
        ...,
        description=(
            "Full URL to a SEC EDGAR API endpoint. Must be on an allowed SEC domain: "
            "data.sec.gov, efts.sec.gov, or www.sec.gov. "
            "Examples:\n"
            "- https://data.sec.gov/submissions/CIK0000320193.json\n"
            "- https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json\n"
            "- https://efts.sec.gov/LATEST/search-index?q=%22goodwill%20impairment%22&forms=8-K\n"
            "- https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=tesla&CIK=&type=10-K&dateb=&owner=include&count=10&search_text=&action=getcompany"
        ),
        min_length=10,
    )
    max_length: int = Field(
        default=100000,
        description="Maximum characters to return from the response. Default: 100000.",
        ge=1000,
        le=500000,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.JSON,
        description="Output format: 'json' returns raw JSON (default), 'markdown' attempts formatted output.",
    )


@mcp.tool(
    name="edgar_raw_query",
    annotations={
        "title": "Raw SEC EDGAR API Query",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def edgar_raw_query(params: EdgarRawQueryInput) -> str:
    """Make a direct request to any SEC EDGAR API endpoint.

    This is an escape-hatch tool for accessing any SEC endpoint not covered
    by the other specialized tools. It validates that the URL points to an
    allowed SEC domain, makes the request with proper rate limiting, and
    returns the raw response.

    Use cases:
    - Accessing lesser-used EDGAR APIs (mutual fund data, insider transactions, etc.)
    - Custom XBRL API queries with parameters not exposed by other tools
    - Exploring new or undocumented SEC endpoints
    - Fetching index pages or filing lists in custom formats

    Args:
        params: URL, max_length, and response_format.

    Returns:
        str: The API response, truncated to max_length if needed.
    """
    from urllib.parse import urlparse

    parsed = urlparse(params.url)
    host = parsed.hostname or ""

    if host not in ALLOWED_SEC_HOSTS:
        return (
            f"Error: Host '{host}' is not allowed. "
            f"URL must be on one of: {', '.join(sorted(ALLOWED_SEC_HOSTS))}."
        )

    if not parsed.scheme or parsed.scheme not in ("http", "https"):
        return "Error: URL must use http or https."

    try:
        global _last_request_time
        now = asyncio.get_event_loop().time()
        elapsed = now - _last_request_time
        if elapsed < REQUEST_DELAY:
            await asyncio.sleep(REQUEST_DELAY - elapsed)

        client = await get_client()
        _last_request_time = asyncio.get_event_loop().time()

        response = await client.get(params.url)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")

        # Try JSON first
        if "json" in content_type or params.url.endswith(".json"):
            try:
                data = response.json()
                if params.response_format == ResponseFormat.JSON:
                    result = json.dumps(data, indent=2)
                else:
                    # Best-effort markdown for JSON
                    result = f"## Raw SEC API Response\n\n**URL:** `{params.url}`\n\n```json\n{json.dumps(data, indent=2)}\n```"
                return result[:params.max_length]
            except Exception:
                pass

        # Fall back to text
        text = response.text
        if params.response_format == ResponseFormat.MARKDOWN:
            text = f"## Raw SEC API Response\n\n**URL:** `{params.url}`\n\n```\n{text}\n```"

        if len(text) > params.max_length:
            text = text[:params.max_length] + f"\n\n... [truncated at {params.max_length:,} characters]"

        return text

    except Exception as e:
        return handle_api_error(e, "raw SEC query")


# =============================================================================
# Tool 11: Advanced Full-Text Search (EFTS Query Builder)
# =============================================================================

class EdgarFullTextSearchInput(BaseModel):
    """Input for advanced full-text search across SEC filings with rich filtering."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description=(
            "Search query with full Lucene syntax support. "
            "Boolean operators: AND, OR, NOT. "
            'Exact phrases: "goodwill impairment". '
            'Wildcards: cyber* (matches cybersecurity, cyberattack). '
            'Proximity: "climate risk"~5 (words within 5 of each other). '
            "Examples:\n"
            '- "material weakness" AND "internal controls"\n'
            "- cybersecurity AND (breach OR incident)\n"
            '- "going concern" NOT "no going concern"'
        ),
        min_length=1,
        max_length=1000,
    )
    entity_name: Optional[str] = Field(
        default=None,
        description="Filter to a specific company name (partial match). Example: 'Tesla'.",
    )
    ciks: Optional[str] = Field(
        default=None,
        description=(
            "Comma-separated CIK numbers to restrict search to specific companies. "
            "Example: '320193,789019' for Apple and Microsoft."
        ),
    )
    tickers: Optional[str] = Field(
        default=None,
        description=(
            "Comma-separated ticker symbols. "
            "Example: 'AAPL,MSFT,GOOGL'. Will be resolved to CIKs automatically."
        ),
    )
    forms: Optional[str] = Field(
        default=None,
        description=(
            "Comma-separated form types. Common types:\n"
            "- Annual/quarterly: 10-K, 10-Q\n"
            "- Current events: 8-K\n"
            "- Proxy: DEF 14A\n"
            "- Registration: S-1, S-3\n"
            "- Insider: 4, 3\n"
            "- Institutional: 13F-HR\n"
            "- Beneficial ownership: SC 13D, SC 13G\n"
            "Example: '10-K,10-Q,8-K'"
        ),
    )
    date_start: Optional[str] = Field(
        default=None,
        description="Start date (YYYY-MM-DD). Example: '2024-01-01'.",
    )
    date_end: Optional[str] = Field(
        default=None,
        description="End date (YYYY-MM-DD). Example: '2024-12-31'.",
    )
    filed_start: Optional[str] = Field(
        default=None,
        description="Start of filing date range (YYYY-MM-DD). Filters by when the filing was submitted to SEC.",
    )
    filed_end: Optional[str] = Field(
        default=None,
        description="End of filing date range (YYYY-MM-DD).",
    )
    limit: int = Field(
        default=20,
        description="Maximum number of results (1-50). Default: 20.",
        ge=1,
        le=50,
    )
    start_from: int = Field(
        default=0,
        description="Offset for pagination. Use with limit to page through results. Default: 0.",
        ge=0,
    )
    include_snippets: bool = Field(
        default=True,
        description="Include text snippets showing where the query matched. Default: true.",
    )


@mcp.tool(
    name="edgar_full_text_search",
    annotations={
        "title": "Advanced Full-Text Search SEC Filings",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def edgar_full_text_search(params: EdgarFullTextSearchInput) -> str:
    """Advanced full-text search across all SEC EDGAR filings since 2001.

    This is a more powerful version of edgar_search_filings with additional
    filtering capabilities: company-specific search, ticker resolution,
    pagination, filing date vs period date filtering, and match snippets.

    Use cases:
    - Find all 8-Ks mentioning "goodwill impairment" in 2024
    - Search for cybersecurity incidents across specific companies
    - Find proxy statements discussing executive compensation at tech firms
    - Locate risk factor disclosures about climate change
    - Track M&A activity via SC 13D filings

    Args:
        params: Search parameters with query, filters, and pagination.

    Returns:
        str: Markdown-formatted search results with metadata, links, and optional snippets.
    """
    # Resolve tickers to CIKs if provided
    cik_list = []
    if params.ciks:
        cik_list.extend([c.strip() for c in params.ciks.split(",") if c.strip()])

    if params.tickers:
        try:
            ticker_map = await load_ticker_map()
            for t in params.tickers.split(","):
                t = t.strip().upper()
                if t in ticker_map:
                    cik_list.append(ticker_map[t]["cik"])
                else:
                    return f"Error: Ticker '{t}' not found in SEC database. Use edgar_search_company to verify."
        except Exception as e:
            return handle_api_error(e, "resolving tickers")

    # Build EFTS query parameters
    query_params: Dict[str, Any] = {
        "q": params.query,
        "from": params.start_from,
        "size": params.limit,
    }

    if params.forms:
        query_params["forms"] = params.forms
    if params.entity_name:
        query_params["entity"] = params.entity_name

    # Date range filtering (period dates on the filing)
    if params.date_start or params.date_end:
        query_params["dateRange"] = "custom"
        query_params["startdt"] = params.date_start or "2001-01-01"
        query_params["enddt"] = params.date_end or "2099-12-31"

    # Filed date filtering (when submitted to SEC)
    if params.filed_start:
        query_params["startdt"] = params.filed_start
        query_params["dateRange"] = "custom"
    if params.filed_end:
        query_params["enddt"] = params.filed_end
        query_params["dateRange"] = "custom"

    # Build URL
    from urllib.parse import quote
    param_parts = []
    for k, v in query_params.items():
        param_parts.append(f"{k}={quote(str(v))}")

    url = f"{EFTS_BASE_URL}/search-index?{'&'.join(param_parts)}"

    try:
        data = await sec_request(url)
    except Exception as e:
        return handle_api_error(e, "full-text search")

    hits = data.get("hits", {})
    total = hits.get("total", {}).get("value", 0)
    results = hits.get("hits", [])

    if not results:
        filters_desc = []
        if params.forms:
            filters_desc.append(f"forms={params.forms}")
        if params.entity_name:
            filters_desc.append(f"company={params.entity_name}")
        if params.date_start:
            filters_desc.append(f"from {params.date_start}")
        if params.date_end:
            filters_desc.append(f"to {params.date_end}")
        filter_str = f" (filters: {', '.join(filters_desc)})" if filters_desc else ""
        return f"No filings found matching '{params.query}'{filter_str}. Try broader terms or different filters."

    # Format output
    lines = [
        f"## EDGAR Full-Text Search Results",
        f"**Query:** `{params.query}`",
    ]

    filter_parts = []
    if params.forms:
        filter_parts.append(f"Forms: {params.forms}")
    if params.entity_name:
        filter_parts.append(f"Company: {params.entity_name}")
    if params.tickers:
        filter_parts.append(f"Tickers: {params.tickers}")
    if params.date_start or params.date_end:
        filter_parts.append(f"Date: {params.date_start or '...'} to {params.date_end or '...'}")
    if filter_parts:
        lines.append(f"**Filters:** {' | '.join(filter_parts)}")

    lines.append(f"**Total matches:** {total:,} | **Showing:** {params.start_from + 1}–{params.start_from + len(results)}")
    if total > params.start_from + params.limit:
        lines.append(f"*Use `start_from={params.start_from + params.limit}` to see more results.*")
    lines.append("")

    for i, r in enumerate(results, start=params.start_from + 1):
        source = r.get("_source", {})
        file_date = source.get("file_date", "N/A")
        form_type = source.get("form_type", "N/A")
        entity_name = source.get("entity_name", "Unknown")
        display_names = source.get("display_names", [])
        file_num = source.get("file_num", "")
        accession = r.get("_id", "")

        names = ", ".join(display_names) if display_names else entity_name

        lines.append(f"### {i}. {names} — {form_type} ({file_date})")
        if accession:
            # Build a direct link to the filing on SEC
            acc_clean = accession.replace("-", "").replace(":", "")
            lines.append(f"  - Accession: `{accession}`")

        # Include text snippets if available and requested
        if params.include_snippets:
            highlight = r.get("highlight", {})
            snippets = highlight.get("content", [])
            if snippets:
                lines.append("  - **Matching excerpts:**")
                for snippet in snippets[:3]:
                    # Clean up HTML highlight tags
                    clean = snippet.replace("<em>", "**").replace("</em>", "**")
                    clean = re.sub(r'<[^>]+>', '', clean).strip()
                    if clean:
                        lines.append(f"    > {clean}")

        lines.append("")

    return "\n".join(lines)


# =============================================================================
# Entry Point
# =============================================================================

if __name__ == "__main__":
    mcp.run()
