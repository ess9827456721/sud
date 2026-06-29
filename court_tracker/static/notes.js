/* ============================================================
   notes.js — Notes & Tasks engine for court_tracker Phase 5
   ============================================================ */

var Notes = (function () {
  'use strict';

  var _caseId = null;
  var _notes  = [];
  var _filter = 'all';    // 'all' | 'note' | 'task'
  var _debounceTimers = {};

  // ── Colour palette ──────────────────────────────────────────────────────────
  var NOTE_COLORS = [
    { value: '',       label: 'Белый',      bg: '#ffffff', border: '#CBD5E0' },
    { value: 'yellow', label: 'Жёлтый',     bg: '#FFFDE7', border: '#F6E05E' },
    { value: 'blue',   label: 'Голубой',    bg: '#EBF8FF', border: '#90CDF4' },
    { value: 'green',  label: 'Зелёный',    bg: '#F0FFF4', border: '#9AE6B4' },
    { value: 'red',    label: 'Красный',    bg: '#FFF5F5', border: '#FC8181' },
    { value: 'purple', label: 'Фиолетовый', bg: '#FAF5FF', border: '#D6BCFA' },
  ];

  // ── Status / priority definitions ────────────────────────────────────────────
  var STATUS_NEXT    = { 'new': 'in_progress', 'in_progress': 'done', 'done': 'new' };
  var STATUS_LABEL   = { 'new': 'Новая', 'in_progress': 'В работе', 'done': 'Выполнено' };
  var STATUS_CLASS   = { 'new': 'badge-warning', 'in_progress': 'badge-kad', 'done': 'badge-active' };

  var PRIORITY_NEXT  = { 'low': 'medium', 'medium': 'high', 'high': 'low' };
  var PRIORITY_LABEL = { 'low': '! низкий', 'medium': '!! средний', 'high': '!!! высокий' };
  var PRIORITY_CLASS = { 'low': 'badge-active', 'medium': 'badge-warning', 'high': 'badge-danger' };

  // ── Helpers ──────────────────────────────────────────────────────────────────

  function escHtml(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function debounce(key, fn, ms) {
    clearTimeout(_debounceTimers[key]);
    _debounceTimers[key] = setTimeout(fn, ms || 800);
  }

  function colorForValue(val) {
    return NOTE_COLORS.find(function (c) { return c.value === (val || ''); }) || NOTE_COLORS[0];
  }

  function formatBytes(bytes) {
    if (!bytes) return '';
    if (bytes < 1024) return bytes + ' Б';
    if (bytes < 1048576) return Math.round(bytes / 1024) + ' КБ';
    return (bytes / 1048576).toFixed(1) + ' МБ';
  }

  // ── Sync in-flight contenteditable values into _notes state ─────────────────

  function syncEditableState() {
    document.querySelectorAll('[data-ce-note]').forEach(function (el) {
      var noteId = parseInt(el.dataset.ceNote, 10);
      var field  = el.dataset.ceField;
      var val    = el.innerText;
      var n = _notes.find(function (x) { return x.id === noteId; });
      if (n) n[field] = val;
      // Flush pending save immediately
      var key = 'ce-' + noteId + '-' + field;
      if (_debounceTimers[key]) {
        clearTimeout(_debounceTimers[key]);
        delete _debounceTimers[key];
        saveField(noteId, field, val);
      }
    });
  }

  function saveField(noteId, field, value) {
    var payload = {};
    payload[field] = value;
    fetch('/api/notes/' + noteId, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  }

  // ── Init ─────────────────────────────────────────────────────────────────────

  function init(caseId) {
    _caseId = caseId;
    loadNotes();
  }

  function loadNotes() {
    fetch('/api/cases/' + _caseId + '/notes')
      .then(function (r) { return r.json(); })
      .then(function (data) { _notes = data; render(); });
  }

  // ── Filter ───────────────────────────────────────────────────────────────────

  function setFilter(f) {
    _filter = f;
    ['all', 'note', 'task'].forEach(function (x) {
      var btn = document.getElementById('note-filter-' + x);
      if (!btn) return;
      btn.className = btn.className
        .replace(/\bbtn-primary\b/g, 'btn-secondary')
        .replace(/\bbtn-secondary\b/g, 'btn-secondary');
      if (x === f) btn.className = btn.className.replace('btn-secondary', 'btn-primary');
    });
    render();
  }

  // ── Render ───────────────────────────────────────────────────────────────────

  function render() {
    syncEditableState();
    var container = document.getElementById('notes-container');
    if (!container) return;
    var visible = _notes.filter(function (n) {
      return _filter === 'all' || n.item_type === _filter;
    });
    if (!visible.length) {
      container.innerHTML = '<div class="empty-state"><span class="empty-icon">📝</span>' +
        (_filter === 'task' ? 'Нет задач' : _filter === 'note' ? 'Нет заметок' : 'Нет заметок и задач') + '</div>';
      return;
    }
    container.innerHTML = '<div class="notes-grid">' + visible.map(renderCard).join('') + '</div>';
    attachContentEditableListeners(container);
  }

  function renderCard(n) {
    var col = colorForValue(n.color);

    /* ── Color dots ── */
    var dots = NOTE_COLORS.map(function (c) {
      var active = (n.color || '') === c.value
        ? ' style="background:' + c.bg + ';border:2px solid ' + c.border + ';outline:2px solid var(--primary);outline-offset:1px"'
        : ' style="background:' + c.bg + ';border:1px solid ' + c.border + '"';
      return '<span class="note-color-dot" title="' + c.label + '"' + active +
        ' onclick="Notes.setColor(' + n.id + ',\'' + c.value + '\')"></span>';
    }).join('');

    /* ── Task badges ── */
    var statusHtml = '';
    var priorityHtml = '';
    var dueDateHtml = '';
    if (n.item_type === 'task') {
      var st = n.task_status || 'new';
      statusHtml = '<span class="badge-pill ' + (STATUS_CLASS[st] || 'badge-warning') +
        '" style="cursor:pointer" title="Нажмите для смены статуса"' +
        ' onclick="Notes.cycleStatus(' + n.id + ',\'' + st + '\')">' +
        escHtml(STATUS_LABEL[st] || st) + '</span> ';

      var pr = n.task_priority || 'medium';
      priorityHtml = '<span class="badge-pill ' + (PRIORITY_CLASS[pr] || 'badge-warning') +
        '" style="cursor:pointer" title="Нажмите для смены приоритета"' +
        ' onclick="Notes.cyclePriority(' + n.id + ',\'' + pr + '\')">' +
        escHtml(PRIORITY_LABEL[pr] || pr) + '</span>';

      var dueVal = n.task_due_date || '';
      var overdue = dueVal && n.task_status !== 'done' && new Date(dueVal) < new Date();
      dueDateHtml = '<div class="note-due-row">' +
        '<label class="text-small text-muted">Срок:&nbsp;</label>' +
        '<input type="date" class="note-due-input" value="' + escHtml(dueVal) + '"' +
        (overdue ? ' style="color:var(--danger);font-weight:700"' : '') +
        ' onchange="Notes.saveDueDate(' + n.id + ', this.value)">' +
        (overdue ? '<span class="text-small" style="color:var(--danger);margin-left:4px">просрочено</span>' : '') +
        '</div>';
    }

    /* ── Checklist ── */
    var checklistHtml = '';
    if (n.checklist && n.checklist.length) {
      checklistHtml = '<ul class="note-checklist">' +
        n.checklist.map(function (item) {
          return '<li class="note-checklist-item">' +
            '<input type="checkbox" class="note-check"' + (item.checked ? ' checked' : '') +
            ' onchange="Notes.toggleCheck(' + item.id + ', this)">' +
            '<span class="note-check-text' + (item.checked ? ' note-check-done' : '') + '">' +
            escHtml(item.text || '') + '</span>' +
            '<button class="note-check-del" title="Удалить пункт"' +
            ' onclick="Notes.deleteCheckItem(' + item.id + ',' + n.id + ')">×</button>' +
            '</li>';
        }).join('') + '</ul>';
    }
    checklistHtml += '<div class="note-add-check">' +
      '<input type="text" class="note-check-input" placeholder="+ пункт (Enter или Ctrl+Enter)"' +
      ' data-note-id="' + n.id + '"' +
      ' onkeydown="Notes.checkKey(event,' + n.id + ',this)"></div>';

    /* ── Tags ── */
    var tagsHtml = '<div class="note-tags">' +
      (n.tags || []).map(function (tag) {
        return '<span class="note-tag">' + escHtml(tag) +
          '<button class="note-tag-del" title="Удалить тег"' +
          ' onclick="Notes.removeTag(' + n.id + ',\'' + escHtml(tag) + '\')">×</button></span>';
      }).join('') +
      '<input type="text" class="note-tag-input" placeholder="добавить тег…"' +
      ' onkeydown="Notes.tagKey(event,' + n.id + ',this)"></div>';

    /* ── Type label ── */
    var typeLabel = n.item_type === 'task' ? '⚙ Задача' : '📝 Заметка';
    var createdDate = (n.created_at || '').slice(0, 10);

    return '<div class="note-card" id="note-' + n.id + '"' +
      ' style="border-top:4px solid ' + col.border + ';background:' + col.bg + '">' +

      '<div class="note-card-top">' +
        '<div class="note-color-bar">' + dots + '</div>' +
        '<button class="note-trash" title="Удалить заметку"' +
        ' onclick="Notes.deleteNote(' + n.id + ')">🗑</button>' +
      '</div>' +

      (statusHtml || priorityHtml
        ? '<div class="note-badges">' + statusHtml + priorityHtml + '</div>'
        : '') +

      '<div class="note-title" contenteditable="true"' +
        ' data-ce-note="' + n.id + '" data-ce-field="title"' +
        ' data-placeholder="' + (n.item_type === 'task' ? 'Задача…' : 'Заголовок…') + '">' +
        escHtml(n.title || '') + '</div>' +

      dueDateHtml +

      '<div class="note-body" contenteditable="true"' +
        ' data-ce-note="' + n.id + '" data-ce-field="body"' +
        ' data-placeholder="Текст заметки…">' +
        escHtml(n.body || '') + '</div>' +

      tagsHtml +
      checklistHtml +

      '<div class="note-card-footer">' +
        '<button class="btn btn-secondary btn-sm note-attach-btn"' +
        ' onclick="Notes.openFileInput(' + n.id + ')">📎 Прикрепить файл</button>' +
        '<input type="file" id="note-file-' + n.id + '" style="display:none"' +
        ' onchange="Notes.attachFile(' + n.id + ',this)">' +
        '<span class="note-meta">' + typeLabel + ' · ' + createdDate + '</span>' +
      '</div>' +

      '</div>';
  }

  function attachContentEditableListeners(container) {
    container.querySelectorAll('[data-ce-note]').forEach(function (el) {
      el.addEventListener('input', function () {
        var noteId = el.dataset.ceNote;
        var field  = el.dataset.ceField;
        var value  = el.innerText;
        var n = _notes.find(function (x) { return x.id === parseInt(noteId, 10); });
        if (n) n[field] = value;
        debounce('ce-' + noteId + '-' + field, function () {
          saveField(parseInt(noteId, 10), field, value);
        }, 800);
      });
    });
  }

  // ── CRUD ─────────────────────────────────────────────────────────────────────

  function createNote(type) {
    fetch('/api/cases/' + _caseId + '/notes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_type: type, title: '', body: '' }),
    })
      .then(function (r) { return r.json(); })
      .then(function (n) {
        _notes.unshift(n);
        render();
        setTimeout(function () {
          var el = document.querySelector('#note-' + n.id + ' .note-title');
          if (el) { el.focus(); placeCaretAtEnd(el); }
        }, 60);
      });
  }

  function deleteNote(noteId) {
    if (!confirm('Удалить эту заметку?')) return;
    fetch('/api/notes/' + noteId, { method: 'DELETE' })
      .then(function () {
        _notes = _notes.filter(function (n) { return n.id !== noteId; });
        render();
      });
  }

  function setColor(noteId, color) {
    fetch('/api/notes/' + noteId, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ color: color }),
    }).then(function () {
      var n = _notes.find(function (x) { return x.id === noteId; });
      if (n) { n.color = color; render(); }
    });
  }

  function cycleStatus(noteId, current) {
    var next = STATUS_NEXT[current] || 'new';
    fetch('/api/notes/' + noteId, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_status: next }),
    }).then(function () {
      var n = _notes.find(function (x) { return x.id === noteId; });
      if (n) { n.task_status = next; render(); }
    });
  }

  function cyclePriority(noteId, current) {
    var next = PRIORITY_NEXT[current] || 'medium';
    fetch('/api/notes/' + noteId, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_priority: next }),
    }).then(function () {
      var n = _notes.find(function (x) { return x.id === noteId; });
      if (n) { n.task_priority = next; render(); }
    });
  }

  function saveDueDate(noteId, value) {
    fetch('/api/notes/' + noteId, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_due_date: value || null }),
    }).then(function () {
      var n = _notes.find(function (x) { return x.id === noteId; });
      if (n) { n.task_due_date = value; render(); }
    });
  }

  // ── Checklist ────────────────────────────────────────────────────────────────

  function checkKey(event, noteId, input) {
    if (event.key === 'Enter') {
      event.preventDefault();
      var text = input.value.trim();
      if (!text) return;
      input.value = '';
      fetch('/api/notes/' + noteId + '/checklist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text }),
      })
        .then(function (r) { return r.json(); })
        .then(function (item) {
          var n = _notes.find(function (x) { return x.id === noteId; });
          if (n) {
            if (!n.checklist) n.checklist = [];
            n.checklist.push({ id: item.id, text: item.text, checked: false });
            render();
          }
        });
    }
  }

  function toggleCheck(itemId, checkbox) {
    fetch('/api/checklist/' + itemId + '/toggle', { method: 'PATCH' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        _notes.forEach(function (n) {
          if (!n.checklist) return;
          var item = n.checklist.find(function (i) { return i.id === itemId; });
          if (item) { item.checked = data.checked; render(); }
        });
      });
  }

  function deleteCheckItem(itemId, noteId) {
    fetch('/api/checklist/' + itemId, { method: 'DELETE' })
      .then(function () {
        var n = _notes.find(function (x) { return x.id === noteId; });
        if (n && n.checklist) {
          n.checklist = n.checklist.filter(function (i) { return i.id !== itemId; });
          render();
        }
      });
  }

  // ── Tags ─────────────────────────────────────────────────────────────────────

  function tagKey(event, noteId, input) {
    if (event.key === 'Enter') {
      event.preventDefault();
      var tag = input.value.trim();
      if (!tag) return;
      input.value = '';
      fetch('/api/notes/' + noteId + '/tags', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tag: tag }),
      }).then(function () {
        var n = _notes.find(function (x) { return x.id === noteId; });
        if (n) {
          if (!n.tags) n.tags = [];
          if (n.tags.indexOf(tag) === -1) n.tags.push(tag);
          render();
        }
      });
    }
  }

  function removeTag(noteId, tag) {
    fetch('/api/notes/' + noteId + '/tags', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tag: tag }),
    }).then(function () {
      var n = _notes.find(function (x) { return x.id === noteId; });
      if (n && n.tags) {
        n.tags = n.tags.filter(function (t) { return t !== tag; });
        render();
      }
    });
  }

  // ── File attachment ───────────────────────────────────────────────────────────

  function openFileInput(noteId) {
    var inp = document.getElementById('note-file-' + noteId);
    if (inp) inp.click();
  }

  function attachFile(noteId, input) {
    var file = input.files && input.files[0];
    if (!file) return;
    var fd = new FormData();
    fd.append('file', file);
    fd.append('note_id', noteId);
    fetch('/api/cases/' + _caseId + '/attachments', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error) { alert('Ошибка загрузки: ' + data.error); return; }
        if (typeof window.loadAttachments === 'function') {
          window.loadAttachments(_caseId);
        }
      });
    input.value = '';
  }

  // ── Utility ───────────────────────────────────────────────────────────────────

  function placeCaretAtEnd(el) {
    el.focus();
    try {
      var range = document.createRange();
      range.selectNodeContents(el);
      range.collapse(false);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } catch (e) {}
  }

  // ── Public API ────────────────────────────────────────────────────────────────

  return {
    init:            init,
    setFilter:       setFilter,
    createNote:      createNote,
    deleteNote:      deleteNote,
    setColor:        setColor,
    cycleStatus:     cycleStatus,
    cyclePriority:   cyclePriority,
    saveDueDate:     saveDueDate,
    checkKey:        checkKey,
    toggleCheck:     toggleCheck,
    deleteCheckItem: deleteCheckItem,
    tagKey:          tagKey,
    removeTag:       removeTag,
    openFileInput:   openFileInput,
    attachFile:      attachFile,
  };
})();
