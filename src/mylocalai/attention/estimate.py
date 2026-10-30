# -*- coding: utf-8 -*-
"""关注度（Attention）参数估计：从一次真实检索把话题转成公式的三个输入。

`attention.py` 只负责「给定 x/y/z 算 E」，本模块负责**从可核验的检索结果里估出 x/y/z**。
两者刻意分开：估参数可以引入启发式，算分数必须是纯确定性。

三个输入的取证口径（与 docs/关注度评估报告-2026-10-26.md 第六节一致）：

* **x（文章数量）**：`search_all` 返回的**按 URL 去重后的独立文章数**。这是可核验条目数，
  是下限而不是全网文章总数——微博/知乎/腾讯新闻评论区需登录或 403，检索后端覆盖不到。
  检索不到结果时 `x = 0`，调用方应把 `quality` 降级为 "missing"。
* **y（文章阅读量）**：**没有任何站点公开文章级精确阅读量**（搜狐文末「阅读 ()」为空、
  人民网文章页的 222 是模板占位）。因此这里只能给**代理值**，绝不伪装成阅读量：
  从标题与摘要里抽取公开的中文数量词（「阅读量 4831.6 万」「热度 1200 万」等），
  取其中最大的一个作为量级代理；抽不到就退化为按检索结果数给出的保守代理
  （每篇 1e5 量级），并明确标注 `estimated=True`。
* **z（文章留言量）**：同样没有全平台留言总量，取**单篇最大值**作为代表值，
  来自摘要里公开的「N 跟贴」「N 评论」「N 条留言」等字样；抽不到填 0。

三个口径全部带 `estimated` 标记，前端必须原样展示「代理/估计」，不能让用户
把 E 当成「有多少人阅读」的绝对结论。

**`estimated` 恒为 True**：即使 y/z 都从页面抽到了数值，它们仍是代理口径
（摘要里的「阅读量」可能是平台热度指数，且只覆盖检索命中的那几篇），
不是全网文章总数 / 真实阅读量 / 全网留言总量。`quality` 才区分数据成色：
`exact` 表示 y/z 都有页面公开值可查，`estimated` 表示有兜底成分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 中文数量词 → 倍率。只认「万/亿」两级，其余（千、百万）在中文站点里通常写作 3.2万/120万，
# 不需要为「千」单独设档（千级阅读在公开摘要里极少出现，且会被万级覆盖）。
_UNIT_SCALE = {"万": 1e4, "亿": 1e8, "w": 1e4, "W": 1e4}

# 「数字 + 可选单位」的通用片段。允许 "4,831.6万" / "12.5 万" / "1200万" / "3.2w"
_NUM = r"(\d+(?:\.\d+)?)\s*(万|亿|w|W)?"

# 关键词与数字之间允许的间隔字符。**刻意排除数字本身**（否则会把 "22小时前" 里的 22
# 误当成紧跟其后的数值），并排除常见的时间/单位词，避免 "18小时前 - 286" 这类
# 跨片段误配：真实检索摘要里最常见的假阳性就是把「N小时前」读成「N 条评论」。
_GAP = r"[^\d\n]{0,6}?"

# 阅读量类关键词：按优先级排列，先匹配更明确的「阅读量/阅读」。
_READ_HINTS = ("阅读量", "阅读数", "浏览量", "浏览次数", "阅读", "点击量", "热度")

# 留言量类关键词。注意「评论」要排除「评论区」这类导航字样，故要求后面紧跟数字。
_COMMENT_HINTS = ("跟贴数", "跟贴", "跟帖", "评论数", "留言数", "评论", "留言", "讨论量", "讨论")

# 关键词后面不该出现的词：出现即说明这不是"关键词+数值"的组合，而是别的句子。
# 例如 "22小时前"、"3天前"、"2026年10月18日" 都是时间表述，不是评论数。
_TIME_WORDS = ("小时前", "分钟前", "天前", "秒前", "年前", "月前",
               "年", "月", "日", "周前")


@dataclass
class AttentionEstimate:
    """一次话题关注度参数估计的结果。

    x/y/z 是公式的三个输入；`quality` 取值：
      * "exact"     —— 至少 y 或 z 有一个是从页面文本抽到的公开数值
      * "estimated" —— 全靠兜底代理（没抽到任何公开数值）
      * "missing"   —— 检索没返回任何结果，x=0
    `estimated=True` 表示三个输入里至少一个是代理/推断值，不是页面公开的精确值。
    """

    x: int
    y: float
    z: float
    quality: str
    estimated: bool
    query: str = ""
    article_count: int = 0
    sources: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "quality": self.quality,
            "estimated": self.estimated,
            "query": self.query,
            "article_count": self.article_count,
            "sources": self.sources[:12],
            "notes": self.notes,
        }

def _to_number(text: str) -> Optional[float]:
    """把 "4,831.6万" / "12.5 万" / "1200" / "3.2w" 转成 float；解析不出返回 None。"""
    m = re.search(_NUM, text or "")
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None
    unit = m.group(2)
    return value * _UNIT_SCALE.get(unit, 1.0) if unit else value


def _is_time_phrase(gap: str, tail: str = "") -> bool:
    """关键词与数字之间的片段是否是时间表述（"22小时前"、"3天前" 等）。

    真实检索摘要最常见的假阳性有两类，都要挡掉：
    1. 数字本身就是时间：「22小时前」「3天前」「18小时前 - 286 跟贴」——
       关键词后的间隔里带时间词。
    2. 数字后面紧跟时间词：「讨论 3天前」里的 3 被「讨论」关键词捕获，
       但 3 的真实身份是天数。这种要看向右的 tail。
    """
    if any(w in gap for w in _TIME_WORDS):
        return True
    return any(tail.startswith(w) for w in _TIME_WORDS)


def _scan(texts: List[str], hints: tuple) -> Optional[float]:
    """在若干段文本里找「关键词 + 数值」组合，取最大值。

    只认紧邻组合，且排除时间表述造成的假阳性；正文里的孤立大数
    （"营收 3 亿元"）不会被误抓，因为它不在任何关键词后面。

    同时支持中文里"数值在前"的写法（「287 条跟贴」「890 万浏览」）：
    这类语序在摘要里同样常见，漏掉会系统性低估 z。
    """
    best: Optional[float] = None

    def offer(value: Optional[float]) -> None:
        nonlocal best
        if value and value > 0 and (best is None or value > best):
            best = value

    for text in texts:
        if not text:
            continue
        for hint in hints:
            # 写法一：关键词 + 数值（"阅读量 4831.6万" / "跟贴 287"）
            for m in re.finditer(re.escape(hint) + _GAP + _NUM, text):
                gap = m.group(0)[len(hint):]
                tail = text[m.end():m.end() + 6]
                if _is_time_phrase(gap, tail):
                    continue
                offer(_to_number(m.group(0)))
            # 写法二：数值 + 量词 + 关键词（"287 条跟贴" / "890 万次浏览"）
            for m in re.finditer(_NUM + r"\s*[条个次篇回]?\s*" + re.escape(hint), text):
                tail = text[m.end():m.end() + 6]
                if any(tail.startswith(w) for w in _TIME_WORDS):
                    continue
                offer(_to_number(m.group(0)))
    return best


def _extract_reads(texts: List[str]) -> Optional[float]:
    """从若干段文本里抽公开的阅读量/热度数值，取最大值。"""
    return _scan(texts, _READ_HINTS)


def _extract_comments(texts: List[str]) -> Optional[float]:
    """从若干段文本里抽公开的评论/跟贴/留言数，取最大值（单篇代表值口径）。"""
    return _scan(texts, _COMMENT_HINTS)


def estimate_from_search(query: str, results: List[Any],
                         per_article_reads: float = 1e5) -> AttentionEstimate:
    """把一次检索结果转成关注度公式的三个输入。

    参数:
        query:            检索词（原样回显，便于用户核对）
        results:          `mylocalai.retrieval.search.search_all` 的 results 列表，
                          元素需有 url/title/snippet 属性（或同名键的 dict）
        per_article_reads: 抽不到公开阅读量时，每篇文章假定的阅读量级（保守代理）

    返回:
        AttentionEstimate。**绝不抛异常**——估不到就降级，绝不阻塞页面。
    """
    items: List[Dict[str, Any]] = []
    for r in results or []:
        if isinstance(r, dict):
            items.append(r)
        else:
            items.append({
                "url": getattr(r, "url", ""),
                "title": getattr(r, "title", ""),
                "snippet": getattr(r, "snippet", "") or "",
            })

    # x：按 URL 去重后的独立文章数（检索后端可能返回重复链接）
    seen = set()
    for it in items:
        url = str(it.get("url") or "").strip()
        if url:
            seen.add(url.rstrip("/").lower())
    x = len(seen)

    texts = [f"{it.get('title') or ''} {it.get('snippet') or ''}" for it in items]
    reads = _extract_reads(texts)
    comments = _extract_comments(texts)

    notes: List[str] = []
    sources: List[str] = []
    for it in items:
        url = str(it.get("url") or "").strip()
        if url and url not in sources:
            sources.append(url)

    if x == 0:
        # 检索完全没结果：x/y/z 全给 0，quality=missing，让调用方明说"取不到"
        return AttentionEstimate(
            x=0, y=0.0, z=0.0, quality="missing", estimated=True, query=query,
            article_count=0, sources=sources,
            notes=["检索未返回任何结果，关注度参数按缺失处理（x/y/z 均为 0）"],
        )

    y = reads if reads is not None else per_article_reads * x
    if reads is not None:
        notes.append(f"y={y:g} 取自检索摘要里公开的阅读量/热度数值（量级代理，非精确阅读量）")
    else:
        notes.append(
            f"摘要未公开阅读量，y 按「每篇 {per_article_reads:g} × {x} 篇」兜底估计；"
            "没有任何站点公开文章级精确阅读量，此值只能作量级代理")

    z = comments if comments is not None else 0.0
    if comments is None:
        notes.append("摘要未公开评论数，z 记为 0（缺失，不等于真实留言量为 0）")

    # 注意：即使 y 和 z 都从页面抽到了，它们仍然是**代理口径**而非精确阅读量——
    # 摘要里的「阅读量」可能是平台热度指数，且只覆盖检索命中的那几篇。
    # 所以 estimated 恒为 True（除非未来接入了真正精确的阅读量来源），
    # 前端据此一律标注「代理 / 估计值」，不让用户把 E 当成绝对结论。
    estimated = True
    quality = "exact" if (reads is not None and comments is not None) else "estimated"
    notes.append("x/y/z 均为可核验口径的代理值，不是全网文章总数/真实阅读量/全网留言总量")

    return AttentionEstimate(
        x=x, y=float(y), z=float(z), quality=quality, estimated=estimated,
        query=query, article_count=x, sources=sources, notes=notes,
    )
