import sqlite3
import logging

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS clients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT NOT NULL CHECK(type IN ('legal','ip','person')),
    name            TEXT NOT NULL,
    short_name      TEXT,
    inn             TEXT,
    ogrn            TEXT,
    address         TEXT,
    status_egrul    TEXT,
    phone           TEXT,
    email           TEXT,
    contact_person  TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cases (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    source                  TEXT NOT NULL CHECK(source IN ('kad','soy')),
    case_number             TEXT NOT NULL UNIQUE,
    case_id_kad             TEXT,
    court                   TEXT,
    judge                   TEXT,
    status                  TEXT,
    case_type               TEXT,
    start_date              TEXT,
    kad_url                 TEXT,
    soy_url_first           TEXT,
    soy_url_appeal          TEXT,
    soy_url_cassation       TEXT,
    custom_label            TEXT,
    custom_status           TEXT,
    client_id               INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    last_synced_at          TEXT,
    soy_scraping_enabled    INTEGER NOT NULL DEFAULT 0,
    soy_scrape_status       TEXT NOT NULL DEFAULT 'pending',
    soy_scrape_last_ok      TEXT,
    soy_scrape_error_msg    TEXT,
    soy_scrape_attempts     INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS client_contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name        TEXT,
    role        TEXT,
    phone       TEXT,
    email       TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS client_powers_of_attorney (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    case_id             INTEGER REFERENCES cases(id) ON DELETE SET NULL,
    number              TEXT,
    issue_date          TEXT,
    expiry_date         TEXT,
    scope_description   TEXT,
    attachment_id       INTEGER,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS participants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    role        TEXT,
    name        TEXT,
    inn         TEXT,
    address     TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id         INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    event_date      TEXT,
    event_type      TEXT,
    description     TEXT,
    document_url    TEXT,
    is_future       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deadlines (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id                 INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    deadline_type           TEXT,
    deadline_date           TEXT,
    trigger_date            TEXT,
    trigger_event           TEXT,
    calculation_basis       TEXT,
    statute_reference       TEXT,
    statute_article         TEXT,
    calculation_note        TEXT,
    is_auto_calculated      INTEGER NOT NULL DEFAULT 1,
    is_manual_override      INTEGER NOT NULL DEFAULT 0,
    is_done                 INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notes_and_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id         INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    title           TEXT,
    body            TEXT,
    color           TEXT,
    item_type       TEXT NOT NULL DEFAULT 'note' CHECK(item_type IN ('note','task')),
    task_status     TEXT DEFAULT 'new' CHECK(task_status IN ('new','in_progress','done','overdue')),
    task_due_date   TEXT,
    task_priority   TEXT DEFAULT 'medium' CHECK(task_priority IN ('low','medium','high')),
    position        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS checklist_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id     INTEGER NOT NULL REFERENCES notes_and_tasks(id) ON DELETE CASCADE,
    text        TEXT,
    checked     INTEGER NOT NULL DEFAULT 0,
    position    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS note_tags (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes_and_tasks(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    note_id     INTEGER REFERENCES notes_and_tasks(id) ON DELETE SET NULL,
    filename    TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    file_size   INTEGER,
    mime_type   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kanban_stage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     INTEGER NOT NULL UNIQUE REFERENCES cases(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL DEFAULT 'first'
                    CHECK(stage IN ('pretension','first','appeal','cassation','supreme','execution','archive')),
    moved_at    TEXT NOT NULL DEFAULT (datetime('now')),
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

CREATE TABLE IF NOT EXISTS time_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id             INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    client_id           INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    started_at          TEXT,
    ended_at            TEXT,
    duration_minutes    INTEGER,
    category            TEXT,
    description         TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     INTEGER REFERENCES cases(id) ON DELETE SET NULL,
    synced_at   TEXT NOT NULL DEFAULT (datetime('now')),
    success     INTEGER NOT NULL DEFAULT 0,
    message     TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     INTEGER REFERENCES cases(id) ON DELETE SET NULL,
    type        TEXT,
    message     TEXT,
    is_read     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

DEFAULT_SETTINGS = [
    ('sync_interval_hours', '2'),
    ('sync_on_startup', '1'),
    ('theme', 'light'),
    ('app_version', '2.0'),
]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    for key, value in DEFAULT_SETTINGS:
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()
    logger.info("Database schema initialised (WAL mode enabled).")


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply any schema additions that may be missing in older DBs."""
    # ── cases ──────────────────────────────────────────────────────────────
    cases_cols = {row[1] for row in conn.execute("PRAGMA table_info(cases)").fetchall()}
    cases_migrations = [
        ("soy_scraping_enabled",  "ALTER TABLE cases ADD COLUMN soy_scraping_enabled  INTEGER NOT NULL DEFAULT 0"),
        ("soy_scrape_status",     "ALTER TABLE cases ADD COLUMN soy_scrape_status      TEXT NOT NULL DEFAULT 'pending'"),
        ("soy_scrape_last_ok",    "ALTER TABLE cases ADD COLUMN soy_scrape_last_ok     TEXT"),
        ("soy_scrape_error_msg",  "ALTER TABLE cases ADD COLUMN soy_scrape_error_msg   TEXT"),
        ("soy_scrape_attempts",   "ALTER TABLE cases ADD COLUMN soy_scrape_attempts    INTEGER NOT NULL DEFAULT 0"),
        # Phase 6 financial fields
        ("claim_amount",          "ALTER TABLE cases ADD COLUMN claim_amount          REAL"),
        ("awarded_amount",        "ALTER TABLE cases ADD COLUMN awarded_amount        REAL"),
        ("state_duty",            "ALTER TABLE cases ADD COLUMN state_duty            REAL"),
        ("legal_costs_claimed",   "ALTER TABLE cases ADD COLUMN legal_costs_claimed   REAL"),
        ("outcome",               "ALTER TABLE cases ADD COLUMN outcome               TEXT"),
    ]
    for col, sql in cases_migrations:
        if col not in cases_cols:
            conn.execute(sql)
            logger.info("Migration applied: added column cases.%s", col)

    # ── clients ─────────────────────────────────────────────────────────────
    client_cols = {row[1] for row in conn.execute("PRAGMA table_info(clients)").fetchall()}
    client_migrations = [
        ("contract_number", "ALTER TABLE clients ADD COLUMN contract_number TEXT"),
        ("contract_date",   "ALTER TABLE clients ADD COLUMN contract_date   TEXT"),
        ("fee_total",       "ALTER TABLE clients ADD COLUMN fee_total       REAL"),
        ("fee_paid",        "ALTER TABLE clients ADD COLUMN fee_paid        REAL"),
    ]
    for col, sql in client_migrations:
        if col not in client_cols:
            conn.execute(sql)
            logger.info("Migration applied: added column clients.%s", col)

    conn.commit()
