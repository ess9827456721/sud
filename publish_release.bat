@echo off
setlocal
echo === Publishing Sud Tracker release ===

if "%GH_TOKEN%"=="" (
  echo ERROR: GH_TOKEN environment variable is not set.
  echo Create a token at https://github.com/settings/tokens with scope repo
  echo and run:  set GH_TOKEN=ghp_...
  exit /b 1
)

for /f "usebackq delims=" %%V in (`node -p "require('./electron/package.json').version"`) do set APPVER=%%V
echo Version from electron/package.json: %APPVER%

echo [1/3] Building Python core + Chromium ^(PyInstaller^)...
cd electron
call build_electron.bat
if errorlevel 1 (
  echo === BUILD FAILED ===
  exit /b 1
)

echo [2/3] Publishing via electron-builder --publish always...
cd /d "%~dp0electron"
call npx electron-builder --publish always
if errorlevel 1 (
  echo === PUBLISH FAILED ===
  exit /b 1
)
cd ..

echo [3/3] Done!
echo Release: https://github.com/ess9827456721/sud/releases/tag/v%APPVER%
echo Verify the release contains: Setup.exe, latest.yml, .blockmap
