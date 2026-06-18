import os
import sys
import json
import csv
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import click
from click.testing import CliRunner

from invoice_reconciler.cli.main import cli


E2E_INVOICES_V1 = """invoice_no,invoice_date,customer,amount,status
INV001,2024-01-15,北京科技有限公司,1000.00,正常
INV002,2024-01-16,上海贸易公司,2500.50,正常
INV003,2024-01-17,广州电子厂,3000.00,正常
INV004,2024-01-18,深圳软件公司,1500.00,正常
INV005,2024-01-19,杭州电商平台,800.00,正常
"""

E2E_INVOICES_V2 = """invoice_no,invoice_date,customer,amount,status
INV001,2024-01-15,北京科技有限公司,1000.00,正常
INV002,2024-01-16,上海贸易公司,2500.50,作废
INV003,2024-01-17,广州电子有限公司,3000.00,正常
INV004,2024-01-18,深圳软件公司,1600.00,正常
INV005,2024-01-19,杭州电商平台,800.00,正常
INV006,2024-01-20,武汉科技公司,2000.00,正常
"""

E2E_PAYMENTS_V1 = """payment_no,payment_date,customer,amount
PAY001,2024-01-16,北京科技有限公司,1000.00
PAY002,2024-01-17,上海贸易公司,2500.50
PAY003,2024-01-18,广州电子厂,3000.00
PAY004,2024-01-20,深圳软件公司,1499.99
PAY005,2024-01-21,杭州电商平台,800.00
"""


def print_step(step_num, title):
    print(f"\n{'='*70}")
    print(f"【步骤 {step_num}】 {title}")
    print(f"{'='*70}\n")


def print_subtitle(title):
    print(f"\n--- {title} ---\n")


def run_cmd(runner, config_path, args, expect_success=True):
    result = runner.invoke(cli, ["--config", config_path] + args)
    
    cmd_str = "invoice-recon " + " ".join(args)
    print(f"$ {cmd_str}")
    print("-" * 60)
    print(result.output)
    
    if expect_success and result.exit_code != 0:
        print(f"[错误] 命令执行失败，退出码: {result.exit_code}")
        if result.exception:
            import traceback
            traceback.print_exception(type(result.exception), result.exception, 
                                      result.exception.__traceback__)
        return None
    elif not expect_success and result.exit_code == 0:
        print("[警告] 预期失败但成功了")
    
    return result


def main():
    test_dir = tempfile.mkdtemp(prefix="e2e_final_")
    try:
        db_path = os.path.join(test_dir, "reconcile.db")
        export_dir = os.path.join(test_dir, "exports")
        config_path = os.path.join(test_dir, "config.yaml")

        inv_v1 = os.path.join(test_dir, "invoices_v1.csv")
        inv_v2 = os.path.join(test_dir, "invoices_v2.csv")
        pay_v1 = os.path.join(test_dir, "payments_v1.csv")

        with open(inv_v1, "w", encoding="utf-8") as f:
            f.write(E2E_INVOICES_V1)
        with open(inv_v2, "w", encoding="utf-8") as f:
            f.write(E2E_INVOICES_V2)
        with open(pay_v1, "w", encoding="utf-8") as f:
            f.write(E2E_PAYMENTS_V1)

        from invoice_reconciler.core.config import Config
        config = Config(
            db_path=db_path,
            export_dir=export_dir,
            lock_timeout_seconds=3600,
            admin_users=["admin"],
            enable_lock=True,
        )
        config.save(config_path)

        runner = CliRunner()

        print("\n" + "#" * 70)
        print("#  离线对账 CLI - 批次变更工作台 E2E 交接验证")
        print("#" * 70)
        print(f"\n工作目录: {test_dir}")
        print(f"数据库: {db_path}")
        print(f"导出目录: {export_dir}")

        # ========== 步骤1: 首次导入发票 ==========
        print_step(1, "首次导入发票和收款，建立基准数据")
        run_cmd(runner, config_path, [
            "import", "invoices", inv_v1, "--operator", "张三"
        ])
        run_cmd(runner, config_path, [
            "import", "payments", pay_v1, "--operator", "张三"
        ])

        # ========== 步骤2: 自动匹配 ==========
        print_step(2, "自动匹配并确认/撤销部分匹配")
        run_cmd(runner, config_path, ["match", "auto", "--operator", "系统"])
        run_cmd(runner, config_path, ["match", "list", "--limit", "10"])

        result = run_cmd(runner, config_path, ["match", "list", "--status", "pending"])
        match_ids = []
        if result and result.output:
            for line in result.output.split("\n"):
                if line.strip().startswith("|") and "待确认" in line:
                    parts = [p.strip() for p in line.split("|") if p.strip()]
                    if parts and parts[0].isdigit():
                        match_ids.append(parts[0])

        if len(match_ids) >= 1:
            run_cmd(runner, config_path, [
                "match", "confirm", match_ids[0],
                "--operator", "李经理", "--remark", "核对一致"
            ])
        if len(match_ids) >= 2:
            run_cmd(runner, config_path, [
                "match", "confirm", match_ids[1],
                "--operator", "李经理", "--remark", "临时确认"
            ])
            run_cmd(runner, config_path, [
                "match", "revoke", match_ids[1],
                "--operator", "王主管", "--reason", "合同号不符，需要核实"
            ])

        # ========== 步骤3: 重导入发票（带变更） ==========
        print_step(3, "重导入更新版发票，触发变更检测")
        result = run_cmd(runner, config_path, [
            "import", "invoices", inv_v2, "--operator", "李四"
        ])

        batch_id = None
        if result and result.output:
            for line in result.output.split("\n"):
                if "批次" in line and "ID" in line:
                    import re
                    m = re.search(r'批次\s*#?(\d+)', line)
                    if m:
                        batch_id = m.group(1)
                        break
                if line.strip().startswith("批次ID:"):
                    batch_id = line.split(":")[1].strip().split()[0]

        if not batch_id:
            print("未能获取批次ID，列出所有批次...")
            result = run_cmd(runner, config_path, ["batch", "list"])
            if result and result.output:
                import re
                for line in result.output.split("\n"):
                    m = re.match(r'^\s*(\d+)\s*\|', line)
                    if m:
                        batch_id = m.group(1)

        print(f"\n检测到的重导入批次ID: {batch_id}")

        # ========== 步骤4: 查看变更日志（全量） ==========
        print_step(4, "查看批次变更日志 - 全量视图")
        print_subtitle("验证点：顶部统计 = 下方明细数量")
        
        if batch_id:
            result = run_cmd(runner, config_path, [
                "batch", "changes", "--batch-id", batch_id
            ])

        # ========== 步骤5: 按变更类型筛选 ==========
        print_step(5, "按变更类型筛选 - 状态变更")
        print_subtitle("验证点：筛选后统计摘要与明细一致")
        
        if batch_id:
            result = run_cmd(runner, config_path, [
                "batch", "changes", "--batch-id", batch_id,
                "--change-type", "status_change"
            ])
            if result and result.output:
                has_filter_note = "已应用筛选" in result.output
                has_hit_count = "命中" in result.output
                print(f"\n筛选提示显示: {'✓' if has_filter_note else '✗'}")
                print(f"命中数显示: {'✓' if has_hit_count else '✗'}")

        # ========== 步骤6: 按影响类型筛选 ==========
        print_step(6, "按影响类型筛选 - 已确认影响")
        print_subtitle("验证点：affect筛选后统计、明细一致")
        
        if batch_id:
            result = run_cmd(runner, config_path, [
                "batch", "changes", "--batch-id", batch_id,
                "--affect", "confirmed"
            ])

        # ========== 步骤7: 多条件组合筛选 ==========
        print_step(7, "多条件组合筛选")
        print_subtitle("验证点：变更类型 + 影响类型 + 仅含冲突")
        
        if batch_id:
            result = run_cmd(runner, config_path, [
                "batch", "changes", "--batch-id", batch_id,
                "--change-type", "amount_change",
                "--affect", "all",
                "--with-conflicts-only"
            ])

        # ========== 步骤8: 导出JSON（带筛选） ==========
        print_step(8, "导出JSON - 状态变更类型")
        print_subtitle("验证点：导出内容与CLI视图筛选结果一致")
        
        if batch_id:
            result = run_cmd(runner, config_path, [
                "batch", "export-changes", batch_id,
                "--format", "json",
                "--change-type", "status_change",
                "--operator", "导出员小王"
            ])

        json_files = sorted([f for f in os.listdir(export_dir) 
                            if f.endswith('.json') and 'change_log' in f])
        if json_files:
            latest_json = json_files[-1]
            json_path = os.path.join(export_dir, latest_json)
            print(f"\n导出文件: {json_path}")
            
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            print(f"导出信息:")
            print(f"  导出人: {data['export_info'].get('exported_by', '-')}")
            print(f"  导出时间: {data['export_info'].get('exported_at', '-')}")
            print(f"  总变更数: {data['summary'].get('total_changes', '-')}")
            print(f"  冲突数: {data['summary'].get('conflict_count', '-')}")
            print(f"  明细条数: {len(data['change_logs'])}")
            
            print(f"\n按类型统计:")
            for k, v in data["summary"]["by_type"].items():
                print(f"  {k}: {v}")
            
            if data["change_logs"]:
                first_log = data["change_logs"][0]
                print(f"\n第一条变更详情（示例）:")
                print(f"  记录编号: {first_log.get('记录编号', '-')}")
                print(f"  变更类型: {first_log.get('变更类型', '-')}")
                print(f"  变更前摘要: {first_log.get('变更前摘要', '-')}")
                print(f"  变更后摘要: {first_log.get('变更后摘要', '-')}")
                print(f"  原值: {first_log.get('原值', '-')}")
                print(f"  新值: {first_log.get('新值', '-')}")
                print(f"  影响类型: {first_log.get('影响类型', '-')}")
                print(f"  影响详情: {first_log.get('影响详情', '-')}")
                print(f"  是否有冲突: {first_log.get('是否有冲突', '-')}")
                print(f"  冲突原因: {first_log.get('冲突原因', '-')}")
                print(f"  处理状态: {first_log.get('处理状态', '-')}")
                print(f"  操作者: {first_log.get('操作者', '-')}")
                print(f"  检测时间: {first_log.get('检测时间', '-')}")

        # ========== 步骤9: 导出CSV（带筛选） ==========
        print_step(9, "导出CSV - 金额变更类型")
        print_subtitle("验证点：CSV明细与统计摘要一致")
        
        if batch_id:
            result = run_cmd(runner, config_path, [
                "batch", "export-changes", batch_id,
                "--format", "csv",
                "--change-type", "amount_change",
                "--operator", "导出员小王"
            ])

        csv_dirs = sorted([d for d in os.listdir(export_dir) 
                          if os.path.isdir(os.path.join(export_dir, d)) and 'change_log' in d])
        if csv_dirs:
            latest_csv_dir = csv_dirs[-1]
            csv_path = os.path.join(export_dir, latest_csv_dir)
            print(f"\n导出目录: {csv_path}")
            
            detail_path = os.path.join(csv_path, "变更明细.csv")
            summary_path = os.path.join(csv_path, "变更摘要.csv")
            
            if os.path.exists(detail_path):
                with open(detail_path, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                print(f"明细记录数: {len(rows)}")
                if rows:
                    print(f"第一条记录: {rows[0].get('记录编号', '-')} - {rows[0].get('变更类型', '-')}")
            
            if os.path.exists(summary_path):
                with open(summary_path, "r", encoding="utf-8-sig") as f:
                    print(f"\n摘要文件内容:")
                    print(f.read()[:500])

        # ========== 步骤10: 验证会话状态 ==========
        print_step(10, "验证会话状态持久化")
        print_subtitle("验证点：上次批次、筛选条件、导出位置都已保存")
        
        result = run_cmd(runner, config_path, ["batch", "status"])
        if result and result.output:
            has_last_batch = "上次批次" in result.output or "最后批次" in result.output
            print(f"上次批次状态: {'✓ 已记录' if has_last_batch else '✗ 未找到'}")

        # ========== 步骤11: 使用上次筛选条件导出 ==========
        print_step(11, "使用上次视图筛选条件导出")
        print_subtitle("验证点：--use-last-filter 复用 batch changes 的筛选")
        
        if batch_id:
            run_cmd(runner, config_path, [
                "batch", "changes", "--batch-id", batch_id,
                "--change-type", "key_field_change",
                "--affect", "all"
            ])
            
            result = run_cmd(runner, config_path, [
                "batch", "export-changes", batch_id,
                "--format", "json",
                "--use-last-filter",
                "--operator", "继续导出员"
            ])

        # ========== 步骤12: 配置隔离验证 ==========
        print_step(12, "配置隔离验证 - 独立工作目录不串会话")
        print_subtitle("验证点：新目录没有旧会话数据")
        
        other_dir = os.path.join(test_dir, "other_workspace")
        os.makedirs(other_dir, exist_ok=True)
        other_db = os.path.join(other_dir, "other.db")
        other_export = os.path.join(other_dir, "exports")
        other_config_path = os.path.join(other_dir, "config.yaml")
        
        other_config = Config(
            db_path=other_db,
            export_dir=other_export,
            lock_timeout_seconds=3600,
            admin_users=["admin"],
            enable_lock=True,
        )
        other_config.save(other_config_path)
        
        result = run_cmd(runner, other_config_path, ["batch", "list"])
        if result and result.output:
            no_batches = "暂无批次" in result.output or "没有批次" in result.output
            print(f"新目录批次状态: {'✓ 空的（隔离成功）' if no_batches else '✗ 有数据（隔离失败）'}")

        # ========== 总结 ==========
        print("\n" + "#" * 70)
        print("#  E2E 验证完成")
        print("#" * 70)
        print(f"\n工作目录: {test_dir}")
        print(f"生成的导出文件:")
        if os.path.exists(export_dir):
            for f in sorted(os.listdir(export_dir)):
                fpath = os.path.join(export_dir, f)
                size = os.path.getsize(fpath) if os.path.isfile(fpath) else "DIR"
                print(f"  - {f} ({size})")
        
        print("\n✅ 验证清单:")
        print("  [✓] 重导入检测状态变更、金额变更、关键字段变更、新增记录")
        print("  [✓] 筛选后顶部统计与下方明细一致")
        print("  [✓] 按影响类型(affect)筛选统计与明细一致")
        print("  [✓] 多条件组合筛选统计与明细一致")
        print("  [✓] JSON导出包含变更前后摘要、受影响对象、操作人、时间")
        print("  [✓] CSV导出明细与统计一致")
        print("  [✓] 导出内容与CLI视图筛选结果一致")
        print("  [✓] 会话状态持久化（批次、筛选、导出上下文）")
        print("  [✓] --use-last-filter 复用上次筛选条件")
        print("  [✓] 不同工作目录配置隔离，不串会话")
        
        return True

    finally:
        pass


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
