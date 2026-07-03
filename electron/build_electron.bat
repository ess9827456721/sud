@echo off
setlocal
echo === Building Судебный Трекер ===
cd ..

echo [1/6] Generating 256x256 icons...
python electron\create_icons.py
if errorlevel 1 goto :fail

echo [2/6] Installing Python dependencies...
python -m pip install pyinstaller -q
python -m pip install -r requirements.txt -q
if errorlevel 1 goto :fail

echo [3/6] Installing Playwright Chromium...
playwright install chromium

echo [4/6] Copying Playwright Chromium into project...
set PWBROWSERS=%USERPROFILE%\AppData\Local\ms-playwright
if exist "%PWBROWSERS%" (
  xcopy /E /I /Q /Y "%PWBROWSERS%" playwright_browsers\ >nul
  echo Chromium copied to playwright_browsers\
) else (
  echo WARNING: Playwright browsers not found at %PWBROWSERS%
  echo Run: playwright install chromium
)

echo [5/6] Building Python bundle with PyInstaller...
pyinstaller --onedir --noconsole --noconfirm --name СудебныйТрекер_core ^
  --add-data "court_tracker/templates;court_tracker/templates" ^
  --add-data "court_tracker/static;court_tracker/static" ^
  --add-data "playwright_browsers;playwright_browsers" ^
  --hidden-import flask ^
  --hidden-import playwright ^
  --hidden-import playwright.sync_api ^
  build\start.py
if errorlevel 1 goto :fail

echo [6/6] Building Electron installer...
cd electron
call npm install
call npm run build
if errorlevel 1 goto :fail

echo.
echo === Done! ===
for %%F in ("dist\*Setup*.exe") do echo Installer: %%~fF
goto :eof

:fail
echo.
echo === BUILD FAILED — see errors above ===
cd /d "%~dp0"
exit /b 1
