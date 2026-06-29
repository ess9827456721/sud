"""Autosave helpers. Called by Flask POST /api/autosave endpoint (Phase 2)."""
import sqlite3
import threading
import logging
from pathlib import Path

from court_tracker.config import DB_PATH

logger = logging.getLogger(__name__)

_local = threading.local()

# ---------------------------------------------------------------------------
# Whitelisted fields per table (security: prevent arbitrary column injection)
# ---------------------------------------------------------------------------

WHITELISTED_FIELDS: dict[str, set[str]] = {
    "cases": {
        "court", "judge", "status", "case_type", "start_date",
        "kad_url", "soy_url_first", "soy_url_appeal", "soy_url_cassation",
        "custom_label", "custom_status", "client_id",
        "soy_scraping_enabled", "soy_scrape_status",
    },
    "clients": {
        "type", "name", "short_name", "inn", "ogrn", "address",
        "status_egrul", "phone", "email", "contact_person", "notes",
    },
    "client_contacts": {"name", "role", "phone", "email", "notes"},
    "client_powers_of_attorney": {
        "number", "issue_date", "expiry_date", "scope_description",
    },
    "notes_and_tasks": {
        "title", "body", "color", "item_type", "task_status",
        "task_due_date", "task_priority", "position",
    },
    "checklist_items": {"text", "checked", "position"},
    "deadlines": {
        "deadline_type", "deadline_date", "trigger_date", "trigger_event",
        "calculation_basis", "statute_reference", "statute_article",
        "calculation_note", "is_auto_calculated", "is_manual_override", "is_done",
    },
    "kanban_stage": {"stage", "notes"},
    "settings": {"value"},
    "participants": {"role", "name", "inn", "address"},
    "events": {"event_date", "event_type", "description", "document_url", "is_future"},
}

# Tables that have an updated_at column
_HAS_UPDATED_AT = {
    "cases", "clients", "notes_and_tasks",
}


def get_db() -> sqlite3.Connection:
    """Return a thread-local WAL-mode SQLite connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn


def autosave_field(table: str, record_id: int, field_name: str, value) -> bool:
    """
    Update a single whitelisted field in the DB for the given record.
    Returns True on success, False if the field is not whitelisted or update fails.
    """
    allowed = WHITELISTED_FIELDS.get(table)
    if allowed is None or field_name not in allowed:
        logger.warning("autosave blocked: %s.%s not whitelisted", table, field_name)
        return False

    try:
        conn = get_db()
        if table in _HAS_UPDATED_AT:
            sql = (
                f"UPDATE {table} SET {field_name} = ?, updated_at = datetime('now') "
                f"WHERE id = ?"
            )
        else:
            sql = f"UPDATE {table} SET {field_name} = ? WHERE id = ?"

        cur = conn.execute(sql, (value, record_id))
        conn.commit()
        if cur.rowcount == 0:
            logger.warning("autosave: no row updated in %s id=%s", table, record_id)
            return False
        return True
    except Exception as exc:
        logger.error("autosave_field error: %s", exc)
        return False
