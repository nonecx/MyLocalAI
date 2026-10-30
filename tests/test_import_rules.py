# -*- coding: utf-8 -*-
"""包依赖方向约束（AST 静态检查，2026-10-30 M1-2 任务 4）。

依赖方向只有一条（工作指导 §4.1）：
    core ← llm ← retrieval ← {classic, rumor, attention} ← {dossier, assistant} ← web/cli/mcp
classic / rumor / attention 三者互不 import。config 与 util 是叶子（不 import 任何内部包）。

检查所有 `import` / `from ... import`（含函数体内的延迟导入）。豁免清单见 _EXEMPT。
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "mylocalai"

# 包 → 禁止出现的内部依赖（mylocalai.<name>）
FORBIDDEN = {
    "mylocalai.core": {"classic", "rumor", "attention", "retrieval", "web", "llm"},
    "mylocalai.config": {"classic", "rumor", "attention", "retrieval", "web", "llm", "core", "util"},
    "mylocalai.util": {"classic", "rumor", "attention", "retrieval", "web", "llm", "core", "config"},
    "mylocalai.llm": {"classic", "rumor", "attention", "web"},
    "mylocalai.retrieval": {"classic", "rumor", "attention", "web"},
    "mylocalai.classic": {"rumor", "attention"},
    "mylocalai.rumor": {"classic", "attention"},
    "mylocalai.attention": {"classic", "rumor"},
}

# 入口适配层豁免： rumor 包内的 cli/mcp/__main__ 是命令行/MCP 入口，
# 允许装配 classic 组件（依赖方向：入口层 → {classic, rumor}）。
_EXEMPT = {
    "mylocalai.rumor": {"cli.py", "mcp.py", "__main__.py"},
}


def _imports_of(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                yield node.module


def _package_of(py_file: Path) -> str:
    rel = py_file.parent.relative_to(SRC).as_posix()
    return "mylocalai" if rel == "." else "mylocalai." + rel.replace("/", ".")


def _violations():
    found = []
    for py in SRC.rglob("*.py"):
        if py.name == "__init__.py":
            continue
        pkg = _package_of(py)
        banned = FORBIDDEN.get(pkg)
        if not banned:
            continue
        if py.name in _EXEMPT.get(pkg, set()):
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for mod in _imports_of(tree):
            for bad in banned:
                if mod == f"mylocalai.{bad}" or mod.startswith(f"mylocalai.{bad}."):
                    found.append(f"{py.relative_to(ROOT)} -> {mod}")
    return found


class TestImportDirection(unittest.TestCase):
    def test_no_forbidden_imports(self):
        viols = _violations()
        self.assertEqual(viols, [], "依赖方向违规：\n" + "\n".join(viols))

    def test_classic_rumor_attention_isolated(self):
        """三引擎互不 import：逐包单独断言（失败信息更可读）。"""
        for pkg in ("mylocalai.classic", "mylocalai.rumor", "mylocalai.attention"):
            banned = FORBIDDEN[pkg]
            exempt = _EXEMPT.get(pkg, set())
            for py in (SRC / pkg.split(".", 1)[1]).glob("*.py"):
                if py.name in exempt or py.name == "__init__.py":
                    continue
                for mod in _imports_of(ast.parse(py.read_text(encoding="utf-8"))):
                    for bad in banned:
                        self.assertFalse(
                            mod == f"mylocalai.{bad}" or mod.startswith(f"mylocalai.{bad}."),
                            f"{py.relative_to(ROOT)} import 了 {mod}")

    def test_leaves_import_nothing_internal(self):
        """config / util 是叶子：不 import 任何 mylocalai 内部包（同包绝对导入除外）。"""
        for leaf in ("mylocalai.config", "mylocalai.util"):
            for py in (SRC / leaf.split(".", 1)[1]).rglob("*.py"):
                for mod in _imports_of(ast.parse(py.read_text(encoding="utf-8"))):
                    if mod == leaf or mod.startswith(leaf + "."):
                        continue
                    self.assertFalse(mod.startswith("mylocalai."),
                                     f"{py.relative_to(ROOT)} 叶子包 import 了 {mod}")

    def test_state_does_not_import_classic(self):
        """装配倒转锁定：rumor/state.py 不再 import classic（组件由入口注入）。"""
        src = (SRC / "rumor" / "state.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        mods = list(_imports_of(tree))
        self.assertFalse([m for m in mods if m.startswith("mylocalai.classic")],
                         f"state.py 出现 classic 导入: {mods}")

    def test_core_judge_does_not_import_attention(self):
        """attention 挂载上移入口层：classic/judge.py 不 import attention。"""
        for py in (SRC / "classic").glob("*.py"):
            for mod in _imports_of(ast.parse(py.read_text(encoding="utf-8"))):
                self.assertFalse(mod.startswith("mylocalai.attention"),
                                 f"{py.relative_to(ROOT)} import 了 {mod}")

    def test_exempt_files_are_entries_only(self):
        """豁免文件必须是真实入口（防豁免清单被滥用扩散）。"""
        expected = {"cli.py", "mcp.py", "__main__.py"}
        self.assertTrue(set(_EXEMPT["mylocalai.rumor"]) <= expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
