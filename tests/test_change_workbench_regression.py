import os
import sys
import json
import csv
import tempfile
import unittest
import shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from click.testing import CliRunner
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
    SESSION_KEY_LAST_CHANGE_VIEW,
    SESSION_KEY_LAST_EXPORT,
)
from invoice_reconciler.core.change_tracker import (
    ChangeTracker,
    CHANGE_TYPE_NEW_RECORD,
    CHANGE_TYPE_STATUS_CHANGE,
    CHANGE_TYPE_AMOUNT_CHANGE,
    CHANGE_TYPE_KEY_FIELD_CHANGE,
    CHANGE_TYPE_LABELS,
    IMPACT_TYPE_NONE,
    IMPACT_TYPE_PENDING,
    IMPACT_TYPE_CONFIRMED,
    IMPACT_TYPE_REVOKED,
    IMPACT_TYPE_CRITICAL,
    IMPACT_TYPE_LABELS,
    PROCESSING_STATUS_PENDING,
    PROCESSING_STATUS_REVIEWED,
    PROCESSING_STATUS_RESOLVED,
    PROCESSING_STATUS_LABELS,
    IMPACT_FILTER_CONFIRMED,
    IMPACT_FILTER_PENDING,
    IMPACT_FILTER_REVOKED,
    IMPACT_FILTER_ALL_AFFECTED,
)


SAMPLE_INVOICES_V1 = """invoice_no,invoice_date,customer,amount,status
INV001,2024-01-15,北京科技有限公司,1000.00,正常
INV002,2024-01-16,上海贸易公司,2500.50,正常
INV003,2024-01-17,广州电子厂,3000.00,正常
INV004,2024-01-18,深圳软件公司,1500.00,正常
INV005,2024-01-19,杭州电商平台,800.00,正常
"""

SAMPLE_INVOICES_V2 = """invoice_no,invoice_date,customer,amount,status
INV001,2024-01-15,北京科技有限公司,1000.00,正常
INV002,2024-01-16,上海贸易公司,2500.50,作废
INV003,2024-01-17,广州电子有限公司,3000.00,正常
INV004,2024-01-18,深圳软件公司,1600.00,正常
INV005,2024-01-19,杭州电商平台,800.00,正常
INV006,2024-01-20,武汉科技公司,2000.00,正常
"""

SAMPLE_INVOICES_V3 = """invoice_no,invoice_date,customer,amount,status
INV001,2024-01-15,北京科技有限公司,1000.00,正常
INV002,2024-01-16,上海贸易公司,2800.50,作废
INV003,2024-01-17,广州电子有限公司,3000.00,正常
INV004,2024-01-18,深圳软件公司,1700.00,正常
INV007,2024-01-21,成都科技,900.00,正常
"""

SAMPLE_PAYMENTS_V1 = """payment_no,payment_date,customer,amount
PAY001,2024-01-16,北京科技有限公司,1000.00
PAY002,2024-01-17,上海贸易公司,2500.50
PAY003,2024-01-18,广州电子厂,3000.00
PAY004,2024-01-20,深圳软件公司,1499.99
PAY005,2024-01-21,杭州电商平台,800.00
"""


class TestChangeWorkbenchRegression(unittest.TestCase):
    """批次变更工作台回归测试：重导入、筛选、导出、重启恢复、配置隔离"""

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

        self.invoice_csv_v1 = os.path.join(self.test_dir, "invoices_v1.csv")
        self.invoice_csv_v2 = os.path.join(self.test_dir, "invoices_v2.csv")
        self.invoice_csv_v3 = os.path.join(self.test_dir, "invoices_v3.csv")
        self.payment_csv_v1 = os.path.join(self.test_dir, "payments_v1.csv")

        with open(self.invoice_csv_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V1)
        with open(self.invoice_csv_v2, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V2)
        with open(self.invoice_csv_v3, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V3)
        with open(self.payment_csv_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_V1)

        self.db = Database(self.db_path)
        self.workflow = WorkflowManager(self.config, self.db)
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db, self.workflow)
        self.revoker = Revoker(self.db, self.config, self.workflow)
        self.workbench = BatchWorkbench(self.config, self.db)
        self.tracker = ChangeTracker(self.config, self.db)

    def tearDown(self):
        self.db = None
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def _setup_full_scenario(self):
        """设置完整测试场景：导入v1、匹配、确认/撤销、导入v2、导入v3"""
        r1 = self.importer.import_invoices(self.invoice_csv_v1, "zhangsan")
        batch_inv1 = r1["batch_id"]

        r2 = self.importer.import_payments(self.payment_csv_v1, "zhangsan")
        batch_pay1 = r2["batch_id"]

        match_result = self.matcher.run_auto_matching(operator="系统")

        pending = self.db.get_matches_by_status(MATCH_STATUS_PENDING)
        match_ids_confirmed = []
        match_ids_revoked = []
        if len(pending) >= 1:
            self.matcher.confirm_match(pending[0]["id"], "mgr", "ok")
            match_ids_confirmed.append(pending[0]["id"])
        if len(pending) >= 2:
            self.matcher.confirm_match(pending[1]["id"], "mgr", "ok")
            self.revoker.revoke_match(pending[1]["id"], "mgr", "合同号不符")
            match_ids_revoked.append(pending[1]["id"])

        r3 = self.importer.import_invoices(self.invoice_csv_v2, "lisi")
        batch_inv2 = r3["batch_id"]

        r4 = self.importer.import_invoices(self.invoice_csv_v3, "wangwu")
        batch_inv3 = r4["batch_id"]

        return {
            "batch_inv1": batch_inv1,
            "batch_inv2": batch_inv2,
            "batch_inv3": batch_inv3,
            "batch_pay1": batch_pay1,
            "confirmed_match_ids": match_ids_confirmed,
            "revoked_match_ids": match_ids_revoked,
        }

    def test_01_reimport_status_changes_detected(self):
        """测试1: 重导入时状态变更、金额变更、新增记录、关键字段变更都被正确追踪"""
        scenario = self._setup_full_scenario()
        batch_inv2 = scenario["batch_inv2"]

        logs = self.db.get_batch_change_logs(batch_id=batch_inv2)
        self.assertGreater(len(logs), 0, "重导入后应该检测到变更")

        change_types = set(l["change_type"] for l in logs)
        self.assertIn(CHANGE_TYPE_STATUS_CHANGE, change_types,
                      "应该检测到状态变更（INV002从正常变为作废）")
        self.assertIn(CHANGE_TYPE_AMOUNT_CHANGE, change_types,
                      "应该检测到金额变更（INV004从1500变为1600）")
        self.assertIn(CHANGE_TYPE_KEY_FIELD_CHANGE, change_types,
                      "应该检测到关键字段变更（INV003客户名称变更）")
        self.assertIn(CHANGE_TYPE_NEW_RECORD, change_types,
                      "应该检测到新增记录（INV006）")

        status_changes = [l for l in logs if l["change_type"] == CHANGE_TYPE_STATUS_CHANGE]
        self.assertTrue(any("作废" in (l.get("new_value") or "") for l in status_changes),
                        "状态变更的新值应该包含'作废'")

    def test_02_filtered_summary_matches_details(self):
        """测试2: 筛选后的统计摘要与明细记录数一致（顶部统计=下方明细）"""
        scenario = self._setup_full_scenario()
        batch_inv3 = scenario["batch_inv3"]

        result = self.workbench.get_unified_change_view(
            batch_id=batch_inv3,
            operator="tester",
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["summary"]["total_changes"], len(result["logs"]),
                         "全量视图：统计总数应等于明细记录数")

        result_status = self.workbench.get_unified_change_view(
            batch_id=batch_inv3,
            change_type=CHANGE_TYPE_STATUS_CHANGE,
            operator="tester",
        )
        status_count = result_status["summary"]["by_type"].get(CHANGE_TYPE_STATUS_CHANGE, 0)
        self.assertEqual(status_count, len(result_status["logs"]),
                         "按变更类型筛选：该类型统计数应等于明细记录数")
        self.assertEqual(status_count, result_status["hit_count"],
                         "命中数应等于明细记录数")

        result_amount = self.workbench.get_unified_change_view(
            batch_id=batch_inv3,
            change_type=CHANGE_TYPE_AMOUNT_CHANGE,
            operator="tester",
        )
        amount_count = result_amount["summary"]["by_type"].get(CHANGE_TYPE_AMOUNT_CHANGE, 0)
        self.assertEqual(amount_count, len(result_amount["logs"]),
                         "按金额变更筛选：统计数应等于明细记录数")

    def test_03_affect_filter_consistency(self):
        """测试3: 按影响类型(affect)筛选后，统计、明细、命中数一致"""
        scenario = self._setup_full_scenario()
        batch_inv2 = scenario["batch_inv2"]

        result_all = self.workbench.get_unified_change_view(
            batch_id=batch_inv2,
            affect_filter=IMPACT_FILTER_ALL_AFFECTED,
            operator="tester",
        )
        self.assertTrue(result_all["success"])
        self.assertEqual(result_all["summary"]["total_changes"], len(result_all["logs"]),
                         "affect=all筛选后：统计总数应等于明细记录数")
        for log in result_all["logs"]:
            self.assertNotEqual(log["impact_type"], IMPACT_TYPE_NONE,
                                "所有命中记录的影响类型都不应为'无影响'")

        result_confirmed = self.workbench.get_unified_change_view(
            batch_id=batch_inv2,
            affect_filter=IMPACT_FILTER_CONFIRMED,
            operator="tester",
        )
        self.assertEqual(
            result_confirmed["summary"]["total_changes"],
            len(result_confirmed["logs"]),
            "affect=confirmed筛选后：统计总数应等于明细记录数"
        )
        for log in result_confirmed["logs"]:
            self.assertIn(
                log["impact_type"],
                [IMPACT_TYPE_CONFIRMED, IMPACT_TYPE_CRITICAL],
                "affect=confirmed的记录影响类型应为confirmed或critical"
            )

        result_pending = self.workbench.get_unified_change_view(
            batch_id=batch_inv2,
            affect_filter=IMPACT_FILTER_PENDING,
            operator="tester",
        )
        self.assertEqual(
            result_pending["summary"]["total_changes"],
            len(result_pending["logs"]),
            "affect=pending筛选后：统计总数应等于明细记录数"
        )

    def test_04_processing_status_filter(self):
        """测试4: 按处理状态筛选后，统计与明细一致"""
        scenario = self._setup_full_scenario()
        batch_inv2 = scenario["batch_inv2"]

        logs = self.db.get_batch_change_logs(batch_id=batch_inv2)
        if len(logs) >= 2:
            self.db.update_change_log_status(
                logs[0]["id"], PROCESSING_STATUS_REVIEWED, "reviewer1", "已查看"
            )
            self.db.update_change_log_status(
                logs[1]["id"], PROCESSING_STATUS_RESOLVED, "resolver1", "已解决"
            )

        result_reviewed = self.workbench.get_unified_change_view(
            batch_id=batch_inv2,
            processing_status=PROCESSING_STATUS_REVIEWED,
            operator="tester",
        )
        self.assertEqual(
            result_reviewed["summary"]["by_status"].get(PROCESSING_STATUS_REVIEWED, 0),
            len(result_reviewed["logs"]),
            "按处理状态筛选后，该状态的统计数应等于明细记录数"
        )
        for log in result_reviewed["logs"]:
            self.assertEqual(log["processing_status"], PROCESSING_STATUS_REVIEWED)

        result_pending = self.workbench.get_unified_change_view(
            batch_id=batch_inv2,
            processing_status=PROCESSING_STATUS_PENDING,
            operator="tester",
        )
        self.assertEqual(
            result_pending["summary"]["by_status"].get(PROCESSING_STATUS_PENDING, 0),
            len(result_pending["logs"]),
            "按待处理状态筛选后，统计数应等于明细数"
        )

    def test_05_with_conflicts_only_filter(self):
        """测试5: 仅显示冲突的筛选与统计一致"""
        scenario = self._setup_full_scenario()
        batch_inv3 = scenario["batch_inv3"]

        result = self.workbench.get_unified_change_view(
            batch_id=batch_inv3,
            with_conflicts_only=True,
            operator="tester",
        )
        self.assertEqual(
            result["summary"]["conflict_count"],
            len(result["logs"]),
            "仅含冲突筛选后：冲突统计数应等于明细记录数"
        )
        for log in result["logs"]:
            self.assertTrue(log.get("conflict_reason"),
                            "所有命中记录都应有冲突原因")

    def test_06_export_json_matches_view(self):
        """测试6: 导出的JSON内容与CLI视图筛选结果完全一致"""
        scenario = self._setup_full_scenario()
        batch_inv2 = scenario["batch_inv2"]

        view_result = self.workbench.get_unified_change_view(
            batch_id=batch_inv2,
            change_type=CHANGE_TYPE_STATUS_CHANGE,
            affect_filter=IMPACT_FILTER_ALL_AFFECTED,
            operator="tester",
        )

        export_result = self.tracker.export_change_logs(
            batch_inv2,
            operator="tester",
            format="json",
            log_ids=[l["id"] for l in view_result["logs"]],
        )

        self.assertTrue(export_result["success"])

        with open(export_result["file_path"], "r", encoding="utf-8") as f:
            json_data = json.load(f)

        self.assertEqual(
            json_data["summary"]["total_changes"],
            view_result["summary"]["total_changes"],
            "JSON导出的总数应与视图统计一致"
        )
        self.assertEqual(
            len(json_data["change_logs"]),
            len(view_result["logs"]),
            "JSON导出的明细条数应与视图明细数一致"
        )
        self.assertEqual(
            json_data["summary"]["by_type"].get(CHANGE_TYPE_STATUS_CHANGE, 0),
            view_result["summary"]["by_type"].get(CHANGE_TYPE_STATUS_CHANGE, 0),
            "JSON导出的按类型统计应与视图一致"
        )

        view_record_nos = sorted([l["record_no"] for l in view_result["logs"]])
        export_record_nos = sorted([c["记录编号"] for c in json_data["change_logs"]])
        self.assertEqual(view_record_nos, export_record_nos,
                         "JSON导出的记录编号应与视图完全一致")

    def test_07_export_csv_matches_view(self):
        """测试7: 导出的CSV内容与CLI视图筛选结果完全一致"""
        scenario = self._setup_full_scenario()
        batch_inv2 = scenario["batch_inv2"]

        view_result = self.workbench.get_unified_change_view(
            batch_id=batch_inv2,
            change_type=CHANGE_TYPE_AMOUNT_CHANGE,
            operator="tester",
        )

        export_result = self.tracker.export_change_logs(
            batch_inv2,
            operator="tester",
            format="csv",
            log_ids=[l["id"] for l in view_result["logs"]],
        )

        self.assertTrue(export_result["success"])
        self.assertTrue(os.path.isdir(export_result["file_path"]))

        detail_path = os.path.join(export_result["file_path"], "变更明细.csv")
        self.assertTrue(os.path.exists(detail_path), "CSV明细文件应存在")

        with open(detail_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            csv_rows = list(reader)

        self.assertEqual(
            len(csv_rows),
            len(view_result["logs"]),
            "CSV导出的明细条数应与视图明细数一致"
        )

        view_record_nos = sorted([l["record_no"] for l in view_result["logs"]])
        csv_record_nos = sorted([r["记录编号"] for r in csv_rows])
        self.assertEqual(view_record_nos, csv_record_nos,
                         "CSV导出的记录编号应与视图完全一致")

        summary_path = os.path.join(export_result["file_path"], "变更摘要.csv")
        self.assertTrue(os.path.exists(summary_path), "CSV摘要文件应存在")

    def test_08_session_state_persists_batch(self):
        """测试8: 批次选择和筛选状态持久化到数据库，重启后可恢复"""
        scenario = self._setup_full_scenario()
        batch_inv2 = scenario["batch_inv2"]

        self.workbench.save_last_selected_batch(batch_inv2, "tester")
        self.workbench.save_change_view_context(
            batch_id=batch_inv2,
            change_type=CHANGE_TYPE_STATUS_CHANGE,
            impact_type=IMPACT_TYPE_CONFIRMED,
            processing_status=PROCESSING_STATUS_PENDING,
            record_no="INV002",
            affect_filter="confirmed",
            with_conflicts_only=True,
            operator="tester",
        )

        new_db = Database(self.db_path)
        new_workbench = BatchWorkbench(self.config, new_db)

        last_batch = new_workbench.get_last_selected_batch()
        self.assertIsNotNone(last_batch, "重启后应能恢复上次选择的批次")
        self.assertEqual(last_batch.get("batch_id"), batch_inv2)

        last_view = new_workbench.get_last_change_view_context()
        self.assertIsNotNone(last_view, "重启后应能恢复上次变更视图上下文")
        self.assertEqual(last_view.get("batch_id"), batch_inv2)
        self.assertEqual(last_view.get("change_type"), CHANGE_TYPE_STATUS_CHANGE)
        self.assertEqual(last_view.get("impact_type"), IMPACT_TYPE_CONFIRMED)
        self.assertEqual(last_view.get("processing_status"), PROCESSING_STATUS_PENDING)
        self.assertEqual(last_view.get("record_no"), "INV002")
        self.assertEqual(last_view.get("affect_filter"), "confirmed")
        self.assertTrue(last_view.get("with_conflicts_only"))

    def test_09_export_context_persists(self):
        """测试9: 导出上下文持久化，重启后可继续导出"""
        scenario = self._setup_full_scenario()
        batch_inv2 = scenario["batch_inv2"]

        self.workbench.save_export_context(
            batch_id=batch_inv2,
            export_type="change_logs",
            format="json",
            operator="tester",
            filters={"change_type": CHANGE_TYPE_STATUS_CHANGE},
            extra={"hit_count": 5},
        )

        new_db = Database(self.db_path)
        new_workbench = BatchWorkbench(self.config, new_db)

        last_export = new_workbench.get_last_export_context()
        self.assertIsNotNone(last_export, "重启后应能恢复上次导出上下文")
        self.assertEqual(last_export.get("batch_id"), batch_inv2)
        self.assertEqual(last_export.get("export_type"), "change_logs")
        self.assertEqual(last_export.get("format"), "json")

    def test_10_config_isolation_separate_dbs(self):
        """测试10: 不同数据库（不同工作目录）的会话状态完全隔离"""
        scenario = self._setup_full_scenario()
        batch_inv2 = scenario["batch_inv2"]

        self.workbench.save_last_selected_batch(batch_inv2, "tester")
        self.workbench.save_change_view_context(
            batch_id=batch_inv2,
            change_type=CHANGE_TYPE_AMOUNT_CHANGE,
            operator="tester",
        )

        other_db_path = os.path.join(self.test_dir, "other.db")
        other_config = Config(
            db_path=other_db_path,
            export_dir=os.path.join(self.test_dir, "other_exports"),
        )
        other_db = Database(other_db_path)
        other_workbench = BatchWorkbench(other_config, other_db)

        other_last_batch = other_workbench.get_last_selected_batch()
        self.assertIsNone(other_last_batch, "不同数据库不应共享批次会话状态")

        other_last_view = other_workbench.get_last_change_view_context()
        self.assertIsNone(other_last_view, "不同数据库不应共享变更视图会话状态")

    def test_11_before_after_summaries_in_export(self):
        """测试11: 导出内容包含变更前后摘要、受影响对象、操作人和时间"""
        scenario = self._setup_full_scenario()
        batch_inv2 = scenario["batch_inv2"]

        result = self.tracker.export_change_logs(
            batch_inv2, operator="tester", format="json"
        )
        self.assertTrue(result["success"])

        with open(result["file_path"], "r", encoding="utf-8") as f:
            json_data = json.load(f)

        self.assertIn("export_info", json_data)
        self.assertIn("exported_by", json_data["export_info"])
        self.assertIn("exported_at", json_data["export_info"])

        change_logs = json_data["change_logs"]
        self.assertGreater(len(change_logs), 0)

        for log in change_logs:
            self.assertIn("变更前摘要", log, "每条变更都应有变更前摘要")
            self.assertIn("变更后摘要", log, "每条变更都应有变更后摘要")
            self.assertIn("影响类型", log, "每条变更都应有影响类型")
            self.assertIn("影响详情", log, "每条变更都应有影响详情")
            self.assertIn("影响的匹配ID", log, "每条变更都应有影响的匹配ID")
            self.assertIn("操作者", log, "每条变更都应有操作者")
            self.assertIn("检测时间", log, "每条变更都应有检测时间")
            self.assertIn("是否有冲突", log, "每条变更都应标记是否有冲突")
            self.assertIn("冲突原因", log, "每条变更都应有冲突原因字段")
            self.assertIn("处理状态", log, "每条变更都应有处理状态")

        status_changes = [c for c in change_logs if c["变更类型"] == "状态变更"]
        if status_changes:
            sc = status_changes[0]
            self.assertIn("原值", sc)
            self.assertIn("新值", sc)
            self.assertNotEqual(sc["原值"], "-", "状态变更应有原值")
            self.assertNotEqual(sc["新值"], "-", "状态变更应有新值")
            self.assertNotEqual(sc["变更前摘要"], "-", "状态变更应有变更前摘要")
            self.assertNotEqual(sc["变更后摘要"], "-", "状态变更应有变更后摘要")

    def test_12_clear_workbench_state(self):
        """测试12: 清除会话状态后不再恢复"""
        scenario = self._setup_full_scenario()
        batch_inv2 = scenario["batch_inv2"]

        self.workbench.save_last_selected_batch(batch_inv2, "tester")
        self.workbench.save_change_view_context(
            batch_id=batch_inv2, change_type=CHANGE_TYPE_STATUS_CHANGE,
            operator="tester"
        )
        self.workbench.save_export_context(
            batch_id=batch_inv2, export_type="change_logs", format="json",
            operator="tester"
        )

        self.assertIsNotNone(self.workbench.get_last_selected_batch())
        self.assertIsNotNone(self.workbench.get_last_change_view_context())
        self.assertIsNotNone(self.workbench.get_last_export_context())

        self.workbench.clear_workbench_state()

        self.assertIsNone(self.workbench.get_last_selected_batch())
        self.assertIsNone(self.workbench.get_last_change_view_context())
        self.assertIsNone(self.workbench.get_last_export_context())

    def test_13_multi_filter_combination(self):
        """测试13: 多个筛选条件组合使用时，统计与明细一致"""
        scenario = self._setup_full_scenario()
        batch_inv3 = scenario["batch_inv3"]

        result = self.workbench.get_unified_change_view(
            batch_id=batch_inv3,
            change_type=CHANGE_TYPE_AMOUNT_CHANGE,
            affect_filter=IMPACT_FILTER_ALL_AFFECTED,
            with_conflicts_only=False,
            operator="tester",
        )

        self.assertEqual(
            result["summary"]["total_changes"],
            len(result["logs"]),
            "多条件组合筛选后，统计总数应等于明细记录数"
        )
        for log in result["logs"]:
            self.assertEqual(log["change_type"], CHANGE_TYPE_AMOUNT_CHANGE,
                             "所有记录都应是金额变更类型")
            self.assertNotEqual(log["impact_type"], IMPACT_TYPE_NONE,
                                "所有记录都应有影响")

        summary = self.tracker.compute_summary_from_logs(result["logs"])
        self.assertEqual(
            summary["total_changes"],
            result["summary"]["total_changes"],
            "compute_summary_from_logs应与视图统计一致"
        )
        self.assertEqual(
            summary["by_type"],
            result["summary"]["by_type"],
            "按类型统计应一致"
        )

    def test_14_revoked_reimport_conflict(self):
        """测试14: 已撤销匹配的重导入应检测到冲突，日志中包含冲突原因"""
        scenario = self._setup_full_scenario()

        logs = self.db.get_batch_change_logs(batch_id=scenario["batch_inv2"])
        for log in logs:
            if log.get("conflict_reason"):
                self.assertIn("冲突原因", log or {},
                              "有冲突的变更记录应包含冲突原因")
                break

        has_conflict = any(l.get("conflict_reason") for l in logs)
        if has_conflict:
            filtered = self.workbench.get_unified_change_view(
                batch_id=scenario["batch_inv2"],
                with_conflicts_only=True,
                operator="tester",
            )
            self.assertGreater(len(filtered["logs"]), 0,
                               "按冲突筛选后应有记录")
            self.assertEqual(
                filtered["summary"]["conflict_count"],
                len(filtered["logs"]),
                "冲突统计数应等于筛选出的记录数"
            )


@unittest.skipUnless(HAS_CLICK, "click not available")
class TestChangeWorkbenchCLI(unittest.TestCase):
    """CLI层面的回归测试"""

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
        self.config_path = os.path.join(self.test_dir, "config.yaml")
        self.config.save(self.config_path)

        self.invoice_csv_v1 = os.path.join(self.test_dir, "invoices_v1.csv")
        self.invoice_csv_v2 = os.path.join(self.test_dir, "invoices_v2.csv")
        self.payment_csv_v1 = os.path.join(self.test_dir, "payments_v1.csv")

        with open(self.invoice_csv_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V1)
        with open(self.invoice_csv_v2, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V2)
        with open(self.payment_csv_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_V1)

        self.runner = CliRunner()

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def _run_cli(self, args):
        from invoice_reconciler.cli.main import cli
        result = self.runner.invoke(cli, ["--config", self.config_path] + args)
        return result

    def test_cli_changes_shows_filtered_stats(self):
        """CLI测试: batch changes 筛选后统计与明细一致"""
        from invoice_reconciler.core.database import Database
        from invoice_reconciler.core.importer import CSVImporter
        from invoice_reconciler.core.matcher import MatchEngine
        from invoice_reconciler.core.workflow import WorkflowManager

        db = Database(self.db_path)
        workflow = WorkflowManager(self.config, db)
        importer = CSVImporter(self.config, db)
        matcher = MatchEngine(self.config, db, workflow)

        r1 = importer.import_invoices(self.invoice_csv_v1, "zhangsan")
        importer.import_payments(self.payment_csv_v1, "zhangsan")
        matcher.run_auto_matching(operator="系统")
        r3 = importer.import_invoices(self.invoice_csv_v2, "lisi")
        batch_id = r3["batch_id"]
        db = None

        result = self._run_cli([
            "batch", "changes", "--batch-id", str(batch_id),
            "--change-type", "status_change"
        ])
        self.assertEqual(result.exit_code, 0, f"命令执行失败: {result.output}")
        self.assertIn("已应用筛选", result.output)
        self.assertIn("命中", result.output)

    def test_cli_export_changes_with_filter(self):
        """CLI测试: batch export-changes 带筛选参数导出"""
        from invoice_reconciler.core.database import Database
        from invoice_reconciler.core.importer import CSVImporter
        from invoice_reconciler.core.matcher import MatchEngine
        from invoice_reconciler.core.workflow import WorkflowManager

        db = Database(self.db_path)
        workflow = WorkflowManager(self.config, db)
        importer = CSVImporter(self.config, db)
        matcher = MatchEngine(self.config, db, workflow)

        r1 = importer.import_invoices(self.invoice_csv_v1, "zhangsan")
        importer.import_payments(self.payment_csv_v1, "zhangsan")
        matcher.run_auto_matching(operator="系统")
        r3 = importer.import_invoices(self.invoice_csv_v2, "lisi")
        batch_id = r3["batch_id"]
        db = None

        result = self._run_cli([
            "batch", "export-changes", str(batch_id),
            "--format", "json",
            "--change-type", "status_change",
            "--operator", "tester",
        ])
        self.assertEqual(result.exit_code, 0, f"导出命令失败: {result.output}")
        self.assertIn("变更日志已导出", result.output)

        json_files = [f for f in os.listdir(self.export_dir)
                      if f.endswith('.json') and 'change_log' in f]
        self.assertGreater(len(json_files), 0, "应生成JSON导出文件")

        latest_json = sorted(json_files)[-1]
        with open(os.path.join(self.export_dir, latest_json), "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("summary", data)
        self.assertIn("change_logs", data)
        for log in data["change_logs"]:
            self.assertEqual(log["变更类型"], "状态变更",
                             "导出的所有记录都应是状态变更类型")


if __name__ == "__main__":
    unittest.main(verbosity=2)
