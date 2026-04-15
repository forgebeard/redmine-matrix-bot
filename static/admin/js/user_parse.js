/**
 * Парсинг пользователей из Redmine → Matrix.
 * Inline внутри пузыря, всегда видимый.
 */
(function () {
  'use strict';

  // DOM elements
  var startBtn = document.getElementById('parse-start');
  var createBtn = document.getElementById('parse-create');
  var targetUrlInput = document.getElementById('parse-target-url');
  var stepProgress = document.getElementById('parse-step-progress');
  var stepResults = document.getElementById('parse-step-results');
  var progressFill = document.getElementById('parse-progress-fill');
  var progressText = document.getElementById('parse-progress-text');
  var summaryDiv = document.getElementById('parse-summary');
  var selectAllCb = document.getElementById('parse-select-all');
  var selectedCount = document.getElementById('parse-selected-count');
  
  // New Grid Containers
  var gridNew = document.getElementById('parse-grid-new');
  var gridExisting = document.getElementById('parse-grid-existing');

  var csrfToken = '';
  var lastScanData = null;

  // ── CSRF ──────────────────────────────────────────────────────

  function getCsrfToken() {
    if (csrfToken) return csrfToken;
    csrfToken = document.querySelector('input[name="csrf_token"]')?.value || '';
    return csrfToken;
  }

  // ── Check if ready ───────────────────────────────────────────

  async function checkReady() {
    try {
      var r = await fetch('/api/users/scan-redmine/check');
      var data = await r.json();
      if (data.ready) {
        // Status message removed or handled via UI logic
      }
    } catch (e) { /* ignore */ }
  }

  // ── Show steps ───────────────────────────────────────────────

  function showStep(step) {
    targetUrlInput.parentElement.style.display = step === 'url' ? '' : 'none';
    stepProgress.style.display = step === 'progress' ? '' : 'none';
    stepResults.style.display = step === 'results' ? '' : 'none';
  }

  function resetView() {
    showStep('url');
    targetUrlInput.value = '';
    lastScanData = null;
    gridNew.innerHTML = '';
    gridExisting.innerHTML = '';
  }

  // ── Scan ────────────────────────────────────────────────────

  async function startScan() {
    var url = targetUrlInput.value.trim();
    if (!url) {
      targetUrlInput.focus();
      return;
    }

    showStep('progress');
    progressFill.style.width = '10%';
    progressText.textContent = 'Загрузка пользователей из Redmine...';

    var formData = new FormData();
    formData.append('target_url', url);
    formData.append('csrf_token', getCsrfToken());

    try {
      var controller = new AbortController();

      var r = await fetch('/api/users/scan-redmine', {
        method: 'POST',
        body: formData,
        signal: controller.signal,
      });

      var data = await r.json();

      if (!r.ok) {
        progressFill.style.width = '0%';
        resetView();
        if (typeof toast !== 'undefined') {
          toast.error(data.error || 'Ошибка сканирования');
        }
        return;
      }

      // Обновляем прогресс с количеством
      var total = data.total || 0;
      var found = data.found || 0;
      var existing = data.existing || 0;
      progressText.textContent = 'Найдено ' + total + ' сотрудников в Redmine. ' +
        'В Matrix сопоставлено: ' + found + ' из ' + total +
        (existing > 0 ? ' (уже в системе: ' + existing + ')' : '');
      progressFill.style.width = '95%';

      lastScanData = data;
      renderResults(data);
      showStep('results');
    } catch (e) {
      console.error('[parse] Error:', e);
      progressFill.style.width = '0%';
      resetView();
      if (typeof toast !== 'undefined') {
        if (e.name === 'AbortError') {
          toast.error('Превышено время ожидания (2 мин). Попробуйте ещё раз.');
        } else {
          toast.error('Ошибка сети: ' + e.message);
        }
      }
    }
  }

  // ── Render results ───────────────────────────────────────────

  function renderResults(data) {
    var matches = data.matches || [];
    
    summaryDiv.innerHTML =
      '<span class="found">✅ Найдено: ' + data.found + '</span>' +
      '<span class="existing">ℹ️ Уже в системе: ' + data.existing + '</span>' +
      '<span class="not-found">❌ Не найдено: ' + data.not_found + '</span>';

    gridNew.innerHTML = '';
    gridExisting.innerHTML = '';

    // Split users
    var newUsers = matches.filter(function (m) { return m.status === 'found'; });
    var existingUsers = matches.filter(function (m) { return m.status === 'existing'; });
    // Not found users are hidden or shown differently, but for now let's focus on found/existing
    // If needed, we can add a "not found" section, but the requirement was to split new vs existing.
    // Let's render only found and existing for now to keep it clean, or maybe not found too?
    // The prompt asked to split "New" vs "Already in system". "Not found" is a separate category.
    // I will add not found to "New" section but marked as error, or just skip them?
    // Usually, you only want to create found ones. Let's render found and existing.
    // Actually, the user sees "Not found" count. Let's keep it simple: just render found and existing.

    // Render New Users
    if (newUsers.length > 0) {
      renderGridSection(gridNew, newUsers, true);
    } else {
      gridNew.innerHTML = '<p class="muted" style="grid-column: 1/-1; text-align:center;">Нет новых пользователей для создания.</p>';
    }

    // Render Existing Users
    if (existingUsers.length > 0) {
      renderGridSection(gridExisting, existingUsers, false);
    } else {
      // Optional: hide section if empty? Or show message.
      gridExisting.innerHTML = '<p class="muted" style="grid-column: 1/-1; text-align:center;">Нет пользователей в системе.</p>';
    }

    updateSelectedCount();
    
    // Bind select all
    if (selectAllCb) {
      selectAllCb.checked = true; // Default check all new
    }
  }

  function renderGridSection(container, users, isNew) {
    for (var i = 0; i < users.length; i++) {
      var m = users[i];
      var div = document.createElement('div');
      div.className = 'parse-card' + (isNew ? '' : ' existing');
      
      var matrixHtml = m.matrix_localpart 
        ? '<span class="card-matrix">@' + m.matrix_localpart + '</span>' 
        : '<span class="card-matrix not-found">Не найден</span>';

      var content = '';
      if (isNew) {
        content = '<input type="checkbox" class="card-cb" data-idx="' + m.redmine_id + '" checked/>';
      }
      
      div.innerHTML = content +
        '<div class="card-info">' +
          '<div class="card-name" title="' + escHtml(m.redmine_name) + '">' + escHtml(m.redmine_name) + '</div>' +
          '<div class="card-meta">' +
            '<span class="card-id">ID: ' + m.redmine_id + '</span>' +
            matrixHtml +
          '</div>' +
        '</div>';

      container.appendChild(div);
    }
  }

  function escHtml(s) {
    var div = document.createElement('div');
    div.textContent = s || '';
    return div.innerHTML;
  }

  function updateSelectedCount() {
    var total = gridNew.querySelectorAll('.card-cb').length;
    var checked = gridNew.querySelectorAll('.card-cb:checked').length;
    selectedCount.textContent = 'Выбрано: ' + checked + ' из ' + total;
  }

  // ── Bulk create ──────────────────────────────────────────────

  async function bulkCreate() {
    console.log('[parse] bulkCreate called');

    var selected = [];
    gridNew.querySelectorAll('.card-cb:checked').forEach(function (cb) {
      var rid = cb.getAttribute('data-idx');
      // Find in lastScanData
      if (lastScanData) {
        var match = lastScanData.matches.find(function (m) { return String(m.redmine_id) === String(rid); });
        if (match) {
          selected.push({
            redmine_id: match.redmine_id,
            redmine_name: match.redmine_name,
            matrix_localpart: match.matrix_localpart || '',
          });
        }
      }
    });

    console.log('[parse] Selected users count:', selected.length);

    if (selected.length === 0) {
      if (typeof toast !== 'undefined') {
        toast.warning('Выберите хотя бы одного пользователя');
      }
      return;
    }

    createBtn.disabled = true;
    createBtn.textContent = 'Создаю...';

    try {
      var r = await fetch('/api/users/bulk-create', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': getCsrfToken(),
        },
        body: JSON.stringify({
          users: selected,
          csrf_token: getCsrfToken(),
        }),
      });

      var data = await r.json();

      if (!r.ok) {
        if (typeof toast !== 'undefined') {
          toast.error(data.error || 'Ошибка создания');
        }
        return;
      }

      var msg = 'Создано: ' + data.total_created;
      if (data.total_skipped) msg += ', пропущено: ' + data.total_skipped;
      if (data.total_errors) msg += ', ошибок: ' + data.total_errors;

      if (typeof toast !== 'undefined') {
        toast.success(msg);
      }

      setTimeout(function () { window.location.href = '/users'; }, 500);
    } catch (e) {
      console.error('[parse] Bulk-create error:', e);
      if (typeof toast !== 'undefined') {
        toast.error('Ошибка сети: ' + e.message);
      }
    } finally {
      createBtn.disabled = false;
      createBtn.textContent = 'Создать выбранных';
    }
  }

  // ── Select all ───────────────────────────────────────────────

  function toggleSelectAll(checked) {
    gridNew.querySelectorAll('.card-cb').forEach(function (cb) {
      cb.checked = checked;
    });
    updateSelectedCount();
  }

  // ── Event listeners ──────────────────────────────────────────

  if (!startBtn) {
    console.error('[parse] startBtn not found!');
  } else {
    startBtn.addEventListener('click', function () {
      startScan();
    });
  }
  if (createBtn) createBtn.addEventListener('click', bulkCreate);

  if (selectAllCb) {
    selectAllCb.addEventListener('change', function () {
      toggleSelectAll(this.checked);
    });
  }

  // Live update on individual checkboxes
  gridNew.addEventListener('change', function (e) {
    if (e.target.classList.contains('card-cb')) {
      updateSelectedCount();
      // Check if "Select All" needs updating
      var total = gridNew.querySelectorAll('.card-cb').length;
      var checked = gridNew.querySelectorAll('.card-cb:checked').length;
      selectAllCb.checked = (checked === total && total > 0);
      selectAllCb.indeterminate = (checked > 0 && checked < total);
    }
  });

  if (targetUrlInput) {
    targetUrlInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        startScan();
      }
    });
  }

  checkReady();
})();
