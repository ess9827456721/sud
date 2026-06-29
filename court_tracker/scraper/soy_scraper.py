"""
SOY (Courts of General Jurisdiction) scraper.
Scrapes case cards from sudrf.ru-based court websites.
Supports all 4 known HTML template variants.
"""
import json
import logging
import random
import re
import time
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


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
        self.context = None
        self.browser = None
        self._playwright = None

    # ── Public entry point ───────────────────────────────────────────────────

    def scrape_case(self, url: str) -> dict:
        """
        Open SOY case card URL, try all template variants, return structured data.
        """
        try:
            self._launch()
            page = self.context.new_page()
            page.set_default_timeout(30_000)

            page.goto(url, wait_until="domcontentloaded")
            time.sleep(random.uniform(1.5, 3.0))

            if self._has_captcha(page):
                return {
                    "success": False,
                    "status": "captcha",
                    "error_msg": "Обнаружена CAPTCHA на сайте суда",
                    "case_info": None,
                    "participants": [],
                    "events": [],
                }

            if not self._page_has_content(page):
                return {
                    "success": False,
                    "status": "failed",
                    "error_msg": "Страница не загрузилась или пустая",
                    "case_info": None,
                    "participants": [],
                    "events": [],
                }

            case_info = self._extract_case_info(page)
            participants = self._extract_participants(page)
            events = self._try_all_variants(page)

            if not events:
                return {
                    "success": False,
                    "status": "no_data",
                    "error_msg": "Таблица движения дела не найдена (шаблон не распознан)",
                    "case_info": case_info,
                    "participants": participants,
                    "events": [],
                }

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
                return {
                    "success": False,
                    "status": "failed",
                    "error_msg": "Таймаут: сайт суда не ответил за 30 секунд",
                    "case_info": None,
                    "participants": [],
                    "events": [],
                }
            return {
                "success": False,
                "status": "failed",
                "error_msg": f"Ошибка парсинга: {str(exc)[:200]}",
                "case_info": None,
                "participants": [],
                "events": [],
            }
        finally:
            self._close()

    # ── Captcha detection ────────────────────────────────────────────────────

    def _has_captcha(self, page) -> bool:
        indicators = [
            "input[name*=captcha]",
            "input[name*=code]",
            "img[src*=captcha]",
            ".captcha",
            "#captcha",
        ]
        for sel in indicators:
            try:
                if page.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        return "captcha" in page.url.lower()

    # ── Page content check ───────────────────────────────────────────────────

    def _page_has_content(self, page) -> bool:
        try:
            return (
                page.locator("table").count() > 0
                or page.locator("#content").count() > 0
                or page.locator(".судДело").count() > 0
            )
        except Exception:
            return False

    # ── Case info extraction ─────────────────────────────────────────────────

    def _extract_case_info(self, page) -> dict:
        info: dict = {}
        try:
            text = page.content()
            m = re.search(r"\d+-\d+/\d{4}", text)
            if m:
                info["case_number"] = m.group(0)
        except Exception:
            pass

        try:
            for row in page.locator("tr").all():
                row_text = row.inner_text()
                if "Судья" in row_text:
                    cells = row.locator("td").all()
                    if len(cells) >= 2:
                        info["judge"] = cells[1].inner_text().strip()
                        break
        except Exception:
            pass

        try:
            for label in ["Результат рассмотрения", "Стадия", "Итог"]:
                els = page.get_by_text(label, exact=False).all()
                if els:
                    parent = els[0].locator("..")
                    cells = parent.locator("td").all()
                    if len(cells) >= 2:
                        info["status"] = cells[-1].inner_text().strip()
                        break
        except Exception:
            pass

        return info

    # ── Participants ─────────────────────────────────────────────────────────

    def _extract_participants(self, page) -> list:
        participants: list = []
        try:
            for table in page.locator("table").all():
                text = table.inner_text()
                if any(kw in text for kw in ["Истец", "Ответчик", "Заявитель", "Должник"]):
                    for row in table.locator("tr").all():
                        cells = row.locator("td").all()
                        if len(cells) >= 2:
                            role = cells[0].inner_text().strip()
                            name = cells[1].inner_text().strip()
                            if role and name and len(name) > 2:
                                participants.append({"role": role, "name": name, "inn": None})
                    if participants:
                        break
        except Exception:
            pass
        return participants

    # ── Event extraction: variant dispatcher ─────────────────────────────────

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

            event_date: Optional[date] = None
            for fmt in ["%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"]:
                try:
                    event_date = datetime.strptime(date_text, fmt).date()
                    break
                except ValueError:
                    pass
            if not event_date:
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
                "event_date": str(event_date),
                "event_type": event_type,
                "description": desc_text[:500],
                "document_url": doc_url,
                "is_future": 1 if event_date > today else 0,
            })
        return events

    def _variant_a_tablcont(self, page) -> list:
        """Вариант A: <table class=tablcont> — основной шаблон (~70% судов)."""
        table = page.locator("table.tablcont, table#tablcont").first
        if table.count() == 0:
            return []
        rows = table.locator("tr").all()
        return self._parse_event_rows(rows[1:] if len(rows) > 1 else rows)

    def _variant_b_dev_ras(self, page) -> list:
        """Вариант B: строки с классом dev_ras (~20% судов)."""
        rows = page.locator("tr.dev_ras").all()
        return self._parse_event_rows(rows) if rows else []

    def _variant_c_odd_even(self, page) -> list:
        """Вариант C: строки odd/even (Мосгорсуд и ряд областных судов)."""
        rows = page.locator("tr.odd, tr.even").all()
        return self._parse_event_rows(rows) if len(rows) >= 2 else []

    def _variant_d_json(self, page) -> list:
        """Вариант D: данные в JSON-скрипте (новый шаблон 2023+)."""
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
                                result = _find_list(v, depth + 1)
                                if result:
                                    return result
                        return None
                    events_raw = _find_list(data)

                if not events_raw:
                    continue

                events: list = []
                for item in events_raw:
                    if not isinstance(item, dict):
                        continue
                    date_val = (
                        item.get("date") or item.get("дата")
                        or item.get("event_date") or item.get("Date", "")
                    )
                    desc_val = (
                        item.get("name") or item.get("description")
                        or item.get("наименование") or item.get("Name", "")
                    )
                    if not date_val or not desc_val:
                        continue
                    try:
                        event_date = date.fromisoformat(str(date_val)[:10])
                    except ValueError:
                        continue
                    events.append({
                        "event_date": str(event_date),
                        "event_type": "hearing" if "заседание" in str(desc_val).lower() else "other",
                        "description": str(desc_val)[:500],
                        "document_url": None,
                        "is_future": 1 if event_date > today else 0,
                    })
                if events:
                    return events
        except Exception:
            pass
        return []

    def _variant_fallback_any_table(self, page) -> list:
        """Fallback: find any table containing DD.MM.YYYY dates."""
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
