"""
Startup wrapper for Судебный Трекер.
Sets PLAYWRIGHT_BROWSERS_PATH, opens browser, runs Flask in a thread.
"""
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

# ── PyInstaller bundle setup ───────────────────────────────────────────────
# Must run BEFORE importing court_tracker / playwright.
if getattr(sys, "frozen", False):
    _base = Path(sys.executable).parent

    # Playwright Chromium bundled by PyInstaller (--onedir puts datas in
    # _internal next to the exe on PyInstaller 6+, or next to the exe on 5.x)
    for _pw_path in (_base / "playwright_browsers",
                     _base / "_internal" / "playwright_browsers"):
        if _pw_path.exists():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_pw_path)
            os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
            break

    # Inside the Electron package templates/static live in
    # resources/court_tracker/, one level up from python_core/
    _resources = os.environ.get("SUD_RESOURCES_PATH", "")
    if _resources:
        os.environ["SUD_TEMPLATES_PATH"] = str(Path(_resources) / "court_tracker" / "templates")
        os.environ["SUD_STATIC_PATH"] = str(Path(_resources) / "court_tracker" / "static")
else:
    _base = Path(__file__).resolve().parent.parent
    # Running as 'python build/start.py' — sys.path[0] is build/, so the
    # court_tracker package next to it is not importable without this:
    if str(_base) not in sys.path:
        sys.path.insert(0, str(_base))
    _pw_path = _base / "playwright_browsers"
    if _pw_path.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_pw_path))
        os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")

# ── Flask app ──────────────────────────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 5000
URL  = f"http://{HOST}:{PORT}"


def _run_flask() -> None:
    from court_tracker.app import create_app
    from court_tracker.config import FLASK_HOST, FLASK_PORT
    flask_app = create_app()
    flask_app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    t = threading.Thread(target=_run_flask, daemon=True)
    t.start()

    # Wait until Flask is ready
    import urllib.request
    for _ in range(20):
        try:
            urllib.request.urlopen(URL, timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    # Electron sets SUD_NO_BROWSER=1 — it shows its own window instead
    if not os.environ.get("SUD_NO_BROWSER"):
        webbrowser.open(URL)
    print(f"Судебный Трекер запущен: {URL}")
    print("Нажмите Ctrl+C для остановки.")
    try:
        t.join()
    except KeyboardInterrupt:
        print("Завершение работы.")
        sys.exit(0)
