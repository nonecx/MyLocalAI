# -*- coding: utf-8 -*-
"""档案对比工具层测试（tools_compare）：注册与校验、digest 口径、不写库、not_found 同形。

对着真实事故形态锁定：
   - 注册元数据（compare_dossiers，read/zero/est_s=2，两个 id 参数走 registry.id_param）；
   - 相同 id：registry.validate 只做单参校验（放过），handler 抛 ValidationError
     （参数层拒绝，上层按 clarify 降级）——两层分工锁定，不互相依赖；
   - digest 口径：只转述纯函数 diff（标签演进/as_of/档位徽章/差集计数/缺口），
     无数值分数、无真假判定词、无升级/降级表述；
   - 不写库断言：档案域七表 + tool_runs 计数、档案行字段、目录文件三处都不变；
   - not_found 同形：不存在与跨工作区逐字同形（「档案 #N 不存在。」+ not_found=True）；
   - 降级：ctx.dossiers 缺失 / store 读炸 / 纯函数返回坏形状都折 ok=False + 诚实 digest。
全离线：临时目录里的真实 SQLite 库（TemporaryDirectory），零网络零 LLM。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mylocalai.assistant import tools_compare  # noqa: E402
from mylocalai.assistant.registry import ToolRegistry, ValidationError  # noqa: E402
from mylocalai.dossier.profiles import ProfileStore  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore  # noqa: E402

#: 固定真假判定词（铁律）：任何出口一个都不许出现
_VERDICT_WORDS = ("是真的", "是假的", "属实", "谣言为真", "基本属实", "不实")
#: 升级/降级判断的典型说法（差异只述"标签从 X 变为 Y"，不做这类判断）
_UPGRADE_PHRASES = ("升级为", "降级为", "事态升级", "事态恶化", "风险上升", "风险下降")

_TEXT_A = "某市自来水公司否认在自来水中投毒，官方通报已介入调查"
_TEXT_B = "某市自来水公司否认在自来水中投毒，官方通报已介入调查"


def _conclusion(label, confidence="medium", as_of="2026-10-01T08:00:00+08:00"):
    return {"label": label, "confidence": confidence, "as_of": as_of}


def _ev(url, domain, title="", stance=""):
    return {"url": url, "domain": domain, "domain_tier": "", "source_kind": "",
            "stance": stance, "title": title, "snippet": "", "fetched_at": ""}


class _BrokenStore:
    """get_dossier 一律抛错的替身（模拟档案库整体故障）。"""

    def __init__(self):
        self.calls = 0

    def get_dossier(self, did):
        self.calls += 1
        raise RuntimeError("档案库文件被锁")

    def conclusions(self, did):
        raise RuntimeError("档案库文件被锁")

    def evidence(self, did):
        raise RuntimeError("档案库文件被锁")


class CompareToolTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "dossier.db"
        self.store = DossierStore(self.db)
        self.addCleanup(self.store.close)
        self.service = DossierService(self.store)
        self.addCleanup(self.service.close)
        self.profiles = ProfileStore(self.db)
        self.addCleanup(self.profiles.close)
        self.registry = ToolRegistry()
        tools_compare.register_all(self.registry, None)
        self.default_profile = self.store.active_profile_id()

    def ctx(self, dossiers=None, profile_id=None):
        return SimpleNamespace(sessions=None,
                               dossiers=self.service if dossiers is None else dossiers,
                               events=None, cancel_check=None, services={},
                               profile_id=(self.default_profile if profile_id is None
                                           else profile_id))

    def call(self, left_id, right_id, dossiers=None, profile_id=None):
        spec = self.registry.get("compare_dossiers")
        assert spec is not None, "compare_dossiers 未注册"
        return spec["handler"](self.ctx(dossiers, profile_id),
                               left_id=left_id, right_id=right_id)

    def make_dossier(self, title, text="", conclusions=(), evidence=()):
        did = self.store.create_dossier(title, text, input_kind="text")
        for c in conclusions:
            self.store.add_conclusion(did, label=c["label"], risk_score=None,
                                      confidence=c.get("confidence", ""),
                                      as_of=c.get("as_of", ""))
        if evidence:
            self.store.add_evidence(did, 1, [dict(e) for e in evidence])
        return did

    def make_pair(self):
        left = self.make_dossier(
            "甲档案", _TEXT_A,
            conclusions=[_conclusion("存疑", "low", "2026-10-01T08:00:00+08:00"),
                         _conclusion("存疑", "medium", "2026-10-15T08:00:00+08:00")],
            evidence=[_ev("https://a.example/1", "a.example", "甲证据一", "支持"),
                      _ev("https://shared.example/2", "shared.example", "共有证据", "反驳")])
        right = self.make_dossier(
            "乙档案", _TEXT_B,
            conclusions=[_conclusion("疑似谣言", "medium", "2026-11-01T08:00:00+08:00"),
                         _conclusion("谣言", "high", "2026-11-20T08:00:00+08:00")],
            evidence=[_ev("https://shared.example/2", "shared.example", "共有证据", "反驳"),
                      _ev("https://b.example/3", "b.example", "乙证据一", "支持")])
        return left, right

    def snapshot(self):
        """档案域全量快照（不写库断言的对拍基准）。"""
        rows = self.store.list_dossiers(limit=1000)
        ids = [r["id"] for r in rows]
        return {
            "dossiers": ids,
            "titles": {r["id"]: r["title"] for r in rows},
            "updated": {r["id"]: r["updated_at"] for r in rows},
            "inputs": {d: (self.store.get_dossier(d) or {}).get("input_text", "")
                       for d in ids},
            "conclusions": {d: len(self.store.conclusions(d)) for d in ids},
            "evidence": {d: len(self.store.evidence(d)) for d in ids},
            "runs": {d: len(self.store.runs(d)) for d in ids},
            "fingerprints": {d: len(self.store.claims_for_dossier(d)) for d in ids},
        }


# ===========================================================================
# 1. 注册与校验（4）
# ===========================================================================
class TestRegistration(CompareToolTestBase):
    def test_registered_metadata(self):
        spec = self.registry.get("compare_dossiers")
        self.assertIn("compare_dossiers", self.registry.names())
        self.assertEqual(spec["params"], ["left_id", "right_id"])
        self.assertEqual(spec["permission"], "read")     # 只读对比：不建档
        self.assertEqual(spec["cost"], "zero")           # 无模型无网络：秒答车道
        self.assertEqual(spec["est_s"], 2.0)
        self.assertIn("疑似同源", spec["description"])
        self.assertIn("无分数", spec["description"])

    def test_id_param_validation(self):
        """路由期澄清层：registry.validate 对非正整数 id 抛 ValidationError
        （上层按 clarify 降级）——这一层不动，只锁死它的拒绝面。"""
        for bad in (0, -1, None, "3", 1.5, True, [1], {"id": 1}):
            with self.subTest(value=bad):
                with self.assertRaises(ValidationError):
                    self.registry.validate("compare_dossiers",
                                           {"left_id": bad, "right_id": 1})
                with self.assertRaises(ValidationError):
                    self.registry.validate("compare_dossiers",
                                           {"left_id": 1, "right_id": bad})
        clean = self.registry.validate("compare_dossiers", {"left_id": 3, "right_id": 7})
        self.assertEqual(clean, {"left_id": 3, "right_id": 7})
        junk = self.registry.validate("compare_dossiers",
                                      {"left_id": 3, "right_id": 7, "hack": 1})
        self.assertEqual(junk, {"left_id": 3, "right_id": 7})   # 未声明的键一律丢弃

    def test_same_id_passes_validate_but_handler_degrades_gracefully(self):
        """两层分工锁定：registry.validate 只做单参校验（放过相同 id），handler 走
        诚实降级——用户可修正的输入不摔成系统错误（同 tools_memory 的 not_found
        文化），loop 照常走叙述层而不是落 error 事件。"""
        clean = self.registry.validate("compare_dossiers", {"left_id": 5, "right_id": 5})
        self.assertEqual(clean, {"left_id": 5, "right_id": 5})
        out = self.call(5, 5)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["card"])
        self.assertTrue(out.get("invalid_params"))     # 比 not_found 只多一个的标记键
        self.assertFalse(out["degraded"])              # 参数劝退不是故障
        self.assertEqual(out["status"], "invalid")
        self.assertIn("左右档案不能相同（#5）", out["digest"])
        self.assertIn("两个不同的档案", out["digest"])
        self.assertIn("get_dossier", out["digest"])    # 指路：看版本历史用 get_dossier
        self.assertEqual(out["cost"], {"requests": 0, "tokens": 0})
        self.assertIsNone(out["dossier_id"])
        self.assertTrue(out["not_saved"])
        self.assertEqual(out["save_reason"], "read_only_capability_no_dossier")
        self.assertEqual(out["report"], "")
        self.assertEqual(set(out),
                         {"ok", "card", "digest", "cost", "dossier_id", "degraded",
                          "status", "not_saved", "save_reason", "report",
                          "invalid_params"})

    def test_register_all_takes_no_services(self):
        """本技能无模型无网络：register_all(reg, None) 与不传 services 都能注册。"""
        reg = ToolRegistry()
        tools_compare.register_all(reg)
        self.assertIn("compare_dossiers", reg.names())


# ===========================================================================
# 2. digest 口径（5）
# ===========================================================================
class TestDigest(CompareToolTestBase):
    def test_digest_structure_and_boundaries(self):
        left, right = self.make_pair()
        out = self.call(left, right)
        digest = out["digest"]
        self.assertIn("[档案对比]", digest)
        self.assertIn("纯读取 diff", digest)
        self.assertIn("左档案：#", digest)
        self.assertIn("右档案：#", digest)
        self.assertIn("置信度", digest)             # 档位徽章
        self.assertIn("as_of", digest)
        self.assertIn("限制前 3 条", digest)
        self.assertIn("缺口前 3 条", digest)
        self.assertIn("verify_claim", digest)       # 指路核查流程
        for word in _VERDICT_WORDS + _UPGRADE_PHRASES:
            self.assertNotIn(word, digest)

    def test_digest_label_evolution_and_counts(self):
        left, right = self.make_pair()
        digest = self.call(left, right)["digest"]
        self.assertIn("标签演进：从「存疑」", digest)
        self.assertIn("变为「谣言」", digest)
        self.assertIn("新增 1 条、减少 1 条", digest)     # 证据差集计数（按 url 去重）
        self.assertIn("信源域名：新增 b.example", digest)
        self.assertIn("减少 a.example", digest)

    def test_digest_has_no_numeric_scores(self):
        left, right = self.make_pair()
        digest = self.call(left, right)["digest"]
        self.assertNotIn("risk_score", digest)
        self.assertNotIn("0.72", digest)            # 阈值数值不出现在叙述层
        self.assertNotIn("风险分", digest)

    def test_similarity_badge_variants(self):
        same = self.make_dossier("同原文甲", _TEXT_A, [_conclusion("存疑")])
        same2 = self.make_dossier("同原文乙", _TEXT_A, [_conclusion("存疑")])
        digest = self.call(same, same2)["digest"]
        self.assertIn("指纹相似度：疑似同源（仅标注，不作同一性断言）", digest)

        other = self.make_dossier("异原文", "某地地铁施工发现古墓", [_conclusion("存疑")])
        digest2 = self.call(same, other)["digest"]
        self.assertIn("指纹相似度：未见明显同源（仅标注，不作同一性断言）", digest2)

        blank = self.make_dossier("无原文", "", [_conclusion("存疑")])
        digest3 = self.call(blank, same)["digest"]
        self.assertIn("指纹相似度：无法比对", digest3)
        self.assertIn("声明原文", digest3)

    def test_digest_partial_marks_incomplete_side(self):
        left, right = self.make_pair()
        broken = DossierService(_EvidenceBroken(self.store, {right}))
        self.addCleanup(broken.close)
        out = self.call(left, right, dossiers=broken)
        self.assertIn("partial", out["digest"])
        self.assertIn("证据本读取失败", out["digest"])


class _EvidenceBroken:
    """evidence 对指定 id 抛错的 store 替身（其余透传）。"""

    def __init__(self, inner, fail_ids):
        self._inner = inner
        self._fail = set(fail_ids)

    def get_dossier(self, did):
        return self._inner.get_dossier(did)

    def conclusions(self, did):
        return self._inner.conclusions(did)

    def evidence(self, did):
        if did in self._fail:
            raise RuntimeError("证据表炸了")
        return self._inner.evidence(did)

    def close(self):
        self._inner.close()


# ===========================================================================
# 3. 不写库断言（3）
# ===========================================================================
class TestNoWrite(CompareToolTestBase):
    def test_no_new_rows_after_compare(self):
        left, right = self.make_pair()
        before = self.snapshot()
        out = self.call(left, right)
        self.assertTrue(out["ok"])
        self.assertEqual(self.snapshot(), before)   # 档案域七表计数一行不变

    def test_dossier_rows_untouched(self):
        left, right = self.make_pair()
        before = {d: dict(self.store.get_dossier(d)) for d in (left, right)}
        self.call(left, right)
        for did, row in before.items():
            self.assertEqual(self.store.get_dossier(did), row)   # 含 updated_at

    def test_no_files_written(self):
        left, right = self.make_pair()
        before = sorted(p.name for p in Path(self.tmp.name).iterdir())
        self.call(left, right)
        after = sorted(p.name for p in Path(self.tmp.name).iterdir())
        self.assertEqual(after, before)              # 连 WAL 侧车文件都不多


# ===========================================================================
# 4. not_found 同形与降级（4）
# ===========================================================================
class TestNotFoundAndDegrade(CompareToolTestBase):
    def test_nonexistent_id_not_found_shape(self):
        mine = self.make_dossier("甲", _TEXT_A, [_conclusion("存疑")])
        out = self.call(mine, 424242)
        self.assertFalse(out["ok"])
        self.assertTrue(out.get("not_found"))
        self.assertFalse(out.get("degraded"))       # 不存在不是故障
        self.assertIsNone(out["card"])
        self.assertEqual(out["dossier_id"], None)
        self.assertEqual(out["cost"], {"requests": 0, "tokens": 0})
        self.assertEqual(out["digest"], "档案 #424242 不存在。")

    def test_cross_workspace_same_shape_as_nonexistent(self):
        other = self.profiles.create_profile("other-ws", "别的工作区", "analyst")
        theirs = self.store.create_dossier("别区档案", "别区正文", profile_id=other)
        mine = self.make_dossier("甲", _TEXT_A, [_conclusion("存疑")])
        cross = self.call(mine, theirs)
        missing = self.call(mine, 424242)
        # 同形：键集合与 digest 句式一致（只差档案号），不泄露别的工作区信息
        self.assertEqual(set(cross), set(missing))
        self.assertEqual(cross["digest"], f"档案 #{theirs} 不存在。")
        self.assertTrue(cross.get("not_found"))
        self.assertNotIn("别的工作区", cross["digest"])
        self.assertNotIn("profile", cross["digest"])
        # 反序也一样（先查左边）
        cross_rev = self.call(theirs, mine)
        self.assertEqual(cross_rev["digest"], f"档案 #{theirs} 不存在。")

    def test_missing_dossiers_service_degrades(self):
        mine = self.make_dossier("甲", _TEXT_A, [_conclusion("存疑")])
        spec = self.registry.get("compare_dossiers")
        ctx = SimpleNamespace(sessions=None, dossiers=None, events=None,
                              cancel_check=None, services={},
                              profile_id=self.default_profile)
        out = spec["handler"](ctx, left_id=mine, right_id=mine + 1)
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertIsNone(out["card"])
        self.assertIn("档案库", out["digest"])

    def test_broken_store_degrades(self):
        mine = self.make_dossier("甲", _TEXT_A, [_conclusion("存疑")])
        broken = SimpleNamespace(store=_BrokenStore())
        out = self.call(mine, mine + 1, dossiers=broken)
        self.assertFalse(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertIn("档案库读取失败", out["digest"])
        self.assertIn("RuntimeError", out["digest"])


# ===========================================================================
# 5. 返回形状（2）
# ===========================================================================
class TestResultShape(CompareToolTestBase):
    def test_ok_result_shape(self):
        left, right = self.make_pair()
        out = self.call(left, right)
        self.assertEqual(set(out), {"ok", "card", "digest", "cost", "dossier_id",
                                    "degraded", "status", "not_saved", "save_reason",
                                    "data"})
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["cost"], {"requests": 0, "tokens": 0})
        self.assertIsNone(out["dossier_id"])          # 对比不指向单一档案
        self.assertTrue(out["not_saved"])
        self.assertEqual(out["save_reason"], "read_only_capability_no_dossier")
        self.assertEqual(out["data"]["left"]["id"], left)
        self.assertEqual(out["data"]["right"]["id"], right)

    def test_invalid_ids_degrade_honestly(self):
        """绕过 registry 直调 handler 的坏 id：不抛，折成 ok=False + 诚实 digest
        （路由期已由 registry.validate 折 clarify，这里是二道闸）。"""
        mine = self.make_dossier("甲", _TEXT_A, [_conclusion("存疑")])
        spec = self.registry.get("compare_dossiers")
        out = spec["handler"](self.ctx(), left_id=0, right_id=mine)
        self.assertFalse(out["ok"])
        self.assertFalse(out["degraded"])             # 参数劝退不是库故障
        self.assertTrue(out.get("invalid_params"))
        self.assertIn("正整数", out["digest"])


if __name__ == "__main__":
    unittest.main()
