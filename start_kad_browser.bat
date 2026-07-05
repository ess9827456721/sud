@echo off
rem ============================================================
rem  Launch Chrome for KAD search in CDP-attach mode.
rem  kad.arbitr.ru (DDoS-Guard) disables search in an
rem  automation-launched browser. This starts YOUR real Chrome
rem  with a debugging port so the app can attach to it.
rem
rem  Steps:
rem    1. Close ALL Chrome windows first.
rem    2. Run this file.
rem    3. In the app: Settings -> "KAD CDP" field -> enter 9222.
rem  Only pure ASCII / English here (see CLAUDE.md).
rem ============================================================
setlocal
set PORT=9222
set PROFILE=%APPDATA%\SudTracker\kad_profile

set CHROME=
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe

if "%CHROME%"=="" (
    echo Chrome not found. Install Google Chrome or edit CHROME path in this file.
    pause
    exit /b 1
)

echo Starting Chrome with remote debugging on port %PORT%...
echo Profile: %PROFILE%
echo.
echo Leave this window open. In the app Settings, set the KAD CDP field to %PORT%.
echo.
start "" "%CHROME%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" https://kad.arbitr.ru/
endlocal
