"""KAD (kad.arbitr.ru) scraper using Playwright sync API."""
import logging
import re
from typing import Optional

from .utils import USER_AGENT, VIEWPORT, TIMEOUT, random_delay, parse_date_ru, normalize_case_number

logger = logging.getLogger(__name__)

KAD_BASE = "https://kad.arbitr.ru"
KAD_SEARCH = f"{KAD_BASE}/Search"


class KADScraper:
    def __init__(self):
        self._browser = None
        self._playwright = None

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self):
        self._start()
        return self

    def __exit__(self, *_):
        self._stop()

    def _start(self):
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)

    def _stop(self):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def _new_page(self):
        context = self._browser.new_context(
            user_agent=USER_AGENT,
            viewport=VIEWPORT,
        )
        page = context.new_page()
        page.set_default_timeout(TIMEOUT)
        return page

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search_by_inn(self, inn: str) -> list[dict]:
        """Search KAD for all cases involving a party with the given INN."""
        try:
            page = self._new_page()
            page.goto(KAD_SEARCH)
            random_delay()

            # Fill INN field
            inn_input = page.locator('input[placeholder*="ИНН"], input[name*="inn"], #PartNumInn')
            inn_input.first.fill(inn)
            random_delay(0.5, 1.5)

            page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle", timeout=TIMEOUT)
            random_delay()

            return self._parse_search_results(page)
        except Exception as exc:
            logger.error("search_by_inn(%s) failed: %s", inn, exc)
            return []

    def search_by_case_number(self, case_number: str) -> Optional[dict]:
        """Search KAD for a specific case number. Returns basic case dict or None."""
        case_number = normalize_case_number(case_number)
        try:
            page = self._new_page()
            page.goto(KAD_SEARCH)
            random_delay()

            num_input = page.locator(
                'input[placeholder*="номер"], input[name*="number"], #CaseNumber, input.b-form-input__input'
            ).first
            num_input.fill(case_number)
            random_delay(0.5, 1.5)

            search_btn = page.locator('button[type="submit"], .b-button_type_submit').first
            search_btn.click()
            page.wait_for_load_state("networkidle", timeout=TIMEOUT)
            random_delay()

            results = self._parse_search_results(page)
            if results:
                return results[0]
            return None
        except Exception as exc:
            logger.error("search_by_case_number(%s) failed: %s", case_number, exc)
            return None

    def get_case_details(self, kad_url: str) -> Optional[dict]:
        """Fetch full case details from a KAD case page."""
        try:
            page = self._new_page()
            page.goto(kad_url)
            random_delay()
            page.wait_for_load_state("networkidle", timeout=TIMEOUT)
            random_delay()

            case_data = self._parse_case_page(page, kad_url)
            return case_data
        except Exception as exc:
            logger.error("get_case_details(%s) failed: %s", kad_url, exc)
            return None

    # ------------------------------------------------------------------
    # Private parsing helpers
    # ------------------------------------------------------------------

    def _parse_search_results(self, page) -> list[dict]:
        results = []
        try:
            page.wait_for_selector(".b-cases, .b-case-result, table.cases", timeout=10_000)
        except Exception:
            logger.debug("No results table found on search page.")
            return results

        rows = page.locator(".b-case-result, tr.case-row, .b-cases tbody tr").all()
        for row in rows:
            try:
                text = row.inner_text()
                link_el = row.locator("a").first
                href = link_el.get_attribute("href") or ""
                if href and not href.startswith("http"):
                    href = KAD_BASE + href

                case_num_match = re.search(r"[АA]\d{2}-\d+/\d{4}", text)
                case_number = normalize_case_number(case_num_match.group(0)) if case_num_match else ""

                results.append({
                    "case_number": case_number,
                    "kad_url": href,
                    "source": "kad",
                    "raw_text": text[:500],
                })
            except Exception as exc:
                logger.debug("Row parse error: %s", exc)
        return results

    def _parse_case_page(self, page, kad_url: str) -> dict:
        data: dict = {
            "kad_url": kad_url,
            "source": "kad",
            "participants": [],
            "events": [],
        }

        # Case number from title or header
        try:
            header = page.locator("h1, .b-case-header__number, .case-number").first.inner_text()
            m = re.search(r"[АA]\d{2}-[\d]+/\d{4}", header)
            if m:
                data["case_number"] = normalize_case_number(m.group(0))
        except Exception:
            pass

        # Extract case_id_kad from URL
        m_id = re.search(r"/([0-9a-f\-]{36})", kad_url)
        if m_id:
            data["case_id_kad"] = m_id.group(1)

        # Court
        try:
            court_el = page.locator('.b-case-header__court, [class*="court"]').first
            data["court"] = court_el.inner_text().strip()
        except Exception:
            pass

        # Judge
        try:
            judge_el = page.locator('[class*="judge"], .b-judge').first
            data["judge"] = judge_el.inner_text().strip()
        except Exception:
            pass

        # Status
        try:
            status_el = page.locator('[class*="status"], .b-case-status').first
            data["status"] = status_el.inner_text().strip()
        except Exception:
            pass

        # Case type
        try:
            type_el = page.locator('[class*="type"], .b-case-type').first
            data["case_type"] = type_el.inner_text().strip()
        except Exception:
            pass

        # Start date
        try:
            date_el = page.locator('[class*="date"], .b-start-date').first
            data["start_date"] = parse_date_ru(date_el.inner_text())
        except Exception:
            pass

        # Participants
        data["participants"] = self._parse_participants(page)

        # Events / hearings
        data["events"] = self._parse_events(page)

        return data

    def _parse_participants(self, page) -> list[dict]:
        participants = []
        try:
            rows = page.locator(".b-participants tr, .participants-table tr, [class*='participant']").all()
            for row in rows:
                cells = row.locator("td").all()
                if len(cells) >= 2:
                    participants.append({
                        "role": cells[0].inner_text().strip() if cells else "",
                        "name": cells[1].inner_text().strip() if len(cells) > 1 else "",
                        "inn": "",
                        "address": cells[2].inner_text().strip() if len(cells) > 2 else "",
                    })
        except Exception as exc:
            logger.debug("Participant parse error: %s", exc)
        return participants

    def _parse_events(self, page) -> list[dict]:
        events = []
        from datetime import datetime
        today = datetime.utcnow().strftime("%Y-%m-%d")
        try:
            rows = page.locator(
                ".b-events tr, .events-table tr, [class*='event'], .b-chrono tr"
            ).all()
            for row in rows:
                try:
                    text = row.inner_text()
                    date_str = parse_date_ru(text)
                    if not date_str:
                        continue
                    is_future = 1 if date_str > today else 0
                    # Try to get doc link
                    doc_url = ""
                    try:
                        a = row.locator("a").first
                        doc_url = a.get_attribute("href") or ""
                    except Exception:
                        pass
                    events.append({
                        "event_date": date_str,
                        "event_type": "hearing",
                        "description": text.strip()[:500],
                        "document_url": doc_url,
                        "is_future": is_future,
                    })
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("Event parse error: %s", exc)
        return events
