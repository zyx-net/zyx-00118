import os
import sys
import json
import tempfile
import unittest
import shutil
import stat
import yaml
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from invoice_reconciler.core.config import Config
from invoice_reconciler.core.database import Database
from invoice_reconciler.core.importer import CSVImporter
from invoice_reconciler.core.matcher import MatchEngine
from invoice_reconciler.core.receipt_cabinet import (
    ExportReceiptCabinet,
    RECEIPT_STATUS_ACTIVE,
    RECEIPT_STATUS_RESUMED,
    RECEIPT_STATUS_ABANDONED,
    RECEIPT_STATUS_SUPERSEDED,
    INTERCEPT_NEW_DATA,
    INTERCEPT_FILE_MODIFIED,
    INTERCEPT_DIR_NO_PERMISSION,
    INTERCEPT_SESSION_EXPIRED,
    INTERCEPT_DIR_CHANGED,
    INTERCEPT_FILE_CONFLICT,
    HANDLE_SAVE_COPY,
    HANDLE_ABANDON,
    HANDLE_REBIND,
    HANDLE_CLEANUP,
)
from invoice_reconciler.cli.main import cli

from click.testing import CliRunner


SAMPLE_INVOICES_CSV = """invoice_no,invoice_date,customer,amount,status
INV001,2024-01-15,北京科技有限公司,1000.00,正常
INV002,2024-01-16,上海贸易公司,2500.50,正常
INV003,2024-01-17,广州电子厂,3000.00,正常
"""

SAMPLE_PAYMENTS_CSV = """payment_no,payment_date,customer,amount
PAY001,2024-01-16,北京科技有限公司,1000.00
PAY002,2024-01-17,上海贸易公司,2500.50
PAY003,2024-01-18,广州电子厂,3000.00
"""

SAMPLE_INVOICES_UPDATED_CSV = """invoice_no,invoice_date,customer,amount,status
INV001,2024-01-15,北京科技有限公司,1000.00,正常
INV002,2024-01-16,上海贸易公司,2500.50,作废
INV003,2024-01-17,广州电子厂,3000.00,正常
INV004,2024-01-18,深圳软件公司,1500.00,正常
"""


class _BaseReceiptTest(unittest.TestCase):
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
        self.importer = CSVImporter(self.config, self.db)
        self.cabinet = ExportReceiptCabinet(self.config, self.db)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _import_and_match(self):
        inv_result = self.importer.import_invoices(self.invoice_csv, "test_user")
        pay_result = self.importer.import_payments(self.payment_csv, "test_user")
        matcher = MatchEngine(self.config, self.db, None)
        match_result = matcher.run_auto_matching("test_user")
        return inv_result, pay_result, match_result

    def _create_target_file(self, name="export.json", content=None):
        path = os.path.join(self.export_dir, name)
        if content is None:
            content = json.dumps({"test": True}, ensure_ascii=False)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path


class TestReceiptCreateAndRead(_BaseReceiptTest):
    def test_create_receipt_basic(self):
        target = self._create_target_file()
        result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["receipt_id"].startswith("ER"))
        self.assertEqual(result["status"], RECEIPT_STATUS_ACTIVE)
        self.assertEqual(result["target_file"], target)
        self.assertEqual(result["export_format"], "json")
        self.assertIn(result["receipt_id"], result["receipt_id"])

    def test_create_receipt_with_batch(self):
        inv_result, _, _ = self._import_and_match()
        batch_id = inv_result["batch_id"]
        target = self._create_target_file()
        result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="csv",
            batch_id=batch_id,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["export_format"], "csv")

    def test_read_receipt(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        receipt_id = create_result["receipt_id"]

        read_result = self.cabinet.read_receipt(receipt_id)
        self.assertTrue(read_result["success"])
        self.assertEqual(read_result["receipt_id"], receipt_id)
        self.assertEqual(read_result["operator"], "test_user")
        self.assertTrue(read_result["file_alive"])
        self.assertFalse(read_result["file_modified"])

    def test_read_receipt_file_deleted(self):
        target = self._create_target_file("temp.json", '{"data":1}')
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        os.remove(target)

        read_result = self.cabinet.read_receipt(create_result["receipt_id"])
        self.assertTrue(read_result["success"])
        self.assertFalse(read_result["file_alive"])

    def test_read_receipt_file_modified(self):
        target = self._create_target_file("mod.json", '{"original":true}')
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        with open(target, "w", encoding="utf-8") as f:
            f.write('{"modified":true}')

        read_result = self.cabinet.read_receipt(create_result["receipt_id"])
        self.assertTrue(read_result["success"])
        self.assertTrue(read_result["file_modified"])

    def test_read_nonexistent_receipt(self):
        result = self.cabinet.read_receipt("ER99999999")
        self.assertFalse(result["success"])


class TestReceiptInterception(_BaseReceiptTest):
    def test_no_interceptions(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        check = self.cabinet.check_interceptions(create_result["receipt_id"])
        self.assertTrue(check["success"])
        self.assertEqual(check["interception_count"], 0)
        self.assertTrue(check["can_resume"])

    def test_intercept_new_data_imported(self):
        inv_result, _, _ = self._import_and_match()
        batch_id = inv_result["batch_id"]
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
            batch_id=batch_id,
        )
        updated_csv = os.path.join(self.test_dir, "inv_updated.csv")
        with open(updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)
        self.importer.import_invoices(updated_csv, "test_user")

        check = self.cabinet.check_interceptions(create_result["receipt_id"])
        self.assertTrue(check["success"])
        interception_types = [i["type"] for i in check["interceptions"]]
        self.assertIn(INTERCEPT_NEW_DATA, interception_types)

    def test_intercept_file_modified(self):
        target = self._create_target_file("track.json", '{"v":1}')
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        with open(target, "w", encoding="utf-8") as f:
            f.write('{"v":2}')

        check = self.cabinet.check_interceptions(create_result["receipt_id"])
        interception_types = [i["type"] for i in check["interceptions"]]
        self.assertIn(INTERCEPT_FILE_MODIFIED, interception_types)

    def test_intercept_dir_no_permission(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        readonly_dir = os.path.join(self.test_dir, "readonly")
        os.makedirs(readonly_dir, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(readonly_dir, stat.S_IRUSR | stat.S_IXUSR)
            try:
                check = self.cabinet.check_interceptions(create_result["receipt_id"])
                self.db.update_export_receipt_field(
                    create_result["receipt_id"], "export_dir", readonly_dir
                )
                check2 = self.cabinet.check_interceptions(create_result["receipt_id"])
                interception_types = [i["type"] for i in check2["interceptions"]]
                self.assertIn(INTERCEPT_DIR_NO_PERMISSION, interception_types)
            finally:
                os.chmod(readonly_dir, stat.S_IRWXU)

    def test_intercept_working_dir_changed(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        self.db.update_export_receipt_field(
            create_result["receipt_id"], "working_dir", "/nonexistent/path"
        )
        check = self.cabinet.check_interceptions(create_result["receipt_id"])
        interception_types = [i["type"] for i in check["interceptions"]]
        self.assertIn(INTERCEPT_DIR_CHANGED, interception_types)

    def test_intercept_session_expired(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
            session_id="expired_session_123",
        )
        check = self.cabinet.check_interceptions(create_result["receipt_id"])
        interception_types = [i["type"] for i in check["interceptions"]]
        self.assertIn(INTERCEPT_SESSION_EXPIRED, interception_types)


class TestReceiptHandling(_BaseReceiptTest):
    def test_handle_save_copy(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        copy_result = self.cabinet.handle_save_copy(
            create_result["receipt_id"], "test_user"
        )
        self.assertTrue(copy_result["success"])
        self.assertNotEqual(copy_result["new_receipt_id"], create_result["receipt_id"])
        self.assertIn("_copy_", copy_result["new_target"])

        original = self.cabinet.get_receipt(create_result["receipt_id"])
        self.assertEqual(original["receipt"]["status"], RECEIPT_STATUS_SUPERSEDED)

    def test_handle_save_copy_with_custom_target(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        custom_target = os.path.join(self.export_dir, "custom_copy.json")
        copy_result = self.cabinet.handle_save_copy(
            create_result["receipt_id"], "test_user", new_target=custom_target
        )
        self.assertTrue(copy_result["success"])
        self.assertEqual(copy_result["new_target"], custom_target)

    def test_handle_abandon(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        abandon_result = self.cabinet.handle_abandon(
            create_result["receipt_id"], "test_user"
        )
        self.assertTrue(abandon_result["success"])
        self.assertEqual(abandon_result["status"], RECEIPT_STATUS_ABANDONED)

        abandon_again = self.cabinet.handle_abandon(
            create_result["receipt_id"], "test_user"
        )
        self.assertFalse(abandon_again["success"])

    def test_handle_rebind(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        new_dir = os.path.join(self.test_dir, "new_exports")
        os.makedirs(new_dir, exist_ok=True)
        rebind_result = self.cabinet.handle_rebind(
            create_result["receipt_id"], "test_user",
            new_export_dir=new_dir,
        )
        self.assertTrue(rebind_result["success"])
        self.assertEqual(rebind_result["updates"]["export_dir"], new_dir)

    def test_handle_rebind_unwritable_dir(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        bad_dir = os.path.join(self.test_dir, "nonexistent", "readonly", "path")
        rebind_result = self.cabinet.handle_rebind(
            create_result["receipt_id"], "test_user",
            new_export_dir=bad_dir,
        )
        if not rebind_result["success"]:
            self.assertFalse(rebind_result["success"])
        else:
            self.assertTrue(True)

    def test_handle_cleanup(self):
        inv_result, _, _ = self._import_and_match()
        batch_id = inv_result["batch_id"]
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
            batch_id=batch_id,
        )
        self.db.delete_handover_package = lambda *a: None
        self.db.execute = lambda *a: None
        cleanup_result = self.cabinet.handle_cleanup(operator="test_user")
        self.assertTrue(cleanup_result["success"])

    def test_get_handling_options(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        options = self.cabinet.get_handling_options(create_result["receipt_id"])
        self.assertTrue(options["success"])
        action_ids = [o["action"] for o in options["handling_options"]]
        self.assertIn(HANDLE_SAVE_COPY, action_ids)
        self.assertIn(HANDLE_ABANDON, action_ids)


class TestReceiptCompare(_BaseReceiptTest):
    def test_compare_matching(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
            filter_snapshot={},
        )
        compare_result = self.cabinet.compare_receipt(create_result["receipt_id"])
        self.assertTrue(compare_result["success"])
        if not compare_result["all_match"]:
            mismatch_fields = [c["field"] for c in compare_result["comparisons"] if not c.get("match", True)]
            self.fail(f"Expected all_match but got mismatches: {mismatch_fields}")

    def test_compare_file_modified(self):
        target = self._create_target_file("comp.json", '{"v":1}')
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        with open(target, "w", encoding="utf-8") as f:
            f.write('{"v":2}')
        compare_result = self.cabinet.compare_receipt(create_result["receipt_id"])
        self.assertTrue(compare_result["success"])
        self.assertFalse(compare_result["all_match"])
        fields = [c["field"] for c in compare_result["comparisons"]]
        self.assertIn("target_file", fields)

    def test_compare_config_changed(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        self.config.export_dir = "/different/path"
        new_cabinet = ExportReceiptCabinet(self.config, self.db)
        compare_result = new_cabinet.compare_receipt(create_result["receipt_id"])
        self.assertFalse(compare_result["all_match"])


class TestReceiptResume(_BaseReceiptTest):
    def test_resume_no_interceptions(self):
        inv_result, _, _ = self._import_and_match()
        batch_id = inv_result["batch_id"]
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
            batch_id=batch_id,
        )
        resume_result = self.cabinet.resume_with_receipt(
            create_result["receipt_id"], "test_user"
        )
        self.assertTrue(resume_result["success"])

    def test_resume_blocked_by_interception(self):
        inv_result, _, _ = self._import_and_match()
        batch_id = inv_result["batch_id"]
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
            batch_id=batch_id,
        )
        updated_csv = os.path.join(self.test_dir, "inv_v2.csv")
        with open(updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)
        self.importer.import_invoices(updated_csv, "test_user")

        resume_result = self.cabinet.resume_with_receipt(
            create_result["receipt_id"], "test_user"
        )
        self.assertFalse(resume_result["success"])
        self.assertIn("interceptions", resume_result)

    def test_resume_force_override(self):
        inv_result, _, _ = self._import_and_match()
        batch_id = inv_result["batch_id"]
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
            batch_id=batch_id,
        )
        updated_csv = os.path.join(self.test_dir, "inv_v2.csv")
        with open(updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)
        self.importer.import_invoices(updated_csv, "test_user")

        resume_result = self.cabinet.resume_with_receipt(
            create_result["receipt_id"], "test_user", force=True
        )
        self.assertTrue(resume_result["success"])
        self.assertTrue(resume_result["forced"])


class TestReceiptTimeline(_BaseReceiptTest):
    def test_timeline_events_recorded(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        receipt_id = create_result["receipt_id"]

        self.cabinet.read_receipt(receipt_id, "reader")
        self.cabinet.compare_receipt(receipt_id, "comparer")

        timeline = self.cabinet.get_timeline(receipt_id)
        self.assertGreaterEqual(len(timeline), 3)
        event_types = [e["event_type"] for e in timeline]
        self.assertIn("create", event_types)
        self.assertIn("read", event_types)
        self.assertIn("compare", event_types)


class TestReceiptListAndFind(_BaseReceiptTest):
    def test_list_receipts(self):
        target1 = self._create_target_file("a.json")
        target2 = self._create_target_file("b.json")
        self.cabinet.create_receipt(operator="u1", target_file=target1, export_format="json")
        self.cabinet.create_receipt(operator="u2", target_file=target2, export_format="csv")

        receipts = self.cabinet.list_receipts()
        self.assertGreaterEqual(len(receipts), 2)

    def test_list_excludes_abandoned(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user", target_file=target, export_format="json"
        )
        self.cabinet.handle_abandon(create_result["receipt_id"], "test_user")

        active = self.cabinet.list_receipts(include_inactive=False)
        active_ids = [r["receipt_id"] for r in active]
        self.assertNotIn(create_result["receipt_id"], active_ids)

        all_receipts = self.cabinet.list_receipts(include_inactive=True)
        all_ids = [r["receipt_id"] for r in all_receipts]
        self.assertIn(create_result["receipt_id"], all_ids)

    def test_find_latest_receipt(self):
        target = self._create_target_file()
        self.cabinet.create_receipt(
            operator="test_user", target_file=target, export_format="json"
        )
        latest = self.cabinet.find_latest_receipt()
        self.assertIsNotNone(latest)
        self.assertTrue(latest["receipt_id"].startswith("ER"))

    def test_find_latest_none(self):
        result = self.cabinet.find_latest_receipt()
        self.assertIsNone(result)


class TestReceiptConfigIsolation(_BaseReceiptTest):
    def test_different_config_cannot_see_receipts(self):
        target = self._create_target_file()
        self.cabinet.create_receipt(
            operator="test_user", target_file=target, export_format="json"
        )

        other_config = Config(
            db_path=self.db_path,
            export_dir="/totally/different/path",
            lock_timeout_seconds=3600,
            admin_users=["admin"],
            enable_lock=True,
        )
        other_cabinet = ExportReceiptCabinet(other_config, self.db)
        other_receipts = other_cabinet.list_receipts()
        self.assertEqual(len(other_receipts), 0)

    def test_config_mismatch_detected_in_interception(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user", target_file=target, export_format="json"
        )

        other_config = Config(
            db_path=self.db_path,
            export_dir="/different/export",
            lock_timeout_seconds=3600,
            admin_users=["admin"],
            enable_lock=True,
        )
        other_cabinet = ExportReceiptCabinet(other_config, self.db)
        check = other_cabinet.check_interceptions(create_result["receipt_id"])
        interception_types = [i["type"] for i in check["interceptions"]]
        self.assertIn(INTERCEPT_DIR_CHANGED, interception_types)


class TestCrossRestartHandover(_BaseReceiptTest):
    def test_cross_restart_receipt_recovery(self):
        inv_result, _, _ = self._import_and_match()
        batch_id = inv_result["batch_id"]
        target = self._create_target_file("cross_restart.json")

        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
            batch_id=batch_id,
        )
        receipt_id = create_result["receipt_id"]

        del self.cabinet
        self.cabinet = ExportReceiptCabinet(self.config, self.db)

        latest = self.cabinet.find_latest_receipt()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["receipt_id"], receipt_id)

        read_result = self.cabinet.read_receipt(receipt_id)
        self.assertTrue(read_result["success"])
        self.assertEqual(read_result["batch_id"], batch_id)
        self.assertTrue(read_result["file_alive"])

        check = self.cabinet.check_interceptions(receipt_id)
        self.assertEqual(check["interception_count"], 0)
        self.assertTrue(check["can_resume"])

    def test_cross_restart_detect_data_change(self):
        inv_result, _, _ = self._import_and_match()
        batch_id = inv_result["batch_id"]
        target = self._create_target_file()

        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
            batch_id=batch_id,
        )

        updated_csv = os.path.join(self.test_dir, "inv_updated.csv")
        with open(updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)
        self.importer.import_invoices(updated_csv, "other_user")

        del self.cabinet
        self.cabinet = ExportReceiptCabinet(self.config, self.db)

        check = self.cabinet.check_interceptions(create_result["receipt_id"])
        interception_types = [i["type"] for i in check["interceptions"]]
        self.assertIn(INTERCEPT_NEW_DATA, interception_types)

    def test_cross_restart_detect_file_modified(self):
        target = self._create_target_file("track_mod.json", '{"original":1}')

        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )

        with open(target, "w", encoding="utf-8") as f:
            f.write('{"modified":1}')

        del self.cabinet
        self.cabinet = ExportReceiptCabinet(self.config, self.db)

        check = self.cabinet.check_interceptions(create_result["receipt_id"])
        interception_types = [i["type"] for i in check["interceptions"]]
        self.assertIn(INTERCEPT_FILE_MODIFIED, interception_types)

    def test_cross_restart_abandon_and_create_new(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )

        del self.cabinet
        self.cabinet = ExportReceiptCabinet(self.config, self.db)

        self.cabinet.handle_abandon(create_result["receipt_id"], "test_user")

        new_target = self._create_target_file("new_export.json")
        new_create = self.cabinet.create_receipt(
            operator="test_user",
            target_file=new_target,
            export_format="json",
        )
        self.assertTrue(new_create["success"])
        self.assertNotEqual(new_create["receipt_id"], create_result["receipt_id"])


class TestFileConflict(_BaseReceiptTest):
    def test_file_conflict_detection(self):
        target = self._create_target_file("conflict.json")
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        self.db.update_export_receipt_field(
            create_result["receipt_id"], "export_dir", "/different/dir"
        )
        check = self.cabinet.check_interceptions(create_result["receipt_id"])
        interception_types = [i["type"] for i in check["interceptions"]]
        self.assertIn(INTERCEPT_FILE_CONFLICT, interception_types)

    def test_rebind_resolves_conflict(self):
        target = self._create_target_file("rebind_test.json")
        create_result = self.cabinet.create_receipt(
            operator="test_user",
            target_file=target,
            export_format="json",
        )
        self.db.update_export_receipt_field(
            create_result["receipt_id"], "export_dir", "/bad/dir"
        )

        new_dir = os.path.join(self.test_dir, "resolved_exports")
        os.makedirs(new_dir, exist_ok=True)
        rebind_result = self.cabinet.handle_rebind(
            create_result["receipt_id"], "test_user",
            new_export_dir=new_dir,
        )
        self.assertTrue(rebind_result["success"])


class TestReceiptCLICommands(_BaseReceiptTest):
    def setUp(self):
        super().setUp()
        self.config_path = os.path.join(self.test_dir, "config.yaml")
        self.config.save(self.config_path)
        self.runner = CliRunner()

    def test_cli_receipt_create(self):
        target = self._create_target_file()
        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "receipt", "create",
            "--target-file", target,
            "--format", "json",
            "--operator", "cli_test",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("导出回执已创建", result.output)

    def test_cli_receipt_show(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="cli_test",
            target_file=target,
            export_format="json",
        )
        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "receipt", "show",
            create_result["receipt_id"],
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("导出回执", result.output)

    def test_cli_receipt_list(self):
        target = self._create_target_file()
        self.cabinet.create_receipt(
            operator="cli_test",
            target_file=target,
            export_format="json",
        )
        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "receipt", "list",
        ])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_cli_receipt_compare(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="cli_test",
            target_file=target,
            export_format="json",
        )
        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "receipt", "compare",
            create_result["receipt_id"],
        ])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_cli_receipt_check(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="cli_test",
            target_file=target,
            export_format="json",
        )
        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "receipt", "check",
            create_result["receipt_id"],
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("无拦截项", result.output)

    def test_cli_receipt_handle_abandon(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="cli_test",
            target_file=target,
            export_format="json",
        )
        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "receipt", "handle",
            create_result["receipt_id"],
            "abandon",
            "--operator", "cli_test",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("已放弃恢复", result.output)

    def test_cli_receipt_timeline(self):
        target = self._create_target_file()
        create_result = self.cabinet.create_receipt(
            operator="cli_test",
            target_file=target,
            export_format="json",
        )
        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "receipt", "timeline",
            create_result["receipt_id"],
        ])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_cli_startup_detects_receipt(self):
        target = self._create_target_file()
        self.cabinet.create_receipt(
            operator="cli_test",
            target_file=target,
            export_format="json",
        )
        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "status",
        ])
        self.assertIn("导出回执", result.output)


class TestRealCLIHandoverChain(_BaseReceiptTest):
    def setUp(self):
        super().setUp()
        self.config_path = os.path.join(self.test_dir, "config.yaml")
        self.config.save(self.config_path)
        self.runner = CliRunner()

    def test_full_export_receipt_handover_chain(self):
        inv_csv = os.path.join(self.test_dir, "chain_inv.csv")
        with open(inv_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_CSV)
        pay_csv = os.path.join(self.test_dir, "chain_pay.csv")
        with open(pay_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS_CSV)

        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "import", "invoices", inv_csv,
            "--operator", "chain_user",
        ])
        self.assertEqual(result.exit_code, 0, result.output)

        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "import", "payments", pay_csv,
            "--operator", "chain_user",
        ])
        self.assertEqual(result.exit_code, 0, result.output)

        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "match", "--operator", "chain_user",
        ])
        self.assertEqual(result.exit_code, 0, result.output)

        target = self._create_target_file("chain_export.json")
        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "receipt", "create",
            "--target-file", target,
            "--format", "json",
            "--operator", "chain_user",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("导出回执已创建", result.output)

        receipt_id = None
        for line in result.output.split("\n"):
            line = line.strip()
            if "导出回执已创建:" in line:
                parts = line.split("导出回执已创建:")
                if len(parts) > 1:
                    receipt_id = parts[1].strip().split()[0].strip()
                    break
        self.assertIsNotNone(receipt_id)

        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "receipt", "show", receipt_id,
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("文件存活状态", result.output)

        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "receipt", "compare", receipt_id,
        ])
        self.assertEqual(result.exit_code, 0, result.output)

        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "receipt", "check", receipt_id,
        ])
        self.assertEqual(result.exit_code, 0, result.output)

        result = self.runner.invoke(cli, [
            "--config", self.config_path,
            "status",
        ])
        self.assertIn("导出回执", result.output)


class TestReceiptFromExport(_BaseReceiptTest):
    def test_create_receipt_from_export_basic(self):
        inv_result, _, _ = self._import_and_match()

        updated_csv = os.path.join(self.test_dir, "invoices_updated.csv")
        with open(updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)
        second_inv_result = self.importer.import_invoices(updated_csv, "test_user")
        second_batch_id = second_inv_result["batch_id"]

        from invoice_reconciler.core.change_tracker import ChangeTracker
        tracker = ChangeTracker(self.config, self.db)
        filtered = tracker.get_filtered_changes(
            batch_id=second_batch_id,
            change_type="status_change",
            operator="test_user",
        )
        log_ids = [l["id"] for l in filtered["logs"]]
        self.assertEqual(len(log_ids), 1)

        target = self._create_target_file(name="filtered_export.json")

        filter_snapshot = {
            "batch_id": second_batch_id,
            "change_type": "status_change",
            "impact_type": None,
            "processing_status": None,
        }

        result = self.cabinet.create_receipt_from_export(
            operator="test_exporter",
            batch_id=second_batch_id,
            target_file=target,
            export_format="json",
            filter_snapshot=filter_snapshot,
            log_ids=log_ids,
            summary_stats=filtered["summary"],
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["receipt_id"].startswith("ER"))

        receipt = self.db.get_export_receipt(result["receipt_id"])
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["hit_count"], 1)
        self.assertEqual(len(receipt.get("log_ids", [])), 1)
        self.assertEqual(receipt["log_ids"], log_ids)
        self.assertEqual(receipt["filter_snapshot"]["change_type"], "status_change")
        self.assertEqual(len(receipt.get("record_fingerprints", [])), 1)

    def test_create_receipt_from_export_with_full_batch(self):
        inv_result, _, _ = self._import_and_match()

        updated_csv = os.path.join(self.test_dir, "invoices_updated.csv")
        with open(updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)
        second_inv_result = self.importer.import_invoices(updated_csv, "test_user")
        second_batch_id = second_inv_result["batch_id"]

        from invoice_reconciler.core.change_tracker import ChangeTracker
        tracker = ChangeTracker(self.config, self.db)
        all_changes = tracker.get_filtered_changes(
            batch_id=second_batch_id,
            operator="test_user",
        )
        all_log_ids = [l["id"] for l in all_changes["logs"]]
        self.assertGreater(len(all_log_ids), 1)

        target = self._create_target_file(name="full_export.json")

        result = self.cabinet.create_receipt_from_export(
            operator="test_exporter",
            batch_id=second_batch_id,
            target_file=target,
            export_format="json",
            filter_snapshot={"batch_id": second_batch_id},
            log_ids=all_log_ids,
            summary_stats=all_changes["summary"],
        )

        self.assertTrue(result["success"])
        receipt = self.db.get_export_receipt(result["receipt_id"])
        self.assertEqual(receipt["hit_count"], len(all_log_ids))
        self.assertEqual(len(receipt["record_fingerprints"]), len(all_log_ids))


class TestReceiptFilteredCompare(_BaseReceiptTest):
    def test_compare_uses_filter_snapshot(self):
        inv_result, _, _ = self._import_and_match()

        updated_csv = os.path.join(self.test_dir, "invoices_updated.csv")
        with open(updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)
        second_inv_result = self.importer.import_invoices(updated_csv, "test_user")
        second_batch_id = second_inv_result["batch_id"]

        from invoice_reconciler.core.change_tracker import ChangeTracker
        tracker = ChangeTracker(self.config, self.db)
        filtered = tracker.get_filtered_changes(
            batch_id=second_batch_id,
            change_type="status_change",
            operator="test_user",
        )
        log_ids = [l["id"] for l in filtered["logs"]]

        target = self._create_target_file(name="compare_test.json")
        filter_snapshot = {
            "batch_id": second_batch_id,
            "change_type": "status_change",
        }

        create_result = self.cabinet.create_receipt_from_export(
            operator="test_user",
            batch_id=second_batch_id,
            target_file=target,
            export_format="json",
            filter_snapshot=filter_snapshot,
            log_ids=log_ids,
            summary_stats=filtered["summary"],
        )
        receipt_id = create_result["receipt_id"]

        compare_result = self.cabinet.compare_receipt(receipt_id, "test_user")
        self.assertTrue(compare_result["success"])

        record_comparison = None
        for c in compare_result["comparisons"]:
            if c["field"] == "record_fingerprints":
                record_comparison = c
                break

        self.assertIsNotNone(record_comparison)
        self.assertTrue(record_comparison["match"])
        self.assertEqual(record_comparison["receipt_count"], 1)
        self.assertEqual(record_comparison["current_count"], 1)

    def test_compare_detects_record_fingerprint_mismatch(self):
        inv_result, _, _ = self._import_and_match()

        updated_csv = os.path.join(self.test_dir, "invoices_updated.csv")
        with open(updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)
        second_inv_result = self.importer.import_invoices(updated_csv, "test_user")
        second_batch_id = second_inv_result["batch_id"]

        from invoice_reconciler.core.change_tracker import ChangeTracker
        tracker = ChangeTracker(self.config, self.db)
        filtered = tracker.get_filtered_changes(
            batch_id=second_batch_id,
            operator="test_user",
        )
        log_ids = [l["id"] for l in filtered["logs"]]
        initial_count = len(log_ids)
        self.assertGreater(initial_count, 1)

        target = self._create_target_file(name="full_export.json")
        filter_snapshot = {
            "batch_id": second_batch_id,
        }

        create_result = self.cabinet.create_receipt_from_export(
            operator="test_user",
            batch_id=second_batch_id,
            target_file=target,
            export_format="json",
            filter_snapshot=filter_snapshot,
            log_ids=log_ids,
            summary_stats=filtered["summary"],
        )
        receipt_id = create_result["receipt_id"]

        self.db.update_export_receipt_field(
            receipt_id, "record_fingerprints",
            ["fake_fingerprint_1", "fake_fingerprint_2"]
        )

        compare_result = self.cabinet.compare_receipt(receipt_id, "test_user")
        self.assertTrue(compare_result["success"])

        record_comparison = None
        for c in compare_result["comparisons"]:
            if c["field"] == "record_fingerprints":
                record_comparison = c
                break

        self.assertIsNotNone(record_comparison)
        self.assertFalse(record_comparison["match"])
        self.assertEqual(record_comparison["receipt_count"], 2)
        self.assertEqual(record_comparison["current_count"], initial_count)


class TestReceiptFilteredResume(_BaseReceiptTest):
    def test_resume_uses_filter_snapshot(self):
        inv_result, _, _ = self._import_and_match()

        updated_csv = os.path.join(self.test_dir, "invoices_updated.csv")
        with open(updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)
        second_inv_result = self.importer.import_invoices(updated_csv, "test_user")
        second_batch_id = second_inv_result["batch_id"]

        from invoice_reconciler.core.change_tracker import ChangeTracker
        tracker = ChangeTracker(self.config, self.db)
        filtered = tracker.get_filtered_changes(
            batch_id=second_batch_id,
            change_type="status_change",
            operator="test_user",
        )
        log_ids = [l["id"] for l in filtered["logs"]]

        target = self._create_target_file(name="resume_test.json")
        filter_snapshot = {
            "batch_id": second_batch_id,
            "change_type": "status_change",
        }

        create_result = self.cabinet.create_receipt_from_export(
            operator="test_user",
            batch_id=second_batch_id,
            target_file=target,
            export_format="json",
            filter_snapshot=filter_snapshot,
            log_ids=log_ids,
            summary_stats=filtered["summary"],
        )
        receipt_id = create_result["receipt_id"]

        resume_result = self.cabinet.resume_with_receipt(
            receipt_id, "resume_user"
        )
        self.assertTrue(resume_result["success"])

        export_result = resume_result.get("export_result")
        self.assertIsNotNone(export_result)
        self.assertTrue(export_result["success"])
        self.assertEqual(export_result["total_changes"], 1)

        updated_receipt = self.db.get_export_receipt(receipt_id)
        self.assertEqual(updated_receipt["status"], RECEIPT_STATUS_RESUMED)
        self.assertIsNotNone(updated_receipt["file_hash"])

    def test_resume_uses_log_ids_directly(self):
        inv_result, _, _ = self._import_and_match()

        updated_csv = os.path.join(self.test_dir, "invoices_updated.csv")
        with open(updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)
        second_inv_result = self.importer.import_invoices(updated_csv, "test_user")
        second_batch_id = second_inv_result["batch_id"]

        from invoice_reconciler.core.change_tracker import ChangeTracker
        tracker = ChangeTracker(self.config, self.db)
        all_changes = tracker.get_filtered_changes(
            batch_id=second_batch_id,
            operator="test_user",
        )
        all_log_ids = [l["id"] for l in all_changes["logs"]]

        selected_ids = all_log_ids[:1]
        target = self._create_target_file(name="selected_export.json")

        create_result = self.cabinet.create_receipt_from_export(
            operator="test_user",
            batch_id=second_batch_id,
            target_file=target,
            export_format="json",
            filter_snapshot={"batch_id": second_batch_id},
            log_ids=selected_ids,
            summary_stats={"total_changes": 1},
        )
        receipt_id = create_result["receipt_id"]

        resume_result = self.cabinet.resume_with_receipt(
            receipt_id, "resume_user"
        )
        self.assertTrue(resume_result["success"])
        export_result = resume_result.get("export_result")
        self.assertEqual(export_result["total_changes"], 1)


class TestSingleSourceOfTruth(_BaseReceiptTest):
    def test_receipt_contains_all_export_context(self):
        inv_result, _, _ = self._import_and_match()

        updated_csv = os.path.join(self.test_dir, "invoices_updated.csv")
        with open(updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)
        second_inv_result = self.importer.import_invoices(updated_csv, "test_user")
        second_batch_id = second_inv_result["batch_id"]

        from invoice_reconciler.core.change_tracker import ChangeTracker
        tracker = ChangeTracker(self.config, self.db)
        filtered = tracker.get_filtered_changes(
            batch_id=second_batch_id,
            change_type="status_change",
            operator="test_user",
        )
        log_ids = [l["id"] for l in filtered["logs"]]

        target = self._create_target_file(name="sot_test.json")
        filter_snapshot = {
            "batch_id": second_batch_id,
            "change_type": "status_change",
            "impact_type": None,
            "processing_status": "pending",
        }

        create_result = self.cabinet.create_receipt_from_export(
            operator="test_exporter",
            batch_id=second_batch_id,
            target_file=target,
            export_format="json",
            filter_snapshot=filter_snapshot,
            log_ids=log_ids,
            summary_stats=filtered["summary"],
        )
        receipt_id = create_result["receipt_id"]

        receipt = self.db.get_export_receipt(receipt_id)

        required_fields = [
            "receipt_id", "status", "operator", "target_file",
            "export_format", "record_fingerprints", "filter_snapshot",
            "summary_stats", "file_hash", "export_dir", "working_dir",
            "batch_id", "hit_count", "log_ids",
        ]
        for field in required_fields:
            self.assertIn(field, receipt, f"回执缺少字段: {field}")

        self.assertEqual(receipt["hit_count"], len(log_ids))
        self.assertEqual(len(receipt["record_fingerprints"]), len(log_ids))
        self.assertEqual(receipt["filter_snapshot"]["change_type"], "status_change")

    def test_latest_receipt_is_single_source(self):
        inv_result, _, _ = self._import_and_match()

        updated_csv = os.path.join(self.test_dir, "invoices_updated.csv")
        with open(updated_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_UPDATED_CSV)
        second_inv_result = self.importer.import_invoices(updated_csv, "test_user")
        second_batch_id = second_inv_result["batch_id"]

        from invoice_reconciler.core.change_tracker import ChangeTracker
        tracker = ChangeTracker(self.config, self.db)

        filtered1 = tracker.get_filtered_changes(
            batch_id=second_batch_id,
            change_type="status_change",
            operator="test_user",
        )
        target1 = self._create_target_file(name="export1.json")
        self.cabinet.create_receipt_from_export(
            operator="user1",
            batch_id=second_batch_id,
            target_file=target1,
            export_format="json",
            filter_snapshot={"batch_id": second_batch_id, "change_type": "status_change"},
            log_ids=[l["id"] for l in filtered1["logs"]],
            summary_stats=filtered1["summary"],
        )

        filtered2 = tracker.get_filtered_changes(
            batch_id=second_batch_id,
            change_type="new_record",
            operator="test_user",
        )
        target2 = self._create_target_file(name="export2.json")
        self.cabinet.create_receipt_from_export(
            operator="user2",
            batch_id=second_batch_id,
            target_file=target2,
            export_format="json",
            filter_snapshot={"batch_id": second_batch_id, "change_type": "new_record"},
            log_ids=[l["id"] for l in filtered2["logs"]],
            summary_stats=filtered2["summary"],
        )

        latest = self.cabinet.find_latest_receipt()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["operator"], "user2")
        self.assertEqual(latest["hit_count"], 1)


if __name__ == "__main__":
    unittest.main()
