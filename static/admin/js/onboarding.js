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
    var notificationTypes = [
      "new",
      "reopened",
      "info",
      "reminder",
      "overdue",
      "issue_updated",
      "status_change"
    ];

    function getEventTemplateInputs(kind) {
      return {
        html: document.getElementById("nt_html_" + kind),
        plain: document.getElementById("nt_plain_" + kind)
      };
    }

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
        var templates = s.notification_templates || {};
        notificationTypes.forEach(function (kind) {
          var pair = getEventTemplateInputs(kind);
          var tpl = templates[kind] || {};
          if (pair.html) pair.html.value = tpl.html || "";
          if (pair.plain) pair.plain.value = tpl.plain || "";
        });
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
      var templatesPayload = {};
      notificationTypes.forEach(function (kind) {
        var pair = getEventTemplateInputs(kind);
        templatesPayload[kind] = {
          html: pair.html ? (pair.html.value || "") : "",
          plain: pair.plain ? (pair.plain.value || "") : ""
        };
      });
      fd.append("notification_templates_json", JSON.stringify(templatesPayload));

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

  /* --- Journal engine Jinja2 templates (notification_templates) --- */
  (function () {
    var root = document.getElementById("tpl-v2-fields");
    var statusEl = document.getElementById("tpl-v2-status");
    if (!root || !statusEl) return;

    function csrfToken() {
      var csrfInput = form.querySelector('input[name="csrf_token"]');
      return csrfInput ? csrfInput.value : "";
    }

    function loadV2() {
      statusEl.textContent = "Загрузка шаблонов v2…";
      var editorNames = {};
      var bootEl = document.getElementById("block-editor-bootstrap");
      if (bootEl && bootEl.textContent) {
        try {
          var bt = JSON.parse(bootEl.textContent);
          (bt.editor_template_names || []).forEach(function (n) {
            editorNames[n] = true;
          });
        } catch (ignore) {}
      }
      fetch("/api/bot/notification-templates", {
        method: "GET",
        credentials: "same-origin",
        headers: { Accept: "application/json" }
      }).then(function (resp) {
        if (!resp.ok) throw new Error("load_failed");
        return resp.json();
      }).then(function (data) {
        root.querySelectorAll(".block-editor-root").forEach(function (el) {
          if (el._blockEditor && typeof el._blockEditor.destroy === "function") {
            el._blockEditor.destroy();
          }
          el._blockEditor = null;
        });
        root.innerHTML = "";
        (data.templates || []).forEach(function (tpl) {
          var displayLabel = tpl.display_name || tpl.name;
          var wrap = document.createElement("div");
          wrap.className = "field";
          var title = document.createElement("div");
          title.className = "card-title";
          title.style.marginTop = "0.5rem";
          title.textContent = displayLabel;
          wrap.appendChild(title);
          if (editorNames[tpl.name]) {
            var bed = document.createElement("div");
            bed.className = "block-editor-root";
            bed.setAttribute("data-template-name", tpl.name);
            wrap.appendChild(bed);
            if (typeof window.BlockEditor === "function") {
              var editor = new window.BlockEditor(bed, tpl.name);
              bed._blockEditor = editor;
              editor.init().catch(function (err) {
                console.error("BlockEditor init failed", err);
                bed.innerHTML = "<p class=\"error\">Не удалось загрузить конструктор</p>";
              });
            } else {
              bed.innerHTML = "<p class=\"error\">Конструктор блоков не загружен</p>";
            }
          } else {
            var lab = document.createElement("label");
            wrap.appendChild(lab);
            var ta = document.createElement("textarea");
            ta.className = "tpl-v2-html";
            ta.setAttribute("data-name", tpl.name);
            ta.rows = 6;
            ta.value = (tpl.override_html != null && tpl.override_html !== "")
              ? tpl.override_html
              : (tpl.default_html || "");
            wrap.appendChild(ta);
            var footer = document.createElement("div");
            footer.className = "block-editor__footer";
            footer.style.marginTop = "0.5rem";
            var st = document.createElement("span");
            st.className = "block-editor__status";
            footer.appendChild(st);
            var actions = document.createElement("div");
            actions.className = "block-editor__footer-actions";
            ["Сохранить", "Сбросить", "Предпросмотр"].forEach(function (label, idx) {
              var b = document.createElement("button");
              b.type = "button";
              b.textContent = label;
              b.className = idx === 0 ? "btn btn-primary" : "btn btn-ghost";
              if (idx === 0) {
                b.classList.add("tpl-v2-save");
                b.setAttribute("data-action", "save");
              }
              if (idx === 1) {
                b.classList.add("tpl-v2-reset");
                b.setAttribute("data-action", "reset");
              }
              if (idx === 2) b.classList.add("tpl-v2-preview");
              b.setAttribute("data-name", tpl.name);
              b.setAttribute("data-display-label", displayLabel);
              actions.appendChild(b);
            });
            footer.appendChild(actions);
            wrap.appendChild(footer);
            var pre = document.createElement("pre");
            pre.className = "tpl-v2-preview-out muted";
            pre.setAttribute("data-name", tpl.name);
            pre.style.whiteSpace = "pre-wrap";
            pre.style.maxHeight = "12rem";
            pre.style.overflow = "auto";
            wrap.appendChild(pre);
          }
          root.appendChild(wrap);
        });
        root.querySelectorAll(".tpl-v2-save").forEach(function (btn) {
          btn.addEventListener("click", function () {
            var name = btn.getAttribute("data-name");
            var label = btn.getAttribute("data-display-label") || name;
            var ta = root.querySelector('.tpl-v2-html[data-name="' + name + '"]');
            var fd = new FormData();
            fd.append("csrf_token", csrfToken());
            fd.append("body_html", ta ? ta.value : "");
            fd.append("body_plain", "");
            statusEl.textContent = "Сохранение " + label + "…";
            fetch("/api/bot/notification-templates/" + encodeURIComponent(name), {
              method: "PUT",
              body: fd,
              credentials: "same-origin",
              headers: { Accept: "application/json" }
            }).then(function (resp) {
              if (!resp.ok) throw new Error("save_failed");
              statusEl.textContent = "Сохранено: " + label;
              showToast("Шаблон " + label + " сохранён", false);
            }).catch(function () {
              statusEl.textContent = "Ошибка сохранения " + label;
            });
          });
        });
        root.querySelectorAll(".tpl-v2-reset").forEach(function (btn) {
          btn.addEventListener("click", function () {
            var name = btn.getAttribute("data-name");
            var label = btn.getAttribute("data-display-label") || name;
            var fd = new FormData();
            fd.append("csrf_token", csrfToken());
            fetch("/api/bot/notification-templates/" + encodeURIComponent(name) + "/reset", {
              method: "POST",
              body: fd,
              credentials: "same-origin",
              headers: { Accept: "application/json" }
            }).then(function (resp) {
              if (!resp.ok) throw new Error("reset_failed");
              loadV2();
            }).catch(function () {
              statusEl.textContent = "Ошибка сброса " + label;
            });
          });
        });
        root.querySelectorAll(".tpl-v2-preview").forEach(function (btn) {
          btn.addEventListener("click", function () {
            var name = btn.getAttribute("data-name");
            var ta = root.querySelector('.tpl-v2-html[data-name="' + name + '"]');
            var pre = root.querySelector('.tpl-v2-preview-out[data-name="' + name + '"]');
            fetch("/api/bot/notification-templates/preview", {
              method: "POST",
              credentials: "same-origin",
              headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": csrfToken(),
                Accept: "application/json"
              },
              body: JSON.stringify({ name: name, body_html: ta ? ta.value : "" })
            }).then(function (resp) { return resp.json(); }).then(function (d) {
              if (pre) pre.textContent = (d && d.html) ? String(d.html) : "";
            }).catch(function () {
              if (pre) pre.textContent = "Ошибка предпросмотра";
            });
          });
        });
        statusEl.textContent = "";
      }).catch(function () {
        statusEl.textContent = "Не удалось загрузить шаблоны v2.";
      });
    }

    window.addEventListener("via-settings-tab", function (ev) {
      if (ev.detail && ev.detail.tab === "notifications") loadV2();
    });
    if (window.location.hash === "#notifications") loadV2();
  })();
})();