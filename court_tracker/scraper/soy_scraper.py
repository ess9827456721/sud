"""
SOY (Courts of General Jurisdiction) scraper.
Parses all 3 tabs of a sudrf.ru case card:
  Tab 1 — «Дело»       : case info, category, judge, dates, UID
  Tab 2 — «Движение»   : events table (4 template variants + AJAX nav)
  Tab 3 — «Стороны»    : parties with roles, names, INN

INN extraction uses FNS checksum validation.
"""
import json
import logging
import random
import re
import time
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


# ── INN validation (FNS algorithm) ──────────────────────────────────────────

def _validate_inn(inn: str) -> bool:
    """Return True if inn passes the FNS checksum algorithm."""
    digits = re.sub(r"\D", "", inn)
    if len(digits) == 10:
        w = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        chk = sum(w[i] * int(digits[i]) for i in range(9)) % 11 % 10
        return chk == int(digits[9])
    if len(digits) == 12:
        w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        c1 = sum(w1[i] * int(digits[i]) for i in range(10)) % 11 % 10
        c2 = sum(w2[i] * int(digits[i]) for i in range(11)) % 11 % 10
        return c1 == int(digits[10]) and c2 == int(digits[11])
    return False


def _extract_inn(text: str) -> Optional[str]:
    """
    Find and validate INN in cell text. Three patterns:
      1. Explicit label — «ИНН: 1234567890» or «ИНН 1234567890»
      2. In parentheses — «(ИНН: 1234567890)» or «(1234567890)»
      3. Standalone sequence of 10 or 12 digits at word boundary
    """
    # Pattern 1: with «ИНН» label
    m = re.search(r'ИНН[:\s]+(\d{10,12})', text)
    if m:
        candidate = m.group(1)
        if _validate_inn(candidate):
            return candidate

    # Pattern 2: in parentheses (with or without label)
    m = re.search(r'\(\s*(?:ИНН[:\s]*)?(\d{10,12})\s*\)', text)
    if m:
        candidate = m.group(1)
        if _validate_inn(candidate):
            return candidate

    # Pattern 3: bare 10/12 digit sequence
    for m in re.finditer(r'\b(\d{10}|\d{12})\b', text):
        candidate = m.group(1)
        if _validate_inn(candidate):
            return candidate

    return None


# ── Date parsing helper ──────────────────────────────────────────────────────

def _parse_date_str(text: str) -> Optional[str]:
    """Parse date from sudrf.ru: DD.MM.YYYY, DD.MM.YY, or ISO YYYY-MM-DD."""
    text = text.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


# ── Main scraper class ───────────────────────────────────────────────────────

class SOYScraper:
    """
    Playwright-based scraper for ГАС «Правосудие» (sudrf.ru).

    Usage:
        result = SOYScraper().scrape_case(url)

    Returns dict with keys:
        success, status, error_msg, case_info, participants, events
    """

    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self.browser = None
        self.context = None

    def _launch(self) -> None:
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(headless=self.headless)
        self.context = self.browser.new_context(
            user_agent=self._USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )

    def _close(self) -> None:
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self.context = self.browser = self._playwright = None

    # ── Public entry point ────────────────────────────────────────────────────

    def scrape_case(self, url: str) -> dict:
        """
        Full 3-tab parse of a sudrf.ru case card.
        Step 1: case info (tab «Дело»)
        Step 2: events (tab «Движение дела», with AJAX navigation)
        Step 3: parties (tab «Стороны», with 3 strategies)
        """
        try:
            self._launch()
            page = self.context.new_page()
            page.set_default_timeout(30_000)

            page.goto(url, wait_until="domcontentloaded")
            time.sleep(random.uniform(1.5, 3.0))

            if self._has_captcha(page):
                return _fail("captcha", "Обнаружена CAPTCHA на сайте суда")

            if not self._page_has_content(page):
                return _fail("failed", "Страница не загрузилась или пустая")

            # ── Step 1: case info ─────────────────────────────────────────────
            case_info = self._extract_case_info_full(page)

            # ── Step 2: events ────────────────────────────────────────────────
            self._navigate_to_events_tab(page, url)
            events = self._try_all_variants(page)

            if not events:
                return _fail("no_data",
                             "Таблица движения дела не найдена (шаблон не распознан)",
                             case_info=case_info)

            # ── Step 3: parties ───────────────────────────────────────────────
            participants = self._extract_participants_full(page, url)

            return {
                "success": True,
                "status": "success",
                "error_msg": None,
                "case_info": case_info,
                "participants": participants,
                "events": events,
            }

        except Exception as exc:
            err_type = type(exc).__name__
            if "Timeout" in err_type:
                return _fail("failed", "Таймаут: сайт суда не ответил за 30 секунд")
            return _fail("failed", f"Ошибка парсинга: {str(exc)[:200]}")
        finally:
            self._close()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _has_captcha(self, page) -> bool:
        for sel in ["input[name*=captcha]", "input[name*=code]",
                    "img[src*=captcha]", ".captcha", "#captcha"]:
            try:
                if page.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        return "captcha" in page.url.lower()

    def _page_has_content(self, page) -> bool:
        try:
            return (page.locator("table").count() > 0
                    or page.locator("#content").count() > 0
                    or page.locator(".судДело").count() > 0)
        except Exception:
            return False

    # ── Tab 1: Full case info ─────────────────────────────────────────────────

    def _extract_case_info_full(self, page) -> dict:
        """
        Extract all fields from the «Дело» tab:
        case_number, judge, category, category_path, receipt_date,
        decision_date, decision_result, court_first, uid
        """
        info: dict = {}

        try:
            text = page.content()

            # Case number (pattern: NN-NNNNN/NNNN)
            m = re.search(r'\d+-\d+/\d{4}', text)
            if m:
                info["case_number"] = m.group(0)

            # UID — usually 25+ digit string in a dedicated row
            uid_m = re.search(r'УИД[:\s]+(\d{15,30})', text)
            if uid_m:
                info["uid"] = uid_m.group(1)
        except Exception:
            pass

        # Scan table rows for labelled fields
        try:
            for row in page.locator("tr").all():
                cells = row.locator("td").all()
                if len(cells) < 2:
                    continue
                label = cells[0].inner_text().strip().rstrip(":")
                value = cells[1].inner_text().strip()
                if not value:
                    continue
                low = label.lower()
                if "судья" in low:
                    info.setdefault("judge", value)
                elif "категория" in low:
                    # Full hierarchical path separated by → or /
                    info["case_category_path"] = value
                    # Short label = last non-empty segment
                    parts = re.split(r'[→/]', value)
                    info["case_category"] = parts[-1].strip() if parts else value
                elif "дата поступления" in low or "поступило" in low:
                    info.setdefault("receipt_date", _parse_date_str(value))
                elif "дата рассмотрения" in low or "рассмотрено" in low:
                    info.setdefault("decision_date", _parse_date_str(value))
                elif any(k in low for k in ["результат", "итог", "решение"]):
                    info.setdefault("decision_result", value[:200])
                elif "суд первой" in low or "первоначальный" in low:
                    info.setdefault("court_first", value)
                elif "уид" in low or "уникальный идентификатор" in low:
                    info.setdefault("uid", re.sub(r'\s+', '', value))
        except Exception:
            pass

        return info

    # ── Tab 2: Events navigation ──────────────────────────────────────────────

    def _navigate_to_events_tab(self, page, base_url: str) -> None:
        """
        Navigate to the events/movement tab.
        Some sudrf.ru sites load all tabs on one page (no action needed).
        Others use AJAX tabs — try clicking the tab or loading via URL param.
        """
        # Check if events table already present
        if self._events_visible(page):
            return

        # Strategy A: click tab with text "Движение"
        try:
            tabs = page.locator("a, li, td, th, span, button").all()
            for tab in tabs:
                t = tab.inner_text().strip().lower()
                if "движение" in t and len(t) < 30:
                    tab.click()
                    page.wait_for_timeout(2000)
                    if self._events_visible(page):
                        return
                    break
        except Exception:
            pass

        # Strategy B: direct URL with name_op=sf
        try:
            sf_url = _replace_param(base_url, "name_op", "sf")
            if sf_url != base_url:
                page.goto(sf_url, wait_until="domcontentloaded")
                time.sleep(1.5)
        except Exception:
            pass

    def _events_visible(self, page) -> bool:
        """Check if an events/movement table is already rendered."""
        try:
            for keyword in ["движение", "наименование события", "заседание", "tablcont"]:
                if keyword.lower() in page.content().lower():
                    return True
        except Exception:
            pass
        return False

    # ── Tab 3: Parties extraction ─────────────────────────────────────────────

    def _extract_participants_full(self, page, base_url: str) -> list:
        """
        Extract parties from «Стороны» tab using 3 strategies in order:
          1. Data already on current page
          2. Click «Стороны» tab (AJAX)
          3. Direct URL with name_op=parts
        """
        # Strategy 1: already on page
        result = self._parse_parties_page(page)
        if result:
            return result

        # Strategy 2: click tab
        try:
            for tab in page.locator("a, li, td, th, span, button").all():
                t = tab.inner_text().strip().lower()
                if ("стороны" in t or "участники" in t) and len(t) < 30:
                    tab.click()
                    page.wait_for_timeout(2000)
                    result = self._parse_parties_page(page)
                    if result:
                        return result
                    break
        except Exception:
            pass

        # Strategy 3: direct URL
        try:
            parts_url = _replace_param(base_url, "name_op", "parts")
            if parts_url != base_url:
                page.goto(parts_url, wait_until="domcontentloaded")
                time.sleep(1.5)
                result = self._parse_parties_page(page)
                if result:
                    return result
        except Exception:
            pass

        return []

    def _parse_parties_page(self, page) -> list:
        """
        Parse parties table from current page state.
        Looks for tables with role keywords; extracts name, INN, representative.
        """
        _ROLE_KEYWORDS = ["истец", "ответчик", "заявитель", "должник",
                          "взыскатель", "третье лицо", "прокурор",
                          "представитель", "иное лицо"]
        participants = []

        try:
            for table in page.locator("table").all():
                raw_text = table.inner_text().lower()
                if not any(kw in raw_text for kw in _ROLE_KEYWORDS):
                    continue

                rows = table.locator("tr").all()
                for row in rows:
                    cells = row.locator("td").all()
                    if len(cells) < 2:
                        continue
                    role_text = cells[0].inner_text().strip()
                    name_text = cells[1].inner_text().strip()

                    if not role_text or not name_text:
                        continue
                    if not any(kw in role_text.lower() for kw in _ROLE_KEYWORDS):
                        continue

                    # Extract INN from name cell (or third cell if present)
                    inn_source = name_text
                    if len(cells) >= 3:
                        inn_source += " " + cells[2].inner_text()

                    inn = _extract_inn(inn_source)

                    # Clean name: remove INN artefacts in parentheses
                    clean_name = re.sub(r'\s*\(\s*(?:ИНН[:\s]*)?\d{10,12}\s*\)', '',
                                        name_text).strip()

                    # Representative: sometimes in a sub-row or 4th cell
                    representative = None
                    if len(cells) >= 4:
                        rep = cells[3].inner_text().strip()
                        if rep and len(rep) > 3:
                            representative = rep[:200]

                    participants.append({
                        "role":           role_text[:100],
                        "name":           clean_name[:300],
                        "inn":            inn,
                        "address":        None,
                        "representative": representative,
                        "inn_manual":     0,
                        "name_manual":    0,
                        "address_manual": 0,
                    })

                if participants:
                    break

        except Exception:
            pass

        return participants

    # ── Event extraction (all 4 variants) ────────────────────────────────────

    def _try_all_variants(self, page) -> list:
        for extractor in [
            self._variant_a_tablcont,
            self._variant_b_dev_ras,
            self._variant_c_odd_even,
            self._variant_d_json,
            self._variant_fallback_any_table,
        ]:
            try:
                events = extractor(page)
                if events:
                    return events
            except Exception:
                continue
        return []

    def _parse_event_rows(self, rows) -> list:
        events: list = []
        today = date.today()
        for row in rows:
            cells = row.locator("td").all()
            if len(cells) < 2:
                continue
            date_text = cells[0].inner_text().strip()
            desc_text = cells[1].inner_text().strip()
            if not date_text or not desc_text:
                continue

            event_date_str = _parse_date_str(date_text)
            if not event_date_str:
                continue
            try:
                event_date = date.fromisoformat(event_date_str)
            except ValueError:
                continue

            desc_lower = desc_text.lower()
            if any(w in desc_lower for w in ["заседание", "слушание", "рассмотрение"]):
                event_type = "hearing"
            elif any(w in desc_lower for w in ["решение", "определение", "приговор", "постановление"]):
                event_type = "decision"
            elif any(w in desc_lower for w in ["приостановление", "возобновление"]):
                event_type = "suspension"
            elif any(w in desc_lower for w in ["поступление", "возбуждение", "принятие к производству"]):
                event_type = "start"
            else:
                event_type = "other"

            doc_url: Optional[str] = None
            try:
                for link in row.locator("a").all():
                    href = link.get_attribute("href")
                    if href and ("sud_delo" in href or ".pdf" in href or "showdoc" in href):
                        doc_url = href if href.startswith("http") else None
                        break
            except Exception:
                pass

            events.append({
                "event_date":   event_date_str,
                "event_type":   event_type,
                "description":  desc_text[:500],
                "document_url": doc_url,
                "is_future":    1 if event_date > today else 0,
            })
        return events

    def _variant_a_tablcont(self, page) -> list:
        table = page.locator("table.tablcont, table#tablcont").first
        if table.count() == 0:
            return []
        rows = table.locator("tr").all()
        return self._parse_event_rows(rows[1:] if len(rows) > 1 else rows)

    def _variant_b_dev_ras(self, page) -> list:
        rows = page.locator("tr.dev_ras").all()
        return self._parse_event_rows(rows) if rows else []

    def _variant_c_odd_even(self, page) -> list:
        rows = page.locator("tr.odd, tr.even").all()
        return self._parse_event_rows(rows) if len(rows) >= 2 else []

    def _variant_d_json(self, page) -> list:
        today = date.today()
        try:
            for script in page.locator("script[type*=json]").all():
                content = script.inner_text()
                if not content.strip():
                    continue
                data = json.loads(content)

                events_raw = None
                for key in ["events", "movements", "движение", "delo_events", "stages"]:
                    if key in data:
                        events_raw = data[key]
                        break
                if not events_raw:
                    def _find_list(d, depth=0):
                        if depth > 4:
                            return None
                        if isinstance(d, list) and len(d) > 0:
                            return d
                        if isinstance(d, dict):
                            for v in d.values():
                                r = _find_list(v, depth + 1)
                                if r:
                                    return r
                        return None
                    events_raw = _find_list(data)
                if not events_raw:
                    continue

                events: list = []
                for item in events_raw:
                    if not isinstance(item, dict):
                        continue
                    date_val = (item.get("date") or item.get("дата")
                                or item.get("event_date") or item.get("Date", ""))
                    desc_val = (item.get("name") or item.get("description")
                                or item.get("наименование") or item.get("Name", ""))
                    if not date_val or not desc_val:
                        continue
                    ed = _parse_date_str(str(date_val)[:10])
                    if not ed:
                        continue
                    try:
                        event_date = date.fromisoformat(ed)
                    except ValueError:
                        continue
                    events.append({
                        "event_date":   ed,
                        "event_type":   "hearing" if "заседание" in str(desc_val).lower() else "other",
                        "description":  str(desc_val)[:500],
                        "document_url": None,
                        "is_future":    1 if event_date > today else 0,
                    })
                if events:
                    return events
        except Exception:
            pass
        return []

    def _variant_fallback_any_table(self, page) -> list:
        date_pattern = re.compile(r"\d{2}\.\d{2}\.\d{4}")
        best_table = None
        best_count = 0
        try:
            for table in page.locator("table").all():
                count = len(date_pattern.findall(table.inner_text()))
                if count > best_count:
                    best_count = count
                    best_table = table
        except Exception:
            return []
        if best_table is None or best_count < 2:
            return []
        rows = best_table.locator("tr").all()
        return self._parse_event_rows(rows[1:] if len(rows) > 1 else rows)


# ── Module-level helpers ──────────────────────────────────────────────────────

def _fail(status: str, msg: str, case_info: Optional[dict] = None) -> dict:
    return {
        "success": False,
        "status": status,
        "error_msg": msg,
        "case_info": case_info,
        "participants": [],
        "events": [],
    }


def _replace_param(url: str, key: str, value: str) -> str:
    """Replace or add a query parameter in a URL."""
    if f"{key}=" in url:
        return re.sub(rf'{re.escape(key)}=[^&]*', f'{key}={value}', url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{key}={value}"
