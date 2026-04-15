(function () {
  /* --- Toast on return from save --- */
  var params = new URLSearchParams(window.location.search);
  if (params.get('saved') === '1') {
    if (typeof showToast === 'function') showToast('Пользователь сохранён');
    params.delete('saved');
    var newUrl = window.location.pathname + (params.toString() ? '?' + params.toString() : '');
    window.history.replaceState({}, '', newUrl);
  }

  var btn = document.getElementById('redmine_lookup_btn');
  var rid = document.getElementById('redmine_id');
  var dname = document.getElementById('display_name');
  var st = document.getElementById('redmine_lookup_status');

  var formRoot = document.querySelector('.user-form-root');
  var matrixDomain = formRoot ? (formRoot.getAttribute('data-matrix-domain') || '') : '';
  var botTz = formRoot ? (formRoot.getAttribute('data-bot-tz') || '') : '';

  function setStatus(msg) {
    if (st) st.textContent = msg || '';
  }

  var messages = {
    not_configured: 'Redmine не настроен (URL/API key).',
    not_found: 'Пользователь с таким ID не найден.',
    invalid_id: 'Введите положительный числовой ID.',
    cooldown: 'Поиск временно недоступен, подождите минуту.',
    timeout: 'Таймаут запроса к Redmine.',
    error: 'Ошибка запроса к Redmine.',
  };

  /* --- Redmine lookup --- */
  async function lookup() {
    if (!btn || !rid) return;
    var id = String(rid.value || '').trim();
    if (!id || !/^[1-9]\d*$/.test(id)) { setStatus(messages.invalid_id); return; }
    setStatus('Запрос…');
    btn.disabled = true;
    try {
      var r = await fetch('/redmine/users/lookup?user_id=' + encodeURIComponent(id), {
        headers: { Accept: 'application/json' }, credentials: 'same-origin',
      });
      var data = await r.json().catch(function () { return {}; });
      if (r.ok && data.ok && data.display_name) {
        if (dname) dname.value = data.display_name;
        if (data.login) {
          var roomInput = document.getElementById('room_localpart');
          if (roomInput && !roomInput.value) { roomInput.value = data.login; }
        }
        setStatus(data.login ? (data.display_name + ' (' + data.login + ')') : data.display_name);
        refreshSummary();
      } else {
        var code = data.error || (r.status === 404 ? 'not_found' : 'error');
        setStatus(messages[code] || ('Ошибка: ' + (code || r.status)));
      }
    } catch (e) { setStatus(messages.error); }
    finally { btn.disabled = false; }
  }
  if (btn) btn.addEventListener('click', lookup);

  /* --- Test message --- */
  var testBtn = document.getElementById('test_message_btn');
  if (testBtn) {
    testBtn.addEventListener('click', async function () {
      var userId = testBtn.getAttribute('data-user-id');
      var roomInput = document.getElementById('room_localpart');
      var lp = roomInput ? roomInput.value.trim() : '';
      var mxid = lp ? ('@' + lp + (matrixDomain ? ':' + matrixDomain : '')) : '';
      if (!userId && !mxid) { setStatus('Укажите Matrix ID или сохраните пользователя'); return; }
      testBtn.disabled = true;
      testBtn.textContent = '⏳ Отправка…';
      try {
        var csrf = (document.querySelector('input[name="csrf_token"]') || {}).value || '';
        var body = new FormData();
        if (userId) body.append('user_id', userId);
        if (mxid) body.append('mxid', mxid);
        var r = await fetch('/users/test-message', {
          method: 'POST',
          headers: { 'Accept': 'application/json', 'X-CSRF-Token': csrf },
          credentials: 'same-origin', body: body,
        });
        var data = await r.json().catch(function () { return {}; });
        setStatus(data.ok ? 'Сообщение доставлено' : ('Не доставлено: ' + (data.error || 'неизвестная ошибка')));
      } catch (e) { setStatus('Не доставлено: ошибка сети'); }
      finally { testBtn.disabled = false; testBtn.textContent = 'Отправить тестовое сообщение'; }
    });
  }

  /* --- Summary helpers --- */
  function textOrDash(v) { var s = String(v || '').trim(); return s || '—'; }
  function selectedText(sel) {
    if (!sel) return '—';
    var opt = sel.options[sel.selectedIndex];
    return opt ? (opt.textContent || '').trim() || '—' : '—';
  }
  function checkedLabels(selector) {
    return Array.from(document.querySelectorAll(selector))
      .filter(function (el) { return el.checked; })
      .map(function (el) { return String((el.parentElement || {}).textContent || '').trim(); })
      .filter(Boolean);
  }
  function selectedNotifyLabel() {
    var a = document.querySelector('input[name="status_preset"]:checked');
    if (!a) return '—';
    if (a.value === 'default') return 'По умолчанию';
    var l = checkedLabels('input[name="status_values"]');
    return l.length ? l.join(', ') : '—';
  }
  function selectedVersionsLabel() {
    var a = document.querySelector('input[name="version_preset"]:checked');
    if (!a) return '—';
    if (a.value === 'default') return 'По умолчанию';
    var l = checkedLabels('input[name="version_values"]');
    return l.length ? l.join(', ') : '—';
  }
  function selectedPrioritiesLabel() {
    var a = document.querySelector('input[name="priority_preset"]:checked');
    if (!a) return '—';
    if (a.value === 'default') return 'По умолчанию';
    var l = checkedLabels('input[name="priority_values"]');
    return l.length ? l.join(', ') : '—';
  }
  function selectedHours() {
    var f = document.getElementById('work_hours_from');
    var t = document.getElementById('work_hours_to');
    var fv = f ? String(f.value || '').trim() : '';
    var tv = t ? String(t.value || '').trim() : '';
    return (fv && tv) ? fv + ' — ' + tv : 'Не задано';
  }
  function dndLabel() {
    var d = document.getElementById('dnd');
    return d && d.checked ? 'Включено' : 'Выключено';
  }
  function setSummary(id, value) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = value;
    el.title = value && value !== '—' ? value : '';
  }
  function refreshSummary() {
    setSummary('summary_name', textOrDash((document.getElementById('display_name') || {}).value));
    setSummary('summary_group', selectedText(document.getElementById('group_id')));
    setSummary('summary_redmine_id', textOrDash((document.getElementById('redmine_id') || {}).value));
    var lp = (document.getElementById('room_localpart') || {}).value || '';
    var full = lp ? ('@' + lp + (matrixDomain ? ':' + matrixDomain : '')) : '';
    setSummary('summary_room', textOrDash(full));
    setSummary('summary_notify', selectedNotifyLabel());
    setSummary('summary_versions', selectedVersionsLabel());
    setSummary('summary_priorities', selectedPrioritiesLabel());
    setSummary('summary_timezone', textOrDash((document.getElementById('timezone_name') || {}).value || botTz));
    setSummary('summary_hours', selectedHours());
    setSummary('summary_dnd', dndLabel());
  }

  /* --- Generic preset/checkbox toggle factory --- */
  function initPresetToggle(presetName, checkboxName) {
    var radios = Array.from(document.querySelectorAll('input[name="' + presetName + '"]'));
    var checkboxes = Array.from(document.querySelectorAll('input[name="' + checkboxName + '"]'));
    if (!radios.length) return;

    function setPreset(val) {
      var t = radios.find(function (r) { return r.value === val; });
      if (!t || t.checked) return;
      t.checked = true;
      t.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function sync() {
      var cur = radios.find(function (r) { return r.checked; });
      if (!cur) return;
      if (cur.value === 'default') {
        checkboxes.forEach(function (cb) { cb.checked = cb.getAttribute('data-default') === 'true'; });
      }
      // При custom — не трогаем чекбоксы (пользователь выбирает сам)
      refreshSummary();
    }

    function onCheck() {
      var cur = radios.find(function (r) { return r.checked; });
      if (!cur) return;
      var defAll = checkboxes
        .filter(function (cb) { return cb.getAttribute('data-default') === 'true'; })
        .every(function (cb) { return cb.checked; });
      var nonDefNone = checkboxes
        .filter(function (cb) { return cb.getAttribute('data-default') !== 'true'; })
        .every(function (cb) { return !cb.checked; });

      if (cur.value === 'default' && !defAll) setPreset('custom');
      else if (cur.value === 'custom' && defAll && nonDefNone) setPreset('default');
      refreshSummary();
    }

    radios.forEach(function (r) { r.addEventListener('change', sync); });
    checkboxes.forEach(function (cb) { cb.addEventListener('change', onCheck); });
    sync();
  }

  initPresetToggle('status_preset', 'status_values');
  initPresetToggle('version_preset', 'version_values');
  initPresetToggle('priority_preset', 'priority_values');

  /* --- Bind summary refresh --- */
  ['display_name', 'redmine_id', 'room_localpart', 'timezone_name',
   'work_hours_from', 'work_hours_to', 'dnd', 'group_id'].forEach(function (id) {
    var el = document.getElementById(id);
    if (!el) return;
    var evt = (id === 'group_id' || id === 'dnd' || id === 'timezone_name') ? 'change' : 'input';
    el.addEventListener(evt, refreshSummary);
    if (evt !== 'change') el.addEventListener('change', refreshSummary);
  });
  Array.from(document.querySelectorAll(
    'input[name="status_values"], input[name="status_preset"],' +
    'input[name="version_values"], input[name="version_preset"],' +
    'input[name="priority_values"], input[name="priority_preset"]'
  )).forEach(function (el) { el.addEventListener('change', refreshSummary); });

  refreshSummary();

  /* --- Form validation --- */
  var form = document.querySelector('.form');
  if (form) {
    form.addEventListener('submit', function (e) {
      var ridEl = document.getElementById('redmine_id');
      var roomEl = document.getElementById('room_localpart');
      var errors = [];
      if (ridEl && !ridEl.value.trim()) { errors.push('Укажите Redmine ID'); ridEl.style.borderColor = '#f87171'; }
      else if (ridEl) ridEl.style.borderColor = '';
      if (roomEl && !roomEl.value.trim()) { errors.push('Укажите Matrix ID'); roomEl.style.borderColor = '#f87171'; }
      else if (roomEl) roomEl.style.borderColor = '';
      if (errors.length) {
        e.preventDefault();
        if (typeof showToast === 'function') showToast(errors.join('. '), true);
      }
    });
  }

  /* --- Time input auto-format --- */
  var TIME_RE = /^[0-2]\d:[0-5]\d$/;
  function validateTime(val) {
    if (!val || val.length < 4) return true;
    if (!TIME_RE.test(val)) return false;
    return parseInt(val.substring(0, 2), 10) <= 23;
  }
  document.querySelectorAll('input[name="work_hours_from"], input[name="work_hours_to"]').forEach(function (el) {
    el.addEventListener('input', function () {
      var val = el.value.replace(/[^\d:]/g, '');
      if (val.length === 2 && !val.includes(':')) val += ':';
      if (val.length > 5) val = val.substring(0, 5);
      el.value = val;
      el.classList.toggle('is-invalid', !validateTime(val));
    });
    el.addEventListener('blur', function () {
      var val = el.value.trim();
      if (!val) return;
      var m = val.match(/^(\d{1,2}):(\d{2})$/);
      if (!m) { el.value = ''; el.classList.add('is-invalid'); return; }
      var h = parseInt(m[1], 10), min = parseInt(m[2], 10);
      if (h > 23 || min > 59) { el.value = ''; el.classList.add('is-invalid'); return; }
      el.value = String(h).padStart(2, '0') + ':' + String(min).padStart(2, '0');
      el.classList.remove('is-invalid');
    });
  });
})();