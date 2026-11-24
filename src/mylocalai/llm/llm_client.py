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
    （见 src/mylocalai/rumor/verdict.py 的分桶与滞回），不要指望调用侧传 seed 解决漂移。

配置（项目根目录 .env，环境变量优先）：
    LLM_PROVIDER       提供方名（longcat / deepseek / openai，默认 longcat）
    LLM_FALLBACKS      备用提供方，逗号分隔（如 openai,deepseek）；主链路失败时依次尝试
    LONGCAT_API_KEY / LONGCAT_BASE_URL / LONGCAT_MODEL    提供方自己的三项配置
    LLM_TIMEOUT_S      单次 HTTP 尝试的最长等待秒数（默认 30）
    LLM_MAX_RETRIES    单次调用的重试次数（默认 1，即最多 2 次尝试）
    MYLOCALAI_MODELS_PATH / MYLOCALAI_MODEL_SECRETS_PATH
                       模型注册表与密钥文件的位置；设置面板「模型管理」写的就是
                       这两份（见 docs/模型管理设计规格.md）

设计约定：
  - API 密钥一律放 .env，禁止硬编码进源码。
  - 模型注册表（设置面板维护的提供方/模型目录/单模型参数）由
    mylocalai.llm.model_registry 落盘，本模块在 providers() 里把它覆盖进提供方表；
    没有 data/models.json 时表内容与覆盖前逐字节一致（规格 §0.4 不变量），
    缓存键因此也不变（tests/test_llm_gateway.py 的金样本锁住这一点）。
  - 所有调用失败都返回错误信息而不抛异常，由调用方决定默认值，
    保证"单模块失败不拖垮整体"的容错策略在各模块一致落地。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from mylocalai.llm import llm_cache
# 顶部直接 import 不会成环：model_registry 刻意不 import 本模块（它抄了一份兜底提供方表），
# 且它只在函数体内惰性 import core.privacy。llm_cache / privacy 的导入顺序保持现状。
from mylocalai.llm import model_registry
from mylocalai.util.text_safety import sanitize_text

# ---- 提供方表：key_env / base_url / model 成组绑定 ----
# 换供应商只改 LLM_PROVIDER 一项，不会出现"换了密钥、地址还指着旧家"的静默错配。
# 表本身外置到 mylocalai/llm/providers.json（2026-10-22 修复 G4）：可用
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
    """当前提供方表（进程内缓存；`reset_cache()` 可强制重读）。

    providers.json 是底表；`data/models.json`（设置面板「模型管理」写的那份）里的提供方
    按规格 §5.1 **覆盖/新增**进来：base_url 用注册表值，model 用注册表当前模型或该提供方
    目录里第一个启用的模型，key_env 用注册表声明的名字（没声明就合成一个）。
    没有 models.json 时原样返回底表——"没配模型管理"的既有部署行为零变化。
    """
    global _PROVIDERS_CACHE
    with _PROVIDERS_LOCK:
        if _PROVIDERS_CACHE is None:
            from mylocalai.config import config_loader
            data, warnings = config_loader.load_config(
                _PROVIDERS_PATH, builtin=_BUILTIN_PROVIDERS,
                required_sections=("longcat",),
                env_full="LLM_PROVIDERS_PATH", env_extra="LLM_PROVIDERS_EXTRA_PATH")
            _PROVIDER_WARNINGS.clear()
            _PROVIDER_WARNINGS.update(warnings)
            _PROVIDERS_CACHE = _merge_registry_providers(data)
        return _PROVIDERS_CACHE


def _merge_registry_providers(base: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把模型注册表里的提供方覆盖/合并进 providers.json 底表（规格 §5.1）。

    两处刻意保守：
      - 没有 data/models.json 时**原样返回底表**。这是规格 §0.4 的"默认行为零变化"
        不变量，金样本缓存键正依赖它——顺带也保住了 LLM_PROVIDERS_PATH 整套替换
        不被注册表的内置投影覆盖；
      - 注册表读取一律 fail-soft：读路径不存在、注册表抛任何异常，都只当"没有覆盖"，
        绝不让模型配置拖垮分析链路（规格 §0.3）。
    """
    try:
        if not model_registry.registry_path().exists():
            return base
        overrides = _registry_provider_overrides()
    except Exception:  # noqa: BLE001  注册表坏掉也不能拖垮提供方表
        return base
    if not overrides:
        return base
    merged = {pid: dict(spec) for pid, spec in base.items()}
    for pid, override in overrides.items():
        spec = dict(merged.get(pid) or {})
        key_env = (override["key_env"] or str(spec.get("key_env") or "")
                   or _synthesized_key_env(pid))
        model_env, base_url_env = _synthesized_env_names(pid, key_env)
        spec["key_env"] = key_env
        # 内置三家在 providers.json 里本就带 *_ENV，setdefault 保留原值；
        # 自定义提供方按 key_env 改名合成（与注册表私有实现同一口径，见 _synthesized_env_names）
        spec.setdefault("base_url_env", base_url_env)
        spec.setdefault("model_env", model_env)
        # 两个键必须落在表里（哪怕注册表给的是空值）：_resolve / provider_spec 都按
        # spec["model"]、spec["base_url"] 直接取值，缺键会 KeyError 而不是"未配置"
        spec["base_url"] = override["base_url"] or str(spec.get("base_url") or "")
        spec["model"] = override["model"] or str(spec.get("model") or "")
        spec.setdefault("supports_thinking_disable", False)
        merged[pid] = spec
    return merged


def _registry_provider_overrides() -> Dict[str, Dict[str, str]]:
    """注册表口径的提供方覆盖表：pid → {base_url, model, key_env}（规格 §5.1）。

    只取 llm_client 用得上的三项。模型取"当前选中的模型"（active.model），其余提供方取
    目录里第一个启用的模型——与注册表 active() 的解析口径一致，避免两边对"当前模型"
    给出不同答案（那种不一致会变成"面板显示 A、客户端发 B"的鬼故事）。
    """
    data = model_registry.view()
    active = data.get("active") or {}
    active_pid = active.get("provider")
    active_model = active.get("model")
    overrides: Dict[str, Dict[str, str]] = {}
    for entry in data.get("providers") or []:
        pid = str(entry.get("id") or "")
        if not pid:
            continue
        model = ""
        if pid == active_pid and active_model:
            model = str(active_model)
        else:
            for item in entry.get("models") or []:
                if item.get("enabled", True):
                    model = str(item.get("id") or "")
                    break
        overrides[pid] = {
            "base_url": str(entry.get("base_url") or ""),
            "model": model,
            "key_env": str(entry.get("key_env") or ""),
        }
    return overrides


def _env_fragment(pid: str) -> str:
    """PID 转环境变量片段：非 [A-Z0-9_] 一律换成下划线（acme-2 → ACME_2）。"""
    return re.sub(r"[^A-Z0-9_]", "_", str(pid).upper())


def _synthesized_key_env(pid: str) -> str:
    """注册表没声明 key_env 时的兜底环境变量名。

    与 model_registry 的私有 `_key_env_name` 保持同一口径：那边不 import 本模块
    （会成环，它抄了一份兜底提供方表），这边也不 import 它的私有名，于是同样的规则
    写了两份——改其一必须同步改其二，否则"面板没配、环境变量配了"会互相找不到。
    """
    return f"MYLOCALAI_{_env_fragment(pid)}_API_KEY"


def _synthesized_env_names(pid: str, key_env: str) -> Tuple[str, str]:
    """自定义提供方的 (模型名环境变量, 接口地址环境变量)。

    照 key_env 改名最直观（EXAMPLE_API_KEY → EXAMPLE_MODEL / EXAMPLE_BASE_URL），
    与注册表私有 `_model_env_name` / `_base_url_env_name` 同一口径（同上，两边必须一致）；
    没声明 key_env 时才按 PID 合成 MYLOCALAI_<PID>_MODEL / _BASE_URL。
    """
    if key_env.endswith("_API_KEY") and len(key_env) > len("_API_KEY"):
        prefix = key_env[:-len("_API_KEY")]
        return f"{prefix}_MODEL", f"{prefix}_BASE_URL"
    fragment = _env_fragment(pid)
    return f"MYLOCALAI_{fragment}_MODEL", f"MYLOCALAI_{fragment}_BASE_URL"


def provider_warnings() -> List[str]:
    """提供方表加载时出现的问题（为空表示一切正常）。"""
    providers()
    return list(_PROVIDER_WARNINGS.values())


def supports_thinking_disable(provider: str) -> bool:
    """该提供方是否接受 extra_body 关闭思维链（不是所有家都认这个参数）。"""
    spec = providers().get(provider) or {}
    return bool(spec.get("supports_thinking_disable"))


def _load_env() -> None:
    """加载项目根目录的 .env；python-dotenv 未安装或文件缺失时静默跳过。

    项目根从本文件向上逐级探测（src/mylocalai/llm → src → 仓库根；pip install -e
    与源码直跑两种布局都成立），最多向上 6 级，找不到就静默跳过。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(here, ".env")):
            load_dotenv(os.path.join(here, ".env"))
            return
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent


_load_env()

# ---- 过程内缓存 ----
_CONFIG_LOCK = threading.Lock()
_CONFIG_ERROR = ""                      # 配置自检失败原因（空串=正常）
_RESOLVED: Optional[Dict[str, str]] = None
# 提供方是否接受"关闭思维链"参数：key=(base_url, model) -> True/False；未记录=未知
_THINKING_SUPPORT: Dict[Tuple[str, str], bool] = {}
# 单模型运行期参数（规格 §5.4）：key=(provider, model) -> {"max_output_tokens": int|None,
# "reasoning": {"param","api_value"}|None}。为什么不每次现算？provider_view() 每次都会
# 深拷贝整份注册表配置，而缓存键在每条提示词上都要算一次，现算会把分析链路拖慢；
# 写接口保存后必调 reset_cache()，这里跟着一起清（口径同 providers.json 底表）。
_PARAMS_LOCK = threading.Lock()
_PARAMS_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
# 提供方重试策略算出的"最多重试几次"（规格 §8.1）：key=provider -> int。
# 为什么不每次现算：_client_for() 在一条备用链里会对每家各算一次，而 provider_view()
# 每次都深拷贝整份注册表；重试次数只随配置变、不随请求变，缓存起来最省。写接口保存后
# 必调 reset_cache()，这里跟着一起清（与 _PARAMS_CACHE 同一套失效口径）。
_RETRY_LOCK = threading.Lock()
_RETRY_CACHE: Dict[str, int] = {}


def reset_cache() -> None:
    """清掉配置与能力缓存（改环境变量后供测试/长驻进程重新解析）。

    模型注册表（data/models.json）的进程内缓存一并清：设置面板的写接口保存后只调
    这一个函数，两边必须同时失效，否则会出现"面板显示新模型、客户端还在用旧的"
    这种最难查的静默不一致。
    """
    global _RESOLVED, _CONFIG_ERROR, _PROVIDERS_CACHE
    with _CONFIG_LOCK:
        _RESOLVED = None
        _CONFIG_ERROR = ""
        _THINKING_SUPPORT.clear()
    with _PARAMS_LOCK:
        _PARAMS_CACHE.clear()
    with _RETRY_LOCK:
        _RETRY_CACHE.clear()
    with _PROVIDERS_LOCK:
        _PROVIDERS_CACHE = None
    model_registry.reset_cache()


def _provider_names() -> List[str]:
    """主提供方 + 备用链（LLM_FALLBACKS 逗号分隔，取合法名并在前向去重）。

    主提供方的口径（规格 §5.3）：LLM_PROVIDER 环境变量 → 注册表当前提供方 → longcat。
    没有 models.json 时注册表给的就是内置主提供方，与改动前完全一致。
    """
    names: List[str] = []
    primary = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if not primary:
        primary = _registry_active_provider() or DEFAULT_PROVIDER
    names.append(primary)
    for raw in (os.environ.get("LLM_FALLBACKS") or "").split(","):
        name = raw.strip().lower()
        if name and name not in names:
            names.append(name)
    return names


def _registry_active_provider() -> str:
    """注册表里的当前提供方（models.json 口径，不掺环境变量）；读不到时返回空串。"""
    try:
        return str(model_registry.active().get("provider") or "").strip()
    except Exception:  # noqa: BLE001  注册表出问题按"没有配置"处理，退回内置主提供方
        return ""


def _provider_key(pid: str, spec: Dict[str, Any]) -> Tuple[str, str]:
    """该提供方的 (密钥, 来源 env|stored|none)（规格 §5.2）。

    环境变量优先、注册表密钥文件其次：与仓库既有口径一致（.env 是部署侧的最终裁定），
    也让"临时换一把钥匙排障"不必先删本地文件。两者都没有就是无密钥，调用方据此给出
    "未配置 X"的提示——与只有 .env 的年代行为完全相同。
    """
    key_env = str(spec.get("key_env") or "")
    from_env = os.environ.get(key_env, "").strip() if key_env else ""
    if from_env:
        return from_env, "env"
    value, source = model_registry.provider_secret(pid)
    value = (value or "").strip()
    if value:
        return value, source
    return "", "none"


def _resolve() -> Dict[str, str]:
    """解析主提供方的 密钥/地址/模型，并做"密钥与服务商是否匹配"的自检。

    自检规则：密钥必须来自当前提供方自己的环境变量；若当前提供方没配密钥、
    却配了别家的密钥，则记下错配原因并拒绝建客户端——这正是
    "只改密钥、地址还指着 LongCat" 那个静默错配的修复点。

    提供方/模型/地址的优先级见 `_provider_names` 与 `providers()`：环境变量 → 注册表 →
    providers.json 内置表；密钥口径见 `_provider_key`（环境变量 → 注册表密钥文件）。
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

        api_key, _key_source = _provider_key(primary, spec)
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


def _config_error_kind(error: str) -> str:
    """把 config_error() 的文案归类成给前端的类别词面（key / unknown）。

    只做"这条错误是哪一类"的分类，与失败先后顺序无关：
    密钥没配 / 密钥与服务商不匹配都算 key（用户要去配密钥或改提供方）；
    "未知提供方"算 unknown（LLM_PROVIDER 指向 providers 表里没有的名字）；
    其余一律 unknown——宁可不猜，也不给一个听起来很确定的错类别。
    """
    if any(token in error for token in ("未配置", "密钥", "不匹配")):
        return "key"
    if "未知提供方" in error:
        return "unknown"
    return "unknown"


def _config_diagnosis() -> Tuple[bool, str, str]:
    """(是否可用, 说明, 失败类别)，检查顺序与 `_chat_raw` 的调用链逐条同序。

    为什么需要它：`/api/health` 原来只消费 `config_error()`（密钥口径），于是
    "隐私授权没开"会被报成"未配置 XXX_API_KEY"——用户照着去配密钥，配完仍然
    全链路降级，因为真正的第一道闸是隐私授权（WS2 实测故障：诊断与真实失败
    原因不一致）。调用链 `_chat_raw_providers` 对主提供方的真实顺序是：
        1. 提供方不在 providers 表里 → "未知提供方"（`spec is None` 直接 continue，
           隐私门根本不会为它执行）；
        2. `load_preferences().require_provider(provider, base_url)` —— fail-closed，
           发生在建客户端与任何外发之前；loopback 豁免由 `allows_provider_url` 处理；
        3. `create_client()` → `config_error()`（密钥 / 未知提供方 / 错配）。
    这里照抄这个顺序：隐私闸不过时类别是 privacy，密钥类问题排在它后面。
    隐私偏好**每次现取**（与调用链同构，不做缓存）：授权状态翻转后诊断必须立刻
    跟着翻，否则"刚在面板里勾上、健康检查还说过期话"比不诊断更糟。
    """
    provider = _provider_names()[0]
    spec = providers().get(provider)
    if spec is not None:
        # 未知提供方时调用链在隐私门之前就跳过（spec is None → continue），所以这里
        # 同样不跑隐私门，直接由 config_error() 给出"未知提供方"（类别 unknown）——
        # 否则"提供方不存在"会被误报成"未授权"，那是另一种诊断与实况不符
        base_url = os.environ.get(spec["base_url_env"], "").strip() or spec["base_url"]
        try:
            from mylocalai.core.privacy import load_preferences
            load_preferences().require_provider(provider, base_url)
        except Exception as exc:  # noqa: BLE001  fail-closed：偏好读取失败也按未授权处理
            return False, f"{type(exc).__name__}: {exc}", "privacy"
    error = config_error()
    if error:
        return False, error, _config_error_kind(error)
    info = _resolve()
    return True, f"提供方 {info['provider']} · 模型 {info['model']} · 地址 {info['base_url']}", ""


def validate_config() -> Tuple[bool, str]:
    """启动自检入口：返回 (是否可用, 说明)。不抛异常。

    返回形状不变（`/api/health` 与 tests/test_stability_config.py 都按两元组消费），
    口径改走 `_config_diagnosis()`：隐私授权不过时说明写的是隐私错误原文，
    而不是排在它后面的密钥错误。
    """
    ok, detail, _kind = _config_diagnosis()
    return ok, detail


def config_error_kind() -> str:
    """配置不可用的类别词面：privacy / key / unknown；配置可用时是空串。

    供 `/api/health` 的 `llm.error_kind` 消费（服务状态 chip 与设置面板据此分流
    提示文案）。口径与检查顺序见 `_config_diagnosis()`。
    """
    ok, _detail, kind = _config_diagnosis()
    return "" if ok else kind


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


def _provider_retry(provider: str) -> int:
    """该提供方本次调用允许的**内部重试次数**（提供方重试策略 retry.policy，规格 §8.1）。

    取注册表里 provider_view(pid)["retry"]["policy"]，再换算成次数：
      - `default`（或缺省、或注册表读不到）→ `get_max_retries()`，即 LLM_MAX_RETRIES（默认 1），
        与加这个字段之前**逐字节一致**——没有 data/models.json 的部署行为不变（规格 §8.0.1）；
      - `off` → 0（不再内部重试）；
      - `aggressive` → 3（更积极）；
      - 任何无法识别的取值 / 注册表换算不出次数（返回 None）→ 一律回退 `get_max_retries()`。
    重试次数只决定"同一次调用最多发几个 HTTP"，**不进 `_cache_key`**：同一段提示词不会因为
    改了几次重试就变成另一条缓存（金样本 tests/test_llm_gateway.py 锁着键的字节级稳定）。

    结果按提供方记忆，`reset_cache()` 一并清（与 `_model_params` 同一套失效口径）。
    **任何异常都不向外冒**：注册表坏掉只当"没有声明策略"，绝不能因为读配置失败让一次
    对话调用直接失败（与 `_model_params` 的 fail-soft 口径相同）。
    """
    with _RETRY_LOCK:
        cached = _RETRY_CACHE.get(provider)
    if cached is not None:
        return cached
    retries = get_max_retries()
    try:
        entry = model_registry.provider_view(provider) or {}
        retry = entry.get("retry") if isinstance(entry, dict) else None
        policy = retry.get("policy") if isinstance(retry, dict) else None
        if isinstance(policy, str) and policy.strip():
            value = model_registry.retry_max_retries(policy.strip())
            # bool 是 int 的子类，True/False 不算合法次数；负数同理
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                retries = value
    except Exception:  # noqa: BLE001  注册表出问题只当"用默认重试"，照常调用
        retries = get_max_retries()
    with _RETRY_LOCK:
        _RETRY_CACHE[provider] = retries
    return retries


def _client_for(provider: str, max_retries: Optional[int] = None):
    """按提供方建客户端；该提供方没有自己的密钥时返回 None。

    max_retries 供"去掉关闭思维链参数后的回落调用"使用（传 0，不再内部重试），
    避免把一次逻辑调用放大成 (max_retries × 2) 个 HTTP。显式传入的值**优先级最高**，
    压过该提供方的重试策略（回落调用必须真的只发一次）。
    """
    spec = providers().get(provider)
    if spec is None:
        return None
    api_key, _key_source = _provider_key(provider, spec)
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
                  max_retries=(max_retries if max_retries is not None
                               else _provider_retry(provider)))


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

    供 mylocalai.llm.adapter（LangChain 侧）复用"提供方表 + 备用链"的同一个口径，
    避免 Agent 只连主提供方、备用链路对它完全无效（2026-10-15 检验报告发现 3）。
    密钥口径同 `_provider_key`：注册表密钥文件里配了的提供方也算"有密钥"。
    """
    names = _provider_names()
    for name in names:
        spec = providers().get(name)
        if spec is None:
            continue
        if _provider_key(name, spec)[0]:
            return name
    return None


def provider_spec(name: str) -> Optional[Dict[str, Any]]:
    """按提供方名取 密钥/地址/模型 三项配置（含环境变量覆盖），未知提供方返回 None。"""
    spec = providers().get(name)
    if spec is None:
        return None
    api_key, _key_source = _provider_key(name, spec)
    return {
        "provider": name,
        "api_key": api_key,
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


# ---------------------------------------------------------------------------
# 单模型运行期参数（规格 §5.4）：max_output_tokens 上限与推理等级档位
# ---------------------------------------------------------------------------
# 数据来自模型注册表里"对单个模型设的参数"（设置面板图片三那一屏）。为什么在调用侧
# 生效而不是改 _resolve() 的返回形状：_resolve() 的四个键是 provider/api_key/base_url/
# model 的长期契约（tests/test_stability_config.py 等按这个形状断言），参数是"这次请求
# 怎么发"的事，与"连哪家"是两层，混在一起会让旧调用方莫名其妙多拿到几个键。

def _model_params(provider: str, model: str) -> Dict[str, Any]:
    """该提供方/模型的运行期参数口径：{"max_output_tokens": int|None, "reasoning": dict|None}。

    注册表里没有这个模型（providers.json 增量加的一家、或根本没有 models.json）时两项
    都是 None，于是"不下调 max_tokens、不多带参数"——没有 models.json 的部署请求逐字节
    不变（规格 §0.4）。结果按 (provider, model) 记忆，reset_cache() 一并清。
    """
    cache_key = (provider, model or "")
    with _PARAMS_LOCK:
        cached = _PARAMS_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    params: Dict[str, Any] = {"max_output_tokens": None, "reasoning": None}
    try:
        entry = model_registry.provider_view(provider) or {}
    except Exception:  # noqa: BLE001  注册表出问题只当"没有声明参数"，照常调用
        entry = {}
    for item in entry.get("models") or []:
        if item.get("id") != model:
            continue
        limit = item.get("max_output_tokens")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            params["max_output_tokens"] = limit
        params["reasoning"] = _active_reasoning(item.get("reasoning"))
        break
    with _PARAMS_LOCK:
        _PARAMS_CACHE[cache_key] = params
    return dict(params)


def _active_reasoning(reasoning: Any) -> Optional[Dict[str, str]]:
    """当前档位真的会发给上游的推理参数 {"param": ..., "api_value": ...}；否则 None。

    只有 enabled 且当前档位 api_value 非空才算数：off 档位（api_value 为 null）的语义
    就是"不额外发参数"，enabled=false 同理——两者都不该让请求多带一个字节。
    """
    data = reasoning if isinstance(reasoning, dict) else {}
    if not data.get("enabled"):
        return None
    active_tier = data.get("active_tier")
    api_value = None
    for tier in data.get("tiers") or []:
        if isinstance(tier, dict) and tier.get("tier") == active_tier:
            api_value = tier.get("api_value")
            break
    if api_value is None or api_value == "":
        return None
    param = str(data.get("param") or "").strip()
    if not param:
        return None
    return {"param": param, "api_value": str(api_value)}


def _clamp_max_tokens(max_tokens: int, params: Dict[str, Any]) -> int:
    """把 max_tokens 下调到该模型声明的 max_output_tokens 以内（规格 §5.4）。

    只下调、不放大：调用方要少了就按少的发（放大只会多花钱、还可能被上游拒）；
    模型没声明上限（None）时原样返回——没有 models.json 的部署一个字节不变。
    """
    limit = (params or {}).get("max_output_tokens")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        return min(int(max_tokens), limit)
    return max_tokens


def _reasoning_extra_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """要合进 extra_body 的推理档位参数 {"参数名": "参数值"}；不需要时返回空字典。

    返回空字典而不是 None，是为了让调用方能区分"没有参数"与"显式不带参数"：
    缓存键只在前者非空时才追加指纹（见 `_cache_key` 的 extra_params）。
    """
    reasoning = (params or {}).get("reasoning") or {}
    param = str(reasoning.get("param") or "").strip()
    value = reasoning.get("api_value")
    if not param or value is None or value == "":
        return {}
    return {param: value}


# ---------------------------------------------------------------------------
# 进程级真实用量计数（M4 成本真实计数，工作指导 §6 M4 / §8）
# ---------------------------------------------------------------------------
# 口径（诚实优先，不估算）：
#   - requests 只在**真实发起上游调用**时计数：计数点放在 _chat_raw_once._call ——
#     主尝试 / extra_body 被拒后的回落尝试 / 备用链各提供方的尝试全部经由这一层，
#     每次真实返回的调用各计 1。缓存命中、single-flight 等待者、预算耗尽（未发起）
#     都到不了这一层，自然不计。
#   - tokens 从响应 usage.total_tokens 取（OpenAI SDK 形状）；取不到（提供方不回
#     usage / 形状不符）就如实记 0，绝不按字数估算。
#   - 失败的尝试（超时 / 5xx / 连接失败）不计：请求未完成计费，计入反而虚高。
#   - 计数器是**进程级**的：llm_cache 的缓存/single-flight 都在同一进程内生效，
#     跨线程安全（threading.Lock + 读时锁拷贝）。
# 已知边界（如实声明，不装看不见）：Agent 的 LangChain 工具轮
# （mylocalai.llm.adapter.build_llm 直连 ChatOpenAI）不经本模块，其上游调用不计数；
# 会话/档案侧看到的 requests/tokens 是"llm_client 可见面"的真实值（路由/叙述/摘要/
# classic/attention/深读/证据与声明抽取等全部覆盖）。
_usage_lock = threading.Lock()
_usage_requests = 0
_usage_tokens = 0


def record_usage(requests: int = 1, tokens: int = 0) -> None:
    """累加真实上游用量（内部使用；供计数点调用，测试也可直接注入）。"""
    global _usage_requests, _usage_tokens
    with _usage_lock:
        _usage_requests += int(requests)
        _usage_tokens += int(tokens)


def usage_snapshot() -> Dict[str, int]:
    """当前进程的累计真实用量 {"requests": int, "tokens": int}（读时锁拷贝）。"""
    with _usage_lock:
        return {"requests": int(_usage_requests), "tokens": int(_usage_tokens)}


def reset_usage() -> None:
    """清零用量计数（测试与检验脚本用）。"""
    global _usage_requests, _usage_tokens
    with _usage_lock:
        _usage_requests = 0
        _usage_tokens = 0


def _response_total_tokens(resp: Any) -> int:
    """从响应取 usage.total_tokens；取不到（None/坏形状）如实记 0，不估算。"""
    try:
        value = getattr(getattr(resp, "usage", None), "total_tokens", None)
    except Exception:  # noqa: BLE001  响应形状怪异不影响调用本身
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


# ---------------------------------------------------------------------------
# 空 content 连续计数器（P1-2，上游质量可观测）
# ---------------------------------------------------------------------------
# LongCat 有返回空 content 的前科（README 已知局限，2026-10-14 检验报告），运行期需要
# "上游质量可观测"。这里在 _chat_raw_once 的既有"正文为空"探测点上挂一个进程级连续
# 计数：真实上游调用返回**空 content**（含"仅思考内容"被兜底返回的情况——上游确实
# 给了空正文）时 +1，返回非空 content 时清零，只记连续值；不做任何阈值动作，只供
# /api/health 等处读取。口径对齐上方 M4 计数器：threading.Lock + 读时锁拷贝；计数的
# 是"真实返回了响应"的尝试，异常/超时/预算耗尽（没有响应）不经过探测点，自然不计。
_empty_content_lock = threading.Lock()
_empty_content_streak = 0


def _note_empty_content() -> None:
    """空 content 探测点调用：连续空正文 +1（内部使用）。"""
    global _empty_content_streak
    with _empty_content_lock:
        _empty_content_streak += 1


def _note_nonempty_content() -> None:
    """拿到非空正文时调用：连续计数清零（内部使用）。"""
    global _empty_content_streak
    with _empty_content_lock:
        _empty_content_streak = 0


def empty_content_streak() -> int:
    """当前"连续返回空 content"的上游调用次数（读时锁拷贝；拿到非空正文即清零）。"""
    with _empty_content_lock:
        return int(_empty_content_streak)


def reset_empty_content_streak() -> None:
    """清零空正文连续计数（测试与检验脚本用，口径同 reset_usage）。"""
    global _empty_content_streak
    with _empty_content_lock:
        _empty_content_streak = 0


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
    extra_params: Optional[dict] = None,
) -> Tuple[str, str]:
    """对单个客户端做一次对话调用，返回 (正文, 错误)。

    `make_fallback_client()`：仅在"带参数被拒"时才调用，用来造一个不再内部重试的
    客户端做回落，避免 `(max_retries × 2)` 的叠乘把一次调用放大成 4 个 HTTP。
    `extra_params`：模型注册表要求这次多带的参数（推理等级档位），与"关闭思维链"
    参数合进同一个 extra_body；为空表示不多带（没有 models.json 时正是如此）。
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
        resp = active.chat.completions.create(**kwargs)
        # M4 真实计数点：这里是一切上游调用的唯一汇合层（主尝试/参数回落/备用链都经此），
        # 缓存命中与 single-flight 等待者到不了这里。只计真实返回的调用（失败/超时未计费
        # 不计），tokens 取响应 usage，取不到如实 0。
        record_usage(requests=1, tokens=_response_total_tokens(resp))
        return resp

    # "关闭思维链"参数发不发由调用方按提供方能力决定（见 _chat_raw_providers 的
    # use_no_thinking：不认这参数的提供方传 enable_thinking=True 进来）；这里只在
    # 明确要求关思维链时才带上它
    no_thinking = None if enable_thinking else thinking_extra_body(base_url, model)
    # 推理档位参数与"关闭思维链"并存（规格 §5.4）：注册表要求发的合进来，
    # 两者都没有时 extra 保持 None——请求与没有 models.json 时逐字节相同
    if extra_params:
        extra = {**no_thinking, **extra_params} if no_thinking else dict(extra_params)
    else:
        extra = no_thinking
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
            # 回落：去掉"关闭思维链"参数再试一次（用不再内部重试的客户端）；
            # 注册表要求的推理档位参数保留——它自己才会被拒时，交给下面 except 原样报错
            try:
                resp = _call(extra_params or None, make_fallback_client())
            except _BudgetExhausted:
                return "", "已超出本次分析的时限预算，未发起调用"
            except Exception:
                raise exc      # 回落也失败：保留原错误，且不留下"参数不支持"的永久标记
            # 只有真的发过"关闭思维链"参数才标记不支持：被拒的也可能是推理档位参数，
            # 那时标记上去会让整进程白白丢掉一个本来有效的参数
            if no_thinking is not None:
                mark_thinking_unsupported(base_url, model)
    except Exception as exc:  # noqa: BLE001
        return "", f"API 调用失败: {type(exc).__name__}: {exc}"

    content = resp.choices[0].message.content if resp.choices else ""
    if content and content.strip():
        _note_nonempty_content()
        return content.strip(), ""
    # 既有"空 content"探测点（P1-2 计数接线处）：上游返回了空正文即 +1——即使下面的
    # 思考内容兜底救回了正文，"上游给了空 content"这个事实也如实计数（LongCat 前科）
    _note_empty_content()
    try:
        reasoning = resp.choices[0].message.model_dump().get("reasoning_content") or ""
    except Exception:  # noqa: BLE001
        reasoning = ""
    if reasoning.strip():
        return reasoning.strip(), ""
    return "", "模型未返回正文（可能 max_tokens 不足或仅返回思考过程）"


def _cache_key(system_prompt: str, user_text: str, temperature: float, max_tokens: int,
               enable_thinking: bool, namespace: str = "",
               extra_params: Optional[dict] = None) -> str:
    """缓存键：同一段提示词 + 同一批参数 → 同一个键（与提供方无关，换供应商也不会串结果）。

    `namespace` 是**缓存分区名**：同一段提示词与参数，分区名不同则键不同。
    用途是"两类链路复用同一个 llm_cache，但绝不能互相串结果"——例如关注度深读
    （`src/mylocalai/attention/deep_read.py`，namespace `attn_deep_read`）与主分析报告
    （`report_generator`，无分区名）。否则一旦两边的提示词与用户文本碰巧相同，
    一边就会读到另一边的缓存，把结论污染过去。

    `extra_params` 是**这次调用真的会多带给上游的参数**（模型注册表里的推理等级档位，
    规格 §5.4）：同一段提示词，带不带档位是两种不同的请求，缓存必须分开，所以非空时
    把参数指纹（排序后的 JSON）追加进 blob。为空（默认）时**不往 blob 里加任何东西**，
    这样既有调用方算出的键与加这个参数之前逐字节相同——加参数不该悄悄换掉全部缓存键，
    金样本（tests/test_llm_gateway.py）正锁着这一点。

    编码前先规整孤立代理项：str 里只有代理项无法按 utf-8 编码，混进文本会让这一行抛
    UnicodeEncodeError（2026-10-15 检验报告发现 12，唯一未通过闸门的成因）。
    """
    blob = [get_provider(), get_base_url(), get_model(), system_prompt, str(user_text),
            temperature, max_tokens, enable_thinking]
    if namespace:
        blob.append(namespace)
    if extra_params:
        blob.append(json.dumps(extra_params, ensure_ascii=False, sort_keys=True))
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
    extra_params: Optional[dict] = None,
) -> Tuple[str, str]:
    """
    底层对话调用（带缓存与请求级时限），返回 (正文文本, 错误信息)。

    缓存：同一段提示词 + 同一批参数在进程内只真正调用一次（TTL/上限见 mylocalai/llm/llm_cache.py）。
      这既省 API，也让"同一文本重复分析"拿到同一个结果——上游不认 seed，只能在调用侧钉住。
      只缓存成功结果；失败与预算耗尽都不写缓存，下次仍会真正重试。
      `use_cache=False` 只跳过读/写缓存，**不跳过 single-flight**（并发同请求仍只算一次），
      用于"同一文本再取一个独立样本"的边界复核（core_judge 的档位边界复核会用）。
      `namespace` 是缓存分区名，见 `_cache_key`：不同链路复用同一份进程内缓存时用它隔开，
      否则一边会读到另一边的结果（把结论污染过去）。
    时限：若调用方用 llm_cache.deadline_scope() 设了总预算，预算用尽时立即返回错误
      （转降级），不再一轮轮等满超时。
    `extra_params`：这次要多发给上游的模型参数（推理等级档位，规格 §5.4）。默认 None =
      按模型注册表即时推导（推导出的 max_tokens 与参数同时进缓存键和真实请求）；
      显式传空字典表示“调用方已自行处理，不要推导”（测试与调试用）。
    """
    if llm_cache.budget_exhausted():
        return "", "已超出本次分析的时限预算，未发起调用"
    # 孤立代理项在此处统一规整：它既进不了缓存键，也发不出去（json/HTTP 编码都会失败），
    # 而本函数的契约是"任何失败都返回错误信息而不抛异常"（chat_json 的承诺由此成立）。
    user_text = sanitize_text(user_text)
    # 模型参数在这里一次算清（注册表口径）：max_tokens 先下调到该模型声明的上限，
    # 推理档位换算成 extra_body 片段。同一个值既进缓存键也进真实请求——否则
    # "下调后的请求"会挂在"未下调的键"上，换个 max_tokens 就互相串结果。
    resolved = _resolve()
    params = _model_params(resolved["provider"], resolved["model"])
    max_tokens = _clamp_max_tokens(max_tokens, params)
    if extra_params is None:
        extra_params = _reasoning_extra_params(params)
    try:
        key = _cache_key(system_prompt, user_text, temperature, max_tokens,
                         enable_thinking, namespace, extra_params)
    except Exception as exc:  # noqa: BLE001  键只是"复用"的手段，算不出来也应照常调用
        key = ""
    if not key:
        return _chat_raw_providers(system_prompt, user_text, temperature, max_tokens,
                                   enable_thinking, extra_params=extra_params)
    return llm_cache.call(
        key,
        produce=lambda: _chat_raw_providers(system_prompt, user_text, temperature,
                                            max_tokens, enable_thinking,
                                            extra_params=extra_params),
        accept=lambda result: bool(result[0]) and not result[1],
        use_cache=use_cache,
    )


def _chat_raw_providers(
    system_prompt: str,
    user_text: str,
    temperature: float,
    max_tokens: int,
    enable_thinking: bool = False,
    extra_params: Optional[dict] = None,
) -> Tuple[str, str]:
    """
    真正发起调用的部分：主提供方 → 备用提供方（LLM_FALLBACKS）依次尝试。
    返回 (正文文本, 错误信息)。成功时 error 为空串。

    `extra_params`：这次要多发给上游的模型参数（推理等级档位），空/None = 不多带。
    由 `_chat_raw` 按**主提供方**推导后整链共用：缓存键钉的就是主提供方，参数口径跟着
    键走，备用链路换提供方时才不会悄悄换一批请求字节（否则同一个键会对应不同请求）。
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
        base_url = os.environ.get(spec["base_url_env"], "").strip() or spec["base_url"]
        try:
            from mylocalai.core.privacy import load_preferences
            load_preferences().require_provider(name, base_url)
        except Exception as exc:
            # Fail closed before client construction or any outbound HTTP request.
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
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
            make_fallback_client=make_fb, extra_params=extra_params,
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

    namespace：缓存分区名，见 `_cache_key`。深读（mylocalai.attention.deep_read）用它把自己
    与主分析报告隔开，避免两边的缓存互相串结果。
    """
    try:
        return _chat_raw(system_prompt, user_text, temperature, max_tokens,
                         enable_thinking, True, namespace)
    except Exception as exc:  # noqa: BLE001  同上：文档承诺"不抛异常"就要兜住
        return "", f"调用异常: {type(exc).__name__}: {exc}"
