/* global Sortable */
(function () {
  "use strict";

  function readBootstrap() {
    var el = document.getElementById("block-editor-bootstrap");
    if (!el || !el.textContent) return { registry: [], editor_template_names: [], template_display_names: {} };
    try {
      return JSON.parse(el.textContent);
    } catch (e) {
      console.error("block-editor-bootstrap parse failed", e);
      return { registry: [], editor_template_names: [], template_display_names: {} };
    }
  }

  function csrfToken() {
    var form = document.getElementById("settings-form");
    var input = form && form.querySelector('input[name="csrf_token"]');
    if (input && input.value) return input.value;
    var meta = document.querySelector('meta[name="csrf-token"]');
    return (meta && meta.content) || "";
  }

  function BlockEditor(container, templateName) {
    this.container = container;
    this.templateName = templateName;
    var boot = readBootstrap();
    var labels = boot.template_display_names || {};
    this.displayName = labels[templateName] || templateName;
    this.registry = boot.registry || [];
    this.blocks = [];
    this.defaultBlocks = [];
    this.isCustomJinja = false;
    this.mode = "blocks";
    this.bodyHtml = "";
    this.compiledJinja = "";
    this.draftKey = "block_editor_draft_" + templateName;
    this._dirty = false;
    this.sortable = null;
    this.previewEl = null;
    this.previewContentEl = null;
    this.blockListEl = null;
    this._previewTimer = null;
    this._statusClearTimer = null;
    this._beforeUnload = this._beforeUnload.bind(this);
  }

  BlockEditor.prototype.init = async function () {
    var resp = await fetch(
      "/api/bot/notification-templates/" + encodeURIComponent(this.templateName) + "/decompose",
      { credentials: "same-origin", headers: { Accept: "application/json" } }
    );
    if (!resp.ok) throw new Error("decompose_failed");
    var data = await resp.json();
    this.blocks = data.blocks || data.default_blocks || [];
    this.defaultBlocks = data.default_blocks || [];
    this.isCustomJinja = !!data.is_custom_jinja;
    this.bodyHtml = data.body_html || "";
    this.mode = this.isCustomJinja ? "code" : "blocks";

    var draft = this.loadDraft();
    if (draft && window.confirm("Найден несохранённый черновик. Восстановить?")) {
      this.blocks = draft.blocks || this.blocks;
      this.bodyHtml = draft.bodyHtml || this.bodyHtml;
      this.mode = draft.mode || this.mode;
    }

    this.render();
    if (this.mode === "blocks") this.refreshPreview();
  };

  BlockEditor.prototype.destroy = function () {
    this.markClean();
    clearTimeout(this._previewTimer);
    this._previewTimer = null;
    if (this._statusClearTimer) {
      clearTimeout(this._statusClearTimer);
      this._statusClearTimer = null;
    }
    if (this.sortable) {
      this.sortable.destroy();
      this.sortable = null;
    }
    this.container.innerHTML = "";
    this.previewEl = null;
    this.previewContentEl = null;
    this.blockListEl = null;
  };

  BlockEditor.prototype.render = function () {
    if (this.sortable) {
      this.sortable.destroy();
      this.sortable = null;
    }
    this.container.innerHTML = "";
    this.container.className = "block-editor";
    this.previewEl = null;
    this.previewContentEl = null;
    this.blockListEl = null;

    var header = document.createElement("div");
    header.className = "block-editor__header";
    this.renderTabs(header);
    this.renderVariablesList(header);
    this.container.appendChild(header);

    var body = document.createElement("div");
    body.className = "block-editor__body";
    var main = document.createElement("div");
    main.className = "block-editor__main";
    if (this.mode === "blocks") {
      this.renderBlockList(main);
    } else {
      this.renderCodeEditor(main);
    }
    body.appendChild(main);
    var aside = document.createElement("div");
    aside.className = "block-editor__aside";
    this.renderPreviewArea(aside);
    body.appendChild(aside);
    this.container.appendChild(body);

    this.renderActions();
  };

  BlockEditor.prototype.renderTabs = function (parent) {
    var tabs = document.createElement("div");
    tabs.className = "block-editor__tabs";
    var self = this;
    tabs.innerHTML =
      '<button type="button" class="block-editor__tab' +
      (this.mode === "blocks" ? " active" : "") +
      '" data-mode="blocks">Блоки</button>' +
      '<button type="button" class="block-editor__tab' +
      (this.mode === "code" ? " active" : "") +
      '" data-mode="code">&lt;/&gt; Код</button>';
    tabs.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-mode]");
      if (!btn) return;
      var mode = btn.getAttribute("data-mode");
      if (!mode || mode === self.mode) return;
      if (mode === "blocks") self.switchToBlocks();
      else self.switchToCode();
    });
    parent.appendChild(tabs);
  };

  BlockEditor.prototype.renderVariablesList = function (parent) {
    var blockIds = {};
    this.blocks.forEach(function (bc) {
      blockIds[bc.block_id] = true;
    });
    var all = {};
    this.registry.forEach(function (b) {
      if (!blockIds[b.id]) return;
      (b.variables || []).forEach(function (v) {
        all[v] = true;
      });
    });
    var p = document.createElement("p");
    p.className = "block-editor__variables";
    var keys = Object.keys(all);
    p.textContent = keys.length ? "Переменные: " + keys.join(" · ") : "Переменные: —";
    parent.appendChild(p);
  };

  BlockEditor.prototype.renderBlockList = function (parent) {
    var list = document.createElement("div");
    list.className = "block-editor__list";
    this.blockListEl = list;
    var self = this;
    var sorted = this.blocks.slice().sort(function (a, b) {
      return a.order - b.order;
    });
    sorted.forEach(function (bc) {
      var def = self.registry.find(function (r) {
        return r.id === bc.block_id;
      });
      if (!def) return;
      var el = document.createElement("div");
      el.className = "block-editor__block" + (bc.enabled ? "" : " disabled");
      el.dataset.blockId = bc.block_id;
      el.innerHTML =
        '<span class="block-editor__handle" title="Перетащить">☰</span>' +
        '<label class="block-editor__toggle">' +
        '<input type="checkbox" ' +
        (bc.enabled ? "checked" : "") +
        ' data-block="' +
        bc.block_id +
        '">' +
        "<strong>" +
        def.label +
        "</strong></label>" +
        '<span class="block-editor__desc">' +
        def.description +
        "</span>" +
        '<div class="block-editor__settings" style="' +
        (bc.enabled ? "" : "display:none") +
        '"></div>';
      var cb = el.querySelector('input[type="checkbox"]');
      cb.addEventListener("change", function (e) {
        self.onBlockToggle(bc.block_id, e.target.checked);
      });
      if (bc.enabled && def.settings_schema && Object.keys(def.settings_schema).length) {
        self.renderBlockSettings(el.querySelector(".block-editor__settings"), bc, def);
      }
      list.appendChild(el);
    });
    parent.appendChild(list);
    if (typeof Sortable !== "undefined" && this.blockListEl) {
      this.sortable = new Sortable(this.blockListEl, {
        handle: ".block-editor__handle",
        animation: 150,
        ghostClass: "block-editor__block--ghost",
        onEnd: function () {
          var items = self.blockListEl.querySelectorAll(".block-editor__block");
          items.forEach(function (node, i) {
            var bid = node.dataset.blockId;
            var blk = self.blocks.find(function (b) {
              return b.block_id === bid;
            });
            if (blk) blk.order = i;
          });
          self.markDirty();
          self.refreshPreview();
        },
      });
    }
  };

  BlockEditor.prototype.renderBlockSettings = function (container, blockConfig, blockDef) {
    var self = this;
    Object.keys(blockDef.settings_schema || {}).forEach(function (key) {
      var schema = blockDef.settings_schema[key];
      var currentValue =
        blockConfig.settings && blockConfig.settings[key] != null
          ? String(blockConfig.settings[key])
          : String(schema.default || "");
      var wrapper = document.createElement("div");
      wrapper.className = "block-editor__setting";
      if (schema.type === "emoji_select") {
        var opts = schema.options || [];
        var isCustom = opts.indexOf(currentValue) === -1;
        var selHtml =
          "<label>" +
          key +
          ":</label><select data-block=\"" +
          blockConfig.block_id +
          '" data-key="' +
          key +
          '">';
        opts.forEach(function (o) {
          selHtml +=
            '<option value="' +
            self.escapeAttr(o) +
            '"' +
            (o === currentValue ? " selected" : "") +
            ">" +
            o +
            "</option>";
        });
        selHtml +=
          '<option value="__custom__"' +
          (isCustom ? " selected" : "") +
          ">Свой…</option></select>" +
          '<input type="text" class="emoji-custom" maxlength="8" value="' +
          self.escapeAttr(isCustom ? currentValue : "") +
          '" placeholder="🎯" style="' +
          (isCustom ? "" : "display:none") +
          '">';
        wrapper.innerHTML = selHtml;
        var select = wrapper.querySelector("select");
        var customInput = wrapper.querySelector(".emoji-custom");
        select.addEventListener("change", function () {
          if (select.value === "__custom__") {
            customInput.style.display = "";
            customInput.focus();
          } else {
            customInput.style.display = "none";
            self.onSettingChange(blockConfig.block_id, key, select.value);
          }
        });
        customInput.addEventListener("input", function () {
          if (customInput.value)
            self.onSettingChange(blockConfig.block_id, key, customInput.value);
        });
      } else {
        wrapper.innerHTML =
          "<label>" +
          key +
          ':</label><input type="text" value="' +
          self.escapeAttr(currentValue) +
          '" data-block="' +
          blockConfig.block_id +
          '" data-key="' +
          key +
          '">';
        wrapper.querySelector("input").addEventListener("input", function (e) {
          self.onSettingChange(blockConfig.block_id, key, e.target.value);
        });
      }
      container.appendChild(wrapper);
    });
  };

  BlockEditor.prototype.escapeAttr = function (s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;");
  };

  // *** FIX: added `parent` parameter ***
  BlockEditor.prototype.renderCodeEditor = function (parent) {
    var wrap = document.createElement("div");
    wrap.className = "block-editor__code";
    if (!this.isCustomJinja) {
      var warn = document.createElement("p");
      warn.className = "block-editor__code-warning";
      warn.textContent =
        "Ручные правки Jinja могут сделать шаблон несовместимым с конструктором блоков.";
      wrap.appendChild(warn);
    }
    var ta = document.createElement("textarea");
    ta.className = "block-editor__textarea";
    ta.rows = 12;
    ta.value = this.bodyHtml || "";
    var self = this;
    ta.addEventListener("input", function () {
      self.bodyHtml = ta.value;
      self.markDirty();
    });
    wrap.appendChild(ta);
    parent.appendChild(wrap);
  };

  BlockEditor.prototype.renderPreviewArea = function (parent) {
    var preview = document.createElement("div");
    preview.className = "block-editor__preview";
    var title = document.createElement("p");
    title.className = "block-editor__preview-heading";
    title.textContent = "Предпросмотр";
    preview.appendChild(title);
    var content = document.createElement("div");
    content.className = "block-editor__preview-content";
    content.innerHTML = '<p class="muted">Загрузка предпросмотра…</p>';
    preview.appendChild(content);
    this.previewEl = preview;
    this.previewContentEl = content;
    parent.appendChild(preview);
  };

  BlockEditor.prototype.showStatus = function (message, isError) {
    var el = this.container.querySelector(".block-editor__status");
    if (!el) return;
    if (this._statusClearTimer) {
      clearTimeout(this._statusClearTimer);
      this._statusClearTimer = null;
    }
    el.textContent = message || "";
    el.classList.remove("block-editor__status--error", "block-editor__status--ok");
    el.classList.add(isError ? "block-editor__status--error" : "block-editor__status--ok");
    if (!isError && message) {
      var self = this;
      this._statusClearTimer = setTimeout(function () {
        self._statusClearTimer = null;
        var cur = self.container.querySelector(".block-editor__status");
        if (cur && !cur.classList.contains("block-editor__status--error")) {
          cur.textContent = "";
          cur.classList.remove("block-editor__status--ok");
        }
      }, 4000);
    }
  };

  BlockEditor.prototype.renderActions = function () {
    var footer = document.createElement("div");
    footer.className = "block-editor__footer";
    var status = document.createElement("span");
    status.className = "block-editor__status";
    footer.appendChild(status);
    var actions = document.createElement("div");
    actions.className = "block-editor__footer-actions";
    actions.innerHTML =
      '<button type="button" class="btn btn-primary" data-action="save">Сохранить</button>' +
      '<button type="button" class="btn btn-ghost" data-action="reset">Сбросить</button>';
    footer.appendChild(actions);
    var self = this;
    footer.querySelector('[data-action="save"]').addEventListener("click", function () {
      self.save();
    });
    footer.querySelector('[data-action="reset"]').addEventListener("click", function () {
      self.reset();
    });
    this.container.appendChild(footer);
  };

  BlockEditor.prototype.onBlockToggle = function (blockId, enabled) {
    var block = this.blocks.find(function (b) {
      return b.block_id === blockId;
    });
    if (block) block.enabled = enabled;
    this.markDirty();
    this.render();
    if (this.mode === "blocks") this.refreshPreview();
  };

  BlockEditor.prototype.onSettingChange = function (blockId, key, value) {
    var block = this.blocks.find(function (b) {
      return b.block_id === blockId;
    });
    if (!block) return;
    block.settings = block.settings || {};
    block.settings[key] = value;
    this.markDirty();
    this.refreshPreview();
  };

  BlockEditor.prototype.switchToCode = async function () {
    if (this.mode !== "blocks") return;
    try {
      var data = await this.compileBlocks();
      this.bodyHtml = data.jinja;
      this.compiledJinja = data.jinja;
    } catch (e) {
      this.showStatus("Ошибка компиляции: " + (e.message || String(e)), true);
      return;
    }
    this.mode = "code";
    this.render();
  };

  BlockEditor.prototype.switchToBlocks = async function () {
    if (this.mode !== "code") return;
    var ta = this.container.querySelector(".block-editor__textarea");
    var currentCode = (ta && ta.value) || this.bodyHtml || "";
    try {
      window.sessionStorage.setItem(this.draftKey + "_code", currentCode);
      var resp = await fetch(
        "/api/bot/notification-templates/" +
          encodeURIComponent(this.templateName) +
          "/decompose-body",
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
            "X-CSRF-Token": csrfToken(),
          },
          body: JSON.stringify({ body_html: currentCode }),
        }
      );
      var data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "decompose_failed");
      if (data.blocks) {
        this.blocks = data.blocks;
        this.mode = "blocks";
        this.render();
        this.refreshPreview();
      } else {
        var useDefaults = window.confirm(
          "Шаблон содержит ручные изменения, которые конструктор не может распознать.\n\n" +
            "Загрузить блоки по умолчанию?"
        );
        if (useDefaults) {
          this.blocks = JSON.parse(JSON.stringify(data.default_blocks || this.defaultBlocks));
          this.mode = "blocks";
          this.render();
          this.refreshPreview();
        }
      }
    } catch (e) {
      this.showStatus("Ошибка при разборе шаблона: " + (e.message || String(e)), true);
    }
  };

  BlockEditor.prototype.compileBlocks = async function () {
    var resp = await fetch("/api/bot/notification-templates/compile-blocks", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-CSRF-Token": csrfToken(),
      },
      body: JSON.stringify({
        blocks: this.blocks,
        template_name: this.templateName,
      }),
    });
    var data = await resp.json().catch(function () {
      return {};
    });
    if (!resp.ok) throw new Error(data.error || data.detail || "Compile failed");
    this.compiledJinja = data.jinja;
    return data;
  };

  BlockEditor.prototype.refreshPreview = function () {
    var self = this;
    if (this.mode !== "blocks") return;
    clearTimeout(this._previewTimer);
    if (this.previewEl) this.previewEl.classList.add("block-editor__preview--loading");
    this._previewTimer = setTimeout(async function () {
      try {
        var data = await self.compileBlocks();
        if (self.previewContentEl) self.previewContentEl.innerHTML = data.html_preview || "";
      } catch (e) {
        if (self.previewContentEl)
          self.previewContentEl.innerHTML =
            '<p class="block-editor__error">Ошибка: ' +
            self.escapeHtml(e.message || String(e)) +
            "</p>";
      } finally {
        if (self.previewEl) self.previewEl.classList.remove("block-editor__preview--loading");
      }
    }, 500);
  };

  BlockEditor.prototype.escapeHtml = function (s) {
    var d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  };

  BlockEditor.prototype.save = async function () {
    var saveBtn = this.container.querySelector('[data-action="save"]');
    var origSaveLabel = saveBtn ? saveBtn.textContent : "Сохранить";
    var restoreSave = function () {
      if (saveBtn) {
        saveBtn.disabled = false;
        saveBtn.textContent = origSaveLabel;
      }
    };
    if (!csrfToken()) {
      this.showStatus("Ошибка: отсутствует CSRF-токен", true);
      return;
    }
    if (saveBtn) {
      saveBtn.disabled = true;
      saveBtn.textContent = "Сохранение…";
    }
    try {
      if (this.mode === "blocks") {
        try {
          var data = await this.compileBlocks();
          this.bodyHtml = data.jinja;
        } catch (e) {
          this.showStatus("Ошибка компиляции: " + (e.message || String(e)), true);
          restoreSave();
          return;
        }
      }
      var fd = new FormData();
      fd.append("body_html", this.bodyHtml || "");
      fd.append("body_plain", "");
      fd.append("csrf_token", csrfToken());
      var resp = await fetch(
        "/api/bot/notification-templates/" + encodeURIComponent(this.templateName),
        { method: "PUT", body: fd, credentials: "same-origin", headers: { Accept: "application/json" } }
      );
      var errBody = await resp.json().catch(function () {
        return {};
      });
      if (!resp.ok)
        throw new Error(errBody.detail || errBody.error || "HTTP " + resp.status);
      this.markClean();
      this.clearDraft();
      this.showStatus("Шаблон «" + this.displayName + "» сохранён", false);
      if (typeof window.showToast === "function")
        window.showToast("Шаблон " + this.displayName + " сохранён", false);
    } catch (e) {
      this.showStatus("Ошибка сохранения: " + (e.message || String(e)), true);
    } finally {
      restoreSave();
    }
  };

  BlockEditor.prototype.reset = async function () {
    if (!csrfToken()) {
      this.showStatus("Ошибка: отсутствует CSRF-токен", true);
      return;
    }
    if (!window.confirm("Сбросить шаблон к значению по умолчанию?")) return;
    var resetBtn = this.container.querySelector('[data-action="reset"]');
    var origResetLabel = resetBtn ? resetBtn.textContent : "Сбросить";
    var restoreReset = function () {
      if (resetBtn) {
        resetBtn.disabled = false;
        resetBtn.textContent = origResetLabel;
      }
    };
    if (resetBtn) {
      resetBtn.disabled = true;
      resetBtn.textContent = "Сброс…";
    }
    try {
      var fd = new FormData();
      fd.append("csrf_token", csrfToken());
      var resp = await fetch(
        "/api/bot/notification-templates/" + encodeURIComponent(this.templateName) + "/reset",
        { method: "POST", body: fd, credentials: "same-origin", headers: { Accept: "application/json" } }
      );
      if (!resp.ok) throw new Error("reset_failed");
      await this.init();
      this.markClean();
      this.clearDraft();
      this.showStatus("Шаблон сброшен к значению по умолчанию", false);
    } catch (e) {
      this.showStatus("Ошибка сброса: " + (e.message || String(e)), true);
    } finally {
      restoreReset();
    }
  };

  BlockEditor.prototype.markDirty = function () {
    this._dirty = true;
    this.saveDraft();
    window.addEventListener("beforeunload", this._beforeUnload);
  };

  BlockEditor.prototype.markClean = function () {
    this._dirty = false;
    window.removeEventListener("beforeunload", this._beforeUnload);
  };

  BlockEditor.prototype._beforeUnload = function (e) {
    if (!this._dirty) return;
    e.preventDefault();
    e.returnValue = "";
  };

  BlockEditor.prototype.saveDraft = function () {
    try {
      window.sessionStorage.setItem(
        this.draftKey,
        JSON.stringify({
          mode: this.mode,
          blocks: this.blocks,
          bodyHtml: this.bodyHtml,
          timestamp: Date.now(),
        })
      );
    } catch (ignore) {}
  };

  BlockEditor.prototype.loadDraft = function () {
    try {
      var raw = window.sessionStorage.getItem(this.draftKey);
      if (!raw) return null;
      var d = JSON.parse(raw);
      if (Date.now() - d.timestamp > 30 * 60 * 1000) {
        window.sessionStorage.removeItem(this.draftKey);
        return null;
      }
      return d;
    } catch (e) {
      return null;
    }
  };

  BlockEditor.prototype.clearDraft = function () {
    try {
      window.sessionStorage.removeItem(this.draftKey);
      window.sessionStorage.removeItem(this.draftKey + "_code");
    } catch (ignore) {}
  };

  window.BlockEditor = BlockEditor;
})();