# -*- coding: utf-8 -*-
"""L4 参谋聚合层：话术模式库 / 信源画像 / 个人信息环境周报（全部纯读取、零 LLM）。

三条铁律在本模块的落法：
  - 不判真假只述证据：输出是"哪些话术在个人库里命中过多少档案、哪些域名反驳过
    多少档案、一周里发生了什么"，全是计数与排序，不做任何真假断言；
  - 大模型不参与任何打分：标签/份额/达标线全部由纯函数从库内数据推导，本模块
    连 LLM 的 import 都没有；
  - 每个出口有降级：空库/坏数据/坏时间串返回合法空结构（"暂无数据"是合法输出），
    四个出口永不抛。

口径说明（docstring 即契约）：
  - 读数据一律经 ``store._conn`` 只读 SQL（同包私有访问，tests 已有此先例）；
  - 域名一律从 url 现算（``retrieval.sources.normalize_host``，与引擎 evidence.py
    同一口径）——evidence_entries.domain 列在真实管线恒为空串，不可信；
  - 时间窗判定用 ISO 字符串直接比较：库内全部时间戳来自同一 ``store._now()``
    （datetime.now().astimezone().isoformat(timespec="seconds")，同一格式、同一
    时区偏移），字符串序与时间序一致才能这么比；
  - "有结论的档案"＝ conclusions 表出现过的 dossier_id；每档案最新结论 label ＝
    该档案 version 最大的 conclusions.label。

依赖方向：dossier → rumor / retrieval（allowed，见 tests/test_import_rules.py）。
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from mylocalai.retrieval.sources import normalize_host
from mylocalai.rumor.verdict import LABEL_HIGH, LABEL_LOW, LABEL_SUSPECT

__all__ = ["QUALIFIED_MIN_REFUTES", "pattern_library", "source_profiles",
           "qualified_source_profile", "weekly_digest"]

#: 信源画像的内置达标线：反驳过 ≥2 个"高风险/疑似"档案的域名才算 qualified。
#: 与 qualified_source_profile 的 min_refutes 缺省同口径（一处常量两处用）。
QUALIFIED_MIN_REFUTES = 2

#: common_pairs / weekly_digest.patterns.top 的条数上限
_TOP_PAIRS = 5
_TOP_PATTERNS = 5


# ---------------------------------------------------------------------------
# 小工具（全部不抛）
# ---------------------------------------------------------------------------

def _load_json(raw: Any, default: Any) -> Any:
    """JSON 文本还原；坏数据/空值退默认（库内 payload 都是 _dump 写的，这里是兜底）。"""
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _latest_labels(conn: Any, profile_id: Optional[int] = None) -> Dict[int, str]:
    """每档案最新结论（version 最大）的 label：{dossier_id: label}。

    profile_id 非 None 时只统计该档案工作区（v6 多 Profile 分家：画像不外泄到
    别的工作区）；None 时不过滤（与加参数前逐位一致）。
    """
    sql = ("SELECT c.dossier_id AS did, c.label AS label FROM conclusions c"
           " JOIN (SELECT dossier_id, MAX(version) AS v FROM conclusions GROUP BY dossier_id) t"
           " ON c.dossier_id = t.dossier_id AND c.version = t.v")
    args: List[Any] = []
    if profile_id is not None:
        sql += " JOIN dossiers d ON d.id = c.dossier_id WHERE d.profile_id = ?"
        args.append(int(profile_id))
    rows = conn.execute(sql, args).fetchall()
    return {int(r["did"]): str(r["label"] or "") for r in rows}


def _marker_rows(conn: Any, profile_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """dossier_markers 全表 JOIN dossiers（CASCADE 保证都在），hits 已还原为 dict。

    profile_id 非 None 时只统计该档案工作区；None 时不过滤（逐位不变）。
    """
    sql = ("SELECT m.dossier_id AS did, m.risk_hits AS risk_hits,"
           " m.credibility_hits AS credibility_hits"
           " FROM dossier_markers m JOIN dossiers d ON d.id = m.dossier_id")
    args: List[Any] = []
    if profile_id is not None:
        sql += " WHERE d.profile_id = ?"
        args.append(int(profile_id))
    rows = conn.execute(sql, args).fetchall()
    return [{"did": int(r["did"]),
             "risk": _load_json(r["risk_hits"], {}),
             "cred": _load_json(r["credibility_hits"], {})} for r in rows]


def _key_labels(rows: List[Dict[str, Any]], field: str) -> Dict[str, str]:
    """key → 中文名（取 marker JSON 内层的 "label"；首次见到为准，同一词表天然一致）。"""
    out: Dict[str, str] = {}
    for row in rows:
        hits_map = row[field]
        if not isinstance(hits_map, dict):
            continue
        for key, inner in hits_map.items():
            if isinstance(inner, dict) and inner.get("hits") and str(key) not in out:
                out[str(key)] = str(inner.get("label") or "")
    return out


# ---------------------------------------------------------------------------
# C3-1 话术模式库
# ---------------------------------------------------------------------------

def pattern_library(store: Any, profile_id: Optional[int] = None) -> Dict[str, Any]:
    """话术模式库：个人库里的话术特征 × 结论标签的聚合（纯读取、永不抛）。

    profile_id 非 None 时只聚合该档案工作区（v6 多 Profile 分家）；None 时全库
    （与加参数前逐位一致）。

    返回：
      total_dossiers        有结论的档案数
      with_markers          有 marker 行的档案数
      by_label              {最新结论label: 档案数}
      risk_patterns         [{"key","label","dossiers","share","by_label"}...]，
                            只统计 hits 非空的类目；share=round(dossiers/with_markers,2)
                            （with_markers=0 时 0.0）；按 dossiers 降序、key 升序
      credibility_patterns  同构（数据源 credibility_hits）
      common_pairs          同一档案同时命中（hits 非空）的 key 对，dossiers≥2 才
                            收录，按 dossiers 降序、keys 升序，取前 5
    """
    empty = {"total_dossiers": 0, "with_markers": 0, "by_label": {},
             "risk_patterns": [], "credibility_patterns": [], "common_pairs": []}
    try:
        conn = store._conn
        labels = _latest_labels(conn, profile_id)
        rows = _marker_rows(conn, profile_id)
    except Exception:  # noqa: BLE001  空库/坏库都是合法的"暂无数据"
        return empty

    total_dossiers = len(labels)
    by_label: Dict[str, int] = {}
    for label in labels.values():
        by_label[label] = by_label.get(label, 0) + 1
    with_markers = len(rows)

    def aggregate(field: str, key_label: Dict[str, str]) -> List[Dict[str, Any]]:
        dossiers_by_key: Dict[str, int] = {}
        by_label_by_key: Dict[str, Dict[str, int]] = {}
        for row in rows:
            hits_map = row[field]
            if not isinstance(hits_map, dict):
                continue
            for key, inner in hits_map.items():
                if not isinstance(inner, dict) or not inner.get("hits"):
                    continue   # hits 空的类目不进模式库
                k = str(key)
                dossiers_by_key[k] = dossiers_by_key.get(k, 0) + 1
                latest = labels.get(row["did"])
                if latest is not None:
                    bucket = by_label_by_key.setdefault(k, {})
                    bucket[latest] = bucket.get(latest, 0) + 1
        out = []
        for k, n in dossiers_by_key.items():
            out.append({"key": k, "label": key_label.get(k, ""), "dossiers": n,
                        "share": round(n / with_markers, 2) if with_markers else 0.0,
                        "by_label": by_label_by_key.get(k, {})})
        out.sort(key=lambda p: (-p["dossiers"], p["key"]))
        return out

    risk_labels = _key_labels(rows, "risk")
    risk_patterns = aggregate("risk", risk_labels)
    credibility_patterns = aggregate("cred", _key_labels(rows, "cred"))

    # common_pairs：同一档案 risk_hits 中同时 hits 非空的 key 组合
    pair_dossiers: Dict[Tuple[str, str], int] = {}
    for row in rows:
        hits_map = row["risk"]
        if not isinstance(hits_map, dict):
            continue
        keys = sorted(str(k) for k, v in hits_map.items()
                      if isinstance(v, dict) and v.get("hits"))
        for pair in itertools.combinations(keys, 2):
            pair_dossiers[pair] = pair_dossiers.get(pair, 0) + 1
    common_pairs = [{"keys": [a, b],
                     "labels": [risk_labels.get(a, ""), risk_labels.get(b, "")],
                     "dossiers": n}
                    for (a, b), n in sorted(pair_dossiers.items()) if n >= 2]
    common_pairs.sort(key=lambda p: (-p["dossiers"], p["keys"]))

    return {"total_dossiers": total_dossiers, "with_markers": with_markers,
            "by_label": by_label,
            "risk_patterns": risk_patterns, "credibility_patterns": credibility_patterns,
            "common_pairs": common_pairs[:_TOP_PAIRS]}


# ---------------------------------------------------------------------------
# C3-2 信源画像
# ---------------------------------------------------------------------------

def source_profiles(store: Any, profile_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """按域名聚合证据条目的信源画像（纯读取、永不抛；空库返回 []）。

    profile_id 非 None 时只聚合该档案工作区的证据（v6 多 Profile 分家）；
    None 时全库（逐位不变）。

    每个域名一项：tier 取该域名条目中最常见的 domain_tier（并列取字母序第一个）；
    refuted_dossiers 只数"该域名反驳条目所在档案中最新结论为 高风险/疑似"的档案
    （反驳过低风险档案不算战绩）；supporting_dossiers 同理只数最新结论为低风险的；
    qualified = refuted_dossiers ≥ QUALIFIED_MIN_REFUTES。排序：refuted_dossiers
    降序、domain 升序。url 解析不出域名的条目跳过。
    """
    try:
        conn = store._conn
        labels = _latest_labels(conn, profile_id)
        sql = ("SELECT dossier_id, url, domain, domain_tier, stance, fetched_at"
               " FROM evidence_entries")
        args: List[Any] = []
        if profile_id is not None:
            sql += (" JOIN dossiers d ON d.id = evidence_entries.dossier_id"
                    " WHERE d.profile_id = ?")
            args.append(int(profile_id))
        rows = conn.execute(sql, args).fetchall()
    except Exception:  # noqa: BLE001  空库/坏库都是合法的"暂无数据"
        return []

    agg: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        # domain 列在真实管线恒为空串，一律从 url 现算（与引擎 evidence 同口径）
        domain = normalize_host(str(r["url"] or ""))
        if not domain:
            continue
        bucket = agg.setdefault(domain, {
            "tiers": {}, "total_entries": 0, "refute_entries": 0, "support_entries": 0,
            "refute_dossiers": set(), "support_dossiers": set(), "last_seen": ""})
        bucket["total_entries"] += 1
        tier = str(r["domain_tier"] or "")
        if tier:
            bucket["tiers"][tier] = bucket["tiers"].get(tier, 0) + 1
        stance = str(r["stance"] or "")
        did = int(r["dossier_id"])
        if stance == "反驳":
            bucket["refute_entries"] += 1
            if labels.get(did) in (LABEL_HIGH, LABEL_SUSPECT):
                bucket["refute_dossiers"].add(did)
        elif stance == "支持":
            bucket["support_entries"] += 1
            if labels.get(did) == LABEL_LOW:
                bucket["support_dossiers"].add(did)
        fetched = str(r["fetched_at"] or "")
        if fetched > bucket["last_seen"]:
            bucket["last_seen"] = fetched

    out: List[Dict[str, Any]] = []
    for domain, b in agg.items():
        tiers = b["tiers"]
        if tiers:
            top = max(tiers.values())
            tier = min(t for t, n in tiers.items() if n == top)   # 并列取字母序第一个
        else:
            tier = ""
        refuted = len(b["refute_dossiers"])
        out.append({"domain": domain, "tier": tier,
                    "total_entries": b["total_entries"],
                    "refute_entries": b["refute_entries"],
                    "support_entries": b["support_entries"],
                    "refuted_dossiers": refuted,
                    "supporting_dossiers": len(b["support_dossiers"]),
                    "last_seen": b["last_seen"],
                    "qualified": refuted >= QUALIFIED_MIN_REFUTES})
    out.sort(key=lambda p: (-p["refuted_dossiers"], p["domain"]))
    return out


def qualified_source_profile(profiles: List[Dict[str, Any]],
                             min_refutes: int = QUALIFIED_MIN_REFUTES
                             ) -> Dict[str, Dict[str, int]]:
    """从画像列表里挑出"达标信源"：{domain: {"refutes": refuted_dossiers}}。

    产出形状即引擎注入（evidence.collect_evidence 的 source_profile 参数）的形状。
    """
    out: Dict[str, Dict[str, int]] = {}
    for p in profiles or []:
        try:
            n = int(p.get("refuted_dossiers", 0))
        except (TypeError, ValueError):
            continue
        domain = str(p.get("domain") or "")
        if domain and n >= min_refutes:
            out[domain] = {"refutes": n}
    return out


# ---------------------------------------------------------------------------
# C3-3 个人信息环境周报
# ---------------------------------------------------------------------------

def _zero_digest(days: int) -> Dict[str, Any]:
    """周报的全零骨架（窗口内无数据 / now 解析失败时的合法输出）。"""
    return {
        "period": {"start": "", "end": "", "days": days},
        "dossiers": {"new_count": 0, "by_input_kind": {}},
        "conclusions": {"count": 0, "by_label": {}},
        "runs": {"count": 0, "degraded_count": 0, "by_kind": {}, "requests": 0, "tokens": 0},
        "tool_runs": {"count": 0, "requests": 0, "tokens": 0},
        "patterns": {"top": []},
        "sources": {"top_refute": []},
        "heat": {"snapshot_count": 0},
        "fingerprints": {"count": 0},
        "as_of": "",
    }


def _fill_digest(conn: Any, digest: Dict[str, Any], start: str, end: str,
                 profile_id: Optional[int] = None) -> None:
    """把窗口 [start, end] 内的各域计数填进 digest（ISO 字符串直接比较，双端闭区间）。

    profile_id 非 None 时全部计数限定该档案工作区（v6 多 Profile 分家：周报是
    “我的”周报，不混别的工作区）；None 时不过滤（与加参数前逐位一致）。
    """
    args = (start, end)
    scoped: List[Any] = [start, end, int(profile_id)] if profile_id is not None else args

    row = conn.execute("SELECT COUNT(*) AS n FROM dossiers"
                       " WHERE created_at >= ? AND created_at <= ?"
                       + (" AND profile_id = ?" if profile_id is not None else ""),
                       scoped).fetchone()
    digest["dossiers"]["new_count"] = int(row["n"])
    for r in conn.execute("SELECT input_kind AS k, COUNT(*) AS n FROM dossiers"
                          " WHERE created_at >= ? AND created_at <= ?"
                          + (" AND profile_id = ?" if profile_id is not None else "")
                          + " GROUP BY input_kind", scoped):
        digest["dossiers"]["by_input_kind"][str(r["k"])] = int(r["n"])

    row = conn.execute("SELECT COUNT(*) AS n FROM conclusions c"
                       " JOIN dossiers d ON d.id = c.dossier_id"
                       " WHERE c.created_at >= ? AND c.created_at <= ?"
                       + (" AND d.profile_id = ?" if profile_id is not None else ""),
                       scoped).fetchone()
    digest["conclusions"]["count"] = int(row["n"])
    for r in conn.execute("SELECT c.label AS k, COUNT(*) AS n FROM conclusions c"
                          " JOIN dossiers d ON d.id = c.dossier_id"
                          " WHERE c.created_at >= ? AND c.created_at <= ?"
                          + (" AND d.profile_id = ?" if profile_id is not None else "")
                          + " GROUP BY c.label", scoped):
        digest["conclusions"]["by_label"][str(r["k"])] = int(r["n"])

    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(degraded), 0) AS deg,"
        " COALESCE(SUM(requests), 0) AS req, COALESCE(SUM(tokens), 0) AS tok"
        " FROM analysis_runs r JOIN dossiers d ON d.id = r.dossier_id"
        " WHERE r.created_at >= ? AND r.created_at <= ?"
        + (" AND d.profile_id = ?" if profile_id is not None else ""), scoped).fetchone()
    digest["runs"]["count"] = int(row["n"])
    digest["runs"]["degraded_count"] = int(row["deg"])
    digest["runs"]["requests"] = int(row["req"])
    digest["runs"]["tokens"] = int(row["tok"])
    for r in conn.execute("SELECT r.kind AS k, COUNT(*) AS n FROM analysis_runs r"
                          " JOIN dossiers d ON d.id = r.dossier_id"
                          " WHERE r.created_at >= ? AND r.created_at <= ?"
                          + (" AND d.profile_id = ?" if profile_id is not None else "")
                          + " GROUP BY r.kind", scoped):
        digest["runs"]["by_kind"][str(r["k"])] = int(r["n"])

    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(requests), 0) AS req,"
        " COALESCE(SUM(tokens), 0) AS tok"
        " FROM tool_runs t JOIN sessions s ON s.id = t.session_id"
        " WHERE t.created_at >= ? AND t.created_at <= ?"
        + (" AND s.profile_id = ?" if profile_id is not None else ""), scoped).fetchone()
    digest["tool_runs"]["count"] = int(row["n"])
    digest["tool_runs"]["requests"] = int(row["req"])
    digest["tool_runs"]["tokens"] = int(row["tok"])

    # patterns.top：窗口内落的 marker 行，risk_hits hits 非空的类目按档案数取前 5
    counts: Dict[str, int] = {}
    names: Dict[str, str] = {}
    for r in conn.execute("SELECT m.risk_hits AS risk_hits FROM dossier_markers m"
                          " JOIN dossiers d ON d.id = m.dossier_id"
                          " WHERE m.created_at >= ? AND m.created_at <= ?"
                          + (" AND d.profile_id = ?" if profile_id is not None else ""),
                          scoped):
        hits_map = _load_json(r["risk_hits"], {})
        if not isinstance(hits_map, dict):
            continue
        for key, inner in hits_map.items():
            if isinstance(inner, dict) and inner.get("hits"):
                k = str(key)
                counts[k] = counts.get(k, 0) + 1
                names.setdefault(k, str(inner.get("label") or ""))
    top = [{"key": k, "label": names.get(k, ""), "dossiers": n}
           for k, n in sorted(counts.items())]
    top.sort(key=lambda p: (-p["dossiers"], p["key"]))
    digest["patterns"]["top"] = top[:_TOP_PATTERNS]

    # sources.top_refute：窗口内**新建档案**的证据条目，反驳条目按域名计数前 5
    refute_counts: Dict[str, int] = {}
    for r in conn.execute(
            "SELECT e.url AS url FROM evidence_entries e"
            " JOIN dossiers d ON d.id = e.dossier_id"
            " WHERE d.created_at >= ? AND d.created_at <= ? AND e.stance = '反驳'"
            + (" AND d.profile_id = ?" if profile_id is not None else ""), scoped):
        domain = normalize_host(str(r["url"] or ""))
        if domain:
            refute_counts[domain] = refute_counts.get(domain, 0) + 1
    top_refute = [{"domain": d, "refute_entries": n} for d, n in sorted(refute_counts.items())]
    top_refute.sort(key=lambda p: (-p["refute_entries"], p["domain"]))
    digest["sources"]["top_refute"] = top_refute[:_TOP_PATTERNS]

    row = conn.execute("SELECT COUNT(*) AS n FROM attention_snapshots a"
                       " JOIN dossiers d ON d.id = a.dossier_id"
                       " WHERE a.taken_at >= ? AND a.taken_at <= ?"
                       + (" AND d.profile_id = ?" if profile_id is not None else ""),
                       scoped).fetchone()
    digest["heat"]["snapshot_count"] = int(row["n"])

    row = conn.execute("SELECT COUNT(*) AS n FROM claim_fingerprints f"
                       " JOIN dossiers d ON d.id = f.dossier_id"
                       " WHERE f.created_at >= ? AND f.created_at <= ?"
                       + (" AND d.profile_id = ?" if profile_id is not None else ""),
                       scoped).fetchone()
    digest["fingerprints"]["count"] = int(row["n"])


def weekly_digest(store: Any, now: str, days: int = 7,
                  profile_id: Optional[int] = None) -> Dict[str, Any]:
    """个人信息环境周报：窗口 [start, end]（end=now，start=now 往前 days 天，闭区间）。

    profile_id 非 None 时只统计该档案工作区（v6 多 Profile 分家）；None 时全库
    （逐位不变）。

    时间口径：ISO 字符串直接比较（库内时间戳同源于 store._now()，见模块 docstring）；
    start 用 datetime.fromisoformat(now) 减 timedelta(days=days) 再 isoformat 得到。
    now 解析失败（TypeError/ValueError）→ 返回 period 为空的全零结构，不抛。
    """
    try:
        days_n = int(days)
    except (TypeError, ValueError):
        days_n = 0
    try:
        end_dt = datetime.fromisoformat(now)
        start_dt = end_dt - timedelta(days=days_n)
    except (TypeError, ValueError):
        return _zero_digest(days_n)
    start, end = start_dt.isoformat(), end_dt.isoformat()

    digest = _zero_digest(days_n)
    digest["period"] = {"start": start, "end": end, "days": days_n}
    digest["as_of"] = str(now)
    try:
        _fill_digest(store._conn, digest, start, end, profile_id)
    except Exception:  # noqa: BLE001  SQL 层任何故障都退全零结构，周报不能崩
        zero = _zero_digest(days_n)
        zero["period"] = digest["period"]
        zero["as_of"] = digest["as_of"]
        return zero
    return digest
