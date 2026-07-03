@echo off
echo === Building Судебный Трекер ===

echo [1/4] Installing Python dependencies...
python -m pip install pyinstaller -q
python -m pip install -r ../requirements.txt -q

echo [2/4] Installing Playwright browsers...
playwright install chromium

echo [3/4] Building Python bundle with PyInstaller...
cd ..
pyinstaller --onedir --noconsole --name СудебныйТрекер_core ^
  --add-data "court_tracker/templates;court_tracker/templates" ^
  --add-data "court_tracker/static;court_tracker/static" ^
  --hidden-import flask ^
  --hidden-import playwright ^
  build/start.py
cd electron

echo [4/4] Building Electron installer...
call npm install
call npm run build

echo === Done! Installer: dist/Судебный Трекер Setup.exe ===
