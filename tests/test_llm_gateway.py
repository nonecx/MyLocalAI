# -*- coding: utf-8 -*-
"""LLM 网关统一（M1-2）后的缓存键稳定性测试。

2026-10-30 把 rumor_agent/llm.py 收敛进 mylocalai/llm（唯一构造路径）时，
要求**缓存键逐字节不变**——键变了等于全量缓存 miss。键的推导函数
（llm_client._cache_key）在搬移中未改一行，本模块用固定环境下的金样本哈希
把这一点锁住：未来任何人改动键推导（或无意中改变其字节输入）都会让这里变红。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 金样本环境：2026-10-30 M1-2 包化当天，用当时实现算出的哈希（provider/model 是
# 键的组成项，必须钉死，否则换 .env 会误报）。
_ENV = {"get_provider": "longcat",
        "get_base_url": "https://api.longcat.chat/openai/v1",
        "get_model": "LongCat-2.5-Preview"}
GOLDEN_NO_NS = "7ffbc2d49746b722542c0e99226474cb545db81eb51def5775031d07a4145152"
GOLDEN_ATTN = "0c72d1f9bc4b324fb6101e04f650474f834fad0260977207e0dc68bffb81e4bc"


class TestCacheKeyStability(unittest.TestCase):
    def _key(self, *args, **kw):
        from mylocalai.llm import llm_client
        patches = [mock.patch.object(llm_client, name, return_value=value, create=True)
                   for name, value in _ENV.items()]
        for p in patches:
            p.start()
        try:
            return llm_client._cache_key(*args, **kw)
        finally:
            for p in patches:
                p.stop()

    def test_golden_key_without_namespace(self):
        self.assertEqual(
            self._key("系统提示", "用户文本", 0.0, 700, False), GOLDEN_NO_NS)

    def test_golden_key_with_namespace(self):
        self.assertEqual(
            self._key("系统提示", "用户文本", 0.0, 700, False, namespace="attn_deep_read"),
            GOLDEN_ATTN)

    def test_namespace_changes_key(self):
        self.assertNotEqual(
            self._key("s", "u", 0.0, 700, False),
            self._key("s", "u", 0.0, 700, False, namespace="assistant_router"))

    def test_distinct_inputs_distinct_keys(self):
        self.assertNotEqual(
            self._key("s", "u1", 0.0, 700, False),
            self._key("s", "u2", 0.0, 700, False))

    def test_adapter_uses_gateway(self):
        """第二构造路径（LangChain 适配）必须从同一网关取提供方/超时口径。"""
        src = (ROOT / "src" / "mylocalai" / "llm" / "adapter.py").read_text(encoding="utf-8")
        self.assertIn("mylocalai.llm", src.replace("mylocalai.llm import", "mylocalai.llm"))
        self.assertIn("llm_client.first_available_provider", src)
        self.assertIn("llm_cache.attempt_timeout", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
