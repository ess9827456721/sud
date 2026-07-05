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
    def __init__(self, headless: bool = True):
        self._browser = None
        self._playwright = None
        self._headless = headless
        # Human-readable explanation of the last failure — callers put it
        # into sync_log / flash messages.
        self.last_error: Optional[str] = None
        # Name of the strategy that produced the last result (replay-capture
        # / ui-commit / legacy-fetch) — logged to sync_log by callers.
        self.last_strategy: Optional[str] = None

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
        self._browser = self._playwright.chromium.launch(headless=self._headless)

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
    # Human-like typing (KAD's Backbone model only reacts to key events —
    # locator.fill() leaves the model empty and the search never runs)
    # ------------------------------------------------------------------

    def _type_into(self, page, locator, text: str) -> bool:
        locator.click()
        locator.press("Control+a")
        locator.press("Delete")
        page.keyboard.type(text, delay=90)  # real keydown/keypress/input events
        random_delay(0.4, 0.9)
        try:
            if locator.input_value() != text:
                logger.error("KAD: typed text did not stick in the field")
                self._debug_dump(page, "type_failed")
                return False
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------
    # Human-readable failure messages (surfaced to sync_log by callers)
    # ------------------------------------------------------------------

    def _log_human(self, status: int) -> None:
        if status == 451:
            self.last_error = (
                "КАД временно ограничил автоматические запросы с этого IP. "
                "Повторите через 15–30 минут; данные дел сохранены."
            )
        else:
            self.last_error = (
                f"КАД вернул HTTP {status}. Скриншот и HTML сохранены в debug/."
            )
        logger.warning(self.last_error)

    # ------------------------------------------------------------------
    # Search: drive the real UI and intercept the site's own XHR.
    # KAD's anti-bot layer answers 451 to hand-crafted fetch() calls
    # (no fingerprint headers), so the page must issue the request itself.
    # ------------------------------------------------------------------

    # Confirmed in the live dump: the submit button lives in #b-form-submit
    _SUBMIT_SELECTORS = ('#b-form-submit button',
                         'button[type="submit"]:has-text("Найти")')

    def _click_submit(self, page) -> None:
        last_exc = None
        for sel in self._SUBMIT_SELECTORS:
            try:
                btn = page.locator(sel).first
                if btn.count() and btn.is_visible(timeout=1500):
                    btn.click()
                    return
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"submit click failed: {last_exc or 'button not found'}")

    def _chip_committed(self, page, container_sel: str, text: str) -> bool:
        """
        KAD's filter fields (#sug-cases / #sug-participants) are tag inputs:
        a committed value becomes a `.tag` CHIP whose text shows the value.
        The input/textarea sits in its own empty `.tag`, so a chip carrying
        the typed text (spaces ignored) is the reliable commit signal.
        """
        try:
            return bool(page.evaluate(
                """(args) => {
                    const [sel, txt] = args;
                    const norm = s => (s || '').replace(/\\s+/g, '');
                    const want = norm(txt);
                    const cont = document.querySelector(sel);
                    if (!cont) return false;
                    for (const tag of cont.querySelectorAll('.tag')) {
                        if (norm(tag.textContent).includes(want) && want) return true;
                    }
                    return false;
                }""", [container_sel, text]))
        except Exception:
            return False

    def _wait_committed(self, page, container_sel: str, text: str,
                        tries: int = 15) -> bool:
        for _ in range(tries):
            if self._chip_committed(page, container_sel, text):
                return True
            page.wait_for_timeout(200)
        return False

    def _commit_input(self, page, container_sel: str, field, text: str) -> bool:
        """
        Commit the typed value into a KAD tag-input. The canonical action is
        clicking the field's «+» add button (i.b-icon.add); Enter and a raw
        mouse click on «+» are fallbacks. Success = a `.tag` chip with the
        value appears in the container.
        """
        add_sel = f"{container_sel} i.b-icon.add"

        # If a suggest dropdown appeared, giving it a moment helps «+» pick
        # up the highlighted value; it is optional, so never block on it.
        try:
            page.locator("#b-suggest").wait_for(state="visible", timeout=2500)
        except Exception:
            pass

        # 1. Click the «+» add button
        try:
            plus = page.locator(add_sel).first
            if plus.count() and plus.is_visible(timeout=1500):
                plus.click()
        except Exception as exc:
            logger.debug("KAD: «+» click failed: %s", exc)
        if self._wait_committed(page, container_sel, text, tries=10):
            return True

        # 2. Enter on the field
        try:
            field.press("Enter")
        except Exception as exc:
            logger.debug("KAD: Enter failed: %s", exc)
        if self._wait_committed(page, container_sel, text, tries=10):
            return True

        # 3. Raw mouse click on «+» at its coordinates
        try:
            plus = page.locator(add_sel).first
            box = plus.bounding_box() if plus.count() else None
            if box:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                page.mouse.move(cx, cy)
                page.wait_for_timeout(120)
                page.mouse.down()
                page.wait_for_timeout(80)
                page.mouse.up()
        except Exception as exc:
            logger.debug("KAD: raw «+» click failed: %s", exc)
        if self._wait_committed(page, container_sel, text, tries=10):
            return True

        self._debug_dump(page, "commit_failed")
        logger.error("KAD: value was not committed into %s (no tag chip)", container_sel)
        self.last_error = (
            "КАД не принял введённое значение в поле фильтра. "
            "Скриншот и HTML сохранены в debug/."
        )
        return False

    def _run_search(self, page, fill_fn) -> Optional[dict]:
        """Type via fill_fn, click «Найти», return the intercepted JSON."""
        self._dismiss_overlays(page)
        if not fill_fn():
            return None
        self._dismiss_overlays(page)
        class _ClickFailed(Exception):
            pass

        try:
            try:
                with page.expect_response(
                        lambda r: "SearchInstances" in r.url,
                        timeout=30_000) as resp_info:
                    try:
                        self._click_submit(page)
                    except Exception as exc:
                        raise _ClickFailed(str(exc)) from exc
                resp = resp_info.value
            except _ClickFailed as exc:
                # Distinguish a click failure from a missing XHR
                self._debug_dump(page, "click_failed")
                self.last_error = (
                    f"КАД: не удалось нажать кнопку «Найти» ({str(exc)[:150]}). "
                    "Скриншот и HTML сохранены в debug/."
                )
                return None

            if resp.status == 451:
                logger.warning("KAD rate-limited (451) on native request; retry in 45s")
                page.wait_for_timeout(45_000)
                with page.expect_response(
                        lambda r: "SearchInstances" in r.url,
                        timeout=30_000) as resp_info2:
                    self._click_submit(page)
                resp = resp_info2.value

            if resp.status != 200:
                self._debug_dump(page, f"http_{resp.status}")
                self._log_human(resp.status)
                return None
            self.last_strategy = "ui-commit"
            return resp.json()
        except Exception as exc:
            # No XHR fired (or timeout) — regression marker for the fill() bug
            logger.warning("KAD: no SearchInstances XHR intercepted: %s", exc)
            try:
                if page.locator(".b-case-blank__emptyText").first.is_visible(timeout=1000):
                    self._debug_dump(page, "search_not_run")
                    self.last_error = (
                        "КАД не выполнил поиск — возможно, изменился интерфейс. "
                        "Скриншот и HTML сохранены в debug/."
                    )
                    return None
            except Exception:
                pass
            self._debug_dump(page, "no_xhr")
            self.last_error = (
                "КАД не выполнил поиск — возможно, изменился интерфейс. "
                "Скриншот и HTML сохранены в debug/."
            )
            return None

    # ------------------------------------------------------------------
    # Replay of a captured reference request (kad-doctor) — primary API path
    # ------------------------------------------------------------------

    @staticmethod
    def _capture_path():
        from court_tracker.config import DEBUG_DIR
        return DEBUG_DIR / "kad_capture.json"

    def _load_capture(self) -> Optional[dict]:
        import json
        p = self._capture_path()
        if not p.exists():
            return None
        try:
            cap = json.loads(p.read_text(encoding="utf-8"))
            if cap.get("request") and cap["request"].get("url"):
                return cap
        except Exception as exc:
            logger.warning("KAD: cannot read capture file: %s", exc)
        return None

    def _replay_search(self, page, substitute: dict) -> tuple:
        """
        Replay the captured SearchInstances request with the exact headers
        KAD's own JS attached. `substitute` overrides fields in the captured
        post_data (e.g. {"CaseNumbers": [...]} / {"Sides": [...]}).
        Returns (status, data): status in {"ok", "rate_limited", "fail"}.
        """
        import json
        cap = self._load_capture()
        if not cap:
            return ("fail", None)
        req = cap["request"]
        url = req.get("url")
        method = req.get("method", "POST")
        headers = {k: v for k, v in (req.get("headers") or {}).items()
                   if k.lower() not in ("cookie", "host", "content-length")}
        try:
            body_obj = json.loads(req.get("post_data") or "{}")
        except Exception:
            body_obj = {}
        body_obj.update(substitute)

        payload = {"url": url, "method": method,
                   "headers": headers, "body": json.dumps(body_obj)}
        script = """async (p) => {
            const r = await fetch(p.url, {
                method: p.method,
                headers: p.headers,
                body: p.body,
                credentials: 'include'
            });
            if (!r.ok) return {__status: r.status};
            return {__status: 200, data: await r.json()};
        }"""
        try:
            res = page.evaluate(script, payload)
        except Exception as exc:
            logger.warning("KAD replay failed: %s", exc)
            return ("fail", None)
        status = res.get("__status") if isinstance(res, dict) else None
        if status in (451, 403):
            return ("rate_limited", None)
        if status != 200:
            return ("fail", None)
        return ("ok", res.get("data"))

    def _try_replay(self, page, substitute: dict) -> Optional[list]:
        """Run the replay path; return mapped cases, or None to fall through."""
        if not self._load_capture():
            return None
        status, data = self._replay_search(page, substitute)
        if status == "ok":
            cases = [c for c in (self._item_to_case(i)
                                 for i in self._api_items(data)) if c]
            self.last_strategy = "replay-capture"
            return cases  # possibly empty — an authoritative empty result
        if status == "rate_limited":
            logger.warning("KAD replay rate-limited (451/403); fingerprint may have rotated")
            self.last_error = (
                "КАД ограничил запросы — отпечаток мог смениться. "
                "Запустите 'python main.py kad-doctor' для повторного захвата."
            )
        return None  # fall through to UI path

    # ------------------------------------------------------------------
    # Diagnostic capture (kad-doctor)
    # ------------------------------------------------------------------

    def capture_reference(self, case_number: str = "А60-33087/2025") -> Optional[str]:
        """
        One-shot ground-truth capture. Opens KAD (non-headless), lets the
        user run one manual search, and records the SearchInstances request
        (method/url/headers/post_data), the response (status + first 2000
        chars), cookies, and #b-form before/after snapshots into
        debug/kad_capture.json.
        """
        import json
        import time
        from court_tracker.config import DEBUG_DIR

        page = self._new_page()
        captured = {"request": None, "response": None, "cookies": None,
                    "b_form_before": None, "b_form_after": None}

        def _on_request(req):
            if captured["request"] is None and "SearchInstances" in req.url:
                try:
                    captured["request"] = {
                        "method": req.method,
                        "url": req.url,
                        "headers": dict(req.headers),
                        "post_data": req.post_data,
                    }
                    logger.info("kad-doctor: captured request to %s", req.url)
                except Exception as exc:
                    logger.warning("capture request failed: %s", exc)

        def _on_response(resp):
            if captured["response"] is None and "SearchInstances" in resp.url:
                try:
                    body = resp.text()
                    captured["response"] = {"status": resp.status, "body": body[:2000]}
                    logger.info("kad-doctor: captured response HTTP %s", resp.status)
                except Exception as exc:
                    logger.warning("capture response failed: %s", exc)

        page.on("request", _on_request)
        page.on("response", _on_response)

        self._open_main_page(page)
        try:
            captured["b_form_before"] = page.locator("#b-form").inner_html()
        except Exception:
            pass

        print("=" * 64)
        print("kad-doctor: Выполните поиск вручную в открывшемся окне:")
        print(f"  введите номер дела {case_number}, выберите подсказку,")
        print("  нажмите «Найти». Окно закроется само после захвата.")
        print("=" * 64)

        deadline = time.time() + 300  # wait up to 5 min for the user's search
        while time.time() < deadline and captured["response"] is None:
            page.wait_for_timeout(500)

        if captured["response"] is not None:
            try:
                captured["b_form_after"] = page.locator("#b-form").inner_html()
            except Exception:
                pass
            try:
                captured["cookies"] = page.context.cookies()
            except Exception:
                pass
            page.wait_for_timeout(5000)  # let the user see the result
        else:
            print("kad-doctor: запрос SearchInstances не был перехвачен за 5 минут.")

        out = DEBUG_DIR / "kad_capture.json"
        try:
            out.write_text(json.dumps(captured, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            print(f"kad-doctor: захват сохранён в {out}")
        except Exception as exc:
            logger.error("kad-doctor: cannot write capture: %s", exc)
            return None
        return str(out)

    # ------------------------------------------------------------------
    # Hand-crafted fetch — LAST resort only (currently earns HTTP 451)
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
        self.last_error = None
        self.last_strategy = None
        page = None
        try:
            page = self._new_page()
            self._open_main_page(page)

            # ── 0. Replay from captured reference (kad-doctor) ────────────
            replay = self._try_replay(page, {"CaseNumbers": [case_number], "Sides": []})
            if replay is not None:
                if replay:
                    return replay[0]
                # Authoritative empty result from the trusted request
                self.last_error = (
                    f"КАД не нашёл дело {case_number}. Проверьте номер: "
                    "буква А — кириллическая."
                )
                return None

            # ── 1. UI + interception of the site's own XHR ────────────────
            def _fill_case():
                field = self._find_case_number_input(page)
                if field is None:
                    logger.error("KAD UI: case number input not found")
                    self._debug_dump(page, "no_case_input")
                    return False
                if not self._type_into(page, field, case_number):
                    return False
                # Typed text must be committed into a filter tag chip
                # (via the «+» button) before «Найти» will search.
                return self._commit_input(page, "#sug-cases", field, case_number)

            data = self._run_search(page, _fill_case)
            cases = [c for c in (self._item_to_case(i) for i in self._api_items(data)) if c]
            if cases:
                return cases[0]
            if data is not None:
                # Search ran but found nothing
                self.last_error = (
                    f"КАД не нашёл дело {case_number}. Проверьте номер: "
                    "буква А — кириллическая."
                )
                return None

            # ── 2. DOM parse of the results page ─────────────────────────
            try:
                page.wait_for_load_state("networkidle", timeout=TIMEOUT)
            except Exception:
                pass
            results = self._parse_search_results(page)
            if results:
                self.last_strategy = "dom-parse"
                return results[0]

            # ── 3. Last resort: hand-crafted fetch (currently 451) ───────
            payload = {
                "Page": 1, "Count": 25, "Courts": [],
                "DateFrom": None, "DateTo": None,
                "Sides": [], "Judges": [],
                "CaseNumbers": [case_number],
                "WithVKSInstances": False,
            }
            data = self._api_search(page, payload)
            cases = [c for c in (self._item_to_case(i) for i in self._api_items(data)) if c]
            if cases:
                self.last_strategy = "legacy-fetch"
                return cases[0]

            self._debug_dump(page, "search_empty")
            return None
        except Exception as exc:
            logger.error("search_by_case_number(%s) failed: %s", case_number, exc)
            if page:
                self._debug_dump(page, "search_error")
            return None

    def search_by_inn(self, inn: str) -> list[dict]:
        """Search KAD for all cases involving a party with the given INN."""
        self.last_error = None
        self.last_strategy = None
        page = None
        try:
            page = self._new_page()
            self._open_main_page(page)

            # ── 0. Replay from captured reference (kad-doctor) ────────────
            side = {"Name": inn, "Type": -1, "ExactMatch": False}
            replay = self._try_replay(page, {"Sides": [side], "CaseNumbers": []})
            if replay is not None:
                if replay:
                    return replay
                self.last_error = f"КАД не нашёл дел по ИНН {inn}."
                return []

            # ── 1. UI + interception ──────────────────────────────────────
            def _fill_inn():
                field = self._find_inn_input(page)
                if field is None:
                    logger.error("KAD UI: participant/INN input not found")
                    self._debug_dump(page, "no_inn_input")
                    return False
                if not self._type_into(page, field, inn):
                    return False
                # Commit the participant value into a filter tag chip via
                # the «+» button. Escape is NEVER pressed (it cancels input).
                return self._commit_input(page, "#sug-participants", field, inn)

            data = self._run_search(page, _fill_inn)
            cases = [c for c in (self._item_to_case(i) for i in self._api_items(data)) if c]
            if cases:
                return cases
            if data is not None:
                self.last_error = f"КАД не нашёл дел по ИНН {inn}."
                return []

            # ── 2. DOM parse ──────────────────────────────────────────────
            try:
                page.wait_for_load_state("networkidle", timeout=TIMEOUT)
            except Exception:
                pass
            results = self._parse_search_results(page)
            if results:
                self.last_strategy = "dom-parse"
                return results

            # ── 3. Last resort: hand-crafted fetch ────────────────────────
            payload = {
                "Page": 1, "Count": 25, "Courts": [],
                "DateFrom": None, "DateTo": None,
                "Sides": [{"Name": inn, "Type": -1, "ExactMatch": False}],
                "Judges": [], "CaseNumbers": [],
                "WithVKSInstances": False,
            }
            data = self._api_search(page, payload)
            cases = [c for c in (self._item_to_case(i) for i in self._api_items(data)) if c]
            if cases:
                self.last_strategy = "legacy-fetch"
                return cases

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
        # Live DOM (verified): #sug-cases .tag > input, placeholder
        # «например, А50-5568/08», empty id
        for sel in ("#sug-cases input",
                    "#sug-cases .tag input"):
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible(timeout=2000):
                    return el
            except Exception:
                pass
        try:
            el = page.get_by_placeholder(re.compile("А50-5568")).first
            if el.is_visible(timeout=2000):
                return el
        except Exception:
            pass
        return None

    def _find_inn_input(self, page):
        # Live DOM (verified): #sug-participants .tag > textarea,
        # placeholder «название, ИНН или ОГРН»
        for sel in ("#sug-participants textarea",
                    "#sug-participants .tag textarea"):
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible(timeout=2000):
                    return el
            except Exception:
                pass
        try:
            el = page.get_by_placeholder("название, ИНН или ОГРН").first
            if el.is_visible(timeout=2000):
                return el
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Card page (details)
    # ------------------------------------------------------------------

    def get_case_details(self, kad_url: str) -> Optional[dict]:
        """
        Fetch full case details from a KAD Card page.
        The card is a JS app that loads its data via /Kad/ XHRs — collect
        those JSON payloads during load and prefer them over DOM scraping.
        """
        page = None
        try:
            page = self._new_page()

            collected: list[dict] = []

            def _on_response(resp):
                try:
                    if "/Kad/" not in resp.url:
                        return
                    ctype = (resp.headers or {}).get("content-type", "")
                    if "json" not in ctype:
                        return
                    body = resp.json()
                    if isinstance(body, dict):
                        collected.append({"url": resp.url, "json": body})
                except Exception:
                    pass

            page.on("response", _on_response)

            page.goto(kad_url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=TIMEOUT)
            except Exception:
                pass
            random_delay(1, 2)
            self._dismiss_overlays(page)

            case_data = self._parse_case_page(page, kad_url)

            # Prefer intercepted JSON payloads over DOM scraping
            api_data = self._extract_from_card_payloads(collected)
            for key in ("case_number", "court", "judge", "status",
                        "case_type", "start_date"):
                if api_data.get(key):
                    case_data[key] = api_data[key]
            if api_data.get("participants"):
                case_data["participants"] = api_data["participants"]
            if api_data.get("events"):
                case_data["events"] = api_data["events"]

            if not case_data.get("case_number") and not case_data.get("events"):
                self._debug_dump(page, "card_empty")
            return case_data
        except Exception as exc:
            logger.error("get_case_details(%s) failed: %s", kad_url, exc)
            if page:
                self._debug_dump(page, "card_error")
            return None

    def _extract_from_card_payloads(self, collected: list[dict]) -> dict:
        """
        Inspect JSON payloads intercepted on the Card page (/Kad/Card,
        /Kad/CaseDocumentsPage, …) and pull out whatever case data they
        carry. Shapes are handled defensively — DOM scraping remains the
        fallback for anything not found here.
        """
        out: dict = {"participants": [], "events": []}
        today = datetime.utcnow().strftime("%Y-%m-%d")

        def _walk(node, depth=0):
            if depth > 6:
                return
            if isinstance(node, dict):
                # Case header info
                num = node.get("CaseNumber") or node.get("CaseNo")
                if num and not out.get("case_number"):
                    out["case_number"] = normalize_case_number(str(num))
                court = node.get("CourtName") or node.get("Court")
                if isinstance(court, str) and court and not out.get("court"):
                    out["court"] = court[:300]
                judge = node.get("Judge") or node.get("JudgeName")
                if isinstance(judge, str) and judge and not out.get("judge"):
                    out["judge"] = judge[:300]

                # Participants
                for key, role in (("Plaintiffs", "Истец"),
                                  ("Respondents", "Ответчик"),
                                  ("Thirds", "Третье лицо")):
                    lst = node.get(key)
                    if isinstance(lst, list):
                        for p in lst:
                            if isinstance(p, dict) and p.get("Name"):
                                out["participants"].append({
                                    "role": role,
                                    "name": str(p["Name"])[:300],
                                    "inn": str(p.get("Inn") or ""),
                                    "address": str(p.get("Address") or "")[:300],
                                })

                # Documents / events
                for key in ("Items", "Documents"):
                    lst = node.get(key)
                    if isinstance(lst, list):
                        for d in lst:
                            if not isinstance(d, dict):
                                continue
                            date_raw = (d.get("Date") or d.get("EventDate")
                                        or d.get("PublishDate") or "")
                            m = re.search(r"\d{4}-\d{2}-\d{2}", str(date_raw))
                            if not m:
                                continue
                            desc = (d.get("ContentTypeName") or d.get("Type")
                                    or d.get("DocumentTypeName") or "")
                            extra = d.get("Declarers") or d.get("Comment") or ""
                            text = " ".join(str(x) for x in (desc, extra) if x).strip()
                            if not text:
                                continue
                            out["events"].append({
                                "event_date": m.group(0),
                                "event_type": "hearing" if "заседан" in text.lower() else "other",
                                "description": text[:500],
                                "document_url": d.get("FileUrl") or "",
                                "is_future": 1 if m.group(0) > today else 0,
                            })
                for v in node.values():
                    _walk(v, depth + 1)
            elif isinstance(node, list):
                for v in node:
                    _walk(v, depth + 1)

        for entry in collected:
            logger.debug("KAD card payload from %s: keys=%s",
                         entry["url"], list(entry["json"].keys()))
            _walk(entry["json"])

        # De-duplicate
        seen = set()
        out["participants"] = [
            p for p in out["participants"]
            if not ((p["role"], p["name"].lower()) in seen
                    or seen.add((p["role"], p["name"].lower())))
        ]
        seen_ev = set()
        out["events"] = [
            e for e in out["events"]
            if not ((e["event_date"], e["description"][:80]) in seen_ev
                    or seen_ev.add((e["event_date"], e["description"][:80])))
        ]
        return out

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
