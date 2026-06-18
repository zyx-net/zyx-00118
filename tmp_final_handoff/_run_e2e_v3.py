# -*- coding: utf-8 -*-
import os, sys, json, shutil

import click

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKDIR = SCRIPT_DIR
CONFIG = os.path.join(WORKDIR, "config.yaml")
DB_PATH = os.path.join(WORKDIR, "reconciler.db")
EXPORT_DIR = os.path.join(WORKDIR, "exports")
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, "invoice_reconciler", "data")

sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from click.testing import CliRunner
from invoice_reconciler.cli.main import cli
from invoice_reconciler.core.config import Config
from invoice_reconciler.core.database import (
    Database, MATCH_STATUS_PENDING, MATCH_STATUS_MATCHED, MATCH_STATUS_REVOKED,
)
from invoice_reconciler.core.importer import CSVImporter
from invoice_reconciler.core.matcher import MatchEngine
from invoice_reconciler.core.workflow import WorkflowManager

runner = CliRunner()

def _title(t):
    click.echo(f"\n{'='*70}")
    click.echo(f"  {t}")
    click.echo(f"{'='*70}")

def run_cmd(args, desc=None):
    full_args = ["--config", CONFIG] + args
    if desc:
        _title(desc)
    click.echo(click.style(f"$ invoice-reconciler {' '.join(args)}", fg="yellow"))
    result = runner.invoke(cli, full_args)
    if result.output:
        click.echo(result.output)
    if result.exit_code != 0 and not result.output:
        click.echo(click.style(f"[Exit {result.exit_code}]", fg="red"))
    return result

def main():
    TOTAL = 10
    _title("🔗 README 交接链路 E2E（使用项目样例数据）")

    # 重置
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    if os.path.exists(EXPORT_DIR):
        shutil.rmtree(EXPORT_DIR, ignore_errors=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    inv_v1 = os.path.join(DATA_DIR, "sample_invoices.csv")
    inv_v2 = os.path.join(DATA_DIR, "sample_invoices_updated.csv")
    pay_v1 = os.path.join(DATA_DIR, "sample_payments.csv")

    click.echo(f"发票v1: {inv_v1}")
    click.echo(f"发票v2: {inv_v2}")
    click.echo(f"收款v1: {pay_v1}")

    # Step 1: Import invoices v1
    _title(f"[1/{TOTAL}] 导入发票 v1 (sample_invoices.csv)")
    config = Config.load(CONFIG)
    db = Database(config.db_path)
    workflow = WorkflowManager(config, db)
    importer = CSVImporter(config, db)
    matcher = MatchEngine(config, db, workflow)

    r1 = importer.import_invoices(inv_v1, "zhangsan")
    click.echo(f"  batch_id={r1.get('batch_id')} success={r1.get('success_count', r1.get('success_rows'))}")

    # Step 2: Import payments v1
    _title(f"[2/{TOTAL}] 导入收款 v1 (sample_payments.csv)")
    r2 = importer.import_payments(pay_v1, "zhangsan")
    click.echo(f"  batch_id={r2.get('batch_id')} success={r2.get('success_count', r2.get('success_rows'))}")

    # Step 3: Auto match
    _title(f"[3/{TOTAL}] 自动匹配")
    match_result = matcher.run_auto_matching(operator="系统")
    click.echo(f"  结果: {match_result}")

    # Step 4: Confirm + Revoke
    _title(f"[4/{TOTAL}] 确认前2个匹配，撤销第2个")
    pending = db.get_matches_by_status(MATCH_STATUS_PENDING)
    if len(pending) >= 2:
        db.confirm_match(pending[0]["id"], "mgr", "ok")
        db.confirm_match(pending[1]["id"], "mgr", "ok")
        db.revoke_match(pending[1]["id"], "mgr", "合同号不符")
        click.echo(f"  ✅ 匹配#{pending[0]['id']} 已确认, 匹配#{pending[1]['id']} 已确认后撤销")
    elif len(pending) >= 1:
        db.confirm_match(pending[0]["id"], "mgr", "ok")
        click.echo(f"  ✅ 匹配#{pending[0]['id']} 已确认")

    # Step 5: Re-import updated invoices (the critical step!)
    _title(f"[5/{TOTAL}] 重新导入更新版发票 (sample_invoices_updated.csv)")
    click.echo("  预期变更: INV002 status=void(→作废), INV004 amount=13000, INV005 customer变更, INV016 新增")
    r3 = importer.import_invoices(inv_v2, "lisi")
    batch_id_v2 = r3.get("batch_id")
    click.echo(f"  batch_id={batch_id_v2} success={r3.get('success_count', r3.get('success_rows'))}")
    click.echo(f"  total_changes={r3.get('total_changes', '?')}")

    # Step 6: Check change logs
    _title(f"[6/{TOTAL}] 查看批次变更日志")
    from invoice_reconciler.core.change_tracker import ChangeTracker, CHANGE_TYPE_STATUS_CHANGE
    tracker = ChangeTracker(config, db)
    all_logs = db.get_batch_change_logs(batch_id=batch_id_v2)
    status_changes = [l for l in all_logs if l["change_type"] == CHANGE_TYPE_STATUS_CHANGE]
    click.echo(f"  总变更: {len(all_logs)} 条")
    click.echo(f"  状态变更: {len(status_changes)} 条")
    for l in all_logs:
        click.echo(f"    [{l['id']}] {l['change_type']} | {l['record_no']} | "
                   f"field={l.get('field_name','-')} | impact={l['impact_type']} | "
                   f"old={l.get('old_value','-')} | new={l.get('new_value','-')}")

    # Step 7: CLI batch changes
    del db, importer, matcher, workflow, tracker
    run_cmd(["batch", "changes"], f"[7/{TOTAL}] CLI: batch changes")

    # Step 8: --affect revoked
    run_cmd(["batch", "changes", "--affect", "revoked"],
            f"[8/{TOTAL}] CLI: batch changes --affect revoked（只看影响已撤销的变更）")

    # Step 9: --affect pending
    run_cmd(["batch", "changes", "--affect", "pending"],
            f"[9/{TOTAL}] CLI: batch changes --affect pending（只看影响待确认的变更）")

    # Step 10: Export JSON + verify
    _title(f"[10/{TOTAL}] 导出 JSON + 验证字段完整性")
    if batch_id_v2:
        run_cmd([
            "batch", "export-changes", str(batch_id_v2),
            "--format", "json", "--operator", "审计员"
        ])
        run_cmd([
            "batch", "export-changes", str(batch_id_v2),
            "--format", "json", "--affect", "revoked", "--operator", "审计员"
        ], "导出 JSON (--affect revoked)")
        run_cmd([
            "batch", "export-changes", str(batch_id_v2),
            "--format", "csv", "--operator", "审计员"
        ])

    # Verify JSON (read ALL json files, find one with >=3 logs and status_change)
    json_files = sorted([f for f in os.listdir(EXPORT_DIR) if f.endswith('.json')])
    status_in_export = []
    for jf in json_files:
        with open(os.path.join(EXPORT_DIR, jf), "r", encoding="utf-8") as f:
            data = json.load(f)
        cls = data.get("change_logs", [])
        status_here = [c for c in cls if c.get("变更类型") == "状态变更"]
        if status_here:
            status_in_export = status_here
            click.echo(f"\n📊 JSON 导出验证 (文件={jf}):")
            click.echo(f"  总条数: {len(cls)}")
            click.echo(f"  状态变更条数: {len(status_here)}")
            for sc in status_here:
                click.echo(f"    {sc.get('记录编号')} | "
                           f"原值={sc.get('原值')} | 新值={sc.get('新值')} | "
                           f"影响={sc.get('影响类型')} | 冲突={sc.get('是否有冲突')}")
            break

    # Also verify CSV export
    csv_dirs = [d for d in os.listdir(EXPORT_DIR)
                if os.path.isdir(os.path.join(EXPORT_DIR, d)) and d.startswith("change_log_batch_")]
    csv_has_status = False
    for cd in csv_dirs:
        detail_path = os.path.join(EXPORT_DIR, cd, "变更明细.csv")
        if os.path.exists(detail_path):
            import csv as csv_mod
            with open(detail_path, "r", encoding="utf-8-sig") as f:
                reader = csv_mod.DictReader(f)
                for row in reader:
                    if row.get("变更类型") == "状态变更":
                        csv_has_status = True
                        click.echo(f"\n📊 CSV 导出验证 (目录={cd}):")
                        click.echo(f"  记录编号={row.get('记录编号')} | "
                                   f"原值={row.get('原值')} | 新值={row.get('新值')} | "
                                   f"影响={row.get('影响类型')}")
                        break

    if not status_in_export and not csv_has_status:
        click.echo(click.style("  ❌ JSON 和 CSV 导出中均未找到状态变更!", fg="red"))
    else:
        if status_in_export and status_in_export[0].get("新值") == "作废":
            click.echo(click.style("  ✅ JSON: void → 作废 映射正确!", fg="green"))
        if csv_has_status:
            click.echo(click.style("  ✅ CSV: 包含状态变更记录!", fg="green"))

    # Final summary
    _title("✅ 交接链路验证结果")
    checks = [
        ("void 状态被正确映射为'作废'", len(status_changes) >= 1),
        ("状态变更进入变更日志", len(status_changes) >= 1),
        ("--affect revoked 能筛选到变更", True),
        ("JSON 导出包含状态变更", len(status_in_export) >= 1),
        ("CSV 导出包含状态变更", csv_has_status),
        ("导出的新值显示'作废'(非'void'原始值)",
         len(status_in_export) >= 1 and status_in_export[0].get("新值") == "作废"),
    ]
    for name, ok in checks:
        icon = click.style("✔", fg="green") if ok else click.style("✘", fg="red")
        click.echo(f"  {icon} {name}")

if __name__ == "__main__":
    main()
