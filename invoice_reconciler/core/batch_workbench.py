from typing import List, Dict, Optional, Any
from datetime import datetime
from .config import Config
from .database import (
    Database,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_PENDING,
    MATCH_STATUS_EXCEPTION,
    MATCH_STATUS_REVOKED,
    MATCH_STATUS_UNMATCHED,
)


CONFLICT_TYPE_NEW_RECORD = "new_record"
CONFLICT_TYPE_STATUS_CHANGE = "status_change"
CONFLICT_TYPE_DUPLICATE_PROCESS = "duplicate_process"
CONFLICT_TYPE_AMOUNT_CHANGE = "amount_change"

CONFLICT_TYPE_LABELS = {
    CONFLICT_TYPE_NEW_RECORD: "新增记录",
    CONFLICT_TYPE_STATUS_CHANGE: "状态冲突",
    CONFLICT_TYPE_DUPLICATE_PROCESS: "重复处理",
    CONFLICT_TYPE_AMOUNT_CHANGE: "金额变更",
}

SESSION_KEY_LAST_BATCH = "last_selected_batch"
SESSION_KEY_FILTER_OPERATOR = "filter_operator"
SESSION_KEY_FILTER_STATUS = "filter_status"
SESSION_KEY_LAST_ACCESS_TIME = "last_access_time"


class BatchWorkbench:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    def get_batch_workbench_summary(self, batch_id: int = None) -> Dict:
        batch_summaries = self.db.get_batch_summary(batch_id)
        if not batch_summaries:
            return {
                "success": False,
                "message": "暂无批次数据",
                "batches": []
            }

        result = {
            "success": True,
            "total_batches": len(batch_summaries),
            "batches": []
        }

        for summary in batch_summaries:
            batch_id = summary["batch_id"]
            conflicts = self.db.get_batch_conflicts(batch_id=batch_id)

            total_processed = (
                (summary["confirmed_matches"] or 0) +
                (summary["exception_matches"] or 0) +
                (summary["revoked_matches"] or 0)
            )
            total_tasks = (
                (summary["pending_matches"] or 0) +
                (summary["confirmed_matches"] or 0) +
                (summary["exception_matches"] or 0) +
                (summary["revoked_matches"] or 0) +
                (summary["unmatched_invoices"] or 0) +
                (summary["unmatched_payments"] or 0)
            )
            progress = (total_processed / total_tasks * 100) if total_tasks > 0 else 100.0

            batch_detail = {
                "batch_id": batch_id,
                "file_type": "发票" if summary["file_type"] == "invoice" else "收款",
                "file_name": summary["file_name"],
                "operator": summary["operator"] or "-",
                "imported_at": summary["imported_at"],
                "total_rows": summary["total_rows"],
                "success_rows": summary["success_rows"],
                "failed_rows": summary["failed_rows"],
                "matched_invoices": summary["matched_invoices"] or 0,
                "unmatched_invoices": summary["unmatched_invoices"] or 0,
                "matched_payments": summary["matched_payments"] or 0,
                "unmatched_payments": summary["unmatched_payments"] or 0,
                "pending_matches": summary["pending_matches"] or 0,
                "confirmed_matches": summary["confirmed_matches"] or 0,
                "exception_matches": summary["exception_matches"] or 0,
                "revoked_matches": summary["revoked_matches"] or 0,
                "conflict_count": summary["conflict_count"] or 0,
                "conflicts": conflicts,
                "total_tasks": total_tasks,
                "total_processed": total_processed,
                "progress_percent": round(progress, 2),
            }

            unfinished = []
            if summary["pending_matches"] or 0 > 0:
                unfinished.append(f"{summary['pending_matches']} 条待确认")
            if summary["unmatched_invoices"] or 0 > 0:
                unfinished.append(f"{summary['unmatched_invoices']} 条未匹配发票")
            if summary["unmatched_payments"] or 0 > 0:
                unfinished.append(f"{summary['unmatched_payments']} 条未匹配收款")
            if summary["conflict_count"] or 0 > 0:
                unfinished.append(f"{summary['conflict_count']} 个冲突待处理")

            batch_detail["unfinished_items"] = unfinished
            batch_detail["has_unfinished"] = len(unfinished) > 0

            result["batches"].append(batch_detail)

        return result

    def get_batch_matches(self, batch_id: int, status: str = None,
                          operator: str = None) -> List[Dict]:
        return self.db.get_matches_by_batch(batch_id, status=status, operator=operator)

    def get_unfinished_reminder(self, batch_id: int = None) -> Dict:
        summary = self.get_batch_workbench_summary(batch_id)
        if not summary["success"]:
            return summary

        reminders = []
        total_unfinished = 0

        for batch in summary["batches"]:
            if batch["has_unfinished"]:
                total_unfinished += batch["pending_matches"]
                total_unfinished += batch["unmatched_invoices"]
                total_unfinished += batch["unmatched_payments"]
                total_unfinished += batch["conflict_count"]

                reminders.append({
                    "batch_id": batch["batch_id"],
                    "file_name": batch["file_name"],
                    "file_type": batch["file_type"],
                    "unfinished_items": batch["unfinished_items"],
                    "pending_matches": batch["pending_matches"],
                    "unmatched_invoices": batch["unmatched_invoices"],
                    "unmatched_payments": batch["unmatched_payments"],
                    "conflict_count": batch["conflict_count"],
                    "progress_percent": batch["progress_percent"],
                })

        return {
            "success": True,
            "total_unfinished": total_unfinished,
            "batch_count": len(reminders),
            "reminders": reminders,
        }

    def export_batch_progress(self, batch_id: int, operator: str = None) -> Dict:
        batch_summary = self.get_batch_workbench_summary(batch_id)
        if not batch_summary["success"] or not batch_summary["batches"]:
            return {
                "success": False,
                "message": "批次不存在或无数据"
            }

        batch = batch_summary["batches"][0]
        matches = self.get_batch_matches(batch_id)
        conflicts = self.db.get_batch_conflicts(batch_id=batch_id)

        from .exporter import STATUS_LABELS, MATCH_TYPE_LABELS

        export_data = {
            "batch_info": {
                "batch_id": batch["batch_id"],
                "file_type": batch["file_type"],
                "file_name": batch["file_name"],
                "operator": batch["operator"],
                "imported_at": batch["imported_at"],
                "total_rows": batch["total_rows"],
                "success_rows": batch["success_rows"],
                "failed_rows": batch["failed_rows"],
            },
            "progress": {
                "total_matches": len(matches),
                "pending_matches": batch["pending_matches"],
                "confirmed_matches": batch["confirmed_matches"],
                "exception_matches": batch["exception_matches"],
                "revoked_matches": batch["revoked_matches"],
                "unmatched_invoices": batch["unmatched_invoices"],
                "unmatched_payments": batch["unmatched_payments"],
                "conflict_count": batch["conflict_count"],
                "progress_percent": batch["progress_percent"],
                "exported_at": datetime.now().isoformat(),
                "exported_by": operator,
            },
            "conflicts": [],
            "matches": [],
        }

        for conflict in conflicts:
            export_data["conflicts"].append({
                "conflict_id": conflict["id"],
                "conflict_type": CONFLICT_TYPE_LABELS.get(conflict["conflict_type"], conflict["conflict_type"]),
                "record_type": "发票" if conflict["record_type"] == "invoice" else "收款",
                "record_no": conflict["record_no"],
                "old_status": STATUS_LABELS.get(conflict["old_status"], conflict["old_status"]) if conflict["old_status"] else "-",
                "new_status": STATUS_LABELS.get(conflict["new_status"], conflict["new_status"]) if conflict["new_status"] else "-",
                "old_operator": conflict["old_operator"] or "-",
                "new_operator": conflict["new_operator"] or "-",
                "old_amount": conflict["old_amount"] if conflict["old_amount"] is not None else "-",
                "new_amount": conflict["new_amount"] if conflict["new_amount"] is not None else "-",
                "conflict_reason": conflict["conflict_reason"],
                "detected_at": conflict["detected_at"],
            })

        for m in matches:
            lock = self.db.get_match_lock(m["id"])
            current_owner = lock["lock_owner"] if lock else None

            history = self.db.get_status_history(match_id=m["id"])
            history_str = "; ".join([
                f"{h['changed_at']}: {STATUS_LABELS.get(h['old_status'], h['old_status'])} -> "
                f"{STATUS_LABELS.get(h['new_status'], h['new_status'])} "
                f"(操作人: {h['operator'] or '系统'}, 备注: {h['remark'] or '-'})"
                for h in history
            ])

            export_data["matches"].append({
                "match_id": m["id"],
                "match_no": m["match_no"],
                "match_type": MATCH_TYPE_LABELS.get(m["match_type"], m["match_type"]),
                "match_status": STATUS_LABELS.get(m["status"], m["status"]),
                "match_score": m["match_score"],
                "match_evidence": m["match_evidence"],
                "invoice_no": m["invoice_no"],
                "invoice_date": m["invoice_date"],
                "invoice_customer": m["inv_customer"],
                "invoice_amount": m["inv_amount"],
                "payment_no": m["payment_no"],
                "payment_date": m["payment_date"],
                "payment_customer": m["pay_customer"],
                "payment_amount": m["pay_amount"],
                "amount_diff": abs(m["inv_amount"] - m["pay_amount"]),
                "operator": m["operator"] or "-",
                "operator_remark": m["operator_remark"] or "-",
                "confirmed_at": m["confirmed_at"] or "-",
                "created_at": m["created_at"],
                "current_owner": current_owner or "-",
                "inv_batch_id": m.get("inv_batch_id"),
                "pay_batch_id": m.get("pay_batch_id"),
                "inv_file": m.get("inv_file", "-"),
                "pay_file": m.get("pay_file", "-"),
                "status_history": history_str,
            })

        return {
            "success": True,
            "batch_id": batch_id,
            "data": export_data,
            "summary": {
                "total_matches": len(matches),
                "pending_count": batch["pending_matches"],
                "confirmed_count": batch["confirmed_matches"],
                "exception_count": batch["exception_matches"],
                "revoked_count": batch["revoked_matches"],
                "conflict_count": len(conflicts),
            }
        }

    def save_last_selected_batch(self, batch_id: int, operator: str = None) -> None:
        self.db.set_session_state(SESSION_KEY_LAST_BATCH, {
            "batch_id": batch_id,
            "selected_by": operator,
            "selected_at": datetime.now().isoformat(),
        })
        self.db.set_session_state(SESSION_KEY_LAST_ACCESS_TIME, datetime.now().isoformat())

    def get_last_selected_batch(self) -> Optional[Dict]:
        session = self.db.get_session_state(SESSION_KEY_LAST_BATCH, None)
        if not session:
            return None

        batch_id = session.get("batch_id")
        if batch_id:
            batch_info = self.db.get_batch(batch_id)
            if batch_info:
                session["file_name"] = batch_info["file_name"]
                session["file_type"] = "发票" if batch_info["file_type"] == "invoice" else "收款"
                session["operator"] = batch_info.get("operator")
                session["imported_at"] = batch_info.get("imported_at")

        return session

    def save_filters(self, operator: str = None, status: str = None) -> None:
        filters = {}
        if operator is not None:
            filters["operator"] = operator
        if status is not None:
            filters["status"] = status

        if filters:
            self.db.set_session_state(SESSION_KEY_FILTER_OPERATOR, filters)
        else:
            self.db.clear_session_state(SESSION_KEY_FILTER_OPERATOR)
        self.db.set_session_state(SESSION_KEY_LAST_ACCESS_TIME, datetime.now().isoformat())

    def get_filters(self) -> Dict:
        return self.db.get_session_state(SESSION_KEY_FILTER_OPERATOR, {})

    def restore_workbench_state(self) -> Dict:
        last_batch = self.get_last_selected_batch()
        filters = self.get_filters()
        last_access = self.db.get_session_state(SESSION_KEY_LAST_ACCESS_TIME, None)

        result = {
            "success": True,
            "restored": False,
            "has_state": False,
            "last_batch": None,
            "last_batch_id": None,
            "filters": filters,
            "last_access_time": last_access,
        }

        if last_batch:
            batch_id = last_batch.get("batch_id")
            batch_info = self.db.get_batch(batch_id)
            if batch_info:
                result["restored"] = True
                result["has_state"] = True
                result["last_batch_id"] = batch_id
                result["last_batch"] = {
                    "batch_id": batch_id,
                    "file_name": batch_info["file_name"],
                    "file_type": "发票" if batch_info["file_type"] == "invoice" else "收款",
                    "operator": last_batch.get("operator"),
                    "selected_at": last_batch.get("selected_at"),
                }

        return result

    def clear_workbench_state(self) -> None:
        self.db.clear_session_state(SESSION_KEY_LAST_BATCH)
        self.db.clear_session_state(SESSION_KEY_FILTER_OPERATOR)
        self.db.clear_session_state(SESSION_KEY_LAST_ACCESS_TIME)
