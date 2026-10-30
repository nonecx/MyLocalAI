# 目标文件: 本仓库（路径按部署位置而定，勿写死盘符）
# -*- coding: utf-8 -*-
"""共享大模型客户端工厂（OpenAI 兼容格式，默认 LongCat）。

统一负责四件事，供 report_generator / language_module / logic_module / rumor_agent 复用：
  - 加载项目根目录的 .env（python-dotenv；未安装或文件缺失时静默跳过）
  - 按"提供方"成组绑定 密钥 / 接口地址 / 模型名（换供应商只需改一个 LLM_PROVIDER）
  - 启动自检：密钥与服务商不匹配时明确报错，而不是静默把请求发到别家
  - 提供 chat_json() / chat_text()：统一的对话调用与稳健 JSON 提取

已知平台特性（2026-10 实测）：
  - 服务端注册模型名为 LongCat-2.5-Preview（英文连字符，中文"预览"会 400）。
  - LongCat-2.5-Preview 是思维链模型：默认会把 token 花在 reasoning_content 上，
    max_tokens 太小时正文为空。分类类任务用
    extra_body={"chat_template_kwargs": {"enable_thinking": False}} 可关闭思维链。
  - 该模型**不认 seed**（同 seed 重复调用仍给出不同结果），所以复现性只能靠下游护栏
    （见 rumor_agent/verdict.py 的分桶与滞回），不要指望调用侧传 seed 解决漂移。

配置（项目根目录 .env，环境变量优先）：
    LLM_PROVIDER       提供方名（longcat / deepseek / openai，默认 longcat）
    LLM_FALLBACKS      备用提供方，逗号分隔（如 openai,deepseek）；主链路失败时依次尝试
    LONGCAT_API_KEY / LONGCAT_BASE_URL / LONGCAT_MODEL    提供方自己的三项配置
    LLM_TIMEOUT_S      单次 HTTP 尝试的最长等待秒数（默认 30）
    LLM_MAX_RETRIES    单次调用的重试次数（默认 1，即最多 2 次尝试）

设计约定：
  - API 密钥一律放 .env，禁止硬编码进源码。
  - 所有调用失败都返回错误信息而不抛异常，由调用方决定默认值，
    保证"单模块失败不拖垮整体"的容错策略在各模块一致落地。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from shared_core import llm_cache
from shared_core.text_safety import sanitize_text

# ---- 提供方表：key_env / base_url / model 成组绑定 ----
# 换供应商只改 LLM_PROVIDER 一项，不会出现"换了密钥、地址还指着旧家"的静默错配。
# 表本身外置到 shared_core/providers.json（2026-10-22 修复 G4）：可用
# LLM_PROVIDERS_PATH 整套替换、LLM_PROVIDERS_EXTRA_PATH 增量加一家；读不到时退回内置默认。
_BUILTIN_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "longcat": {
        "key_env": "LONGCAT_API_KEY",
        "base_url_env": "LONGCAT_BASE_URL",
        "model_env": "LONGCAT_MODEL",
        "base_url": "https://api.longcat.chat/openai/v1",
        "model": "LongCat-2.5-Preview",
        "supports_thinking_disable": True,
    },
    "deepseek": {
        "key_env": "DEEPSEEK_API_KEY",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "model_env": "DEEPSEEK_MODEL",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "supports_thinking_disable": False,
    },
    "openai": {
        "key_env": "OPENAI_API_KEY",
        "base_url_env": "OPENAI_BASE_URL",
        "model_env": "OPENAI_MODEL",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "supports_thinking_disable": False,
    },
}

_PROVIDERS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "providers.json")
_PROVIDERS_LOCK = threading.Lock()
_PROVIDERS_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_PROVIDER_WARNINGS: Dict[str, str] = {}

DEFAULT_PROVIDER = "longcat"

# 兼容旧变量名：历史 .env 只有 LONGCAT_*，也允许 DEEPSEEK_* / OPENAI_* 直接覆盖
_LEGACY_KEY_FALLBACK = ("LONGCAT_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")


def providers() -> Dict[str, Dict[str, Any]]:
    """当前提供方表（进程内缓存；`reset_cache()` 可强制重读）。"""
    global _PROVIDERS_CACHE
    with _PROVIDERS_LOCK:
        if _PROVIDERS_CACHE is None:
            from shared_core import config_loader
            data, warnings = config_loader.load_config(
                _PROVIDERS_PATH, builtin=_BUILTIN_PROVIDERS,
                required_sections=("longcat",),
                env_full="LLM_PROVIDERS_PATH", env_extra="LLM_PROVIDERS_EXTRA_PATH")
            _PROVIDER_WARNINGS.clear()
            _PROVIDER_WARNINGS.update(warnings)
            _PROVIDERS_CACHE = data
        return _PROVIDERS_CACHE


def provider_warnings() -> List[str]:
    """提供方表加载时出现的问题（为空表示一切正常）。"""
    providers()
    return list(_PROVIDER_WARNINGS.values())


def supports_thinking_disable(provider: str) -> bool:
    """该提供方是否接受 extra_body 关闭思维链（不是所有家都认这个参数）。"""
    spec = providers().get(provider) or {}
    return bool(spec.get("supports_thinking_disable"))


def _load_env() -> None:
    """加载项目根目录的 .env；python-dotenv 未安装或文件缺失时静默跳过。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(root, ".env"))


_load_env()

# ---- 过程内缓存 ----
_CONFIG_LOCK = threading.Lock()
_CONFIG_ERROR = ""                      # 配置自检失败原因（空串=正常）
_RESOLVED: Optional[Dict[str, str]] = None
# 提供方是否接受"关闭思维链"参数：key=(base_url, model) -> True/False；未记录=未知
_THINKING_SUPPORT: Dict[Tuple[str, str], bool] = {}


def reset_cache() -> None:
    """清掉配置与能力缓存（改环境变量后供测试/长驻进程重新解析）。"""
    global _RESOLVED, _CONFIG_ERROR, _PROVIDERS_CACHE
    with _CONFIG_LOCK:
        _RESOLVED = None
        _CONFIG_ERROR = ""
        _THINKING_SUPPORT.clear()
    with _PROVIDERS_LOCK:
        _PROVIDERS_CACHE = None


def _provider_names() -> List[str]:
    """主提供方 + 备用链（LLM_FALLBACKS 逗号分隔，取合法名并在前向去重）。"""
    names: List[str] = []
    primary = (os.environ.get("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    names.append(primary)
    for raw in (os.environ.get("LLM_FALLBACKS") or "").split(","):
        name = raw.strip().lower()
        if name and name not in names:
            names.append(name)
    return names


def _resolve() -> Dict[str, str]:
    """解析主提供方的 密钥/地址/模型，并做"密钥与服务商是否匹配"的自检。

    自检规则：密钥必须来自当前提供方自己的环境变量；若当前提供方没配密钥、
    却配了别家的密钥，则记下错配原因并拒绝建客户端——这正是
    "只改密钥、地址还指着 LongCat" 那个静默错配的修复点。
    """
    global _RESOLVED, _CONFIG_ERROR
    with _CONFIG_LOCK:
        if _RESOLVED is not None:
            return dict(_RESOLVED)

        primary = _provider_names()[0]
        spec = providers().get(primary)
        if spec is None:
            _CONFIG_ERROR = (f"未知提供方 {primary!r}（LLM_PROVIDER 只支持 "
                             f"{'、'.join(sorted(providers()))}）")
            _RESOLVED = {"provider": primary, "api_key": "", "base_url": "", "model": ""}
            return dict(_RESOLVED)

        api_key = os.environ.get(spec["key_env"], "").strip()
        base_url = os.environ.get(spec["base_url_env"], "").strip() or spec["base_url"]
        model = os.environ.get(spec["model_env"], "").strip() or spec["model"]

        if not api_key:
            mismatch = [name for name in _LEGACY_KEY_FALLBACK
                        if name != spec["key_env"] and os.environ.get(name, "").strip()]
            if mismatch:
                primary_of = {v["key_env"]: k for k, v in providers().items()}
                others = "、".join(f"{n}（提供方 {primary_of.get(n, '?')}）" for n in mismatch)
                _CONFIG_ERROR = (
                    f"密钥与服务商不匹配：当前提供方是 {primary}，但只设置了 {others} 的密钥。"
                    f"请设置 {spec['key_env']}，或把 LLM_PROVIDER 改成对应提供方（如 LLM_PROVIDER=openai）。")
            else:
                _CONFIG_ERROR = f"未配置 {spec['key_env']}（提供方 {primary}）"
        else:
            _CONFIG_ERROR = ""

        _RESOLVED = {"provider": primary, "api_key": api_key, "base_url": base_url, "model": model}
        return dict(_RESOLVED)


def config_error() -> str:
    """配置自检结果：空串表示可用；非空是给用户看的原因说明。"""
    _resolve()
    return _CONFIG_ERROR


def validate_config() -> Tuple[bool, str]:
    """启动自检入口：返回 (是否可用, 说明)。不抛异常。"""
    error = config_error()
    if error:
        return False, error
    info = _resolve()
    return True, f"提供方 {info['provider']} · 模型 {info['model']} · 地址 {info['base_url']}"


def get_provider() -> str:
    """当前主提供方名。"""
    return _resolve()["provider"]


def get_api_key() -> Optional[str]:
    """读取主提供方的 API 密钥。"""
    return _resolve()["api_key"] or None


def get_base_url() -> str:
    """返回主提供方的接口地址。"""
    return _resolve()["base_url"]


def get_model() -> str:
    """返回主提供方的模型名。注意：LongCat 服务端注册名是 LongCat-2.5-Preview。"""
    return _resolve()["model"]


def get_timeout() -> float:
    """单次 HTTP 尝试的超时秒数（LLM_TIMEOUT_S，默认 30）。"""
    try:
        value = float(os.environ.get("LLM_TIMEOUT_S") or 30)
    except (TypeError, ValueError):
        return 30.0
    return max(1.0, value)


def get_max_retries() -> int:
    """单次调用的重试次数（LLM_MAX_RETRIES，默认 1 → 最多 2 次尝试）。

    原默认值是 openai 库的 2（最多 3 次尝试），叠加"extra_body 失败再回落一次"
    会让一次逻辑调用最多发出 6 个 HTTP 请求，把 60s 超时放大到约 130s/模块。
    """
    try:
        value = int(os.environ.get("LLM_MAX_RETRIES") or 1)
    except (TypeError, ValueError):
        return 1
    return max(0, value)


def _client_for(provider: str, max_retries: Optional[int] = None):
    """按提供方建客户端；该提供方没有自己的密钥时返回 None。

    max_retries 供"去掉关闭思维链参数后的回落调用"使用（传 0，不再内部重试），
    避免把一次逻辑调用放大成 (max_retries × 2) 个 HTTP。
    """
    spec = providers().get(provider)
    if spec is None:
        return None
    api_key = os.environ.get(spec["key_env"], "").strip()
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None
    base_url = os.environ.get(spec["base_url_env"], "").strip() or spec["base_url"]
    # 单次尝试的超时不超过"本次分析剩余预算"，避免上游挂掉时一轮轮等满 60s
    timeout = llm_cache.attempt_timeout(get_timeout())
    return OpenAI(api_key=api_key, base_url=base_url, timeout=timeout,
                  max_retries=get_max_retries() if max_retries is None else max_retries)


def create_client(provider: Optional[str] = None):
    """创建 OpenAI 兼容客户端；配置不可用或 openai 库未安装时返回 None。

    provider 省略时用主提供方（并走配置自检）；显式传入时按该提供方自己的密钥建。
    失败原因用 config_error() 读取。
    """
    if provider is not None:
        return _client_for(provider)
    if config_error():
        return None
    return _client_for(_resolve()["provider"])


def first_available_provider() -> Optional[str]:
    """主提供方有密钥就用主提供方，否则按 LLM_FALLBACKS 顺序找第一个配了密钥的。

    供 rumor_agent.llm（LangChain 侧）复用"提供方表 + 备用链"的同一个口径，
    避免 Agent 只连主提供方、备用链路对它完全无效（2026-10-15 检验报告发现 3）。
    """
    names = _provider_names()
    for name in names:
        spec = providers().get(name)
        if spec is None:
            continue
        if os.environ.get(spec["key_env"], "").strip():
            return name
    return None


def provider_spec(name: str) -> Optional[Dict[str, Any]]:
    """按提供方名取 密钥/地址/模型 三项配置（含环境变量覆盖），未知提供方返回 None。"""
    spec = providers().get(name)
    if spec is None:
        return None
    return {
        "provider": name,
        "api_key": os.environ.get(spec["key_env"], "").strip(),
        "base_url": os.environ.get(spec["base_url_env"], "").strip() or spec["base_url"],
        "model": os.environ.get(spec["model_env"], "").strip() or spec["model"],
    }


def thinking_extra_body(base_url: str, model: str) -> Optional[dict]:
    """返回关闭思维链的 extra_body；已探明该提供方不支持时返回 None（避免每次白打一次）。"""
    if _THINKING_SUPPORT.get((base_url, model)) is False:
        return None
    return {"chat_template_kwargs": {"enable_thinking": False}}


def mark_thinking_unsupported(base_url: str, model: str) -> None:
    """记下"该提供方不接受关闭思维链参数"，后续调用不再白打一次。"""
    _THINKING_SUPPORT[(base_url, model)] = False


def extract_json_obj(text: str) -> Optional[dict]:
    """
    从模型返回的文本中稳健地抽取 JSON 对象。

    - 优先整体解析；失败则剥离 markdown 代码围栏后再解析；
    - 再失败则用首尾大括号截取。
    """
    if not text:
        return None
    candidates = [text]
    cleaned = text.replace("```json", "").replace("```", "").strip()
    if cleaned != text:
        candidates.append(cleaned)
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _is_param_rejected(exc: Exception) -> bool:
    """上游是否因为不认识我们发的参数（关闭思维链）而拒绝。

    只对 4xx 参数类错误回落。5xx / 超时 / DNS 是瞬时故障：回落既无意义，还会把
    "该提供方不支持这个参数"永久记进 `_THINKING_SUPPORT`，从此整进程不再带它
    （2026-10-22 检验报告发现 5：一次网络抖动就会让参数永久失效）。
    """
    return getattr(exc, "status_code", None) == 400


class _BudgetExhausted(Exception):
    """单次调用内部发现预算已用尽——不再发起 HTTP。"""


def _chat_raw_once(
    client,
    system_prompt: str,
    user_text: str,
    temperature: float,
    max_tokens: int,
    enable_thinking: bool,
    base_url: str,
    model: str,
    make_fallback_client=None,
) -> Tuple[str, str]:
    """对单个客户端做一次对话调用，返回 (正文, 错误)。

    `make_fallback_client()`：仅在"带参数被拒"时才调用，用来造一个不再内部重试的
    客户端做回落，避免 `(max_retries × 2)` 的叠乘把一次调用放大成 4 个 HTTP。
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": str(user_text)},
    ]

    def _call(extra: Optional[dict], client_override=None):
        # 每次尝试之前都查一次预算：只在调用入口查一次时，预算会在重试/回落过程中被耗尽，
        # 于是仍多发一次完整尝试（2026-10-22 检验报告发现 4）。
        if llm_cache.budget_exhausted():
            raise _BudgetExhausted()
        kwargs: Dict[str, Any] = dict(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if extra is not None:
            kwargs["extra_body"] = extra
        active = client_override if client_override is not None else client
        return active.chat.completions.create(**kwargs)

    extra = None if enable_thinking else thinking_extra_body(base_url, model)
    try:
        try:
            resp = _call(extra)
        except _BudgetExhausted:
            return "", "已超出本次分析的时限预算，未发起调用"
        except Exception as exc:
            if extra is None or not _is_param_rejected(exc):
                raise
            if make_fallback_client is None:
                raise
            # 回落：去掉关闭思维链参数再试一次（用不再内部重试的客户端）
            try:
                resp = _call(None, make_fallback_client())
            except _BudgetExhausted:
                return "", "已超出本次分析的时限预算，未发起调用"
            except Exception:
                raise exc      # 回落也失败：保留原错误，且不留下"参数不支持"的永久标记
            mark_thinking_unsupported(base_url, model)
    except Exception as exc:  # noqa: BLE001
        return "", f"API 调用失败: {type(exc).__name__}: {exc}"

    content = resp.choices[0].message.content if resp.choices else ""
    if content and content.strip():
        return content.strip(), ""
    try:
        reasoning = resp.choices[0].message.model_dump().get("reasoning_content") or ""
    except Exception:  # noqa: BLE001
        reasoning = ""
    if reasoning.strip():
        return reasoning.strip(), ""
    return "", "模型未返回正文（可能 max_tokens 不足或仅返回思考过程）"


def _cache_key(system_prompt: str, user_text: str, temperature: float, max_tokens: int,
               enable_thinking: bool, namespace: str = "") -> str:
    """缓存键：同一段提示词 + 同一批参数 → 同一个键（与提供方无关，换供应商也不会串结果）。

    `namespace` 是**缓存分区名**：同一段提示词与参数，分区名不同则键不同。
    用途是"两类链路复用同一个 llm_cache，但绝不能互相串结果"——例如关注度深读
    （`attn_agent/deep_read.py`，namespace `attn_deep_read`）与主分析报告
    （`report_generator`，无分区名）。否则一旦两边的提示词与用户文本碰巧相同，
    一边就会读到另一边的缓存，把结论污染过去。

    编码前先规整孤立代理项：str 里只有代理项无法按 utf-8 编码，混进文本会让这一行抛
    UnicodeEncodeError（2026-10-15 检验报告发现 12，唯一未通过闸门的成因）。

    注意：分区名为空时**不往 blob 里加任何东西**，这样既有调用方（不传分区名）
    算出的键与加这个参数之前逐字节相同——加参数不该悄悄换掉全部缓存键。
    """
    blob = [get_provider(), get_base_url(), get_model(), system_prompt, str(user_text),
            temperature, max_tokens, enable_thinking]
    if namespace:
        blob.append(namespace)
    return hashlib.sha256(sanitize_text(json.dumps(blob, ensure_ascii=False))
                          .encode("utf-8", "replace")).hexdigest()


def _chat_raw(
    system_prompt: str,
    user_text: str,
    temperature: float,
    max_tokens: int,
    enable_thinking: bool = False,
    use_cache: bool = True,
    namespace: str = "",
) -> Tuple[str, str]:
    """
    底层对话调用（带缓存与请求级时限），返回 (正文文本, 错误信息)。

    缓存：同一段提示词 + 同一批参数在进程内只真正调用一次（TTL/上限见 shared_core/llm_cache.py）。
      这既省 API，也让"同一文本重复分析"拿到同一个结果——上游不认 seed，只能在调用侧钉住。
      只缓存成功结果；失败与预算耗尽都不写缓存，下次仍会真正重试。
      `use_cache=False` 只跳过读/写缓存，**不跳过 single-flight**（并发同请求仍只算一次），
      用于"同一文本再取一个独立样本"的边界复核（core_judge 的档位边界复核会用）。
      `namespace` 是缓存分区名，见 `_cache_key`：不同链路复用同一份进程内缓存时用它隔开，
      否则一边会读到另一边的结果（把结论污染过去）。
    时限：若调用方用 llm_cache.deadline_scope() 设了总预算，预算用尽时立即返回错误
      （转降级），不再一轮轮等满超时。
    """
    if llm_cache.budget_exhausted():
        return "", "已超出本次分析的时限预算，未发起调用"
    # 孤立代理项在此处统一规整：它既进不了缓存键，也发不出去（json/HTTP 编码都会失败），
    # 而本函数的契约是"任何失败都返回错误信息而不抛异常"（chat_json 的承诺由此成立）。
    user_text = sanitize_text(user_text)
    try:
        key = _cache_key(system_prompt, user_text, temperature, max_tokens,
                         enable_thinking, namespace)
    except Exception as exc:  # noqa: BLE001  键只是"复用"的手段，算不出来也应照常调用
        key = ""
    if not key:
        return _chat_raw_providers(system_prompt, user_text, temperature, max_tokens, enable_thinking)
    return llm_cache.call(
        key,
        produce=lambda: _chat_raw_providers(system_prompt, user_text, temperature,
                                            max_tokens, enable_thinking),
        accept=lambda result: bool(result[0]) and not result[1],
        use_cache=use_cache,
    )


def _chat_raw_providers(
    system_prompt: str,
    user_text: str,
    temperature: float,
    max_tokens: int,
    enable_thinking: bool = False,
) -> Tuple[str, str]:
    """
    真正发起调用的部分：主提供方 → 备用提供方（LLM_FALLBACKS）依次尝试。
    返回 (正文文本, 错误信息)。成功时 error 为空串。
    """
    errors: List[str] = []
    for index, name in enumerate(_provider_names()):
        # 每个提供方切换之前先看预算：否则预算已耗尽时仍会为备用提供方再发一次完整尝试
        # （2026-10-22 检验报告发现 4：实测第 3 个提供方在 left=-0.251s 时仍发起）。
        if llm_cache.budget_exhausted():
            return "", "已超出本次分析的时限预算，未发起调用"
        spec = providers().get(name)
        if spec is None:
            errors.append(f"{name}: 未知提供方")
            continue
        # index 0 走模块级 create_client（保留"monkeypatch create_client -> None
        # 即可验证报告回退"这一既有测试手法）；备用链路按提供方各自取密钥
        if index == 0:
            client = create_client()
            missing = config_error() or "客户端不可用"
        else:
            client = _client_for(name)
            missing = f"未配置 {spec['key_env']}"
        if client is None:
            errors.append(f"{name}: {missing}")
            continue
        base_url = os.environ.get(spec["base_url_env"], "").strip() or spec["base_url"]
        model = os.environ.get(spec["model_env"], "").strip() or spec["model"]
        # 关闭思维链：只对该提供方声明了 supports_thinking_disable 的发，别家一律不带
        # （不是所有提供方都认这个 extra_body；认的才发，省一次白打）
        use_no_thinking = (enable_thinking is False and supports_thinking_disable(name))
        # 回落用的客户端（不再内部重试）按需构造，避免每次调用都多建一个客户端
        make_fb = (lambda: _client_for(name, max_retries=0)) if use_no_thinking else None
        content, err = _chat_raw_once(
            client, system_prompt, user_text, temperature, max_tokens,
            enable_thinking=(not use_no_thinking), base_url=base_url, model=model,
            make_fallback_client=make_fb,
        )
        if not err:
            return content, ""
        errors.append(f"{name}: {err}")
    if not errors:
        return "", "API 客户端不可用（未配置密钥或未安装 openai 库）"
    return "", "；".join(errors)


def chat_json(
    system_prompt: str,
    user_text: str,
    temperature: float = 0.0,
    max_tokens: int = 512,
    enable_thinking: bool = False,
    use_cache: bool = True,
    namespace: str = "",
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    调用大模型并解析 JSON 输出，供三个分析模块复用。

    返回 (obj, error)：
      - 成功: (dict, "")
      - 失败: (None, 错误信息)  —— 无密钥 / openai 未安装 / 网络 / 解析失败
    调用方据此返回默认值字典 + error 键，保证永不抛异常。

    use_cache=False：不读不写进程内缓存，但仍走 single-flight。用于"同一文本再取一个
    独立样本"的边界复核（core_judge 的档位边界），否则第二次调用只会命中缓存、
    拿不到独立样本。
    namespace：缓存分区名，见 `_cache_key`。不同链路复用同一份缓存时用它隔开。
    """
    try:
        content, err = _chat_raw(system_prompt, user_text, temperature, max_tokens,
                                 enable_thinking, use_cache, namespace)
    except Exception as exc:  # noqa: BLE001  这句"永不抛异常"必须靠兜底成立
        return None, f"调用异常: {type(exc).__name__}: {exc}"
    if err:
        return None, err
    obj = extract_json_obj(content)
    if obj is None:
        return None, "模型返回无法解析的JSON"
    return obj, ""


def chat_text(
    system_prompt: str,
    user_text: str,
    temperature: float = 0.3,
    max_tokens: int = 800,
    enable_thinking: bool = False,
    namespace: str = "",
) -> Tuple[str, str]:
    """
    调用大模型获取自由文本输出（报告生成用），返回 (正文, 错误信息)。
    任何失败都不抛异常，错误信息放入第二个返回值。

    namespace：缓存分区名，见 `_cache_key`。深读（attn_agent.deep_read）用它把自己
    与主分析报告隔开，避免两边的缓存互相串结果。
    """
    try:
        return _chat_raw(system_prompt, user_text, temperature, max_tokens,
                         enable_thinking, True, namespace)
    except Exception as exc:  # noqa: BLE001  同上：文档承诺"不抛异常"就要兜住
        return "", f"调用异常: {type(exc).__name__}: {exc}"
