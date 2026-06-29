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

def save_participants(conn: sqlite3.Connection, case_id: int,
                      participants: list[dict], smart: bool = False) -> None:
    """
    Insert or update participants for a case.

    smart=False (default, KAD): delete-and-replace — always authoritative.
    smart=True  (SOY scraper): merge preserving _manual-flagged fields.
      - Match existing row by (role, first-20-chars-of-name).
      - If matched: only overwrite fields where {field}_manual == 0.
      - If no match: insert as new participant.
    """
    if not smart:
        conn.execute("DELETE FROM participants WHERE case_id = ?", (case_id,))
        for p in participants:
            conn.execute(
                """INSERT INTO participants
                   (case_id, role, name, inn, address, representative,
                    inn_manual, name_manual, address_manual)
                   VALUES (?,?,?,?,?,?,0,0,0)""",
                (case_id, p.get("role"), p.get("name"), p.get("inn"),
                 p.get("address"), p.get("representative")),
            )
        conn.commit()
        return

    # ── Smart merge (SOY) ────────────────────────────────────────────────
    existing = conn.execute(
        "SELECT * FROM participants WHERE case_id = ?", (case_id,)
    ).fetchall()
    # Build lookup: (normalised_role, normalised_name_prefix) → row
    def _key(role, name):
        return ((role or "").lower().strip(),
                (name or "").lower().strip()[:20])

    existing_map = {_key(r["role"], r["name"]): r for r in existing}

    for p in participants:
        key = _key(p.get("role"), p.get("name"))
        ex = existing_map.get(key)

        if ex:
            # Update only non-manual fields
            updates, vals = [], []
            for field in ("name", "inn", "address", "representative"):
                manual_flag = f"{field}_manual"
                # representative has no manual flag — always update
                if field == "representative" or not ex[manual_flag]:
                    updates.append(f"{field}=?")
                    vals.append(p.get(field))
            if updates:
                vals.append(ex["id"])
                conn.execute(
                    f"UPDATE participants SET {', '.join(updates)} WHERE id=?", vals
                )
        else:
            conn.execute(
                """INSERT INTO participants
                   (case_id, role, name, inn, address, representative,
                    inn_manual, name_manual, address_manual)
                   VALUES (?,?,?,?,?,?,0,0,0)""",
                (case_id, p.get("role"), p.get("name"), p.get("inn"),
                 p.get("address"), p.get("representative")),
            )

    conn.commit()


def freeze_participant_field(conn: sqlite3.Connection,
                             participant_id: int, field: str,
                             value: str) -> bool:
    """
    Manually set a participant field and mark it as frozen (_manual=1).
    field must be one of: inn, name, address.
    """
    if field not in ("inn", "name", "address"):
        return False
    conn.execute(
        f"UPDATE participants SET {field}=?, {field}_manual=1 WHERE id=?",
        (value, participant_id),
    )
    conn.commit()
    return conn.execute(
        "SELECT changes()"
    ).fetchone()[0] > 0


def unfreeze_participant_field(conn: sqlite3.Connection,
                               participant_id: int,
                               field: Optional[str] = None) -> bool:
    """
    Clear manual flag(s) for a participant.
    If field is None, clears all three flags.
    """
    if field and field not in ("inn", "name", "address"):
        return False
    if field:
        conn.execute(
            f"UPDATE participants SET {field}_manual=0 WHERE id=?",
            (participant_id,),
        )
    else:
        conn.execute(
            "UPDATE participants SET inn_manual=0, name_manual=0, address_manual=0 WHERE id=?",
            (participant_id,),
        )
    conn.commit()
    return True


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


def get_all_clients(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """SELECT cl.*,
                  COUNT(DISTINCT c.id) AS case_count
           FROM clients cl
           LEFT JOIN cases c ON c.client_id = cl.id
           GROUP BY cl.id
           ORDER BY cl.name"""
    )
    return _rows(cur)


def get_client_with_cases(conn: sqlite3.Connection, client_id: int) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,))
    row = cur.fetchone()
    if not row:
        return None
    client = _row_to_dict(cur, row)
    cur2 = conn.execute(
        """SELECT c.*, k.stage
           FROM cases c
           LEFT JOIN kanban_stage k ON k.case_id = c.id
           WHERE c.client_id = ?
           ORDER BY c.updated_at DESC""",
        (client_id,),
    )
    client["cases"] = _rows(cur2)
    cur3 = conn.execute("SELECT * FROM client_contacts WHERE client_id = ?", (client_id,))
    client["contacts"] = _rows(cur3)
    cur4 = conn.execute(
        """SELECT p.*, a.file_path, a.filename
           FROM client_powers_of_attorney p
           LEFT JOIN attachments a ON a.id = p.attachment_id
           WHERE p.client_id = ?
           ORDER BY p.expiry_date""",
        (client_id,),
    )
    client["powers_of_attorney"] = _rows(cur4)
    return client


# ---------------------------------------------------------------------------
# Client contacts
# ---------------------------------------------------------------------------

def add_contact(conn: sqlite3.Connection, data: dict) -> int:
    cur = conn.execute(
        "INSERT INTO client_contacts(client_id, name, role, phone, email, notes) VALUES (?,?,?,?,?,?)",
        (data["client_id"], data.get("name"), data.get("role"),
         data.get("phone"), data.get("email"), data.get("notes")),
    )
    conn.commit()
    return cur.lastrowid


def delete_contact(conn: sqlite3.Connection, contact_id: int) -> bool:
    cur = conn.execute("DELETE FROM client_contacts WHERE id = ?", (contact_id,))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Powers of attorney
# ---------------------------------------------------------------------------

def add_power_of_attorney(conn: sqlite3.Connection, data: dict) -> int:
    now = _now()
    cur = conn.execute(
        """INSERT INTO client_powers_of_attorney
            (client_id, case_id, number, issue_date, expiry_date,
             scope_description, attachment_id, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            data["client_id"], data.get("case_id"), data.get("number"),
            data.get("issue_date"), data.get("expiry_date"),
            data.get("scope_description"), data.get("attachment_id"), now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def delete_power_of_attorney(conn: sqlite3.Connection, poa_id: int) -> bool:
    cur = conn.execute("DELETE FROM client_powers_of_attorney WHERE id = ?", (poa_id,))
    conn.commit()
    return cur.rowcount > 0


def get_expiring_powers_of_attorney(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    cutoff = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    cur = conn.execute(
        """SELECT p.*, cl.name AS client_name, cl.id AS client_id_ref
           FROM client_powers_of_attorney p
           JOIN clients cl ON cl.id = p.client_id
           WHERE p.expiry_date IS NOT NULL
             AND p.expiry_date >= ?
             AND p.expiry_date <= ?
           ORDER BY p.expiry_date""",
        (today, cutoff),
    )
    return _rows(cur)


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


# ---------------------------------------------------------------------------
# Deadlines (Phase 4)
# ---------------------------------------------------------------------------

def get_deadlines_for_case(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM deadlines WHERE case_id=? ORDER BY deadline_date",
        (case_id,),
    )
    return _rows(cur)


def create_deadline(conn: sqlite3.Connection, data: dict) -> int:
    now = _now()
    cur = conn.execute(
        """INSERT INTO deadlines
            (case_id, deadline_type, deadline_date, trigger_date, trigger_event,
             calculation_basis, statute_reference, statute_article, calculation_note,
             is_auto_calculated, is_manual_override, is_done, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,0,?)""",
        (
            data["case_id"],
            data.get("deadline_type"),
            data.get("deadline_date"),
            data.get("trigger_date"),
            data.get("trigger_event"),
            data.get("calculation_basis"),
            data.get("statute_reference"),
            data.get("statute_article"),
            data.get("calculation_note"),
            1 if data.get("is_auto_calculated") else 0,
            now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_deadline_date(conn: sqlite3.Connection, deadline_id: int, new_date: str) -> bool:
    cur = conn.execute(
        "UPDATE deadlines SET deadline_date=?, is_manual_override=1 WHERE id=?",
        (new_date, deadline_id),
    )
    conn.commit()
    return cur.rowcount > 0


def toggle_deadline_done(conn: sqlite3.Connection, deadline_id: int) -> Optional[bool]:
    row = conn.execute("SELECT is_done FROM deadlines WHERE id=?", (deadline_id,)).fetchone()
    if not row:
        return None
    new_val = 0 if row[0] else 1
    conn.execute("UPDATE deadlines SET is_done=? WHERE id=?", (new_val, deadline_id))
    conn.commit()
    return bool(new_val)


def delete_deadline(conn: sqlite3.Connection, deadline_id: int) -> bool:
    cur = conn.execute("DELETE FROM deadlines WHERE id=?", (deadline_id,))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Kanban (Phase 4)
# ---------------------------------------------------------------------------

KANBAN_STAGES = ["pretension", "first", "appeal", "cassation", "supreme", "execution", "archive"]
STAGE_NAMES = {
    "pretension": "Претензия",
    "first": "Первая инстанция",
    "appeal": "Апелляция",
    "cassation": "Кассация",
    "supreme": "ВС РФ",
    "execution": "Исполнение",
    "archive": "Архив",
}

# KAD status → expected stage keywords for mismatch detection
_STAGE_KAD_HINTS: dict[str, list[str]] = {
    "appeal":    ["апелляц"],
    "cassation": ["кассац"],
    "supreme":   ["верховн", "президиум"],
    "execution": ["исполн"],
    "archive":   ["прекращ", "завершен", "оставлен без рассмотрения"],
}


def get_kanban_board(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Return all cases grouped by kanban stage, with next hearing date."""
    board: dict[str, list[dict]] = {s: [] for s in KANBAN_STAGES}

    rows = conn.execute(
        """SELECT c.id, c.case_number, c.court, c.status, c.source, c.client_id,
                  cl.name AS client_name,
                  k.stage,
                  (SELECT MIN(e.event_date) FROM events e
                   WHERE e.case_id=c.id AND e.is_future=1) AS next_hearing
           FROM cases c
           LEFT JOIN kanban_stage k ON k.case_id = c.id
           LEFT JOIN clients cl ON cl.id = c.client_id
           ORDER BY c.case_number"""
    ).fetchall()

    cols = ["id", "case_number", "court", "status", "source", "client_id",
            "client_name", "stage", "next_hearing"]
    for row in rows:
        r = dict(zip(cols, row))
        stage = r.get("stage") or "first"
        if stage not in board:
            stage = "first"
        board[stage].append(r)

    return board


def check_stage_mismatch(case_status: Optional[str], new_stage: str) -> Optional[str]:
    if not case_status:
        return None
    status_lower = case_status.lower()
    for stage, keywords in _STAGE_KAD_HINTS.items():
        if stage == new_stage:
            continue
        if any(kw in status_lower for kw in keywords):
            return (
                f"КАД показывает «{STAGE_NAMES.get(stage, stage)}», "
                f"но карточка перемещена в «{STAGE_NAMES.get(new_stage, new_stage)}»"
            )
    return None


def move_kanban(conn: sqlite3.Connection, case_id: int, new_stage: str) -> Optional[str]:
    """
    Update kanban stage. Returns mismatch warning string or None.
    """
    now = _now()
    existing = conn.execute(
        "SELECT id FROM kanban_stage WHERE case_id=?", (case_id,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE kanban_stage SET stage=?, moved_at=? WHERE case_id=?",
            (new_stage, now, case_id),
        )
    else:
        conn.execute(
            "INSERT INTO kanban_stage(case_id, stage, moved_at) VALUES (?,?,?)",
            (case_id, new_stage, now),
        )
    conn.commit()

    # Check mismatch
    row = conn.execute("SELECT status FROM cases WHERE id=?", (case_id,)).fetchone()
    if row:
        return check_stage_mismatch(row[0], new_stage)
    return None


# ---------------------------------------------------------------------------
# Notes / Tasks — extended CRUD (Phase 5)
# ---------------------------------------------------------------------------

def update_note(conn: sqlite3.Connection, note_id: int, data: dict) -> bool:
    allowed = {'title', 'body', 'color', 'task_status', 'task_due_date',
               'task_priority', 'position'}
    sets: list[str] = []
    params: list[Any] = []
    for k, v in data.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    params.append(_now())
    params.append(note_id)
    cur = conn.execute(
        f"UPDATE notes_and_tasks SET {', '.join(sets)} WHERE id=?",
        params,
    )
    conn.commit()
    return cur.rowcount > 0


def delete_note(conn: sqlite3.Connection, note_id: int) -> bool:
    cur = conn.execute("DELETE FROM notes_and_tasks WHERE id=?", (note_id,))
    conn.commit()
    return cur.rowcount > 0


def add_checklist_item(conn: sqlite3.Connection, note_id: int, text: str) -> int:
    pos = conn.execute(
        "SELECT COUNT(*) FROM checklist_items WHERE note_id=?", (note_id,)
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO checklist_items(note_id, text, checked, position) VALUES (?,?,0,?)",
        (note_id, text, pos),
    )
    conn.commit()
    return cur.lastrowid


def toggle_checklist_item(conn: sqlite3.Connection, item_id: int) -> Optional[bool]:
    row = conn.execute("SELECT checked FROM checklist_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return None
    new_val = 0 if row[0] else 1
    conn.execute("UPDATE checklist_items SET checked=? WHERE id=?", (new_val, item_id))
    conn.commit()
    return bool(new_val)


def delete_checklist_item(conn: sqlite3.Connection, item_id: int) -> bool:
    cur = conn.execute("DELETE FROM checklist_items WHERE id=?", (item_id,))
    conn.commit()
    return cur.rowcount > 0


def add_note_tag(conn: sqlite3.Connection, note_id: int, tag: str) -> bool:
    existing = conn.execute(
        "SELECT id FROM note_tags WHERE note_id=? AND tag=?", (note_id, tag)
    ).fetchone()
    if existing:
        return False
    conn.execute("INSERT INTO note_tags(note_id, tag) VALUES (?,?)", (note_id, tag))
    conn.commit()
    return True


def delete_note_tag(conn: sqlite3.Connection, note_id: int, tag: str) -> bool:
    cur = conn.execute("DELETE FROM note_tags WHERE note_id=? AND tag=?", (note_id, tag))
    conn.commit()
    return cur.rowcount > 0


def get_all_tags(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT tag FROM note_tags ORDER BY tag").fetchall()
    return [r[0] for r in rows]


def reorder_notes(conn: sqlite3.Connection, case_id: int, ordered_ids: list[int]) -> None:
    for pos, note_id in enumerate(ordered_ids):
        conn.execute(
            "UPDATE notes_and_tasks SET position=? WHERE id=? AND case_id=?",
            (pos, note_id, case_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Attachments (Phase 5)
# ---------------------------------------------------------------------------

def create_attachment(conn: sqlite3.Connection, data: dict) -> int:
    now = _now()
    cur = conn.execute(
        """INSERT INTO attachments
            (case_id, note_id, filename, stored_name, file_path,
             file_size, mime_type, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            data["case_id"], data.get("note_id"), data["filename"],
            data["stored_name"], data["file_path"],
            data.get("file_size"), data.get("mime_type"), now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_attachments_for_case(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    cur = conn.execute(
        """SELECT a.*, n.title AS note_title
           FROM attachments a
           LEFT JOIN notes_and_tasks n ON n.id = a.note_id
           WHERE a.case_id=?
           ORDER BY a.created_at DESC""",
        (case_id,),
    )
    return _rows(cur)


def delete_attachment(conn: sqlite3.Connection, att_id: int) -> Optional[str]:
    """Delete DB row and return file_path so caller can remove from disk."""
    row = conn.execute("SELECT file_path FROM attachments WHERE id=?", (att_id,)).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM attachments WHERE id=?", (att_id,))
    conn.commit()
    return row[0]


# ---------------------------------------------------------------------------
# get_case_critical_deadline (Phase 4, kept here)
# ---------------------------------------------------------------------------

def get_case_critical_deadline(conn: sqlite3.Connection, case_id: int) -> Optional[str]:
    """Return deadline_date if any non-done deadline is within 3 days."""
    from datetime import date, timedelta
    cutoff = (date.today() + timedelta(days=3)).isoformat()
    today = date.today().isoformat()
    row = conn.execute(
        """SELECT deadline_date FROM deadlines
           WHERE case_id=? AND is_done=0
             AND deadline_date >= ? AND deadline_date <= ?
           ORDER BY deadline_date LIMIT 1""",
        (case_id, today, cutoff),
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Analytics (Phase 6)
# ---------------------------------------------------------------------------

def get_analytics_cases(conn: sqlite3.Connection) -> dict:
    from datetime import date as _date

    total = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(*) FROM cases WHERE status NOT IN ('Завершено','Прекращено')"
    ).fetchone()[0]

    outcomes: dict[str, int] = dict(
        conn.execute("SELECT COALESCE(outcome,'pending'), COUNT(*) FROM cases GROUP BY outcome").fetchall()
    )
    won     = outcomes.get("won", 0)
    lost    = outcomes.get("lost", 0)
    partial = outcomes.get("partial", 0)
    pending = outcomes.get("pending", 0)
    win_rate = round(won / (won + lost) * 100, 1) if (won + lost) > 0 else 0.0

    by_type   = dict(conn.execute(
        "SELECT case_type, COUNT(*) FROM cases WHERE case_type IS NOT NULL GROUP BY case_type ORDER BY 2 DESC"
    ).fetchall())
    by_source = dict(conn.execute("SELECT source, COUNT(*) FROM cases GROUP BY source").fetchall())

    # New / closed per month for last 12 months
    new_by_month: dict[str, int] = dict(conn.execute(
        """SELECT strftime('%Y-%m', created_at), COUNT(*)
           FROM cases WHERE created_at >= date('now','-12 months')
           GROUP BY 1 ORDER BY 1"""
    ).fetchall())
    closed_by_month: dict[str, int] = dict(conn.execute(
        """SELECT strftime('%Y-%m', updated_at), COUNT(*)
           FROM cases WHERE status IN ('Завершено','Прекращено')
             AND updated_at >= date('now','-12 months')
           GROUP BY 1 ORDER BY 1"""
    ).fetchall())

    by_month = []
    today = _date.today()
    for i in range(11, -1, -1):
        mo = today.month - i
        yr = today.year
        while mo <= 0:
            mo += 12
            yr -= 1
        m_str = f"{yr}-{mo:02d}"
        by_month.append({"month": m_str,
                          "new": new_by_month.get(m_str, 0),
                          "closed": closed_by_month.get(m_str, 0)})

    avg_row = conn.execute(
        """SELECT AVG(julianday(updated_at) - julianday(start_date))
           FROM cases WHERE status IN ('Завершено','Прекращено') AND start_date IS NOT NULL"""
    ).fetchone()[0]
    avg_duration = round(avg_row) if avg_row else None

    by_court = []
    for court, cnt, w, l in conn.execute(
        """SELECT court,
                  COUNT(*) AS total,
                  SUM(CASE WHEN outcome='won'  THEN 1 ELSE 0 END),
                  SUM(CASE WHEN outcome='lost' THEN 1 ELSE 0 END)
           FROM cases WHERE court IS NOT NULL
           GROUP BY court ORDER BY total DESC LIMIT 20"""
    ).fetchall():
        by_court.append({
            "court": court, "total": cnt, "won": w, "lost": l,
            "win_rate_pct": round(w / (w + l) * 100) if (w + l) > 0 else None,
        })

    return {
        "total_cases": total, "active_cases": active,
        "won": won, "lost": lost, "partial": partial, "pending": pending,
        "win_rate_pct": win_rate,
        "by_type": by_type, "by_source": by_source,
        "by_month": by_month,
        "avg_duration_days": avg_duration,
        "by_court": by_court,
    }


def get_analytics_judges(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT judge, court,
                  COUNT(*) AS total,
                  SUM(CASE WHEN outcome='won'  THEN 1 ELSE 0 END),
                  SUM(CASE WHEN outcome='lost' THEN 1 ELSE 0 END)
           FROM cases
           WHERE judge IS NOT NULL AND judge != ''
           GROUP BY judge, court
           HAVING total >= 2
           ORDER BY total DESC"""
    ).fetchall()
    result = []
    for judge, court, total, won, lost in rows:
        result.append({
            "judge": judge, "court": court or "—",
            "total_cases": total, "won": won, "lost": lost,
            "win_rate_pct": round(won / (won + lost) * 100) if (won + lost) > 0 else None,
        })
    return result


def get_analytics_finance(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COALESCE(SUM(claim_amount),0), COALESCE(SUM(awarded_amount),0), COALESCE(SUM(state_duty),0) FROM cases"
    ).fetchone()
    total_claim, total_awarded, total_duty = row

    cl_row = conn.execute(
        "SELECT COALESCE(SUM(fee_total),0), COALESCE(SUM(fee_paid),0) FROM clients"
    ).fetchone()
    fee_billed, fee_paid = cl_row

    by_client = []
    for row2 in conn.execute(
        """SELECT cl.name,
                  COUNT(c.id)                                AS active_cases,
                  COALESCE(SUM(c.claim_amount), 0)          AS claim_total,
                  COALESCE(cl.fee_total, 0)                 AS fee_total,
                  COALESCE(cl.fee_paid, 0)                  AS fee_paid
           FROM clients cl
           LEFT JOIN cases c ON c.client_id = cl.id
             AND (c.status IS NULL OR c.status NOT IN ('Завершено','Прекращено'))
           GROUP BY cl.id ORDER BY cl.name"""
    ).fetchall():
        by_client.append({
            "client_name": row2[0],
            "active_cases": row2[1],
            "claim_total":  row2[2],
            "fee_total":    row2[3],
            "fee_paid":     row2[4],
        })

    return {
        "total_claim_amount":  total_claim,
        "total_awarded":       total_awarded,
        "total_state_duty":    total_duty,
        "total_fee_billed":    fee_billed,
        "total_fee_paid":      fee_paid,
        "fee_receivable":      fee_billed - fee_paid,
        "by_client":           by_client,
    }


def get_dashboard_financials(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """SELECT COALESCE(SUM(claim_amount), 0)
           FROM cases WHERE status IS NULL OR status NOT IN ('Завершено','Прекращено')"""
    ).fetchone()
    total_active_claims = row[0]
    cl_row = conn.execute(
        "SELECT COALESCE(SUM(fee_total),0) - COALESCE(SUM(fee_paid),0) FROM clients"
    ).fetchone()
    return {
        "total_active_claims": total_active_claims,
        "fee_receivable":      cl_row[0],
    }


# ---------------------------------------------------------------------------
# Calendar (Phase 6)
# ---------------------------------------------------------------------------

def get_calendar_events(conn: sqlite3.Connection, year: int, month: int) -> dict:
    import calendar as _cal
    _, last_day = _cal.monthrange(year, month)
    start = f"{year}-{month:02d}-01"
    end   = f"{year}-{month:02d}-{last_day:02d}"

    days: dict[str, list[dict]] = {}

    for ev_date, ev_type, desc, is_fut, case_num, case_id in conn.execute(
        """SELECT e.event_date, e.event_type, e.description, e.is_future,
                  c.case_number, c.id
           FROM events e JOIN cases c ON c.id = e.case_id
           WHERE e.event_date BETWEEN ? AND ?
           ORDER BY e.event_date""",
        (start, end),
    ).fetchall():
        d = (ev_date or "")[:10]
        days.setdefault(d, []).append({
            "type":        "event",
            "event_type":  ev_type or "",
            "description": desc or "",
            "is_future":   bool(is_fut),
            "case_number": case_num,
            "case_id":     case_id,
        })

    for dl_date, dl_type, case_num, case_id in conn.execute(
        """SELECT d.deadline_date, d.deadline_type, c.case_number, c.id
           FROM deadlines d JOIN cases c ON c.id = d.case_id
           WHERE d.deadline_date BETWEEN ? AND ? AND d.is_done = 0
           ORDER BY d.deadline_date""",
        (start, end),
    ).fetchall():
        d = (dl_date or "")[:10]
        days.setdefault(d, []).append({
            "type":        "deadline",
            "event_type":  dl_type or "Срок",
            "description": "",
            "is_future":   False,
            "case_number": case_num,
            "case_id":     case_id,
        })

    return {"year": year, "month": month, "days": days}


# ---------------------------------------------------------------------------
# Export helpers (Phase 7)
# ---------------------------------------------------------------------------

def get_all_cases_for_export(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """SELECT c.*, cl.name AS client_name
           FROM cases c
           LEFT JOIN clients cl ON cl.id = c.client_id
           ORDER BY c.created_at DESC"""
    )
    return _rows(cur)


def get_future_events_for_export(conn: sqlite3.Connection) -> list[dict]:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    cur = conn.execute(
        """SELECT e.*, c.case_number, c.court
           FROM events e
           JOIN cases c ON c.id = e.case_id
           WHERE e.event_date >= ?
           ORDER BY e.event_date""",
        (today,),
    )
    return _rows(cur)


def get_open_deadlines_for_export(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """SELECT d.*, c.case_number, c.court
           FROM deadlines d
           JOIN cases c ON c.id = d.case_id
           WHERE d.is_done = 0
           ORDER BY d.deadline_date""",
    )
    return _rows(cur)


def get_notes_for_report(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM notes_and_tasks WHERE case_id=? ORDER BY position",
        (case_id,),
    )
    notes = _rows(cur)
    for note in notes:
        cur2 = conn.execute(
            "SELECT * FROM checklist_items WHERE note_id=? ORDER BY position",
            (note["id"],),
        )
        note["checklist"] = _rows(cur2)
    return notes


# ---------------------------------------------------------------------------
# SOY sync helpers (Phase 7)
# ---------------------------------------------------------------------------

def get_soy_cases_for_sync(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """SELECT * FROM cases
           WHERE source='soy' AND soy_scraping_enabled=1
             AND (soy_url_first IS NOT NULL OR soy_url_appeal IS NOT NULL
                  OR soy_url_cassation IS NOT NULL)"""
    )
    return _rows(cur)


def upsert_case_field(conn: sqlite3.Connection, case_id: int,
                      field: str, value) -> None:
    safe_fields = {
        "judge", "status", "court", "last_synced_at",
        "soy_scrape_status", "soy_scrape_last_ok",
        "soy_scrape_error_msg", "soy_scrape_attempts",
        "soy_scraping_enabled",
        # Phase 7.2 SOY extended fields
        "case_category", "case_category_path", "uid",
        "court_first", "receipt_date", "decision_date", "decision_result",
    }
    if field not in safe_fields:
        return
    conn.execute(f"UPDATE cases SET {field}=?, updated_at=datetime('now') WHERE id=?",
                 (value, case_id))
    conn.commit()


def update_soy_scrape_status(conn: sqlite3.Connection, case_id: int,
                              status: str, error_msg: Optional[str] = None) -> None:
    now = _now()
    if status == "success":
        conn.execute(
            """UPDATE cases SET soy_scrape_status=?, soy_scrape_last_ok=?,
               soy_scrape_error_msg=NULL, soy_scrape_attempts=0,
               last_synced_at=?, updated_at=? WHERE id=?""",
            (status, now, now, now, case_id),
        )
    else:
        conn.execute(
            """UPDATE cases SET soy_scrape_status=?,
               soy_scrape_error_msg=?,
               soy_scrape_attempts=soy_scrape_attempts+1,
               updated_at=? WHERE id=?""",
            (status, error_msg, now, case_id),
        )
    conn.commit()


def get_failed_soy_cases(conn: sqlite3.Connection, min_attempts: int = 5) -> list[dict]:
    cur = conn.execute(
        """SELECT c.id, c.case_number, c.soy_scrape_status, c.soy_scrape_attempts,
                  c.soy_scrape_error_msg
           FROM cases c
           WHERE c.source='soy' AND c.soy_scraping_enabled=1
             AND c.soy_scrape_attempts >= ?
           ORDER BY c.soy_scrape_attempts DESC""",
        (min_attempts,),
    )
    return _rows(cur)


# ---------------------------------------------------------------------------
# Notifications (Phase 7)
# ---------------------------------------------------------------------------

def create_notification(conn: sqlite3.Connection, case_id: Optional[int],
                        notif_type: str, message: str) -> int:
    cur = conn.execute(
        "INSERT INTO notifications(case_id, type, message) VALUES (?,?,?)",
        (case_id, notif_type, message),
    )
    conn.commit()
    return cur.lastrowid


def get_notifications(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    cur = conn.execute(
        """SELECT n.*, c.case_number FROM notifications n
           LEFT JOIN cases c ON c.id = n.case_id
           ORDER BY n.created_at DESC LIMIT ?""",
        (limit,),
    )
    return _rows(cur)


def mark_notification_read(conn: sqlite3.Connection, notif_id: int) -> None:
    conn.execute("UPDATE notifications SET is_read=1 WHERE id=?", (notif_id,))
    conn.commit()


def mark_all_notifications_read(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE notifications SET is_read=1")
    conn.commit()
