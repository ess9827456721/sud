"""
KAD monitoring of client cases (Block 3).

Periodically checks each client's INN against kad.arbitr.ru and records
newly found cases as candidates the lawyer can add to tracking or ignore.
"""
import logging
import random
import sqlite3
import time
from datetime import datetime

logger = logging.getLogger(__name__)


def check_client(conn: sqlite3.Connection, client) -> dict:
    """
    Search KAD by the client's INN; insert unseen cases into
    client_case_candidates (status='new') and notify about each of them.
    Returns {'client_id', 'found': n, 'new': m}.
    """
    from court_tracker.db import queries
    from court_tracker.scraper.kad_scraper import KADScraper

    client = dict(client)
    client_id = client["id"]
    inn = (client.get("inn") or "").strip()
    summary = {"client_id": client_id, "found": 0, "new": 0}
    if not inn:
        return summary

    with KADScraper() as scraper:
        found = scraper.search_by_inn(inn)

    summary["found"] = len(found)
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    for case in found:
        case_number = case.get("case_number")
        if not case_number:
            continue
        # Already tracked as a real case — skip
        exists = conn.execute(
            "SELECT 1 FROM cases WHERE case_number=?", (case_number,)
        ).fetchone()
        if exists:
            continue
        cur = conn.execute(
            """INSERT OR IGNORE INTO client_case_candidates
               (client_id, case_number, kad_url, court)
               VALUES (?,?,?,?)""",
            (client_id, case_number, case.get("kad_url"), case.get("court")),
        )
        if cur.rowcount > 0:
            summary["new"] += 1
            queries.create_notification(
                conn, None, "new_case",
                f"По клиенту {client.get('name', '')} найдено новое дело "
                f"{case_number} ({case.get('court') or 'суд не указан'})",
            )

    conn.execute(
        "UPDATE clients SET kad_last_checked=? WHERE id=?", (now, client_id)
    )
    conn.commit()

    queries.log_sync(
        conn, None, True,
        f"КАД-мониторинг клиента {client.get('name', client_id)}: "
        f"найдено {summary['found']}, новых {summary['new']}",
    )
    return summary


def check_all_clients(conn: sqlite3.Connection) -> list[dict]:
    """Check every monitored client with an INN; polite 3-7s pauses."""
    from court_tracker.db import queries

    clients = conn.execute(
        "SELECT * FROM clients WHERE kad_monitoring=1 AND inn IS NOT NULL AND inn != ''"
    ).fetchall()

    results = []
    for i, client in enumerate(clients):
        try:
            results.append(check_client(conn, client))
        except Exception as exc:
            logger.warning("client monitor error for client %s: %s",
                           client["id"], exc)
            queries.log_sync(conn, None, False,
                             f"КАД-мониторинг клиента {client['name']}: {str(exc)[:150]}")
        if i < len(clients) - 1:
            time.sleep(random.uniform(3, 7))  # be polite to KAD
    return results
