/**
 * Универсальный менеджер справочников Redmine.
 * Управление Статусами, Версиями и Приоритетами.
 * Три колонки: Все | По умолчанию | Корзина
 * Drag-and-drop между колонками.
 */
(function () {
  'use strict';

  var csrfToken = '';

  function getCsrfToken() {
    if (csrfToken) return csrfToken;
    csrfToken = document.querySelector('input[name="csrf_token"]')?.value || '';
    return csrfToken;
  }

  // ── CatalogManager ──────────────────────────────────────────────

  function CatalogManager(config) {
    var self = this;
    self.config = config;
    self.items = [];
    self.dragDropInitialized = false;

    self.allContainer = document.getElementById(config.prefix + '-all-items');
    self.defaultContainer = document.getElementById(config.prefix + '-default-items');
    self.trashContainer = document.getElementById(config.prefix + '-trash-items');

    var addInput = document.getElementById(config.prefix + '-add-name');
    var addBtn = document.getElementById(config.prefix + '-add-btn');
    if (addBtn) {
      addBtn.addEventListener('click', function () {
        var name = addInput.value.trim();
        if (!name) { showToastMsg(config, 'Введите название', true); return; }
        var fd = new FormData();
        fd.append('redmine_' + config.singular + '_id', '0');
        fd.append('name', name);
        fd.append('csrf_token', getCsrfToken());
        fetch('/api/catalog/' + config.plural, { method: 'POST', credentials: 'same-origin', body: fd })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (data.ok || data.id) {
              addInput.value = '';
              self.items.push(data);
              self.render();
              showToastMsg(config, config.label + ' добавлен');
            } else {
              showToastMsg(config, data.error || 'Ошибка', true);
            }
          });
      });
    }
  }

  CatalogManager.prototype.load = function () {
    var self = this;
    fetch('/api/catalog/' + self.config.plural, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        self.items = data[self.config.plural] || [];
        self.render();
      });
  };

  CatalogManager.prototype.render = function () {
    var self = this;
    if (!self.allContainer || !self.defaultContainer || !self.trashContainer) return;

    var active = self.items.filter(function (s) { return s.is_active !== false; });
    var isDefault = active.filter(function (s) { return s.is_default === true; });
    var notDefault = active.filter(function (s) { return s.is_default !== true; });
    var trashed = self.items.filter(function (s) { return s.is_active === false; });

    self.allContainer.innerHTML = notDefault.map(function (s) { return self.renderItem(s); }).join('');
    self.defaultContainer.innerHTML = isDefault.map(function (s) { return self.renderItem(s); }).join('');
    self.trashContainer.innerHTML = trashed.map(function (s) {
      return '<div class="status-item" draggable="true" data-id="' + s.id + '" data-name="' + escAttr(s.name) + '">' +
        '<span class="status-item-name">' + escHtml(s.name) + '</span>' +
        '<button type="button" class="status-item-del" data-action="restore" title="Восстановить">↩</button>' +
        '</div>';
    }).join('');

    updateCounts(self.config.prefix, isDefault.length, trashed.length);
    self.setupDragDrop();
  };

  CatalogManager.prototype.renderItem = function (s) {
    return '<div class="status-item" draggable="true" data-id="' + s.id + '" data-name="' + escAttr(s.name) + '">' +
      '<span class="status-item-name">' + escHtml(s.name) + '</span>' +
      '<button type="button" class="status-item-del" data-action="trash" title="В корзину">✕</button>' +
      '</div>';
  };

  CatalogManager.prototype.toggleField = function (id, field) {
    var self = this;
    fetch('/api/catalog/' + self.config.plural + '/' + id + '/toggle?field=' + field, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-CSRF-Token': getCsrfToken() },
      body: JSON.stringify({ csrf_token: getCsrfToken() }),
    }).then(function (r) { return r.json(); })
      .then(function (data) { if (data.ok) self.load(); });
  };

  CatalogManager.prototype.permanentDelete = function (id) {
    var self = this;
    showConfirm('Удалить «' + (self.items.find(function (x) { return String(x.id) === String(id); }) || {}).name + '» навсегда? Он будет удалён у всех пользователей и групп.', function () {
      fetch('/api/catalog/' + self.config.plural + '/' + id, {
        method: 'DELETE', credentials: 'same-origin',
        headers: { 'X-CSRF-Token': getCsrfToken() },
      }).then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok) { self.load(); showToastMsg(self.config, self.config.label + ' удалён'); }
          else { showToastMsg(self.config, data.error || 'Ошибка', true); }
        });
    });
  };

  CatalogManager.prototype.setupDragDrop = function () {
    var self = this;
    if (self.dragDropInitialized) return;
    self.dragDropInitialized = true;

    var zones = [self.allContainer, self.defaultContainer, self.trashContainer];
    zones.forEach(function (zone) {
      zone.addEventListener('dragstart', function (e) {
        var item = e.target.closest('.status-item');
        if (!item) return;
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', item.getAttribute('data-id'));
        item.classList.add('dragging');
      });
      zone.addEventListener('dragend', handleGlobalDragEnd);
      zone.addEventListener('dragover', handleDragOver);
      zone.addEventListener('dragleave', handleDragLeave);
      zone.addEventListener('drop', function (e) {
        handleGlobalDrop.call(this, e, self);
      });
    });

    self.allContainer.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-action="trash"]');
      if (!btn) return;
      var item = btn.closest('.status-item');
      if (item) self.toggleField(item.getAttribute('data-id'), 'is_active');
    });
    self.defaultContainer.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-action="trash"]');
      if (!btn) return;
      var item = btn.closest('.status-item');
      if (item) self.toggleField(item.getAttribute('data-id'), 'is_active');
    });
    self.trashContainer.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-action="restore"]');
      if (!btn) return;
      var item = btn.closest('.status-item');
      if (item) self.toggleField(item.getAttribute('data-id'), 'is_active');
    });
    self.trashContainer.addEventListener('dblclick', function (e) {
      var item = e.target.closest('.status-item');
      if (item) self.permanentDelete(parseInt(item.getAttribute('data-id'), 10));
    });
  };

  // ── Global drag-and-drop handlers ───────────────────────────────

  function handleDragOver(e) {
    if (e.preventDefault) e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    this.classList.add('drag-over');
    return false;
  }

  function handleDragLeave() { this.classList.remove('drag-over'); }

  function handleGlobalDrop(e, manager) {
    if (e.stopPropagation) e.stopPropagation();
    this.classList.remove('drag-over');
    var id = e.dataTransfer.getData('text/plain');
    if (!id) return false;

    var targetZone = this;
    var isAll = targetZone === manager.allContainer;
    var isDefault = targetZone === manager.defaultContainer;
    var isTrash = targetZone === manager.trashContainer;

    // Определяем откуда тащили по родителю dragged-элемента
    var draggedEl = document.querySelector('.status-item.dragging');
    if (!draggedEl) return false;
    var fromTrash = draggedEl.closest('.statuses-trash-col');
    var fromDefault = draggedEl.closest('.statuses-default-col');
    var fromAll = draggedEl.closest('.statuses-all-col');

    if (isTrash && !fromTrash) {
      manager.toggleField(id, 'is_active');
    } else if (isAll && fromTrash) {
      manager.toggleField(id, 'is_active');
    } else if (isDefault && fromAll) {
      manager.toggleField(id, 'is_default');
    } else if (isAll && fromDefault) {
      manager.toggleField(id, 'is_default');
    }
    return false;
  }

  function handleGlobalDragEnd() {
    document.querySelectorAll('.status-item').forEach(function (i) { i.classList.remove('dragging'); });
    document.querySelectorAll('.statuses-dropzone').forEach(function (z) { z.classList.remove('drag-over'); });
  }

  // ── Managers ────────────────────────────────────────────────────

  var managers = [];

  function initManagers() {
    managers = [
      new CatalogManager({ prefix: 'statuses', plural: 'statuses', singular: 'status', label: 'Статус' }),
      new CatalogManager({ prefix: 'versions', plural: 'versions', singular: 'version', label: 'Версия' }),
      new CatalogManager({ prefix: 'priorities', plural: 'priorities', singular: 'priority', label: 'Приоритет' }),
    ];
    managers.forEach(function (m) { m.load(); });
  }

  // ── Sync all ────────────────────────────────────────────────────

  var syncBtn = document.getElementById('sync-statuses-btn');
  if (syncBtn) {
    syncBtn.addEventListener('click', function () {
      syncBtn.disabled = true;
      syncBtn.textContent = 'Синхронизация…';
      fetch('/api/catalog/sync-all', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
        body: JSON.stringify({ csrf_token: getCsrfToken() }),
      }).then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) { showToastMsg({ prefix: 'statuses', label: 'Статус' }, data.error || 'Ошибка синхронизации', true); return; }
          var parts = [];
          if (data.statuses) parts.push('Статусы: +' + (data.statuses.added||0) + ' ~' + (data.statuses.updated||0) + ' −' + (data.statuses.hidden||0));
          if (data.versions) parts.push('Версии: +' + (data.versions.added||0) + ' ~' + (data.versions.updated||0) + ' −' + (data.versions.hidden||0));
          if (data.priorities) parts.push('Приоритеты: +' + (data.priorities.added||0) + ' ~' + (data.priorities.updated||0) + ' −' + (data.priorities.hidden||0));
          showToastMsg({ prefix: 'statuses', label: 'Каталог' }, 'Обновлено: ' + parts.join(', '));
          managers.forEach(function (m) { m.load(); });
        })
        .finally(function () {
          syncBtn.disabled = false;
          syncBtn.textContent = 'Обновить из Redmine';
        });
    });
  }

  // ── Helpers ─────────────────────────────────────────────────────

  function escHtml(s) { var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
  function escAttr(s) { return (s || '').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }

  function updateCounts(prefix, defCount, trashCount) {
    var defEl = document.getElementById(prefix + '-default-count');
    var trashEl = document.getElementById(prefix + '-trash-count');
    if (defEl) { defEl.textContent = defCount; defEl.classList.toggle('visible', defCount > 0); }
    if (trashEl) { trashEl.textContent = trashCount; trashEl.classList.toggle('visible', trashCount > 0); }
  }

  function showConfirm(message, onOk) {
    var overlay = document.createElement('div');
    overlay.className = 'custom-confirm-overlay';
    overlay.innerHTML = '<div class="custom-confirm"><p>' + escHtml(message) + '</p>' +
      '<div class="custom-confirm-actions"><button type="button" class="btn btn-ghost" id="cc-cancel">Отмена</button>' +
      '<button type="button" class="btn btn-danger" id="cc-ok">Удалить</button></div></div>';
    document.body.appendChild(overlay);
    function close(r) { overlay.remove(); if (r) onOk(); }
    overlay.querySelector('#cc-cancel').addEventListener('click', function () { close(false); });
    overlay.querySelector('#cc-ok').addEventListener('click', function () { close(true); });
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(false); });
  }

  function showToastMsg(config, text, isError) {
    var t = document.createElement('div');
    t.className = 'custom-toast' + (isError ? ' custom-toast--error' : '');
    t.textContent = text;
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.classList.add('show'); });
    setTimeout(function () { t.classList.remove('show'); setTimeout(function () { t.remove(); }, 400); }, 3000);
  }

  // ── Init ────────────────────────────────────────────────────────

  initManagers();
})();