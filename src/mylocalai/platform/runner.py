# -*- coding: utf-8 -*-
"""巡检编排（platform/runner.py）：发现 → 去重 → 初筛 → 深查 → 风险分级 → 落库。

两阶段成本控制（计划 §5）：
  阶段一（轻，每候选）：多源发现 → URL/指纹去重 → 话题热度估计（零额外检索）
    → 话术特征本地初扫（零 LLM）→ 不过门槛只登记不深查；
  阶段二（重，预算封顶）：过门槛的候选跑 RumorAgent 全链路查验 → 经
    DossierService 建档（input_kind='topic'）→ verdict 回填信息流条目。

隐私与降级（fail-closed，与助手核心同一文化）：
  - 外部检索/抓取未授权（external_allowed("public-search")）→ 整次巡检跳过
    （status=skipped + 明确原因），不外发、不落库、不静默；
  - 结果保存未启用（result_allowed）→ 条目只进当次返回、不写库；档案由
    DossierService 自行门控（start_dossier 返 None）；
  - LLM 不可用/查验失败 → 条目保留、status=verify_failed，风险只由热度与
    来源推导，**不编 verdict**；
  - 预算耗尽/取消 → 停止开新深查，已完成条目保留，scan 记 partial/cancelled。

依赖方向：platform → {retrieval, attention, rumor, dossier, core}；引擎与档案
服务一律经构造函数注入（测试可换 stub），本模块自身不 import web/assistant。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from mylocalai.core.privacy import PrivacyPreferences, load_preferences
from mylocalai.dossier.service import DossierService
from mylocalai.platform.candidates import (FeedCandidate, dedup_candidates,
                                           near_duplicate)
from mylocalai.platform.heat import estimate_heat, relative_change
from mylocalai.platform.risk import assess_spread_risk
from mylocalai.platform.sources import SEARCH_PROVIDER_ID, build_providers
from mylocalai.platform.store import PlatformStore

__all__ = ["PlatformRunner", "AGENT_MAX_CHARS"]

#: 深查喂给 RumorAgent 的文本上限（标题+摘要，4000 足够，别烧配额）。
AGENT_MAX_CHARS = 4000


class PlatformRunner:
    """一次巡检的编排门面。所有重依赖可注入；缺省惰性装配真实引擎。"""

    def __init__(self, *, store: Optional[PlatformStore] = None,
                 rules: Optional[Dict[str, Any]] = None,
                 privacy: Optional[PrivacyPreferences] = None,
                 dossiers: Optional[DossierService] = None,
                 providers: Optional[List[Any]] = None,
                 agent: Any = None) -> None:
        from mylocalai.platform.rules import load_rules
        self.store = store or PlatformStore()
        loaded, self.rule_warnings = load_rules()
        self.rules = rules if rules is not None else loaded
        self.privacy = privacy or load_preferences()
        self.dossiers = dossiers or DossierService(privacy=self.privacy)
        self._agent = agent
        if providers is not None:
            self.providers = list(providers)
        else:
            self.providers, w = build_providers(self.rules)
            self.rule_warnings.extend(w)

    # ---- 引擎惰性装配（与 web 层 get_rumor_agent 同口径）----

    def _get_agent(self) -> Any:
        if self._agent is None:
            from mylocalai.rumor import RumorAgent
            self._agent = RumorAgent(
                max_chars=AGENT_MAX_CHARS,
                funcs={
                    "emotion": _lazy("mylocalai.classic.language", "analyze_emotion"),
                    "aggression": _lazy("mylocalai.classic.language", "analyze_aggression"),
                    "logic": _lazy("mylocalai.classic.logic", "analyze_logic"),
                },
            )
        return self._agent

    # ---- 监控词解析 ----

    def _resolve_queries(self, explicit: Optional[List[str]]) -> List[Tuple[int, str]]:
        """显式查询词优先；否则取库内启用监控词；库空时用规则段做首次种子。"""
        cap = max(1, int(self.rules.get("max_queries_per_scan", 5)))
        if explicit:
            out: List[Tuple[int, str]] = []
            seen = set()
            for q in explicit:
                q = str(q or "").strip()
                if not q or q in seen:
                    continue
                seen.add(q)
                out.append((self.store.ensure_watch_query(q), q))
                if len(out) >= cap:
                    break
            return out
        rows = self.store.list_watch_queries(enabled_only=True)
        if not rows:
            for seed in self.rules.get("watch_queries") or []:
                if isinstance(seed, dict) and seed.get("enabled", True):
                    self.store.ensure_watch_query(str(seed.get("query") or ""))
                elif isinstance(seed, str):
                    self.store.ensure_watch_query(seed)
            rows = self.store.list_watch_queries(enabled_only=True)
        return [(int(r["id"]), str(r["query"])) for r in rows[:cap]]

    # ---- 主流程 ----

    def scan(self, *, queries: Optional[List[str]] = None,
             cancel_check: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
        """跑一次巡检。**绝不抛异常**：任何失败都落成 scan 状态与 summary 字段。"""
        started = time.perf_counter()
        summary: Dict[str, Any] = {
            "scan_id": None, "status": "failed", "queries": [],
            "counters": {"queries": 0, "candidates": 0, "verified": 0,
                         "deduped": 0, "degraded": 0},
            "entries": [], "error": "", "warnings": list(self.rule_warnings),
        }
        scan_id: Optional[int] = None
        try:
            scan_id = self.store.start_scan_run()
            summary["scan_id"] = scan_id
        except Exception as exc:  # noqa: BLE001  连 scan 记录都写不了也只影响记账
            summary["warnings"].append(f"scan_runs 记录失败：{type(exc).__name__}")

        persist = bool(self.privacy.result_allowed())
        if not persist:
            summary["warnings"].append("核查结果保存未启用：本次巡检结果只进当次返回，不落库")

        # 外部授权门：搜索与 RSS 抓取都算外发，未授权整次跳过（fail-closed）
        if not self.privacy.external_allowed(SEARCH_PROVIDER_ID):
            summary["status"] = "skipped"
            summary["error"] = "external_processing_not_authorized"
            self._finish(scan_id, summary, started)
            return summary

        resolved = self._resolve_queries(queries)
        if not resolved:
            summary["status"] = "completed"
            summary["warnings"].append("没有启用中的监控词（可在平台页添加）")
            self._finish(scan_id, summary, started)
            return summary
        summary["counters"]["queries"] = len(resolved)

        # 深查总预算：整次巡检一个 deadline scope（下游 LLM 调用经 deadline_at()
        # 自动继承，与 JUDGE_DEADLINE_S/CHAT_TURN_DEADLINE_S 同一机制）；<=0 不限。
        state = {"verify_left": max(0, int(self.rules.get("deep_verify", {})
                                           .get("max_per_scan", 3)))}
        cancelled = False
        scope_s = float(self.rules.get("deep_verify", {}).get("deadline_s", 120) or 0)
        from mylocalai.llm import llm_cache
        with llm_cache.deadline_scope(scope_s if scope_s > 0 else None):
            for query_id, query in resolved:
                if _cancelled(cancel_check):
                    cancelled = True
                    break
                try:
                    if self._scan_query(query_id, query, summary, persist, state,
                                        cancel_check):
                        cancelled = True     # 候选循环里取消：已完成的条目保留
                        break
                except Exception as exc:  # noqa: BLE001  单条监控词失败不拖垮整次巡检
                    summary["counters"]["degraded"] += 1
                    summary["warnings"].append(f"监控词「{query[:40]}」处理失败："
                                               f"{type(exc).__name__}: {exc}")

        if cancelled:
            summary["status"] = "cancelled"
            summary["error"] = "已取消（用户中断）"
        elif summary["counters"]["degraded"] and not summary["entries"]:
            summary["status"] = "partial"
        else:
            summary["status"] = "completed"
        self._finish(scan_id, summary, started)
        return summary

    # ---- 单监控词 ----

    def _scan_query(self, query_id: int, query: str, summary: Dict[str, Any],
                    persist: bool, state: Dict[str, int],
                    cancel_check: Optional[Callable[[], bool]]) -> bool:
        """处理一条监控词。返回 True 表示处理途中被取消（已完成的条目已保留）。"""
        k = max(1, int(self.rules.get("max_candidates_per_query", 8)))
        raw: List[FeedCandidate] = []
        for provider in self.providers:
            res = provider.discover(query, k)
            if res.degraded:
                summary["counters"]["degraded"] += 1
                summary["warnings"].append(
                    f"源 {getattr(provider, 'name', '?')} 降级：{res.error or '未知原因'}")
            raw.extend(res.candidates or [])
        batch = dedup_candidates(raw)
        # 批内指纹去重：同一事件多家转载只保留第一条（检索排序优先）
        kept: List[FeedCandidate] = []
        threshold = float(self.rules.get("dedup", {}).get("fingerprint_threshold", 0.72))
        for cand in batch:
            if not near_duplicate(cand, kept, threshold):
                kept.append(cand)

        # 话题热度：从本批发现结果估计（零额外检索）；RSS-only 时 x 即批大小
        heat = estimate_heat(query, [c.to_dict() for c in kept])
        e_score = float(heat.get("e") or 0.0) if not heat.get("error") else None
        e_prev = self.store.latest_e_for_query(query_id) if persist else None
        growth = relative_change(e_score, e_prev) if e_score is not None else 0.0

        breadth = _breadth_stats(kept)
        summary["queries"].append({"id": query_id, "query": query, "e": e_score,
                                   "quality": heat.get("quality", "missing"),
                                   "candidates": len(kept)})

        screen_e = float(self.rules.get("screen", {}).get("heat_screen_e", 40))
        for cand in kept:
            if _cancelled(cancel_check):
                return True
            # 巡检中途监控词可能被删（其下条目会级联消失）：诚实跳过其余候选，
            # 记 degraded——不让外键冲突伪装成"去重"（冒烟测试抓到的真实 race）。
            if persist and not self.store.watch_query_exists(query_id):
                summary["counters"]["degraded"] += 1
                summary["warnings"].append(
                    f"监控词「{query[:40]}」在巡检中途被删除，其余候选已跳过")
                return False
            self._process_candidate(query_id, cand, summary, persist, e_score,
                                    e_prev, growth, breadth, screen_e, state)
        return False

    def _process_candidate(self, query_id: int, cand: FeedCandidate,
                           summary: Dict[str, Any], persist: bool,
                           e_score: Optional[float], e_prev: Optional[float],
                           growth: float, breadth: Dict[str, int], screen_e: float,
                           state: Dict[str, int]) -> None:
        entry_id: Optional[int] = None
        if persist:
            entry_id = self.store.add_feed_entry(
                query_id=query_id, title=cand.title, url=cand.url,
                snippet=cand.snippet, domain=cand.domain, domain_tier=cand.domain_tier,
                discovered_at=cand.published_at, e_score=e_score)
            if entry_id is None:
                summary["counters"]["deduped"] += 1     # URL 已在库：跨巡检去重
                return
        summary["counters"]["candidates"] += 1

        # 阶段一：本地初扫（零 LLM）+ 深查门槛
        from mylocalai.rumor.patterns import scan_markers
        markers = scan_markers(f"{cand.title} {cand.snippet}")
        hot = e_score is not None and e_score >= screen_e
        needs_deep = hot or bool(markers.get("risk_hits"))

        rumor: Optional[Dict[str, Any]] = None
        status = "screened"
        if needs_deep and state["verify_left"] > 0 and _deadline_left() > 0:
            status, rumor = self._deep_verify(cand, summary, state)

        risk = assess_spread_risk(
            heat=e_score if e_score is not None else 0.0,
            growth=growth, breadth=breadth,
            rumor=rumor, rules=self.rules)

        entry = {"query_id": query_id, "title": cand.title, "url": cand.url,
                 "domain": cand.domain, "domain_tier": cand.domain_tier,
                 "discovered_at": cand.published_at, "e_score": e_score,
                 "e_prev": e_prev, "risk_level": risk["level"],
                 "risk_reasons": risk["reasons"], "status": status,
                 "verdict_label": (rumor or {}).get("label", ""),
                 "risk_score": (rumor or {}).get("risk_score"),
                 "dossier_id": None, "markers": markers.get("marker_points", 0)}
        if persist and entry_id is not None:
            self.store.update_feed_entry(
                entry_id, status=status, risk_level=risk["level"],
                risk_reasons=risk["reasons"],
                verdict_label=entry["verdict_label"],
                risk_score=entry["risk_score"], e_prev=e_prev)
            entry["id"] = entry_id
        summary["entries"].append(entry)

    def _deep_verify(self, cand: FeedCandidate, summary: Dict[str, Any],
                     state: Dict[str, int]) -> Tuple[str, Optional[Dict[str, Any]]]:
        """阶段二：RumorAgent 全链路 + 建档。失败降级不抛，不编 verdict。"""
        state["verify_left"] = max(0, state["verify_left"] - 1)   # 预算先扣后跑
        text = f"{cand.title}\n{cand.snippet}".strip()[:AGENT_MAX_CHARS]
        t0 = time.perf_counter()
        try:
            result = self._get_agent().analyze(text)
        except Exception as exc:  # noqa: BLE001  引擎异常 → 候选保留、状态如实
            summary["counters"]["degraded"] += 1
            summary["warnings"].append(
                f"深查失败（{cand.title[:30]}…）：{type(exc).__name__}: {exc}")
            return "verify_failed", None
        if not isinstance(result, dict) or "label" not in result:
            summary["counters"]["degraded"] += 1
            return "verify_failed", None
        if result.get("degraded_components") or result.get("agent_error"):
            summary["counters"]["degraded"] += 1

        dossier_id = None
        try:
            if self.privacy.result_allowed():
                dossier_id = self.dossiers.start_dossier(
                    text, title=cand.title[:24] or "平台巡检", input_kind="topic",
                    claim_excerpt=cand.title[:200])
                if dossier_id is not None:
                    self.dossiers.record_analysis(
                        dossier_id, result, kind="platform_verify",
                        duration_s=time.perf_counter() - t0)
                    heat_detail = _heat_detail_for_dossier(result)
                    if heat_detail:
                        self.dossiers.record_heat(dossier_id, heat_detail)
        except Exception as exc:  # noqa: BLE001  建档失败不影响结论呈现
            summary["counters"]["degraded"] += 1
            summary["warnings"].append(f"建档失败：{type(exc).__name__}: {exc}")
            dossier_id = None

        summary["counters"]["verified"] += 1
        return "verified", {"label": str(result.get("label") or ""),
                            "risk_score": result.get("risk_score")}

    def _finish(self, scan_id: Optional[int], summary: Dict[str, Any],
                started: float) -> None:
        """收尾：scan_runs 落账 + 耗时。记账失败只留 warning，不影响返回。"""
        if scan_id is None:
            return
        try:
            self.store.finish_scan_run(
                scan_id, summary["status"],
                queries=summary["counters"]["queries"],
                candidates=summary["counters"]["candidates"],
                verified=summary["counters"]["verified"],
                degraded=summary["counters"]["degraded"],
                error=summary["error"])
        except Exception as exc:  # noqa: BLE001
            summary["warnings"].append(f"scan_runs 收尾失败：{type(exc).__name__}")


# ---- 模块级纯助手 ----


def _lazy(module_path: str, attr: str) -> Callable[..., Any]:
    """延迟 import 的组件函数（装配时才拉 classic，平台包顶层不 import 引擎）。"""
    def _call(*args: Any, **kwargs: Any) -> Any:
        import importlib
        mod = importlib.import_module(module_path)
        return getattr(mod, attr)(*args, **kwargs)
    return _call


def _cancelled(cancel_check: Optional[Callable[[], bool]]) -> bool:
    try:
        return bool(cancel_check()) if cancel_check is not None else False
    except Exception:  # noqa: BLE001  探测失败不误杀巡检
        return False


def _deadline_left() -> float:
    """深查总预算剩余秒数（llm_cache 单调钟口径）。未设预算返回 inf（不限）；
    预算面不可用返回 0（保守：不开新深查）。"""
    try:
        from mylocalai.llm import llm_cache
        left = llm_cache.remaining_seconds()
        return float("inf") if left is None else max(0.0, float(left))
    except Exception:  # noqa: BLE001  预算面不可用 → 保守不开新深查
        return 0.0


def _breadth_stats(cands: List[FeedCandidate]) -> Dict[str, int]:
    """覆盖面统计：独立条目数 + 官方/权威媒体档计数（risk 的 breadth 因素）。"""
    from mylocalai.retrieval.sources import OFFICIAL_TIERS, TIER_MEDIA
    official = sum(1 for c in cands if c.domain_tier in OFFICIAL_TIERS)
    media = sum(1 for c in cands if c.domain_tier == TIER_MEDIA)
    return {"total": len(cands), "official": official, "reliable": official + media}


def _heat_detail_for_dossier(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 agent 结果里取热度细节（detail 形状含 x/y/z/score）；没有返回 None。"""
    detail = result.get("attention_detail") or result.get("heat_detail")
    return detail if isinstance(detail, dict) and "score" in detail else None
