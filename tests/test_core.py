"""核心链路测试：接入幂等、快照隔离、归并/修订生效、可复现性。

运行：python3 -m unittest tests.test_core -v
"""
import os
import shutil
import tempfile
import unittest

from app import analysis, ingest, worker
from app.db import connect, init_db


ASSIGN = "merchant_id,arm\nM1,control\nM2,treatment\n"

EVENTS1 = """event_uid,event_type,user_id,merchant_id,ts,amount,order_id
E1,exposure,u1,M1,2026-11-02T10:00:00,,
E2,exposure,u1,M2,2026-11-02T11:00:00,,
E3,exposure,u2,M1,2026-11-02T10:00:00,,
E4,conversion,u2,M1,2026-11-02T12:00:00,100.00,O1
E5,conversion,u2,M1,2026-11-02T12:05:00,100.00,O1
E6,refund,u2,M1,2026-11-03T09:00:00,30.00,O1
E7,refund,u9,M2,2026-11-03T10:00:00,10.00,O999
E10,exposure,u3,M2,2026-11-02T10:30:00,,
E11,conversion,u3,M2,2026-11-02T13:00:00,80.00,O3
"""

EVENTS2 = """event_uid,event_type,user_id,merchant_id,ts,amount,order_id
E8,exposure,u1b,M2,2026-11-04T10:00:00,,
E9,conversion,u1b,M2,2026-11-04T11:00:00,50.00,O2
"""

MERGES = "merge_id,primary_user,secondary_user\nMG1,u1,u1b\n"

REVISIONS = """revision_id,event_uid,new_merchant_id,reason
RV1,E2,M1,fix
RV2,E8,M1,fix
"""


def drain(db):
    while True:
        conn = connect(db)
        n = conn.execute(
            "SELECT COUNT(*) c FROM reports WHERE status='pending'"
        ).fetchone()["c"]
        conn.close()
        if n == 0:
            return
        worker.run_pending(db, once=True, log=lambda *a: None)


class TestStats(unittest.TestCase):
    def test_union_find_canonical(self):
        uf = analysis.UF()
        uf.union("b", "a")
        uf.union("c", "b")
        self.assertEqual(uf.find("c"), "a")
        self.assertEqual(uf.find("b"), "a")

    def test_bootstrap_deterministic(self):
        import random
        t = [1, 0, 1, 1, 0, 1]
        c = [0, 0, 1, 0, 0, 0]
        r1 = analysis.boot_ci(t, c, 200, random.Random("42:1:x"))
        r2 = analysis.boot_ci(t, c, 200, random.Random("42:1:x"))
        self.assertEqual(r1, r2)
        w = analysis.welch(t, c)
        self.assertAlmostEqual(w["diff"], 0.5, places=6)

    def test_seed_changes_interval(self):
        import random
        data_rng = random.Random(0)
        t = [data_rng.randrange(100) for _ in range(50)]
        c = [data_rng.randrange(100) for _ in range(50)]
        r1 = analysis.boot_ci(t, c, 100, random.Random("seed-a"))
        r2 = analysis.boot_ci(t, c, 100, random.Random("seed-b"))
        self.assertNotEqual(r1, r2)


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "app.db")
        init_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_ingest_idempotent(self):
        p = self._write("a.csv", ASSIGN)
        r1 = ingest.ingest_file(self.db, p, "EXP")
        self.assertEqual(r1["status"], "ok")
        r2 = ingest.ingest_file(self.db, p, "EXP")
        self.assertEqual(r2["status"], "duplicate")
        p2 = self._write("e.csv", EVENTS1)
        r3 = ingest.ingest_file(self.db, p2, "EXP")
        self.assertEqual(r3["accepted"], 9)
        # 文件内重复 event_uid 会被拒绝
        dup = EVENTS1 + "E1,exposure,u1,M1,2026-11-02T10:00:00,,\n"
        p3 = self._write("e2.csv", dup)
        r4 = ingest.ingest_file(self.db, p3, "EXP")
        self.assertEqual(r4["detail"]["duplicate_event_uid"], 10)  # 全部重复

    def test_snapshot_isolation_and_reproducibility(self):
        exp = "EXP"
        ingest.ingest_file(self.db, self._write("b1.csv", ASSIGN), exp)
        ingest.ingest_file(self.db, self._write("b2.csv", EVENTS1), exp)
        ingest.ingest_file(self.db, self._write("b3.csv", EVENTS2), exp)

        snap1 = worker.create_snapshot(self.db, exp, "归并前")
        r1 = worker.queue_report(self.db, exp, snap1, seed=42, n_boot=50)
        drain(self.db)
        conn = connect(self.db)
        s1 = analysis.load_sections(conn, r1)
        # 归并前：u1 / u2 / u1b / u3 四个用户；u1 受污染；重复转化与未匹配退款留痕
        self.assertEqual(s1["summary"]["n_users"], 4)
        self.assertEqual(s1["summary"]["n_contaminated_users"], 1)
        self.assertEqual(s1["dq"]["duplicate_order_id"], 1)
        self.assertEqual(s1["dq"]["unmatched_refund"], 1)
        # u2: 100 - 30 = 70 元净收入（u1 首次曝光为对照但无转化）
        self.assertEqual(s1["as_assigned"]["control"]["net_cents"], 7000)

        # 身份归并到达 → 新快照；旧报告必须保持原输入版本
        ingest.ingest_file(self.db, self._write("b4.csv", MERGES), exp)
        snap2 = worker.create_snapshot(self.db, exp, "归并后")
        r2 = worker.queue_report(self.db, exp, snap2, seed=42, n_boot=50)
        drain(self.db)
        s2 = analysis.load_sections(conn, r2)
        self.assertEqual(s2["summary"]["n_users"], 3)  # u1b 并入 u1
        self.assertEqual(s2["summary"]["n_contaminated_users"], 1)
        # 旧报告内容不变
        self.assertEqual(analysis.load_sections(conn, r1), s1)

        # 曝光修订到达 → 新快照；u1 的两条跨组曝光都改回 M1 → 不再受污染
        ingest.ingest_file(self.db, self._write("b5.csv", REVISIONS), exp)
        snap3 = worker.create_snapshot(self.db, exp, "修订后")
        r3 = worker.queue_report(self.db, exp, snap3, seed=42, n_boot=50)
        drain(self.db)
        s3 = analysis.load_sections(conn, r3)
        self.assertEqual(s3["summary"]["n_contaminated_users"], 0)
        self.assertEqual(s3["dq"]["revision_rows_applied"], 2)
        # 旧报告依旧不变
        self.assertEqual(analysis.load_sections(conn, r1), s1)
        self.assertEqual(analysis.load_sections(conn, r2), s2)

        # 可复现：同快照同参数重算，结果逐位一致
        r4 = worker.queue_report(self.db, exp, snap2, seed=42, n_boot=50)
        drain(self.db)
        self.assertEqual(analysis.load_sections(conn, r4), s2)

        # 换一个 seed 重算同样确定（两次 seed=7 的结果一致）
        r5 = worker.queue_report(self.db, exp, snap2, seed=7, n_boot=50)
        r6 = worker.queue_report(self.db, exp, snap2, seed=7, n_boot=50)
        drain(self.db)
        self.assertEqual(analysis.load_sections(conn, r5),
                         analysis.load_sections(conn, r6))
        conn.close()


if __name__ == "__main__":
    unittest.main()
