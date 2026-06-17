import os
import json
import csv
from datetime import datetime
from typing import List, Dict, Optional
from openpyxl import Workbook
from .config import Config
from .database import (
    Database,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_PENDING,
    MATCH_STATUS_EXCEPTION,
    MATCH_STATUS_UNMATCHED,
    MATCH_STATUS_REVOKED,
)
from .matcher import (
    MATCH_TYPE_AUTO_EXACT,
    MATCH_TYPE_AUTO_FUZZY,
    MATCH_TYPE_AUTO_MULTI_CANDIDATE,
    MATCH_TYPE_MANUAL,
)

CONFLICT_TYPE_LABELS = {
    "new_record": "新增记录",
    "status_change": "状态冲突",
    "duplicate_process": "重复处理",
    "amount_change": "金额变更",
}


STATUS_LABELS = {
    MATCH_STATUS_MATCHED: "已匹配",
    MATCH_STATUS_PENDING: "待确认",
    MATCH_STATUS_EXCEPTION: "异常",
    MATCH_STATUS_UNMATCHED: "未匹配",
    MATCH_STATUS_REVOKED: "已撤销",
}

MATCH_TYPE_LABELS = {
    MATCH_TYPE_AUTO_EXACT: "自动精确匹配",
    MATCH_TYPE_AUTO_FUZZY: "自动模糊匹配",
    MATCH_TYPE_AUTO_MULTI_CANDIDATE: "多候选匹配",
    MATCH_TYPE_MANUAL: "人工匹配",
}


class ReportExporter:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        os.makedirs(config.export_dir, exist_ok=True)

    def _get_lock_info(self, match_id: int) -> Dict:
        lock = self.db.get_match_lock(match_id)
        if not lock:
            return {
                "当前责任人": "",
                "锁定原因": "",
                "锁定时间": "",
                "锁到期时间": "",
                "是否锁定": "否",
            }
        is_expired = "否"
        if lock.get("lock_expires_at"):
            try:
                expire_time = datetime.strptime(lock["lock_expires_at"], "%Y-%m-%d %H:%M:%S")
                if expire_time <= datetime.now():
                    is_expired = "是"
            except (ValueError, TypeError):
                pass
        return {
            "当前责任人": lock["lock_owner"],
            "锁定原因": lock.get("lock_reason") or "",
            "锁定时间": lock.get("locked_at") or "",
            "锁到期时间": lock.get("lock_expires_at") or "",
            "是否锁定": "是（已过期）" if is_expired == "是" else "是",
        }

    def _get_lock_history_and_evidence(self, match_id: int, match: Dict) -> Dict:
        lock_history = self.db.get_lock_history(match_id=match_id)
        from .workflow import LOCK_STATUS_LABELS
        lock_history_str = "; ".join([
            f"{lh['created_at']}: {LOCK_STATUS_LABELS.get(lh['action'], lh['action'])} "
            f"(操作人: {lh['operator']}, 原持有人: {lh['old_owner'] or '-'}, "
            f"新持有人: {lh['new_owner'] or '-'}, 原因: {lh['reason'] or '-'})"
            for lh in lock_history
        ])

        takeover_reason = ""
        for lh in lock_history:
            if lh.get("action") == "takeover" and lh.get("reason"):
                takeover_reason = lh["reason"]
                break

        last_confirm_evidence = ""
        if match["status"] == MATCH_STATUS_MATCHED:
            last_confirm_evidence = (
                f"确认人: {match.get('operator', '-')}, "
                f"备注: {match.get('operator_remark', '-')}, "
                f"确认时间: {match.get('confirmed_at', '-')}, "
                f"匹配类型: {MATCH_TYPE_LABELS.get(match.get('match_type'), match.get('match_type', '-'))}, "
                f"得分: {match.get('match_score', '-')}, "
                f"证据: {match.get('match_evidence', '-')}"
            )

        return {
            "锁历史": lock_history_str,
            "接管原因": takeover_reason,
            "最后确认证据": last_confirm_evidence,
        }

    def _generate_summary(self) -> List[Dict]:
        stats = self.db.get_statistics()
        config_info = self.config.to_dict()

        return [
            {"项目": "发票总数", "值": stats["total_invoices"], "备注": ""},
            {"项目": "收款总数", "值": stats["total_payments"], "备注": ""},
            {"项目": "已匹配发票", "值": stats["matched_invoices"], "备注": ""},
            {"项目": "已匹配收款", "值": stats["matched_payments"], "备注": ""},
            {"项目": "未匹配发票", "值": stats["unmatched_invoices"], "备注": ""},
            {"项目": "未匹配收款", "值": stats["unmatched_payments"], "备注": ""},
            {"项目": "待确认匹配", "值": stats["pending_matches"], "备注": ""},
            {"项目": "已确认匹配", "值": stats["confirmed_matches"], "备注": ""},
            {"项目": "异常匹配", "值": stats["exception_matches"], "备注": ""},
            {"项目": "已撤销匹配", "值": stats["revoked_matches"], "备注": ""},
            {"项目": "无效发票", "值": stats["invalid_invoices"], "备注": "数据验证失败的记录"},
            {"项目": "无效收款", "值": stats["invalid_payments"], "备注": "数据验证失败的记录"},
            {"项目": "导入错误总数", "值": stats["total_errors"], "备注": ""},
            {"项目": "金额容差", "值": config_info["amount_tolerance"], "备注": "匹配配置"},
            {"项目": "日期窗口(天)", "值": config_info["date_window_days"], "备注": "匹配配置"},
            {"项目": "导出时间", "值": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "备注": ""},
        ]

    def _generate_matched_data(self) -> List[Dict]:
        matches = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
        result = []
        for m in matches:
            history = self.db.get_status_history(match_id=m["id"])
            history_str = "; ".join([
                f"{h['changed_at']}: {STATUS_LABELS.get(h['old_status'], h['old_status'])} -> "
                f"{STATUS_LABELS.get(h['new_status'], h['new_status'])} "
                f"(操作人: {h['operator'] or '系统'}, 备注: {h['remark'] or '-'})"
                for h in history
            ])

            lock_info = self._get_lock_info(m["id"])
            lock_extra = self._get_lock_history_and_evidence(m["id"], m)

            item = {
                "匹配编号": m["match_no"],
                "匹配类型": MATCH_TYPE_LABELS.get(m["match_type"], m["match_type"]),
                "匹配状态": STATUS_LABELS.get(m["status"], m["status"]),
                "匹配得分": m["match_score"],
                "匹配证据": m["match_evidence"],
                "发票号": m["invoice_no"],
                "发票日期": m["invoice_date"],
                "发票客户": m["inv_customer"],
                "发票金额": m["inv_amount"],
                "发票行号": m.get("inv_row_num", ""),
                "发票文件": m.get("inv_file", ""),
                "收款号": m["payment_no"],
                "收款日期": m["payment_date"],
                "收款客户": m["pay_customer"],
                "收款金额": m["pay_amount"],
                "收款行号": m.get("pay_row_num", ""),
                "收款文件": m.get("pay_file", ""),
                "金额差异": abs(m["inv_amount"] - m["pay_amount"]),
                "确认人": m["operator"],
                "人工备注": m["operator_remark"] or "",
                "确认时间": m["confirmed_at"],
                "创建时间": m["created_at"],
                "状态历史": history_str,
            }
            item.update(lock_info)
            item.update(lock_extra)
            result.append(item)
        return result

    def _generate_pending_data(self) -> List[Dict]:
        matches = self.db.get_matches_by_status(MATCH_STATUS_PENDING)
        result = []
        for m in matches:
            lock_info = self._get_lock_info(m["id"])
            lock_extra = self._get_lock_history_and_evidence(m["id"], m)
            item = {
                "匹配编号": m["match_no"],
                "匹配类型": MATCH_TYPE_LABELS.get(m["match_type"], m["match_type"]),
                "匹配状态": STATUS_LABELS.get(m["status"], m["status"]),
                "匹配得分": m["match_score"],
                "匹配证据": m["match_evidence"],
                "发票号": m["invoice_no"],
                "发票日期": m["invoice_date"],
                "发票客户": m["inv_customer"],
                "发票金额": m["inv_amount"],
                "发票行号": m.get("inv_row_num", ""),
                "收款号": m["payment_no"],
                "收款日期": m["payment_date"],
                "收款客户": m["pay_customer"],
                "收款金额": m["pay_amount"],
                "收款行号": m.get("pay_row_num", ""),
                "金额差异": abs(m["inv_amount"] - m["pay_amount"]),
                "当前备注": m["operator_remark"] or "",
                "创建时间": m["created_at"],
            }
            item.update(lock_info)
            item.update(lock_extra)
            result.append(item)
        return result

    def _generate_exception_data(self) -> List[Dict]:
        matches = self.db.get_matches_by_status(MATCH_STATUS_EXCEPTION)
        result = []
        for m in matches:
            history = self.db.get_status_history(match_id=m["id"])
            history_str = "; ".join([
                f"{h['changed_at']}: {STATUS_LABELS.get(h['old_status'], h['old_status'])} -> "
                f"{STATUS_LABELS.get(h['new_status'], h['new_status'])} "
                f"(操作人: {h['operator'] or '系统'}, 备注: {h['remark'] or '-'})"
                for h in history
            ])

            lock_info = self._get_lock_info(m["id"])
            lock_extra = self._get_lock_history_and_evidence(m["id"], m)

            item = {
                "匹配编号": m["match_no"],
                "匹配类型": MATCH_TYPE_LABELS.get(m["match_type"], m["match_type"]),
                "匹配状态": STATUS_LABELS.get(m["status"], m["status"]),
                "发票号": m["invoice_no"],
                "发票日期": m["invoice_date"],
                "发票客户": m["inv_customer"],
                "发票金额": m["inv_amount"],
                "收款号": m["payment_no"],
                "收款日期": m["payment_date"],
                "收款客户": m["pay_customer"],
                "收款金额": m["pay_amount"],
                "处理人": m["operator"],
                "拒绝原因": m["operator_remark"] or "",
                "处理时间": m["confirmed_at"],
                "状态历史": history_str,
            }
            item.update(lock_info)
            item.update(lock_extra)
            result.append(item)
        return result

    def _generate_unmatched_invoices(self) -> List[Dict]:
        invoices = self.db.get_unmatched_invoices()
        result = []
        for inv in invoices:
            result.append({
                "发票ID": inv["id"],
                "发票号": inv["invoice_no"],
                "发票日期": inv["invoice_date"],
                "客户": inv["customer"],
                "发票金额": inv["amount"],
                "发票状态": inv["status"],
                "匹配状态": STATUS_LABELS.get(inv["match_status"], inv["match_status"]),
                "文件行号": inv["file_row_num"],
                "来源文件": inv.get("file_name", ""),
                "导入时间": inv.get("batch_imported_at", ""),
                "错误信息": inv.get("error_message", ""),
            })
        return result

    def _generate_unmatched_payments(self) -> List[Dict]:
        payments = self.db.get_unmatched_payments()
        result = []
        for pay in payments:
            result.append({
                "收款ID": pay["id"],
                "收款号": pay["payment_no"],
                "收款日期": pay["payment_date"],
                "客户": pay["customer"],
                "收款金额": pay["amount"],
                "收款状态": pay["status"],
                "匹配状态": STATUS_LABELS.get(pay["match_status"], pay["match_status"]),
                "文件行号": pay["file_row_num"],
                "来源文件": pay.get("file_name", ""),
                "导入时间": pay.get("batch_imported_at", ""),
                "错误信息": pay.get("error_message", ""),
            })
        return result

    def _generate_revoked_data(self) -> List[Dict]:
        matches = self.db.get_matches_by_status(MATCH_STATUS_REVOKED)
        result = []
        for m in matches:
            history = self.db.get_status_history(match_id=m["id"])
            history_str = "; ".join([
                f"{h['changed_at']}: {STATUS_LABELS.get(h['old_status'], h['old_status'])} -> "
                f"{STATUS_LABELS.get(h['new_status'], h['new_status'])} "
                f"(操作人: {h['operator'] or '系统'}, 备注: {h['remark'] or '-'})"
                for h in history
            ])

            lock_info = self._get_lock_info(m["id"])
            lock_extra = self._get_lock_history_and_evidence(m["id"], m)

            item = {
                "匹配编号": m["match_no"],
                "匹配类型": MATCH_TYPE_LABELS.get(m["match_type"], m["match_type"]),
                "匹配状态": STATUS_LABELS.get(m["status"], m["status"]),
                "发票号": m["invoice_no"],
                "发票金额": m["inv_amount"],
                "收款号": m["payment_no"],
                "收款金额": m["pay_amount"],
                "撤销备注": m["operator_remark"] or "",
                "状态历史": history_str,
            }
            item.update(lock_info)
            item.update(lock_extra)
            result.append(item)
        return result

    def _generate_errors_data(self) -> List[Dict]:
        errors = self.db.get_errors()
        batches = {b["id"]: b for b in self.db.get_batches()}

        result = []
        for err in errors:
            batch = batches.get(err["batch_id"], {})
            result.append({
                "错误ID": err["id"],
                "批次ID": err["batch_id"],
                "文件类型": "发票" if err["file_type"] == "invoice" else "收款",
                "文件名": batch.get("file_name", ""),
                "文件行号": err["file_row_num"],
                "错误类型": err["error_type"],
                "错误信息": err["error_message"],
                "原始数据": err["raw_data"] or "",
                "发生时间": err["created_at"],
            })
        return result

    def _generate_history_data(self) -> List[Dict]:
        history = self.db.get_status_history()
        result = []
        for h in history:
            result.append({
                "历史ID": h["id"],
                "匹配ID": h["match_id"] or "",
                "发票ID": h["invoice_id"] or "",
                "收款ID": h["payment_id"] or "",
                "原状态": STATUS_LABELS.get(h["old_status"], h["old_status"]),
                "新状态": STATUS_LABELS.get(h["new_status"], h["new_status"]),
                "操作人": h["operator"] or "系统",
                "备注": h["remark"] or "",
                "变更时间": h["changed_at"],
            })
        return result

    def _generate_conflicts_data(self, batch_id: int = None) -> List[Dict]:
        conflicts = self.db.get_batch_conflicts(batch_id=batch_id)
        result = []
        for c in conflicts:
            result.append({
                "冲突ID": c["id"],
                "批次ID": c["batch_id"],
                "来源文件": c["file_name"],
                "冲突类型": CONFLICT_TYPE_LABELS.get(c["conflict_type"], c["conflict_type"]),
                "记录类型": "发票" if c["record_type"] == "invoice" else "收款",
                "记录编号": c["record_no"],
                "原状态": STATUS_LABELS.get(c["old_status"], c["old_status"]) if c["old_status"] else "-",
                "新状态": STATUS_LABELS.get(c["new_status"], c["new_status"]) if c["new_status"] else "-",
                "原操作人": c["old_operator"] or "-",
                "新操作人": c["new_operator"] or "-",
                "原金额": f"{c['old_amount']:.2f}" if c["old_amount"] is not None else "-",
                "新金额": f"{c['new_amount']:.2f}" if c["new_amount"] is not None else "-",
                "冲突原因": c["conflict_reason"],
                "检测时间": c["detected_at"],
            })
        return result

    def _export_xlsx(self, base_name: str, sheets_data: Dict[str, List[Dict]]) -> str:
        file_path = os.path.join(self.config.export_dir, f"{base_name}.xlsx")

        wb = Workbook()
        first_sheet = True

        for sheet_name, data in sheets_data.items():
            if first_sheet:
                ws = wb.active
                ws.title = sheet_name
                first_sheet = False
            else:
                ws = wb.create_sheet(title=sheet_name)

            if data:
                headers = list(data[0].keys())
                ws.append(headers)
                for row in data:
                    ws.append([str(v) if v is not None else "" for v in row.values()])
            else:
                ws.append(["提示"])
                ws.append(["无数据"])

        wb.save(file_path)
        return file_path

    def _export_csv(self, base_name: str, sheets_data: Dict[str, List[Dict]]) -> str:
        dir_path = os.path.join(self.config.export_dir, base_name)
        os.makedirs(dir_path, exist_ok=True)

        for sheet_name, data in sheets_data.items():
            file_name = f"{sheet_name}.csv"
            file_path = os.path.join(dir_path, file_name)

            if data:
                with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=data[0].keys())
                    writer.writeheader()
                    writer.writerows(data)
            else:
                with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
                    f.write("提示\n无数据\n")

        return dir_path

    def _export_json(self, base_name: str, data: Dict) -> str:
        file_path = os.path.join(self.config.export_dir, f"{base_name}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        return file_path

    def export_full_report(self, operator: str = None, format: str = None) -> Dict:
        export_format = format or self.config.export_format
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"reconciliation_report_{timestamp}"

        summary = self._generate_summary()
        matched_data = self._generate_matched_data()
        pending_data = self._generate_pending_data()
        exception_data = self._generate_exception_data()
        unmatched_invoices_data = self._generate_unmatched_invoices()
        unmatched_payments_data = self._generate_unmatched_payments()
        revoked_data = self._generate_revoked_data()
        errors_data = self._generate_errors_data()
        history_data = self._generate_history_data()
        conflicts_data = self._generate_conflicts_data()

        export_data = {
            "概览": summary,
            "已匹配": matched_data,
            "待确认": pending_data,
            "异常": exception_data,
            "未匹配发票": unmatched_invoices_data,
            "未匹配收款": unmatched_payments_data,
            "已撤销": revoked_data,
            "导入错误": errors_data,
            "状态历史": history_data,
            "批次冲突": conflicts_data,
        }

        if export_format == "xlsx":
            file_path = self._export_xlsx(base_name, export_data)
        elif export_format == "csv":
            file_path = self._export_csv(base_name, export_data)
        elif export_format == "json":
            file_path = self._export_json(base_name, export_data)
        else:
            raise ValueError(f"不支持的导出格式: {export_format}")

        return {
            "success": True,
            "file_path": file_path,
            "format": export_format,
            "generated_at": datetime.now().isoformat(),
            "operator": operator,
            "summary": {
                "matched_count": len(matched_data),
                "pending_count": len(pending_data),
                "exception_count": len(exception_data),
                "unmatched_invoices_count": len(unmatched_invoices_data),
                "unmatched_payments_count": len(unmatched_payments_data),
                "revoked_count": len(revoked_data),
                "errors_count": len(errors_data),
                "conflicts_count": len(conflicts_data),
            }
        }

    def export_batch_progress(self, batch_id: int, operator: str = None, format: str = None) -> Dict:
        from .batch_workbench import BatchWorkbench
        workbench = BatchWorkbench(self.config, self.db)

        export_format = format or self.config.export_format
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"batch_progress_{batch_id}_{timestamp}"

        progress_result = workbench.export_batch_progress(batch_id, operator)
        if not progress_result["success"]:
            return progress_result

        data = progress_result["data"]
        batch_info = data["batch_info"]
        progress = data["progress"]
        raw_matches = data["matches"]
        raw_conflicts = data["conflicts"]
        raw_unmatched_inv = data.get("unmatched_invoices", [])
        raw_unmatched_pay = data.get("unmatched_payments", [])

        matches = []
        for m in raw_matches:
            matches.append({
                "匹配ID": m.get("match_id", ""),
                "匹配编号": m.get("match_no", ""),
                "匹配类型": m.get("match_type", ""),
                "状态": m.get("match_status", ""),
                "发票号": m.get("invoice_no", ""),
                "发票金额": f"{m.get('invoice_amount', 0):.2f}" if m.get("invoice_amount") is not None else "-",
                "收款号": m.get("payment_no", ""),
                "收款金额": f"{m.get('payment_amount', 0):.2f}" if m.get("payment_amount") is not None else "-",
                "匹配方式": m.get("match_evidence", ""),
                "处理人": m.get("operator", ""),
                "操作时间": m.get("confirmed_at", "") or m.get("created_at", ""),
            })

        conflicts = []
        for c in raw_conflicts:
            old_amount = c.get("old_amount")
            new_amount = c.get("new_amount")
            try:
                old_amount_str = f"{float(old_amount):.2f}" if old_amount is not None and old_amount != "-" else "-"
            except (ValueError, TypeError):
                old_amount_str = str(old_amount) if old_amount is not None else "-"
            try:
                new_amount_str = f"{float(new_amount):.2f}" if new_amount is not None and new_amount != "-" else "-"
            except (ValueError, TypeError):
                new_amount_str = str(new_amount) if new_amount is not None else "-"

            conflicts.append({
                "冲突ID": c.get("conflict_id", ""),
                "批次ID": batch_id,
                "来源文件": batch_info.get("file_name", ""),
                "冲突类型": c.get("conflict_type", ""),
                "记录类型": c.get("record_type", ""),
                "记录编号": c.get("record_no", ""),
                "原状态": c.get("old_status", "-"),
                "新状态": c.get("new_status", "-"),
                "原操作人": c.get("old_operator", "-"),
                "新操作人": c.get("new_operator", "-"),
                "原金额": old_amount_str,
                "新金额": new_amount_str,
                "冲突原因": c.get("conflict_reason", ""),
                "检测时间": c.get("detected_at", ""),
            })

        batch_summary = [
            {"项目": "批次ID", "值": batch_info["batch_id"], "备注": ""},
            {"项目": "文件类型", "值": batch_info["file_type"], "备注": ""},
            {"项目": "文件名", "值": batch_info["file_name"], "备注": ""},
            {"项目": "导入操作人", "值": batch_info["operator"], "备注": ""},
            {"项目": "导入时间", "值": batch_info["imported_at"], "备注": ""},
            {"项目": "总行数", "值": batch_info["total_rows"], "备注": ""},
            {"项目": "成功行数", "值": batch_info["success_rows"], "备注": ""},
            {"项目": "失败行数", "值": batch_info["failed_rows"], "备注": ""},
            {"项目": "进度百分比", "值": f"{progress['progress_percent']:.2f}%", "备注": ""},
            {"项目": "待确认匹配", "值": progress["pending_matches"], "备注": ""},
            {"项目": "已确认匹配", "值": progress["confirmed_matches"], "备注": ""},
            {"项目": "异常匹配", "值": progress["exception_matches"], "备注": ""},
            {"项目": "已撤销匹配", "值": progress["revoked_matches"], "备注": ""},
            {"项目": "未匹配发票", "值": progress["unmatched_invoices"], "备注": ""},
            {"项目": "未匹配收款", "值": progress["unmatched_payments"], "备注": ""},
            {"项目": "冲突数量", "值": progress["conflict_count"], "备注": ""},
            {"项目": "导出时间", "值": progress["exported_at"], "备注": ""},
            {"项目": "导出人", "值": progress["exported_by"], "备注": ""},
        ]

        export_data = {
            "批次摘要": batch_summary,
            "匹配明细": matches,
            "批次冲突": conflicts,
            "未匹配发票": [],
            "未匹配收款": [],
        }

        for inv in raw_unmatched_inv:
            export_data["未匹配发票"].append({
                "发票ID": inv.get("invoice_id", ""),
                "发票号": inv.get("invoice_no", ""),
                "发票日期": inv.get("invoice_date", ""),
                "客户": inv.get("customer", ""),
                "发票金额": f"{inv.get('amount', 0):.2f}" if inv.get("amount") is not None else "-",
                "发票状态": inv.get("status", ""),
                "匹配状态": inv.get("match_status", ""),
                "文件行号": inv.get("file_row_num", ""),
                "来源文件": inv.get("file_name", ""),
                "导入时间": inv.get("batch_imported_at", ""),
            })

        for pay in raw_unmatched_pay:
            export_data["未匹配收款"].append({
                "收款ID": pay.get("payment_id", ""),
                "收款号": pay.get("payment_no", ""),
                "收款日期": pay.get("payment_date", ""),
                "客户": pay.get("customer", ""),
                "收款金额": f"{pay.get('amount', 0):.2f}" if pay.get("amount") is not None else "-",
                "收款状态": pay.get("status", ""),
                "匹配状态": pay.get("match_status", ""),
                "文件行号": pay.get("file_row_num", ""),
                "来源文件": pay.get("file_name", ""),
                "导入时间": pay.get("batch_imported_at", ""),
            })

        if export_format == "xlsx":
            file_path = self._export_xlsx(base_name, export_data)
        elif export_format == "csv":
            file_path = self._export_csv(base_name, export_data)
        elif export_format == "json":
            file_path = self._export_json(base_name, {
                "batch_info": batch_info,
                "progress": progress,
                "matches": matches,
                "conflicts": conflicts,
                "unmatched_invoices": export_data["未匹配发票"],
                "unmatched_payments": export_data["未匹配收款"],
            })
        else:
            raise ValueError(f"不支持的导出格式: {export_format}")

        return {
            "success": True,
            "file_path": file_path,
            "format": export_format,
            "generated_at": datetime.now().isoformat(),
            "operator": operator,
            "batch_id": batch_id,
            "summary": progress_result["summary"],
        }

    def export_diff_report(self, operator: str = None, format: str = None) -> Dict:
        export_format = format or self.config.export_format
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"diff_report_{timestamp}"

        summary = self._generate_summary()
        unmatched_invoices_data = self._generate_unmatched_invoices()
        unmatched_payments_data = self._generate_unmatched_payments()
        exception_data = self._generate_exception_data()
        errors_data = self._generate_errors_data()

        inv_amount = sum(r["发票金额"] for r in unmatched_invoices_data)
        pay_amount = sum(r["收款金额"] for r in unmatched_payments_data)
        diff_amount = inv_amount - pay_amount

        diff_summary = [
            {
                "项目": "未匹配发票数量",
                "值": len(unmatched_invoices_data),
                "备注": ""
            },
            {
                "项目": "未匹配收款数量",
                "值": len(unmatched_payments_data),
                "备注": ""
            },
            {
                "项目": "未匹配发票总金额",
                "值": inv_amount,
                "备注": ""
            },
            {
                "项目": "未匹配收款总金额",
                "值": pay_amount,
                "备注": ""
            },
            {
                "项目": "差异金额",
                "值": diff_amount,
                "备注": "正数表示发票多，负数表示收款多"
            },
            {
                "项目": "异常匹配数量",
                "值": len(exception_data),
                "备注": ""
            },
            {
                "项目": "导入错误数量",
                "值": len(errors_data),
                "备注": ""
            },
        ]

        export_data = {
            "差异概览": diff_summary,
            "统计概览": summary,
            "未匹配发票": unmatched_invoices_data,
            "未匹配收款": unmatched_payments_data,
            "异常匹配": exception_data,
            "导入错误": errors_data,
        }

        if export_format == "xlsx":
            file_path = self._export_xlsx(base_name, export_data)
        elif export_format == "csv":
            file_path = self._export_csv(base_name, export_data)
        elif export_format == "json":
            file_path = self._export_json(base_name, export_data)
        else:
            raise ValueError(f"不支持的导出格式: {export_format}")

        return {
            "success": True,
            "file_path": file_path,
            "format": export_format,
            "generated_at": datetime.now().isoformat(),
            "operator": operator,
            "diff_amount": diff_amount,
            "unmatched_invoice_amount": inv_amount,
            "unmatched_payment_amount": pay_amount,
        }

    def export_snapshot(self, snapshot_data: Dict, operator: str = None,
                        format: str = None) -> Dict:
        export_format = format or self.config.export_format
        snapshot_info = snapshot_data["snapshot_info"]
        items = snapshot_data["items"]
        snapshot_no = snapshot_info["快照编号"]

        base_name = f"snapshot_{snapshot_no}"

        export_data = {
            "快照信息": [snapshot_info],
            "匹配明细": items,
        }

        if export_format == "xlsx":
            file_path = self._export_xlsx(base_name, export_data)
        elif export_format == "csv":
            file_path = self._export_csv(base_name, export_data)
        elif export_format == "json":
            file_path = self._export_json(base_name, {
                "snapshot_info": snapshot_info,
                "items": items,
            })
        else:
            raise ValueError(f"不支持的导出格式: {export_format}")

        return {
            "success": True,
            "snapshot_no": snapshot_no,
            "file_path": file_path,
            "format": export_format,
            "generated_at": datetime.now().isoformat(),
            "operator": operator,
            "item_count": len(items),
        }
