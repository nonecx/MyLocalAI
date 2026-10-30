# -*- coding: utf-8 -*-
"""判定口径的外部配置加载（把写死在 Python 里的阈值/名单挪出来）。

对齐检验报告 2026-10-14 的发现 1：风险分阈值、证据抬分锚点、信源分级名单原先都是
源码字面量，换行业/换判定标准必须改 Python。现在它们放在两个 JSON 里，代码只读不写死：

    src/mylocalai/config/verdict_rules.json    阈值、封顶、证据抬分锚点、稳定性护栏
    src/mylocalai/config/sources_registry.json 信源分级名单、官方/非官方站名表

覆盖方式（按优先级）：
    1. 环境变量 RUMOR_RULES_PATH / RUMOR_SOURCES_PATH 指向完整的替代文件
    2. 环境变量 RUMOR_RULES_EXTRA_PATH / RUMOR_SOURCES_EXTRA_PATH 指向增量文件（同结构，深合并覆盖）
    3. 否则用包内默认文件

任何一步失败（文件缺失、JSON 坏、类型不对）都不抛异常，退回内置默认值并在
`load_warnings()` 里留下说明——判定链路不能因为一个配置文件的笔误而崩掉。
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))

# ---- 内置兜底（与 verdict_rules.json / sources_registry.json 的默认值一致） ----
DEFAULT_RULES: Dict[str, Any] = {
    "speech_tier": {"high_score": 7, "suspect_score": 5},
    "thresholds": {"high": 70, "suspect": 45, "doubt": 25},
    "no_claim_risk_cap": 24,
    "evidence_lift": {"multi_source_risk": 78, "single_source_risk": 58, "min_weight_for_high": 2},
    "stability": {"emotion_score_bucket": 2, "emotion_max_points": 20,
                  "memoize_ttl_s": 900, "memoize_max_entries": 256},
    "markers": {
        "marker_cap": 45,
        "credibility_cap": 15,
        "risk_weights": {"source_vague": 12, "virality": 15, "fear_claim": 10,
                         "conspiracy": 15, "absolute": 6, "urgency": 6},
        "credibility_weights": {"source_cited": 6, "official": 6, "numeric_detail": 4},
    },
}

DEFAULT_SOURCES: Dict[str, Any] = {
    "tiers": {
        "官方权威": ["gov.cn", "gov", "mil.cn", "piyao.org.cn", "12377.cn", "who.int", "cdc.gov",
                     "samr.gov.cn", "nhc.gov.cn", "chinacdc.cn"],
        "权威媒体/辟谣平台": ["xinhuanet.com", "news.cn", "people.com.cn", "people.cn", "cctv.com",
                              "cctv.cn", "cnr.cn", "chinanews.com.cn", "chinadaily.com.cn", "gmw.cn",
                              "thepaper.cn", "caixin.com", "bjnews.com.cn", "jfdaily.com", "yicai.com",
                              "cri.cn", "ce.cn", "legaldaily.com.cn", "xinhuanet.com.cn", "dxy.cn",
                              "dxy.com", "guokr.com", "kepuchina.cn", "science.org.cn", "cas.cn",
                              "reuters.com", "bbc.com", "apnews.com"],
        "一般门户媒体": ["sina.com.cn", "sohu.com", "163.com", "ifeng.com", "news.qq.com", "new.qq.com",
                         "qq.com", "qianzhan.com", "bwchinese.com", "bwchinese.com.cn"],
        "自媒体/UGC": ["mp.weixin.qq.com", "baijiahao.baidu.com", "baidu.com", "toutiao.com",
                       "zhihu.com", "weibo.com", "douyin.com", "bilibili.com", "so.com"],
    },
    "official_sites": {"cctv.com": "央视新闻", "cctv.cn": "央视新闻", "xinhuanet.com": "新华网",
                       "news.cn": "新华网", "cnr.cn": "央广网", "people.com.cn": "人民网",
                       "people.cn": "人民网"},
    "commercial_sites": {"sina.com.cn": "新浪网", "qq.com": "腾讯网", "qianzhan.com": "前瞻网",
                         "bwchinese.com": "BWCHINESE中文网", "baidu.com": "百度"},
    "unknown_kind": "其他",
    # 哪个档位算"可靠来源/可抓取/可扩链/算官方"、各档的渠道权重，全部按档名配置。
    # 对齐检验报告 2026-10-15 发现 1：换行业把档位重命名后，这些判定不该静默失效。
    # 没写这几项的 registry（含被整套替换、只声明了 tiers 的）按 tiers 的声明顺序取前几档，
    # 不会退化成"哪个档都不可靠"。
    "reliable_tiers": ["官方权威", "权威媒体/辟谣平台"],
    "fetch_tiers": ["官方权威", "权威媒体/辟谣平台", "一般门户媒体"],
    "link_tiers": ["官方权威", "权威媒体/辟谣平台"],
    "official_tiers": ["官方权威"],
    "channel_score": {"官方权威": 3.0, "权威媒体/辟谣平台": 2.0, "一般门户媒体": 1.5,
                      "自媒体/UGC": 1.0, "未知来源": 0.5},
}

_WARNINGS: Dict[str, List[str]] = {}


def add_warning(key: str, message: str) -> None:
    """给别的模块（如 sources.py 的取值保护）留告警的入口，统一从 load_warnings() 读。"""
    _WARNINGS[key] = message


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"配置根节点必须是对象，实际是 {type(data).__name__}")
    return data


def _strip_comments(data: Dict[str, Any]) -> Dict[str, Any]:
    """去掉 JSON 里的 `_xxx_comment` 说明键，只留真正的数据。"""
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    """深合并：字典递归合并；**字符串列表取并集**（增量文件里加一个域名不会清掉原有名单）；
    其它类型直接覆盖。要整体替换某个列表，请用 RUMOR_SOURCES_PATH 指定完整文件。"""
    merged = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        elif isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = _union(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _union(base_list: List[Any], extra_list: List[Any]) -> List[Any]:
    """列表并集：保留 base 的顺序，把 extra 里没出现过的追加在后面。"""
    out = list(base_list)
    for item in extra_list:
        if item not in out:
            out.append(item)
    return out


def _load(default: Dict[str, Any], name: str, env_full: str, env_extra: str) -> Dict[str, Any]:
    """读默认文件 → 叠加增量文件 → 失败则退回内置默认，并把原因记进 _WARNINGS。

    警告按条形 key 存（同名只留最新一条），因此反复读取不会把旧告警累积成噪音。

    `env_full`（RUMOR_RULES_PATH / RUMOR_SOURCES_PATH）按文档语义是**整套替换**：
    给了它就不再以内置默认为底稿。否则换行业的名单里没写到的档位会从内置默认漏进来，
    让"重命名档位"看起来生效、而"哪几档可靠"仍按旧档名判定（2026-10-15 检验报告发现 1 的静默失效）。
    替换文件缺了哪些段会写进告警，读的人才知道是自己漏写；读坏则整段退回内置默认并报警。
    """
    data = copy.deepcopy(default)
    override = os.environ.get(env_full)
    base_path = override or os.path.join(_HERE, name)
    if override:
        data = {}
    try:
        loaded = _strip_comments(_read_json(base_path))
        data = _deep_merge(data, loaded)
        _WARNINGS.pop(name, None)
        if override:
            # 整套替换：替换文件没写到的段就真的没有兜底（换行业的名单不该混进旧档位与旧域名）。
            # 但别让它静默——缺了哪些段要说清楚，读的人才知道是不是自己漏写了，
            # 并且这正是档位角色段没跟着改时能被发现的地方。
            missing = [k for k in default if k not in loaded]
            if missing:
                _WARNINGS[f"{name}#replaced"] = (
                    f"{env_full} 指向的替换文件未包含这些段，没有内置默认兜底："
                    f"{'、'.join(missing)}（请确认是有意省略）")
        # 不再整套替换时，上一次替换留下的"漏段"告警就已经过期了，必须清掉——
        # 否则它会一直挂到进程结束，让 load_warnings() 报出与当前环境变量无关的旧告警
        # （契约是"只反映当前环境变量的加载结果"）。
        if not override:
            _WARNINGS.pop(f"{name}#replaced", None)
    except FileNotFoundError:
        _WARNINGS[name] = f"{name} 未找到（{base_path}），已用内置默认值"
        data = copy.deepcopy(default)
    except Exception as exc:  # noqa: BLE001  坏配置不能拖垮判定链路
        _WARNINGS[name] = f"{name} 读取失败（{base_path}）：{type(exc).__name__}: {exc}，已用内置默认值"
        data = copy.deepcopy(default)

    extra_path = os.environ.get(env_extra)
    if extra_path:
        try:
            data = _deep_merge(data, _strip_comments(_read_json(extra_path)))
            _WARNINGS.pop(f"{name}#extra", None)
        except Exception as exc:  # noqa: BLE001
            _WARNINGS[f"{name}#extra"] = (
                f"{name} 增量配置读取失败（{extra_path}）：{type(exc).__name__}: {exc}")
    else:
        _WARNINGS.pop(f"{name}#extra", None)
    return data


# 结构契约校验（对齐检验报告 2026-10-15 发现 10）：
# 光做深合并、不校验类型是不够的——official_sites 写成列表会让 import 期崩掉，
# tiers 的值写成字符串会把域名字典退化成单字符键、把政府域名静默降级成"未知来源"。
# 这里把明显写坏的结构段丢弃，并留下 load_warnings()。
# ===========================================================================

#: 形状校验自己会写的告警键（只清这些，才不会把 _load() 写的 #replaced / #extra 一起抹掉）。
#: 2026-10-22 检验报告发现 1：原来用 startswith("sources_registry.json#") 整段清理，
#: 把"整套替换漏了哪些段"的告警也清掉了，导致漏段完全不可观测。
_SHAPE_WARNING_KEYS = (
    "sources_registry.json#tiers",
    "sources_registry.json#official_sites",
    "sources_registry.json#commercial_sites",
    "sources_registry.json#reliable_tiers",
    "sources_registry.json#fetch_tiers",
    "sources_registry.json#link_tiers",
    "sources_registry.json#official_tiers",
    "sources_registry.json#channel_score",
    "sources_registry.json#unknown_kind",
)

#: 同上，判定口径（verdict_rules）形状校验自己会写的告警键。
#: 注意不能写成 startswith("verdict_rules.json#")——那会把 _load() 写的
#: "#extra"（增量配置读取失败）与 "#replaced"（整套替换漏段）一起抹掉。
_RULES_SHAPE_WARNING_KEYS = (
    "verdict_rules.json#thresholds",
    "verdict_rules.json#no_claim_risk_cap",
    "verdict_rules.json#evidence_lift",
    "verdict_rules.json#stability",
    "verdict_rules.json#speech_tier",
    "verdict_rules.json#markers",
)


def _is_number(value: Any) -> bool:
    """是 int/float 且不是 bool（bool 是 int 的子类，不该被当成合法阈值）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int_like(value: Any) -> bool:
    """是整数或整数值的浮点（7.0 允许，7.5 不允许），且不是 bool。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return float(value).is_integer()


def validate_rules_shape(data: Dict[str, Any]) -> Dict[str, Any]:
    """校验判定口径（阈值/封顶/抬分锚点/稳定性护栏）的已知结构。

    规则：明显的类型错误退回内置默认，并留 load_warnings()。
    这条路径此前是缺的：`thresholds.high` 写成字符串时 `_cfg_int()` 静默退回默认，
    用户拿不到任何信号（2026-10-22 检验报告发现 2）。
    """
    out = copy.deepcopy(data)
    for stale in _RULES_SHAPE_WARNING_KEYS:
        _WARNINGS.pop(stale, None)

    thresholds = out.get("thresholds")
    if isinstance(thresholds, dict):
        cleaned = {}
        for key in ("high", "suspect", "doubt"):
            value = thresholds.get(key)
            if _is_number(value):
                cleaned[key] = value
            else:
                default_value = DEFAULT_RULES["thresholds"][key]
                if key in thresholds:
                    add_warning("verdict_rules.json#thresholds",
                                f"thresholds.{key} 必须是数字（得到 {type(value).__name__}: "
                                f"{value!r}），已退回默认 {default_value}")
                cleaned[key] = default_value
        out["thresholds"] = cleaned
    elif thresholds is not None:
        add_warning("verdict_rules.json#thresholds",
                    f"thresholds 必须是字典（得到 {type(thresholds).__name__}），已整段退回内置默认")
        out["thresholds"] = copy.deepcopy(DEFAULT_RULES["thresholds"])

    for key in ("no_claim_risk_cap",):
        value = out.get(key)
        if value is not None and not _is_number(value):
            add_warning("verdict_rules.json#" + key,
                        f"{key} 必须是数字（得到 {type(value).__name__}: {value!r}），已退回默认 "
                        f"{DEFAULT_RULES[key]}")
            out[key] = DEFAULT_RULES[key]

    lift = out.get("evidence_lift")
    if isinstance(lift, dict):
        cleaned_lift = {}
        for key in ("multi_source_risk", "single_source_risk", "min_weight_for_high"):
            value = lift.get(key)
            if _is_number(value):
                cleaned_lift[key] = value
            else:
                if key in lift:
                    add_warning("verdict_rules.json#evidence_lift",
                                f"evidence_lift.{key} 必须是数字（得到 {type(value).__name__}: "
                                f"{value!r}），已退回默认 {DEFAULT_RULES['evidence_lift'][key]}")
                cleaned_lift[key] = DEFAULT_RULES["evidence_lift"][key]
        out["evidence_lift"] = cleaned_lift
    elif lift is not None:
        add_warning("verdict_rules.json#evidence_lift",
                    f"evidence_lift 必须是字典（得到 {type(lift).__name__}），已整段退回内置默认")
        out["evidence_lift"] = copy.deepcopy(DEFAULT_RULES["evidence_lift"])

    stability = out.get("stability")
    if isinstance(stability, dict):
        cleaned_stab = {}
        for key, checker in (("emotion_score_bucket", _is_int_like),
                             ("emotion_max_points", _is_int_like)):
            value = stability.get(key)
            if checker(value):
                cleaned_stab[key] = int(value)
            else:
                if key in stability:
                    add_warning("verdict_rules.json#stability",
                                f"stability.{key} 必须是整数（得到 {type(value).__name__}: "
                                f"{value!r}），已退回默认 {DEFAULT_RULES['stability'][key]}")
                cleaned_stab[key] = DEFAULT_RULES["stability"][key]
        for key in ("memoize_ttl_s", "memoize_max_entries"):
            if key in stability:
                cleaned_stab[key] = stability[key]
        out["stability"] = cleaned_stab
    elif stability is not None:
        add_warning("verdict_rules.json#stability",
                    f"stability 必须是字典（得到 {type(stability).__name__}），已整段退回内置默认")
        out["stability"] = copy.deepcopy(DEFAULT_RULES["stability"])

    speech = out.get("speech_tier")
    if isinstance(speech, dict):
        cleaned_speech = {}
        for key in ("high_score", "suspect_score"):
            value = speech.get(key)
            if _is_number(value):
                cleaned_speech[key] = value
            else:
                if key in speech:
                    add_warning("verdict_rules.json#speech_tier",
                                f"speech_tier.{key} 必须是数字（得到 {type(value).__name__}: "
                                f"{value!r}），已退回默认 {DEFAULT_RULES['speech_tier'][key]}")
                cleaned_speech[key] = DEFAULT_RULES["speech_tier"][key]
        out["speech_tier"] = cleaned_speech
    elif speech is not None:
        add_warning("verdict_rules.json#speech_tier",
                    f"speech_tier 必须是字典（得到 {type(speech).__name__}），已整段退回内置默认")
        out["speech_tier"] = copy.deepcopy(DEFAULT_RULES["speech_tier"])

    markers = out.get("markers")
    if isinstance(markers, dict):
        cleaned_markers = {}
        for key, default_value in (("marker_cap", DEFAULT_RULES["markers"]["marker_cap"]),
                                   ("credibility_cap", DEFAULT_RULES["markers"]["credibility_cap"])):
            value = markers.get(key)
            if _is_number(value):
                cleaned_markers[key] = value
            else:
                if key in markers:
                    add_warning("verdict_rules.json#markers",
                                f"markers.{key} 必须是数字（得到 {type(value).__name__}: "
                                f"{value!r}），已退回默认 {default_value}")
                cleaned_markers[key] = default_value
        for key, default_map in (("risk_weights", DEFAULT_RULES["markers"]["risk_weights"]),
                                 ("credibility_weights", DEFAULT_RULES["markers"]["credibility_weights"])):
            value = markers.get(key)
            if isinstance(value, dict):
                cleaned = {}
                bad_keys = []
                for name, weight in value.items():
                    if _is_number(weight):
                        cleaned[name] = weight
                    else:
                        bad_keys.append(str(name))
                        if name in default_map:
                            cleaned[name] = default_map[name]
                for name, weight in default_map.items():
                    cleaned.setdefault(name, weight)
                if bad_keys:
                    add_warning("verdict_rules.json#markers",
                                f"markers.{key} 里这些权重不是数字，已退回默认：{'、'.join(bad_keys)}")
                cleaned_markers[key] = cleaned
            elif value is not None:
                add_warning("verdict_rules.json#markers",
                            f"markers.{key} 必须是字典（得到 {type(value).__name__}），已整段退回内置默认")
                cleaned_markers[key] = copy.deepcopy(default_map)
            else:
                cleaned_markers[key] = copy.deepcopy(default_map)
        out["markers"] = cleaned_markers
    elif markers is not None:
        add_warning("verdict_rules.json#markers",
                    f"markers 必须是字典（得到 {type(markers).__name__}），已整段退回内置默认")
        out["markers"] = copy.deepcopy(DEFAULT_RULES["markers"])
    return out


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def validate_sources_shape(data: Dict[str, Any]) -> Dict[str, Any]:
    """校验 sources registry 的已知结构；不合规的段丢弃或退回内置默认，并留告警。

    规则：
      tiers            必须是 {档名: [域名, ...]}；某个档位的值写坏 → 该档退回内置默认（若有同名档）
      official_sites   必须是 {域名: 站名}；写坏 → 整段退回内置默认
      commercial_sites 同上
      reliable_tiers / fetch_tiers / link_tiers   必须是字符串列表（档名）
      channel_score    必须是 {档名: 数值}
      unknown_kind     必须是字符串
    """
    out = copy.deepcopy(data)
    # 只清形状校验自己写过的告警键：_load() 写的 `#replaced`（替换文件漏段）与
    # `#extra`（增量文件读取失败）必须留着，否则"漏段/读失败"会变成完全静默。
    for stale in _SHAPE_WARNING_KEYS:
        _WARNINGS.pop(stale, None)

    tiers = out.get("tiers")
    if isinstance(tiers, dict):
        cleaned: Dict[str, Any] = {}
        bad: List[str] = []
        for tier, domains in tiers.items():
            if str(tier).startswith("_"):
                continue
            if _is_str_list(domains):
                cleaned[tier] = domains
            elif tier in DEFAULT_SOURCES["tiers"]:
                # 值写坏（例如写成一个字符串）会让域名字典退化成单字符键，把政府域名
                # 静默降级成"未知来源"——这里退回内置默认，绝不放行
                cleaned[tier] = copy.deepcopy(DEFAULT_SOURCES["tiers"][tier])
                bad.append(f"{tier}（值不是字符串列表，已退回内置默认）")
            else:
                bad.append(f"{tier}（值不是字符串列表，已丢弃）")
        if bad:
            add_warning("sources_registry.json#tiers",
                        "tiers 段存在不合规档位：" + "；".join(bad))
        out["tiers"] = cleaned
    elif tiers is not None:
        add_warning("sources_registry.json#tiers",
                    f"tiers 必须是字典（得到 {type(tiers).__name__}），已整段退回内置默认")
        out["tiers"] = copy.deepcopy(DEFAULT_SOURCES["tiers"])

    for key in ("official_sites", "commercial_sites"):
        value = out.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            cleaned_map = {k: v for k, v in value.items()
                           if not str(k).startswith("_") and isinstance(k, str) and isinstance(v, str)}
            dropped = [str(k) for k in value
                       if not str(k).startswith("_") and not (isinstance(k, str) and isinstance(value[k], str))]
            if dropped:
                add_warning(f"sources_registry.json#{key}",
                            f"{key} 里这些条的键或值不是字符串，已丢弃：{'、'.join(dropped)}")
            out[key] = cleaned_map
        else:
            add_warning(f"sources_registry.json#{key}",
                        f"{key} 必须是字典（得到 {type(value).__name__}），已整段退回内置默认")
            out[key] = copy.deepcopy(DEFAULT_SOURCES[key])

    for key in ("reliable_tiers", "fetch_tiers", "link_tiers", "official_tiers"):
        value = out.get(key)
        if value is not None and not _is_str_list(value):
            add_warning(f"sources_registry.json#{key}",
                        f"{key} 必须是字符串列表（档名），得到 {type(value).__name__}，已忽略该项")
            out.pop(key, None)

    scores = out.get("channel_score")
    if scores is not None:
        if isinstance(scores, dict):
            cleaned_scores = {}
            for tier, val in scores.items():
                if str(tier).startswith("_"):
                    continue
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    cleaned_scores[tier] = float(val)
                else:
                    add_warning("sources_registry.json#channel_score",
                                f"channel_score 里 {tier} 的值不是数字，已丢弃该条")
            out["channel_score"] = cleaned_scores
        else:
            add_warning("sources_registry.json#channel_score",
                        f"channel_score 必须是字典（得到 {type(scores).__name__}），已忽略该项")
            out.pop("channel_score", None)

    unknown = out.get("unknown_kind")
    if unknown is not None and not isinstance(unknown, str):
        add_warning("sources_registry.json#unknown_kind",
                    f"unknown_kind 必须是字符串（得到 {type(unknown).__name__}），改用默认「其他」")
        out["unknown_kind"] = DEFAULT_SOURCES["unknown_kind"]
    return out


def load_rules() -> Dict[str, Any]:
    """读取判定口径（阈值/封顶/抬分锚点/稳定性护栏），并做结构契约校验。"""
    return validate_rules_shape(
        _load(DEFAULT_RULES, "verdict_rules.json", "RUMOR_RULES_PATH", "RUMOR_RULES_EXTRA_PATH"))


def load_sources() -> Dict[str, Any]:
    """读取信源分级名单与官方/非官方站名表（含结构契约校验）。"""
    return validate_sources_shape(
        _load(DEFAULT_SOURCES, "sources_registry.json", "RUMOR_SOURCES_PATH", "RUMOR_SOURCES_EXTRA_PATH"))


def load_warnings() -> List[str]:
    """本次进程里加载配置时出现的问题（为空表示一切正常）。

    只反映**当前**环境变量的加载结果：上一次调用留下的告警会被覆盖或清掉。
    """
    return list(_WARNINGS.values())


def _int(data: Dict[str, Any], key: str, default: int) -> int:
    """取整数配置项，坏值退回 default（配置里写字符串不会让判定崩）。"""
    try:
        return int(data[key])
    except (KeyError, TypeError, ValueError):
        return default
