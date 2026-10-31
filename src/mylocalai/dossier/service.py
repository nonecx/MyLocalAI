# -*- coding: utf-8 -*-
"""档案库服务编排（M2）：自动建档、结论/证据落库、复检版本化。

职责（工作指导 §6 M2-7 / M3-2 / M3-6）：
  - verify/recheck 类工具跑完 agent 全链路后，把结论卡片、证据本、分析运行记录
    写进档案库（结论按版本追加，证据按批次追加）；
  - recheck 时在原档案上追加 v2/v3… 与新批次证据，"版本对比"由调用方取
    conclusions() 自行渲染；
  - 章节懒加载：热度快照 / 观点光谱 / 深读由对应工具触发时写入（本模块只存）；
  - 超时/降级时只写已完成的部分，档案状态如实（degraded 标记），不编造；
  - M3 读面：chapters() 六章节聚合（纯读取、永不抛）与 export_markdown()
    六章节 Markdown 导出（纯字符串拼接、零 LLM，含免责语与 as_of 页脚）。

依赖方向：dossier → rumor/classic/attention（allowed），上游不知道档案库存在。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from mylocalai.core.card import derive_card
from mylocalai.dossier.store import DossierStore

__all__ = ["DossierService"]

#: 章节聚合 / 导出共用的常量（口径与 assistant/tools_memory._EVIDENCE_PREVIEW 一致）
_EVIDENCE_PREVIEW = 5   # 每个证据批次最多预览条数

#: 固定免责语（M3-6 验收项：快照断言含此句，逐字不改）
EXPORT_DISCLAIMER = "本报告只述证据与立场分布，不对消息真假下结论；请以官方通报为准。"

#: 章节缺数据的如实文案（懒加载是常态：没生成过的章节明说，不编造）
_ABSENT_TEXT = "本节暂无数据（该章节尚未生成；章节按需生成，不预生成、不编造）。"

#: 言论土壤章节在 raw_verdict 里的字段（record_speech_analysis 的落库键）
_SPEECH_KEYS = ("emotion", "attack", "logic", "conclusion")


def _trend_block(snapshots: List[Dict[str, Any]]) -> Dict[str, Any]:
    """热度趋势区块：try-import attention.trend（并行工作流交付中）。

    契约（编排者钉死）：render_trend_svg(snapshots)->str 与 trend_summary(snapshots)->dict
    均为纯函数。模块尚未落地 / 导入失败 → {"trend_status": "pending"}，前端显示
    "趋势组件加载中"占位，编排者收口后自然生效；本模块不建、不改 attention/ 下任何文件。
    """
    try:
        from mylocalai.attention import trend as _trend  # 延迟到调用时（并行交付中）
    except ImportError:
        return {"trend_status": "pending"}
    try:
        svg = str(_trend.render_trend_svg(snapshots) or "")
        summary = _trend.trend_summary(snapshots)
        return {"svg": svg,
                "summary": summary if isinstance(summary, dict) else {}}
    except Exception as exc:  # noqa: BLE001  趋势算不出也不能拖垮章节读取
        return {"trend_status": "pending", "error": f"{type(exc).__name__}: {exc}"}


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fmt_g(value: Any) -> str:
    """快照数值的紧凑格式（坏值如实显示 ?，不编 0）。"""
    return f"{value:g}" if _is_num(value) else "?"


def _batches_text(by_batch: Any) -> str:
    """{batch: count} → "批次 1×2、批次 2×5"（空 → "无"）。"""
    if not isinstance(by_batch, dict) or not by_batch:
        return "无"
    return "、".join(f"批次 {b}×{n}" for b, n in by_batch.items())


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class DossierService:
    """档案域编排门面（store 之上的业务语义层）。"""

    def __init__(self, store: Optional[DossierStore] = None) -> None:
        self.store = store or DossierStore()

    def close(self) -> None:
        self.store.close()

    # ---- 建档 ----

    def start_dossier(self, text: str, title: Optional[str] = None,
                      input_kind: str = "text") -> int:
        title = title or (str(text or "").strip()[:24] or "未命名核查")
        return self.store.create_dossier(title, str(text or ""), input_kind=input_kind)

    # ---- 结论落库（verify / recheck 共用） ----

    def record_analysis(self, dossier_id: int, result: Dict[str, Any],
                        kind: str = "verify", duration_s: float = 0.0,
                        requests: int = 0, tokens: int = 0) -> Dict[str, Any]:
        """把一次 agent 全链路结果写入档案：卡片化结论 + 证据批次 + 运行记录。

        result 是 RumorAgent.analyze() 的返回（label/risk_score/evidence_book/...）。
        返回 {"version", "card", "batch"}。失败不抛：写不进的部分返回 degraded 标记。
        """
        out: Dict[str, Any] = {"version": None, "card": None, "batch": None}
        try:
            book = result.get("evidence_book") or {}
            entries = [e for e in (book.get("entries") or []) if isinstance(e, dict)]
            batch = 1
            prev = self.store.evidence(dossier_id)
            if prev:
                batch = max(int(r.get("batch", 1)) for r in prev) + 1
            self.store.add_evidence(dossier_id, batch, entries)
            out["batch"] = batch

            card = derive_card(result, entries, as_of=_now_iso())
            version = self.store.add_conclusion(
                dossier_id,
                label=str(result.get("label", "")),
                risk_score=result.get("risk_score"),
                confidence=str(card["confidence"]),
                evidence_sufficiency=card["evidence_sufficiency"],
                as_of=card["as_of"],
                recheck_conditions=card["recheck_conditions"],
                raw_verdict={k: result.get(k) for k in
                             ("label", "risk_score", "signals", "speech_tier", "confidence")
                             if k in result},
            )
            card["label"] = str(result.get("label", ""))
            out["version"] = version
            out["card"] = card

            degraded = bool(result.get("degraded_components") or result.get("agent_error"))
            self.store.add_run(dossier_id, kind=kind, status="ok" if not degraded else "degraded",
                               degraded=degraded, duration_s=duration_s,
                               requests=requests, tokens=tokens)
            self.store.touch_dossier(dossier_id)
        except Exception as exc:  # noqa: BLE001  档案写失败不影响主结论
            out["degraded"] = True
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    def record_speech_analysis(self, dossier_id: int, result: Dict[str, Any],
                               duration_s: float = 0.0) -> Dict[str, Any]:
        """classic（言论土壤）结果建档：四档结论为标签，无风险分，无证据本。"""
        out: Dict[str, Any] = {"version": None, "card": None}
        try:
            conclusion = str(result.get("conclusion", ""))
            card = derive_card({"label": conclusion, "risk_score": None}, [], as_of=_now_iso())
            version = self.store.add_conclusion(
                dossier_id, label=conclusion, risk_score=None,
                confidence=card["confidence"],
                evidence_sufficiency=card["evidence_sufficiency"],
                as_of=card["as_of"], recheck_conditions=card["recheck_conditions"],
                raw_verdict={k: result.get(k) for k in
                             ("emotion", "attack", "logic", "conclusion") if k in result})
            out["version"] = version
            out["card"] = card
            degraded = bool(result.get("degraded_components"))
            self.store.add_run(dossier_id, kind="speech",
                               status="ok" if not degraded else "degraded",
                               degraded=degraded, duration_s=duration_s)
            self.store.touch_dossier(dossier_id)
        except Exception as exc:  # noqa: BLE001
            out["degraded"] = True
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    # ---- 章节落库 ----

    def record_heat(self, dossier_id: int, detail: Dict[str, Any],
                    profile: str = "") -> int:
        return self.store.add_snapshot(
            dossier_id, float(detail.get("x", 0)), float(detail.get("y", 0)),
            float(detail.get("z", 0)), float(detail.get("score", 0)),
            profile=profile or str(detail.get("profile", "")),
            proxy_flags=list(detail.get("proxy_flags") or []))

    def record_opinions(self, dossier_id: int, payload: Dict[str, Any]) -> int:
        book = payload.get("evidence_book") or {}
        prev = self.store.evidence(dossier_id)
        batch = (max(int(r.get("batch", 1)) for r in prev) if prev else 1)
        return self.store.add_opinions(
            dossier_id, batch, payload.get("distribution") or {},
            summary=str(payload.get("summary", "")), degraded=bool(payload.get("degraded")))

    def record_deep_read(self, dossier_id: int, payload: Dict[str, Any]) -> int:
        return self.store.add_deep_read(
            dossier_id, payload, degraded=bool(payload.get("degraded")),
            guard_dropped=list(payload.get("guard_dropped") or []))

    # ---- 版本对比（M3-2 的数据面） ----

    def compare_versions(self, dossier_id: int) -> Dict[str, Any]:
        """v1→vN 的标签/分数变化 + 证据批次增减（纯读取，供 get_dossier 渲染）。"""
        cs = self.store.conclusions(dossier_id)
        ev = self.store.evidence(dossier_id)
        by_batch: Dict[int, int] = {}
        for e in ev:
            by_batch[int(e.get("batch", 1))] = by_batch.get(int(e.get("batch", 1)), 0) + 1
        return {
            "versions": [{"version": c["version"], "label": c["label"],
                          "risk_score": c["risk_score"], "as_of": c["as_of"]} for c in cs],
            "evidence_batches": by_batch,
            "changed": len(cs) > 1 and cs[0]["label"] != cs[-1]["label"],
        }

    # ---- 六章节聚合（M3 档案库页的读面；纯读取，全路径永不抛） ----

    def chapters(self, dossier_id: int) -> Dict[str, Any]:
        """六章节聚合：conclusion / evidence / speech / heat / opinions / deep_read。

        口径（§6 M3-2 / M3-6，与 assistant/tools_memory 的缺章约定一致）：
          - conclusion：全部版本结论 + compare_versions 版本对比 + 分析运行摘要；
          - evidence  ：按批次分组计数 + 每批前 N 条预览；
          - speech    ：取结论 raw_verdict 里的四档字段，无则 {"chapter_status": "absent"}；
          - heat      ：快照序列；有 ≥1 条时附 trend（try-import attention.trend，
                        ImportError → {"trend_status": "pending"} 占位）；
          - opinions  ：观点光谱行；无则 absent；
          - deep_read ：最新一次深读 payload + degraded + guard_dropped；无则 absent。
        任何一读失败都退空数据，绝不抛——档案页不能因为一章坏数据整页崩掉。
        """
        try:
            did = int(dossier_id)
        except (TypeError, ValueError):
            did = -1   # 垃圾 id 当"不存在的档案"处理：六章形状照常返回，各章空数据
        return {
            "conclusion": self._chapter_conclusion(did),
            "evidence": self._chapter_evidence(did),
            "speech": self._chapter_speech(did),
            "heat": self._chapter_heat(did),
            "opinions": self._chapter_opinions(did),
            "deep_read": self._chapter_deep_read(did),
        }

    def _chapter_conclusion(self, did: int) -> Dict[str, Any]:
        try:
            cs = self.store.conclusions(did)
        except Exception:  # noqa: BLE001
            cs = []
        try:
            compare = self.compare_versions(did)
        except Exception:  # noqa: BLE001
            compare = {"versions": [], "evidence_batches": {}, "changed": False}
        try:
            runs = self.store.runs(did)
        except Exception:  # noqa: BLE001
            runs = []
        return {
            "count": len(cs),
            "conclusions": cs,
            "compare": compare,
            "runs": [{"kind": str(r.get("kind", "")), "status": str(r.get("status", "")),
                      "degraded": bool(r.get("degraded")),
                      "created_at": str(r.get("created_at", ""))} for r in runs],
            "degraded": any(bool(r.get("degraded")) for r in runs),
        }

    def _chapter_evidence(self, did: int) -> Dict[str, Any]:
        try:
            rows = self.store.evidence(did)
        except Exception:  # noqa: BLE001
            rows = []
        by_batch: Dict[int, List[Dict[str, Any]]] = {}
        for e in rows:
            if not isinstance(e, dict):
                continue
            by_batch.setdefault(int(e.get("batch", 1) or 1), []).append(e)
        keys = ("url", "domain", "domain_tier", "source_kind", "stance", "title",
                "snippet", "fetched_at")
        batches = [{"batch": b, "count": len(items),
                    "entries": [{k: item.get(k) for k in keys}
                                for item in items[:_EVIDENCE_PREVIEW]]}
                   for b, items in sorted(by_batch.items())]
        return {"total": len(rows), "batches": batches}

    def _chapter_speech(self, did: int) -> Dict[str, Any]:
        try:
            cs = self.store.conclusions(did)
        except Exception:  # noqa: BLE001
            cs = []
        for c in reversed(cs):
            raw = c.get("raw_verdict")
            if not isinstance(raw, dict):
                continue
            picked = {k: raw[k] for k in _SPEECH_KEYS if k in raw}
            if not picked:
                continue
            picked["label"] = str(c.get("label", ""))
            picked["version"] = c.get("version")
            picked["as_of"] = str(c.get("as_of", ""))
            return picked
        return {"chapter_status": "absent"}

    def _chapter_heat(self, did: int) -> Dict[str, Any]:
        try:
            snaps = self.store.snapshots(did)
        except Exception:  # noqa: BLE001
            snaps = []
        chapter: Dict[str, Any] = {"count": len(snaps), "snapshots": snaps}
        if snaps:
            chapter["trend"] = _trend_block(snaps)
        return chapter

    def _chapter_opinions(self, did: int) -> Dict[str, Any]:
        try:
            rows = self.store.opinions(did)
        except Exception:  # noqa: BLE001
            rows = []
        if not rows:
            return {"chapter_status": "absent"}
        return {"count": len(rows), "rows": rows}

    def _chapter_deep_read(self, did: int) -> Dict[str, Any]:
        try:
            rows = self.store.deep_reads(did)
        except Exception:  # noqa: BLE001
            rows = []
        if not rows:
            return {"chapter_status": "absent"}
        latest = rows[-1]
        payload = latest.get("payload")
        return {"count": len(rows),
                "payload": payload if isinstance(payload, dict) else {},
                "degraded": bool(latest.get("degraded")),
                "guard_dropped": list(latest.get("guard_dropped") or []),
                "created_at": str(latest.get("created_at", ""))}

    # ---- 六章节 Markdown 导出（M3-6；纯字符串拼接，零 LLM） ----

    def export_markdown(self, dossier_id: int) -> str:
        """档案 → 六章节 Markdown 文本（含徽章、免责语固定段、as_of 页脚）。

        各章 absent 时如实写"本节暂无数据"；任何一步失败也返回一段诚实说明的
        文本而不是抛异常——导出是读路径的末端，不能让调用方拿到 500。
        """
        try:
            return self._render_export(int(dossier_id))
        except Exception as exc:  # noqa: BLE001
            return (f"# 档案导出（id={dossier_id}）\n\n"
                    f"导出生成失败：{type(exc).__name__}: {exc}\n\n"
                    f"{EXPORT_DISCLAIMER}\n")

    def _render_export(self, did: int) -> str:
        header = self.store.get_dossier(did) or {}
        ch = self.chapters(did)
        lines: List[str] = [
            f"# 档案：{header.get('title') or '未命名核查'}",
            "",
            f"- 档案编号：{did}",
            f"- 类型：{header.get('input_kind', 'text') or 'text'}",
            f"- 建档时间：{header.get('created_at', '') or '未知'}",
            f"- 最近更新：{header.get('updated_at', '') or '未知'}",
            "",
            f"> 免责声明：{EXPORT_DISCLAIMER}",
        ]
        lines += self._export_conclusion_section(ch.get("conclusion") or {})
        lines += self._export_evidence_section(ch.get("evidence") or {})
        lines += self._export_speech_section(ch.get("speech") or {})
        lines += self._export_heat_section(ch.get("heat") or {})
        lines += self._export_opinions_section(ch.get("opinions") or {})
        lines += self._export_deep_read_section(ch.get("deep_read") or {})

        latest_as_of = ""
        for c in reversed((ch.get("conclusion") or {}).get("conclusions") or []):
            if c.get("as_of"):
                latest_as_of = str(c["as_of"])
                break
        lines += ["", "---",
                  f"证据截至（as_of）：{latest_as_of or '未知'} · "
                  f"报告生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
                  EXPORT_DISCLAIMER, ""]
        return "\n".join(lines)

    @staticmethod
    def _export_badge_line(confidence: str, suff: Dict[str, Any],
                           degraded: Optional[bool] = None) -> str:
        """徽章行：口径沿用卡片字段（置信度 / 证据充分度 / 降级）。"""
        merged = {"official": 0, "unofficial": 0, "unknown": 0, "total": 0}
        merged.update({k: v for k, v in (suff or {}).items() if _is_num(v)})
        badges = [f"置信度 {confidence or '未知'}",
                  "证据充分度：官方 {official} / 非官方 {unofficial} / 未知 {unknown}"
                  "，共 {total} 条".format(**merged)]
        if degraded is not None:
            badges.append("降级输出：是" if degraded else "降级输出：否")
        return "- 徽章：" + " · ".join(badges)

    def _export_conclusion_section(self, concl: Dict[str, Any]) -> List[str]:
        lines = ["", "## 一、核查结论（版本化）"]
        compare = concl.get("compare") or {}
        versions = compare.get("versions") or []
        if not versions:
            return lines + ["", _ABSENT_TEXT]
        lines += ["", f"- 版本对比：v1→v{versions[-1].get('version')} 标签"
                      f"{'【' + str(versions[0].get('label', '')) + '】→【' + str(versions[-1].get('label', '')) + '】（有变化）' if compare.get('changed') else '未变'}"
                      f"；证据批次 {_batches_text(compare.get('evidence_batches'))}"]
        if concl.get("degraded"):
            lines.append("- 分析记录：含降级运行（超时/组件失败时只写已完成部分，档案状态如实）")
        for c in concl.get("conclusions") or []:
            risk = c.get("risk_score")
            risk_txt = f"（风险分 {risk:g}/100）" if _is_num(risk) else "（无风险分）"
            lines += ["", f"### v{c.get('version')}：{c.get('label') or '（无标签）'}{risk_txt}"]
            lines.append(self._export_badge_line(str(c.get("confidence", "")),
                                                 c.get("evidence_sufficiency") or {}))
            lines.append(f"- 证据截至（as_of）：{c.get('as_of') or '未知'}")
            for rc in c.get("recheck_conditions") or []:
                lines.append(f"- 复审条件：{rc}")
        return lines

    @staticmethod
    def _export_evidence_section(evidence: Dict[str, Any]) -> List[str]:
        lines = ["", "## 二、证据本"]
        batches = evidence.get("batches") or []
        if not batches:
            return lines + ["", _ABSENT_TEXT]
        lines += ["", f"共 {evidence.get('total', 0)} 条，分 {len(batches)} 个批次"]
        for b in batches:
            lines += ["", f"### 批次 {b.get('batch')}（{b.get('count', 0)} 条）"]
            for e in b.get("entries") or []:
                stance = e.get("stance") or "无关"
                tier = e.get("source_kind") or e.get("domain_tier") or "未知来源"
                title = e.get("title") or e.get("url") or "（无标题）"
                lines.append(f"- [{stance}] {title}（{tier}）")
                if e.get("url"):
                    lines.append(f"  <{e['url']}>")
        return lines

    @staticmethod
    def _export_speech_section(speech: Dict[str, Any]) -> List[str]:
        lines = ["", "## 三、言论土壤（classic 四档）"]
        if speech.get("chapter_status") == "absent":
            return lines + ["", _ABSENT_TEXT]
        label = speech.get("conclusion") or speech.get("label") or "（无档位）"
        lines += ["", f"- 结论档位：{label}"]
        names = {"emotion": "情感倾向", "attack": "攻击等级", "logic": "逻辑关联"}
        for key, name in names.items():
            value = speech.get(key)
            if value is None:
                continue
            if isinstance(value, dict):
                compact = "；".join(f"{k} {v}" for k, v in value.items())
            else:
                compact = str(value)
            lines.append(f"- {name}：{compact}")
        lines.append(f"- 证据截至（as_of）：{speech.get('as_of') or '未知'}")
        return lines

    @staticmethod
    def _export_heat_section(heat: Dict[str, Any]) -> List[str]:
        lines = ["", "## 四、热度快照与趋势"]
        snaps = heat.get("snapshots") or []
        if not snaps:
            return lines + ["", _ABSENT_TEXT]
        lines += ["", f"共 {len(snaps)} 次快照（E 分为确定性公式输出，y/z 多为代理值）。"]
        for s in snaps:
            flags = "、".join(str(f) for f in (s.get("proxy_flags") or []))
            lines.append(
                f"- {s.get('taken_at', '')}：x={_fmt_g(s.get('x', 0))} y={_fmt_g(s.get('y', 0))}"
                f" z={_fmt_g(s.get('z', 0))} E={_fmt_g(s.get('e_score', 0))}"
                + (f"（代理值：{flags}）" if flags else ""))
        trend = heat.get("trend") or {}
        if trend.get("trend_status") == "pending":
            lines.append("- 趋势：趋势组件加载中（占位；组件收口后此行自动带出趋势摘要）")
        else:
            summary = trend.get("summary") or {}
            if summary:
                lines.append("- 趋势摘要：" + "；".join(f"{k} {v}" for k, v in summary.items()))
        return lines

    @staticmethod
    def _export_opinions_section(opinions: Dict[str, Any]) -> List[str]:
        lines = ["", "## 五、观点光谱"]
        if opinions.get("chapter_status") == "absent":
            return lines + ["", _ABSENT_TEXT]
        for row in opinions.get("rows") or []:
            dist = row.get("distribution") or {}
            dist_txt = "、".join(f"{k} {v}" for k, v in dist.items()) if dist else "（无分布数据）"
            badge = "【降级输出】" if row.get("degraded") else ""
            lines.append(f"- 批次 {row.get('batch', 1)}：{dist_txt}"
                         f"（{row.get('created_at', '') or '时间未知'}）{badge}")
            if row.get("summary"):
                lines.append(f"  摘要：{row['summary']}")
        return lines

    @staticmethod
    def _export_deep_read_section(deep: Dict[str, Any]) -> List[str]:
        lines = ["", "## 六、背景解读（深读）"]
        if deep.get("chapter_status") == "absent":
            return lines + ["", _ABSENT_TEXT]
        badges: List[str] = []
        if deep.get("degraded"):
            badges.append("【降级输出】")
        dropped = deep.get("guard_dropped") or []
        if dropped:
            badges.append(f"【护栏丢弃 {len(dropped)} 项：{'、'.join(str(x) for x in dropped)}】")
        lines += ["", f"- 口径徽章：{' '.join(badges) if badges else '正常输出（未降级、无护栏丢弃）'}"]
        payload = deep.get("payload") or {}
        text = str(payload.get("text") or "").strip()
        if text:
            lines += ["", text]
        else:
            lines += ["", "（解读正文为空：模型判定不适用或输出被护栏丢弃，如实呈现，不代写。）"]
        lines.append(f"- 解读生成时间：{deep.get('created_at') or '未知'}")
        return lines
