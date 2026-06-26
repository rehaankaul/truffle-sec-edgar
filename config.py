"""Configuration for the SEC EDGAR Truffle app.

Settings are read from two layers, in priority order:

1. App variables (runtime-editable, shared across the foreground and background
   containers) — these let the user change the watchlist and watched forms from
   chat without redeploying. Written via the foreground edgar_set_* tools.
2. Environment variables populated by the `text` install steps — the initial
   seed / fallback when no app variable has been set.

Nothing here is hardcoded to a specific user, and every getter has a safe
default so imports and tests work without a configured device.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("sec-edgar.config")

# The SEC requires every API client to identify itself with a descriptive
# User-Agent of the form "Sample Company Name AdminContact@example.com".
DEFAULT_USER_AGENT = "Truffle EDGAR App admin@example.com"

# Forms the background monitor treats as noteworthy when newly filed.
DEFAULT_WATCH_FORMS = ["8-K", "10-K", "10-Q"]

# Bound the per-cycle and overall work the background monitor does.
DEFAULT_MAX_NEW_FILINGS_PER_RUN = 8
DEFAULT_SUBMISSIONS_LOOKBACK = 30  # most-recent filings scanned per ticker
MAX_SEEN_ACCESSIONS = 4000

# App-variable keys for the runtime-editable settings.
APP_VAR_WATCHLIST = "watchlist"
APP_VAR_WATCH_FORMS = "watch_forms"


# --------------------------------------------------------------------------- #
# App-variable access (runtime-editable, FG/BG-shared state)
# --------------------------------------------------------------------------- #
def app_vars_enabled() -> bool:
    """True when running inside a Truffle container with runtime access."""
    return bool(
        os.environ.get("APP_ID", "").strip()
        and os.environ.get("APP_SESSION_TOKEN", "").strip()
    )


def get_app_var(key: str) -> str | None:
    """Read an app variable, or None if unavailable/unset. Never raises."""
    if not app_vars_enabled():
        return None
    try:
        from truffile.app_runtime import AppRuntimeClient, init_channel

        with init_channel() as channel:
            return AppRuntimeClient(channel).get_app_var(key)
    except Exception as exc:  # noqa: BLE001 — fall back to env on any failure
        logger.warning("Could not read app var %s: %s", key, exc)
        return None


def set_app_var(key: str, value: str) -> None:
    """Write an app variable. Raises if runtime app vars are unavailable."""
    if not app_vars_enabled():
        raise RuntimeError(
            "App variables are unavailable outside a Truffle container. "
            "Reconfigure with `truffile deploy --replace` instead."
        )
    from truffile.app_runtime import AppRuntimeClient, init_channel

    with init_channel() as channel:
        AppRuntimeClient(channel).set_app_var(key, value)


def get_user_agent() -> str:
    """SEC-required contact User-Agent. Falls back to a generic identifier."""
    value = os.environ.get("SEC_EDGAR_USER_AGENT", "").strip()
    return value or DEFAULT_USER_AGENT


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def parse_watchlist(raw: str) -> list[str]:
    """Parse a comma/newline-separated ticker list (uppercased, deduped)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for ticker in _split_csv(raw):
        upper = ticker.upper()
        if upper not in seen:
            seen.add(upper)
            ordered.append(upper)
    return ordered


def get_watchlist() -> list[str]:
    """Tickers the background monitor watches — app var first, then install env."""
    raw = get_app_var(APP_VAR_WATCHLIST)
    if not raw or not raw.strip():
        raw = os.environ.get("SEC_EDGAR_WATCHLIST", "")
    return parse_watchlist(raw)


def get_watch_forms() -> list[str]:
    """Watched form types — app var first, then install env, then default."""
    raw = get_app_var(APP_VAR_WATCH_FORMS)
    if raw is None or not raw.strip():
        raw = os.environ.get("SEC_EDGAR_WATCH_FORMS", "")
    forms = _split_csv(raw)
    if not forms:
        return list(DEFAULT_WATCH_FORMS)
    # Normalize case while preserving SEC's hyphenated form style.
    return [form.upper() for form in forms]


def set_watchlist(raw: str) -> list[str]:
    """Persist a new watchlist as an app variable. Returns the parsed tickers."""
    tickers = parse_watchlist(raw)
    if not tickers:
        raise ValueError("Provide at least one ticker, e.g. AAPL, MSFT, TSLA.")
    set_app_var(APP_VAR_WATCHLIST, ",".join(tickers))
    return tickers


def set_watch_forms(raw: str) -> list[str]:
    """Persist new watched form types as an app variable. Returns the forms."""
    forms = [form.upper() for form in _split_csv(raw)]
    if not forms:
        forms = list(DEFAULT_WATCH_FORMS)
    set_app_var(APP_VAR_WATCH_FORMS, ",".join(forms))
    return forms


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
