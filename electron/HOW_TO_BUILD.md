# Как собрать инсталлятор «Судебный Трекер»

## Требования

1. **Node.js 18+** — https://nodejs.org
2. **Python 3.11+** — https://python.org (галочка «Add to PATH» при установке)
3. **Git** — https://git-scm.com

## Сборка

```bat
cd electron
build_electron.bat
```

Скрипт выполнит четыре шага:

1. Установит Python-зависимости (`requirements.txt` + PyInstaller)
2. Загрузит браузер Chromium для Playwright
3. Соберёт Python-ядро приложения через PyInstaller (`--onedir --noconsole`)
4. Соберёт установщик Windows через electron-builder (NSIS)

## Результат

```
electron/dist/Судебный Трекер Setup.exe
```

Этот `.exe` устанавливает полное приложение — Python на целевой машине
**не требуется**: интерпретатор и все зависимости упакованы внутрь.

## Запуск в режиме разработки (без сборки)

```bat
cd electron
npm install
npm start
```

Горячие клавиши: **F5** — обновить страницу, **Ctrl+Shift+D** — DevTools.
