# -*- coding: utf-8 -*-
"""模型注册表：提供方 / 模型目录 / 单模型参数 / 密钥 的落盘与内存视图。

为什么单独一个模块？设置面板要「添加提供方 → 获取可用模型 → 对单个模型设参数」，
这些数据既不属于提示词配置（prompt_config），也不适合塞进 .env：.env 存密钥合适，
存"目录结构 + 每个模型的上下文窗口/推理档位"不合适。于是两份文件分工：

    data/models.json         提供方 + 模型目录 + 当前选择（**不含密钥**，可整份读进页面）
    data/model_secrets.json  只存密钥（尽力 0600；绝不回显、绝不进日志、绝不进 models.json）

三条硬约束（对应设计规格 §0，动代码前先读；改其一前请先改规格）：

1. **fail-soft**：文件缺失 / JSON 坏 / schema_version 不符 → 退回内置提供方表并留一条
   告警（`warnings()` 可读），**绝不抛异常**拖垮分析链路；坏文件也**不被静默覆盖**
   ——只有用户显式保存时才写盘。这是"配置写坏不能让整个网站起不来"的底线。
2. **不落敏**：任何 view 返回值只给 `api_key_set` / `api_key_source` / `api_key_masked`；
   明文密钥只出现在 `runtime_resolution()`（仅供 llm_client 在进程内建客户端）与写盘瞬间。
3. **原子写**：`mkstemp(同目录)` → `fsync` → `os.replace`，参考
   `mylocalai/core/privacy.py::save_preferences`。半截文件比没有文件更麻烦。

内置提供方（longcat / deepseek / openai）来自同目录的 `providers.json`（运行期唯一事实来源，
与 llm_client 共用），投影成本模块的注册表结构并标 `builtin=True`：可改可选，但不能删。

提供方级 `retry` 策略（default / off / aggressive）是**可选字段**（规格 §8.1）：老文件没有
这个键、或根本没有 models.json 时都按 default 处理，行为与加这个字段之前一致；
`retry_max_retries()` 是 policy → 重试次数的**唯一**映射，供 llm_client 使用。

进程内缓存：读入口带缓存，写入函数成功后自行刷新；**外部直接改文件后请调用
`reset_cache()`**，否则读到的是旧快照。缓存按"路径"区分，测试换环境变量指向临时目录时
无需先清缓存也不会串数据。
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

__all__ = [
    "SCHEMA_VERSION",
    "PROTOCOLS",
    "RUNTIME_PROTOCOLS",
    "INPUT_TYPES",
    "REASONING_TIERS",
    "DEFAULT_REASONING_PARAM",
    "RETRY_POLICIES",
    "RETRY_POLICY_LABELS",
    "LIMITS",
    "ModelRegistryError",
    "registry_path",
    "secrets_path",
    "registry_paths",
    "reset_cache",
    "warnings",
    "view",
    "provider_view",
    "default_model_views",
    "upsert_provider",
    "delete_provider",
    "upsert_model",
    "delete_model",
    "reorder_models",
    "restore_default_models",
    "set_secret",
    "key_status",
    "set_active",
    "active",
    "provider_secret",
    "runtime_resolution",
    "retry_max_retries",
    "available_models",
    "available_models_adhoc",
]

# ---------------------------------------------------------------------------
# 常量：对外契约的一部分（前端下拉、limits 展示都按这里来）
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1
# 只有 openai 协议本站运行期能执行；另外两种先允许"存下来"，UI 标注「本站运行期不支持」
PROTOCOLS = ("openai", "anthropic", "gemini")
RUNTIME_PROTOCOLS = ("openai",)
INPUT_TYPES = ("text", "image")
REASONING_TIERS = ("off", "low", "medium", "high")
DEFAULT_REASONING_PARAM = "reasoning_effort"

# 提供方级重试策略（规格 §8.1）：只影响"发几次"，不进入金样本缓存键。
RETRY_POLICIES = ("default", "off", "aggressive")
RETRY_POLICY_LABELS = {
    "default": "默认（仅临时错误）",
    "off": "不重试",
    "aggressive": "更积极（最多 3 次）",
}

# policy → 客户端 max_retries 的**唯一**映射（None = 沿用 llm_client.get_max_retries()）。
# 为什么集中在一处：llm_client 建客户端时要按它算重试次数，设置面板要按它显示档位文案，
# 两处各写一份 switch 迟早会不一致（"面板写着不重试，实际还在重试两次"最难查）。
_RETRY_POLICY_MAX_RETRIES: Dict[str, Optional[int]] = {
    "default": None,   # 沿用 LLM_MAX_RETRIES（默认 1 → 最多 2 次尝试）
    "off": 0,          # 一次都不重试
    "aggressive": 3,   # 最多 3 次重试（共 4 次尝试）
}

# 上限：写在校验里，也通过 view()["limits"] 交给前端做即时提示，避免"填完才报错"
MAX_PROVIDERS = 50
MAX_MODELS_PER_PROVIDER = 200
MAX_CONTEXT_WINDOW = 10_000_000
MAX_OUTPUT_TOKENS = 1_000_000
MAX_BASE_URL_LEN = 512
MAX_MODEL_ID_LEN = 200
MAX_API_VALUE_LEN = 64

LIMITS = {
    "max_providers": MAX_PROVIDERS,
    "max_models_per_provider": MAX_MODELS_PER_PROVIDER,
    "max_context_window": MAX_CONTEXT_WINDOW,
    "max_output_tokens": MAX_OUTPUT_TOKENS,
}

_ENV_MODELS_PATH = "MYLOCALAI_MODELS_PATH"
_ENV_SECRETS_PATH = "MYLOCALAI_MODEL_SECRETS_PATH"

# 仓库根：src/mylocalai/llm/model_registry.py → parents[3] 与 core/privacy.py 同一口径
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROVIDERS_JSON = Path(__file__).with_name("providers.json")

_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_REASONING_PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,39}$")

# 内置提供方的展示名：providers.json 只声明密钥/地址/模型三项，展示名在本模块补齐，
# 避免为了一个中文/大小写写法去改那张"运行期事实来源"表（LongCat 的驼峰不是 capitalize 能猜的）
_BUILTIN_DISPLAY_NAMES = {"longcat": "LongCat", "deepseek": "DeepSeek", "openai": "OpenAI"}

_PROTOCOL_LABELS = {
    "openai": "OpenAI Chat Completions",
    "anthropic": "Anthropic Messages",
    "gemini": "Gemini generateContent",
}
_INPUT_TYPE_LABELS = {"text": "文本", "image": "图片"}

# providers.json 读不到时的兜底：故意与本仓库 providers.json 的三家保持一致。
# 为什么不 import llm_client 的 _BUILTIN_PROVIDERS？因为集成后 llm_client 会 import 本模块，
# 反向 import 会成环；代价是这张小表要抄一份（改 providers.json 时两边都会有人提醒）。
_FALLBACK_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "longcat": {
        "key_env": "LONGCAT_API_KEY",
        "base_url_env": "LONGCAT_BASE_URL",
        "model_env": "LONGCAT_MODEL",
        "base_url": "https://api.longcat.chat/openai/v1",
        "model": "LongCat-2.5-Preview",
    },
    "deepseek": {
        "key_env": "DEEPSEEK_API_KEY",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "model_env": "DEEPSEEK_MODEL",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "openai": {
        "key_env": "OPENAI_API_KEY",
        "base_url_env": "OPENAI_BASE_URL",
        "model_env": "OPENAI_MODEL",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
}

# 默认主提供方：与 llm_client.DEFAULT_PROVIDER 一致，保证"没有 models.json 时行为零变化"
DEFAULT_PROVIDER = "longcat"

# 写入白名单：多一个字段就多一个说不清的语义面，所以不是白名单里的必须点名拒绝
_PROVIDER_PAYLOAD_KEYS = frozenset(
    {"id", "display_name", "base_url", "protocol", "key_env", "enabled", "models", "api_key",
     "retry"})
_MODEL_PAYLOAD_KEYS = frozenset(
    {"id", "display_name", "context_window", "max_output_tokens", "input_types", "enabled",
     "reasoning"})
_REASONING_KEYS = frozenset({"enabled", "param", "active_tier", "tiers"})
_REASONING_TIER_KEYS = frozenset({"tier", "api_value"})
_RETRY_KEYS = frozenset({"policy"})
# 这些是 GET 派生出来的只读字段；前端/调用方把整个 view 回传时必须点名拒绝，
# 否则"保存一次就把只读字段写进文件"这种事故很难查。
_PROVIDER_VIEW_ONLY_KEYS = frozenset(
    {"builtin", "is_active", "api_key_set", "api_key_source", "api_key_masked",
     "protocol_runtime_supported", "default_models"})
_MODEL_VIEW_ONLY_KEYS = frozenset({"is_active", "migrated_from"})


class ModelRegistryError(ValueError):
    """参数/结构非法（对应 HTTP 400）；错误信息里必须点名是哪个字段、为什么、怎么办。"""


# ---------------------------------------------------------------------------
# 路径与缓存
# ---------------------------------------------------------------------------

def _env_path(name: str) -> Optional[Path]:
    configured = (os.environ.get(name) or "").strip()
    return Path(configured).expanduser() if configured else None


def registry_path() -> Path:
    """模型/提供方配置文件路径（`MYLOCALAI_MODELS_PATH` 可整套重定向，测试用）。"""
    return _env_path(_ENV_MODELS_PATH) or (_REPO_ROOT / "data" / "models.json")


def secrets_path() -> Path:
    """密钥文件路径（`MYLOCALAI_MODEL_SECRETS_PATH` 可整套重定向，测试用）。"""
    return _env_path(_ENV_SECRETS_PATH) or (_REPO_ROOT / "data" / "model_secrets.json")


def registry_paths() -> Dict[str, str]:
    """两份配置文件的绝对路径（`view()["paths"]` 与「打开配置文件」按钮的数据源）。

    纯字符串：**不读也不写**，文件不存在也照常返回路径——"文件在不在"由调用方自己
    （`Path.exists()`）判断，设置面板要据此显示"保存一次后才会生成"。
    """
    return {"models": str(registry_path()), "secrets": str(secrets_path())}


_LOCK = threading.RLock()
# 缓存形态 (文件签名, 状态, 告警)。签名 = 路径 + mtime + 大小：路径不同（测试切临时目录）
# 或文件被**外部**改动过都视为新快照，避免"手改了文件却一直读到旧值"的鬼故事。
_MODELS_CACHE: Optional[Tuple[Tuple[Any, ...], Dict[str, Any], List[str]]] = None
_SECRETS_CACHE: Optional[Tuple[Tuple[Any, ...], Dict[str, str], List[str]]] = None
_BUILTIN_TABLE: Optional[Dict[str, Dict[str, Any]]] = None


def _file_signature(path: Path) -> Tuple[Any, ...]:
    """(路径, mtime_ns, 大小)；文件不存在时后两项为 None（"没有文件"也是一种快照）。"""
    try:
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (str(path), None, None)


def reset_cache() -> None:
    """清进程内缓存（写入后、测试里、以及外部手改文件之后调用）。"""
    global _MODELS_CACHE, _SECRETS_CACHE
    with _LOCK:
        _MODELS_CACHE = None
        _SECRETS_CACHE = None


def _builtin_table() -> Dict[str, Dict[str, Any]]:
    """内置提供方表（providers.json；读不到/形状不对就退回模块内兜底副本）。"""
    global _BUILTIN_TABLE
    with _LOCK:
        if _BUILTIN_TABLE is not None:
            return _BUILTIN_TABLE
        table: Dict[str, Dict[str, Any]] = {}
        try:
            raw = json.loads(_PROVIDERS_JSON.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if str(key).startswith("_") or not isinstance(value, dict):
                        continue  # `_comment` 之类的说明键不是提供方
                    if not all(isinstance(value.get(field), str)
                               for field in ("key_env", "base_url", "model")):
                        continue
                    table[str(key)] = dict(value)
        except Exception:  # noqa: BLE001  providers.json 坏掉也不能拖垮注册表
            table = {}
        if not table:
            table = copy.deepcopy(_FALLBACK_PROVIDERS)
        _BUILTIN_TABLE = table
        return table


def _default_retry() -> Dict[str, Any]:
    """重试策略的缺省结构：default = 沿用 `get_max_retries()`（老文件/无文件行为不变）。"""
    return {"policy": "default"}


def _default_reasoning() -> Dict[str, Any]:
    """未启用推理档位时的默认结构：只留一个 off 档（api_value=None 表示不额外发参数）。"""
    return {
        "enabled": False,
        "param": DEFAULT_REASONING_PARAM,
        "active_tier": "off",
        "tiers": [{"tier": "off", "api_value": None}],
    }


def _default_model_entry(model_id: str) -> Dict[str, Any]:
    """内置提供方的默认模型目录只有一项 = providers.json 声明的 model。

    context_window / max_output_tokens 故意留 None（"未声明"）：
    留 None 时 llm_client 不会下调 max_tokens、不会多带参数，
    这样"没有 data/models.json"时请求与改动前逐字节一致（规格 §0.4）。
    """
    return {
        "id": model_id,
        "display_name": model_id,
        "context_window": None,
        "max_output_tokens": None,
        "input_types": ["text"],
        "enabled": True,
        "reasoning": _default_reasoning(),
    }


def _builtin_provider_entry(pid: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """把 providers.json 的一条规格投影成注册表的提供方对象。"""
    return {
        "display_name": _BUILTIN_DISPLAY_NAMES.get(pid, pid),
        "base_url": str(spec.get("base_url") or ""),
        "protocol": "openai",   # providers.json 的三家都是 OpenAI 兼容协议
        "key_env": str(spec.get("key_env") or ""),
        "enabled": True,
        "models": [_default_model_entry(str(spec.get("model") or ""))],
        "retry": _default_retry(),
    }


def _empty_provider_entry(pid: str) -> Dict[str, Any]:
    """自定义提供方的字段默认值：缺 base_url 的会在校验时被明确拒绝，而不是猜一个地址。"""
    return {
        "display_name": pid,
        "base_url": "",
        "protocol": "openai",
        "key_env": "",
        "enabled": True,
        "models": [],
        "retry": _default_retry(),
    }


def _builtin_state(table: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """没有配置文件时的完整状态：内置三家 + 默认选中内置主提供方。"""
    providers = {pid: _builtin_provider_entry(pid, spec) for pid, spec in table.items()}
    provider = DEFAULT_PROVIDER if DEFAULT_PROVIDER in providers else next(iter(providers), "")
    return {
        "schema_version": SCHEMA_VERSION,
        "active": {"provider": provider, "model": None},
        "providers": providers,
    }


# ---------------------------------------------------------------------------
# 校验（规格 §2.2，逐条落实；错误信息点名字段 + 给出取值示例）
# ---------------------------------------------------------------------------

def _field(where: str, name: str) -> str:
    """把字段路径拼成 "provider.base_url" 这种能直接被前端展示的样子。"""
    return f"{where}.{name}" if where else name


def _coerce_bool(value: Any, field: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ModelRegistryError(
        f"{field} 必须是 true 或 false，实际是 {type(value).__name__}（收到 {value!r}）。")


def _coerce_int(value: Any, field: str, low: int, high: int) -> Optional[int]:
    """整数范围校验；None 表示"未声明"（未声明就不拦、不下调参数）。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelRegistryError(
            f"{field} 必须是整数（例如 65536），或不填表示未声明，实际是 "
            f"{type(value).__name__}（收到 {value!r}）。")
    if value < low or value > high:
        raise ModelRegistryError(
            f"{field} 超出允许范围 {low}..{high}（收到 {value}）。")
    return value


def _coerce_text(value: Any, field: str, max_len: int, default: str) -> str:
    """短文本字段：去前后空白、长度上限；空值退回默认（例如 display_name 退回 id）。"""
    if value is None:
        return default
    if not isinstance(value, str):
        raise ModelRegistryError(
            f"{field} 必须是字符串，实际是 {type(value).__name__}（收到 {value!r}）。")
    text = value.strip()
    if not text:
        return default
    if len(text) > max_len:
        raise ModelRegistryError(f"{field} 太长（{len(text)} 字符 > {max_len}），请精简。")
    return text


def _validate_provider_id(pid: Any) -> str:
    if not isinstance(pid, str):
        raise ModelRegistryError(
            f"提供方 ID 必须是字符串，实际是 {type(pid).__name__}（收到 {pid!r}）。")
    if not _PROVIDER_ID_RE.match(pid):
        raise ModelRegistryError(
            f"提供方 ID 非法：{pid!r}。要求：小写字母开头，只含小写字母/数字/下划线/连字符，"
            f"最长 32 个字符（例如 deepseek、my-gateway-2）。")
    return pid


def _validate_base_url(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ModelRegistryError(
            f"{field} 必须是字符串（例如 https://api.deepseek.com/v1），实际是 "
            f"{type(value).__name__}（收到 {value!r}）。")
    text = value.strip()
    if not text:
        raise ModelRegistryError(f"{field} 不能为空，请填写提供方的接口地址（如 https://api.example.com/v1）。")
    if len(text) > MAX_BASE_URL_LEN:
        raise ModelRegistryError(f"{field} 太长（{len(text)} 字符 > {MAX_BASE_URL_LEN}）。")
    if any(ch.isspace() for ch in text):
        raise ModelRegistryError(f"{field} 里不能有空白字符：{text!r}。")
    if not text.lower().startswith(("http://", "https://")):
        raise ModelRegistryError(
            f"{field} 必须以 http:// 或 https:// 开头（收到 {text!r}）。")
    try:
        host = (urlsplit(text).hostname or "").strip()
    except ValueError as exc:
        raise ModelRegistryError(f"{field} 不是合法地址：{text!r}（{exc}）。") from exc
    if not host:
        raise ModelRegistryError(
            f"{field} 里没有主机名（host）：{text!r}。请写成 https://api.example.com/v1 这样的完整地址。")
    return text


def _validate_key_env(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ModelRegistryError(
            f"{field} 必须是字符串或留空，实际是 {type(value).__name__}（收到 {value!r}）。")
    text = value.strip()
    if text and not _KEY_ENV_RE.match(text):
        raise ModelRegistryError(
            f"{field} 不是合法的环境变量名：{text!r}。要求：大写字母开头，只含大写字母/数字/下划线，"
            f"最长 64 个字符（例如 DEEPSEEK_API_KEY）；不需要读环境变量时留空。")
    return text


def _normalize_input_types(value: Any, where: str) -> List[str]:
    field = _field(where, "input_types")
    if value is None:
        return ["text"]
    if not isinstance(value, list):
        raise ModelRegistryError(
            f"{field} 必须是数组，取值来自 {'、'.join(INPUT_TYPES)}（例如 [\"text\"]）。")
    picked: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ModelRegistryError(
                f"{field} 里的每一项都必须是字符串，实际有一项是 {type(item).__name__}（收到 {item!r}）。")
        name = item.strip()
        if name not in INPUT_TYPES:
            raise ModelRegistryError(
                f"{field} 里不认识的取值 {item!r}：只支持 {'、'.join(INPUT_TYPES)}。")
        if name not in picked:
            picked.append(name)   # 去重：同一类型重复勾选没有额外语义，存一份即可
    if not picked:
        raise ModelRegistryError(f"{field} 不能为空：至少选一种输入类型（{'、'.join(INPUT_TYPES)}）。")
    return picked


def _normalize_reasoning(value: Any, where: str = "") -> Dict[str, Any]:
    """推理档位：档位表 + 当前档位 + 该档位要发给上游的参数名/参数值。

    空 tiers 强制 enabled=false（没有档位就必然没法"启用"），active_tier 落回 off。
    """
    prefix = _field(where, "reasoning")
    if value is None:
        return _default_reasoning()
    if not isinstance(value, dict):
        raise ModelRegistryError(
            f"{prefix} 必须是对象（含 enabled / param / active_tier / tiers），实际是 "
            f"{type(value).__name__}（收到 {value!r}）。")
    unknown = sorted(set(value) - _REASONING_KEYS)
    if unknown:
        raise ModelRegistryError(
            f"不接受的字段 {prefix}.{unknown[0]}：{prefix} 只允许 "
            f"{'、'.join(sorted(_REASONING_KEYS))}。请检查字段名拼写。")

    enabled = _coerce_bool(value.get("enabled"), f"{prefix}.enabled", False)
    param_raw = value.get("param", DEFAULT_REASONING_PARAM)
    if not isinstance(param_raw, str) or not _REASONING_PARAM_RE.match(param_raw.strip()):
        raise ModelRegistryError(
            f"{prefix}.param 非法：{param_raw!r}。要求：字母或下划线开头，只含字母/数字/下划线/点，"
            f"最长 40 个字符（例如 reasoning_effort、thinking.budget）。")
    param = param_raw.strip()

    tiers_raw = value.get("tiers")
    if tiers_raw is None:
        tiers_raw = []
    if not isinstance(tiers_raw, list):
        raise ModelRegistryError(
            f"{prefix}.tiers 必须是数组，每一项形如 {{\"tier\": \"low\", \"api_value\": \"low\"}}。")
    tiers: List[Dict[str, Any]] = []
    seen: List[str] = []
    for index, item in enumerate(tiers_raw):
        item_field = f"{prefix}.tiers[{index}]"
        if not isinstance(item, dict):
            raise ModelRegistryError(
                f"{item_field} 必须是对象（含 tier 与 api_value 两个字段），实际是 {type(item).__name__}。")
        unknown = sorted(set(item) - _REASONING_TIER_KEYS)
        if unknown:
            raise ModelRegistryError(
                f"不接受的字段 {item_field}.{unknown[0]}：档位只需 tier 与 api_value 两个字段。")
        tier = item.get("tier")
        if tier not in REASONING_TIERS:
            raise ModelRegistryError(
                f"{item_field}.tier 非法：{tier!r}。只能是 {'、'.join(REASONING_TIERS)} 之一。")
        if tier in seen:
            raise ModelRegistryError(
                f"{item_field}.tier 重复：{tier!r} 已经出现过一次（同一档位只能有一条 API 参数值）。")
        seen.append(tier)
        api_value = item.get("api_value")
        if api_value is not None:
            if not isinstance(api_value, str):
                raise ModelRegistryError(
                    f"{item_field}.api_value 必须是 null 或字符串，实际是 "
                    f"{type(api_value).__name__}（收到 {api_value!r}）。")
            api_value = api_value.strip()
            if len(api_value) > MAX_API_VALUE_LEN:
                raise ModelRegistryError(
                    f"{item_field}.api_value 太长（{len(api_value)} 字符 > {MAX_API_VALUE_LEN}）。")
            if not api_value:
                api_value = None   # 清空文本框 = 该档位不额外发参数，与 null 同义
        tiers.append({"tier": tier, "api_value": api_value})

    if not tiers:
        return {"enabled": False, "param": param, "active_tier": "off", "tiers": []}

    active_tier = value.get("active_tier")
    if active_tier is None:
        active_tier = "off" if "off" in seen else seen[0]
    if not isinstance(active_tier, str) or active_tier not in seen:
        raise ModelRegistryError(
            f"{prefix}.active_tier 非法：{active_tier!r} 不在 tiers 里"
            f"（可选：{'、'.join(seen)}）。")
    return {"enabled": enabled, "param": param, "active_tier": active_tier, "tiers": tiers}


def _normalize_retry(value: Any, where: str = "") -> Dict[str, Any]:
    """重试策略（提供方级）：{"policy": "default" | "off" | "aggressive"}。

    `None`（字段不存在 / 老文件）→ default，所以"老文件没有 retry 键"与"根本没有
    models.json"两种情况的重试行为都与加这个字段之前逐字节一致（规格 §8.0 / §8.1）。
    未知 policy、多余键一律点名拒绝——悄悄按 default 处理会让用户以为改生效了。
    """
    prefix = _field(where, "retry")
    if value is None:
        return _default_retry()
    if not isinstance(value, dict):
        raise ModelRegistryError(
            f"{prefix} 必须是对象（形如 {{\"policy\": \"default\"}}），实际是 "
            f"{type(value).__name__}（收到 {value!r}）。")
    unknown = sorted(set(value) - _RETRY_KEYS)
    if unknown:
        raise ModelRegistryError(
            f"不接受的字段 {prefix}.{unknown[0]}：{prefix} 只允许 "
            f"{'、'.join(sorted(_RETRY_KEYS))}。请检查字段名拼写。")
    policy = value.get("policy")
    if policy is None:
        return _default_retry()
    if not isinstance(policy, str) or policy.strip() not in RETRY_POLICIES:
        raise ModelRegistryError(
            f"{prefix}.policy 非法：{policy!r}。只能是 {'、'.join(RETRY_POLICIES)} 之一"
            f"（default = 沿用 LLM_MAX_RETRIES，off = 不重试，aggressive = 最多 3 次）。")
    return {"policy": policy.strip()}


def retry_max_retries(policy: Any) -> Optional[int]:
    """重试策略 → 该提供方的 max_retries；`None` 表示"沿用 llm_client.get_max_retries()"。

    这是 policy → 次数的**唯一**映射（`_RETRY_POLICY_MAX_RETRIES` 是它唯一的数据源），
    llm_client 建客户端时必须调它，不要在别处再写一遍 switch。
    未知/缺省一律按 default 返回 None：配置文件里出现一个没见过的策略名不该让调用链炸。
    """
    name = policy.strip() if isinstance(policy, str) else ""
    return _RETRY_POLICY_MAX_RETRIES.get(name, _RETRY_POLICY_MAX_RETRIES["default"])


def _normalize_model(value: Any, where: str) -> Dict[str, Any]:
    """单个模型对象：id / 显示名 / 上下文窗口 / 最大输出 / 输入类型 / 启用 / 推理档位。"""
    if not isinstance(value, dict):
        raise ModelRegistryError(
            f"{where} 必须是对象（模型对象，含 id 等字段），实际是 {type(value).__name__}（收到 {value!r}）。")
    unknown = sorted(set(value) - _MODEL_PAYLOAD_KEYS)
    if unknown:
        raise ModelRegistryError(
            f"不接受的字段 {_field(where, unknown[0])}：模型只允许 "
            f"{'、'.join(sorted(_MODEL_PAYLOAD_KEYS))}。请检查字段名拼写。")
    read_only = sorted(set(value) & _MODEL_VIEW_ONLY_KEYS)
    if read_only:
        raise ModelRegistryError(
            f"字段 {_field(where, read_only[0])} 是只读派生值（服务端算出来的），不能作为写入参数；"
            f"请提交模型本身的字段。")

    model_id = value.get("id")
    if not isinstance(model_id, str):
        raise ModelRegistryError(
            f"{_field(where, 'id')} 必须是字符串（模型 ID，例如 deepseek-chat），实际是 "
            f"{type(model_id).__name__}（收到 {model_id!r}）。")
    if not model_id or model_id != model_id.strip():
        raise ModelRegistryError(
            f"{_field(where, 'id')} 不能为空、也不能有前后空白：{model_id!r}。")
    if len(model_id) > MAX_MODEL_ID_LEN:
        raise ModelRegistryError(
            f"{_field(where, 'id')} 太长（{len(model_id)} 字符 > {MAX_MODEL_ID_LEN}）。")

    display_name = _coerce_text(value.get("display_name"), _field(where, "display_name"),
                                128, model_id)
    context_window = _coerce_int(value.get("context_window"), _field(where, "context_window"),
                                 1, MAX_CONTEXT_WINDOW)
    max_output_tokens = _coerce_int(value.get("max_output_tokens"),
                                    _field(where, "max_output_tokens"),
                                    1, MAX_OUTPUT_TOKENS)
    if context_window is not None and max_output_tokens is not None \
            and max_output_tokens > context_window:
        raise ModelRegistryError(
            f"{_field(where, 'max_output_tokens')}（{max_output_tokens}）不能大于 "
            f"{_field(where, 'context_window')}（{context_window}）：输出上限超过上下文窗口会永远触发截断。")

    return {
        "id": model_id,
        "display_name": display_name,
        "context_window": context_window,
        "max_output_tokens": max_output_tokens,
        "input_types": _normalize_input_types(value.get("input_types"), where),
        "enabled": _coerce_bool(value.get("enabled"), _field(where, "enabled"), True),
        "reasoning": _normalize_reasoning(value.get("reasoning"), where),
    }


def _normalize_models(value: Any) -> List[Dict[str, Any]]:
    """模型目录整体：数量上限 + ID 唯一。重复 ID 会让"按 ID 找模型"变成随机结果。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ModelRegistryError(
            f"models 必须是数组（模型对象的列表），实际是 {type(value).__name__}（收到 {value!r}）。")
    if len(value) > MAX_MODELS_PER_PROVIDER:
        raise ModelRegistryError(
            f"模型数量超限：每个提供方最多 {MAX_MODELS_PER_PROVIDER} 个（收到 {len(value)} 个）。")
    models: List[Dict[str, Any]] = []
    seen: List[str] = []
    for index, item in enumerate(value):
        model = _normalize_model(item, f"models[{index}]")
        if model["id"] in seen:
            raise ModelRegistryError(
                f"models[{index}].id 重复：{model['id']!r} 在同一个提供方下只能出现一次。")
        seen.append(model["id"])
        models.append(model)
    return models


def _normalize_provider(pid: str, value: Any, where: str = "provider") -> Dict[str, Any]:
    """提供方对象（不含密钥；密钥走 model_secrets.json）。"""
    pid = _validate_provider_id(pid)
    if not isinstance(value, dict):
        raise ModelRegistryError(
            f"{where} 必须是对象（提供方对象），实际是 {type(value).__name__}（收到 {value!r}）。")
    unknown = sorted(set(value) - _PROVIDER_PAYLOAD_KEYS)
    if unknown:
        raise ModelRegistryError(
            f"不接受的字段 {_field(where, unknown[0])}：提供方只允许 "
            f"{'、'.join(sorted(_PROVIDER_PAYLOAD_KEYS))}。请检查字段名拼写。")
    read_only = sorted(set(value) & _PROVIDER_VIEW_ONLY_KEYS)
    if read_only:
        raise ModelRegistryError(
            f"字段 {_field(where, read_only[0])} 是只读派生值（服务端算出来的），不能作为写入参数。")
    # `id` 出现在 POST /api/models/providers 的请求体里（规格 §3.2），允许带但必须与路径一致，
    # 否则用户会遇到"改了 ID 却写到了另一家"的静默错配。
    if "id" in value and value["id"] != pid:
        raise ModelRegistryError(
            f"{_field(where, 'id')}（{value['id']!r}）与请求路径里的提供方 ID（{pid!r}）不一致，"
            f"请只在一处声明。")

    return {
        "display_name": _coerce_text(value.get("display_name"), _field(where, "display_name"), 64, pid),
        "base_url": _validate_base_url(value.get("base_url"), _field(where, "base_url")),
        "protocol": _normalize_protocol(value.get("protocol"), _field(where, "protocol")),
        "key_env": _validate_key_env(value.get("key_env"), _field(where, "key_env")),
        "enabled": _coerce_bool(value.get("enabled"), _field(where, "enabled"), True),
        "models": _normalize_models(value.get("models")),
        "retry": _normalize_retry(value.get("retry"), where),
    }


def _normalize_protocol(value: Any, field: str) -> str:
    if value is None:
        return "openai"
    if not isinstance(value, str) or value.strip() not in PROTOCOLS:
        raise ModelRegistryError(
            f"{field} 非法：{value!r}。只能是 {'、'.join(PROTOCOLS)} 之一"
            f"（当前运行期只支持 {'、'.join(RUNTIME_PROTOCOLS)}）。")
    return value.strip()


# ---------------------------------------------------------------------------
# 读取（fail-soft）与写入（原子）
# ---------------------------------------------------------------------------

def _load_state(path: Path) -> Tuple[Dict[str, Any], List[str]]:
    """读 data/models.json；任何异常都退回内置提供方表并留告警，绝不抛。

    逐条容错而不是"一处坏就整份丢"：单个提供方写坏时只忽略它（点名告警），
    其余合法配置照常生效——用户改错一家不该把另外几家一起弄丢。
    """
    warns: List[str] = []
    table = _builtin_table()
    base = _builtin_state(table)
    if not path.exists():
        warns.append(
            f"尚未创建模型配置文件（{path}），当前使用内置提供方表"
            f"（{'、'.join(base['providers'])}）。在设置面板保存后才会创建该文件。")
        return base, warns
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001  坏 JSON 不能让整站起不来
        warns.append(
            f"模型配置文件读取失败（{path}）：{type(exc).__name__}: {exc}；"
            f"已退回内置提供方表。原文件保持不变，可在设置面板中修正后保存。")
        return base, warns
    if not isinstance(raw, dict):
        warns.append(
            f"模型配置文件根节点必须是对象，实际是 {type(raw).__name__}（{path}）；"
            f"已退回内置提供方表，原文件保持不变。")
        return base, warns
    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        warns.append(
            f"模型配置文件版本不受支持（schema_version={version!r}，期望 {SCHEMA_VERSION}）："
            f"{path}；已退回内置提供方表，原文件保持不变。")
        return base, warns

    state = copy.deepcopy(base)
    providers_raw = raw.get("providers")
    if providers_raw is None:
        providers_raw = {}
    if not isinstance(providers_raw, dict):
        warns.append(
            f"模型配置文件的 providers 必须是对象，实际是 {type(providers_raw).__name__}；"
            f"已忽略其中的提供方，使用内置提供方表。")
        providers_raw = {}
    for pid, entry in providers_raw.items():
        try:
            merged = copy.deepcopy(base["providers"].get(pid) or _empty_provider_entry(str(pid)))
            if isinstance(entry, dict):
                if "api_key" in entry:
                    # 有人把密钥手写进了 models.json：这张表会被整份读进页面，必须提醒挪走。
                    warns.append(
                        f"提供方 {pid} 的配置里出现了 api_key 字段：密钥不能写在 models.json 里，"
                        f"已忽略该字段（请改用设置面板填写，密钥会存进 model_secrets.json）。")
                merged.update(copy.deepcopy(entry))
            state["providers"][pid] = _normalize_provider(pid, merged, where=f"providers.{pid}")
        except ModelRegistryError as exc:
            warns.append(f"提供方 {pid} 的配置无效，已忽略该提供方：{exc}")

    active_raw = raw.get("active")
    if isinstance(active_raw, dict):
        provider = active_raw.get("provider")
        model = active_raw.get("model")
        if isinstance(provider, str) and provider in state["providers"]:
            state["active"]["provider"] = provider
            if isinstance(model, str) and model.strip():
                state["active"]["model"] = model.strip()
        elif provider:
            warns.append(
                f"模型配置文件的 active.provider（{provider!r}）不在提供方列表里，"
                f"已改用内置主提供方 {state['active']['provider']!r}。")
    return state, warns


def _load_secrets(path: Path) -> Tuple[Dict[str, str], List[str]]:
    """读 data/model_secrets.json；坏文件一律当"没有密钥"，**告警里绝不出现密钥值**。"""
    if not path.exists():
        return {}, []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {}, [f"密钥文件读取失败（{path}）：{type(exc).__name__}；"
                    f"已按「未配置密钥」处理，原文件保持不变。"]
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return {}, [f"密钥文件结构不受支持（{path}，schema_version 期望 {SCHEMA_VERSION}）；"
                    f"已按「未配置密钥」处理，原文件保持不变。"]
    secrets_raw = raw.get("secrets")
    if secrets_raw is None:
        return {}, []
    if not isinstance(secrets_raw, dict):
        return {}, [f"密钥文件的 secrets 必须是对象（提供方 ID → 密钥），实际是 "
                    f"{type(secrets_raw).__name__}；已按「未配置密钥」处理。"]
    secrets: Dict[str, str] = {}
    for pid, value in secrets_raw.items():
        if isinstance(pid, str) and isinstance(value, str) and value.strip():
            secrets[pid] = value.strip()
    return secrets, []


def _ensure_models() -> Tuple[Dict[str, Any], List[str]]:
    global _MODELS_CACHE
    with _LOCK:
        path = registry_path()
        key = _file_signature(path)
        if _MODELS_CACHE is not None and _MODELS_CACHE[0] == key:
            return copy.deepcopy(_MODELS_CACHE[1]), list(_MODELS_CACHE[2])
        state, warns = _load_state(path)
        _MODELS_CACHE = (key, copy.deepcopy(state), list(warns))
        return state, warns


def _ensure_secrets() -> Tuple[Dict[str, str], List[str]]:
    global _SECRETS_CACHE
    with _LOCK:
        path = secrets_path()
        key = _file_signature(path)
        if _SECRETS_CACHE is not None and _SECRETS_CACHE[0] == key:
            return dict(_SECRETS_CACHE[1]), list(_SECRETS_CACHE[2])
        secrets, warns = _load_secrets(path)
        _SECRETS_CACHE = (key, dict(secrets), list(warns))
        return secrets, warns


def _atomic_write_json(target: Path, data: Any, private: bool = False) -> None:
    """原子写：同目录 mkstemp → fsync → os.replace。

    先写临时文件再改名，读者永远看不到半截 JSON；顺手在改名**前**把权限收紧，
    这样密钥文件从出现的第一刻就是 0600（Windows 上 chmod 只影响只读位，失败就忽略）。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if private:
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass   # Windows 上无实际作用；权限收紧只是"尽力"，不能因此让保存失败
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _save_state(state: Dict[str, Any]) -> None:
    """写回 data/models.json：只落 schema 允许的字段（密钥永远不在这张表里）。"""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "active": {
            "provider": state["active"]["provider"],
            "model": state["active"]["model"],
        },
        "providers": state["providers"],
    }
    path = registry_path()
    _atomic_write_json(path, payload)
    global _MODELS_CACHE
    with _LOCK:
        # 写成功后作废缓存：下次读走一遍真实的加载路径，页面看到的告警必然与磁盘一致
        # （用户的"保存"就是"这份配置现在是对的"，不该继续显示上一次的告警）。
        _MODELS_CACHE = None


def _save_secrets(secrets: Dict[str, str]) -> None:
    payload = {"schema_version": SCHEMA_VERSION, "secrets": dict(secrets)}
    path = secrets_path()
    _atomic_write_json(path, payload, private=True)
    global _SECRETS_CACHE
    with _LOCK:
        _SECRETS_CACHE = None


def warnings() -> List[str]:
    """加载期告警（空列表 = 一切正常）。密钥文件的问题也在这里，但**不含密钥值**。"""
    _state, model_warns = _ensure_models()
    _secrets, secret_warns = _ensure_secrets()
    return list(model_warns) + list(secret_warns)


# ---------------------------------------------------------------------------
# 密钥：来源优先级 key_env 环境变量 → model_secrets.json → 无
# ---------------------------------------------------------------------------

def _sanitize_pid_fragment(pid: str) -> str:
    return re.sub(r"[^A-Z0-9_]", "_", str(pid).upper())


def _key_env_name(pid: str, entry: Dict[str, Any], builtin_spec: Optional[Dict[str, Any]]) -> str:
    """该提供方读哪个环境变量：注册表声明的 key_env → 内置规格 → 合成 MYLOCALAI_<PID>_API_KEY。"""
    declared = str(entry.get("key_env") or "").strip()
    if declared:
        return declared
    if builtin_spec and builtin_spec.get("key_env"):
        return str(builtin_spec["key_env"])
    return f"MYLOCALAI_{_sanitize_pid_fragment(pid)}_API_KEY"


def _model_env_name(pid: str, entry: Dict[str, Any], builtin_spec: Optional[Dict[str, Any]]) -> str:
    """该提供方读哪个"模型名"环境变量：照 key_env 改名（FOO_API_KEY → FOO_MODEL）最直观。"""
    if builtin_spec and builtin_spec.get("model_env"):
        return str(builtin_spec["model_env"])
    declared = str(entry.get("key_env") or "").strip()
    if declared.endswith("_API_KEY") and len(declared) > len("_API_KEY"):
        return declared[:-len("_API_KEY")] + "_MODEL"
    return f"MYLOCALAI_{_sanitize_pid_fragment(pid)}_MODEL"


def _base_url_env_name(pid: str, entry: Dict[str, Any],
                       builtin_spec: Optional[Dict[str, Any]]) -> str:
    if builtin_spec and builtin_spec.get("base_url_env"):
        return str(builtin_spec["base_url_env"])
    declared = str(entry.get("key_env") or "").strip()
    if declared.endswith("_API_KEY") and len(declared) > len("_API_KEY"):
        return declared[:-len("_API_KEY")] + "_BASE_URL"
    return f"MYLOCALAI_{_sanitize_pid_fragment(pid)}_BASE_URL"


def _mask(value: str) -> str:
    """掩码格式 `sk-…4f2a`（前 3 + 省略号 + 后 4）。

    不足 8 字符的短值走"…+尾部"，因为"前 3 + 后 4"会和省略号重叠、看起来像没打码；
    真实密钥都远长于 8 字符，这个分支只是兜底。
    """
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) < 8:
        return "…" + text[-4:]
    return text[:3] + "…" + text[-4:]


def _resolve_secret(pid: str) -> Tuple[str, str]:
    """返回 (密钥, 来源)。来源：env | stored | none。顺序固定，前端徽章据此显示。

    环境变量优先于本地文件：与仓库既有口径一致（.env 是部署侧的最终裁定），
    也让"临时换一把钥匙排障"不必先删本地文件。
    """
    state, _warns = _ensure_models()
    entry = state["providers"].get(pid) or {}
    builtin_spec = _builtin_table().get(pid)
    env_name = _key_env_name(pid, entry, builtin_spec)
    from_env = (os.environ.get(env_name) or "").strip() if env_name else ""
    if from_env:
        return from_env, "env"
    secrets, _swarns = _ensure_secrets()
    stored = (secrets.get(pid) or "").strip()
    if stored:
        return stored, "stored"
    return "", "none"


def key_status(pid: str) -> Dict[str, Any]:
    """该提供方的密钥状态：{"set": bool, "source": "env|stored|none", "masked": str}。"""
    value, source = _resolve_secret(pid)
    return {"set": bool(value), "source": source, "masked": _mask(value)}


def set_secret(pid: str, api_key: str) -> None:
    """写/清该提供方的本地密钥（空串 = 清除）。明文只落在 model_secrets.json，不回显。"""
    pid = _validate_provider_id(pid)
    if not isinstance(api_key, str):
        raise ModelRegistryError(
            f"api_key 必须是字符串（空串表示清除），实际是 {type(api_key).__name__}。")
    text = api_key.strip()
    if len(text) > 4096:
        raise ModelRegistryError(
            f"api_key 过长（{len(text)} 字符 > 4096），请确认粘贴的是密钥本身而不是整段配置。")
    with _LOCK:
        state, _warns = _ensure_models()
        if pid not in state["providers"]:
            raise ModelRegistryError(f"未知提供方 {pid!r}：请先创建提供方，再设置密钥。")
        secrets, _swarns = _ensure_secrets()
        updated = dict(secrets)
        if text:
            updated[pid] = text
        else:
            updated.pop(pid, None)
        _save_secrets(updated)


# ---------------------------------------------------------------------------
# 视图（GET 用；密钥只给掩码）
# ---------------------------------------------------------------------------

def _builtin_spec(pid: str) -> Optional[Dict[str, Any]]:
    return _builtin_table().get(pid)


def default_model_views(pid: str) -> List[Dict[str, Any]]:
    """内置默认模型目录（"恢复默认模型"的数据源）；自定义提供方没有默认目录 = []。

    返回的是**模型对象本身**（不带 is_active），因为它代表"出厂目录"而不是"当前目录项"。
    """
    spec = _builtin_spec(pid)
    if not spec:
        return []
    return [_default_model_entry(str(spec.get("model") or ""))]


def _is_builtin(pid: str) -> bool:
    return pid in _builtin_table()


def _model_view(model: Dict[str, Any], active_provider: str, active_model: Optional[str]) -> Dict[str, Any]:
    view = copy.deepcopy(model)
    view["is_active"] = bool(active_model is not None
                             and model["id"] == active_model)
    return view


def active() -> Dict[str, Any]:
    """当前选中（models.json 口径，不掺环境变量）：
    {"provider": str, "model": str | None}。model 为 None 时按目录/内置规格即时解析。
    """
    state, _warns = _ensure_models()
    provider = state["active"]["provider"]
    if provider not in state["providers"]:
        provider = _default_provider_id(state["providers"])
    return {"provider": provider, "model": _resolve_active_model(state, provider)}


def _default_provider_id(providers: Dict[str, Any]) -> str:
    if DEFAULT_PROVIDER in providers:
        return DEFAULT_PROVIDER
    return next(iter(providers), "")


def _resolve_active_model(state: Dict[str, Any], provider: str) -> Optional[str]:
    """当前模型：文件里存的 → 内置规格声明的 model → 目录里第一个启用的模型。"""
    stored = state["active"]["model"]
    if isinstance(stored, str) and stored.strip() and state["active"]["provider"] == provider:
        return stored.strip()
    spec = _builtin_spec(provider)
    if spec and spec.get("model"):
        return str(spec["model"])
    entry = state["providers"].get(provider) or {}
    for model in entry.get("models") or []:
        if model.get("enabled", True):
            return str(model["id"])
    return None


def provider_view(pid: str) -> Optional[Dict[str, Any]]:
    """单个提供方的视图（含掩码密钥状态、模型目录、内置默认目录）；未知返回 None。"""
    state, _warns = _ensure_models()
    entry = state["providers"].get(pid)
    if entry is None:
        return None
    active_info = active()
    status = key_status(pid)
    return {
        "id": pid,
        "display_name": entry["display_name"],
        "base_url": entry["base_url"],
        "protocol": entry["protocol"],
        "protocol_runtime_supported": entry["protocol"] in RUNTIME_PROTOCOLS,
        "key_env": entry["key_env"],
        "api_key_set": status["set"],
        "api_key_source": status["source"],
        "api_key_masked": status["masked"],
        "enabled": entry["enabled"],
        "builtin": _is_builtin(pid),
        "is_active": active_info["provider"] == pid,
        "models": [_model_view(model, pid, active_info["model"] if active_info["provider"] == pid else None)
                   for model in entry["models"]],
        "default_models": default_model_views(pid),
        # 提供方级重试策略（规格 §8.2）；entry 里一定有（_normalize_provider 补齐），
        # 这里再兜一层是防手工构造的旧内存快照漏字段
        "retry": copy.deepcopy(entry.get("retry") or _default_retry()),
    }


def _env_provider_name() -> str:
    """LLM_PROVIDER 环境变量的归一化值（strip + lower）；未设置/空白时返回空串。

    与 `runtime_resolution()` 里判"是不是 env 覆盖"的算法同一口径（抽成 helper
    就是为了两处永远一致）：`_runtime_snapshot()` 要靠它回答"当前生效提供方
    是不是 .env 压过面板选择"，两边各写一份迟早会对不上。
    """
    return (os.environ.get("LLM_PROVIDER") or "").strip().lower()


def _runtime_snapshot() -> Dict[str, Any]:
    """runtime_resolution 的安全子集（不含 api_key / params），可直接进 HTTP 响应。"""
    resolved = runtime_resolution()
    env_provider = _env_provider_name()
    return {
        "provider": resolved["provider"],
        "model": resolved["model"],
        "base_url": resolved["base_url"],
        "key_source": resolved["key_source"],
        "protocol": resolved["protocol"],
        # env 覆盖显性化（加法，不破坏既有键）：source=env 表示当前生效提供方来自
        # LLM_PROVIDER 环境变量（压过设置面板的选择），env_provider 给归一化后的值；
        # source=registry 表示来自 models.json 的当前选择（env_provider 为空串）。
        # 设置面板据此把"面板选了 A、实际生效是 B"的错配直接讲出来（WS2）。
        "source": "env" if env_provider else "registry",
        "env_provider": env_provider,
    }


def view() -> Dict[str, Any]:
    """GET /api/models 的完整载荷（规格 §3.1）：密钥只以掩码出现。"""
    state, _warns = _ensure_models()
    order = list(_builtin_table()) + [pid for pid in state["providers"]
                                      if pid not in _builtin_table()]
    return {
        "schema_version": SCHEMA_VERSION,
        "active": active(),
        "runtime": _runtime_snapshot(),
        "protocols": [
            {"id": protocol, "label": _PROTOCOL_LABELS.get(protocol, protocol),
             "runtime_supported": protocol in RUNTIME_PROTOCOLS}
            for protocol in PROTOCOLS
        ],
        "input_types": [{"id": name, "label": _INPUT_TYPE_LABELS.get(name, name)}
                        for name in INPUT_TYPES],
        "reasoning_tiers": [{"id": tier, "label": tier} for tier in REASONING_TIERS],
        "retry_policies": [{"id": policy, "label": RETRY_POLICY_LABELS.get(policy, policy)}
                           for policy in RETRY_POLICIES],
        "limits": dict(LIMITS),
        "paths": registry_paths(),
        "providers": [view for view in (provider_view(pid) for pid in order) if view],
        "warnings": warnings(),
    }


# ---------------------------------------------------------------------------
# 写入：提供方 / 模型目录
# ---------------------------------------------------------------------------

def _reconcile_active(state: Dict[str, Any]) -> None:
    """目录变更后让 active 自洽：选中的模型被删了就不要继续指着它（否则运行期静默用错模型）。"""
    act = state["active"]
    provider = state["providers"].get(act["provider"])
    if provider is None:
        state["active"] = {"provider": _default_provider_id(state["providers"]), "model": None}
        return
    if act["model"] is not None:
        known = {model["id"] for model in provider["models"]}
        if act["model"] not in known:
            act["model"] = None


def upsert_provider(pid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """新建/覆盖提供方（`api_key` 非空则顺带写密钥）。返回更新后的 provider view。"""
    pid = _validate_provider_id(pid)
    if not isinstance(payload, dict):
        raise ModelRegistryError(
            f"提供方请求体必须是对象，实际是 {type(payload).__name__}。")
    # `id` 出现在 POST /api/models/providers 的请求体里（规格 §3.2），但写入目标由路径决定：
    # 两处不一致时直接拒绝，否则会出现"改了 ID 却写进了另一家"的静默错配。
    # 注意这一句必须在下面过滤 `id` 之前执行，否则等于没检查。
    if "id" in payload and payload["id"] != pid:
        raise ModelRegistryError(
            f"provider.id（{payload['id']!r}）与请求路径里的提供方 ID（{pid!r}）不一致，"
            f"请只在一处声明。")
    with _LOCK:
        state, _warns = _ensure_models()
        existing = state["providers"].get(pid)
        if existing is None:
            if len(state["providers"]) >= MAX_PROVIDERS:
                raise ModelRegistryError(
                    f"提供方数量已达上限 {MAX_PROVIDERS}：请先删除不需要的提供方，再新增。")
            base = copy.deepcopy(_builtin_provider_entry(pid, _builtin_table()[pid])
                                 if _is_builtin(pid) else _empty_provider_entry(pid))
        else:
            base = copy.deepcopy(existing)
        fields = {key: copy.deepcopy(value) for key, value in payload.items()
                  if key not in ("api_key", "id")}
        base.update(fields)
        state["providers"][pid] = _normalize_provider(pid, base)
        _reconcile_active(state)
        _save_state(state)
        api_key = payload.get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            # 密钥不走 models.json：这里复用 set_secret，保证"掩码/来源/清除"只有一条实现。
            # 空串表示"不改"，清除请走 PUT /secret（避免"只改显示名"把已有密钥抹掉）。
            set_secret(pid, api_key)
    view_result = provider_view(pid)
    if view_result is None:   # 理论上不可能：上面刚写完；仍兜底成空视图而不是抛
        return {}
    return view_result


def delete_provider(pid: str) -> bool:
    """删除自定义提供方（内置抛 ModelRegistryError）；未知提供方返回 False。"""
    pid = _validate_provider_id(pid)
    if _is_builtin(pid):
        raise ModelRegistryError(
            f"内置提供方 {pid!r} 不能删除（来自 llm/providers.json）。"
            f"如果不想用它，可以把「启用」关掉，或换一个当前提供方。")
    with _LOCK:
        state, _warns = _ensure_models()
        if pid not in state["providers"]:
            return False
        del state["providers"][pid]
        _reconcile_active(state)
        _save_state(state)
        secrets, _swarns = _ensure_secrets()
        if pid in secrets:
            updated = dict(secrets)
            updated.pop(pid, None)
            _save_secrets(updated)
    return True


def upsert_model(pid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """新增/覆盖该提供方目录里的一个模型。返回更新后的 provider view。"""
    pid = _validate_provider_id(pid)
    if not isinstance(payload, dict):
        raise ModelRegistryError(f"模型请求体必须是对象，实际是 {type(payload).__name__}。")
    with _LOCK:
        state, _warns = _ensure_models()
        if pid not in state["providers"]:
            raise ModelRegistryError(f"未知提供方 {pid!r}：请先创建提供方，再添加模型。")
        model = _normalize_model(payload, "model")
        models = state["providers"][pid]["models"]
        for index, existing in enumerate(models):
            if existing["id"] == model["id"]:
                models[index] = model
                break
        else:
            if len(models) >= MAX_MODELS_PER_PROVIDER:
                raise ModelRegistryError(
                    f"模型数量已达上限 {MAX_MODELS_PER_PROVIDER}（提供方 {pid}）：请先删掉不需要的模型。")
            models.append(model)
        _reconcile_active(state)
        _save_state(state)
    view_result = provider_view(pid)
    return view_result if view_result is not None else {}


def delete_model(pid: str, model_id: str) -> bool:
    """删除该提供方目录里的一个模型；未知提供方/未知模型返回 False。"""
    with _LOCK:
        state, _warns = _ensure_models()
        entry = state["providers"].get(pid)
        if entry is None:
            return False
        target = model_id.strip() if isinstance(model_id, str) else model_id
        remaining = [model for model in entry["models"] if model["id"] != target]
        if len(remaining) == len(entry["models"]):
            return False
        entry["models"] = remaining
        _reconcile_active(state)
        _save_state(state)
    return True


def reorder_models(pid: str, order: List[str]) -> Dict[str, Any]:
    """按 order 重排该提供方的模型目录，返回更新后的 provider view（规格 §8.2）。

    order 必须与现有 id 集合**完全相等**（不多不少、不重）：目录顺序有语义——
    `_resolve_active_model` 取"目录里第一个启用的模型"当默认模型，所以"少传一个"
    等于偷偷删模型、"多传一个"等于偷偷建模型，这两种都不该由排序接口代劳。
    不等时点名差在哪（缺哪些 / 多哪些 / 哪个重复），让前端能直接把话显示给用户。
    """
    pid = _validate_provider_id(pid)
    if not isinstance(order, list):
        raise ModelRegistryError(
            f"order 必须是数组（模型 ID 列表，例如 [\"gpt-4o\", \"gpt-4o-mini\"]），实际是 "
            f"{type(order).__name__}（收到 {order!r}）。")
    cleaned: List[str] = []
    for index, item in enumerate(order):
        if not isinstance(item, str):
            raise ModelRegistryError(
                f"order[{index}] 必须是字符串（模型 ID），实际是 {type(item).__name__}（收到 {item!r}）。")
        name = item.strip()
        if not name:
            raise ModelRegistryError(f"order[{index}] 不能为空：请填该提供方下已有的模型 ID。")
        cleaned.append(name)
    with _LOCK:
        state, _warns = _ensure_models()
        entry = state["providers"].get(pid)
        if entry is None:
            raise ModelRegistryError(f"未知提供方 {pid!r}：请先创建提供方，再调整模型顺序。")
        models = entry["models"]
        existing = [model["id"] for model in models]
        seen: List[str] = []
        duplicates: List[str] = []
        for name in cleaned:
            if name in seen and name not in duplicates:
                duplicates.append(name)
            seen.append(name)
        if duplicates:
            raise ModelRegistryError(
                f"order 里重复的模型 ID：{'、'.join(repr(name) for name in duplicates)}。"
                f"每个模型只能出现一次（现有目录：{'、'.join(existing) or '空'}）。")
        known = set(existing)
        missing = [name for name in existing if name not in set(cleaned)]
        extra = [name for name in cleaned if name not in known]
        if missing or extra:
            details: List[str] = []
            if missing:
                details.append(f"缺少 {len(missing)} 个（{'、'.join(missing)}）")
            if extra:
                details.append(f"多出 {len(extra)} 个目录里没有的（{'、'.join(extra)}）")
            raise ModelRegistryError(
                f"order 与提供方 {pid!r} 的模型目录不一致：{'；'.join(details)}。"
                f"排序必须把现有目录里的模型 ID 原样全部列出（现有目录："
                f"{'、'.join(existing) or '空'}）。")
        by_id = {model["id"]: model for model in models}
        entry["models"] = [by_id[name] for name in cleaned]
        _reconcile_active(state)
        _save_state(state)
    view_result = provider_view(pid)
    return view_result if view_result is not None else {}


def restore_default_models(pid: str) -> List[Dict[str, Any]]:
    """把该提供方的模型目录写回内置默认目录，返回新目录（自定义提供方 = 空目录）。"""
    pid = _validate_provider_id(pid)
    with _LOCK:
        state, _warns = _ensure_models()
        if pid not in state["providers"]:
            raise ModelRegistryError(f"未知提供方 {pid!r}：没有可恢复的模型目录。")
        defaults = default_model_views(pid)
        state["providers"][pid]["models"] = copy.deepcopy(defaults)
        _reconcile_active(state)
        _save_state(state)
    return copy.deepcopy(defaults)


# ---------------------------------------------------------------------------
# 当前选择与运行期解析（llm_client 的集成点）
# ---------------------------------------------------------------------------

def set_active(provider: str, model: Optional[str] = None) -> Dict[str, Any]:
    """设为当前提供方（可选指定模型）；返回更新后的 provider view。

    运行期只执行 openai 协议，所以选中 anthropic/gemini 的提供方在这里就拒绝，
    比让 llm_client 到调用时才炸更早、更好解释。
    """
    provider = _validate_provider_id(provider)
    if model is not None:
        if not isinstance(model, str):
            raise ModelRegistryError(
                f"model 必须是字符串或省略，实际是 {type(model).__name__}（收到 {model!r}）。")
        model = model.strip()
        if not model:
            model = None
        elif len(model) > MAX_MODEL_ID_LEN or model != model.strip() or " " in model:
            raise ModelRegistryError(
                f"model 非法：{model!r}。不能为空白、不能含空格、最长 {MAX_MODEL_ID_LEN} 字符。")
    with _LOCK:
        state, _warns = _ensure_models()
        entry = state["providers"].get(provider)
        if entry is None:
            raise ModelRegistryError(f"未知提供方 {provider!r}：请先创建提供方。")
        if entry["protocol"] not in RUNTIME_PROTOCOLS:
            raise ModelRegistryError(
                f"提供方 {provider!r} 的协议是 {entry['protocol']}，本站运行期只支持 "
                f"{'、'.join(RUNTIME_PROTOCOLS)}，不能设为当前提供方。")
        state["active"] = {"provider": provider, "model": model}
        _save_state(state)
    view_result = provider_view(provider)
    return view_result if view_result is not None else {}


def _find_model(state: Dict[str, Any], provider: str, model_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not model_id:
        return None
    entry = state["providers"].get(provider) or {}
    for model in entry.get("models") or []:
        if model.get("id") == model_id:
            return model
    return None


def provider_secret(pid: str) -> Tuple[str, str]:
    """任意提供方的明文密钥与来源（env | stored | none）——**只供 llm_client 建客户端**。

    为什么单独开一个公开入口？备用链路（LLM_FALLBACKS）也要按提供方逐个建客户端，
    而 runtime_resolution() 只覆盖"当前选中的那一个"；key_status() 又只给掩码。
    用法同 runtime_resolution()：返回值禁止进任何 HTTP 响应体、日志与告警。
    """
    return _resolve_secret(pid if isinstance(pid, str) else str(pid))


def runtime_resolution() -> Dict[str, Any]:
    """运行期最终生效的提供方/模型/地址/密钥/参数（**含明文 api_key，禁止进任何响应体**）。

    优先级（与仓库既有口径一致，环境变量优先）：
        提供方  LLM_PROVIDER → models.json 的 active.provider → longcat
        模型    提供方自己的 *_MODEL 环境变量 → active.model → 内置 providers.json 的 model
    """
    state, _warns = _ensure_models()
    providers = state["providers"]
    env_provider = _env_provider_name()
    provider = env_provider or state["active"]["provider"]
    if provider not in providers:
        provider = _default_provider_id(providers) if not env_provider else provider
    entry = providers.get(provider) or {}
    spec = _builtin_spec(provider)

    base_url = str(entry.get("base_url") or "")
    if spec:
        override = (os.environ.get(_base_url_env_name(provider, entry, spec)) or "").strip()
        if override:
            base_url = override

    model_env = _model_env_name(provider, entry, spec)
    model = (os.environ.get(model_env) or "").strip() or _resolve_active_model(state, provider)
    api_key, key_source = _resolve_secret(provider)
    model_entry = _find_model(state, provider, model)

    params: Dict[str, Any] = {"context_window": None, "max_output_tokens": None, "reasoning": None}
    if model_entry:
        params["context_window"] = model_entry.get("context_window")
        params["max_output_tokens"] = model_entry.get("max_output_tokens")
        reasoning = model_entry.get("reasoning") or {}
        if reasoning.get("enabled"):
            active_tier = reasoning.get("active_tier")
            api_value = None
            for tier in reasoning.get("tiers") or []:
                if tier.get("tier") == active_tier:
                    api_value = tier.get("api_value")
                    break
            params["reasoning"] = {
                "param": reasoning.get("param") or DEFAULT_REASONING_PARAM,
                "tier": active_tier,
                "api_value": api_value,
            }

    return {
        "provider": provider,
        "model": model or "",
        "base_url": base_url,
        "api_key": api_key,
        "key_source": key_source,
        "protocol": entry.get("protocol") or "openai",
        "params": params,
    }


# ---------------------------------------------------------------------------
# 获取可用模型（唯一会真的发 HTTP 的入口，所以必须先过隐私外发授权）
# ---------------------------------------------------------------------------

_MAX_DISCOVER_BYTES = 4 * 1024 * 1024
_DEFAULT_DISCOVER_TIMEOUT = 15.0


def _clamp_timeout(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return _DEFAULT_DISCOVER_TIMEOUT
    if not math.isfinite(seconds):
        return _DEFAULT_DISCOVER_TIMEOUT
    return max(1.0, min(seconds, 60.0))


def _host_is_loopback(base_url: str) -> bool:
    """localhost / 127.0.0.1 / ::1 视为本地（与 core/privacy.py 同一口径）。"""
    try:
        host = (urlsplit(str(base_url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host in {"localhost", "127.0.0.1", "::1"}


def _models_endpoint(base_url: str) -> str:
    return str(base_url or "").rstrip("/") + "/models"


def _fetch_model_catalog(base_url: str, api_key: str,
                         timeout_s: float = _DEFAULT_DISCOVER_TIMEOUT) -> Dict[str, Any]:
    """真发一次 `GET {base_url}/models` 并解析：{"models": [...], "count": int, "error": str}。

    这是**唯一**的"发请求 + 解析 + 错误映射"实现：已保存提供方的 `available_models()` 与
    未保存提供方的 `available_models_adhoc()` 共用它，避免两条路径的错误文案慢慢走样。
    本函数故意**不做**授权与密钥判断——那两件事取决于调用方拿哪个提供方名去问隐私偏好
    （已保存的用注册表里的 pid，未保存的用表单里的草稿名），放进来反而会把两套口径搅在一起。
    网络/上游失败一律 error 非空 + models 为空，不抛；上游诚实返回空目录时 error 为空、count=0。
    """
    result: Dict[str, Any] = {"models": [], "count": 0, "error": ""}
    url = _models_endpoint(base_url)
    headers = {"Accept": "application/json", "User-Agent": "MyLocalAI-model-registry"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    timeout = _clamp_timeout(timeout_s)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 本地配置的地址
            body = response.read(_MAX_DISCOVER_BYTES)
    except urllib.error.HTTPError as exc:
        result["error"] = (f"上游返回 HTTP {exc.code}：{url}。"
                           f"常见原因：密钥无效/余额不足/该地址不是 OpenAI 兼容接口。")
        return result
    except urllib.error.URLError as exc:
        result["error"] = f"网络请求失败：{url}（{exc.reason}）。请检查地址、网络与代理设置。"
        return result
    except Exception as exc:  # noqa: BLE001  超时/连接被重置/证书问题等，一律如实报错不抛
        result["error"] = f"请求 {url} 时出错：{type(exc).__name__}: {exc}"
        return result

    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (UnicodeDecodeError, ValueError):
        result["error"] = f"上游返回的不是合法 JSON：{url}。请确认该地址是 OpenAI 兼容的 /models 接口。"
        return result

    raw_models = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(raw_models, list):
        result["error"] = (f"上游返回的结构无法识别（期望 {{\"data\": [{{\"id\": ...}}]}}）：{url}。")
        return result
    models: List[Dict[str, str]] = []
    seen: List[str] = []
    for item in raw_models:
        model_id = item.get("id") if isinstance(item, dict) else item
        if not isinstance(model_id, str) or not model_id.strip() or model_id.strip() in seen:
            continue
        model_id = model_id.strip()
        seen.append(model_id)
        owned_by = item.get("owned_by") if isinstance(item, dict) else ""
        models.append({"id": model_id,
                       "owned_by": owned_by.strip() if isinstance(owned_by, str) else ""})
    # 诚实报空：上游就是没给模型时返回空目录，不拿内置目录假装"获取到了"
    result["models"] = models
    result["count"] = len(models)
    return result


def available_models(pid: str, timeout_s: float = 15.0) -> Dict[str, Any]:
    """真发 `GET {base_url}/models`，返回 §3.3 的发现结果（**不写盘**，由用户勾选后再落盘）。

    顺序很重要：先解析提供方 → **先过隐私外发授权**（未授权抛 PrivacyAuthorizationError，
    一个字节都不外发）→ 再看有没有密钥 → 最后才发请求。
    网络/上游失败一律 error 非空 + models 为空，不抛；上游诚实返回空目录时 error 为空、count=0。
    """
    result: Dict[str, Any] = {"provider": pid if isinstance(pid, str) else str(pid),
                              "base_url": "", "models": [], "count": 0, "error": ""}
    state, _warns = _ensure_models()
    entry = state["providers"].get(pid) if isinstance(pid, str) else None
    if entry is None:
        result["error"] = f"未知提供方 {pid!r}：请先在设置面板里创建提供方。"
        return result
    base_url = str(entry.get("base_url") or "")
    result["base_url"] = base_url

    # fail-closed：授权检查放在**任何**网络动作之前，且异常不吞（由 web 层转 403）
    from mylocalai.core.privacy import load_preferences
    load_preferences().require_provider(pid, base_url)

    api_key, _source = _resolve_secret(pid)
    if not api_key and not _host_is_loopback(base_url):
        # 没有密钥还硬发一次请求只会拿到 401，还多一次外发；本地服务（ollama 之类）通常不要密钥
        result["error"] = (f"提供方 {pid!r} 还没有配置 API 密钥：请先在设置面板里填写密钥"
                           f"（或设置环境变量 {_key_env_name(pid, entry, _builtin_spec(pid))}）。")
        return result

    fetched = _fetch_model_catalog(base_url, api_key, timeout_s)
    result["models"] = fetched["models"]
    result["count"] = fetched["count"]
    result["error"] = fetched["error"]
    return result


def available_models_adhoc(provider: str, base_url: str, api_key: str = "",
                           timeout_s: float = 15.0) -> Dict[str, Any]:
    """未保存提供方的「获取可用模型」：用表单里填的地址/密钥真发一次 GET，**不写盘**（§8.2）。

    为什么需要它：设置面板的"新建提供方"弹层里，用户还没点保存就想先取一次模型目录；
    走已保存那条路会先撞上"未知提供方"，于是"先建再取"卡死在第一步。

    顺序（与 available_models 同一套纪律，但授权键来自调用方）：
        1. 校验 base_url（口径同 `_validate_base_url`：http/https + 必须有 host）——
           非法地址连"要不要外发"都谈不上，直接抛 ModelRegistryError 点名；
        2. `require_provider(provider, base_url)`：用**表单里的提供方名**当键，未授权 →
           PrivacyAuthorizationError，一个字节都不外发；
        3. 没有密钥且目标不是 loopback → error 返回（不外发：没有密钥硬发只会拿到 401）；
        4. 发请求 + 解析 + 错误映射（与 available_models 共用 `_fetch_model_catalog`）。
    api_key 留空是允许的（本地服务如 ollama 本就不需要密钥）。
    """
    name = provider if isinstance(provider, str) else str(provider)
    result: Dict[str, Any] = {"provider": name, "base_url": "", "models": [], "count": 0, "error": ""}
    validated = _validate_base_url(base_url, "base_url")
    result["base_url"] = validated

    # fail-closed：授权检查放在**任何**网络动作之前；键是调用方给的提供方名（草稿态还没有注册表条目）
    from mylocalai.core.privacy import load_preferences
    load_preferences().require_provider(name, validated)

    if api_key is None:
        key = ""
    elif isinstance(api_key, str):
        key = api_key.strip()
    else:
        raise ModelRegistryError(
            f"api_key 必须是字符串（留空表示不需要密钥），实际是 {type(api_key).__name__}"
            f"（收到 {api_key!r}）。")
    if not key and not _host_is_loopback(validated):
        result["error"] = (f"提供方 {name!r} 还没有填写 API 密钥：请先在表单里填写密钥再点"
                           f"「获取可用模型」（本地服务如 ollama 可以留空）。")
        return result

    fetched = _fetch_model_catalog(validated, key, timeout_s)
    result["models"] = fetched["models"]
    result["count"] = fetched["count"]
    result["error"] = fetched["error"]
    return result
