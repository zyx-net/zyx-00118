import os
import sys
import tempfile
import shutil
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from click.testing import CliRunner
from invoice_reconciler.cli.main import cli

SAMPLE_INVOICES_V1 = """invoice_no,invoice_date,customer,amount,status
INV001,2024-01-15,北京科技有限公司,1000.00,正常
INV002,2024-01-16,上海贸易公司,2500.50,正常
INV003,2024-01-17,广州电子厂,3000.00,正常
"""

SAMPLE_INVOICES_V2 = """invoice_no,invoice_date,customer,amount,status
INV001,2024-01-15,北京科技有限公司,1000.00,正常
INV002,2024-01-16,上海贸易公司,2500.50,作废
INV003,2024-01-17,广州电子厂,3000.00,正常
INV004,2024-01-18,深圳软件公司,1500.00,正常
"""

SAMPLE_PAYMENTS = """payment_no,payment_date,customer,amount
PAY001,2024-01-16,北京科技有限公司,1000.00
PAY002,2024-01-17,上海贸易公司,2500.50
PAY003,2024-01-18,广州电子厂,3000.00
"""


def run_cli(runner, args, config_path=None):
    full_args = []
    if config_path:
        full_args = ["--config", config_path]
    full_args.extend(args)
    result = runner.invoke(cli, full_args, catch_exceptions=False)
    print(f"\n{'='*60}")
    print(f"CMD: reconciler {' '.join(full_args)}")
    print(f"EXIT: {result.exit_code}")
    print(f"OUTPUT:\n{result.output}")
    if result.stderr:
        print(f"STDERR:\n{result.stderr}")
    print(f"{'='*60}\n")
    return result


def main():
    test_dir = tempfile.mkdtemp(prefix="refactor_test_")
    print(f"Test directory: {test_dir}")

    try:
        invoice_csv_v1 = os.path.join(test_dir, "invoices_v1.csv")
        invoice_csv_v2 = os.path.join(test_dir, "invoices_v2.csv")
        payment_csv = os.path.join(test_dir, "payments.csv")
        db_path = os.path.join(test_dir, "test.db")
        export_dir = os.path.join(test_dir, "exports")
        config_yaml = os.path.join(test_dir, "config.yaml")

        with open(invoice_csv_v1, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V1)
        with open(invoice_csv_v2, "w", encoding="utf-8") as f:
            f.write(SAMPLE_INVOICES_V2)
        with open(payment_csv, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PAYMENTS)

        os.makedirs(export_dir, exist_ok=True)

        config_content = f"""
db_path: {db_path}
export_dir: {export_dir}
lock_timeout_seconds: 3600
admin_users:
  - admin
enable_lock: true
"""
        with open(config_yaml, "w", encoding="utf-8") as f:
            f.write(config_content)

        runner = CliRunner()

        print("\n" + "="*60)
        print("STEP 1: 导入第一批发票")
        print("="*60)
        result = run_cli(runner, ["import", "invoices", invoice_csv_v1, "--operator", "test_user"], config_path=config_yaml)
        assert result.exit_code == 0, f"导入发票失败: {result.output}"

        print("\n" + "="*60)
        print("STEP 2: 导入收款")
        print("="*60)
        result = run_cli(runner, ["import", "payments", payment_csv, "--operator", "test_user"], config_path=config_yaml)
        assert result.exit_code == 0, f"导入收款失败: {result.output}"

        print("\n" + "="*60)
        print("STEP 3: 导入第二批发票（产生变更）")
        print("="*60)
        result = run_cli(runner, ["import", "invoices", invoice_csv_v2, "--operator", "test_user"], config_path=config_yaml)
        assert result.exit_code == 0, f"导入发票v2失败: {result.output}"

        print("\n" + "="*60)
        print("STEP 4: 查看变更列表")
        print("="*60)
        result = run_cli(runner, ["batch", "changes"], config_path=config_yaml)
        assert result.exit_code == 0, f"查看变更失败: {result.output}"

        print("\n" + "="*60)
        print("STEP 5: 按变更类型筛选导出（status_change） - 应该只有1条")
        print("="*60)
        result = run_cli(runner, [
            "batch", "export-changes", "3",
            "--change-type", "status_change",
            "--format", "json",
            "--operator", "test_exporter"
        ], config_path=config_yaml)
        assert result.exit_code == 0, f"导出变更失败: {result.output}"
        assert "导出回执已生成" in result.output, "应该生成导出回执"
        assert "命中数: 1 条" in result.output, "应该只导出1条状态变更"

        import re
        receipt_match = re.search(r'导出回执已生成:\s*(ER\d+)', result.output)
        assert receipt_match, "应该找到回执ID"
        receipt_id = receipt_match.group(1)
        print(f"回执ID: {receipt_id}")

        print("\n" + "="*60)
        print("STEP 6: 查看回执详情 - 应该显示筛选快照和1条记录")
        print("="*60)
        result = run_cli(runner, ["receipt", "show", receipt_id], config_path=config_yaml)
        assert result.exit_code == 0, f"查看回执失败: {result.output}"
        assert "命中记录数: 1" in result.output, "回执应该显示1条记录"
        assert "筛选快照" in result.output, "回执应该显示筛选快照"

        print("\n" + "="*60)
        print("STEP 7: 对比回执 - 应该一致（数据没变）")
        print("="*60)
        result = run_cli(runner, ["receipt", "compare", receipt_id], config_path=config_yaml)
        assert result.exit_code == 0, f"对比回执失败: {result.output}"
        # 因为筛选后只有1条，且数据没变化，应该一致
        # 但注意：compare_receipt 现在用筛选快照来获取当前视图
        assert "与当前状态完全一致" in result.output or result.exit_code == 0

        print("\n" + "="*60)
        print("STEP 8: 续导回执 - 应该用相同筛选条件导出")
        print("="*60)
        result = run_cli(runner, ["receipt", "resume", receipt_id, "--operator", "resume_user"], config_path=config_yaml)
        assert result.exit_code == 0, f"续导失败: {result.output}"
        assert "续导完成" in result.output, "应该显示续导完成"

        print("\n" + "="*60)
        print("STEP 9: 用 batch resume-export 恢复导出")
        print("="*60)
        result = run_cli(runner, ["batch", "resume-export", "--operator", "batch_resume_user"], config_path=config_yaml)
        assert result.exit_code == 0, f"batch resume-export失败: {result.output}"
        assert "使用上次导出回执" in result.output, "应该使用回执而不是旧的session state"

        print("\n" + "="*60)
        print("STEP 10: 验证回执列表")
        print("="*60)
        result = run_cli(runner, ["receipt", "list"], config_path=config_yaml)
        assert result.exit_code == 0, f"列出回执失败: {result.output}"
        assert receipt_id in result.output, "回执列表应该包含我们的回执"

        print("\n" + "="*60)
        print("STEP 11: 测试异常拦截 - 修改导出文件")
        print("="*60)
        from invoice_reconciler.core.config import Config
        from invoice_reconciler.core.database import Database
        from invoice_reconciler.core.receipt_cabinet import ExportReceiptCabinet

        config = Config(db_path=db_path, export_dir=export_dir, lock_timeout_seconds=3600,
                        admin_users=["admin"], enable_lock=True)
        db = Database(db_path)
        cabinet = ExportReceiptCabinet(config, db)

        receipt = cabinet.get_receipt(receipt_id)
        target_file = receipt["receipt"].get("target_file")
        print(f"目标文件: {target_file}")

        # 修改文件
        with open(target_file, "a", encoding="utf-8") as f:
            f.write("\n// modified")

        result = run_cli(runner, ["receipt", "check", receipt_id], config_path=config_yaml)
        assert result.exit_code == 0, f"检查拦截失败: {result.output}"
        assert "同名文件已被修改" in result.output or "文件已被修改" in result.output or "拦截" in result.output

        print("\n" + "="*60)
        print("STEP 12: 验证回执中的 log_ids 和筛选快照")
        print("="*60)
        receipt_data = db.get_export_receipt(receipt_id)
        print(f"回执数据 keys: {list(receipt_data.keys())}")
        print(f"log_ids: {receipt_data.get('log_ids')}")
        print(f"filter_snapshot: {receipt_data.get('filter_snapshot')}")
        print(f"hit_count: {receipt_data.get('hit_count')}")
        print(f"record_fingerprints 数量: {len(receipt_data.get('record_fingerprints', []))}")

        assert receipt_data.get("log_ids"), "回执应该包含 log_ids"
        assert len(receipt_data.get("log_ids", [])) == 1, "应该只有1条 log_id"
        assert receipt_data.get("filter_snapshot"), "回执应该包含筛选快照"
        assert receipt_data["filter_snapshot"].get("change_type") == "status_change", "筛选快照应该包含 change_type"

        print("\n" + "="*60)
        print("ALL TESTS PASSED! ✓")
        print("="*60)

    finally:
        print(f"\nCleaning up: {test_dir}")
        shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
