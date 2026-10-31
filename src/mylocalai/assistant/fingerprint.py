# -*- coding: utf-8 -*-
"""声明指纹纯函数（M3-1，工作指导 §2.5/§6 M3-1）：谣言库沉淀 + 相似说法"先问再查"。

四条纯函数，**零 IO、零 LLM、永不抛异常**（§0 铁律：指纹是"秒回"增益层，
任何输入（None/数字/写坏的行）最坏也只是"不命中"，绝不挡住正常核查）：

  - normalize_claim：把一条声明规整成可比对的规范形（NFKC/小写/去标点空白/全半角/
    去"网传|据说|听说|曝"引导前缀）——同一谣言换个引导词或标点传入，不该被当成新声明；
  - shingles：字符 k-shingle 的稳定哈希集合（内置 hash() 每进程随机化、落库后跨进程
    不可复现，必须用 blake2b 稳定哈希）；
  - jaccard：两个 shingle 集的 Jaccard 相似度（§6 M3-1 的相似口径）；
  - best_match：从候选行里挑 Jaccard 最高且达阈值的最佳命中（参考实现核；生产路径
    store.find_fingerprints 走同口径 _fp_* 副本——本模块是口径的权威定义与对拍基准）。

注意（依赖方向）：dossier 包禁 import assistant（test_import_rules），dossier/store.py
的 find_fingerprints 有一份**逐字节同口径**的最小副本（_fp_* 前缀），两处一致性由
tests/test_fingerprint.py 的对拍用例锁定，改动必须两处同步。
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["DEFAULT_SHINGLE_SIZE", "normalize_claim", "shingles", "jaccard", "best_match"]

#: 缺省 shingle 窗口（与 assistant_rules.json 的 fingerprint.shingle_size 同值）。
DEFAULT_SHINGLE_SIZE = 4

#: 引导前缀（剥 rumor 话术包装，只留声明本体；**按长度降序**排列——"据网传"必须
#: 先于"网传"尝试，否则"据网传…"只剥一半）。规则（§6 M3-1）：谣言消息常以
#: "网传/据说/听说/曝"开头，同一条谣言换引导词、换标点传入应判为同一声明。
#: 只剥**声明开头**的引导词（句中的"听说"是内容的一部分，不动）；剥空保护见
#: normalize_claim 内部。
_LEAD_PREFIXES: Tuple[str, ...] = tuple(sorted(
    ("网传", "据网传", "据传", "据说", "听说", "惊爆", "曝"), key=len, reverse=True))

#: 引导前缀最多连剥层数（"网传曝据说…"这类堆叠；3 层足够，防恶意超长前缀拖慢）。
_MAX_PREFIX_STRIPS = 3


def normalize_claim(text: Any) -> str:
    """把一条声明规整成可比对的规范形。永不抛：非字符串输入按空串处理。

    规则（按序）：
      1. NFKC 归一化：全角字母数字/符号折半角（"１２３"→"123"），兼容形收敛；
      2. 小写：英文说法大小写不敏感；
      3. 去标点/空白/控制字符：只保留字母与数字（含 CJK，str.isalnum 口径）——
         "自来水 有毒！"与"自来水有毒"是同一声明；
      4. 循环剥声明开头的引导前缀（网传/据说/听说/曝…，见 _LEAD_PREFIXES）；
         剥后为空则不剥（"网传"两个字的声明剥完就没了，如实保留原文）。
    幂等：对已规整文本重复归一化结果不变（best_match 依赖这一点，调用方传原文
    或已规整文本均可）。
    """
    try:
        s = unicodedata.normalize("NFKC", str(text or ""))
    except Exception:  # noqa: BLE001  归一化失败按空串（不命中），绝不抛
        return ""
    s = s.lower()
    s = "".join(ch for ch in s if ch.isalnum())
    for _ in range(_MAX_PREFIX_STRIPS):
        stripped = False
        for prefix in _LEAD_PREFIXES:
            if s.startswith(prefix) and len(s) > len(prefix):
                s = s[len(prefix):]
                stripped = True
                break
        if not stripped:
            break
    return s


def _stable_hash(text: str) -> int:
    """跨进程稳定的 shingle 哈希。

    内置 hash() 受 PYTHONHASHSEED 影响每进程不同——指纹要存 SQLite 并在**不同
    进程/不同天**的会话间比较，必须用确定性哈希；blake2b(8B) 足够快也足够散。
    """
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")


def shingles(text: str, k: int = DEFAULT_SHINGLE_SIZE) -> frozenset:
    """字符 k-shingle 的稳定哈希集合。永不抛：k 非法退缺省，空文本返回空集。

    短于 k 的非空文本整串算一个 shingle——否则 4 字以内的短声明 shingle 全空、
    Jaccard 恒 0，永远召回不了（秒回能力对短句失效）。
    """
    s = str(text or "")
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = DEFAULT_SHINGLE_SIZE
    if k <= 0:
        k = DEFAULT_SHINGLE_SIZE
    if not s:
        return frozenset()
    if len(s) < k:
        return frozenset({_stable_hash(s)})
    return frozenset(_stable_hash(s[i:i + k]) for i in range(len(s) - k + 1))


def jaccard(a: Any, b: Any) -> float:
    """两个集合的 Jaccard 相似度 |A∩B| / |A∪B| ∈ [0, 1]。

    约定：任一方为空 → 0.0（空集与任何集合"无共同内容"，0/0 不定义，按不相似处理）。
    非 set 输入尽力转 frozenset，转不动（不可迭代）返回 0.0，绝不抛。
    """
    try:
        sa = frozenset(a) if a is not None else frozenset()
        sb = frozenset(b) if b is not None else frozenset()
    except TypeError:
        return 0.0
    if not sa or not sb:
        return 0.0
    union = len(sa | sb)
    if union == 0:
        return 0.0
    return len(sa & sb) / union


def _as_shingle_set(value: Any) -> frozenset:
    """行内 shingles 字段的容错还原：frozenset/set 原样用；JSON list/tuple 逐元素收 int。"""
    if isinstance(value, (frozenset, set)):
        return frozenset(value)
    if isinstance(value, (list, tuple)):
        out = set()
        for v in value:
            if isinstance(v, bool) or not isinstance(v, int):
                return frozenset()   # 形状坏：按空集（不相似）处理，不抛
            out.add(v)
        return frozenset(out)
    return frozenset()


def best_match(query_norm: Any, rows: Optional[Sequence[Any]], threshold: float
               ) -> Tuple[Optional[Dict[str, Any]], float]:
    """从候选行里挑 Jaccard 最高分且 ≥ threshold 的一条。

    参数:
        query_norm: 查询声明（原文或 normalize_claim 后的规范形，本函数幂等归一）；
        rows:       候选行序列，每行需含 "shingles"（JSON list 或 frozenset），
                    其余键（dossier_id/normalized/label…）原样带回；
        threshold:  命中阈值（=threshold 命中，略低不中——边界语义见测试）。

    返回 (row, score)：无命中 row=None（score 仍返回最高分，便于调用方记录）。
    永不抛：threshold 写坏按 0 处理、单行形状坏跳过、rows=None 按空表处理。
    """
    try:
        th = float(threshold)
    except (TypeError, ValueError):
        th = 0.0
    query_shingles = shingles(normalize_claim(query_norm))
    best_row: Optional[Dict[str, Any]] = None
    best_score = 0.0
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        score = jaccard(query_shingles, _as_shingle_set(row.get("shingles")))
        if score > best_score:
            best_row, best_score = row, score
    if best_row is not None and best_score >= th:
        return best_row, best_score
    return None, best_score
