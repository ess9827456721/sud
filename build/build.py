"""
Build script for Судебный Трекер.
Generates PyInstaller spec, runs PyInstaller, then copies Playwright browsers.

Run on Windows:
    python build/build.py

Requirements:
    pip install pyinstaller
    playwright install chromium
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST_DIR   = ROOT / "dist"
BUILD_DIR  = ROOT / "build"
APP_NAME   = "СудебныйТрекер"
ICON_PATH  = ROOT / "court_tracker" / "static" / "icon.ico"
SPEC_PATH  = BUILD_DIR / "court_tracker.spec"


def _ensure_icon() -> str:
    """Return icon path string; create a minimal .ico if missing."""
    if ICON_PATH.exists():
        return str(ICON_PATH)
    # Create a tiny 16x16 blue square ICO (minimal valid ICO header + BMP)
    import struct
    ico_data = (
        # ICO header: reserved=0, type=1 (icon), count=1
        b"\x00\x00\x01\x00\x01\x00"
        # Image directory entry: w=16 h=16 colorCount=0 reserved=0
        # planes=1 bitCount=32 bytesInRes=40+16*16*4 imageOffset=22
        b"\x10\x10\x00\x00\x01\x00\x20\x00"
        + struct.pack("<I", 40 + 16 * 16 * 4)
        + struct.pack("<I", 22)
        # BITMAPINFOHEADER
        + struct.pack("<IIIHHIIIIII", 40, 16, 32, 1, 32, 0,
                      16 * 16 * 4, 0, 0, 0, 0)
        # 16×16 BGRA pixels (solid blue #2B4EFF = R43 G78 B255)
        + b"\xFF\x4E\x2B\xFF" * (16 * 16)
    )
    ICON_PATH.write_bytes(ico_data)
    print(f"Created placeholder icon: {ICON_PATH}")
    return str(ICON_PATH)


def _write_spec(icon: str) -> None:
    templates_src = str(ROOT / "court_tracker" / "templates")
    static_src    = str(ROOT / "court_tracker" / "static")
    spec = f"""# -*- mode: python ; coding: utf-8 -*-
block_cipher = None

a = Analysis(
    [r'{ROOT / "build" / "start.py"}'],
    pathex=[r'{ROOT}'],
    binaries=[],
    datas=[
        (r'{templates_src}', 'court_tracker/templates'),
        (r'{static_src}',    'court_tracker/static'),
    ],
    hiddenimports=[
        'flask', 'flask.templating', 'flask.json',
        'jinja2', 'werkzeug', 'werkzeug.serving',
        'sqlite3', 'openpyxl', 'docx', 'docx.oxml',
        'playwright', 'playwright.sync_api',
        'court_tracker', 'court_tracker.app',
        'court_tracker.config', 'court_tracker.db.schema',
        'court_tracker.db.queries', 'court_tracker.services.autosave',
        'court_tracker.services.deadline_service',
        'court_tracker.services.egrul_service',
        'court_tracker.services.notification_service',
        'court_tracker.scraper.kad_scraper',
        'court_tracker.scraper.soy_scraper',
        'court_tracker.scraper.scheduler',
    ],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='{APP_NAME}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=r'{icon}',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='{APP_NAME}',
)
"""
    SPEC_PATH.write_text(spec, encoding="utf-8")
    print(f"Spec written: {SPEC_PATH}")


def _run_pyinstaller() -> None:
    print("Running PyInstaller…")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", str(SPEC_PATH)],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        print("PyInstaller failed.")
        sys.exit(1)
    print("PyInstaller succeeded.")


def _copy_playwright_browsers() -> None:
    """Copy Playwright Chromium into the dist folder."""
    import shutil as _sh

    # Find installed playwright browsers path
    try:
        import playwright
        pw_root = Path(playwright.__file__).parent / "driver" / "package" / ".local-browsers"
        if not pw_root.exists():
            # Try env var
            pw_root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH",
                                           Path.home() / "AppData/Local/ms-playwright"))
    except ImportError:
        pw_root = Path.home() / "AppData/Local/ms-playwright"

    dest = DIST_DIR / APP_NAME / "playwright_browsers"
    dest.mkdir(parents=True, exist_ok=True)

    if pw_root.exists():
        chromium_dirs = list(pw_root.glob("chromium-*"))
        if chromium_dirs:
            src = chromium_dirs[0]
            target = dest / src.name
            if not target.exists():
                print(f"Copying {src} → {target} …")
                _sh.copytree(str(src), str(target))
            else:
                print(f"Playwright chromium already at {target}")
        else:
            print(f"WARNING: No chromium-* dir in {pw_root}")
    else:
        print(f"WARNING: Playwright browsers not found at {pw_root}")
        print("Run: playwright install chromium")


if __name__ == "__main__":
    print("=== Судебный Трекер build ===")
    icon = _ensure_icon()
    _write_spec(icon)
    _run_pyinstaller()
    _copy_playwright_browsers()
    print(f"\nBuild complete. Output: {DIST_DIR / APP_NAME}")
    print("Next: run Inno Setup on build/installer.iss")
