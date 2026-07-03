@echo off
setlocal
echo === Публикация релиза «Судебный Трекер» ===

if "%GH_TOKEN%"=="" (
  echo ERROR: переменная окружения GH_TOKEN не установлена.
  echo Создайте токен на https://github.com/settings/tokens (scope: repo)
  echo и выполните:  set GH_TOKEN=ghp_...
  exit /b 1
)

for /f "usebackq delims=" %%V in (`node -p "require('./electron/package.json').version"`) do set APPVER=%%V
echo Версия из electron/package.json: %APPVER%

echo [1/3] Сборка Python-ядра + Chromium (PyInstaller)...
cd electron
call build_electron.bat
if errorlevel 1 (
  echo === СБОРКА НЕ УДАЛАСЬ ===
  exit /b 1
)

echo [2/3] Публикация через electron-builder --publish always...
call npx electron-builder --publish always
if errorlevel 1 (
  echo === ПУБЛИКАЦИЯ НЕ УДАЛАСЬ ===
  exit /b 1
)
cd ..

echo [3/3] Готово!
echo Релиз: https://github.com/ess9827456721/sud/releases/tag/v%APPVER%
echo Проверьте, что в релизе есть: Setup.exe, latest.yml, .blockmap
