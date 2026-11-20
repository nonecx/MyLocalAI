# -*- coding: utf-8 -*-
"""模型管理 HTTP 接口层（设置面板「模型」Tab 的后端）。

契约唯一事实来源是 docs/模型管理设计规格.md §3；本文件只做三件事：
    请求体原样收下 → 调 mylocalai.llm.model_registry 的对应函数 → 把异常翻成 HTTP 状态码。

为什么请求体一律是 `Dict[str, Any]` 而不是 pydantic 固定字段模型：
model_registry 的「未知字段点名拒绝」（规格 §2.2）靠请求体里**原样出现**的字段触发。
pydantic 模型会悄悄剥掉没声明的字段——拼错一个字段名不会报错，而是被静默忽略，
"保存一次就少一个参数"这种事故在页面上看不出来。用 dict 收下，校验与点名
交给注册表那一处实现。

错误口径（规格 §3.2，前端会把 detail 原文显示出来，所以中文必须说清"哪一步、为什么"）：
    ModelRegistryError（字段/取值非法） → 400，detail = 后端原文
    未知提供方 / 未知模型                → 404
    删内置提供方、activate 非 openai 协议 → 400（注册表自带中文理由）
    /discover 未授权外发                 → 403（PrivacyAuthorizationError，一个字节都不外发）
    /discover 上游/网络失败              → 502，detail = 后端原文
    写盘 OSError                         → 500，detail 带目标文件路径
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Path

from mylocalai.core.privacy import PrivacyAuthorizationError
from mylocalai.llm import llm_client
from mylocalai.llm import model_registry

router = APIRouter(prefix="/api/models", tags=["models"])


# ---------------------------------------------------------------------------
# 小工具：请求体、存在性、异常翻译、写后刷新
# ---------------------------------------------------------------------------

def _body_dict(body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """请求体 → dict；缺省（None）当空对象。

    DELETE / 恢复默认这类操作前端不发体；activate / discover 发的体可以整体省略，
    所以这里不能假设 body 一定在。
    """
    if body is None:
        return {}
    if not isinstance(body, dict):
        # FastAPI 对 Dict[str, Any] 正常只会给 dict 或 None，这句是别把意外形状
        # （例如顶层数组）当成"合法但空"的请求放过去
        raise HTTPException(
            status_code=400,
            detail=f"请求体必须是 JSON 对象，实际是 {type(body).__name__}。")
    return body


def _provider_or_404(pid: str) -> Dict[str, Any]:
    """提供方视图；未知 → 404。每个写端点动手前都先过这一道。

    为什么存在性检查收在 web 层：注册表对"未知提供方"的口径并不统一——
    upsert_model / set_secret / restore_default_models 抛 ModelRegistryError（那是 400
    的语义），delete_* 返回 False。HTTP 契约要求未知一律 404，所以在调用前先确认，
    顺带把"这个提供方现在长什么样"拿回来给 /discover 拼错误文案用。
    """
    view = model_registry.provider_view(pid)
    if view is None:
        raise HTTPException(
            status_code=404,
            detail=f"未知提供方 {pid!r}：请先在设置面板里创建该提供方。")
    return view


def _save(action, target: str):
    """执行一次会写盘的注册表调用，把它的异常翻成 HTTP 状态码。

    为什么单独包一层：九个写端点要的是同一套翻译（ModelRegistryError → 400 且保留
    中文原文、OSError → 500 且 detail 带路径），逐字抄九遍只会让其中一处慢慢走样。
    target 用字符串而不是 Path：delete_provider 可能同时写 models.json 与密钥文件，
    写哪张表失败就把那张表的路径说清楚，比笼统一个"配置目录"有用。
    """
    try:
        return action()
    except model_registry.ModelRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"写入 {target} 失败（{type(exc).__name__}: {exc}）："
                   f"请检查该目录的写入权限与磁盘空间后重试。") from exc


def _refresh_after_write() -> None:
    """写成功后清两层缓存（规格 §3）。

    注册表的写函数自己会作废自己的快照，但 llm_client 那边的提供方表看不见：
    不清的话"刚在面板里保存的提供方/模型"要等到下次重启才出现在分析链路里。
    """
    llm_client.reset_cache()
    model_registry.reset_cache()


def _discover_timeout(payload: Dict[str, Any]) -> Optional[float]:
    """校验可选的 timeout_s（秒）；缺省返回 None，让注册表用它自己的默认值。

    范围钳制（1..60）交给 model_registry._clamp_timeout：那是唯一实现，
    这里只挡"根本不是数字"的输入，避免两处各写一份规则后慢慢不一致。
    """
    raw = payload.get("timeout_s")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise HTTPException(
            status_code=400,
            detail=f"timeout_s 必须是数字（秒，例如 15），实际是 {raw!r}。")
    return float(raw)


# ---------------------------------------------------------------------------
# 读：GET /api/models（设置面板的一次性全量载荷，规格 §3.1）
# ---------------------------------------------------------------------------

@router.get("")
def get_models() -> Dict[str, Any]:
    """全量视图：内置+自定义提供方、模型目录、当前选择、协议/输入类型/档位清单、告警。

    注意装饰器写 `""` 而不是 `"/"`：带 prefix 时 `"/"` 会注册成 /api/models/，
    与前端 SETTINGS_API.models（/api/models，无尾斜杠）是两条不同的路由，
    前后端自洽测试（tests/test_models_ui.py）逐字比对路径，必须对齐。
    密钥只以 api_key_set / api_key_source / api_key_masked 出现，绝不明文。
    """
    return model_registry.view()


# ---------------------------------------------------------------------------
# 写：提供方（规格 §3.2 前四行）
# ---------------------------------------------------------------------------

@router.post("/providers")
def create_provider(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """新建/覆盖提供方；body 里的 api_key 非空则顺带写密钥。

    id 从 body 取（路径里没有提供方 ID）：缺省或形状不对时交给注册表点名拒绝，
    400 的中文 detail 比在这里先判一次更接近"错在哪"。
    """
    payload = _body_dict(body)
    result = _save(lambda: model_registry.upsert_provider(payload.get("id"), payload),
                   f"模型配置 {model_registry.registry_path()}")
    _refresh_after_write()
    return result


@router.put("/providers/{pid}")
def update_provider(pid: str = Path(..., min_length=1),
                    body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """改提供方（不含密钥）；未知提供方 → 404。

    规格只允许 PUT 改**已存在**的提供方：新建走 POST，免得"以为在改、其实新建了"。
    密钥故意不在这条路径上——只改个显示名不该把已保存的密钥抹掉，清密钥走
    PUT .../secret（空串）。
    """
    payload = _body_dict(body)
    _provider_or_404(pid)
    result = _save(lambda: model_registry.upsert_provider(pid, payload),
                   f"模型配置 {model_registry.registry_path()}")
    _refresh_after_write()
    return result


@router.delete("/providers/{pid}")
def delete_provider(pid: str = Path(..., min_length=1)) -> Dict[str, Any]:
    """删自定义提供方；内置 → 400（不可删，可关启用或换当前提供方），未知 → 404。"""
    deleted = _save(lambda: model_registry.delete_provider(pid),
                    f"模型配置 {model_registry.registry_path()} / "
                    f"密钥文件 {model_registry.secrets_path()}")
    if not deleted:
        raise HTTPException(status_code=404,
                            detail=f"未知提供方 {pid!r}：没有可删除的对象。")
    _refresh_after_write()
    return {"deleted": True, "id": pid}


@router.put("/providers/{pid}/secret")
def put_provider_secret(pid: str = Path(..., min_length=1),
                        body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """写/清密钥（空串 = 清除）；返回更新后的 provider view（只有掩码）。

    缺 api_key 字段时回 400 而不是当成清除：清密钥是 destructive 操作，
    "忘带字段"和"想清空"必须用不同的请求表达，不能靠同一个形状蒙混过去。
    """
    payload = _body_dict(body)
    _provider_or_404(pid)
    if "api_key" not in payload:
        raise HTTPException(
            status_code=400,
            detail="请求体缺少 api_key 字段：要清除已保存的密钥请传空字符串 \"\"。")
    _save(lambda: model_registry.set_secret(pid, payload["api_key"]),
          f"密钥文件 {model_registry.secrets_path()}")
    _refresh_after_write()
    return _provider_or_404(pid)


@router.post("/providers/{pid}/activate")
def activate_provider(pid: str = Path(..., min_length=1),
                      body: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """设为当前提供方（可带 model 指定模型，缺省用该提供方的默认模型）。

    协议非 openai → 400：本站运行期只执行 openai 兼容协议，选中 anthropic/gemini
    会让分析链路在调用时才炸，早拒绝早解释（理由由注册表给中文原文）。
    """
    payload = _body_dict(body)
    _provider_or_404(pid)
    result = _save(lambda: model_registry.set_active(pid, payload.get("model")),
                   f"模型配置 {model_registry.registry_path()}")
    _refresh_after_write()
    return result


# ---------------------------------------------------------------------------
# 获取可用模型（唯一真的发 HTTP 出去的端点，规格 §3.3）
# ---------------------------------------------------------------------------

@router.post("/providers/{pid}/discover")
def discover_provider(pid: str = Path(..., min_length=1),
                      body: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """向 {base_url}/models 发一次真 GET，返回发现结果；**不写盘**。

    安全顺序在注册表里：先过隐私外发授权（未授权抛 PrivacyAuthorizationError，
    一个字节都不外发）→ 才看密钥 → 才发请求。这里负责把 PrivacyAuthorizationError
    翻成 403——它必须是 except 的第一条，因为它是 PermissionError（OSError 子类），
    排在 OSError 后面会被当成"写盘失败"回 500，把"未授权"说成"磁盘满了"。

    失败（上游 4xx/5xx、网络不通、没配密钥）不写盘也不假装成功：error 非空 →
    502，detail 用后端原文；上游诚实返回空目录时 error 为空、count=0，照样 200。
    由用户在面板上勾选后另行 POST .../models 落盘（"获取"与"保存"分开）。
    """
    payload = _body_dict(body)
    provider = _provider_or_404(pid)
    timeout_s = _discover_timeout(payload)
    try:
        if timeout_s is None:
            result = model_registry.available_models(pid)
        else:
            result = model_registry.available_models(pid, timeout_s=timeout_s)
    except PrivacyAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=(
            f"未获授权向提供方 {pid!r} 外发「获取可用模型」请求"
            f"（目标 {provider['base_url']}）：请先在「隐私与授权」里开启外部处理，"
            f"并勾选该提供方。后端原文：{exc}")) from exc
    except OSError as exc:
        # 注册表内部已把网络类异常翻成 error 字段，这里只是别让意外形状漏成裸 500
        raise HTTPException(
            status_code=500,
            detail=f"获取可用模型时出错（{type(exc).__name__}: {exc}）。") from exc
    if result.get("error"):
        raise HTTPException(status_code=502, detail=result["error"])
    return result


# ---------------------------------------------------------------------------
# 写：单个提供方的模型目录（规格 §3.2 后三行）
# ---------------------------------------------------------------------------

@router.post("/providers/{pid}/models")
def upsert_provider_model(pid: str = Path(..., min_length=1),
                          body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """新增/覆盖一个模型（body = 完整模型对象），返回更新后的 provider view。

    只收模型对象本身：前端把勾选到的模型逐条 POST 进来（"导入所选模型"），
    目录整体保存也走这条（逐条 upsert），不提供"整份替换目录"的入口——
    那会让并发两次保存互相覆盖。
    """
    payload = _body_dict(body)
    _provider_or_404(pid)
    result = _save(lambda: model_registry.upsert_model(pid, payload),
                   f"模型配置 {model_registry.registry_path()}")
    _refresh_after_write()
    return result


@router.delete("/providers/{pid}/models/{model_id:path}")
def delete_provider_model(pid: str = Path(..., min_length=1),
                          model_id: str = Path(..., min_length=1)) -> Dict[str, Any]:
    """删模型；未知提供方 / 未知模型 → 404（注册表用 False 表达，这里翻成 404）。

    model_id 用 `:path` 转换器而不是默认的 str：前端对它做了 encodeURIComponent，
    含 `/` 的模型 ID（`%2F`）解回来后会跨路径段，str 转换器只匹配单段，
    会把请求 404 到别的路由形状上。`:path` 通配整段尾部，两种形态都落在本端点。
    """
    deleted = _save(lambda: model_registry.delete_model(pid, model_id),
                    f"模型配置 {model_registry.registry_path()}")
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"提供方 {pid!r} 下没有模型 {model_id!r}（或该提供方本身不存在）。")
    _refresh_after_write()
    return {"deleted": True, "id": model_id}


@router.post("/providers/{pid}/models/restore")
def restore_provider_models(pid: str = Path(..., min_length=1)) -> Dict[str, Any]:
    """把该提供方的模型目录写回内置默认（自定义提供方 = 空目录），返回更新后的 view。

    不声明请求体：前端发的是无体 POST；真带了体也忽略——这个操作没有可调参数，
    多一个入口就多一处要想清楚"体和路径谁说话算数"。
    """
    _provider_or_404(pid)
    _save(lambda: model_registry.restore_default_models(pid),
          f"模型配置 {model_registry.registry_path()}")
    _refresh_after_write()
    return _provider_or_404(pid)
