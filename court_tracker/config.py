"""Application-wide configuration."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Base directory: project root (one level up from this file)
BASE_DIR = Path(__file__).parent

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "court_tracker.db"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
ATTACHMENTS_DIR.mkdir(exist_ok=True)

# Flask
FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"

# Scraper
HEADLESS = os.getenv("HEADLESS", "1") == "1"

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
