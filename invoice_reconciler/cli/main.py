import os
import sys
import io
import json
from datetime import datetime
import click

if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
        )
    except Exception:
        pass

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
from invoice_reconciler.core.exporter import ReportExporter, STATUS_LABELS, MATCH_TYPE_LABELS
from invoice_reconciler.core.reviewer import (
    ReviewSnapshot,
    SNAPSHOT_TYPE_LABELS,
    SNAPSHOT_TYPE_MANUAL,
)
from invoice_reconciler.core.workflow import (
    WorkflowManager,
    LOCK_STATUS_LABELS,
    USER_ROLE_LABELS,
)


def get_current_user() -> str:
    return os.environ.get("USER", os.environ.get("USERNAME", "unknown"))


def print_table(headers, rows):
    if not rows:
        click.echo("无数据")
        return

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))

    format_str = " | ".join([f"{{:<{w}}}" for w in col_widths])
    click.echo(format_str.format(*headers))
    click.echo("-+-".join(["-" * w for w in col_widths]))
    for row in rows:
        click.echo(format_str.format(*[str(c) for c in row]))


@click.group()
@click.option("--config", "config_path", default=None, help="配置文件路径")
@click.pass_context
def cli(ctx, config_path):
    """发票与收款核对 CLI 工具"""
    try:
        config = Config.load(config_path)
        errors = config.validate()
        if errors:
            click.echo("配置错误:")
            for err in errors:
                click.echo(f"  - {err}")
            sys.exit(1)

        db = Database(config.db_path)
        workflow = WorkflowManager(config, db)
        ctx.obj = {
            "config": config,
            "db": db,
            "workflow": workflow,
            "importer": CSVImporter(config, db),
            "matcher": MatchEngine(config, db, workflow),
            "revoker": Revoker(db, config, workflow),
            "exporter": ReportExporter(config, db),
            "reviewer": ReviewSnapshot(db),
        }
    except Exception as e:
        click.echo(f"初始化失败: {e}", err=True)
        sys.exit(1)


@cli.group()
def config():
    """配置管理"""
    pass


@config.command("show")
@click.pass_context
def config_show(ctx):
    """显示当前配置"""
    cfg = ctx.obj["config"]
    click.echo("=== 当前配置 ===")
    for key, value in cfg.to_dict().items():
        if isinstance(value, list):
            value = ", ".join(value)
        click.echo(f"  {key}: {value}")


@config.command("set")
@click.option("--amount-tolerance", type=float, help="金额容差")
@click.option("--date-window", type=int, help="日期窗口天数")
@click.option("--export-format", type=click.Choice(["xlsx", "csv"]), help="导出格式")
@click.option("--config-path", "save_path", default=None, help="保存路径")
@click.pass_context
def config_set(ctx, amount_tolerance, date_window, export_format, save_path):
    """修改配置"""
    cfg = ctx.obj["config"]

    if amount_tolerance is not None:
        cfg.amount_tolerance = amount_tolerance
    if date_window is not None:
        cfg.date_window_days = date_window
    if export_format is not None:
        cfg.export_format = export_format

    errors = cfg.validate()
    if errors:
        click.echo("配置错误:")
        for err in errors:
            click.echo(f"  - {err}")
        return

    save_path = save_path or "invoice_reconciler/data/config.yaml"
    cfg.save(save_path)
    click.echo(f"配置已保存到: {save_path}")


@cli.group()
def import_cmd():
    """CSV 数据导入"""
    pass


@import_cmd.command("invoices")
@click.argument("file_path")
@click.option("--operator", default=None, help="操作者")
@click.option("--preview", is_flag=True, help="仅预览不导入")
@click.pass_context
def import_invoices(ctx, file_path, operator, preview):
    """导入发票台账 CSV"""
    importer = ctx.obj["importer"]
    operator = operator or get_current_user()

    if preview:
        result = importer.preview_csv(file_path, "invoice")
        click.echo(f"文件: {file_path}")
        click.echo(f"列: {', '.join(result['headers'])}")
        if result["missing_columns"]:
            click.echo(click.style(
                f"缺少必填列: {', '.join(result['missing_columns'])}",
                fg="red"
            ))
        click.echo(f"\n预览前 {len(result['preview_rows'])} 行:")
        for i, row in enumerate(result["preview_rows"], 1):
            click.echo(f"  行 {i}: {json.dumps(row, ensure_ascii=False)}")
        return

    try:
        result = importer.import_invoices(file_path, operator)
        if result.get("skipped"):
            click.echo(click.style(result["message"], fg="yellow"))
        else:
            click.echo(click.style(result["message"], fg="green"))
        click.echo(f"批次ID: {result['batch_id']}")
        click.echo(f"总计: {result['total_rows']}, 成功: {result['success_rows']}, 失败: {result['failed_rows']}")
    except Exception as e:
        click.echo(click.style(f"导入失败: {e}", fg="red"), err=True)
        sys.exit(1)


@import_cmd.command("payments")
@click.argument("file_path")
@click.option("--operator", default=None, help="操作者")
@click.option("--preview", is_flag=True, help="仅预览不导入")
@click.pass_context
def import_payments(ctx, file_path, operator, preview):
    """导入收款流水 CSV"""
    importer = ctx.obj["importer"]
    operator = operator or get_current_user()

    if preview:
        result = importer.preview_csv(file_path, "payment")
        click.echo(f"文件: {file_path}")
        click.echo(f"列: {', '.join(result['headers'])}")
        if result["missing_columns"]:
            click.echo(click.style(
                f"缺少必填列: {', '.join(result['missing_columns'])}",
                fg="red"
            ))
        click.echo(f"\n预览前 {len(result['preview_rows'])} 行:")
        for i, row in enumerate(result["preview_rows"], 1):
            click.echo(f"  行 {i}: {json.dumps(row, ensure_ascii=False)}")
        return

    try:
        result = importer.import_payments(file_path, operator)
        if result.get("skipped"):
            click.echo(click.style(result["message"], fg="yellow"))
        else:
            click.echo(click.style(result["message"], fg="green"))
        click.echo(f"批次ID: {result['batch_id']}")
        click.echo(f"总计: {result['total_rows']}, 成功: {result['success_rows']}, 失败: {result['failed_rows']}")
    except Exception as e:
        click.echo(click.style(f"导入失败: {e}", fg="red"), err=True)
        sys.exit(1)


@import_cmd.command("batches")
@click.pass_context
def import_batches(ctx):
    """查看导入批次"""
    db = ctx.obj["db"]
    batches = db.get_batches()

    if not batches:
        click.echo("暂无导入批次")
        return

    headers = ["ID", "类型", "文件名", "总行数", "成功", "失败", "操作者", "导入时间"]
    rows = []
    for b in batches:
        rows.append([
            b["id"],
            "发票" if b["file_type"] == "invoice" else "收款",
            b["file_name"],
            b["total_rows"],
            b["success_rows"],
            b["failed_rows"],
            b["operator"] or "-",
            b["imported_at"],
        ])
    print_table(headers, rows)


@import_cmd.command("errors")
@click.option("--batch-id", type=int, default=None, help="批次ID")
@click.pass_context
def import_errors(ctx, batch_id):
    """查看导入错误"""
    db = ctx.obj["db"]
    errors = db.get_errors(batch_id)

    if not errors:
        click.echo("暂无错误记录")
        return

    headers = ["ID", "批次ID", "类型", "行号", "错误类型", "错误信息"]
    rows = []
    for e in errors:
        rows.append([
            e["id"],
            e["batch_id"],
            "发票" if e["file_type"] == "invoice" else "收款",
            e["file_row_num"],
            e["error_type"],
            e["error_message"],
        ])
    print_table(headers, rows)


@cli.command("match")
@click.option("--operator", default=None, help="操作者")
@click.pass_context
def run_match(ctx, operator):
    """执行自动匹配"""
    matcher = ctx.obj["matcher"]
    operator = operator or get_current_user()

    click.echo("开始自动匹配...")
    result = matcher.run_auto_matching(operator)

    click.echo(click.style(f"处理发票: {result['total_invoices']}, 收款: {result['total_payments']}", fg="cyan"))
    click.echo(f"  精确匹配: {result['exact_matches']}")
    click.echo(f"  模糊匹配(待确认): {result['fuzzy_matches']}")
    click.echo(f"  多候选(待确认): {result['multi_candidate_invoices']}")
    click.echo(click.style(f"共创建 {result['created_matches']} 个匹配", fg="green"))

    if result["auto_exact_match_nos"]:
        click.echo(f"\n自动确认的匹配编号: {', '.join(result['auto_exact_match_nos'])}")
    if result["pending_match_ids"]:
        click.echo(f"\n待人工确认的匹配ID: {', '.join(map(str, result['pending_match_ids']))}")


@cli.group()
def confirm():
    """人工确认管理"""
    pass


@confirm.command("list")
@click.option("--status", default="pending",
              type=click.Choice(["pending", "matched", "exception", "all"]),
              help="状态过滤")
@click.pass_context
def confirm_list(ctx, status):
    """列出匹配记录"""
    db = ctx.obj["db"]

    if status == "all":
        matches = db.get_matches_by_status()
    else:
        matches = db.get_matches_by_status(status)

    if not matches:
        click.echo("暂无匹配记录")
        return

    headers = ["ID", "匹配编号", "类型", "状态", "发票号", "金额", "收款号", "金额", "得分", "确认人"]
    rows = []
    for m in matches:
        rows.append([
            m["id"],
            m["match_no"],
            MATCH_TYPE_LABELS.get(m["match_type"], m["match_type"]),
            STATUS_LABELS.get(m["status"], m["status"]),
            m["invoice_no"],
            f"{m['inv_amount']:.2f}",
            m["payment_no"],
            f"{m['pay_amount']:.2f}",
            f"{m['match_score']:.1f}",
            m["operator"] or "-",
        ])
    print_table(headers, rows)


@confirm.command("show")
@click.argument("match_id", type=int)
@click.pass_context
def confirm_show(ctx, match_id):
    """显示匹配详情"""
    db = ctx.obj["db"]
    match = db.get_match_by_id(match_id)
    if not match:
        click.echo(click.style(f"匹配记录不存在: {match_id}", fg="red"))
        return

    click.echo(f"=== 匹配详情 (ID: {match_id}) ===")
    click.echo(f"匹配编号: {match['match_no']}")
    click.echo(f"类型: {MATCH_TYPE_LABELS.get(match['match_type'], match['match_type'])}")
    click.echo(f"状态: {STATUS_LABELS.get(match['status'], match['status'])}")
    click.echo(f"匹配得分: {match['match_score']:.1f}")
    click.echo(f"匹配证据: {match['match_evidence']}")
    click.echo(f"\n--- 发票 ---")
    click.echo(f"  发票号: {match['invoice_no']}")
    click.echo(f"  日期: {match['invoice_date']}")
    click.echo(f"  客户: {match['inv_customer']}")
    click.echo(f"  金额: {match['inv_amount']:.2f}")
    click.echo(f"\n--- 收款 ---")
    click.echo(f"  收款号: {match['payment_no']}")
    click.echo(f"  日期: {match['payment_date']}")
    click.echo(f"  客户: {match['pay_customer']}")
    click.echo(f"  金额: {match['pay_amount']:.2f}")
    click.echo(f"\n--- 处理 ---")
    click.echo(f"  确认人: {match['operator'] or '-'}")
    click.echo(f"  备注: {match['operator_remark'] or '-'}")
    click.echo(f"  确认时间: {match['confirmed_at'] or '-'}")
    click.echo(f"  创建时间: {match['created_at']}")

    history = db.get_status_history(match_id=match_id)
    if history:
        click.echo(f"\n--- 状态历史 ---")
        for h in history:
            click.echo(
                f"  {h['changed_at']}: "
                f"{STATUS_LABELS.get(h['old_status'], h['old_status'])} -> "
                f"{STATUS_LABELS.get(h['new_status'], h['new_status'])} "
                f"(操作人: {h['operator'] or '系统'}, 备注: {h['remark'] or '-'})"
            )


@confirm.command("approve")
@click.argument("match_id", type=int)
@click.option("--operator", default=None, help="操作者")
@click.option("--remark", default=None, help="确认备注")
@click.option("--select-payment", type=int, default=None, help="选择其他候选收款ID")
@click.pass_context
def confirm_approve(ctx, match_id, operator, remark, select_payment):
    """确认匹配"""
    matcher = ctx.obj["matcher"]
    operator = operator or get_current_user()

    try:
        result = matcher.confirm_match(match_id, operator, remark, select_payment)
        click.echo(click.style(f"[OK] {result['message']}", fg="green"))
        click.echo(f"匹配编号: {result['match_no']}")
    except ValueError as e:
        click.echo(click.style(f"[!!] {e}", fg="red"), err=True)
        sys.exit(1)


@confirm.command("reject")
@click.argument("match_id", type=int)
@click.option("--operator", default=None, help="操作者")
@click.option("--remark", required=True, help="拒绝原因")
@click.pass_context
def confirm_reject(ctx, match_id, operator, remark):
    """拒绝匹配"""
    matcher = ctx.obj["matcher"]
    operator = operator or get_current_user()

    try:
        result = matcher.reject_match(match_id, operator, remark)
        click.echo(click.style(f"[OK] {result['message']}", fg="green"))
    except ValueError as e:
        click.echo(click.style(f"[!!] {e}", fg="red"), err=True)
        sys.exit(1)


@confirm.command("manual")
@click.argument("invoice_id", type=int)
@click.argument("payment_id", type=int)
@click.option("--operator", default=None, help="操作者")
@click.option("--remark", default=None, help="匹配备注")
@click.pass_context
def confirm_manual(ctx, invoice_id, payment_id, operator, remark):
    """人工强制匹配"""
    matcher = ctx.obj["matcher"]
    operator = operator or get_current_user()

    try:
        result = matcher.manual_match(invoice_id, payment_id, operator, remark)
        click.echo(click.style(f"[OK] {result['message']}", fg="green"))
        click.echo(f"匹配编号: {result['match_no']}")
    except ValueError as e:
        click.echo(click.style(f"[!!] {e}", fg="red"), err=True)
        sys.exit(1)


@confirm.command("candidates")
@click.option("--invoice-id", type=int, default=None, help="指定发票ID")
@click.pass_context
def confirm_candidates(ctx, invoice_id):
    """查看多候选匹配"""
    matcher = ctx.obj["matcher"]
    db = ctx.obj["db"]

    if invoice_id:
        candidates = db.get_match_candidates(invoice_id)
        if not candidates:
            click.echo("该发票暂无候选收款")
            return

        pending_matches = [m for m in db.get_matches_by_status(MATCH_STATUS_PENDING)
                           if m["invoice_id"] == invoice_id]
        match_info = ""
        if pending_matches:
            match_info = f" (匹配ID: {pending_matches[0]['id']}, 匹配编号: {pending_matches[0]['match_no']})"

        click.echo(f"=== 发票 {candidates[0]['invoice_no']} 的候选收款{match_info} ===")
        headers = ["收款ID", "收款号", "日期", "客户", "金额", "得分", "匹配原因"]
        rows = []
        for c in candidates:
            rows.append([
                c["payment_id"],
                c["payment_no"],
                c["payment_date"],
                c["pay_customer"],
                f"{c['pay_amount']:.2f}",
                f"{c['match_score']:.1f}",
                c["match_reason"],
            ])
        print_table(headers, rows)
        click.echo()
        if pending_matches:
            click.echo(f"操作: confirm approve {pending_matches[0]['id']} --select-payment <收款ID>")
        else:
            click.echo("提示: 该发票暂无待确认的匹配记录")
    else:
        invoices = matcher.get_multi_candidate_invoices()
        if not invoices:
            click.echo("暂无可选的多候选匹配")
            return

        for inv in invoices:
            click.echo(f"\n=== 发票 {inv['invoice_no']} (金额: {inv['inv_amount']:.2f}) ===")
            headers = ["收款ID", "收款号", "日期", "客户", "金额", "得分"]
            rows = []
            for c in inv["candidates"]:
                rows.append([
                    c["payment_id"],
                    c["payment_no"],
                    c["payment_date"],
                    c["pay_customer"],
                    f"{c['pay_amount']:.2f}",
                    f"{c['match_score']:.1f}",
                ])
            print_table(headers, rows)


@cli.group()
def revoke():
    """撤销匹配"""
    pass


@revoke.command("match")
@click.argument("match_id", type=int)
@click.option("--operator", default=None, help="操作者")
@click.option("--remark", default=None, help="撤销备注")
@click.pass_context
def revoke_match(ctx, match_id, operator, remark):
    """按ID撤销匹配"""
    revoker = ctx.obj["revoker"]
    operator = operator or get_current_user()

    result = revoker.revoke_match(match_id, operator, remark)
    if result["success"]:
        click.echo(click.style(f"[OK] {result['message']}", fg="green"))
        click.echo(f"匹配编号: {result['match_no']}")
        click.echo(f"发票: {result['invoice_no']}, 收款: {result['payment_no']}")
    else:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)


@revoke.command("by-no")
@click.argument("match_no")
@click.option("--operator", default=None, help="操作者")
@click.option("--remark", default=None, help="撤销备注")
@click.pass_context
def revoke_by_no(ctx, match_no, operator, remark):
    """按匹配编号撤销"""
    revoker = ctx.obj["revoker"]
    operator = operator or get_current_user()

    result = revoker.revoke_by_match_no(match_no, operator, remark)
    if result["success"]:
        click.echo(click.style(f"[OK] {result['message']}", fg="green"))
    else:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)


@revoke.command("list")
@click.option("--revoked", is_flag=True, help="显示已撤销的匹配")
@click.pass_context
def revoke_list(ctx, revoked):
    """列出可撤销/已撤销的匹配"""
    revoker = ctx.obj["revoker"]

    if revoked:
        matches = revoker.get_revoked_matches()
        title = "已撤销的匹配"
    else:
        matches = revoker.get_revokable_matches()
        title = "可撤销的匹配"

    if not matches:
        click.echo(f"暂无{title}")
        return

    click.echo(f"=== {title} ===")
    headers = ["ID", "匹配编号", "发票号", "金额", "收款号", "金额", "确认人", "确认时间"]
    rows = []
    for m in matches:
        rows.append([
            m["id"],
            m["match_no"],
            m["invoice_no"],
            f"{m['inv_amount']:.2f}",
            m["payment_no"],
            f"{m['pay_amount']:.2f}",
            m["operator"] or "-",
            m["confirmed_at"] or "-",
        ])
    print_table(headers, rows)


@revoke.command("history")
@click.option("--match-id", type=int, default=None, help="匹配ID")
@click.option("--match-no", default=None, help="匹配编号")
@click.pass_context
def revoke_history(ctx, match_id, match_no):
    """查看状态历史"""
    revoker = ctx.obj["revoker"]
    history = revoker.get_match_status_history(match_id, match_no)

    if not history:
        click.echo("暂无状态历史")
        return

    headers = ["时间", "原状态", "新状态", "操作人", "备注"]
    rows = []
    for h in history:
        rows.append([
            h["changed_at"],
            STATUS_LABELS.get(h["old_status"], h["old_status"]),
            STATUS_LABELS.get(h["new_status"], h["new_status"]),
            h["operator"] or "系统",
            h["remark"] or "-",
        ])
    print_table(headers, rows)


@cli.group()
def export():
    """导出报告"""
    pass


@export.command("full")
@click.option("--operator", default=None, help="操作者")
@click.option("--format", "export_format", type=click.Choice(["xlsx", "csv", "json"]),
              default=None, help="导出格式，默认使用配置文件中的设置")
@click.pass_context
def export_full(ctx, operator, export_format):
    """导出完整报告"""
    exporter = ctx.obj["exporter"]
    operator = operator or get_current_user()

    click.echo("正在生成完整报告...")
    result = exporter.export_full_report(operator, format=export_format)

    click.echo(click.style(f"[OK] 报告已生成: {result['file_path']}", fg="green"))
    click.echo(f"格式: {result['format']}")
    click.echo(f"生成时间: {result['generated_at']}")
    click.echo("\n报告摘要:")
    for key, value in result["summary"].items():
        click.echo(f"  {key}: {value}")


@export.command("diff")
@click.option("--operator", default=None, help="操作者")
@click.option("--format", "export_format", type=click.Choice(["xlsx", "csv", "json"]),
              default=None, help="导出格式，默认使用配置文件中的设置")
@click.pass_context
def export_diff(ctx, operator, export_format):
    """导出差异报告"""
    exporter = ctx.obj["exporter"]
    operator = operator or get_current_user()

    click.echo("正在生成差异报告...")
    result = exporter.export_diff_report(operator, format=export_format)

    click.echo(click.style(f"[OK] 差异报告已生成: {result['file_path']}", fg="green"))
    click.echo(f"格式: {result['format']}")
    click.echo(f"\n差异摘要:")
    click.echo(f"  未匹配发票金额: {result['unmatched_invoice_amount']:.2f}")
    click.echo(f"  未匹配收款金额: {result['unmatched_payment_amount']:.2f}")
    click.echo(click.style(
        f"  差异金额: {result['diff_amount']:.2f}",
        fg="red" if abs(result['diff_amount']) > 0 else "green"
    ))


@export.command("snapshot")
@click.option("--snapshot-no", default=None, help="快照编号")
@click.option("--snapshot-id", type=int, default=None, help="快照ID")
@click.option("--operator", default=None, help="操作者")
@click.option("--format", "export_format", type=click.Choice(["xlsx", "csv", "json"]),
              default=None, help="导出格式，默认使用配置文件中的设置")
@click.pass_context
def export_snapshot(ctx, snapshot_no, snapshot_id, operator, export_format):
    """导出复核快照"""
    if not snapshot_no and not snapshot_id:
        click.echo(click.style("请指定 --snapshot-no 或 --snapshot-id", fg="red"), err=True)
        sys.exit(1)

    reviewer = ctx.obj["reviewer"]
    exporter = ctx.obj["exporter"]
    operator = operator or get_current_user()

    try:
        snapshot_data = reviewer.get_snapshot_for_export(
            snapshot_no=snapshot_no, snapshot_id=snapshot_id
        )
    except ValueError as e:
        click.echo(click.style(f"[!!] {e}", fg="red"), err=True)
        sys.exit(1)

    click.echo("正在导出快照...")
    result = exporter.export_snapshot(snapshot_data, operator, format=export_format)

    click.echo(click.style(f"[OK] 快照已导出: {result['file_path']}", fg="green"))
    click.echo(f"快照编号: {result['snapshot_no']}")
    click.echo(f"格式: {result['format']}")
    click.echo(f"记录数: {result['item_count']}")


@cli.command("status")
@click.pass_context
def show_status(ctx):
    """显示当前核对状态"""
    matcher = ctx.obj["matcher"]
    db = ctx.obj["db"]
    summary = matcher.get_match_summary()
    stats = summary["statistics"]

    click.echo("=== 核对状态概览 ===")
    click.echo(f"发票总数: {stats['total_invoices']}")
    click.echo(f"收款总数: {stats['total_payments']}")
    click.echo()
    click.echo(f"已匹配发票: {stats['matched_invoices']} / {stats['total_invoices']}")
    click.echo(f"已匹配收款: {stats['matched_payments']} / {stats['total_payments']}")
    click.echo(f"已匹配总金额: {summary['matched_amount_total']:.2f}")
    click.echo()
    click.echo(f"待确认匹配: {stats['pending_matches']}")
    click.echo(f"待确认金额: {summary['pending_amount_total']:.2f}")
    click.echo(f"异常匹配: {stats['exception_matches']}")
    click.echo(f"已撤销匹配: {stats['revoked_matches']}")
    click.echo()
    click.echo(f"未匹配发票: {stats['unmatched_invoices']}")
    click.echo(f"未匹配收款: {stats['unmatched_payments']}")
    click.echo()
    click.echo(f"无效发票: {stats['invalid_invoices']}")
    click.echo(f"无效收款: {stats['invalid_payments']}")
    click.echo(f"导入错误: {stats['total_errors']}")

    if summary["pending"]:
        click.echo(f"\n=== 待确认匹配 ({len(summary['pending'])}) ===")
        headers = ["ID", "匹配编号", "类型", "发票号", "金额", "收款号", "金额", "得分"]
        rows = []
        for m in summary["pending"]:
            rows.append([
                m["id"],
                m["match_no"],
                MATCH_TYPE_LABELS.get(m["match_type"], m["match_type"]),
                m["invoice_no"],
                f"{m['inv_amount']:.2f}",
                m["payment_no"],
                f"{m['pay_amount']:.2f}",
                f"{m['match_score']:.1f}",
            ])
        print_table(headers, rows)

    if summary["exception"]:
        click.echo(f"\n=== 异常匹配 ({len(summary['exception'])}) ===")
        headers = ["ID", "匹配编号", "发票号", "收款号", "处理人", "原因"]
        rows = []
        for m in summary["exception"]:
            rows.append([
                m["id"],
                m["match_no"],
                m["invoice_no"],
                m["payment_no"],
                m["operator"] or "-",
                (m["operator_remark"] or "")[:30],
            ])
        print_table(headers, rows)

    if summary["unmatched_invoices"]:
        click.echo(f"\n=== 未匹配发票 ({len(summary['unmatched_invoices'])}) ===")
        headers = ["ID", "发票号", "日期", "客户", "金额", "行号"]
        rows = []
        for inv in summary["unmatched_invoices"][:10]:
            rows.append([
                inv["id"],
                inv["invoice_no"],
                inv["invoice_date"],
                inv["customer"],
                f"{inv['amount']:.2f}",
                inv["file_row_num"],
            ])
        print_table(headers, rows)
        if len(summary["unmatched_invoices"]) > 10:
            click.echo(f"... 还有 {len(summary['unmatched_invoices']) - 10} 条")

    if summary["unmatched_payments"]:
        click.echo(f"\n=== 未匹配收款 ({len(summary['unmatched_payments'])}) ===")
        headers = ["ID", "收款号", "日期", "客户", "金额", "行号"]
        rows = []
        for pay in summary["unmatched_payments"][:10]:
            rows.append([
                pay["id"],
                pay["payment_no"],
                pay["payment_date"],
                pay["customer"],
                f"{pay['amount']:.2f}",
                pay["file_row_num"],
            ])
        print_table(headers, rows)
        if len(summary["unmatched_payments"]) > 10:
            click.echo(f"... 还有 {len(summary['unmatched_payments']) - 10} 条")


@cli.command("list-unmatched")
@click.option("--type", "list_type", default="all",
              type=click.Choice(["all", "invoices", "payments"]),
              help="列出类型")
@click.pass_context
def list_unmatched(ctx, list_type):
    """列出未匹配项"""
    db = ctx.obj["db"]

    if list_type in ["all", "invoices"]:
        invoices = db.get_unmatched_invoices()
        click.echo(f"\n=== 未匹配发票 ({len(invoices)}) ===")
        if invoices:
            headers = ["ID", "发票号", "日期", "客户", "金额", "行号", "来源文件"]
            rows = []
            for inv in invoices:
                rows.append([
                    inv["id"],
                    inv["invoice_no"],
                    inv["invoice_date"],
                    inv["customer"],
                    f"{inv['amount']:.2f}",
                    inv["file_row_num"],
                    inv.get("file_name", "-"),
                ])
            print_table(headers, rows)
        else:
            click.echo("无未匹配发票")

    if list_type in ["all", "payments"]:
        payments = db.get_unmatched_payments()
        click.echo(f"\n=== 未匹配收款 ({len(payments)}) ===")
        if payments:
            headers = ["ID", "收款号", "日期", "客户", "金额", "行号", "来源文件"]
            rows = []
            for pay in payments:
                rows.append([
                    pay["id"],
                    pay["payment_no"],
                    pay["payment_date"],
                    pay["customer"],
                    f"{pay['amount']:.2f}",
                    pay["file_row_num"],
                    pay.get("file_name", "-"),
                ])
            print_table(headers, rows)
        else:
            click.echo("无未匹配收款")


@cli.group()
def review():
    """复核快照与回放校验"""
    pass


@review.group("snapshot")
def review_snapshot():
    """复核快照管理"""
    pass


@review_snapshot.command("create")
@click.option("--description", default=None, help="快照描述")
@click.option("--operator", default=None, help="操作者")
@click.pass_context
def snapshot_create(ctx, description, operator):
    """创建复核快照"""
    reviewer = ctx.obj["reviewer"]
    operator = operator or get_current_user()

    click.echo("正在生成复核快照...")
    snapshot = reviewer.create_snapshot(
        snapshot_type=SNAPSHOT_TYPE_MANUAL,
        description=description,
        operator=operator
    )

    click.echo(click.style(f"[OK] 快照已创建", fg="green"))
    click.echo(f"快照编号: {snapshot['snapshot_no']}")
    click.echo(f"类型: {SNAPSHOT_TYPE_LABELS.get(snapshot['snapshot_type'], snapshot['snapshot_type'])}")
    click.echo(f"总匹配数: {snapshot['total_matches']}")
    click.echo(f"已匹配: {snapshot['matched_count']}")
    click.echo(f"待确认: {snapshot['pending_count']}")
    click.echo(f"异常: {snapshot['exception_count']}")
    click.echo(f"已撤销: {snapshot['revoked_count']}")
    click.echo(f"生成时间: {snapshot['created_at']}")


@review_snapshot.command("list")
@click.option("--limit", type=int, default=50, help="显示数量")
@click.pass_context
def snapshot_list(ctx, limit):
    """列出复核快照"""
    reviewer = ctx.obj["reviewer"]
    snapshots = reviewer.list_snapshots(limit)

    if not snapshots:
        click.echo("暂无快照")
        return

    headers = ["编号", "类型", "总匹配", "已匹配", "待确认", "异常", "已撤销", "操作者", "生成时间"]
    rows = []
    for s in snapshots:
        rows.append([
            s["snapshot_no"],
            s["type_label"],
            s["total_matches"],
            s["matched_count"],
            s["pending_count"],
            s["exception_count"],
            s["revoked_count"],
            s["operator"] or "-",
            s["created_at"],
        ])
    print_table(headers, rows)


@review_snapshot.command("show")
@click.option("--snapshot-no", default=None, help="快照编号")
@click.option("--snapshot-id", type=int, default=None, help="快照ID")
@click.pass_context
def snapshot_show(ctx, snapshot_no, snapshot_id):
    """显示快照详情"""
    if not snapshot_no and not snapshot_id:
        click.echo(click.style("请指定 --snapshot-no 或 --snapshot-id", fg="red"), err=True)
        sys.exit(1)

    reviewer = ctx.obj["reviewer"]
    snapshot = reviewer.get_snapshot(snapshot_no=snapshot_no, snapshot_id=snapshot_id)

    if not snapshot:
        click.echo(click.style("快照不存在", fg="red"), err=True)
        sys.exit(1)

    click.echo(f"=== 快照详情 ===")
    click.echo(f"快照编号: {snapshot['snapshot_no']}")
    click.echo(f"类型: {snapshot['type_label']}")
    click.echo(f"描述: {snapshot['description'] or '-'}")
    click.echo(f"操作者: {snapshot['operator'] or '-'}")
    click.echo(f"生成时间: {snapshot['created_at']}")
    click.echo()
    click.echo(f"总匹配数: {snapshot['total_matches']}")
    click.echo(f"已匹配: {snapshot['matched_count']}")
    click.echo(f"待确认: {snapshot['pending_count']}")
    click.echo(f"异常: {snapshot['exception_count']}")
    click.echo(f"已撤销: {snapshot['revoked_count']}")
    click.echo(f"发票总金额: {snapshot['total_invoice_amount']:.2f}")
    click.echo(f"收款总金额: {snapshot['total_payment_amount']:.2f}")
    click.echo(f"已匹配金额: {snapshot['matched_amount']:.2f}")

    if snapshot["items"]:
        click.echo(f"\n=== 匹配明细 ({len(snapshot['items'])} 条) ===")
        headers = ["匹配编号", "类型", "状态", "发票号", "金额", "收款号", "金额", "操作者"]
        rows = []
        for item in snapshot["items"]:
            rows.append([
                item["match_no"],
                MATCH_TYPE_LABELS.get(item["match_type"], item["match_type"]),
                STATUS_LABELS.get(item["status"], item["status"]),
                item["invoice_no"],
                f"{item['inv_amount']:.2f}",
                item["payment_no"],
                f"{item['pay_amount']:.2f}",
                item["operator"] or "-",
            ])
        print_table(headers, rows[:20])
        if len(snapshot["items"]) > 20:
            click.echo(f"... 还有 {len(snapshot['items']) - 20} 条")


@review.command("replay")
@click.option("--snapshot-no", default=None, help="快照编号")
@click.option("--snapshot-id", type=int, default=None, help="快照ID")
@click.pass_context
def review_replay(ctx, snapshot_no, snapshot_id):
    """回放校验：对比当前状态与快照"""
    if not snapshot_no and not snapshot_id:
        click.echo(click.style("请指定 --snapshot-no 或 --snapshot-id", fg="red"), err=True)
        sys.exit(1)

    reviewer = ctx.obj["reviewer"]

    try:
        result = reviewer.replay_verify(snapshot_no=snapshot_no, snapshot_id=snapshot_id)
    except ValueError as e:
        click.echo(click.style(f"[!!] {e}", fg="red"), err=True)
        sys.exit(1)

    click.echo(f"=== 回放校验结果 ===")
    click.echo(f"快照编号: {result['snapshot_no']}")
    click.echo(f"快照类型: {result['snapshot_type_label']}")
    click.echo(f"快照生成时间: {result['snapshot_created_at']}")
    click.echo()
    click.echo(f"快照记录数: {result['snapshot_total']}")
    click.echo(f"当前记录数: {result['current_total']}")
    click.echo()
    click.echo(f"一致: {result['matched']}")
    click.echo(f"状态变更: {result['status_changed']}")
    click.echo(f"新增: {result['new_in_current']}")
    click.echo(f"缺失: {result['missing_in_current']}")
    click.echo()

    if result["is_consistent"]:
        click.echo(click.style("[OK] 校验通过：当前状态与快照完全一致", fg="green"))
    else:
        click.echo(click.style(f"[!!] 校验未通过：共 {len(result['differences'])} 处差异", fg="red", bold=True))

        if result["differences"]:
            click.echo("\n=== 差异明细 ===")
            for diff in result["differences"][:20]:
                if diff["type"] == "status_changed":
                    click.echo(
                        f"  [{diff['match_no']}] 状态变更: "
                        f"{diff['snapshot_status_label']} -> {diff['current_status_label']} "
                        f"(发票: {diff['invoice_no']}, 收款: {diff['payment_no']})"
                    )
                elif diff["type"] == "new_in_current":
                    click.echo(
                        f"  [{diff['match_no']}] 新增记录: "
                        f"{diff['current_status_label']} "
                        f"(发票: {diff['invoice_no']}, 收款: {diff['payment_no']}, "
                        f"操作人: {diff['current_operator'] or '-'})"
                    )
                elif diff["type"] == "missing_in_current":
                    click.echo(
                        f"  [{diff['match_no']}] 记录缺失: "
                        f"快照中为 {diff['snapshot_status_label']} "
                        f"(发票: {diff['invoice_no']}, 收款: {diff['payment_no']}, "
                        f"操作人: {diff['snapshot_operator'] or '-'})"
                    )

            if len(result["differences"]) > 20:
                click.echo(f"... 还有 {len(result['differences']) - 20} 处差异")


@review.command("conflicts")
@click.option("--invoice-no", default=None, help="指定发票号")
@click.pass_context
def review_conflicts(ctx, invoice_no):
    """检测同一发票被不同操作者处理的冲突"""
    reviewer = ctx.obj["reviewer"]
    conflicts = reviewer.check_conflicts(invoice_no=invoice_no)

    if not conflicts:
        click.echo(click.style("[OK] 未检测到冲突", fg="green"))
        return

    click.echo(click.style(f"[!!] 检测到 {len(conflicts)} 个冲突", fg="red", bold=True))
    click.echo()

    for i, conflict in enumerate(conflicts, 1):
        click.echo(f"=== 冲突 {i}: 发票 {conflict['invoice_no']} ===")
        click.echo(f"匹配记录数: {conflict['match_count']}")
        click.echo(f"涉及操作者: {', '.join(conflict['operators']) if conflict['operators'] else '-'}")
        click.echo()

        headers = ["匹配编号", "类型", "状态", "收款号", "金额", "操作人", "备注"]
        rows = []
        for m in conflict["matches"]:
            rows.append([
                m["match_no"],
                m["match_type_label"],
                m["status_label"],
                m["payment_no"],
                f"{m['inv_amount']:.2f}",
                m["operator"] or "-",
                (m.get("operator_remark") or "")[:30],
            ])
        print_table(headers, rows)
        click.echo()


@cli.group()
def lock():
    """工单锁定管理"""
    pass


@lock.command("list")
@click.option("--all", "show_all", is_flag=True, help="显示所有锁（包括已过期）")
@click.option("--owner", default=None, help="按持有人过滤")
@click.option("--operator", default=None, help="当前操作者（用于权限判断的备选）")
@click.pass_context
def lock_list(ctx, show_all, owner, operator):
    """列出锁定记录"""
    workflow = ctx.obj["workflow"]
    current_operator = operator or get_current_user()

    if owner:
        locks = workflow.get_locks_by_owner(owner)
    else:
        locks = workflow.list_all_locks(include_expired=show_all)

    if not locks:
        click.echo("暂无锁定记录")
        return

    headers = ["锁ID", "匹配ID", "匹配编号", "匹配状态", "发票号", "收款号", "持有人", "锁定原因", "锁定时间", "到期时间", "是否过期"]
    rows = []
    for lock in locks:
        is_expired = "否"
        if lock.get("lock_expires_at"):
            try:
                from datetime import datetime
                expire_time = datetime.strptime(lock["lock_expires_at"], "%Y-%m-%d %H:%M:%S")
                if expire_time <= datetime.now():
                    is_expired = "是"
            except (ValueError, TypeError):
                pass
        rows.append([
            lock["id"],
            lock["match_id"],
            lock["match_no"],
            STATUS_LABELS.get(lock.get("match_status"), lock.get("match_status", "-")),
            lock["invoice_no"],
            lock["payment_no"],
            lock["lock_owner"],
            (lock.get("lock_reason") or "")[:20],
            lock.get("locked_at", "-"),
            lock.get("lock_expires_at", "-"),
            is_expired,
        ])
    print_table(headers, rows)
    click.echo(f"\n共 {len(locks)} 条锁定记录")


@lock.command("acquire")
@click.argument("match_id", type=int)
@click.option("--operator", default=None, help="操作者")
@click.option("--reason", default=None, help="锁定原因")
@click.pass_context
def lock_acquire(ctx, match_id, operator, reason):
    """锁定匹配记录"""
    workflow = ctx.obj["workflow"]
    operator = operator or get_current_user()

    try:
        result = workflow.acquire_lock(match_id, operator, reason)
        if result.get("skipped"):
            click.echo(click.style(result["message"], fg="yellow"))
        elif result.get("already_locked"):
            click.echo(click.style(result["message"], fg="yellow"))
        else:
            click.echo(click.style(f"[OK] {result['message']}", fg="green"))
            click.echo(f"匹配编号: {result['match_no']}")
            click.echo(f"持有人: {result['owner']}")
            if result.get("expires_at"):
                click.echo(f"到期时间: {result['expires_at']}")
    except ValueError as e:
        click.echo(click.style(f"[!!] {e}", fg="red"), err=True)
        sys.exit(1)


@lock.command("release")
@click.argument("match_id", type=int)
@click.option("--operator", default=None, help="操作者")
@click.option("--reason", default=None, help="解锁原因")
@click.pass_context
def lock_release(ctx, match_id, operator, reason):
    """解锁匹配记录"""
    workflow = ctx.obj["workflow"]
    operator = operator or get_current_user()

    try:
        result = workflow.release_lock(match_id, operator, reason)
        if result.get("skipped"):
            click.echo(click.style(result["message"], fg="yellow"))
        elif result["success"]:
            click.echo(click.style(f"[OK] {result['message']}", fg="green"))
            click.echo(f"匹配编号: {result['match_no']}")
            click.echo(f"原持有人: {result['old_owner']}")
        else:
            click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
            sys.exit(1)
    except ValueError as e:
        click.echo(click.style(f"[!!] {e}", fg="red"), err=True)
        sys.exit(1)


@lock.command("transfer")
@click.argument("match_id", type=int)
@click.argument("to_user")
@click.option("--operator", default=None, help="当前操作者")
@click.option("--reason", default=None, help="转交原因")
@click.pass_context
def lock_transfer(ctx, match_id, to_user, operator, reason):
    """转交匹配记录"""
    workflow = ctx.obj["workflow"]
    operator = operator or get_current_user()

    try:
        result = workflow.transfer_lock(match_id, operator, to_user, reason)
        if result.get("skipped"):
            click.echo(click.style(result["message"], fg="yellow"))
        else:
            click.echo(click.style(f"[OK] {result['message']}", fg="green"))
            click.echo(f"匹配编号: {result['match_no']}")
            click.echo(f"原持有人: {result['old_owner']}")
            click.echo(f"新持有人: {result['new_owner']}")
    except ValueError as e:
        click.echo(click.style(f"[!!] {e}", fg="red"), err=True)
        sys.exit(1)


@lock.command("takeover")
@click.argument("match_id", type=int)
@click.option("--operator", default=None, help="操作者")
@click.option("--reason", default=None, help="接管原因")
@click.pass_context
def lock_takeover(ctx, match_id, operator, reason):
    """接管匹配记录"""
    workflow = ctx.obj["workflow"]
    operator = operator or get_current_user()

    try:
        result = workflow.takeover_lock(match_id, operator, reason)
        if result.get("skipped"):
            click.echo(click.style(result["message"], fg="yellow"))
        elif result.get("already_locked"):
            click.echo(click.style(result["message"], fg="yellow"))
        else:
            click.echo(click.style(f"[OK] {result['message']}", fg="green"))
            click.echo(f"匹配编号: {result['match_no']}")
            if result.get("old_owner"):
                click.echo(f"原持有人: {result['old_owner']}")
            click.echo(f"新持有人: {result['new_owner']}")
            if result.get("was_expired"):
                click.echo(click.style("原锁已过期", fg="yellow"))
    except ValueError as e:
        click.echo(click.style(f"[!!] {e}", fg="red"), err=True)
        sys.exit(1)


@lock.command("force-unlock")
@click.argument("match_id", type=int)
@click.option("--operator", default=None, help="操作者（需管理员权限）")
@click.option("--reason", default=None, help="强制解锁原因")
@click.pass_context
def lock_force_unlock(ctx, match_id, operator, reason):
    """强制解锁（仅管理员）"""
    workflow = ctx.obj["workflow"]
    operator = operator or get_current_user()

    try:
        result = workflow.force_unlock(match_id, operator, reason)
        if result["success"]:
            click.echo(click.style(f"[OK] {result['message']}", fg="green"))
            click.echo(f"匹配编号: {result['match_no']}")
            click.echo(f"原持有人: {result['old_owner']}")
        else:
            click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
            sys.exit(1)
    except ValueError as e:
        click.echo(click.style(f"[!!] {e}", fg="red"), err=True)
        sys.exit(1)


@lock.command("batch-unlock")
@click.option("--operator", default=None, help="操作者（需管理员权限）")
@click.option("--reason", default=None, help="批量解锁原因")
@click.option("--owner", default=None, help="仅解锁指定持有人的锁")
@click.pass_context
def lock_batch_unlock(ctx, operator, reason, owner):
    """批量解锁（仅管理员）"""
    workflow = ctx.obj["workflow"]
    operator = operator or get_current_user()

    try:
        result = workflow.batch_force_unlock(operator, reason, owner)
        click.echo(click.style(f"[OK] {result['message']}", fg="green"))
    except ValueError as e:
        click.echo(click.style(f"[!!] {e}", fg="red"), err=True)
        sys.exit(1)


@lock.command("history")
@click.option("--match-id", type=int, default=None, help="匹配ID")
@click.option("--match-no", default=None, help="匹配编号")
@click.option("--operator", default=None, help="按操作人过滤")
@click.option("--limit", type=int, default=50, help="显示条数")
@click.pass_context
def lock_history(ctx, match_id, match_no, operator, limit):
    """查看锁操作历史"""
    workflow = ctx.obj["workflow"]
    db = ctx.obj["db"]

    if match_no and not match_id:
        match = db.get_match_by_no(match_no)
        if match:
            match_id = match["id"]

    history = workflow.get_lock_history(match_id=match_id, operator=operator, limit=limit)

    if not history:
        click.echo("暂无锁操作历史")
        return

    headers = ["ID", "匹配ID", "操作类型", "操作人", "原持有人", "新持有人", "原因", "操作时间"]
    rows = []
    for h in history:
        rows.append([
            h["id"],
            h["match_id"],
            h.get("action_label", h["action"]),
            h["operator"],
            h.get("old_owner") or "-",
            h.get("new_owner") or "-",
            (h.get("reason") or "")[:30],
            h["created_at"],
        ])
    print_table(headers, rows)


@cli.group()
def user():
    """用户与角色管理"""
    pass


@user.command("list")
@click.option("--operator", default=None, help="当前操作者（用于权限判断）")
@click.pass_context
def user_list(ctx, operator):
    """列出所有用户"""
    workflow = ctx.obj["workflow"]
    current_op = operator or get_current_user()

    if not workflow.is_admin(current_op):
        click.echo(click.style("[!!] 只有管理员可以查看用户列表", fg="red"), err=True)
        sys.exit(1)

    users = workflow.list_users()
    if not users:
        click.echo("暂无用户")
        return

    headers = ["ID", "用户名", "角色", "状态", "创建时间"]
    rows = []
    for u in users:
        rows.append([
            u["id"],
            u["username"],
            u.get("role_label", u.get("role", "-")),
            u.get("status", "-"),
            u.get("created_at", "-"),
        ])
    print_table(headers, rows)


@user.command("set-role")
@click.argument("username")
@click.argument("role", type=click.Choice(["reviewer", "admin"]))
@click.option("--operator", default=None, help="当前操作者（需管理员权限）")
@click.pass_context
def user_set_role(ctx, username, role, operator):
    """设置用户角色（仅管理员）"""
    workflow = ctx.obj["workflow"]
    operator = operator or get_current_user()

    try:
        result = workflow.update_user_role(operator, username, role)
        click.echo(click.style(f"[OK] {result['message']}", fg="green"))
    except ValueError as e:
        click.echo(click.style(f"[!!] {e}", fg="red"), err=True)
        sys.exit(1)


@user.command("info")
@click.option("--username", default=None, help="查询的用户名，默认当前用户")
@click.pass_context
def user_info(ctx, username):
    """查看用户信息和锁统计"""
    workflow = ctx.obj["workflow"]
    username = username or get_current_user()

    role = workflow.get_user_role(username)
    role_label = USER_ROLE_LABELS.get(role, role)
    summary = workflow.get_user_locks_summary(username)

    click.echo(f"=== 用户信息 ===")
    click.echo(f"用户名: {username}")
    click.echo(f"角色: {role_label}")
    click.echo()
    click.echo(f"=== 锁统计 ===")
    click.echo(f"总锁定数: {summary['total_locks']}")
    click.echo(f"有效锁: {summary['active_locks']}")
    click.echo(f"已过期: {summary['expired_locks']}")


if __name__ == "__main__":
    cli()
