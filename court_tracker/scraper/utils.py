"""Scraper utility helpers."""
import random
import time
import re
import logging

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

VIEWPORT = {"width": 1280, "height": 900}
TIMEOUT = 45_000  # ms — increased for slow KAD responses


def random_delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    time.sleep(random.uniform(min_s, max_s))


def normalize_case_number(raw: str) -> str:
    """
    Canonical case-number form uses the CYRILLIC 'А' (U+0410).
    KAD search returns nothing for the Latin spelling ("A70-20030/2025"),
    so Latin A/a is converted to Cyrillic. The DB stores this form and
    all KAD queries use it.
    """
    s = raw.strip()
    s = s.replace("A", "А").replace("a", "А")
    return s


def parse_date_ru(text: str) -> str | None:
    """Convert Russian date strings like '15 января 2024 г.' to ISO YYYY-MM-DD."""
    months = {
        "января": "01", "февраля": "02", "марта": "03",
        "апреля": "04", "мая": "05", "июня": "06",
        "июля": "07", "августа": "08", "сентября": "09",
        "октября": "10", "ноября": "11", "декабря": "12",
    }
    m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", text or "")
    if m:
        day, mon, year = m.group(1).zfill(2), m.group(2).lower(), m.group(3)
        return f"{year}-{months.get(mon, '00')}-{day}"
    # Try numeric format DD.MM.YYYY
    m2 = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text or "")
    if m2:
        return f"{m2.group(3)}-{m2.group(2)}-{m2.group(1)}"
    return None
