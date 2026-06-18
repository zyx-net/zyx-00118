import os
import sys
import json
import tempfile
import unittest
import shutil
import yaml
import subprocess
from datetime import datetime
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from invoice_reconciler.cli.main import cli
from invoice_reconciler.core.config import Config
from invoice_reconciler.core.database import Database


SAMPLE_INVOICES_V1 = """invoice_no,invoice_date,customer,amount,status
INV-CWD-001,2024-01-15,北京科技有限公司,10000.00,normal
INV-CWD-002,2024-01-20,上海贸易公司,5500.50,normal
INV-CWD-003,2024-02-01,广州科技有限公司,8000.00,normal
INV-CWD-004,2024-02-10,深圳电子有限公司,12500.00,normal
INV-CWD-005,2024-02-15,杭州互联网公司,3200.00,normal
"""

SAMPLE_INVOICES_V2 = """invoice_no,invoice_date,customer,amount,status
INV-CWD-001,2024-01-15,北京科技有限公司,10000.00,normal
INV-CWD-002,2024-01-20,上海贸易公司,5500.50,void
INV-CWD-003,2024-02-01,广州科技有限公司,8000.00,normal
INV-CWD-004,2024-02-10,深圳电子有限公司,13000.00,normal
INV-CWD-005,2024-02-15,杭州互联网有限公司,3200.00,normal
INV-CWD-016,2024-03-01,西安新客户,9800.00,normal
"""

SAMPLE_PAYMENTS = """payment_no,payment_date,customer,amount
PAY-CWD-001,2024-01-16,北京科技有限公司,10000.00
PAY-CWD-002,2024-01-17,上海贸易公司,5500.50
PAY-CWD-003,2024-02-02,广州科技有限公司,8000.00
PAY-CWD-004,2024-02-11,深圳电子有限公司,12500.00
PAY-CWD-005,2024-02-16,杭州互联网公司,3200.00
"""


class TestCWDPathRegression(unittest.TestCase):
    """cwd 路径稳定性回归测试 - 防止同配置换目录导致数据漂移、回执空、续传断链"""

    def setUp(self):
        self.test_root = tempfile.mkdtemp(prefix="cwd_regression_")
        self.cwd_a = os.path.join(self.test_root, "workdir_a")
        self.cwd_b = os.path.join(self.test_root, "workdir_b")
        os.makedirs(self.cwd_a)
        os.makedirs(self.cwd_b)

        self.data_dir = os.path.join(self.test_root, "data")
        self.db_path = os.path.join(self.data_dir, "reconciler.db")
        self.export_dir = os.path.join(self.data_dir, "exports")
        self.config_path = os.path.join(self.data_dir, "config.yaml")

        self.invoice_v1 = os.path.join(self.data_dir, "invoices_v1.csv")
        self.invoice_v2 = os.path.join(self.data_dir, "invoices_v2.csv")
        self.payment_v1 = os.path.join(self.data_dir, "payments_v1.csv")

        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.invoice_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V1)
        with open(self.invoice_v2, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V2)
        with open(self.payment_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS)

        self.runner = CliRunner()

    def tearDown(self):
        shutil.rmtree(self.test_root, ignore_errors=True)

    def _create_config(self, absolute_paths=True):
        """创建配置文件，可选使用绝对路径或相对路径"""
        config_data = {
            "amount_tolerance": 0.01,
            "date_window_days": 30,
            "invoice_required_columns": ["invoice_no", "invoice_date", "customer", "amount", "status"],
            "payment_required_columns": ["payment_no", "payment_date", "customer", "amount"],
            "export_format": "json",
            "db_path": self.db_path if absolute_paths else "data/reconciler.db",
            "export_dir": self.export_dir if absolute_paths else "data/exports",
            "lock_timeout_seconds": 3600,
            "default_user_role": "reviewer",
            "admin_users": ["admin"],
            "enable_lock": True,
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return self.config_path

    def _run_in_cwd(self, cwd, *args):
        """在指定工作目录运行 CLI 命令"""
        full_args = ["--config", self.config_path] + list(args)
        original_cwd = os.getcwd()
        try:
            os.chdir(cwd)
            result = self.runner.invoke(cli, full_args)
            return result
        finally:
            os.chdir(original_cwd)

    def _import_and_match(self, cwd):
        """在指定目录执行完整的导入-匹配-重导-导出链路，返回回执ID"""
        r = self._run_in_cwd(cwd, "import", "invoices", self.invoice_v1, "--operator", "operator_a")
        self.assertEqual(r.exit_code, 0, f"导入发票v1失败: {r.output}\n{r.exception}")

        r = self._run_in_cwd(cwd, "import", "payments", self.payment_v1, "--operator", "operator_a")
        self.assertEqual(r.exit_code, 0, f"导入收款失败: {r.output}\n{r.exception}")

        r = self._run_in_cwd(cwd, "match", "--operator", "operator_a")
        self.assertEqual(r.exit_code, 0, f"匹配失败: {r.output}\n{r.exception}")

        r = self._run_in_cwd(cwd, "import", "invoices", self.invoice_v2, "--operator", "operator_b")
        self.assertEqual(r.exit_code, 0, f"导入发票v2失败: {r.output}\n{r.exception}")

        r = self._run_in_cwd(cwd, "batch", "list")
        self.assertEqual(r.exit_code, 0)

        batch_id = None
        for line in r.output.split("\n"):
            parts = line.strip().split("|")
            if len(parts) >= 3 and parts[0].strip().isdigit():
                try:
                    candidate = int(parts[0].strip())
                    file_name = parts[2].strip() if len(parts) > 2 else ""
                    if "invoices_v2" in file_name or candidate > 2:
                        batch_id = candidate
                        break
                except (ValueError, IndexError):
                    continue
        if batch_id is None:
            for line in r.output.split("\n"):
                parts = line.strip().split()
                if parts and parts[0].isdigit():
                    try:
                        candidate = int(parts[0])
                        if candidate > 2:
                            batch_id = candidate
                            break
                    except ValueError:
                        continue
        self.assertIsNotNone(batch_id, f"无法找到重导批次ID，输出:\n{r.output}")

        r = self._run_in_cwd(cwd, "batch", "export-changes", str(batch_id), "--operator", "operator_b", "--format", "json", "--change-type", "status_change")
        self.assertEqual(r.exit_code, 0, f"导出变更失败: {r.output}\n{r.exception}")

        receipt_id = None
        import re
        for line in r.output.split("\n"):
            if "回执" in line and "已" in line:
                m = re.search(r"ER\d+", line)
                if m:
                    receipt_id = m.group()
                    break
        if receipt_id is None:
            m = re.search(r"ER\d+", r.output)
            if m:
                receipt_id = m.group()
        self.assertIsNotNone(receipt_id, f"无法找到回执ID，输出:\n{r.output}")

        return receipt_id, batch_id

    def test_absolute_paths_same_data_across_cwd(self):
        """绝对路径配置：不同cwd必须读到同一份数据、同一份回执列表"""
        self._create_config(absolute_paths=True)

        receipt_id, batch_id = self._import_and_match(self.cwd_a)

        r_a = self._run_in_cwd(self.cwd_a, "receipt", "list")
        self.assertEqual(r_a.exit_code, 0, f"cwd_a 查回执失败: {r_a.output}")
        self.assertIn(receipt_id, r_a.output, f"cwd_a 回执列表应包含 {receipt_id}")

        count_a = 0
        for line in r_a.output.split("\n"):
            if "ER" in line and "20" in line:
                count_a += 1
        self.assertGreater(count_a, 0, "cwd_a 回执列表不应为空")

        r_b = self._run_in_cwd(self.cwd_b, "receipt", "list")
        self.assertEqual(r_b.exit_code, 0, f"cwd_b 查回执失败: {r_b.output}")
        self.assertIn(receipt_id, r_b.output, f"cwd_b 回执列表应包含 {receipt_id}，实际输出:\n{r_b.output}")

        count_b = 0
        for line in r_b.output.split("\n"):
            if "ER" in line and "20" in line:
                count_b += 1
        self.assertEqual(count_b, count_a, f"cwd_a({count_a}) 和 cwd_b({count_b}) 回执数量必须一致")

        r_a_show = self._run_in_cwd(self.cwd_a, "receipt", "show", receipt_id)
        self.assertEqual(r_a_show.exit_code, 0)
        self.assertIn("status_change", r_a_show.output)

        r_b_show = self._run_in_cwd(self.cwd_b, "receipt", "show", receipt_id)
        self.assertEqual(r_b_show.exit_code, 0)
        self.assertIn("status_change", r_b_show.output)

        r_a_status = self._run_in_cwd(self.cwd_a, "status")
        self.assertEqual(r_a_status.exit_code, 0)
        self.assertIn("已匹配", r_a_status.output)

        r_b_status = self._run_in_cwd(self.cwd_b, "status")
        self.assertEqual(r_b_status.exit_code, 0)
        self.assertIn("已匹配", r_b_status.output)

        for key_phrase in ["发票总数", "收款总数", "已匹配发票", "已匹配收款"]:
            self.assertIn(key_phrase, r_a_status.output)
            self.assertIn(key_phrase, r_b_status.output)

    def test_relative_paths_drift_detection(self):
        """相对路径配置：cwd变更时应明确失败，而非静默建空库"""
        self._create_config(absolute_paths=False)

        receipt_id, batch_id = self._import_and_match(self.cwd_a)

        r_a = self._run_in_cwd(self.cwd_a, "receipt", "list")
        self.assertEqual(r_a.exit_code, 0)
        self.assertIn(receipt_id, r_a.output)

        r_b = self._run_in_cwd(self.cwd_b, "receipt", "list")
        self.assertEqual(r_b.exit_code, 0, "命令本身应成功执行")

        found_in_b = receipt_id in r_b.output
        count_in_a = sum(1 for line in r_a.output.split("\n") if "ER" in line and "20" in line)
        count_in_b = sum(1 for line in r_b.output.split("\n") if "ER" in line and "20" in line)

        if not found_in_b or count_in_b == 0:
            self.fail(
                f"路径漂移检测失败！\n"
                f"配置文件: {self.config_path}\n"
                f"cwd_a({self.cwd_a}) 回执数={count_in_a}, 含 {receipt_id}\n"
                f"cwd_b({self.cwd_b}) 回执数={count_in_b}, 是否含 {receipt_id}={found_in_b}\n"
                f"cwd_b 输出:\n{r_b.output}\n"
                f"---\n"
                f"问题根源：config.yaml 中 db_path 是相对路径 'data/reconciler.db'，\n"
                f"在 cwd_a 解析为 {os.path.join(self.cwd_a, 'data/reconciler.db')}，\n"
                f"在 cwd_b 解析为 {os.path.join(self.cwd_b, 'data/reconciler.db')}，\n"
                f"这是两个完全不同的数据库！必须使用绝对路径或在 Config.load 中基于 config.yaml 位置解析相对路径。"
            )

    def test_cross_cwd_resume_export(self):
        """跨重启（不同cwd）续导：同一配置必须能稳定续传，不能断链"""
        self._create_config(absolute_paths=True)

        receipt_id, batch_id = self._import_and_match(self.cwd_a)

        r_a_show = self._run_in_cwd(self.cwd_a, "receipt", "show", receipt_id)
        self.assertEqual(r_a_show.exit_code, 0)
        self.assertIn(receipt_id, r_a_show.output)

        r_b_resume_blocked = self._run_in_cwd(self.cwd_b, "receipt", "resume", receipt_id, "--operator", "operator_c")
        self.assertEqual(
            r_b_resume_blocked.exit_code, 1,
            f"跨 cwd 续导应先被拦截（工作目录变更检测），退出码应为1，实际为{r_b_resume_blocked.exit_code}"
        )
        self.assertTrue(
            "工作目录" in r_b_resume_blocked.output or "cwd" in r_b_resume_blocked.output.lower() or "directory" in r_b_resume_blocked.output.lower(),
            f"拦截信息应提及工作目录变更，实际输出:\n{r_b_resume_blocked.output}"
        )
        self.assertTrue(
            "--force" in r_b_resume_blocked.output,
            f"拦截信息应提示使用 --force，实际输出:\n{r_b_resume_blocked.output}"
        )

        r_b_resume = self._run_in_cwd(self.cwd_b, "receipt", "resume", receipt_id, "--operator", "operator_c", "--force")
        self.assertEqual(
            r_b_resume.exit_code, 0,
            f"跨 cwd 续导（带 --force）失败！\n"
            f"cwd_a 导出的回执 {receipt_id} 在 cwd_b 续导时出错\n"
            f"退出码: {r_b_resume.exit_code}\n"
            f"输出: {r_b_resume.output}\n"
            f"异常: {r_b_resume.exception}"
        )
        self.assertIn("OK", r_b_resume.output)
        self.assertTrue(
            "导出" in r_b_resume.output or "续导" in r_b_resume.output or "resume" in r_b_resume.output,
            f"续导输出应包含导出相关信息，实际输出:\n{r_b_resume.output}"
        )

        r_b_show = self._run_in_cwd(self.cwd_b, "receipt", "show", receipt_id)
        self.assertEqual(r_b_show.exit_code, 0)
        self.assertIn(receipt_id, r_b_show.output)

        r_a_list = self._run_in_cwd(self.cwd_a, "receipt", "list")
        r_b_list = self._run_in_cwd(self.cwd_b, "receipt", "list")
        count_a = sum(1 for line in r_a_list.output.split("\n") if "ER" in line and "20" in line)
        count_b = sum(1 for line in r_b_list.output.split("\n") if "ER" in line and "20" in line)
        self.assertEqual(count_a, count_b, f"续导后两边回执数必须一致：a={count_a}, b={count_b}")

        r_batch_resume_blocked = self._run_in_cwd(self.cwd_b, "batch", "resume-export", "--operator", "operator_d")
        self.assertEqual(r_batch_resume_blocked.exit_code, 1, f"batch resume-export 跨 cwd 应先被拦截")
        self.assertTrue(
            "工作目录" in r_batch_resume_blocked.output or "cwd" in r_batch_resume_blocked.output.lower(),
            f"batch resume-export 也应检测工作目录变更，实际输出:\n{r_batch_resume_blocked.output}"
        )

        r_batch_resume = self._run_in_cwd(self.cwd_b, "batch", "resume-export", "--operator", "operator_d", "--force")
        self.assertEqual(r_batch_resume.exit_code, 0, f"batch resume-export 跨 cwd（带 --force）失败: {r_batch_resume.output}")
        self.assertIn("OK", r_batch_resume.output)

    def test_export_file_modification_intercept_and_force(self):
        """导出文件被改动：应拦截，--force 应能放行，且不回退到全量视角"""
        self._create_config(absolute_paths=True)

        receipt_id, batch_id = self._import_and_match(self.cwd_a)

        r_show = self._run_in_cwd(self.cwd_a, "receipt", "show", receipt_id)
        self.assertEqual(r_show.exit_code, 0)

        target_file = None
        for line in r_show.output.split("\n"):
            if "target_file" in line or "目标文件" in line:
                parts = line.split(":", 1)
                if len(parts) > 1:
                    candidate = parts[1].strip()
                    if candidate and os.path.exists(candidate):
                        target_file = candidate
                        break

        if target_file is None:
            r_list = self._run_in_cwd(self.cwd_a, "receipt", "list")
            for line in r_list.output.split("\n"):
                if ".json" in line:
                    import re
                    m = re.search(r'(\S+\.json)', line)
                    if m:
                        candidate = m.group(1)
                        if os.path.exists(candidate):
                            target_file = candidate
                            break

        self.assertIsNotNone(target_file, f"无法找到导出文件路径，show 输出:\n{r_show.output}")

        with open(target_file, "r", encoding="utf-8") as f:
            original_content = f.read()

        with open(target_file, "w", encoding="utf-8") as f:
            f.write(original_content + "\n// 人工修改标记\n")

        r_modified_show = self._run_in_cwd(self.cwd_a, "receipt", "show", receipt_id)
        self.assertEqual(r_modified_show.exit_code, 0)

        r_resume_blocked = self._run_in_cwd(self.cwd_a, "receipt", "resume", receipt_id, "--operator", "operator_e")
        self.assertEqual(r_resume_blocked.exit_code, 1, f"文件被改后应拦截，退出码应为1，实际为{r_resume_blocked.exit_code}")
        self.assertTrue(
            "拦截" in r_resume_blocked.output or "!!" in r_resume_blocked.output or "modified" in r_resume_blocked.output.lower(),
            f"文件被改后应输出拦截信息，实际输出:\n{r_resume_blocked.output}"
        )
        self.assertTrue(
            "文件" in r_resume_blocked.output or "file" in r_resume_blocked.output.lower(),
            f"拦截信息应提及文件，实际输出:\n{r_resume_blocked.output}"
        )

        r_resume_forced = self._run_in_cwd(self.cwd_a, "receipt", "resume", receipt_id, "--operator", "operator_f", "--force")
        self.assertEqual(r_resume_forced.exit_code, 0, f"--force 应该能跳过文件哈希拦截: {r_resume_forced.output}")
        self.assertIn("OK", r_resume_forced.output)

        r_show_after = self._run_in_cwd(self.cwd_a, "receipt", "show", receipt_id)
        self.assertEqual(r_show_after.exit_code, 0)

        r_compare = self._run_in_cwd(self.cwd_a, "receipt", "compare", receipt_id)
        self.assertEqual(r_compare.exit_code, 0)

        expected_hit_count = None
        for line in r_show_after.output.split("\n"):
            if "hit_count" in line or "命中记录" in line:
                parts = line.split(":", 1)
                if len(parts) > 1:
                    try:
                        expected_hit_count = int(parts[1].strip())
                    except (ValueError, TypeError):
                        pass
                    break

        self.assertIsNotNone(expected_hit_count, "应能读取到命中记录数")
        self.assertGreater(expected_hit_count, 0, "命中记录数应大于0")
        self.assertLess(expected_hit_count, 10, "只导出了 status_change 类型，命中数应该很少（1-2条），不应回退到全量")

    def test_zero_records_assertion_fails_fast(self):
        """命令执行但命中0条或结果为空时，断言必须失败，不能输出'已通过'"""
        self._create_config(absolute_paths=True)

        r = self._run_in_cwd(self.cwd_a, "import", "invoices", self.invoice_v1, "--operator", "test")
        self.assertEqual(r.exit_code, 0)

        r_list = self._run_in_cwd(self.cwd_a, "receipt", "list")
        self.assertEqual(r_list.exit_code, 0)

        receipt_count = sum(1 for line in r_list.output.split("\n") if "ER" in line and "20" in line)

        try:
            self.assertGreater(receipt_count, 0, "receipt list 命中0条，应该失败，不应输出'已通过'")
        except AssertionError as e:
            expected_error = str(e)
            self.assertIn("命中0条", expected_error or "receipt count should be > 0")
            return

        self.fail("当命中0条时，assertGreater 应该抛出 AssertionError，但没有。这说明验证逻辑太松了！")

    def test_command_error_fails_fast(self):
        """命令写错或执行失败时，必须立即失败，不能继续输出'已通过'"""
        self._create_config(absolute_paths=True)

        r_bad_cmd = self._run_in_cwd(self.cwd_a, "nonexistent", "command")

        try:
            self.assertEqual(r_bad_cmd.exit_code, 0, "故意写的错误命令，exit_code 应该非0")
        except AssertionError:
            return

        self.fail("命令执行失败后，断言应该失败，但没有。请检查是否误用了 assertNotEqual 或忽略了 exit_code！")


class TestSubprocessCWDDrift(unittest.TestCase):
    """使用 subprocess 模拟真实 CLI 调用场景，更贴近用户实际使用"""

    def setUp(self):
        self.test_root = tempfile.mkdtemp(prefix="subprocess_cwd_")
        self.cwd_a = os.path.join(self.test_root, "cwd_a")
        self.cwd_b = os.path.join(self.test_root, "cwd_b")
        os.makedirs(self.cwd_a)
        os.makedirs(self.cwd_b)

        self.data_dir = os.path.join(self.test_root, "data")
        self.db_path = os.path.join(self.data_dir, "reconciler.db")
        self.export_dir = os.path.join(self.data_dir, "exports")
        self.config_path = os.path.join(self.data_dir, "config.yaml")

        self.invoice_v1 = os.path.join(self.data_dir, "inv_v1.csv")
        self.invoice_v2 = os.path.join(self.data_dir, "inv_v2.csv")
        self.payment_v1 = os.path.join(self.data_dir, "pay_v1.csv")

        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.invoice_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V1)
        with open(self.invoice_v2, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V2)
        with open(self.payment_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS)

        self.base_env = os.environ.copy()
        self.base_env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.base_env["PYTHONIOENCODING"] = "utf-8"

    def tearDown(self):
        shutil.rmtree(self.test_root, ignore_errors=True)

    def _create_config_relative(self):
        config_data = {
            "amount_tolerance": 0.01,
            "date_window_days": 30,
            "invoice_required_columns": ["invoice_no", "invoice_date", "customer", "amount", "status"],
            "payment_required_columns": ["payment_no", "payment_date", "customer", "amount"],
            "export_format": "json",
            "db_path": "data/reconciler.db",
            "export_dir": "data/exports",
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def _run_cli(self, cwd, *args):
        cmd = [sys.executable, "-m", "invoice_reconciler.cli.main", "--config", self.config_path] + list(args)
        r = subprocess.run(cmd, env=self.base_env, cwd=cwd, capture_output=True, timeout=120)
        out = r.stdout.decode("utf-8", errors="replace")
        err = r.stderr.decode("utf-8", errors="replace")
        return r.returncode, out + err

    def test_relative_path_drift_subprocess(self):
        """subprocess 模拟真实场景：相对路径+换cwd=数据漂移（复现BUG用，这个测试应该能复现问题）"""
        self._create_config_relative()

        code_a, out_a = self._run_cli(self.cwd_a, "import", "invoices", self.invoice_v1, "--operator", "tester")
        self.assertEqual(code_a, 0, f"cwd_a 导入失败: {out_a}")

        code_a, out_a = self._run_cli(self.cwd_a, "import", "payments", self.payment_v1, "--operator", "tester")
        self.assertEqual(code_a, 0, f"cwd_a 导入收款失败: {out_a}")

        code_a, out_a = self._run_cli(self.cwd_a, "status")
        self.assertEqual(code_a, 0)
        self.assertIn("发票总数: 5", out_a)

        code_b, out_b = self._run_cli(self.cwd_b, "status")
        self.assertEqual(code_b, 0)

        if "发票总数: 0" in out_b or "发票总数: 5" not in out_b:
            db_a = os.path.join(self.cwd_a, "data", "reconciler.db")
            db_b = os.path.join(self.cwd_b, "data", "reconciler.db")
            self.fail(
                f"BUG 已复现！相对路径导致数据漂移\n"
                f"cwd_a({self.cwd_a}) status:\n{out_a}\n"
                f"cwd_b({self.cwd_b}) status:\n{out_b}\n"
                f"cwd_a 数据库: {db_a} (存在={os.path.exists(db_a)})\n"
                f"cwd_b 数据库: {db_b} (存在={os.path.exists(db_b)})\n"
                f"这就是用户说的'换个工作目录执行后，数据库按当前目录重新解析，读到新建空库'！"
            )


if __name__ == "__main__":
    unittest.main()
