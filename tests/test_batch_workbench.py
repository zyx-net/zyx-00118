import os
import sys
import json
import csv
import tempfile
import unittest
import shutil
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from invoice_reconciler.core.config import Config
from invoice_reconciler.core.database import (
    Database,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_PENDING,
    MATCH_STATUS_EXCEPTION,
    MATCH_STATUS_REVOKED,
)
from invoice_reconciler.core.importer import CSVImporter
from invoice_reconciler.core.matcher import MatchEngine
from invoice_reconciler.core.revoker import Revoker
from invoice_reconciler.core.exporter import ReportExporter
from invoice_reconciler.core.workflow import WorkflowManager
from invoice_reconciler.core.batch_workbench import (
    BatchWorkbench,
    CONFLICT_TYPE_NEW_RECORD,
    CONFLICT_TYPE_STATUS_CHANGE,
    CONFLICT_TYPE_DUPLICATE_PROCESS,
    CONFLICT_TYPE_AMOUNT_CHANGE,
    SESSION_KEY_LAST_BATCH,
    SESSION_KEY_FILTER_OPERATOR,
    SESSION_KEY_FILTER_STATUS,
)


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

SAMPLE_INVOICES_UPDATED_CSV = """invoice_no,invoice_date,customer,amount,status
INV001,2024-01-15,北京科技有限公司,1000.00,正常
INV002,2024-01-16,上海贸易公司,2500.50,作废
INV003,2024-01-17,广州电子厂,3000.00,正常
INV004,2024-01-18,深圳软件公司,1600.00,正常
INV005,2024-01-19,杭州电商平台,800.00,正常
INV006,2024-01-20,武汉科技公司,2000.00,正常
"""

SAMPLE_PAYMENTS_UPDATED_CSV = """payment_no,payment_date,customer,amount
PAY001,2024-01-16,北京科技有限公司,1000.00
PAY002,2024-01-17,上海贸易公司,2500.50
PAY003,2024-01-18,广州电子厂,3000.00
PAY004,2024-01-20,深圳软件公司,1499.99
PAY005,2024-01-21,杭州电商平台,800.00
PAY006,2024-01-22,武汉科技公司,2000.00
"""


class TestBatchWorkbenchSessionState(unittest.TestCase):
    """测试批次工作台会话状态管理 - 重启恢复"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")

        self.config = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
            lock_timeout_seconds=3600,
            admin_users=["admin"],
            enable_lock=True,
        )

        self.invoice_csv = os.path.join(self.test_dir, "invoices.csv")
        self.payment_csv = os.path.join(self.test_dir, "payments.csv")
        with open(self.invoice_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_CSV)
        with open(self.payment_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_CSV)

        self.db = Database(self.db_path)
        self.workflow = WorkflowManager(self.config, self.db)
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db, self.workflow)
        self.workbench = BatchWorkbench(self.config, self.db)

        inv_result = self.importer.import_invoices(self.invoice_csv, "init_user")
        pay_result = self.importer.import_payments(self.payment_csv, "init_user")
        self.inv_batch_id = inv_result["batch_id"]
        self.pay_batch_id = pay_result["batch_id"]

        self.matcher.run_auto_matching("init_user")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_and_get_last_selected_batch(self):
        """测试保存和获取上次选择的批次"""
        self.workbench.save_last_selected_batch(self.inv_batch_id, "test_user")

        last_batch = self.workbench.get_last_selected_batch()
        self.assertIsNotNone(last_batch)
        self.assertEqual(last_batch["batch_id"], self.inv_batch_id)
        self.assertEqual(last_batch["selected_by"], "test_user")
        self.assertIn("file_name", last_batch)
        self.assertIn("file_type", last_batch)

    def test_save_and_get_filters(self):
        """测试保存和获取筛选条件"""
        self.workbench.save_filters(operator="user_a", status=MATCH_STATUS_PENDING)

        filters = self.workbench.get_filters()
        self.assertIsNotNone(filters)
        self.assertEqual(filters["operator"], "user_a")
        self.assertEqual(filters["status"], MATCH_STATUS_PENDING)

    def test_clear_filters(self):
        """测试清除筛选条件"""
        self.workbench.save_filters(operator="user_a", status=MATCH_STATUS_PENDING)

        self.workbench.save_filters(operator=None, status=None)

        filters = self.workbench.get_filters()
        self.assertEqual(filters, {})

    def test_restore_workbench_state(self):
        """测试恢复工作台状态"""
        self.workbench.save_last_selected_batch(self.inv_batch_id, "test_user")
        self.workbench.save_filters(operator="user_a", status=MATCH_STATUS_PENDING)

        del self.workbench
        del self.db

        new_db = Database(self.db_path)
        new_workbench = BatchWorkbench(self.config, new_db)

        restore_result = new_workbench.restore_workbench_state()

        self.assertTrue(restore_result["restored"])
        self.assertIsNotNone(restore_result["last_batch"])
        self.assertEqual(restore_result["last_batch"]["batch_id"], self.inv_batch_id)
        self.assertIsNotNone(restore_result["filters"])
        self.assertEqual(restore_result["filters"]["operator"], "user_a")
        self.assertEqual(restore_result["filters"]["status"], MATCH_STATUS_PENDING)

    def test_clear_workbench_state(self):
        """测试清除工作台状态"""
        self.workbench.save_last_selected_batch(self.inv_batch_id, "test_user")
        self.workbench.save_filters(operator="user_a", status=MATCH_STATUS_PENDING)

        self.workbench.clear_workbench_state()

        restore_result = self.workbench.restore_workbench_state()
        self.assertFalse(restore_result["restored"])
        self.assertIsNone(restore_result["last_batch"])
        self.assertEqual(restore_result["filters"], {})

    def test_no_state_when_nothing_saved(self):
        """测试没有保存状态时返回空"""
        restore_result = self.workbench.restore_workbench_state()
        self.assertFalse(restore_result["restored"])
        self.assertIsNone(restore_result["last_batch"])
        self.assertEqual(restore_result["filters"], {})

    def test_session_state_persistence_across_instances(self):
        """测试会话状态在不同实例间持久化"""
        self.workbench.save_last_selected_batch(self.inv_batch_id, "user_1")
        self.workbench.save_filters(operator="operator_x", status=MATCH_STATUS_EXCEPTION)

        db2 = Database(self.db_path)
        workbench2 = BatchWorkbench(self.config, db2)

        last_batch = workbench2.get_last_selected_batch()
        self.assertEqual(last_batch["batch_id"], self.inv_batch_id)
        self.assertEqual(last_batch["selected_by"], "user_1")

        filters = workbench2.get_filters()
        self.assertEqual(filters["operator"], "operator_x")
        self.assertEqual(filters["status"], MATCH_STATUS_EXCEPTION)


class TestBatchWorkbenchConflictDetection(unittest.TestCase):
    """测试批次冲突检测"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")

        self.config = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
            lock_timeout_seconds=3600,
            admin_users=["admin"],
            enable_lock=True,
        )

        self.invoice_csv = os.path.join(self.test_dir, "invoices.csv")
        self.payment_csv = os.path.join(self.test_dir, "payments.csv")
        self.invoice_updated_csv = os.path.join(self.test_dir, "invoices_updated.csv")
        self.payment_updated_csv = os.path.join(self.test_dir, "payments_updated.csv")

        with open(self.invoice_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_CSV)
        with open(self.payment_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_CSV)
        with open(self.invoice_updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)
        with open(self.payment_updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_UPDATED_CSV)

        self.db = Database(self.config.db_path)
        self.workflow = WorkflowManager(self.config, self.db)
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db, self.workflow)
        self.workbench = BatchWorkbench(self.config, self.db)
        self.revoker = Revoker(self.db, self.config, self.workflow)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_detect_new_records_on_reimport(self):
        """测试重新导入时检测新增记录"""
        self.importer.import_invoices(self.invoice_csv, "user_a")

        result = self.importer.import_invoices(self.invoice_updated_csv, "user_b")

        self.assertIn("conflicts", result)
        new_record_conflicts = [c for c in result["conflicts"]
                                if c["conflict_type"] == CONFLICT_TYPE_NEW_RECORD]

        self.assertTrue(any(c["record_no"] == "INV006" for c in new_record_conflicts))

        db_conflicts = self.db.get_batch_conflicts(conflict_type=CONFLICT_TYPE_NEW_RECORD)
        self.assertTrue(any(c["record_no"] == "INV006" for c in db_conflicts))

    def test_detect_amount_change_on_reimport(self):
        """测试重新导入时检测金额变更"""
        self.importer.import_invoices(self.invoice_csv, "user_a")

        result = self.importer.import_invoices(self.invoice_updated_csv, "user_b")

        amount_conflicts = [c for c in result["conflicts"]
                            if c["conflict_type"] == CONFLICT_TYPE_AMOUNT_CHANGE]

        self.assertTrue(any(c["record_no"] == "INV004" for c in amount_conflicts))

        inv004_conflict = next(c for c in amount_conflicts if c["record_no"] == "INV004")
        self.assertAlmostEqual(inv004_conflict["old_amount"], 1500.00)
        self.assertAlmostEqual(inv004_conflict["new_amount"], 1600.00)

    def test_detect_status_change_on_reimport(self):
        """测试重新导入时检测状态变更"""
        self.importer.import_invoices(self.invoice_csv, "user_a")

        result = self.importer.import_invoices(self.invoice_updated_csv, "user_b")

        status_conflicts = [c for c in result["conflicts"]
                            if c["conflict_type"] == CONFLICT_TYPE_STATUS_CHANGE]

        self.assertTrue(any(c["record_no"] == "INV002" for c in status_conflicts))

        inv002_conflict = next(c for c in status_conflicts if c["record_no"] == "INV002")
        self.assertEqual(inv002_conflict["old_status"], "正常")
        self.assertEqual(inv002_conflict["new_status"], "作废")

    def test_detect_duplicate_processing(self):
        """测试检测重复处理"""
        inv_result = self.importer.import_invoices(self.invoice_csv, "user_a")
        pay_result = self.importer.import_payments(self.payment_csv, "user_a")

        self.matcher.run_auto_matching("user_a")

        matches = self.db.get_matches_by_batch(inv_result["batch_id"])
        self.assertGreater(len(matches), 0, "Auto-matching should create at least one match")

        match_id = matches[0]["id"]
        current_status = matches[0]["status"]

        if current_status == MATCH_STATUS_PENDING:
            self.matcher.confirm_match(match_id, "user_a", "确认匹配")

        with self.db._get_conn() as conn:
            conn.execute(
                "UPDATE matches SET operator = ? WHERE id = ?",
                ("user_a", match_id)
            )
            conn.commit()

        result = self.importer.import_invoices(self.invoice_updated_csv, "user_b")

        duplicate_conflicts = [c for c in result.get("conflicts", [])
                               if c["conflict_type"] == CONFLICT_TYPE_DUPLICATE_PROCESS]

        for c in duplicate_conflicts:
            self.assertEqual(c["old_operator"], "user_a")
            self.assertEqual(c["new_operator"], "user_b")
            self.assertIn("已被", c["conflict_reason"])
            self.assertIn("重新处理", c["conflict_reason"])

    def test_conflict_reasons_are_logged(self):
        """测试冲突原因被正确记录"""
        self.importer.import_invoices(self.invoice_csv, "user_a")

        result = self.importer.import_invoices(self.invoice_updated_csv, "user_b")

        for c in result["conflicts"]:
            self.assertIsNotNone(c["conflict_reason"])
            self.assertGreater(len(c["conflict_reason"]), 0)

        db_conflicts = self.db.get_batch_conflicts()
        for c in db_conflicts:
            self.assertIsNotNone(c["conflict_reason"])
            self.assertIsNotNone(c["detected_at"])

    def test_conflict_detection_returns_all_types(self):
        """测试冲突检测返回所有类型的冲突"""
        self.importer.import_invoices(self.invoice_csv, "user_a")

        result = self.importer.import_invoices(self.invoice_updated_csv, "user_b")

        conflict_types = {c["conflict_type"] for c in result["conflicts"]}

        self.assertIn(CONFLICT_TYPE_NEW_RECORD, conflict_types)
        self.assertIn(CONFLICT_TYPE_AMOUNT_CHANGE, conflict_types)
        self.assertIn(CONFLICT_TYPE_STATUS_CHANGE, conflict_types)

        type_labels = {
            CONFLICT_TYPE_NEW_RECORD: "新增记录",
            CONFLICT_TYPE_STATUS_CHANGE: "状态冲突",
            CONFLICT_TYPE_DUPLICATE_PROCESS: "重复处理",
            CONFLICT_TYPE_AMOUNT_CHANGE: "金额变更",
        }
        for c in result["conflicts"]:
            self.assertIn(c["conflict_type"], type_labels)


class TestBatchWorkbenchExportStability(unittest.TestCase):
    """测试导出字段稳定性"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")

        self.config = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
            lock_timeout_seconds=3600,
            admin_users=["admin"],
            enable_lock=True,
        )

        self.invoice_csv = os.path.join(self.test_dir, "invoices.csv")
        self.payment_csv = os.path.join(self.test_dir, "payments.csv")
        self.invoice_updated_csv = os.path.join(self.test_dir, "invoices_updated.csv")

        with open(self.invoice_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_CSV)
        with open(self.payment_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_CSV)
        with open(self.invoice_updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)

        self.db = Database(self.db_path)
        self.workflow = WorkflowManager(self.config, self.db)
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db, self.workflow)
        self.exporter = ReportExporter(self.config, self.db)
        self.workbench = BatchWorkbench(self.config, self.db)

        inv_result = self.importer.import_invoices(self.invoice_csv, "init_user")
        pay_result = self.importer.import_payments(self.payment_csv, "init_user")
        self.inv_batch_id = inv_result["batch_id"]
        self.pay_batch_id = pay_result["batch_id"]

        self.matcher.run_auto_matching("init_user")

        updated_result = self.importer.import_invoices(self.invoice_updated_csv, "updater")
        self.updated_batch_id = updated_result["batch_id"]

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_export_batch_progress_json_fields_stable(self):
        """测试JSON格式批次进度导出字段稳定"""
        result = self.exporter.export_batch_progress(self.inv_batch_id, "test_user", format="json")

        self.assertTrue(result["success"])

        with open(result["file_path"], "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("batch_info", data)
        self.assertIn("progress", data)
        self.assertIn("matches", data)
        self.assertIn("conflicts", data)

        expected_batch_fields = [
            "batch_id", "file_type", "file_name", "operator",
            "imported_at", "total_rows", "success_rows", "failed_rows"
        ]
        for field in expected_batch_fields:
            self.assertIn(field, data["batch_info"], f"缺少字段: {field}")

        expected_progress_fields = [
            "total_matches", "pending_matches", "confirmed_matches",
            "exception_matches", "revoked_matches", "unmatched_invoices",
            "unmatched_payments", "progress_percent", "conflict_count",
            "exported_at", "exported_by"
        ]
        for field in expected_progress_fields:
            self.assertIn(field, data["progress"], f"缺少进度字段: {field}")

        if data["matches"]:
            expected_match_fields = [
                "匹配ID", "匹配编号", "匹配类型", "状态", "发票号",
                "发票金额", "收款号", "收款金额", "匹配方式", "处理人",
                "操作时间"
            ]
            for field in expected_match_fields:
                self.assertIn(field, data["matches"][0], f"缺少匹配字段: {field}")

        if data["conflicts"]:
            expected_conflict_fields = [
                "冲突ID", "批次ID", "来源文件", "冲突类型", "记录类型",
                "记录编号", "原状态", "新状态", "原操作人", "新操作人",
                "原金额", "新金额", "冲突原因", "检测时间"
            ]
            for field in expected_conflict_fields:
                self.assertIn(field, data["conflicts"][0], f"缺少冲突字段: {field}")

    def test_export_batch_progress_csv_fields_stable(self):
        """测试CSV格式批次进度导出字段稳定"""
        result = self.exporter.export_batch_progress(self.inv_batch_id, "test_user", format="csv")

        self.assertTrue(result["success"])

        dir_path = result["file_path"]
        self.assertTrue(os.path.isdir(dir_path))

        summary_path = os.path.join(dir_path, "批次摘要.csv")
        matches_path = os.path.join(dir_path, "匹配明细.csv")
        conflicts_path = os.path.join(dir_path, "批次冲突.csv")

        self.assertTrue(os.path.exists(summary_path), f"Summary file not found at {summary_path}")
        self.assertTrue(os.path.exists(matches_path), f"Matches file not found at {matches_path}")
        self.assertTrue(os.path.exists(conflicts_path), f"Conflicts file not found at {conflicts_path}")

        with open(summary_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames
            self.assertIsNotNone(headers)
            if "项目" in headers:
                self.assertIn("项目", headers)
                self.assertIn("值", headers)

        with open(matches_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames
            self.assertIsNotNone(headers)
            rows = list(reader)
            if "匹配ID" in headers:
                expected_headers = [
                    "匹配ID", "匹配编号", "匹配类型", "状态", "发票号",
                    "发票金额", "收款号", "收款金额", "匹配方式", "处理人",
                    "操作时间"
                ]
                for h in expected_headers:
                    self.assertIn(h, headers)

        with open(conflicts_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames
            self.assertIsNotNone(headers)
            if "冲突ID" in headers:
                expected_headers = [
                    "冲突ID", "批次ID", "来源文件", "冲突类型", "记录类型",
                    "记录编号", "原状态", "新状态", "原操作人", "新操作人",
                    "原金额", "新金额", "冲突原因", "检测时间"
                ]
                for h in expected_headers:
                    self.assertIn(h, headers)

    def test_export_full_report_includes_conflicts(self):
        """测试完整报表导出包含冲突数据"""
        result = self.exporter.export_full_report("test_user", format="json")

        self.assertTrue(result["success"])

        with open(result["file_path"], "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("批次冲突", data)
        self.assertIn("conflicts_count", result["summary"])

    def test_batch_progress_export_consistent_across_formats(self):
        """测试不同格式导出内容一致"""
        json_result = self.exporter.export_batch_progress(
            self.inv_batch_id, "test_user", format="json"
        )

        with open(json_result["file_path"], "r", encoding="utf-8") as f:
            json_data = json.load(f)

        xlsx_result = self.exporter.export_batch_progress(
            self.inv_batch_id, "test_user", format="xlsx"
        )

        self.assertTrue(xlsx_result["success"])
        self.assertEqual(xlsx_result["batch_id"], self.inv_batch_id)
        self.assertEqual(xlsx_result["summary"]["total_matches"],
                         json_data["progress"]["total_matches"])
        self.assertEqual(xlsx_result["summary"]["conflict_count"],
                         json_data["progress"]["conflict_count"])


class TestBatchWorkbenchUndoConsistency(unittest.TestCase):
    """测试撤销后再次导出结果一致"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")

        self.config = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
            lock_timeout_seconds=3600,
            admin_users=["admin"],
            enable_lock=False,
        )

        self.invoice_csv = os.path.join(self.test_dir, "invoices.csv")
        self.payment_csv = os.path.join(self.test_dir, "payments.csv")

        with open(self.invoice_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_CSV)
        with open(self.payment_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_CSV)

        self.db = Database(self.db_path)
        self.workflow = WorkflowManager(self.config, self.db)
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db, self.workflow)
        self.revoker = Revoker(self.db, self.config, self.workflow)
        self.exporter = ReportExporter(self.config, self.db)
        self.workbench = BatchWorkbench(self.config, self.db)

        inv_result = self.importer.import_invoices(self.invoice_csv, "init_user")
        self.importer.import_payments(self.payment_csv, "init_user")
        self.inv_batch_id = inv_result["batch_id"]

        self.matcher.run_auto_matching("init_user")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_export_before_and_after_undo(self):
        """测试撤销前后导出的字段结构保持一致"""
        all_matches = self.db.get_matches_by_batch(self.inv_batch_id)
        self.assertGreater(len(all_matches), 0)

        match = None
        for m in all_matches:
            if m["status"] == MATCH_STATUS_MATCHED:
                match = m
                break
        if not match:
            match = all_matches[0]
            if match["status"] == MATCH_STATUS_PENDING:
                result = self.matcher.confirm_match(
                    match["id"], "user_a", "确认匹配"
                )
                self.assertTrue(result["success"])

        match_id = match["id"]

        export1 = self.exporter.export_batch_progress(
            self.inv_batch_id, "test_user", format="json"
        )
        self.assertTrue(export1["success"])

        with open(export1["file_path"], "r", encoding="utf-8") as f:
            data1 = json.load(f)

        revoked1 = [m for m in data1["matches"] if m["状态"] == "已撤销"]
        matched1 = [m for m in data1["matches"] if m["状态"] == "已匹配"]
        pending1 = [m for m in data1["matches"] if m["状态"] == "待确认"]

        self.assertEqual(len(revoked1), 0)
        self.assertGreater(len(matched1), 0)

        revoke_result = self.revoker.revoke_match(match_id, "user_a", "撤销确认")
        self.assertTrue(revoke_result["success"])

        export2 = self.exporter.export_batch_progress(
            self.inv_batch_id, "test_user", format="json"
        )
        self.assertTrue(export2["success"])

        with open(export2["file_path"], "r", encoding="utf-8") as f:
            data2 = json.load(f)

        revoked2 = [m for m in data2["matches"] if m["状态"] == "已撤销"]
        matched2 = [m for m in data2["matches"] if m["状态"] == "已匹配"]
        pending2 = [m for m in data2["matches"] if m["状态"] == "待确认"]

        self.assertEqual(len(revoked2), 1)
        self.assertEqual(len(matched2), len(matched1) - 1)
        self.assertEqual(len(pending2), len(pending1))

        for key in data1["progress"]:
            self.assertIn(key, data2["progress"])

        self.assertEqual(len(data1["matches"]), len(data2["matches"]))
        for i, m1 in enumerate(data1["matches"]):
            m2 = data2["matches"][i]
            for key in m1:
                self.assertIn(key, m2)

    def test_undo_then_reconfirm_export_consistent(self):
        """测试匹配生命周期内导出结构一致（待确认->已确认->已撤销）"""
        all_matches = self.db.get_matches_by_batch(self.inv_batch_id)
        self.assertGreater(len(all_matches), 0)

        pending_match = None
        for m in all_matches:
            if m["status"] == MATCH_STATUS_PENDING:
                pending_match = m
                break

        export1 = self.exporter.export_batch_progress(
            self.inv_batch_id, "test_user", format="json"
        )
        with open(export1["file_path"], "r", encoding="utf-8") as f:
            data1 = json.load(f)

        progress_keys = set(data1["progress"].keys())
        match_keys = set(data1["matches"][0].keys()) if data1["matches"] else set()

        if pending_match:
            self.matcher.confirm_match(
                pending_match["id"], "user_a", "确认匹配"
            )

            export2 = self.exporter.export_batch_progress(
                self.inv_batch_id, "test_user", format="json"
            )
            with open(export2["file_path"], "r", encoding="utf-8") as f:
                data2 = json.load(f)

            self.assertEqual(progress_keys, set(data2["progress"].keys()))
            if data2["matches"]:
                self.assertEqual(match_keys, set(data2["matches"][0].keys()))

            self.revoker.revoke_match(pending_match["id"], "user_a", "撤销确认")

        export3 = self.exporter.export_batch_progress(
            self.inv_batch_id, "test_user", format="json"
        )
        with open(export3["file_path"], "r", encoding="utf-8") as f:
            data3 = json.load(f)

        self.assertEqual(progress_keys, set(data3["progress"].keys()))
        if data3["matches"]:
            self.assertEqual(match_keys, set(data3["matches"][0].keys()))

        self.assertEqual(len(data1["matches"]), len(data3["matches"]))

    def test_revoke_multiple_times_export_stable(self):
        """测试多次撤销导出结构稳定"""
        all_matches = self.db.get_matches_by_batch(self.inv_batch_id)
        self.assertGreater(len(all_matches), 0)

        match_ids = []
        for m in all_matches:
            if m["status"] == MATCH_STATUS_MATCHED:
                match_ids.append(m["id"])
            if len(match_ids) >= 3:
                break

        if len(match_ids) < 3:
            for m in all_matches:
                if m["status"] == MATCH_STATUS_PENDING and len(match_ids) < 3:
                    self.matcher.confirm_match(
                        m["id"], "user_a", "确认匹配"
                    )
                    match_ids.append(m["id"])

        self.assertGreaterEqual(len(match_ids), 1, "Need at least 1 match to test")
        num_revokes = len(match_ids)

        export1 = self.exporter.export_batch_progress(
            self.inv_batch_id, "test_user", format="json"
        )
        with open(export1["file_path"], "r", encoding="utf-8") as f:
            data1 = json.load(f)

        for mid in match_ids:
            self.revoker.revoke_match(mid, "user_a", "撤销确认")

        export2 = self.exporter.export_batch_progress(
            self.inv_batch_id, "test_user", format="json"
        )
        with open(export2["file_path"], "r", encoding="utf-8") as f:
            data2 = json.load(f)

        self.assertEqual(data1["progress"]["confirmed_matches"] - num_revokes,
                         data2["progress"]["confirmed_matches"])
        self.assertEqual(data1["progress"]["revoked_matches"] + num_revokes,
                         data2["progress"]["revoked_matches"])

        for m in data2["matches"]:
            self.assertIn("匹配ID", m)
            self.assertIn("状态", m)
            self.assertIn("匹配编号", m)


class TestBatchWorkbenchSummary(unittest.TestCase):
    """测试批次工作台摘要视图"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")

        self.config = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
            lock_timeout_seconds=3600,
            admin_users=["admin"],
            enable_lock=True,
        )

        self.invoice_csv = os.path.join(self.test_dir, "invoices.csv")
        self.payment_csv = os.path.join(self.test_dir, "payments.csv")

        with open(self.invoice_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_CSV)
        with open(self.payment_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_CSV)

        self.db = Database(self.db_path)
        self.workflow = WorkflowManager(self.config, self.db)
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db, self.workflow)
        self.workbench = BatchWorkbench(self.config, self.db)

        inv_result = self.importer.import_invoices(self.invoice_csv, "init_user")
        self.importer.import_payments(self.payment_csv, "init_user")
        self.inv_batch_id = inv_result["batch_id"]

        self.matcher.run_auto_matching("init_user")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_get_batch_workbench_summary(self):
        """测试获取批次工作台摘要"""
        result = self.workbench.get_batch_workbench_summary()

        self.assertTrue(result["success"])
        self.assertGreater(result["total_batches"], 0)

        for batch in result["batches"]:
            self.assertIn("batch_id", batch)
            self.assertIn("file_name", batch)
            self.assertIn("file_type", batch)
            self.assertIn("operator", batch)
            self.assertIn("imported_at", batch)
            self.assertIn("progress_percent", batch)
            self.assertIn("pending_matches", batch)
            self.assertIn("confirmed_matches", batch)
            self.assertIn("exception_matches", batch)
            self.assertIn("revoked_matches", batch)
            self.assertIn("has_unfinished", batch)
            self.assertIsInstance(batch["has_unfinished"], bool)

    def test_get_specific_batch_summary(self):
        """测试获取指定批次的摘要"""
        result = self.workbench.get_batch_workbench_summary(batch_id=self.inv_batch_id)

        self.assertTrue(result["success"])
        self.assertEqual(len(result["batches"]), 1)
        self.assertEqual(result["batches"][0]["batch_id"], self.inv_batch_id)

    def test_get_unfinished_reminder(self):
        """测试获取未完成项提醒"""
        result = self.workbench.get_unfinished_reminder()

        self.assertTrue(result["success"])
        self.assertIsInstance(result["total_unfinished"], int)
        self.assertIsInstance(result["batch_count"], int)
        self.assertIsInstance(result["reminders"], list)

    def test_get_batch_matches_with_filters(self):
        """测试按处理人过滤获取匹配"""
        matches = self.db.get_matches_by_batch(self.inv_batch_id)
        if matches:
            mid = matches[0]["id"]
            with self.db._get_conn() as conn:
                conn.execute(
                    "UPDATE matches SET operator = ? WHERE id = ?",
                    ("test_filter_user", mid)
                )
                conn.commit()

        filtered = self.workbench.get_batch_matches(
            self.inv_batch_id,
            operator="test_filter_user"
        )

        if matches:
            self.assertGreater(len(filtered), 0)
            for m in filtered:
                self.assertEqual(m.get("operator"), "test_filter_user")

    def test_get_batch_matches_with_status_filter(self):
        """测试按状态过滤获取匹配"""
        filtered = self.workbench.get_batch_matches(
            self.inv_batch_id,
            status=MATCH_STATUS_PENDING
        )

        for m in filtered:
            self.assertEqual(m["status"], MATCH_STATUS_PENDING)

    def test_progress_percent_calculation(self):
        """测试进度百分比计算"""
        result = self.workbench.get_batch_workbench_summary(batch_id=self.inv_batch_id)
        batch = result["batches"][0]

        total = batch["total_tasks"]
        if total > 0:
            completed = batch["confirmed_matches"] + batch["matched_invoices"] + batch["matched_payments"]
            expected_progress = (completed / total) * 100 if total > 0 else 100
            self.assertAlmostEqual(batch["progress_percent"], expected_progress, places=1)


if __name__ == "__main__":
    unittest.main()
