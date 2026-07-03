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

Скрипт выполнит шесть шагов:

1. Сгенерирует иконки 256×256 (`create_icons.py` — требование electron-builder)
2. Установит Python-зависимости (`requirements.txt` + PyInstaller)
3. Загрузит браузер Chromium для Playwright
4. Скопирует Chromium из `%USERPROFILE%\AppData\Local\ms-playwright` в `playwright_browsers/`
5. Соберёт Python-ядро через PyInstaller (`--onedir --noconsole`, с Chromium внутри)
6. Соберёт установщик Windows через electron-builder (NSIS); Python-ядро попадает
   в пакет как ресурс `python_core/`

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
