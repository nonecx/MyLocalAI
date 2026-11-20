/* ============================================================
   profiles.js —— 档案工作区（多 Profile 分家，v6，2026-11-20）

   定位：本系统是单用户本地工具（无账号无鉴权），"普通用户 / 专业用户"
   不做成账号，而做成同一产品内的多个档案工作区——每个工作区有独立的
   会话、档案与画像数据，顶栏一键切换。

   本文件负责：
     1) 顶栏切换器：当前档案 + 角色图标 + 下拉菜单（切换 / 管理入口）；
     2) 管理弹层：列表（改名 / 改角色 / 删除空档案）+ 新建表单；
     3) 切换成功后派发 DOM 事件 mylocalai:profile-changed，
        chat.js 监听后重载左栏（会话/档案）并清空当前会话视图——
        两文件经事件解耦，不互相 import（沿用 ChatCards/SettingsPanel
        的 window 全局 + 事件惯例，但这里用 CustomEvent 更松）。

   纪律（同 settings.js）：
     - 纯 vanilla、IIFE、无 ES module、无框架；
     - 用户输入一律 createElement + textContent，禁止 innerHTML 拼接；
     - 外链只允许 http(s)（本文件不产生外链）；
     - 关闭途径三条：关闭按钮 / 点遮罩 / Esc。
   ============================================================ */
"use strict";

(function () {
  /* file:// 双击打开时 shell.js 已给出启动引导，这里静默退出 */
  if (location.protocol === "file:") return;

  const $ = (sel) => document.querySelector(sel);

  const API = {
    list: "/api/profiles",
    create: "/api/profiles",
    update: "/api/profiles/{id}",
    activate: "/api/profiles/{id}/activate",
    remove: "/api/profiles/{id}",
  };

  /* 角色图标与中文名（与后端 dossier/profiles.py 的 ROLE_LABELS 同口径；
     后端 /api/profiles 的 roles 段也会给出，这里只做图标映射的兜底） */
  const ROLE_META = {
    layperson: { icon: "🔍", label: "普通用户" },
    analyst: { icon: "🧪", label: "专业用户 · 材料分析" },
    monitor: { icon: "📡", label: "专业用户 · 领域监测" },
    full: { icon: "🧭", label: "全部能力" },
  };

  /* 切换/事件名：chat.js 监听同一常量字符串（双侧各自定义，测试锁定一致） */
  const CHANGED_EVENT = "mylocalai:profile-changed";

  const state = {
    profiles: [],
    active: null,
    roles: [],
    busy: false,
    lastFocus: null,
  };

  function apiPath(tpl, id) {
    return String(tpl).replace(/\{id\}/g, encodeURIComponent(String(id)));
  }

  function status(msg, isError) {
    const box = $("#profileStatus");
    if (!box) return;
    box.textContent = msg || "";
    box.classList.toggle("hidden", !msg);
    box.classList.toggle("error", Boolean(isError));
  }

  function roleMeta(role) {
    return ROLE_META[String(role || "")] || { icon: "🧭", label: String(role || "未知") };
  }

  /* ---------- 读取 ---------- */

  async function loadProfiles() {
    const resp = await fetch(API.list);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    state.profiles = (data && data.profiles) || [];
    state.active = (data && data.active) || null;
    state.roles = (data && data.roles) || [];
    renderSwitcher();
    if (!$("#profileDialog").classList.contains("hidden")) renderDialog();
    return state;
  }

  /* ---------- 顶栏切换器 ---------- */

  function renderSwitcher() {
    const active = state.active || {};
    const meta = roleMeta(active.role);
    const icon = $("#profileRoleIcon");
    const name = $("#profileName");
    if (icon) icon.textContent = meta.icon;
    if (name) name.textContent = active.display_name || "默认档案";
    const btn = $("#profileBtn");
    if (btn) {
      btn.title = `当前档案工作区：${active.display_name || "默认档案"}（${meta.label}）`;
    }
    renderMenu();
  }

  function renderMenu() {
    const menu = $("#profileMenu");
    if (!menu) return;
    menu.textContent = "";
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
    if (!menu || !btn) return;
    menu.classList.toggle("hidden", !open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      document.addEventListener("click", onDocClickCloseMenu, true);
    } else {
      document.removeEventListener("click", onDocClickCloseMenu, true);
    }
  }

  function onDocClickCloseMenu(e) {
    const wrap = $("#profileSwitcher");
    if (wrap && !wrap.contains(e.target)) setMenuOpen(false);
  }

  /* ---------- 管理弹层 ---------- */

  function openDialog() {
    const dialog = $("#profileDialog");
    if (!dialog) return;
    state.lastFocus = document.activeElement;
    dialog.classList.remove("hidden");
    status("");
    renderDialog();
    const close = $("#profileDialogClose");
    if (close) close.focus();
  }

  function closeDialog() {
    const dialog = $("#profileDialog");
    if (!dialog) return;
    dialog.classList.add("hidden");
    if (state.lastFocus && state.lastFocus.focus) state.lastFocus.focus();
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
      rename.addEventListener("click", () => {
        const next = window.prompt("新的显示名称：", p.display_name || p.key);
        if (next && next.trim()) update(p.id, { display_name: next.trim() });
      });
      actions.appendChild(rename);

      const roleBtn = document.createElement("button");
      roleBtn.type = "button";
      roleBtn.className = "btn-ghost";
      roleBtn.textContent = "改角色";
      roleBtn.addEventListener("click", () => {
        const options = state.roles.map((r) => r.role).join("/");
        const next = window.prompt(`角色（${options}）：`, p.role);
        if (next && next.trim()) update(p.id, { role: next.trim() });
      });
      actions.appendChild(roleBtn);

      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn-ghost profile-danger";
      del.textContent = "删除";
      del.addEventListener("click", () => {
        if (window.confirm(`删除档案「${p.display_name || p.key}」？仅空档案可删。`)) {
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

  async function update(id, payload) {
    if (state.busy) return;
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
    } catch (err) {
      status(`保存失败：${(err && err.message) || err}`, true);
    } finally {
      state.busy = false;
    }
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
        setMenuOpen(menu.classList.contains("hidden"));
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
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!$("#profileMenu").classList.contains("hidden")) { setMenuOpen(false); return; }
      if (!$("#profileDialog").classList.contains("hidden")) closeDialog();
    });
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
    reload: loadProfiles,
    snapshot: () => ({ profiles: state.profiles, active: state.active }),
    roleMeta,
  };

  document.addEventListener("DOMContentLoaded", () => {
    bind();
    loadProfiles().catch((err) => {
      status(`档案工作区加载失败：${(err && err.message) || err}`, true);
    });
  });
})();
