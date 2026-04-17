(function () {
  var form = document.getElementById("settings-form");
  if (!form) return;

  var catalogStatus = document.getElementById("catalog-save-status");
  var persistTimer = null;

  /* --- Masked inputs highlight --- */
  function markMaskedInputs() {
    form.querySelectorAll('input[name^="secret_"]').forEach(function (input) {
      if (input.value && input.value.indexOf('••••') !== -1) {
        input.classList.add('is-masked');
      } else {
        input.classList.remove('is-masked');
      }
    });
  }
  markMaskedInputs();

  /* --- Toggle visibility (глазки) --- */
  initToggleVisibility();

  /* --- Catalog auto-save --- */
  function persistCatalog() {
    var fd = new FormData();
    var csrf = form.querySelector('input[name="csrf_token"]');
    fd.append("csrf_token", csrf ? csrf.value : "");
    fd.append("catalog_versions_json", document.getElementById("catalog_versions_json").value || "[]");
    if (catalogStatus) catalogStatus.textContent = "Сохранение справочника...";
    fetch("/onboarding/catalog/save", {
      method: "POST",
      body: fd,
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    }).then(function (resp) {
      if (!resp.ok) throw new Error("catalog_save_failed");
      if (catalogStatus) catalogStatus.textContent = "Справочник сохранен.";
    }).catch(function () {
      if (catalogStatus) catalogStatus.textContent = "Ошибка сохранения справочника.";
    });
  }

  function queuePersist() {
    if (persistTimer) clearTimeout(persistTimer);
    persistTimer = setTimeout(persistCatalog, 400);
  }

  /* --- Init catalog editors --- */
  createCatalogEditor({
    kind: "simple",
    listId: "versions-list",
    addInputId: "versions-add-input",
    addBtnId: "versions-add",
    hiddenId: "catalog_versions_json",
    onSync: queuePersist
  });

  /* --- Form submit (save) --- */
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var fd = new FormData(form);
    fetch("/onboarding/save", {
      method: "POST",
      body: fd,
      credentials: "same-origin",
      headers: { Accept: "text/html" }
    }).then(function (resp) {
      if (resp.redirected) {
        window.location.href = resp.url;
      } else {
        showToast("Изменения сохранены", false);
      }
    }).catch(function () {
      showToast("Ошибка при сохранении", true);
    });
  });

  /* --- Check access --- */
  (function () {
    var btn = document.getElementById("check-access-btn");
    var status = document.getElementById("check-access-status");
    if (!btn || !status) return;

    btn.addEventListener("click", function () {
      status.textContent = "Проверка...";
      btn.disabled = true;
      var fd = new FormData(form);
      // Добавляем CSRF токен
      var csrf = form.querySelector('input[name="csrf_token"]');
      fd.set("csrf_token", csrf ? csrf.value : "");
      fetch("/onboarding/check", {
        method: "POST",
        body: fd,
        credentials: "same-origin",
        headers: { Accept: "application/json" }
      }).then(function (resp) {
        return resp.json().catch(function () { return {}; });
      }).then(function (data) {
        if (!Array.isArray(data.checks)) {
          status.textContent = "Не удалось выполнить проверку.";
          return;
        }
        var lines = data.checks.map(function (item) {
          return (item && item.message) ? String(item.message) : "";
        }).filter(Boolean);
        status.textContent = lines.join(" ");
      }).catch(function () {
        status.textContent = "Ошибка сети при проверке.";
      }).finally(function () {
        btn.disabled = false;
      });
    });
  })();

  /* --- Regenerate DB credentials --- */
  (function () {
    var btn = document.getElementById("regenerate-db-credentials");
    var status = document.getElementById("db-regenerate-status");
    var dbPasswordInput = document.getElementById("db_password");
    var masterKeyInput = document.getElementById("master_key");
    if (!btn || !status) return;

    btn.addEventListener("click", function () {
      if (!confirm(
        "Сгенерировать новые credentials?\n\n" +
        "После этого необходимо перезапустить контейнеры:\n" +
        "docker compose restart postgres bot admin\n\n" +
        "Продолжить?"
      )) return;

      status.textContent = "Генерация...";
      btn.disabled = true;

      var fd = new FormData();
      var csrfInput = form.querySelector('input[name="csrf_token"]');
      fd.append("csrf_token", csrfInput ? csrfInput.value : "");
      fd.append("regenerate_password", "1");
      fd.append("regenerate_key", "1");

      fetch("/settings/db-config/regenerate", {
        method: "POST",
        body: fd,
        credentials: "same-origin",
        headers: { Accept: "application/json" }
      }).then(function (resp) {
        return resp.json();
      }).then(function (data) {
        if (data.ok) {
          status.textContent = data.message;
          if (dbPasswordInput && data.new_postgres_password) {
            dbPasswordInput.value = data.new_postgres_password;
            dbPasswordInput.type = "text";
          }
          if (masterKeyInput && data.new_app_master_key) {
            masterKeyInput.value = data.new_app_master_key;
            masterKeyInput.type = "text";
          }
        } else {
          status.textContent = "Ошибка: " + (data.detail || "Неизвестная ошибка");
        }
      }).catch(function () {
        status.textContent = "Ошибка сети.";
      }).finally(function () {
        btn.disabled = false;
      });
    });
  })();

  /* --- Notifications settings (Phase 2) --- */
  (function () {
    var saveBtn = document.getElementById("notifications-save-btn");
    var status = document.getElementById("notifications-save-status");
    var enabled = document.getElementById("daily_report_enabled");
    var hour = document.getElementById("daily_report_hour");
    var minute = document.getElementById("daily_report_minute");
    var htmlTpl = document.getElementById("daily_report_html_template");
    var plainTpl = document.getElementById("daily_report_plain_template");
    if (!saveBtn || !status || !enabled || !hour || !minute || !htmlTpl || !plainTpl) return;

    function csrfToken() {
      var csrfInput = form.querySelector('input[name="csrf_token"]');
      return csrfInput ? csrfInput.value : "";
    }

    function setBusy(isBusy) {
      saveBtn.disabled = !!isBusy;
    }

    function normalizeInt(raw, min, max, fallback) {
      var n = parseInt(raw, 10);
      if (!isFinite(n)) return fallback;
      if (n < min) return min;
      if (n > max) return max;
      return n;
    }

    function loadSettings() {
      status.textContent = "Загрузка настроек...";
      fetch("/api/bot/content", {
        method: "GET",
        credentials: "same-origin",
        headers: { Accept: "application/json" }
      }).then(function (resp) {
        if (!resp.ok) throw new Error("load_failed");
        return resp.json();
      }).then(function (data) {
        var s = (data && data.settings) || {};
        enabled.checked = !!s.daily_report_enabled;
        hour.value = normalizeInt(s.daily_report_hour, 0, 23, 9);
        minute.value = normalizeInt(s.daily_report_minute, 0, 59, 0);
        htmlTpl.value = s.daily_report_html_template || "";
        plainTpl.value = s.daily_report_plain_template || "";
        status.textContent = "";
      }).catch(function () {
        status.textContent = "Не удалось загрузить настройки уведомлений.";
      });
    }

    saveBtn.addEventListener("click", function () {
      setBusy(true);
      status.textContent = "Сохранение...";

      var fd = new FormData();
      fd.append("csrf_token", csrfToken());
      fd.append("daily_report_enabled", enabled.checked ? "true" : "false");
      fd.append("daily_report_hour", String(normalizeInt(hour.value, 0, 23, 9)));
      fd.append("daily_report_minute", String(normalizeInt(minute.value, 0, 59, 0)));
      fd.append("daily_report_html_template", htmlTpl.value || "");
      fd.append("daily_report_plain_template", plainTpl.value || "");

      fetch("/api/bot/content", {
        method: "POST",
        body: fd,
        credentials: "same-origin",
        headers: { Accept: "application/json" }
      }).then(function (resp) {
        if (!resp.ok) throw new Error("save_failed");
        return resp.json();
      }).then(function (data) {
        if (data && data.ok) {
          status.textContent = "Настройки уведомлений сохранены.";
          showToast("Настройки уведомлений сохранены", false);
        } else {
          status.textContent = "Ошибка сохранения.";
        }
      }).catch(function () {
        status.textContent = "Ошибка сети при сохранении.";
      }).finally(function () {
        setBusy(false);
      });
    });

    loadSettings();
  })();
})();