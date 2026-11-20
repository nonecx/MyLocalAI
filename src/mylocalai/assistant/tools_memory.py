# -*- coding: utf-8 -*-
"""记忆类工具封装（§5.2）：list_dossiers / get_dossier / export_card。

全部只读（export_card 只生成文本，不写文件）；零 API 成本，秒答车道。
数据一律经 DossierService/DossierStore 读取，与档案库单一事实源保持一致
（test_tools_memory 有与 store 直读的对拍用例）。

缺章口径：档案的章节（证据本/热度快照/观点光谱/深读）是懒加载的——没生成过的
章节返回 {"chapter_status": "absent"}，**不编造**（铁律：不为有输出而编）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mylocalai.assistant.registry import ToolRegistry, ValidationError, id_param, \
    limit_param, optional_query
from mylocalai.assistant.tools_guard import dossier_visible

__all__ = ["register_all", "format_param"]

_EVIDENCE_PREVIEW = 5   # get_dossier 返回证据本前 5 条 + 计数（§2.2 展开层）


def format_param(value: Any) -> str:
    """export_card 的 format：M2 仅支持 md（pdf/docx 与写文件留 M3-6，见 §6 M3-6）。"""
    fmt = "md" if value is None else value
    if fmt != "md":
        raise ValidationError("参数 format 目前仅支持 md")
    return fmt


def _conclusion_card(latest: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """把库里的结论行还原成结论卡（字段原样取自存储，不重算、不装饰）。"""
    if not latest:
        return None
    return {
        "version": latest.get("version"),
        "label": str(latest.get("label", "")),
        "risk_score": latest.get("risk_score"),
        "confidence": str(latest.get("confidence", "")),
        "evidence_sufficiency": latest.get("evidence_sufficiency") or {},
        "as_of": str(latest.get("as_of", "")),
        "recheck_conditions": latest.get("recheck_conditions") or [],
        "verdict_basis": "verdict.assess() 纯函数输出，大模型不参与打分",
    }


def _list_dossiers(ctx: Any, query: str = "", limit: int = 20) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    try:
        # profile_id：只列当前会话所属工作区的档案（v6 多 Profile 分家）；
        # ctx 未注入（stub/旧装配）时 None = 不过滤，与加参数前逐位一致。
        rows = ctx.dossiers.store.list_dossiers(
            str(query or ""), int(limit or 20),
            profile_id=getattr(ctx, "profile_id", None)) or []
    except Exception as exc:  # noqa: BLE001  库读失败诚实降级，不崩
        return {"ok": False, "card": None,
                "digest": f"档案库读取失败：{type(exc).__name__}",
                "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": True,
                "items": [], "count": 0}
    items = [{"id": int(r.get("id", 0)), "title": str(r.get("title", "")),
              "input_kind": str(r.get("input_kind", "")),
              "updated_at": str(r.get("updated_at", ""))} for r in rows]
    lines = [f"[档案列表] 共 {len(items)} 个" + (f"（筛选：{query}）" if query else "")]
    for it in items:
        lines.append(f"· #{it['id']}《{it['title'] or '未命名'}》 更新于 {it['updated_at']}")
    return {"ok": True, "card": None, "digest": "\n".join(lines),
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": False,
            "items": items, "count": len(items)}


def _get_dossier(ctx: Any, id: int) -> Dict[str, Any]:  # noqa: A002  与路由参数名一致
    store = ctx.dossiers.store
    visible, store_error = dossier_visible(ctx, id)
    if store_error:
        return {"ok": False, "card": None, "digest": f"档案库读取失败：{store_error}",
                "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": True}
    if not visible:
        # 不存在或不属于当前工作区：同一种回答（tools_guard 契约）
        return {"ok": False, "card": None, "digest": f"档案 #{id} 不存在。",
                "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": False,
                "not_found": True}

    try:
        row = store.get_dossier(int(id))
    except Exception as exc:  # noqa: BLE001
        row = None
        store_error = f"{type(exc).__name__}: {exc}"
    else:
        store_error = ""
    if store_error:
        return {"ok": False, "card": None, "digest": f"档案库读取失败：{store_error}",
                "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": True}
    if not row:
        return {"ok": False, "card": None, "digest": f"档案 #{id} 不存在。",
                "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": False,
                "not_found": True}

    try:
        conclusions = store.conclusions(int(id))
        evidence = store.evidence(int(id))
        snapshots = store.snapshots(int(id))
        opinions = store.opinions(int(id))
        deep_reads = store.deep_reads(int(id))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "card": None,
                "digest": f"档案 #{id} 章节读取失败：{type(exc).__name__}: {exc}",
                "cost": {"requests": 0, "tokens": 0}, "dossier_id": int(id), "degraded": True}

    latest = conclusions[-1] if conclusions else None
    card = _conclusion_card(latest)

    # 章节状态：没生成过的章节如实 absent（懒加载是常态，不编造）
    chapters: Dict[str, Any] = {
        "conclusions": {"count": len(conclusions),
                        "versions": [c.get("version") for c in conclusions]},
        "evidence": {"total": len(evidence),
                     "entries": [{k: e.get(k) for k in
                                  ("url", "domain", "domain_tier", "source_kind",
                                   "stance", "title", "snippet", "batch")}
                                 for e in evidence[:_EVIDENCE_PREVIEW]]},
        "attention": ({"count": len(snapshots),
                       "snapshots": snapshots[-3:]} if snapshots
                      else {"chapter_status": "absent"}),
        "opinions": ({"count": len(opinions), "latest": opinions[-1]} if opinions
                     else {"chapter_status": "absent"}),
        "deep_read": ({"count": len(deep_reads), "latest": deep_reads[-1]} if deep_reads
                      else {"chapter_status": "absent"}),
    }

    lines = [f"[档案 #{id}]《{row.get('title', '')}》 建于 {row.get('created_at', '')}"]
    if latest:
        score = latest.get("risk_score")
        lines.append(f"- 最新结论 v{latest.get('version')}：{latest.get('label', '')}"
                     f"（风险分 {'暂无' if score is None else score}，置信度 {latest.get('confidence', '')}，"
                     f"as_of {latest.get('as_of', '') or '未知'}）")
        suff = latest.get("evidence_sufficiency") or {}
        if suff:
            lines.append(f"- 证据充分度：官方 {suff.get('official', 0)} / 非官方 "
                         f"{suff.get('unofficial', 0)} / 未知 {suff.get('unknown', 0)}"
                         f"，共 {suff.get('total', 0)} 条")
        rc = latest.get("recheck_conditions") or []
        if rc:
            lines.append("- 复审条件：" + "；".join(str(x) for x in rc))
    else:
        lines.append("- 尚无结论（档案还没有跑过核查）")
    lines.append(f"- 证据本共 {len(evidence)} 条（下方展示前 {min(len(evidence), _EVIDENCE_PREVIEW)} 条）")
    absent = [name for name, ch in chapters.items() if isinstance(ch, dict)
              and ch.get("chapter_status") == "absent"]
    if absent:
        lines.append("- 未生成章节：" + "、".join(absent) + "（按需生成，不预生成）")
    return {"ok": True, "card": card, "digest": "\n".join(lines),
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": int(id), "degraded": False,
            "title": str(row.get("title", "")), "input_text": str(row.get("input_text", "")),
            "chapters": chapters, "conclusion": card}


def _export_card(ctx: Any, id: int, format: str = "md") -> Dict[str, Any]:  # noqa: A002
    """export_card：生成 md 文本（含卡片字段 + 免责语），M2 不写文件。

    文件落盘（Markdown 导出为 .md 文件）按工作指导 §6 M3-6 另做；M2 的返回值
    （content 字段）就是完整可复制的 md 文本，web/MCP 层自行决定呈现方式。
    """
    if str(format) != "md":
        # 注册表校验器已拦；这里再兜一层（直接调用 handler 的路径）
        return {"ok": False, "card": None, "digest": "M2 仅支持 md 格式导出。",
                "cost": {"requests": 0, "tokens": 0}, "dossier_id": None, "degraded": False}
    got = _get_dossier(ctx, id)
    if not got.get("ok"):
        got["digest"] = f"档案 #{id} 无法导出：{got.get('digest', '')}"
        return got
    latest = got.get("conclusion")
    if not latest:
        return {"ok": False, "card": None,
                "digest": f"档案 #{id} 还没有结论，先核查一次再导出。",
                "cost": {"requests": 0, "tokens": 0}, "dossier_id": int(id), "degraded": False}

    suff = latest.get("evidence_sufficiency") or {}
    rc = latest.get("recheck_conditions") or []
    score = latest.get("risk_score")
    md_lines: List[str] = [
        f"# 核查结论卡片 · 档案 #{id}",
        "",
        f"- 标签：{latest.get('label', '')}",
        f"- 风险分：{'暂无' if score is None else score}/100（兼容层口径）",
        f"- 置信度：{latest.get('confidence', '')}",
        f"- 证据充分度：官方 {suff.get('official', 0)} / 非官方 {suff.get('unofficial', 0)}"
        f" / 未知 {suff.get('unknown', 0)}，共 {suff.get('total', 0)} 条",
        f"- 证据截至（as_of）：{latest.get('as_of', '') or '未知'}",
        f"- 复审条件：{'；'.join(str(x) for x in rc) if rc else '（无）'}",
        f"- 口径：{latest.get('verdict_basis', 'verdict.assess() 纯函数输出，大模型不参与打分')}",
        "",
        "## 免责声明",
        "",
        "本卡片由确定性引擎从证据本派生（大模型不参与打分），只描述证据支持什么、"
        "不足以支持什么，不构成真假判定，也不给\"该不该转发\"的行动建议。"
        "证据会更新，请按复审条件择时再看。",
        "",
    ]
    content = "\n".join(md_lines)
    return {"ok": True, "card": latest, "digest": f"已生成档案 #{id} 的 md 导出文本"
                                                  f"（含 as_of 与免责语；文件落盘 M3-6 提供）",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": int(id), "degraded": False,
            "format": "md", "content": content}


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    """注册记忆类工具（全部只读；export_card 的文件落盘留 M3-6）。"""
    registry.register("list_dossiers", _list_dossiers, params=["query", "limit"],
                      permission="read", cost="zero", est_s=0.0,
                      description="列出查过的档案（可按关键词筛选）",
                      validators={"query": optional_query, "limit": limit_param})
    registry.register("get_dossier", _get_dossier, params=["id"],
                      permission="read", cost="zero", est_s=0.0,
                      description="回看某份档案：最新结论 + 证据本前 5 条 + 各章节状态",
                      validators={"id": id_param})
    registry.register("export_card", _export_card, params=["id", "format"],
                      permission="read", cost="zero", est_s=0.0,
                      description="把某份档案的结论卡导出为 md 文本（含免责语）",
                      validators={"id": id_param, "format": format_param})
