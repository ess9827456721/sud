// Injected into every Flask page by main.js after did-finish-load
(function () {
  'use strict';

  if (window.__sudErrorOverlayInstalled) return;
  window.__sudErrorOverlayInstalled = true;

  // ── Error catalogue ────────────────────────────────────────────────────────
  const ERROR_CATALOGUE = {
    'playwright':        'Браузерный движок (Playwright) недоступен. Убедитесь, что зависимости установлены.',
    'connection':        'Нет соединения с сервером. Проверьте сеть.',
    'database':          'Ошибка базы данных. Попробуйте перезапустить приложение.',
    'timeout':           'Сервер не ответил вовремя. Попробуйте ещё раз.',
    'permission':        'Отказано в доступе. Проверьте права на папку приложения.',
    'disk':              'Ошибка записи на диск. Проверьте свободное место.',
    'scraper':           'Ошибка парсинга. Сайт суда мог изменить структуру страницы.',
    'kad':               'Не удалось получить данные с kad.arbitr.ru.',
    'sudrf':             'Не удалось получить данные с ГАС «Правосудие».',
  };

  function friendlyMessage(raw) {
    const lower = (raw || '').toLowerCase();
    for (const [key, msg] of Object.entries(ERROR_CATALOGUE)) {
      if (lower.includes(key)) return msg;
    }
    return raw || 'Неизвестная ошибка.';
  }

  // ── Banner element ─────────────────────────────────────────────────────────
  let banner = null;
  let hideTimer = null;

  function ensureBanner() {
    if (banner) return banner;

    banner = document.createElement('div');
    banner.id = '__sud-error-banner';
    Object.assign(banner.style, {
      position:        'fixed',
      top:             '0',
      left:            '0',
      right:           '0',
      zIndex:          '999999',
      background:      '#c0392b',
      color:           '#fff',
      fontFamily:      'system-ui, sans-serif',
      fontSize:        '14px',
      padding:         '10px 16px',
      display:         'flex',
      alignItems:      'center',
      gap:             '10px',
      boxShadow:       '0 2px 8px rgba(0,0,0,.35)',
      transition:      'opacity .3s',
      opacity:         '0',
      pointerEvents:   'none',
    });

    const icon = document.createElement('span');
    icon.textContent = '⚠';
    icon.style.fontSize = '18px';

    const text = document.createElement('span');
    text.id = '__sud-error-text';
    text.style.flex = '1';

    const link = document.createElement('a');
    link.textContent = 'Подробнее';
    link.href = '#';
    link.style.color = '#ffeeba';
    link.addEventListener('click', e => {
      e.preventDefault();
      if (window.electronAPI) {
        window.electronAPI.openExternal('https://github.com/ess9827456721/sud/issues');
      }
    });

    const closeBtn = document.createElement('button');
    closeBtn.textContent = '✕';
    Object.assign(closeBtn.style, {
      background:  'transparent',
      border:      'none',
      color:       '#fff',
      cursor:      'pointer',
      fontSize:    '16px',
      lineHeight:  '1',
      padding:     '0 4px',
    });
    closeBtn.addEventListener('click', hideBanner);

    banner.appendChild(icon);
    banner.appendChild(text);
    banner.appendChild(link);
    banner.appendChild(closeBtn);
    document.body.appendChild(banner);
    return banner;
  }

  function showBanner(msg) {
    const b = ensureBanner();
    b.querySelector('#__sud-error-text').textContent = friendlyMessage(msg);
    b.style.opacity = '1';
    b.style.pointerEvents = 'auto';
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(hideBanner, 12000);

    if (window.electronAPI) {
      window.electronAPI.notifyError(friendlyMessage(msg));
    }
  }

  function hideBanner() {
    if (!banner) return;
    banner.style.opacity = '0';
    banner.style.pointerEvents = 'none';
  }

  // ── fetch interceptor ──────────────────────────────────────────────────────
  const _origFetch = window.fetch.bind(window);
  window.fetch = async function (...args) {
    let resp;
    try {
      resp = await _origFetch(...args);
    } catch (err) {
      showBanner('connection');
      throw err;
    }
    if (resp.status >= 500) {
      resp.clone().text().then(body => showBanner(body || 'server error'));
    }
    return resp;
  };

  // ── IPC: errors forwarded from Flask stderr ────────────────────────────────
  if (window.electronAPI && window.electronAPI.onFlaskError) {
    window.electronAPI.onFlaskError(msg => showBanner(msg));
  }

  // ── Custom DOM event dispatched by Flask Jinja pages ──────────────────────
  window.addEventListener('app-scraper-error', e => {
    showBanner((e.detail && e.detail.message) || 'scraper');
  });
})();
