# 目标文件: 本仓库（路径按部署位置而定，勿写死盘符）
# -*- coding: utf-8 -*-
"""
网络言论智能分析系统 —— 网站后端（FastAPI）。

职责：
  - 复用项目根目录的分析链路 core_judge.judge（三个 API 分析模块 + 规则引擎 + 报告生成）
  - 提供 REST 接口：
      POST /api/analyze       分析一段文本
      POST /api/attention     关注度评分（手动传 x/y/z，纯确定性、不调大模型）
      POST /api/attention/search  关注度评分（输入话题，联网估出 x/y/z 后评分）
      GET  /api/history       最近的分析记录（内存保存，重启清空）
      GET  /api/health        健康检查
  - 托管 static/ 下的前端页面，单进程即可前后端一体运行
  - 交互式接口文档：http://127.0.0.1:8000/docs

启动（在项目根目录的 venv 中）：
    cd my_website
    ../.venv/Scripts/python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
    然后浏览器访问 http://127.0.0.1:8000
（路径按你的实际项目位置替换；也可直接双击 my_website/start.bat。）
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 关注度模块：纯确定性、不调用大模型。放在这里导入而不是函数内，导入失败时
# /api/health 仍能报出来（前端据此把关注度面板置灰，而不是让用户点了才报错）。
from mylocalai.attention import attention as _attn  # noqa: E402
from mylocalai.attention.deep_read import deep_read as _deep_read  # noqa: E402
from mylocalai.attention.estimate import estimate_from_search  # noqa: E402
from mylocalai.classic.judge import judge, judge_deadline_s  # noqa: E402
from mylocalai.classic.language import analyze_aggression, analyze_emotion  # noqa: E402
from mylocalai.classic.logic import analyze_logic  # noqa: E402
from mylocalai.llm import llm_cache  # noqa: E402
from mylocalai.llm import llm_client as _llm_client  # noqa: E402
from mylocalai.util.console_io import force_utf8_stdio  # noqa: E402
from mylocalai.util.text_safety import has_surrogates, sanitize_text  # noqa: E402

# 非 UTF-8 控制台（英文 Windows 的 cp1252 等）下，日志里的中文会抛 UnicodeEncodeError。
force_utf8_stdio()

MAX_TEXT_LENGTH = 8000   # 与前端输入框上限一致（Agent 模式下同为此上限）
HISTORY_LIMIT = 50       # 内存历史最多保留条数

VALID_MODES = ("classic", "agent")

# 应用层并发上限：框架层（anyio 线程池）默认 40 个工作线程，超过只会排队到线程池，
# 而单次 analyze 最差可挂 8 分钟——40 条这样的请求足以把 /api/health、/api/history
# 一起阻塞（它们同为同步端点，共用线程池）。所以这里显式限制在跑的分析数，
# 超出的请求立刻返回 503（带 Retry-After），而不是堆在池子里。
MAX_CONCURRENT_ANALYSES = 4
ANALYZE_QUEUE_TIMEOUT_S = 2.0   # 等待并发名额的时间；等到就继续跑，超时就回 503

_analyze_slots = threading.BoundedSemaphore(MAX_CONCURRENT_ANALYSES)
_active_analyses = 0
_active_lock = threading.Lock()

# 谣言 Agent 单例：惰性创建（首次 agent 请求才加载 langchain），analyze() 每次调用
# 自建独立 AnalysisState/工具集，实例本身无请求态，可安全并发复用。
_rumor_agent = None
_rumor_agent_error = ""


def get_rumor_agent():
    global _rumor_agent, _rumor_agent_error
    if _rumor_agent is None and not _rumor_agent_error:
        try:
            from mylocalai.rumor import RumorAgent
            # classic 三维组件由入口层装配注入（依赖方向：web → {classic, rumor}），
            # rumor 包内部不再 import classic。
            _rumor_agent = RumorAgent(
                max_chars=MAX_TEXT_LENGTH,
                funcs={
                    "emotion": analyze_emotion,
                    "aggression": analyze_aggression,
                    "logic": analyze_logic,
                },
            )
        except Exception as exc:  # noqa: BLE001  依赖缺失/配置错误时给出可读的 503
            _rumor_agent_error = f"{type(exc).__name__}: {exc}"
    return _rumor_agent

app = FastAPI(
    title="网络言论智能分析系统 API",
    description="情感 / 攻击性 / 逻辑三维分析 + 规则引擎裁判 + 报告生成（复用项目根目录分析链路）",
    version="1.0.0",
)

# 内存历史（最新在前）。仅作演示用途：重启服务即清空，不落盘。
_history: List[Dict[str, Any]] = []

# ---- 多进程部署检测（2026-10-22 修复 S5）----
# 进程内结果缓存与内存历史都是**每进程一份**的：多 worker 部署时，同一段文本
# 在不同 worker 上可能拿到不同结论（上游采样漂移 + 缓存互不共享）。
# 启动时提示一次，避免这种错判一直静默。uvicorn 的 --workers 不会自动设环境变量，
# 所以多进程部署请显式设 APP_WORKERS，或用响应里的 cache_instance 字段观察。
_WORKERS = 1
for _var in ("APP_WORKERS", "UVICORN_WORKERS", "WEB_CONCURRENCY"):
    try:
        _WORKERS = int(os.environ.get(_var, "1") or "1")
    except (TypeError, ValueError):
        _WORKERS = 1
    if _WORKERS > 1:
        break

_warned_multiworker = False


def _warn_if_multiworker() -> None:
    global _warned_multiworker
    if _WORKERS > 1 and not _warned_multiworker:
        _warned_multiworker = True
        import logging
        logging.getLogger("my_website").warning(
            "检测到多进程部署（WORKERS=%s）：进程内结果缓存与内存历史各自独立，"
            "同一段文本在不同 worker 上可能给出不同结论。如需跨进程一致，"
            "请用单 worker 启动（uvicorn 不带 --workers），或改用外部缓存。", _WORKERS)


# 启动时提示一次（放在函数定义之后，避免 NameError）
_warn_if_multiworker()


class AnalyzeRequest(BaseModel):
    """POST /api/analyze 的请求体。

    mode: classic = 三维分析+规则引擎（默认）；agent = 谣言识别 Agent（联网取证，较慢）。
    """
    text: str
    mode: str = "classic"


class AttentionRequest(BaseModel):
    """POST /api/attention 的请求体：手动给定三个输入，纯确定性算分。

    x: 文章数量；y: 文章阅读量；z: 文章留言量。三者必须 >= 0。
    profile: 参数档位名，留空用默认档（"spec"，即用户指定的原始参数）。
    """

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    profile: str = ""


class AttentionSearchRequest(BaseModel):
    """POST /api/attention/search 的请求体：给一个话题，联网估出 x/y/z 再算分。

    query: 话题关键词（与在搜索框里输入的内容一致）
    profile: 参数档位名，留空用默认档
    """

    query: str = ""
    profile: str = ""


@app.get("/api/health")
def health() -> Dict[str, Any]:
    """健康检查：前端用它显示服务状态。

    这个端点**不碰大模型链路**，因此即使上游挂掉、分析请求把并发名额占满，
    它也能立刻回话——这是"服务还活着"的信号，别再让它跟着一起卡。

    注意 `status=ok` 只表示**网站进程**活着，不代表上游可用；上游的配置与可用性
    在 `llm` 字段里单独给出（2026-10-22 检验报告发现 9）。
    """
    llm_ok, llm_detail = _llm_client.validate_config()
    return {
        "status": "ok",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "history_count": len(_history),
        "active_analyses": _active_analyses,
        "max_concurrent": MAX_CONCURRENT_ANALYSES,
        "analyze_budget_s": judge_deadline_s(),
        "llm_cache": llm_cache.stats(),
        "llm": {
            "usable": llm_ok,
            "provider": _llm_client.get_provider(),
            "model": _llm_client.get_model(),
            "base_url": _llm_client.get_base_url(),
            "error": "" if llm_ok else llm_detail,
        },
    }


class _Slot:
    """并发名额：拿不到就抛 503（带 Retry-After），不把请求堆在框架线程池里。"""

    def __enter__(self):
        global _active_analyses
        if not _analyze_slots.acquire(timeout=ANALYZE_QUEUE_TIMEOUT_S):
            raise HTTPException(
                status_code=503,
                detail=f"服务正忙（同时在跑的分析已达 {MAX_CONCURRENT_ANALYSES} 个），请稍后重试",
                headers={"Retry-After": "5"},
            )
        with _active_lock:
            _active_analyses += 1
        return self

    def __exit__(self, *exc):
        global _active_analyses
        with _active_lock:
            _active_analyses -= 1
        _analyze_slots.release()
        return False


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> Dict[str, Any]:
    """
    分析一段文本。

    mode=classic（默认）：core_judge.judge 串行调用情感/攻击性/逻辑三个分析模块
    （LongCat API），规则引擎得出四档结论，report_generator 生成报告。
    mode=agent：谣言识别 Agent 调度 7 个分析工具（含联网搜索证据本），
    标签与 0-100 风险分由确定性规则计算，耗时约 30-120 秒。
    两种模式单模块失败均不影响整体；整条链路有请求级时长预算（JUDGE_DEADLINE_S，
    默认 120 秒），预算用尽即快速降级，不会挂满几分钟。
    """
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="输入不能为空")
    # 孤立代理项（例："ok\ud800bad"）是无效文本：留着会在编码/序列化处出错。
    # 这是用户输入的问题，回 400，并规整后再走分析——别把内部异常当 500 回给调用方。
    if has_surrogates(text):
        raise HTTPException(status_code=400, detail="输入包含无效字符（无法编码的 Unicode 代理项），请检查后重试")
    if len(text) > MAX_TEXT_LENGTH:
        raise HTTPException(status_code=400, detail=f"文本过长（最多 {MAX_TEXT_LENGTH} 字）")
    mode = (req.mode or "classic").strip().lower()
    if mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail=f"不支持的分析模式: {req.mode}（可选 classic / agent）")

    t0 = time.perf_counter()
    with _Slot():
        if mode == "agent":
            return _analyze_agent(text, t0)
        cache_before = llm_cache.stats()
        try:
            result = judge(text)
        except Exception as exc:  # noqa: BLE001  judge 各模块自带容错，此为兜底
            raise HTTPException(status_code=500, detail=f"分析链路异常: {type(exc).__name__}: {exc}") from exc
        cache_after = llm_cache.stats()
        elapsed = round(time.perf_counter() - t0, 1)

        record: Dict[str, Any] = {
            "id": f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{len(_history) + 1}",
            "text": text,
            "mode": "classic",
            "conclusion": result.get("conclusion", "未知"),
            "emotion": result.get("emotion", {}),
            "attack": result.get("attack", {}),
            "logic": result.get("logic", {}),
            "report": result.get("report", ""),
            # 命中缓存的组件数与缓存实例：多进程部署时每个 worker 各有一份缓存，
            # 同一段文本在不同 worker 上可能得到不同结论（2026-10-22 修复 S5）
            "cache_hits": max(0, int(cache_after.get("hits", 0)) - int(cache_before.get("hits", 0))),
            "cache_instance": cache_after.get("instance", ""),
            # 降级清单：哪些组件没给出可用结果、报告是否走了本地模板回退。
            # 改造前 classic 记录里没有这个字段，前端分不清"正常结论"和"硬撑出来的结论"
            # （2026-10-22 检验报告发现 9）。
            "degraded_components": result.get("degraded_components", []),
            "empty_input": result.get("empty_input", False),
            "elapsed": elapsed,
            "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            # 关注度：对本次分析的文本算一个确定性分数。x/y/z 取不到真实值
            # （本模式不联网），因此 estimated=True，前端必须标注「估计」。
            "attention": _attention_for_text(text, estimated=True),
        }
        _history.insert(0, record)
        del _history[HISTORY_LIMIT:]
        return record


def _analyze_agent(text: str, t0: float) -> Dict[str, Any]:
    """Agent 模式：调用谣言识别 Agent，把结果整理成与经典模式同构的历史记录。"""
    agent = get_rumor_agent()
    if agent is None:
        raise HTTPException(status_code=503,
                            detail=f"谣言 Agent 初始化失败：{_rumor_agent_error or '未知原因'}")
    try:
        r = agent.analyze(text)
    except Exception as exc:  # noqa: BLE001  analyze 设计上不抛异常，此为兜底
        raise HTTPException(status_code=500, detail=f"Agent 分析异常: {type(exc).__name__}: {exc}") from exc
    elapsed = round(time.perf_counter() - t0, 1)

    record: Dict[str, Any] = {
        "id": f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{len(_history) + 1}",
        "text": text,
        "mode": "agent",
        # conclusion 复用经典记录的字段名，历史列表可以统一渲染
        "conclusion": r.get("label", "未知"),
        "risk_score": r.get("risk_score", 0),
        "confidence": r.get("confidence", ""),
        "speech_tier": r.get("speech_tier", ""),
        # 三维分析结果：与 classic 记录同名字段，前端可复用同一套渲染
        # （2026-10-22 修复 F8：此前 agent 模式只看得到风险分，看不到三个维度）
        "emotion": r.get("emotion") or {},
        "attack": r.get("attack") or {},
        "logic": r.get("logic") or {},
        "has_factual_claim": r.get("has_factual_claim"),
        "claims": r.get("claims", []),
        "signals": r.get("signals", []),
        "score_breakdown": r.get("score_breakdown", {}),
        "evidence_book": r.get("evidence_book"),
        "explanation": r.get("explanation", ""),
        "explanation_source": r.get("explanation_source", ""),
        "advice": r.get("advice", ""),
        "degraded_components": r.get("degraded_components", []),
        "agent_mode": r.get("mode", ""),
        "agent_error": r.get("agent_error", ""),
        "tools_called": len(r.get("tools_called", [])),
        "llm_turns": r.get("llm_turns", 0),
        "truncated": r.get("truncated", False),
        "elapsed": elapsed,
        "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # 关注度：Agent 模式有联网证据本，可以从证据条目数推一个 x，比 classic 实在些；
        # 仍不是精确阅读量，estimated=True 不变。
        "attention": _attention_for_text(
            text, estimated=True,
            evidence_entries=(r.get("evidence_book") or {}).get("entries") or []),
    }
    _history.insert(0, record)
    del _history[HISTORY_LIMIT:]
    return record


def _attention_for_text(text: str, estimated: bool = True,
                        evidence_entries: Optional[List[Any]] = None) -> Dict[str, Any]:
    """给一段被分析的文本算关注度，附在分析记录里。

    这里**不联网**：分析链路本身没有"这篇文章被读了多少次"这个信息，
    所以 y/z 只能给保守兜底值，`estimated` 恒为 True，前端必须标注「估计」。
    用户要看真实量级的关注度，应该用 /api/attention/search（它会联网取证）。

    evidence_entries 是 Agent 模式的证据本条目，用来把 x 从 0 提到一个可核验的数
    （证据本里确实有多少条独立来源），比拍一个 0 诚实。

    永不抛异常：算不出就返回空 dict，前端整块隐藏，不影响主分析流程。
    """
    try:
        x = 0
        notes = ["本次分析未联网取数，y/z 为保守兜底值，仅表示「几乎无人关注」的量级"]
        if evidence_entries:
            urls = {str(e.get("url") or "").strip().rstrip("/").lower()
                    for e in evidence_entries if isinstance(e, dict)}
            urls.discard("")
            if urls:
                x = len(urls)
                notes.append(f"x={x} 取自本次联网证据本的独立来源数（可核验）")
        detail = _attention_payload(float(x), 0.0, 0.0)
        detail["estimated"] = estimated
        detail["quality"] = "estimated"
        detail["notes"] = notes
        return detail
    except Exception:  # noqa: BLE001  关注度是附加信息，算不出也不能拖垮主流程
        return {}


@app.get("/api/history")
def history() -> List[Dict[str, Any]]:
    """返回最近的分析记录（最新在前，最多 HISTORY_LIMIT 条）。"""
    return _history


# ===========================================================================
# 关注度（Attention）评分
# ===========================================================================

def _attention_payload(x: float, y: float, z: float,
                       profile: str = "") -> Dict[str, Any]:
    """算一次关注度，返回可直接给前端的完整 payload（含分项、档位、深读标记）。

    输入非法时抛 400，而不是让 ValueError 冒成 500：这是用户填错数字，不是服务故障。
    `profile` 为空时用默认档（spec），非法档位名会告警并回退默认档。
    """
    params, warnings = _attn.load_profile(profile or None)
    try:
        detail = _attn.score_detail(x, y, z, params)
    except _attn.AttentionError as exc:
        raise HTTPException(status_code=400, detail=f"关注度参数不合法：{exc}") from exc
    effective = profile if profile in _attn.PROFILES else _attn.DEFAULT_PROFILE
    detail["profile"] = effective
    detail["threshold"] = _attn.profile_threshold(effective)
    detail["threshold_s"] = round(-math.log(1 - detail["threshold"] / 100)
                                  / detail["params"]["lambda"], 6)
    detail["profile_warnings"] = warnings
    return detail


@app.get("/api/attention/profiles")
def attention_profiles() -> Dict[str, Any]:
    """列出可用的关注度参数档位，供前端渲染下拉框。"""
    return {
        "default": _attn.DEFAULT_PROFILE,
        "profiles": [
            {
                "name": name,
                "comment": _attn.PROFILES[name].get("_comment", ""),
                "params": _attn.PROFILES[name]["params"],
            }
            for name in _attn.list_profiles()
        ],
    }


@app.post("/api/attention")
def attention_score_api(req: AttentionRequest) -> Dict[str, Any]:
    """关注度评分：手动给定 x（文章数）/ y（阅读量）/ z（留言量）。

    纯确定性：不调用大模型、不联网，同一组输入必然复现同一分数。
    返回 E、S、三个加权分项、档位、以及是否触发深读（E > 66 才触发，严格大于才算）。
    触发时附带 `deep_read` 解读（受控生成：议题标签 + 张力 + 现象 + 反方 + 边界；
    LLM 不可用则回退本地模板并标 degraded）。
    """
    detail = _attention_payload(req.x, req.y, req.z, req.profile)
    detail["deep_read"] = _deep_read("", detail, profile=req.profile or None)
    return detail


@app.post("/api/attention/search")
def attention_search_api(req: AttentionSearchRequest) -> Dict[str, Any]:
    """关注度评分：输入一个话题，联网估出 x/y/z 后算分。

    与 /api/attention 的区别只在 x/y/z 的来源：这里由 `mylocalai.attention.estimate` 从一次真实
    检索里估出来，因此**必须带 estimated / quality 标记**，前端要原样展示
    「代理/估计」，不能让用户把 E 当成「有多少人阅读」的绝对结论。

    检索本身可能失败（网络不可达、被反爬），这时不报错：按缺失处理返回 E 对应
    全零输入的结果，并在 notes 里说明取不到。**估不到就说估不到，不编数字。**

    触发深读时附带受控生成的结构化解读（LLM 不可用则回退本地模板并标 degraded）。
    """
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="请输入要评估的话题关键词")
    if len(query) > 200:
        raise HTTPException(status_code=400, detail="话题关键词过长（最多 200 字）")
    if has_surrogates(query):
        raise HTTPException(status_code=400, detail="输入包含无效字符（无法编码的 Unicode 代理项）")

    try:
        from mylocalai.retrieval.search import search_all
        payload = search_all(query, k=8)
        results = payload.get("results") or []
        engines = payload.get("engines") or {}
    except Exception as exc:  # noqa: BLE001  检索失败不能让页面 500
        return {
            **_attention_payload(0.0, 0.0, 0.0),
            "query": query,
            "quality": "missing",
            "estimated": True,
            "sources": [],
            "notes": [f"检索失败（{type(exc).__name__}: {exc}），关注度按缺失处理"],
            "engines": {},
            "deep_read": {"triggered": False, "text": "", "source": "",
                          "degraded": False, "warnings": [], "applicable": False,
                          "result": {}, "guard_dropped": []},
        }

    est = estimate_from_search(query, results)
    out = _attention_payload(float(est.x), est.y, est.z, req.profile)
    out.update(est.to_dict())
    out["engines"] = engines
    # 代理值提示要进深读的 prompt：否则模型会把 y 当成精确阅读量做绝对化断言
    out["deep_read"] = _deep_read(query, out, profile=req.profile or None,
                                  evidence=_evidence_from_results(results))
    return out


def _evidence_from_results(results: List[Any]) -> List[Dict[str, Any]]:
    """把检索结果转成深读可用的**带编号发言证据**（e1/e2/...）。

    口径：标题 + 摘要拼成一条证据文本，domain_tier 作为"来源"备注。
    没有证据时返回空列表——此时深读应判不适用（applicable=false），
    而不是让模型对着空证据编内容。
    """
    out: List[Dict[str, Any]] = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        text = f"{title} {snippet}".strip()
        if not text:
            continue
        entry: Dict[str, Any] = {"text": text[:300]}
        tier = str(item.get("domain_tier") or "").strip()
        if tier:
            entry["source"] = tier
        url = str(item.get("url") or "").strip()
        if url:
            entry["url"] = url
        out.append(entry)
    return out[:12]


# ---- 托管前端静态页 ----
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    """首页（static/index.html）。"""
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))
