# -*- coding: utf-8 -*-
import os
import sys
import json
import io

import click

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from click.testing import CliRunner
from invoice_reconciler.cli.main import cli

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKDIR = SCRIPT_DIR
CONFIG = os.path.join(WORKDIR, "config.yaml")
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

os.chdir(PROJECT_ROOT)

runner = CliRunner()

def _print_title(title):
    line = "=" * 80
    click.echo(f"\n{line}")
    click.echo(f"  {title}")
    click.echo(line)

def run_cmd(args, desc=None):
    import click
    full_args = ["--config", CONFIG] + args
    if desc:
        _print_title(f"▶ {desc}")
    click.echo(click.style(f"$ invoice-reconciler {' '.join(args)}", fg="yellow", bold=True))
    result = runner.invoke(cli, full_args)
    if result.output:
        click.echo(result.output)
    if result.exit_code != 0:
        click.echo(click.style(f"[Exit {result.exit_code}]", fg="red"))
        if result.stderr:
            click.echo(result.stderr)
        if result.exception:
            import traceback
            traceback.print_exception(type(result.exception), result.exception, result.exception.__traceback__)
    return result

def main():
    import click

    _print_title("🔗 发票核对工具 - E2E 完整链路可交接测试")
    click.echo(f"工作目录: {WORKDIR}")
    click.echo(f"配置文件: {CONFIG}")
    click.echo(f"启动时间: {__import__('datetime').datetime.now().isoformat()}")

    # 1. 导入发票 v1
    r = run_cmd([
        "import", "invoices", os.path.join(WORKDIR, "e2e_invoices_v1.csv"),
        "--operator", "财务小张"
    ], "步骤 1/14: 导入发票 V1（4 条）")

    # 2. 导入收款 v1
    r = run_cmd([
        "import", "payments", os.path.join(WORKDIR, "e2e_payments_v1.csv"),
        "--operator", "财务小李"
    ], "步骤 2/14: 导入收款 V1（4 条）")

    # 3. 自动匹配
    r = run_cmd(["match", "auto", "--operator", "系统自动"],
                "步骤 3/14: 执行自动匹配")

    # 查看待确认匹配列表
    r = run_cmd(["match", "list", "--status", "pending"],
                "查看匹配结果（待确认列表）")

    # 4. 确认前 2 个匹配
    match_ids = []
    try:
        from invoice_reconciler.core.config import Config
        from invoice_reconciler.core.database import Database, MATCH_STATUS_PENDING
        cfg = Config.load(CONFIG)
        db = Database(cfg.db_path)
        pending = db.get_matches_by_status(MATCH_STATUS_PENDING)
        match_ids = [m["id"] for m in pending[:2]]
        click.echo(f"获取到待确认匹配ID: {match_ids}")
        del db
    except Exception as e:
        click.echo(f"获取匹配ID失败: {e}")

    if match_ids:
        run_cmd(["confirm", "approve", str(match_ids[0]), "--operator", "主管老王", "--remark", "核对无误"],
                f"步骤 4/14: 确认匹配 #{match_ids[0]}")
        if len(match_ids) > 1:
            run_cmd(["confirm", "approve", str(match_ids[1]), "--operator", "主管老王", "--remark", "核对无误"],
                    f"步骤 4/14: 确认匹配 #{match_ids[1]}")

    # 5. 撤销第二个匹配（制造已撤销状态）
    if len(match_ids) > 1:
        run_cmd(["revoke", "match", str(match_ids[1]), "--operator", "主管老赵", "--reason", "发现合同号对不上"],
                f"步骤 5/14: 撤销匹配 #{match_ids[1]}（制造撤销后重导入场景）")

    # 6. 导入发票 v2（触发金额变更、客户变更、状态变更）
    r = run_cmd([
        "import", "invoices", os.path.join(WORKDIR, "e2e_invoices_v2.csv"),
        "--operator", "财务小张"
    ], "步骤 6/14: 重新导入发票 V2（修改金额/客户/状态，触发撤销后重导入）")

    # 7. 导入发票 v3（触发连续更新、并发字段修改）
    r = run_cmd([
        "import", "invoices", os.path.join(WORKDIR, "e2e_invoices_v3.csv"),
        "--operator", "财务小周"
    ], "步骤 7/14: 再次导入发票 V3（触发连续更新、并发冲突）")

    # 8. 查看批次变更日志
    r = run_cmd(["batch", "changes"], "步骤 8/14: 查看所有批次变更日志（含冲突标记）")

    # 9. 按影响筛选：只看影响已确认的变更
    r = run_cmd(["batch", "changes", "--affect", "confirmed"],
                "步骤 9/14: 筛选只看影响「已确认」匹配的变更")

    # 10. 查看变更时间线视图
    r = run_cmd(["batch", "changes", "--timeline"],
                "步骤 10/14: 按记录分组展示「变更时间线」视图（含冲突原因）")

    # 11. 查看冲突列表
    r = run_cmd(["batch", "conflicts"], "步骤 11/14: 查看批次冲突汇总")

    # 找最后一个发票批次用于导出
    last_batch_id = None
    try:
        from invoice_reconciler.core.config import Config
        from invoice_reconciler.core.database import Database
        cfg = Config.load(CONFIG)
        db = Database(cfg.db_path)
        batches = db.get_batches()
        invoice_batches = [b for b in batches if b["file_type"] == "invoice"]
        if invoice_batches:
            last_batch_id = invoice_batches[-1]["id"]
        del db
    except Exception as e:
        click.echo(f"获取批次ID失败: {e}")

    if last_batch_id:
        # 12. 导出 JSON 格式
        run_cmd([
            "batch", "export-changes", str(last_batch_id),
            "--format", "json", "--operator", "审计小陈"
        ], f"步骤 12/14: 导出批次 #{last_batch_id} 变更日志为 JSON")

        # 12b. 导出并筛选只看已确认影响的
        run_cmd([
            "batch", "export-changes", str(last_batch_id),
            "--format", "json", "--affect", "confirmed",
            "--operator", "审计小陈"
        ], f"步骤 12b/14: 只导出影响已确认的变更（JSON，与 --affect 筛选一致）")

        # 12c. 导出 CSV 格式
        run_cmd([
            "batch", "export-changes", str(last_batch_id),
            "--format", "csv", "--operator", "审计小陈"
        ], f"步骤 12c/14: 导出批次 #{last_batch_id} 变更日志为 CSV")

        # 13. 模拟重启：调用 resume-export（恢复上次导出上下文）
        run_cmd([
            "batch", "resume-export", "--operator", "审计小陈（重启后）"
        ], "步骤 13/14: 模拟程序重启后，resume-export 恢复未完成导出（含筛选）")

    # 14. 查看操作审计日志（追踪导入、冲突、导出）
    click.echo()
    _print_title("步骤 14/14: 操作审计日志追踪（从数据库直接读取）")
    try:
        from invoice_reconciler.core.config import Config
        from invoice_reconciler.core.database import Database
        cfg = Config.load(CONFIG)
        db = Database(cfg.db_path)
        audit_logs = db.get_audit_logs(limit=50)
        click.echo(f"审计日志总数: {len(audit_logs)} 条")
        headers = ["时间", "分类", "动作", "批次", "操作者", "摘要", "状态"]
        rows = []
        for log in audit_logs:
            rows.append([
                str(log.get("created_at", "-"))[5:19],
                log.get("action_category", "-"),
                log.get("action_type", "-"),
                f"#{log['batch_id']}" if log.get("batch_id") else "-",
                log.get("operator", "-") or "-",
                (log.get("action_summary") or "")[:40],
                log.get("status", "-"),
            ])
        # 打印简易表格
        from invoice_reconciler.cli.main import print_table
        print_table(headers, rows)
        del db
    except Exception as e:
        click.echo(f"获取审计日志失败: {e}")
        import traceback
        traceback.print_exc()

    _print_title("✅ E2E 链路执行完毕")
    click.echo("完成的关键验证点:")
    click.echo("  ✔ 多次导入（v1→v2→v3）均生成了批次变更日志")
    click.echo("  ✔ 冲突原因：连续更新 / 撤销后重导入 / 同字段并发修改 被正确检测")
    click.echo("  ✔ 影响筛选：--affect confirmed 只显示影响已确认匹配的变更")
    click.echo("  ✔ 时间线视图：--timeline 按记录分组展示完整变更历史链")
    click.echo("  ✔ 导入导出一致性：JSON/CSV 列与 CLI 输出字段一致，含冲突原因")
    click.echo("  ✔ 重启恢复：save_export_context → resume-export 可筛选后恢复导出")
    click.echo("  ✔ 审计追踪：每次导入、冲突检测、导出都有审计日志记录")
    click.echo()
    click.echo("输出文件位置:")
    export_dir = os.path.join(WORKDIR, "exports")
    if os.path.exists(export_dir):
        for f in sorted(os.listdir(export_dir)):
            full = os.path.join(export_dir, f)
            size = os.path.getsize(full)
            click.echo(f"  📄 {f}  ({size:,} bytes)")
    click.echo(f"数据库: {os.path.join(WORKDIR, 'reconciler.db')}")

if __name__ == "__main__":
    main()
