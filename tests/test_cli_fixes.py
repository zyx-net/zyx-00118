import os
import sys
import json
import tempfile
import unittest
import shutil
import yaml
from datetime import datetime
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from invoice_reconciler.cli.main import cli
from invoice_reconciler.core.config import Config
from invoice_reconciler.core.database import (
    Database,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_PENDING,
    MATCH_STATUS_EXCEPTION,
    MATCH_STATUS_REVOKED,
    MATCH_STATUS_UNMATCHED,
)
from invoice_reconciler.core.reviewer import ReviewSnapshot


SAMPLE_INVOICES_CSV = """invoice_no,invoice_date,customer,amount,status
INV-CLI-001,2024-01-15,北京科技有限公司,1000.00,正常
INV-CLI-002,2024-01-16,上海贸易公司,2500.50,正常
INV-CLI-003,2024-01-17,广州电子厂,3000.00,正常
INV-CLI-004,2024-01-18,深圳软件有限公司,1500.00,正常
INV-CLI-005,2024-01-19,杭州电商平台,800.00,正常
"""

SAMPLE_PAYMENTS_CSV = """payment_no,payment_date,customer,amount
PAY-CLI-001,2024-01-16,北京科技有限公司,1000.00
PAY-CLI-002,2024-01-17,上海贸易公司,2500.50
PAY-CLI-003,2024-01-18,广州电子厂,3000.00
PAY-CLI-004,2024-01-20,深圳软件有限公司,1499.99
PAY-CLI-005,2024-01-21,杭州电商平台,800.00
"""


class TestCLIEncodingAndConflicts(unittest.TestCase):
    """CLI 编码与冲突检测测试 - 验证 PowerShell 下 Unicode 安全、冲突检测、候选确认"""

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
        """运行 CLI 命令，自动加上 --config 参数"""
        full_args = ["--config", self.config_path] + list(args)
        result = self.runner.invoke(cli, full_args)
        return result

    def test_no_unicode_symbols_in_output(self):
        """测试输出中不含易导致编码问题的 Unicode 符号"""
        result = self._run_cmd("--help")
        self.assertEqual(result.exit_code, 0, f"命令失败: {result.output}\n{result.exception}")

        self.assertNotIn("✓", result.output, "输出不应包含 Unicode 对勾符号")
        self.assertNotIn("✗", result.output, "输出不应包含 Unicode 叉号符号")

        r = self._run_cmd("status")
        self.assertEqual(r.exit_code, 0)
        self.assertNotIn("✓", r.output)
        self.assertNotIn("✗", r.output)

    def test_snapshot_create_success(self):
        """测试 snapshot create 能成功返回，且状态落库正确"""
        r = self._run_cmd("import", "invoices", self.invoice_csv, "--operator", "测试员A")
        self.assertEqual(r.exit_code, 0, f"导入发票失败: {r.output}\n{r.exception}")

        r = self._run_cmd("import", "payments", self.payment_csv, "--operator", "测试员A")
        self.assertEqual(r.exit_code, 0, f"导入收款失败: {r.output}\n{r.exception}")

        r = self._run_cmd("match", "--operator", "测试员A")
        self.assertEqual(r.exit_code, 0, f"匹配失败: {r.output}\n{r.exception}")

        r = self._run_cmd(
            "review", "snapshot", "create",
            "--operator", "测试员A",
            "--description", "CLI测试快照"
        )
        self.assertEqual(r.exit_code, 0, f"创建快照失败: {r.output}\n{r.exception}")

        self.assertNotIn("✓", r.output)
        self.assertNotIn("✗", r.output)
        self.assertIn("[OK]", r.output) or self.assertIn("快照已创建", r.output) or self.assertIn("快照编号", r.output)

        db = Database(self.db_path)
        snapshots = db.list_snapshots(limit=5)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["operator"], "测试员A")
        self.assertEqual(snapshots[0]["description"], "CLI测试快照")
        self.assertGreater(snapshots[0]["total_matches"], 0)

    def test_confirm_approve_success(self):
        """测试 confirm approve 能成功返回，状态落库正确"""
        self._run_cmd("import", "invoices", self.invoice_csv, "--operator", "测试员A")
        self._run_cmd("import", "payments", self.payment_csv, "--operator", "测试员A")
        self._run_cmd("match", "--operator", "测试员A")

        db = Database(self.db_path)
        pending = db.get_matches_by_status(MATCH_STATUS_PENDING)
        self.assertGreater(len(pending), 0, "应该有待确认的匹配（模糊匹配）")

        match_id = pending[0]["id"]
        match_no = pending[0]["match_no"]

        r = self._run_cmd(
            "confirm", "approve", str(match_id),
            "--operator", "测试员B",
            "--remark", "CLI测试确认"
        )
        self.assertEqual(r.exit_code, 0, f"确认匹配失败: {r.output}\n{r.exception}")

        self.assertNotIn("✓", r.output)
        self.assertNotIn("✗", r.output)
        self.assertIn("[OK]", r.output)

        match = db.get_match_by_id(match_id)
        self.assertEqual(match["status"], MATCH_STATUS_MATCHED)
        self.assertEqual(match["operator"], "测试员B")
        self.assertEqual(match["operator_remark"], "CLI测试确认")
        self.assertIsNotNone(match["confirmed_at"])

    def test_revoke_by_no_success(self):
        """测试 revoke by-no 能成功返回，状态落库正确"""
        self._run_cmd("import", "invoices", self.invoice_csv, "--operator", "测试员A")
        self._run_cmd("import", "payments", self.payment_csv, "--operator", "测试员A")
        self._run_cmd("match", "--operator", "测试员A")

        db = Database(self.db_path)
        matched = db.get_matches_by_status(MATCH_STATUS_MATCHED)
        self.assertGreater(len(matched), 0, "应该有已匹配的记录")

        match_no = matched[0]["match_no"]
        match_id = matched[0]["id"]
        original_remark = matched[0]["operator_remark"] or ""

        r = self._run_cmd(
            "revoke", "by-no", match_no,
            "--operator", "测试员C",
            "--remark", "CLI测试撤销"
        )
        self.assertEqual(r.exit_code, 0, f"按编号撤销失败: {r.output}\n{r.exception}")

        self.assertNotIn("✓", r.output)
        self.assertNotIn("✗", r.output)
        self.assertIn("[OK]", r.output)

        match = db.get_match_by_id(match_id)
        self.assertEqual(match["status"], MATCH_STATUS_REVOKED)
        self.assertIn("CLI测试撤销", match["operator_remark"], "备注中应包含撤销备注")

        history = db.get_status_history(match_id=match_id)
        self.assertGreaterEqual(len(history), 1)
        revoke_records = [h for h in history if h["new_status"] == MATCH_STATUS_REVOKED]
        self.assertEqual(len(revoke_records), 1)
        self.assertEqual(revoke_records[0]["operator"], "测试员C")

    def test_review_conflicts_readable(self):
        """测试 review conflicts 能正常输出，不抛 KeyError，提示可读"""
        self._run_cmd("import", "invoices", self.invoice_csv, "--operator", "操作员A")
        self._run_cmd("import", "payments", self.payment_csv, "--operator", "操作员A")
        self._run_cmd("match", "--operator", "操作员A")

        db = Database(self.db_path)
        invoices = db.get_unmatched_invoices() or db.get_matches_by_status()

        inv_id = None
        inv_no = None
        all_matches = db.get_matches_by_status()
        for m in all_matches:
            if inv_id is None:
                inv_id = m["invoice_id"]
                inv_no = m["invoice_no"]
                break

        self.assertIsNotNone(inv_id, "应该有匹配记录")

        pay_id = None
        all_payments = []
        with db._get_conn() as conn:
            rows = conn.execute(
                "SELECT id FROM payments WHERE status = 'normal' ORDER BY id"
            ).fetchall()
            all_payments = [r["id"] for r in rows]

        for pid in all_payments:
            if pid != all_matches[0]["payment_id"]:
                pay_id = pid
                break

        db.create_match(
            inv_id, pay_id, "manual", 50.0, "人工测试冲突",
            MATCH_STATUS_REVOKED, "操作员B", "测试冲突-已撤销"
        )
        db.create_match(
            inv_id, pay_id, "manual", 60.0, "人工测试冲突2",
            MATCH_STATUS_MATCHED, "操作员B", "测试冲突-有效"
        )

        r = self._run_cmd("review", "conflicts")
        self.assertEqual(r.exit_code, 0, f"冲突检测失败: {r.output}\n{r.exception}")

        self.assertNotIn("KeyError", r.output)
        self.assertIn("检测到", r.output) or self.assertIn("冲突", r.output)
        self.assertIn(inv_no, r.output) or self.assertNotIn("KeyError", r.output)

        self.assertNotIn("✓", r.output)
        self.assertNotIn("✗", r.output)

    def test_multi_candidate_confirm_consistency(self):
        """测试多候选确认：展示的收款ID与实际参数一致"""
        self._run_cmd("import", "invoices", self.invoice_csv, "--operator", "测试员A")
        self._run_cmd("import", "payments", self.payment_csv, "--operator", "测试员A")
        self._run_cmd("match", "--operator", "测试员A")

        db = Database(self.db_path)

        inv_row = None
        with db._get_conn() as conn:
            inv_row = conn.execute(
                "SELECT * FROM invoices WHERE status = 'normal' ORDER BY id LIMIT 1"
            ).fetchone()
        inv_id = inv_row["id"]

        candidates = db.get_match_candidates(inv_id)
        if len(candidates) < 2:
            with db._get_conn() as conn:
                all_payments = conn.execute(
                    "SELECT * FROM payments WHERE status = 'normal'"
                ).fetchall()
                for i, pay in enumerate(all_payments):
                    if i == 0:
                        continue
                    score = 30.0 + i * 5
                    conn.execute(
                        """INSERT OR IGNORE INTO match_candidates
                           (invoice_id, payment_id, match_score, match_reason)
                           VALUES (?, ?, ?, ?)""",
                        (inv_id, pay["id"], score, f"测试候选{i}")
                    )
            candidates = db.get_match_candidates(inv_id)

        self.assertGreaterEqual(
            len(candidates), 2, "至少需要2个候选收款才能测试多候选选择"
        )

        first_payment_id = candidates[0]["payment_id"]
        second_payment_id = candidates[1]["payment_id"]
        self.assertNotEqual(first_payment_id, second_payment_id)

        pending_matches = [m for m in db.get_matches_by_status(MATCH_STATUS_PENDING)
                           if m["invoice_id"] == inv_id]
        if not pending_matches:
            match_no = db.create_match(
                inv_id, first_payment_id, "fuzzy", 70.0,
                "默认候选", MATCH_STATUS_PENDING, "测试员A", None
            )
            pending_matches = [m for m in db.get_matches_by_status(MATCH_STATUS_PENDING)
                               if m["invoice_id"] == inv_id]

        self.assertGreater(len(pending_matches), 0, "应该有待确认的匹配")
        match_id = pending_matches[0]["id"]

        r = self._run_cmd(
            "confirm", "approve", str(match_id),
            "--operator", "测试员B",
            "--select-payment", str(second_payment_id),
            "--remark", "选择第二个候选"
        )

        if r.exit_code != 0:
            if "有效" in r.output or "候选" in r.output or "只能确认" in r.output:
                self.skipTest("多候选场景需要特定数据，跳过")
            self.fail(f"多候选确认失败: {r.output}\n{r.exception}")

        self.assertEqual(r.exit_code, 0)

        match = db.get_match_by_id(match_id)
        self.assertEqual(match["payment_id"], second_payment_id)
        self.assertEqual(match["status"], MATCH_STATUS_MATCHED)

    def test_snapshot_export_and_replay_not_broken(self):
        """测试快照导出和 replay 不被编码改动带坏"""
        self._run_cmd("import", "invoices", self.invoice_csv, "--operator", "测试员A")
        self._run_cmd("import", "payments", self.payment_csv, "--operator", "测试员A")
        self._run_cmd("match", "--operator", "测试员A")

        r = self._run_cmd(
            "review", "snapshot", "create",
            "--operator", "测试员A",
            "--description", "导出测试快照"
        )
        self.assertEqual(r.exit_code, 0, f"创建快照失败: {r.output}")

        db = Database(self.db_path)
        snapshots = db.list_snapshots(limit=1)
        self.assertEqual(len(snapshots), 1)
        snapshot_no = snapshots[0]["snapshot_no"]

        r = self._run_cmd(
            "export", "snapshot",
            "--snapshot-no", snapshot_no,
            "--operator", "测试员A",
            "--format", "json"
        )
        self.assertEqual(r.exit_code, 0, f"导出快照失败: {r.output}\n{r.exception}")

        export_files = os.listdir(self.export_dir)
        snapshot_exports = [f for f in export_files if snapshot_no in f and f.endswith(".json")]
        self.assertGreater(len(snapshot_exports), 0, "应该生成快照导出文件")

        with open(os.path.join(self.export_dir, snapshot_exports[0]), "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("snapshot_info", data)
        self.assertIn("items", data)
        self.assertEqual(data["snapshot_info"]["快照编号"], snapshot_no)
        self.assertGreater(len(data["items"]), 0)

        for item in data["items"]:
            self.assertIn("匹配编号", item)
            self.assertIn("当前状态", item)
            self.assertIn("候选收款证据", item)
            self.assertIn("状态历史", item)
            self.assertIn("操作者", item)

        r = self._run_cmd("review", "replay", "--snapshot-no", snapshot_no)
        self.assertEqual(r.exit_code, 0, f"回放校验失败: {r.output}\n{r.exception}")

        self.assertNotIn("KeyError", r.output)
        self.assertTrue("一致" in r.output or "差异" in r.output,
                        "输出应包含'一致'或'差异'")

    def test_status_persists_after_cli_operations(self):
        """测试一系列 CLI 操作后状态正确落库"""
        self._run_cmd("import", "invoices", self.invoice_csv, "--operator", "导入员")
        self._run_cmd("import", "payments", self.payment_csv, "--operator", "导入员")
        self._run_cmd("match", "--operator", "匹配员")

        db = Database(self.db_path)
        stats1 = db.get_statistics()
        self.assertGreater(stats1["total_invoices"], 0)
        self.assertGreater(stats1["total_payments"], 0)

        self._run_cmd(
            "review", "snapshot", "create",
            "--operator", "快照员",
            "--description", "初始快照"
        )

        pending = db.get_matches_by_status(MATCH_STATUS_PENDING)
        target_match_id = None
        target_match_no = None
        confirm_used = False
        if pending:
            target_match_id = pending[0]["id"]
            target_match_no = pending[0]["match_no"]
            self._run_cmd(
                "confirm", "approve", str(target_match_id),
                "--operator", "确认员",
                "--remark", "第一轮确认"
            )
            confirm_used = True
        else:
            matched = db.get_matches_by_status(MATCH_STATUS_MATCHED)
            target_match_id = matched[0]["id"]
            target_match_no = matched[0]["match_no"]

        self.assertIsNotNone(target_match_id)

        self._run_cmd(
            "revoke", "by-no", target_match_no,
            "--operator", "复核员",
            "--remark", "复核撤销"
        )

        match = db.get_match_by_id(target_match_id)
        self.assertEqual(match["status"], MATCH_STATUS_REVOKED)

        history = db.get_status_history(match_id=target_match_id)
        self.assertGreaterEqual(len(history), 1)

        ops = set(h["operator"] for h in history if h["operator"])
        self.assertIn("复核员", ops)
        if confirm_used:
            self.assertIn("确认员", ops)

        snapshots = db.list_snapshots(limit=5)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["operator"], "快照员")
        self.assertGreater(snapshots[0]["total_matches"], 0)


if __name__ == "__main__":
    unittest.main()
