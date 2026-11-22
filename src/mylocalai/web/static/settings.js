/* ============================================================
   settings.js —— 设置弹层（「模型」面板 + 可操作的「隐私与授权」面板）
   与 shell.js / chat.js 同风格：普通 <script>、无框架、无构建步骤。
   职责：
     - 对外只暴露 window.SettingsPanel = { open, close, isOpen }，
       开合按钮 / 遮罩 / Esc 的接线在 shell.js（照 aboutDialog 的写法），
       本文件只负责弹层里"看到什么、改什么、怎么落盘"；
     - 提供方：列表 + 表单（Provider ID / 显示名称 / API 地址 / API 协议 /
       API 密钥 / 启用）+ 保存 / 设为当前 / 删除；
     - 模型目录：卡片式（上下文窗口 / 最大输出 token / 输入类型 /
       推理等级档位）+ 添加模型 / 保存模型目录 / 获取可用模型 / 恢复默认模型；
     - 隐私与授权：外部处理总开关 + 提供方勾选清单 + 保存（PUT
       /api/privacy/preferences 完整载荷，save_enabled / retention_days 按
       GET 到的原值回传），保存后立即生效、无需重启服务；
     - 所有 HTTP 路径集中在 SETTINGS_API，与 web/models_api.py 逐条对应，
       tests/test_models_ui.py 会把它和后端路由表做自洽比对；
     - 写操作成功后一律重新 GET /api/models 全量刷新（以磁盘现状为准，
       前端不维护"我猜的服务端状态"）；失败时把后端 detail 原文显示出来；
     - 一律 document.createElement + textContent 建 DOM：模型 ID / 显示名称 /
       base_url / 密钥都是用户输入，绝不拼进 innerHTML。
   ============================================================ */
"use strict";

(function (global) {
  /* ---------- 后端接口清单（与 docs/模型管理设计规格.md §3 逐条对应） ----------
     {pid} / {model_id} 由 apiPath() 替换并 encodeURIComponent；
     任何一条写错路径前端就 404，因此这张表就是前后端契约的唯一落点。 */
  const SETTINGS_API = {
    models: "/api/models",
    providers: "/api/models/providers",
    provider: "/api/models/providers/{pid}",
    providerSecret: "/api/models/providers/{pid}/secret",
    providerActivate: "/api/models/providers/{pid}/activate",
    providerDiscover: "/api/models/providers/{pid}/discover",
    providerModels: "/api/models/providers/{pid}/models",
    providerModel: "/api/models/providers/{pid}/models/{model_id}",
    providerModelsRestore: "/api/models/providers/{pid}/models/restore",
    privacyPreferences: "/api/privacy/preferences",
  };

  /* HTTP 状态码 → 中文动作说明：具体原因由后端 detail 原文补充，这里只说"哪一步失败了" */
  const STATUS_LEAD = {
    400: "请求不合法",
    403: "未获授权（外部处理授权被拒绝）",
    404: "找不到对应的提供方或模型",
    500: "写盘失败",
    502: "上游服务失败",
  };

  /* 状态区图标：纯文本字符，避免为两个图标引入字体或图片 */
  const STATUS_ICON = { ok: "✔", error: "⚠", warn: "!", busy: "…" };

  const DEFAULT_REASONING_PARAM = "reasoning_effort";

  /* ============================================================
     一、纯函数：token 数的解析与显示
     用户要能填 1M / 128K / 65536 这类写法，后端契约只收整数（§2.2）。
     M 按 1000000、K 按 1024 换算——这样两种写法都能原样往返：
     1000000 ↔ "1M"、131072 ↔ "128K"（显示时先试 M 再试 K，取能整除的写法）。
     ============================================================ */
  function parseTokenCount(raw) {
    const text = String(raw === null || raw === undefined ? "" : raw).trim();
    if (!text) return null;                       // 空 = 未声明，契约允许 null
    const matched = /^([0-9]+(?:\.[0-9]+)?)\s*([kKmM]?)$/.exec(text);
    if (!matched) return NaN;                     // NaN = 写法非法，由调用方给出中文提示
    const unit = matched[2].toLowerCase();
    const multiplier = unit === "m" ? 1000000 : unit === "k" ? 1024 : 1;
    return Math.round(parseFloat(matched[1]) * multiplier);
  }

  function formatTokenCount(value) {
    if (value === null || value === undefined || value === "") return "";
    const num = Number(value);
    if (!Number.isFinite(num) || num <= 0) return String(value);
    if (num % 1000000 === 0) return num / 1000000 + "M";
    if (num % 1024 === 0) return num / 1024 + "K";
    return String(num);
  }

  /* ---------- 路径模板填充 ---------- */
  function apiPath(template, values) {
    const source = values || {};
    return String(template).replace(/\{(\w+)\}/g, (whole, key) =>
      encodeURIComponent(String(source[key] === null || source[key] === undefined ? "" : source[key])));
  }

  /* ============================================================
     二、DOM 小工具（全部走 createElement / textContent）
     ============================================================ */
  function $(selector) {
    return document.querySelector(selector);
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== null && text !== undefined) node.textContent = String(text);
    return node;
  }

  function button(text, className, onClick, ariaLabel) {
    const node = el("button", className, text);
    node.type = "button";
    if (ariaLabel) node.setAttribute("aria-label", ariaLabel);
    if (onClick) node.addEventListener("click", onClick);
    return node;
  }

  function textInput(options) {
    const opts = options || {};
    const input = el("input", "settings-input");
    input.type = opts.type || "text";
    input.value = opts.value === null || opts.value === undefined ? "" : String(opts.value);
    if (opts.placeholder) input.placeholder = opts.placeholder;
    if (opts.maxlength) input.maxLength = opts.maxlength;
    if (opts.autocomplete) input.autocomplete = opts.autocomplete;
    if (opts.readonly) {
      input.readOnly = true;
      input.classList.add("settings-input-readonly");
    }
    return input;
  }

  function checkboxInput(checked) {
    const input = el("input", "settings-checkbox");
    input.type = "checkbox";
    input.checked = !!checked;
    return input;
  }

  function selectInput(options, currentValue, small) {
    const select = el("select", "settings-input" + (small ? " settings-input-sm" : ""));
    const list = options || [];
    list.forEach((opt) => {
      const option = el("option", null, opt.label);
      option.value = opt.value;
      if (String(opt.value) === String(currentValue)) option.selected = true;
      select.appendChild(option);
    });
    /* 后端没给可选清单时不编造选项：显示「未知」，把问题暴露给用户 */
    if (!list.length) {
      const unknown = el("option", null, "未知（后端未返回可选值）");
      unknown.value = currentValue === null || currentValue === undefined ? "" : String(currentValue);
      unknown.selected = true;
      select.appendChild(unknown);
    }
    return select;
  }

  /* 表单字段：label 包住控件，点击文字即可聚焦（键盘与读屏器都能正确归属） */
  function field(labelText, control, hint) {
    const wrap = el("label", "settings-field");
    wrap.appendChild(el("span", "settings-field-label", labelText));
    wrap.appendChild(control);
    if (hint) wrap.appendChild(el("span", "settings-field-hint", hint));
    return wrap;
  }

  function readonlyValue(value) {
    return el("span", "settings-readonly", value === null || value === undefined || value === "" ? "未知" : value);
  }

  function readValue(selector) {
    const node = $(selector);
    if (!node) return "";
    return String(node.value === null || node.value === undefined ? "" : node.value);
  }

  function readChecked(selector) {
    const node = $(selector);
    return !!(node && node.checked);
  }

  /* ============================================================
     三、页面状态
     只缓存"最近一次 GET /api/models 的结果"和"正在编辑的模型草稿"，
     服务端事实永远以重新拉取为准。
     ============================================================ */
  const state = {
    data: null,        // GET /api/models 的完整响应
    tab: "models",     // 左栏导航当前项
    selectedPid: null, // 选中的提供方 id
    creating: false,   // true = 正在填「新建提供方」表单（此时 selectedPid 为 null）
    models: [],        // 当前提供方的模型草稿（含新增未保存的卡）
    discover: null,    // 最近一次 /discover 结果 { pid, baseUrl, count, error, models[] }
    privacy: null,     // 隐私偏好 { data, error }：进「隐私与授权」时 GET，保存后用 PUT 响应刷新
    busy: false,       // 有请求在跑：挡住重复点击
    lastFocus: null,   // 打开弹层前的焦点，关闭时还回去
  };

  function dialogEl() {
    return $("#settingsDialog");
  }

  /* ============================================================
     四、请求与状态区
     ============================================================ */
  async function request(method, path, body) {
    const init = { method: method, headers: {} };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(path, init);
    } catch (err) {
      throw new Error("无法连接本机服务（" + path + "）：" + (err && err.message ? err.message : "网络错误"));
    }
    let payload = null;
    try {
      payload = await resp.json();
    } catch (err) {
      payload = null;                             // 非 JSON（如静态 404 页）按无 detail 处理
    }
    if (!resp.ok) {
      const rawDetail = payload && payload.detail !== undefined ? payload.detail : null;
      /* detail 可能是字符串（契约）也可能是校验错误数组：两种都原样展示，绝不吞掉 */
      const detail = typeof rawDetail === "string"
        ? rawDetail
        : (rawDetail ? JSON.stringify(rawDetail) : "");
      const lead = STATUS_LEAD[resp.status] || "请求失败";
      throw new Error(lead + "（HTTP " + resp.status + "）" + (detail ? "：" + detail : "：后端未给出原因"));
    }
    return payload;
  }

  /* 状态区：中文结论 + 后端 detail 原文。空文本 = 收起 */
  function showStatus(kind, text) {
    const box = $("#settingsStatus");
    if (!box) return;
    box.textContent = "";
    box.className = "settings-status";
    if (!text) {
      box.classList.add("hidden");
      return;
    }
    box.classList.add(kind || "info");
    box.appendChild(el("span", "settings-status-icon", STATUS_ICON[kind] || ""));
    box.appendChild(el("span", "settings-status-text", text));
  }

  function setBusy(busy) {
    state.busy = !!busy;
    const dialog = dialogEl();
    if (dialog) {
      dialog.classList.toggle("settings-busy", !!busy);
      dialog.setAttribute("aria-busy", busy ? "true" : "false");
    }
  }

  /* ============================================================
     五、弹层开合（shell.js 只调用这里，不碰内部 DOM）
     ============================================================ */
  function isOpen() {
    const dialog = dialogEl();
    return !!dialog && !dialog.classList.contains("hidden");
  }

  function setOpen(open) {
    const dialog = dialogEl();
    if (!dialog) return;
    const trigger = $("#settingsBtn");
    if (open) {
      if (!dialog.classList.contains("hidden")) {
        const closeBtn = $("#settingsCloseBtn");
        if (closeBtn) closeBtn.focus();
        return;
      }
      state.lastFocus = document.activeElement;
      dialog.classList.remove("hidden");
      if (trigger) trigger.setAttribute("aria-expanded", "true");
      setActiveNav(state.tab);
      showStatus("");
      state.privacy = null;             // 每次打开都重新 GET 隐私偏好：看到的一定是磁盘现状
      renderPane();                     // 先出骨架，避免打开后一片空白等网络
      const closeBtn = $("#settingsCloseBtn");
      if (closeBtn) closeBtn.focus();    // 焦点进弹层（与 aboutDialog 一致）
      loadAll();                        // 打开即拉全量：看到的一定是磁盘现状
    } else {
      if (dialog.classList.contains("hidden")) return;
      dialog.classList.add("hidden");
      if (trigger) {
        trigger.setAttribute("aria-expanded", "false");
        trigger.focus();                // 焦点回到触发按钮
      } else if (state.lastFocus && state.lastFocus.focus) {
        state.lastFocus.focus();
      }
    }
  }

  global.SettingsPanel = {
    open: function () { setOpen(true); },
    close: function () { setOpen(false); },
    isOpen: isOpen,
  };

  /* ============================================================
     六、全量刷新
     ============================================================ */
  function providerIds() {
    const list = state.data && Array.isArray(state.data.providers) ? state.data.providers : [];
    return list.map((item) => String(item.id)).filter((id) => id);
  }

  function currentProvider() {
    if (!state.data || state.creating || !state.selectedPid) return null;
    const list = Array.isArray(state.data.providers) ? state.data.providers : [];
    for (let i = 0; i < list.length; i += 1) {
      if (String(list[i].id) === String(state.selectedPid)) return list[i];
    }
    return null;
  }

  async function loadAll() {
    try {
      const data = await request("GET", SETTINGS_API.models);
      state.data = data;
      const ids = providerIds();
      /* 选中项尽量保持；原选中项被删/改名后才退回「当前提供方」或第一个 */
      if (!state.creating && (!state.selectedPid || ids.indexOf(String(state.selectedPid)) < 0)) {
        const activeProvider = data.active && data.active.provider ? String(data.active.provider) : "";
        state.selectedPid = activeProvider && ids.indexOf(activeProvider) >= 0 ? activeProvider : (ids[0] || null);
      }
      if (!ids.length) {
        state.creating = true;                 // 一个提供方都没有：直接进新建表单
        state.selectedPid = null;
      }
      syncModelsFromData();
      renderPane();
    } catch (err) {
      showStatus("error", err.message);
      renderPane();                            // 仍然渲染骨架（含"载入失败"提示）
    }
  }

  /* ============================================================
     七、模型草稿（把服务端 model view 变成卡片可编辑的形状）
     ============================================================ */
  function cloneModel(model) {
    const src = model || {};
    const reasoning = src.reasoning || {};
    const tiers = Array.isArray(reasoning.tiers)
      ? reasoning.tiers.map((row) => ({
          tier: String(row && row.tier !== null && row.tier !== undefined ? row.tier : "off"),
          api_value: row && row.api_value !== null && row.api_value !== undefined ? String(row.api_value) : null,
        }))
      : [];
    const inputTypes = Array.isArray(src.input_types) ? src.input_types.slice() : [];
    return {
      isNew: false,
      origId: String(src.id || ""),
      id: String(src.id || ""),
      displayName: String(src.display_name || src.id || ""),
      contextWindowRaw: formatTokenCount(src.context_window),
      maxOutputRaw: formatTokenCount(src.max_output_tokens),
      inputTypes: inputTypes.length ? inputTypes : ["text"],
      enabled: src.enabled !== false,
      reasoning: {
        enabled: !!reasoning.enabled,
        param: String(reasoning.param || DEFAULT_REASONING_PARAM),
        activeTier: String(reasoning.active_tier || (tiers.length ? tiers[0].tier : "off")),
        tiers: tiers,
      },
    };
  }

  function blankModelDraft() {
    return {
      isNew: true,
      origId: "",
      id: "",
      displayName: "",
      contextWindowRaw: "",
      maxOutputRaw: "",
      inputTypes: ["text"],
      enabled: true,
      reasoning: {
        enabled: false,
        param: DEFAULT_REASONING_PARAM,
        activeTier: "off",
        tiers: [{ tier: "off", api_value: null }],
      },
    };
  }

  function syncModelsFromData() {
    const provider = currentProvider();
    const models = provider && Array.isArray(provider.models) ? provider.models : [];
    state.models = models.map((model) => cloneModel(model));
  }

  /* ============================================================
     八、渲染：Tab 分发
     ============================================================ */
  function renderPane() {
    const pane = $("#settingsPane");
    if (!pane) return;
    pane.textContent = "";
    if (state.tab === "privacy") {
      pane.appendChild(buildPrivacyPane());
      return;
    }
    if (!state.data) {
      pane.appendChild(el("p", "settings-empty", "正在载入模型配置…"));
      return;
    }
    const warnings = Array.isArray(state.data.warnings) ? state.data.warnings : [];
    pane.appendChild(buildKeyHealthCard());   // 密钥体检卡：「模型」Tab 顶部第一行
    if (warnings.length) pane.appendChild(buildWarnings(warnings));
    pane.appendChild(buildModelsPane());
  }

  function buildWarnings(warnings) {
    const box = el("div", "settings-notice");
    box.appendChild(el("strong", null, "配置读取告警："));
    const list = el("ul", null);
    warnings.forEach((item) => list.appendChild(el("li", null, item)));
    box.appendChild(list);
    return box;
  }

  /* ---------- 密钥体检卡（模型 Tab 顶部，密钥健康度显性化）----------
     数据源：GET /api/models 响应 runtime 段的 key_source，三值词面以后端
     src/mylocalai/llm/model_registry.py 的 _resolve_secret() 为准：
     env=环境变量 / stored=本机密钥文件（设置面板保存）/ none=没配。
     key_source=none 时后端所有分析都会静默降级——四处配置位都在不代表能用，
     这张卡把降级显性化并给出下一步动作。
     刷新钩子：本卡在 renderPane() 里随「模型」Tab 重建，而提供方切换 / 新建 /
     保存 / 设为当前 / 删除全都汇入 renderPane()（写操作先 loadAll() 重新拉
     /api/models），落盘后卡片自动跟随服务端现状，不需要额外监听。
     样式约定：settings.css 不新增规则，盒底复用既有 .settings-notice；配色变体
     .key-health-card / .key-health-none / .key-health-env / .key-health-secrets /
     .key-health-text 定义在 chat.css（跨文件约定，tests/test_models_ui.py 有断言）；
     DOM 全部 createElement/textContent（密钥状态是接口数据，不拼 HTML）。
     出处：本卡自 P0-2「密钥健康度显性化」在途稿移植，词面已对齐后端——原稿
     误把 stored 写成 secrets，后端 _resolve_secret() 永远不会返回 secrets。 */
  const KEY_SOURCE_HINT = {
    env: { cls: "key-health-env", text: "密钥已配置（来源：环境变量）" },
    stored: { cls: "key-health-secrets", text: "密钥已配置（来源：设置面板）" },
  };

  function buildKeyHealthCard() {
    const runtime = state.data && state.data.runtime ? state.data.runtime : null;
    const source = runtime ? String(runtime.key_source || "") : "";
    const known = KEY_SOURCE_HINT[source];
    const card = el("div", "settings-notice key-health-card");
    if (known) {
      card.classList.add(known.cls);
      card.appendChild(el("span", "key-health-text", known.text));
    } else {
      /* none 与「后端没给 runtime」一律按未配置处理：不猜来源，宁可多警示 */
      card.classList.add("key-health-none");
      card.appendChild(el("span", "key-health-text",
        "未配置密钥：分析将以降级模式运行，证据发现不可用——请在下方选择提供方并填写密钥"));
    }
    return card;
  }

  function buildModelsPane() {
    const wrap = el("div", "settings-models");
    wrap.appendChild(buildProviderColumn());
    wrap.appendChild(buildDetailColumn());
    return wrap;
  }

  /* ---------- 提供方列表（左） ---------- */
  function keyBadge(provider) {
    if (!provider.api_key_set) return el("span", "badge gray", "未配置");
    const source = String(provider.api_key_source || "");
    const label = source === "env" ? "已配置(环境变量)" : "已配置(本地)";
    const badge = el("span", "badge green", label);
    /* 掩码只做 title 提示：不在列表里占用一行，也不冒充明文 */
    if (provider.api_key_masked) badge.title = "密钥掩码：" + provider.api_key_masked;
    return badge;
  }

  function buildProviderColumn() {
    const col = el("div", "settings-provider-col");
    const head = el("div", "settings-col-head");
    head.appendChild(el("h3", null, "提供方"));
    head.appendChild(button("＋ 新建提供方", "btn-ghost settings-mini-btn", () => {
      state.creating = true;
      state.selectedPid = null;
      state.models = [];
      state.discover = null;
      showStatus("");
      renderPane();
      const idInput = $("#settingsProviderId");
      if (idInput) idInput.focus();
    }, "新建提供方"));
    col.appendChild(head);

    const list = el("ul", "settings-provider-list");
    const providers = Array.isArray(state.data.providers) ? state.data.providers : [];
    providers.forEach((provider) => list.appendChild(buildProviderRow(provider)));
    if (!providers.length) {
      list.appendChild(el("li", "settings-empty", "还没有提供方：点「＋ 新建提供方」添加一个"));
    }
    col.appendChild(list);
    return col;
  }

  function buildProviderRow(provider) {
    const row = el("li", "settings-provider-row");
    const active = !state.creating && String(state.selectedPid) === String(provider.id);
    const btn = el("button", "settings-provider-btn" + (active ? " active" : ""));
    btn.type = "button";
    btn.setAttribute("aria-pressed", active ? "true" : "false");
    btn.appendChild(el("span", "settings-provider-name", provider.display_name || provider.id || "未知"));
    btn.appendChild(el("span", "settings-provider-id", provider.id || "未知"));
    const badges = el("span", "settings-badges");
    badges.appendChild(keyBadge(provider));
    if (provider.builtin) badges.appendChild(el("span", "badge gray", "内置"));
    if (provider.is_active) badges.appendChild(el("span", "badge green", "当前"));
    btn.appendChild(badges);
    btn.addEventListener("click", () => selectProvider(provider.id));
    row.appendChild(btn);
    return row;
  }

  function selectProvider(pid) {
    if (!state.creating && String(state.selectedPid) === String(pid)) return;
    state.creating = false;
    state.selectedPid = String(pid);
    state.discover = null;
    showStatus("");
    syncModelsFromData();
    renderPane();
  }

  /* ---------- 右栏：提供方表单 + 模型目录 + 发现结果 ---------- */
  function buildDetailColumn() {
    const col = el("div", "settings-detail-col");
    if (state.creating) {
      col.appendChild(buildProviderForm(null));
      return col;
    }
    const provider = currentProvider();
    if (!provider) {
      col.appendChild(el("p", "settings-empty", "请先在左侧选择一个提供方，或点「＋ 新建提供方」。"));
      return col;
    }
    col.appendChild(buildProviderForm(provider));
    col.appendChild(buildModelCatalog(provider));
    if (state.discover && String(state.discover.pid) === String(provider.id)) {
      col.appendChild(buildDiscoverSection(provider));
    }
    return col;
  }

  function protocolOptions() {
    const protocols = state.data && Array.isArray(state.data.protocols) ? state.data.protocols : [];
    return protocols.map((proto) => ({
      value: proto.id,
      label: String(proto.label || proto.id) + (proto.runtime_supported ? "" : "（本站运行期不支持）"),
    }));
  }

  function tierOptions() {
    const tiers = state.data && Array.isArray(state.data.reasoning_tiers) ? state.data.reasoning_tiers : [];
    return tiers.map((tier) => ({ value: tier.id, label: String(tier.label || tier.id) }));
  }

  function inputTypeOptions() {
    const types = state.data && Array.isArray(state.data.input_types) ? state.data.input_types : [];
    return types.map((item) => ({ value: item.id, label: String(item.label || item.id) }));
  }

  function buildProviderForm(provider) {
    const isNew = !provider;
    const section = el("section", "settings-section");
    const head = el("div", "settings-section-head");
    head.appendChild(el("h3", null, isNew ? "新建提供方" : "提供方设置"));
    head.appendChild(el("span", "badge gray", isNew ? "未保存" : (provider.builtin ? "内置提供方" : "自定义提供方")));
    section.appendChild(head);

    const grid = el("div", "settings-form-grid");

    const idInput = textInput({
      value: isNew ? "" : provider.id,
      placeholder: "例如 deepseek",
      maxlength: 32,
      readonly: !isNew,
    });
    idInput.id = "settingsProviderId";
    grid.appendChild(field("Provider ID", idInput,
      isNew ? "小写字母开头，可用小写字母、数字、_ 与 -，最多 32 位" : "已存在的 Provider ID 不可修改"));

    const nameInput = textInput({ value: isNew ? "" : (provider.display_name || ""), placeholder: "例如 DeepSeek" });
    nameInput.id = "settingsProviderName";
    grid.appendChild(field("显示名称", nameInput));

    const urlInput = textInput({ value: isNew ? "" : (provider.base_url || ""), placeholder: "https://api.example.com/v1", maxlength: 512 });
    urlInput.id = "settingsProviderBaseUrl";
    grid.appendChild(field("API 地址", urlInput, "必须以 http:// 或 https:// 开头"));

    const protocolSelect = selectInput(protocolOptions(), isNew ? "openai" : provider.protocol, false);
    protocolSelect.id = "settingsProviderProtocol";
    if (!protocolSelect.options.length) {
      const unknown = el("option", null, "未知（后端未返回可选协议）");
      unknown.value = "";
      protocolSelect.appendChild(unknown);
    }
    grid.appendChild(field("API 协议", protocolSelect, "只有 OpenAI 协议的提供方能在本站真正调用"));

    const keyInput = textInput({
      type: "password",
      value: "",
      placeholder: isNew ? "输入 API 密钥" : "留空表示不改",
      autocomplete: "new-password",
    });
    keyInput.id = "settingsProviderKey";
    let keyHint;
    if (isNew) {
      keyHint = "可留空；密钥只写入本机 data/model_secrets.json，任何接口都不回明文";
    } else if (provider.api_key_set) {
      keyHint = "已配置" + (provider.api_key_masked ? "（" + provider.api_key_masked + "）" : "") + "；留空表示不改，填新值则覆盖";
    } else {
      keyHint = "尚未配置密钥；填好后点「保存提供方」";
    }
    grid.appendChild(field("API 密钥", keyInput, keyHint));

    if (!isNew && provider.key_env) {
      grid.appendChild(field("密钥环境变量", readonlyValue(provider.key_env), "环境变量优先于本页保存的密钥"));
    }

    const enabledInput = checkboxInput(isNew ? true : provider.enabled !== false);
    enabledInput.id = "settingsProviderEnabled";
    grid.appendChild(field("启用", enabledInput, "关闭后该提供方不参与分析调用"));
    section.appendChild(grid);

    const actions = el("div", "settings-actions");
    actions.appendChild(button("保存提供方", "btn-primary settings-btn-sm", () => saveProvider(provider), "保存提供方"));
    if (!isNew) {
      actions.appendChild(button("设为当前", "btn-ghost settings-btn-sm", () => activateProvider(provider), "设为当前提供方"));
      const removeBtn = button("删除提供方", "btn-ghost settings-btn-sm settings-danger-btn",
        () => deleteProvider(provider), "删除提供方");
      if (provider.builtin) {
        removeBtn.disabled = true;
        removeBtn.setAttribute("aria-disabled", "true");
        removeBtn.title = "内置提供方不可删除（可以修改或设为当前）";
      }
      actions.appendChild(removeBtn);
    }
    section.appendChild(actions);

    if (!isNew && provider.builtin) {
      section.appendChild(el("p", "settings-hint",
        "内置提供方来自 providers.json：可以修改配置、可以设为当前，但不能删除。"));
    }
    if (!isNew && provider.protocol_runtime_supported === false) {
      section.appendChild(el("p", "settings-hint settings-hint-warn",
        "该提供方声明的 API 协议本站运行期不支持：配置可以保存，但设为当前会被后端拒绝。"));
    }
    return section;
  }

  /* ---------- 模型目录 ---------- */
  function catalogStateText(provider) {
    const defaults = Array.isArray(provider.default_models) ? provider.default_models : [];
    const current = Array.isArray(provider.models) ? provider.models : [];
    const defaultIds = defaults.map((item) => String(item.id));
    const currentIds = current.map((item) => String(item.id));
    const same = defaultIds.length > 0
      && defaultIds.length === currentIds.length
      && defaultIds.every((id) => currentIds.indexOf(id) >= 0);
    return same ? "内置默认模型目录" : "已自定义模型目录";
  }

  function buildModelCatalog(provider) {
    const section = el("section", "settings-section");
    const head = el("div", "settings-section-head");
    head.appendChild(el("h3", null, "模型目录"));
    head.appendChild(el("span", "settings-catalog-state", catalogStateText(provider)));
    const tools = el("div", "settings-section-tools");
    tools.appendChild(button("获取可用模型", "btn-ghost settings-mini-btn",
      () => discoverModels(provider), "获取可用模型"));
    const restoreBtn = button("恢复默认模型", "btn-ghost settings-mini-btn",
      () => restoreDefaultModels(provider), "恢复默认模型");
    const defaults = Array.isArray(provider.default_models) ? provider.default_models : [];
    if (!defaults.length) {
      restoreBtn.disabled = true;
      restoreBtn.title = "该提供方没有内置默认模型目录（恢复默认只对内置提供方有效）";
    }
    tools.appendChild(restoreBtn);
    head.appendChild(tools);
    section.appendChild(head);

    const list = el("div", "settings-model-list");
    section.appendChild(list);

    function refreshList() {
      list.textContent = "";
      if (!state.models.length) {
        list.appendChild(el("p", "settings-empty", "该提供方还没有模型：点「获取可用模型」或「＋ 添加模型」。"));
        return;
      }
      state.models.forEach((draft) => list.appendChild(buildModelCard(provider, draft, refreshList)));
    }
    refreshList();

    const foot = el("div", "settings-actions");
    foot.appendChild(button("＋ 添加模型", "btn-ghost settings-btn-sm", () => {
      state.models.push(blankModelDraft());
      refreshList();
      const cards = list.querySelectorAll(".settings-model-card");
      const lastCard = cards[cards.length - 1];
      const focusInput = lastCard ? lastCard.querySelector(".settings-input") : null;
      if (focusInput) focusInput.focus();
    }, "添加模型"));
    foot.appendChild(button("保存模型目录", "btn-primary settings-btn-sm",
      () => saveCatalog(provider), "保存模型目录"));
    section.appendChild(foot);
    return section;
  }

  function buildModelCard(provider, draft, onListChange) {
    const card = el("div", "settings-model-card");

    /* 第一行：模型 ID / 显示名称 / 启用 / 删除 */
    const row1 = el("div", "settings-model-row settings-model-row-main");
    const idInput = textInput({ value: draft.id, placeholder: "模型 ID，例如 deepseek-chat", maxlength: 200 });
    idInput.addEventListener("input", () => { draft.id = idInput.value; });
    row1.appendChild(field("模型 ID", idInput));

    const nameInput = textInput({ value: draft.displayName, placeholder: "显示名称（留空用模型 ID）" });
    nameInput.addEventListener("input", () => { draft.displayName = nameInput.value; });
    row1.appendChild(field("显示名称", nameInput));

    const enabledInput = checkboxInput(draft.enabled);
    enabledInput.addEventListener("change", () => { draft.enabled = enabledInput.checked; });
    row1.appendChild(field("启用", enabledInput));

    row1.appendChild(button("🗑", "btn-ghost settings-icon-btn", () => removeModelCard(provider, draft, onListChange),
      "删除模型 " + (draft.id || "未命名")));
    card.appendChild(row1);

    /* 第二行：上下文窗口 / 最大输出 token 数（支持 1M / 128K / 65536 写法） */
    const row2 = el("div", "settings-model-row");
    const contextInput = textInput({ value: draft.contextWindowRaw, placeholder: "例如 128K、1M、65536" });
    const contextHint = el("span", "settings-field-hint", "支持 K / M 写法；留空 = 未声明");
    contextInput.addEventListener("input", () => {
      draft.contextWindowRaw = contextInput.value;
      const parsed = parseTokenCount(contextInput.value);
      contextInput.classList.toggle("settings-input-invalid", Number.isNaN(parsed));
    });
    const contextField = field("上下文窗口", contextInput, null);
    contextField.appendChild(contextHint);
    row2.appendChild(contextField);

    const outputInput = textInput({ value: draft.maxOutputRaw, placeholder: "例如 8K、8192" });
    const outputHint = el("span", "settings-field-hint", "不得大于上下文窗口（后端校验）");
    outputInput.addEventListener("input", () => {
      draft.maxOutputRaw = outputInput.value;
      const parsed = parseTokenCount(outputInput.value);
      outputInput.classList.toggle("settings-input-invalid", Number.isNaN(parsed));
    });
    const outputField = field("最大输出 token 数", outputInput, null);
    outputField.appendChild(outputHint);
    row2.appendChild(outputField);
    card.appendChild(row2);

    /* 第三行：输入类型（文本 / 图片） */
    const row3 = el("div", "settings-model-row settings-model-row-tight");
    row3.appendChild(el("span", "settings-field-label", "输入类型"));
    const typeBox = el("div", "settings-check-group");
    const types = inputTypeOptions();
    if (!types.length) typeBox.appendChild(el("span", "settings-field-hint", "未知（后端未返回输入类型清单）"));
    types.forEach((item) => {
      const cb = checkboxInput(draft.inputTypes.indexOf(item.value) >= 0);
      const label = el("label", "settings-check");
      label.appendChild(cb);
      label.appendChild(el("span", null, item.label));
      cb.addEventListener("change", () => {
        if (cb.checked) {
          if (draft.inputTypes.indexOf(item.value) < 0) draft.inputTypes.push(item.value);
        } else {
          draft.inputTypes = draft.inputTypes.filter((value) => value !== item.value);
        }
      });
      typeBox.appendChild(label);
    });
    row3.appendChild(typeBox);
    card.appendChild(row3);

    /* 第四行：推理等级（开关 + 当前档位 + 档位表） */
    card.appendChild(buildReasoningBlock(draft));
    return card;
  }

  function buildReasoningBlock(draft) {
    const box = el("div", "settings-model-reasoning");
    const head = el("div", "settings-model-row settings-model-row-tight");

    const toggle = checkboxInput(draft.reasoning.enabled);
    const toggleLabel = el("label", "settings-check settings-check-strong");
    toggleLabel.appendChild(toggle);
    toggleLabel.appendChild(el("span", null, "推理等级"));
    head.appendChild(toggleLabel);

    const activeSelect = el("select", "settings-input settings-input-sm");
    head.appendChild(field("当前档位", activeSelect));
    head.appendChild(el("span", "settings-field-hint", "参数名 " + (draft.reasoning.param || DEFAULT_REASONING_PARAM)));
    box.appendChild(head);

    const tierBox = el("div", "settings-tier-box");
    box.appendChild(tierBox);

    function syncActiveOptions() {
      activeSelect.textContent = "";
      const rows = draft.reasoning.tiers;
      if (!rows.length) {
        const option = el("option", null, "未知（尚未添加档位）");
        option.value = "";
        activeSelect.appendChild(option);
        activeSelect.disabled = true;
        return;
      }
      activeSelect.disabled = false;
      const available = tierOptions();
      rows.forEach((row) => {
        const matched = available.filter((item) => item.value === row.tier)[0];
        const option = el("option", null, matched ? matched.label : row.tier);
        option.value = row.tier;
        if (row.tier === draft.reasoning.activeTier) option.selected = true;
        activeSelect.appendChild(option);
      });
      if (!rows.some((row) => row.tier === draft.reasoning.activeTier)) {
        draft.reasoning.activeTier = rows[0].tier;
        activeSelect.value = rows[0].tier;
      }
    }

    function repaintTiers() {
      tierBox.textContent = "";
      const rows = draft.reasoning.tiers;
      if (!draft.reasoning.enabled) {
        tierBox.classList.add("hidden");
        return;
      }
      tierBox.classList.remove("hidden");
      if (!rows.length) {
        tierBox.appendChild(el("p", "settings-empty", "还没有档位：点「＋ 添加档位」加一行。"));
      }
      rows.forEach((row) => tierBox.appendChild(buildTierRow(draft, row, () => {
        draft.reasoning.tiers = draft.reasoning.tiers.filter((item) => item !== row);
        repaintTiers();
        syncActiveOptions();
      })));
      const addBtn = button("＋ 添加档位", "btn-ghost settings-mini-btn", () => {
        const next = nextFreeTier(draft.reasoning.tiers);
        if (!next) return;
        draft.reasoning.tiers.push({ tier: next, api_value: next === "off" ? null : next });
        repaintTiers();
        syncActiveOptions();
      }, "添加推理档位");
      const allUsed = !nextFreeTier(draft.reasoning.tiers);
      if (allUsed) {
        addBtn.disabled = true;
        addBtn.title = "四个档位都已添加";
      }
      tierBox.appendChild(addBtn);
    }

    toggle.addEventListener("change", () => {
      draft.reasoning.enabled = toggle.checked;
      if (draft.reasoning.enabled && !draft.reasoning.tiers.length) {
        draft.reasoning.tiers.push({ tier: "off", api_value: null });
        if (!draft.reasoning.activeTier) draft.reasoning.activeTier = "off";
      }
      repaintTiers();
      syncActiveOptions();
    });

    activeSelect.addEventListener("change", () => {
      draft.reasoning.activeTier = activeSelect.value;
    });

    syncActiveOptions();
    repaintTiers();
    return box;
  }

  /* 下一个还没用过的档位；四个都用满时返回 ""（后端不允许重复档位） */
  function nextFreeTier(rows) {
    const all = tierOptions();
    const used = rows.map((row) => row.tier);
    for (let i = 0; i < all.length; i += 1) {
      if (used.indexOf(all[i].value) < 0) return all[i].value;
    }
    return "";
  }

  function buildTierRow(draft, row, onRemove) {
    const line = el("div", "settings-tier-row");
    const tierSelect = selectInput(tierOptions(), row.tier, true);
    const apiInput = textInput({ value: row.api_value === null ? "" : row.api_value, placeholder: "例如 low" });

    function syncOffState() {
      const off = row.tier === "off";
      line.classList.toggle("settings-tier-row-off", off);
      apiInput.disabled = off;
      if (off) {
        /* off 档的语义就是"关闭思考"：值置空并禁止填写，避免用户以为填了会生效 */
        row.keptValue = apiInput.value;
        apiInput.value = "";
        apiInput.placeholder = "关闭思考";
        row.api_value = null;
      } else {
        apiInput.placeholder = "例如 low / 1024";
        if (row.keptValue) {
          apiInput.value = row.keptValue;
          row.api_value = row.keptValue;
          row.keptValue = "";
        }
      }
    }

    tierSelect.addEventListener("change", () => {
      row.tier = tierSelect.value;
      syncOffState();
    });
    apiInput.addEventListener("input", () => {
      row.api_value = apiInput.value === "" ? null : apiInput.value;
    });
    line.appendChild(field("档位", tierSelect));
    line.appendChild(field("API 参数值", apiInput));
    line.appendChild(button("🗑", "btn-ghost settings-icon-btn", onRemove, "删除档位"));
    syncOffState();
    return line;
  }

  /* ---------- 发现结果（勾选 → 导入） ---------- */
  function buildDiscoverSection(provider) {
    const section = el("section", "settings-section settings-discover");
    const head = el("div", "settings-section-head");
    head.appendChild(el("h3", null, "获取可用模型结果"));
    if (state.discover.baseUrl) head.appendChild(el("span", "settings-catalog-state", state.discover.baseUrl));
    section.appendChild(head);

    if (state.discover.error) {
      section.appendChild(el("p", "settings-hint settings-hint-warn", "上游提示：" + state.discover.error));
    }
    const models = state.discover.models || [];
    if (!models.length) {
      section.appendChild(el("p", "settings-empty", "上游没有返回任何模型（空目录也是合法结果）。"));
      const actions = el("div", "settings-actions");
      actions.appendChild(button("收起", "btn-ghost settings-btn-sm", () => {
        state.discover = null;
        renderPane();
      }, "收起获取结果"));
      section.appendChild(actions);
      return section;
    }

    const list = el("ul", "settings-discover-list");
    models.forEach((item) => {
      const row = el("li", "settings-discover-item");
      const cb = checkboxInput(item.checked);
      cb.addEventListener("change", () => { item.checked = cb.checked; });
      const label = el("label", "settings-check");
      label.appendChild(cb);
      label.appendChild(el("span", "settings-discover-id", item.id));
      if (item.ownedBy) label.appendChild(el("span", "settings-discover-owner", item.ownedBy));
      row.appendChild(label);
      list.appendChild(row);
    });
    section.appendChild(list);
    section.appendChild(el("p", "settings-hint",
      "勾选后点「导入所选模型」才会写盘：导入的模型先用默认参数（未声明上下文窗口、纯文本输入、推理关闭），可在上方卡片里再改。"));

    const actions = el("div", "settings-actions");
    actions.appendChild(button("导入所选模型", "btn-primary settings-btn-sm", () => importDiscovered(provider), "导入所选模型"));
    actions.appendChild(button("全选", "btn-ghost settings-btn-sm", () => {
      state.discover.models.forEach((item) => { item.checked = true; });
      renderPane();
    }, "全选全部模型"));
    actions.appendChild(button("收起", "btn-ghost settings-btn-sm", () => {
      state.discover = null;
      renderPane();
    }, "收起获取结果"));
    section.appendChild(actions);
    return section;
  }

  /* ---------- 隐私与授权面板（总开关 + 提供方勾选 + 保存即生效） ----------
     偏好来自 GET /api/privacy/preferences（state.privacy 缓存，打开弹层即重置）；
     提供方清单直接用 GET /api/models 载荷里的 state.data.providers（id/display_name）。
     保存 = PUT 完整载荷：外部处理两字段用面板草稿，save_enabled / retention_days
     本期不在面板里改、按 GET 到的原值回传；成功后用 PUT 响应重渲染（含最新 warning），
     400 时把后端 detail 原文显示在状态区。 */
  function buildPrivacyPane() {
    const wrap = el("div", "settings-privacy");
    const section = el("section", "settings-section");
    section.appendChild(el("h3", null, "隐私与授权"));
    section.appendChild(el("p", "settings-hint",
      "「获取可用模型」会真的向提供方发一次 HTTP 请求，因此受隐私偏好里的「外部处理授权」约束："
      + "被拒绝时后端直接回 403 且不发出请求；localhost / 127.0.0.1 / ::1 视为本地、自动放行。"));
    const link = el("a", "settings-link", "查看当前隐私偏好（JSON，只读）");
    link.href = SETTINGS_API.privacyPreferences;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    section.appendChild(link);

    /* 首次渲染先出骨架，异步 GET 完成后 renderPane 换成控件（避免 renderPane→GET→renderPane 死循环） */
    if (!state.privacy) {
      section.appendChild(el("p", "settings-empty", "正在载入隐私偏好…"));
      wrap.appendChild(section);
      loadPrivacyPreferences();
      return wrap;
    }
    if (state.privacy.error) {
      section.appendChild(el("p", "settings-empty", "隐私偏好载入失败：" + state.privacy.error));
      const retry = el("div", "settings-actions");
      retry.appendChild(button("重新载入", "btn-ghost settings-btn-sm", () => {
        state.privacy = null;
        renderPane();
      }, "重新载入隐私偏好"));
      section.appendChild(retry);
      wrap.appendChild(section);
      return wrap;
    }

    const prefs = state.privacy.data;
    if (prefs.warning) {
      /* 复用配置读取告警的样式（settings-notice）：坏文件 fail-closed 的原因要给人看 */
      const warn = el("div", "settings-notice");
      warn.appendChild(el("strong", null, "隐私偏好告警："));
      warn.appendChild(el("span", null, String(prefs.warning)));
      section.appendChild(warn);
    }

    /* 草稿只活在本次渲染内：切换 Tab 或保存成功重渲染都会回到服务端现状 */
    const draft = {
      enabled: !!prefs.external_processing_enabled,
      providers: (Array.isArray(prefs.external_processing_providers)
        ? prefs.external_processing_providers : []).map(String),
    };

    /* ① 外部处理总开关 */
    const toggleGroup = el("div", "settings-check-group");
    const toggle = checkboxInput(draft.enabled);
    toggle.id = "settingsPrivacyExternalToggle";
    const toggleLabel = el("label", "settings-check settings-check-strong");
    toggleLabel.appendChild(toggle);
    toggleLabel.appendChild(el("span", null, "允许外部处理（外发请求）"));
    toggleGroup.appendChild(toggleLabel);
    section.appendChild(toggleGroup);
    section.appendChild(el("p", "settings-hint",
      "总开关关闭时下方提供方全部禁用并清空勾选（后端不允许「开关关着还授权提供方」）；"
      + "打开并勾选后，该提供方的「获取可用模型」等外发请求才被放行。"));

    /* ② 提供方勾选清单（display_name + id；总开关关闭时禁用并清空） */
    section.appendChild(el("div", "settings-field-label", "允许外发的提供方"));
    const list = el("ul", "settings-discover-list");
    const boxes = [];
    const providers = state.data && Array.isArray(state.data.providers) ? state.data.providers : [];
    if (!providers.length) {
      list.appendChild(el("li", "settings-empty", "还没有提供方：请先在「模型」页添加提供方，再回来勾选。"));
    }
    providers.forEach((provider) => {
      const item = el("li", "settings-discover-item");
      const cb = checkboxInput(draft.providers.indexOf(String(provider.id)) >= 0);
      cb.disabled = !draft.enabled;
      boxes.push(cb);
      cb.addEventListener("change", () => {
        const pid = String(provider.id);
        if (cb.checked) {
          if (draft.providers.indexOf(pid) < 0) draft.providers.push(pid);
        } else {
          draft.providers = draft.providers.filter((id) => id !== pid);
        }
      });
      const label = el("label", "settings-check");
      label.appendChild(cb);
      label.appendChild(el("span", "settings-discover-id", provider.display_name || provider.id || "未知"));
      label.appendChild(el("span", "settings-discover-owner", provider.id || "未知"));
      item.appendChild(label);
      list.appendChild(item);
    });
    section.appendChild(list);

    toggle.addEventListener("change", () => {
      draft.enabled = toggle.checked;
      if (!draft.enabled) draft.providers = [];     // 开关关闭 = 清空勾选（与后端校验一致）
      boxes.forEach((box) => {
        box.disabled = !draft.enabled;
        if (!draft.enabled) box.checked = false;
      });
    });

    /* ③ 保存 + ④ 只读 JSON 链接与本地放行提示已在上方保留 */
    const actions = el("div", "settings-actions");
    actions.appendChild(button("保存隐私授权", "btn-primary settings-btn-sm",
      () => savePrivacyPreferences(draft), "保存隐私授权"));
    section.appendChild(actions);
    section.appendChild(el("p", "settings-hint",
      "保存 = PUT " + SETTINGS_API.privacyPreferences + "（完整载荷：外部处理两字段用上面的勾选状态，"
      + "数据保留三项按当前值原样回传，本期不在面板里修改保留策略）。"
      + "保存后立即生效，无需重启服务；localhost / 127.0.0.1 / ::1 的提供方视为本地，无需勾选即可调用。"));
    wrap.appendChild(section);
    return wrap;
  }

  async function loadPrivacyPreferences() {
    try {
      const data = await request("GET", SETTINGS_API.privacyPreferences);
      state.privacy = { data: data, error: "" };
    } catch (err) {
      state.privacy = { data: null, error: err.message };
    }
    if (state.tab === "privacy") renderPane();      // 用户可能已切走：不在别的 Tab 上抢渲染
  }

  /* ============================================================
     九、写操作：每一处都"先落盘、再全量刷新"
     ============================================================ */

  /* 例外：隐私偏好不在 GET /api/models 载荷里，无法靠 loadAll() 刷新——
     保存成功后直接把 PUT 响应（含最新 warning）写回 state.privacy 并重渲染。
     save_enabled / retention_days 按 GET 到的原值回传：面板本期不提供保留策略编辑，
     也不该在保存授权时"顺手"改掉用户没看到的保留天数。 */
  async function savePrivacyPreferences(draft) {
    if (state.busy) return;
    const prefs = state.privacy && state.privacy.data ? state.privacy.data : null;
    if (!prefs) {
      showStatus("error", "隐私偏好尚未载入：等载入完成后再保存。");
      return;
    }
    const body = {
      save_enabled: Object.assign({}, prefs.save_enabled),
      retention_days: Object.assign({}, prefs.retention_days),
      external_processing_enabled: !!draft.enabled,
      external_processing_providers: draft.providers.slice(),
    };
    setBusy(true);
    try {
      const saved = await request("PUT", SETTINGS_API.privacyPreferences, body);
      state.privacy = { data: saved, error: "" };
      const warning = saved && saved.warning ? String(saved.warning) : "";
      showStatus(warning ? "warn" : "ok",
        warning ? "已保存隐私授权，但后端提示：" + warning
                : "已保存隐私授权：立即生效，无需重启服务。");
      renderPane();
    } catch (err) {
      showStatus("error", err.message);   // 400 时后端 detail 原文已由 request() 拼进消息
    } finally {
      setBusy(false);
    }
  }

  async function saveProvider(provider) {
    if (state.busy) return;
    const isNew = !provider;
    const id = readValue("#settingsProviderId").trim();
    const displayName = readValue("#settingsProviderName").trim();
    const baseUrl = readValue("#settingsProviderBaseUrl").trim();
    const protocol = readValue("#settingsProviderProtocol");
    const apiKey = readValue("#settingsProviderKey");
    const enabled = readChecked("#settingsProviderEnabled");

    if (!id) {
      showStatus("error", "Provider ID 必填：小写字母开头，例如 my-provider。");
      return;
    }
    if (!baseUrl) {
      showStatus("error", "API 地址必填：例如 https://api.example.com/v1。");
      return;
    }
    if (!protocol) {
      showStatus("error", "API 协议必填：请在后端返回的协议清单里选一个。");
      return;
    }

    setBusy(true);
    try {
      if (isNew) {
        const body = {
          id: id,
          display_name: displayName || id,
          base_url: baseUrl,
          protocol: protocol,
          key_env: "",
          enabled: enabled,
        };
        if (apiKey) body.api_key = apiKey;
        await request("POST", SETTINGS_API.providers, body);
        state.selectedPid = id;
        state.creating = false;
        showStatus("ok", "已保存提供方 " + id + (apiKey ? "（含新密钥）" : "") + "。");
      } else {
        const body = {
          display_name: displayName || provider.display_name || provider.id,
          base_url: baseUrl,
          protocol: protocol,
          key_env: provider.key_env || "",
          enabled: enabled,
        };
        await request("PUT", apiPath(SETTINGS_API.provider, { pid: provider.id }), body);
        if (apiKey) {
          await request("PUT", apiPath(SETTINGS_API.providerSecret, { pid: provider.id }), { api_key: apiKey });
        }
        showStatus("ok", "已保存提供方 " + provider.id + (apiKey ? "（含新密钥）" : "") + "。");
      }
      syncModelsFromData();
      await loadAll();
    } catch (err) {
      showStatus("error", err.message);
    } finally {
      setBusy(false);
    }
  }

  async function activateProvider(provider) {
    if (state.busy) return;
    const model = pickActivateModel(provider);
    setBusy(true);
    try {
      await request("POST", apiPath(SETTINGS_API.providerActivate, { pid: provider.id }),
        model ? { model: model } : {});
      showStatus("ok", "已设为当前提供方：" + (provider.display_name || provider.id)
        + (model ? "（模型 " + model + "）" : "") + "。");
      await loadAll();
    } catch (err) {
      showStatus("error", err.message);
    } finally {
      setBusy(false);
    }
  }

  /* 设为当前时带哪个模型：沿用当前已选模型（属于即保留），否则用该提供方第一个启用的模型 */
  function pickActivateModel(provider) {
    const active = state.data && state.data.active ? state.data.active : {};
    if (String(active.provider || "") === String(provider.id) && active.model) return String(active.model);
    const models = Array.isArray(provider.models) ? provider.models : [];
    for (let i = 0; i < models.length; i += 1) {
      if (models[i].enabled !== false) return String(models[i].id);
    }
    return models.length ? String(models[0].id) : "";
  }

  async function deleteProvider(provider) {
    if (state.busy) return;
    if (provider.builtin) {
      showStatus("error", "内置提供方不可删除（" + provider.id + "）：可以修改配置或设为当前。");
      return;
    }
    const label = provider.display_name || provider.id;
    if (!global.confirm("删除提供方「" + label + "」及其模型目录和已保存密钥？该操作会立即写盘，不可撤销。")) return;
    setBusy(true);
    try {
      await request("DELETE", apiPath(SETTINGS_API.provider, { pid: provider.id }));
      state.selectedPid = null;
      state.creating = false;
      showStatus("ok", "已删除提供方 " + provider.id + "。");
      await loadAll();
    } catch (err) {
      showStatus("error", err.message);
    } finally {
      setBusy(false);
    }
  }

  async function discoverModels(provider) {
    if (state.busy) return;
    setBusy(true);
    showStatus("busy", "正在向 " + (provider.base_url || "提供方") + " 请求可用模型（会真的发一次 HTTP 请求）…");
    try {
      const result = await request("POST", apiPath(SETTINGS_API.providerDiscover, { pid: provider.id }), {});
      const rawModels = result && Array.isArray(result.models) ? result.models : [];
      const known = (Array.isArray(provider.models) ? provider.models : []).map((item) => String(item.id));
      const models = [];
      rawModels.forEach((item) => {
        const modelId = String(item && item.id !== undefined && item.id !== null ? item.id : "").trim();
        if (!modelId) return;
        models.push({
          id: modelId,
          ownedBy: String(item && item.owned_by ? item.owned_by : ""),
          /* 目录里已有的默认不勾：避免重复导入覆盖用户改过的参数 */
          checked: known.indexOf(modelId) < 0,
        });
      });
      state.discover = {
        pid: String(provider.id),
        baseUrl: result && result.base_url ? String(result.base_url) : "",
        count: result && result.count !== undefined ? result.count : models.length,
        error: result && result.error ? String(result.error) : "",
        models: models,
      };
      if (!models.length) {
        showStatus("warn", "上游没有返回任何模型：空目录也是合法结果，可以检查 API 地址与密钥。");
      } else {
        showStatus("ok", "发现 " + models.length + " 个模型：勾选后点「导入所选模型」才会写盘。");
      }
      renderPane();
      const section = document.querySelector(".settings-discover");
      if (section && section.scrollIntoView) section.scrollIntoView({ block: "nearest" });
    } catch (err) {
      showStatus("error", err.message);
    } finally {
      setBusy(false);
    }
  }

  /* 导入一个发现结果：走 POST .../models，未勾选的一条都不发 */
  async function importDiscovered(provider) {
    if (state.busy) return;
    const discovered = state.discover ? state.discover.models || [] : [];
    const picked = discovered.filter((item) => item.checked);
    if (!picked.length) {
      showStatus("error", "请先勾选要导入的模型，再点「导入所选模型」。");
      return;
    }
    setBusy(true);
    try {
      let imported = 0;
      const failures = [];
      for (let i = 0; i < picked.length; i += 1) {
        try {
          await request("POST", apiPath(SETTINGS_API.providerModels, { pid: provider.id }),
            defaultModelPayload(picked[i].id));
          imported += 1;
        } catch (err) {
          failures.push(picked[i].id + "：" + err.message);
        }
      }
      if (failures.length) {
        showStatus("error", "已导入 " + imported + " 个模型，" + failures.length + " 个失败 —— " + failures.join("；"));
      } else {
        showStatus("ok", "已导入 " + imported + " 个模型。");
      }
      if (!failures.length) state.discover = null;
      await loadAll();
    } finally {
      setBusy(false);
    }
  }

  function defaultModelPayload(modelId) {
    return {
      id: modelId,
      display_name: modelId,
      context_window: null,
      max_output_tokens: null,
      input_types: ["text"],
      enabled: true,
      reasoning: {
        enabled: false,
        param: DEFAULT_REASONING_PARAM,
        active_tier: "off",
        tiers: [{ tier: "off", api_value: null }],
      },
    };
  }

  async function restoreDefaultModels(provider) {
    if (state.busy) return;
    const label = provider.display_name || provider.id;
    if (!global.confirm("用内置默认模型目录覆盖「" + label + "」当前的模型目录？自定义的模型与参数会被移除。")) return;
    setBusy(true);
    try {
      await request("POST", apiPath(SETTINGS_API.providerModelsRestore, { pid: provider.id }));
      showStatus("ok", "已把「" + label + "」的模型目录恢复为内置默认。");
      await loadAll();
    } catch (err) {
      showStatus("error", err.message);
    } finally {
      setBusy(false);
    }
  }

  async function removeModelCard(provider, draft, onListChange) {
    if (state.busy) return;
    if (draft.isNew) {
      state.models = state.models.filter((item) => item !== draft);
      onListChange();
      return;
    }
    const modelId = draft.origId || draft.id;
    if (!global.confirm("删除模型「" + modelId + "」？该操作会立即写盘。")) return;
    setBusy(true);
    try {
      await request("DELETE", apiPath(SETTINGS_API.providerModel, { pid: provider.id, model_id: modelId }));
      showStatus("ok", "已删除模型 " + modelId + "。");
      await loadAll();
    } catch (err) {
      showStatus("error", err.message);
    } finally {
      setBusy(false);
    }
  }

  /* 推理块转请求体：off 档强制 api_value = null（"关闭思考"不额外发参数，§5.4） */
  function buildReasoningPayload(draft) {
    const rows = draft.reasoning.tiers || [];
    const tiers = rows.map((row) => {
      const apiValue = row.tier === "off" ? null : (row.api_value === null || row.api_value === "" ? null : String(row.api_value).slice(0, 64));
      return { tier: row.tier, api_value: apiValue };
    });
    const param = draft.reasoning.param || DEFAULT_REASONING_PARAM;
    if (!tiers.length) {
      return { enabled: false, param: param, active_tier: "off", tiers: [] };
    }
    let activeTier = draft.reasoning.activeTier;
    if (!tiers.some((row) => row.tier === activeTier)) activeTier = tiers[0].tier;
    return {
      enabled: !!draft.reasoning.enabled,
      param: param,
      active_tier: activeTier,
      tiers: tiers,
    };
  }

  async function saveCatalog(provider) {
    if (state.busy) return;
    if (!state.models.length) {
      showStatus("error", "模型目录为空：至少保留一个模型，或用「恢复默认模型」把内置目录写回来。");
      return;
    }
    const payloads = [];
    for (let i = 0; i < state.models.length; i += 1) {
      const draft = state.models[i];
      const modelId = String(draft.id || "").trim();
      if (!modelId) {
        showStatus("error", "第 " + (i + 1) + " 张模型卡的「模型 ID」不能为空。");
        return;
      }
      const contextWindow = parseTokenCount(draft.contextWindowRaw);
      if (Number.isNaN(contextWindow)) {
        showStatus("error", "模型 " + modelId + " 的「上下文窗口」写法不合法：可填 128K / 1M / 65536。");
        return;
      }
      const maxOutput = parseTokenCount(draft.maxOutputRaw);
      if (Number.isNaN(maxOutput)) {
        showStatus("error", "模型 " + modelId + " 的「最大输出 token 数」写法不合法：可填 8K / 8192。");
        return;
      }
      payloads.push({
        draft: draft,
        body: {
          id: modelId,
          display_name: String(draft.displayName || "").trim() || modelId,
          context_window: contextWindow,
          max_output_tokens: maxOutput,
          input_types: draft.inputTypes.length ? draft.inputTypes.slice() : ["text"],
          enabled: !!draft.enabled,
          reasoning: buildReasoningPayload(draft),
        },
      });
    }

    setBusy(true);
    try {
      let saved = 0;
      for (let i = 0; i < payloads.length; i += 1) {
        const item = payloads[i];
        /* 改过模型 ID 的卡：先删旧 ID 再落新 ID，否则目录里会留一条重命名的孤儿记录 */
        if (item.draft.origId && item.draft.origId !== item.body.id) {
          await request("DELETE", apiPath(SETTINGS_API.providerModel,
            { pid: provider.id, model_id: item.draft.origId }));
        }
        await request("POST", apiPath(SETTINGS_API.providerModels, { pid: provider.id }), item.body);
        saved += 1;
      }
      showStatus("ok", "已保存 " + saved + " 个模型的设置。");
      await loadAll();
    } catch (err) {
      showStatus("error", err.message);
    } finally {
      setBusy(false);
    }
  }

  /* ============================================================
     十、左栏导航绑定（弹层里的内容交互，不属于外壳）
     ============================================================ */
  function setActiveNav(tab) {
    const nav = $("#settingsNav");
    if (!nav) return;
    const items = nav.querySelectorAll("[data-settings-tab]");
    for (let i = 0; i < items.length; i += 1) {
      const on = items[i].getAttribute("data-settings-tab") === tab;
      items[i].classList.toggle("active", on);
      if (on) items[i].setAttribute("aria-current", "page");
      else items[i].removeAttribute("aria-current");
    }
  }

  function bindNav() {
    const nav = $("#settingsNav");
    if (!nav) return;
    const items = nav.querySelectorAll("[data-settings-tab]");
    for (let i = 0; i < items.length; i += 1) {
      items[i].addEventListener("click", () => {
        const tab = items[i].getAttribute("data-settings-tab") || "models";
        if (state.tab === tab) return;
        state.tab = tab;
        setActiveNav(tab);
        showStatus("");
        renderPane();
      });
    }
    setActiveNav(state.tab);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindNav);
  } else {
    bindNav();
  }
})(typeof window !== "undefined" ? window : this);
