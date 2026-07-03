/**
 * Electron main process for Судебный Трекер.
 * Starts Flask as a child process, shows the app in a native window,
 * manages system tray and native Windows notifications.
 */
const { app, BrowserWindow, Tray, Menu, nativeImage,
        ipcMain, Notification, shell, dialog, globalShortcut } = require('electron');
const path   = require('path');
const os     = require('os');
const { spawn } = require('child_process');
const http   = require('http');
const fs     = require('fs');

// ── User data dir (must match court_tracker/config.get_data_dir) ───────────
function getUserDataDir() {
  const base = process.env.APPDATA || os.homedir();
  const dir = path.join(base, 'SudTracker');
  try { fs.mkdirSync(dir, { recursive: true }); } catch (_) {}
  return dir;
}

function readAppSettings() {
  try {
    return JSON.parse(fs.readFileSync(path.join(getUserDataDir(), 'settings.json'), 'utf8'));
  } catch (_) {
    return {};
  }
}

function writeAppSettings(patch) {
  const cur = readAppSettings();
  const next = { ...cur, ...patch };
  try {
    fs.writeFileSync(path.join(getUserDataDir(), 'settings.json'),
                     JSON.stringify(next, null, 2));
  } catch (_) {}
  return next;
}

function updaterLog(msg) {
  try {
    const logDir = path.join(getUserDataDir(), 'logs');
    fs.mkdirSync(logDir, { recursive: true });
    fs.appendFileSync(path.join(logDir, 'updater.log'),
                      `[${new Date().toISOString()}] ${msg}\n`);
  } catch (_) {}
}

// ── Config ─────────────────────────────────────────────────────────────────
const PORT       = 5000;
const HOST       = '127.0.0.1';
const APP_URL    = `http://${HOST}:${PORT}`;
const MAX_WAIT_S = 30;

// ── State ──────────────────────────────────────────────────────────────────
let mainWindow = null;
let tray       = null;
let flaskProc  = null;
let isQuitting = false;

// ── Python / Flask startup ─────────────────────────────────────────────────

const isDev = process.argv.includes('--dev') || !app.isPackaged;

function getPythonPath() {
  if (!isDev && app.isPackaged) {
    // PyInstaller bundle: the .exe IS the Python runtime
    const exeName = process.platform === 'win32'
      ? 'SudTracker_core.exe'
      : 'SudTracker_core';
    const bundled = path.join(process.resourcesPath, 'python_core', exeName);
    if (fs.existsSync(bundled)) return bundled;
    // Fallback: look for any executable in python_core/
    const coreDir = path.join(process.resourcesPath, 'python_core');
    if (fs.existsSync(coreDir)) {
      const files = fs.readdirSync(coreDir).filter(f => f.endsWith('.exe'));
      if (files.length > 0) return path.join(coreDir, files[0]);
    }
  }
  // Dev mode: use system python
  return process.platform === 'win32' ? 'python' : 'python3';
}

function getStartScript() {
  return path.join(__dirname, '..', 'build', 'start.py');
}

function startFlask() {
  const python = getPythonPath();
  const isBundle = app.isPackaged && python.endsWith('.exe')
                   && !python.includes('python.exe');
  // PyInstaller bundle: run the exe directly (it IS the start script).
  // System python: run 'python build/start.py'.
  const args = isBundle ? [] : [getStartScript()];
  const cwd  = app.isPackaged ? process.resourcesPath
                              : path.join(__dirname, '..');

  flaskProc = spawn(python, args, {
    cwd,
    env: {
      ...process.env,
      FLASK_DEBUG: '0',
      SUD_NO_BROWSER: '1',
      // Tell the PyInstaller bundle where templates/static live
      SUD_RESOURCES_PATH: app.isPackaged ? process.resourcesPath : '',
    },
    windowsHide: true,
  });

  flaskProc.stdout.on('data', d => {
    const msg = d.toString().trim();
    if (msg) console.log('[Flask]', msg);
  });

  flaskProc.stderr.on('data', d => {
    const msg = d.toString().trim();
    if (msg) console.error('[Flask ERR]', msg);
    if (mainWindow && msg.includes('ERROR')) {
      mainWindow.webContents.send('flask-error', msg);
    }
  });

  flaskProc.on('exit', code => {
    console.log('[Flask] exited with code', code);
    if (!isQuitting && code !== 0) {
      showErrorNotification(
        'Flask завершился неожиданно (код ' + code + '). Перезапустите приложение.'
      );
    }
  });
}

function waitForFlask(callback) {
  let attempts = 0;
  function ping() {
    http.get(APP_URL, res => {
      if (res.statusCode < 500) {
        callback(null);
      } else {
        retry();
      }
      res.resume();
    }).on('error', retry);
  }
  function retry() {
    attempts++;
    if (attempts >= MAX_WAIT_S * 2) {
      callback(new Error('Flask не запустился за ' + MAX_WAIT_S + ' секунд'));
    } else {
      setTimeout(ping, 500);
    }
  }
  ping();
}

// ── Window ─────────────────────────────────────────────────────────────────

function createWindow() {
  const iconPath = path.join(__dirname, 'assets', 'icon.ico');
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    title: 'Судебный Трекер',
    icon: fs.existsSync(iconPath) ? iconPath : undefined,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
    backgroundColor: '#F7FAFC',
  });

  // Inject error overlay + renderer helpers into every page after load
  mainWindow.webContents.on('did-finish-load', () => {
    for (const file of ['error_overlay.js', 'renderer.js']) {
      try {
        const script = fs.readFileSync(path.join(__dirname, file), 'utf8');
        mainWindow.webContents.executeJavaScript(script).catch(() => {});
      } catch (e) {
        console.error('Could not inject ' + file + ':', e.message);
      }
    }
  });

  // Open external links in system browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(APP_URL)) {
      shell.openExternal(url);
    }
    return { action: 'deny' };
  });

  // Minimise to tray on close
  mainWindow.on('close', e => {
    if (!isQuitting) {
      e.preventDefault();
      mainWindow.hide();
      if (tray && tray.displayBalloon) {
        tray.displayBalloon({
          title: 'Судебный Трекер',
          content: 'Приложение свёрнуто в трей. Щёлкните иконку для открытия.',
          iconType: 'info',
        });
      }
    }
  });
}

// ── Tray ───────────────────────────────────────────────────────────────────

function createTray() {
  const trayIconPath = path.join(__dirname, 'assets', 'tray.ico');
  const fallbackPath = path.join(__dirname, 'assets', 'icon.ico');
  const iconPath = fs.existsSync(trayIconPath) ? trayIconPath : fallbackPath;

  try {
    tray = new Tray(iconPath);
  } catch (e) {
    // nativeImage fallback (empty 16x16) — won't look great but won't crash
    tray = new Tray(nativeImage.createEmpty());
  }

  tray.setToolTip('Судебный Трекер');

  const menu = Menu.buildFromTemplate([
    { label: 'Открыть', click: () => { mainWindow.show(); mainWindow.focus(); } },
    { type: 'separator' },
    { label: 'Синхронизировать сейчас', click: triggerSync },
    { label: 'Проверить обновления', click: () => checkForUpdates(true) },
    { type: 'separator' },
    { label: 'Выход', click: () => { isQuitting = true; app.quit(); } },
  ]);
  tray.setContextMenu(menu);
  tray.on('double-click', () => { mainWindow.show(); mainWindow.focus(); });
}

// ── Sync from tray ──────────────────────────────────────────────────────────

function triggerSync() {
  const req = http.request(
    { host: HOST, port: PORT, path: '/api/sync/trigger', method: 'POST' },
    res => {
      res.resume();
      if (res.statusCode < 300) {
        showInfoNotification('Синхронизация', 'Синхронизация запущена.');
      }
    }
  );
  req.on('error', () => {});
  req.end();
}

// ── Sync-result poller ──────────────────────────────────────────────────────
// scheduler.py writes <user-data-dir>/last_sync_result.json after each
// sync run; poll it every 60s and raise a native notification if fresh.

function pollSyncResult() {
  // scheduler.py writes into the user data dir (%APPDATA%\SudTracker)
  const notifyFile = path.join(getUserDataDir(), 'last_sync_result.json');
  setInterval(() => {
    try {
      const raw  = fs.readFileSync(notifyFile, 'utf8');
      const data = JSON.parse(raw);
      const ts   = new Date(data.timestamp).getTime();
      if (Date.now() - ts < 70000) {  // result is fresh (< 70s old)
        if (data.new_events > 0) {
          showInfoNotification(
            'Синхронизация завершена',
            `Новых событий: ${data.new_events}` +
            (data.errors > 0 ? `. Ошибок: ${data.errors}` : '')
          );
        } else if (data.errors > 0) {
          showErrorNotification(
            `Синхронизация: ${data.errors} ошибок. Откройте приложение для деталей.`
          );
        }
      }
    } catch (_) {}
  }, 60000);
}

// ── Auto-update (electron-updater + GitHub Releases) ────────────────────────

let autoUpdater = null;
let updateDownloaded = false;

function initAutoUpdater() {
  try {
    autoUpdater = require('electron-updater').autoUpdater;
  } catch (e) {
    updaterLog('electron-updater not installed: ' + e.message);
    return;
  }
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('update-available', info => {
    updaterLog('update available: ' + info.version);
    showInfoNotification('Обновление',
      `Загружается обновление ${info.version}…`);
  });

  autoUpdater.on('update-downloaded', info => {
    updateDownloaded = true;
    updaterLog('update downloaded: ' + info.version);
    const choice = dialog.showMessageBoxSync(mainWindow, {
      type: 'info',
      title: 'Обновление готово',
      message: `Версия ${info.version} загружена. Перезапустить сейчас?`,
      buttons: ['Перезапустить сейчас', 'Позже'],
      defaultId: 0,
      cancelId: 1,
    });
    if (choice === 0) {
      isQuitting = true;
      // Stop the Flask/PyInstaller child so the installer can replace files
      if (flaskProc) {
        try { flaskProc.kill(); } catch (_) {}
      }
      autoUpdater.quitAndInstall(false, true);
    }
  });

  autoUpdater.on('error', err => {
    updaterLog('updater error: ' + (err && err.message));
  });
}

function checkForUpdates(manual = false) {
  if (!autoUpdater) {
    if (manual) showInfoNotification('Обновления', 'Модуль обновлений недоступен.');
    return;
  }
  if (!app.isPackaged) {
    if (manual) showInfoNotification('Обновления', 'Проверка доступна только в установленной версии.');
    return;
  }
  autoUpdater.checkForUpdates()
    .then(res => {
      if (manual) {
        const latest = res && res.updateInfo && res.updateInfo.version;
        if (!latest || latest === app.getVersion()) {
          showInfoNotification('Обновления', `У вас последняя версия (${app.getVersion()}).`);
        }
      }
    })
    .catch(err => {
      updaterLog('checkForUpdates failed: ' + (err && err.message));
      if (manual) {
        showInfoNotification('Обновления',
          'Не удалось проверить обновления. Проверьте соединение с интернетом.');
      }
    });
}

// ── Notifications ───────────────────────────────────────────────────────────

function showErrorNotification(msg) {
  if (!Notification.isSupported()) return;
  const iconPath = path.join(__dirname, 'assets', 'icon.png');
  new Notification({
    title: 'Судебный Трекер — ошибка',
    body: msg,
    icon: fs.existsSync(iconPath) ? iconPath : undefined,
  }).show();
}

function showInfoNotification(title, msg) {
  if (!Notification.isSupported()) return;
  const iconPath = path.join(__dirname, 'assets', 'icon.png');
  new Notification({
    title: 'Судебный Трекер: ' + title,
    body: msg,
    icon: fs.existsSync(iconPath) ? iconPath : undefined,
  }).show();
}

// ── IPC handlers ────────────────────────────────────────────────────────────

ipcMain.on('notify-error', (_, msg) => showErrorNotification(msg));
ipcMain.on('notify-info',  (_, title, msg) => showInfoNotification(title, msg));
ipcMain.on('open-external', (_, url) => shell.openExternal(url));
ipcMain.handle('get-app-version', () => app.getVersion());
ipcMain.on('check-updates', () => checkForUpdates(true));
ipcMain.handle('get-auto-update-setting', () => {
  const s = readAppSettings();
  return s.autoCheckUpdates !== false;  // default on
});
ipcMain.on('set-auto-update-setting', (_, enabled) => {
  writeAppSettings({ autoCheckUpdates: !!enabled });
});

// ── App lifecycle ────────────────────────────────────────────────────────────

app.whenReady().then(() => {
  createWindow();
  createTray();
  startFlask();
  pollSyncResult();
  initAutoUpdater();

  // Quiet startup check (10s after window shows); network failures are silent
  if (readAppSettings().autoCheckUpdates !== false) {
    setTimeout(() => {
      try { checkForUpdates(false); } catch (e) { updaterLog(String(e)); }
    }, 10000);
  }

  // F5 — reload current page
  globalShortcut.register('F5', () => {
    if (mainWindow) mainWindow.webContents.reload();
  });
  // Ctrl+Shift+D — open DevTools (for debugging)
  globalShortcut.register('CmdOrCtrl+Shift+D', () => {
    if (mainWindow) mainWindow.webContents.openDevTools();
  });

  waitForFlask(err => {
    if (err) {
      dialog.showErrorBox(
        'Ошибка запуска',
        'Не удалось запустить сервер.\n\n' + err.message +
        '\n\nПроверьте, что Python установлен и зависимости загружены.'
      );
      isQuitting = true;
      app.quit();
      return;
    }
    mainWindow.loadURL(APP_URL);
    mainWindow.show();
  });
});

// Keep running in tray when all windows closed
app.on('window-all-closed', () => {});

app.on('will-quit', () => globalShortcut.unregisterAll());

app.on('before-quit', () => {
  isQuitting = true;
  if (flaskProc) {
    try { flaskProc.kill(); } catch (e) {}
  }
});
