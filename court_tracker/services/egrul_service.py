"""EGRUL / EGRIP lookup via nalog.ru public API with 1-hour in-memory cache."""
import logging
import random
import string
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, Optional[dict]]] = {}  # inn → (ts, data)
_CACHE_TTL = 3600  # seconds

_SEARCH_URL = "https://egrul.nalog.ru/"
_TOKEN_URL  = "https://egrul.nalog.ru/search-result/"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://egrul.nalog.ru/",
}

_STATUS_MAP = {
    "ДЕЙСТВУЮЩЕЕ": "ACTIVE",
    "ЛИКВИДИРОВАНО": "LIQUIDATED",
    "БАНКРОТСТВО": "BANKRUPT",
    "В ПРОЦЕССЕ ЛИКВИДАЦИИ": "LIQUIDATING",
    "В ПРОЦЕССЕ РЕОРГАНИЗАЦИИ": "REORGANIZING",
}


def _rnd() -> str:
    return "".join(random.choices(string.digits, k=10))


def _parse_row(row: dict) -> dict:
    """Normalize a nalog.ru result row to our schema dict."""
    name    = row.get("n", "") or row.get("name", "")
    short   = row.get("c", "") or row.get("shortName", "") or name
    ogrn    = row.get("o", "") or row.get("ogrn", "")
    inn     = row.get("i", "") or row.get("inn", "")
    kpp     = row.get("p", "") or row.get("kpp", "")
    address = row.get("a", "") or row.get("address", "")
    raw_status = (row.get("e", "") or row.get("status", "")).upper()
    status = _STATUS_MAP.get(raw_status, raw_status or "ACTIVE")
    # Determine type: IP has 12-digit INN
    client_type = "ip" if len(inn) == 12 else "legal"

    return {
        "type":         client_type,
        "name":         name,
        "short_name":   short,
        "inn":          inn,
        "ogrn":         ogrn,
        "kpp":          kpp,
        "address":      address,
        "status_egrul": status,
    }


def fetch_by_inn(inn: str) -> Optional[dict]:
    """
    Query nalog.ru for an entity by INN.
    Returns normalized dict or None on failure / not found.
    Results are cached for 1 hour.
    """
    inn = inn.strip()
    now = time.time()
    if inn in _CACHE:
        ts, cached = _CACHE[inn]
        if now - ts < _CACHE_TTL:
            return cached

    result = _do_fetch(inn)
    _CACHE[inn] = (now, result)
    return result


def _do_fetch(inn: str) -> Optional[dict]:
    session = requests.Session()
    session.headers.update(_HEADERS)
    try:
        # Step 1: submit search query, get token
        resp = session.post(
            _SEARCH_URL,
            data={"query": inn, "rnd": _rnd(), "versiya": "2.0"},
            timeout=10,
        )
        resp.raise_for_status()
        token_data = resp.json()
        token = token_data.get("t") or token_data.get("token")
        if not token:
            # Some API versions return rows directly
            rows = token_data.get("rows") or token_data.get("v", [])
            if rows:
                return _parse_row(rows[0])
            logger.debug("egrul: no token in response for INN %s", inn)
            return None

        # Step 2: fetch results by token
        time.sleep(0.5)
        resp2 = session.get(
            f"{_TOKEN_URL}{token}",
            params={"r": _rnd(), "_": int(time.time() * 1000)},
            timeout=10,
        )
        resp2.raise_for_status()
        result_data = resp2.json()
        rows = result_data.get("rows") or result_data.get("v", [])
        if not rows:
            logger.debug("egrul: empty rows for INN %s", inn)
            return None
        return _parse_row(rows[0])

    except requests.exceptions.Timeout:
        logger.warning("egrul: timeout for INN %s", inn)
        return None
    except requests.exceptions.RequestException as exc:
        logger.warning("egrul: request error for INN %s: %s", inn, exc)
        return None
    except Exception as exc:
        logger.warning("egrul: unexpected error for INN %s: %s", inn, exc)
        return None


def clear_cache() -> None:
    _CACHE.clear()
