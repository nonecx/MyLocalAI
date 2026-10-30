# -*- coding: utf-8 -*-
"""大模型 API 连通性冒烟测试（配置统一读自 shared_core.llm_client / .env）。

先做配置自检（密钥与服务商是否匹配），再真正发一次请求。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared_core.llm_client import create_client, get_model, validate_config

ok, detail = validate_config()
print(f"配置自检: {detail}")
client = create_client()
if client is None:
    print("FAIL: API 客户端不可用")
    print(f"      原因：{detail}")
    print("      检查 .env 的 LLM_PROVIDER 与对应提供方的密钥（如 LONGCAT_API_KEY），以及 openai 是否已安装")
    sys.exit(1)

print(f"配置: base_url OK, model={get_model()}")
try:
    resp = client.chat.completions.create(
        model=get_model(),
        messages=[{"role": "user", "content": "请只回复两个字：正常"}],
        max_tokens=16,
        temperature=0,
    )
    content = resp.choices[0].message.content if resp.choices else ""
    print(f"CHAT OK, 返回: {content!r}")
    # 2026-10-30 P0：LongCat 有冒烟返回 None/空串的前科，content 为空视为失败（退出码非 0）。
    if not (isinstance(content, str) and content.strip()):
        print("FAIL: 返回 content 为空或 None，不能算冒烟通过")
        sys.exit(1)
except Exception as e:  # noqa: BLE001
    print(f"CHAT FAIL: {type(e).__name__}: {e}")
