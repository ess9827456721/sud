"""
KAD (kad.arbitr.ru) scraper using Playwright sync API.

Primary search path is KAD's own internal JSON API (POST /Kad/SearchInstances)
called from inside the loaded page so cookies/wizard tokens apply. The UI
search remains as a fallback. A promo popup («Электронный страж») intercepts
pointer events and is removed before any interaction.
"""
import logging
import re
from datetime import datetime
from typing import Optional

from .utils import USER_AGENT, VIEWPORT, TIMEOUT, random_delay, parse_date_ru, normalize_case_number

logger = logging.getLogger(__name__)

KAD_BASE = "https://kad.arbitr.ru"


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
    # Debug artifacts
    # ------------------------------------------------------------------

    def _debug_dump(self, page, tag: str) -> str:
        """Save screenshot + HTML to the debug/ folder; return message part."""
        try:
            from court_tracker.config import DEBUG_DIR
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            png = DEBUG_DIR / f"kad_{tag}_{ts}.png"
            html = DEBUG_DIR / f"kad_{tag}_{ts}.html"
            try:
                page.screenshot(path=str(png), full_page=True)
            except Exception:
                pass
            try:
                html.write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            logger.info("KAD debug artifacts: %s, %s", png, html)
            return f"debug: {png.name}, {html.name}"
        except Exception as exc:
            logger.debug("debug dump failed: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # Overlay killer
    # ------------------------------------------------------------------

    def _dismiss_overlays(self, page) -> None:
        """Close/remove the promo popup that intercepts all pointer events."""
        try:
            close = page.locator(
                'a.b-promo_notification-popup-close, '
                '[class*="promo"] [class*="close"]'
            ).first
            if close.is_visible(timeout=1000):
                close.click()
        except Exception:
            pass
        try:
            page.evaluate("""() => {
                document.querySelectorAll(
                  '.b-promo_notification, .b-promo_notification-popup_wrapper, ' +
                  '.js-promo_notification-popup, [class*="promo_notification"]'
                ).forEach(e => e.remove());
            }""")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal JSON API
    # ------------------------------------------------------------------

    def _api_search(self, page, payload: dict) -> Optional[dict]:
        """POST /Kad/SearchInstances from within the page context."""
        script = """async (payload) => {
            const r = await fetch('/Kad/SearchInstances', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                body: JSON.stringify(payload)
            });
            if (!r.ok) return {__error: r.status};
            return await r.json();
        }"""
        try:
            data = page.evaluate(script, payload)
            if isinstance(data, dict) and data.get("__error"):
                logger.warning("KAD API returned HTTP %s", data["__error"])
                return None
            return data
        except Exception as exc:
            logger.warning("KAD API search failed: %s", exc)
            return None

    @staticmethod
    def _api_items(data) -> list:
        """Extract the items list from the API response defensively."""
        if not isinstance(data, dict):
            return []
        logger.debug("KAD API response top-level keys: %s", list(data.keys()))
        result = data.get("Result", data)
        if isinstance(result, dict):
            for key in ("Items", "items", "Cases"):
                items = result.get(key)
                if isinstance(items, list):
                    return items
        if isinstance(result, list):
            return result
        return []

    @staticmethod
    def _item_to_case(item) -> Optional[dict]:
        """Map one API item to our case dict."""
        if not isinstance(item, dict):
            return None
        num = item.get("CaseNumber") or item.get("CaseNo") or item.get("Number")
        case_id = item.get("CaseId") or item.get("Id") or item.get("caseId")
        court = item.get("CourtName") or item.get("Court") or ""
        if isinstance(court, dict):
            court = court.get("Name") or court.get("Title") or ""
        judge = item.get("Judge") or ""
        if isinstance(judge, dict):
            judge = judge.get("Name") or ""
        date_raw = item.get("Date") or item.get("StartDate") or ""
        start_date = None
        if date_raw:
            m = re.search(r"\d{4}-\d{2}-\d{2}", str(date_raw))
            start_date = m.group(0) if m else parse_date_ru(str(date_raw))
        if not num:
            return None
        return {
            "case_number": normalize_case_number(str(num)),
            "case_id_kad": str(case_id) if case_id else None,
            "kad_url": f"{KAD_BASE}/Card/{case_id}" if case_id else None,
            "court": str(court) or None,
            "judge": str(judge) or None,
            "start_date": start_date,
            "source": "kad",
        }

    def _open_main_page(self, page) -> None:
        page.goto(f"{KAD_BASE}/", wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=TIMEOUT)
        except Exception:
            pass
        random_delay(1, 2)
        self._dismiss_overlays(page)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search_by_case_number(self, case_number: str) -> Optional[dict]:
        """Search KAD for a specific case number. Returns case dict or None."""
        case_number = normalize_case_number(case_number)
        page = None
        try:
            page = self._new_page()
            self._open_main_page(page)

            # ── Primary: internal JSON API ────────────────────────────────
            payload = {
                "Page": 1, "Count": 25, "Courts": [],
                "DateFrom": None, "DateTo": None,
                "Sides": [], "Judges": [],
                "CaseNumbers": [case_number],
                "WithVKSInstances": False,
            }
            data = self._api_search(page, payload)
            items = self._api_items(data)
            cases = [c for c in (self._item_to_case(i) for i in items) if c]
            if cases:
                return cases[0]

            # ── Fallback: UI search ───────────────────────────────────────
            logger.info("KAD API gave no results for %s — trying UI fallback", case_number)
            result = self._ui_search_case_number(page, case_number)
            if result:
                return result

            self._debug_dump(page, "search_empty")
            return None
        except Exception as exc:
            logger.error("search_by_case_number(%s) failed: %s", case_number, exc)
            if page:
                self._debug_dump(page, "search_error")
            return None

    def search_by_inn(self, inn: str) -> list[dict]:
        """Search KAD for all cases involving a party with the given INN."""
        page = None
        try:
            page = self._new_page()
            self._open_main_page(page)

            payload = {
                "Page": 1, "Count": 25, "Courts": [],
                "DateFrom": None, "DateTo": None,
                "Sides": [{"Name": inn, "Type": -1, "ExactMatch": False}],
                "Judges": [], "CaseNumbers": [],
                "WithVKSInstances": False,
            }
            data = self._api_search(page, payload)
            items = self._api_items(data)
            cases = [c for c in (self._item_to_case(i) for i in items) if c]
            if cases:
                return cases

            logger.info("KAD API gave no results for INN %s — trying UI fallback", inn)
            ui = self._ui_search_inn(page, inn)
            if ui:
                return ui

            self._debug_dump(page, "inn_empty")
            return []
        except Exception as exc:
            logger.error("search_by_inn(%s) failed: %s", inn, exc)
            if page:
                self._debug_dump(page, "inn_error")
            return []

    # ------------------------------------------------------------------
    # UI fallback (never bare input[type="text"] — it matches the judge field)
    # ------------------------------------------------------------------

    def _find_case_number_input(self, page):
        # a) known id
        try:
            el = page.locator("input#sug-cases").first
            if el.is_visible(timeout=2000):
                return el
        except Exception:
            pass
        # b) KAD's example placeholder «например, А50-5568/08»
        try:
            el = page.get_by_placeholder(re.compile("А50-5568")).first
            if el.is_visible(timeout=1500):
                return el
        except Exception:
            pass
        # c) input inside the container labelled «Номер дела»
        for sel in ('div:has-text("Номер дела") input',
                    'li:has-text("Номер дела") input'):
            try:
                el = page.locator(sel).last
                if el.is_visible(timeout=1500):
                    ph = (el.get_attribute("placeholder") or "").lower()
                    if "судь" not in ph:  # never the judge field
                        return el
            except Exception:
                pass
        return None

    def _find_inn_input(self, page):
        for sel in ("input#sug-participants",
                    'div:has-text("Участник дела") textarea',
                    'div:has-text("Участник дела") input',
                    'input[placeholder*="ИНН"]'):
            try:
                el = page.locator(sel).last
                if el.is_visible(timeout=1500):
                    return el
            except Exception:
                pass
        return None

    def _submit_search(self, page, field) -> None:
        self._dismiss_overlays(page)
        for sel in ("#b-form-submit button", 'button:has-text("Найти")',
                    'button[type="submit"]'):
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=1000):
                    btn.click()
                    return
            except Exception:
                pass
        try:
            field.press("Enter")
        except Exception:
            pass

    def _ui_search_case_number(self, page, case_number: str) -> Optional[dict]:
        self._dismiss_overlays(page)
        field = self._find_case_number_input(page)
        if field is None:
            logger.error("KAD UI: case number input not found")
            self._debug_dump(page, "no_case_input")
            return None
        field.fill(case_number)
        random_delay(0.5, 1.0)
        self._submit_search(page, field)
        try:
            page.wait_for_load_state("networkidle", timeout=TIMEOUT)
        except Exception:
            pass
        random_delay(1, 2)
        results = self._parse_search_results(page)
        return results[0] if results else None

    def _ui_search_inn(self, page, inn: str) -> list[dict]:
        self._dismiss_overlays(page)
        field = self._find_inn_input(page)
        if field is None:
            logger.error("KAD UI: participant/INN input not found")
            self._debug_dump(page, "no_inn_input")
            return []
        field.fill(inn)
        random_delay(0.5, 1.0)
        self._submit_search(page, field)
        try:
            page.wait_for_load_state("networkidle", timeout=TIMEOUT)
        except Exception:
            pass
        random_delay(1, 2)
        return self._parse_search_results(page)

    # ------------------------------------------------------------------
    # Card page (details)
    # ------------------------------------------------------------------

    def get_case_details(self, kad_url: str) -> Optional[dict]:
        """Fetch full case details from a KAD Card page."""
        page = None
        try:
            page = self._new_page()
            page.goto(kad_url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=TIMEOUT)
            except Exception:
                pass
            random_delay(1, 2)
            self._dismiss_overlays(page)

            case_data = self._parse_case_page(page, kad_url)
            if not case_data.get("case_number") and not case_data.get("events"):
                self._debug_dump(page, "card_empty")
            return case_data
        except Exception as exc:
            logger.error("get_case_details(%s) failed: %s", kad_url, exc)
            if page:
                self._debug_dump(page, "card_error")
            return None

    # ------------------------------------------------------------------
    # Private parsing helpers
    # ------------------------------------------------------------------

    def _parse_search_results(self, page) -> list[dict]:
        results = []
        try:
            page.wait_for_selector(
                "#b-cases, .b-cases, table#b-cases, .b-case-result", timeout=10_000
            )
        except Exception:
            logger.debug("No results table found on search page.")
            return results

        rows = page.locator("#b-cases tbody tr, .b-cases tbody tr, .b-case-result").all()
        for row in rows:
            try:
                text = row.inner_text()
                link_el = row.locator("a").first
                href = link_el.get_attribute("href") or ""
                if href and not href.startswith("http"):
                    href = KAD_BASE + href

                case_num_match = re.search(r"[АA]\d{1,2}-\d+/\d{4}", text)
                case_number = normalize_case_number(case_num_match.group(0)) if case_num_match else ""
                if not case_number:
                    continue

                m_id = re.search(r"/([0-9a-fA-F\-]{36})", href)
                results.append({
                    "case_number": case_number,
                    "case_id_kad": m_id.group(1) if m_id else None,
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

        # Case number: header selectors, then regex over full content
        try:
            header = page.locator(
                "#case_number, .b-case-header, .b-case-header__number, .case-number, h1"
            ).first.inner_text()
            m = re.search(r"[АA]\d{1,2}-\d+/\d{4}", header)
            if m:
                data["case_number"] = normalize_case_number(m.group(0))
        except Exception:
            pass
        if not data.get("case_number"):
            try:
                m = re.search(r"[АA]\d{1,2}-\d+/\d{4}", page.content())
                if m:
                    data["case_number"] = normalize_case_number(m.group(0))
            except Exception:
                pass

        # Extract case_id_kad from URL
        m_id = re.search(r"/([0-9a-fA-F\-]{36})", kad_url)
        if m_id:
            data["case_id_kad"] = m_id.group(1)

        # Court / judge / status / type — best-effort selectors
        for field, sels in (
            ("court",  '.b-case-header__court, [class*="instantion-name"], [class*="court"]'),
            ("judge",  '[class*="judge"], .b-judge'),
            ("status", '[class*="status"], .b-case-status'),
            ("case_type", '.b-case-header__type, [class*="case-type"]'),
        ):
            try:
                el = page.locator(sels).first
                val = el.inner_text().strip()
                if val:
                    data[field] = val[:300]
            except Exception:
                pass
        try:
            date_el = page.locator('[class*="date"], .b-start-date').first
            data["start_date"] = parse_date_ru(date_el.inner_text())
        except Exception:
            pass

        data["participants"] = self._parse_participants(page)
        data["events"] = self._parse_events(page)
        return data

    def _parse_participants(self, page) -> list[dict]:
        participants = []
        # Card sidebar blocks: plaintiffs / respondents / third parties
        _BLOCKS = (
            ("Истец", '[class*="plaintiff"]'),
            ("Ответчик", '[class*="respondent"]'),
            ("Третье лицо", '[class*="third"]'),
        )
        try:
            for role, sel in _BLOCKS:
                for el in page.locator(f"{sel} li, {sel} span").all()[:20]:
                    try:
                        name = el.inner_text().strip()
                        if name and 3 < len(name) < 300 and role.lower() not in name.lower():
                            participants.append({
                                "role": role, "name": name[:300],
                                "inn": "", "address": "",
                            })
                    except Exception:
                        pass
            # de-duplicate
            seen = set()
            unique = []
            for p in participants:
                key = (p["role"], p["name"].lower())
                if key not in seen:
                    seen.add(key)
                    unique.append(p)
            participants = unique
        except Exception as exc:
            logger.debug("Sidebar participant parse error: %s", exc)

        if participants:
            return participants

        # Fallback: old table layout
        try:
            rows = page.locator(".b-participants tr, .participants-table tr").all()
            for row in rows:
                cells = row.locator("td").all()
                if len(cells) >= 2:
                    participants.append({
                        "role": cells[0].inner_text().strip(),
                        "name": cells[1].inner_text().strip(),
                        "inn": "",
                        "address": cells[2].inner_text().strip() if len(cells) > 2 else "",
                    })
        except Exception as exc:
            logger.debug("Participant parse error: %s", exc)
        return participants

    def _parse_events(self, page) -> list[dict]:
        events = []
        today = datetime.utcnow().strftime("%Y-%m-%d")
        try:
            rows = page.locator(
                ".b-chrono-item, .b-events tr, .events-table tr, .b-chrono tr"
            ).all()
            for row in rows:
                try:
                    text = row.inner_text()
                    date_str = parse_date_ru(text)
                    if not date_str:
                        continue
                    is_future = 1 if date_str > today else 0
                    doc_url = ""
                    try:
                        a = row.locator("a").first
                        doc_url = a.get_attribute("href") or ""
                        if doc_url and not doc_url.startswith("http"):
                            doc_url = KAD_BASE + doc_url
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
