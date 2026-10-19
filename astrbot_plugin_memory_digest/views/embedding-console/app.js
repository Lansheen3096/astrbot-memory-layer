const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);

const state = {
  status: null,
  mode: "kb",
  busy: false,
  theme: "light",
  manualTheme: false,
};

function t(key, fallback, vars) {
  let text = fallback;
  try {
    text = bridge.t(`views.embedding-console.${key}`, fallback);
  } catch (_error) {
    text = fallback;
  }
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.split(`{${name}}`).join(String(value));
    }
  }
  return text;
}

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    if (!el.dataset.fallback) {
      el.dataset.fallback = el.textContent.trim();
    }
    el.textContent = t(el.dataset.i18n, el.dataset.fallback);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    if (!el.dataset.fallbackPlaceholder) {
      el.dataset.fallbackPlaceholder = el.getAttribute("placeholder") || "";
    }
    el.setAttribute(
      "placeholder",
      t(el.dataset.i18nPlaceholder, el.dataset.fallbackPlaceholder),
    );
  });
  refreshKeyPlaceholder();
}

function refreshKeyPlaceholder() {
  const cloud = state.status?.config?.cloud;
  if (!cloud) {
    return;
  }
  const input = $("cloud_api_key");
  input.placeholder = cloud.api_key_set
    ? t("key_saved", "已保存（{mask}），留空保持不变", {
        mask: cloud.api_key_hint || "••••",
      })
    : t("key_placeholder", "留空则保持已保存的 Key");
}

function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function applyTheme(theme, manual = false) {
  const isDark = theme === "dark";
  document.documentElement.dataset.theme = isDark ? "dark" : "light";
  const icon = $("theme-icon");
  const label = $("theme-label");
  const toggle = $("theme-toggle");
  if (icon) {
    icon.textContent = isDark ? "☀" : "☾";
  }
  if (label) {
    label.textContent = isDark
      ? t("theme_dark", "深色")
      : t("theme_light", "浅色");
  }
  if (toggle) {
    toggle.setAttribute("aria-label", t("theme_toggle", "切换深色模式"));
    toggle.setAttribute("title", t("theme_toggle", "切换深色模式"));
    toggle.setAttribute("aria-pressed", isDark ? "true" : "false");
  }
  state.theme = isDark ? "dark" : "light";
  if (manual) {
    state.manualTheme = true;
  }
}

function toggleTheme() {
  applyTheme(currentTheme() === "dark" ? "light" : "dark", true);
}

function log(message) {
  const el = $("log");
  const stamp = new Date().toLocaleTimeString();
  const line = `[${stamp}] ${message}`;
  el.textContent = el.textContent ? `${el.textContent}\n${line}` : line;
  el.scrollTop = el.scrollHeight;
}

function setBusy(busy, which) {
  state.busy = busy;
  $("test").disabled = busy;
  $("save").disabled = busy;
  $("refresh").disabled = busy;
  document.querySelectorAll(".seg").forEach((button) => {
    button.disabled = busy;
  });
  if (busy) {
    $("test").textContent =
      which === "test" ? t("testing", "正在测试……") : t("test", "测试连接");
    $("save").textContent =
      which === "save" ? t("saving", "正在保存并绑定……") : t("save", "保存并应用");
  } else {
    $("test").textContent = t("test", "测试连接");
    $("save").textContent = t("save", "保存并应用");
  }
}

function setText(id, value) {
  const el = $(id);
  if (el) {
    el.textContent = value;
  }
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll("[data-mode]").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === mode);
    button.setAttribute("aria-selected", button.dataset.mode === mode ? "true" : "false");
  });
  $("panel-kb").hidden = mode !== "kb";
  $("panel-cloud").hidden = mode !== "cloud";
  $("panel-local").hidden = mode !== "local";
}

function fillForm(config) {
  const cloud = config.cloud || {};
  const local = config.local || {};

  $("cloud_api_base").value = cloud.api_base || "";
  $("cloud_model").value = cloud.model || "";
  $("cloud_dimensions").value = cloud.dimensions > 0 ? String(cloud.dimensions) : "";
  $("cloud_api_key").value = "";

  $("local_host").value = local.host || "127.0.0.1";
  $("local_port").value = local.port || 11434;
  $("local_api").value = local.api || "ollama";
  $("local_model").value = local.model || "bge-m3";
  $("local_dimensions").value =
    local.dimensions > 0 ? String(local.dimensions) : "";

  $("timeout").value = config.timeout || 60;
  refreshKeyPlaceholder();

  setMode(config.embedding_mode || "kb");
}

function renderStatus(data) {
  const kb = data.kb || {};
  const digest = data.digest || {};

  const badge = $("kb-state-badge");
  if (kb.exists) {
    badge.textContent = t("status_loaded", "已加载");
    badge.className = "badge ok";
  } else {
    badge.textContent = t("status_missing", "不存在");
    badge.className = "badge warn";
  }

  setText("stat-kb", kb.name || "—");
  setText(
    "stat-provider",
    kb.embedding_provider_id || t("status_none", "未配置"),
  );
  $("stat-provider").title = kb.embedding_base || "";
  setText(
    "stat-managed",
    kb.exists
      ? kb.managed_by_plugin
        ? t("status_managed", "由本插件管理")
        : t("status_unmanaged", "外部配置")
      : "",
  );
  setText("stat-model", kb.embedding_model || "—");
  setText("stat-dim", kb.vector_dim ? String(kb.vector_dim) : "—");
  setText("stat-docs", String(kb.doc_count ?? "—"));
  setText("stat-pending", String(digest.pending_conversations ?? "—"));
}

function collectPayload() {
  const intValue = (id, fallback) => {
    const raw = $(id).value.trim();
    if (raw === "") {
      return fallback;
    }
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : fallback;
  };

  return {
    embedding_mode: state.mode,
    cloud: {
      api_base: $("cloud_api_base").value.trim(),
      api_key: $("cloud_api_key").value,
      model: $("cloud_model").value.trim(),
      dimensions: intValue("cloud_dimensions", 0),
    },
    local: {
      host: $("local_host").value.trim(),
      port: intValue("local_port", 11434),
      api: $("local_api").value,
      model: $("local_model").value.trim(),
      dimensions: intValue("local_dimensions", 0),
    },
    timeout: intValue("timeout", 60),
    create_kb: $("create_kb").checked,
    force: $("force").checked,
  };
}

async function loadStatus(showLog = false) {
  const data = await bridge.apiGet("status");
  state.status = data;
  fillForm(data.config || {});
  renderStatus(data);
  if (showLog) {
    log(t("refresh_ok", "状态已刷新。"));
  }
}

async function runTest() {
  if (state.busy) {
    return;
  }
  setBusy(true, "test");
  log(t("testing", "正在测试……"));
  try {
    const result = await bridge.apiPost("test", collectPayload());
    if (result.mode === "kb") {
      log(result.message || "当前是「跟随知识库」模式。");
    } else {
      log(
        t("test_ok", "连接正常：{model}，维度 {dim}，耗时 {ms} ms。", {
          model: result.model || "?",
          dim: result.dimension,
          ms: result.latency_ms,
        }),
      );
      for (const warning of result.warnings || []) {
        log(`⚠ ${warning}`);
      }
    }
  } catch (error) {
    log(
      t("error_prefix", "失败：{message}", {
        message: error?.message || String(error),
      }),
    );
  } finally {
    setBusy(false);
  }
}

async function runSave() {
  if (state.busy) {
    return;
  }
  setBusy(true, "save");
  log(t("saving", "正在保存并绑定……"));
  try {
    const result = await bridge.apiPost("save", collectPayload());
    log(
      t("save_ok", "已保存并应用：{message}", {
        message: result.message || t("saved", "已保存"),
      }),
    );
    for (const warning of result.warnings || []) {
      log(`⚠ ${warning}`);
    }
    try {
      await loadStatus();
    } catch (refreshError) {
      log(
        t("error_prefix", "失败：{message}", {
          message: `状态刷新失败：${refreshError?.message || String(refreshError)}`,
        }),
      );
    }
  } catch (error) {
    log(
      t("error_prefix", "失败：{message}", {
        message: error?.message || String(error),
      }),
    );
  } finally {
    setBusy(false);
  }
}

async function main() {
  if (!bridge) {
    document.body.innerHTML =
      '<main class="wrap"><section class="card"><h1>Bridge unavailable</h1><p>请从 AstrBot WebUI 的插件页打开此视图。</p></section></main>';
    return;
  }

  let context = null;
  try {
    context = await Promise.race([
      bridge.ready(),
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error("bridge ready timeout")), 5000),
      ),
    ]);
  } catch (error) {
    log(`Bridge 初始化失败：${error?.message || String(error)}`);
    return;
  }

  applyI18n();
  const initialTheme =
    typeof context?.isDark === "boolean"
      ? context.isDark
        ? "dark"
        : "light"
      : currentTheme();
  applyTheme(initialTheme);
  setMode("kb");

  document.querySelectorAll("[data-mode]").forEach((button) => {
    button.addEventListener("click", () => setMode(button.dataset.mode));
  });
  $("theme-toggle").addEventListener("click", toggleTheme);
  $("refresh").addEventListener("click", async () => {
    if (state.busy) {
      return;
    }
    try {
      await loadStatus(true);
    } catch (error) {
      log(
        t("error_prefix", "失败：{message}", {
          message: error?.message || String(error),
        }),
      );
    }
  });
  $("test").addEventListener("click", runTest);
  $("save").addEventListener("click", runSave);

  try {
    await loadStatus();
    log(t("log_ready", "填写参数后可以先测试连接，再保存。"));
  } catch (error) {
    log(
      t("error_prefix", "失败：{message}", {
        message: error?.message || String(error),
      }),
    );
  }

  bridge.onContext((nextContext) => {
    applyI18n();
    if (!state.manualTheme && typeof nextContext?.isDark === "boolean") {
      applyTheme(nextContext.isDark ? "dark" : "light");
    } else {
      applyTheme(state.theme || currentTheme());
    }
  });
}

main();
