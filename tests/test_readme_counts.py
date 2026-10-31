# -*- coding: utf-8 -*-
"""README 数字守护测试（M4，工作指导 §6 M4 / §10.6："M4 后由用例计数测试自动守护"）。

治"文档数字漂移"（§3.3 结构问题 5 的教训）：README 里所有可以被机器数出来的断言，
都由本模块对着实数校验——
  1. "离线套件共 N 个用例" 的 N = unittest discover 实际加载的用例数；
  2. mock 闸门 "28 条：26 条既有 + 2 条 M2 探针" 与检验脚本静态解析一致：
     既有闸门数 = mlai_verify.mode_digest 里**非 else 回落分支**的 gates.append(_gate(...))
     调用数（每个模式产出结果时该模式块贡献的闸门；else 分支是"未运行"占位，运行时不
     同时存在）；探针数 = mock_run._print_checklist 汇总清单里追加的 §7.2 探针行数；
  3. README 引用的 tests/*.py 模块文件全部真实存在（防改名/移动后文档失真）；
  4. README 文档清单里的 docs/*.md 全部真实存在（含新增评估文档，防链接漂移）。

全部离线（discover 只加载不执行；AST 静态解析不跑检验器）。

运行（项目根目录下）：
    .venv/Scripts/python.exe -X utf8 -m unittest tests.test_readme_counts -v
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
TESTS_DIR = ROOT / "tests"
VERIFY_DIR = ROOT / ".tmp" / "verify-2026-10-22"


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def _discovered_case_count() -> int:
    """unittest discover 全量加载（不执行）后的用例总数——与 README 测试段命令同参：
    `python -m unittest discover -s tests -p "test_*.py"`。"""
    suite = unittest.TestLoader().discover(str(TESTS_DIR), pattern="test_*.py")
    return suite.countTestCases()


def _mock_gate_components() -> tuple:
    """静态解析 mock 闸门构成，返回 (既有闸门数, §7.2 探针行数)。

    口径（与 mock_run.py 实际输出的检验清单一致）：
      - 既有闸门数：mode_digest() 中不在任何 if 的 else 回落分支里的
        gates.append(_gate(...)) 调用数——即"所有模式都产出结果"时的闸门总数；
        else 分支的同名/占位闸门与其互斥（运行时只出现其一），故不计。
      - 探针行数：_print_checklist() 汇总清单在既有闸门之后追加的 §7.2 探针
        （for key, label in (("chat_turn_budget", ...), ("sse_cancel", ...))）的条目数。
    """
    digest_src = (VERIFY_DIR / "mlai_verify.py").read_text(encoding="utf-8")
    tree = ast.parse(digest_src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "mode_digest")

    def is_gate_append(node: ast.AST) -> bool:
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "gates"
                and bool(node.args) and isinstance(node.args[0], ast.Call)
                and isinstance(node.args[0].func, ast.Name)
                and node.args[0].func.id == "_gate")

    all_appends = [n for n in ast.walk(fn) if is_gate_append(n)]
    fallback_ids = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            for stmt in node.orelse:          # "模式未产出结果"的占位闸门
                for sub in ast.walk(stmt):
                    if is_gate_append(sub):
                        fallback_ids.add(id(sub))
    gates = len([n for n in all_appends if id(n) not in fallback_ids])

    runner_src = (VERIFY_DIR / "mock_run.py").read_text(encoding="utf-8")
    tree2 = ast.parse(runner_src)
    chk = next(n for n in ast.walk(tree2)
               if isinstance(n, ast.FunctionDef) and n.name == "_print_checklist")
    probes = 0
    for node in ast.walk(chk):
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple):
            keys = [e.elts[0].value for e in node.iter.elts
                    if isinstance(e, ast.Tuple) and e.elts
                    and isinstance(e.elts[0], ast.Constant)]
            if keys and all(isinstance(k, str) for k in keys):
                probes += len(keys)
    return gates, probes


class TestReadmeCounts(unittest.TestCase):
    def test_total_case_count_matches_readme(self):
        """README"离线套件共 N 个用例"的 N = discover 实际加载数（改测试必须回写 README）。"""
        text = _readme_text()
        match = re.search(r"离线套件共\s*(\d+)\s*个用例", text)
        self.assertIsNotNone(match, "README 缺少'离线套件共 N 个用例'的守护锚点")
        claimed = int(match.group(1))
        actual = _discovered_case_count()
        self.assertEqual(
            claimed, actual,
            f"README 用例数漂移：README 写 {claimed}，discover 实际 {actual}。"
            f"增删用例后请同步回写 README（M4 起本测试自动守护该数字）。")

    def test_mock_gate_count_matches_readme(self):
        """README mock 闸门 "28 条：26 条既有" 与检验脚本静态解析一致。"""
        text = _readme_text()
        match = re.search(r"不耗配额，\s*(\d+)\s*条：\s*(\d+)\s*条既有", text)
        self.assertIsNotNone(match, "README 缺少 mock 闸门 'N 条：M 条既有' 的守护锚点")
        claimed_total, claimed_existing = int(match.group(1)), int(match.group(2))
        gates, probes = _mock_gate_components()
        self.assertEqual(claimed_existing, gates,
                         f"README '既有闸门'数漂移：README {claimed_existing}，"
                         f"mlai_verify.mode_digest 非回落分支实数 {gates}")
        self.assertEqual(claimed_total, gates + probes,
                         f"README mock 闸门总数漂移：README {claimed_total}，"
                         f"实数 = 既有 {gates} + 探针 {probes}")

    def test_readme_test_module_references_exist(self):
        """README 引用的 tests/*.py 全部真实存在（两种写法：tests.test_x 与 tests/test_x.py）。"""
        text = _readme_text()
        refs = set(re.findall(r"tests/[A-Za-z0-9_]+\.py", text))
        refs |= {f"tests/test_{m}.py" for m in re.findall(r"tests\.test_([A-Za-z0-9_]+)", text)}
        self.assertTrue(refs, "没有从 README 解析到 tests/ 引用（守护锚点丢了？）")
        missing = sorted(r for r in refs if not (ROOT / r).is_file())
        self.assertEqual(missing, [], f"README 引用了不存在的测试文件：{missing}")

    def test_readme_docs_listed_files_exist(self):
        """README 文档清单里的 docs/*.md 全部真实存在（含新增评估文档，防链接漂移）。"""
        text = _readme_text()
        docs = set(re.findall(r"docs/[^`\s]+\.md", text))
        self.assertTrue(docs, "没有从 README 解析到 docs/ 引用（守护锚点丢了？）")
        missing = sorted(d for d in docs if not (ROOT / d).is_file())
        self.assertEqual(missing, [], f"README 文档清单指向不存在的文件：{missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
