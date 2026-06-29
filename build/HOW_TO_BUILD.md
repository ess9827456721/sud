# Сборка Судебного Трекера в Windows-инсталлятор

## Требования

- Windows 10/11 x64
- Python 3.11+
- Inno Setup 6.x — https://jrsoftware.org/isdl.php

## Шаги

### 1. Установить зависимости

```
pip install -r requirements.txt
pip install pyinstaller
playwright install chromium
```

### 2. Собрать .exe

```
python build/build.py
```

Скрипт:
- Генерирует `build/court_tracker.spec`
- Запускает PyInstaller → создаёт `dist/СудебныйТрекер/`
- Копирует Playwright Chromium в `dist/СудебныйТрекер/playwright_browsers/`

### 3. Создать инсталлятор

1. Откройте Inno Setup Compiler
2. Откройте файл `build/installer.iss`
3. Нажмите **Build → Compile**
4. Результат: `dist/SetupСудебныйТрекер.exe`

## Структура dist/

```
dist/СудебныйТрекер/
  СудебныйТрекер.exe          ← точка входа
  court_tracker/
    templates/                ← Jinja2 шаблоны
    static/                   ← CSS, JS, иконки
  playwright_browsers/
    chromium-XXXX/            ← браузер для парсинга
  data/                       ← создаётся при первом запуске
    court_tracker.db
    attachments/
    templates/
```

## Что делает инсталлятор

- Устанавливает в `%ProgramFiles%\Судебный Трекер\`
- Создаёт иконку на рабочем столе (опционально)
- Добавляет пункт в Пуск
- При запуске открывает `http://localhost:5000` в браузере по умолчанию

## Важно

- Данные хранятся в `data/` рядом с `.exe` — не удалять при обновлении
- Файл `data/court_tracker.db` — вся база данных
- Резервная копия: в приложении → Настройки → «Создать резервную копию»
