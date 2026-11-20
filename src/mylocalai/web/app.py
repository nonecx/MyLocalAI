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
      GET  /api/models        模型管理（提供方 / 模型目录 / 密钥，设置面板用）
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
from mylocalai.llm import model_registry as _model_registry  # noqa: E402
from mylocalai.util.console_io import force_utf8_stdio  # noqa: E402
from mylocalai.util.text_safety import has_surrogates, sanitize_text  # noqa: E402
from mylocalai.web.models_api import router as models_router  # noqa: E402
from mylocalai.web.privacy_api import router as privacy_router  # noqa: E402

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
app.include_router(privacy_router)
# 模型管理（设置面板）：与 privacy_router 紧挨——两者都是"用户在本机改自己的配置"，
# 前缀/异常口径保持一致（404 未知、400 中文 detail、写后 llm_client.reset_cache()）
app.include_router(models_router)

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
        # 模型注册表的加载期告警数：data/models.json 缺失/损坏/版本不符时 > 0
        # （注册表会 fail-soft 退回内置提供方表，服务照常起）。原文在设置面板里看，
        # 这里给一个计数，便于不看页面也知道"配置没被正常加载"。
        "models": {"warnings": len(_model_registry.warnings())},
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


@app.middleware("http")
async def _static_revalidate_cache(request: Request, call_next):
    """静态资源一律"可缓存但须回源校验"（Cache-Control: no-cache）。

    页面升级后用户普通刷新即可拿到新 CSS/JS（文件变了返回新内容，
    没变返回 304），不会吃到浏览器启发式缓存里的旧样式；
    /api/* 与页面本身不受影响。
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
def index() -> FileResponse:
    """首页（static/index.html）。"""
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


# ===========================================================================
# 智能助手对话层（M2）：/api/chat（SSE）+ /api/sessions + /api/dossiers
# ===========================================================================
# 依赖方向：web → {assistant, dossier} 的公开服务接口，不直连引擎内部。
# 助手核心 AssistantLoop（§4.2）由并行工作流开发：web 层只依赖
# build_assistant_loop() 返回"带 handle(session_id, message, events, cancel_check)
# 的对象或 None"这一句契约；测试通过 monkeypatch 本模块的工厂函数注入 stub，
# 与 loop 的落地进度解耦。SSE 事件名/帧格式一律以 assistant.events 为唯一权威。

import asyncio  # noqa: E402

from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse  # noqa: E402

from mylocalai.assistant.events import (  # noqa: E402
    EVENT_NAMES as _SSE_EVENT_NAMES,
    EventStream,
    format_sse,
    make_event,
)
from mylocalai.assistant.registry import (  # noqa: E402
    MAX_TEXT_LENGTH as _ASSISTANT_MAX_TEXT_LENGTH,
    ToolNotAllowed as _AssistantToolNotAllowed,
    ValidationError as _AssistantValidationError,
)
from mylocalai.assistant.reminders import aggregate_reminder_lines  # noqa: E402

#: chat 请求轮里 loop 正常收尾的事件名（events.py 协议：异常出口是 error）。
_TERMINAL_SSE_EVENTS = ("done", "error")

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # 反向代理/压缩中间层默认会缓冲响应体，SSE 事件必须即时可达，显式关闭
    "X-Accel-Buffering": "no",
}


def build_assistant_loop():
    """构造助手核心 AssistantLoop（惰性 import）；未装配/构造失败时返回 None。

    解耦点（编排者钉死）：所有 chat 端点只经本函数取 loop；None 时端点走
    SSE error{code:"assistant_unavailable"} 降级收尾，绝不 500。loop 落地前
    import 不到是预期状态；测试 monkeypatch 本函数注入 stub loop。
    """
    try:
        from mylocalai.assistant.loop import AssistantLoop  # 延迟到首请求才加载
    except Exception:  # noqa: BLE001  loop 未落地 / 依赖缺失 → 降级出口
        return None
    try:
        return AssistantLoop()
    except Exception as exc:  # noqa: BLE001  构造失败（如配置缺失）同样降级
        import logging
        logging.getLogger("mylocalai.web").warning(
            "AssistantLoop 构造失败: %s: %s", type(exc).__name__, exc)
        return None


# ---- 会话库 / 档案库服务工厂（单例 + 测试注入点） ----

_session_store = None
_dossier_service = None


def get_session_store():
    """会话库（sessions/messages/tool_runs 三表，与档案域同库）。测试注入点。"""
    global _session_store
    if _session_store is None:
        from mylocalai.assistant.session_store import SessionStore
        _session_store = SessionStore()
    return _session_store


def get_dossier_service():
    """档案域服务门面：数据一律经 DossierService/store 读取，不直连引擎。测试注入点。"""
    global _dossier_service
    if _dossier_service is None:
        from mylocalai.dossier.service import DossierService
        _dossier_service = DossierService()
    return _dossier_service


# ---- 对话（/api/chat，SSE） ----

class ChatRequest(BaseModel):
    """POST /api/chat 的请求体。session_id 缺省时自动新建会话（meta 事件回传 id）。

    action（可选，x/y/z 精确输入通道）：{"name": 工具名, "args": {...}}，供前端按钮
    直接指定只读工具、跳过路由模型。类型刻意写成 Any 而非 Dict：形状错误要按本项目
    既有的 400 语义（{"detail": ...}）返回，而不是 pydantic 的 422。
    """
    session_id: Optional[int] = None
    message: str
    action: Any = None


def _validate_chat_message(message: str) -> str:
    """消息校验：沿用网站现有风格（非空、无代理项、≤8000 字）。

    长度上限单一来源 = assistant.registry.MAX_TEXT_LENGTH（工具参数校验同用此值，
    改一处两端生效）；模块内 MAX_TEXT_LENGTH 常量仍服务于 /api/analyze 契约。
    """
    text = (message or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="输入不能为空")
    if has_surrogates(text):
        raise HTTPException(status_code=400,
                            detail="输入包含无效字符（无法编码的 Unicode 代理项），请检查后重试")
    if len(text) > _ASSISTANT_MAX_TEXT_LENGTH:
        raise HTTPException(status_code=400,
                            detail=f"文本过长（最多 {_ASSISTANT_MAX_TEXT_LENGTH} 字）")
    return text


def _normalize_chat_action(action: Any) -> Optional[Dict[str, Any]]:
    """校验并规整 POST /api/chat 的可选 action 字段（形状层，不查注册表）。

    形状非法 → 400（与 message 校验同一 400 语义）；语义层（未注册 / 不在 allowlist /
    写权限 / 参数越界）由 AssistantLoop.resolve_action 在拿到 loop 后再校验，同样 400。
    形状先于"解析/新建会话"校验：坏请求不该在会话列表里留下一个空会话。
    """
    if action is None:
        return None
    # 惰性 import（先例：build_assistant_loop）：chat 模块导入期不装配助手核心
    from mylocalai.assistant.loop import action_payload
    try:
        name, args = action_payload(action)
    except _AssistantValidationError as exc:
        raise HTTPException(status_code=400, detail=f"action 非法：{exc}")
    return {"name": name, "args": args}


async def drain_event_stream(events: EventStream, request: Any, worker_done,
                             poll_interval: float = 0.05):
    """drain 循环（独立拆出、可单测）：把 EventStream 新增事件逐帧吐成 SSE。

    终止契约（§5.3）：出现 done/error 事件**且**工作线程已结束，二者缺一就继续
    轮询（done 之后 loop 还可能补写落库，线程收尾前不关流）。
    断开取消链（§5.3）：客户端断开 → events.request_cancel() 置 cancelled，
    loop 的 cancel_check 轮询到后协作收工；断开后已无法投递，立即结束本流、
    不再等待工作线程（线程自身会尽快退出）。
    """
    seen = 0
    while True:
        try:
            disconnected = await request.is_disconnected()
        except Exception:  # noqa: BLE001  断开检测自身失败不吞事件流
            disconnected = False
        if disconnected:
            events.request_cancel()
            return
        snapshot = events.snapshot()
        total = len(snapshot)
        while seen < total:
            yield format_sse(snapshot[seen])
            seen += 1
        last_name = snapshot[-1]["event"] if snapshot else ""
        if last_name in _TERMINAL_SSE_EVENTS and worker_done():
            return
        if events.cancelled and worker_done():
            return
        await asyncio.sleep(poll_interval)


def _unavailable_stream(session_id: int):
    """loop 未装配的降级出口：meta（回传/绑定会话 id）+ error 后收尾。"""
    async def gen():
        yield format_sse(make_event("meta", session_id=session_id, run_id=None))
        yield format_sse(make_event(
            "error", code="assistant_unavailable", recoverable=True,
            message="助手核心尚未就绪（AssistantLoop 未装配），本次请求未执行，请稍后重试。"))
    return gen()


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    """对话端点：SSE 流式返回助手事件（事件名封闭枚举见 mylocalai.assistant.events）。

    流程：校验消息与 action 形状 → 解析/新建会话 → build_assistant_loop() 取助手核心
    → action 语义校验（未注册/写工具/坏参数 → 400，发生在 SSE 开始之前） →
    工作线程跑 loop.handle(session_id, message, events, cancel_check[, action])，主协程
    drain_event_stream 轮询 events.snapshot() 吐帧。loop 抛出协议外异常时由
    web 兜底发 error 收尾，保证流必然终止。

    action（可选）是 x/y/z 精确输入通道：合法时 loop 跳过路由模型直接执行该只读工具，
    事件协议与顺序一个都不变（meta → status → tool_start → card → narrative → tool_end
    → done）。非法 action 用 400 {"detail": ...} 拒绝——HTTP 层拒绝比"流里塞 error"
    更像一次没发生的请求：前端不会误以为回合已经开始。
    """
    message = _validate_chat_message(req.message)
    action = _normalize_chat_action(req.action)
    store = get_session_store()
    if req.session_id is not None:
        if isinstance(req.session_id, bool) or not isinstance(req.session_id, int) \
                or req.session_id <= 0:
            raise HTTPException(status_code=400, detail="session_id 必须是正整数")
        if store.get_session(int(req.session_id)) is None:
            raise HTTPException(status_code=404, detail=f"会话不存在: {req.session_id}")
        session_id = int(req.session_id)
    else:
        session_id = store.create_session(title=message[:24])

    loop = build_assistant_loop()
    if loop is None:
        return StreamingResponse(_unavailable_stream(session_id),
                                 media_type="text/event-stream", headers=_SSE_HEADERS)

    if action is not None:
        # SSE 开始之前的语义校验（注册表/allowlist/只读/参数）：抛错即 400。
        # getattr 兜底：只实现 handle 的注入式核心（测试 stub / 旧装配）不该因本通道
        # 缺失而 500，明确 400 告诉调用方"该核心不支持 action"。
        resolver = getattr(loop, "resolve_action", None)
        if resolver is None:
            raise HTTPException(status_code=400,
                                detail="action 非法：当前助手核心不支持结构化 action")
        try:
            resolver(action)
        except (_AssistantValidationError, _AssistantToolNotAllowed) as exc:
            raise HTTPException(status_code=400, detail=f"action 非法：{exc}")

    events = EventStream()
    worker_done = threading.Event()

    def _run() -> None:
        try:
            if action is None:
                # 无 action 时保持原调用形状（既有 stub loop 只认这四个位置参数）
                loop.handle(session_id, message, events, lambda: events.cancelled)
            else:
                loop.handle(session_id, message, events, lambda: events.cancelled,
                            action=action)
        except Exception as exc:  # noqa: BLE001  loop 内部兜底之外的最外层防线
            try:
                events.emit("error", code="internal", recoverable=False,
                            message=f"助手处理异常: {type(exc).__name__}: {exc}")
            except ValueError:
                pass  # emit 只接受封闭枚举事件名，这里恒为合法事件，理论到不了
        finally:
            worker_done.set()

    threading.Thread(target=_run, name="assistant-chat-turn", daemon=True).start()

    async def gen():
        try:
            async for frame in drain_event_stream(events, request, worker_done.is_set):
                yield frame
        finally:
            # 兜底取消：正常结束/断开/异常路径都通知 loop 收工（对已结束的循环无副作用）
            events.request_cancel()

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


# ---- 会话 API ----

class SessionCreateRequest(BaseModel):
    """POST /api/sessions 的请求体（可整体省略）。"""
    title: str = ""


def _sum_runs_cost(rows: Any) -> Dict[str, int]:
    """运行记录行的 requests/tokens 求和（M4 成本汇总口径：只累真实落库值，不估算）。

    会话侧来源 = tool_runs（工具段真实 delta）；档案侧来源 = analysis_runs。
    轮内路由/叙述成本只在当轮 done.cost 徽章呈现、不落库（消息表无成本列），
    因此这里如实只汇总落库部分——缺哪块就少算哪块，不编数字。
    """
    requests = 0
    tokens = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            requests += int(row.get("requests") or 0)
        except (TypeError, ValueError):
            pass
        try:
            tokens += int(row.get("tokens") or 0)
        except (TypeError, ValueError):
            pass
    return {"requests": requests, "tokens": tokens}


@app.get("/api/sessions")
def sessions_list() -> Dict[str, Any]:
    """会话列表（未归档，updated_at 倒序，最多 100 条），含 title/updated_at。

    M4：每项附 cost 汇总 {"requests","tokens"}（sum 该会话 tool_runs 的真实落库值），
    供左栏会话列表渲染会话级累计成本徽章。逐会话取 tool_runs 是 N 次进程内 SQLite
    查询（N≤100、单机个人库量级），不值得为此加一条聚合 SQL。
    """
    store = get_session_store()
    items = store.list_sessions(limit=100)
    for item in items:
        try:
            item["cost"] = _sum_runs_cost(store.runs_for_session(int(item["id"])))
        except Exception:  # noqa: BLE001  成本汇总是增益面：取不到就缺省零值
            item["cost"] = {"requests": 0, "tokens": 0}
    return {"sessions": items}


@app.post("/api/sessions")
def sessions_create(req: Optional[SessionCreateRequest] = None) -> Dict[str, Any]:
    """新建会话，返回含 id 的完整会话记录。"""
    body = req or SessionCreateRequest()
    title = (body.title or "").strip()
    if has_surrogates(title):
        raise HTTPException(status_code=400, detail="标题包含无效字符（无法编码的 Unicode 代理项）")
    store = get_session_store()
    return store.get_session(store.create_session(title=title[:100]))


@app.get("/api/sessions/{session_id}/messages")
def session_messages(session_id: str) -> Dict[str, Any]:
    """会话消息（时间正序）+ M4 成本汇总（cost = sum tool_runs 的真实落库值）。
    非法 sid（非正整数/不存在）→ 404。"""
    store = get_session_store()
    sid = _parse_path_id(session_id, "会话")
    if store.get_session(sid) is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {sid}")
    return {"session_id": sid, "messages": store.messages(sid),
            "cost": _sum_runs_cost(store.runs_for_session(sid))}


def _session_dossier_ids(store: Any, sid: int) -> List[int]:
    """该会话涉及过的档案号（按 tool_runs 首现顺序，去重）。

    两个来源：tool_runs.dossier_id（verify/recheck 等落库时回填）；args.id
    （get_dossier/recheck_dossier/export_card 等记忆类工具的显式档案号——
    用户"回看过"的档案也可能需要提醒）。读失败返回已收集部分，不抛。
    """
    ids: List[int] = []
    try:
        for run in store.runs_for_session(sid):
            if not isinstance(run, dict):
                continue
            candidates = [run.get("dossier_id")]
            args = run.get("args")
            if isinstance(args, dict):
                candidates.append(args.get("id"))
            for cand in candidates:
                if isinstance(cand, bool) or not isinstance(cand, int) or cand <= 0:
                    continue
                if cand not in ids:
                    ids.append(cand)
    except Exception:  # noqa: BLE001  tool_runs 读失败时提醒退空，不影响会话本身
        return ids
    return ids


@app.get("/api/sessions/{session_id}/reminders")
def session_reminders(session_id: str) -> Dict[str, Any]:
    """会话的被动提醒（M3-7，§2.2"不制造新压力"）。

    聚合该会话涉及过的档案 → reminder_lines（纯函数，规则外置
    assistant_rules.reminders / prompts.reminder_line）。只在用户打开会话时被
    前端取一次，消息流顶部一行浅色呈现；无命中返回空列表，前端不渲染任何东西。
    非法 sid（非正整数/不存在）→ 404，与 messages 端点同口径。
    """
    store = get_session_store()
    sid = _parse_path_id(session_id, "会话")
    if store.get_session(sid) is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {sid}")
    dossier_ids = _session_dossier_ids(store, sid)
    lines = aggregate_reminder_lines(dossier_ids, get_dossier_service().store)
    return {"session_id": sid, "dossier_ids": dossier_ids, "lines": lines}


def _parse_path_id(raw: str, kind: str) -> int:
    """路径参数 → 正整数；非法一律 404（契约：非法 sid → 404，而不是 422）。"""
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail=f"{kind}不存在: {raw}") from None
    if value <= 0:
        raise HTTPException(status_code=404, detail=f"{kind}不存在: {raw}")
    return value


# ---- 档案库 API（M3 档案库页：列表 / 详情六章节 / Markdown 导出） ----

@app.get("/api/dossiers")
def dossiers_list(query: str = "", limit: int = 50) -> Dict[str, Any]:
    """档案列表（updated_at 倒序）。数据只经 DossierService/store 读取。"""
    return {"dossiers": get_dossier_service().store.list_dossiers(
        query=query or "", limit=max(1, min(int(limit), 200)))}


@app.get("/api/dossiers/{dossier_id}")
def dossier_detail(dossier_id: str) -> Dict[str, Any]:
    """档案详情：档案头 + 全版本结论 + 证据批次计数 + 快照计数 + 版本对比
    + chapters 六章节聚合（M3-2：conclusion/evidence/speech/heat/opinions/deep_read，
    经 service.chapters()，纯读取、缺章如实 absent、永不抛）。
    M4：附 cost 汇总（sum analysis_runs 的真实落库 requests/tokens）。"""
    service = get_dossier_service()
    did = _parse_path_id(dossier_id, "档案")
    header = service.store.get_dossier(did)
    if header is None:
        raise HTTPException(status_code=404, detail=f"档案不存在: {did}")
    cmp = service.compare_versions(did)
    runs = service.store.runs(did)
    return {
        **header,
        "conclusions": service.store.conclusions(did),
        "evidence_batches": cmp["evidence_batches"],
        "versions": cmp["versions"],
        "changed": cmp["changed"],
        "snapshot_count": len(service.store.snapshots(did)),
        "chapters": service.chapters(did),
        "cost": _sum_runs_cost(runs),
    }


@app.get("/api/dossiers/{dossier_id}/export")
def dossier_export(dossier_id: str, format: str = "md") -> PlainTextResponse:
    """导出档案为 Markdown 文本（M3-6：六章节 + 徽章 + 免责语 + as_of 页脚）。

    文本由 service.export_markdown() 纯字符串拼接（零 LLM）；响应带
    Content-Disposition attachment（浏览器点击即下载 .md 文件）。
    目前仅支持 format=md。
    """
    fmt = (format or "md").strip().lower()
    if fmt != "md":
        raise HTTPException(status_code=400, detail=f"暂只支持 format=md（收到: {format}）")
    service = get_dossier_service()
    did = _parse_path_id(dossier_id, "档案")
    header = service.store.get_dossier(did)
    if header is None:
        raise HTTPException(status_code=404, detail=f"档案不存在: {did}")
    filename = f"dossier-{did}.md"
    return PlainTextResponse(
        service.export_markdown(did),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename=\"{filename}\"; filename*=UTF-8''{filename}"},
    )


# ===========================================================================
# L4 参谋（insights）：话术模式库 / 信源画像 / 个人信息环境周报
# ===========================================================================
# 三个只读端点：数据一律经 get_dossier_service().store 读取（测试注入点同档案库
# 端点），聚合用 mylocalai.dossier.insights 的纯函数——零 LLM、零联网、只做计数
# 与排序，不产生任何真假断言。处理体任何故障 → 503 {"error": "insights_unavailable"}
# （参谋是增益面，绝不把页面打成 500）；days 参数用字符串手工解析保持 400 语义。


@app.get("/api/insights/patterns")
def insights_patterns() -> Dict[str, Any]:
    """话术模式库（零 LLM、纯只读聚合）：个人档案库里的话术特征 × 结论标签统计。"""
    try:
        from mylocalai.dossier.insights import pattern_library  # 惰性 import（先例：build_assistant_loop）
        return {"patterns": pattern_library(get_dossier_service().store)}
    except Exception:  # noqa: BLE001  参谋数据取不到就如实 503，不让页面 500
        return JSONResponse({"error": "insights_unavailable"}, status_code=503)


@app.get("/api/insights/sources")
def insights_sources() -> Dict[str, Any]:
    """信源画像（零 LLM、纯只读聚合）：按域名聚合证据条目的反驳/支持战绩（已排序）。"""
    try:
        from mylocalai.dossier.insights import source_profiles
        return {"profiles": source_profiles(get_dossier_service().store)}
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "insights_unavailable"}, status_code=503)


@app.get("/api/insights/weekly")
def insights_weekly(days: str = "7") -> Dict[str, Any]:
    """个人信息环境周报（零 LLM、纯只读聚合）：窗口内档案/结论/成本/话术/信源/热度计数。

    days 用字符串参数手工解析（不用 FastAPI 类型校验，保持 400 语义）：
    int() 失败或不在 1..90 → 400 {"error": "invalid_days"}；缺省 7 天。
    """
    try:
        n = int(str(days))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid_days"}, status_code=400)
    if not 1 <= n <= 90:
        return JSONResponse({"error": "invalid_days"}, status_code=400)
    try:
        from mylocalai.dossier.insights import weekly_digest
        now = datetime.now().astimezone().isoformat()
        return {"digest": weekly_digest(get_dossier_service().store, now=now, days=n)}
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "insights_unavailable"}, status_code=503)
