# -*- coding: utf-8 -*-
"""辟谣方法指南（通用技能，零检索纯知识）：教用户自行识别与查证谣言的方法。

改了什么、为什么、锁定什么：
  - 新增 `assistant.skills.guide.guide`：输入用户问题 → 一次 LLM 受约束讲解 →
    代码校验 → 组装"五步法 + 讲解 + 下一步提示"（`fact-check-guide` /
    `GuideSkills.run`）。
  - **零检索零取页**：本模块**禁止 import 任何检索/取页函数**（结构上就没有
    search_fn/fetch_fn/transport 注入位），只经注入的 llm_fn 做一次讲解调用——
    教方法是纯知识任务，不需要也不允许外发检索请求。
  - **五步法口径代码锁定**：看出处 / 交叉验证 / 查原始引用 / 识别情绪化标题与
    断言强度 / 理解证据等级。steps 与 follow_up_hint 由代码生成，模型无权改动；
    模型只写讲解正文（answer），且不得新增 URL、不得出现真假判定词、不对具体
    事件说法给真假结论——越界即丢弃并降级为内置五步法模板。
  - **每个出口都有降级**：模型不可用/超时/输出越界 → state=degraded，answer 用
    内置模板兜底（五步法清单照常输出）；输入为空/超长 → needs_clarification；
    死线/预算到顶 → budget_exhausted；取消 → cancelled。绝不把"模板兜底"说成
    "模型讲解成功"（state 如实标 degraded）。
  - 全离线可测：llm_fn / clock 可注入，零网络（除注入的 llm_fn 桩）。

与 collect_core 的关系：预算/状态/SKILL.md 装配复用共享底座；本模块只做编排、
口径锁定与降级模板，不重复实现那套东西。

依赖方向：只 import 标准库与 assistant 内部包，**不 import retrieval / transport /
web / platform / dossier**（保证零外发检索/取页请求）。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from mylocalai.assistant.skills.collect_core import (
    CollectBudget,
    CollectRunState,
    load_collect_rules,
    load_skill_prompt,
    utc_now_iso,
)
from mylocalai.assistant.skills.pipeline import normalize_text

__all__ = ["GuideSkills", "guide_fact_check", "render_report", "load_rules",
           "FIVE_STEPS", "FIVE_STEP_TITLES"]

#: 用户可见的三个进度阶段；不暴露内部技能名
_PROGRESS_STAGES = ("解析问题", "组织讲解", "整理输出")

#: 查证五步法（口径代码锁定；SKILL.md 正文与 steps 输出都用这一份，模型不得改动）
FIVE_STEPS: Tuple[str, ...] = (
    "看出处：先看作者、发布机构是否可识别、可追责——域名是什么、账号主体是谁、"
    "出了错能不能找到责任方；查无此人的“网友爆料”先存疑。",
    "交叉验证：拿同一条信息去多个相互独立的信源核对，官方通报、权威媒体、"
    "专业机构说法一致才可信；只有单一来源在传、互相转来转去的，先按存疑处理。",
    "查原始引用：转述常常走样——找到它引用的原始文件、原始发布或原始数据，"
    "对照原话看有没有断章取义、偷换时间地点或夸大数据。",
    "识别情绪化标题与断言强度：警惕绝对化措辞（100%、全都、立刻扩散）、"
    "恐吓式提醒（速转！不转不是中国人）、煽动愤怒或恐慌的标题——情绪越满，"
    "越要慢下来核实。",
    "理解证据等级：官方文件 > 权威媒体报道 > 自媒体转述；等级越低越要存疑，"
    "截图、聊天记录、“听说”都不能单独当证据。",
)

#: 五步法短标题（摘要/卡片用）
FIVE_STEP_TITLES: Tuple[str, ...] = ("看出处", "交叉验证", "查原始引用",
                                     "识别情绪化标题与断言强度", "理解证据等级")

#: 下一步提示（代码模板，模型无权改动）：想核查具体说法 → 引导到 verify_claim /
#: verify_authority（取证与权威核验流程），本技能不对具体说法下结论。
FOLLOW_UP_HINT = ("想核查某个具体说法时，直接说“帮我核验：<说法>”，"
                  "我会走取证与权威核验流程（verify_claim / verify_authority），"
                  "逐条找可定位引文与权威证据倾向，而不是凭印象判断。")

#: 讲解的机器契约：模型只写 answer，且五步法结构由代码给定
_ANSWER_CONTRACT = ('只输出 JSON：{"answer":"<讲解正文>"}；讲解必须围绕“查证五步法”'
                    "逐步展开（看出处、交叉验证、查原始引用、识别情绪化标题与断言强度、"
                    "理解证据等级），只教方法；不对用户问题里的具体事件/说法给出真假"
                    "结论；不给数值分；不新增 URL/链接/日期；不超过 1200 字。")

#: 固定真假判定词（铁律 1）：讲解正文里一个都不许出现
_VERDICT_WORDS = ("是真的", "是假的", "属实", "谣言为真", "基本属实", "不实")
_VERDICT_RE = re.compile("|".join(re.escape(w) for w in _VERDICT_WORDS))
_URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）】]+", re.I)

_HERE = Path(__file__).resolve().parent
_SKILL_DIR = _HERE

# 内置默认规则（本技能不外置 rules.json：纯知识任务没有可运维的检索/取页预算，
# 只留输入/讲解长度与一次模型调用的上限；load_rules 仍支持传路径/覆盖项以便测试）。
DEFAULT_GUIDE_RULES: Dict[str, Any] = {
    "max_input_chars": 2000,
    "max_llm_calls": 1,
    "total_deadline_s": 30.0,
    "max_answer_chars": 1600,
}

# 数值安全区间（键名锁定）
_RULE_LIMITS: Dict[str, Tuple[float, float]] = {
    "max_input_chars": (50, 200000),
    "max_llm_calls": (1, 10),
    "total_deadline_s": (1, 3600),
    "max_answer_chars": (200, 20000),
}

_QUESTION_MAX_CHARS = 300


def load_rules(path: Optional[Any] = None) -> Dict[str, Any]:
    """加载指南规则：不传路径时用内置默认（无 rules.json 可读），传 dict 按
    覆盖项处理，传路径读该文件。缺键回退默认、数值夹取、坏类型回退、未知键丢弃。"""
    if isinstance(path, Mapping):
        merged = dict(DEFAULT_GUIDE_RULES)
        merged.update({k: v for k, v in path.items() if k in DEFAULT_GUIDE_RULES})
        return _clamp_rules(merged)
    return load_collect_rules(Path(path) if path else None,
                              DEFAULT_GUIDE_RULES, _RULE_LIMITS)


def _clamp_rules(rules: Mapping[str, Any]) -> Dict[str, Any]:
    """与 collect_core.load_collect_rules 同口径的夹取（dict 覆盖项没有文件可读）。"""
    out: Dict[str, Any] = {}
    for key, default in DEFAULT_GUIDE_RULES.items():
        value = rules.get(key, default)
        if isinstance(default, bool):
            out[key] = value if isinstance(value, bool) else default
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = default
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            number = float(default)
        low, high = _RULE_LIMITS[key]
        number = min(max(number, low), high)
        out[key] = int(round(number)) if isinstance(default, int) else number
    return out


def _clean(value: Any, limit: int = 200) -> str:
    """压空白 + 截断：所有进入输出结构的自由文本都过这里。"""
    return normalize_text(value)[:limit]


def _template_answer() -> str:
    """降级模板：模型不可用/输出越界时，用内置口径讲一遍五步法（代码生成）。"""
    lines = ["本次模型讲解不可用，先用内置的“查证五步法”讲一遍自行识别与查证谣言的方法："]
    for index, step in enumerate(FIVE_STEPS, start=1):
        lines.append(f"{index}. {step}")
    lines.append("五步走完仍然存疑的说法，先不要转发；把它交给核查流程处理，"
                 "不要凭印象下结论。")
    return "\n".join(lines)


class GuideSkills:
    """辟谣方法指南流水线（解析问题 → 一次受约束讲解 → 代码校验 → 组装输出）。

    只有 `llm_fn(system, user) -> (obj, err)`（chat_json 契约）与 clock 可注入；
    **没有** search_fn/fetch_fn/transport 注入位——本技能零检索零取页，结构上
    就发不出外发检索/取页请求。
    """

    def __init__(self, *, rules: Optional[Any] = None,
                 llm_fn: Optional[Callable[[str, str], Any]] = None,
                 clock: Optional[Callable[[], str]] = None,
                 on_progress: Optional[Callable[[str, int, int], Any]] = None) -> None:
        self.rules = load_rules(rules)
        self.llm_fn = llm_fn
        self.clock = clock or utc_now_iso
        self.on_progress = on_progress
        self._on_progress: Optional[Callable[[str, int, int], Any]] = None

    # -- 入口 ---------------------------------------------------------------

    def run(self, question: str, *, cancel_check: Optional[Callable[[], bool]] = None,
            deadline: Optional[float] = None,
            on_progress: Optional[Callable[[str, int, int], Any]] = None) -> Dict[str, Any]:
        """跑一次指南讲解，返回统一运行外壳（data 键：question/state/answer/
        steps/follow_up_hint，键名锁定）。"""
        started_at = self.clock()
        self._on_progress = on_progress or self.on_progress
        raw = "" if question is None else str(question)
        if deadline is None:
            # 未传死线时按 total_deadline_s 自建：预算上限不该因为调用方没传就失效
            deadline = time.monotonic() + float(self.rules["total_deadline_s"])
        budget = CollectBudget(self.rules, deadline)
        st = CollectRunState(budget, cancel_check)
        st.data = {"question": "", "state": "degraded", "answer": "",
                   "steps": list(FIVE_STEPS), "follow_up_hint": FOLLOW_UP_HINT}

        if not self._precheck(st, raw):
            return self._finish(st, started_at)
        st.data["question"] = _clean(raw, _QUESTION_MAX_CHARS)
        self._progress(st, _PROGRESS_STAGES[0], 1)
        if st.is_cancelled():
            st.gap("运行在组织讲解前被取消")
            self._fallback(st, "运行被取消，本次只有内置五步法清单")
            return self._finish(st, started_at)

        self._stage_answer(st, raw)
        self._progress(st, _PROGRESS_STAGES[1], 2)
        if not st.data["answer"]:
            self._fallback(st, "讲解未生成，使用内置五步法模板")
        self._progress(st, _PROGRESS_STAGES[2], 3)
        return self._finish(st, started_at)

    # -- 阶段：预检 ---------------------------------------------------------

    def _precheck(self, st: CollectRunState, raw: str) -> bool:
        if not raw.strip():
            st.error("precheck", "empty_input", "问题为空或只有空白字符")
            st.clarify("输入为空：请说一说你想弄清楚什么（例如“怎么辨别养生类谣言”）")
            return False
        limit = int(self.rules["max_input_chars"])
        if len(raw) > limit:
            st.error("precheck", "input_too_long",
                     f"问题 {len(raw)} 字符，超过上限 {limit}")
            st.clarify(f"输入长度 {len(raw)} 超过单次上限 {limit}：请一句话说明想学哪种辨别方法")
            return False
        return True

    # -- 阶段：受约束讲解 ---------------------------------------------------

    def _stage_answer(self, st: CollectRunState, raw: str) -> None:
        """一次 LLM 讲解 + 代码校验。越界/不可用 → 保持 degraded，走模板兜底。"""
        if self.llm_fn is None:
            st.error("guide", "llm_unavailable", "未注入模型实现，讲解未生成")
            st.gap("模型讲解不可用，使用内置五步法模板")
            return
        if not st.budget.allow("max_llm_calls"):
            st.budget.block(st.budget.why("max_llm_calls"))
            return
        if st.is_cancelled():
            st.gap("运行在讲解前被取消")
            return
        st.budget.spend("max_llm_calls")
        entry = st.operation("llm", "guide_answer", False)
        system = load_skill_prompt(_SKILL_DIR, _ANSWER_CONTRACT)
        user = (f"用户问题：\n{raw}\n\n"
                "查证五步法（讲解必须覆盖这五步，顺序可调整）：\n" +
                "\n".join(f"{i}. {title}" for i, title in enumerate(FIVE_STEP_TITLES, 1)))
        try:
            out = self.llm_fn(system, user)
        except Exception as exc:  # noqa: BLE001  模型侧异常按"本次讲解失败"处理
            st.error("guide", "llm_exception", f"模型调用异常：{type(exc).__name__}")
            st.gap("模型讲解不可用，使用内置五步法模板")
            return
        obj = None
        if isinstance(out, tuple) and len(out) == 2:
            obj, err = out
            if not isinstance(obj, dict):
                st.error("guide", "llm_call_failed", str(err) or "模型未给出可解析的讲解")
                st.gap("模型讲解未生成，使用内置五步法模板")
                return
        elif isinstance(out, dict):
            obj = out
        else:
            st.error("guide", "llm_bad_shape", "模型返回形状不符")
            st.gap("模型讲解未生成，使用内置五步法模板")
            return
        answer = _clean(obj.get("answer"), int(self.rules["max_answer_chars"]))
        if not answer:
            st.error("guide", "answer_empty", "讲解正文为空，已丢弃")
            st.gap("模型讲解为空，使用内置五步法模板")
            return
        if _URL_RE.search(answer):
            # 指南零外发：讲解里不许塞链接（教方法不需要 URL，也避免引导用户点开未知站）
            st.error("guide", "answer_new_url", "讲解正文新增了 URL，已整条丢弃")
            st.gap("模型讲解越界（含 URL），使用内置五步法模板")
            return
        if _VERDICT_RE.search(answer):
            # 铁律 1：指南不判具体说法真假，判定词一出现整条丢弃
            st.error("guide", "answer_verdict_word", "讲解正文含固定真假判定词，已丢弃")
            st.gap("模型讲解越界（含真假判定词），使用内置五步法模板")
            return
        entry["ok"] = True
        st.data["answer"] = answer
        st.data["state"] = "ok"

    # -- 阶段：降级 ---------------------------------------------------------

    def _fallback(self, st: CollectRunState, reason: str) -> None:
        """降级出口：answer 用内置五步法模板；state 保持 degraded（如实标注）。"""
        if not st.data["answer"]:
            st.data["answer"] = _template_answer()
        if reason and reason not in st.gaps:
            st.gap(reason)

    # -- 收尾 ---------------------------------------------------------------

    def _progress(self, st: CollectRunState, stage: str, done: int) -> None:
        hook = self._on_progress
        if hook is None:
            return
        try:
            hook(stage, done, len(_PROGRESS_STAGES))
        except Exception:  # noqa: BLE001  进度回调异常不影响指南结果本身
            pass

    def _finish(self, st: CollectRunState, started_at: str) -> Dict[str, Any]:
        core = st.finish()
        audit = st.audit
        result = {
            "tool_id": "guide_fact_check",
            "status": core["status"],
            "task_status": core["task_status"],
            "data": core["data"],
            "gaps": list(core["gaps"]),
            "errors": list(core["errors"]),
            # 计数取预算实耗（与 authority/research 同口径）：operations 行的 ok
            # 是事后回填的，audit["llm_calls"] 只按创建时点计数，不可靠。
            "audit": {"operations": list(audit.get("operations") or []),
                      "searches": 0, "pages": 0,
                      "llm_calls": int(st.budget.used.get("max_llm_calls") or 0)},
            "budget": core["budget"],
            "report": "",
            "started_at": started_at,
            "finished_at": self.clock(),
        }
        if core["status"] == "needs_clarification":
            result["gaps"].insert(0, "需要用户补充：请说明想学哪种辨别/查证方法")
        if st.is_cancelled():
            result["gaps"].insert(0, "运行被取消，已保留内置五步法清单")
        if st.budget.blocked_reason:
            result["gaps"].insert(0, f"预算/死线到顶提前停止（{st.budget.blocked_reason}），"
                                     "已保留内置五步法清单")
        result["report"] = render_report(result)
        return result


def guide_fact_check(question: str, **kw) -> Dict[str, Any]:
    """便捷函数 = GuideSkills(**kw).run(question)。工具层用它。"""
    return GuideSkills(**kw).run(question)


# ---------------------------------------------------------------------------
# 确定性纯文本报告
# ---------------------------------------------------------------------------

def _report_field(value: Any, limit: int = 160) -> str:
    """非正文字段的渲染：去控制字符、把 HTML/markdown 标记字符换成全角等价物、压空白、截断。"""
    text = normalize_text(value)
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    for raw, safe in (("<", "＜"), (">", "＞"), ("[", "［"), ("]", "］")):
        text = text.replace(raw, safe)
    return text[:limit]


def render_report(result: Dict[str, Any]) -> str:
    """把一次指南结果渲染成确定性纯文本报告（无 HTML、无 markdown 链接语法）。"""
    if not isinstance(result, dict):
        return "辟谣方法指南报告：（结果形状不符，无可渲染内容）"
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    steps = [str(s) for s in (data.get("steps") or [])]

    lines: List[str] = ["辟谣方法指南报告（只教方法，不对具体说法下结论）"]
    lines.append(f"执行状态：{_report_field(result.get('status'), 40) or '（未知）'}"
                 f"　任务状态：{_report_field(result.get('task_status'), 40) or '（未知）'}"
                 f"　讲解状态：{_report_field(data.get('state'), 20) or '（未知）'}")
    lines.append(f"开始：{_report_field(result.get('started_at'), 40)}"
                 f"　结束：{_report_field(result.get('finished_at'), 40)}")
    lines.append(f"问题：{_report_field(data.get('question'), 300)}")
    lines.append(f"查证五步法（共 {len(steps)} 步，口径代码锁定）：")
    for index, step in enumerate(steps, start=1):
        lines.append(f"  {index}. {_report_field(step, 200)}")
    lines.append("讲解：")
    for line in _report_field(data.get("answer"), 4000).splitlines():
        lines.append(f"  {line}")
    lines.append(f"下一步：{_report_field(data.get('follow_up_hint'), 200)}")
    gaps = [_report_field(g, 240) for g in (result.get("gaps") or [])]
    lines.append(f"缺口（共 {len(gaps)} 条）")
    for index, text in enumerate(gaps, start=1):
        lines.append(f"  {index}. {text}")
    errors = [e for e in (result.get("errors") or []) if isinstance(e, dict)]
    lines.append(f"错误（共 {len(errors)} 条）")
    for item in errors:
        lines.append(f"  - [{_report_field(item.get('stage'), 20)}] "
                     f"{_report_field(item.get('category'), 40)}："
                     f"{_report_field(item.get('detail'), 200)}")
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    lines.append("预算与可观测："
                 f"模型调用 {int(audit.get('llm_calls') or 0)} 次；"
                 "检索 0 次、读页 0 次（本技能零检索零取页）")
    return "\n".join(lines)
