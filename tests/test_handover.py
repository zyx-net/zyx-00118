import os
import sys
import json
import tempfile
import unittest
import shutil
import stat
import yaml
from datetime import datetime
from click.testing import CliRunner

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
from invoice_reconciler.core.batch_workbench import BatchWorkbench
from invoice_reconciler.core.handover import (
    BatchHandover,
    HANDOVER_STATUS_ACTIVE,
    HANDOVER_STATUS_DISCARDED,
    HANDOVER_STATUS_RESTORED,
    HANDOVER_EVENT_CREATE,
    HANDOVER_CONFLICT_REIMPORT,
    HANDOVER_CONFLICT_CONFIG_MISMATCH,
    HANDOVER_CONFLICT_EXPORT_NOT_WRITABLE,
)
from invoice_reconciler.cli.main import cli


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


class TestHandoverCreateAndOverwrite(unittest.TestCase):

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
        self.importer = CSVImporter(self.config, self.db)
        self.workbench = BatchWorkbench(self.config, self.db)
        self.handover = BatchHandover(self.config, self.db)

        inv_result = self.importer.import_invoices(self.invoice_csv, "init_user")
        pay_result = self.importer.import_payments(self.payment_csv, "init_user")
        self.inv_batch_id = inv_result["batch_id"]
        self.pay_batch_id = pay_result["batch_id"]

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_create_package_returns_success(self):
        result = self.handover.create_package("test_user", description="first package")
        self.assertTrue(result["success"])
        self.assertIn("package_id", result)
        self.assertEqual(result["status"], HANDOVER_STATUS_ACTIVE)
        self.assertIn("config_hash", result)
        self.assertIn("created_at", result)

    def test_package_id_increments_on_same_day(self):
        result1 = self.handover.create_package("user_a", description="first")
        result2 = self.handover.create_package("user_a", description="second")
        self.assertNotEqual(result1["package_id"], result2["package_id"])

        prefix = "HP" + datetime.now().strftime("%Y%m%d")
        self.assertTrue(result1["package_id"].startswith(prefix))
        self.assertTrue(result2["package_id"].startswith(prefix))

        seq1 = int(result1["package_id"][-3:])
        seq2 = int(result2["package_id"][-3:])
        self.assertEqual(seq2, seq1 + 1)

    def test_create_package_captures_current_batch_state(self):
        self.workbench.save_last_selected_batch(self.inv_batch_id, "test_user")
        self.workbench.save_filters(operator="test_operator", status=MATCH_STATUS_PENDING)

        result = self.handover.create_package("test_user", description="with state")
        self.assertTrue(result["success"])

        package = self.db.get_handover_package(result["package_id"])
        self.assertIsNotNone(package)

        state = self.handover._extract_package_state(package)

        batch_info = state.get("last_selected_batch")
        self.assertIsNotNone(batch_info)
        self.assertEqual(batch_info.get("batch_id"), self.inv_batch_id)

        filters = state.get("filters")
        self.assertIsNotNone(filters)
        self.assertEqual(filters.get("operator"), "test_operator")
        self.assertEqual(filters.get("status"), MATCH_STATUS_PENDING)

    def test_create_with_explicit_batch_id(self):
        result = self.handover.create_package(
            "test_user", batch_id=self.inv_batch_id, description="explicit batch"
        )
        self.assertTrue(result["success"])

        package = self.db.get_handover_package(result["package_id"])
        state = self.handover._extract_package_state(package)

        batch_info = state.get("last_selected_batch")
        self.assertIsNotNone(batch_info)
        self.assertEqual(batch_info.get("batch_id"), self.inv_batch_id)

    def test_overwrite_by_creating_new_package(self):
        self.workbench.save_last_selected_batch(self.inv_batch_id, "user_a")
        result1 = self.handover.create_package("user_a", description="original")
        self.assertTrue(result1["success"])

        self.workbench.save_last_selected_batch(self.pay_batch_id, "user_a")
        result2 = self.handover.create_package("user_a", description="updated")

        self.assertTrue(result2["success"])
        self.assertNotEqual(result1["package_id"], result2["package_id"])

        pkg1 = self.db.get_handover_package(result1["package_id"])
        pkg2 = self.db.get_handover_package(result2["package_id"])
        self.assertIsNotNone(pkg1)
        self.assertIsNotNone(pkg2)

        state2 = self.handover._extract_package_state(pkg2)
        batch_info = state2.get("last_selected_batch")
        self.assertIsNotNone(batch_info)
        self.assertEqual(batch_info.get("batch_id"), self.pay_batch_id)

    def test_list_packages_shows_created(self):
        self.handover.create_package("user_a", description="pkg1")
        self.handover.create_package("user_a", description="pkg2")

        packages = self.handover.list_packages()
        self.assertGreaterEqual(len(packages), 2)

        for p in packages:
            self.assertIn("package_id", p)
            self.assertIn("status", p)
            self.assertIn("created_at", p)


class TestHandoverRestartRecovery(unittest.TestCase):

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
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db, None)
        self.workbench = BatchWorkbench(self.config, self.db)
        self.handover = BatchHandover(self.config, self.db)

        inv_result = self.importer.import_invoices(self.invoice_csv, "init_user")
        self.importer.import_payments(self.payment_csv, "init_user")
        self.inv_batch_id = inv_result["batch_id"]
        self.matcher.run_auto_matching("init_user")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_restore_recovers_batch_selection(self):
        self.workbench.save_last_selected_batch(self.inv_batch_id, "alice")
        result = self.handover.create_package("alice", description="before restart")
        self.assertTrue(result["success"])
        package_id = result["package_id"]

        self.workbench.clear_workbench_state()
        restored = self.workbench.restore_workbench_state()
        self.assertFalse(restored["restored"])

        new_db = Database(self.db_path)
        new_workbench = BatchWorkbench(self.config, new_db)
        new_handover = BatchHandover(self.config, new_db)

        restore_result = new_handover.restore_package(package_id, "alice")
        self.assertTrue(restore_result["success"])

        last_batch = new_workbench.get_last_selected_batch()
        self.assertIsNotNone(last_batch)
        self.assertEqual(last_batch["batch_id"], self.inv_batch_id)

    def test_restore_recovers_filters(self):
        self.workbench.save_last_selected_batch(self.inv_batch_id, "alice")
        self.workbench.save_filters(operator="alice", status=MATCH_STATUS_PENDING)

        result = self.handover.create_package("alice", description="with filters")
        package_id = result["package_id"]

        self.workbench.clear_workbench_state()

        new_db = Database(self.db_path)
        new_workbench = BatchWorkbench(self.config, new_db)
        new_handover = BatchHandover(self.config, new_db)

        restore_result = new_handover.restore_package(package_id, "alice")
        self.assertTrue(restore_result["success"])

        filters = new_workbench.get_filters()
        self.assertIsNotNone(filters)
        self.assertEqual(filters.get("operator"), "alice")
        self.assertEqual(filters.get("status"), MATCH_STATUS_PENDING)

    def test_filters_not_silently_dropped(self):
        self.workbench.save_last_selected_batch(self.inv_batch_id, "bob")
        self.workbench.save_filters(operator="bob", status=MATCH_STATUS_EXCEPTION)

        result = self.handover.create_package("bob", description="filters test")
        package_id = result["package_id"]

        preview = self.handover.preview_package(package_id)
        self.assertTrue(preview["success"])
        self.assertTrue(preview["preview"]["will_restore_filters"])

        preview_filters = preview["preview"]["filters"]
        self.assertEqual(preview_filters.get("operator"), "bob")
        self.assertEqual(preview_filters.get("status"), MATCH_STATUS_EXCEPTION)

    def test_full_restart_recovery_workflow(self):
        self.workbench.save_last_selected_batch(self.inv_batch_id, "charlie")
        self.workbench.save_filters(operator="charlie", status=MATCH_STATUS_MATCHED)

        result = self.handover.create_package("charlie", description="full workflow")
        package_id = result["package_id"]

        self.workbench.clear_workbench_state()
        del self.workbench
        del self.handover
        del self.db

        new_db = Database(self.db_path)
        new_workbench = BatchWorkbench(self.config, new_db)
        new_handover = BatchHandover(self.config, new_db)

        restore_result = new_handover.restore_package(package_id, "charlie")
        self.assertTrue(restore_result["success"])
        self.assertIn("last_selected_batch", restore_result.get("applied", []))
        self.assertIn("filters", restore_result.get("applied", []))

        last_batch = new_workbench.get_last_selected_batch()
        self.assertIsNotNone(last_batch)
        self.assertEqual(last_batch["batch_id"], self.inv_batch_id)

        filters = new_workbench.get_filters()
        self.assertEqual(filters.get("operator"), "charlie")
        self.assertEqual(filters.get("status"), MATCH_STATUS_MATCHED)


class TestHandoverConfigIsolation(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path_a = os.path.join(self.test_dir, "test_a.db")
        self.db_path_b = os.path.join(self.test_dir, "test_b.db")
        self.export_dir_a = os.path.join(self.test_dir, "exports_a")
        self.export_dir_b = os.path.join(self.test_dir, "exports_b")

        self.config_a = Config(
            db_path=self.db_path_a,
            export_dir=self.export_dir_a,
            lock_timeout_seconds=3600,
            admin_users=["admin"],
            enable_lock=True,
        )

        self.config_b = Config(
            db_path=self.db_path_b,
            export_dir=self.export_dir_b,
            lock_timeout_seconds=7200,
            admin_users=["admin"],
            enable_lock=False,
        )

        self.invoice_csv = os.path.join(self.test_dir, "invoices.csv")
        self.payment_csv = os.path.join(self.test_dir, "payments.csv")
        with open(self.invoice_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_CSV)
        with open(self.payment_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_CSV)

        self.db_a = Database(self.db_path_a)
        self.db_b = Database(self.db_path_b)

        self.importer_a = CSVImporter(self.config_a, self.db_a)
        self.importer_b = CSVImporter(self.config_b, self.db_b)

        inv_a = self.importer_a.import_invoices(self.invoice_csv, "user_a")
        inv_b = self.importer_b.import_invoices(self.invoice_csv, "user_b")
        self.inv_batch_id_a = inv_a["batch_id"]
        self.inv_batch_id_b = inv_b["batch_id"]

        self.handover_a = BatchHandover(self.config_a, self.db_a)
        self.handover_b = BatchHandover(self.config_b, self.db_b)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_different_configs_have_different_hashes(self):
        hash_a = self.handover_a._compute_config_hash()
        hash_b = self.handover_b._compute_config_hash()
        self.assertNotEqual(hash_a, hash_b)

    def test_packages_isolated_by_config_hash(self):
        result_a = self.handover_a.create_package("user_a", description="config A package")
        result_b = self.handover_b.create_package("user_b", description="config B package")

        self.assertTrue(result_a["success"])
        self.assertTrue(result_b["success"])

        packages_a = self.handover_a.list_packages()
        packages_b = self.handover_b.list_packages()

        a_ids = {p["package_id"] for p in packages_a}
        b_ids = {p["package_id"] for p in packages_b}

        self.assertIn(result_a["package_id"], a_ids)

        self.assertIn(result_b["package_id"], b_ids)

        self.assertEqual(len(packages_a), 1)
        self.assertEqual(len(packages_b), 1)

    def test_restore_isolated_by_config(self):
        workbench_a = BatchWorkbench(self.config_a, self.db_a)
        workbench_a.save_last_selected_batch(self.inv_batch_id_a, "user_a")

        result_a = self.handover_a.create_package("user_a", description="A only")
        self.assertTrue(result_a["success"])

        packages_b = self.handover_b.list_packages()
        self.assertEqual(len(packages_b), 0)

    def test_config_mismatch_detected_on_restore(self):
        workbench_a = BatchWorkbench(self.config_a, self.db_a)
        workbench_a.save_last_selected_batch(self.inv_batch_id_a, "user_a")

        result_a = self.handover_a.create_package("user_a", description="A package")
        package_id_a = result_a["package_id"]

        handover_b_with_a_db = BatchHandover(self.config_b, self.db_a)
        restore_result = handover_b_with_a_db.restore_package(package_id_a, "user_b")

        self.assertFalse(restore_result["success"])
        self.assertIn("conflicts", restore_result)

        conflict_types = [c["conflict_type"] for c in restore_result["conflicts"]]
        self.assertIn(HANDOVER_CONFLICT_CONFIG_MISMATCH, conflict_types)


class TestHandoverDuplicateImportConflict(unittest.TestCase):

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
        self.invoice_updated_csv = os.path.join(self.test_dir, "invoices_updated.csv")

        with open(self.invoice_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_CSV)
        with open(self.payment_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_CSV)
        with open(self.invoice_updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)

        self.db = Database(self.db_path)
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db, None)
        self.workbench = BatchWorkbench(self.config, self.db)
        self.handover = BatchHandover(self.config, self.db)

        inv_result = self.importer.import_invoices(self.invoice_csv, "init_user")
        self.importer.import_payments(self.payment_csv, "init_user")
        self.inv_batch_id = inv_result["batch_id"]
        self.matcher.run_auto_matching("init_user")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_reimport_creates_conflict_on_restore(self):
        self.workbench.save_last_selected_batch(self.inv_batch_id, "alice")

        pkg_result = self.handover.create_package("alice", description="before reimport")
        self.assertTrue(pkg_result["success"])
        package_id = pkg_result["package_id"]

        updated_result = self.importer.import_invoices(self.invoice_updated_csv, "bob")
        self.assertGreater(updated_result["batch_id"], self.inv_batch_id)

        restore_result = self.handover.restore_package(package_id, "alice")
        self.assertFalse(restore_result["success"])
        self.assertIn("conflicts", restore_result)

        conflict_types = [c["conflict_type"] for c in restore_result["conflicts"]]
        self.assertIn(HANDOVER_CONFLICT_REIMPORT, conflict_types)

    def test_force_restore_succeeds_despite_reimport(self):
        self.workbench.save_last_selected_batch(self.inv_batch_id, "alice")

        pkg_result = self.handover.create_package("alice", description="before reimport")
        package_id = pkg_result["package_id"]

        self.importer.import_invoices(self.invoice_updated_csv, "bob")

        restore_result = self.handover.restore_package(package_id, "alice", force=True)
        self.assertTrue(restore_result["success"])

        self.assertIn("conflicts", restore_result)
        self.assertGreater(len(restore_result["conflicts"]), 0)
        self.assertIn("undo_id", restore_result)

    def test_preview_shows_reimport_conflict(self):
        self.workbench.save_last_selected_batch(self.inv_batch_id, "alice")

        pkg_result = self.handover.create_package("alice", description="before reimport")
        package_id = pkg_result["package_id"]

        self.importer.import_invoices(self.invoice_updated_csv, "bob")

        preview = self.handover.preview_package(package_id)
        self.assertTrue(preview["success"])
        self.assertTrue(preview["preview"]["has_conflicts"])

        conflict_types = [c["conflict_type"] for c in preview["preview"]["conflicts"]]
        self.assertIn(HANDOVER_CONFLICT_REIMPORT, conflict_types)


class TestHandoverPermissionFailureRollback(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.export_dir = os.path.join(self.test_dir, "exports")
        self.readonly_dir = os.path.join(self.test_dir, "readonly_exports")

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
        self.importer = CSVImporter(self.config, self.db)
        self.matcher = MatchEngine(self.config, self.db, None)
        self.workbench = BatchWorkbench(self.config, self.db)
        self.handover = BatchHandover(self.config, self.db)

        inv_result = self.importer.import_invoices(self.invoice_csv, "init_user")
        self.importer.import_payments(self.payment_csv, "init_user")
        self.inv_batch_id = inv_result["batch_id"]
        self.matcher.run_auto_matching("init_user")

    def tearDown(self):
        if os.path.exists(self.readonly_dir):
            for root, dirs, files in os.walk(self.readonly_dir):
                for f in files:
                    os.chmod(os.path.join(root, f), stat.S_IWRITE | stat.S_IREAD)
                for d in dirs:
                    os.chmod(os.path.join(root, d), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            os.chmod(self.readonly_dir, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_export_not_writable_detected_as_conflict(self):
        self.workbench.save_last_selected_batch(self.inv_batch_id, "alice")
        self.workbench.save_export_context(
            batch_id=self.inv_batch_id,
            export_type="batch_progress",
            format="json",
            operator="alice",
        )

        from invoice_reconciler.core.batch_workbench import SESSION_KEY_LAST_EXPORT
        invalid_path = "Z:\\nonexistent_root_dir\\exports"
        export_ctx = self.db.get_session_state(SESSION_KEY_LAST_EXPORT, None)
        if export_ctx:
            export_ctx["export_dir"] = invalid_path
            self.db.set_session_state(SESSION_KEY_LAST_EXPORT, export_ctx)

        pkg_result = self.handover.create_package("alice", description="invalid export dir")
        self.assertTrue(pkg_result["success"])
        package_id = pkg_result["package_id"]

        pkg = self.db.get_handover_package(package_id)
        state = self.handover._extract_package_state(pkg)
        export_ctx_from_pkg = state.get("export_context")
        self.assertIsNotNone(export_ctx_from_pkg)

        is_writable = self.handover._check_export_writable(invalid_path)
        self.assertFalse(is_writable)

        conflicts = self.handover._check_conflicts(package_id)
        conflict_types = [c["conflict_type"] for c in conflicts]
        self.assertIn(HANDOVER_CONFLICT_EXPORT_NOT_WRITABLE, conflict_types)

        restore_result = self.handover.restore_package(package_id, "alice")
        self.assertFalse(restore_result["success"])

    def test_undo_rolls_back_state(self):
        self.workbench.save_last_selected_batch(self.inv_batch_id, "alice")
        self.workbench.save_filters(operator="original_op", status=MATCH_STATUS_PENDING)

        pkg_result = self.handover.create_package("alice", description="to undo")
        package_id = pkg_result["package_id"]

        self.workbench.save_filters(operator="bob", status=MATCH_STATUS_MATCHED)

        restore_result = self.handover.restore_package(package_id, "alice", force=True)
        self.assertTrue(restore_result["success"])
        undo_id = restore_result["undo_id"]

        filters_after_restore = self.workbench.get_filters()
        self.assertEqual(filters_after_restore.get("operator"), "original_op")
        self.assertEqual(filters_after_restore.get("status"), MATCH_STATUS_PENDING)

        undo_result = self.handover.undo_restore(undo_id, "alice")
        self.assertTrue(undo_result["success"])
        self.assertEqual(undo_result["package_id"], package_id)

        filters_after_undo = self.workbench.get_filters()
        self.assertEqual(filters_after_undo.get("operator"), "bob")
        self.assertEqual(filters_after_undo.get("status"), MATCH_STATUS_MATCHED)

    def test_undo_nonexistent_returns_failure(self):
        result = self.handover.undo_restore("NONEXISTENT_UNDO_ID", "alice")
        self.assertFalse(result["success"])

    def test_check_export_writable_on_normal_dir(self):
        os.makedirs(self.export_dir, exist_ok=True)
        self.assertTrue(self.handover._check_export_writable(self.export_dir))

    def test_check_export_writable_on_nonexistent_path(self):
        self.assertFalse(self.handover._check_export_writable(""))


class TestHandoverCLICommands(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_cli.db")
        self.export_dir = os.path.join(self.test_dir, "exports")
        self.config_path = os.path.join(self.test_dir, "config.yaml")

        self.invoice_csv = os.path.join(self.test_dir, "invoices.csv")
        self.payment_csv = os.path.join(self.test_dir, "payments.csv")
        with open(self.invoice_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_CSV)
        with open(self.payment_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_CSV)

        config_data = {
            "amount_tolerance": 0.001,
            "date_window_days": 30,
            "invoice_required_columns": ["invoice_no", "invoice_date", "customer", "amount", "status"],
            "payment_required_columns": ["payment_no", "payment_date", "customer", "amount"],
            "export_format": "json",
            "db_path": self.db_path,
            "export_dir": self.export_dir,
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True)

        self.runner = CliRunner()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _run_cmd(self, *args):
        full_args = ["--config", self.config_path] + list(args)
        result = self.runner.invoke(cli, full_args)
        return result

    def _setup_data(self):
        self._run_cmd("import", "invoices", self.invoice_csv, "--operator", "test_user")
        self._run_cmd("import", "payments", self.payment_csv, "--operator", "test_user")
        self._run_cmd("match", "--operator", "test_user")

    def test_handover_create(self):
        self._setup_data()
        r = self._run_cmd("handover", "create", "--operator", "test_user", "--description", "CLI test")
        self.assertEqual(r.exit_code, 0, f"handover create failed: {r.output}\n{r.exception}")
        self.assertIn("[OK]", r.output)

        db = Database(self.db_path)
        config = Config.load(self.config_path)
        handover = BatchHandover(config, db)
        packages = handover.list_packages()
        self.assertGreater(len(packages), 0)

    def test_handover_list(self):
        self._setup_data()
        self._run_cmd("handover", "create", "--operator", "test_user", "--description", "list test")

        r = self._run_cmd("handover", "list")
        self.assertEqual(r.exit_code, 0, f"handover list failed: {r.output}\n{r.exception}")
        self.assertNotIn("暂无交接包", r.output)

    def test_handover_preview(self):
        self._setup_data()
        create_r = self._run_cmd("handover", "create", "--operator", "test_user", "--description", "preview test")
        self.assertEqual(create_r.exit_code, 0)

        db = Database(self.db_path)
        config = Config.load(self.config_path)
        handover = BatchHandover(config, db)
        packages = handover.list_packages()
        self.assertGreater(len(packages), 0)
        package_id = packages[0]["package_id"]

        r = self._run_cmd("handover", "preview", package_id)
        self.assertEqual(r.exit_code, 0, f"handover preview failed: {r.output}\n{r.exception}")
        self.assertIn("交接包预览", r.output)

    def test_handover_restore(self):
        self._setup_data()
        create_r = self._run_cmd("handover", "create", "--operator", "test_user", "--description", "restore test")
        self.assertEqual(create_r.exit_code, 0)

        db = Database(self.db_path)
        config = Config.load(self.config_path)
        handover = BatchHandover(config, db)
        packages = handover.list_packages()
        package_id = packages[0]["package_id"]

        r = self._run_cmd("handover", "restore", package_id, "--operator", "test_user")
        self.assertEqual(r.exit_code, 0, f"handover restore failed: {r.output}\n{r.exception}")
        self.assertIn("[OK]", r.output)
        self.assertIn("撤销ID", r.output)

    def test_handover_diff(self):
        self._setup_data()
        create_r = self._run_cmd("handover", "create", "--operator", "test_user", "--description", "diff test")
        self.assertEqual(create_r.exit_code, 0)

        db = Database(self.db_path)
        config = Config.load(self.config_path)
        handover = BatchHandover(config, db)
        packages = handover.list_packages()
        package_id = packages[0]["package_id"]

        r = self._run_cmd("handover", "diff", package_id)
        self.assertEqual(r.exit_code, 0, f"handover diff failed: {r.output}\n{r.exception}")

    def test_handover_undo(self):
        self._setup_data()
        create_r = self._run_cmd("handover", "create", "--operator", "test_user", "--description", "undo test")
        self.assertEqual(create_r.exit_code, 0)

        db = Database(self.db_path)
        config = Config.load(self.config_path)
        handover = BatchHandover(config, db)
        packages = handover.list_packages()
        package_id = packages[0]["package_id"]

        restore_r = self._run_cmd("handover", "restore", package_id, "--operator", "test_user")
        self.assertEqual(restore_r.exit_code, 0)

        undos = db.list_handover_undos(package_id)
        if undos:
            undo_id = undos[0]["undo_id"]
            r = self._run_cmd("handover", "undo", undo_id, "--operator", "test_user")
            self.assertEqual(r.exit_code, 0, f"handover undo failed: {r.output}\n{r.exception}")
            self.assertIn("[OK]", r.output)

    def test_handover_save_copy(self):
        self._setup_data()
        create_r = self._run_cmd("handover", "create", "--operator", "test_user", "--description", "copy test")
        self.assertEqual(create_r.exit_code, 0)

        db = Database(self.db_path)
        config = Config.load(self.config_path)
        handover = BatchHandover(config, db)
        packages = handover.list_packages()
        package_id = packages[0]["package_id"]

        r = self._run_cmd("handover", "save-copy", package_id, "--operator", "test_user")
        self.assertEqual(r.exit_code, 0, f"handover save-copy failed: {r.output}\n{r.exception}")
        self.assertIn("[OK]", r.output)

        all_packages = handover.list_packages(include_discarded=True)
        self.assertGreaterEqual(len(all_packages), 2)

    def test_handover_discard(self):
        self._setup_data()
        create_r = self._run_cmd("handover", "create", "--operator", "test_user", "--description", "discard test")
        self.assertEqual(create_r.exit_code, 0)

        db = Database(self.db_path)
        config = Config.load(self.config_path)
        handover = BatchHandover(config, db)
        packages = handover.list_packages()
        package_id = packages[0]["package_id"]

        r = self._run_cmd("handover", "discard", package_id, "--operator", "test_user")
        self.assertEqual(r.exit_code, 0, f"handover discard failed: {r.output}\n{r.exception}")
        self.assertIn("[OK]", r.output)

        active_packages = handover.list_packages(include_discarded=False)
        active_ids = [p["package_id"] for p in active_packages]
        self.assertNotIn(package_id, active_ids)

    def test_handover_cleanup(self):
        self._setup_data()
        r = self._run_cmd("handover", "cleanup", "--operator", "test_user")
        self.assertEqual(r.exit_code, 0, f"handover cleanup failed: {r.output}\n{r.exception}")

    def test_handover_timeline(self):
        self._setup_data()
        create_r = self._run_cmd("handover", "create", "--operator", "test_user", "--description", "timeline test")
        self.assertEqual(create_r.exit_code, 0)

        r = self._run_cmd("handover", "timeline")
        self.assertEqual(r.exit_code, 0, f"handover timeline failed: {r.output}\n{r.exception}")
        self.assertIn("时间线", r.output)


if __name__ == "__main__":
    unittest.main()
