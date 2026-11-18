# -*- coding: utf-8 -*-
"""声明指纹秒回测试（工作指导 §6 M3-1 首要验收项）+ 章节工具接线冒烟（M3-3/M3-5）。

覆盖四组：
  A. 纯函数（fingerprint.py）：归一化等价对、shingle 稳定性、Jaccard 性质、
     阈值边界（=阈值命中/略低不中）、近义不误判、best_match 最高分与容错；
  B. store v3：claim_fingerprints CRUD（幂等沉淀/相似召回）、v2→v3 迁移旧数据保留、
     store._fp_* 与 assistant.fingerprint 的对拍锁定（依赖方向禁 dossier→assistant，
     store 内是同口径最小副本，改动必须两处同步）；
  C. loop 指纹钩子：命中先问（零工具/零叙述调用+待决 system_note）、"秒回"旁过路由、
     "重查"跳过指纹闸、无命中正常核查、落库后指纹沉淀（二次同文命中）、
     ask_before_reuse=False 自动秒回、拒绝预检优先于待决旁过；
  D. 章节接线：read_opinions/deep_read 进注册表+枚举+allowlist、经 AssistantLoop.handle
     走通（stub 服务）章节落库、digest 非"未接入"、枚举外仍 clarify、真实引擎离线
     变体（llm=False / use_llm=False）冒烟、证据本复用/空本不传/opinions_signals 装配。

全离线：SessionStore/DossierService 用 tempfile 库，verify/route/narrate/章节服务全部
stub 或 mock 模块属性，零网络零 LLM 零配额。
"""

from __future__ import annotations

import inspect
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mylocalai.assistant as assistant_pkg  # noqa: E402
from mylocalai.assistant import fingerprint as fp  # noqa: E402
from mylocalai.assistant import router as router_mod  # noqa: E402
from mylocalai.assistant.loop import AssistantLoop, _opinions_signals_from_rows  # noqa: E402
from mylocalai.assistant.events import EventStream  # noqa: E402
from mylocalai.assistant.session_store import SessionStore  # noqa: E402
from mylocalai.dossier import store as ds  # noqa: E402
from mylocalai.dossier.service import DossierService  # noqa: E402
from mylocalai.dossier.store import DossierStore, SCHEMA_VERSION, connect  # noqa: E402

ASSISTANT_DIR = Path(assistant_pkg.__file__).resolve().parent


def load_json(name: str) -> dict:
    return json.loads((ASSISTANT_DIR / name).read_text(encoding="utf-8"))


# ===========================================================================
# A. 纯函数
# ===========================================================================

class TestNormalizeClaim(unittest.TestCase):
    def test_equivalence_pairs(self):
        """归一化等价对：引导词/标点/空白/全半角/大小写的包装差异不改变同一声明。"""
        pairs = [
            ("网传：自来水被下毒了！", "自来水被下毒了"),
            ("据说  自来水被下毒了……", "自来水被下毒了"),
            ("听说自来水被下毒了", "自来水被下毒了"),
            ("曝！自来水被下毒了", "自来水被下毒了"),
            ("据网传，自来水被下毒了", "自来水被下毒了"),   # 长前缀整段剥除
            ("网传曝据说自来水被下毒了", "自来水被下毒了"),  # 堆叠前缀循环剥
            ("Ｔｅｓｔ　ＡＢＣ，１２３", "testabc123"),      # NFKC 全半角 + 小写
            ("ABC", "abc"),
        ]
        for a, b in pairs:
            with self.subTest(a=a, b=b):
                self.assertEqual(fp.normalize_claim(a), fp.normalize_claim(b))
                self.assertEqual(fp.normalize_claim(a), b)

    def test_idempotent_and_never_raises(self):
        """幂等（best_match 依赖）+ 任意怪输入不抛（§0：纯函数永不抛）。"""
        for s in ("网传：自来水被下毒了！", "abc", "。。。",
                  "曝", "网传", None, 123, ["x"], {}, object()):
            once = fp.normalize_claim(s)
            self.assertEqual(fp.normalize_claim(once), once, f"非幂等: {s!r}")
        # 引导词剥空保护："曝"/"网传"本身就是全部内容，不剥成空串
        self.assertEqual(fp.normalize_claim("曝"), "曝")
        self.assertEqual(fp.normalize_claim("网传"), "网传")
        self.assertEqual(fp.normalize_claim("。。。"), "")


class TestShinglesAndJaccard(unittest.TestCase):
    def test_shingles_stable_and_shape(self):
        """同文本跨调用稳定（blake2b，非进程随机 hash）；不同文本不同；短文本整串一个。"""
        s = fp.shingles("自来水被下毒了，官方不敢说")
        self.assertEqual(s, fp.shingles("自来水被下毒了，官方不敢说"))
        self.assertNotEqual(s, fp.shingles("自来水被下毒了，官方不敢说！"))
        self.assertEqual(len(s), len("自来水被下毒了，官方不敢说") - 4 + 1)
        self.assertTrue(all(isinstance(x, int) for x in s))
        # 短于 k：整串一个 shingle（否则短声明永远召回不了）
        self.assertEqual(len(fp.shingles("abc")), 1)
        self.assertEqual(fp.shingles("abc"), fp.shingles("abc"))
        self.assertEqual(fp.shingles(""), frozenset())
        # k 缺省 4、k 非法退 4
        self.assertEqual(fp.shingles("自来水被下毒了", 4), fp.shingles("自来水被下毒了"))
        self.assertEqual(fp.shingles("自来水被下毒了", 0), fp.shingles("自来水被下毒了", 4))

    def test_jaccard_properties(self):
        """Jaccard 性质：恒等=1、不相交=0、空集=0、对称、(0,1) 开区间、坏输入不抛。"""
        a = fp.shingles("自来水里发现红色虫卵")
        b = fp.shingles("自来水里发现红色虫卵了")
        self.assertEqual(fp.jaccard(a, a), 1.0)
        self.assertEqual(fp.jaccard(a, fp.shingles("明天停水检修")), 0.0)
        self.assertEqual(fp.jaccard(a, frozenset()), 0.0)
        self.assertEqual(fp.jaccard(frozenset(), frozenset()), 0.0)
        self.assertEqual(fp.jaccard(a, b), fp.jaccard(b, a))
        self.assertTrue(0.0 < fp.jaccard(a, b) < 1.0)
        self.assertEqual(fp.jaccard(None, None), 0.0)
        self.assertEqual(fp.jaccard(123, a), 0.0)


class TestBestMatch(unittest.TestCase):
    ROW_A = {"dossier_id": 1, "shingles": sorted(fp.shingles("自来水里发现红色虫卵"))}
    ROW_B = {"dossier_id": 2, "shingles": sorted(fp.shingles("隔夜茶喝多了会致癌"))}

    def test_best_match_picks_highest(self):
        """两条候选取 Jaccard 最高的一条；无关候选不干扰。"""
        row, score = fp.best_match("自来水里发现红色虫卵了", [self.ROW_B, self.ROW_A], 0.5)
        self.assertEqual(row["dossier_id"], 1)
        self.assertGreater(score, 0.5)
        row2, score2 = fp.best_match("完全不相干的另一条声明", [self.ROW_A, self.ROW_B], 0.5)
        self.assertIsNone(row2)
        self.assertEqual(score2, 0.0)

    def test_threshold_boundary_exact_hit_slightly_below_miss(self):
        """阈值边界：score == threshold 命中；略低（+0.01）不中。"""
        query = "自来水里发现红色虫卵"
        exact, _ = fp.best_match(query, [self.ROW_A], 1.0)   # 同文 score=1.0，=阈值
        self.assertIsNotNone(exact)
        miss, score = fp.best_match(query, [self.ROW_A], 1.0 + 0.01)
        self.assertIsNone(miss)
        self.assertEqual(score, 1.0)
        # 相邻相似度：构造 5/7 的精确分做边界
        s1, s2 = "甲乙丙丁戊己庚辛", "甲乙丙丁戊己庚辛壬癸"
        j = fp.jaccard(fp.shingles(s1), fp.shingles(s2))
        self.assertAlmostEqual(j, 5 / 7)
        row_hit, _ = fp.best_match(s1, [{"shingles": sorted(fp.shingles(s2))}], j)
        self.assertIsNotNone(row_hit)
        row_miss, _ = fp.best_match(s1, [{"shingles": sorted(fp.shingles(s2))}], j + 0.01)
        self.assertIsNone(row_miss)

    def test_near_synonym_not_mismatched(self):
        """近义不误判：同话题不同声明（停水通知 vs 虫卵）Jaccard 远低于阈值；
        相似说法（加字/去标点/换引导词）确实被召回。"""
        rows = [self.ROW_A, self.ROW_B]
        row, score = fp.best_match("自来水厂明天停水检修", rows, 0.72)
        self.assertIsNone(row)
        self.assertLess(score, 0.72)
        row_hit, hit_score = fp.best_match("网传：自来水里发现红色虫卵！！",
                                           [self.ROW_A], 0.72)
        self.assertIsNotNone(row_hit)
        self.assertGreaterEqual(hit_score, 0.72)

    def test_best_match_never_raises_on_bad_rows(self):
        """坏行/坏阈值/None 输入一律按不命中处理，绝不抛（§0）。"""
        bad_rows = [None, 123, {}, {"shingles": "坏形状"}, {"shingles": [1, "x", 3]},
                    {"shingles": [1, 2, 3]}]
        for th in (0.72, None, "坏", -1):
            with self.subTest(th=th):
                row, _score = fp.best_match("自来水里发现红色虫卵", bad_rows, th)
                self.assertTrue(row is None or isinstance(row, dict))


# ===========================================================================
# B. store v3
# ===========================================================================

class TestStoreFingerprintParity(unittest.TestCase):
    def test_store_helpers_match_fingerprint_module(self):
        """对拍锁定：store._fp_* 与 assistant.fingerprint.* 同口径（依赖方向禁
        dossier→assistant，store 内是同口径最小副本；改一处必须两处同步）。"""
        corpus = ["网传：自来水被下毒了！", "Ｔｅｓｔ　ＡＢＣ，１２３", "据说……曝！据网传内幕",
                  "ABC", "", "。。。", "曝", "网传", "The QUICK brown Fox！"]
        for s in corpus:
            self.assertEqual(ds._fp_normalize(s), fp.normalize_claim(s), s)
            for k in (2, 4, 9):
                self.assertEqual(ds._fp_shingle_hashes(s, k), fp.shingles(s, k), (s, k))
        a = fp.shingles("自来水被下毒了")
        b = fp.shingles("自来水被下毒了吗")
        self.assertEqual(ds._fp_jaccard(a, b), fp.jaccard(a, b))


class TestStoreClaimFingerprints(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DossierStore(Path(self.tmp.name) / "dossier.db")
        self.addCleanup(self.store.close)

    def test_add_idempotent_per_dossier(self):
        """幂等：同 dossier 同 normalized 不重插（同 id）；不同 dossier 各自成行。"""
        d1 = self.store.create_dossier("自来水", "网传自来水被下毒了")
        d2 = self.store.create_dossier("自来水二", "自来水被下毒了！")
        fid1 = self.store.add_claim_fingerprint(d1, "网传自来水被下毒了", label="疑似谣言")
        self.assertGreater(fid1, 0)
        self.assertEqual(self.store.add_claim_fingerprint(d1, "自来水被下毒了！"), fid1,
                         "同档案同声明（归一化后）必须幂等")
        fid2 = self.store.add_claim_fingerprint(d2, "听说自来水被下毒了")
        self.assertNotEqual(fid1, fid2)
        rows = self.store.claims_for_dossier(d1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "疑似谣言")
        self.assertEqual(rows[0]["normalized"], "自来水被下毒了")
        self.assertTrue(all(isinstance(x, int) for x in rows[0]["shingles"]))
        # 规整后为空（纯标点）：如实不沉淀
        self.assertEqual(self.store.add_claim_fingerprint(d1, "。。。"), 0)
        self.assertEqual(len(self.store.claims_for_dossier(d1)), 1)

    def test_find_fingerprints_order_limit_and_shapes(self):
        """召回：按分数降序、limit 截断；shingles JSON 往返；坏形状行跳过不炸。"""
        d1 = self.store.create_dossier("A", "自来水里发现红色虫卵")
        d2 = self.store.create_dossier("B", "自来水里发现红色虫卵了")
        self.store.add_claim_fingerprint(d1, "自来水里发现红色虫卵")
        self.store.add_claim_fingerprint(d2, "自来水里发现红色虫卵了")
        # 直接塞一行坏 shingles（模拟历史脏数据）：跳过，不抛
        with self.store._lock, self.store._conn:
            self.store._conn.execute(
                "INSERT INTO claim_fingerprints (dossier_id, normalized, shingles, label,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (d1, "坏行", "不是JSON", "", ds._now()))
        # 查询"网传…"剥引导前缀后与 d1 同文 → d1 满分在前
        hits = self.store.find_fingerprints("网传自来水里发现红色虫卵！！", 0.72)
        self.assertEqual([h["dossier_id"] for h in hits], [d1, d2])
        self.assertGreaterEqual(hits[0]["score"], hits[1]["score"])
        self.assertGreaterEqual(hits[1]["score"], 0.72)
        self.assertEqual(len(self.store.find_fingerprints(
            "自来水里发现红色虫卵", 0.72, limit=1)), 1)
        # 空查询/纯标点查询：无召回
        self.assertEqual(self.store.find_fingerprints("。。。", 0.72), [])
        self.assertEqual(self.store.find_fingerprints("", 0.72), [])

    def test_v2_to_v3_migration_preserves_old_data(self):
        """迁移：手工建 v2 库（只跑前两个迁移）+ 旧数据 → 开 DossierStore 升到当前版本，
        旧数据逐字段保留，指纹表可用（§6 M3-1：迁移只允许追加；v4 起一并升到最新）。"""
        db = Path(self.tmp.name) / "upgrade.db"
        conn = connect(db)
        with conn:
            conn.executescript("CREATE TABLE IF NOT EXISTS schema_migrations ("
                               "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            conn.executescript(ds.MIGRATIONS[0])
            conn.executescript(ds.MIGRATIONS[1])
            conn.execute("INSERT INTO schema_migrations (version, applied_at)"
                         " VALUES (1, 'seed'), (2, 'seed')")
            cur = conn.execute(
                "INSERT INTO dossiers (created_at, updated_at, title, input_text,"
                " input_kind, schema_version) VALUES ('t0', 't0', '旧档案', '网传旧说法',"
                " 'text', 2)")
            did = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO conclusions (dossier_id, version, label, risk_score,"
                " created_at) VALUES (?, 1, '疑似谣言', 62.0, 't0')", (did,))
        conn.close()

        store = DossierStore(db)   # 构造即迁移 v2→当前版本
        try:
            self.assertEqual(SCHEMA_VERSION, 5)
            row = store.get_dossier(did)
            self.assertEqual(row["title"], "旧档案")
            self.assertEqual(store.latest_conclusion(did)["label"], "疑似谣言")
            # v3 新表可用：旧档案也能补沉淀指纹
            fid = store.add_claim_fingerprint(did, "网传旧说法")
            self.assertGreater(fid, 0)
            self.assertEqual(len(store.find_fingerprints("旧说法", 0.5)), 1)
            conn2 = connect(db)
            try:
                versions = {r["version"] for r in conn2.execute(
                    "SELECT version FROM schema_migrations")}
            finally:
                conn2.close()
            self.assertEqual(versions, {1, 2, 3, 4, 5})
        finally:
            store.close()


# ===========================================================================
# C. loop 指纹钩子
# ===========================================================================

VERIFY_TEXT = "气死了！听说自来水被人下毒了，官方根本不敢说！速转！"
VERIFY_TEXT_NEAR = "网传：自来水被人下毒了，官方根本不敢说，速转！"   # 近重复（非逐字）

OFFICIAL_ENTRY = {"url": "https://gov.example/1", "domain": "gov.example",
                  "domain_tier": "官方权威", "source_kind": "官方（新华网）",
                  "stance": "反驳", "title": "官方辟谣", "snippet": "不实信息",
                  "fetched_at": "2026-10-31T08:00:00+08:00"}
UNOFFICIAL_ENTRY = {"url": "https://bbs.example/2", "domain": "bbs.example",
                    "source_kind": "非官方（微博）", "stance": "支持",
                    "title": "网帖", "snippet": "听说……"}


def stub_route(action="verify_claim", reply="", **extra):
    calls = []

    def _llm(system, user):
        calls.append((system, user))
        return ({"action": action, "reply": reply, **extra}, "")

    _llm.calls = calls
    return _llm


def stub_verify(fail=False, label="疑似谣言"):
    calls = []

    def _svc(text, deep=False, on_component_done=None, cancel_check=None):
        calls.append({"text": text})
        if fail:
            raise RuntimeError("上游取证爆炸")
        return {"label": label, "risk_score": 62, "signals": ["来源模糊"],
                "degraded_components": [], "agent_error": "", "llm_turns": 3,
                "evidence_book": {"claim": text,
                                  "entries": [OFFICIAL_ENTRY, UNOFFICIAL_ENTRY]}}

    _svc.calls = calls
    return _svc


def stub_narrate(text="证据支持以下判断。证据截至今天，仍缺官方细节。"):
    calls = []

    def _svc(system, user):
        calls.append((system, user))
        return (text, "")

    _svc.calls = calls
    return _svc


class LoopTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "app.db"
        self.sessions = SessionStore(db)
        self.addCleanup(self.sessions.close)
        self.dossiers = DossierService(DossierStore(db))
        self.addCleanup(self.dossiers.close)
        self.rules = load_json("assistant_rules.json")
        self.prompts = load_json("prompts.json")
        self.session_id = self.sessions.create_session("指纹测试会话")

    def run_turn(self, services, user_text, rules=None, prompts=None):
        loop = AssistantLoop(sessions=self.sessions, dossiers=self.dossiers,
                             services=services, rules=rules or self.rules,
                             prompts=prompts or self.prompts)
        events = EventStream()
        out = loop.handle(self.session_id, user_text, events, None)
        return out, events.snapshot()

    def system_notes(self):
        return [m for m in self.sessions.messages(self.session_id)
                if m["role"] == "system_note"]

    def seed_hit(self, text=VERIFY_TEXT):
        """第一回合真实核查落库（verify stub），沉淀指纹。"""
        return self.run_turn({"route_llm": stub_route("verify_claim", text=text),
                              "verify": stub_verify(), "narrate_llm": stub_narrate()},
                             text)


class TestFingerprintAskAndResolve(LoopTestBase):
    def test_hit_asks_before_reuse_zero_tool_zero_narration(self):
        """命中先问（M3-1 首要验收）：不执行工具不建档零叙述调用；问句同文落库；
        待决 system_note 落库；result 增 fingerprint_hit；成本只剩路由一次。"""
        self.seed_hit()
        runs_before = self.sessions.runs_for_session(self.session_id)
        verify = stub_verify()
        narrate = stub_narrate()
        out, events = self.run_turn({"route_llm": stub_route("verify_claim",
                                                             text=VERIFY_TEXT_NEAR),
                                     "verify": verify, "narrate_llm": narrate},
                                    VERIFY_TEXT_NEAR)
        # 事件形状与 meta 回合同族：meta/status 起、done 收，无工具链事件
        names = [e["event"] for e in events]
        self.assertEqual(names[:2], ["meta", "status"])
        self.assertEqual(names[-1], "done")
        for banned in ("tool_start", "card", "tool_end", "scan", "progress", "error"):
            self.assertNotIn(banned, names)
        # 问句按句切分逐句 narrative，拼回来等于回复原文
        self.assertEqual("".join(e["delta"] for e in events if e["event"] == "narrative"),
                         out["reply"])
        # verify 未被触达、无新档案、无新 tool_run；叙述模型零调用（零工具零额外成本）
        self.assertEqual(verify.calls, [])
        self.assertEqual(len(self.dossiers.store.list_dossiers()), 1)
        self.assertEqual(self.sessions.runs_for_session(self.session_id), runs_before)
        self.assertEqual(narrate.calls, [])
        # 问句 = prompts.fingerprint_hit 格式化（档案号 + 旧档案标题）
        did = out["fingerprint_hit"]["dossier_id"]
        title = self.dossiers.store.get_dossier(did)["title"]
        self.assertEqual(out["reply"],
                         self.prompts["fingerprint_hit"].format(dossier_id=did, title=title))
        self.assertIn("秒回", out["reply"])
        # 机器可读待决标记 + 消息落库形状
        notes = self.system_notes()
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0]["content"].startswith("[指纹待决] dossier_id="))
        self.assertIn(f"dossier_id={did}", notes[0]["content"])
        msgs = self.sessions.messages(self.session_id)
        self.assertEqual(msgs[-2]["kind"], "text")       # 问句同文落库
        self.assertEqual(msgs[-2]["content"], out["reply"])
        # result：fingerprint_hit 形状 + 成本只有路由段（M4 真实计数：stub 路由无
        # 真实上游调用 → 0；原粗口径"路由咨询 1 次"是快照不可用时的 fallback）
        self.assertEqual(set(out["fingerprint_hit"].keys()), {"dossier_id", "score"})
        self.assertGreaterEqual(out["fingerprint_hit"]["score"],
                                self.rules["fingerprint_threshold"])
        self.assertEqual(out["cost"], {"requests": 0, "tokens": 0})
        self.assertEqual(out["action"], "verify_claim")

    def test_reuse_keyword_bypasses_router_and_replies_old_dossier(self):
        """下一轮"秒回"：旁过路由 LLM，直接 get_dossier 旧档案出卡；不新建档案；
        待决被消费（[指纹已决] system_note）。"""
        self.seed_hit()
        route = stub_route("verify_claim", text=VERIFY_TEXT_NEAR)
        self.run_turn({"route_llm": route, "verify": stub_verify(),
                       "narrate_llm": stub_narrate()}, VERIFY_TEXT_NEAR)
        before = len(route.calls)
        did_old = self.dossiers.store.list_dossiers()[0]["id"]
        out, events = self.run_turn({"route_llm": route, "narrate_llm": stub_narrate()},
                                    "秒回")
        self.assertEqual(len(route.calls), before, "秒回必须旁过路由模型")
        names = [e["event"] for e in events]
        self.assertIn("tool_start", names)
        self.assertIn("card", names)
        self.assertEqual(next(e for e in events if e["event"] == "card")["card_type"],
                         "conclusion")
        self.assertEqual(out["dossier_id"], did_old)
        self.assertEqual(len(self.dossiers.store.list_dossiers()), 1, "不新建档案")
        notes = self.system_notes()
        self.assertTrue(notes[-1]["content"].startswith("[指纹已决] dossier_id="))
        self.assertEqual(out["action"], "get_dossier")

    def test_recheck_keyword_skips_gate_and_verifies_original_text(self):
        """下一轮"重查"：跳过指纹闸（不问第二遍）、verify 收到档案原文、出新档案。"""
        self.seed_hit()
        verify = stub_verify()
        self.run_turn({"route_llm": stub_route("verify_claim", text=VERIFY_TEXT_NEAR),
                       "verify": stub_verify(), "narrate_llm": stub_narrate()},
                      VERIFY_TEXT_NEAR)
        out, _ = self.run_turn({"route_llm": stub_route("verify_claim"),
                                "verify": verify, "narrate_llm": stub_narrate()},
                               "重查")
        self.assertEqual(len(verify.calls), 1)
        self.assertEqual(verify.calls[0]["text"], VERIFY_TEXT, "重查用档案原文")
        self.assertNotIn("fingerprint_hit", out)
        self.assertIsNotNone(out["dossier_id"])
        self.assertEqual(len(self.dossiers.store.list_dossiers()), 2, "verify 新建档案")
        self.assertTrue(self.system_notes()[-1]["content"].startswith("[指纹已决]"))

    def test_resolved_pending_does_not_hijack_later_recheck(self):
        """待决已消费后，用户之后随口说'重查'走普通路由，不再旁过陈年待决。"""
        self.seed_hit()
        self.run_turn({"route_llm": stub_route("verify_claim", text=VERIFY_TEXT_NEAR),
                       "verify": stub_verify(), "narrate_llm": stub_narrate()},
                      VERIFY_TEXT_NEAR)
        self.run_turn({"route_llm": stub_route("chitchat", reply="好的。"),
                       "narrate_llm": stub_narrate()}, "秒回")
        route = stub_route("verify_claim", text="隔夜茶致癌的说法")
        verify = stub_verify()
        out, _ = self.run_turn({"route_llm": route, "verify": verify,
                                "narrate_llm": stub_narrate()}, "重查一下隔夜茶的事")
        self.assertEqual(len(route.calls), 1, "已决之后的重查必须走正常路由")
        self.assertEqual(verify.calls[0]["text"], "隔夜茶致癌的说法")
        self.assertEqual(out["action"], "verify_claim")

    def test_unrelated_reply_after_ask_routes_normally(self):
        """问句之后用户没表态（说了别的）→ 普通路由，且待决立即关闭（复核 P2-2：
        待决只对紧随下一轮生效，防此后任意"重查/秒回"字样被陈年待决劫持）。"""
        self.seed_hit()
        self.run_turn({"route_llm": stub_route("verify_claim", text=VERIFY_TEXT_NEAR),
                       "verify": stub_verify(), "narrate_llm": stub_narrate()},
                      VERIFY_TEXT_NEAR)
        route = stub_route("chitchat", reply="在的。")
        out, _ = self.run_turn({"route_llm": route, "narrate_llm": stub_narrate()},
                               "先别管这个，你好")
        self.assertEqual(len(route.calls), 1)
        self.assertEqual(out["action"], "chitchat")
        self.assertTrue(self.system_notes()[-1]["content"].startswith("[指纹已决]"),
                        "未表态也必须关闭待决窗口")
        # 关闭后含"重查"字样的消息不再被旁过：走正常路由且不重查旧档案原文
        route2 = stub_route("chitchat", reply="好的。")
        out2, _ = self.run_turn({"route_llm": route2, "narrate_llm": stub_narrate()},
                                "帮我重查一下档案#1")
        self.assertEqual(len(route2.calls), 1, "待决关闭后'重查'必须走正常路由")
        self.assertEqual(out2["action"], "chitchat")

    def test_no_hit_normal_verify_unchanged(self):
        """无命中：verify 正常执行建档，result 无 fingerprint_hit，返回键集不变。"""
        route = stub_route("verify_claim", text=VERIFY_TEXT)
        verify = stub_verify()
        out, _ = self.run_turn({"route_llm": route, "verify": verify,
                                "narrate_llm": stub_narrate()}, VERIFY_TEXT)
        self.assertEqual(len(verify.calls), 1)
        self.assertNotIn("fingerprint_hit", out)
        self.assertEqual(set(out.keys()),
                         {"session_id", "reply", "action", "card", "tool_run_id",
                          "dossier_id", "degraded", "cost", "claim_answer"})
        self.assertEqual(out["action"], "verify_claim")
        self.assertIsNotNone(out["dossier_id"])

    def test_deposit_after_verify_hits_on_second_identical(self):
        """沉淀：verify 落库成功后指纹表有记录；二次同文命中（M3-1 验收）。"""
        self.seed_hit()
        did = self.dossiers.store.list_dossiers()[0]["id"]
        rows = self.dossiers.store.claims_for_dossier(did)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["normalized"], fp.normalize_claim(VERIFY_TEXT))
        self.assertEqual(rows[0]["label"], "疑似谣言")
        # 二次同文（同一会话再发一遍）→ 命中先问
        out, _ = self.run_turn({"route_llm": stub_route("verify_claim", text=VERIFY_TEXT),
                                "verify": stub_verify(), "narrate_llm": stub_narrate()},
                               VERIFY_TEXT)
        self.assertIn("fingerprint_hit", out)
        self.assertEqual(out["fingerprint_hit"]["dossier_id"], did)

    def test_deposit_skipped_when_record_failed(self):
        """record_analysis 失败（工具异常无 dossier/version）不沉淀：指纹表保持为空。"""
        out, _ = self.run_turn({"route_llm": stub_route("verify_claim", text=VERIFY_TEXT),
                                "verify": stub_verify(fail=True),
                                "narrate_llm": stub_narrate()}, VERIFY_TEXT)
        self.assertTrue(out["degraded"])
        self.assertEqual(self.dossiers.store.claims_for_dossier(1), [])

    def test_ask_before_reuse_false_auto_reuses(self):
        """ask_before_reuse=False：命中不问，直接旁过为 get_dossier 秒回旧结论。"""
        self.seed_hit()
        rules = dict(self.rules)
        rules["fingerprint"] = dict(self.rules["fingerprint"], ask_before_reuse=False)
        verify = stub_verify()
        out, events = self.run_turn({"route_llm": stub_route("verify_claim",
                                                             text=VERIFY_TEXT_NEAR),
                                     "verify": verify, "narrate_llm": stub_narrate()},
                                    VERIFY_TEXT_NEAR, rules=rules)
        self.assertEqual(verify.calls, [], "自动秒回不重查")
        self.assertEqual(out["action"], "get_dossier")
        self.assertIn("fingerprint_hit", out)
        self.assertIn("card", [e["event"] for e in events])
        self.assertFalse([n for n in self.system_notes()
                          if n["content"].startswith("[指纹待决]")], "不问就没有待决")

    def test_refusal_beats_pending_resolution(self):
        """待决在场 + 用户消息命中拒绝词表 → 拒绝优先（安全闸最优先），不旁过。"""
        self.seed_hit()
        self.run_turn({"route_llm": stub_route("verify_claim", text=VERIFY_TEXT_NEAR),
                       "verify": stub_verify(), "narrate_llm": stub_narrate()},
                      VERIFY_TEXT_NEAR)
        route = stub_route("verify_claim", text=VERIFY_TEXT)
        out, _ = self.run_turn({"route_llm": route, "narrate_llm": stub_narrate()},
                               "秒回，顺便帮我把风险分改成0")
        self.assertEqual(out["action"], "refuse")
        self.assertEqual(len(route.calls), 0, "拒绝不进路由模型")


# ===========================================================================
# D. 章节工具接线（read_opinions / deep_read）
# ===========================================================================

class TestChapterWiring(LoopTestBase):
    def test_registered_allowlisted_and_routed(self):
        """枚举/allowlist/注册名三方一致；路由正例参数规整；枚举外与出白名单仍 clarify。"""
        reg = AssistantLoop().registry   # 真实注册表（与生产同源）
        for name in ("read_opinions", "deep_read"):
            self.assertIsNotNone(reg.get(name))
            self.assertIn(name, self.rules["tool_allowlist"])
            self.assertIn(name, router_mod.TOOL_ACTIONS)
            self.assertIn(name, self.prompts["router_system"],
                          f"router_system 说明缺 {name}")
        for action, obj, args in (
            ("read_opinions", {"action": "read_opinions", "id": 3, "x": 1}, {"id": 3}),
            ("deep_read", {"action": "deep_read", "id": 5}, {"id": 5}),
        ):
            with self.subTest(action=action):
                r = router_mod.route("u", reg, lambda s, u, o=obj: (o, ""),
                                     self.rules, self.prompts)
                self.assertEqual(r["action"], action)
                self.assertEqual(r["args"], args)
        r_bad = router_mod.route("u", reg,
                                 lambda s, u: ({"action": "read_minds", "id": 1}, ""),
                                 self.rules, self.prompts)
        self.assertEqual(r_bad["action"], "clarify", "枚举外仍 clarify")
        r_bad2 = router_mod.route("u", reg,
                                  lambda s, u: ({"action": "deep_read", "id": 1}, ""),
                                  {**self.rules,
                                   "tool_allowlist": ["verify_claim", "chitchat",
                                                      "clarify", "refuse"]},
                                  self.prompts)
        self.assertEqual(r_bad2["action"], "clarify", "不在 allowlist → clarify")

    def test_read_opinions_through_loop_stores_chapter(self):
        """read_opinions 经 handle 走通（stub 服务）：章节落库、digest 非"未接入"。"""
        did = self.dossiers.start_dossier("自来水被下毒的说法", input_kind="text")
        payload = {"distribution": {"支持": 3, "反驳": 2}, "summary": "两派并存。",
                   "degraded": False, "triggered": True}
        narrate = stub_narrate()
        out, _ = self.run_turn(
            {"route_llm": stub_route("read_opinions", id=did),
             "opinions": (lambda dossier_id: dict(payload)),
             "narrate_llm": narrate},
            f"看看档案{did}大家怎么看")
        self.assertEqual(out["action"], "read_opinions")
        self.assertEqual(out["dossier_id"], did)
        # digest（进叙述上下文的 [本轮工具结果]）非"未接入"，且带分布计数
        digest = narrate.calls[0][1]
        self.assertIn("[本轮工具结果]", digest)
        self.assertIn("支持 3", digest)
        self.assertNotIn("未接入", digest)
        rows = self.dossiers.store.opinions(did)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["distribution"], {"支持": 3, "反驳": 2})
        self.assertEqual(rows[0]["summary"], "两派并存。")

    def test_deep_read_through_loop_stores_chapter(self):
        """deep_read 经 handle 走通（stub 服务）：整包 payload 落库、digest 非"未接入"。"""
        did = self.dossiers.start_dossier("自来水被下毒的说法", input_kind="text")
        payload = {"triggered": True, "applicable": True,
                   "text": "张力：信息发布节奏与公众知情需求。",
                   "degraded": False, "guard_dropped": [], "source": "llm"}
        narrate = stub_narrate()
        out, _ = self.run_turn(
            {"route_llm": stub_route("deep_read", id=did),
             "deep_read": (lambda dossier_id: dict(payload)),
             "narrate_llm": narrate},
            f"把档案{did}查透")
        self.assertEqual(out["action"], "deep_read")
        digest = narrate.calls[0][1]
        self.assertIn("[本轮工具结果]", digest)
        self.assertIn("解读要点", digest)
        self.assertNotIn("未接入", digest)
        rows = self.dossiers.store.deep_reads(did)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["text"], payload["text"])
        self.assertFalse(rows[0]["degraded"])


class TestRealChapterServices(LoopTestBase):
    """真实引擎离线变体：llm=False / use_llm=False 的服务变体 + mock 模块属性。"""

    def make_real_loop(self):
        return AssistantLoop(sessions=self.sessions, dossiers=self.dossiers,
                             services={}, rules=self.rules, prompts=self.prompts)

    @staticmethod
    def seed_ugc_evidence(service, did, n=6):
        entries = [{"url": f"https://bbs.example/post/{i}",
                    "domain": "bbs.example", "domain_tier": "自媒体/UGC",
                    "source_kind": "非官方（论坛）",
                    "stance": "反驳" if i % 2 else "支持",
                    "title": f"网帖{i}",
                    "snippet": f"发言{i}：通报时间比网友爆料晚了快一天",
                    "fetched_at": "2026-10-31T08:00:00+08:00"}
                   for i in range(1, n + 1)]
        service.store.add_evidence(did, 1, entries)
        return entries

    def test_real_opinions_service_offline_llm_false(self):
        """真实 opinions 服务（llm=False）：档案证据本触发民间观点分析，分布计数正确。"""
        did = self.dossiers.start_dossier("自来水被下毒的说法", input_kind="text")
        self.seed_ugc_evidence(self.dossiers, did)
        svc = self.make_real_loop().services["opinions"]   # 真实服务（owner=loop）
        with mock.patch("mylocalai.rumor.opinions.fetch_article",
                        side_effect=RuntimeError("离线禁网")):
            payload = svc(did, llm=False)    # 离线变体：零 LLM 零网络
        self.assertTrue(payload["triggered"])
        self.assertEqual(payload["distribution"], {"支持": 3, "反驳": 3})
        self.assertTrue(payload["degraded"], "llm=False 的固有局限如实记降级")
        self.assertIsInstance(payload["summary"], str)

    def test_real_deep_read_service_offline_use_llm_false_and_specs(self):
        """真实 deep_read 服务（use_llm=False）：高 E 快照 → 模板回退触发；
        规格①③④ 的传参形状用记录桩逐项锁定。"""
        from mylocalai.attention import deep_read as dr
        did = self.dossiers.start_dossier("自来水被下毒的说法", input_kind="text")
        self.seed_ugc_evidence(self.dossiers, did)
        self.dossiers.store.add_snapshot(did, 100.0, 5_000_000.0, 50_000.0, 80.0,
                                         profile="spec",
                                         proxy_flags=["y_proxy", "z_proxy"])
        svc = self.make_real_loop().services["deep_read"]
        real_deep_read = dr.deep_read   # 先取真函数：wraps 桩会遮蔽 inspect 探测（复核 P2-1）
        supported = "snapshot_history" in inspect.signature(real_deep_read).parameters
        self.assertTrue(supported, "引擎契约：deep_read 应支持 snapshot_history")
        with mock.patch.object(dr, "deep_read", wraps=real_deep_read) as spy:
            payload = svc(did, use_llm=False)   # 离线变体：模板回退，零 LLM
            self.assertTrue(payload["triggered"])
            self.assertTrue(payload["text"], "模板回退也有正文，不空转")
            self.assertEqual(payload["source"], "template")
            self.assertTrue(payload["degraded"])
            # 规格①：档案证据行（snippet 非空）编号成 evidence 传入
            kwargs = spy.call_args.kwargs
            self.assertEqual(len(kwargs["evidence"]), 6)
            self.assertEqual(kwargs["evidence"][0]["text"],
                             "发言1：通报时间比网友爆料晚了快一天")
            self.assertEqual(kwargs["query"], "自来水被下毒的说法")
            self.assertEqual(kwargs["detail"]["score"], 80.0)
            self.assertEqual(kwargs["detail"]["band"], "高关注")
            # 规格②（复核 P2-1 改为实断言）：快照历史真传给引擎，e_score 升序提取
            self.assertIn("snapshot_history", kwargs)
            self.assertEqual(list(kwargs["snapshot_history"]), [80.0])
            # 规格③：观点章节不存在 → 不传 opinions_signals
            self.assertNotIn("opinions_signals", kwargs)
        # 规格③正向：观点行带结构化信号字段时透传（"有什么取什么"）
        self.dossiers.store.add_opinions(did, 1, {"支持": 3}, summary="两派")
        with mock.patch.object(dr, "deep_read", wraps=dr.deep_read) as spy2, \
                mock.patch("mylocalai.assistant.loop._opinions_signals_from_rows",
                           return_value={"appeals": ["要求公布检测报告"],
                                         "attribution_targets": [],
                                         "action_tendencies": []}):
            svc(did, use_llm=False)
            self.assertEqual(spy2.call_args.kwargs["opinions_signals"]["appeals"],
                             ["要求公布检测报告"])

    def test_real_deep_read_empty_evidence_no_snapshot_honest_exit(self):
        """规格④：空证据本不传 evidence；无快照 E=0 → 如实不触发深读（合法出口）。"""
        from mylocalai.attention import deep_read as dr
        did = self.dossiers.start_dossier("没什么料的档案", input_kind="text")
        svc = self.make_real_loop().services["deep_read"]
        with mock.patch.object(dr, "deep_read", wraps=dr.deep_read) as spy:
            payload = svc(did, use_llm=False)
            kwargs = spy.call_args.kwargs
            self.assertNotIn("evidence", kwargs, "空证据本不传 evidence（规格④）")
            self.assertEqual(kwargs["detail"]["score"], 0.0)
        self.assertFalse(payload["triggered"])
        self.assertIn("未超过深读阈值", payload["reason"])

    def test_opinions_signals_from_rows_takes_what_exists(self):
        """_opinions_signals_from_rows："有什么取什么"——行里有同名字段透传，没有则空；
        三键恒在（与 deep_read prompt 的消费键对齐）。"""
        empty = {"appeals": [], "attribution_targets": [], "action_tendencies": []}
        self.assertEqual(_opinions_signals_from_rows([]), empty)
        self.assertEqual(_opinions_signals_from_rows([{"distribution": {}, "summary": "s"}]),
                         empty)
        got = _opinions_signals_from_rows([
            {"distribution": {"支持": 1}, "summary": "s"},
            {"distribution": {}, "summary": "s2",
             "appeals": ["公布报告", None, 3], "attribution_targets": ["发布方"],
             "action_tendencies": "坏形状"}])
        self.assertEqual(got["appeals"], ["公布报告"])
        self.assertEqual(got["attribution_targets"], ["发布方"])
        self.assertEqual(got["action_tendencies"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
