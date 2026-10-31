# -*- coding: utf-8 -*-
"""助手核心（M2）：路由 → 工具循环 → 叙述 → 落库。

模块清单见工作指导 §4.2；依赖方向：assistant 只准调 dossier/classic/rumor/attention/
retrieval 的公开服务接口，不准被它们反向 import。
"""
