import os
import sys
import json
import tempfile
import unittest
import shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from invoice_reconciler.core.config import Config
from invoice_reconciler.core.database import (
    Database,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_PENDING,
    MATCH_STATUS_EXCEPTION,
    MATCH_STATUS_REVOKED,
    MATCH_STATUS_UNMATCHED,
)
from invoice_reconciler.core.importer import CSVImporter
from invoice_reconciler.core.matcher import MatchEngine
from invoice_reconciler.core.revoker import Revoker
from invoice_reconciler.core.exporter import ReportExporter
from invoice_reconciler.core.reviewer import ReviewSnapshot


SAMPLE_INVOICES_CSV = """invoice_no,invoice_date,customer,amount,status
INV001,2024-01-15,北京科技有限公司,1000.00,正常
INV002,2024-01-16,上海贸易公司,2500.50,正常
INV003,2024-01-17,广州电子厂,3000.00,正常
INV004,2024-01-18,深圳软件公司,1500.00,正常
INV005,2024-01-19,杭州电商平台,800.00,正常
"""

SAMPLE_PAYMENTS_CSV = """payment_no,payment_date,customer,amount
PAY001,2024-01-16,北京科技有限公司,1000.00
PAY002,2024-01-17,上海贸易公司,2500.50
PAY003,2024-01-18,广州电子厂,3000.00
PAY004,2024-01-20,深圳软件公司,1499.99
PAY005,2024-01-21,杭州电商平台,800.00
"""


class TestReviewSnapshot(unittest.TestCase):
    """复核快照测试"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")

        self.config = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
        )

        self.invoice_csv = os.path.join(self.test_dir, "invoices.csv")
        self.payment_csv = os.path.join(self.test_dir, "payments.csv")
        with open(self.invoice_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_CSV)
        with open(self.payment_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_CSV)

        self.db = Database(self.db_path)
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db)
        self.revoker = Revoker(self.db)
        self.exporter = ReportExporter(self.config, self.db)
        self.reviewer = ReviewSnapshot(self.db)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _import_and_match(self, operator="测试员A"):
        """辅助方法：导入数据并执行匹配"""
        self.importer.import_invoices(self.invoice_csv, operator)
        self.importer.import_payments(self.payment_csv, operator)
        result = self.matcher.run_auto_matching(operator)
        return result

    def test_snapshot_create_and_list(self):
        """测试创建快照和列出快照"""
        self._import_and_match()

        snapshot = self.reviewer.create_snapshot(
            snapshot_type="manual",
            description="测试快照",
            operator="测试员A"
        )

        self.assertIsNotNone(snapshot)
        self.assertIn("snapshot_no", snapshot)
        self.assertTrue(snapshot["snapshot_no"].startswith("R"))
        self.assertEqual(snapshot["snapshot_type"], "manual")
        self.assertEqual(snapshot["operator"], "测试员A")
        self.assertGreater(snapshot["total_matches"], 0)

        snapshots = self.reviewer.list_snapshots()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["snapshot_no"], snapshot["snapshot_no"])

    def test_snapshot_stable_number(self):
        """测试同一快照编号稳定，多次查询不变"""
        self._import_and_match()

        snapshot1 = self.reviewer.create_snapshot(
            description="第一次快照",
            operator="测试员A"
        )
        no1 = snapshot1["snapshot_no"]

        snapshot2 = self.reviewer.get_snapshot(snapshot_no=no1)
        no2 = snapshot2["snapshot_no"]

        self.assertEqual(no1, no2, "同一快照编号应该保持一致")

    def test_snapshot_contains_full_info(self):
        """测试快照包含完整信息：状态、历史、候选、操作者、时间"""
        self._import_and_match()

        matches = self.db.get_matches_by_status()
        pending_matches = [m for m in matches if m["status"] == MATCH_STATUS_PENDING]
        if pending_matches:
            self.matcher.confirm_match(
                pending_matches[0]["id"],
                "测试员A",
                "测试确认备注"
            )

        snapshot = self.reviewer.create_snapshot(
            description="完整信息测试",
            operator="测试员A"
        )
        snapshot_detail = self.reviewer.get_snapshot(snapshot_no=snapshot["snapshot_no"])

        self.assertIn("items", snapshot_detail)
        self.assertGreater(len(snapshot_detail["items"]), 0)

        for item in snapshot_detail["items"]:
            self.assertIn("match_no", item)
            self.assertIn("status", item)
            self.assertIn("status_history", item)
            self.assertIn("candidate_payments", item)
            self.assertIn("invoice_no", item)
            self.assertIn("payment_no", item)
            self.assertIn("created_at", item)

            self.assertIsInstance(item["status_history"], list)
            self.assertIsInstance(item["candidate_payments"], list)

    def test_replay_verify_consistent(self):
        """测试回放校验：快照后无操作应完全一致"""
        self._import_and_match()

        snapshot = self.reviewer.create_snapshot(operator="测试员A")
        result = self.reviewer.replay_verify(snapshot_no=snapshot["snapshot_no"])

        self.assertTrue(result["is_consistent"])
        self.assertEqual(result["status_changed"], 0)
        self.assertEqual(result["new_in_current"], 0)
        self.assertEqual(result["missing_in_current"], 0)
        self.assertEqual(len(result["differences"]), 0)

    def test_replay_verify_after_changes(self):
        """测试回放校验：撤销后应检测到差异"""
        self._import_and_match()

        snapshot = self.reviewer.create_snapshot(operator="测试员A")

        matched = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
        if matched:
            self.revoker.revoke_match(matched[0]["id"], "测试员B", "测试撤销")

        result = self.reviewer.replay_verify(snapshot_no=snapshot["snapshot_no"])

        self.assertFalse(result["is_consistent"])
        self.assertGreater(len(result["differences"]), 0)

        diff_types = [d["type"] for d in result["differences"]]
        self.assertIn("status_changed", diff_types)

    def test_snapshot_export_json(self):
        """测试快照导出 JSON 格式"""
        self._import_and_match()

        snapshot = self.reviewer.create_snapshot(operator="测试员A")
        snapshot_data = self.reviewer.get_snapshot_for_export(
            snapshot_no=snapshot["snapshot_no"]
        )
        result = self.exporter.export_snapshot(
            snapshot_data,
            operator="测试员A",
            format="json"
        )

        self.assertTrue(result["success"])
        self.assertTrue(os.path.exists(result["file_path"]))
        self.assertEqual(result["format"], "json")

        with open(result["file_path"], "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("snapshot_info", data)
        self.assertIn("items", data)
        self.assertEqual(data["snapshot_info"]["快照编号"], snapshot["snapshot_no"])
        self.assertGreater(len(data["items"]), 0)

    def test_snapshot_export_csv(self):
        """测试快照导出 CSV 格式"""
        self._import_and_match()

        snapshot = self.reviewer.create_snapshot(operator="测试员A")
        snapshot_data = self.reviewer.get_snapshot_for_export(
            snapshot_no=snapshot["snapshot_no"]
        )
        result = self.exporter.export_snapshot(
            snapshot_data,
            operator="测试员A",
            format="csv"
        )

        self.assertTrue(result["success"])
        self.assertTrue(os.path.isdir(result["file_path"]))
        self.assertEqual(result["format"], "csv")

        files = os.listdir(result["file_path"])
        self.assertIn("快照信息.csv", files)
        self.assertIn("匹配明细.csv", files)

    def test_snapshot_export_consistency(self):
        """测试同一快照多次导出内容一致性"""
        self._import_and_match()

        snapshot = self.reviewer.create_snapshot(operator="测试员A")
        snapshot_no = snapshot["snapshot_no"]
        snapshot_data1 = self.reviewer.get_snapshot_for_export(
            snapshot_no=snapshot_no
        )

        pending_matches = self.db.get_matches_by_status(MATCH_STATUS_PENDING)
        if pending_matches:
            self.matcher.confirm_match(
                pending_matches[0]["id"],
                "测试员B",
                "测试确认"
            )

        snapshot_data2 = self.reviewer.get_snapshot_for_export(
            snapshot_no=snapshot_no
        )

        self.assertEqual(
            len(snapshot_data1["items"]),
            len(snapshot_data2["items"]),
            "同一快照导出的条目数应该一致，不受后续操作影响"
        )

        self.assertEqual(
            snapshot_data1["snapshot_info"]["快照编号"],
            snapshot_data2["snapshot_info"]["快照编号"],
            "同一快照的编号应该一致"
        )

        for i in range(len(snapshot_data1["items"])):
            self.assertEqual(
                snapshot_data1["items"][i]["匹配编号"],
                snapshot_data2["items"][i]["匹配编号"]
            )
            self.assertEqual(
                snapshot_data1["items"][i]["当前状态"],
                snapshot_data2["items"][i]["当前状态"]
            )


class TestConflictDetection(unittest.TestCase):
    """冲突检测测试"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.config = Config(db_path=self.db_path)
        self.db = Database(self.db_path)
        self.matcher = MatchEngine(self.config, self.db)
        self.reviewer = ReviewSnapshot(self.db)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_invoice(self, invoice_no, amount=1000.0):
        """辅助方法：直接在数据库创建发票"""
        with self.db._get_conn() as conn:
            conn.execute(
                """INSERT INTO invoices
                   (invoice_no, invoice_date, customer, amount, status, match_status)
                   VALUES (?, '2024-01-01', '测试客户', ?, 'normal', 'unmatched')""",
                (invoice_no, amount)
            )
            row = conn.execute(
                "SELECT id FROM invoices WHERE invoice_no = ?",
                (invoice_no,)
            ).fetchone()
            return row["id"]

    def _create_payment(self, payment_no, amount=1000.0):
        """辅助方法：直接在数据库创建收款"""
        with self.db._get_conn() as conn:
            conn.execute(
                """INSERT INTO payments
                   (payment_no, payment_date, customer, amount, status, match_status)
                   VALUES (?, '2024-01-02', '测试客户', ?, 'normal', 'unmatched')""",
                (payment_no, amount)
            )
            row = conn.execute(
                "SELECT id FROM payments WHERE payment_no = ?",
                (payment_no,)
            ).fetchone()
            return row["id"]

    def test_no_conflict_single_match(self):
        """测试单条匹配无冲突"""
        inv_id = self._create_invoice("INV-TEST-001")
        pay_id1 = self._create_payment("PAY-TEST-001")

        self.db.create_match(
            inv_id, pay_id1, "manual", 100.0, "人工匹配",
            MATCH_STATUS_MATCHED, "操作员A", "测试匹配"
        )

        conflicts = self.reviewer.check_conflicts()
        invoice_conflicts = [c for c in conflicts if c["invoice_no"] == "INV-TEST-001"]
        self.assertEqual(len(invoice_conflicts), 0, "单条匹配不应有冲突")

    def test_conflict_different_operators(self):
        """测试同一发票被不同操作者处理时检测到冲突"""
        inv_id = self._create_invoice("INV-TEST-002")
        pay_id1 = self._create_payment("PAY-TEST-002")
        pay_id2 = self._create_payment("PAY-TEST-003")

        self.db.create_match(
            inv_id, pay_id1, "manual", 100.0, "人工匹配1",
            MATCH_STATUS_REVOKED, "操作员A", "第一次匹配"
        )
        self.db.create_match(
            inv_id, pay_id2, "manual", 100.0, "人工匹配2",
            MATCH_STATUS_MATCHED, "操作员B", "第二次匹配"
        )

        conflicts = self.reviewer.check_conflicts(invoice_no="INV-TEST-002")
        self.assertGreater(len(conflicts), 0, "应检测到多操作者冲突")

        conflict = conflicts[0]
        self.assertEqual(conflict["invoice_no"], "INV-TEST-002")
        self.assertIn("操作员A", conflict["operators"])
        self.assertIn("操作员B", conflict["operators"])
        self.assertGreaterEqual(conflict["match_count"], 2)


class TestRestartPersistence(unittest.TestCase):
    """重启持久化测试"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")

        self.config = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
        )

        self.invoice_csv = os.path.join(self.test_dir, "invoices.csv")
        self.payment_csv = os.path.join(self.test_dir, "payments.csv")
        with open(self.invoice_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_CSV)
        with open(self.payment_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_CSV)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_database_persists_after_restart(self):
        """测试重启后数据库数据不丢失"""
        db1 = Database(self.db_path)
        importer1 = CSVImporter(self.config, db1)
        matcher1 = MatchEngine(self.config, db1)
        reviewer1 = ReviewSnapshot(db1)

        importer1.import_invoices(self.invoice_csv, "操作员A")
        importer1.import_payments(self.payment_csv, "操作员A")
        matcher1.run_auto_matching("操作员A")

        snapshot = reviewer1.create_snapshot(
            description="重启前快照",
            operator="操作员A"
        )
        snapshot_no = snapshot["snapshot_no"]

        stats_before = db1.get_statistics()
        matches_before = db1.get_matches_by_status()

        del db1, importer1, matcher1, reviewer1

        db2 = Database(self.db_path)
        reviewer2 = ReviewSnapshot(db2)

        stats_after = db2.get_statistics()
        matches_after = db2.get_matches_by_status()

        self.assertEqual(
            stats_before["total_invoices"],
            stats_after["total_invoices"],
            "重启后发票数量应一致"
        )
        self.assertEqual(
            stats_before["total_payments"],
            stats_after["total_payments"],
            "重启后收款数量应一致"
        )
        self.assertEqual(
            stats_before["confirmed_matches"],
            stats_after["confirmed_matches"],
            "重启后已匹配数量应一致"
        )
        self.assertEqual(
            len(matches_before),
            len(matches_after),
            "重启后匹配记录数应一致"
        )

        snapshot_after = reviewer2.get_snapshot(snapshot_no=snapshot_no)
        self.assertIsNotNone(snapshot_after, "重启后快照应仍然存在")
        self.assertEqual(snapshot_after["snapshot_no"], snapshot_no)
        self.assertEqual(snapshot_after["total_matches"], snapshot["total_matches"])

    def test_status_history_persists(self):
        """测试状态历史在重启后不丢失"""
        db1 = Database(self.db_path)
        importer1 = CSVImporter(self.config, db1)
        matcher1 = MatchEngine(self.config, db1)
        revoker1 = Revoker(db1)

        importer1.import_invoices(self.invoice_csv, "操作员A")
        importer1.import_payments(self.payment_csv, "操作员A")
        matcher1.run_auto_matching("操作员A")

        matched = db1.get_matches_by_status(MATCH_STATUS_MATCHED)
        if matched:
            match_id = matched[0]["id"]
            match_no = matched[0]["match_no"]
            history_before = db1.get_status_history(match_id=match_id)

            revoker1.revoke_match(match_id, "操作员B", "测试撤销")
            history_before_after_revoke = db1.get_status_history(match_id=match_id)

            del db1, importer1, matcher1, revoker1

            db2 = Database(self.db_path)
            history_after = db2.get_status_history(match_id=match_id)

            self.assertEqual(
                len(history_before_after_revoke),
                len(history_after),
                "重启后状态历史记录数应一致"
            )

            for i in range(len(history_after)):
                self.assertEqual(
                    history_before_after_revoke[i]["old_status"],
                    history_after[i]["old_status"]
                )
                self.assertEqual(
                    history_before_after_revoke[i]["new_status"],
                    history_after[i]["new_status"]
                )


class TestRevokeReplay(unittest.TestCase):
    """撤销与回放测试"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")

        self.config = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
        )

        self.invoice_csv = os.path.join(self.test_dir, "invoices.csv")
        self.payment_csv = os.path.join(self.test_dir, "payments.csv")
        with open(self.invoice_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_CSV)
        with open(self.payment_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_CSV)

        self.db = Database(self.db_path)
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db)
        self.revoker = Revoker(self.db)
        self.reviewer = ReviewSnapshot(self.db)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_revoke_then_reconfirm(self):
        """测试撤销后再确认的完整流程"""
        self.importer.import_invoices(self.invoice_csv, "操作员A")
        self.importer.import_payments(self.payment_csv, "操作员A")
        self.matcher.run_auto_matching("操作员A")

        matched = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
        self.assertGreater(len(matched), 0, "应该有自动匹配的记录")

        match_id = matched[0]["id"]
        match_no = matched[0]["match_no"]
        invoice_id = matched[0]["invoice_id"]

        snapshot1 = self.reviewer.create_snapshot(
            description="撤销前快照",
            operator="操作员A"
        )

        revoke_result = self.revoker.revoke_match(
            match_id, "操作员B", "测试撤销备注"
        )
        self.assertTrue(revoke_result["success"])

        revoked_match = self.db.get_match_by_id(match_id)
        self.assertEqual(revoked_match["status"], MATCH_STATUS_REVOKED)

        history = self.db.get_status_history(match_id=match_id)
        statuses = [h["new_status"] for h in history]
        self.assertIn(MATCH_STATUS_REVOKED, statuses)

        invoice = self.db.get_invoice_by_id(invoice_id)
        self.assertEqual(invoice["match_status"], MATCH_STATUS_UNMATCHED)

        snapshot2 = self.reviewer.create_snapshot(
            description="撤销后快照",
            operator="操作员B"
        )

        replay_result = self.reviewer.replay_verify(
            snapshot_no=snapshot1["snapshot_no"]
        )
        self.assertFalse(replay_result["is_consistent"])
        self.assertGreaterEqual(replay_result["status_changed"], 1)

        self.assertEqual(snapshot1["snapshot_no"], snapshot1["snapshot_no"])
        self.assertNotEqual(snapshot1["snapshot_no"], snapshot2["snapshot_no"])

    def test_replay_identifies_status_changes(self):
        """测试回放校验能正确识别状态变更类型"""
        self.importer.import_invoices(self.invoice_csv, "操作员A")
        self.importer.import_payments(self.payment_csv, "操作员A")
        self.matcher.run_auto_matching("操作员A")

        snapshot = self.reviewer.create_snapshot(operator="操作员A")

        pending = self.db.get_matches_by_status(MATCH_STATUS_PENDING)
        if pending:
            self.matcher.confirm_match(
                pending[0]["id"], "操作员A", "确认测试"
            )

        matched = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
        if matched:
            self.revoker.revoke_match(
                matched[0]["id"], "操作员B", "撤销测试"
            )

        result = self.reviewer.replay_verify(snapshot_no=snapshot["snapshot_no"])

        self.assertFalse(result["is_consistent"])

        diff_types = set(d["type"] for d in result["differences"])
        self.assertTrue(
            len(diff_types) > 0,
            "应该检测到至少一种差异类型"
        )


class TestExportConsistency(unittest.TestCase):
    """导出一致性测试"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")

        self.config = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
        )

        self.invoice_csv = os.path.join(self.test_dir, "invoices.csv")
        self.payment_csv = os.path.join(self.test_dir, "payments.csv")
        with open(self.invoice_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_CSV)
        with open(self.payment_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_CSV)

        self.db = Database(self.db_path)
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db)
        self.exporter = ReportExporter(self.config, self.db)
        self.reviewer = ReviewSnapshot(self.db)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_snapshot_export_stable_number(self):
        """测试同一快照多次导出编号不变"""
        self.importer.import_invoices(self.invoice_csv, "操作员A")
        self.importer.import_payments(self.payment_csv, "操作员A")
        self.matcher.run_auto_matching("操作员A")

        snapshot = self.reviewer.create_snapshot(operator="操作员A")
        snapshot_no = snapshot["snapshot_no"]

        for i in range(3):
            snapshot_data = self.reviewer.get_snapshot_for_export(
                snapshot_no=snapshot_no
            )
            self.assertEqual(
                snapshot_data["snapshot_info"]["快照编号"],
                snapshot_no,
                f"第{i+1}次导出快照编号应保持不变"
            )

    def test_json_export_format(self):
        """测试 JSON 导出格式正确性"""
        self.importer.import_invoices(self.invoice_csv, "操作员A")
        self.importer.import_payments(self.payment_csv, "操作员A")
        self.matcher.run_auto_matching("操作员A")

        result = self.exporter.export_full_report("操作员A", format="json")

        self.assertTrue(result["success"])
        self.assertTrue(os.path.exists(result["file_path"]))

        with open(result["file_path"], "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIsInstance(data, dict)
        self.assertIn("概览", data)
        self.assertIn("已匹配", data)
        self.assertIn("待确认", data)
        self.assertIn("未匹配发票", data)
        self.assertIn("未匹配收款", data)
        self.assertIn("状态历史", data)

        for item in data["已匹配"]:
            self.assertIn("匹配编号", item)
            self.assertIn("当前状态", item) if "当前状态" in item else self.assertIn("匹配状态", item)
            self.assertIn("发票号", item)
            self.assertIn("收款号", item)
            self.assertIn("确认人", item)
            self.assertIn("人工备注", item)
            self.assertIn("状态历史", item)

    def test_diff_export_json(self):
        """测试差异报告 JSON 导出"""
        self.importer.import_invoices(self.invoice_csv, "操作员A")
        self.importer.import_payments(self.payment_csv, "操作员A")
        self.matcher.run_auto_matching("操作员A")

        result = self.exporter.export_diff_report("操作员A", format="json")

        self.assertTrue(result["success"])
        self.assertTrue(os.path.exists(result["file_path"]))

        with open(result["file_path"], "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("差异概览", data)
        self.assertIn("未匹配发票", data)
        self.assertIn("未匹配收款", data)


if __name__ == "__main__":
    unittest.main()
