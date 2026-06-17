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
from invoice_reconciler.core.reviewer import ReviewSnapshot
from invoice_reconciler.core.workflow import WorkflowManager


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


class TestWorkflowRestartRecovery(unittest.TestCase):
    """测试重启恢复锁状态"""

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

        self.importer.import_invoices(self.invoice_csv, "init_user")
        self.importer.import_payments(self.payment_csv, "init_user")
        self.matcher.run_auto_matching("init_user")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_restart_recovery_lock_persistence(self):
        """测试重启后锁状态恢复"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]

        result = self.workflow.acquire_lock(match_id, "user_a", "测试锁定")
        self.assertTrue(result["success"])

        del self.db
        del self.workflow

        new_db = Database(self.db_path)
        new_workflow = WorkflowManager(self.config, new_db)

        lock = new_db.get_match_lock(match_id)
        self.assertIsNotNone(lock)
        self.assertEqual(lock["lock_owner"], "user_a")
        self.assertEqual(lock["lock_reason"], "测试锁定")

        history = new_db.get_lock_history(match_id=match_id)
        self.assertGreaterEqual(len(history), 1)
        self.assertEqual(history[0]["action"], "lock")
        self.assertEqual(history[0]["operator"], "user_a")

    def test_restart_recovery_expired_lock(self):
        """测试重启后过期锁状态正确"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]

        self.workflow.acquire_lock(match_id, "user_a", "测试锁定")

        with self.db._get_conn() as conn:
            past_time = (datetime.now() - timedelta(seconds=7200)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE match_locks SET locked_at = ?, lock_expires_at = ? WHERE match_id = ?",
                (past_time, past_time, match_id)
            )

        del self.db
        del self.workflow

        new_db = Database(self.db_path)
        new_workflow = WorkflowManager(self.config, new_db)

        is_expired = new_db.is_lock_expired(match_id)
        self.assertTrue(is_expired)

        locks = new_workflow.list_all_locks(include_expired=False)
        lock_match_ids = [l["match_id"] for l in locks]
        self.assertNotIn(match_id, lock_match_ids)

        all_locks = new_workflow.list_all_locks(include_expired=True)
        all_match_ids = [l["match_id"] for l in all_locks]
        self.assertIn(match_id, all_match_ids)


class TestLockConflict(unittest.TestCase):
    """测试锁冲突"""

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

        self.importer.import_invoices(self.invoice_csv, "init_user")
        self.importer.import_payments(self.payment_csv, "init_user")
        self.matcher.run_auto_matching("init_user")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_same_record_cannot_be_locked_twice(self):
        """测试同一条记录不能被两个用户同时锁定"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]

        result_a = self.workflow.acquire_lock(match_id, "user_a", "用户A锁定")
        self.assertTrue(result_a["success"])

        with self.assertRaises(ValueError) as ctx:
            self.workflow.acquire_lock(match_id, "user_b", "用户B锁定")
        self.assertIn("已被", str(ctx.exception))

        lock = self.db.get_match_lock(match_id)
        self.assertEqual(lock["lock_owner"], "user_a")

    def test_reviewer_cannot_operate_others_lock(self):
        """测试普通复核员不能操作别人锁定的记录"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]

        self.workflow.acquire_lock(match_id, "user_a", "用户A锁定")

        can_operate, msg = self.workflow.can_operate_match("user_b", match_id)
        self.assertFalse(can_operate)
        self.assertIn("锁定", msg)

    def test_admin_can_force_unlock(self):
        """测试管理员可以强制解锁"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]

        self.workflow.acquire_lock(match_id, "user_a", "用户A锁定")

        result = self.workflow.force_unlock(match_id, "admin", "管理员强制解锁")
        self.assertTrue(result["success"])

        lock = self.db.get_match_lock(match_id)
        self.assertIsNone(lock)

        history = self.db.get_lock_history(match_id=match_id)
        actions = [h["action"] for h in history]
        self.assertIn("force_unlock", actions)

    def test_reviewer_cannot_force_unlock(self):
        """测试普通用户不能强制解锁"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]

        self.workflow.acquire_lock(match_id, "user_a", "用户A锁定")

        with self.assertRaises(ValueError) as ctx:
            self.workflow.force_unlock(match_id, "user_b", "用户B尝试强制解锁")
        self.assertIn("管理员", str(ctx.exception))

    def test_owner_can_release_lock(self):
        """测试锁持有人可以解锁"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]

        self.workflow.acquire_lock(match_id, "user_a", "用户A锁定")

        result = self.workflow.release_lock(match_id, "user_a", "完成处理")
        self.assertTrue(result["success"])

        lock = self.db.get_match_lock(match_id)
        self.assertIsNone(lock)


class TestTakeoverAndRevoke(unittest.TestCase):
    """测试接管后撤销重做"""

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
        self.revoker = Revoker(self.db, self.config, self.workflow)

        self.importer.import_invoices(self.invoice_csv, "init_user")
        self.importer.import_payments(self.payment_csv, "init_user")
        self.matcher.run_auto_matching("init_user")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_expired_lock_can_be_taken_over(self):
        """测试过期锁可以被接管"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]

        self.workflow.acquire_lock(match_id, "user_a", "用户A锁定")

        with self.db._get_conn() as conn:
            past_time = (datetime.now() - timedelta(seconds=7200)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE match_locks SET locked_at = ?, lock_expires_at = ? WHERE match_id = ?",
                (past_time, past_time, match_id)
            )

        result = self.workflow.takeover_lock(match_id, "user_b", "锁已过期，接管处理")
        self.assertTrue(result["success"])
        self.assertTrue(result.get("was_expired", False))
        self.assertEqual(result["new_owner"], "user_b")

        lock = self.db.get_match_lock(match_id)
        self.assertEqual(lock["lock_owner"], "user_b")

    def test_takeover_then_revoke_then_reconfirm(self):
        """测试接管后撤销再重新确认"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]
        match_no = match["match_no"]
        invoice_id = match["invoice_id"]
        payment_id = match["payment_id"]

        self.workflow.acquire_lock(match_id, "user_a", "用户A锁定")

        with self.db._get_conn() as conn:
            past_time = (datetime.now() - timedelta(seconds=7200)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE match_locks SET locked_at = ?, lock_expires_at = ? WHERE match_id = ?",
                (past_time, past_time, match_id)
            )

        self.workflow.takeover_lock(match_id, "user_b", "接管处理")

        revoke_result = self.revoker.revoke_match(match_id, "user_b", "接管后撤销重处理")
        self.assertTrue(revoke_result["success"])

        lock_after_revoke = self.db.get_match_lock(match_id)
        self.assertIsNone(lock_after_revoke)

        match_after_revoke = self.db.get_match_by_id(match_id)
        self.assertEqual(match_after_revoke["status"], MATCH_STATUS_REVOKED)

        reconfirm_result = self.matcher.manual_match(
            invoice_id, payment_id, "user_c", "重新人工匹配确认"
        )
        self.assertTrue(reconfirm_result["success"])

        new_match_no = reconfirm_result["match_no"]
        new_match = self.db.get_match_by_no(new_match_no)
        new_match_id = new_match["id"]

        lock_after_reconfirm = self.db.get_match_lock(new_match_id)
        self.assertIsNotNone(lock_after_reconfirm)
        self.assertEqual(lock_after_reconfirm["lock_owner"], "user_c")

        match_after_reconfirm = self.db.get_match_by_id(new_match_id)
        self.assertEqual(match_after_reconfirm["status"], MATCH_STATUS_MATCHED)

        history = self.db.get_lock_history(match_id=match_id)
        actions = [h["action"] for h in history]
        self.assertIn("lock", actions)
        self.assertIn("takeover", actions)
        self.assertIn("unlock", actions)


class TestPermissionBoundary(unittest.TestCase):
    """测试权限边界"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")

        self.config = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
            lock_timeout_seconds=3600,
            admin_users=["admin_user"],
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

        self.importer.import_invoices(self.invoice_csv, "init_user")
        self.importer.import_payments(self.payment_csv, "init_user")
        self.matcher.run_auto_matching("init_user")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_reviewer_can_lock_unlocked(self):
        """测试普通复核员可以锁定未锁定的记录"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]

        can_lock, msg = self.workflow.can_lock("reviewer_a", match_id)
        self.assertTrue(can_lock)

    def test_reviewer_cannot_lock_others(self):
        """测试普通复核员不能锁定别人已锁定的记录"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]

        self.workflow.acquire_lock(match_id, "reviewer_a", "A锁定")

        can_lock, msg = self.workflow.can_lock("reviewer_b", match_id)
        self.assertFalse(can_lock)

    def test_admin_can_force_unlock_all(self):
        """测试管理员可以强制解锁任何记录"""
        matches = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
        for m in matches[:3]:
            self.workflow.acquire_lock(m["id"], "reviewer_a", "A锁定")

        result = self.workflow.batch_force_unlock("admin_user", "管理员批量解锁")
        self.assertTrue(result["success"])
        self.assertGreaterEqual(result["unlocked_count"], 3)

        locks = self.workflow.list_all_locks()
        self.assertEqual(len(locks), 0)

    def test_admin_can_lock_anything(self):
        """测试管理员可以操作任何记录，即使已被锁定"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]

        self.workflow.acquire_lock(match_id, "reviewer_a", "A锁定")

        can_operate, msg = self.workflow.can_operate_match("admin_user", match_id)
        self.assertTrue(can_operate)

    def test_user_role_from_config(self):
        """测试用户角色可以从配置中获取"""
        self.assertTrue(self.workflow.is_admin("admin_user"))
        self.assertFalse(self.workflow.is_admin("reviewer_a"))

    def test_update_user_role(self):
        """测试更新用户角色"""
        self.workflow.ensure_user("reviewer_b")
        self.assertFalse(self.workflow.is_admin("reviewer_b"))

        self.workflow.update_user_role("admin_user", "reviewer_b", "admin")
        self.assertTrue(self.workflow.is_admin("reviewer_b"))

        self.workflow.update_user_role("admin_user", "reviewer_b", "reviewer")
        self.assertFalse(self.workflow.is_admin("reviewer_b"))

    def test_transfer_lock_owner_to_other(self):
        """测试锁持有人可以转交锁"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]

        self.workflow.acquire_lock(match_id, "reviewer_a", "A锁定")

        result = self.workflow.transfer_lock(match_id, "reviewer_a", "reviewer_b", "转交处理")
        self.assertTrue(result["success"])
        self.assertEqual(result["old_owner"], "reviewer_a")
        self.assertEqual(result["new_owner"], "reviewer_b")

        lock = self.db.get_match_lock(match_id)
        self.assertEqual(lock["lock_owner"], "reviewer_b")

    def test_transfer_lock_not_owner_fails(self):
        """测试非锁持有人不能转交"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]

        self.workflow.acquire_lock(match_id, "reviewer_a", "A锁定")

        with self.assertRaises(ValueError) as ctx:
            self.workflow.transfer_lock(match_id, "reviewer_c", "reviewer_b", "非法转交")
        self.assertIn("自己锁定", str(ctx.exception))


class TestExportAndReplay(unittest.TestCase):
    """测试导出回放包含锁信息"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")
        os.makedirs(self.export_dir, exist_ok=True)

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
        self.revoker = Revoker(self.db, self.config, self.workflow)
        self.exporter = ReportExporter(self.config, self.db)
        self.reviewer = ReviewSnapshot(self.db)

        self.importer.import_invoices(self.invoice_csv, "init_user")
        self.importer.import_payments(self.payment_csv, "init_user")
        self.matcher.run_auto_matching("init_user")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_snapshot_contains_lock_info(self):
        """测试快照包含锁信息"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]
        match_no = match["match_no"]

        self.workflow.acquire_lock(match_id, "reviewer_a", "复核中")

        snapshot = self.reviewer.create_snapshot(
            snapshot_type="manual",
            description="测试快照",
            operator="admin"
        )
        self.assertIsNotNone(snapshot)
        self.assertIn("snapshot_no", snapshot)

        snapshot_data = self.reviewer.get_snapshot_for_export(snapshot_no=snapshot["snapshot_no"])
        items = snapshot_data["items"]

        matched_items = [item for item in items if item["匹配编号"] == match_no]
        self.assertEqual(len(matched_items), 1)

        item = matched_items[0]
        self.assertEqual(item["当前责任人"], "reviewer_a")
        self.assertEqual(item["锁定原因"], "复核中")
        self.assertIn("锁定", item["锁历史"])
        self.assertIn("最后确认证据", item)

    def test_snapshot_stable_id_on_reexport(self):
        """测试重复导出快照编号稳定"""
        snapshot1 = self.reviewer.create_snapshot(
            snapshot_type="manual",
            description="测试快照1",
            operator="admin"
        )
        snapshot2 = self.reviewer.create_snapshot(
            snapshot_type="manual",
            description="测试快照2",
            operator="admin"
        )

        self.assertNotEqual(snapshot1["snapshot_no"], snapshot2["snapshot_no"])

        export_data = self.reviewer.get_snapshot_for_export(snapshot_no=snapshot1["snapshot_no"])
        items = export_data["items"]

        match_nos = [item["匹配编号"] for item in items]
        self.assertEqual(len(match_nos), len(set(match_nos)))

    def test_replay_with_lock_info(self):
        """测试回放校验输出包含锁信息上下文"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]
        match_no = match["match_no"]

        self.workflow.acquire_lock(match_id, "reviewer_a", "初始锁定")

        snapshot = self.reviewer.create_snapshot(
            snapshot_type="manual",
            description="基线快照",
            operator="admin"
        )
        snapshot_no = snapshot["snapshot_no"]

        snapshot_data = self.reviewer.get_snapshot_for_export(snapshot_no=snapshot_no)
        self.assertIn("items", snapshot_data)
        self.assertGreater(len(snapshot_data["items"]), 0)

        first_item = snapshot_data["items"][0]
        self.assertIn("当前责任人", first_item)
        self.assertIn("锁历史", first_item)
        self.assertIn("接管原因", first_item)
        self.assertIn("最后确认证据", first_item)

        replay_result = self.reviewer.replay_verify(snapshot_no=snapshot_no)
        self.assertIn("snapshot_no", replay_result)
        self.assertIn("differences", replay_result)
        self.assertIn("is_consistent", replay_result)
        self.assertTrue(replay_result["is_consistent"])

    def test_import_old_batch_preserves_locks(self):
        """测试导入旧批次不冲掉已有锁"""
        match = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)[0]
        match_id = match["id"]

        self.workflow.acquire_lock(match_id, "reviewer_a", "锁定测试")

        lock_before = self.db.get_match_lock(match_id)
        history_before = self.db.get_lock_history(match_id=match_id)

        self.importer.import_invoices(self.invoice_csv, "reimport_user")

        lock_after = self.db.get_match_lock(match_id)
        self.assertIsNotNone(lock_after)
        self.assertEqual(lock_after["lock_owner"], lock_before["lock_owner"])
        self.assertEqual(lock_after["lock_reason"], lock_before["lock_reason"])

        history_after = self.db.get_lock_history(match_id=match_id)
        self.assertEqual(len(history_after), len(history_before))


class TestStrictLockBeforeOperate(unittest.TestCase):
    """测试严格的先锁再处理机制"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")

        self.config = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
            lock_timeout_seconds=3600,
            admin_users=["admin_user"],
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
        self.revoker = Revoker(self.db, self.config, self.workflow)

        self.importer.import_invoices(self.invoice_csv, "init_user")
        self.importer.import_payments(self.payment_csv, "init_user")
        self.matcher.run_auto_matching("init_user")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_reviewer_cannot_confirm_unlocked_record(self):
        """测试普通复核员不能确认未锁定的记录"""
        pending = self.db.get_matches_by_status(MATCH_STATUS_PENDING)
        if not pending:
            matched = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
            match_id = matched[0]["id"]
            with self.assertRaises(ValueError) as ctx:
                self.matcher.confirm_match(match_id, "reviewer_x", "尝试不锁定确认")
            self.assertIn("锁定", str(ctx.exception))
            self.assertIn("lock acquire", str(ctx.exception))
        else:
            match_id = pending[0]["id"]
            with self.assertRaises(ValueError) as ctx:
                self.matcher.confirm_match(match_id, "reviewer_x", "尝试不锁定确认")
            self.assertIn("锁定", str(ctx.exception))

    def test_reviewer_cannot_reject_unlocked_record(self):
        """测试普通复核员不能拒绝未锁定的记录"""
        pending = self.db.get_matches_by_status(MATCH_STATUS_PENDING)
        if pending:
            match_id = pending[0]["id"]
            with self.assertRaises(ValueError) as ctx:
                self.matcher.reject_match(match_id, "reviewer_x", "不锁定就拒绝")
            self.assertIn("锁定", str(ctx.exception))

    def test_reviewer_cannot_revoke_unlocked_record(self):
        """测试普通复核员不能撤销未锁定的已确认记录"""
        matched = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
        self.assertGreater(len(matched), 0)
        match_id = matched[0]["id"]

        result = self.revoker.revoke_match(match_id, "reviewer_x", "不锁定就撤销")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_type"], "lock_violation")
        self.assertIn("锁定", result["message"])

    def test_locked_then_confirm_success(self):
        """测试锁定后确认成功"""
        pending = self.db.get_matches_by_status(MATCH_STATUS_PENDING)
        if pending:
            match_id = pending[0]["id"]
            self.workflow.acquire_lock(match_id, "reviewer_ok", "开始复核")
            result = self.matcher.confirm_match(match_id, "reviewer_ok", "锁定后确认")
            self.assertTrue(result["success"])

            m = self.db.get_match_by_id(match_id)
            self.assertEqual(m["status"], MATCH_STATUS_MATCHED)
            self.assertEqual(m["operator"], "reviewer_ok")

            lock = self.db.get_match_lock(match_id)
            self.assertIsNotNone(lock)
            self.assertEqual(lock["lock_owner"], "reviewer_ok")

    def test_locked_then_reject_success(self):
        """测试锁定后拒绝成功"""
        pending = self.db.get_matches_by_status(MATCH_STATUS_PENDING)
        if pending:
            match_id = pending[0]["id"]
            self.workflow.acquire_lock(match_id, "reviewer_ok", "开始复核")
            result = self.matcher.reject_match(match_id, "reviewer_ok", "锁定后拒绝")
            self.assertTrue(result["success"])

            m = self.db.get_match_by_id(match_id)
            self.assertEqual(m["status"], MATCH_STATUS_EXCEPTION)

    def test_locked_then_revoke_success(self):
        """测试锁定后撤销成功"""
        matched = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
        self.assertGreater(len(matched), 0)
        match_id = matched[0]["id"]

        self.workflow.acquire_lock(match_id, "reviewer_ok", "锁定准备撤销")
        result = self.revoker.revoke_match(match_id, "reviewer_ok", "锁定后撤销")
        self.assertTrue(result["success"])

        m = self.db.get_match_by_id(match_id)
        self.assertEqual(m["status"], MATCH_STATUS_REVOKED)

    def test_admin_bypass_lock_for_confirm(self):
        """测试管理员可以跳过锁确认"""
        pending = self.db.get_matches_by_status(MATCH_STATUS_PENDING)
        if pending:
            match_id = pending[0]["id"]
            self.workflow.acquire_lock(match_id, "other_reviewer", "其他人锁定")
            result = self.matcher.confirm_match(match_id, "admin_user", "管理员跳过锁")
            self.assertTrue(result["success"])

    def test_admin_bypass_lock_for_revoke(self):
        """测试管理员可以跳过锁撤销"""
        matched = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
        self.assertGreater(len(matched), 0)
        match_id = matched[0]["id"]

        self.workflow.acquire_lock(match_id, "other_reviewer", "其他人锁定")
        result = self.revoker.revoke_match(match_id, "admin_user", "管理员撤销")
        self.assertTrue(result["success"])


class TestTakeoverRevokePreserveHistory(unittest.TestCase):
    """测试接管后撤销重做不冲掉历史"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")

        self.config = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
            lock_timeout_seconds=3600,
            admin_users=["admin_user"],
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
        self.revoker = Revoker(self.db, self.config, self.workflow)

        self.importer.import_invoices(self.invoice_csv, "init_user")
        self.importer.import_payments(self.payment_csv, "init_user")
        self.matcher.run_auto_matching("init_user")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_takeover_then_revoke_preserves_lock_history(self):
        """测试接管后撤销，锁历史保留"""
        matched = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
        match_id = matched[0]["id"]
        invoice_id = matched[0]["invoice_id"]
        payment_id = matched[0]["payment_id"]

        self.workflow.acquire_lock(match_id, "user_a", "用户A锁定处理")

        with self.db._get_conn() as conn:
            past = (datetime.now() - timedelta(seconds=7200)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE match_locks SET locked_at = ?, lock_expires_at = ? WHERE match_id = ?",
                (past, past, match_id)
            )

        history_before = self.db.get_lock_history(match_id=match_id)
        actions_before = [h["action"] for h in history_before]

        self.workflow.takeover_lock(match_id, "user_b", "接管过期锁处理")
        self.revoker.revoke_match(match_id, "user_b", "接管后发现错误，撤销")

        history_after = self.db.get_lock_history(match_id=match_id)
        actions_after = [h["action"] for h in history_after]

        for a in actions_before:
            self.assertIn(a, actions_after)

        self.assertIn("takeover", actions_after)
        self.assertIn("unlock", actions_after)

        operators_after = [h["operator"] for h in history_after]
        self.assertIn("user_a", operators_after)
        self.assertIn("user_b", operators_after)

    def test_reconfirm_after_takeover_revoke_preserves_old_history(self):
        """测试接管撤销后重新确认，旧历史不冲掉"""
        matched = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
        match_id = matched[0]["id"]
        invoice_id = matched[0]["invoice_id"]
        payment_id = matched[0]["payment_id"]
        old_match_no = matched[0]["match_no"]

        self.workflow.acquire_lock(match_id, "user_a", "初始锁定")
        with self.db._get_conn() as conn:
            past = (datetime.now() - timedelta(seconds=7200)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE match_locks SET locked_at = ?, lock_expires_at = ? WHERE match_id = ?",
                (past, past, match_id)
            )

        old_status_history = self.db.get_status_history(match_id=match_id)

        self.workflow.takeover_lock(match_id, "user_b", "用户B接管")
        self.revoker.revoke_match(match_id, "user_b", "用户B撤销")

        old_lock_history = self.db.get_lock_history(match_id=match_id)
        old_lock_actions = [h["action"] for h in old_lock_history]

        new_result = self.matcher.manual_match(
            invoice_id, payment_id, "user_c", "用户C重做确认"
        )
        new_match = self.db.get_match_by_no(new_result["match_no"])

        status_history = self.db.get_status_history(match_id=match_id)
        self.assertEqual(len(status_history), len(old_status_history) + 1)

        lock_history = self.db.get_lock_history(match_id=match_id)
        lock_actions = [h["action"] for h in lock_history]
        for a in old_lock_actions:
            self.assertIn(a, lock_actions)

        new_lock_history = self.db.get_lock_history(match_id=new_match["id"])
        self.assertGreaterEqual(len(new_lock_history), 1)
        lock_owners_new = [h["new_owner"] for h in new_lock_history if h.get("new_owner")]
        self.assertIn("user_c", lock_owners_new)


class TestExportContainsLockInfo(unittest.TestCase):
    """测试导出包含完整锁信息"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")
        os.makedirs(self.export_dir, exist_ok=True)

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
        self.exporter = ReportExporter(self.config, self.db)
        self.reviewer = ReviewSnapshot(self.db)

        self.importer.import_invoices(self.invoice_csv, "init_user")
        self.importer.import_payments(self.payment_csv, "init_user")
        self.matcher.run_auto_matching("init_user")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_json_export_contains_lock_history_and_evidence(self):
        """测试 JSON 完整导出包含锁历史、接管原因、确认证据"""
        matched = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
        self.assertGreater(len(matched), 0)
        match_id = matched[0]["id"]

        self.workflow.acquire_lock(match_id, "user_a", "测试锁定")

        result = self.exporter.export_full_report("operator_test", format="json")
        self.assertTrue(result["success"])
        self.assertTrue(os.path.exists(result["file_path"]))

        with open(result["file_path"], "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("已匹配", data)
        self.assertGreater(len(data["已匹配"]), 0)

        first_matched = data["已匹配"][0]
        self.assertIn("当前责任人", first_matched)
        self.assertIn("锁历史", first_matched)
        self.assertIn("接管原因", first_matched)
        self.assertIn("最后确认证据", first_matched)

        self.assertEqual(first_matched["当前责任人"], "user_a")
        self.assertIn("锁定", first_matched["锁历史"])
        self.assertIn("确认人", first_matched["最后确认证据"])
        self.assertIn("匹配类型", first_matched["最后确认证据"])

    def test_csv_export_sheets_contain_lock_columns(self):
        """测试 CSV 导出的各个 sheet 包含锁相关列"""
        matched = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
        self.assertGreater(len(matched), 0)
        match_id = matched[0]["id"]
        self.workflow.acquire_lock(match_id, "csv_user", "CSV测试锁定")

        result = self.exporter.export_full_report("op_csv", format="csv")
        self.assertTrue(result["success"])
        self.assertTrue(os.path.isdir(result["file_path"]))

        matched_csv = os.path.join(result["file_path"], "已匹配.csv")
        self.assertTrue(os.path.exists(matched_csv))

        with open(matched_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []

        self.assertIn("当前责任人", headers)
        self.assertIn("锁历史", headers)
        self.assertIn("接管原因", headers)
        self.assertIn("最后确认证据", headers)

    def test_pending_export_contains_lock_info(self):
        """测试待确认导出也包含锁信息"""
        pending = self.db.get_matches_by_status(MATCH_STATUS_PENDING)
        if pending:
            match_id = pending[0]["id"]
            self.workflow.acquire_lock(match_id, "pending_user", "待确认锁定")

        result = self.exporter.export_full_report("op_pending", format="json")
        with open(result["file_path"], "r", encoding="utf-8") as f:
            data = json.load(f)

        if "待确认" in data and len(data["待确认"]) > 0:
            first_pending = data["待确认"][0]
            self.assertIn("当前责任人", first_pending)
            self.assertIn("锁历史", first_pending)
            self.assertIn("接管原因", first_pending)
            self.assertIn("最后确认证据", first_pending)


class TestStartupRestoresLockConfig(unittest.TestCase):
    """测试程序启动后按配置恢复锁状态"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")

        self.invoice_csv = os.path.join(self.test_dir, "invoices.csv")
        self.payment_csv = os.path.join(self.test_dir, "payments.csv")
        with open(self.invoice_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_CSV)
        with open(self.payment_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_CSV)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_restart_refreshes_expire_time_from_config(self):
        """测试重启后根据配置刷新锁过期时间"""
        config_short = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
            lock_timeout_seconds=60,
            admin_users=["admin"],
            enable_lock=True,
        )

        db1 = Database(self.db_path)
        wf1 = WorkflowManager(config_short, db1)
        imp = CSVImporter(config_short, db1)
        mch = MatchEngine(config_short, db1, wf1)

        imp.import_invoices(self.invoice_csv, "u1")
        imp.import_payments(self.payment_csv, "u1")
        mch.run_auto_matching("u1")

        matched = db1.get_matches_by_status(MATCH_STATUS_MATCHED)
        match_id = matched[0]["id"]

        wf1.acquire_lock(match_id, "user_1", "短超时锁")

        lock_before = db1.get_match_lock(match_id)
        locked_at = datetime.strptime(lock_before["locked_at"], "%Y-%m-%d %H:%M:%S")
        old_expires = datetime.strptime(lock_before["lock_expires_at"], "%Y-%m-%d %H:%M:%S")
        expected_old_expires = locked_at + timedelta(seconds=60)
        self.assertEqual((old_expires - locked_at).total_seconds(), 60)

        del db1
        del wf1

        config_long = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
            lock_timeout_seconds=7200,
            admin_users=["admin"],
            enable_lock=True,
        )

        db2 = Database(self.db_path)
        wf2 = WorkflowManager(config_long, db2)
        restore_result = wf2.restore_locks_on_startup()

        self.assertTrue(restore_result["restored"])
        self.assertGreaterEqual(restore_result["total_locks"], 1)
        self.assertEqual(restore_result["config_timeout"], 7200)

        lock_after = db2.get_match_lock(match_id)
        new_expires = datetime.strptime(lock_after["lock_expires_at"], "%Y-%m-%d %H:%M:%S")
        expected_new_expires = locked_at + timedelta(seconds=7200)
        self.assertEqual((new_expires - locked_at).total_seconds(), 7200)

        self.assertGreater(new_expires, old_expires)

    def test_restart_reports_correct_expired_count(self):
        """测试重启后正确报告过期锁数量"""
        config = Config(
            db_path=self.db_path,
            export_dir=self.export_dir,
            lock_timeout_seconds=3600,
            admin_users=["admin"],
            enable_lock=True,
        )

        db1 = Database(self.db_path)
        wf1 = WorkflowManager(config, db1)
        imp = CSVImporter(config, db1)
        mch = MatchEngine(config, db1, wf1)

        imp.import_invoices(self.invoice_csv, "u1")
        imp.import_payments(self.payment_csv, "u1")
        mch.run_auto_matching("u1")

        matched = db1.get_matches_by_status(MATCH_STATUS_MATCHED)
        self.assertGreaterEqual(len(matched), 2)

        wf1.acquire_lock(matched[0]["id"], "user_a", "正常锁")
        wf1.acquire_lock(matched[1]["id"], "user_b", "即将过期锁")

        with db1._get_conn() as conn:
            past = (datetime.now() - timedelta(seconds=7200)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE match_locks SET locked_at = ?, lock_expires_at = ? WHERE lock_owner = ?",
                (past, past, "user_b")
            )

        del db1
        del wf1

        db2 = Database(self.db_path)
        wf2 = WorkflowManager(config, db2)
        restore = wf2.restore_locks_on_startup()

        self.assertEqual(restore["total_locks"], 2)
        self.assertEqual(restore["expired_locks"], 1)


if __name__ == "__main__":
    unittest.main()
