"""Application-wide configuration."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Base directory: project root (one level up from this file)
BASE_DIR = Path(__file__).parent


def get_data_dir() -> Path:
    """
    User data lives OUTSIDE the install dir (%APPDATA%\\SudTracker on Windows,
    ~/SudTracker elsewhere) — auto-update replaces the app folder, and the DB
    must survive it. SUD_DATA_DIR env var overrides for tests/portable mode.
    """
    override = os.environ.get("SUD_DATA_DIR")
    if override:
        d = Path(override)
    else:
        base = os.environ.get("APPDATA") or str(Path.home())
        d = Path(base) / "SudTracker"
    d.mkdir(parents=True, exist_ok=True)
    return d


DATA_DIR = get_data_dir()

DB_PATH = DATA_DIR / "court_tracker.db"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
ATTACHMENTS_DIR.mkdir(exist_ok=True)

DEBUG_DIR = DATA_DIR / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

# One-time migration: copy a legacy DB from the old in-app data folder
_LEGACY_DB = BASE_DIR / "data" / "court_tracker.db"
if not DB_PATH.exists() and _LEGACY_DB.exists():
    import shutil
    shutil.copy2(_LEGACY_DB, DB_PATH)
    print(f"[config] Legacy DB migrated: {_LEGACY_DB} -> {DB_PATH}")

# Flask
FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"

# Scraper
HEADLESS = os.getenv("HEADLESS", "1") == "1"

# Attachments
ALLOWED_EXTENSIONS = {
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'txt', 'rtf',
    'jpg', 'jpeg', 'png', 'gif', 'bmp',
    'zip', 'rar', '7z', 'odt', 'ods',
}
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "20"))

# Document templates
TEMPLATES_DIR = DATA_DIR / "templates"
TEMPLATES_DIR.mkdir(exist_ok=True)
TEMP_DIR = DATA_DIR / "temp"
TEMP_DIR.mkdir(exist_ok=True)

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
