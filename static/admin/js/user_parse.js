/**
 * Парсинг пользователей из Redmine → Matrix.
 * Модалка с 3 шагами: URL → прогресс → результаты.
 */
(function () {
  'use strict';

  // DOM elements
  var modal = document.getElementById('parse-modal');
  var openBtn = document.getElementById('parse-users-btn');
  var closeBtn = document.getElementById('parse-modal-close');
  var cancel1 = document.getElementById('parse-cancel-1');
  var cancel3 = document.getElementById('parse-cancel-3');
  var startBtn = document.getElementById('parse-start');
  var createBtn = document.getElementById('parse-create');
  var targetUrlInput = document.getElementById('parse-target-url');
  var stepUrl = document.getElementById('parse-step-url');
  var stepProgress = document.getElementById('parse-step-progress');
  var stepResults = document.getElementById('parse-step-results');
  var progressFill = document.getElementById('parse-progress-fill');
  var progressText = document.getElementById('parse-progress-text');
  var summaryDiv = document.getElementById('parse-summary');
  var selectAllCb = document.getElementById('parse-select-all');
  var selectAllHeader = document.getElementById('parse-select-all-header');
  var selectedCount = document.getElementById('parse-selected-count');
  var resultsBody = document.getElementById('parse-results-body');

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
        openBtn.disabled = false;
        openBtn.title = 'Начать парсинг пользователей';
      }
    } catch (e) { /* ignore */ }
  }

  // ── Modal open/close ─────────────────────────────────────────

  function openModal() {
    modal.style.display = 'flex';
    showStep('url');
    targetUrlInput.value = '';
    targetUrlInput.focus();
    checkReady();
  }

  function closeModal() {
    modal.style.display = 'none';
    lastScanData = null;
  }

  function showStep(step) {
    stepUrl.style.display = step === 'url' ? '' : 'none';
    stepProgress.style.display = step === 'progress' ? '' : 'none';
    stepResults.style.display = step === 'results' ? '' : 'none';
  }

  // ── Scan ─────────────────────────────────────────────────────

  async function startScan() {
    var url = targetUrlInput.value.trim();
    if (!url) {
      targetUrlInput.focus();
      return;
    }

    showStep('progress');
    progressFill.style.width = '10%';
    progressText.textContent = 'Подключение к Redmine...';

    var formData = new FormData();
    formData.append('target_url', url);
    formData.append('csrf_token', getCsrfToken());

    try {
      progressFill.style.width = '30%';
      progressText.textContent = 'Загрузка пользователей из Redmine...';

      var r = await fetch('/api/users/scan-redmine', {
        method: 'POST',
        body: formData,
      });

      progressFill.style.width = '80%';
      progressText.textContent = 'Сопоставление с Matrix...';

      var data = await r.json();

      if (!r.ok) {
        progressFill.style.width = '0%';
        showStep('url');
        if (typeof toast !== 'undefined') {
          toast.error(data.error || 'Ошибка сканирования');
        }
        return;
      }

      progressFill.style.width = '100%';
      lastScanData = data;
      renderResults(data);
      showStep('results');
    } catch (e) {
      progressFill.style.width = '0%';
      showStep('url');
      if (typeof toast !== 'undefined') {
        toast.error('Ошибка сети: ' + e.message);
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

    resultsBody.innerHTML = '';
    matches.forEach(function (m, i) {
      var tr = document.createElement('tr');

      var statusText, statusClass;
      if (m.status === 'found') {
        statusText = '✅ Найден';
        statusClass = 'status-found';
      } else if (m.status === 'existing') {
        statusText = 'ℹ️ Уже в системе';
        statusClass = 'status-existing';
      } else {
        statusText = '❌ Не найден';
        statusClass = 'status-not-found';
      }

      var cbChecked = m.status === 'found' ? 'checked' : '';
      var cbDisabled = m.status !== 'found' ? 'disabled' : '';

      tr.innerHTML =
        '<td><input type="checkbox" class="parse-cb" data-idx="' + i + '" ' + cbChecked + ' ' + cbDisabled + '/></td>' +
        '<td>' + escHtml(m.redmine_name) + '</td>' +
        '<td>' + m.redmine_id + '</td>' +
        '<td>' + escHtml(m.matrix_localpart || '—') + '</td>' +
        '<td class="' + statusClass + '">' + statusText + '</td>';

      resultsBody.appendChild(tr);
    });

    updateSelectedCount();
  }

  function escHtml(s) {
    var div = document.createElement('div');
    div.textContent = s || '';
    return div.innerHTML;
  }

  function updateSelectedCount() {
    var total = resultsBody.querySelectorAll('.parse-cb:not([disabled])').length;
    var checked = resultsBody.querySelectorAll('.parse-cb:checked:not([disabled])').length;
    selectedCount.textContent = 'Выбрано: ' + checked + ' из ' + total;
  }

  // ── Bulk create ──────────────────────────────────────────────

  async function bulkCreate() {
    var selected = [];
    resultsBody.querySelectorAll('.parse-cb:checked').forEach(function (cb) {
      var idx = parseInt(cb.getAttribute('data-idx'), 10);
      if (lastScanData && lastScanData.matches[idx]) {
        var m = lastScanData.matches[idx];
        selected.push({
          redmine_id: m.redmine_id,
          redmine_name: m.redmine_name,
          matrix_localpart: m.matrix_localpart || '',
        });
      }
    });

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
        headers: { 'Content-Type': 'application/json' },
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

      closeModal();
      // Обновляем страницу чтобы увидеть новых пользователей
      setTimeout(function () { window.location.reload(); }, 800);
    } catch (e) {
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
    resultsBody.querySelectorAll('.parse-cb:not([disabled])').forEach(function (cb) {
      cb.checked = checked;
    });
    updateSelectedCount();
  }

  // ── Event listeners ──────────────────────────────────────────

  if (openBtn) openBtn.addEventListener('click', openModal);
  if (closeBtn) closeBtn.addEventListener('click', closeModal);
  if (cancel1) cancel1.addEventListener('click', closeModal);
  if (cancel3) cancel3.addEventListener('click', closeModal);
  if (startBtn) startBtn.addEventListener('click', startScan);
  if (createBtn) createBtn.addEventListener('click', bulkCreate);

  if (selectAllCb) {
    selectAllCb.addEventListener('change', function () {
      toggleSelectAll(this.checked);
      if (selectAllHeader) selectAllHeader.checked = this.checked;
    });
  }
  if (selectAllHeader) {
    selectAllHeader.addEventListener('change', function () {
      toggleSelectAll(this.checked);
      if (selectAllCb) selectAllCb.checked = this.checked;
    });
  }

  // Enter in URL input
  if (targetUrlInput) {
    targetUrlInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        startScan();
      }
    });
  }

  // Close on overlay click
  if (modal) {
    modal.addEventListener('click', function (e) {
      if (e.target === modal) closeModal();
    });
  }

  // Check readiness on load
  checkReady();
})();
