# -*- coding: utf-8 -*-
"""L4 参谋工具封装（§5.2）：insights —— 话术模式库 / 信源画像 / 本周简报（只读、零 LLM）。

三条设计约束（与 mylocalai.dossier.insights 的纯函数口径一致）：
  - 只读：permission="read"、cost="zero"、est_s=0.0，不建档（dossier_id 恒 None）；
    结构化 action 通道只放行 read 工具，本工具正是"按钮点一下看画像"的那类入口；
  - 零 LLM：聚合全部复用 dossier.insights 的纯函数（pattern_library / source_profiles
    / weekly_digest），本模块连 LLM 的 import 都没有；
  - digest 只放计数摘要：叙述层经 build_context 吃 digest，因此**域名明细不进 digest**
    （个人信源画像属于个人数据，叙述模型无需也不该看到）；完整数据只进 card 给 UI 渲染。

降级口径：空库是合法输出（"暂无数据"照常出卡，ok=True、degraded=False）；库不可用/
聚合抛异常时才 ok=False、degraded=True，digest 如实说明原因，**绝不把对话打崩**。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from mylocalai.assistant.registry import ToolRegistry, ValidationError

__all__ = ["KINDS", "kind_param", "register_all"]

#: 封闭枚举：三种画像视图（越界一律 ValidationError，绝不猜用户想看哪个）。
KINDS = ("patterns", "sources", "weekly")

#: 周报窗口天数（与 web 层 /api/insights/weekly 的缺省 days=7 同口径）。
WEEKLY_DAYS = 7

#: 各榜单在 digest 里最多列几条（叙述层只需要"高频是什么"，不要长清单）。
_DIGEST_TOP = 3


def kind_param(value: Any) -> str:
    """insights 的 kind 参数：封闭枚举校验（None/非字符串/越界值都抛 ValidationError）。

    必填而非缺省：三种视图的数据形状完全不同，替用户挑一个就是"猜"——路由层因此
    在拿不准时降级 clarify（§5.1 防护链同一哲学）。
    """
    if value is None:
        raise ValidationError("缺少参数 kind")
    if not isinstance(value, str):
        raise ValidationError("参数 kind 必须是字符串")
    kind = value.strip().lower()
    if kind not in KINDS:
        raise ValidationError("参数 kind 必须是 " + "|".join(KINDS))
    return kind


def _now_iso() -> str:
    """卡片 as_of：库内时间戳同源口径（store._now() 同格式同时区偏移）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _store_of(ctx: Any) -> Any:
    """档案库 store（只读聚合的唯一数据源）；缺服务/库未就绪时抛，由 handler 统一降级。"""
    dossiers = getattr(ctx, "dossiers", None)
    store = getattr(dossiers, "store", None) if dossiers is not None else None
    if store is None:
        raise RuntimeError("档案库不可用（未装配 DossierService）")
    return store


def _label(name: Any) -> str:
    """空标签显示成"未标注"（结论行的 label 理论上非空，坏数据时不显示空白占位）。"""
    text = str(name or "").strip()
    return text or "未标注"


def _counts_text(counter: Any) -> str:
    """{键: 计数} → "高风险谣言 1、疑似谣言 1"（空 dict → "无"）。"""
    if not isinstance(counter, dict) or not counter:
        return "无"
    return "、".join(f"{_label(k)} {int(v)}" for k, v in counter.items())


def _patterns_digest(patterns: Dict[str, Any]) -> str:
    """话术模式库的叙述要点：只给计数与话术中文名，不含任何域名。"""
    lines = [f"[话术模式库] 有结论档案 {int(patterns.get('total_dossiers', 0))} 个，"
             f"其中发生过话术特征 {int(patterns.get('with_markers', 0))} 个"]
    lines.append(f"- 结论分布：{_counts_text(patterns.get('by_label'))}")
    for title, key in (("风险话术", "risk_patterns"), ("可信度线索", "credibility_patterns")):
        rows = [r for r in (patterns.get(key) or []) if isinstance(r, dict)]
        if not rows:
            lines.append(f"- {title}：暂无（档案里还没有命中过该类特征）")
            continue
        top = "、".join(
            f"{_label(r.get('label') or r.get('key'))}（{int(r.get('dossiers', 0))} 个档案，"
            f"占 {float(r.get('share', 0.0)) * 100:.0f}%）" for r in rows[:_DIGEST_TOP])
        lines.append(f"- {title}：{top}")
    pairs = [p for p in (patterns.get("common_pairs") or []) if isinstance(p, dict)]
    if pairs:
        top_pairs = "、".join(
            f"{_label((p.get('labels') or ['', ''])[0])}+{_label((p.get('labels') or ['', ''])[1])}"
            f"（{int(p.get('dossiers', 0))} 个档案）" for p in pairs[:_DIGEST_TOP])
        lines.append(f"- 同现组合：{top_pairs}")
    return "\n".join(lines)


def _sources_digest(profiles: List[Dict[str, Any]]) -> str:
    """信源画像的叙述要点：只给域名个数/条目计数/层级分布，**不列域名**。

    为什么连域名都不给：画像里的域名是用户本人查过的信源史，属于个人数据面；
    叙述只负责说"你的库里有几个信源、几成达标"，展开细节交给卡片。
    """
    rows = [p for p in (profiles or []) if isinstance(p, dict)]
    if not rows:
        return "[信源画像] 暂无数据（档案里还没有可归因到域名的证据条目）"
    qualified = [p for p in rows if p.get("qualified")]
    tiers: Dict[str, int] = {}
    refute_entries = support_entries = 0
    by_refuted: Dict[int, int] = {}
    for p in rows:
        tier = str(p.get("tier") or "").strip()
        tiers[tier or "层级未知"] = tiers.get(tier or "层级未知", 0) + 1
        refute_entries += int(p.get("refute_entries", 0) or 0)
        support_entries += int(p.get("support_entries", 0) or 0)
        n = int(p.get("refuted_dossiers", 0) or 0)
        by_refuted[n] = by_refuted.get(n, 0) + 1
    lines = [f"[信源画像] 共 {len(rows)} 个信源域名；达标信源 {len(qualified)} 个"
             f"（反驳过 ≥2 个高风险/疑似档案）"]
    lines.append("- 反驳档案数分布：" + "、".join(
        f"{n} 个档案 {c} 个信源" for n, c in sorted(by_refuted.items(), reverse=True)))
    lines.append(f"- 证据条目：反驳 {refute_entries} 条、支持 {support_entries} 条")
    lines.append(f"- 层级分布：{_counts_text(tiers)}")
    return "\n".join(lines)


def _weekly_digest_text(digest: Dict[str, Any]) -> str:
    """周报的叙述要点：窗口 + 各域计数（域名明细同样留 card，不进叙述）。"""
    period = digest.get("period") if isinstance(digest.get("period"), dict) else {}
    start = str(period.get("start") or "")
    end = str(period.get("end") or "")
    days_n = period.get("days", WEEKLY_DAYS)
    window = f"{start} → {end}" if start and end else "（窗口未知）"
    lines = [f"[本周简报] 窗口 {window}（{days_n} 天）"]

    dossiers = digest.get("dossiers") or {}
    lines.append(f"- 新增档案 {int(dossiers.get('new_count', 0) or 0)} 个"
                 f"（{_counts_text(dossiers.get('by_input_kind'))}）")
    conclusions = digest.get("conclusions") or {}
    lines.append(f"- 结论 {int(conclusions.get('count', 0) or 0)} 条："
                 f"{_counts_text(conclusions.get('by_label'))}")

    runs = digest.get("runs") or {}
    lines.append(f"- 分析运行 {int(runs.get('count', 0) or 0)} 次"
                 f"（降级 {int(runs.get('degraded_count', 0) or 0)} 次），"
                 f"请求 {int(runs.get('requests', 0) or 0)} 次 / token "
                 f"{int(runs.get('tokens', 0) or 0)}")
    tool_runs = digest.get("tool_runs") or {}
    lines.append(f"- 工具运行 {int(tool_runs.get('count', 0) or 0)} 次，"
                 f"请求 {int(tool_runs.get('requests', 0) or 0)} 次 / token "
                 f"{int(tool_runs.get('tokens', 0) or 0)}")

    tops = [p for p in ((digest.get("patterns") or {}).get("top") or [])
            if isinstance(p, dict)]
    if tops:
        lines.append("- 话术高频：" + "、".join(
            f"{_label(p.get('label') or p.get('key'))}（{int(p.get('dossiers', 0))} 个档案）"
            for p in tops[:_DIGEST_TOP]))
    else:
        lines.append("- 话术高频：窗口内没有新的命中的话术特征")

    refutes = [p for p in ((digest.get("sources") or {}).get("top_refute") or [])
               if isinstance(p, dict)]
    if refutes:
        lines.append(f"- 反驳信源 {len(refutes)} 个，最多的一条信源反驳 "
                     f"{int(refutes[0].get('refute_entries', 0) or 0)} 条")
    else:
        lines.append("- 反驳信源：窗口内没有新的反驳条目")

    heat = digest.get("heat") or {}
    lines.append(f"- 热度快照 {int(heat.get('snapshot_count', 0) or 0)} 个；"
                 f"指纹命中 {int((digest.get('fingerprints') or {}).get('count', 0) or 0)} 次")
    return "\n".join(lines)


def _card(kind: str, as_of: str, payload_key: str, data: Any) -> Dict[str, Any]:
    """卡片形状：{card_type, kind, as_of, patterns|profiles|digest}。

    card_type 是前端渲染器分发的键（loop._CARD_TYPE 同值），kind 用于选子视图。
    """
    return {"card_type": "insights", "kind": kind, "as_of": as_of, payload_key: data}


def _degraded(reason: str) -> Dict[str, Any]:
    """降级出口：ok=False + degraded=True，digest 如实说明（不编空卡片冒充成功）。"""
    return {"ok": False, "card": None,
            "digest": f"个人信息环境画像暂时不可用：{reason}。"
                      "这不是空结果，只是本次没能读到本机档案库；请稍后重试。",
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None,
            "degraded": True, "not_saved": False}


def _insights(ctx: Any, kind: str) -> Dict[str, Any]:
    """insights：按 kind 产出三种只读画像之一（零 LLM、零落库、永不抛）。"""
    as_of = _now_iso()
    try:
        # 惰性 import（先例：loop._qualified_profile）：测试可 mock 模块属性模拟聚合故障。
        from mylocalai.dossier.insights import (pattern_library, source_profiles,
                                                weekly_digest)
        store = _store_of(ctx)
        if kind == "patterns":
            data = pattern_library(store)
            card, digest = _card(kind, as_of, "patterns", data), _patterns_digest(data)
        elif kind == "sources":
            data = source_profiles(store)
            card, digest = _card(kind, as_of, "profiles", data), _sources_digest(data)
        else:   # kind_param 已限定封闭枚举，走到这里只可能是 weekly
            data = weekly_digest(store, now=as_of, days=WEEKLY_DAYS)
            card, digest = _card(kind, as_of, "digest", data), _weekly_digest_text(data)
    except Exception as exc:  # noqa: BLE001  参谋是增益面：任何故障降级，绝不挡对话
        return _degraded(f"{type(exc).__name__}: {exc}")
    return {"ok": True, "card": card, "digest": digest,
            "cost": {"requests": 0, "tokens": 0}, "dossier_id": None,
            "degraded": False,
            # 只读聚合不产生待保存产物：not_saved 是"写失败"的语义位，这里恒 False
            "not_saved": False, "kind": kind}


def register_all(registry: ToolRegistry, services: Any = None) -> None:
    """注册 insights 工具（services 形参约定同 tools_verify.register_all）。

    只读零 LLM：permission="read" 让结构化 action 通道放行，cost="zero"/est_s=0.0
    让成本徽章与 tool_start 的预估如实（本地 SQLite 聚合，毫秒级）。
    """
    registry.register("insights", _insights, params=["kind"],
                      permission="read", cost="zero", est_s=0.0,
                      description="看个人档案库的画像与周报（话术模式/信源画像/本周简报，零 LLM 只读）",
                      validators={"kind": kind_param})
