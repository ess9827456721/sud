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

    def _debug_dump(self, page, tag: str) -> str:
        """Save screenshot + HTML to the debug/ folder; return message part."""
        try:
            from court_tracker.config import DEBUG_DIR
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            png = DEBUG_DIR / f"soy_{tag}_{ts}.png"
            html = DEBUG_DIR / f"soy_{tag}_{ts}.html"
            try:
                page.screenshot(path=str(png), full_page=True)
            except Exception:
                pass
            try:
                html.write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            logger.info("SOY debug artifacts: %s, %s", png, html)
            return f" [debug: {png.name}]"
        except Exception as exc:
            logger.debug("debug dump failed: %s", exc)
            return ""

    def scrape_case(self, url: str) -> dict:
        """
        Full 3-tab parse of a sudrf.ru case card.
        Step 1: case info (tab «Дело»)
        Step 2: events (tab «Движение дела», with AJAX navigation)
        Step 3: parties (tab «Стороны», with 3 strategies)
        """
        page = None
        try:
            self._launch()
            page = self.context.new_page()
            page.set_default_timeout(30_000)

            page.goto(url, wait_until="domcontentloaded")
            time.sleep(random.uniform(1.5, 3.0))

            if self._has_captcha(page):
                return _fail("captcha",
                             "Обнаружена CAPTCHA на сайте суда" + self._debug_dump(page, "captcha"))

            if not self._page_has_content(page):
                return _fail("failed",
                             "Страница не загрузилась или пустая" + self._debug_dump(page, "empty"))

            # ── Step 1: case info ─────────────────────────────────────────────
            case_info = self._extract_case_info_full(page)

            # ── Step 2: events ────────────────────────────────────────────────
            self._navigate_to_events_tab(page, url)
            events = self._try_all_variants(page)

            if not events:
                return _fail("no_data",
                             "Таблица движения дела не найдена (шаблон не распознан)"
                             + self._debug_dump(page, "no_data"),
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
            dbg = self._debug_dump(page, "exception") if page else ""
            if "Timeout" in err_type:
                return _fail("failed", "Таймаут: сайт суда не ответил за 30 секунд" + dbg)
            return _fail("failed", f"Ошибка парсинга: {str(exc)[:200]}" + dbg)
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

    # ── Court name (page header, not the «Дело» table) ────────────────────────

    _COURT_RE = re.compile(r'([А-ЯЁ][^|—]*?(?:суд|судебный участок)[^|—]*)')

    @staticmethod
    def _clean_court_name(raw: str) -> str:
        s = " ".join(raw.split())
        # Strip navigation / boilerplate words around the name
        for junk in ("Главная", "Официальный сайт", "ГАС «Правосудие»",
                     "ГАС \"Правосудие\"", "::", "—", "–"):
            s = s.replace(junk, " ")
        s = " ".join(s.split()).strip(" -–—|·,")
        return s[:150]

    def _extract_court_name(self, page) -> str:
        # 1. Document <title> — sudrf card titles contain the court name
        try:
            title = page.title() or ""
            m = self._COURT_RE.search(title)
            if m:
                name = self._clean_court_name(m.group(1))
                if len(name) > 5:
                    return name
        except Exception:
            pass

        # 2. Header elements — first text containing «суд»
        for sel in ("#header", ".heading", ".header", "td.heading", "a.heading"):
            try:
                for el in page.locator(sel).all()[:5]:
                    text = el.inner_text().strip()
                    m = self._COURT_RE.search(text)
                    if m:
                        name = self._clean_court_name(m.group(1))
                        if len(name) > 5:
                            return name
            except Exception:
                pass

        # 3. Fallback: raw host from the URL — the field is never empty
        try:
            m = re.match(r'https?://([^/]+)', page.url or "")
            if m:
                return m.group(1)[:150]
        except Exception:
            pass
        return ""

    # ── Tab 1: Full case info ─────────────────────────────────────────────────

    def _extract_case_info_full(self, page) -> dict:
        """
        Extract all fields from the «Дело» tab:
        case_number, judge, category, category_path, receipt_date,
        decision_date, decision_result, court_first, uid
        """
        info: dict = {}

        # Court name lives in the PAGE HEADER (or <title>), not in the
        # «Дело» table. Priority: <title> → header elements → URL host.
        info["court"] = self._extract_court_name(page)

        try:
            text = page.content()

            # Case number (pattern: NN-NNNNN/NNNN)
            m = re.search(r'\d+-\d+/\d{4}', text)
            if m:
                info["case_number"] = m.group(0)

            # UID like «51RS0018-01-2026-000467-24» — letters and dashes
            uid_m = re.search(r'УИД[:\s]+([\w\-]{15,40})', text)
            if uid_m:
                info["uid"] = uid_m.group(1).strip()
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

    @staticmethod
    def _build_parties_col_map(header_texts: list[str]) -> dict:
        """
        Real sudrf.ru parties header: «Вид лица, участвующего в деле |
        Фамилия / наименование | ИНН | КПП | ОГРН». КПП/ОГРН are recognised
        and ignored — КПП must never be read as 'representative'.
        """
        col_map: dict = {}
        for idx, raw in enumerate(header_texts):
            t = raw.lower().strip()
            if not t:
                continue
            if ("вид лица" in t or "лицо, участвующее" in t) and "role" not in col_map:
                col_map["role"] = idx
            elif ("фамилия" in t or "наименование" in t) and "name" not in col_map:
                col_map["name"] = idx
            elif "инн" in t and "inn" not in col_map:
                col_map["inn"] = idx
            elif "кпп" in t and "kpp" not in col_map:
                col_map["kpp"] = idx      # recognised, ignored
            elif "огрн" in t and "ogrn" not in col_map:
                col_map["ogrn"] = idx     # recognised, ignored
            elif "представител" in t and "representative" not in col_map:
                col_map["representative"] = idx
        return col_map

    def _parse_parties_page(self, page) -> list:
        """
        Parse the parties table using header-based column mapping.
        INN comes only from the ИНН column (checksum-validated) when the
        header has one; representative only from a «Представитель» column.
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
                if not rows:
                    continue

                header_cells = rows[0].locator("th").all() or rows[0].locator("td").all()
                header_texts = []
                for c in header_cells:
                    try:
                        header_texts.append(c.inner_text().strip())
                    except Exception:
                        header_texts.append("")
                col_map = self._build_parties_col_map(header_texts)
                has_header = bool(col_map)
                col_map.setdefault("role", 0)
                col_map.setdefault("name", 1)
                data_rows = rows[1:] if has_header else rows

                for row in data_rows:
                    cells = row.locator("td").all()
                    if len(cells) < 2:
                        continue

                    def _cell(key):
                        idx = col_map.get(key)
                        if idx is None or idx >= len(cells):
                            return ""
                        try:
                            return cells[idx].inner_text().strip()
                        except Exception:
                            return ""

                    role_text = _cell("role")
                    name_text = _cell("name")
                    if not role_text or not name_text:
                        continue
                    if not any(kw in role_text.lower() for kw in _ROLE_KEYWORDS):
                        continue

                    # INN: dedicated column first (checksum validated),
                    # otherwise search the name cell text
                    inn = None
                    if "inn" in col_map:
                        candidate = re.sub(r"\D", "", _cell("inn"))
                        if candidate and _validate_inn(candidate):
                            inn = candidate
                    if not inn:
                        inn = _extract_inn(name_text)

                    # Clean name: remove INN artefacts in parentheses
                    clean_name = re.sub(r'\s*\(\s*(?:ИНН[:\s]*)?\d{10,12}\s*\)', '',
                                        name_text).strip()

                    # Representative ONLY from an explicit column — never
                    # positional cells[3] (that is КПП on sudrf.ru)
                    representative = None
                    if "representative" in col_map:
                        rep = _cell("representative")
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

    @staticmethod
    def _classify_event(title: str) -> str:
        low = title.lower()
        if any(w in low for w in ["заседание", "слушание", "рассмотрение"]):
            return "hearing"
        if any(w in low for w in ["решение", "определение", "приговор", "постановление"]):
            return "decision"
        if any(w in low for w in ["приостановление", "возобновление"]):
            return "suspension"
        if any(w in low for w in ["поступление", "возбуждение", "принятие к производству"]):
            return "start"
        return "other"

    @staticmethod
    def _build_event_col_map(header_texts: list[str]) -> dict:
        """
        Map sudrf.ru movement-table headers to logical columns. Real order is
        «Наименование события | Дата | Время | Зал заседания | Результат
        события | Основание … | Примечание | Дата размещения».
        """
        col_map: dict = {}
        for idx, raw in enumerate(header_texts):
            t = raw.lower().strip()
            if not t:
                continue
            if ("наименование" in t or "событ" in t) and "title" not in col_map:
                col_map["title"] = idx
            elif t.startswith("дата") and "размещен" not in t and "date" not in col_map:
                col_map["date"] = idx
            elif "время" in t and "time" not in col_map:
                col_map["time"] = idx
            elif "зал" in t and "hall" not in col_map:
                col_map["hall"] = idx
            elif "результат" in t and "основание" not in t and "result" not in col_map:
                col_map["result"] = idx
            elif "примечание" in t and "note" not in col_map:
                col_map["note"] = idx
        return col_map

    def _parse_event_table(self, table) -> list:
        """
        Header-aware parsing of a movement table. Column positions come from
        the header row; if no header matches, fall back to a positional
        heuristic (first cell that parses as a date is the date column).
        """
        events: list = []
        today = date.today()

        rows = table.locator("tr").all()
        if not rows:
            return events

        # 1. Header row: th cells, or td of the first row
        header_cells = rows[0].locator("th").all()
        if not header_cells:
            header_cells = rows[0].locator("td").all()
        header_texts = []
        for c in header_cells:
            try:
                header_texts.append(c.inner_text().strip())
            except Exception:
                header_texts.append("")
        col_map = self._build_event_col_map(header_texts)
        data_rows = rows[1:] if col_map else rows

        # 2. Positional fallback: find the date column from the first data row
        if "date" not in col_map:
            for row in data_rows[:3]:
                cells = row.locator("td").all()
                for idx, c in enumerate(cells):
                    try:
                        if _parse_date_str(c.inner_text().strip()):
                            col_map["date"] = idx
                            break
                    except Exception:
                        pass
                if "date" in col_map:
                    break
            if "date" in col_map and "title" not in col_map:
                col_map["title"] = 0 if col_map["date"] != 0 else 1

        if "date" not in col_map:
            return events
        col_map.setdefault("title", 0)

        skipped = 0
        for row in data_rows:
            cells = row.locator("td").all()
            if len(cells) <= max(col_map["date"], col_map["title"]):
                continue

            def _cell(key):
                idx = col_map.get(key)
                if idx is None or idx >= len(cells):
                    return ""
                try:
                    return cells[idx].inner_text().strip()
                except Exception:
                    return ""

            title = _cell("title")
            date_text = _cell("date")
            if not title and not date_text:
                continue

            event_date_str = _parse_date_str(date_text) if date_text else None
            if not event_date_str:
                skipped += 1
                continue
            try:
                event_date = date.fromisoformat(event_date_str)
            except ValueError:
                skipped += 1
                continue

            time_text = _cell("time")
            m_time = re.search(r"\d{1,2}:\d{2}", time_text or "")
            event_time = m_time.group(0) if m_time else None

            result = _cell("result")
            hall = _cell("hall")
            description = title
            if result:
                description += " — " + result
            if hall:
                description += ", зал " + hall
            if event_time:
                description += f" ({event_time})"

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
                "event_time":   event_time,
                "event_type":   self._classify_event(title),
                "description":  description[:500],
                "document_url": doc_url,
                "is_future":    1 if event_date > today else 0,
            })

        if skipped:
            logger.info("SOY movement table: %d rows skipped (no parsable date)", skipped)
        return events

    def _variant_a_tablcont(self, page) -> list:
        # Several .tablcont tables may exist (case info, parties…) — pick the
        # one that actually is the movement table.
        for table in page.locator("table.tablcont, table#tablcont").all():
            try:
                text = table.inner_text().lower()
            except Exception:
                continue
            if "наименование события" in text or "движение" in text:
                events = self._parse_event_table(table)
                if events:
                    return events
        # Single-table fallback
        table = page.locator("table.tablcont, table#tablcont").first
        if table.count() == 0:
            return []
        return self._parse_event_table(table)

    def _table_of(self, page, row_selector: str):
        """Return the table containing the first row matched by selector."""
        row = page.locator(row_selector).first
        if row.count() == 0:
            return None
        table = row.locator("xpath=ancestor::table[1]")
        return table if table.count() > 0 else None

    def _variant_b_dev_ras(self, page) -> list:
        table = self._table_of(page, "tr.dev_ras")
        return self._parse_event_table(table) if table else []

    def _variant_c_odd_even(self, page) -> list:
        rows = page.locator("tr.odd, tr.even").all()
        if len(rows) < 2:
            return []
        table = self._table_of(page, "tr.odd, tr.even")
        return self._parse_event_table(table) if table else []

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
                text = table.inner_text()
                # Must look like a movement table — avoids parsing menus etc.
                low = text.lower()
                if "наименование события" not in low and "движение" not in low:
                    continue
                count = len(date_pattern.findall(text))
                if count > best_count:
                    best_count = count
                    best_table = table
        except Exception:
            return []
        if best_table is None or best_count < 2:
            return []
        return self._parse_event_table(best_table)


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
