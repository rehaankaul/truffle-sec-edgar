"""Environment-driven configuration for the SEC EDGAR Truffle app.

All user-provided settings arrive as environment variables that are populated by
the `text` install step in truffile.yaml. Nothing here is hardcoded to a
specific user, and every getter has a safe default so imports and tests work
without a configured device.
"""

from __future__ import annotations

import os

# The SEC requires every API client to identify itself with a descriptive
# User-Agent of the form "Sample Company Name AdminContact@example.com".
DEFAULT_USER_AGENT = "Truffle EDGAR App admin@example.com"

# Forms the background monitor treats as noteworthy when newly filed.
DEFAULT_WATCH_FORMS = ["8-K", "10-K", "10-Q"]

# Bound the per-cycle and overall work the background monitor does.
DEFAULT_MAX_NEW_FILINGS_PER_RUN = 8
DEFAULT_SUBMISSIONS_LOOKBACK = 30  # most-recent filings scanned per ticker
MAX_SEEN_ACCESSIONS = 4000


def get_user_agent() -> str:
    """SEC-required contact User-Agent. Falls back to a generic identifier."""
    value = os.environ.get("SEC_EDGAR_USER_AGENT", "").strip()
    return value or DEFAULT_USER_AGENT


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def get_watchlist() -> list[str]:
    """Tickers the background monitor watches for new filings (uppercased, deduped)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for ticker in _split_csv(os.environ.get("SEC_EDGAR_WATCHLIST", "")):
        upper = ticker.upper()
        if upper not in seen:
            seen.add(upper)
            ordered.append(upper)
    return ordered


def get_watch_forms() -> list[str]:
    """Form types that count as noteworthy. Defaults to 8-K / 10-K / 10-Q."""
    forms = _split_csv(os.environ.get("SEC_EDGAR_WATCH_FORMS", ""))
    if not forms:
        return list(DEFAULT_WATCH_FORMS)
    # Normalize case while preserving SEC's hyphenated form style.
    return [form.upper() for form in forms]


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default)).strip()))
    except (TypeError, ValueError):
        return default


def get_max_new_filings_per_run() -> int:
    return _env_int("SEC_EDGAR_MAX_NEW_FILINGS_PER_RUN", DEFAULT_MAX_NEW_FILINGS_PER_RUN)


def get_submissions_lookback() -> int:
    return _env_int("SEC_EDGAR_SUBMISSIONS_LOOKBACK", DEFAULT_SUBMISSIONS_LOOKBACK)


def get_state_path() -> str:
    """Where the background monitor persists its seen-filing state."""
    return os.environ.get(
        "SEC_EDGAR_STATE_PATH", "/root/.sec-edgar-truffle/bg_state.json"
    )
