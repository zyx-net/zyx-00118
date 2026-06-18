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
from invoice_reconciler.core.batch_workbench import (
    BatchWorkbench,
    CONFLICT_TYPE_LABELS,
)
from invoice_reconciler.core.change_tracker import (
    ChangeTracker,
    CHANGE_TYPE_LABELS,
    IMPACT_TYPE_LABELS,
    PROCESSING_STATUS_LABELS,
)
from invoice_reconciler.core.handover import (
    BatchHandover,
    HandoverPlaybackCenter,
    HANDOVER_STATUS_ACTIVE,
    HANDOVER_STATUS_DISCARDED,
    HANDOVER_STATUS_RESTORED,
    HANDOVER_EVENT_CREATE,
    HANDOVER_EVENT_RESTORE,
    HANDOVER_EVENT_UNDO,
    HANDOVER_EVENT_DISCARD,
    HANDOVER_EVENT_SAVE_COPY,
    HANDOVER_EVENT_CLEANUP,
    HANDOVER_EVENT_EXPORT_PACKAGE,
    HANDOVER_EVENT_RESUME_EXPORT,
    HANDOVER_EVENT_VIEW_SUMMARY,
    HANDOVER_EVENT_LABELS,
    HANDOVER_CONFLICT_LABELS,
    HANDOVER_ACTION_LABELS,
)
from invoice_reconciler.core.receipt_cabinet import (
    ExportReceiptCabinet,
    RECEIPT_STATUS_ACTIVE,
    RECEIPT_STATUS_RESUMED,
    RECEIPT_STATUS_ABANDONED,
    RECEIPT_STATUS_SUPERSEDED,
    RECEIPT_STATUS_LABELS,
    RECEIPT_EVENT_LABELS,
    INTERCEPT_LABELS,
    HANDLE_LABELS,
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
        restore_result = workflow.restore_locks_on_startup()
        if restore_result.get("restored") and restore_result.get("total_locks", 0) > 0:
            click.echo(click.style(
                f"[锁状态恢复] 共 {restore_result['total_locks']} 条锁，"
                f"其中已过期 {restore_result['expired_locks']} 条。"
                f"配置超时: {restore_result.get('config_timeout', 'N/A')} 秒",
                fg="yellow"
            ))
        workbench = BatchWorkbench(config, db)
        handover = BatchHandover(config, db)
        playback_center = HandoverPlaybackCenter(config, db)
        active_packages = handover.list_packages()
        if active_packages:
            latest_pkg = active_packages[0]
            pkg_id = latest_pkg.get("package_id", "?")
            pkg_desc = latest_pkg.get("description") or ""
            pkg_operator = latest_pkg.get("operator") or "-"
            pkg_created = latest_pkg.get("created_at", "-")
            pkg_status = latest_pkg.get("status", "-")
            status_labels = {
                HANDOVER_STATUS_ACTIVE: "可用",
                HANDOVER_STATUS_RESTORED: "已恢复",
                HANDOVER_STATUS_DISCARDED: "已废弃",
            }
            pkg_status_label = status_labels.get(pkg_status, pkg_status)
            click.echo(click.style(
                f"[交接包] 检测到可恢复的交接包: {pkg_id} "
                f"(状态: {pkg_status_label}, 操作人: {pkg_operator}, "
                f"时间: {pkg_created})"
                + (f" - {pkg_desc}" if pkg_desc else ""),
                fg="magenta", bold=True
            ))
            click.echo(click.style(
                f"  预览: handover preview {pkg_id}  |  "
                f"恢复: handover restore {pkg_id}  |  "
                f"废弃: handover discard {pkg_id}",
                fg="magenta"
            ))
        restore_workbench = workbench.restore_workbench_state()
        if restore_workbench.get("restored") and restore_workbench.get("last_batch"):
            lb = restore_workbench["last_batch"]
            click.echo(click.style(
                f"[会话恢复] 上次打开批次: #{lb['batch_id']} - {lb['file_name']} ({lb['file_type']})",
                fg="yellow"
            ))
            if restore_workbench.get("filters"):
                filters = restore_workbench["filters"]
                filter_parts = []
                if filters.get("operator"):
                    filter_parts.append(f"处理人: {filters['operator']}")
                if filters.get("status"):
                    filter_parts.append(f"状态: {STATUS_LABELS.get(filters['status'], filters['status'])}")
                if filter_parts:
                    click.echo(click.style(
                        f"[会话恢复] 筛选条件: {', '.join(filter_parts)}",
                        fg="yellow"
                    ))

        last_view = workbench.get_last_change_view_context()
        if last_view:
            parts = []
            if last_view.get("batch_id"):
                parts.append(f"批次 #{last_view['batch_id']}")
            else:
                parts.append("全部批次")
            if last_view.get("change_type"):
                ct_label = CHANGE_TYPE_LABELS.get(last_view['change_type'], last_view['change_type'])
                parts.append(f"变更类型: {ct_label}")
            if last_view.get("impact_type"):
                it_label = IMPACT_TYPE_LABELS.get(last_view['impact_type'], last_view['impact_type'])
                parts.append(f"影响类型: {it_label}")
            if last_view.get("processing_status"):
                ps_label = PROCESSING_STATUS_LABELS.get(last_view['processing_status'],
                                                        last_view['processing_status'])
                parts.append(f"处理状态: {ps_label}")
            if last_view.get("record_no"):
                parts.append(f"记录: {last_view['record_no']}")
            if last_view.get("affect_filter"):
                parts.append(f"影响筛选: {last_view['affect_filter']}")
            if last_view.get("with_conflicts_only"):
                parts.append("仅含冲突")
            if parts:
                click.echo(click.style(
                    f"[会话恢复] 上次查看变更: {', '.join(parts)}",
                    fg="yellow"
                ))

        ctx.obj = {
            "config": config,
            "db": db,
            "workflow": workflow,
            "importer": CSVImporter(config, db),
            "matcher": MatchEngine(config, db, workflow),
            "revoker": Revoker(db, config, workflow),
            "exporter": ReportExporter(config, db),
            "reviewer": ReviewSnapshot(db),
            "workbench": workbench,
            "change_tracker": ChangeTracker(config, db),
            "handover": handover,
            "playback_center": playback_center,
            "receipt_cabinet": ExportReceiptCabinet(config, db),
        }

        receipt_cabinet = ctx.obj["receipt_cabinet"]
        latest_receipt = receipt_cabinet.find_latest_receipt()
        if latest_receipt:
            rid = latest_receipt.get("receipt_id", "?")
            r_status = latest_receipt.get("status", "-")
            r_op = latest_receipt.get("operator", "-")
            r_target = latest_receipt.get("target_file", "-")
            r_format = latest_receipt.get("export_format", "-")
            r_hits = latest_receipt.get("hit_count", 0)
            r_time = latest_receipt.get("exported_at", "-")
            r_status_label = RECEIPT_STATUS_LABELS.get(r_status, r_status)

            check = receipt_cabinet.check_interceptions(rid)
            has_issues = check.get("interception_count", 0) > 0

            click.echo(click.style(
                f"[导出回执] 检测到上次导出回执: {rid} "
                f"(状态: {r_status_label}, 操作人: {r_op}, "
                f"记录数: {r_hits}, 格式: {r_format})",
                fg="cyan", bold=True
            ))
            click.echo(click.style(
                f"  导出时间: {r_time} | 目标: {r_target}",
                fg="cyan"
            ))
            if has_issues:
                click.echo(click.style(
                    f"  ⚠ 检测到 {check['interception_count']} 个拦截项:",
                    fg="red", bold=True
                ))
                for i in check["interceptions"]:
                    click.echo(click.style(
                        f"    • [{i['severity']}] {i['label']}: {i['detail']}",
                        fg="red" if i['severity'] == 'critical' else "yellow"
                    ))
                click.echo(click.style(
                    f"  处理: receipt show {rid} | receipt compare {rid} | receipt handle {rid}",
                    fg="cyan"
                ))
            else:
                click.echo(click.style(
                    f"  续导: receipt resume {rid} | 对比: receipt compare {rid}",
                    fg="cyan"
                ))
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

        if result.get("conflicts"):
            conflicts = result["conflicts"]
            click.echo()
            click.echo(click.style(f"⚠ 检测到 {len(conflicts)} 个差异或冲突:", fg="yellow", bold=True))
            for c in conflicts:
                c_type = CONFLICT_TYPE_LABELS.get(c["conflict_type"], c["conflict_type"])
                record_type = "发票" if c["record_type"] == "invoice" else "收款"
                click.echo(f"  • [{c_type}] {record_type} {c['record_no']}: {c['conflict_reason']}")
            click.echo(click.style("详细冲突信息已记录到数据库，可通过 `batch conflicts` 命令查看", fg="yellow"))

        if result.get("change_count", 0) > 0:
            click.echo()
            click.echo(click.style(f"📋 追踪到 {result['change_count']} 条变更记录:", fg="cyan", bold=True))
            for ct, count in result.get("changes_by_type", {}).items():
                ct_label = CHANGE_TYPE_LABELS.get(ct, ct)
                click.echo(f"  • {ct_label}: {count} 条")

            if result.get("impact_summary"):
                click.echo()
                click.echo(click.style("🔍 影响分析:", fg="magenta", bold=True))
                for it, count in result["impact_summary"].items():
                    if count > 0:
                        it_label = IMPACT_TYPE_LABELS.get(it, it)
                        click.echo(f"  • {it_label}: {count} 条")

            click.echo(click.style("详细变更日志已记录，可通过 `batch changes` 命令查看", fg="cyan"))
            click.echo(click.style("导出变更日志: `batch export-changes <batch_id>`", fg="cyan"))
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

        if result.get("conflicts"):
            conflicts = result["conflicts"]
            click.echo()
            click.echo(click.style(f"⚠ 检测到 {len(conflicts)} 个差异或冲突:", fg="yellow", bold=True))
            for c in conflicts:
                c_type = CONFLICT_TYPE_LABELS.get(c["conflict_type"], c["conflict_type"])
                record_type = "发票" if c["record_type"] == "invoice" else "收款"
                click.echo(f"  • [{c_type}] {record_type} {c['record_no']}: {c['conflict_reason']}")
            click.echo(click.style("详细冲突信息已记录到数据库，可通过 `batch conflicts` 命令查看", fg="yellow"))

        if result.get("change_count", 0) > 0:
            click.echo()
            click.echo(click.style(f"📋 追踪到 {result['change_count']} 条变更记录:", fg="cyan", bold=True))
            for ct, count in result.get("changes_by_type", {}).items():
                ct_label = CHANGE_TYPE_LABELS.get(ct, ct)
                click.echo(f"  • {ct_label}: {count} 条")

            if result.get("impact_summary"):
                click.echo()
                click.echo(click.style("🔍 影响分析:", fg="magenta", bold=True))
                for it, count in result["impact_summary"].items():
                    if count > 0:
                        it_label = IMPACT_TYPE_LABELS.get(it, it)
                        click.echo(f"  • {it_label}: {count} 条")

            click.echo(click.style("详细变更日志已记录，可通过 `batch changes` 命令查看", fg="cyan"))
            click.echo(click.style("导出变更日志: `batch export-changes <batch_id>`", fg="cyan"))
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


@cli.group()
def batch():
    """批次工作台管理"""
    pass


@batch.command("summary")
@click.option("--batch-id", type=int, default=None, help="指定批次ID，默认显示所有批次")
@click.option("--operator", default=None, help="按处理人过滤")
@click.option("--status", default=None,
              type=click.Choice(["pending", "matched", "exception", "revoked"]),
              help="按状态过滤")
@click.pass_context
def batch_summary(ctx, batch_id, operator, status):
    """批次工作台摘要视图"""
    workbench = ctx.obj["workbench"]
    db = ctx.obj["db"]

    if batch_id:
        workbench.save_last_selected_batch(batch_id, get_current_user())

    result = workbench.get_batch_workbench_summary(batch_id=batch_id)

    if not result["success"]:
        click.echo(click.style(result["message"], fg="yellow"))
        return

    click.echo(click.style(f"=== 批次工作台摘要 ({result['total_batches']} 个批次) ===", fg="cyan", bold=True))

    for batch in result["batches"]:
        click.echo()
        click.echo(click.style(f"━━━ 批次 #{batch['batch_id']}: {batch['file_name']} ━━━", fg="white", bold=True))
        click.echo(f"  类型: {batch['file_type']} | 操作人: {batch['operator']} | 导入时间: {batch['imported_at']}")
        click.echo(f"  导入: 总计 {batch['total_rows']} 行 | 成功 {batch['success_rows']} 行 | 失败 {batch['failed_rows']} 行")
        click.echo()

        click.echo(click.style("  匹配进度:", fg="cyan"))
        click.echo(f"    已确认: {batch['confirmed_matches']} | 待确认: {batch['pending_matches']} | "
                   f"异常: {batch['exception_matches']} | 已撤销: {batch['revoked_matches']}")
        click.echo(f"    已匹配发票: {batch['matched_invoices']} / 未匹配: {batch['unmatched_invoices']}")
        click.echo(f"    已匹配收款: {batch['matched_payments']} / 未匹配: {batch['unmatched_payments']}")
        click.echo(f"    冲突数量: {batch['conflict_count']}")
        click.echo()

        progress_bar = ""
        progress = batch["progress_percent"]
        filled = int(progress / 5)
        progress_bar = "█" * filled + "░" * (20 - filled)
        click.echo(f"  总进度: {progress_bar} {progress:.1f}%")

        if batch["has_unfinished"]:
            click.echo()
            click.echo(click.style("  ⚠ 待处理项:", fg="yellow", bold=True))
            for item in batch["unfinished_items"]:
                click.echo(f"    • {item}")

        if batch["conflict_count"] > 0:
            click.echo()
            click.echo(click.style("  🔴 冲突明细:", fg="red", bold=True))
            for c in batch["conflicts"]:
                conflict_type = CONFLICT_TYPE_LABELS.get(c["conflict_type"], c["conflict_type"])
                click.echo(f"    [{c['detected_at']}] {conflict_type}: {c['conflict_reason']}")

        if batch_id and batch["total_tasks"] > 0:
            filters = workbench.get_filters()
            matches = workbench.get_batch_matches(
                batch_id,
                status=filters.get("status") or status,
                operator=filters.get("operator") or operator
            )
            if matches:
                click.echo()
                click.echo(click.style(f"  匹配明细 ({len(matches)} 条):", fg="cyan"))
                headers = ["ID", "匹配编号", "类型", "状态", "发票号", "金额", "收款号", "金额", "处理人"]
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
                        m.get("operator") or "-",
                    ])
                print_table(headers, rows[:15])
                if len(matches) > 15:
                    click.echo(f"  ... 还有 {len(matches) - 15} 条")


@batch.command("select")
@click.argument("batch_id", type=int)
@click.option("--operator", default=None, help="当前操作者")
@click.pass_context
def batch_select(ctx, batch_id, operator):
    """选择当前处理的批次（程序重启后自动恢复）"""
    workbench = ctx.obj["workbench"]
    db = ctx.obj["db"]

    batch_info = db.get_batch(batch_id)
    if not batch_info:
        click.echo(click.style(f"批次不存在: {batch_id}", fg="red"), err=True)
        sys.exit(1)

    operator = operator or get_current_user()
    workbench.save_last_selected_batch(batch_id, operator)

    file_type = "发票" if batch_info["file_type"] == "invoice" else "收款"
    click.echo(click.style(f"[OK] 已选择批次 #{batch_id}: {batch_info['file_name']} ({file_type})", fg="green"))
    click.echo("程序重启后将自动恢复到此批次上下文")


@batch.command("filter")
@click.option("--operator", default=None, help="按处理人过滤")
@click.option("--status", default=None,
              type=click.Choice(["pending", "matched", "exception", "revoked"]),
              help="按状态过滤")
@click.option("--clear", is_flag=True, help="清除所有筛选条件")
@click.pass_context
def batch_filter(ctx, operator, status, clear):
    """设置筛选条件（程序重启后自动恢复）"""
    workbench = ctx.obj["workbench"]

    if clear:
        workbench.save_filters(operator=None, status=None)
        click.echo(click.style("[OK] 已清除所有筛选条件", fg="green"))
        return

    if operator is None and status is None:
        filters = workbench.get_filters()
        if filters:
            click.echo("=== 当前筛选条件 ===")
            if filters.get("operator"):
                click.echo(f"  处理人: {filters['operator']}")
            if filters.get("status"):
                click.echo(f"  状态: {STATUS_LABELS.get(filters['status'], filters['status'])}")
            if not filters:
                click.echo("  无筛选条件")
        else:
            click.echo("暂无筛选条件")
        return

    workbench.save_filters(operator=operator, status=status)

    parts = []
    if operator:
        parts.append(f"处理人: {operator}")
    if status:
        parts.append(f"状态: {STATUS_LABELS.get(status, status)}")

    click.echo(click.style(f"[OK] 筛选条件已设置: {', '.join(parts)}", fg="green"))
    click.echo("程序重启后将自动恢复这些筛选条件")


@batch.command("reminders")
@click.option("--batch-id", type=int, default=None, help="指定批次ID，默认所有批次")
@click.pass_context
def batch_reminders(ctx, batch_id):
    """未完成项提醒"""
    workbench = ctx.obj["workbench"]

    result = workbench.get_unfinished_reminder(batch_id=batch_id)

    if not result["success"]:
        click.echo(click.style(result["message"], fg="yellow"))
        return

    if result["total_unfinished"] == 0:
        click.echo(click.style("[OK] 所有批次处理完成，暂无待办事项", fg="green"))
        return

    click.echo(click.style(f"⚠ 共有 {result['total_unfinished']} 个待处理项，涉及 {result['batch_count']} 个批次", fg="yellow", bold=True))
    click.echo()

    for reminder in result["reminders"]:
        click.echo(click.style(f"━━━ 批次 #{reminder['batch_id']}: {reminder['file_name']} ━━━", fg="white"))
        click.echo(f"  进度: {reminder['progress_percent']:.1f}%")
        for item in reminder["unfinished_items"]:
            click.echo(f"  • {item}")
        click.echo()


@batch.command("export-progress")
@click.argument("batch_id", type=int)
@click.option("--operator", default=None, help="当前操作者")
@click.option("--format", "export_format", type=click.Choice(["xlsx", "csv", "json"]),
              default=None, help="导出格式")
@click.pass_context
def batch_export_progress(ctx, batch_id, operator, export_format):
    """一键导出当前批次进度（含冲突和差异）"""
    exporter = ctx.obj["exporter"]
    workbench = ctx.obj["workbench"]
    operator = operator or get_current_user()

    workbench.save_last_selected_batch(batch_id, operator)
    workbench.save_export_context(
        batch_id=batch_id,
        export_type="batch_progress",
        format=export_format or workbench.config.export_format,
        operator=operator,
    )

    click.echo(f"正在导出批次 #{batch_id} 进度...")
    result = exporter.export_batch_progress(batch_id, operator, format=export_format)

    if not result["success"]:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style(f"[OK] 批次进度已导出: {result['file_path']}", fg="green"))
    click.echo(f"格式: {result['format']}")
    click.echo(f"生成时间: {result['generated_at']}")
    click.echo()
    click.echo("报告摘要:")
    summary = result["summary"]
    click.echo(f"  总匹配数: {summary['total_matches']}")
    click.echo(f"  待确认: {summary['pending_count']}")
    click.echo(f"  已确认: {summary['confirmed_count']}")
    click.echo(f"  异常: {summary['exception_count']}")
    click.echo(f"  已撤销: {summary['revoked_count']}")
    click.echo(f"  冲突数: {summary['conflict_count']}")


@batch.command("conflicts")
@click.option("--batch-id", type=int, default=None, help="指定批次ID")
@click.option("--conflict-type", default=None,
              type=click.Choice(["new_record", "status_change", "duplicate_process", "amount_change"]),
              help="按冲突类型过滤")
@click.pass_context
def batch_conflicts(ctx, batch_id, conflict_type):
    """查看批次冲突明细"""
    db = ctx.obj["db"]

    conflicts = db.get_batch_conflicts(batch_id=batch_id, conflict_type=conflict_type)

    if not conflicts:
        click.echo(click.style("[OK] 未检测到冲突", fg="green"))
        return

    click.echo(click.style(f"[!!] 检测到 {len(conflicts)} 个冲突", fg="red", bold=True))
    click.echo()

    headers = ["ID", "批次ID", "文件", "冲突类型", "记录类型", "记录编号", "冲突原因", "检测时间"]
    rows = []
    for c in conflicts:
        rows.append([
            c["id"],
            c["batch_id"],
            c["file_name"],
            CONFLICT_TYPE_LABELS.get(c["conflict_type"], c["conflict_type"]),
            "发票" if c["record_type"] == "invoice" else "收款",
            c["record_no"],
            (c["conflict_reason"] or "")[:50],
            c["detected_at"],
        ])
    print_table(headers, rows)

    click.echo()
    click.echo("冲突说明:")
    click.echo("  • 新增记录: 本次导入新增的记录，之前批次中不存在")
    click.echo("  • 状态冲突: 同一记录在不同批次中的状态不一致")
    click.echo("  • 重复处理: 同一记录被不同操作者处理")
    click.echo("  • 金额变更: 同一记录在不同批次中的金额不一致")


@batch.command("restore")
@click.pass_context
def batch_restore(ctx):
    """手动恢复上次会话状态"""
    workbench = ctx.obj["workbench"]

    result = workbench.restore_workbench_state()

    if not result["restored"]:
        click.echo(click.style("没有可恢复的会话状态", fg="yellow"))
        return

    click.echo(click.style("[OK] 已恢复上次会话状态", fg="green"))
    if result["last_batch"]:
        lb = result["last_batch"]
        click.echo(f"  批次: #{lb['batch_id']} - {lb['file_name']} ({lb['file_type']})")
        if lb.get("selected_at"):
            click.echo(f"  选择时间: {lb['selected_at']}")
    if result["filters"]:
        filters = result["filters"]
        parts = []
        if filters.get("operator"):
            parts.append(f"处理人: {filters['operator']}")
        if filters.get("status"):
            parts.append(f"状态: {STATUS_LABELS.get(filters['status'], filters['status'])}")
        if parts:
            click.echo(f"  筛选: {', '.join(parts)}")
    if result["last_access_time"]:
        click.echo(f"  上次访问: {result['last_access_time']}")


@batch.command("clear-state")
@click.pass_context
def batch_clear_state(ctx):
    """清除会话状态（不恢复上次批次和筛选条件）"""
    workbench = ctx.obj["workbench"]
    workbench.clear_workbench_state()
    click.echo(click.style("[OK] 已清除会话状态，下次启动将不会恢复", fg="green"))


@batch.command("changes")
@click.option("--batch-id", type=int, default=None, help="指定批次ID，默认显示所有批次")
@click.option("--change-type", default=None,
              type=click.Choice(["new_record", "status_change", "amount_change",
                                 "key_field_change", "duplicate_process"]),
              help="按变更类型过滤")
@click.option("--impact-type", default=None,
              type=click.Choice(["none", "affects_pending", "affects_confirmed",
                                 "affects_revoked", "warning", "critical"]),
              help="按影响类型过滤")
@click.option("--status", "processing_status", default=None,
              type=click.Choice(["pending", "reviewed", "resolved", "ignored"]),
              help="按处理状态过滤")
@click.option("--record-no", default=None, help="按记录编号过滤")
@click.option("--affect", "affect_filter", default=None,
              type=click.Choice(["confirmed", "pending", "revoked", "all"]),
              help="按影响的匹配状态过滤：已确认/待确认/已撤销/所有有影响的")
@click.option("--timeline", is_flag=True, default=False,
              help="按记录编号分组展示变更时间线视图")
@click.option("--with-conflicts-only", is_flag=True, default=False,
              help="只显示包含冲突原因的变更")
@click.pass_context
def batch_changes(ctx, batch_id, change_type, impact_type, processing_status,
                  record_no, affect_filter, timeline, with_conflicts_only):
    """查看批次变更日志明细（支持影响筛选、时间线视图、冲突原因）"""
    from invoice_reconciler.core.change_tracker import (
        IMPACT_FILTER_CONFIRMED, IMPACT_FILTER_PENDING,
        IMPACT_FILTER_REVOKED, IMPACT_FILTER_ALL_AFFECTED,
    )

    tracker = ctx.obj["change_tracker"]
    workbench = ctx.obj["workbench"]
    current_user = get_current_user()

    view_result = workbench.get_unified_change_view(
        batch_id=batch_id,
        change_type=change_type,
        impact_type=impact_type,
        processing_status=processing_status,
        record_no=record_no,
        affect_filter=affect_filter,
        with_conflicts_only=with_conflicts_only,
        operator=current_user,
    )

    logs = view_result["logs"]
    summary = view_result["summary"]
    is_filtered = view_result["is_filtered"]
    full_summary = view_result.get("full_summary", summary)

    if is_filtered:
        filter_desc_parts = []
        if change_type:
            filter_desc_parts.append(f"变更类型={CHANGE_TYPE_LABELS.get(change_type, change_type)}")
        if impact_type:
            filter_desc_parts.append(f"影响类型={IMPACT_TYPE_LABELS.get(impact_type, impact_type)}")
        if processing_status:
            filter_desc_parts.append(f"处理状态={PROCESSING_STATUS_LABELS.get(processing_status, processing_status)}")
        if record_no:
            filter_desc_parts.append(f"记录编号={record_no}")
        if affect_filter:
            filter_labels = {"confirmed": "影响已确认", "pending": "影响待确认",
                             "revoked": "影响已撤销", "all": "所有有影响的"}
            filter_desc_parts.append(f"affect={filter_labels.get(affect_filter, affect_filter)}")
        if with_conflicts_only:
            filter_desc_parts.append("仅含冲突")
        filter_desc = "、".join(filter_desc_parts)
        click.echo(click.style(
            f"🔍 已应用筛选：{filter_desc}，命中 {view_result['hit_count']} 条 / 共 {full_summary['total_changes']} 条",
            fg="cyan"
        ))
        click.echo()

    if timeline:
        timeline_data = tracker.get_change_timeline(
            batch_id=batch_id, operator=current_user
        )
        if is_filtered:
            valid_record_keys = set((l["record_type"], l["record_no"]) for l in logs)
            timeline_data["records"] = [
                r for r in timeline_data["records"]
                if (r["record_type"], r["record_no"]) in valid_record_keys
            ]
            timeline_data["total_records"] = len(timeline_data["records"])
            filtered_change_count = sum(r["change_count"] for r in timeline_data["records"])
            timeline_data["total_changes"] = filtered_change_count
            timeline_data["records_with_conflict"] = sum(
                1 for r in timeline_data["records"] if r["has_conflict"]
            )

        if not timeline_data["records"]:
            click.echo(click.style("[OK] 未检测到变更记录", fg="green"))
            return

        click.echo(click.style(
            f"📅 变更时间线：{timeline_data['total_records']} 条记录、"
            f"{timeline_data['total_changes']} 次变更、"
            f"{timeline_data['records_with_conflict']} 条含冲突",
            fg="cyan", bold=True
        ))
        click.echo()
        for idx, rec in enumerate(timeline_data["records"][:30]):
            rt_label = "发票" if rec["record_type"] == "invoice" else "收款"
            conflict_badge = click.style(" ⚠冲突", fg="red") if rec["has_conflict"] else ""
            click.echo(click.style(
                f"[{idx+1}] {rt_label} {rec['record_no']} "
                f"（{rec['change_count']} 次变更）{conflict_badge}",
                fg="blue", bold=True
            ))
            for ti, step in enumerate(rec["timeline"]):
                step_prefix = "  └─" if ti == len(rec["timeline"]) - 1 else "  ├─"
                conflict_note = ""
                if step["conflict"]:
                    conflict_note = click.style(
                        f" [冲突: {step['conflict'][:40]}...]",
                        fg="red"
                    ) if len(step["conflict"]) > 40 else click.style(
                        f" [冲突: {step['conflict']}]", fg="red"
                    )
                click.echo(
                    f"{step_prefix} #{step['batch_id']} {step['change_type']}: "
                    f"{step['summary'][:50]} | {step['detected_at']}"
                    f" | 影响: {step['impact']}{conflict_note}"
                )
                if step["field"] != "-" and (step["old_value"] != "-" or step["new_value"] != "-"):
                    click.echo(
                        f"     字段 {step['field']}: {step['old_value']} → {step['new_value']}"
                    )
            click.echo()

        total_timeline = len(timeline_data["records"])
        if total_timeline > 30:
            click.echo(f"... 还有 {total_timeline - 30} 条记录的时间线，建议导出查看")
        return

    if not logs:
        click.echo(click.style("[OK] 未检测到变更记录", fg="green"))
        return

    conflict_count = summary.get("conflict_count", 0)
    click.echo(click.style(f"📋 共 {len(logs)} 条变更记录"
                           f"（{conflict_count} 条含冲突原因）", fg="cyan", bold=True))
    click.echo()
    click.echo(click.style("=== 变更统计 ===", fg="cyan"))
    for ct, count in summary["by_type"].items():
        click.echo(f"  {CHANGE_TYPE_LABELS.get(ct, ct)}: {count}")
    click.echo()
    click.echo(click.style("=== 影响统计 ===", fg="magenta"))
    for it, count in summary["by_impact"].items():
        if count > 0:
            click.echo(f"  {IMPACT_TYPE_LABELS.get(it, it)}: {count}")
    click.echo()
    click.echo(click.style("=== 处理状态 ===", fg="yellow"))
    for ps, count in summary["by_status"].items():
        click.echo(f"  {PROCESSING_STATUS_LABELS.get(ps, ps)}: {count}")
    if conflict_count > 0:
        click.echo(click.style(f"=== 冲突记录: {conflict_count} 条 ===", fg="red"))
    click.echo()

    headers = ["ID", "批次", "变更类型", "记录类型", "记录编号",
               "影响类型", "处理状态", "冲突?", "变更摘要", "检测时间"]
    rows = []
    for log in logs[:20]:
        conflict_flag = click.style("⚠", fg="red") if log.get("conflict_reason") else "-"
        rows.append([
            log["id"],
            log["batch_id"],
            CHANGE_TYPE_LABELS.get(log["change_type"], log["change_type"]),
            "发票" if log["record_type"] == "invoice" else "收款",
            log["record_no"],
            IMPACT_TYPE_LABELS.get(log["impact_type"] or "none", log["impact_type"] or "none"),
            PROCESSING_STATUS_LABELS.get(log["processing_status"] or "pending",
                                        log["processing_status"] or "pending"),
            conflict_flag,
            (log["change_summary"] or "")[:40],
            log["detected_at"],
        ])
    print_table(headers, rows)

    conflict_logs = [l for l in logs[:20] if l.get("conflict_reason")]
    if conflict_logs:
        click.echo()
        click.echo(click.style("⚠ 以下变更包含冲突原因：", fg="red", bold=True))
        for clog in conflict_logs[:10]:
            click.echo(click.style(
                f"  • 日志 #{clog['id']} 批次 #{clog['batch_id']} "
                f"{'发票' if clog['record_type'] == 'invoice' else '收款'} {clog['record_no']}:",
                fg="red"
            ))
            for line in clog["conflict_reason"].split(" | "):
                click.echo(f"      - {line}")

    if len(logs) > 20:
        click.echo(f"\n... 还有 {len(logs) - 20} 条，使用 `batch export-changes` 导出完整明细")

    click.echo()
    click.echo("变更说明:")
    click.echo("  • 新增记录: 本次导入新增的记录，之前批次中不存在")
    click.echo("  • 状态变更: 同一记录在不同批次中的状态不一致")
    click.echo("  • 金额变更: 同一记录在不同批次中的金额不一致")
    click.echo("  • 关键字段变更: 客户、日期等关键字段发生变化")
    click.echo("  • 重复处理: 同一记录被不同操作者处理")
    click.echo()
    click.echo(click.style("冲突类型说明:", fg="red"))
    click.echo("  • 连续更新: 同记录在多个批次中连续发生同字段变更")
    click.echo("  • 撤销后重导入: 已撤销的匹配关联记录重新导入")
    click.echo("  • 同字段并发修改: 不同批次/文件同时改到同一关键字段")


@batch.command("export-changes")
@click.argument("batch_id", type=int)
@click.option("--operator", default=None, help="当前操作者")
@click.option("--format", "export_format", type=click.Choice(["json", "csv"]),
              default="json", help="导出格式，默认JSON")
@click.option("--change-type", default=None,
              type=click.Choice(["new_record", "status_change", "amount_change",
                                 "key_field_change", "duplicate_process"]),
              help="按变更类型过滤导出")
@click.option("--impact-type", default=None,
              type=click.Choice(["none", "affects_pending", "affects_confirmed",
                                 "affects_revoked", "warning", "critical"]),
              help="按影响类型过滤导出")
@click.option("--status", "processing_status", default=None,
              type=click.Choice(["pending", "reviewed", "resolved", "ignored"]),
              help="按处理状态过滤导出")
@click.option("--record-no", default=None, help="按记录编号过滤导出")
@click.option("--affect", "affect_filter", default=None,
              type=click.Choice(["confirmed", "pending", "revoked", "all"]),
              help="按影响的匹配状态过滤导出：已确认/待确认/已撤销/所有有影响的")
@click.option("--with-conflicts-only", is_flag=True, default=False,
              help="只导出包含冲突原因的变更")
@click.option("--use-last-filter", is_flag=True, default=False,
              help="使用上次 batch changes 的筛选条件导出")
@click.pass_context
def batch_export_changes(ctx, batch_id, operator, export_format,
                         change_type, impact_type, processing_status,
                         record_no, affect_filter, with_conflicts_only,
                         use_last_filter):
    """导出批次变更日志（JSON/CSV，筛选与 batch changes 命令完全一致）"""
    tracker = ctx.obj["change_tracker"]
    workbench = ctx.obj["workbench"]
    operator = operator or get_current_user()

    if use_last_filter:
        last_view = workbench.get_last_change_view_context()
        if last_view:
            if change_type is None:
                change_type = last_view.get("change_type")
            if impact_type is None:
                impact_type = last_view.get("impact_type")
            if processing_status is None:
                processing_status = last_view.get("processing_status")
            if record_no is None:
                record_no = last_view.get("record_no")
            if affect_filter is None:
                affect_filter = last_view.get("affect_filter")
            if not with_conflicts_only:
                with_conflicts_only = last_view.get("with_conflicts_only") or False
            click.echo(click.style(
                f"🔁 使用上次视图筛选条件",
                fg="cyan"
            ))
        else:
            click.echo(click.style(
                "⚠ 没有找到上次的筛选条件，将导出全量变更",
                fg="yellow"
            ))

    view_result = workbench.get_unified_change_view(
        batch_id=batch_id,
        change_type=change_type,
        impact_type=impact_type,
        processing_status=processing_status,
        record_no=record_no,
        affect_filter=affect_filter,
        with_conflicts_only=with_conflicts_only,
        operator=operator,
    )

    logs = view_result["logs"]
    summary = view_result["summary"]
    is_filtered = view_result["is_filtered"]
    filter_info = view_result["filter_info"]

    log_ids = [l["id"] for l in logs] if is_filtered else None

    filter_desc_parts = []
    if change_type:
        filter_desc_parts.append(f"变更类型={CHANGE_TYPE_LABELS.get(change_type, change_type)}")
    if impact_type:
        filter_desc_parts.append(f"影响类型={IMPACT_TYPE_LABELS.get(impact_type, impact_type)}")
    if processing_status:
        filter_desc_parts.append(f"处理状态={PROCESSING_STATUS_LABELS.get(processing_status, processing_status)}")
    if record_no:
        filter_desc_parts.append(f"记录编号={record_no}")
    if affect_filter:
        filter_labels = {"confirmed": "影响已确认", "pending": "影响待确认",
                         "revoked": "影响已撤销", "all": "所有有影响的"}
        filter_desc_parts.append(f"affect={filter_labels.get(affect_filter, affect_filter)}")
    if with_conflicts_only:
        filter_desc_parts.append("仅含冲突")
    filter_note = "、".join(filter_desc_parts) if filter_desc_parts else "未应用筛选，导出全量变更"

    extra_meta = {
        "filter_note": filter_note,
        "applied_filters": {k: v for k, v in filter_info.items() if v is not None and v is not False},
        "hit_count": len(logs),
        "export_note": ("导出内容与 `batch changes` 命令筛选条件完全一致，"
                        "包含变更前后摘要、导入来源、操作者、时间、关联批次、处理状态、冲突原因"),
        "cli_command_tip": (
            f"可执行: reconciler batch changes --batch-id {batch_id}"
            + (f" --change-type {change_type}" if change_type else "")
            + (f" --impact-type {impact_type}" if impact_type else "")
            + (f" --status {processing_status}" if processing_status else "")
            + (f" --record-no {record_no}" if record_no else "")
            + (f" --affect {affect_filter}" if affect_filter else "")
            + (" --with-conflicts-only" if with_conflicts_only else "")
            + " 查看 CLI 输出"
        ),
    }

    workbench.save_export_context(
        batch_id=batch_id,
        export_type="change_logs",
        format=export_format,
        operator=operator,
        filters=filter_info,
        extra=extra_meta,
    )

    click.echo(f"正在导出批次 #{batch_id} 变更日志（{filter_note}）...")
    result = tracker.export_change_logs(
        batch_id, operator, format=export_format,
        log_ids=log_ids, extra_meta=extra_meta
    )

    if not result["success"]:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    receipt_cabinet = ctx.obj["receipt_cabinet"]
    actual_log_ids = log_ids if log_ids else [l["id"] for l in logs]
    receipt_result = receipt_cabinet.create_receipt_from_export(
        operator=operator,
        batch_id=batch_id,
        target_file=result["file_path"],
        export_format=export_format,
        filter_snapshot=filter_info,
        log_ids=actual_log_ids,
        summary_stats=result["summary"],
    )

    click.echo(click.style(f"[OK] 变更日志已导出: {result['file_path']}", fg="green"))
    click.echo(f"格式: {result['format']}")
    click.echo(f"命中数: {result['total_changes']} 条")
    click.echo(f"生成时间: {result['generated_at']}")
    if receipt_result.get("success"):
        click.echo(click.style(f"[OK] 导出回执已生成: {receipt_result['receipt_id']}", fg="green"))
        click.echo(f"  查看回执: receipt show {receipt_result['receipt_id']}")
        click.echo(f"  对比回执: receipt compare {receipt_result['receipt_id']}")
        click.echo(f"  续导回执: receipt resume {receipt_result['receipt_id']}")
    click.echo()
    click.echo("导出摘要（与筛选视图一致）:")
    click.echo("  按变更类型:")
    for ct, count in result["summary"]["by_type"].items():
        click.echo(f"    {CHANGE_TYPE_LABELS.get(ct, ct)}: {count}")
    click.echo("  按影响类型:")
    for it, count in result["summary"]["by_impact"].items():
        if count > 0:
            click.echo(f"    {IMPACT_TYPE_LABELS.get(it, it)}: {count}")
    click.echo("  按处理状态:")
    for ps, count in result["summary"]["by_status"].items():
        click.echo(f"    {PROCESSING_STATUS_LABELS.get(ps, ps)}: {count}")


@batch.command("change-status")
@click.argument("log_id", type=int)
@click.option("--status", "processing_status", required=True,
              type=click.Choice(["pending", "reviewed", "resolved", "ignored"]),
              help="设置处理状态")
@click.option("--operator", default=None, help="当前操作者")
@click.option("--remark", default=None, help="处理备注")
@click.pass_context
def batch_change_status(ctx, log_id, processing_status, operator, remark):
    """更新变更日志处理状态"""
    db = ctx.obj["db"]
    operator = operator or get_current_user()

    db.update_change_log_status(log_id, processing_status, operator, remark)

    status_label = PROCESSING_STATUS_LABELS.get(processing_status, processing_status)
    click.echo(click.style(f"[OK] 变更日志 #{log_id} 状态已更新为: {status_label}", fg="green"))
    if remark:
        click.echo(f"备注: {remark}")


@batch.command("resume-export")
@click.option("--operator", default=None, help="当前操作者")
@click.option("--force", is_flag=True, default=False, help="强制续导（忽略非严重拦截项）")
@click.pass_context
def batch_resume_export(ctx, operator, force):
    """使用上次导出上下文继续导出（程序重启后不丢失）"""
    workbench = ctx.obj["workbench"]
    tracker = ctx.obj["change_tracker"]
    receipt_cabinet = ctx.obj["receipt_cabinet"]
    exporter = ctx.obj["exporter"]
    operator = operator or get_current_user()

    latest_receipt = receipt_cabinet.find_latest_receipt()
    if latest_receipt:
        receipt_id = latest_receipt.get("receipt_id")
        batch_id = latest_receipt.get("batch_id")
        export_format = latest_receipt.get("export_format", "json")
        hit_count = latest_receipt.get("hit_count", 0)

        click.echo(click.style(f"[会话恢复] 使用上次导出回执: {receipt_id}", fg="cyan"))
        click.echo(f"  批次: #{batch_id} - {latest_receipt.get('file_name', '-') or latest_receipt.get('target_file', '-')}")
        click.echo(f"  格式: {export_format}")
        click.echo(f"  命中记录: {hit_count} 条")
        if latest_receipt.get("exported_at"):
            click.echo(f"  上次导出时间: {latest_receipt['exported_at']}")
        click.echo()

        check = receipt_cabinet.check_interceptions(receipt_id)
        if check.get("interceptions") and not force:
            click.echo(click.style(
                f"[!!] 检测到 {check['interception_count']} 个拦截项:",
                fg="red", bold=True
            ))
            for i in check["interceptions"]:
                click.echo(click.style(
                    f"  • [{i['severity']}] {i['label']}: {i['detail']}",
                    fg="red" if i["severity"] == "critical" else "yellow"
                ))
            if check.get("can_resume"):
                click.echo(click.style("使用 --force 强制续导（仅非严重拦截项）", fg="yellow"))
            else:
                click.echo(click.style("存在严重拦截项，请先处理: receipt handle", fg="red"))
            sys.exit(1)

        resume_result = receipt_cabinet.resume_with_receipt(
            receipt_id, operator, force=force
        )

        if not resume_result["success"]:
            click.echo(click.style(f"[!!] 续导失败", fg="red"), err=True)
            if resume_result.get("message"):
                click.echo(resume_result["message"])
            sys.exit(1)

        export_result = resume_result.get("export_result")
        if export_result and export_result.get("success"):
            click.echo(click.style(f"[OK] 导出已完成: {export_result['file_path']}", fg="green"))
            click.echo(f"格式: {export_result['format']}")
            click.echo(f"生成时间: {export_result['generated_at']}")
            if export_result.get("total_changes") is not None:
                click.echo(f"记录数: {export_result['total_changes']}")
        else:
            click.echo(click.style("[!!] 导出未成功", fg="red"), err=True)
            if export_result and export_result.get("message"):
                click.echo(export_result["message"])
            sys.exit(1)

        return

    last_context = workbench.get_last_export_context()
    if not last_context:
        click.echo(click.style("[!!] 没有可恢复的导出上下文", fg="yellow"), err=True)
        click.echo("请先使用 `batch export-changes` 或 `batch export-progress` 进行导出")
        return

    batch_id = last_context["batch_id"]
    export_type = last_context.get("export_type", "change_logs")
    export_format = last_context.get("format", "json")

    click.echo(click.style(f"[会话恢复] 使用上次导出上下文:", fg="cyan"))
    click.echo(f"  批次: #{batch_id} - {last_context.get('file_name', '-')}")
    click.echo(f"  导出类型: {export_type}")
    click.echo(f"  格式: {export_format}")
    if last_context.get("exported_at"):
        click.echo(f"  上次导出时间: {last_context['exported_at']}")
    click.echo()

    if export_type == "change_logs":
        extra_context = last_context.get("extra", {})
        log_ids = extra_context.get("log_ids")
        extra_meta = extra_context.get("extra_meta") or {}
        if log_ids:
            extra_meta.setdefault("filter_note", "恢复重启前的筛选条件")
            extra_meta.setdefault("restored_from", last_context.get("exported_at", "-"))
        filter_msg = (
            f"（{extra_meta.get('filter_note', '全量')}"
            f"，{len(log_ids) if log_ids else '全部'} 条）"
        )
        click.echo(f"正在导出批次 #{batch_id} 变更日志{filter_msg}...")
        result = tracker.export_change_logs(
            batch_id, operator, format=export_format,
            log_ids=log_ids, extra_meta=extra_meta
        )
    elif export_type == "batch_progress":
        click.echo(f"正在导出批次 #{batch_id} 进度...")
        result = exporter.export_batch_progress(batch_id, operator, format=export_format)
    else:
        click.echo(click.style(f"[!!] 未知的导出类型: {export_type}", fg="red"), err=True)
        return

    if not result["success"]:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style(f"[OK] 导出已完成: {result['file_path']}", fg="green"))
    click.echo(f"格式: {result['format']}")
    click.echo(f"生成时间: {result['generated_at']}")


@batch.command("list")
@click.option("--all", "show_all", is_flag=True, help="显示所有批次（含已处理完成）")
@click.pass_context
def batch_list(ctx, show_all):
    """列出所有批次"""
    workbench = ctx.obj["workbench"]

    result = workbench.get_batch_workbench_summary()

    if not result["success"]:
        click.echo(click.style(result["message"], fg="yellow"))
        return

    batches = result["batches"]
    if not show_all:
        batches = [b for b in batches if b["has_unfinished"]]

    if not batches:
        click.echo("暂无批次" if show_all else "所有批次已处理完成")
        return

    headers = ["ID", "类型", "文件名", "操作人", "进度", "待办", "冲突", "导入时间"]
    rows = []
    for b in batches:
        todo_count = b["pending_matches"] + b["unmatched_invoices"] + b["unmatched_payments"] + b["conflict_count"]
        rows.append([
            b["batch_id"],
            b["file_type"],
            b["file_name"],
            b["operator"],
            f"{b['progress_percent']:.1f}%",
            todo_count if b["has_unfinished"] else "-",
            b["conflict_count"] if b["conflict_count"] > 0 else "-",
            b["imported_at"],
        ])
    print_table(headers, rows)


@cli.group()
def handover():
    """批次交接包管理"""
    pass


@handover.command("create")
@click.option("--operator", default=None, help="操作者")
@click.option("--batch-id", type=int, default=None, help="指定批次ID")
@click.option("--description", default=None, help="交接包描述")
@click.pass_context
def handover_create(ctx, operator, batch_id, description):
    """创建批次交接包"""
    handover_mgr = ctx.obj["handover"]
    operator = operator or get_current_user()

    result = handover_mgr.create_package(operator, batch_id=batch_id, description=description)

    if not result["success"]:
        click.echo(click.style(f"[!!] 创建失败", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style(f"[OK] 交接包已创建: {result['package_id']}", fg="green"))
    click.echo(f"配置哈希: {result['config_hash']}")
    click.echo(f"状态: {result['status']}")
    click.echo(f"创建时间: {result['created_at']}")
    if description:
        click.echo(f"描述: {description}")


@handover.command("list")
@click.option("--all", "show_all", is_flag=True, help="包含已废弃的交接包")
@click.pass_context
def handover_list(ctx, show_all):
    """列出交接包"""
    handover_mgr = ctx.obj["handover"]

    packages = handover_mgr.list_packages(include_discarded=show_all)

    if not packages:
        click.echo("暂无交接包")
        return

    status_labels = {
        HANDOVER_STATUS_ACTIVE: "可用",
        HANDOVER_STATUS_RESTORED: "已恢复",
        HANDOVER_STATUS_DISCARDED: "已废弃",
    }

    headers = ["交接包ID", "状态", "操作人", "描述", "创建时间"]
    rows = []
    for p in packages:
        rows.append([
            p.get("package_id", "-"),
            status_labels.get(p.get("status"), p.get("status", "-")),
            p.get("operator") or "-",
            (p.get("description") or "")[:30],
            p.get("created_at", "-"),
        ])
    print_table(headers, rows)


@handover.command("preview")
@click.argument("package_id")
@click.pass_context
def handover_preview(ctx, package_id):
    """预览交接包（恢复前查看）"""
    handover_mgr = ctx.obj["handover"]

    result = handover_mgr.preview_package(package_id)

    if not result["success"]:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    preview = result["preview"]
    click.echo(click.style(f"=== 交接包预览: {package_id} ===", fg="cyan", bold=True))
    click.echo(f"状态: {preview['status']} | 操作人: {preview.get('operator', '-')} | 创建时间: {preview.get('created_at', '-')}")
    if preview.get("description"):
        click.echo(f"描述: {preview['description']}")
    click.echo()

    if preview.get("will_restore_batch") and preview.get("batch_info"):
        bi = preview["batch_info"]
        click.echo(click.style("[批次]", fg="cyan") + f" 将恢复批次: #{bi.get('batch_id', '-')} - {bi.get('file_name', '-')}")
    else:
        click.echo(click.style("[批次]", fg="yellow") + " 无批次信息")

    if preview.get("will_restore_filters") and preview.get("filters"):
        filters = preview["filters"]
        parts = []
        if filters.get("operator"):
            parts.append(f"处理人: {filters['operator']}")
        if filters.get("status"):
            parts.append(f"状态: {STATUS_LABELS.get(filters['status'], filters['status'])}")
        click.echo(click.style("[筛选]", fg="cyan") + f" 将恢复筛选: {', '.join(parts)}")
    else:
        click.echo(click.style("[筛选]", fg="yellow") + " 无筛选条件")

    if preview.get("will_restore_change_view") and preview.get("change_view_context"):
        cv = preview["change_view_context"]
        parts = []
        if cv.get("change_type"):
            parts.append(f"变更类型: {CHANGE_TYPE_LABELS.get(cv['change_type'], cv['change_type'])}")
        if cv.get("impact_type"):
            parts.append(f"影响类型: {IMPACT_TYPE_LABELS.get(cv['impact_type'], cv['impact_type'])}")
        click.echo(click.style("[变更视图]", fg="cyan") + f" 将恢复: {', '.join(parts) if parts else '全部'}")
    else:
        click.echo(click.style("[变更视图]", fg="yellow") + " 无变更视图上下文")

    if preview.get("will_restore_export") and preview.get("export_context"):
        ec = preview["export_context"]
        click.echo(click.style("[导出]", fg="cyan") + f" 将恢复导出上下文: 批次 #{ec.get('batch_id', '-')} ({ec.get('export_type', '-')}, {ec.get('format', '-')})")
    else:
        click.echo(click.style("[导出]", fg="yellow") + " 无导出上下文")

    if preview.get("has_conflicts"):
        click.echo()
        click.echo(click.style("⚠ 冲突检测:", fg="red", bold=True))
        for c in preview["conflicts"]:
            ct_label = HANDOVER_CONFLICT_LABELS.get(c["conflict_type"], c["conflict_type"])
            click.echo(click.style(f"  • [{ct_label}] {c.get('detail', c.get('message', ''))}", fg="red"))
        click.echo()
        click.echo(click.style("使用 --force 强制恢复", fg="yellow"))


@handover.command("restore")
@click.argument("package_id")
@click.option("--operator", default=None, help="操作者")
@click.option("--force", is_flag=True, default=False, help="强制恢复（忽略冲突）")
@click.pass_context
def handover_restore(ctx, package_id, operator, force):
    """恢复交接包"""
    handover_mgr = ctx.obj["handover"]
    operator = operator or get_current_user()

    result = handover_mgr.restore_package(package_id, operator, force=force)

    if not result["success"]:
        if result.get("conflicts"):
            click.echo(click.style(f"[!!] 存在冲突，无法恢复:", fg="red", bold=True))
            for c in result["conflicts"]:
                ct_label = HANDOVER_CONFLICT_LABELS.get(c["conflict_type"], c["conflict_type"])
                click.echo(click.style(f"  • [{ct_label}] {c.get('detail', c.get('message', ''))}", fg="red"))
            click.echo()
            click.echo(click.style("使用 --force 强制恢复，或先解决冲突", fg="yellow"))
        else:
            click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style(f"[OK] 交接包已恢复: {package_id}", fg="green"))
    click.echo(f"撤销ID: {result['undo_id']}")
    click.echo(f"已恢复项: {', '.join(result.get('applied', []))}")

    if result.get("conflicts"):
        click.echo()
        click.echo(click.style("⚠ 恢复过程中检测到冲突（已强制跳过）:", fg="yellow"))
        for c in result["conflicts"]:
            ct_label = HANDOVER_CONFLICT_LABELS.get(c["conflict_type"], c["conflict_type"])
            click.echo(f"  • [{ct_label}] {c.get('detail', c.get('message', ''))}")


@handover.command("diff")
@click.argument("package_id")
@click.pass_context
def handover_diff(ctx, package_id):
    """查看交接包与当前状态的差异"""
    handover_mgr = ctx.obj["handover"]

    result = handover_mgr.diff_package(package_id)

    if not result["success"]:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    if result["no_change"]:
        click.echo(click.style("[OK] 当前状态与交接包一致，无差异", fg="green"))
        return

    click.echo(click.style(f"=== 交接包差异: {package_id} ({result['change_count']} 处) ===", fg="cyan", bold=True))

    field_labels = {
        "last_selected_batch": "选中批次",
        "filters": "筛选条件",
        "change_view_context": "变更视图",
        "export_context": "导出上下文",
        "stats": "统计摘要",
    }

    for change in result["changes"]:
        field = change["field"]
        label = field_labels.get(field, field)
        before = change["before"]
        after = change["after"]
        click.echo(click.style(f"\n  [{label}]", fg="yellow"))
        if field == "last_selected_batch":
            before_label = f"#{before.get('batch_id', '-')}" if before else "无"
            after_label = f"#{after.get('batch_id', '-')}" if after else "无"
            click.echo(f"    当前: {before_label} → 恢复后: {after_label}")
        elif field == "filters":
            before_parts = [f"{k}={v}" for k, v in (before or {}).items() if v is not None] or ["无"]
            after_parts = [f"{k}={v}" for k, v in (after or {}).items() if v is not None] or ["无"]
            click.echo(f"    当前: {', '.join(before_parts)}")
            click.echo(f"    恢复后: {', '.join(after_parts)}")
        else:
            click.echo(f"    当前: {json.dumps(before, ensure_ascii=False)[:80] if before else '无'}")
            click.echo(f"    恢复后: {json.dumps(after, ensure_ascii=False)[:80] if after else '无'}")


@handover.command("undo")
@click.argument("undo_id")
@click.option("--operator", default=None, help="操作者")
@click.pass_context
def handover_undo(ctx, undo_id, operator):
    """撤销交接包恢复"""
    handover_mgr = ctx.obj["handover"]
    operator = operator or get_current_user()

    result = handover_mgr.undo_restore(undo_id, operator)

    if not result["success"]:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style(f"[OK] 已撤销恢复: {undo_id}", fg="green"))
    click.echo(f"交接包: {result.get('package_id', '-')}")
    click.echo(f"已恢复项: {', '.join(result.get('applied', []))}")


@handover.command("save-copy")
@click.argument("package_id")
@click.option("--operator", default=None, help="操作者")
@click.option("--description", default=None, help="新描述")
@click.pass_context
def handover_save_copy(ctx, package_id, operator, description):
    """另存交接包副本"""
    handover_mgr = ctx.obj["handover"]
    operator = operator or get_current_user()

    result = handover_mgr.save_as_copy(package_id, operator, description=description)

    if not result["success"]:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style(f"[OK] 已创建副本: {result['new_package_id']}", fg="green"))
    click.echo(f"原始: {result['original_package_id']}")
    click.echo(f"描述: {result['description']}")


@handover.command("discard")
@click.argument("package_id")
@click.option("--operator", default=None, help="操作者")
@click.pass_context
def handover_discard(ctx, package_id, operator):
    """废弃交接包（不删除，保留审计记录）"""
    handover_mgr = ctx.obj["handover"]
    operator = operator or get_current_user()

    result = handover_mgr.discard_package(package_id, operator)

    if not result["success"]:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style(f"[OK] 交接包已废弃: {package_id}", fg="yellow"))


@handover.command("cleanup")
@click.option("--operator", default=None, help="操作者")
@click.pass_context
def handover_cleanup(ctx, operator):
    """清理失效的交接包记录"""
    handover_mgr = ctx.obj["handover"]
    operator = operator or get_current_user()
    config_hash = handover_mgr._compute_config_hash()

    result = handover_mgr.cleanup_invalid(config_hash, operator)

    if result["removed_count"] > 0:
        click.echo(click.style(f"[OK] 已清理 {result['removed_count']} 个失效记录", fg="green"))
        for detail in result.get("invalid_details", []):
            reasons = ", ".join(detail["reasons"])
            click.echo(f"  • {detail['package_id']}: {reasons}")
    else:
        click.echo(click.style("[OK] 无失效记录", fg="green"))


@handover.command("timeline")
@click.option("--package-id", default=None, help="指定交接包ID")
@click.option("--limit", type=int, default=20, help="显示条数")
@click.pass_context
def handover_timeline(ctx, package_id, limit):
    """查看交接包操作时间线"""
    handover_mgr = ctx.obj["handover"]

    events = handover_mgr.get_timeline(package_id=package_id, limit=limit)

    if not events:
        click.echo("暂无操作记录")
        return

    event_labels = {
        HANDOVER_EVENT_CREATE: "创建",
        HANDOVER_EVENT_RESTORE: "恢复",
        HANDOVER_EVENT_UNDO: "撤销",
        HANDOVER_EVENT_DISCARD: "废弃",
        HANDOVER_EVENT_SAVE_COPY: "另存副本",
        HANDOVER_EVENT_CLEANUP: "清理",
    }

    click.echo(click.style("=== 交接包操作时间线 ===", fg="cyan", bold=True))
    for evt in events:
        evt_label = event_labels.get(evt["event_type"], evt["event_type"])
        pkg = evt.get("package_id", "-")
        op = evt.get("operator", "-")
        ts = evt.get("timestamp", "-")
        click.echo(f"  [{ts}] {evt_label} | 交接包: {pkg} | 操作人: {op}")


@handover.command("session-summary")
@click.argument("package_id")
@click.option("--operator", default=None, help="操作者")
@click.pass_context
def handover_session_summary(ctx, package_id, operator):
    """查看交接会话摘要（筛选条件、导出状态、文件状态、可用动作）"""
    playback = ctx.obj["playback_center"]
    operator = operator or get_current_user()

    result = playback.get_session_summary(package_id, operator)

    if not result["success"]:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    summary = result["summary"]
    conflicts = result["conflicts"]
    file_status = result["file_status"]
    available_actions = result["available_actions"]

    click.echo(click.style(f"=== 交接会话摘要: {package_id} ===", fg="cyan", bold=True))
    click.echo(f"状态: {result['package_status']} | 操作人: {result.get('operator', '-')} | 创建时间: {result.get('created_at', '-')}")
    if result.get("description"):
        click.echo(f"描述: {result['description']}")
    click.echo()

    click.echo(click.style("--- 筛选条件 ---", fg="yellow"))
    filters = summary.get("filters", {})
    filter_parts = []
    if filters.get("operator"):
        filter_parts.append(f"处理人: {filters['operator']}")
    if filters.get("status"):
        filter_parts.append(f"状态: {STATUS_LABELS.get(filters['status'], filters['status'])}")
    if filters.get("impact_filter"):
        filter_parts.append(f"影响类型: {filters['impact_filter']}")
    if filters.get("with_conflicts_only"):
        filter_parts.append("仅含冲突")
    if filters.get("change_type"):
        filter_parts.append(f"变更类型: {CHANGE_TYPE_LABELS.get(filters['change_type'], filters['change_type'])}")
    if filters.get("impact_type"):
        filter_parts.append(f"影响类型: {IMPACT_TYPE_LABELS.get(filters['impact_type'], filters['impact_type'])}")
    if filters.get("processing_status"):
        filter_parts.append(f"处理状态: {PROCESSING_STATUS_LABELS.get(filters['processing_status'], filters['processing_status'])}")

    if filter_parts:
        click.echo(f"  已应用筛选: {', '.join(filter_parts)}")
    else:
        click.echo(click.style("  ⚠ 无筛选条件（全量视图）", fg="red"))

    if summary.get("is_full_view"):
        click.echo(click.style("  警告：恢复后将回到全量视图", fg="red", bold=True))
    click.echo()

    click.echo(click.style("--- 导出摘要 ---", fg="yellow"))
    if summary.get("export_type"):
        click.echo(f"  导出类型: {summary['export_type']}")
        click.echo(f"  导出格式: {summary.get('export_format', '-')}")
        click.echo(f"  导出路径: {summary.get('export_path', '-')}")
        if summary.get("exported_at"):
            click.echo(f"  导出时间: {summary['exported_at']}")
        if summary.get("hit_count") is not None:
            click.echo(f"  记录数: {summary['hit_count']}")
    else:
        click.echo("  暂无导出记录")
    click.echo()

    click.echo(click.style("--- 文件状态 ---", fg="yellow"))
    click.echo(f"  路径: {file_status['export_path']}")
    click.echo(f"  存在: {'是' if file_status['exists'] else '否'}")
    click.echo(f"  可写: {'是' if file_status['is_writable'] else '否'}")
    if file_status.get("file_count"):
        click.echo(f"  文件数: {file_status['file_count']}")
        click.echo(f"  总大小: {file_status['total_size']} bytes")
    click.echo()

    if conflicts:
        click.echo(click.style("--- 冲突检测 ---", fg="red", bold=True))
        for c in conflicts:
            ct_label = HANDOVER_CONFLICT_LABELS.get(c["conflict_type"], c["conflict_type"])
            click.echo(click.style(f"  • [{ct_label}] {c.get('detail', c.get('message', ''))}", fg="red"))
        click.echo()

    click.echo(click.style("--- 可用动作 ---", fg="green"))
    for action in available_actions:
        status_icon = "✓" if action.get("enabled") else "✗"
        status_color = "green" if action.get("enabled") else "yellow"
        warning = ""
        if action.get("warning"):
            warning = f" ({action['warning']})"
        danger = ""
        if action.get("dangerous"):
            danger = " [危险]"
        requires_confirm = ""
        if action.get("requires_confirmation"):
            requires_confirm = " [需确认]"
        click.echo(click.style(
            f"  {status_icon} {action['label']}{warning}{danger}{requires_confirm}",
            fg=status_color
        ))
        click.echo(f"     {action['description']}")
    click.echo()

    if summary.get("is_full_view"):
        click.echo(click.style(
            "⚠ 重要：此交接包无筛选条件，恢复后将回到全量视图。"
            "建议先设置筛选条件后重新创建交接包。",
            fg="red", bold=True
        ))


@handover.command("resume-export")
@click.argument("package_id")
@click.option("--operator", default=None, help="操作者")
@click.option("--force", is_flag=True, default=False, help="强制续导出（忽略冲突和全量视图警告）")
@click.pass_context
def handover_resume_export(ctx, package_id, operator, force):
    """续导出：恢复交接包并继续导出（需确认筛选条件）"""
    playback = ctx.obj["playback_center"]
    operator = operator or get_current_user()

    result = playback.resume_export(package_id, operator, force=force)

    if not result["success"]:
        if result.get("warning") == "full_view_fallback":
            click.echo(click.style("[!!] 无筛选条件，恢复后将回到全量视图", fg="red", bold=True), err=True)
            click.echo(click.style(result["message"], fg="red"), err=True)
            click.echo()
            summary = result.get("summary", {})
            if summary:
                click.echo("当前筛选状态:")
                if summary.get("is_full_view"):
                    click.echo("  全量视图（无筛选条件）")
        elif result.get("conflicts"):
            click.echo(click.style(f"[!!] 存在 {len(result['conflicts'])} 个冲突，无法续导出:", fg="red", bold=True), err=True)
            for c in result["conflicts"]:
                ct_label = HANDOVER_CONFLICT_LABELS.get(c["conflict_type"], c["conflict_type"])
                click.echo(click.style(f"  • [{ct_label}] {c.get('detail', c.get('message', ''))}", fg="red"))
            click.echo()
            click.echo(click.style("使用 --force 强制续导出，或先解决冲突", fg="yellow"))
        elif result.get("file_status"):
            click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        else:
            click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style(f"[OK] 续导出完成: {package_id}", fg="green", bold=True))
    click.echo(f"撤销ID: {result.get('undo_id', '-')}")
    click.echo(f"已恢复项: {', '.join(result.get('applied', []))}")
    click.echo()

    export_result = result.get("export_result", {})
    if export_result.get("success"):
        click.echo(click.style("--- 导出结果 ---", fg="green"))
        click.echo(f"  文件路径: {export_result.get('file_path', '-')}")
        click.echo(f"  格式: {export_result.get('format', '-')}")
        if export_result.get("total_changes") is not None:
            click.echo(f"  记录数: {export_result['total_changes']}")
    else:
        click.echo(click.style("--- 导出结果 ---", fg="yellow"))
        click.echo(f"  状态: 部分成功")
        click.echo(f"  导出信息: {export_result.get('message', '未知')}")

    if result.get("conflicts"):
        click.echo()
        click.echo(click.style("⚠ 恢复过程中检测到冲突（已强制跳过）:", fg="yellow"))
        for c in result["conflicts"]:
            ct_label = HANDOVER_CONFLICT_LABELS.get(c["conflict_type"], c["conflict_type"])
            click.echo(f"  • [{ct_label}] {c.get('detail', c.get('message', ''))}")


@handover.command("export-package")
@click.argument("package_id")
@click.option("--operator", default=None, help="操作者")
@click.option("--target-dir", default=None, help="目标导出目录")
@click.option("--format", "export_format", type=click.Choice(["json", "csv"]),
              default="json", help="数据导出格式")
@click.pass_context
def handover_export_package(ctx, package_id, operator, target_dir, export_format):
    """导出交接包为独立目录（含元数据、筛选条件、数据文件）"""
    playback = ctx.obj["playback_center"]
    operator = operator or get_current_user()

    result = playback.export_handover_package(
        package_id, operator, target_dir=target_dir, format=export_format
    )

    if not result["success"]:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style(f"[OK] 交接包已导出: {package_id}", fg="green", bold=True))
    click.echo(f"导出路径: {result['export_path']}")
    click.echo(f"格式: {result['format']}")
    click.echo(f"包含文件:")
    for f in result["files"]:
        click.echo(f"  - {f}")
    if not result.get("has_active_filters"):
        click.echo()
        click.echo(click.style("⚠ 注意：此交接包无筛选条件（全量视图）", fg="yellow"))


@handover.command("detailed-timeline")
@click.option("--package-id", default=None, help="指定交接包ID")
@click.option("--limit", type=int, default=50, help="显示条数")
@click.pass_context
def handover_detailed_timeline(ctx, package_id, limit):
    """查看详细操作时间线（含事件结果和错误信息）"""
    playback = ctx.obj["playback_center"]

    events = playback.get_detailed_timeline(package_id=package_id, limit=limit)

    if not events:
        click.echo("暂无操作记录")
        return

    click.echo(click.style("=== 详细操作时间线 ===", fg="cyan", bold=True))
    for evt in events:
        status_icon = "✓" if evt["status"] == "success" else ("⚠" if evt["status"] == "partial" else "✗")
        status_color = "green" if evt["status"] == "success" else ("yellow" if evt["status"] == "partial" else "red")

        click.echo(click.style(
            f"  [{evt['timestamp']}] {status_icon} {evt['event_type_label']}",
            fg=status_color
        ))
        click.echo(f"     事件ID: {evt['event_id']}")
        if evt.get("package_id"):
            click.echo(f"     交接包: {evt['package_id']}")
        if evt.get("operator"):
            click.echo(f"     操作人: {evt['operator']}")
        if evt.get("result_summary"):
            click.echo(f"     结果: {evt['result_summary']}")
        if evt.get("error_message"):
            click.echo(click.style(f"     错误: {evt['error_message']}", fg="red"))
        if evt.get("event_details"):
            details = evt["event_details"]
            if isinstance(details, dict):
                detail_str = ", ".join(f"{k}={v}" for k, v in list(details.items())[:5])
                click.echo(f"     详情: {detail_str}")
            else:
                click.echo(f"     详情: {str(details)[:80]}")
        click.echo()


@cli.group()
def receipt():
    """导出回执档案柜"""
    pass


@receipt.command("create")
@click.option("--operator", default=None, help="操作者")
@click.option("--target-file", required=True, help="目标导出文件路径")
@click.option("--format", "export_format", type=click.Choice(["json", "csv"]),
              default="json", help="导出格式")
@click.option("--batch-id", type=int, default=None, help="关联批次ID")
@click.pass_context
def receipt_create(ctx, operator, target_file, export_format, batch_id):
    """创建导出回执"""
    cabinet = ctx.obj["receipt_cabinet"]
    operator = operator or get_current_user()

    result = cabinet.create_receipt(
        operator=operator,
        target_file=target_file,
        export_format=export_format,
        batch_id=batch_id,
    )

    if not result["success"]:
        click.echo(click.style(f"[!!] 创建回执失败", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style(f"[OK] 导出回执已创建: {result['receipt_id']}", fg="green", bold=True))
    click.echo(f"配置哈希: {result['config_hash']}")
    click.echo(f"状态: {result['status']}")
    click.echo(f"目标文件: {result['target_file']}")
    click.echo(f"格式: {result['export_format']}")
    click.echo(f"命中记录数: {result['hit_count']}")
    click.echo(f"导出时间: {result['exported_at']}")


@receipt.command("show")
@click.argument("receipt_id")
@click.option("--operator", default=None, help="操作者")
@click.pass_context
def receipt_show(ctx, receipt_id, operator):
    """查看导出回执详情"""
    cabinet = ctx.obj["receipt_cabinet"]
    operator = operator or get_current_user()

    result = cabinet.read_receipt(receipt_id, operator)

    if not result["success"]:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style(f"=== 导出回执: {receipt_id} ===", fg="cyan", bold=True))
    click.echo(f"状态: {RECEIPT_STATUS_LABELS.get(result['status'], result['status'])}")
    click.echo(f"操作者: {result.get('operator', '-')}")
    click.echo(f"导出时间: {result.get('exported_at', '-')}")
    click.echo(f"创建时间: {result.get('created_at', '-')}")
    click.echo()

    click.echo(click.style("--- 导出信息 ---", fg="yellow"))
    click.echo(f"  目标文件: {result.get('target_file', '-')}")
    click.echo(f"  导出格式: {result.get('export_format', '-')}")
    click.echo(f"  命中记录数: {result.get('hit_count', 0)}")
    click.echo(f"  关联批次: #{result.get('batch_id', '-')}")
    click.echo(f"  导出目录: {result.get('export_dir', '-')}")
    click.echo(f"  工作目录: {result.get('working_dir', '-')}")
    click.echo()

    file_alive = result.get("file_alive", True)
    file_modified = result.get("file_modified", False)
    click.echo(click.style("--- 文件存活状态 ---", fg="yellow"))
    click.echo(f"  文件存在: {'是' if file_alive else '否'}")
    click.echo(f"  文件被修改: {'是' if file_modified else '否'}")
    if result.get("original_file_hash"):
        click.echo(f"  原始哈希: {result['original_file_hash']}")
    if result.get("current_file_hash"):
        click.echo(f"  当前哈希: {result['current_file_hash']}")
    click.echo()

    filter_snapshot = result.get("filter_snapshot", {})
    if filter_snapshot:
        click.echo(click.style("--- 筛选快照 ---", fg="yellow"))
        for k, v in filter_snapshot.items():
            if v is not None and v is not False:
                click.echo(f"  {k}: {v}")
        click.echo()

    summary_stats = result.get("summary_stats", {})
    if summary_stats:
        click.echo(click.style("--- 摘要统计 ---", fg="yellow"))
        for k, v in summary_stats.items():
            if v is not None:
                click.echo(f"  {k}: {v}")
        click.echo()

    subsequent_actions = result.get("subsequent_actions", [])
    if subsequent_actions:
        click.echo(click.style("--- 可执行动作 ---", fg="green"))
        for action in subsequent_actions:
            click.echo(f"  • {action}")
        click.echo()

    if not file_alive:
        click.echo(click.style("⚠ 目标文件已不存在，续导前需重绑目标或另存副本", fg="red", bold=True))
    elif file_modified:
        click.echo(click.style("⚠ 目标文件已被修改，续导可能覆盖变更", fg="yellow", bold=True))


@receipt.command("list")
@click.option("--all", "show_all", is_flag=True, help="包含已放弃的回执")
@click.pass_context
def receipt_list(ctx, show_all):
    """列出导出回执"""
    cabinet = ctx.obj["receipt_cabinet"]

    receipts = cabinet.list_receipts(include_inactive=show_all)

    if not receipts:
        click.echo("暂无导出回执")
        return

    headers = ["回执ID", "状态", "操作人", "目标文件", "格式", "记录数", "导出时间"]
    rows = []
    for r in receipts:
        r_status = RECEIPT_STATUS_LABELS.get(r.get("status", ""), r.get("status", "-"))
        rows.append([
            r.get("receipt_id", "-"),
            r_status,
            r.get("operator") or "-",
            (r.get("target_file") or "")[:40],
            r.get("export_format", "-"),
            r.get("hit_count", 0),
            r.get("exported_at", "-"),
        ])
    print_table(headers, rows)


@receipt.command("compare")
@click.argument("receipt_id")
@click.option("--operator", default=None, help="操作者")
@click.pass_context
def receipt_compare(ctx, receipt_id, operator):
    """回执对比：对比回执中的导出目标与当前状态"""
    cabinet = ctx.obj["receipt_cabinet"]
    operator = operator or get_current_user()

    result = cabinet.compare_receipt(receipt_id, operator)

    if not result["success"]:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    if result["all_match"]:
        click.echo(click.style(f"[OK] 回执 {receipt_id} 与当前状态完全一致", fg="green"))
        return

    click.echo(click.style(
        f"=== 回执对比: {receipt_id} ({result['mismatch_count']} 处差异) ===",
        fg="cyan", bold=True
    ))

    for comp in result["comparisons"]:
        label = comp.get("label", comp["field"])
        match_icon = "✓" if comp.get("match", True) else "✗"
        color = "green" if comp.get("match", True) else "red"
        click.echo(click.style(f"\n  [{label}] {match_icon}", fg=color))

        if comp["field"] == "record_fingerprints":
            click.echo(f"    回执记录数: {comp.get('receipt_count', '-')}")
            click.echo(f"    当前记录数: {comp.get('current_count', '-')}")
            click.echo(f"    新增: {comp.get('added_count', 0)}, 缺失: {comp.get('removed_count', 0)}")
            if comp.get("detail"):
                click.echo(f"    详情: {comp['detail']}")
        elif comp["field"] == "target_file":
            click.echo(f"    文件存在: {'是' if comp.get('file_alive') else '否'}")
            click.echo(f"    文件被修改: {'是' if comp.get('file_modified') else '否'}")
            if comp.get("receipt_hash"):
                click.echo(f"    回执哈希: {comp['receipt_hash']}")
            if comp.get("current_hash"):
                click.echo(f"    当前哈希: {comp['current_hash']}")
        else:
            if comp.get("receipt") is not None:
                click.echo(f"    回执: {json.dumps(comp['receipt'], ensure_ascii=False)[:80]}")
            if comp.get("current") is not None:
                click.echo(f"    当前: {json.dumps(comp['current'], ensure_ascii=False)[:80]}")


@receipt.command("check")
@click.argument("receipt_id")
@click.pass_context
def receipt_check(ctx, receipt_id):
    """检查回执拦截项"""
    cabinet = ctx.obj["receipt_cabinet"]

    result = cabinet.check_interceptions(receipt_id)

    if not result["success"]:
        click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
        sys.exit(1)

    if result["interception_count"] == 0:
        click.echo(click.style(f"[OK] 回执 {receipt_id} 无拦截项，可以安全续导", fg="green"))
        return

    click.echo(click.style(
        f"[!!] 回执 {receipt_id} 存在 {result['interception_count']} 个拦截项:",
        fg="red", bold=True
    ))
    for i in result["interceptions"]:
        severity_icon = "🔴" if i["severity"] == "critical" else ("🟡" if i["severity"] == "high" else "🟠")
        click.echo(click.style(
            f"  {severity_icon} [{i['severity']}] {i['label']}: {i['detail']}",
            fg="red" if i["severity"] == "critical" else "yellow"
        ))

    click.echo()
    if result["can_resume"]:
        click.echo(click.style("可使用 --force 强制续导，或先处理拦截项", fg="yellow"))
    else:
        click.echo(click.style("存在严重拦截项，无法强制续导，请先处理", fg="red", bold=True))

    click.echo()
    click.echo("处理选项:")
    handling = cabinet.get_handling_options(receipt_id)
    for opt in handling.get("handling_options", []):
        avail_icon = "✓" if opt.get("available") else "✗"
        click.echo(f"  {avail_icon} {opt['label']}: {opt['description']}")


@receipt.command("handle")
@click.argument("receipt_id")
@click.argument("action", type=click.Choice(["save_copy", "abandon", "rebind", "cleanup"]))
@click.option("--operator", default=None, help="操作者")
@click.option("--new-export-dir", default=None, help="重绑的新导出目录")
@click.option("--new-target-file", default=None, help="重绑的新目标文件")
@click.pass_context
def receipt_handle(ctx, receipt_id, action, operator, new_export_dir, new_target_file):
    """处理回执拦截项（另存副本/放弃恢复/重绑目标/清理失效）"""
    cabinet = ctx.obj["receipt_cabinet"]
    operator = operator or get_current_user()

    if action == "save_copy":
        result = cabinet.handle_save_copy(receipt_id, operator)
        if result["success"]:
            click.echo(click.style(f"[OK] 已另存副本: {result['new_receipt_id']}", fg="green"))
            click.echo(f"新目标: {result['new_target']}")
        else:
            click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
            sys.exit(1)

    elif action == "abandon":
        result = cabinet.handle_abandon(receipt_id, operator)
        if result["success"]:
            click.echo(click.style(f"[OK] 已放弃恢复: {receipt_id}", fg="yellow"))
        else:
            click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
            sys.exit(1)

    elif action == "rebind":
        if not new_export_dir and not new_target_file:
            click.echo(click.style("请指定 --new-export-dir 或 --new-target-file", fg="red"), err=True)
            sys.exit(1)
        result = cabinet.handle_rebind(receipt_id, operator,
                                       new_export_dir=new_export_dir,
                                       new_target_file=new_target_file)
        if result["success"]:
            click.echo(click.style(f"[OK] 已重绑目标: {receipt_id}", fg="green"))
            for k, v in result.get("updates", {}).items():
                click.echo(f"  {k}: {v}")
        else:
            click.echo(click.style(f"[!!] {result['message']}", fg="red"), err=True)
            sys.exit(1)

    elif action == "cleanup":
        result = cabinet.handle_cleanup(operator=operator)
        if result["removed_count"] > 0:
            click.echo(click.style(f"[OK] 已清理 {result['removed_count']} 个失效回执", fg="green"))
            for detail in result.get("invalid_details", []):
                reasons = ", ".join(detail["reasons"])
                click.echo(f"  • {detail['receipt_id']}: {reasons}")
        else:
            click.echo(click.style("[OK] 无失效回执", fg="green"))


@receipt.command("resume")
@click.argument("receipt_id")
@click.option("--operator", default=None, help="操作者")
@click.option("--force", is_flag=True, default=False, help="强制续导（忽略非严重拦截项）")
@click.pass_context
def receipt_resume(ctx, receipt_id, operator, force):
    """基于回执续导出"""
    cabinet = ctx.obj["receipt_cabinet"]
    operator = operator or get_current_user()

    result = cabinet.resume_with_receipt(receipt_id, operator, force=force)

    if not result["success"]:
        if result.get("interceptions"):
            click.echo(click.style(
                f"[!!] 存在 {len(result['interceptions'])} 个拦截项，无法续导:",
                fg="red", bold=True
            ))
            for i in result["interceptions"]:
                click.echo(click.style(
                    f"  • [{i['severity']}] {i['label']}: {i['detail']}",
                    fg="red" if i["severity"] == "critical" else "yellow"
                ))
            if result.get("can_force"):
                click.echo(click.style("使用 --force 强制续导（仅非严重拦截项）", fg="yellow"))
            else:
                click.echo(click.style("存在严重拦截项，请先处理: receipt handle", fg="red"))
        else:
            click.echo(click.style(f"[!!] {result.get('message', '续导失败')}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style(f"[OK] 续导完成: {receipt_id}", fg="green", bold=True))
    if result.get("forced"):
        click.echo(click.style("  (已强制忽略拦截项)", fg="yellow"))

    export_result = result.get("export_result")
    if export_result:
        if export_result.get("success"):
            click.echo(click.style("--- 导出结果 ---", fg="green"))
            click.echo(f"  文件路径: {export_result.get('file_path', '-')}")
            click.echo(f"  格式: {export_result.get('format', '-')}")
            if export_result.get("total_changes") is not None:
                click.echo(f"  记录数: {export_result['total_changes']}")
        else:
            click.echo(click.style("--- 导出结果 ---", fg="yellow"))
            click.echo(f"  状态: 部分成功")
            click.echo(f"  信息: {export_result.get('message', '未知')}")


@receipt.command("timeline")
@click.argument("receipt_id")
@click.option("--limit", type=int, default=20, help="显示条数")
@click.pass_context
def receipt_timeline(ctx, receipt_id, limit):
    """查看回执操作时间线"""
    cabinet = ctx.obj["receipt_cabinet"]

    events = cabinet.get_timeline(receipt_id, limit=limit)

    if not events:
        click.echo("暂无操作记录")
        return

    click.echo(click.style(f"=== 回执时间线: {receipt_id} ===", fg="cyan", bold=True))
    for evt in events:
        status_icon = "✓" if evt["status"] == "success" else ("⚠" if evt["status"] == "partial" else "✗")
        color = "green" if evt["status"] == "success" else ("yellow" if evt["status"] == "partial" else "red")
        label = evt.get("event_label") or RECEIPT_EVENT_LABELS.get(evt["event_type"], evt["event_type"])
        click.echo(click.style(
            f"  [{evt['created_at']}] {status_icon} {label}",
            fg=color
        ))
        if evt.get("operator"):
            click.echo(f"     操作人: {evt['operator']}")
        if evt.get("result_summary"):
            click.echo(f"     结果: {evt['result_summary']}")
        if evt.get("error_message"):
            click.echo(click.style(f"     错误: {evt['error_message']}", fg="red"))
        if evt.get("event_details") and isinstance(evt["event_details"], dict):
            detail_str = ", ".join(f"{k}={v}" for k, v in list(evt["event_details"].items())[:5])
            click.echo(f"     详情: {detail_str}")


if __name__ == "__main__":
    cli()
