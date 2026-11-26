# -*- coding: utf-8 -*-
"""能力使用率审计（只读）：把"注册了什么"和"本机真的用了什么"摆到一张表上。

为什么有这个脚本：项目建了 13 项用户能力、20 个对话工具 action、14 份执行层
SKILL.md，但"有代码""有文档""能路由""被用过"是四件不同的事，混在一起看就会
把"写了"当成"有用了"。本脚本只做一件事——用可复现的口径把四者对齐：

  1. `tool_runs`：按工具统计调用次数 / 最近一次时间 / 成功 / 失败 / 降级；
  2. 复合能力入口：建档数、分析运行、材料批次、监测订阅、巡检、关注度快照、
     主张指纹——这些能力的"真实入口"不在对话里，只能数表；
  3. 能力清单对照（反射，不硬编码）：
     - 运行时注册表（`AssistantLoop().registry`）——助手真正认得哪些工具；
     - 模块级注册能力（各 `tools_*.register_all`）——哪些工具"能注册但没接线"；
     - 路由枚举（`router.TOOL_ACTIONS`）——路由模型能吐出的 action 闭集；
     - 能力注册表（`assistant/workflow/registry.py` 的 13 条）——对话入口 action；
     - 由此算出：注册但不可路由 / 在路由枚举但没有服务实现 / 从未被调用过。

**只读铁律**：库用 `mode=ro` 打开（再叠一层 `PRAGMA query_only=1`），不建表、
不迁移、不写库、不发任何网络请求。空库 / 缺表 / 坏库文件一律退化为 0 并在
`notes` 里用中文写明原因，不抛异常——审计工具自己崩掉就失去了审计的意义。

纯函数化：统计逻辑（`summarize_*` / `diff_wiring` / `build_report` /
`render_report`）不碰 I/O，`collect()` 是唯一做 I/O 的编排层，`main()` 只是薄
封装。`tests/test_capability_audit.py` 直接对纯函数喂构造数据。

用法（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 scripts/audit_capability_usage.py
    .venv/Scripts/python.exe -X utf8 scripts/audit_capability_usage.py --json
    set DOSSIER_DB_PATH=D:\\somewhere\\dossier.db     # 换库（与包内 default_db_path 同源）
    .venv/Scripts/python.exe -X utf8 scripts/audit_capability_usage.py --db .tmp\\x.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: 报告结构版本：`--json` 的键冻结在此。加键 = 升版本，防下游解析被静默改坏。
SCHEMA_VERSION = 1

#: 工具调用记录表（本机历史最全的一张）。
TOOL_RUNS_TABLE = "tool_runs"

#: 复合能力的入口表：表名 → (中文说明, 归哪项能力)。只数行数，不解释内容。
ENTRY_TABLES: Tuple[Tuple[str, str, str], ...] = (
    ("dossiers", "建档数（对话建档的唯一落点）", "claim-checking"),
    ("analysis_runs", "档案分析运行（六章节/复检）", "claim-checking"),
    ("batches", "材料批次（专业用户 A 面板入口）", "material-analysis"),
    ("batch_items", "批次条目（逐条核查）", "material-analysis"),
    ("subscriptions", "领域监测订阅（专业用户 B 面板入口）", "domain-monitoring"),
    ("subscription_runs", "订阅运行记录", "domain-monitoring"),
    ("scan_runs", "平台巡检运行（监测平台页）", "domain-monitoring"),
    ("scan_reports", "巡检报告（代码汇总 + LLM 整理）", "domain-monitoring"),
    ("attention_snapshots", "关注度快照（监测档案证据批次）", "topic-understanding"),
    ("claim_fingerprints", "主张指纹（同源分组/复检命中）", "claim-checking"),
)

#: `tool_runs.status` 里算"失败"的取值（loop 实际只写 ok/degraded/cancelled/timeout/error）。
FAILED_STATUSES: Tuple[str, ...] = ("error", "failed", "timeout", "cancelled")

#: 单条记录的归类（顺序即优先级：失败 > 降级 > 成功 > 其它）。
RUN_CLASSES: Tuple[str, ...] = ("ok", "failed", "degraded", "other")


# ---------------------------------------------------------------------------
# 纯函数：统计与对照（不碰 I/O，便于单测）
# ---------------------------------------------------------------------------


def classify_run(status: Any, degraded: Any) -> str:
    """把一条 tool_run 归到 ok / failed / degraded / other。

    优先级：失败 > 降级 > 成功。为什么失败在前——超时/取消的记录 loop 会同时置
    degraded=1，若降级优先就会把"没跑完"算成"跑完了但质量差"，那是两种不同的病。
    status 为空（历史行/半写）且未标降级 → other，不猜成成功。
    """
    name = str(status or "").strip().lower()
    if name in FAILED_STATUSES:
        return "failed"
    if bool(degraded):
        return "degraded"
    if name == "ok":
        return "ok"
    if name == "degraded":
        return "degraded"
    return "other"


def _parse_stamp(value: Any) -> Optional[datetime]:
    """宽松解析 ISO8601 时间戳；解析不了返 None（坏时间不参与"最近一次"比较）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest(stamps: Iterable[Any]) -> Optional[str]:
    """取最近一次时间：能解析的按时间比，全部不可解析时退化为字符串最大。"""
    texts = [str(s or "").strip() for s in stamps]
    texts = [t for t in texts if t]
    if not texts:
        return None
    parsed = [(stamp, text) for text in texts
              for stamp in [_parse_stamp(text)] if stamp is not None]
    if parsed:
        best = max(parsed, key=lambda pair: pair[0])
        return best[1]
    return max(texts)


def summarize_tool_runs(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """按工具聚合调用记录：次数 / 最近一次 / 四类计数。

    rows 每项需含 tool/status/degraded/created_at（缺键按空处理）。tool 名为空的行
    单独计入 `unnamed`，不混进任何工具——"不知道是哪个工具"本身就是要看见的信号。
    """
    tools: Dict[str, Dict[str, Any]] = {}
    unnamed = 0
    total = 0
    for row in rows:
        total += 1
        name = str(row.get("tool") or "").strip()
        if not name:
            unnamed += 1
            continue
        bucket = tools.setdefault(name, {
            "calls": 0, "last_at": None,
            "ok": 0, "failed": 0, "degraded": 0, "other": 0,
            "statuses": {},
        })
        bucket["calls"] += 1
        cls = classify_run(row.get("status"), row.get("degraded"))
        bucket[cls] += 1
        status = str(row.get("status") or "").strip()
        bucket["statuses"][status] = bucket["statuses"].get(status, 0) + 1
        bucket["last_at"] = _latest([bucket["last_at"], row.get("created_at")])
    ordered = {name: tools[name] for name in sorted(tools)}
    return {"total": total, "unnamed": unnamed, "tools": ordered}


def summarize_entries(counts: Mapping[str, Optional[int]]) -> Dict[str, Any]:
    """复合能力入口计数 → 稳定结构（缺表/读失败为 None，不退化成 0 冒充"没有"）。

    这里刻意区分 0 与 None：0 是"表在、确实没有记录"，None 是"没读到"。
    审计报告里这两者结论不同（前者是"能力没被用"，后者是"这次没数到"）。
    """
    entries: Dict[str, Dict[str, Any]] = {}
    missing: List[str] = []
    for table, label, capability in ENTRY_TABLES:
        value = counts.get(table)
        entries[table] = {
            "label": label,
            "capability": capability,
            "rows": int(value) if isinstance(value, int) else None,
            "readable": isinstance(value, int),
        }
        if not isinstance(value, int):
            missing.append(table)
    readable = {table: item["rows"] for table, item in entries.items() if item["readable"]}
    return {
        "entries": entries,
        "missing_tables": missing,
        "readable_total": len(readable),
        "non_empty": sorted(t for t, n in readable.items() if n),
    }


def diff_wiring(runtime_tools: Iterable[str],
                routed_actions: Iterable[str],
                registerable_tools: Iterable[str] = (),
                capability_actions: Iterable[str] = (),
                horizontal_tools: Iterable[str] = (),
                handler_names: Iterable[str] = ()) -> Dict[str, Any]:
    """能力清单四方对照（全部集合运算，不硬编码任何清单）。

    四个集合：
      - runtime：助手运行时注册表（`AssistantLoop().registry.names()`）；
      - routed：路由枚举 `router.TOOL_ACTIONS`（路由模型能吐出的闭集）；
      - registerable：各 `tools_*.register_all` 能注册的名字（"能注册"≠"已接线"）；
      - capability：13 条能力登记的对话入口 action。
    handler_names 为"注册且有可调用 handler"的名字；空表示调用方未做 handler 检查。
    """
    runtime = {str(n) for n in runtime_tools}
    routed = {str(n) for n in routed_actions}
    registerable = {str(n) for n in registerable_tools}
    caps = {str(n) for n in capability_actions}
    horizontal = {str(n) for n in horizontal_tools}
    handlers = {str(n) for n in handler_names}

    # handler_names 为空 = 调用方没做 handler 检查 → 视为"都实现了"，不误报无服务实现；
    # 非空时按它裁剪（路由枚举里有、handler 清单里没有 → 无服务实现）。
    implemented = runtime & handlers if handlers else set(runtime)
    routed_without_handler = sorted((routed - implemented) if handlers else set())
    return {
        "counts": {
            "runtime": len(runtime),
            "routed": len(routed),
            "registerable": len(registerable),
            "capability_actions": len(caps),
            "horizontal": len(horizontal),
        },
        "routed_and_registered": sorted(routed & runtime),
        # 注册了但路由枚举里没有 → 自然语言路由永远到不了（platform_feed 就是这类）。
        # 取"运行时 ∪ 模块可注册"的并集：platform_feed 只有模块级 register_all，
        # 没进运行时注册表，只按运行时算会漏掉它——而那正是最该被看见的一类。
        "registered_not_routed": sorted((runtime | registerable) - routed),
        # 其中"已接进运行时注册表、但路由到不了"的更窄子集
        "runtime_not_routed": sorted(runtime - routed),
        # 路由枚举里有但运行时注册表没有 → 路由模型吐出来必然 ToolNotAllowed
        "routed_not_registered": sorted(routed - runtime),
        # 模块能注册但没进运行时注册表 → "有代码、没接线"
        "registerable_not_wired": sorted(registerable - runtime),
        # 路由枚举里但找不到可调用 handler → "在枚举里但没有服务实现"
        "routed_without_handler": routed_without_handler,
        # 能力入口 action 与路由枚举的差集
        "capability_actions_not_routed": sorted(caps - routed),
        # 路由枚举里既不是能力入口、也不是横向工具的孤儿 action
        "routed_orphan_actions": sorted(routed - caps - horizontal),
    }


def mark_usage(wiring: Mapping[str, Any],
               called_tools: Iterable[str],
               routed_actions: Iterable[str] = ()) -> Dict[str, Any]:
    """把"本机调用过"叠加到能力清单上，算从未被调用过的 action。

    两个"从未调用"清单分开列，因为它们结论不同：
      - `never_called_routed`：路由枚举内没被调过——能力是真的没被用到；
      - `never_called_unrouted`：本来就不在路由枚举里，当然调不到——这是接线问题，
        不是使用率问题。混成一个清单会把两种病说成一种。
    """
    called = {str(n) for n in called_tools}
    routed = {str(n) for n in routed_actions}
    known = routed | {str(n) for n in (wiring.get("registered_not_routed") or [])}
    return {
        "called_actions": sorted(called & known),
        "never_called_routed": sorted(routed - called),
        "never_called_unrouted": sorted(known - routed - called),
        "called_outside_router": sorted(called - known),
        "coverage": f"{len(called & routed)}/{len(routed)}" if routed else "0/0",
    }


def build_report(db_info: Mapping[str, Any],
                 tool_runs: Mapping[str, Any],
                 entries: Mapping[str, Any],
                 wiring: Mapping[str, Any],
                 usage: Mapping[str, Any],
                 capabilities: Mapping[str, Any],
                 notes: Sequence[str]) -> Dict[str, Any]:
    """组装冻结结构的报告（键集合由 `--json` 契约锁定，见测试）。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": str(db_info.get("generated_at") or ""),
        "db": {
            "path": str(db_info.get("path") or ""),
            "exists": bool(db_info.get("exists")),
            "opened_readonly": bool(db_info.get("opened_readonly")),
            "degraded": bool(db_info.get("degraded")),
            "reason": str(db_info.get("reason") or ""),
            "tables": list(db_info.get("tables") or []),
        },
        "tool_runs": {
            "total": int(tool_runs.get("total") or 0),
            "unnamed": int(tool_runs.get("unnamed") or 0),
            "tools": dict(tool_runs.get("tools") or {}),
        },
        "entries": {
            "readable_total": int(entries.get("readable_total") or 0),
            "missing_tables": list(entries.get("missing_tables") or []),
            "non_empty": list(entries.get("non_empty") or []),
            "entries": dict(entries.get("entries") or {}),
        },
        "wiring": {
            "counts": dict(wiring.get("counts") or {}),
            "routed_and_registered": list(wiring.get("routed_and_registered") or []),
            "registered_not_routed": list(wiring.get("registered_not_routed") or []),
            "runtime_not_routed": list(wiring.get("runtime_not_routed") or []),
            "routed_not_registered": list(wiring.get("routed_not_registered") or []),
            "registerable_not_wired": list(wiring.get("registerable_not_wired") or []),
            "routed_without_handler": list(wiring.get("routed_without_handler") or []),
            "capability_actions_not_routed": list(
                wiring.get("capability_actions_not_routed") or []),
            "routed_orphan_actions": list(wiring.get("routed_orphan_actions") or []),
        },
        "usage": {
            "called_actions": list(usage.get("called_actions") or []),
            "never_called_routed": list(usage.get("never_called_routed") or []),
            "never_called_unrouted": list(usage.get("never_called_unrouted") or []),
            "called_outside_router": list(usage.get("called_outside_router") or []),
            "coverage": str(usage.get("coverage") or "0/0"),
        },
        "capabilities": {
            "total": int(capabilities.get("total") or 0),
            "items": dict(capabilities.get("items") or {}),
        },
        "notes": [str(n) for n in notes],
    }


# ---------------------------------------------------------------------------
# 渲染：人读表格（stdout）
# ---------------------------------------------------------------------------


def _width(text: Any) -> int:
    """终端显示宽度：东亚宽字符按 2 列算，否则中文表头会整体错位。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in str(text))


def _pad(text: Any, width: int, align: str = "left") -> str:
    body = str(text)
    gap = max(0, width - _width(body))
    if align == "right":
        return " " * gap + body
    return body + " " * gap


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
           aligns: Optional[Sequence[str]] = None) -> List[str]:
    """渲染一张定宽表（中文对齐按显示宽度算）。"""
    aligns = list(aligns or ["left"] * len(headers))
    widths = [_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _width(cell))
    out = ["  ".join(_pad(h, widths[i]) for i, h in enumerate(headers))]
    out.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        out.append("  ".join(_pad(cell, widths[i], aligns[i]) for i, cell in enumerate(row)))
    return out


def _dash(value: Any) -> str:
    return "—" if value is None else str(value)


def render_report(report: Mapping[str, Any]) -> str:
    """报告 → 人读文本（中文表头 + 分节；空数据也照常出表，不空转）。"""
    db = report.get("db") or {}
    runs = report.get("tool_runs") or {}
    entries = report.get("entries") or {}
    wiring = report.get("wiring") or {}
    usage = report.get("usage") or {}
    caps = report.get("capabilities") or {}

    lines: List[str] = []
    lines.append("=== 能力使用率审计（只读） ===")
    lines.append(f"库：{_dash(db.get('path'))}")
    lines.append(f"生成时间：{_dash(report.get('generated_at'))}")
    lines.append(f"打开方式：{'只读（mode=ro + query_only）' if db.get('opened_readonly') else '未打开'}"
                 f"　库状态：{'降级' if db.get('degraded') else '正常'}"
                 f"　可见表：{len(db.get('tables') or [])} 张")
    if db.get("reason"):
        lines.append(f"降级原因：{db.get('reason')}")
    lines.append("")

    lines.append(f"[1] 工具调用（{TOOL_RUNS_TABLE}，共 {runs.get('total', 0)} 行）")
    tools = runs.get("tools") or {}
    if tools:
        rows = []
        for name, item in tools.items():
            rows.append([name, item.get("calls", 0), _dash(item.get("last_at")),
                         item.get("ok", 0), item.get("failed", 0),
                         item.get("degraded", 0), item.get("other", 0)])
        lines.extend(_table(
            ["工具", "调用", "最近一次", "成功", "失败", "降级", "其它"],
            rows, ["left", "right", "left", "right", "right", "right", "right"]))
    else:
        lines.append("  （没有任何工具调用记录）")
    if runs.get("unnamed"):
        lines.append(f"  另有 {runs['unnamed']} 行工具名为空（计入总数，不归任何工具）")
    lines.append("")

    lines.append("[2] 复合能力入口计数")
    items = entries.get("entries") or {}
    rows = []
    for table, item in items.items():
        rows.append([table, item.get("label", ""), item.get("capability", ""),
                     _dash(item.get("rows"))])
    if rows:
        lines.extend(_table(["表", "说明", "归属能力", "行数"], rows,
                            ["left", "left", "left", "right"]))
    else:
        lines.append("  （没有可读的入口表）")
    if entries.get("missing_tables"):
        lines.append("  缺表/读不到：" + "、".join(entries["missing_tables"]))
    lines.append("")

    counts = wiring.get("counts") or {}
    lines.append("[3] 能力清单对照（反射发现，不硬编码）")
    lines.append(f"  运行时注册 {counts.get('runtime', 0)} 个工具 · 路由枚举 "
                 f"{counts.get('routed', 0)} 个 action · 模块可注册 "
                 f"{counts.get('registerable', 0)} 个 · 能力入口 "
                 f"{counts.get('capability_actions', 0)} 个 · 横向工具 "
                 f"{counts.get('horizontal', 0)} 个")
    wiring_rows = [
        ["注册但不在路由枚举", "自然语言路由永远到不了", wiring.get("registered_not_routed")],
        ["其中已接进运行时", "注册表里有、路由仍到不了", wiring.get("runtime_not_routed")],
        ["在路由枚举但未注册", "路由模型吐出来必然被拒", wiring.get("routed_not_registered")],
        ["可注册但未接线", "有 register_all，运行时没用它", wiring.get("registerable_not_wired")],
        ["在枚举但无服务实现", "找不到可调用 handler", wiring.get("routed_without_handler")],
        ["能力入口不在枚举", "能力登记了但路由吐不出", wiring.get("capability_actions_not_routed")],
        ["孤儿 action", "既非能力入口也非横向工具", wiring.get("routed_orphan_actions")],
    ]
    rendered = []
    for title, meaning, names in wiring_rows:
        rendered.append([title, meaning,
                         "、".join(names) if names else "—"])
    lines.extend(_table(["检查项", "含义", "命中"], rendered))
    lines.append("")

    lines.append(f"[4] 本机实测覆盖（tool_runs 对照路由枚举：{usage.get('coverage', '0/0')}）")
    if usage.get("called_actions"):
        lines.append("  调用过：" + "、".join(usage["called_actions"]))
    if usage.get("never_called_routed"):
        lines.append("  从未调用（路由枚举内）：" + "、".join(usage["never_called_routed"]))
    if usage.get("never_called_unrouted"):
        lines.append("  从未调用（不在路由枚举，接线问题而非使用率问题）："
                     + "、".join(usage["never_called_unrouted"]))
    if usage.get("called_outside_router"):
        lines.append("  不在已知清单却有记录：" + "、".join(usage["called_outside_router"]))
    if not any(usage.get(key) for key in ("called_actions", "never_called_routed",
                                          "never_called_unrouted")):
        lines.append("  （没有可对照的 action）")
    lines.append("")

    lines.append(f"[5] 能力注册表（{caps.get('total', 0)} 条）")
    cap_items = caps.get("items") or {}
    cap_rows = []
    for cap_id, item in cap_items.items():
        actions = item.get("entry_actions") or []
        called = [a for a in actions if a in set(usage.get("called_actions") or [])]
        cap_rows.append([cap_id, item.get("channel", ""), item.get("category", ""),
                         "、".join(actions) if actions else "（面板通道）",
                         f"{len(called)}/{len(actions)}" if actions else "—"])
    if cap_rows:
        lines.extend(_table(["能力", "通道", "分类", "对话入口", "本机调用"],
                            cap_rows, ["left", "left", "left", "left", "right"]))
    else:
        lines.append("  （能力注册表读不到）")
    lines.append("")

    notes = report.get("notes") or []
    if notes:
        lines.append("[6] 说明与边界")
        for note in notes:
            lines.append(f"  - {note}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# I/O 层：只读打开 + 读表（唯一会碰磁盘的地方）
# ---------------------------------------------------------------------------


def _readonly_uri(path: Path) -> str:
    """构造 SQLite 只读 URI（百分号编码，中文/空格路径安全）。

    `as_uri()` 要求绝对路径，所以先 resolve（strict=False，不要求文件存在）。
    """
    return path.resolve().as_uri() + "?mode=ro"


def open_readonly(path: Path) -> Tuple[Optional[sqlite3.Connection], str]:
    """以只读方式打开库；失败返 (None, 中文原因)，绝不抛。

    三道保险：① URI `mode=ro`（内核层拒绝写）；② `PRAGMA query_only=1`（本连接
    即便被误用写语句也会被拒）；③ 不调用任何 migrate（审计不是迁移）。
    打开后会主动读一次 `sqlite_master`——坏库/非 SQLite 文件在第一次真正读页时才
    报错，不先探一下就会把它当成"空库"，把"打不开"说成"没有记录"。
    """
    if not path.exists():
        return None, f"库文件不存在：{path}（按空库 0 统计）"
    try:
        conn = sqlite3.connect(_readonly_uri(path), uri=True)
    except sqlite3.Error as exc:
        return None, f"只读打开失败（{type(exc).__name__}: {exc}）"
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = 1")
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.Error as exc:
        conn.close()
        return None, f"库文件不是可用的 SQLite 库（{type(exc).__name__}: {exc}）"
    return conn, ""


def list_tables(conn: Optional[sqlite3.Connection]) -> List[str]:
    """库里的表名（按名字排序）；连接为空或坏库返空列表。"""
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [str(row[0]) for row in rows]


def count_rows(conn: Optional[sqlite3.Connection], table: str) -> Optional[int]:
    """数一张表的行数；表不存在 / 坏库 → None（区分"没有记录"与"没读到"）。"""
    if conn is None:
        return None
    try:
        row = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row is not None else None


def read_tool_runs(conn: Optional[sqlite3.Connection]) -> Tuple[List[Dict[str, Any]], str]:
    """读 tool_runs 的四个审计列；表缺失/列缺失都降级为能读多少读多少。

    列名先经 `PRAGMA table_info` 取交集再拼 SELECT——老 schema 少列时不会整表读失败，
    缺的列按 None 处理（`classify_run` 会把它们归到 other 而不是猜成成功）。
    """
    if conn is None:
        return [], "库未打开"
    wanted = ("tool", "status", "degraded", "created_at")
    try:
        info = conn.execute(f'PRAGMA table_info("{TOOL_RUNS_TABLE}")').fetchall()
    except sqlite3.Error as exc:
        return [], f"{TOOL_RUNS_TABLE} 结构读取失败（{type(exc).__name__}: {exc}）"
    if not info:
        return [], f"{TOOL_RUNS_TABLE} 表不存在（按 0 行统计）"
    present = [str(row[1]) for row in info]
    columns = [name for name in wanted if name in present]
    if "tool" not in columns:
        return [], f"{TOOL_RUNS_TABLE} 缺少 tool 列（按 0 行统计）"
    sql = "SELECT " + ", ".join(f'"{name}"' for name in columns) + f' FROM "{TOOL_RUNS_TABLE}"'
    try:
        raw = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        return [], f"{TOOL_RUNS_TABLE} 读取失败（{type(exc).__name__}: {exc}）"
    rows: List[Dict[str, Any]] = []
    for row in raw:
        item = dict(row)
        rows.append({name: item.get(name) for name in wanted})
    return rows, ""


# ---------------------------------------------------------------------------
# 反射层：从包内取四份清单（不硬编码任何名字）
# ---------------------------------------------------------------------------


def discover_runtime_tools() -> Tuple[List[str], List[str], str]:
    """运行时注册表：助手真正认得的工具名 + 其中有可调用 handler 的。

    走 `AssistantLoop().registry`——这是线上对话实际用的那份注册表，不是"理论上
    能注册什么"。导入失败返空清单 + 原因（审计工具不能因为包坏了就崩）。
    """
    try:
        from mylocalai.assistant.loop import AssistantLoop
    except Exception as exc:  # noqa: BLE001  包不可导入时如实降级
        return [], [], f"助手运行时注册表不可导入（{type(exc).__name__}: {exc}）"
    try:
        registry = AssistantLoop().registry
        names = list(registry.names())
    except Exception as exc:  # noqa: BLE001  装配失败同样是"没数到"
        return [], [], f"助手运行时注册表装配失败（{type(exc).__name__}: {exc}）"
    handlers = [name for name in names
                if callable((registry.get(name) or {}).get("handler"))]
    return sorted(names), sorted(handlers), ""


def discover_registerable_tools() -> Tuple[List[str], List[str], str]:
    """模块级注册能力：扫 `assistant/tools_*.py`，逐个调 `register_all` 收集工具名。

    与运行时注册表的差集就是"有代码、没接线"（如 `platform_feed`）。这里**只调
    注册函数**，不执行任何 handler，因此不触发任何 I/O。
    """
    try:
        import importlib
        import pkgutil

        import mylocalai.assistant as assistant_pkg
        from mylocalai.assistant.registry import ToolRegistry
    except Exception as exc:  # noqa: BLE001
        return [], [], f"工具模块扫描不可用（{type(exc).__name__}: {exc}）"
    names: List[str] = []
    modules: List[str] = []
    failures: List[str] = []
    for info in pkgutil.iter_modules(assistant_pkg.__path__):
        if not info.name.startswith("tools_"):
            continue
        try:
            module = importlib.import_module(f"mylocalai.assistant.{info.name}")
        except Exception as exc:  # noqa: BLE001  单个模块坏了不影响其余
            failures.append(f"{info.name}({type(exc).__name__})")
            continue
        register = getattr(module, "register_all", None)
        if not callable(register):
            continue
        registry = ToolRegistry()
        try:
            register(registry, None)
        except Exception as exc:  # noqa: BLE001  注册契约坏掉 → 记下来继续
            failures.append(f"{info.name}({type(exc).__name__})")
            continue
        found = list(registry.names())
        if found:
            modules.append(info.name)
            names.extend(found)
    reason = ("部分工具模块注册失败：" + "、".join(failures)) if failures else ""
    return sorted(set(names)), sorted(set(modules)), reason


def discover_routed_actions() -> Tuple[List[str], str]:
    """路由枚举 `router.TOOL_ACTIONS`（路由模型能吐出的 action 闭集）。"""
    try:
        from mylocalai.assistant import router
        return sorted(str(a) for a in router.TOOL_ACTIONS), ""
    except Exception as exc:  # noqa: BLE001
        return [], f"路由枚举不可导入（{type(exc).__name__}: {exc}）"


def discover_capabilities() -> Tuple[Dict[str, Any], List[str], List[str], str]:
    """能力注册表（13 条）→ (逐条视图, 全部入口 action, 横向工具, 原因)。"""
    try:
        from mylocalai.assistant.workflow import registry as cap_registry
    except Exception as exc:  # noqa: BLE001
        return {}, [], [], f"能力注册表不可导入（{type(exc).__name__}: {exc}）"
    try:
        items = cap_registry.capabilities()
        actions = sorted(cap_registry.entry_action_set())
        horizontal = sorted(str(n) for n in cap_registry.HORIZONTAL_TOOLS)
    except Exception as exc:  # noqa: BLE001
        return {}, [], [], f"能力注册表读取失败（{type(exc).__name__}: {exc}）"
    view = {
        cap_id: {
            "channel": str(item.get("channel") or ""),
            "category": str(item.get("category") or ""),
            "entry_actions": list(item.get("entry_actions") or []),
            "composite": bool(item.get("composite")),
        }
        for cap_id, item in items.items()
    }
    return view, actions, horizontal, ""


def resolve_db_path(explicit: Optional[str] = None) -> Path:
    """库路径：显式 --db > DOSSIER_DB_PATH > 包内 default_db_path()。

    与包内同源（`dossier.store.default_db_path`），避免审计脚本自己维护一份
    "仓库根在哪"的判断而与实际运行分叉。
    """
    if explicit:
        return Path(explicit)
    try:
        from mylocalai.dossier.store import default_db_path
        return Path(default_db_path())
    except Exception:  # noqa: BLE001  包不可导入时退回脚本相对路径
        return Path(__file__).resolve().parent.parent / "data" / "dossier.db"


# ---------------------------------------------------------------------------
# 编排：collect() 是唯一"读盘 + 调纯函数"的地方
# ---------------------------------------------------------------------------


def collect(db_path: Optional[Path] = None,
            now: Optional[str] = None) -> Dict[str, Any]:
    """跑一次完整审计，返回冻结结构的报告 dict（永不抛）。

    步骤：只读开库 → 数表 → 读 tool_runs → 反射四份清单 → 纯函数算对照 → 组装。
    任何一步失败都降级为 0 / 空清单，并在 notes 里写明原因。
    """
    target = Path(db_path) if db_path is not None else resolve_db_path()
    notes: List[str] = []
    generated_at = now or datetime.now().astimezone().isoformat(timespec="seconds")

    conn, open_reason = open_readonly(target)
    tables = list_tables(conn)
    if open_reason:
        notes.append(open_reason)
    if conn is not None and not tables:
        notes.append("库里没有任何表（schema 未建？按全 0 统计）")

    counts: Dict[str, Optional[int]] = {}
    for table, _label, _cap in ENTRY_TABLES:
        counts[table] = count_rows(conn, table)

    rows, runs_reason = read_tool_runs(conn)
    if runs_reason:
        notes.append(runs_reason)
    tool_runs = summarize_tool_runs(rows)
    entries = summarize_entries(counts)

    runtime_tools, handler_tools, runtime_reason = discover_runtime_tools()
    if runtime_reason:
        notes.append(runtime_reason)
    registerable, _modules, register_reason = discover_registerable_tools()
    if register_reason:
        notes.append(register_reason)
    routed, routed_reason = discover_routed_actions()
    if routed_reason:
        notes.append(routed_reason)
    cap_items, cap_actions, horizontal, cap_reason = discover_capabilities()
    if cap_reason:
        notes.append(cap_reason)

    wiring = diff_wiring(runtime_tools, routed, registerable, cap_actions,
                         horizontal, handler_tools)
    usage = mark_usage(wiring, tool_runs["tools"].keys(), routed)
    capabilities = {"total": len(cap_items), "items": cap_items}

    if tool_runs["total"] == 0 and not open_reason:
        notes.append("tool_runs 里没有任何调用记录：本机还没走过对话工具通道，"
                     "或该库是新建的（不能据此断言能力无用，见审计报告边界声明）")
    if wiring["registered_not_routed"]:
        notes.append("注册但不在路由枚举（自然语言路由到不了）："
                     + "、".join(wiring["registered_not_routed"]))
    if wiring["routed_not_registered"]:
        notes.append("在路由枚举但运行时未注册（调用必被拒）："
                     + "、".join(wiring["routed_not_registered"]))
    if wiring["registerable_not_wired"]:
        notes.append("模块可注册但运行时未接线（有代码、没进注册表）："
                     + "、".join(wiring["registerable_not_wired"]))
    if wiring["routed_without_handler"]:
        notes.append("在路由枚举但找不到可调用 handler："
                     + "、".join(wiring["routed_without_handler"]))

    db_info = {
        "path": str(target),
        "exists": target.exists(),
        "opened_readonly": conn is not None,
        "degraded": bool(open_reason),
        "reason": open_reason,
        "tables": tables,
        "generated_at": generated_at,
    }
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return build_report(db_info, tool_runs, entries, wiring, usage, capabilities, notes)


# ---------------------------------------------------------------------------
# CLI：薄封装（解析参数 → collect → 输出）
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit_capability_usage.py",
        description="能力使用率审计（只读）：工具调用统计 + 能力清单四方对照。")
    parser.add_argument("--db", default=None,
                        help="库路径（默认 DOSSIER_DB_PATH，再默认仓库 data/dossier.db）")
    parser.add_argument("--json", action="store_true",
                        help="输出机器可读 JSON（键集合由 SCHEMA_VERSION 冻结）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口：审计本身永不因数据问题失败，只有参数错误才非 0。"""
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  Windows 控制台/重定向兜底
        pass
    report = collect(Path(args.db) if args.db else None)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False))
    else:
        print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
