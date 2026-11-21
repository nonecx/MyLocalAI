# -*- coding: utf-8 -*-
"""民间观点聚合分析：证据本搜不到权威定论时，转而分析"民间怎么说"。

联网证据本常常只有一堆私人看法、评论和自媒体文章：这种情况下"有没有辟谣"判断不了，
但"民间怎么说"本身可以分析。本模块把证据本里的私人看法按对主张的立场分成
支持 / 反驳两类（只有一类时不做两类对比），每类随机抽样深读、抽取论据，再做三项分析：

  1. 证据重合度  该论据在同类文章中出现的篇数 ÷ 该类实际抽取的篇数
  2. 矛盾        论据自身是否自相矛盾，不同论据之间是否互相矛盾
  3. 可信度      从来源、获取渠道、是否提到具体来源、是否可核实等维度打分定级

分工与包内其他模块一致：抽取/聚类/矛盾检测交给大模型，可信度的分数与等级由确定性
代码计算（大模型不参与定级），任何一步失败都记入 degraded 并继续，绝不抛异常。

工程约定：
  - search_fn / fetch_fn 的默认值在调用期解析为模块级 search_all / fetch_article，
    便于测试用 mock.patch 替换（与 evidence.collect_evidence 同一做法）。
  - chat_json 在各 LLM 函数内部延迟导入，测试可 patch mylocalai.llm.llm_client.chat_json。
  - 不把待测原文原样交给搜索引擎；只分析证据本里已经检索到的私人看法条目。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from .evidence import build_query, collect_evidence, judge_stances_llm  # noqa: F401  规格 §复用 要求复用既有设施
from mylocalai.retrieval.search import fetch_article, search_all
from mylocalai.retrieval.sources import RELIABLE_TIERS, TIER_UNKNOWN, classify_domain, normalize_host, source_kind  # noqa: F401
from mylocalai.retrieval.sources import channel_score_table

SAMPLE_PER_SIDE = 4
# 渠道权重按档名从 sources_registry.json 的 channel_score 读（换行业重命名档位不再静默失效）
CHANNEL_SCORE = channel_score_table()
OVERLAP_THRESHOLD = 0.5      # 2 字滑窗重合率，达到即视为同一论据（规则回退用）
MAX_ARGUMENTS_PER_ARTICLE = 6

SIDES = ("支持", "反驳")
_BASIS_CUES = ("数据", "研究", "论文", "通报", "实验")

# ---- 论据抽取 System Prompt（内置默认；三段结构加在开头，字段要求与 JSON 契约原样在后）----
ARGUMENTS_SYSTEM_PROMPT_BUILTIN = (
    "【角色身份】你是事实核查链的论据抽取助理，负责从民间文章里抽取支持或反对主张的论据。\n"
    "【任务目标】你为民间观点聚合分析环节达成可聚类、可定级的论据条目："
    "每条含 argument/basis/source/channel/verifiable。\n"
    "【边界】你只抽取并一句话概括论据，不给论据打分或定级（可信度由确定性代码计算）；"
    "文中没有论据时返回空数组。\n"
    "用户会给出一篇文章正文（可能来自自媒体、论坛或评论区），"
    "请抽取其中用来支持或反对某一主张的论据，每条论据给出一句话概括。\n"
    "每条论据输出以下字段：\n"
    "  argument   论据的一句话概括\n"
    "  basis      该论据依据什么（数据/个人经历/传言/专家说法等）\n"
    "  source     论据里提到的具体来源，没有则空串\n"
    "  channel    获取渠道（亲历/亲友转述/网络传言/媒体报道/官方通报）\n"
    "  verifiable 只能取 可核实 或 无法核实\n"
    f"最多输出 {MAX_ARGUMENTS_PER_ARTICLE} 条，没有论据时返回空数组。\n"
    '严格按以下 JSON 格式输出，键名必须是 arguments：\n'
    '{"arguments": [{"argument": "论据概括", "basis": "依据", "source": "", '
    '"channel": "网络传言", "verifiable": "无法核实"}]}'
)

# ---- 论据聚类 System Prompt（内置默认；三段结构加在开头，编号规则与 JSON 契约原样在后）----
CLUSTER_SYSTEM_PROMPT_BUILTIN = (
    "【角色身份】你是事实核查链的论据聚类助理，负责把跨文章抽取的编号论据按意思归组。\n"
    "【任务目标】你为民间观点聚合分析环节达成跨文章的证据重合度统计："
    "输出 clusters 编号分组（每个编号恰好出现一次）。\n"
    "【边界】你只输出编号分组，不改写、不评价论据内容；"
    "两条例据意思是否相同拿不准时，按意思不同各自成组。\n"
    "用户会给出一组带编号的论据，请把表达同一意思（同一条论据）的"
    "编号归为一组，意思不同的论据各自成组。\n"
    "编号是全局连续编号，从 0 开始；每个编号必须恰好出现一次，不得重复、不得遗漏、"
    "不得越界。\n"
    '严格按以下 JSON 格式输出，键名必须是 clusters：\n'
    '{"clusters": [[0, 2], [1]]}'
)

# ---- 矛盾检测 System Prompt（内置默认；三段结构加在开头，输出要求与 JSON 契约原样在后）----
CONTRADICTION_SYSTEM_PROMPT_BUILTIN = (
    "【角色身份】你是事实核查链的矛盾检测助理，负责发现论据之间的自相矛盾与互相矛盾。\n"
    "【任务目标】你为民间观点聚合分析环节达成矛盾清单：每条含 kind/a/b/why，"
    "供报告提示口径冲突。\n"
    "【边界】你只找矛盾并一句话说明矛盾所在，不评估论据真假或可信度；"
    "矛盾类型拿不准时归“互相矛盾”，没有矛盾时返回空数组。\n"
    "用户会给出一组带编号的论据，请找出其中的矛盾：\n"
    "  - 自相矛盾：同一条论据内部前后不一致（此时 a 与 b 填同一条论据）\n"
    "  - 互相矛盾：两条不同论据相互冲突\n"
    "每条矛盾输出 kind（自相矛盾|互相矛盾）、a（论据A概括）、b（论据B概括）、"
    "why（矛盾在哪，一句话）。没有矛盾返回空数组。\n"
    '严格按以下 JSON 格式输出，键名必须是 contradictions：\n'
    '{"contradictions": [{"kind": "互相矛盾", "a": "论据A", "b": "论据B", "why": "原因"}]}'
)

# ---- 生效值：配置里有就用配置的，否则用上面的内置默认（src/mylocalai/rumor/prompts.json） ----
from mylocalai.config import prompt_config

ARGUMENTS_SYSTEM_PROMPT = prompt_config.get_prompt(
    "opinions_arguments_system", ARGUMENTS_SYSTEM_PROMPT_BUILTIN)
CLUSTER_SYSTEM_PROMPT = prompt_config.get_prompt(
    "opinions_cluster_system", CLUSTER_SYSTEM_PROMPT_BUILTIN)
CONTRADICTION_SYSTEM_PROMPT = prompt_config.get_prompt(
    "opinions_contradiction_system", CONTRADICTION_SYSTEM_PROMPT_BUILTIN)


# ---------------------------------------------------------------------------
# 触发条件
# ---------------------------------------------------------------------------

def should_trigger(book: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """判断是否该走民间观点分析。

    规则：
      - 证据本为空 / 已 skipped / 没有 entries → 不触发；
      - entries 里存在任何 官方/权威 级别的条目 → 不触发（已有定论，无需分析民间看法）；
      - 剩下的私人看法条目中，立场为 支持/反驳 的合计 ≥ 1 → 触发；
      - 否则（只有无关/空立场）→ 不触发。
    """
    if not book or not isinstance(book, dict):
        return False, "证据本为空"
    if book.get("skipped"):
        return False, f"证据本已跳过：{book['skipped']}"
    entries = book.get("entries")
    if not entries:
        return False, "证据本没有条目"
    for e in entries:
        if isinstance(e, dict) and e.get("domain_tier") in RELIABLE_TIERS:
            return False, "已有官方或权威媒体来源，不走民间观点分析"
    opinions = sum(1 for e in entries
                   if isinstance(e, dict) and e.get("stance") in SIDES)
    if opinions >= 1:
        return True, ""
    return False, "没有可分析的私人看法"


# ---------------------------------------------------------------------------
# 分组与抽样
# ---------------------------------------------------------------------------

def split_by_stance(entries: Optional[Sequence[Dict[str, Any]]]) -> Dict[str, Any]:
    """按对主张的立场把条目分成 支持 / 反驳 两组。

    返回 {"支持": [...], "反驳": [...], "neutral": int, "single_sided": bool,
          "group_mode": "two-sided"|"single-sided"}。
    只有一类非空（或两类都空）时 single_sided=True、group_mode="single-sided"。
    """
    groups: Dict[str, List[Dict[str, Any]]] = {"支持": [], "反驳": []}
    neutral = 0
    for e in entries or []:
        s = e.get("stance") if isinstance(e, dict) else None
        if s in groups:
            groups[s].append(e)
        else:
            neutral += 1
    two_sided = bool(groups["支持"]) and bool(groups["反驳"])
    return {
        "支持": groups["支持"], "反驳": groups["反驳"],
        "neutral": neutral,
        "single_sided": not two_sided,
        "group_mode": "two-sided" if two_sided else "single-sided",
    }


def sample_articles(entries: Optional[Sequence[Dict[str, Any]]],
                    n: int = SAMPLE_PER_SIDE,
                    rng: Optional[Union[random.Random, int]] = None) -> List[Dict[str, Any]]:
    """从 entries 里随机抽 n 篇；不足 n 篇就全选。

    - rng 可传 random.Random 实例或整数 seed；缺省用系统随机源。
    - 抽完按原列表索引升序返回，保证同 seed 结果稳定可复现。
    """
    items = list(entries or [])
    if n <= 0:
        return []
    if len(items) <= n:
        return items
    r = rng if isinstance(rng, random.Random) else random.Random(rng)
    idx = sorted(r.sample(range(len(items)), n))
    return [items[i] for i in idx]


# ---------------------------------------------------------------------------
# 论据抽取
# ---------------------------------------------------------------------------

def _norm_arg(raw: Any) -> Optional[Dict[str, str]]:
    """把一条模型返回的论据规整成固定字段的字典；无有效概括则丢弃。"""
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("argument") or "").strip()
    if not text:
        return None
    ver = str(raw.get("verifiable") or "").strip()
    return {
        "argument": text[:120],
        "basis": str(raw.get("basis") or "").strip(),
        "source": str(raw.get("source") or "").strip(),
        "channel": str(raw.get("channel") or "").strip(),
        # 精确取值：只认"可核实"；"不可核实"/"无法核实"等一律归无法核实
        # （旧写法用子串判断，"不可核实"含"可核实"会被误判，实测踩过）
        "verifiable": "可核实" if ver == "可核实" else "无法核实",
        "derived": "llm",
    }


def _entry_tier(entry: Any) -> str:
    """取条目来源等级：优先 domain_tier，缺失时用 URL 现算；url 为 None 也不抛异常。"""
    if not isinstance(entry, dict):
        return TIER_UNKNOWN
    return entry.get("domain_tier") or classify_domain(str(entry.get("url") or ""))


def _entry_kind(entry: Any) -> str:
    """取条目"官方/非官方"标注；url 为 None 时返回空串而非抛异常。"""
    if not isinstance(entry, dict):
        return ""
    return entry.get("source_kind") or source_kind(str(entry.get("url") or ""))


def extract_arguments(text: str) -> Dict[str, Any]:
    """用一次大模型调用从文章正文里抽取论据。

    返回 {"arguments": [...], "error": None|错误说明}。任何失败都不抛异常，
    解析失败时返回空论据列表并在 error 里说明原因。
    """
    body = str(text or "").strip()
    if not body:
        return {"arguments": [], "error": "正文为空"}
    from mylocalai.llm.llm_client import chat_json
    user = "文章正文：\n" + body[:3000]
    try:
        obj, err = chat_json(ARGUMENTS_SYSTEM_PROMPT, user, temperature=0.0, max_tokens=800)
    except Exception as exc:  # noqa: BLE001  模型/网络异常一律降级
        return {"arguments": [], "error": f"{type(exc).__name__}: {exc}"}
    if err or not isinstance(obj, dict):
        return {"arguments": [], "error": err or "模型返回无法解析的JSON"}
    raw = obj.get("arguments")
    if not isinstance(raw, list):
        return {"arguments": [], "error": "arguments字段缺失或非数组"}
    args = [a for a in (_norm_arg(x) for x in raw) if a]
    return {"arguments": args[:MAX_ARGUMENTS_PER_ARTICLE], "error": None}


def _bigrams(text: str) -> set:
    """文本的 2 字滑窗片段集合。"""
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _overlap_ratio(a: str, b: str) -> float:
    """2 字滑窗重合率 = 公共 2 字片段数 ÷ min(片段数A, 片段数B)。任一为空返回 0。"""
    ga, gb = _bigrams(a or ""), _bigrams(b or "")
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


def fallback_cluster(claim_texts: Optional[Sequence[str]]) -> List[List[int]]:
    """纯规则聚类（LLM 聚类失败时的回退）。

    严格按规格 §三：与已有分组的代表论据 2 字滑窗重合率 ≥ OVERLAP_THRESHOLD 即并入该组，
    否则另起一组。返回编号分组的列表，如 [[0, 2], [1]]。
    """
    texts = [str(t or "") for t in (claim_texts or [])]
    clusters: List[List[int]] = []
    for i, t in enumerate(texts):
        for cl in clusters:
            if _overlap_ratio(t, texts[cl[0]]) >= OVERLAP_THRESHOLD:
                cl.append(i)
                break
        else:
            clusters.append([i])
    return clusters


# ---------------------------------------------------------------------------
# 论据合并（跨文章的全局聚类 + 重合度）
# ---------------------------------------------------------------------------

def _valid_clusters(clusters: Any, n: int) -> bool:
    """校验聚类编号：必须是二维非空数组，0..n-1 每个数恰好出现一次。"""
    if not isinstance(clusters, list) or not clusters:
        return False
    seen: List[int] = []
    for cl in clusters:
        if not isinstance(cl, list) or not cl:
            return False
        for x in cl:
            if isinstance(x, bool) or not isinstance(x, int) or not (0 <= x < n):
                return False
            seen.append(x)
    return sorted(seen) == list(range(n))


def _llm_cluster(texts: List[str], stance: str) -> Tuple[Optional[List[List[int]]], Optional[str]]:
    """一次大模型调用做全局聚类；返回 (clusters, 错误)。失败时 clusters 为 None。"""
    from mylocalai.llm.llm_client import chat_json
    lines = [f"[{i}] {t}" for i, t in enumerate(texts)]
    user = f"立场：{stance}\n论据列表：\n" + "\n".join(lines)
    try:
        obj, err = chat_json(CLUSTER_SYSTEM_PROMPT, user, temperature=0.0, max_tokens=800)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    if err or not isinstance(obj, dict):
        return None, err or "模型返回无法解析的JSON"
    clusters = obj.get("clusters")
    if not _valid_clusters(clusters, len(texts)):
        return None, "聚类编号越界/重复/遗漏"
    return [list(c) for c in clusters], None


def _dedup(values: Sequence[Any]) -> List[str]:
    """去重并保持出现顺序，丢弃空值。"""
    out: List[str] = []
    for v in values:
        s = str(v or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def merge_arguments(per_article: Optional[Sequence[Dict[str, Any]]], stance: str,
                    llm: bool = True) -> Dict[str, Any]:
    """把某一类所有文章的论据做一次全局聚类，得到合并后的论据与重合度。

    编号跨文章连续（从 0 开始）。LLM 返回的 clusters 任一编号越界/重复/遗漏即判无效，
    回退到 fallback_cluster 规则聚类。同一篇文章里重复出现的同一论据只算 1 篇。
    llm=False 时完全不调用大模型，直接走规则聚类（离线/无密钥部署用）。

    返回 schema（规格 §三）：items 每项含 argument / article_count / article_ratio /
    articles / sources / channels；另附 members（该聚类的论据明细），供可信度打分内部使用，
    但 score_credibility 不依赖它——缺失时会按代表文本回推，保证按文档 schema 构造也能用。
    """
    articles = [a for a in (per_article or []) if isinstance(a, dict)]
    flat: List[Tuple[int, Dict[str, Any]]] = []
    for ai, art in enumerate(articles):
        for a in (art.get("arguments") or []):
            if isinstance(a, dict):
                flat.append((ai, a))
    if not flat:
        return {"items": [], "method": "rule", "error": None}

    texts = [str(a.get("argument") or "") for _, a in flat]
    if not llm:
        clusters, method, error = fallback_cluster(texts), "rule", None
    else:
        clusters, err = _llm_cluster(texts, stance)
        if clusters is None:
            clusters, method, error = fallback_cluster(texts), "rule", (err or "聚类失败，回退规则")
        else:
            method, error = "llm", None

    total = len(articles)
    items: List[Dict[str, Any]] = []
    for cl in clusters:
        members = [flat[i][1] for i in cl]
        arts = sorted({flat[i][0] for i in cl})
        cnt = len(arts)
        items.append({
            "argument": str(flat[cl[0]][1].get("argument") or ""),
            "article_count": cnt,
            "article_ratio": round(cnt / total, 2) if total else 0.0,
            "articles": arts,
            "sources": _dedup([m.get("source") for m in members]),
            "channels": _dedup([m.get("channel") for m in members]),
            "members": members,
        })
    items.sort(key=lambda it: (-it["article_count"], it["articles"][0] if it["articles"] else 0))
    return {"items": items, "method": method, "error": error}


# ---------------------------------------------------------------------------
# 矛盾检测
# ---------------------------------------------------------------------------

def _contradiction_prompt(articles: List[Dict[str, Any]],
                          items: List[Dict[str, Any]]) -> str:
    """拼矛盾检测的输入：先按篇给出论据原文（含同一篇内的多条，用来看自相矛盾），
    再给出合并后的论据（编好号，用于看互相矛盾）。"""
    lines: List[str] = ["【按文章列出的论据原文】"]
    for ai, art in enumerate(articles):
        args = [str(a.get("argument") or "") for a in (art.get("arguments") or [])]
        if args:
            lines.append(f"文章{ai}：" + "；".join(args))
    lines.append("")
    lines.append("【合并后的论据（编号）】")
    lines += [f"[{i}] {it.get('argument', '')}" for i, it in enumerate(items)]
    return "\n".join(lines)


def find_contradictions(per_article: Optional[Sequence[Dict[str, Any]]],
                        merged: Optional[Dict[str, Any]],
                        llm: bool = True) -> Dict[str, Any]:
    """用一次大模型调用检测论据的自相矛盾与互相矛盾。失败返回空列表 + error。

    输入同时给"按文章列出的论据原文"与"合并后的编号论据"——只看合并后的代表句无法发现
    同一篇文章内部前后不一致（自相矛盾）。llm=False 时完全不调用大模型。
    """
    articles = [a for a in (per_article or []) if isinstance(a, dict)]
    items = (merged or {}).get("items") or []
    if not items:
        return {"contradictions": [], "error": None}
    if not llm:
        return {"contradictions": [], "error": None}
    from mylocalai.llm.llm_client import chat_json
    user = _contradiction_prompt(articles, items)
    try:
        obj, err = chat_json(CONTRADICTION_SYSTEM_PROMPT, user, temperature=0.0, max_tokens=800)
    except Exception as exc:  # noqa: BLE001
        return {"contradictions": [], "error": f"{type(exc).__name__}: {exc}"}
    if err or not isinstance(obj, dict):
        return {"contradictions": [], "error": err or "模型返回无法解析的JSON"}
    raw = obj.get("contradictions")
    if not isinstance(raw, list):
        return {"contradictions": [], "error": "contradictions字段缺失或非数组"}
    out: List[Dict[str, str]] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        a = str(c.get("a") or "").strip()
        b = str(c.get("b") or "").strip()
        if not a and not b:
            continue
        kind = str(c.get("kind") or "").strip()
        if kind not in ("自相矛盾", "互相矛盾"):
            kind = "互相矛盾"
        out.append({"kind": kind, "a": a, "b": b or a,
                    "why": str(c.get("why") or "").strip()})
    return {"contradictions": out, "error": None}


# ---------------------------------------------------------------------------
# 可信度（分数与等级由代码计算，大模型不参与定级）
# ---------------------------------------------------------------------------

def _has_basis_cue(basis: str) -> bool:
    return any(c in (basis or "") for c in _BASIS_CUES)


def _level_of(score: float) -> str:
    if score >= 0.66:
        return "高"
    if score >= 0.4:
        return "中"
    return "低"


def _cluster_args(it: Dict[str, Any], articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """取出一条合并论据对应的论据明细。

    优先用内部字段 members；若调用方只按规格 schema（argument/article_count/
    article_ratio/articles/sources/channels）构造传入，members 缺失时按代表文本从
    对应文章的论据里回推，保证 score_credibility 不依赖未写进规格的字段。
    """
    members = [m for m in (it.get("members") or []) if isinstance(m, dict)]
    if members:
        return members
    rep = str(it.get("argument") or "")
    found: List[Dict[str, Any]] = []
    for i in (it.get("articles") or []):
        if not (isinstance(i, int) and 0 <= i < len(articles)):
            continue
        for a in (articles[i].get("arguments") or []):
            if isinstance(a, dict) and str(a.get("argument") or "") == rep:
                found.append(a)
    return found


def score_credibility(per_article: Optional[Sequence[Dict[str, Any]]],
                      merged: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对每条合并后的论据按代码规则打分定级，返回按 score 降序的列表。

    score = 渠道分/3.0*0.6 + 来源分*0.25 + 可验分*0.15，归一化到 0-1：
      - 渠道分：该论据来源文章里最高的 CHANNEL_SCORE（按 domain_tier 取最大）
      - 来源分：论据提到具体来源（source 非空，或 basis 含 数据/研究/论文/通报/实验）→ 1.0
      - 可验分：verifiable == "可核实" → 1.0
    等级：score ≥ 0.66 高；≥ 0.4 中；其余 低。reasons 逐项列出加减分原因。
    """
    articles = [a for a in (per_article or []) if isinstance(a, dict)]
    items = (merged or {}).get("items") or []
    # 档名可能被配置改名，取权重一律走 .get 兜底，别用可能不存在的字面量键
    unknown_weight = CHANNEL_SCORE.get(TIER_UNKNOWN, 0.5)
    out: List[Dict[str, Any]] = []
    for it in items:
        idxs = [i for i in (it.get("articles") or [])
                if isinstance(i, int) and 0 <= i < len(articles)]
        tiers = [_entry_tier(articles[i]) for i in idxs]
        if tiers:
            best_tier = max(tiers, key=lambda t: CHANNEL_SCORE.get(t, unknown_weight))
        else:
            best_tier = TIER_UNKNOWN
        chan = CHANNEL_SCORE.get(best_tier, unknown_weight)

        kind = ""
        for i in idxs:
            if _entry_tier(articles[i]) == best_tier:
                kind = _entry_kind(articles[i])
                break

        members = _cluster_args(it, articles)
        # 该聚类里是否混入了规则兜底论据（标题首句，不是模型从正文读出的真实论据）：
        # 混入时在 reasons 里明说，避免把"标题被当成论据"当成模型抽取的论据来解读。
        rule_derived = sum(1 for m in members if m.get("derived") == "rule")
        source_msg = ""
        for m in members:
            if m.get("source"):
                source_msg = f"论据提到的来源：{m['source']}"
                break
            if _has_basis_cue(m.get("basis")):
                source_msg = f"论据依据含可查证线索（{m['basis']}）"
                break
        if not source_msg and it.get("sources"):
            source_msg = "论据提到的来源：" + "、".join(str(s) for s in it["sources"][:3])
        src_score = 1.0 if source_msg else 0.0
        ver_score = 1.0 if any(m.get("verifiable") == "可核实" for m in members) else 0.0

        score = round(chan / 3.0 * 0.6 + src_score * 0.25 + ver_score * 0.15, 3)
        reasons = [
            f"渠道 {best_tier} 记 {chan}/3.0",
            source_msg or "论据未提具体来源",
            "标注为可核实" if ver_score else "标注为无法核实",
            f"被 {it.get('article_count', 0)} 篇抽样文章提及",
        ]
        if rule_derived and rule_derived == len(members):
            reasons.append("该论据取自文章标题（规则兜底），非大模型从正文抽取")
        elif rule_derived:
            reasons.append(f"其中 {rule_derived} 条为标题兜底论据，其余为大模型抽取")
        out.append({
            "argument": it.get("argument", ""),
            "level": _level_of(score),
            "score": score,
            "channel": best_tier,
            "source_kind": kind,
            "reasons": reasons,
        })
    out.sort(key=lambda r: -r["score"])
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _deg(url: Any, reason: str) -> Dict[str, str]:
    """构造一条降级记录：既带原因，也带出问题的 URL（规格 §六.3 要求把失败文章 URL 记入 degraded）。"""
    return {"url": str(url or ""), "reason": reason}


def _rule_arguments(source_text: str, title: str = "") -> List[Dict[str, str]]:
    """规则兜底论据：LLM 抽取失败时，用标题/正文首句当作一条论据。

    结果带 derived="rule" 标记：这类"论据"其实是标题首句，不是模型从正文里读出来的，
    下游（重合度/可信度）与报告据此可以区分，不会把它当模型抽取的论据。
    """
    base = (title or "").strip() or re.sub(r"\s+", " ", str(source_text or "")).strip()
    if not base:
        return []
    base = re.split(r"[。！？!?\n]", base)[0].strip() or base.strip()
    return [{"argument": base[:60], "basis": "", "source": "",
             "channel": "未知", "verifiable": "无法核实", "derived": "rule"}]


def _read_article(entry: Dict[str, Any], fetch_fn: Callable[[str], Any],
                  degraded: List[Dict[str, str]]) -> Dict[str, str]:
    """取一篇文章的正文；抓取失败或正文为空时用 title + snippet 兜底。"""
    url = entry.get("url") or ""
    title = entry.get("title", "") or ""
    try:
        art = fetch_fn(url) if url else None
    except Exception as exc:  # noqa: BLE001  抓取异常只是降级
        art = None
        degraded.append(_deg(url, f"抓取异常: {type(exc).__name__}"))
    if isinstance(art, dict) and art.get("text"):
        return {"read_from": "全文", "source_text": str(art["text"]),
                "title": title or str(art.get("title") or "")}
    degraded.append(_deg(url, "抓取失败，改用摘要兜底"))
    text = f"{title}。{entry.get('snippet', '') or ''}".strip("。 ")
    return {"read_from": "摘要", "source_text": text, "title": title}


def _article_arguments(entry: Dict[str, Any], source_text: str, title: str,
                       llm: bool, degraded: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """抽一篇的论据：LLM 抽取失败或无结果时回退规则论据。"""
    if llm:
        try:
            res = extract_arguments(source_text)
        except Exception as exc:  # noqa: BLE001
            res = {"arguments": [], "error": f"{type(exc).__name__}: {exc}"}
        if res.get("error"):
            degraded.append(_deg(entry.get("url"), f"论据提取失败: {res['error']}"))
        if res.get("arguments"):
            return res["arguments"]
    return _rule_arguments(source_text, title)


def collect_opinion_analysis(
    text: str,
    evidence_book: Optional[Dict[str, Any]] = None,
    sample_n: int = SAMPLE_PER_SIDE,
    seed: Optional[int] = None,
    search_fn: Optional[Callable[[str, int], Dict[str, Any]]] = None,
    fetch_fn: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    llm: bool = True,
) -> Dict[str, Any]:
    """民间观点聚合分析主流程。

    evidence_book 为 None 时自己调 collect_evidence；search_fn/fetch_fn 在调用期解析为
    模块级 search_all/fetch_article（不写成默认参数值，测试才能 mock 模块属性）。
    llm=False 时全程不调用大模型（论据抽取、聚类、矛盾检测都走规则/留空）。
    任何一步失败都记入 degraded 并继续，绝不抛异常。

    注意（llm=False 的固有局限，不改变）：证据本的立场判定此时走 evidence 的规则回退，
    而该规则要求可靠来源才给"支持/反驳"，自媒体/UGC 条目一律判"无关"并被过滤
    （evidence.py:281-283）——因此自己采集证据本时，纯私人看法在 --no-llm 下往往整本
    被过滤成空而不触发。要分析民间看法请传入已完成立场判定的 evidence_book，或保持 llm=True。
    """
    search_fn = search_fn or search_all
    fetch_fn = fetch_fn or fetch_article
    result: Dict[str, Any] = {
        "triggered": False, "skip_reason": "", "group_mode": "",
        "groups": {"支持": [], "反驳": []},
        "sampled": {"支持": [], "反驳": []},
        "per_article": [], "per_group": {}, "degraded": [], "error": None,
    }

    # 1. 证据本。自己采集时把 llm 透传给 collect_evidence：llm=False 时立场判定
    #    也走规则、不打大模型（否则 --no-llm 仍会打一次立场判定 API）。
    book: Optional[Dict[str, Any]] = evidence_book
    if book is None:
        try:
            book = collect_evidence(text, search_fn=search_fn, fetch_fn=fetch_fn, llm_judge=llm)
        except Exception as exc:  # noqa: BLE001  collect_evidence 本身不抛，此处兜底
            result["skip_reason"] = "证据本获取异常"
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
    if not isinstance(book, dict):
        result["skip_reason"] = "证据本不可用"
        return result

    # 2. 触发判定
    ok, reason = should_trigger(book)
    if not ok:
        result["skip_reason"] = reason
        return result
    if book.get("error"):
        result["degraded"].append(_deg("", f"证据本: {book['error']}"))
    if book.get("llm_error"):
        result["degraded"].append(_deg("", f"证据本立场判定: {book['llm_error']}"))
    if not llm:
        result["degraded"].append(_deg("", "未启用大模型：论据抽取与聚类走规则，不做矛盾检测"))

    # 3. 分组 → 抽样 → 逐篇读正文 + 抽论据
    split = split_by_stance(book.get("entries") or [])
    result["group_mode"] = split["group_mode"]
    rng = random.Random(seed)
    per_article_all: List[Dict[str, Any]] = []
    for key in SIDES:
        group = split.get(key) or []
        if not group:
            # 该侧无条目：仍给出形状一致的空结果，消费方按 res["per_group"]["反驳"] 取值不炸
            result["per_group"][key] = {
                "overlap": {"items": [], "method": "rule", "error": None},
                "contradictions": {"contradictions": [], "error": None},
                "credibility": [],
            }
            continue
        result["groups"][key] = [e.get("url") or "" for e in group]
        sampled = sample_articles(group, n=sample_n, rng=rng)
        result["sampled"][key] = [e.get("url") or "" for e in sampled]

        group_articles: List[Dict[str, Any]] = []
        for e in sampled:
            art = _read_article(e, fetch_fn, result["degraded"])
            args = _article_arguments(e, art["source_text"], art["title"], llm, result["degraded"])
            group_articles.append({
                "url": e.get("url") or "", "title": e.get("title", ""),
                "stance": e.get("stance", ""),
                "domain_tier": _entry_tier(e),
                "source_kind": _entry_kind(e),
                "arguments": args, "read_from": art["read_from"],
            })
        per_article_all.extend(group_articles)

        # 4. 每类分别做 重合度 / 矛盾 / 可信度
        merged = merge_arguments(group_articles, key, llm=llm)
        if merged.get("error"):
            result["degraded"].append(_deg("", f"{key}组证据重合度：{merged['error']}"))
        contra = find_contradictions(group_articles, merged, llm=llm)
        if contra.get("error"):
            result["degraded"].append(_deg("", f"{key}组矛盾检测：{contra['error']}"))
        cred = score_credibility(group_articles, merged)
        result["per_group"][key] = {
            "overlap": merged, "contradictions": contra, "credibility": cred,
        }

    result["per_article"] = per_article_all
    result["triggered"] = True
    return result


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def _format_report(res: Dict[str, Any]) -> str:
    """人类可读的中文摘要。"""
    if not res.get("triggered"):
        return "【民间观点分析】未触发：" + (res.get("skip_reason") or "无可分析内容")
    mode_note = {"two-sided": "支持 / 反驳 两类立场对比",
                 "single-sided": "仅单类立场，不做两类对比"}
    lines = ["【民间观点聚合分析】分组模式：" + res.get("group_mode", "")
             + f"（{mode_note.get(res.get('group_mode'), '')}）"]
    for key in SIDES:
        if key in res.get("groups", {}):
            lines.append(f"· {key}立场：共 {len(res['groups'][key])} 篇，"
                         f"抽样 {len(res.get('sampled', {}).get(key, []))} 篇")
    for key, grp in (res.get("per_group") or {}).items():
        lines.append("")
        lines.append(f"■ {key}组")
        ov = grp.get("overlap") or {}
        lines.append(f"  证据重合度（方法 {ov.get('method')}）：")
        items = ov.get("items") or []
        if not items:
            lines.append("    （无论据）")
        for it in items:
            lines.append(f"    {it['article_count']}篇 / 重合率 {it['article_ratio']:.2f}："
                         f"{it['argument'][:40]}")
        cons = (grp.get("contradictions") or {}).get("contradictions") or []
        if not cons:
            lines.append("  矛盾：未发现")
        else:
            lines.append("  矛盾：")
            for c in cons:
                lines.append(f"    [{c['kind']}] {c['a'][:24]} ⇔ {c['b'][:24]}：{c['why']}")
        lines.append("  可信度：")
        cred = grp.get("credibility") or []
        if not cred:
            lines.append("    （无论据）")
        for c in cred:
            lines.append(f"    {c['level']}（{c['score']}）{c['argument'][:30]}｜"
                         + "；".join(c["reasons"]))
    if res.get("degraded"):
        lines.append("")
        lines.append("⚠️ 降级：")
        for d in res["degraded"]:
            if isinstance(d, dict):
                url = f"（{d['url']}）" if d.get("url") else ""
                lines.append(f"  · {d.get('reason', '')}{url}")
            else:
                lines.append(f"  · {d}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口：--json 文本 | --file 路径，可选 --seed / --sample / --no-llm。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="python -m mylocalai.rumor.opinions",
        description="民间观点聚合分析：把私人看法按立场分组，抽取并比对论据")
    parser.add_argument("text", nargs="?", help="待分析文本")
    parser.add_argument("--file", metavar="路径", help="从 UTF-8 文件读取待分析文本")
    parser.add_argument("--json", action="store_true", help="输出完整结构化 JSON 而非文字摘要")
    parser.add_argument("--seed", type=int, default=None, help="抽样随机种子（同种子可复现）")
    parser.add_argument("--sample", type=int, default=SAMPLE_PER_SIDE, help="每类抽样篇数")
    parser.add_argument("--no-llm", action="store_true", help="不调用大模型，仅规则聚类")
    args = parser.parse_args(argv)

    if args.file:
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except (OSError, ValueError, UnicodeError) as exc:
            # 非 UTF-8 编码（GBK/ANSI 文本）抛的是 UnicodeDecodeError（ValueError 子类，不是 OSError），
            # 目录、权限等抛 OSError——都按普通错误处理，不抛异常。
            print(f"读取文件失败（需 UTF-8 编码的文本文件）：{exc}", file=sys.stderr)
            return 2
    elif args.text:
        text = args.text
    else:
        parser.print_usage(sys.stderr)
        print("错误：需要提供待分析文本（位置参数或 --file）。", file=sys.stderr)
        return 2

    res = collect_opinion_analysis(text, sample_n=args.sample, seed=args.seed,
                                   llm=not args.no_llm)
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else _format_report(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
