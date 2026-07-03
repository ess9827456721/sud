/**
 * Electron main process for Судебный Трекер.
 * Starts Flask as a child process, shows the app in a native window,
 * manages system tray and native Windows notifications.
 */
const { app, BrowserWindow, Tray, Menu, nativeImage,
        ipcMain, Notification, shell, dialog, globalShortcut } = require('electron');
const path   = require('path');
const { spawn } = require('child_process');
const http   = require('http');
const fs     = require('fs');

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

function getPythonPath() {
  if (app.isPackaged) {
    const bundled = path.join(process.resourcesPath, 'python', 'python.exe');
    if (fs.existsSync(bundled)) return bundled;
  }
  return process.platform === 'win32' ? 'python' : 'python3';
}

function getStartScript() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'start.py');
  }
  return path.join(__dirname, '..', 'build', 'start.py');
}

function startFlask() {
  const python = getPythonPath();
  const script = getStartScript();
  const cwd    = app.isPackaged ? process.resourcesPath
                                : path.join(__dirname, '..');

  flaskProc = spawn(python, [script], {
    cwd,
    env: { ...process.env, FLASK_DEBUG: '0', SUD_NO_BROWSER: '1' },
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
// scheduler.py writes court_tracker/data/last_sync_result.json after each
// sync run; poll it every 60s and raise a native notification if fresh.

function pollSyncResult() {
  const notifyFile = path.join(
    app.isPackaged ? process.resourcesPath : path.join(__dirname, '..'),
    'court_tracker', 'data', 'last_sync_result.json'
  );
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

// ── App lifecycle ────────────────────────────────────────────────────────────

app.whenReady().then(() => {
  createWindow();
  createTray();
  startFlask();
  pollSyncResult();

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
