import json
import csv
import os
from typing import List, Dict, Optional, Any, Tuple
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

CHANGE_TYPE_NEW_RECORD = "new_record"
CHANGE_TYPE_STATUS_CHANGE = "status_change"
CHANGE_TYPE_AMOUNT_CHANGE = "amount_change"
CHANGE_TYPE_KEY_FIELD_CHANGE = "key_field_change"
CHANGE_TYPE_DUPLICATE_PROCESS = "duplicate_process"

CHANGE_TYPE_LABELS = {
    CHANGE_TYPE_NEW_RECORD: "新增记录",
    CHANGE_TYPE_STATUS_CHANGE: "状态变更",
    CHANGE_TYPE_AMOUNT_CHANGE: "金额变更",
    CHANGE_TYPE_KEY_FIELD_CHANGE: "关键字段变更",
    CHANGE_TYPE_DUPLICATE_PROCESS: "重复处理",
}

IMPACT_TYPE_NONE = "none"
IMPACT_TYPE_PENDING = "affects_pending"
IMPACT_TYPE_CONFIRMED = "affects_confirmed"
IMPACT_TYPE_REVOKED = "affects_revoked"
IMPACT_TYPE_WARNING = "warning"
IMPACT_TYPE_CRITICAL = "critical"

IMPACT_TYPE_LABELS = {
    IMPACT_TYPE_NONE: "无影响",
    IMPACT_TYPE_PENDING: "影响待确认",
    IMPACT_TYPE_CONFIRMED: "影响已确认",
    IMPACT_TYPE_REVOKED: "影响已撤销",
    IMPACT_TYPE_WARNING: "警告",
    IMPACT_TYPE_CRITICAL: "严重",
}

PROCESSING_STATUS_PENDING = "pending"
PROCESSING_STATUS_REVIEWED = "reviewed"
PROCESSING_STATUS_RESOLVED = "resolved"
PROCESSING_STATUS_IGNORED = "ignored"

PROCESSING_STATUS_LABELS = {
    PROCESSING_STATUS_PENDING: "待处理",
    PROCESSING_STATUS_REVIEWED: "已查看",
    PROCESSING_STATUS_RESOLVED: "已解决",
    PROCESSING_STATUS_IGNORED: "已忽略",
}

KEY_FIELDS_INVOICE = ["customer", "invoice_date"]
KEY_FIELDS_PAYMENT = ["customer", "payment_date"]

CONFLICT_REASON_CONSECUTIVE_UPDATE = "consecutive_update"
CONFLICT_REASON_REVOKED_REIMPORT = "revoked_reimport"
CONFLICT_REASON_CONCURRENT_FIELD_CHANGE = "concurrent_field_change"

AUDIT_CATEGORY_IMPORT = "import"
AUDIT_CATEGORY_CONFLICT = "conflict_detect"
AUDIT_CATEGORY_EXPORT = "export"
AUDIT_CATEGORY_CHANGE_VIEW = "change_view"

AUDIT_ACTION_IMPORT_START = "import_start"
AUDIT_ACTION_IMPORT_COMPLETE = "import_complete"
AUDIT_ACTION_CONFLICT_DETECTED = "conflict_detected"
AUDIT_ACTION_EXPORT_START = "export_start"
AUDIT_ACTION_EXPORT_COMPLETE = "export_complete"
AUDIT_ACTION_CHANGE_VIEWED = "change_viewed"

IMPACT_FILTER_CONFIRMED = "confirmed"
IMPACT_FILTER_PENDING = "pending"
IMPACT_FILTER_REVOKED = "revoked"
IMPACT_FILTER_ALL_AFFECTED = "all_affected"


class ChangeTracker:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    def _get_record_by_no(self, record_type: str, record_no: str,
                          batch_id: int = None) -> Optional[Dict]:
        if record_type == "invoice":
            with self.db._get_conn() as conn:
                if batch_id:
                    row = conn.execute(
                        "SELECT * FROM invoices WHERE invoice_no = ? AND batch_id = ?",
                        (record_no, batch_id)
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM invoices WHERE invoice_no = ? ORDER BY batch_id DESC LIMIT 1",
                        (record_no,)
                    ).fetchone()
        else:
            with self.db._get_conn() as conn:
                if batch_id:
                    row = conn.execute(
                        "SELECT * FROM payments WHERE payment_no = ? AND batch_id = ?",
                        (record_no, batch_id)
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM payments WHERE payment_no = ? ORDER BY batch_id DESC LIMIT 1",
                        (record_no,)
                    ).fetchone()
        return dict(row) if row else None

    def _get_related_matches(self, record_type: str, record_no: str) -> List[Dict]:
        matches = []
        if record_type == "invoice":
            inv = self._get_record_by_no("invoice", record_no)
            if inv:
                with self.db._get_conn() as conn:
                    rows = conn.execute(
                        """SELECT m.*, i.invoice_no, p.payment_no
                           FROM matches m
                           JOIN invoices i ON m.invoice_id = i.id
                           JOIN payments p ON m.payment_id = p.id
                           WHERE i.invoice_no = ?
                           ORDER BY m.created_at DESC""",
                        (record_no,)
                    ).fetchall()
                    matches = [dict(r) for r in rows]
        else:
            pay = self._get_record_by_no("payment", record_no)
            if pay:
                with self.db._get_conn() as conn:
                    rows = conn.execute(
                        """SELECT m.*, i.invoice_no, p.payment_no
                           FROM matches m
                           JOIN invoices i ON m.invoice_id = i.id
                           JOIN payments p ON m.payment_id = p.id
                           WHERE p.payment_no = ?
                           ORDER BY m.created_at DESC""",
                        (record_no,)
                    ).fetchall()
                    matches = [dict(r) for r in rows]
        return matches

    def _analyze_impact(self, record_type: str, record_no: str,
                        change_type: str, field_name: str = None,
                        old_value: Any = None, new_value: Any = None) -> Tuple[str, str, List[int]]:
        matches = self._get_related_matches(record_type, record_no)
        if not matches:
            return IMPACT_TYPE_NONE, "无相关匹配记录", []

        impact_type = IMPACT_TYPE_NONE
        impact_details = []
        impacted_ids = []

        for m in matches:
            status = m["status"]
            match_id = m["id"]
            impacted_ids.append(match_id)

            if status == MATCH_STATUS_MATCHED:
                if change_type == CHANGE_TYPE_AMOUNT_CHANGE and field_name == "amount":
                    impact_type = IMPACT_TYPE_CRITICAL
                    impact_details.append(
                        f"匹配 #{m['match_no']} 已确认，金额从 {old_value} 变为 {new_value} 将导致匹配失效"
                    )
                elif change_type == CHANGE_TYPE_STATUS_CHANGE:
                    impact_type = IMPACT_TYPE_CRITICAL
                    impact_details.append(
                        f"匹配 #{m['match_no']} 已确认，记录状态变更可能需要重新核对"
                    )
                else:
                    if impact_type != IMPACT_TYPE_CRITICAL:
                        impact_type = IMPACT_TYPE_CONFIRMED
                    impact_details.append(
                        f"匹配 #{m['match_no']} 已确认，需评估变更影响"
                    )
            elif status == MATCH_STATUS_PENDING:
                if impact_type not in [IMPACT_TYPE_CRITICAL, IMPACT_TYPE_CONFIRMED]:
                    impact_type = IMPACT_TYPE_PENDING
                impact_details.append(
                    f"匹配 #{m['match_no']} 待确认，变更后可能需要重新匹配"
                )
            elif status == MATCH_STATUS_REVOKED:
                if impact_type == IMPACT_TYPE_NONE:
                    impact_type = IMPACT_TYPE_REVOKED
                impact_details.append(
                    f"匹配 #{m['match_no']} 已撤销，变更仅作参考"
                )
            elif status == MATCH_STATUS_EXCEPTION:
                if impact_type == IMPACT_TYPE_NONE:
                    impact_type = IMPACT_TYPE_WARNING
                impact_details.append(
                    f"匹配 #{m['match_no']} 为异常状态，需关注变更"
                )

        impact_detail_str = "; ".join(impact_details) if impact_details else "无相关匹配"
        return impact_type, impact_detail_str, impacted_ids

    def _format_summary(self, record: Dict, record_type: str) -> str:
        if record_type == "invoice":
            return (
                f"发票号: {record.get('invoice_no', '-')}, "
                f"客户: {record.get('customer', '-')}, "
                f"日期: {record.get('invoice_date', '-')}, "
                f"金额: {record.get('amount', 0):.2f}, "
                f"状态: {record.get('status', '-')}, "
                f"匹配状态: {record.get('match_status', '-')}"
            )
        else:
            return (
                f"收款号: {record.get('payment_no', '-')}, "
                f"客户: {record.get('customer', '-')}, "
                f"日期: {record.get('payment_date', '-')}, "
                f"金额: {record.get('amount', 0):.2f}, "
                f"状态: {record.get('status', '-')}, "
                f"匹配状态: {record.get('match_status', '-')}"
            )

    def _detect_conflict_reasons(self, batch_id: int, record_type: str,
                                record_no: str, change_type: str,
                                field_name: str = None,
                                old_value: Any = None,
                                new_value: Any = None) -> str:
        reasons = []
        history = self.db.get_record_change_history(record_type, record_no)
        relevant_history = [h for h in history if h["batch_id"] < batch_id]

        if len(relevant_history) >= 1 and change_type != CHANGE_TYPE_NEW_RECORD:
            prior_changes = [h for h in relevant_history
                          if h["change_type"] == change_type
                          and (field_name is None or h.get("field_name") == field_name)]
            if len(prior_changes) >= 1:
                prev = prior_changes[-1]
                prev_batch = prev.get("batch_id")
                reasons.append(
                    f"连续更新：该记录在批次 #{prev_batch} 已发生过同类型变更"
                    f"（{prev.get("old_value", "-")} → {prev.get("new_value", "-")}），"
                    f"本次批次 #{batch_id} 再次变更，"
                    f"需确认是否为有意连续修正"
                )

        revoked_status = self.db.get_record_revoked_status(record_type, record_no)
        if revoked_status["has_been_revoked"] and revoked_status["reimported_after_revoke"]:
            revoked_matches = revoked_status["revoked_matches"]
            if revoked_matches:
                rm = revoked_matches[0]
                reasons.append(
                    f"撤销后重导入：相关匹配 {rm.get("match_no", "-")} "
                    f"（状态已撤销，本次导入后重新生效，"
                    f"需确认是否需要重新核对"
                )

        if field_name and change_type in [CHANGE_TYPE_STATUS_CHANGE,
                                       CHANGE_TYPE_AMOUNT_CHANGE,
                                       CHANGE_TYPE_KEY_FIELD_CHANGE]:
            concurrent = self.db.get_concurrent_field_conflicts(
                batch_id, record_type, record_no, field_name
            )
            concurrent_batches = [c for c in concurrent
                            if c["batch_id"] < batch_id]
            if len(concurrent_batches) >= 1:
                cb = concurrent_batches[-1]
                reasons.append(
                    f"同字段并发修改：字段 {field_name} 在批次 #{cb["batch_id"]} "
                    f"（文件 {cb.get("file_name", "-")} 中已由 "
                    f"{cb.get("batch_operator", "未知操作者")} 修改为 "
                    f"{cb.get("new_value", "-")}，"
                    f"本次批次 #{batch_id} 又改为 {new_value}，存在冲突"
                )

        if not reasons:
            return None
        return " | ".join(reasons)

    def track_new_record(self, batch_id: int, record_type: str,
                         record_no: str, new_record: Dict,
                         operator: str = None) -> int:
        change_summary = (
            f"{'发票' if record_type == 'invoice' else '收款'} {record_no} 为本次导入新增记录"
        )
        after_summary = self._format_summary(new_record, record_type)
        impact_type, impact_details, impacted_ids = self._analyze_impact(
            record_type, record_no, CHANGE_TYPE_NEW_RECORD
        )

        conflict_reason = self._detect_conflict_reasons(
            batch_id, record_type, record_no, CHANGE_TYPE_NEW_RECORD
        )

        return self.db.insert_batch_change_log(
            batch_id=batch_id,
            change_type=CHANGE_TYPE_NEW_RECORD,
            record_type=record_type,
            record_no=record_no,
            change_summary=change_summary,
            before_summary=None,
            after_summary=after_summary,
            impact_type=impact_type,
            impact_details=impact_details,
            impacted_match_ids=impacted_ids,
            operator=operator,
            processing_status=PROCESSING_STATUS_PENDING,
            conflict_reason=conflict_reason,
        )

    def track_status_change(self, batch_id: int, record_type: str,
                            record_no: str, old_record: Dict,
                            new_record: Dict, old_status: str,
                            new_status: str, operator: str = None) -> int:
        change_summary = (
            f"{'发票' if record_type == 'invoice' else '收款'} {record_no} 状态从 "
            f"{old_status} 变为 {new_status}"
        )
        before_summary = self._format_summary(old_record, record_type)
        after_summary = self._format_summary(new_record, record_type)
        impact_type, impact_details, impacted_ids = self._analyze_impact(
            record_type, record_no, CHANGE_TYPE_STATUS_CHANGE,
            field_name="status", old_value=old_status, new_value=new_status
        )

        conflict_reason = self._detect_conflict_reasons(
            batch_id, record_type, record_no, CHANGE_TYPE_STATUS_CHANGE,
            field_name="status", old_value=old_status, new_value=new_status
        )

        return self.db.insert_batch_change_log(
            batch_id=batch_id,
            change_type=CHANGE_TYPE_STATUS_CHANGE,
            record_type=record_type,
            record_no=record_no,
            field_name="status",
            old_value=str(old_status),
            new_value=str(new_status),
            change_summary=change_summary,
            before_summary=before_summary,
            after_summary=after_summary,
            impact_type=impact_type,
            impact_details=impact_details,
            impacted_match_ids=impacted_ids,
            operator=operator,
            processing_status=PROCESSING_STATUS_PENDING,
            conflict_reason=conflict_reason,
        )

    def track_amount_change(self, batch_id: int, record_type: str,
                            record_no: str, old_record: Dict,
                            new_record: Dict, old_amount: float,
                            new_amount: float, operator: str = None) -> int:
        change_summary = (
            f"{'发票' if record_type == 'invoice' else '收款'} {record_no} 金额从 "
            f"{old_amount:.2f} 变为 {new_amount:.2f}，差额 {abs(new_amount - old_amount):.2f}"
        )
        before_summary = self._format_summary(old_record, record_type)
        after_summary = self._format_summary(new_record, record_type)
        impact_type, impact_details, impacted_ids = self._analyze_impact(
            record_type, record_no, CHANGE_TYPE_AMOUNT_CHANGE,
            field_name="amount", old_value=old_amount, new_value=new_amount
        )

        conflict_reason = self._detect_conflict_reasons(
            batch_id, record_type, record_no, CHANGE_TYPE_AMOUNT_CHANGE,
            field_name="amount", old_value=old_amount, new_value=new_amount
        )

        return self.db.insert_batch_change_log(
            batch_id=batch_id,
            change_type=CHANGE_TYPE_AMOUNT_CHANGE,
            record_type=record_type,
            record_no=record_no,
            field_name="amount",
            old_value=f"{old_amount:.2f}",
            new_value=f"{new_amount:.2f}",
            change_summary=change_summary,
            before_summary=before_summary,
            after_summary=after_summary,
            impact_type=impact_type,
            impact_details=impact_details,
            impacted_match_ids=impacted_ids,
            operator=operator,
            processing_status=PROCESSING_STATUS_PENDING,
            conflict_reason=conflict_reason,
        )

    def track_key_field_change(self, batch_id: int, record_type: str,
                               record_no: str, field_name: str,
                               old_value: Any, new_value: Any,
                               old_record: Dict, new_record: Dict,
                               operator: str = None) -> int:
        field_label = "客户" if field_name == "customer" else field_name
        change_summary = (
            f"{'发票' if record_type == 'invoice' else '收款'} {record_no} {field_label}从 "
            f"{old_value} 变为 {new_value}"
        )
        before_summary = self._format_summary(old_record, record_type)
        after_summary = self._format_summary(new_record, record_type)
        impact_type, impact_details, impacted_ids = self._analyze_impact(
            record_type, record_no, CHANGE_TYPE_KEY_FIELD_CHANGE,
            field_name=field_name, old_value=old_value, new_value=new_value
        )

        conflict_reason = self._detect_conflict_reasons(
            batch_id, record_type, record_no, CHANGE_TYPE_KEY_FIELD_CHANGE,
            field_name=field_name, old_value=old_value, new_value=new_value
        )

        return self.db.insert_batch_change_log(
            batch_id=batch_id,
            change_type=CHANGE_TYPE_KEY_FIELD_CHANGE,
            record_type=record_type,
            record_no=record_no,
            field_name=field_name,
            old_value=str(old_value),
            new_value=str(new_value),
            change_summary=change_summary,
            before_summary=before_summary,
            after_summary=after_summary,
            impact_type=impact_type,
            impact_details=impact_details,
            impacted_match_ids=impacted_ids,
            operator=operator,
            processing_status=PROCESSING_STATUS_PENDING,
            conflict_reason=conflict_reason,
        )

    def track_duplicate_process(self, batch_id: int, record_type: str,
                                record_no: str, old_operator: str,
                                new_operator: str, old_record: Dict,
                                new_record: Dict, operator: str = None) -> int:
        change_summary = (
            f"{'发票' if record_type == 'invoice' else '收款'} {record_no} 已由 {old_operator} 处理，"
            f"现由 {new_operator} 重新处理"
        )
        before_summary = self._format_summary(old_record, record_type)
        after_summary = self._format_summary(new_record, record_type)
        impact_type, impact_details, impacted_ids = self._analyze_impact(
            record_type, record_no, CHANGE_TYPE_DUPLICATE_PROCESS
        )

        conflict_reason = self._detect_conflict_reasons(
            batch_id, record_type, record_no, CHANGE_TYPE_DUPLICATE_PROCESS
        )

        return self.db.insert_batch_change_log(
            batch_id=batch_id,
            change_type=CHANGE_TYPE_DUPLICATE_PROCESS,
            record_type=record_type,
            record_no=record_no,
            field_name="operator",
            old_value=old_operator,
            new_value=new_operator,
            change_summary=change_summary,
            before_summary=before_summary,
            after_summary=after_summary,
            impact_type=impact_type,
            impact_details=impact_details,
            impacted_match_ids=impacted_ids,
            operator=operator,
            processing_status=PROCESSING_STATUS_PENDING,
            conflict_reason=conflict_reason,
        )

    def detect_and_track_changes(self, batch_id: int, file_type: str,
                                 operator: str = None) -> Dict:
        from .importer import (
            STATUS_LABEL_INVOICE,
            STATUS_LABEL_PAYMENT,
        )

        all_batches = self.db.get_batches()
        prev_batches = [b for b in all_batches
                         if b["file_type"] == file_type and b["id"] < batch_id]

        if not prev_batches:
            return {
                "success": True,
                "total_changes": 0,
                "changes_by_type": {},
                "impact_summary": {},
                "change_log_ids": [],
            }

        existing_records = {}
        prev_batch_ids = [b["id"] for b in prev_batches]

        if file_type == "invoice":
            table_name = "invoices"
            no_field = "invoice_no"
            key_fields = KEY_FIELDS_INVOICE
            status_label_map = STATUS_LABEL_INVOICE
        else:
            table_name = "payments"
            no_field = "payment_no"
            key_fields = KEY_FIELDS_PAYMENT
            status_label_map = STATUS_LABEL_PAYMENT

        for prev_batch_id in prev_batch_ids:
            with self.db._get_conn() as conn:
                rows = conn.execute(
                    f"SELECT * FROM {table_name} WHERE batch_id = ?",
                    (prev_batch_id,)
                ).fetchall()
                for r in rows:
                    existing_records[r[no_field]] = dict(r)

        with self.db._get_conn() as conn:
            new_rows = conn.execute(
                f"SELECT * FROM {table_name} WHERE batch_id = ?",
                (batch_id,)
            ).fetchall()

        change_log_ids = []
        changes_by_type = {}
        impact_summary = {
            IMPACT_TYPE_CRITICAL: 0,
            IMPACT_TYPE_CONFIRMED: 0,
            IMPACT_TYPE_PENDING: 0,
            IMPACT_TYPE_REVOKED: 0,
            IMPACT_TYPE_WARNING: 0,
            IMPACT_TYPE_NONE: 0,
        }

        for r in new_rows:
            new_record = dict(r)
            record_no = new_record[no_field]

            if record_no not in existing_records:
                log_id = self.track_new_record(
                    batch_id, file_type, record_no, new_record, operator
                )
                change_log_ids.append(log_id)
                changes_by_type[CHANGE_TYPE_NEW_RECORD] = changes_by_type.get(CHANGE_TYPE_NEW_RECORD, 0) + 1
                continue

            old_record = existing_records[record_no]

            new_status = status_label_map.get(new_record["status"], new_record["status"])
            old_status = status_label_map.get(old_record["status"], old_record["status"])
            if old_status != new_status:
                log_id = self.track_status_change(
                    batch_id, file_type, record_no, old_record, new_record,
                    old_status, new_status, operator
                )
                change_log_ids.append(log_id)
                changes_by_type[CHANGE_TYPE_STATUS_CHANGE] = changes_by_type.get(CHANGE_TYPE_STATUS_CHANGE, 0) + 1

            old_amount = old_record["amount"]
            new_amount = new_record["amount"]
            if abs(new_amount - old_amount) > 0.001:
                log_id = self.track_amount_change(
                    batch_id, file_type, record_no, old_record, new_record,
                    old_amount, new_amount, operator
                )
                change_log_ids.append(log_id)
                changes_by_type[CHANGE_TYPE_AMOUNT_CHANGE] = changes_by_type.get(CHANGE_TYPE_AMOUNT_CHANGE, 0) + 1

            for field in key_fields:
                old_val = old_record.get(field)
                new_val = new_record.get(field)
                if old_val != new_val:
                    log_id = self.track_key_field_change(
                        batch_id, file_type, record_no, field, old_val, new_val,
                        old_record, new_record, operator
                    )
                    change_log_ids.append(log_id)
                    changes_by_type[CHANGE_TYPE_KEY_FIELD_CHANGE] = changes_by_type.get(CHANGE_TYPE_KEY_FIELD_CHANGE, 0) + 1

        all_logs = self.db.get_batch_change_logs(batch_id=batch_id)
        for log in all_logs:
            if log["impact_type"] in impact_summary:
                impact_summary[log["impact_type"]] += 1

        conflict_count = sum(1 for l in all_logs if l.get("conflict_reason"))

        audit_details = json.dumps({
            "file_type": file_type,
            "change_count": len(change_log_ids),
            "changes_by_type": changes_by_type,
            "impact_summary": impact_summary,
            "conflict_count": conflict_count,
        }, ensure_ascii=False)

        self.db.insert_audit_log(
            action_type=AUDIT_ACTION_CONFLICT_DETECTED,
            action_category=AUDIT_CATEGORY_CONFLICT,
            action_summary=(
                f"批次 #{batch_id} 导入检测：{len(change_log_ids)} 条变更，"
                f"{conflict_count} 条含冲突原因"
            ),
            batch_id=batch_id,
            record_type=file_type,
            operator=operator,
            action_details=audit_details,
            status="success",
        )

        return {
            "success": True,
            "total_changes": len(change_log_ids),
            "changes_by_type": changes_by_type,
            "impact_summary": impact_summary,
            "change_log_ids": change_log_ids,
            "conflict_count": conflict_count,
        }

    def get_change_summary(self, batch_id: int = None) -> Dict:
        logs = self.db.get_batch_change_logs(batch_id=batch_id)

        total = len(logs)
        by_type = {}
        by_impact = {}
        by_status = {}

        for log in logs:
            ct = log["change_type"]
            by_type[ct] = by_type.get(ct, 0) + 1

            it = log["impact_type"] or IMPACT_TYPE_NONE
            by_impact[it] = by_impact.get(it, 0) + 1

            ps = log["processing_status"] or PROCESSING_STATUS_PENDING
            by_status[ps] = by_status.get(ps, 0) + 1

        return {
            "success": True,
            "total_changes": total,
            "by_type": by_type,
            "by_impact": by_impact,
            "by_status": by_status,
        }

    def export_change_logs(self, batch_id: int, operator: str = None,
                           format: str = "json", log_ids: List[int] = None,
                           extra_meta: Dict = None) -> Dict:
        logs = self.db.get_batch_change_logs(batch_id=batch_id)
        if log_ids:
            id_set = set(log_ids)
            logs = [l for l in logs if l["id"] in id_set]
            logs.sort(key=lambda x: log_ids.index(x["id"]) if x["id"] in log_ids else 9999)
        batch_info = self.db.get_batch(batch_id)

        if not batch_info:
            self.db.insert_audit_log(
                action_type=AUDIT_ACTION_EXPORT_START,
                action_category=AUDIT_CATEGORY_EXPORT,
                action_summary=f"批次 #{batch_id} 变更日志导出失败：批次不存在",
                batch_id=batch_id,
                operator=operator,
                status="failed",
                error_message="批次不存在",
            )
            return {"success": False, "message": "批次不存在"}

        export_op = operator or get_current_user()

        self.db.insert_audit_log(
            action_type=AUDIT_ACTION_EXPORT_START,
            action_category=AUDIT_CATEGORY_EXPORT,
            action_summary=(
                f"开始导出批次 #{batch_id} 变更日志"
                f"（{len(logs)} 条记录，格式: {format}）"
            ),
            batch_id=batch_id,
            operator=export_op,
            status="success",
        )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_type_label = "发票" if batch_info["file_type"] == "invoice" else "收款"

        export_info = {
            "exported_at": datetime.now().isoformat(),
            "exported_by": operator or get_current_user(),
            "batch_id": batch_id,
            "file_name": batch_info["file_name"],
            "file_type": file_type_label,
            "imported_at": batch_info["imported_at"],
            "imported_by": batch_info.get("operator", "-"),
            "total_changes": len(logs),
        }
        if extra_meta:
            export_info.update(extra_meta)
        export_data = {
            "export_info": export_info,
            "summary": {
                "by_type": {},
                "by_impact": {},
                "by_status": {},
            },
            "change_logs": [],
        }

        for log in logs:
            ct = log["change_type"]
            export_data["summary"]["by_type"][ct] = export_data["summary"]["by_type"].get(ct, 0) + 1
            it = log["impact_type"] or IMPACT_TYPE_NONE
            export_data["summary"]["by_impact"][it] = export_data["summary"]["by_impact"].get(it, 0) + 1
            ps = log["processing_status"] or PROCESSING_STATUS_PENDING
            export_data["summary"]["by_status"][ps] = export_data["summary"]["by_status"].get(ps, 0) + 1

            conflict_reason_val = log.get("conflict_reason") or "-"
            has_conflict = "是" if log.get("conflict_reason") else "否"

            log_entry = {
                "日志ID": log["id"],
                "批次ID": log["batch_id"],
                "来源文件": log.get("file_name", "-"),
                "变更类型": CHANGE_TYPE_LABELS.get(log["change_type"], log["change_type"]),
                "记录类型": "发票" if log["record_type"] == "invoice" else "收款",
                "记录编号": log["record_no"],
                "变更字段": log.get("field_name") or "-",
                "原值": log.get("old_value") or "-",
                "新值": log.get("new_value") or "-",
                "变更摘要": log["change_summary"],
                "变更前摘要": log.get("before_summary") or "-",
                "变更后摘要": log.get("after_summary") or "-",
                "影响类型": IMPACT_TYPE_LABELS.get(log["impact_type"] or IMPACT_TYPE_NONE,
                                                  log["impact_type"] or IMPACT_TYPE_NONE),
                "影响详情": log.get("impact_details") or "-",
                "影响的匹配ID": ", ".join(map(str, log.get("impacted_match_ids") or [])) or "-",
                "是否有冲突": has_conflict,
                "冲突原因": conflict_reason_val,
                "处理状态": PROCESSING_STATUS_LABELS.get(
                    log["processing_status"] or PROCESSING_STATUS_PENDING,
                    log["processing_status"] or PROCESSING_STATUS_PENDING
                ),
                "操作者": log.get("operator") or "-",
                "检测时间": log["detected_at"],
                "处理时间": log.get("processed_at") or "-",
                "处理人": log.get("processed_by") or "-",
                "备注": log.get("remark") or "-",
            }
            export_data["change_logs"].append(log_entry)

        base_name = f"change_log_batch_{batch_id}_{timestamp}"
        os.makedirs(self.config.export_dir, exist_ok=True)

        result = None

        if format == "json":
            file_path = os.path.join(self.config.export_dir, f"{base_name}.json")
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)

            result = {
                "success": True,
                "file_path": file_path,
                "format": "json",
                "total_changes": len(logs),
                "generated_at": datetime.now().isoformat(),
                "summary": export_data["summary"],
            }

        elif format == "csv":
            dir_path = os.path.join(self.config.export_dir, base_name)
            os.makedirs(dir_path, exist_ok=True)

            summary_path = os.path.join(dir_path, "变更摘要.csv")
            with open(summary_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["项目", "值", "说明"])
                writer.writerow(["批次ID", batch_id, ""])
                writer.writerow(["文件名", batch_info["file_name"], ""])
                writer.writerow(["文件类型", file_type_label, ""])
                writer.writerow(["导入时间", batch_info["imported_at"], ""])
                writer.writerow(["导入人", batch_info.get("operator", "-"), ""])
                writer.writerow(["导出时间", datetime.now().isoformat(), ""])
                writer.writerow(["导出人", operator or get_current_user(), ""])
                writer.writerow(["总变更数", len(logs), ""])
                writer.writerow([])
                writer.writerow(["—— 按变更类型统计 ——"])
                for ct, count in export_data["summary"]["by_type"].items():
                    writer.writerow([CHANGE_TYPE_LABELS.get(ct, ct), count, ""])
                writer.writerow([])
                writer.writerow(["—— 按影响类型统计 ——"])
                for it, count in export_data["summary"]["by_impact"].items():
                    writer.writerow([IMPACT_TYPE_LABELS.get(it, it), count, ""])
                writer.writerow([])
                writer.writerow(["—— 按处理状态统计 ——"])
                for ps, count in export_data["summary"]["by_status"].items():
                    writer.writerow([PROCESSING_STATUS_LABELS.get(ps, ps), count, ""])

            logs_path = os.path.join(dir_path, "变更明细.csv")
            if export_data["change_logs"]:
                fieldnames = list(export_data["change_logs"][0].keys())
                with open(logs_path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(export_data["change_logs"])

            result = {
                "success": True,
                "file_path": dir_path,
                "format": "csv",
                "total_changes": len(logs),
                "generated_at": datetime.now().isoformat(),
                "summary": export_data["summary"],
                "files": [summary_path, logs_path],
            }

        else:
            result = {"success": False, "message": f"不支持的导出格式: {format}"}

        if result["success"]:
            audit_details = json.dumps(result, ensure_ascii=False, default=str)
            self.db.insert_audit_log(
                action_type=AUDIT_ACTION_EXPORT_COMPLETE,
                action_category=AUDIT_CATEGORY_EXPORT,
                action_summary=(
                    f"批次 #{batch_id} 变更日志导出成功"
                    f"（{len(logs)} 条，{format}）"
                ),
                batch_id=batch_id,
                operator=export_op,
                action_details=audit_details,
                status="success",
            )
        else:
            self.db.insert_audit_log(
                action_type=AUDIT_ACTION_EXPORT_COMPLETE,
                action_category=AUDIT_CATEGORY_EXPORT,
                action_summary=f"批次 #{batch_id} 变更日志导出失败",
                batch_id=batch_id,
                operator=export_op,
                status="failed",
                error_message=result.get("message"),
            )

        return result

    def get_change_timeline(self, batch_id: int = None,
                           record_type: str = None,
                           operator: str = None) -> Dict:
        if batch_id:
            logs = self.db.get_batch_change_logs(batch_id=batch_id)
        else:
            logs = self.db.get_batch_change_logs()
            if record_type:
                logs = [l for l in logs if l["record_type"] == record_type]

        by_record = {}
        for log in logs:
            key = (log["record_type"], log["record_no"])
            if key not in by_record:
                by_record[key] = {
                    "record_type": key[0],
                    "record_no": key[1],
                    "change_count": 0,
                    "has_conflict": False,
                    "timeline": [],
                }
            entry = by_record[key]
            entry["change_count"] += 1
            if log.get("conflict_reason"):
                entry["has_conflict"] = True
            timeline_item = {
                "log_id": log["id"],
                "batch_id": log["batch_id"],
                "file_name": log.get("file_name", "-"),
                "change_type": CHANGE_TYPE_LABELS.get(
                    log["change_type"], log["change_type"]
                ),
                "field": log.get("field_name") or "-",
                "old_value": log.get("old_value") or "-",
                "new_value": log.get("new_value") or "-",
                "summary": log["change_summary"],
                "before": log.get("before_summary") or "-",
                "after": log.get("after_summary") or "-",
                "impact": IMPACT_TYPE_LABELS.get(
                    log["impact_type"] or IMPACT_TYPE_NONE,
                    log["impact_type"] or IMPACT_TYPE_NONE
                ),
                "conflict": log.get("conflict_reason") or None,
                "operator": log.get("operator") or "-",
                "detected_at": log["detected_at"],
            }
            entry["timeline"].append(timeline_item)

        for key in by_record:
            by_record[key]["timeline"].sort(key=lambda x: x["detected_at"])

        self.db.insert_audit_log(
            action_type=AUDIT_ACTION_CHANGE_VIEWED,
            action_category=AUDIT_CATEGORY_CHANGE_VIEW,
            action_summary=(
                f"查看{('批次 #' + str(batch_id)) if batch_id else '全部'} "
                f"变更时间线：{len(by_record)} 条记录、"
                f"{len(logs)} 条变更"
            ),
            batch_id=batch_id,
            operator=operator,
            action_details=json.dumps({
                "record_count": len(by_record),
                "change_count": len(logs),
                "conflict_records": sum(1 for r in by_record.values() if r["has_conflict"]),
            }, ensure_ascii=False),
            status="success",
        )

        return {
            "total_records": len(by_record),
            "total_changes": len(logs),
            "records_with_conflict": sum(1 for r in by_record.values() if r["has_conflict"]),
            "records": list(by_record.values()),
        }

    def filter_changes_by_impact(self, batch_id: int = None,
                                impact_filter: str = IMPACT_FILTER_ALL_AFFECTED,
                                operator: str = None) -> List[Dict]:
        logs = self.db.get_batch_change_logs(batch_id=batch_id)

        filter_map = {
            IMPACT_FILTER_CONFIRMED: lambda l: l["impact_type"] in (
                IMPACT_TYPE_CONFIRMED, IMPACT_TYPE_CRITICAL
            ),
            IMPACT_FILTER_PENDING: lambda l: l["impact_type"] == IMPACT_TYPE_PENDING,
            IMPACT_FILTER_REVOKED: lambda l: l["impact_type"] == IMPACT_TYPE_REVOKED,
            IMPACT_FILTER_ALL_AFFECTED: lambda l: l["impact_type"] != IMPACT_TYPE_NONE,
        }

        predicate = filter_map.get(impact_filter, filter_map[IMPACT_FILTER_ALL_AFFECTED])
        filtered = [l for l in logs if predicate(l)]

        self.db.insert_audit_log(
            action_type=AUDIT_ACTION_CHANGE_VIEWED,
            action_category=AUDIT_CATEGORY_CHANGE_VIEW,
            action_summary=(
                f"按影响筛选批次 #{batch_id if batch_id else '全部'} "
                f"变更：筛选={impact_filter}, 命中={len(filtered)}/{len(logs)}"
            ),
            batch_id=batch_id,
            operator=operator,
            status="success",
        )

        return filtered


def get_current_user() -> str:
    import os
    return os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
