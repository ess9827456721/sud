"""All database query functions for court_tracker."""
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    desc = cursor.description
    return [{col[0]: row[i] for i, col in enumerate(desc)} for row in cursor.fetchall()]


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def upsert_case(conn: sqlite3.Connection, data: dict) -> int:
    """Insert or update a case by case_number. Returns the row id."""
    soy_enabled = data.get("soy_scraping_enabled")
    if soy_enabled is None:
        # Auto-enable SOY scraping if any SOY URL is provided
        has_soy_url = any(data.get(f) for f in ("soy_url_first", "soy_url_appeal", "soy_url_cassation"))
        soy_enabled = 1 if (data.get("source") == "soy" and has_soy_url) else 0

    existing = conn.execute(
        "SELECT id FROM cases WHERE case_number = ?", (data["case_number"],)
    ).fetchone()

    now = _now()
    if existing:
        case_id = existing[0]
        conn.execute(
            """UPDATE cases SET
                source=COALESCE(?,source), case_id_kad=COALESCE(?,case_id_kad),
                court=COALESCE(?,court), judge=COALESCE(?,judge),
                status=COALESCE(?,status), case_type=COALESCE(?,case_type),
                start_date=COALESCE(?,start_date), kad_url=COALESCE(?,kad_url),
                soy_url_first=COALESCE(?,soy_url_first),
                soy_url_appeal=COALESCE(?,soy_url_appeal),
                soy_url_cassation=COALESCE(?,soy_url_cassation),
                client_id=COALESCE(?,client_id),
                last_synced_at=?, updated_at=?
            WHERE id=?""",
            (
                data.get("source"), data.get("case_id_kad"),
                data.get("court"), data.get("judge"),
                data.get("status"), data.get("case_type"),
                data.get("start_date"), data.get("kad_url"),
                data.get("soy_url_first"), data.get("soy_url_appeal"),
                data.get("soy_url_cassation"), data.get("client_id"),
                now, now, case_id,
            ),
        )
    else:
        cur = conn.execute(
            """INSERT INTO cases
                (source, case_number, case_id_kad, court, judge, status,
                 case_type, start_date, kad_url,
                 soy_url_first, soy_url_appeal, soy_url_cassation,
                 custom_label, custom_status, client_id,
                 last_synced_at, soy_scraping_enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data.get("source", "kad"),
                data["case_number"],
                data.get("case_id_kad"),
                data.get("court"),
                data.get("judge"),
                data.get("status"),
                data.get("case_type"),
                data.get("start_date"),
                data.get("kad_url"),
                data.get("soy_url_first"),
                data.get("soy_url_appeal"),
                data.get("soy_url_cassation"),
                data.get("custom_label"),
                data.get("custom_status"),
                data.get("client_id"),
                now,
                soy_enabled,
                now,
                now,
            ),
        )
        case_id = cur.lastrowid
        # Create default kanban stage
        conn.execute(
            "INSERT OR IGNORE INTO kanban_stage(case_id, stage) VALUES (?, 'first')",
            (case_id,),
        )
    conn.commit()
    return case_id


def get_all_cases(conn: sqlite3.Connection, filters: Optional[dict] = None) -> list[dict]:
    sql = """
        SELECT c.*, cl.name AS client_name
        FROM cases c
        LEFT JOIN clients cl ON cl.id = c.client_id
    """
    params: list[Any] = []
    where: list[str] = []
    if filters:
        if filters.get("source"):
            where.append("c.source = ?")
            params.append(filters["source"])
        if filters.get("status"):
            where.append("c.status = ?")
            params.append(filters["status"])
        if filters.get("client_id"):
            where.append("c.client_id = ?")
            params.append(filters["client_id"])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY c.updated_at DESC"
    cur = conn.execute(sql, params)
    return _rows(cur)


def get_case_full(conn: sqlite3.Connection, case_id: int) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,))
    row = cur.fetchone()
    if not row:
        return None
    case = _row_to_dict(cur, row)
    cur2 = conn.execute("SELECT * FROM participants WHERE case_id = ?", (case_id,))
    case["participants"] = _rows(cur2)
    cur3 = conn.execute("SELECT * FROM events WHERE case_id = ? ORDER BY event_date DESC", (case_id,))
    case["events"] = _rows(cur3)
    cur4 = conn.execute("SELECT * FROM deadlines WHERE case_id = ? ORDER BY deadline_date", (case_id,))
    case["deadlines"] = _rows(cur4)
    cur5 = conn.execute("SELECT * FROM notes_and_tasks WHERE case_id = ? ORDER BY position", (case_id,))
    case["notes"] = _rows(cur5)
    return case


def get_case_by_number(conn: sqlite3.Connection, case_number: str) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM cases WHERE case_number = ?", (case_number,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row else None


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------

def save_participants(conn: sqlite3.Connection, case_id: int, participants: list[dict]) -> None:
    conn.execute("DELETE FROM participants WHERE case_id = ?", (case_id,))
    for p in participants:
        conn.execute(
            "INSERT INTO participants(case_id, role, name, inn, address) VALUES (?,?,?,?,?)",
            (case_id, p.get("role"), p.get("name"), p.get("inn"), p.get("address")),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def save_events(conn: sqlite3.Connection, case_id: int, events: list[dict]) -> None:
    """Smart merge: insert events that don't already exist (match by date+type)."""
    existing = conn.execute(
        "SELECT event_date, event_type FROM events WHERE case_id = ?", (case_id,)
    ).fetchall()
    existing_set = {(r[0], r[1]) for r in existing}

    for ev in events:
        key = (ev.get("event_date"), ev.get("event_type"))
        if key not in existing_set:
            conn.execute(
                """INSERT INTO events(case_id, event_date, event_type, description, document_url, is_future)
                   VALUES (?,?,?,?,?,?)""",
                (
                    case_id,
                    ev.get("event_date"),
                    ev.get("event_type"),
                    ev.get("description"),
                    ev.get("document_url"),
                    ev.get("is_future", 0),
                ),
            )
    conn.commit()


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------

def get_approaching_deadlines(conn: sqlite3.Connection, days: int = 14) -> list[dict]:
    cutoff = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    cur = conn.execute(
        """SELECT d.*, c.case_number, c.court
           FROM deadlines d
           JOIN cases c ON c.id = d.case_id
           WHERE d.deadline_date <= ? AND d.deadline_date >= ? AND d.is_done = 0
           ORDER BY d.deadline_date""",
        (cutoff, today),
    )
    return _rows(cur)


def get_upcoming_hearings(conn: sqlite3.Connection, days: int = 7) -> list[dict]:
    cutoff = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    cur = conn.execute(
        """SELECT e.*, c.case_number, c.court
           FROM events e
           JOIN cases c ON c.id = e.case_id
           WHERE e.is_future = 1 AND e.event_date >= ? AND e.event_date <= ?
           ORDER BY e.event_date""",
        (today, cutoff),
    )
    return _rows(cur)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

def upsert_client(conn: sqlite3.Connection, data: dict) -> int:
    now = _now()
    existing = None
    if data.get("inn"):
        existing = conn.execute("SELECT id FROM clients WHERE inn = ?", (data["inn"],)).fetchone()

    if existing:
        client_id = existing[0]
        conn.execute(
            """UPDATE clients SET
                type=COALESCE(?,type), name=COALESCE(?,name),
                short_name=COALESCE(?,short_name), ogrn=COALESCE(?,ogrn),
                address=COALESCE(?,address), status_egrul=COALESCE(?,status_egrul),
                phone=COALESCE(?,phone), email=COALESCE(?,email),
                contact_person=COALESCE(?,contact_person), notes=COALESCE(?,notes),
                updated_at=?
            WHERE id=?""",
            (
                data.get("type"), data.get("name"), data.get("short_name"),
                data.get("ogrn"), data.get("address"), data.get("status_egrul"),
                data.get("phone"), data.get("email"), data.get("contact_person"),
                data.get("notes"), now, client_id,
            ),
        )
    else:
        cur = conn.execute(
            """INSERT INTO clients
                (type, name, short_name, inn, ogrn, address, status_egrul,
                 phone, email, contact_person, notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data.get("type", "legal"), data.get("name", ""),
                data.get("short_name"), data.get("inn"), data.get("ogrn"),
                data.get("address"), data.get("status_egrul"),
                data.get("phone"), data.get("email"), data.get("contact_person"),
                data.get("notes"), now, now,
            ),
        )
        client_id = cur.lastrowid
    conn.commit()
    return client_id


def get_client_with_cases(conn: sqlite3.Connection, client_id: int) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,))
    row = cur.fetchone()
    if not row:
        return None
    client = _row_to_dict(cur, row)
    cur2 = conn.execute("SELECT * FROM cases WHERE client_id = ? ORDER BY updated_at DESC", (client_id,))
    client["cases"] = _rows(cur2)
    cur3 = conn.execute("SELECT * FROM client_contacts WHERE client_id = ?", (client_id,))
    client["contacts"] = _rows(cur3)
    return client


# ---------------------------------------------------------------------------
# SOY sync helpers
# ---------------------------------------------------------------------------

def get_soy_cases_for_sync(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """SELECT * FROM cases
           WHERE source = 'soy'
             AND soy_scraping_enabled = 1
             AND soy_scrape_attempts < 5
           ORDER BY soy_scrape_last_ok ASC""",
    )
    return _rows(cur)


def update_soy_scrape_status(
    conn: sqlite3.Connection,
    case_id: int,
    status: str,
    error_msg: Optional[str] = None,
) -> None:
    now = _now()
    if status == "success":
        conn.execute(
            """UPDATE cases SET
                soy_scrape_status = 'success',
                soy_scrape_last_ok = ?,
                soy_scrape_attempts = 0,
                soy_scrape_error_msg = NULL,
                updated_at = ?
               WHERE id = ?""",
            (now, now, case_id),
        )
    else:
        conn.execute(
            """UPDATE cases SET
                soy_scrape_status = ?,
                soy_scrape_error_msg = ?,
                soy_scrape_attempts = soy_scrape_attempts + 1,
                updated_at = ?
               WHERE id = ?""",
            (status, error_msg, now, case_id),
        )
        # Disable scraping after 5 consecutive failures
        conn.execute(
            """UPDATE cases SET soy_scraping_enabled = 0
               WHERE id = ? AND soy_scrape_attempts >= 5""",
            (case_id,),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Sync log
# ---------------------------------------------------------------------------

def log_sync(conn: sqlite3.Connection, case_id: Optional[int], success: bool, message: str) -> None:
    conn.execute(
        "INSERT INTO sync_log(case_id, success, message) VALUES (?,?,?)",
        (case_id, 1 if success else 0, message),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Notes / Tasks
# ---------------------------------------------------------------------------

def create_note(conn: sqlite3.Connection, data: dict) -> int:
    now = _now()
    cur = conn.execute(
        """INSERT INTO notes_and_tasks
            (case_id, title, body, color, item_type, task_status,
             task_due_date, task_priority, position, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            data["case_id"], data.get("title"), data.get("body"),
            data.get("color"), data.get("item_type", "note"),
            data.get("task_status", "new"), data.get("task_due_date"),
            data.get("task_priority", "medium"), data.get("position", 0),
            now, now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_notes_for_case(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM notes_and_tasks WHERE case_id = ? ORDER BY position, created_at",
        (case_id,),
    )
    notes = _rows(cur)
    for note in notes:
        cur2 = conn.execute(
            "SELECT * FROM checklist_items WHERE note_id = ? ORDER BY position",
            (note["id"],),
        )
        note["checklist"] = _rows(cur2)
        cur3 = conn.execute("SELECT tag FROM note_tags WHERE note_id = ?", (note["id"],))
        note["tags"] = [r[0] for r in cur3.fetchall()]
    return notes


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (key, value))
    conn.commit()
