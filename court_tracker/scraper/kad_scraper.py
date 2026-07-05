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

    # kad.arbitr.ru sits behind DDoS-Guard, which runs a WASM fingerprint
    # (window.chrome / navigator.plugins / webdriver / canvas / WebGL). A
    # plain automated Chromium fails it and its own JS then refuses to send
    # /Kad/SearchInstances. These anti-detection measures make the browser
    # look like an ordinary one so the fingerprint passes.
    _STEALTH_JS = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = window.chrome || { runtime: {}, app: {}, csi: function(){},
            loadTimes: function(){} };
        Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU','ru','en-US','en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        const _q = navigator.permissions && navigator.permissions.query;
        if (_q) navigator.permissions.query = (p) => (
            p && p.name === 'notifications'
              ? Promise.resolve({state: Notification.permission})
              : _q(p));
    """

    _LAUNCH_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]

    def _start(self):
        import os
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        # SUD_KAD_HEADFUL=1 forces a visible window — DDoS-Guard's WASM
        # fingerprint is more likely to pass with a real (non-headless) UI.
        headful = os.environ.get("SUD_KAD_HEADFUL") == "1"
        headless = self._headless and not headful
        self._browser = self._playwright.chromium.launch(
            headless=headless,
            args=self._LAUNCH_ARGS,
        )

    def _stop(self):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def _new_page(self):
        context = self._browser.new_context(
            user_agent=USER_AGENT,
            viewport=VIEWPORT,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )
        try:
            context.add_init_script(self._STEALTH_JS)
        except Exception:
            pass
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

    def _trigger_search(self, page) -> str:
        """
        Start the search the way a human does:
          - if the autocomplete suggest is showing, CLICK its item — in KAD
            that runs the search immediately (no «Найти» needed);
          - otherwise (e.g. a full INN shows no suggest) click «Найти».
        The «+»/«−» icons are NOT used — they only add a second search row.
        Returns 'suggest' or 'submit' for logging.
        """
        try:
            page.locator("#b-suggest").wait_for(state="visible", timeout=3500)
            for sel in ("#b-suggest .body__i li.active a",
                        "#b-suggest .body__i li a",
                        "#b-suggest .body__i li",
                        "#b-suggest li a", "#b-suggest li"):
                item = page.locator(sel).first
                if item.count() and item.is_visible(timeout=800):
                    item.click()
                    return "suggest"
        except Exception:
            pass
        self._click_submit(page)
        return "submit"

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
                    try:
                        btn.click()
                    except Exception:
                        btn.click(force=True)
                    return
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"submit click failed: {last_exc or 'button not found'}")

    # POST /Kad/SearchInstances returns an HTML results table (NOT JSON).
    # Each result row carries an anchor to the case card plus judge/court.
    _RESULT_CARD_RE = re.compile(
        r'href="(https?://kad\.arbitr\.ru/Card/([0-9a-fA-F\-]+))"[^>]*'
        r'class="num_case"[^>]*>\s*([^<]+?)\s*<',
        re.S,
    )

    def _parse_search_html(self, html: str) -> list[dict]:
        """Parse the HTML results table returned by /Kad/SearchInstances."""
        results: list[dict] = []
        if not html:
            return results
        for block in re.split(r'<tr[\s>]', html):
            m = self._RESULT_CARD_RE.search(block)
            if not m:
                continue
            kad_url, guid, num = m.group(1), m.group(2), m.group(3)
            case_number = normalize_case_number(re.sub(r"\s+", "", num))

            mj = re.search(r'class="judge"[^>]*title="([^"]*)"', block)
            judge = mj.group(1).strip() if mj else None

            # Court = the first <div title="…"> WITHOUT a class (judge/date
            # divs carry a class); skip anything that looks like a date.
            court = None
            for mc in re.finditer(r'<div\s+title="([^"]+)"\s*>', block):
                val = mc.group(1).strip()
                if not re.match(r"\d{2}\.\d{2}\.\d{4}", val):
                    court = val
                    break

            start_date = None
            md = re.search(r"<span>(\d{2})\.(\d{2})\.(\d{4})</span>", block)
            if md:
                start_date = f"{md.group(3)}-{md.group(2)}-{md.group(1)}"

            results.append({
                "case_number": case_number,
                "case_id_kad": guid,
                "kad_url": kad_url,
                "court": court,
                "judge": judge,
                "start_date": start_date,
                "source": "kad",
            })
        return results

    def _run_search(self, page, fill_fn) -> Optional[list[dict]]:
        """
        Drive the search like a human: fill_fn TYPES the value into the field
        with real keyboard events (which — unlike fill() — register in KAD's
        Backbone model), then _trigger_search either CLICKS the autocomplete
        suggestion (that runs the search directly) or, when no suggest appears
        (a full INN), clicks «Найти». /Kad/SearchInstances returns an HTML
        results table which we parse.

        Returns: list of case dicts (possibly empty = search ran, 0 results),
        or None when the search request could not be observed.
        """
        self._dismiss_overlays(page)
        if not fill_fn():
            return None
        self._dismiss_overlays(page)

        try:
            with page.expect_response(
                    lambda r: "SearchInstances" in r.url,
                    timeout=30_000) as resp_info:
                self._trigger_search(page)
            resp = resp_info.value

            if resp.status == 451:
                logger.warning("KAD rate-limited (451) on native request; retry in 45s")
                page.wait_for_timeout(45_000)
                with page.expect_response(
                        lambda r: "SearchInstances" in r.url,
                        timeout=30_000) as resp_info2:
                    self._trigger_search(page)
                resp = resp_info2.value

            if resp.status != 200:
                self._debug_dump(page, f"http_{resp.status}")
                self._log_human(resp.status)
                return None

            html = resp.text()
            self.last_strategy = "ui-search"
            return self._parse_search_html(html)
        except Exception as exc:
            logger.warning("KAD: SearchInstances not observed: %s", exc)
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
            return {__status: 200, html: await r.text()};
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
        return ("ok", res.get("html"))

    def _try_replay(self, page, substitute: dict) -> Optional[list]:
        """Run the replay path; return parsed cases, or None to fall through."""
        if not self._load_capture():
            return None
        status, html = self._replay_search(page, substitute)
        if status == "ok":
            cases = self._parse_search_html(html)
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

    @staticmethod
    def _best_case_match(cases: list[dict], target: str) -> dict:
        """Pick the row whose case number matches the target, else the first."""
        tnorm = normalize_case_number(target)
        for c in cases:
            if c.get("case_number") == tnorm:
                return c
        return cases[0]

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
                # Typing with real keyboard events registers the value; the
                # search is then triggered by clicking the suggestion (or
                # «Найти») in _trigger_search.
                return self._type_into(page, field, case_number)

            cases = self._run_search(page, _fill_case)
            if cases:
                return self._best_case_match(cases, case_number)
            if cases is not None:
                # Search ran but found nothing
                self.last_error = (
                    f"КАД не нашёл дело {case_number}. Проверьте номер: "
                    "буква А — кириллическая."
                )
                return None

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
                # A full INN shows no suggest, so _trigger_search will click
                # «Найти»; a partial match with a suggest is clicked instead.
                return self._type_into(page, field, inn)

            cases = self._run_search(page, _fill_inn)
            if cases:
                return cases
            if cases is not None:
                self.last_error = f"КАД не нашёл дел по ИНН {inn}."
                return []

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
        # «Номер дела» field: editable (non-disabled) input in #sug-cases,
        # placeholder «например, А50-5568/08». :not([disabled]) skips the
        # disabled input inside an already-committed .tag.added chip.
        for sel in ("#sug-cases input:not([disabled])",
                    "input[placeholder*='5568']:not([disabled])"):
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible(timeout=2000):
                    return el
            except Exception:
                pass
        return None

    def _find_inn_input(self, page):
        # Top «Участник дела» field — «название, ИНН или ОГРН». The client
        # INN search MUST land here (not in «Номер дела»/«Судья»). It is the
        # editable (non-disabled) TEXTAREA in #sug-participants; the disabled
        # textarea inside a committed chip is excluded via :not([disabled]).
        for sel in ("#sug-participants textarea:not([disabled])",
                    "textarea[placeholder*='ОГРН']:not([disabled])",
                    "textarea[placeholder*='ИНН']:not([disabled])"):
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible(timeout=2000):
                    return el
            except Exception:
                pass
        # Placeholder anchor as a last resort
        try:
            el = page.get_by_placeholder(re.compile("ИНН|ОГРН")).first
            if el.count() and el.is_visible(timeout=2000):
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
