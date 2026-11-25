/* ============================================================
   profiles.js —— 档案工作区（多 Profile 分家，v7，2026-11-22）

   定位：本系统是单用户本地工具（无账号无鉴权），"普通用户 / 专业用户"
   不做成账号，而做成同一产品内的多个档案工作区——每个工作区有独立的
   会话、档案与画像数据，顶栏一键切换。

   本文件负责：
     1) 顶栏切换器：当前档案 + 角色图标 + 下拉菜单（切换 / 管理入口）；
     2) 管理弹层：列表（改名 / 改角色 / 删除空档案）+ 新建表单；
        改名 / 改角色走 index.html 预置的内联表单（profileRenameForm /
        profileRoleForm，互斥显示），不再用 window.prompt 手输；
     3) 切换成功后派发 DOM 事件 mylocalai:profile-changed，
        chat.js 监听后重载左栏（会话/档案）并清空当前会话视图——
        两文件经事件解耦，不互相 import（沿用 ChatCards/SettingsPanel
        的 window 全局 + 事件惯例，但这里用 CustomEvent 更松）。

   键盘与焦点（v7 补齐）：
     - profileMenu：ArrowUp/ArrowDown 在菜单项间移动焦点，Home/End 到首尾，
       Tab/Shift+Tab 循环不逃逸（菜单开着时）；菜单项本身是 <button>，
       Enter/Space 由浏览器原生激活，不重复绑定；Esc 关闭并把焦点还给
       profileBtn（aria-expanded 由 setMenuOpen 同步）；
     - profileDialog：Tab/Shift+Tab 在弹层内循环（焦点陷阱）；关闭后焦点
       还给打开者（打开者已随菜单销毁时退回 profileBtn）；Esc 关闭保持。

   纪律（同 settings.js）：
     - 纯 vanilla、IIFE、无 ES module、无框架；
     - 用户输入一律 createElement + textContent，禁止 innerHTML 拼接；
     - 外链只允许 http(s)（本文件不产生外链）；
     - 关闭途径三条：关闭按钮 / 点遮罩 / Esc。

    v8 角色可见化（2026-11-24，解决"不知道角色在哪切"）：
     - 顶栏按钮常驻「档案名 · 角色短名」（#profileRoleLabel），角色一眼可见；
     - 菜单顶部新增「切换角色（当前档案）」区：四个角色一键换，当前项打勾，
       每个角色带一句能力说明（说明优先取 GET /api/profiles 的 roles[].description，
       单一事实源在后端 dossier/profiles.py 的 ROLE_DESCRIPTIONS；roles 段缺失时
       退 ROLE_META.label，不凭空编造说明）；
     - 左栏「切换角色」按钮（#chatRoleSwitchBtn）与顶栏按钮打开同一个菜单，
       键盘焦点直接落到当前角色项；
     - 换角色走 PUT /api/profiles/{id} 的 role 字段（与弹层「改角色」同一端点），
       成功后菜单保持打开、就地打勾并显示一行结果；仍派发 CHANGED_EVENT，
       batch/monitor 面板按新角色重判显隐。
   ============================================================ */
"use strict";

(function () {
  /* file:// 双击打开时 shell.js 已给出启动引导，这里静默退出 */
  if (location.protocol === "file:") return;

  const $ = (sel) => document.querySelector(sel);

  /* apiPath 用 cards.js 收口的公共版（window.ChatCards.apiPath）：单值模式
     替换全部 {...} 占位符，兼容本文件的 {id} 模板——不再自留副本（同 chat.js
     的取用方式；脚本顺序 cards → profiles 有 test_models_ui 锁定）。 */
  const { apiPath } = window.ChatCards;

  const API = {
    list: "/api/profiles",
    create: "/api/profiles",
    update: "/api/profiles/{id}",
    activate: "/api/profiles/{id}/activate",
    remove: "/api/profiles/{id}",
  };

  /* 角色图标 + 短名。对齐来源：
     - 后端 src/mylocalai/dossier/profiles.py 的 ROLE_LABELS（角色封闭枚举
       ROLES 的中文名，/api/profiles 的 roles 段按同一份口径连同 description
       一起返回；本表 label 只作 roles 段缺失时的兜底，不另立一套说明）；
     - index.html 的 #profileRoleSelect（改角色内联表单）四个 option 文案
       与本表 label 逐字一致——改任一侧必须同步另一侧；
     - short 是顶栏常驻标签与菜单项标题的窄口径显示名（两个专业用户角色按
       主能力取名），纯排版用途，不参与后端对拍。 */
  const ROLE_META = {
    layperson: { icon: "🔍", label: "普通用户", short: "普通用户" },
    analyst: { icon: "🧪", label: "专业用户 · 材料分析", short: "材料分析" },
    monitor: { icon: "📡", label: "专业用户 · 领域监测", short: "领域监测" },
    full: { icon: "🧭", label: "全部能力", short: "全部能力" },
  };

  /* roles 段缺失（后端老版本 / 首载失败）时的兜底枚举：顺序与后端 ROLES 一致 */
  const ROLE_FALLBACK = ["layperson", "analyst", "monitor", "full"];

  /* 切换/事件名：chat.js 监听同一常量字符串（双侧各自定义，测试锁定一致） */
  const CHANGED_EVENT = "mylocalai:profile-changed";
  /* 首载完成（轻量）：chat.js 的"当前档案"行在 DOMContentLoaded 就渲染过一次，
     那时本文件的 fetch 还没回来（行是空的）。这里补一次通知让它回填，
     但不派发 CHANGED_EVENT——那条会清空会话视图，首载不该动用户的会话。 */
  const READY_EVENT = "mylocalai:profiles-ready";

  const state = {
    profiles: [],
    active: null,
    roles: [],
    busy: false,
    lastFocus: null,
    editing: null,    // 内联表单当前编辑目标 { id, kind, original, trigger }
    loadError: null,  // 最近一次档案列表加载失败的原因（null = 正常）
    menuNote: null,   // 菜单内一行结果/错误 { text, isError }（换角色后就地反馈）
    menuOpener: null, // 打开菜单的按钮（关闭时把焦点还给它，顶栏/左栏两个入口各还各的）
  };

  function status(msg, isError) {
    const box = $("#profileStatus");
    if (!box) return;
    box.textContent = msg || "";
    box.classList.toggle("hidden", !msg);
    box.classList.toggle("error", Boolean(isError));
  }

  function roleMeta(role) {
    return ROLE_META[String(role || "")] || {
      icon: "🧭", label: String(role || "未知"), short: String(role || "未知"),
    };
  }

  /* 角色说明：只认后端 roles[].description（单一事实源在 dossier/profiles.py 的
     ROLE_DESCRIPTIONS）；roles 段缺失时返回空串，界面只显示角色名，不编造说明。 */
  function roleDescription(role) {
    const hit = state.roles.find((r) => String((r && r.role) || "") === String(role || ""));
    return hit && hit.description ? String(hit.description) : "";
  }

  /* ---------- 读取 ---------- */

  async function loadProfiles() {
    const resp = await fetch(API.list);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    state.profiles = (data && data.profiles) || [];
    state.active = (data && data.active) || null;
    state.roles = (data && data.roles) || [];
    state.loadError = null;
    renderSwitcher();
    if (!$("#profileDialog").classList.contains("hidden")) renderDialog();
    /* 首载/切换后都通知一声：chat.js 只重画"当前档案"行，不动会话视图 */
    document.dispatchEvent(new CustomEvent(READY_EVENT, {
      detail: { profile: state.active || null },
    }));
    return state;
  }

  /* ---------- 顶栏切换器 ---------- */

  function renderSwitcher() {
    const active = state.active || {};
    const meta = roleMeta(active.role);
    const profileName = active.display_name || "默认档案";
    const icon = $("#profileRoleIcon");
    const name = $("#profileName");
    const chip = $("#profileRoleLabel");
    if (icon) icon.textContent = meta.icon;
    if (name) name.textContent = profileName;
    /* 角色短名常驻顶栏：用户不用展开菜单就能看见"我现在是什么角色" */
    if (chip) {
      chip.textContent = meta.short || meta.label;
      chip.title = meta.label;
    }
    const btn = $("#profileBtn");
    if (btn) {
      const desc = roleDescription(active.role);
      btn.title = `当前档案工作区：${profileName}（${meta.label}）`
        + (desc ? `\n${desc}` : "")
        + "\n点此切换角色或档案工作区";
      btn.setAttribute("aria-label",
        `切换角色或档案工作区（当前：${profileName} · ${meta.label}）`);
    }
    renderMenu();
  }

  /* 菜单里的可聚焦项：全部是 <button>（空态说明是 <p>，天然不进 Tab 序列） */
  function menuItems() {
    const menu = $("#profileMenu");
    if (!menu) return [];
    return Array.from(menu.querySelectorAll("button"));
  }

  function focusMenuItem(idx) {
    const items = menuItems();
    if (!items.length) return;
    items[(idx + items.length) % items.length].focus();
  }

  /* 菜单顶部的「切换角色」区（v8）：当前档案的四个角色一键换，当前项打勾。
     角色名与说明都取后端 roles 段（ROLE_META 只兜底图标与短名），不在前端写死
     能力说明——避免"文档说一套、界面说一套"。roles 段缺失时退 ROLE_FALLBACK，
     只显示角色名，不编造说明。 */
  function renderRoleSection() {
    const menu = $("#profileMenu");
    if (!menu) return;
    const active = state.active || {};
    const currentRole = String(active.role || "");

    const title = document.createElement("p");
    title.className = "profile-menu-title";
    title.textContent = "切换角色（当前档案）";
    menu.appendChild(title);

    const group = document.createElement("div");
    group.className = "profile-role-group";
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "切换当前档案的角色");

    const roles = state.roles.length
      ? state.roles.map((r) => ({
        role: String((r && r.role) || ""),
        label: (r && r.label) || "",
        description: (r && r.description) || "",
      }))
      : ROLE_FALLBACK.map((role) => ({ role, label: roleMeta(role).label, description: "" }));

    roles.forEach((r) => {
      const meta = roleMeta(r.role);
      const checked = r.role === currentRole;
      const item = document.createElement("button");
      item.type = "button";
      item.className = "profile-role-item" + (checked ? " active" : "");
      item.setAttribute("role", "menuitemradio");
      item.setAttribute("aria-checked", checked ? "true" : "false");
      item.dataset.role = r.role;                 // 换角色后按 role 找回焦点

      const icon = document.createElement("span");
      icon.className = "profile-role-icon";
      icon.textContent = meta.icon;

      const body = document.createElement("span");
      body.className = "profile-role-body";
      const name = document.createElement("span");
      name.className = "profile-role-name";
      name.textContent = r.label || meta.label;
      body.appendChild(name);
      if (r.description) {
        const desc = document.createElement("span");
        desc.className = "profile-role-desc muted";
        desc.textContent = r.description;
        body.appendChild(desc);
      }

      const check = document.createElement("span");
      check.className = "profile-role-check";
      check.setAttribute("aria-hidden", "true");   // 选中态已由 aria-checked 表达
      check.textContent = checked ? "✓" : "";

      item.appendChild(icon);
      item.appendChild(body);
      item.appendChild(check);
      item.addEventListener("click", () => changeRole(r.role));
      group.appendChild(item);
    });
    menu.appendChild(group);

    /* 换角色的结果 / 失败原因就地显示（菜单开着时唯一可见的状态位） */
    if (state.menuNote) {
      const note = document.createElement("p");
      note.className = "profile-role-note" + (state.menuNote.isError ? " error" : "");
      note.setAttribute("role", "status");
      note.setAttribute("aria-live", "polite");
      note.textContent = state.menuNote.text;
      menu.appendChild(note);
    }

    const sep = document.createElement("div");
    sep.className = "profile-menu-sep";
    menu.appendChild(sep);

    const next = document.createElement("p");
    next.className = "profile-menu-title";
    next.textContent = "切换档案工作区";
    menu.appendChild(next);
  }

  function renderMenu() {
    const menu = $("#profileMenu");
    if (!menu) return;
    menu.textContent = "";
    renderRoleSection();
    if (!state.profiles.length) {
      /* 防御空态：理论上至少有默认档案；真没数据（或首载失败）时菜单里
         给一行说明，不让用户对着空菜单发呆（状态行在弹层内，此刻不可见） */
      const empty = document.createElement("p");
      empty.className = "profile-menu-empty";
      empty.textContent = "暂无档案数据，点管理查看";
      menu.appendChild(empty);
    }
    state.profiles.forEach((p) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "profile-menu-item" + (p.is_active ? " active" : "");
      item.setAttribute("role", "menuitemradio");
      item.setAttribute("aria-checked", p.is_active ? "true" : "false");
      const icon = document.createElement("span");
      icon.className = "profile-role-icon";
      icon.textContent = roleMeta(p.role).icon;
      const name = document.createElement("span");
      name.className = "profile-menu-name";
      name.textContent = p.display_name || p.key;
      const meta = document.createElement("span");
      meta.className = "profile-menu-meta muted";
      meta.textContent = `${roleMeta(p.role).label} · ${p.session_count || 0} 会话 / ${p.dossier_count || 0} 档案`;
      item.appendChild(icon);
      item.appendChild(name);
      item.appendChild(meta);
      item.addEventListener("click", () => {
        setMenuOpen(false);
        if (!p.is_active) activate(p.id);
      });
      menu.appendChild(item);
    });
    const manage = document.createElement("button");
    manage.type = "button";
    manage.className = "profile-menu-item profile-menu-manage";
    manage.setAttribute("role", "menuitem");
    manage.textContent = "⚙️ 管理档案工作区…";
    manage.addEventListener("click", () => {
      setMenuOpen(false);
      openDialog();
    });
    menu.appendChild(manage);
  }

  function setMenuOpen(open) {
    const menu = $("#profileMenu");
    const btn = $("#profileBtn");
    const sideBtn = $("#chatRoleSwitchBtn");
    if (!menu || !btn) return;
    menu.classList.toggle("hidden", !open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    /* 左栏「切换角色」按钮驱动同一个菜单，展开态必须同步（ARIA 状态跟着菜单走） */
    if (sideBtn) sideBtn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      /* 每次打开重渲染：丢掉上次的换角色结果行，勾选按最新 active 重算 */
      state.menuNote = null;
      renderMenu();
      clampMenuToViewport();
      window.addEventListener("resize", clampMenuToViewport);
      document.addEventListener("click", onDocClickCloseMenu, true);
      /* 打开即把焦点送进菜单第一项（:focus-visible 样式保证鼠标用户看不到焦点环） */
      const items = menuItems();
      if (items.length) items[0].focus();
    } else {
      window.removeEventListener("resize", clampMenuToViewport);
      document.removeEventListener("click", onDocClickCloseMenu, true);
      /* 焦点还留在菜单里（键盘激活/刚被销毁）时收回打开它的按钮 */
      if (menu.contains(document.activeElement)) {
        const back = state.menuOpener && document.contains(state.menuOpener)
          ? state.menuOpener : btn;
        back.focus();
      }
    }
  }

  /* 菜单水平夹取（窄屏）：按钮不在屏幕最右时，CSS 的 right:0 会把 300px 宽的
     菜单顶出视口（390px 宽实测 left=-117，角色名被切掉）。这里按视口夹一次，
     左右各留 12px；宽屏下算出来的值与 right:0 等效，不改变既有观感。 */
  function clampMenuToViewport() {
    const menu = $("#profileMenu");
    const wrap = $("#profileSwitcher");
    if (!menu || !wrap || menu.classList.contains("hidden")) return;
    menu.style.left = "";
    menu.style.right = "";
    const margin = 12;
    const wrapRect = wrap.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    const desiredLeft = Math.min(
      wrapRect.right - menuRect.width,
      window.innerWidth - menuRect.width - margin,
    );
    const left = Math.max(margin, desiredLeft);
    menu.style.left = `${Math.round(left - wrapRect.left)}px`;
    menu.style.right = "auto";
  }

  function onDocClickCloseMenu(e) {
    const wrap = $("#profileSwitcher");
    const sideBtn = $("#chatRoleSwitchBtn");
    /* 左栏入口驱动的是同一个菜单：点它不算"点外面"（否则先关再开，焦点会闪） */
    if (sideBtn && sideBtn.contains(e.target)) return;
    if (wrap && !wrap.contains(e.target)) setMenuOpen(false);
  }

  /* 菜单项键盘导航：方向键移动焦点、Home/End 到首尾、Tab 循环不逃逸；
     Enter/Space 激活走 <button> 原生行为，不重复绑定（避免二次触发）。 */
  function onMenuKeydown(e) {
    const items = menuItems();
    if (!items.length) return;
    const current = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      focusMenuItem(current < 0 ? 0 : current + 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      focusMenuItem(current < 0 ? items.length - 1 : current - 1);
    } else if (e.key === "Home") {
      e.preventDefault();
      items[0].focus();
    } else if (e.key === "End") {
      e.preventDefault();
      items[items.length - 1].focus();
    } else if (e.key === "Tab") {
      e.preventDefault();
      const down = !e.shiftKey;
      focusMenuItem(current < 0 ? (down ? 0 : items.length - 1) : current + (down ? 1 : -1));
    }
  }

  /* ---------- 内联编辑表单（改名 / 改角色，互斥显示） ---------- */

  function inlineForms() {
    return { rename: $("#profileRenameForm"), role: $("#profileRoleForm") };
  }

  function inlineFormOpen() {
    const forms = inlineForms();
    return Boolean(
      (forms.rename && !forms.rename.classList.contains("hidden"))
      || (forms.role && !forms.role.classList.contains("hidden"))
    );
  }

  function hideInlineForms() {
    const forms = inlineForms();
    if (forms.rename) forms.rename.classList.add("hidden");
    if (forms.role) forms.role.classList.add("hidden");
    state.editing = null;
  }

  /* 关闭内联表单；refocus=true 时把焦点还给触发行按钮（取消 / Esc 路径） */
  function closeInlineForm(refocus) {
    const trigger = state.editing && state.editing.trigger;
    hideInlineForms();
    if (refocus && trigger && document.contains(trigger) && trigger.focus) trigger.focus();
  }

  /* 点行内「改名 / 改角色」打开对应表单（弹层内、列表下方，预填当前值）；
     trigger 记住触发行按钮，取消/Esc 时焦点还给原按钮。 */
  function openInlineForm(kind, profile, trigger) {
    if (!profile) return;
    const forms = inlineForms();
    const form = kind === "rename" ? forms.rename : forms.role;
    if (!form) return;
    hideInlineForms();                          // 互斥：同一时刻只显示一个
    state.editing = {
      id: profile.id,
      kind: kind,
      original: kind === "rename"
        ? String(profile.display_name || profile.key || "")
        : String(profile.role || ""),
      trigger: trigger || null,
    };
    const name = profile.display_name || profile.key;
    const title = form.querySelector(".profile-inline-form-title");
    if (title) title.textContent = `${kind === "rename" ? "改名" : "改角色"}：${name}`;
    if (kind === "rename") {
      const input = $("#profileRenameInput");
      if (input) {
        input.value = profile.display_name || profile.key || "";
        input.focus();
        input.select();
      }
    } else {
      const select = $("#profileRoleSelect");
      if (select) {
        /* 四角色封闭枚举（index.html option 与 ROLE_META 对齐）；
           出现枚举外角色时保持默认 full，不手输、不猜 */
        const hit = Array.from(select.options).some((o) => o.value === String(profile.role || ""));
        if (hit) select.value = String(profile.role);
        select.focus();
      }
    }
    form.classList.remove("hidden");
    form.scrollIntoView({ block: "nearest" });  // 列表较长时把表单滚进可视区
  }

  async function confirmRename() {
    const input = $("#profileRenameInput");
    const edit = state.editing;
    if (!input || !edit || edit.kind !== "rename") return;
    const next = input.value.trim();
    if (!next) {
      status("显示名称不能为空。", true);
      return;
    }
    if (next === edit.original) {
      closeInlineForm(true);                    // 没改东西就不打接口
      return;
    }
    const ok = await update(edit.id, { display_name: next });
    if (ok) hideInlineForms();                  // 成功后行已重渲染，不回焦旧行按钮
  }

  async function confirmRole() {
    const select = $("#profileRoleSelect");
    const edit = state.editing;
    if (!select || !edit || edit.kind !== "role") return;
    if (select.value === edit.original) {
      closeInlineForm(true);
      return;
    }
    const ok = await update(edit.id, { role: select.value });
    if (ok) hideInlineForms();
  }

  /* 表单标题（写清目标档案名）动态补进 index.html 预置的表单骨架；
     index.html 不在 U3 手里，DOM 由 JS 最小注入（createElement + textContent）。 */
  function ensureInlineTitles() {
    const addTitle = (form, base) => {
      if (!form || form.querySelector(".profile-inline-form-title")) return;
      const t = document.createElement("p");
      t.className = "profile-inline-form-title";
      t.textContent = base;
      form.insertBefore(t, form.firstChild);
    };
    addTitle($("#profileRenameForm"), "改名");
    addTitle($("#profileRoleForm"), "改角色");
  }

  function bindInlineForms() {
    const renameForm = $("#profileRenameForm");
    if (renameForm) {
      renameForm.addEventListener("submit", (e) => {
        e.preventDefault();                     // 输入框里按 Enter = 保存
        confirmRename();
      });
      const ok = $("#profileRenameConfirm");
      if (ok) ok.addEventListener("click", confirmRename);
      const cancel = $("#profileRenameCancel");
      if (cancel) cancel.addEventListener("click", () => closeInlineForm(true));
    }
    const roleForm = $("#profileRoleForm");
    if (roleForm) {
      roleForm.addEventListener("submit", (e) => {
        e.preventDefault();
        confirmRole();
      });
      const ok = $("#profileRoleConfirm");
      if (ok) ok.addEventListener("click", confirmRole);
      const cancel = $("#profileRoleCancel");
      if (cancel) cancel.addEventListener("click", () => closeInlineForm(true));
    }
    ensureInlineTitles();
  }

  /* ---------- 管理弹层 ---------- */

  function openDialog() {
    const dialog = $("#profileDialog");
    if (!dialog) return;
    state.lastFocus = document.activeElement;
    dialog.classList.remove("hidden");
    hideInlineForms();                          // 上次会话遗留的编辑态不带入
    /* 首载失败时如实显示（profileStatus 有 role=status，读屏器可感知） */
    status(state.loadError ? `档案工作区加载失败：${state.loadError}` : "");
    renderDialog();
    const close = $("#profileDialogClose");
    if (close) close.focus();
  }

  function closeDialog() {
    const dialog = $("#profileDialog");
    if (!dialog) return;
    dialog.classList.add("hidden");
    hideInlineForms();
    const back = state.lastFocus;
    if (back && back.focus && document.contains(back)) {
      back.focus();
    } else {
      /* 打开者（管理入口）已随菜单重渲染销毁：退回顶栏切换按钮 */
      const btn = $("#profileBtn");
      if (btn) btn.focus();
    }
  }

  function renderDialog() {
    renderRoleOptions();
    const list = $("#profileList");
    if (!list) return;
    list.textContent = "";
    if (!state.profiles.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "暂无档案工作区。";
      list.appendChild(empty);
      return;
    }
    state.profiles.forEach((p) => {
      const row = document.createElement("div");
      row.className = "profile-row" + (p.is_active ? " active" : "");

      const head = document.createElement("div");
      head.className = "profile-row-head";
      const icon = document.createElement("span");
      icon.className = "profile-role-icon";
      icon.textContent = roleMeta(p.role).icon;
      const name = document.createElement("strong");
      name.textContent = p.display_name || p.key;
      const badge = document.createElement("span");
      badge.className = "badge gray";
      badge.textContent = p.is_active ? "当前" : roleMeta(p.role).label;
      head.appendChild(icon);
      head.appendChild(name);
      head.appendChild(badge);

      const meta = document.createElement("div");
      meta.className = "profile-row-meta muted";
      meta.textContent =
        `key: ${p.key} · 角色: ${roleMeta(p.role).label} · `
        + `${p.session_count || 0} 会话 / ${p.dossier_count || 0} 档案`;

      const actions = document.createElement("div");
      actions.className = "profile-row-actions";

      if (!p.is_active) {
        const use = document.createElement("button");
        use.type = "button";
        use.className = "btn-ghost";
        use.textContent = "切换到此";
        use.addEventListener("click", () => activate(p.id));
        actions.appendChild(use);
      }

      const rename = document.createElement("button");
      rename.type = "button";
      rename.className = "btn-ghost";
      rename.textContent = "改名";
      rename.addEventListener("click", () => openInlineForm("rename", p, rename));
      actions.appendChild(rename);

      const roleBtn = document.createElement("button");
      roleBtn.type = "button";
      roleBtn.className = "btn-ghost";
      roleBtn.textContent = "改角色";
      roleBtn.addEventListener("click", () => openInlineForm("role", p, roleBtn));
      actions.appendChild(roleBtn);

      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn-ghost profile-danger";
      del.textContent = "删除";
      /* 删除按钮只有「删除」两字，读屏器需要带上目标档案名 */
      del.setAttribute("aria-label", `删除档案 ${p.display_name || p.key}`);
      del.addEventListener("click", () => {
        /* 确认文案带显示名（对齐 monitor.js 删除订阅的口径） */
        if (window.confirm(`删除档案「${p.display_name || p.key}」？删除后不可恢复（仅空档案可删）。`)) {
          remove(p.id);
        }
      });
      actions.appendChild(del);

      row.appendChild(head);
      row.appendChild(meta);
      row.appendChild(actions);
      list.appendChild(row);
    });
  }

  function renderRoleOptions() {
    const select = $("#profileNewRole");
    if (!select) return;
    const roles = state.roles.length
      ? state.roles
      : Object.keys(ROLE_META).map((r) => ({ role: r, label: roleMeta(r).label }));
    select.textContent = "";
    roles.forEach((r) => {
      const opt = document.createElement("option");
      opt.value = r.role;
      opt.textContent = `${r.label || roleMeta(r.role).label}（${r.role}）`;
      select.appendChild(opt);
    });
    const full = Array.from(select.options).find((o) => o.value === "full");
    if (full) select.value = "full";
  }

  /* ---------- 弹层焦点陷阱 ---------- */

  function isDialogOpen() {
    const dialog = $("#profileDialog");
    return Boolean(dialog && !dialog.classList.contains("hidden"));
  }

  /* 弹层内可聚焦元素：跳过禁用项与不可见项（hidden 的内联表单/状态行 offsetParent
     为 null，天然排除） */
  function dialogFocusables() {
    const dialog = $("#profileDialog");
    if (!dialog) return [];
    return Array.from(dialog.querySelectorAll("button, input, select, a[href], textarea"))
      .filter((el) => !el.disabled && el.offsetParent !== null);
  }

  /* Tab/Shift+Tab 在弹层内循环；焦点落在弹层外（重渲染把原按钮换掉等）时拉回来 */
  function trapDialogTab(e) {
    if (!isDialogOpen()) return;
    const focusables = dialogFocusables();
    if (!focusables.length) {
      e.preventDefault();
      return;
    }
    const current = focusables.indexOf(document.activeElement);
    if (current < 0) {
      e.preventDefault();
      (e.shiftKey ? focusables[focusables.length - 1] : focusables[0]).focus();
      return;
    }
    const last = focusables.length - 1;
    if (!e.shiftKey && current === last) {
      e.preventDefault();
      focusables[0].focus();
    } else if (e.shiftKey && current === 0) {
      e.preventDefault();
      focusables[last].focus();
    }
  }

  /* ---------- 动作 ---------- */

  async function activate(id) {
    if (state.busy) return;
    state.busy = true;
    try {
      const resp = await fetch(apiPath(API.activate, id), { method: "POST" });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      await loadProfiles();
      /* 通知 chat.js：左栏换边 + 清空当前会话视图（会话归属可能已变） */
      document.dispatchEvent(new CustomEvent(CHANGED_EVENT, {
        detail: { profile: data.profile || null },
      }));
    } catch (err) {
      status(`切换失败：${(err && err.message) || err}`, true);
    } finally {
      state.busy = false;
    }
  }

  /* 返回 true/false 供内联表单决定是否收起（失败时表单保持打开，输入不丢） */
  async function update(id, payload) {
    if (state.busy) return false;
    state.busy = true;
    status("");
    try {
      const resp = await fetch(apiPath(API.update, id), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      await loadProfiles();
      /* 改名/改角色也可能影响当前激活档案的展示：同样广播 */
      document.dispatchEvent(new CustomEvent(CHANGED_EVENT, {
        detail: { profile: data.profile || null, renamed: true },
      }));
      return true;
    } catch (err) {
      status(`保存失败：${(err && err.message) || err}`, true);
      return false;
    } finally {
      state.busy = false;
    }
  }

  /* 换当前档案的角色（v8 菜单一键换）：与弹层「改角色」同一端点（PUT role 字段），
     成功后菜单保持打开、就地打勾并显示一行结果；焦点按 role 找回刚选的项，
     键盘连续切换不丢位。失败时同一行显示后端 detail，选中态不动（不假装成功）。 */
  async function changeRole(role) {
    const active = state.active;
    const target = String(role || "");
    if (!active || !target) return;
    if (target === String(active.role || "")) {
      state.menuNote = null;
      renderMenu();
      focusRoleItem(target);
      return;
    }
    if (state.busy) return;
    state.busy = true;
    try {
      const resp = await fetch(apiPath(API.update, active.id), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: target }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      const desc = roleDescription(target);
      state.menuNote = {
        text: `已切换为「${roleMeta(target).label}」`,
        isError: false,
      };
      /* loadProfiles 内部 renderSwitcher → renderMenu：顶栏标签、菜单勾选、
         左栏"当前档案"行（经 CHANGED_EVENT）一起换到新角色 */
      await loadProfiles();
      focusRoleItem(target);
      document.dispatchEvent(new CustomEvent(CHANGED_EVENT, {
        detail: { profile: data.profile || null, role_changed: true },
      }));
    } catch (err) {
      state.menuNote = { text: `切换角色失败：${(err && err.message) || err}`, isError: true };
      renderMenu();
      focusRoleItem(target);
    } finally {
      state.busy = false;
    }
  }

  /* 按 role 找回菜单里的角色项（菜单每次重渲染都会换掉按钮节点） */
  function focusRoleItem(role) {
    const menu = $("#profileMenu");
    if (!menu) return;
    const target = Array.from(menu.querySelectorAll("button[data-role]"))
      .find((el) => el.dataset.role === String(role || ""));
    if (target) target.focus();
  }

  /* 左栏「切换角色」入口：打开与顶栏同一个菜单，焦点直接落在当前角色项上
     （不用先按方向键找；用户看到的第一眼就是"角色可以在这里换"）。 */
  function openRoleSection() {
    const menu = $("#profileMenu");
    const sideBtn = $("#chatRoleSwitchBtn");
    if (!menu) return;
    if (menu.classList.contains("hidden")) {
      state.menuOpener = sideBtn || $("#profileBtn");
      setMenuOpen(true);
    }
    focusRoleItem((state.active || {}).role);
  }

  async function create(key, displayName, role) {
    if (state.busy) return;
    state.busy = true;
    status("");
    try {
      const resp = await fetch(API.create, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, display_name: displayName, role }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      await loadProfiles();
      status(`已创建「${displayName}」。`);
    } catch (err) {
      status(`创建失败：${(err && err.message) || err}`, true);
    } finally {
      state.busy = false;
    }
  }

  async function remove(id) {
    if (state.busy) return;
    state.busy = true;
    status("");
    try {
      const resp = await fetch(apiPath(API.remove, id), { method: "DELETE" });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      await loadProfiles();
      status("已删除。");
    } catch (err) {
      status(`删除失败：${(err && err.message) || err}`, true);
    } finally {
      state.busy = false;
    }
  }

  /* ---------- 事件绑定 ---------- */

  function bind() {
    const btn = $("#profileBtn");
    const menu = $("#profileMenu");
    if (btn && menu) {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const open = menu.classList.contains("hidden");
        if (open) state.menuOpener = btn;
        setMenuOpen(open);
      });
      /* 菜单没开时方向键也能开并把焦点送进首/尾项（ARIA menu 惯例） */
      btn.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
        e.preventDefault();
        if (menu.classList.contains("hidden")) {
          state.menuOpener = btn;
          setMenuOpen(true);
          return;
        }
        const items = menuItems();
        if (!items.length) return;
        (e.key === "ArrowUp" ? items[items.length - 1] : items[0]).focus();
      });
      menu.addEventListener("keydown", onMenuKeydown);
    }
    /* 左栏「切换角色」：打开同一个菜单，焦点直接落在当前角色项 */
    const sideBtn = $("#chatRoleSwitchBtn");
    if (sideBtn && menu) {
      sideBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        openRoleSection();
      });
      sideBtn.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
        e.preventDefault();
        openRoleSection();
      });
    }
    const dialog = $("#profileDialog");
    const close = $("#profileDialogClose");
    if (close) close.addEventListener("click", closeDialog);
    if (dialog) {
      dialog.addEventListener("click", (e) => {
        if (e.target === dialog) closeDialog();
      });
    }
    /* 全局键：Esc 按 菜单 → 内联表单 → 弹层 的层级收；Tab 只在弹层开着时设陷阱 */
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        const openMenu = $("#profileMenu");
        if (openMenu && !openMenu.classList.contains("hidden")) {
          setMenuOpen(false);
          return;
        }
        if (inlineFormOpen()) {
          closeInlineForm(true);
          return;
        }
        const openDialogEl = $("#profileDialog");
        if (openDialogEl && !openDialogEl.classList.contains("hidden")) closeDialog();
        return;
      }
      if (e.key === "Tab") trapDialogTab(e);
    });
    bindInlineForms();
    const form = $("#profileCreateForm");
    if (form) {
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        const key = $("#profileNewKey").value.trim();
        const name = $("#profileNewName").value.trim();
        const role = $("#profileNewRole").value || "full";
        if (!key || !name) {
          status("key 与显示名称都不能为空。", true);
          return;
        }
        create(key, name, role);
      });
    }
  }

  /* 供 chat.js / 测试读取的只读快照（不暴露写操作） */
  window.ProfilesPanel = {
    CHANGED_EVENT,
    READY_EVENT,
    reload: loadProfiles,
    snapshot: () => ({ profiles: state.profiles, active: state.active }),
    roleMeta,
  };

  document.addEventListener("DOMContentLoaded", () => {
    bind();
    loadProfiles().catch((err) => {
      /* 首载失败：profileStatus 在弹层内（此刻不可见），错误先记下，
         打开弹层时如实显示；菜单经 renderSwitcher 给出空态说明 */
      state.loadError = (err && err.message) || String(err);
      renderSwitcher();
      status(`档案工作区加载失败：${state.loadError}`, true);
    });
  });
})();
