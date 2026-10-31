# -*- coding: utf-8 -*-
"""关注度深读（Deep Read）：解释"为什么人们会关注这件事"。

`attention.py` 只回答"有多少人关注"（确定性分数），本模块回答"为什么关注"
（叙事解读）。两者刻意分开：**分数必须可复现，解读允许大模型参与**。

改造后职责（三阶段落地，当前为阶段三：受控生成 + 后处理护栏 + 结构化回退）：
   - 触发规则外置到 `deep_read_rules.json`（absolute / quantile / change_rate / any
     四种模式，可按参数档位 by_profile 覆盖），判定是纯函数 `should_deep_read()`，
     不碰 LLM、可单测。默认口径与改造前一致（spec 档 absolute，E > 66 严格大于）。
   - 输入信号结构化：`attention.derive_signals()` 派生 engagement_ratio / spread /
     resonance / share_* / profile / proxy_flags，随 prompt 一起给模型，
     解决"只给 E 值、模型只能复述关注度高"的解释力缺失。
   - 受控生成：议题只能从封闭清单 `society_issues.json` 里选，社会问题只能
     表述为「A 与 B 之间的张力」，每个判断必须挂 evidence_id，抽象层级
     逐级（L1 文本证据 → L2 检索证据 → L3 结构推断，L3 不得无 L1/L2 支撑）。
   - 后处理护栏 `deep_read_guard.validate_and_sanitize()`：不依赖模型自觉，
     在代码里复核 schema / 议题封闭性 / 证据有效性 / L3 挂靠 / 禁词禁归因 /
     反方必填 / 字数上限，不合规的条目整段丢弃并标 `degraded`。
   - 回退模板：LLM 不可用或输出被全量丢弃时，按**结构化字段拼接**本地模板，
     不做自由生成，并标 `degraded` + `degraded_reason`。

设计约束（与项目既有约定一致）：
   - **大模型只负责叙事，不参与定级**：是否触发深读由 `deep_read_triggered` 决定，
     解读文本不改变 E、档位、或任何分数。
   - **降级必须可见**：LLM 不可用时回退本地模板，并带 `degraded` 标记，
     调用方（网页/CLI）要能区分"模型解读"和"模板解读"。
   - **永不抛异常**：与 report_generator / 三个分析模块一致。
   - **口径提示必须随文本一起给出**：解读建立在"关注度高"这个前提上，
     而 E 的 y 可能是代理值（见 estimate.py），所以模板里要写明这一点，
     否则读者会把解读当成对确定事实的断言。
   - **物理隔离**：本模块不得 import `mylocalai.rumor.verdict` 或任何 verdict 相关
     模块；深读产出不写入 `mylocalai.llm.llm_cache` 影响主分析结论（复用缓存时
     用独立 namespace）；深读抛任何异常都被捕获，主分析流程不受影响。

提示词外置到 `src/mylocalai/attention/prompts.json`（`deep_read_system` / `deep_read_fewshot`，
格式对齐 `src/mylocalai/rumor/prompts.json`）；触发规则与受控生成参数外置到
`deep_read_rules.json`；议题清单外置到 `society_issues.json`。三者都经
`mylocalai.config.config_loader` 加载，缺失或写坏时退回内置默认并留中文告警，不崩溃。
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from mylocalai.attention import attention as _attn
from mylocalai.attention import deep_read_guard as _guard

# 深读**文本口径**（旧版 system_prompt + template）外置到 attn_deep_read.json。
# 保留原路径名不变：既有调用方与测试按 RULES_PATH 读它，改名即破坏对外行为。
RULES_PATH: Path = Path(__file__).resolve().parent / "attn_deep_read.json"

# 深读**规则**（触发口径 + 受控生成参数 + 护栏禁词）外置到 deep_read_rules.json。
# 与 rumor_agent / shared_core 的既有约定一致：改配置不改代码，写坏退回内置默认并留告警。
DEEP_READ_RULES_PATH: Path = Path(__file__).resolve().parent / "deep_read_rules.json"

# 议题清单（封闭集，见 2.1）
ISSUES_PATH: Path = Path(__file__).resolve().parent / "society_issues.json"

# 深读提示词（受控生成的 system 与 few-shot）外置到 prompts.json
PROMPTS_PATH: Path = Path(__file__).resolve().parent / "prompts.json"

# LLM 缓存分区名：深读与主分析（report_generator / 三个模块）复用同一个进程内缓存
# `mylocalai.llm.llm_cache`，必须分区——否则两边的提示词与用户文本一旦碰巧相同，
# 一边就会读到另一边的缓存，把结论污染过去（任务书 3.2「物理隔离」要求之一）。
# 名字固定，测试按它断言隔离性。
CACHE_NAMESPACE: str = "attn_deep_read"

# ---- 内置默认：触发规则 ----
# 默认口径必须与改造前**完全一致**：spec 档 absolute + 阈值 66（E > 66 严格大于）。
# 即：不显式指定 profile 时，本模块的触发判定与改动前逐位相同。
#
# 注意 `by_profile` **不放进 builtin 兜底**：它若进兜底，用户用 --rules-path /
# DEEP_READ_RULES_PATH 换一份只写顶层 trigger 的配置时，合并会把 spec/reachable 的
# 档位覆盖也继承下来，于是用户改的顶层阈值被档位覆盖悄悄吃掉（改了像没改）。
# 档位覆盖只在配置里显式写出来时才生效。
DEFAULT_TRIGGER_RULES: Dict[str, Any] = {
    "mode": "absolute",
    "absolute_threshold": 66.0,
    "quantile_top_ratio": 0.1,
    "change_rate_threshold": None,
    "by_profile": {
        "spec": {"mode": "absolute", "absolute_threshold": 66.0},
        "reachable": {"mode": "quantile", "quantile_top_ratio": 0.1},
    },
}

# ---- 内置默认：受控生成参数 ----
DEFAULT_DEEP_READ_PARAMS: Dict[str, Any] = {
    "max_words": 300,
    "require_counter_reading": True,
    "max_issue_tags": 3,
    "allow_other_tag": True,
}

# ---- 内置默认：护栏禁词（正则列表）----
# 命中即整段丢弃该条目。覆盖三类：具体主体归因、样本推普遍、价值判断/预测。
# 与 `deep_read_guard.DEFAULT_BANNED_PATTERNS` 保持一致（guard 是唯一实现处，
# 这里 re-export 供"配置与代码不漂移"的测试断言）。
DEFAULT_BANNED_PATTERNS: List[Dict[str, str]] = _guard.DEFAULT_BANNED_PATTERNS

# ---- 内置默认：议题清单（最小集，仅用于文件缺失/写坏时的兜底）----
# 只放两个：任务书 2.1 要求的最小默认（info_transparency_trust 与 other）。
# 完整 30-50 项清单在 society_issues.json，缺它时退回这里并留告警。
DEFAULT_ISSUES: Dict[str, Any] = {
    "version": "builtin",
    "issues": [
        {
            "id": "info_transparency_trust",
            "label": "公共信息透明度与公众信任",
            "definition": "围绕官方信息发布及时性、完整性与公众知情需求之间的张力",
            "tension_a_hint": "信息发布节奏",
            "tension_b_hint": "公众知情需求",
            "include": ["通报与传闻不一致", "信息延迟引发的质疑"],
            "exclude": ["单纯的情绪宣泄", "对具体个人的指责"],
        },
        {
            "id": "other",
            "label": "其他/未归类议题",
            "definition": "兜底标签：话题确实涉及社会议题但不属于清单内任何一项",
            "tension_a_hint": "议题一端",
            "tension_b_hint": "议题另一端",
            "include": ["无法归入既有议题的社会性讨论"],
            "exclude": ["纯情绪宣泄", "纯事实陈述", "纯商业推广"],
        },
    ],
    "fallback": "other",
}

# ---- 内置默认：受控生成提示词（文件缺失/写坏时兜底）----
# 与 prompts.json 的 deep_read_system / deep_read_fewshot 对应。
DEEP_READ_SYSTEM_PROMPT = """\
你是社会议题分析助手。严格按以下规则工作：
1. 只能从给定议题清单中选择 issue_tags，不得自创 id；清单外的一律归为 other。
2. 社会问题只能表述为「A 与 B 之间的张力」，A/B 取自所选议题的 tension 提示；
   禁止写成因果归因（"因为 X 所以 Y"）。
3. 每个判断必须挂 evidence_id；无证据的判断不要输出。
4. 抽象层级必须逐级：具体现象(L1) → 同类聚合(L2) → 结构性议题(L3)。禁止跳级，
   L3 不得无 L1/L2 支撑。
5. 禁止：归因到具体机构/公司/群体/地域/身份；群体贴标签；从样本推普遍性
   （"全社会/普遍/必然/所有人"）；价值判断词（"可悲/落后/愚昧"）；对未发生事件的预测。
6. 允许并鼓励：张力表述、诉求描述、归因对象的中性列举（只列不评）、
   规模限定（"本样本内/多数/少数"）、反方解读、显式不确定性。
7. 必须输出 counter_reading（找不到反方证据时写"未发现反方证据"并说明）。
8. 证据不足或话题不适合做社会议题分析时，输出 {"applicable": false, "reason": "..."}。
9. 输出严格 JSON，不加任何解释性前后缀。
"""

DEEP_READ_FEWSHOT = """\
【坏例 —— 不要这样输出】
这反映了社会信任危机，说明公众对政府信息发布极度不满，长此以往将引发更大矛盾。
（坏在：因果归因到具体主体、样本推普遍、"将引发"是预测、无证据）

【好例 —— 应输出这样的严格 JSON】
{"applicable": true, "issue_tags": [{"id":"info_transparency_trust","confidence":"medium","evidence_ids":["e3","e5","e7"]}], "tensions": [{"a":"信息发布节奏","b":"公众知情需求","evidence_ids":["e3","e7","e9"],"level":"L3"}], "phenomena": [{"text":"多条发言指向通报时间晚于网络传闻","level":"L1","evidence_ids":["e3","e5","e7"],"count":"7/12"}], "counter_reading": {"text":"3 条发言认为质疑源于误解","evidence_ids":["e11"]}, "boundary": {"scope":"sample","sample_size":12,"proxy_flags":["y_proxy"]}, "uncertainty": ["代理阅读量口径为估计值，E 仅同口径可比"], "degraded": false}
"""

# 触发模式的合法取值
_TRIGGER_MODES: Tuple[str, ...] = ("absolute", "quantile", "change_rate", "any")

# 环比增幅的分母下限：增幅 = (最新 - 前一) / max(前一, CHANGE_RATE_EPSILON)。
# 前一快照为 0（或为负的脏值）时避免除零；E 本在 (0, 100) 开区间，这里纯防御。
CHANGE_RATE_EPSILON: float = 1e-9


# ---------------------------------------------------------------------------
# 规则加载（config_loader 复用：整套替换 + 增量覆盖 + 失败退回默认 + 告警）
# ---------------------------------------------------------------------------

def load_rules(path: Optional[str] = None) -> Tuple[Dict[str, Any], List[str]]:
    """加载深读规则，返回 (rules, warnings)。**永不抛异常。**

    结构：
        trigger    —— 触发规则（mode/absolute_threshold/quantile_top_ratio/by_profile）
        deep_read  —— 受控生成参数（max_words/require_counter_reading/max_issue_tags/allow_other_tag）
        banned_patterns —— 护栏禁词正则列表

    失败（文件缺失 / JSON 写坏 / 结构不合法）时对应段退回内置默认并留中文告警，
    与 `attention.load_params`、`config_loader.load_config` 的既有约定一致。
    段级降级：某一段写坏只退回那一段，不影响其他段。
    """
    from mylocalai.config import config_loader

    target = Path(path) if path is not None else DEEP_READ_RULES_PATH
    builtin: Dict[str, Any] = {
        "trigger": {k: (dict(v) if isinstance(v, dict) else v)
                    for k, v in DEFAULT_TRIGGER_RULES.items() if k != "by_profile"},
        "deep_read": dict(DEFAULT_DEEP_READ_PARAMS),
        "banned_patterns": [dict(p) for p in DEFAULT_BANNED_PATTERNS],
    }
    # 先单独看一眼原文件：`config_loader` 会用 builtin 兜底合并，"缺段"这件事在合并后
    # 就看不到了，而缺段必须留告警（与 rumor_agent 的口径一致：整套替换漏段要说出来）。
    warnings = _missing_section_warnings(target)
    data, loader_warnings = config_loader.load_config(
        str(target), builtin=builtin,
        required_sections=("trigger", "deep_read"),
        env_full="DEEP_READ_RULES_PATH",
        env_extra="DEEP_READ_RULES_EXTRA_PATH",
    )
    warnings.extend(loader_warnings.values())

    rules, issues = _normalize_rules(data)
    warnings.extend(issues)
    return rules, warnings


def _missing_section_warnings(target: Path) -> List[str]:
    """原文件里缺了哪个必填段就告警哪个（读不到文件时不告警，交给 load_config 报）。"""
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:  # noqa: BLE001  读不到/解析失败由 load_config 统一报告
        return []
    if not isinstance(raw, dict):
        return []
    missing = [name for name in ("trigger", "deep_read") if name not in raw]
    return [f"深读规则文件（{target.name}）缺少 {name} 段，该段已用内置默认" for name in missing]


def _normalize_rules(data: Any) -> Tuple[Dict[str, Any], List[str]]:
    """把加载到的配置规整成合法结构；不合规的段退回内置默认并给出中文说明。"""
    rules: Dict[str, Any] = {}
    issues: List[str] = []
    if not isinstance(data, dict):
        return {
            "trigger": {k: (dict(v) if isinstance(v, dict) else v)
                        for k, v in DEFAULT_TRIGGER_RULES.items()},
            "deep_read": dict(DEFAULT_DEEP_READ_PARAMS),
            "banned_patterns": [dict(p) for p in DEFAULT_BANNED_PATTERNS],
        }, ["深读规则根节点必须是对象，已全部退回内置默认"]
    # ---- trigger 段 ----
    trigger_raw = data.get("trigger")
    trigger: Dict[str, Any] = {}
    if isinstance(trigger_raw, dict):
        trigger = dict(trigger_raw)
    else:
        issues.append("深读规则缺少 trigger 段（或不是对象），触发规则已退回内置默认")

    mode = trigger.get("mode", DEFAULT_TRIGGER_RULES["mode"])
    if mode not in _TRIGGER_MODES:
        issues.append(f"触发模式 {mode!r} 不合法（可用：{'、'.join(_TRIGGER_MODES)}），"
                      f"已退回 {DEFAULT_TRIGGER_RULES['mode']!r}")
        mode = DEFAULT_TRIGGER_RULES["mode"]
    trigger["mode"] = mode

    threshold = trigger.get("absolute_threshold", DEFAULT_TRIGGER_RULES["absolute_threshold"])
    if not _is_number(threshold) or threshold < 0:
        issues.append(f"absolute_threshold 必须是非负实数（实际 {threshold!r}），已退回 66")
        threshold = 66.0
    trigger["absolute_threshold"] = float(threshold)

    ratio = trigger.get("quantile_top_ratio", DEFAULT_TRIGGER_RULES["quantile_top_ratio"])
    if not _is_number(ratio) or not (0.0 < float(ratio) <= 1.0):
        issues.append(f"quantile_top_ratio 必须落在 (0, 1]（实际 {ratio!r}），已退回 0.1")
        ratio = 0.1
    trigger["quantile_top_ratio"] = float(ratio)

    # change_rate_threshold：None（未配置，环比判定停用）或非负有限实数。
    # 写了别的类型/负数时退回 None 并告警——环比是"增幅"口径，负阈值没有语义，
    # 静默沿用坏值反而会让触发判定说谎。
    change = trigger.get("change_rate_threshold")
    if change is None:
        trigger["change_rate_threshold"] = None
    elif _is_number(change) and math.isfinite(float(change)):
        if float(change) < 0:
            issues.append(f"change_rate_threshold 必须是非负实数（实际 {change!r}），"
                          f"环比判定已停用")
            trigger["change_rate_threshold"] = None
        else:
            trigger["change_rate_threshold"] = float(change)
    else:
        issues.append(f"change_rate_threshold 必须是非负实数或 null（实际 {change!r}），"
                      f"环比判定已停用")
        trigger["change_rate_threshold"] = None

    by_profile_raw = trigger.get("by_profile")
    by_profile: Dict[str, Any] = {}
    if isinstance(by_profile_raw, dict):
        for name, spec in by_profile_raw.items():
            if isinstance(spec, dict):
                by_profile[str(name)] = dict(spec)
            else:
                issues.append(f"trigger.by_profile.{name} 不是对象，该档位覆盖已忽略")
    elif by_profile_raw is not None:
        issues.append("trigger.by_profile 不是对象，档位覆盖已忽略")
    trigger["by_profile"] = by_profile

    # ---- deep_read 段 ----
    params_raw = data.get("deep_read")
    params: Dict[str, Any] = dict(DEFAULT_DEEP_READ_PARAMS)
    if isinstance(params_raw, dict):
        for key in ("max_words", "require_counter_reading", "max_issue_tags", "allow_other_tag"):
            if key not in params_raw:
                continue
            value = params_raw[key]
            if key in ("require_counter_reading", "allow_other_tag"):
                if isinstance(value, bool):
                    params[key] = value
                else:
                    issues.append(f"deep_read.{key} 必须是布尔值（实际 {value!r}），已用内置默认")
            else:
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    params[key] = value
                else:
                    issues.append(f"deep_read.{key} 必须是正整数（实际 {value!r}），已用内置默认")
    else:
        issues.append("深读规则缺少 deep_read 段（或不是对象），生成参数已退回内置默认")

    # ---- banned_patterns 段 ----
    banned_raw = data.get("banned_patterns")
    banned: List[Dict[str, str]] = []
    if isinstance(banned_raw, list):
        for item in banned_raw:
            if isinstance(item, dict) and isinstance(item.get("pattern"), str) and item["pattern"].strip():
                banned.append({"id": str(item.get("id", "")),
                               "pattern": item["pattern"],
                               "note": str(item.get("note", ""))})
            else:
                issues.append("banned_patterns 含不合规项（需含非空 pattern 字符串），该项已忽略")
    elif banned_raw is not None:
        issues.append("banned_patterns 不是列表，禁词已退回内置默认")
    if not banned:
        banned = [dict(p) for p in DEFAULT_BANNED_PATTERNS]
        if banned_raw is not None:
            issues.append("banned_patterns 无有效项，禁词已退回内置默认")

    rules["trigger"] = trigger
    rules["deep_read"] = params
    rules["banned_patterns"] = banned
    return rules, issues


def _is_number(value: Any) -> bool:
    """是否是实数（bool 不算）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_issues(path: Optional[str] = None) -> Tuple[Dict[str, Any], List[str]]:
    """加载封闭议题清单，返回 (issues, warnings)。**永不抛异常。**

    结构（见 `society_issues.json`）：
        version —— 清单版本号（原样回显，便于追溯）
        issues  —— 议题数组，每项含 id/label/definition/tension_a_hint/
                   tension_b_hint/include/exclude
        fallback —— 清单外兜底 id（默认 "other"）

    失败（文件缺失 / JSON 写坏 / 结构不合法 / issues 为空）时退回内置最小默认
    `DEFAULT_ISSUES`（至少含 info_transparency_trust 与 other）并留中文告警。
    """
    from mylocalai.config import config_loader

    target = Path(path) if path is not None else ISSUES_PATH
    builtin = {k: (v if not isinstance(v, list) else [dict(x) for x in v])
               for k, v in DEFAULT_ISSUES.items()}
    data, loader_warnings = config_loader.load_config(
        str(target), builtin=builtin,
        required_sections=("issues",),
        env_full="DEEP_READ_ISSUES_PATH",
        env_extra="DEEP_READ_ISSUES_EXTRA_PATH",
    )
    warnings = list(loader_warnings.values())

    # 原文件自身缺 issues 段、或写了空数组：config_loader 的"列表取并集"合并会让
    # builtin 兜底悄悄补上，于是合并后看不出问题——必须单独看原文件并留告警
    # （与 _missing_section_warnings 同一个理由：整套替换漏段要说出来）。
    if _issues_section_missing_or_empty(target):
        warnings.append("议题清单文件缺少 issues 段（或 issues 为空数组），已用内置最小默认")

    issues_raw = data.get("issues")
    normalized: List[Dict[str, Any]] = []
    if isinstance(issues_raw, list):
        for item in issues_raw:
            if not isinstance(item, dict):
                continue
            tag_id = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            if not tag_id or not label:
                continue
            normalized.append({
                "id": tag_id,
                "label": label,
                "definition": str(item.get("definition") or "").strip(),
                "tension_a_hint": str(item.get("tension_a_hint") or "").strip(),
                "tension_b_hint": str(item.get("tension_b_hint") or "").strip(),
                "include": [str(x) for x in (item.get("include") or []) if str(x).strip()],
                "exclude": [str(x) for x in (item.get("exclude") or []) if str(x).strip()],
            })
    if not normalized:
        warnings.append("议题清单无有效条目（issues 缺失/为空/结构不合规），已退回内置最小默认")
        normalized = [dict(x) for x in DEFAULT_ISSUES["issues"]]

    # id 去重（保留首次出现）：重复 id 会让"清单外判定"出现歧义。
    # 注意：`config_loader` 会把内置兜底清单与文件清单做**列表并集合并**，所以
    # 兜底里的 other/info_transparency_trust 常与文件里的同名项同时出现在合并结果里
    # ——那是正常合并，不是"用户写重了"。因此查重要看**原文件自身**，
    # 不能看合并后的 data，否则每次加载都会误报一条重复告警。
    duplicated = _duplicate_ids_in_file(target)
    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for item in normalized:
        if item["id"] in seen:
            if item["id"] in duplicated:
                warnings.append(f"议题 id {item['id']} 重复，已保留首次出现的定义")
            continue
        seen.add(item["id"])
        deduped.append(item)

    fallback = str(data.get("fallback") or "other").strip() or "other"
    if fallback not in seen:
        warnings.append(f"议题清单的 fallback {fallback!r} 不在 issues 内，已改用 other")
        fallback = "other"

    return {"version": str(data.get("version") or "1"),
            "issues": deduped,
            "fallback": fallback}, warnings


def _issues_section_missing_or_empty(target: Path) -> bool:
    """原文件是否缺 issues 段、或 issues 是空数组（读不到文件时返回 False，交给 load_config 报）。"""
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:  # noqa: BLE001  读不到/解析失败由 load_config 统一报告
        return False
    if not isinstance(raw, dict):
        return False
    issues = raw.get("issues")
    return not isinstance(issues, list) or not issues


def _duplicate_ids_in_file(target: Path) -> set:
    """原文件 issues 里自身重复的 id 集合（读不到文件时返回空集）。"""
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:  # noqa: BLE001  读不到/解析失败由 load_config 统一报告
        return set()
    if not isinstance(raw, dict) or not isinstance(raw.get("issues"), list):
        return set()
    names: List[str] = []
    for item in raw["issues"]:
        if isinstance(item, dict):
            name = str(item.get("id") or "").strip()
            if name:
                names.append(name)
    return {name for name in names if names.count(name) > 1}


def load_prompts(path: Optional[str] = None) -> Tuple[Dict[str, str], List[str]]:
    """加载受控生成提示词，返回 ({"system":..,"fewshot":..}, warnings)。**永不抛异常。**"""
    from mylocalai.config import config_loader

    target = Path(path) if path is not None else PROMPTS_PATH
    builtin = {"deep_read_system": DEEP_READ_SYSTEM_PROMPT,
               "deep_read_fewshot": DEEP_READ_FEWSHOT}
    data, loader_warnings = config_loader.load_config(
        str(target), builtin=builtin,
        required_sections=("deep_read_system",),
        env_full="DEEP_READ_PROMPTS_PATH",
        env_extra="DEEP_READ_PROMPTS_EXTRA_PATH",
    )
    warnings = list(loader_warnings.values())
    out: Dict[str, str] = {}
    for key, fallback in (("deep_read_system", DEEP_READ_SYSTEM_PROMPT),
                          ("deep_read_fewshot", DEEP_READ_FEWSHOT)):
        value = data.get(key, fallback)
        if not isinstance(value, str) or not value.strip():
            warnings.append(f"深读提示词 {key} 不是非空字符串，已用内置默认")
            out[key.replace("deep_read_", "")] = fallback
        else:
            out[key.replace("deep_read_", "")] = value
    return out, warnings


def resolve_trigger_rules(rules: Dict[str, Any], profile: Optional[str]) -> Dict[str, Any]:
    """按参数档位解析触发规则：`by_profile.<profile>` 覆盖顶层同名字段。

    profile 为空或不在 by_profile 里时用顶层规则（spec 的默认即顶层 absolute/66）。
    """
    trigger = rules.get("trigger") or {}
    resolved: Dict[str, Any] = {
        "mode": trigger.get("mode", "absolute"),
        "absolute_threshold": trigger.get("absolute_threshold", 66.0),
        "quantile_top_ratio": trigger.get("quantile_top_ratio", 0.1),
        "change_rate_threshold": trigger.get("change_rate_threshold"),
    }
    if profile:
        spec = (trigger.get("by_profile") or {}).get(profile)
        if isinstance(spec, dict):
            for key in ("mode", "absolute_threshold", "quantile_top_ratio", "change_rate_threshold"):
                if key in spec:
                    resolved[key] = spec[key]
    return resolved


def _change_rate_detail(history: Optional[Sequence[float]],
                        threshold: Any) -> Dict[str, Any]:
    """环比增幅评估（**纯函数**，口径诚实、永不抛异常）。

    history 是同档案 e_score 的时序序列（升序=时间正序，调用方决定是否已含当前
    快照）；**只看最后两个有效数值**：增幅 = (最新 - 前一) / max(前一, ε)。

    返回详情 dict（键固定）：
        evaluated=False 时 —— {"evaluated", "triggered"(恒 False), "reason"}
            reason 如实说明停用原因：阈值未配置 / 快照历史不足（<2 条，单快照
            不触发环比判定）/ 比率非有限（快照数值过于极端）。
        evaluated=True 时  —— {"evaluated", "triggered", "prev", "latest",
                               "rate", "threshold"}
            triggered 指**环比条件本身**（rate >= threshold）是否满足，不是整体
            深读触发结论；prev/latest/rate 如实记录所用两快照与比率。

    history 中非有限实数（bool/nan/inf/字符串）一律跳过；阈值不是非负有限实数
    视为未配置（与 _normalize_rules 的口径一致，手搓 rules 也走同一防线）。
    """
    if not _is_number(threshold) or not math.isfinite(float(threshold)) \
            or float(threshold) < 0:
        return {"evaluated": False, "triggered": False,
                "reason": "change_rate_threshold 未配置为非负实数，环比判定停用"}
    values: List[float] = []
    if isinstance(history, (list, tuple)):
        for item in history:
            if _is_number(item) and math.isfinite(float(item)):
                values.append(float(item))
    if len(values) < 2:
        return {"evaluated": False, "triggered": False,
                "reason": (f"快照历史不足（有效数值 {len(values)} 条，需 ≥2），"
                           f"单快照不触发环比判定")}
    prev, latest = values[-2], values[-1]
    rate = (latest - prev) / max(prev, CHANGE_RATE_EPSILON)
    if not math.isfinite(rate):
        return {"evaluated": False, "triggered": False,
                "reason": "环比比率非有限（快照数值过于极端），环比判定停用",
                "prev": prev, "latest": latest}
    return {"evaluated": True, "triggered": bool(rate >= float(threshold)),
            "prev": prev, "latest": latest, "rate": rate,
            "threshold": float(threshold)}


def should_deep_read(scores: Any, profile: Optional[str] = None,
                     rules: Optional[Dict[str, Any]] = None,
                     snapshot_history: Optional[Sequence[float]] = None) -> bool:
    """是否触发深读（**纯函数**，不碰 LLM、不联网、可单测）。

    参数:
        scores:  判定依据，两种形态：
                   * dict —— 单个话题的 `score_detail()` 输出（含 score）；
                   * Sequence[float|dict] —— **同批话题**的分数集合。元素是 dict 时取
                     ["score"]；本函数用同批分位来判定 quantile 模式，因此取的是
                     "当前话题在整批里的相对位置"，需要调用方把整批一起传进来。
        profile: 参数档位名（"spec" / "reachable"），决定用哪套触发规则。
        rules:   已加载的规则（`load_rules()` 的输出）。None 时用内置默认。
        snapshot_history: 同档案 e_score 的时序序列（升序=时间正序），**仅
                     change_rate / any 模式使用**。None 或有效数值不足 2 条时
                     change_rate 恒为 False（单快照不触发，回落既有规则）；
                     传入时不改变 absolute/quantile 的既有判定。

    模式:
        absolute   —— E 严格大于阈值（等于不算，与 `attention.deep_read_triggered` 一致）
        quantile   —— 同批 Top-K%：当前话题分数排进前 K%
        change_rate—— 环比增幅：(最新-前一)/max(前一, ε) ≥ change_rate_threshold
                      （ε=CHANGE_RATE_EPSILON）。阈值未配置为非负实数、或
                      snapshot_history 不足 2 条时恒为 False。
        any        —— absolute 或 quantile 或 change_rate 任一满足

    行为兼容性保证：profile 为 None / "spec" 且走默认规则时，absolute 模式退化为
    `score > 66`，与改造前的 `deep_read_triggered(score)` 逐位一致；不传
    snapshot_history 时 change_rate 不参与，所有既有模式的判定逐位不变。
    """
    if rules is None:
        rules = {"trigger": DEFAULT_TRIGGER_RULES, "deep_read": DEFAULT_DEEP_READ_PARAMS,
                 "banned_patterns": DEFAULT_BANNED_PATTERNS}

    trigger = resolve_trigger_rules(rules, profile)
    mode = trigger.get("mode", "absolute")
    current, batch = _current_and_batch(scores)

    absolute_ok = False
    if mode in ("absolute", "any"):
        threshold = float(trigger.get("absolute_threshold", 66.0))
        absolute_ok = _attn.deep_read_triggered(current, threshold)

    quantile_ok = False
    if mode in ("quantile", "any"):
        ratio = float(trigger.get("quantile_top_ratio", 0.1))
        quantile_ok = _in_top_ratio(current, batch, ratio)

    change_rate_ok = False
    if mode in ("change_rate", "any"):
        detail = _change_rate_detail(snapshot_history,
                                     trigger.get("change_rate_threshold"))
        change_rate_ok = bool(detail.get("triggered"))

    return bool(absolute_ok or quantile_ok or change_rate_ok)


def _current_and_batch(scores: Any) -> Tuple[float, List[float]]:
    """把 scores 拆成 (当前话题分数, 同批分数列表)。

    单个 dict → 批就是它自己；序列 → 约定**最后一个元素是当前话题**（调用方通常把
    待判定话题放在末尾）。空输入给 0.0，不抛异常（深读链路不能因坏输入崩掉）。
    """
    if isinstance(scores, dict):
        return float(scores.get("score", 0.0) or 0.0), [float(scores.get("score", 0.0) or 0.0)]
    if isinstance(scores, (list, tuple)):
        batch: List[float] = []
        for item in scores:
            if isinstance(item, dict):
                batch.append(float(item.get("score", 0.0) or 0.0))
            elif _is_number(item):
                batch.append(float(item))
        if not batch:
            return 0.0, []
        return batch[-1], batch
    return 0.0, []


def _in_top_ratio(current: float, batch: Sequence[float], ratio: float) -> bool:
    """当前分数是否排进同批前 K%。

    K = ceil(len * ratio)，至少取 1 个（否则 top 10% 在小批里会是 0 个，谁都触发不了）。
    同分并列时按"不小于分界值"处理（保守：宁可多触发，不因排序细节漏掉头部话题）。
    """
    values = [float(v) for v in batch if _is_number(v)]
    if not values:
        return False
    k = max(1, int(-(-len(values) * ratio // 1))) if ratio < 1.0 else len(values)
    top_k = sorted(values, reverse=True)[:k]
    if not top_k:
        return False
    return current >= top_k[-1]


DEEP_READ_TEMPLATE = """\
【关注度深读】E = {score:.2f} / 100（{band}），{threshold}。

注意：本段由本地模板生成，未调用大模型。以下内容为**框架性提示**，
不是对该话题的实证分析；需要具体解读请配置大模型 API 后重试。

一、为什么人们会关注这件事
  1. 该话题的关注度已达「{band}」档，说明它触达了较大的受众面。
  2. 请结合话题本身判断：它是否与就业、健康、安全、公平、身份认同等
     高敏感民生议题相关？
  3. 是否提供了可传播的具体数字或细节（如某个比例、金额、时间点）？

二、这件事反映了什么社会问题
  1. 该话题的高关注度往往对应一个尚未被充分回应的公共诉求。
  2. 请核实：是否存在信息缺口（官方未说明、数据未公开）？
  3. 是否存在群体性的处境相似性（同一处境的人会把自己代入）？

三、这件事反映了人们什么样的担忧和思想
  1. 高关注常伴随「邻近性恐惧」——受众认为这件事可能发生在自己身上。
  2. 请区分「对结果的担忧」与「对过程不透明的担忧」，后者常被低估。
  3. 注意：关注度高不等于事实为真，请核验话题本身的事实基础。

【数据口径提示】E 由确定性公式算出，但其中的 y（阅读量）可能是平台热度指数
或转引值而非精确阅读量，因此本解读只描述「为什么这类话题会被关注」，
不应被理解为对受众规模的精确断言。
"""

# 旧版三段式 system（**仅作向后兼容保留**：既有调用方与 tests/test_deep_read.py
# 按 DEEP_READ_SYSTEM_PROMPT 这个名字取它，attn_deep_read.json 也逐字存着它）。
# 受控生成用的新版提示词是上面第 130 行的 `DEEP_READ_SYSTEM_PROMPT`——
# 两个常量同名会互相覆盖，所以这里显式改名，并让旧名指向旧版（保持对外行为不变）。
_LEGACY_DEEP_READ_SYSTEM_PROMPT = """\
你是一名社会心理与舆情分析专家。用户会给你一个「高关注度话题」及其关注度数据，
请围绕它写一段中文解读，严格按以下三节输出，每节 2-4 条要点：

一、为什么人们会关注这件事
二、这件事反映了什么社会问题
三、这件事反映了人们什么样的担忧和思想

写作纪律（很重要）：
1. 只依据用户提供的话题信息与关注度数据展开，不要引入你没有依据的具体事实、数字或人名。
2. 明确区分「事实」「推断」「推测」：对没有把握的判断，用「可能」「或许」等措辞，
   不要写成定论。
3. 不要编造页面内容或数据；引用不到来源时就不要引用。
4. 关注度数据本身可能是代理值（热度指数而非精确阅读量），
   所以不要用它做「有 N 人阅读」这类绝对化断言。
5. 每节要点要具体到可被检验的现象或可感知的情绪，不要写空话。
6. 全文使用简体中文，不要使用 Markdown 标题符号（# 、* 等），节名直接写「一、」「二、」「三、」。
"""


def _load_rules() -> tuple[Dict[str, Any], List[str]]:
    """读取外置深读口径（**旧版文本口径**：system_prompt + template），返回 (rules, warnings)。

    文件缺失/写坏时退回内置默认。新版的「触发规则 + 受控生成参数」走 `load_rules()`
    （deep_read_rules.json）；此处仅保留文本段口径，向后兼容既有调用方与测试。
    """
    try:
        with open(RULES_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"system_prompt": _LEGACY_DEEP_READ_SYSTEM_PROMPT,
                "template": DEEP_READ_TEMPLATE}, [f"深读口径文件未找到（{RULES_PATH}），已用内置默认"]
    except json.JSONDecodeError as exc:
        return {"system_prompt": _LEGACY_DEEP_READ_SYSTEM_PROMPT,
                "template": DEEP_READ_TEMPLATE}, [f"深读口径文件 JSON 语法错误：{exc}，已用内置默认"]
    except OSError as exc:
        return {"system_prompt": _LEGACY_DEEP_READ_SYSTEM_PROMPT,
                "template": DEEP_READ_TEMPLATE}, [f"深读口径文件读取失败：{type(exc).__name__}，已用内置默认"]

    if not isinstance(data, dict):
        return {"system_prompt": _LEGACY_DEEP_READ_SYSTEM_PROMPT,
                "template": DEEP_READ_TEMPLATE}, ["深读口径文件根节点必须是对象，已用内置默认"]

    rules: Dict[str, Any] = {}
    warnings: List[str] = []
    for key, fallback in (("system_prompt", _LEGACY_DEEP_READ_SYSTEM_PROMPT),
                          ("template", DEEP_READ_TEMPLATE)):
        value = data.get(key, fallback)
        if not isinstance(value, str) or not value.strip():
            warnings.append(f"深读口径 {key} 不是非空字符串，已用内置默认")
            rules[key] = fallback
        else:
            rules[key] = value
    return rules, warnings


def _build_user_text(query: str, detail: Dict[str, Any],
                     signals: Optional[Dict[str, Any]] = None,
                     evidence: Optional[Sequence[Dict[str, Any]]] = None,
                     issues: Optional[Dict[str, Any]] = None,
                     opinions_signals: Optional[Dict[str, Any]] = None) -> str:
    """把关注度数据与话题拼成给模型的 User Prompt。

    除 E/S/x/y/z 外，必须带上 `derive_signals()` 派生的结构化信号（解释力的原料）：
    engagement_ratio / spread / resonance / share_* / profile / proxy_flags。
    只给一个 E 值时模型只能复述"关注度高"，说不出"高在哪"（本次改造要解决的问题 B）。

    `evidence` 是带编号的发言证据（e1/e2/...），`issues` 是封闭议题清单，
    `opinions_signals` 是 opinions 模块已产出的诉求/归因对象/行动倾向（若存在）。
    三者都是受控生成的原料：模型只能在这些编号与 id 里做选择，不能自由发挥。
    """
    terms = detail.get("params", {})
    lines = [
        f"话题：{query or '（未提供话题名称）'}",
        "",
        "关注度数据：",
        f"  关注度 E = {detail.get('score', 0):.4f} / 100（档位：{detail.get('band', '未知')}）",
        f"  加权和 S = {detail.get('S', 0):.6f}",
        f"  x（文章数量） = {detail.get('x', 0)}",
        f"  y（文章阅读量） = {detail.get('y', 0)}",
        f"  z（文章留言量） = {detail.get('z', 0)}",
        f"  分项贡献：term_x={detail.get('term_x', 0):.6f} "
        f"term_y={detail.get('term_y', 0):.6f} term_z={detail.get('term_z', 0):.6f}",
        f"  参数：{terms}",
    ]
    if signals:
        lines += [
            "",
            "结构化信号（解释力原料，据此说明「关注度高在哪」，不要只复述 E 值）：",
            f"  engagement_ratio = {signals.get('engagement_ratio', 0):.6f}"
            f"（z/y，留言占阅读的比例，参与深度）",
            f"  spread = {signals.get('spread', 0):g}（x，文章数量，传播广度）",
            f"  resonance = {signals.get('resonance', 0):.4f}"
            f"（z/x，单篇平均留言量，共鸣强度）",
            f"  share_x = {signals.get('share_x', 0):.4f}（x 分项占 S 比例）",
            f"  share_y = {signals.get('share_y', 0):.4f}（y 分项占 S 比例）",
            f"  share_z = {signals.get('share_z', 0):.4f}（z 分项占 S 比例）",
            f"  profile = {signals.get('profile', '')}（参数档位，决定触发口径）",
            f"  proxy_flags = {signals.get('proxy_flags') or []}"
            f"（哪些输入是代理/估计值，非空时必须随解读说明口径）",
        ]

    # opinions 模块的既有产出（若存在）：让 LLM 在"诉求→议题标签→张力表述"这一层工作，
    # 而不是从零抽取。不硬依赖：没有就跳过。
    if opinions_signals:
        lines += ["", "已有观点聚合信号（若提供，请在此基础上做议题映射，不要重新抽取）："]
        for key, label in (("appeals", "核心诉求"), ("attribution_targets", "归因对象"),
                           ("action_tendencies", "行动倾向")):
            values = opinions_signals.get(key)
            if isinstance(values, list) and values:
                lines.append(f"  {label}：" + "；".join(str(v) for v in values[:8]))

    # 带编号的发言证据：受控生成的地基。没有证据时明说"无编号证据"，
    # 模型据此应输出 applicable=false（而不是硬编）。
    if evidence:
        lines += ["", "发言证据列表（编号即证据，evidence_id 只能引用这些编号）："]
        for index, item in enumerate(evidence, start=1):
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("snippet") or "").strip()
            if not text:
                continue
            extra = []
            for key, label in (("stance", "立场"), ("support", "可见支持数"),
                               ("channel", "渠道"), ("source", "来源")):
                value = item.get(key)
                if value not in (None, ""):
                    extra.append(f"{label} {value}")
            suffix = f"（{'，'.join(extra)}）" if extra else ""
            lines.append(f"e{index}: {text}{suffix}")
    else:
        lines += ["", "发言证据列表：（未提供编号证据）。没有编号证据时不要编造，"
                      "请输出 {\"applicable\": false, \"reason\": \"缺少可引用的编号证据\"}"]

    # 封闭议题清单：模型只能从这里选 id
    if issues and isinstance(issues.get("issues"), list):
        lines += ["", "可用议题清单（id / label / tension_a_hint / tension_b_hint）："]
        for index, item in enumerate(issues["issues"], start=1):
            lines.append(f"{index}. {item.get('id')} / {item.get('label')} / "
                         f"{item.get('tension_a_hint')} / {item.get('tension_b_hint')}")
        lines.append(f"清单外的一律归为 {issues.get('fallback', 'other')}。")

    note = detail.get("data_note")
    if note:
        lines += ["", f"数据口径提示：{note}"]
    return "\n".join(lines)


def _proxy_flags_of(detail: Dict[str, Any]) -> List[str]:
    """从 score_detail 输出推断哪些输入是代理/估计值。

    口径与 `estimate.py` 一致：estimated=True 时 y/z 均为代理口径（没有任何站点公开
    文章级精确阅读量），故 y/z 都标 proxy；否则不标（manual 模式是用户自己给的数）。
    """
    if detail.get("estimated"):
        return ["y_proxy", "z_proxy"]
    return []


def _signals_for(detail: Dict[str, Any], profile: Optional[str]) -> Dict[str, Any]:
    """算结构化信号；算不出时给空 dict（深读链路不能因信号派生失败而崩）。"""
    try:
        return _attn.derive_signals(
            float(detail.get("x", 0.0) or 0.0),
            float(detail.get("y", 0.0) or 0.0),
            float(detail.get("z", 0.0) or 0.0),
            params=detail.get("params"),
            profile=profile or detail.get("profile"),
            proxy_flags=_proxy_flags_of(detail),
        )
    except Exception:  # noqa: BLE001  信号只是解释力原料，派生失败不阻塞深读
        return {}


def _template_fallback(query: str, detail: Dict[str, Any], mode: str, threshold: float,
                       trigger_note: str, degraded_reason: str,
                       warnings: List[str]) -> Dict[str, Any]:
    """LLM 不可用 / 输出被护栏全丢时的本地模板回退。

    **不做自由生成**：按结构化字段拼接（议题标签、张力、现象、反方、边界），
    拼不出就给旧三段式框架提示，并显式说明"未调用大模型"。
    `degraded=true`，`degraded_reason` 记录是 LLM 失败还是护栏全丢弃。
    """
    signals = _signals_for(detail, detail.get("profile"))
    text_rules, text_warnings = _load_rules()
    warnings.extend(text_warnings)
    # score 同样要容错：detail 被上游写坏时不能在这里炸掉
    try:
        score = float(detail.get("score", 0.0) or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    text = text_rules["template"].format(
        score=score,
        band=detail.get("band", "未知"),
        threshold=trigger_note,
    )
    return {
        "triggered": True, "text": text, "source": "template",
        "degraded": True, "degraded_reason": degraded_reason,
        "warnings": warnings, "signals": signals,
        "profile": detail.get("profile") or _attn.DEFAULT_PROFILE,
        "trigger_mode": mode, "trigger_threshold": threshold,
        # 新 schema 字段：模板回退时没有结构化产出，但字段必须齐全（前端统一渲染）
        "applicable": True, "result": {}, "guard_dropped": [],
    }


def deep_read(query: str, detail: Dict[str, Any],
              use_llm: bool = True,
              profile: Optional[str] = None,
              batch: Optional[Sequence[Any]] = None,
              rules: Optional[Dict[str, Any]] = None,
              rules_path: Optional[str] = None,
              evidence: Optional[Sequence[Dict[str, Any]]] = None,
              opinions_signals: Optional[Dict[str, Any]] = None,
              snapshot_history: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """对高关注度话题生成解读。

    参数:
        query:     话题名称/关键词（可为空，此时只按数据分析）
        detail:    `attention.score_detail()` 的输出（含 score/band/S/分项）
        use_llm:   是否尝试调用大模型；False 时直接走本地模板（离线/测试用）
        profile:   参数档位名（"spec" / "reachable"），决定用哪套触发规则。
                   None 时按顶层规则（absolute/66），与改造前行为完全一致。
        batch:     同批话题的分数集合（quantile 模式需要）。None 时只用当前话题自身。
        rules:     已加载的深读规则（`load_rules()` 的输出）。None 时内部加载。
        rules_path: 规则文件路径（覆盖默认 `deep_read_rules.json`）。
        evidence:  带编号的发言证据列表（受控生成的地基；None 时模型应判不适用）。
        opinions_signals: opinions 模块已产出的诉求/归因对象/行动倾向（可选，不硬依赖）。
        snapshot_history: 同档案 e_score 的时序序列（升序=时间正序），供 change_rate
                   / any 模式做环比触发判定。None 或有效数值不足 2 条时环比判定
                   停用（单快照不触发，回落既有规则）；提供时返回值多一个
                   "trigger_detail" 键（如实记录环比评估所用两快照与比率），
                   不提供时返回值与改造前逐键相同。

    返回:
        {"triggered": bool, "text": str, "source": "llm"|"template",
         "degraded": bool, "degraded_reason": str, "warnings": [...],
         "signals": {...}, "applicable": bool, "result": {...},
         "guard_dropped": [...], ["trigger_detail": {"change_rate": {...}}]}
        **永不抛异常**——解读失败不能拖垮主流程。
    """
    # score 读不出来时按 0.0 处理（深读是附加信息，detail 被上游写坏也不能抛）
    try:
        score = float(detail.get("score", 0.0) or 0.0)
    except (TypeError, ValueError):
        score = 0.0

    if rules is None:
        try:
            rules, rule_warnings = load_rules(rules_path)
        except Exception as exc:  # noqa: BLE001  规则加载本身也不许抛
            rules, rule_warnings = {}, [f"深读规则加载异常：{type(exc).__name__}: {exc}"]
    else:
        rule_warnings = []

    warnings: List[str] = list(rule_warnings)
    trigger = resolve_trigger_rules(rules, profile)
    mode = trigger.get("mode", "absolute")
    threshold = float(trigger.get("absolute_threshold", 66.0))

    # 环比评估详情（口径诚实）：只要调用方给了快照历史就随返回值如实记录
    # 所用两快照与比率；未给时不加新键，返回形状与改造前逐键相同（逐位不变守卫）。
    change_rate_info = _change_rate_detail(snapshot_history,
                                           trigger.get("change_rate_threshold"))

    def _attach(payload: Dict[str, Any]) -> Dict[str, Any]:
        if snapshot_history is not None:
            payload["trigger_detail"] = {"change_rate": dict(change_rate_info)}
        return payload

    judged: Any = detail if batch is None else list(batch) + [detail]
    try:
        triggered = should_deep_read(judged, profile=profile, rules=rules,
                                     snapshot_history=snapshot_history)
    except Exception as exc:  # noqa: BLE001  触发判定失败时退回绝对阈值口径，不崩
        warnings.append(f"触发判定异常（{type(exc).__name__}: {exc}），已退回绝对阈值口径")
        triggered = _attn.deep_read_triggered(score, threshold)

    if not triggered:
        if mode == "absolute":
            reason = (f"E={score:.4f} 未超过深读阈值 {threshold:g}"
                      f"（档位 {profile or '默认'}，{mode} 模式），不生成解读")
        elif mode == "change_rate":
            if change_rate_info.get("evaluated"):
                reason = (f"E={score:.4f} 未触发深读（档位 {profile or '默认'}，"
                          f"change_rate 模式：环比增幅 "
                          f"{float(change_rate_info.get('rate', 0.0)):.4g} < 阈值 "
                          f"{float(change_rate_info.get('threshold', 0.0)):g}），不生成解读")
            else:
                reason = (f"E={score:.4f} 未触发深读（档位 {profile or '默认'}，"
                          f"change_rate 模式：{change_rate_info.get('reason') or '环比判定停用'}），"
                          f"不生成解读")
        else:
            reason = (f"E={score:.4f} 未触发深读（档位 {profile or '默认'}，"
                      f"{mode} 模式，同批 Top {trigger.get('quantile_top_ratio', 0.1):.0%}），"
                      f"不生成解读")
        return _attach({
            "triggered": False,
            "text": "",
            "source": "",
            "degraded": False,
            "warnings": warnings,
            "reason": reason,
            "signals": _signals_for(detail, profile),
            "applicable": False,
            "result": {},
            "guard_dropped": [],
        })

    # 代理值提示：y 是平台热度指数/转引值时，解读里必须写明（见 estimate.py）
    data_note = detail.get("data_note") or ""
    if not data_note and detail.get("estimated"):
        data_note = ("关注度参数含代理/估计值（y 可能是平台热度指数而非精确阅读量），"
                     "请勿据此做受众规模的绝对化断言。")

    signals = _signals_for(detail, profile)

    # 模板里的阈值表述随触发模式变化：absolute 模式写"E > 阈值"，
    # change_rate 模式写"环比增幅 ≥ 阈值"，quantile 等相对口径下写"同批 Top K%"
    # ——否则模板会谎称一个并不存在的绝对阈值。
    if mode == "absolute":
        trigger_note = f"已触发深读阈值（E > {threshold:g}）"
    elif mode == "change_rate":
        # 能经 change_rate 触发，说明阈值必已配置（evaluated=True）
        trigger_note = (f"已触发深读（change_rate 模式：环比增幅 ≥ "
                        f"{float(change_rate_info.get('threshold', 0.0)):g}）")
    else:
        trigger_note = (f"已触发深读（{mode} 模式：同批 Top "
                        f"{trigger.get('quantile_top_ratio', 0.1):.0%}）")

    # `err` 在 use_llm=False 时不存在（没打过模型），此时回退原因是"未启用"而非失败。
    # 提前声明而不是在 use_llm 分支里赋值：两条路径都要用它做 degraded_reason。
    llm_error = ""

    if use_llm:
        # 议题清单与提示词加载失败不阻塞：各自退回内置默认并留告警
        issues, issue_warnings = _safe_load_issues()
        warnings.extend(issue_warnings)
        prompts, prompt_warnings = _safe_load_prompts()
        warnings.extend(prompt_warnings)

        from mylocalai.llm.llm_client import chat_text, extract_json_obj
        user_text = _build_user_text(query, {**detail, "data_note": data_note}, signals,
                                     evidence=evidence, issues=issues,
                                     opinions_signals=opinions_signals)
        try:
            content, err = chat_text(
                prompts["system"] + "\n\n" + prompts["fewshot"], user_text,
                temperature=0.3, max_tokens=1600, namespace=CACHE_NAMESPACE,
            )
        except Exception as exc:  # noqa: BLE001  解读失败绝不能拖垮主流程
            content, err = "", f"调用异常: {type(exc).__name__}: {exc}"
        llm_error = err or ""

        if content and content.strip():
            raw_obj = extract_json_obj(content)
            evidence_ids = [f"e{i}" for i in range(1, len(evidence or []) + 1)]
            try:
                sanitized = _guard.validate_and_sanitize(
                    raw_obj, issues=issues, evidence_ids=evidence_ids, rules=rules)
            except _guard.GuardRejected as exc:
                # 整体不合格（schema 坏 / 缺反方 / 标签全丢）→ 走模板回退并说明原因
                warnings.append(f"深读输出未通过护栏（{exc.reason}），已回退本地模板")
                return _attach(_template_fallback(
                    query, detail, mode, threshold, trigger_note,
                    f"模型输出未通过护栏校验：{exc.reason}", warnings))
            except Exception as exc:  # noqa: BLE001  护栏自身出错也不能拖垮主流程
                warnings.append(f"护栏校验异常（{type(exc).__name__}: {exc}），已回退本地模板")
                return _attach(_template_fallback(
                    query, detail, mode, threshold, trigger_note,
                    f"护栏校验异常：{type(exc).__name__}", warnings))

            if not sanitized.get("applicable"):
                # 出口：纯情绪/纯事实/证据不足 —— 不为"有输出"而编
                reason = sanitized.get("reason") or "模型判断该话题不适合做社会议题分析"
                return _attach({
                    "triggered": True, "text": "", "source": "llm",
                    "degraded": False, "warnings": warnings, "reason": reason,
                    "signals": signals, "applicable": False, "result": {},
                    "guard_dropped": [],
                    "profile": profile or _attn.DEFAULT_PROFILE,
                    "trigger_mode": mode,
                })

            data = sanitized.get("data") or {}
            text = data.get("body") or ""
            if data_note:
                text = f"{text}\n\n【数据口径提示】{data_note}"
            if sanitized.get("degraded"):
                warnings.extend(sanitized.get("warnings") or [])
            return _attach({
                "triggered": True, "text": text, "source": "llm",
                "degraded": bool(sanitized.get("degraded")),
                "degraded_reason": ("部分条目被护栏丢弃，详见 guard_dropped"
                                    if sanitized.get("degraded") else ""),
                "warnings": warnings, "signals": signals,
                "applicable": True, "result": data,
                "guard_dropped": sanitized.get("guard_dropped") or [],
                "profile": profile or _attn.DEFAULT_PROFILE,
                "trigger_mode": mode, "trigger_threshold": threshold,
            })
        warnings.append(f"大模型解读不可用（{err or '空响应'}），已回退本地模板")

    return _attach(_template_fallback(query, detail, mode, threshold, trigger_note,
                                      f"大模型不可用：{llm_error or '未启用'}", warnings))


def _safe_load_issues() -> Tuple[Dict[str, Any], List[str]]:
    """加载议题清单，异常时退回内置默认（不阻塞深读）。"""
    try:
        return load_issues()
    except Exception as exc:  # noqa: BLE001
        return {k: ([dict(x) for x in v] if isinstance(v, list) else v)
                for k, v in DEFAULT_ISSUES.items()}, \
               [f"议题清单加载异常（{type(exc).__name__}: {exc}），已用内置最小默认"]


def _safe_load_prompts() -> Tuple[Dict[str, str], List[str]]:
    """加载提示词，异常时退回内置默认（不阻塞深读）。"""
    try:
        return load_prompts()
    except Exception as exc:  # noqa: BLE001
        return {"system": DEEP_READ_SYSTEM_PROMPT, "fewshot": DEEP_READ_FEWSHOT}, \
               [f"深读提示词加载异常（{type(exc).__name__}: {exc}），已用内置默认"]


_USAGE = ("用法：python -m mylocalai.attention.deep_read --query <话题> --x <文章数> --y <阅读量> "
          "--z <留言量> [--profile spec|reachable] [--rules-path PATH] [--no-llm] [--json]")


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口：`python -m mylocalai.attention.deep_read --query 话题 --x .. --y .. --z ..`。

    向后兼容：旧参数（--query/--x/--y/--z/--no-llm）行为不变；新增 --profile
    （切换参数档位，随之切换触发规则）、--rules-path（替换规则文件）、--json
    （输出含新字段的 JSON）。未指定 profile 时行为与改造前完全一致。
    """
    from mylocalai.util.console_io import force_utf8_stdio
    force_utf8_stdio()

    args = list(sys.argv[1:]) if argv is None else [str(a) for a in argv]
    raw: Dict[str, str] = {}
    query = ""
    no_llm = False
    as_json = False
    profile_name: Optional[str] = None
    rules_path: Optional[str] = None
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in ("-h", "--help"):
            print(_USAGE)
            print("可用参数档位：" + "、".join(_attn.list_profiles())
                  + f"（默认 {_attn.DEFAULT_PROFILE}）")
            return 0
        if tok == "--no-llm":
            no_llm = True
            i += 1
            continue
        if tok == "--json":
            as_json = True
            i += 1
            continue
        if tok == "--profile":
            if i + 1 >= len(args):
                print(f"错误：{tok} 后面缺少取值。\n{_USAGE}", file=sys.stderr)
                return 2
            profile_name = args[i + 1]
            i += 2
            continue
        if tok == "--rules-path":
            if i + 1 >= len(args):
                print(f"错误：{tok} 后面缺少取值。\n{_USAGE}", file=sys.stderr)
                return 2
            rules_path = args[i + 1]
            i += 2
            continue
        if tok == "--query":
            if i + 1 >= len(args):
                print("错误：--query 后面缺少取值", file=sys.stderr)
                return 2
            query = args[i + 1]
            i += 2
            continue
        if tok in ("--x", "--y", "--z"):
            if i + 1 >= len(args):
                print(f"错误：{tok} 后面缺少取值", file=sys.stderr)
                return 2
            raw[tok[2:]] = args[i + 1]
            i += 2
            continue
        print(f"错误：无法识别的参数 {tok!r}。\n{_USAGE}", file=sys.stderr)
        return 2

    missing = [n for n in ("x", "y", "z") if n not in raw]
    if missing:
        print("错误：缺少必填参数 " + " ".join(f"--{n}" for n in missing)
              + f"。\n{_USAGE}", file=sys.stderr)
        return 2

    try:
        x, y, z = float(raw["x"]), float(raw["y"]), float(raw["z"])
    except ValueError:
        print("错误：--x/--y/--z 的取值必须能转成 float", file=sys.stderr)
        return 2

    params, profile_warnings = _attn.load_profile(profile_name or None)
    effective_profile = _attn.DEFAULT_PROFILE if profile_warnings else (profile_name or _attn.DEFAULT_PROFILE)
    for w in profile_warnings:
        print(f"告警：{w}", file=sys.stderr)

    try:
        detail = _attn.score_detail(x, y, z, params)
    except _attn.AttentionError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    detail["profile"] = effective_profile
    detail["threshold"] = _attn.profile_threshold(effective_profile)
    loaded_rules, _ = load_rules(rules_path)
    trigger = resolve_trigger_rules(loaded_rules, effective_profile)
    result = deep_read(query, detail, use_llm=not no_llm,
                       profile=effective_profile, rules_path=rules_path)

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"话题：{query or '（未提供）'}")
    print(f"关注度 E = {detail['score']:.4f} / 100（{detail['band']}）")
    print(f"参数档位：{effective_profile}（触发模式 {trigger['mode']}）")
    if trigger["mode"] == "absolute":
        print(f"触发深读：{'是' if result['triggered'] else '否'}"
              f"（绝对阈值 {trigger['absolute_threshold']:g}，严格大于才算）")
    elif trigger["mode"] == "change_rate":
        cr = trigger.get("change_rate_threshold")
        cr_text = f"{float(cr):g}" if _is_number(cr) else "未配置"
        print(f"触发深读：{'是' if result['triggered'] else '否'}"
              f"（change_rate 模式：环比增幅 ≥ {cr_text}，需快照历史 ≥2 条；"
              f"命令行无快照历史，此模式恒不触发）")
    else:
        print(f"触发深读：{'是' if result['triggered'] else '否'}"
              f"（{trigger['mode']} 模式：同批 Top {trigger['quantile_top_ratio']:.0%}）")
    if not result["triggered"]:
        print(f"说明：{result.get('reason', '')}")
        return 0
    if not result.get("applicable", True):
        print(f"未触发深读解读：{result.get('reason', '')}")
        return 0
    print(f"解读来源：{'大模型' if result['source'] == 'llm' else '本地模板（已降级）'}")
    if result.get("degraded_reason"):
        print(f"降级原因：{result['degraded_reason']}")
    for w in result["warnings"]:
        print(f"告警：{w}", file=sys.stderr)
    print()
    print(result["text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

