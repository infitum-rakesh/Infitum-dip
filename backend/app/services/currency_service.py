"""
currency_service.py — real foreign-exchange conversion, built for Invoice
Intelligence's cross-currency KPI totals.

Real bug found live: the Invoice Inbox "Total Amount" card summed every
ApInvoice.total regardless of currency and labeled the mixed result "$" —
silently wrong the moment more than one currency was present (confirmed
live: USD + INR + AUD invoices in the same org summed into one dollar
figure). This module provides the real conversion that fix needs.

Rates come from frankfurter.app — a free, no-API-key, European Central
Bank-backed FX API (https://www.frankfurter.app) — fetched live and
cached in-process for CACHE_TTL_SECONDS, since ECB rates publish once per
business day and re-fetching per request would be both wasteful and
pointless. Never fabricates a rate: if the API is unreachable, or a
currency code isn't one the ECB publishes a rate for, that amount is
reported back to the caller as excluded/unconverted rather than silently
folded into the total at a guessed rate.
"""
from __future__ import annotations

import time

import httpx

CACHE_TTL_SECONDS = 12 * 60 * 60  # ECB publishes once/business day
FRANKFURTER_URL = "https://api.frankfurter.app/latest"
REQUEST_TIMEOUT = 8.0

# base_currency -> (fetched_at_monotonic, {target_currency: rate})
_rate_cache: dict[str, tuple[float, dict[str, float]]] = {}


class CurrencyServiceError(Exception):
    """Raised when live rates genuinely can't be fetched — callers must
    disclose this to the user, never fall back to a fabricated or
    indefinitely-stale rate."""


async def get_rates(base: str) -> dict[str, float]:
    """Real live rates FROM `base` TO every other currency frankfurter.app
    publishes, cached per-base for CACHE_TTL_SECONDS. Raises
    CurrencyServiceError on any network/parse failure — no silent fallback."""
    base = base.upper()
    cached = _rate_cache.get(base)
    now = time.monotonic()
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(FRANKFURTER_URL, params={"from": base})
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CurrencyServiceError(f"Could not fetch live exchange rates for {base}: {exc}") from exc

    rates = dict(data.get("rates") or {})
    rates[base] = 1.0  # frankfurter omits the base currency from its own rates map
    _rate_cache[base] = (now, rates)
    return rates


async def convert_amounts(amounts_by_currency: dict[str, float], display_currency: str) -> dict:
    """Converts a {currency_code: summed_amount} map into one total in
    `display_currency`, using real live rates. Returns:
      {"converted_total": float, "display_currency": str,
       "included_currencies": [...], "excluded_currencies": [...]}
    "excluded_currencies" lists any currency code frankfurter.app doesn't
    recognize (a typo'd or unsupported code, or a blank/unknown currency
    upstream) — those amounts are deliberately left OUT of converted_total
    rather than guessed at, and the caller is expected to disclose them."""
    display_currency = display_currency.upper()
    rates = await get_rates(display_currency)

    converted_total = 0.0
    included: list[str] = []
    excluded: list[str] = []
    for currency, amount in amounts_by_currency.items():
        code = (currency or "").upper()
        if not code:
            excluded.append(currency or "")
            continue
        if code == display_currency:
            converted_total += amount
            included.append(code)
            continue
        rate_to_display = rates.get(code)
        if not rate_to_display:
            excluded.append(code)
            continue
        # rates[code] = "how many units of `code` equal 1 unit of
        # display_currency" (since rates were fetched FROM display_currency).
        # To go FROM code TO display_currency, divide.
        converted_total += amount / rate_to_display
        included.append(code)

    return {
        "converted_total": round(converted_total, 2),
        "display_currency": display_currency,
        "included_currencies": included,
        "excluded_currencies": excluded,
    }
