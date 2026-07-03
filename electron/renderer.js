// Injected into every page alongside error_overlay.js by main.js.
// Updates the window title to show the current app section.
(function () {
  'use strict';

  if (window.__sudRendererInstalled) return;
  window.__sudRendererInstalled = true;

  const SECTION_NAMES = {
    '/':          'Дашборд',
    '/cases':     'Дела',
    '/clients':   'Клиенты',
    '/kanban':    'Канбан',
    '/calendar':  'Календарь',
    '/analytics': 'Аналитика',
    '/settings':  'Настройки',
  };

  function updateTitle(version) {
    const path = window.location.pathname;
    const section = SECTION_NAMES[path] || document.title;
    document.title = 'Судебный Трекер — ' + section
                   + (version ? ' · v' + version : '');
  }

  updateTitle();
  if (window.electronAPI && window.electronAPI.getVersion) {
    window.electronAPI.getVersion().then(v => updateTitle(v)).catch(() => {});
  }
})();
