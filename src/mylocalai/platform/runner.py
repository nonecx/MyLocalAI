# -*- coding: utf-8 -*-
"""巡检编排（platform/runner.py）：发现 → 去重 → 初筛 → 深查 → 风险分级 → 落库 → 报告。

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
领域线程（platform.domains：监控词 → 领域判定与检索提示）同样可注入、缺省
惰性 import；模块缺失/解析失败 → 无领域模式（domain=""）继续巡检，不静默。

报告阶段（platform.report：巡检收尾后跑一次，契约冻结的三段式）：
   status 定稿后、scan_runs 落账前调用 PatrolReport.build()——纯代码汇总
   （计数/分布/环比，零 LLM）+ LLM 一次调用（只做语言组织与逻辑判断）+ 代码
   校验器（安全闸/截断/白名单）。报告是增益面：技能不可用/LLM 失败/落库
   失败都只落 warning 与 payload 里的 llm_status/gaps，scan 状态与返回绝不
   被拖累（skipped 与"无监控词"两条早退路径没有可汇总的一轮，不产报告）。
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

#: domains 注入点"未设置"哨兵（None 是合法注入值=显式不要领域模式，
#: 用哨兵区分"没传"与"传了 None"）。
_DOMAINS_UNSET = object()

#: report 注入点"未设置"哨兵（同上：None = 显式不要报告阶段，
#: 哨兵区分"没传"与"传了 None"——没传才走惰性装配）。
_REPORT_UNSET = object()


class PlatformRunner:
    """一次巡检的编排门面。所有重依赖可注入；缺省惰性装配真实引擎。"""

    def __init__(self, *, store: Optional[PlatformStore] = None,
                 rules: Optional[Dict[str, Any]] = None,
                 privacy: Optional[PrivacyPreferences] = None,
                 dossiers: Optional[DossierService] = None,
                 providers: Optional[List[Any]] = None,
                 agent: Any = None,
                 domains: Any = None,
                 report: Any = None) -> None:
        from mylocalai.platform.rules import load_rules
        self.store = store or PlatformStore()
        loaded, self.rule_warnings = load_rules()
        self.rules = rules if rules is not None else loaded
        self.privacy = privacy or load_preferences()
        self.dossiers = dossiers or DossierService(privacy=self.privacy)
        self._agent = agent
        # 领域线程（platform.domains）：注入优先；未注入走惰性 import property。
        self._domains = _DOMAINS_UNSET if domains is None else domains
        # 报告技能（platform.report）：注入优先；未注入走惰性 import（_get_report）。
        self._report = _REPORT_UNSET if report is None else report
        # store 是否已收 topic_domain 落库字段（数据层 v7 落地前的过渡检测：
        # 未落地时不传该字段，巡检其余流程不变；落地后自动带上）。
        self._feed_topic_domain = _feed_store_supports_topic_domain(self.store)
        if providers is not None:
            self.providers = list(providers)
        else:
            self.providers, w = build_providers(self.rules, store=self.store)
            self.rule_warnings.extend(w)

    # ---- 引擎惰性装配（与 web 层 get_rumor_agent 同口径）----

    @property
    def domains(self) -> Any:
        """领域模块（platform.domains）：None 时惰性 import 并缓存；
        import 失败 → None，runner 降级为无领域模式（不重试、不抛）。"""
        if self._domains is _DOMAINS_UNSET:
            try:
                from mylocalai.platform import domains as d
                self._domains = d
            except Exception:  # noqa: BLE001  领域模块缺失/损坏 → 无领域模式
                self._domains = None
        return self._domains

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

    def _get_report(self) -> Any:
        """巡检报告技能（platform.report.PatrolReport）惰性装配。

        注入优先；未注入走惰性 import 并缓存（与 domains property 同模式）。
        import 失败（模块缺失/损坏）→ None：runner 降级为无报告阶段，
        _build_report 据此只留 warning 说明，不重试、不抛（报告是增益面）。
        规则段 rules["report"] 原样传给技能（缺失退技能内置默认，与
        platform/rules.py DEFAULT_RULES["report"] 同值）。
        """
        if self._report is _REPORT_UNSET:
            try:
                from mylocalai.platform.report import PatrolReport
                self._report = PatrolReport(rules=self.rules.get("report") or {})
            except Exception:  # noqa: BLE001  报告模块缺失/损坏 → 无报告阶段
                self._report = None
        return self._report

    def _domain_hints(self, domain: str) -> Optional[List[str]]:
        """领域 → 检索提示（权威站点清单，喂 RumorAgent 的 domain_hints）。
        无领域/领域模块不可用/取提示失败/提示为空 → None（不追加定向检索轮）。"""
        if not domain or self.domains is None:
            return None
        try:
            hints = self.domains.domain_hints(domain)
        except Exception:  # noqa: BLE001  提示缺失不该让深查失败
            return None
        return list(hints) if hints else None

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
        # 报告阶段：status 定稿后才生成（scan_status 要如实进 payload）。
        # 增益面——任何失败只落 warning，不影响下面落账与返回。
        self._build_report(summary, scan_id, persist, cancel_check)
        self._finish(scan_id, summary, started)
        return summary

    # ---- 单监控词 ----

    def _scan_query(self, query_id: int, query: str, summary: Dict[str, Any],
                    persist: bool, state: Dict[str, int],
                    cancel_check: Optional[Callable[[], bool]]) -> bool:
        """处理一条监控词。返回 True 表示处理途中被取消（已完成的条目已保留）。"""
        k = max(1, int(self.rules.get("max_candidates_per_query", 8)))
        # 领域解析在候选循环前：读监控词整行（用户手设领域 + 推断缓存），
        # 经 domains.resolve_domain 走「user > 缓存(TTL) > 文本分类 > 历史 > none」链。
        row = self._watch_query_row(query_id)
        domain, domain_source = self._resolve_domain(query, row, summary)
        raw: List[FeedCandidate] = []
        for provider in self.providers:
            res = provider.discover(query, k, query_id=query_id)
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
        # growth 进 queries 供报告的分监控词环比用：relative_change 是 [0,1]
        # 相对变化（risk 的 growth 因素照旧吃这个小数），报告条目行要的是
        # 「环比=X%」——同一语义两种单位，这里换算成百分比（×100，两位小数），
        # 免得报告里出现「环比=0.12%」这种把小数当百分比的误导。
        summary["queries"].append({"id": query_id, "query": query, "e": e_score,
                                   "quality": heat.get("quality", "missing"),
                                   "candidates": len(kept),
                                   "growth": round(growth * 100.0, 2),
                                   "domain": domain,
                                   "domain_source": domain_source})

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
                                    e_prev, growth, breadth, screen_e, state,
                                    domain, domain_source)
        return False

    def _watch_query_row(self, query_id: int) -> Optional[Dict[str, Any]]:
        """读监控词整行（用户手设领域 + 推断缓存）。store 无该方法/读失败 → None
        （按"无领域信息"继续，领域链自然落到后续环节）。"""
        try:
            getter = getattr(self.store, "get_watch_query", None)
            if getter is None:
                return None
            row = getter(int(query_id))
            return dict(row) if isinstance(row, dict) else None
        except Exception:  # noqa: BLE001  读行失败不拖垮巡检
            return None

    def _resolve_domain(self, query: str, row: Optional[Dict[str, Any]],
                        summary: Dict[str, Any]) -> Tuple[str, str]:
        """监控词 → (领域, 来源)。领域模块不可用/解析失败 → ("", "none")，
        runner 降级为无领域模式继续巡检（不静默：失败留 warning）。"""
        d = self.domains
        if d is None:
            return "", "none"
        try:
            domain_rules = self.rules.get("domain") or {}
            out = d.resolve_domain(
                str((row or {}).get("domain") or ""), query, row or {}, self.store,
                None,
                now_iso=_now_iso(),
                ttl_days=float(domain_rules.get("infer_ttl_days", 30)),
                history_min=int(domain_rules.get("history_min", 2)))
            if not (isinstance(out, tuple) and len(out) == 2):
                return "", "none"
            return str(out[0] or ""), str(out[1] or "none")
        except Exception as exc:  # noqa: BLE001  领域解析失败 → 无领域模式
            summary["warnings"].append(
                f"领域解析失败（{query[:40]}）：{type(exc).__name__}: {exc}")
            return "", "none"

    def _process_candidate(self, query_id: int, cand: FeedCandidate,
                           summary: Dict[str, Any], persist: bool,
                           e_score: Optional[float], e_prev: Optional[float],
                           growth: float, breadth: Dict[str, int], screen_e: float,
                           state: Dict[str, int], domain: str = "",
                           domain_source: str = "") -> None:
        entry_id: Optional[int] = None
        if persist:
            topic_kwargs: Dict[str, Any] = {}
            if self._feed_topic_domain:
                topic_kwargs = {"topic_domain": domain,
                                "topic_domain_source": domain_source}
            entry_id = self.store.add_feed_entry(
                query_id=query_id, title=cand.title, url=cand.url,
                snippet=cand.snippet, domain=cand.domain, domain_tier=cand.domain_tier,
                discovered_at=cand.published_at, e_score=e_score, **topic_kwargs)
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
            status, rumor = self._deep_verify(cand, summary, state, domain)

        risk = assess_spread_risk(
            heat=e_score if e_score is not None else 0.0,
            growth=growth, breadth=breadth,
            rumor=rumor, rules=self.rules)

        entry = {"query_id": query_id, "title": cand.title, "url": cand.url,
                 "domain": cand.domain, "domain_tier": cand.domain_tier,
                 "topic_domain": domain, "topic_domain_source": domain_source,
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
                risk_score=entry["risk_score"], e_prev=e_prev,
                topic_domain=domain, topic_domain_source=domain_source)
            entry["id"] = entry_id
        summary["entries"].append(entry)

    def _deep_verify(self, cand: FeedCandidate, summary: Dict[str, Any],
                     state: Dict[str, int],
                     domain: str = "") -> Tuple[str, Optional[Dict[str, Any]]]:
        """阶段二：RumorAgent 全链路 + 建档。失败降级不抛，不编 verdict。"""
        state["verify_left"] = max(0, state["verify_left"] - 1)   # 预算先扣后跑
        text = f"{cand.title}\n{cand.snippet}".strip()[:AGENT_MAX_CHARS]
        hints = self._domain_hints(domain)
        t0 = time.perf_counter()
        try:
            result = self._get_agent().analyze(text, domain_hints=hints)
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

    def _build_report(self, summary: Dict[str, Any], scan_id: Optional[int],
                      persist: bool,
                      cancel_check: Optional[Callable[[], bool]]) -> Optional[Dict[str, Any]]:
        """生成一轮巡检报告（platform.report.PatrolReport）并挂进 summary。

        位置：scan() 里 status 定稿后、_finish 前——报告要读到定稿的
        scan_status 与全部计数；skipped 与"无监控词"两条早退路径没有可汇总的
        一轮，直接 return 不走到这里。

        入参（skills 契约）：entries/queries/counters/warnings 取自 summary，
        prev_scan 是上一轮 scan_runs 行（环比基准），cancel_check 透传
        （已取消的巡检 → 报告记 llm_status=cancelled，不白烧一次 LLM）。

        降级文化（每个出口都有降级，scan 主流程绝不因本阶段抛异常）：
          - 报告技能未注入且 import 失败 → 只留 warning，不产报告；
          - prev_scan 取不到（store 无该方法/读失败/首轮）→ None，报告按
            "无基准"处理（changes={}，不假装有环比）；
          - build 自身承诺永不抛，这里仍兜最外层异常：任何意外只落成
            warning「巡检报告生成失败：...」并返回 None；
          - 落库失败（save_report 抛：如 llm_status 越界/外键冲突）同样只留
            warning「巡检报告落库失败：...」——报告已进 summary 照常呈现。
        """
        try:
            report_skill = self._get_report()
            if report_skill is None:
                summary["warnings"].append(
                    "巡检报告未生成：报告技能不可用（模块缺失或装配失败）")
                return None
            prev_scan: Optional[Dict[str, Any]] = None
            if scan_id is not None:
                try:
                    prev_scan = self.store.previous_scan_run(scan_id)
                except Exception as exc:  # noqa: BLE001  基准轮缺失只影响环比
                    summary["warnings"].append(
                        f"上一轮巡检读取失败，报告不含环比：{type(exc).__name__}")
            # 报告 LLM 调用有独立预算面（rules["report"]["deadline_s"]，默认
            # 60s；<=0 不限）——与深查的 deadline scope 同一机制、互不占用。
            try:
                scope_s = float((self.rules.get("report") or {}).get("deadline_s", 60) or 0)
            except (TypeError, ValueError):
                scope_s = 0.0
            from mylocalai.llm import llm_cache
            with llm_cache.deadline_scope(scope_s if scope_s > 0 else None):
                report = report_skill.build(
                    scan_id=scan_id,
                    scan_status=summary["status"],
                    entries=summary.get("entries") or [],
                    queries=summary.get("queries") or [],
                    counters=summary.get("counters") or {},
                    warnings=summary.get("warnings") or [],
                    prev_scan=prev_scan,
                    cancel_check=cancel_check)
        except Exception as exc:  # noqa: BLE001  报告是增益面：绝不炸 scan
            summary["warnings"].append(
                f"巡检报告生成失败：{type(exc).__name__}: {exc}")
            return None
        if not isinstance(report, dict):
            summary["warnings"].append("巡检报告生成失败：返回形状不是对象")
            return None
        summary["report"] = report
        if persist and scan_id is not None and hasattr(self.store, "save_report"):
            try:
                self.store.save_report(
                    scan_id, title=report.get("title", ""),
                    llm_status=report.get("llm_status", ""),
                    status=report.get("scan_status", ""), payload=report)
            except Exception as exc:  # noqa: BLE001  落库失败不影响报告呈现
                summary["warnings"].append(
                    f"巡检报告落库失败：{type(exc).__name__}: {exc}")
        return report

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


def _now_iso() -> str:
    """本地时间戳（ISO 秒级、带偏移；与 store._now 同时格式）。
    供领域解析 now_iso 用：T2 的 TTL 比较吃 fromisoformat，带偏移本地/UTC 皆可。"""
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _feed_store_supports_topic_domain(store: Any) -> bool:
    """store.add_feed_entry 是否已收 topic_domain 参数（数据层 v7 落地检测）。
    未落地时不传该字段、巡检其余流程不变；落地后自动带上（并行集成过渡用）。"""
    try:
        import inspect
        params = inspect.signature(store.add_feed_entry).parameters
    except (AttributeError, TypeError, ValueError):
        return False
    return "topic_domain" in params


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
