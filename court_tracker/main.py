#!/usr/bin/env python3
"""
Трекер судебных дел v2 — CLI entry point.

Usage:
  python main.py add-inn <INN>
  python main.py add-case <CASE_NUMBER>
  python main.py kad-doctor [CASE_NUMBER]
  python main.py kad-debug <CASE_NUMBER|INN>
  python main.py list
  python main.py sync <case_id>
  python main.py sync-all
  python main.py serve
"""
import sys
import logging
import sqlite3
from pathlib import Path

# Allow running as `python main.py` from inside court_tracker/ or from project root
_here = Path(__file__).parent
if str(_here.parent) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from court_tracker.config import DB_PATH, LOG_LEVEL
from court_tracker.db.schema import init_db, run_migrations
from court_tracker.db import queries

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    run_migrations(conn)
    return conn


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_add_inn(inn: str) -> None:
    print(f"Searching KAD for INN: {inn} …")
    from court_tracker.scraper.kad_scraper import KADScraper
    conn = open_db()
    with KADScraper() as scraper:
        results = scraper.search_by_inn(inn)
        kad_error = scraper.last_error
        strategy = scraper.last_strategy
    if not results:
        human = kad_error or f"add-inn {inn}: no results"
        print(human if kad_error else "No cases found.")
        queries.log_sync(conn, None, False, human)
        return
    print(f"Strategy: {strategy or '?'}")
    for r in results:
        case_id = queries.upsert_case(conn, r)
        if r.get("kad_url"):
            with KADScraper() as scraper:
                details = scraper.get_case_details(r["kad_url"])
            if details:
                details["case_number"] = r["case_number"]
                case_id = queries.upsert_case(conn, details)
                queries.save_participants(conn, case_id, details.get("participants", []))
                queries.save_events(conn, case_id, details.get("events", []))
        queries.log_sync(conn, case_id, True, f"add-inn import (стратегия: {strategy or '?'})")
        print(f"  Saved: {r.get('case_number')} (id={case_id})")
    print(f"Done. {len(results)} case(s) imported.")


def cmd_add_case(case_number: str) -> None:
    from court_tracker.scraper.utils import normalize_case_number
    case_number = normalize_case_number(case_number)
    print(f"Fetching case: {case_number} …")
    from court_tracker.scraper.kad_scraper import KADScraper
    conn = open_db()

    with KADScraper() as scraper:
        result = scraper.search_by_case_number(case_number)
        if not result:
            human = scraper.last_error or f"add-case {case_number}: not found"
            print(human)
            queries.log_sync(conn, None, False, human)
            return
        strategy = scraper.last_strategy
        details = None
        if result.get("kad_url"):
            details = scraper.get_case_details(result["kad_url"])

    data = details if details else result
    data.setdefault("case_number", case_number)
    data.setdefault("source", "kad")

    case_id = queries.upsert_case(conn, data)
    if details:
        queries.save_participants(conn, case_id, details.get("participants", []))
        queries.save_events(conn, case_id, details.get("events", []))
    queries.log_sync(conn, case_id, True, f"add-case import (стратегия: {strategy or '?'})")
    print(f"  Стратегия: {strategy or '?'}")

    print(f"\n{'='*60}")
    print(f"  Дело:     {data.get('case_number')}")
    print(f"  Суд:      {data.get('court', '—')}")
    print(f"  Судья:    {data.get('judge', '—')}")
    print(f"  Статус:   {data.get('status', '—')}")
    print(f"  Тип:      {data.get('case_type', '—')}")
    print(f"  Дата:     {data.get('start_date', '—')}")
    print(f"  URL:      {data.get('kad_url', '—')}")
    if details:
        print(f"  Участников: {len(details.get('participants', []))}")
        print(f"  Событий:    {len(details.get('events', []))}")
    print(f"  ID в БД:  {case_id}")
    print(f"{'='*60}\n")


def cmd_kad_debug(query: str) -> None:
    """Open KAD in a visible window with DevTools, log the network, keep it open."""
    from court_tracker.scraper.kad_scraper import KADScraper
    is_inn = query.replace(" ", "").isdigit()
    print(f"kad-debug: запуск видимого окна с DevTools для "
          f"{'ИНН' if is_inn else 'дела'} {query}…")
    with KADScraper(headless=False, devtools=True) as scraper:
        scraper.debug_search(query, is_inn=is_inn)


def cmd_kad_doctor(case_number: str = "А60-33087/2025") -> None:
    """One-shot diagnostic capture of KAD's own search request."""
    from court_tracker.scraper.kad_scraper import KADScraper
    print("kad-doctor: запуск браузера (видимое окно)…")
    with KADScraper(headless=False) as scraper:
        path = scraper.capture_reference(case_number)
    if path:
        print(f"Готово. Эталон захвачен: {path}")
        print("Теперь 'python main.py add-case ...' будет использовать путь replay-capture.")
    else:
        print("Захват не выполнен — см. лог.")


def cmd_list() -> None:
    conn = open_db()
    cases = queries.get_all_cases(conn)
    if not cases:
        print("No cases in database.")
        return

    col = [("ID", 4), ("Номер дела", 22), ("Суд", 30), ("Статус", 20), ("Источник", 8), ("Обновлено", 19)]
    header = "  ".join(f"{h:<{w}}" for h, w in col)
    print(header)
    print("-" * len(header))
    for c in cases:
        row = (
            f"{c['id']:<4}  "
            f"{(c.get('case_number') or ''):<22}  "
            f"{(c.get('court') or '')[:28]:<30}  "
            f"{(c.get('status') or '')[:18]:<20}  "
            f"{(c.get('source') or ''):<8}  "
            f"{(c.get('updated_at') or '')[:19]:<19}"
        )
        print(row)
    print(f"\nTotal: {len(cases)} case(s)")


def cmd_sync(case_id: int) -> None:
    conn = open_db()
    case = queries.get_case_full(conn, case_id)
    if not case:
        print(f"Case id={case_id} not found.")
        return
    if not case.get("kad_url"):
        print(f"Case {case['case_number']} has no KAD URL, cannot sync.")
        return
    print(f"Syncing case {case['case_number']} …")
    from court_tracker.scraper.kad_scraper import KADScraper
    with KADScraper() as scraper:
        details = scraper.get_case_details(case["kad_url"])
    if not details:
        queries.log_sync(conn, case_id, False, "sync: scraper returned None")
        print("Sync failed (scraper error).")
        return
    details["case_number"] = case["case_number"]
    queries.upsert_case(conn, details)
    queries.save_participants(conn, case_id, details.get("participants", []))
    queries.save_events(conn, case_id, details.get("events", []))
    queries.log_sync(conn, case_id, True, "manual sync")
    print(f"Synced: {case['case_number']}")


def cmd_sync_all() -> None:
    conn = open_db()
    cases = queries.get_all_cases(conn, {"source": "kad"})
    print(f"Syncing {len(cases)} KAD case(s) …")
    for c in cases:
        try:
            cmd_sync(c["id"])
        except Exception as exc:
            logger.error("sync_all: case %s failed: %s", c.get("case_number"), exc)
    print("sync-all complete.")


def cmd_serve() -> None:
    from court_tracker.app import create_app
    from court_tracker.config import FLASK_HOST, FLASK_PORT, FLASK_DEBUG
    print(f"Starting Flask on http://{FLASK_HOST}:{FLASK_PORT}")
    app = create_app()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG, threaded=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    if cmd == "add-inn" and len(args) >= 2:
        cmd_add_inn(args[1])
    elif cmd == "add-case" and len(args) >= 2:
        cmd_add_case(args[1])
    elif cmd == "kad-doctor":
        cmd_kad_doctor(args[1] if len(args) >= 2 else "А60-33087/2025")
    elif cmd == "kad-debug" and len(args) >= 2:
        cmd_kad_debug(args[1])
    elif cmd == "list":
        cmd_list()
    elif cmd == "sync" and len(args) >= 2:
        cmd_sync(int(args[1]))
    elif cmd == "sync-all":
        cmd_sync_all()
    elif cmd == "serve":
        cmd_serve()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
