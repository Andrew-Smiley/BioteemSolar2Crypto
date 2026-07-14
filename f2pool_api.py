"""
Pulls today's mining earnings from F2Pool's v2 API (requires an API
token -- their older public/no-auth endpoint was retired). Combines it
with a live BTC/USD price from CoinGecko (public, no key) so the daily
summary can show both the coin amount and an approximate dollar value.

Getting a token: f2pool.com -> account name (top right) -> Account
Settings -> API -> Generate API Token. You must whitelist the IP(s) that
will call the API (your Pi's public IP, not its 192.168.x.x local one --
check with `curl -s https://ifconfig.me` on the Pi), and complete 2FA.

F2Pool v2 docs: https://api.f2pool.com/v2/doc/en.html

--- Why this picks between two different fields ---
F2Pool settles by UTC calendar day, not local calendar day. Two fields
matter here:
  - yesterday_income: the last FULLY SETTLED UTC day (fixed, accurate)
  - estimated_today_income: the current UTC day so far (live, partial)
Depending on your timezone, your local daily summary time might fall
before or after the most recent UTC midnight -- if it falls before,
yesterday_income is a full day stale (it's still showing the day before
your local "today"); if after, yesterday_income has already rolled over
and accurately reflects your local today's mining. Rather than hardcode
a threshold hour, _yesterday_reflects_today() derives it from the actual
system clock/timezone, so it keeps working correctly across DST changes.
"""

import requests
from datetime import datetime, timezone

F2POOL_BASE = "https://api.f2pool.com/v2"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"

COIN_INFO = {
    "bitcoin": {"coingecko_id": "bitcoin", "ticker": "BTC"},
}


def get_balance_info(currency: str, username: str, token: str) -> dict:
    """Calls POST /v2/assets/balance. Returns the balance_info dict."""
    resp = requests.post(
        f"{F2POOL_BASE}/assets/balance",
        headers={"Content-Type": "application/json", "F2P-API-SECRET": token},
        json={
            "currency": currency,
            "mining_user_name": username,
            "calculate_estimated_income": True,
            "historical_total_income_outcome": True,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if "code" in data and "balance_info" not in data:
        raise RuntimeError(data.get("msg", f"F2Pool API error {data.get('code')}"))
    return data["balance_info"]


def get_price_cad(currency: str) -> float:
    coingecko_id = COIN_INFO.get(currency, {}).get("coingecko_id", currency)
    resp = requests.get(
        COINGECKO_PRICE_URL,
        params={"ids": coingecko_id, "vs_currencies": "cad"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()[coingecko_id]["cad"]


def _yesterday_reflects_today() -> bool:
    """
    True once today's most recent UTC-midnight rollover has already
    happened in absolute time -- at that point F2Pool's yesterday_income
    reflects the UTC day that just completed, which (for daylight-only
    solar mining) captures essentially all of today's local mining day.
    Before that rollover, yesterday_income is still one full day stale,
    and only estimated_today_income (partial, still accumulating)
    reflects what's happened locally today so far.

    Derived from the system's actual configured timezone (DST-aware),
    not a hardcoded UTC offset.
    """
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now().astimezone()
    local_midnight_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_at_local_midnight = local_midnight_today.astimezone(timezone.utc)
    return now_utc.date() > utc_at_local_midnight.date()


def get_daily_summary_line(currency: str, username: str, token: str) -> str:
    """Returns a ready-to-use summary line. Never raises -- a flaky pool
    or price API shouldn't break the whole daily digest, so failures show
    up as a short note in the line itself instead."""
    ticker = COIN_INFO.get(currency, {}).get("ticker", currency.upper())
    try:
        balance_info = get_balance_info(currency, username, token)
    except Exception as e:
        return f"F2Pool: couldn't fetch stats ({e})"

    if _yesterday_reflects_today():
        coin_amount = balance_info.get("yesterday_income", 0)
        label = "today"
    else:
        coin_amount = balance_info.get("estimated_today_income", 0)
        label = "today so far (partial, still updating)"

    try:
        price = get_price_cad(currency)
        cad_value = coin_amount * price
        return f"F2Pool: {coin_amount:.8f} {ticker} mined {label} (~${cad_value:,.2f} CAD at ${price:,.0f} CAD/{ticker})"
    except Exception:
        return f"F2Pool: {coin_amount:.8f} {ticker} mined {label} (price lookup failed)"
