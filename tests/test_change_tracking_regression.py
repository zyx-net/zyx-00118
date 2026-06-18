import os
import sys
import io
import json
import csv
import tempfile
import unittest
import shutil
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from click.testing import CliRunner
    from click.testing import Result as ClickResult
    HAS_CLICK = True
except ImportError:
    HAS_CLICK = False

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
    SESSION_KEY_LAST_BATCH,
    SESSION_KEY_FILTER_OPERATOR,
)
from invoice_reconciler.core.change_tracker import (
    ChangeTracker,
    CHANGE_TYPE_NEW_RECORD,
    CHANGE_TYPE_STATUS_CHANGE,
    CHANGE_TYPE_AMOUNT_CHANGE,
    CHANGE_TYPE_KEY_FIELD_CHANGE,
    CHANGE_TYPE_DUPLICATE_PROCESS,
    CHANGE_TYPE_LABELS,
    IMPACT_TYPE_NONE,
    IMPACT_TYPE_PENDING,
    IMPACT_TYPE_CONFIRMED,
    IMPACT_TYPE_REVOKED,
    IMPACT_TYPE_WARNING,
    IMPACT_TYPE_CRITICAL,
    IMPACT_TYPE_LABELS,
    PROCESSING_STATUS_PENDING,
    PROCESSING_STATUS_REVIEWED,
    PROCESSING_STATUS_RESOLVED,
    PROCESSING_STATUS_IGNORED,
    PROCESSING_STATUS_LABELS,
)


SAMPLE_INVOICES_V1 = """invoice_no,invoice_date,customer,amount,status
INV001,2024-01-15,北京科技有限公司,1000.00,正常
INV002,2024-01-16,上海贸易公司,2500.50,正常
INV003,2024-01-17,广州电子厂,3000.00,正常
INV004,2024-01-18,深圳软件公司,1500.00,正常
INV005,2024-01-19,杭州电商平台,800.00,正常
"""

SAMPLE_PAYMENTS_V1 = """payment_no,payment_date,customer,amount
PAY001,2024-01-16,北京科技有限公司,1000.00
PAY002,2024-01-17,上海贸易公司,2500.50
PAY003,2024-01-18,广州电子厂,3000.00
PAY004,2024-01-20,深圳软件公司,1499.99
PAY005,2024-01-21,杭州电商平台,800.00
"""

SAMPLE_INVOICES_V2 = """invoice_no,invoice_date,customer,amount,status
INV001,2024-01-15,北京科技有限公司,1000.00,正常
INV002,2024-01-16,上海贸易公司,2500.50,作废
INV003,2024-01-17,广州电子有限公司,3000.00,正常
INV004,2024-01-18,深圳软件公司,1600.00,正常
INV005,2024-01-19,杭州电商平台,800.00,正常
INV006,2024-01-20,武汉科技公司,2000.00,正常
"""

SAMPLE_PAYMENTS_V2 = """payment_no,payment_date,customer,amount
PAY001,2024-01-16,北京科技有限公司,1000.00
PAY002,2024-01-17,上海贸易公司,2500.50
PAY003,2024-01-18,广州电子有限公司,3000.00
PAY004,2024-01-20,深圳软件公司,1499.99
PAY005,2024-01-21,杭州电商平台,800.00
PAY006,2024-01-22,武汉科技公司,2000.00
"""


class TestChangeTrackerCore(unittest.TestCase):
    """测试变更追踪核心功能"""

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

        self.invoice_v1 = os.path.join(self.test_dir, "invoices_v1.csv")
        self.payment_v1 = os.path.join(self.test_dir, "payments_v1.csv")
        self.invoice_v2 = os.path.join(self.test_dir, "invoices_v2.csv")
        self.payment_v2 = os.path.join(self.test_dir, "payments_v2.csv")

        with open(self.invoice_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V1)
        with open(self.payment_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_V1)
        with open(self.invoice_v2, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V2)
        with open(self.payment_v2, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_V2)

        self.db = Database(self.db_path)
        self.workflow = WorkflowManager(self.config, self.db)
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db, self.workflow)
        self.tracker = ChangeTracker(self.config, self.db)
        self.workbench = BatchWorkbench(self.config, self.db)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_detect_new_records(self):
        """测试检测新增记录"""
        self.importer.import_invoices(self.invoice_v1, "operator_a")
        result = self.importer.import_invoices(self.invoice_v2, "operator_b")

        self.assertIn("change_count", result)
        self.assertGreater(result["change_count"], 0)

        changes = self.db.get_batch_change_logs(batch_id=result["batch_id"])
        new_records = [c for c in changes if c["change_type"] == CHANGE_TYPE_NEW_RECORD]

        self.assertTrue(any(c["record_no"] == "INV006" for c in new_records))

        inv006 = next(c for c in new_records if c["record_no"] == "INV006")
        self.assertIsNotNone(inv006["after_summary"])
        self.assertIn("INV006", inv006["after_summary"])
        self.assertIsNone(inv006["before_summary"])

    def test_detect_status_changes(self):
        """测试检测状态变更"""
        self.importer.import_invoices(self.invoice_v1, "operator_a")
        result = self.importer.import_invoices(self.invoice_v2, "operator_b")

        changes = self.db.get_batch_change_logs(batch_id=result["batch_id"])
        status_changes = [c for c in changes if c["change_type"] == CHANGE_TYPE_STATUS_CHANGE]

        self.assertTrue(any(c["record_no"] == "INV002" for c in status_changes))

        inv002 = next(c for c in status_changes if c["record_no"] == "INV002")
        self.assertEqual(inv002["old_value"], "正常")
        self.assertEqual(inv002["new_value"], "作废")
        self.assertIsNotNone(inv002["before_summary"])
        self.assertIsNotNone(inv002["after_summary"])

    def test_detect_amount_changes(self):
        """测试检测金额变更"""
        self.importer.import_invoices(self.invoice_v1, "operator_a")
        result = self.importer.import_invoices(self.invoice_v2, "operator_b")

        changes = self.db.get_batch_change_logs(batch_id=result["batch_id"])
        amount_changes = [c for c in changes if c["change_type"] == CHANGE_TYPE_AMOUNT_CHANGE]

        self.assertTrue(any(c["record_no"] == "INV004" for c in amount_changes))

        inv004 = next(c for c in amount_changes if c["record_no"] == "INV004")
        self.assertEqual(inv004["old_value"], "1500.00")
        self.assertEqual(inv004["new_value"], "1600.00")

    def test_detect_key_field_changes(self):
        """测试检测关键字段变更（客户名称等）"""
        self.importer.import_invoices(self.invoice_v1, "operator_a")
        result = self.importer.import_invoices(self.invoice_v2, "operator_b")

        changes = self.db.get_batch_change_logs(batch_id=result["batch_id"])
        key_changes = [c for c in changes if c["change_type"] == CHANGE_TYPE_KEY_FIELD_CHANGE]

        self.assertTrue(any(c["record_no"] == "INV003" for c in key_changes))

        inv003 = next(c for c in key_changes if c["record_no"] == "INV003")
        self.assertEqual(inv003["field_name"], "customer")
        self.assertEqual(inv003["old_value"], "广州电子厂")
        self.assertEqual(inv003["new_value"], "广州电子有限公司")

    def test_all_change_types_detected(self):
        """测试所有变更类型都能被检测到"""
        self.importer.import_invoices(self.invoice_v1, "operator_a")
        self.importer.import_payments(self.payment_v1, "operator_a")
        self.matcher.run_auto_matching("operator_a")

        matches = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
        for m in matches:
            self.db.confirm_match(m["id"], "operator_a", "确认匹配")

        result = self.importer.import_invoices(self.invoice_v2, "operator_b")

        changes = self.db.get_batch_change_logs(batch_id=result["batch_id"])
        change_types = {c["change_type"] for c in changes}

        self.assertIn(CHANGE_TYPE_NEW_RECORD, change_types)
        self.assertIn(CHANGE_TYPE_STATUS_CHANGE, change_types)
        self.assertIn(CHANGE_TYPE_AMOUNT_CHANGE, change_types)
        self.assertIn(CHANGE_TYPE_KEY_FIELD_CHANGE, change_types)


class TestImpactAnalysis(unittest.TestCase):
    """测试影响分析功能"""

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

        self.invoice_v1 = os.path.join(self.test_dir, "invoices_v1.csv")
        self.payment_v1 = os.path.join(self.test_dir, "payments_v1.csv")
        self.invoice_v2 = os.path.join(self.test_dir, "invoices_v2.csv")

        with open(self.invoice_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V1)
        with open(self.payment_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_V1)
        with open(self.invoice_v2, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V2)

        self.db = Database(self.db_path)
        self.workflow = WorkflowManager(self.config, self.db)
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db, self.workflow)
        self.tracker = ChangeTracker(self.config, self.db)
        self.revoker = Revoker(self.db, self.config, self.workflow)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_impact_on_confirmed_matches(self):
        """测试已确认匹配的影响分析"""
        self.importer.import_invoices(self.invoice_v1, "operator_a")
        self.importer.import_payments(self.payment_v1, "operator_a")
        self.matcher.run_auto_matching("operator_a")

        matches = self.db.get_matches_by_status()
        confirmed_ids = []
        for m in matches:
            if m["status"] == MATCH_STATUS_PENDING:
                self.matcher.confirm_match(m["id"], "operator_a", "确认匹配")
                confirmed_ids.append(m["id"])
            elif m["status"] == MATCH_STATUS_MATCHED:
                confirmed_ids.append(m["id"])

        self.assertGreater(len(confirmed_ids), 0, "应有已确认的匹配")

        result = self.importer.import_invoices(self.invoice_v2, "operator_b")

        changes = self.db.get_batch_change_logs(batch_id=result["batch_id"])
        amount_changes = [c for c in changes
                         if c["change_type"] == CHANGE_TYPE_AMOUNT_CHANGE]

        if amount_changes:
            for c in amount_changes:
                self.assertIsNotNone(c["impact_type"])
                self.assertIsNotNone(c["impact_details"])
                if c["record_no"] == "INV004":
                    self.assertIn(c["impact_type"],
                                  [IMPACT_TYPE_CONFIRMED, IMPACT_TYPE_CRITICAL])
                    self.assertIsNotNone(c["impacted_match_ids"])
                    self.assertGreater(len(c["impacted_match_ids"]), 0)

    def test_impact_on_pending_matches(self):
        """测试待确认匹配的影响分析"""
        self.importer.import_invoices(self.invoice_v1, "operator_a")
        self.importer.import_payments(self.payment_v1, "operator_a")
        self.matcher.run_auto_matching("operator_a")

        matches = self.db.get_matches_by_status()
        pending_count = 0
        for m in matches:
            if m["status"] == MATCH_STATUS_PENDING:
                pending_count += 1
            elif m["status"] == MATCH_STATUS_MATCHED:
                self.db.confirm_match(m["id"], "operator_a", "确认匹配")

        pending_matches = self.db.get_matches_by_status(MATCH_STATUS_PENDING)
        if pending_count == 0 and len(pending_matches) == 0:
            all_matches = self.db.get_matches_by_status()
            self.assertGreater(len(all_matches), 0, "至少应有一些匹配")
        else:
            self.assertGreater(pending_count + len(pending_matches), 0, "应有匹配存在")

        result = self.importer.import_invoices(self.invoice_v2, "operator_b")

        changes = self.db.get_batch_change_logs(batch_id=result["batch_id"])

        for c in changes:
            self.assertIsNotNone(c["impact_type"])
            self.assertIsNotNone(c["impact_details"])

        impact_summary = result.get("impact_summary", {})
        self.assertIn(IMPACT_TYPE_PENDING, impact_summary)
        self.assertIn(IMPACT_TYPE_CONFIRMED, impact_summary)

    def test_impact_on_revoked_matches(self):
        """测试已撤销匹配的影响分析"""
        self.importer.import_invoices(self.invoice_v1, "operator_a")
        self.importer.import_payments(self.payment_v1, "operator_a")
        self.matcher.run_auto_matching("operator_a")

        matches = self.db.get_matches_by_status()
        revoked_count = 0
        for m in matches:
            if m["status"] == MATCH_STATUS_MATCHED:
                self.revoker.revoke_match(m["id"], "operator_a", "撤销测试")
                revoked_count += 1
            elif m["status"] == MATCH_STATUS_PENDING:
                self.matcher.confirm_match(m["id"], "operator_a", "确认后撤销")
                self.revoker.revoke_match(m["id"], "operator_a", "撤销测试")
                revoked_count += 1

        self.assertGreater(revoked_count, 0, "应有已撤销的匹配")

        result = self.importer.import_invoices(self.invoice_v2, "operator_b")
        impact_summary = result.get("impact_summary", {})

        self.assertIn(IMPACT_TYPE_REVOKED, impact_summary)

    def test_impact_summary_in_import_result(self):
        """测试导入结果包含影响统计"""
        self.importer.import_invoices(self.invoice_v1, "operator_a")
        self.importer.import_payments(self.payment_v1, "operator_a")
        self.matcher.run_auto_matching("operator_a")

        result = self.importer.import_invoices(self.invoice_v2, "operator_b")

        self.assertIn("impact_summary", result)
        impact_summary = result["impact_summary"]

        for it in [IMPACT_TYPE_CRITICAL, IMPACT_TYPE_CONFIRMED,
                   IMPACT_TYPE_PENDING, IMPACT_TYPE_REVOKED,
                   IMPACT_TYPE_WARNING, IMPACT_TYPE_NONE]:
            self.assertIn(it, impact_summary)


class TestChangeLogExport(unittest.TestCase):
    """测试变更日志导出功能"""

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

        self.invoice_v1 = os.path.join(self.test_dir, "invoices_v1.csv")
        self.payment_v1 = os.path.join(self.test_dir, "payments_v1.csv")
        self.invoice_v2 = os.path.join(self.test_dir, "invoices_v2.csv")

        with open(self.invoice_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V1)
        with open(self.payment_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_V1)
        with open(self.invoice_v2, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V2)

        self.db = Database(self.db_path)
        self.workflow = WorkflowManager(self.config, self.db)
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db, self.workflow)
        self.tracker = ChangeTracker(self.config, self.db)
        self.workbench = BatchWorkbench(self.config, self.db)

        self.importer.import_invoices(self.invoice_v1, "operator_a")
        self.importer.import_payments(self.payment_v1, "operator_a")
        self.matcher.run_auto_matching("operator_a")

        result = self.importer.import_invoices(self.invoice_v2, "operator_b")
        self.batch_id = result["batch_id"]

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_export_json_format(self):
        """测试JSON格式导出"""
        result = self.tracker.export_change_logs(
            self.batch_id, "export_user", format="json"
        )

        self.assertTrue(result["success"])
        self.assertTrue(os.path.exists(result["file_path"]))
        self.assertEqual(result["format"], "json")

        with open(result["file_path"], "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("export_info", data)
        self.assertIn("summary", data)
        self.assertIn("change_logs", data)

        export_info = data["export_info"]
        self.assertEqual(export_info["batch_id"], self.batch_id)
        self.assertEqual(export_info["exported_by"], "export_user")
        self.assertIn("exported_at", export_info)
        self.assertIn("imported_at", export_info)
        self.assertIn("imported_by", export_info)

        summary = data["summary"]
        self.assertIn("by_type", summary)
        self.assertIn("by_impact", summary)
        self.assertIn("by_status", summary)

        change_logs = data["change_logs"]
        self.assertGreater(len(change_logs), 0)

        required_fields = [
            "日志ID", "批次ID", "来源文件", "变更类型", "记录类型", "记录编号",
            "变更字段", "原值", "新值", "变更摘要", "变更前摘要", "变更后摘要",
            "影响类型", "影响详情", "影响的匹配ID", "处理状态",
            "操作者", "检测时间", "处理时间", "处理人", "备注",
        ]
        for log in change_logs:
            for field in required_fields:
                self.assertIn(field, log, f"缺少字段: {field}")

    def test_export_csv_format(self):
        """测试CSV格式导出"""
        result = self.tracker.export_change_logs(
            self.batch_id, "export_user", format="csv"
        )

        self.assertTrue(result["success"])
        self.assertTrue(os.path.isdir(result["file_path"]))
        self.assertEqual(result["format"], "csv")
        self.assertEqual(len(result["files"]), 2)

        summary_path = result["files"][0]
        logs_path = result["files"][1]

        self.assertTrue(os.path.exists(summary_path))
        self.assertTrue(os.path.exists(logs_path))

        with open(summary_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)
            self.assertGreater(len(rows), 0)

        with open(logs_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            self.assertIsNotNone(fieldnames)

            required_fields = [
                "日志ID", "批次ID", "来源文件", "变更类型", "记录类型", "记录编号",
                "变更字段", "原值", "新值", "变更摘要",
            ]
            for field in required_fields:
                self.assertIn(field, fieldnames, f"CSV缺少字段: {field}")

            rows = list(reader)
            self.assertGreater(len(rows), 0)

    def test_export_contains_before_after_summaries(self):
        """测试导出包含变更前后摘要"""
        result = self.tracker.export_change_logs(
            self.batch_id, "export_user", format="json"
        )

        with open(result["file_path"], "r", encoding="utf-8") as f:
            data = json.load(f)

        for log in data["change_logs"]:
            self.assertIn("变更前摘要", log)
            self.assertIn("变更后摘要", log)
            self.assertNotEqual(log["变更后摘要"], "-")
            if log["变更类型"] != "新增记录":
                self.assertNotEqual(log["变更前摘要"], "-")

    def test_export_contains_impact_info(self):
        """测试导出包含影响分析信息"""
        result = self.tracker.export_change_logs(
            self.batch_id, "export_user", format="json"
        )

        with open(result["file_path"], "r", encoding="utf-8") as f:
            data = json.load(f)

        for log in data["change_logs"]:
            self.assertIn("影响类型", log)
            self.assertIn("影响详情", log)
            self.assertIn("影响的匹配ID", log)
            self.assertIsNotNone(log["影响类型"])
            self.assertIsNotNone(log["影响详情"])

    def test_export_contains_operator_and_timestamps(self):
        """测试导出包含操作者和时间戳"""
        result = self.tracker.export_change_logs(
            self.batch_id, "export_user", format="json"
        )

        with open(result["file_path"], "r", encoding="utf-8") as f:
            data = json.load(f)

        export_info = data["export_info"]
        self.assertIn("exported_at", export_info)
        self.assertIn("exported_by", export_info)

        for log in data["change_logs"]:
            self.assertIn("检测时间", log)
            self.assertIn("操作者", log)
            self.assertIsNotNone(log["检测时间"])

    def test_export_batch_info_preserved(self):
        """测试导出保留批次信息"""
        result = self.tracker.export_change_logs(
            self.batch_id, "export_user", format="json"
        )

        with open(result["file_path"], "r", encoding="utf-8") as f:
            data = json.load(f)

        export_info = data["export_info"]
        self.assertEqual(export_info["batch_id"], self.batch_id)
        self.assertIn("file_name", export_info)
        self.assertIn("file_type", export_info)
        self.assertIn("imported_at", export_info)

        for log in data["change_logs"]:
            self.assertEqual(log["批次ID"], self.batch_id)
            self.assertIn("来源文件", log)


class TestRestartRecovery(unittest.TestCase):
    """测试重启恢复功能"""

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

        self.invoice_v1 = os.path.join(self.test_dir, "invoices_v1.csv")
        self.payment_v1 = os.path.join(self.test_dir, "payments_v1.csv")
        self.invoice_v2 = os.path.join(self.test_dir, "invoices_v2.csv")

        with open(self.invoice_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V1)
        with open(self.payment_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_V1)
        with open(self.invoice_v2, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V2)

        self.db = Database(self.db_path)
        self.workflow = WorkflowManager(self.config, self.db)
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db, self.workflow)
        self.tracker = ChangeTracker(self.config, self.db)
        self.workbench = BatchWorkbench(self.config, self.db)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_export_context_persists_across_instances(self):
        """测试导出上下文跨实例持久化"""
        inv1 = self.importer.import_invoices(self.invoice_v1, "op_a")
        batch_id = inv1["batch_id"]

        self.workbench.save_export_context(
            batch_id=batch_id,
            export_type="change_logs",
            format="json",
            operator="export_user",
        )

        del self.db
        del self.workbench

        new_db = Database(self.db_path)
        new_workbench = BatchWorkbench(self.config, new_db)

        context = new_workbench.get_last_export_context()
        self.assertIsNotNone(context)
        self.assertEqual(context["batch_id"], batch_id)
        self.assertEqual(context["export_type"], "change_logs")
        self.assertEqual(context["format"], "json")
        self.assertEqual(context["operator"], "export_user")
        self.assertIn("file_name", context)
        self.assertIn("file_type", context)

    def test_change_view_context_persists(self):
        """测试变更查看上下文持久化"""
        inv1 = self.importer.import_invoices(self.invoice_v1, "op_a")
        batch_id = inv1["batch_id"]

        self.workbench.save_change_view_context(
            batch_id=batch_id,
            change_type=CHANGE_TYPE_AMOUNT_CHANGE,
            impact_type=IMPACT_TYPE_CRITICAL,
            processing_status=PROCESSING_STATUS_PENDING,
            operator="viewer",
        )

        del self.db
        del self.workbench

        new_db = Database(self.db_path)
        new_workbench = BatchWorkbench(self.config, new_db)

        context = new_workbench.get_last_change_view_context()
        self.assertIsNotNone(context)
        self.assertEqual(context["batch_id"], batch_id)
        self.assertEqual(context["change_type"], CHANGE_TYPE_AMOUNT_CHANGE)
        self.assertEqual(context["impact_type"], IMPACT_TYPE_CRITICAL)
        self.assertEqual(context["processing_status"], PROCESSING_STATUS_PENDING)

    def test_last_selected_batch_recovery(self):
        """测试上次选中批次恢复"""
        inv1 = self.importer.import_invoices(self.invoice_v1, "op_a")
        batch_id = inv1["batch_id"]

        self.workbench.save_last_selected_batch(batch_id, "user_a")
        self.workbench.save_filters(operator="op_a", status=MATCH_STATUS_PENDING)

        del self.db
        del self.workbench

        new_db = Database(self.db_path)
        new_workbench = BatchWorkbench(self.config, new_db)

        restore_result = new_workbench.restore_workbench_state()
        self.assertTrue(restore_result["restored"])
        self.assertIsNotNone(restore_result["last_batch"])
        self.assertEqual(restore_result["last_batch"]["batch_id"], batch_id)
        self.assertEqual(restore_result["filters"]["operator"], "op_a")
        self.assertEqual(restore_result["filters"]["status"], MATCH_STATUS_PENDING)

    def test_change_logs_persist_across_restarts(self):
        """测试变更日志跨重启持久化"""
        self.importer.import_invoices(self.invoice_v1, "op_a")
        result = self.importer.import_invoices(self.invoice_v2, "op_b")
        batch_id = result["batch_id"]

        original_logs = self.db.get_batch_change_logs(batch_id=batch_id)
        original_count = len(original_logs)
        self.assertGreater(original_count, 0)

        del self.db
        del self.tracker

        new_db = Database(self.db_path)
        new_tracker = ChangeTracker(self.config, new_db)

        restored_logs = new_db.get_batch_change_logs(batch_id=batch_id)
        self.assertEqual(len(restored_logs), original_count)

        for orig, restored in zip(original_logs, restored_logs):
            self.assertEqual(orig["change_type"], restored["change_type"])
            self.assertEqual(orig["record_no"], restored["record_no"])
            self.assertEqual(orig["impact_type"], restored["impact_type"])

    def test_processing_status_persists(self):
        """测试处理状态持久化"""
        self.importer.import_invoices(self.invoice_v1, "op_a")
        result = self.importer.import_invoices(self.invoice_v2, "op_b")
        batch_id = result["batch_id"]

        logs = self.db.get_batch_change_logs(batch_id=batch_id)
        log_id = logs[0]["id"]

        self.db.update_change_log_status(
            log_id, PROCESSING_STATUS_REVIEWED, "reviewer", "已查看"
        )

        del self.db

        new_db = Database(self.db_path)
        updated = new_db.get_batch_change_logs(batch_id=batch_id)
        updated_log = next(l for l in updated if l["id"] == log_id)

        self.assertEqual(updated_log["processing_status"], PROCESSING_STATUS_REVIEWED)
        self.assertEqual(updated_log["processed_by"], "reviewer")
        self.assertIsNotNone(updated_log["processed_at"])


class TestChangeTrackingFullWorkflow(unittest.TestCase):
    """完整工作流回归测试：导入->变更检测->查看->导出->重启恢复"""

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

        self.invoice_v1 = os.path.join(self.test_dir, "invoices_v1.csv")
        self.payment_v1 = os.path.join(self.test_dir, "payments_v1.csv")
        self.invoice_v2 = os.path.join(self.test_dir, "invoices_v2.csv")
        self.payment_v2 = os.path.join(self.test_dir, "payments_v2.csv")

        with open(self.invoice_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V1)
        with open(self.payment_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_V1)
        with open(self.invoice_v2, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V2)
        with open(self.payment_v2, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_V2)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_full_workflow(self):
        """测试完整工作流"""
        db = Database(self.db_path)
        workflow = WorkflowManager(self.config, db)
        importer = CSVImporter(self.config, db)
        matcher = MatchEngine(self.config, db, workflow)
        tracker = ChangeTracker(self.config, db)
        workbench = BatchWorkbench(self.config, db)

        inv1 = importer.import_invoices(self.invoice_v1, "operator_a")
        pay1 = importer.import_payments(self.payment_v1, "operator_a")
        matcher.run_auto_matching("operator_a")

        matches = db.get_matches_by_status()
        for m in matches:
            if m["status"] == MATCH_STATUS_PENDING:
                matcher.confirm_match(m["id"], "operator_a", "确认匹配")

        inv_batch1 = inv1["batch_id"]
        inv2 = importer.import_invoices(self.invoice_v2, "operator_b")
        inv_batch2 = inv2["batch_id"]

        self.assertIn("change_count", inv2)
        self.assertGreater(inv2["change_count"], 0)
        self.assertIn("changes_by_type", inv2)
        self.assertIn("impact_summary", inv2)

        workbench.save_last_selected_batch(inv_batch2, "operator_b")
        workbench.save_filters(operator="operator_a", status=MATCH_STATUS_MATCHED)

        changes = db.get_batch_change_logs(batch_id=inv_batch2)
        self.assertGreater(len(changes), 0)

        for c in changes:
            self.assertIsNotNone(c["change_summary"])
            self.assertIsNotNone(c["impact_type"])
            self.assertIsNotNone(c["impact_details"])

        change_summary = tracker.get_change_summary(batch_id=inv_batch2)
        self.assertTrue(change_summary["success"])
        self.assertGreater(change_summary["total_changes"], 0)
        self.assertIn("by_type", change_summary)
        self.assertIn("by_impact", change_summary)

        json_export = tracker.export_change_logs(
            inv_batch2, "exporter", format="json"
        )
        self.assertTrue(json_export["success"])

        csv_export = tracker.export_change_logs(
            inv_batch2, "exporter", format="csv"
        )
        self.assertTrue(csv_export["success"])

        workbench.save_export_context(
            batch_id=inv_batch2,
            export_type="change_logs",
            format="json",
            operator="exporter",
        )

        workbench.save_change_view_context(
            batch_id=inv_batch2,
            change_type=CHANGE_TYPE_AMOUNT_CHANGE,
            impact_type=IMPACT_TYPE_CRITICAL,
            operator="viewer",
        )

        del db
        del workflow
        del importer
        del matcher
        del tracker
        del workbench

        db2 = Database(self.db_path)
        workflow2 = WorkflowManager(self.config, db2)
        tracker2 = ChangeTracker(self.config, db2)
        workbench2 = BatchWorkbench(self.config, db2)

        restore_result = workbench2.restore_workbench_state()
        self.assertTrue(restore_result["restored"])
        self.assertEqual(restore_result["last_batch"]["batch_id"], inv_batch2)
        self.assertEqual(restore_result["filters"]["operator"], "operator_a")
        self.assertEqual(restore_result["filters"]["status"], MATCH_STATUS_MATCHED)

        export_context = workbench2.get_last_export_context()
        self.assertIsNotNone(export_context)
        self.assertEqual(export_context["batch_id"], inv_batch2)
        self.assertEqual(export_context["export_type"], "change_logs")
        self.assertEqual(export_context["format"], "json")

        view_context = workbench2.get_last_change_view_context()
        self.assertIsNotNone(view_context)
        self.assertEqual(view_context["batch_id"], inv_batch2)
        self.assertEqual(view_context["change_type"], CHANGE_TYPE_AMOUNT_CHANGE)

        changes_after = db2.get_batch_change_logs(batch_id=inv_batch2)
        self.assertEqual(len(changes_after), len(changes))

        json_export2 = tracker2.export_change_logs(
            inv_batch2, "exporter2", format="json"
        )
        self.assertTrue(json_export2["success"])
        self.assertTrue(os.path.exists(json_export2["file_path"]))

        with open(json_export2["file_path"], "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["export_info"]["batch_id"], inv_batch2)
        self.assertEqual(data["export_info"]["exported_by"], "exporter2")
        self.assertGreater(len(data["change_logs"]), 0)

        for log in data["change_logs"]:
            self.assertIsNotNone(log["变更前摘要"] if log["变更类型"] != "新增记录" else True)
            self.assertIsNotNone(log["变更后摘要"])
            self.assertIsNotNone(log["影响类型"])
            self.assertIsNotNone(log["影响详情"])
            self.assertIn(log["影响类型"], IMPACT_TYPE_LABELS.values())
            self.assertIn(log["变更类型"], CHANGE_TYPE_LABELS.values())
            self.assertIn(log["处理状态"], PROCESSING_STATUS_LABELS.values())

        importer2 = CSVImporter(self.config, db2)
        pay2 = importer2.import_payments(self.payment_v2, "operator_c")
        self.assertIn("change_count", pay2)
        self.assertGreater(pay2["change_count"], 0)

        pay_changes = db2.get_batch_change_logs(batch_id=pay2["batch_id"])
        self.assertGreater(len(pay_changes), 0)

        pay_export = tracker2.export_change_logs(
            pay2["batch_id"], "exporter", format="json"
        )
        self.assertTrue(pay_export["success"])


class TestChangeTrackingHandoffDocRegression(unittest.TestCase):
    """
    交接文档复现链路的回归保护测试。
    专门卡住两个容易踩的坑：
    1. 用同一个文件做"重新导入" → 除了 duplicate_process 不会有其他变更
    2. 记错批次号顺序 → v1发票=1、v1收款=2、v2发票=3
    """

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

        self.invoice_v1 = os.path.join(self.test_dir, "invoices_v1.csv")
        self.payment_v1 = os.path.join(self.test_dir, "payments_v1.csv")
        self.invoice_v2 = os.path.join(self.test_dir, "invoices_v2.csv")

        with open(self.invoice_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V1)
        with open(self.payment_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_V1)
        with open(self.invoice_v2, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V2)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_same_content_file_is_skipped(self):
        """
        相同内容的文件（同哈希）重复导入会直接被跳过，不会产生新批次。
        卡住"用同一个 sample_invoices.csv 演示变更追踪"的坑——
        接手人如果照着旧文档用同一个文件导入两遍，第二次直接被跳过，
        连新批次都不会产生，更看不到变更日志。
        """
        db = Database(self.db_path)
        importer = CSVImporter(self.config, db)

        same_content_copy = os.path.join(self.test_dir, "invoices_copy.csv")
        shutil.copyfile(self.invoice_v1, same_content_copy)

        r1 = importer.import_invoices(self.invoice_v1, "op_a")
        self.assertEqual(r1["batch_id"], 1)
        self.assertFalse(r1.get("skipped", False))

        r2 = importer.import_invoices(same_content_copy, "op_b")
        self.assertTrue(r2.get("skipped", False),
                        "相同内容（同文件哈希）的文件应被跳过，不产生新批次。"
                        "这就是为什么文档示例必须用 sample_invoices_updated.csv"
                        "而不是再导一次 sample_invoices.csv。")
        self.assertEqual(r2["batch_id"], 1,
                         "跳过后应复用原批次ID")

        changes_after_skip = db.get_batch_change_logs()
        self.assertEqual(len(changes_after_skip), 0,
                         "文件被跳过意味着没有新批次，也就不会产生变更日志")

        del db

    def test_batch_sequence_and_full_handoff_chain(self):
        """
        按 v1发票 → v1收款 → v2发票 的标准交接流程导入，
        验证 v2发票批次号为 3（文档示例用的 3），并完整跑通：
        默认查看 → 按批次查看 → 导出 → 重启后 resume-export
        专门卡住"batch_id 写错导致看不对批次"的坑。
        """
        db = Database(self.db_path)
        workflow = WorkflowManager(self.config, db)
        importer = CSVImporter(self.config, db)
        matcher = MatchEngine(self.config, db, workflow)
        tracker = ChangeTracker(self.config, db)
        workbench = BatchWorkbench(self.config, db)
        workbench.clear_workbench_state()

        r_inv1 = importer.import_invoices(self.invoice_v1, "op_a")
        self.assertEqual(r_inv1["batch_id"], 1, "v1发票应为批次 1")

        r_pay1 = importer.import_payments(self.payment_v1, "op_a")
        self.assertEqual(r_pay1["batch_id"], 2, "v1收款应为批次 2")

        matcher.run_auto_matching("op_a")

        r_inv2 = importer.import_invoices(self.invoice_v2, "op_b")
        self.assertEqual(r_inv2["batch_id"], 3,
                         "v2发票应为批次 3（文档示例用的就是 3，顺序错了就看不对批次）")

        self.assertIn("change_count", r_inv2)
        self.assertGreater(r_inv2["change_count"], 0)
        changes = db.get_batch_change_logs(batch_id=r_inv2["batch_id"])
        types_found = {c["change_type"] for c in changes}
        self.assertIn(CHANGE_TYPE_NEW_RECORD, types_found,
                      "更新版文件导入后应有新增记录")
        self.assertIn(CHANGE_TYPE_STATUS_CHANGE, types_found,
                      "更新版文件导入后应有状态变更")
        self.assertIn(CHANGE_TYPE_AMOUNT_CHANGE, types_found,
                      "更新版文件导入后应有金额变更")
        self.assertIn(CHANGE_TYPE_KEY_FIELD_CHANGE, types_found,
                      "更新版文件导入后应用关键字段变更")

        summary = tracker.get_change_summary()
        self.assertTrue(summary["success"])
        self.assertGreater(summary["total_changes"], 0)

        result_json = tracker.export_change_logs(3, "export_user", format="json")
        self.assertTrue(result_json["success"])
        self.assertTrue(os.path.exists(result_json["file_path"]))

        with open(result_json["file_path"], "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["export_info"]["batch_id"], 3)
        self.assertEqual(data["export_info"]["exported_by"], "export_user")
        self.assertIn("change_logs", data)
        self.assertGreater(len(data["change_logs"]), 0)

        workbench.save_export_context(
            batch_id=3,
            export_type="change_logs",
            format="json",
            operator="export_user",
        )

        del db
        del workflow
        del importer
        del matcher
        del tracker
        del workbench

        db2 = Database(self.db_path)
        workbench2 = BatchWorkbench(self.config, db2)
        tracker2 = ChangeTracker(self.config, db2)

        export_ctx = workbench2.get_last_export_context()
        self.assertIsNotNone(export_ctx, "重启后应能恢复导出上下文")
        self.assertEqual(export_ctx["batch_id"], 3)
        self.assertEqual(export_ctx["format"], "json")

        result_resume = tracker2.export_change_logs(
            export_ctx["batch_id"], "resume_user", format=export_ctx["format"]
        )
        self.assertTrue(result_resume["success"],
                        "用恢复的上下文继续导出应成功")
        self.assertTrue(os.path.exists(result_resume["file_path"]))

        with open(result_resume["file_path"], "r", encoding="utf-8") as f:
            data2 = json.load(f)
        self.assertEqual(data2["export_info"]["exported_by"], "resume_user")
        self.assertEqual(data2["export_info"]["batch_id"], 3)

        del db2


@unittest.skipUnless(HAS_CLICK, "Click is required for CLI tests")
class TestBatchChangesCLIEntrypoints(unittest.TestCase):
    """batch changes 命令的 CLI 入口回归测试，覆盖不带参数和带 batch_id 两种情况"""

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

        self.invoice_v1 = os.path.join(self.test_dir, "invoices_v1.csv")
        self.payment_v1 = os.path.join(self.test_dir, "payments_v1.csv")
        self.invoice_v2 = os.path.join(self.test_dir, "invoices_v2.csv")

        with open(self.invoice_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V1)
        with open(self.payment_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_V1)
        with open(self.invoice_v2, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V2)

        self._setup_data()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _setup_data(self):
        db = Database(self.db_path)
        workflow = WorkflowManager(self.config, db)
        importer = CSVImporter(self.config, db)
        matcher = MatchEngine(self.config, db, workflow)

        importer.import_invoices(self.invoice_v1, "op_a")
        importer.import_payments(self.payment_v1, "op_a")
        matcher.run_auto_matching("op_a")
        inv2 = importer.import_invoices(self.invoice_v2, "op_b")
        self.batch_2_id = inv2["batch_id"]

        del db
        del workflow
        del importer
        del matcher

    def _run_cli(self, args):
        from invoice_reconciler.cli.main import cli
        try:
            runner = CliRunner(mix_stderr=False)
        except TypeError:
            runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", self._write_config()] + args,
            catch_exceptions=False,
        )
        return result

    def _write_config(self):
        config_path = os.path.join(self.test_dir, "config.yaml")
        import yaml
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump({
                "amount_tolerance": 0.01,
                "date_window_days": 30,
                "invoice_required_columns": [
                    "invoice_no", "invoice_date", "customer", "amount", "status"
                ],
                "payment_required_columns": [
                    "payment_no", "payment_date", "customer", "amount"
                ],
                "export_format": "xlsx",
                "db_path": self.db_path,
                "export_dir": self.export_dir,
                "lock_timeout_seconds": 3600,
                "default_user_role": "reviewer",
                "admin_users": ["admin"],
                "enable_lock": False,
            }, f)
        return config_path

    def test_batch_changes_without_batch_id_no_crash(self):
        """不带 batch_id 执行 batch changes 不应崩溃（稳定复现旧的 UnboundLocalError）"""
        result = self._run_cli(["batch", "changes"])

        self.assertNotIn(
            "UnboundLocalError", result.output + (result.stderr or ""),
            f"不应出现未绑定变量错误，输出: {result.output}"
        )
        self.assertNotEqual(
            result.exit_code, 2,
            f"不应因参数使用错误崩溃 (exit_code=2)，stderr: {result.stderr}"
        )

        self.assertIn("变更记录", result.output)
        self.assertIn("变更统计", result.output)

    def test_batch_changes_with_batch_id_works(self):
        """带 batch_id 执行 batch changes 正常工作"""
        result = self._run_cli(["batch", "changes", "--batch-id", str(self.batch_2_id)])

        self.assertEqual(result.exit_code, 0, f"stderr: {result.stderr}")
        self.assertIn(str(self.batch_2_id), result.output)
        self.assertIn("变更记录", result.output)
        self.assertIn("INV002", result.output)

    def test_batch_changes_two_entries_context_not_polluted(self):
        """不带参数 -> 带参数 -> 不带参数 查看上下文互不污染"""
        db = Database(self.db_path)
        workbench = BatchWorkbench(self.config, db)
        workbench.clear_workbench_state()

        result1 = self._run_cli(["batch", "changes"])
        self.assertEqual(result1.exit_code, 0, f"stderr: {result1.stderr}")

        ctx1 = workbench.get_last_change_view_context()
        self.assertIsNotNone(ctx1, "不带 batch_id 也应保存查看上下文")
        self.assertIsNone(ctx1.get("batch_id"), "上下文的 batch_id 应为 None 表示全部批次")

        result2 = self._run_cli(["batch", "changes", "--batch-id", str(self.batch_2_id)])
        self.assertEqual(result2.exit_code, 0, f"stderr: {result2.stderr}")

        ctx2 = workbench.get_last_change_view_context()
        self.assertIsNotNone(ctx2)
        self.assertEqual(ctx2.get("batch_id"), self.batch_2_id,
                          "带 batch_id 后上下文应记录具体批次号")

        result3 = self._run_cli(["batch", "changes"])
        self.assertEqual(result3.exit_code, 0, f"stderr: {result3.stderr}")

        ctx3 = workbench.get_last_change_view_context()
        self.assertIsNotNone(ctx3)
        self.assertIsNone(ctx3.get("batch_id"),
                          "再次不带 batch_id 应重新将上下文设为全部批次，不应被上次污染")

        del db

    def test_batch_changes_view_filter_persists(self):
        """batch changes 的过滤条件应被持久化，下次重启能恢复"""
        db = Database(self.db_path)
        workbench = BatchWorkbench(self.config, db)
        workbench.clear_workbench_state()

        self._run_cli([
            "batch", "changes",
            "--change-type", "amount_change",
            "--impact-type", "critical",
            "--status", "pending",
        ])

        ctx = workbench.get_last_change_view_context()
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.get("change_type"), "amount_change")
        self.assertEqual(ctx.get("impact_type"), "critical")
        self.assertEqual(ctx.get("processing_status"), "pending")
        self.assertIsNone(ctx.get("batch_id"))

        self._run_cli(["batch", "changes", "--batch-id", str(self.batch_2_id)])
        ctx_after = workbench.get_last_change_view_context()
        self.assertEqual(ctx_after.get("batch_id"), self.batch_2_id)
        self.assertIsNone(ctx_after.get("change_type"),
                        "切换到带 batch_id 时不应该保留之前的过滤条件（用户已明确指定了新范围）")
        self.assertIsNone(ctx_after.get("impact_type"))
        self.assertIsNone(ctx_after.get("processing_status"))

        del db

    def test_resume_after_changes_then_export(self):
        """先 batch changes（不带参数），然后 export-changes，再重启 batch changes 能恢复"""
        db = Database(self.db_path)
        workbench = BatchWorkbench(self.config, db)
        workbench.clear_workbench_state()

        result_view = self._run_cli(["batch", "changes"])
        self.assertEqual(result_view.exit_code, 0)

        result_export = self._run_cli([
            "batch", "export-changes", str(self.batch_2_id),
            "--format", "json",
        ])
        self.assertEqual(result_export.exit_code, 0, f"stderr: {result_export.stderr}")
        self.assertIn("变更日志已导出", result_export.output)

        export_ctx = workbench.get_last_export_context()
        self.assertIsNotNone(export_ctx)
        self.assertEqual(export_ctx.get("batch_id"), self.batch_2_id)
        self.assertEqual(export_ctx.get("format"), "json")

        result_view2 = self._run_cli(["batch", "changes"])
        self.assertEqual(result_view2.exit_code, 0)

        self.assertIn("会话恢复", result_view2.output)
        self.assertIn("导出", result_view2.output)
        self.assertIn("上次查看变更", result_view2.output)

        del db

    def test_four_entrypoints_handoff_chain(self):
        """
        交接文档四个入口全链路校验：
        1. batch changes (不带 batch_id) → 默认查看全部变更
        2. batch changes --batch-id X   → 按批次缩小范围
        3. batch export-changes X       → 导出变更日志
        4. batch resume-export          → 重启后继续导出不丢上下文

        专门卡住"文档承诺了但实际跑不通"的缺口。
        """
        db = Database(self.db_path)
        workbench = BatchWorkbench(self.config, db)
        workbench.clear_workbench_state()
        del db
        del workbench

        result1 = self._run_cli(["batch", "changes"])
        self.assertEqual(result1.exit_code, 0,
                        f"入口1失败: batch changes 不带参数应正常运行，stderr={result1.stderr}")
        self.assertIn("变更记录", result1.output)
        self.assertIn("变更统计", result1.output)
        self.assertIn("影响统计", result1.output)
        self.assertIn("处理状态", result1.output)

        result2 = self._run_cli(["batch", "changes", "--batch-id", str(self.batch_2_id)])
        self.assertEqual(result2.exit_code, 0,
                        f"入口2失败: batch changes --batch-id 应正常运行，stderr={result2.stderr}")
        self.assertIn(str(self.batch_2_id), result2.output)
        self.assertIn("变更记录", result2.output)

        result3 = self._run_cli([
            "batch", "export-changes", str(self.batch_2_id),
            "--operator", "handover_test", "--format", "json",
        ])
        self.assertEqual(result3.exit_code, 0,
                        f"入口3失败: batch export-changes 应正常运行，stderr={result3.stderr}")
        self.assertIn("变更日志已导出", result3.output)
        self.assertIn("格式: json", result3.output)

        result3_csv = self._run_cli([
            "batch", "export-changes", str(self.batch_2_id),
            "--operator", "handover_test", "--format", "csv",
        ])
        self.assertEqual(result3_csv.exit_code, 0,
                        f"入口3-CSV失败: batch export-changes csv 应正常运行，stderr={result3_csv.stderr}")
        self.assertIn("变更日志已导出", result3_csv.output)

        result4 = self._run_cli(["batch", "resume-export", "--operator", "handover_resume"])
        self.assertEqual(result4.exit_code, 0,
                        f"入口4失败: batch resume-export 应正常运行，stderr={result4.stderr}")
        self.assertIn("使用上次导出上下文", result4.output)
        self.assertIn(f"批次: #{self.batch_2_id}", result4.output)
        self.assertIn("导出已完成", result4.output)

        db2 = Database(self.db_path)
        workbench2 = BatchWorkbench(self.config, db2)
        export_ctx = workbench2.get_last_export_context()
        self.assertIsNotNone(export_ctx,
                            "导出后应能获取到导出上下文，用于重启恢复")
        self.assertEqual(export_ctx.get("batch_id"), self.batch_2_id)
        self.assertIn(export_ctx.get("format"), ["json", "csv"])
        self.assertIsNotNone(export_ctx.get("exported_at"),
                            "导出上下文应包含导出时间")

        view_ctx = workbench2.get_last_change_view_context()
        self.assertIsNotNone(view_ctx,
                            "查看变更后应能获取到查看上下文，用于重启恢复")
        self.assertEqual(view_ctx.get("batch_id"), self.batch_2_id)

        del db2


if __name__ == "__main__":
    unittest.main()
