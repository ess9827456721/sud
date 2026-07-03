"""
Background synchronisation scheduler.
Runs KAD + SOY scraping on a configurable interval in a daemon thread.
"""
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from court_tracker.config import DB_PATH, DATA_DIR

logger = logging.getLogger(__name__)


def _notify_sync_result(new_events_count: int, errors_count: int) -> None:
    """Write sync result to a JSON file that Electron polls for notifications."""
    import json
    try:
        result = {
            "new_events": new_events_count,
            "errors": errors_count,
            "timestamp": datetime.now().isoformat(),
        }
        notify_path = DATA_DIR / "last_sync_result.json"
        notify_path.write_text(json.dumps(result), encoding="utf-8")
    except Exception as exc:
        logger.debug("Could not write last_sync_result.json: %s", exc)


def _open_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


class SyncScheduler:
    """
    Daemon thread that periodically syncs all cases.
    One instance lives for the lifetime of the Flask app.
    """

    def __init__(self, interval_hours: float = 2.0):
        self._interval_hours = interval_hours
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_sync: Optional[str] = None
        self._is_running = False
        self._cases_synced = 0
        self._run_new_events = 0
        self._run_errors = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, delay_seconds: float = 5.0) -> None:
        """Start the scheduler daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, args=(delay_seconds,), daemon=True, name="SyncScheduler"
        )
        self._thread.start()
        logger.info("SyncScheduler started (interval=%sh, startup_delay=%ss)",
                    self._interval_hours, delay_seconds)

    def stop(self) -> None:
        self._stop_event.set()

    def trigger_now(self) -> None:
        """Trigger an immediate sync in a one-shot thread."""
        t = threading.Thread(target=self._sync_all, daemon=True, name="SyncOnDemand")
        t.start()

    def update_interval(self, hours: float) -> None:
        with self._lock:
            self._interval_hours = max(0.25, float(hours))

    def get_status(self) -> dict:
        with self._lock:
            next_sync = None
            if self._last_sync and not self._is_running:
                try:
                    ls = datetime.fromisoformat(self._last_sync)
                    next_dt = ls + timedelta(hours=self._interval_hours)
                    next_sync = next_dt.isoformat()
                except Exception:
                    pass
            conn = _open_conn()
            try:
                count = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
            except Exception:
                count = 0
            finally:
                conn.close()
            return {
                "last_sync": self._last_sync,
                "next_sync": next_sync,
                "is_running": self._is_running,
                "cases_count": count,
                "interval_hours": self._interval_hours,
            }

    # ── Internal loop ─────────────────────────────────────────────────────────

    def _loop(self, startup_delay: float) -> None:
        time.sleep(startup_delay)
        while not self._stop_event.is_set():
            self._sync_all()
            # Sleep in small slices so stop_event is noticed quickly
            interval_secs = self._interval_hours * 3600
            elapsed = 0.0
            while elapsed < interval_secs and not self._stop_event.is_set():
                time.sleep(min(60, interval_secs - elapsed))
                elapsed += 60

    def _sync_all(self) -> None:
        with self._lock:
            if self._is_running:
                logger.debug("SyncScheduler: sync already running, skipping")
                return
            self._is_running = True

        self._run_new_events = 0
        self._run_errors = 0
        conn = _open_conn()
        try:
            self._sync_kad(conn)
            self._sync_soy(conn)
            self._last_sync = datetime.utcnow().isoformat()
            logger.info("SyncScheduler: sync complete at %s", self._last_sync)
        except Exception as exc:
            logger.error("SyncScheduler: sync error: %s", exc)
            self._run_errors += 1
        finally:
            conn.close()
            with self._lock:
                self._is_running = False
            _notify_sync_result(self._run_new_events, self._run_errors)

    # ── KAD sync ─────────────────────────────────────────────────────────────

    def _sync_kad(self, conn: sqlite3.Connection) -> None:
        from court_tracker.db import queries
        from court_tracker.scraper.kad_scraper import KADScraper

        cases = queries.get_all_cases(conn, {"source": "kad"})
        for case in cases:
            if not case.get("kad_url"):
                continue
            try:
                with KADScraper() as scraper:
                    details = scraper.get_case_details(case["kad_url"])
                if not details:
                    continue
                details["case_number"] = case["case_number"]
                cid = queries.upsert_case(conn, details)
                queries.save_participants(conn, cid, details.get("participants", []), smart=False)

                old_events = {
                    (e["event_date"], e["event_type"])
                    for e in conn.execute(
                        "SELECT event_date, event_type FROM events WHERE case_id=?", (cid,)
                    ).fetchall()
                }
                queries.save_events(conn, cid, details.get("events", []))

                new_events = [
                    e for e in details.get("events", [])
                    if (e.get("event_date"), e.get("event_type")) not in old_events
                ]
                self._run_new_events += len(new_events)
                for ev in new_events:
                    queries.create_notification(
                        conn, cid, "new_event",
                        f"Новое событие в деле {case['case_number']}: "
                        f"{ev.get('event_type', '')} {ev.get('event_date', '')}",
                    )

                try:
                    from court_tracker.services.deadline_service import auto_create_deadlines_for_case
                    auto_create_deadlines_for_case(conn, cid)
                except Exception as exc:
                    logger.warning("auto_create_deadlines error case %s: %s", cid, exc)

                queries.log_sync(conn, cid, True, "scheduler KAD sync")
                self._cases_synced += 1
            except Exception as exc:
                logger.warning("KAD sync error case %s: %s", case.get("case_number"), exc)
                queries.log_sync(conn, case["id"], False, str(exc)[:200])
                self._run_errors += 1

    # ── SOY sync ─────────────────────────────────────────────────────────────

    def _sync_soy(self, conn: sqlite3.Connection) -> None:
        from court_tracker.db import queries
        from court_tracker.scraper.soy_scraper import SOYScraper

        cases = queries.get_soy_cases_for_sync(conn)
        for case in cases:
            url = (
                case.get("soy_url_cassation")
                or case.get("soy_url_appeal")
                or case.get("soy_url_first")
            )
            if not url:
                continue

            # Skip if already failed too many times
            if (case.get("soy_scrape_attempts") or 0) >= 10:
                continue

            try:
                scraper = SOYScraper(headless=True)
                result = scraper.scrape_case(url)

                if result["success"]:
                    old_events = {
                        (e["event_date"], e["event_type"])
                        for e in conn.execute(
                            "SELECT event_date, event_type FROM events WHERE case_id=?",
                            (case["id"],),
                        ).fetchall()
                    }
                    queries.save_events(conn, case["id"], result["events"])

                    ci = result.get("case_info") or {}
                    if ci.get("judge"):
                        queries.upsert_case_field(conn, case["id"], "judge", ci["judge"])
                    if ci.get("status"):
                        queries.upsert_case_field(conn, case["id"], "status", ci["status"])

                    if result.get("participants"):
                        queries.save_participants(conn, case["id"], result["participants"], smart=True)

                    queries.update_soy_scrape_status(conn, case["id"], "success")
                    queries.log_sync(conn, case["id"], True, "scheduler СОЮ sync")

                    new_events = [
                        e for e in result["events"]
                        if (e.get("event_date"), e.get("event_type")) not in old_events
                    ]
                    self._run_new_events += len(new_events)
                    for ev in new_events:
                        queries.create_notification(
                            conn, case["id"], "new_event",
                            f"СОЮ — новое событие в деле {case['case_number']}: "
                            f"{ev.get('event_type', '')} {ev.get('event_date', '')}",
                        )
                    self._cases_synced += 1
                else:
                    queries.update_soy_scrape_status(
                        conn, case["id"], result["status"], result.get("error_msg")
                    )
                    queries.log_sync(conn, case["id"], False, result.get("error_msg", ""))
                    self._run_errors += 1

                    # Notify after 5th consecutive failure
                    attempts = (case.get("soy_scrape_attempts") or 0) + 1
                    if attempts == 5:
                        queries.create_notification(
                            conn, case["id"], "soy_fail",
                            f"Автообновление недоступно для дела {case['case_number']}: "
                            f"{result.get('error_msg', 'ошибка')}",
                        )
            except Exception as exc:
                logger.warning("SOY sync error case %s: %s", case.get("case_number"), exc)
                queries.update_soy_scrape_status(conn, case["id"], "failed", str(exc)[:200])
                queries.log_sync(conn, case["id"], False, str(exc)[:200])
                self._run_errors += 1
