import csv
import os
import json
from datetime import datetime
from typing import List, Dict, Tuple, Optional
from .config import Config
from .database import (
    Database,
    INVOICE_STATUS_NORMAL,
    INVOICE_STATUS_INVALID,
    PAYMENT_STATUS_NORMAL,
    PAYMENT_STATUS_INVALID,
)

STATUS_MAP_INVOICE = {
    "正常": INVOICE_STATUS_NORMAL,
    "normal": INVOICE_STATUS_NORMAL,
    "作废": "invalid",
    "invalid": INVOICE_STATUS_INVALID,
}

STATUS_MAP_PAYMENT = {
    "正常": PAYMENT_STATUS_NORMAL,
    "normal": PAYMENT_STATUS_NORMAL,
    "作废": "invalid",
    "invalid": PAYMENT_STATUS_INVALID,
}

STATUS_LABEL_INVOICE = {
    INVOICE_STATUS_NORMAL: "正常",
    "normal": "正常",
    INVOICE_STATUS_INVALID: "作废",
    "invalid": "作废",
}

STATUS_LABEL_PAYMENT = {
    PAYMENT_STATUS_NORMAL: "正常",
    "normal": "正常",
    PAYMENT_STATUS_INVALID: "作废",
    "invalid": "作废",
}


ERROR_TYPE_MISSING_COLUMN = "missing_column"
ERROR_TYPE_MISSING_CUSTOMER = "missing_customer"
ERROR_TYPE_INVALID_AMOUNT = "invalid_amount"
ERROR_TYPE_INVALID_DATE = "invalid_date"
ERROR_TYPE_MISSING_REQUIRED = "missing_required"
ERROR_TYPE_DUPLICATE = "duplicate"

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


class CSVImporter:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    def import_invoices(self, file_path: str, operator: str = None) -> Dict:
        return self._import_file(file_path, "invoice", operator)

    def import_payments(self, file_path: str, operator: str = None) -> Dict:
        return self._import_file(file_path, "payment", operator)

    def _import_file(self, file_path: str, file_type: str, operator: str = None) -> Dict:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        existing_batch = self.db.check_file_imported(file_path, file_type)
        if existing_batch:
            return {
                "success": True,
                "skipped": True,
                "batch_id": existing_batch["id"],
                "message": f"文件已导入，批次ID: {existing_batch['id']}，跳过重复导入",
                "total_rows": existing_batch["total_rows"],
                "success_rows": existing_batch["success_rows"],
                "failed_rows": existing_batch["failed_rows"]
            }

        required_columns = (
            self.config.invoice_required_columns
            if file_type == "invoice"
            else self.config.payment_required_columns
        )

        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []

            missing_cols = [c for c in required_columns if c not in headers]
            if missing_cols:
                raise ValueError(
                    f"缺少必填列: {', '.join(missing_cols)}。"
                    f"现有列: {', '.join(headers)}"
                )

            rows = list(reader)
            total_rows = len(rows)

            file_name = os.path.basename(file_path)
            batch_id = self.db.create_batch(
                file_type, file_path, file_name, total_rows, operator
            )

            success_count = 0
            failed_count = 0
            conflict_count = 0

            for idx, row in enumerate(rows, start=2):
                result = self._validate_and_insert_row(
                    batch_id, idx, row, file_type, required_columns
                )
                if result["success"]:
                    success_count += 1
                else:
                    failed_count += 1
                    self.db.insert_error(
                        batch_id, file_type, idx,
                        result["error_type"], result["error_message"],
                        json.dumps(row, ensure_ascii=False)
                    )

            self.db.update_batch_stats(batch_id, success_count, failed_count)

            conflicts = self._detect_batch_conflicts(batch_id, file_type, operator)
            conflict_count = len(conflicts)

            from .change_tracker import ChangeTracker
            tracker = ChangeTracker(self.config, self.db)
            change_result = tracker.detect_and_track_changes(batch_id, file_type, operator)

            message = f"导入完成：成功 {success_count} 条，失败 {failed_count} 条"
            if conflict_count > 0:
                message += f"，检测到 {conflict_count} 个冲突"
            if change_result["total_changes"] > 0:
                message += f"，追踪到 {change_result['total_changes']} 条变更记录"

            return {
                "success": True,
                "skipped": False,
                "batch_id": batch_id,
                "total_rows": total_rows,
                "success_rows": success_count,
                "failed_rows": failed_count,
                "conflict_count": conflict_count,
                "conflicts": conflicts,
                "change_count": change_result["total_changes"],
                "changes_by_type": change_result["changes_by_type"],
                "impact_summary": change_result["impact_summary"],
                "change_log_ids": change_result["change_log_ids"],
                "message": message
            }

    def _detect_batch_conflicts(self, batch_id: int, file_type: str,
                                 new_operator: str = None) -> List[Dict]:
        from .database import (
            MATCH_STATUS_MATCHED,
            MATCH_STATUS_PENDING,
            MATCH_STATUS_EXCEPTION,
            MATCH_STATUS_REVOKED,
            MATCH_STATUS_UNMATCHED,
        )

        conflicts = []
        existing_records = {}

        all_batches = self.db.get_batches()
        prev_batches = [b for b in all_batches
                         if b["file_type"] == file_type and b["id"] < batch_id]

        if not prev_batches:
            return conflicts

        prev_batch_ids = [b["id"] for b in prev_batches]

        if file_type == "invoice":
            for prev_batch_id in prev_batch_ids:
                with self.db._get_conn() as conn:
                    rows = conn.execute(
                        """SELECT id, invoice_no, amount, match_status, status
                           FROM invoices WHERE batch_id = ?""",
                        (prev_batch_id,)
                    ).fetchall()
                    for r in rows:
                        existing_records[r["invoice_no"]] = {
                            "id": r["id"],
                            "amount": r["amount"],
                            "match_status": r["match_status"],
                            "status": r["status"],
                            "batch_id": prev_batch_id,
                        }

            with self.db._get_conn() as conn:
                new_rows = conn.execute(
                    """SELECT id, invoice_no, amount, match_status, status
                       FROM invoices WHERE batch_id = ?""",
                    (batch_id,)
                ).fetchall()

            for r in new_rows:
                invoice_no = r["invoice_no"]
                new_amount = r["amount"]
                new_match_status = r["match_status"]
                new_inv_status = r["status"]

                if invoice_no not in existing_records:
                    conflict_id = self.db.insert_batch_conflict(
                        batch_id=batch_id,
                        conflict_type=CONFLICT_TYPE_NEW_RECORD,
                        record_type="invoice",
                        record_no=invoice_no,
                        conflict_reason=f"发票 {invoice_no} 为本次导入新增记录，之前批次中不存在",
                        new_amount=new_amount,
                        new_status=new_match_status,
                        new_operator=new_operator,
                    )
                    conflicts.append({
                        "conflict_id": conflict_id,
                        "conflict_type": CONFLICT_TYPE_NEW_RECORD,
                        "record_type": "invoice",
                        "record_no": invoice_no,
                        "conflict_reason": f"发票 {invoice_no} 为本次导入新增记录",
                        "old_amount": None,
                        "new_amount": new_amount,
                        "old_status": None,
                        "new_status": new_inv_status,
                        "old_operator": None,
                        "new_operator": new_operator,
                    })
                    continue

                existing = existing_records[invoice_no]
                old_amount = existing["amount"]
                old_match_status = existing["match_status"]
                old_inv_status = existing["status"]
                old_operator = None

                with self.db._get_conn() as conn:
                    match_row = conn.execute(
                        """SELECT m.operator FROM matches m
                           JOIN invoices i ON m.invoice_id = i.id
                           WHERE i.id = ? AND m.status != 'revoked'
                           ORDER BY m.id DESC LIMIT 1""",
                        (existing["id"],)
                    ).fetchone()
                    if match_row:
                        old_operator = match_row["operator"]

                if abs(new_amount - old_amount) > 0.001:
                    conflict_id = self.db.insert_batch_conflict(
                        batch_id=batch_id,
                        conflict_type=CONFLICT_TYPE_AMOUNT_CHANGE,
                        record_type="invoice",
                        record_no=invoice_no,
                        conflict_reason=f"发票 {invoice_no} 金额变更: 旧 {old_amount:.2f} -> 新 {new_amount:.2f}",
                        old_amount=old_amount,
                        new_amount=new_amount,
                        old_status=old_inv_status,
                        new_status=new_inv_status,
                    )
                    conflicts.append({
                        "conflict_id": conflict_id,
                        "conflict_type": CONFLICT_TYPE_AMOUNT_CHANGE,
                        "record_type": "invoice",
                        "record_no": invoice_no,
                        "conflict_reason": f"发票 {invoice_no} 金额从 {old_amount:.2f} 变为 {new_amount:.2f}",
                        "old_amount": old_amount,
                        "new_amount": new_amount,
                        "old_status": old_inv_status,
                        "new_status": new_inv_status,
                        "old_operator": old_operator,
                        "new_operator": new_operator,
                    })

                if old_inv_status != new_inv_status and old_inv_status is not None:
                    conflict_id = self.db.insert_batch_conflict(
                        batch_id=batch_id,
                        conflict_type=CONFLICT_TYPE_STATUS_CHANGE,
                        record_type="invoice",
                        record_no=invoice_no,
                        conflict_reason=f"发票 {invoice_no} 状态冲突: 旧状态 {old_inv_status}，新导入后为 {new_inv_status}",
                        old_status=old_inv_status,
                        new_status=new_inv_status,
                        old_operator=old_operator,
                        new_operator=new_operator,
                    )
                    conflicts.append({
                        "conflict_id": conflict_id,
                        "conflict_type": CONFLICT_TYPE_STATUS_CHANGE,
                        "record_type": "invoice",
                        "record_no": invoice_no,
                        "conflict_reason": f"发票 {invoice_no} 状态从 {old_inv_status} 变为 {new_inv_status}",
                        "old_amount": old_amount,
                        "new_amount": new_amount,
                        "old_status": old_inv_status,
                        "new_status": new_inv_status,
                        "old_operator": old_operator,
                        "new_operator": new_operator,
                    })

                if old_operator and new_operator and old_operator != new_operator and old_match_status == MATCH_STATUS_MATCHED:
                    conflict_id = self.db.insert_batch_conflict(
                        batch_id=batch_id,
                        conflict_type=CONFLICT_TYPE_DUPLICATE_PROCESS,
                        record_type="invoice",
                        record_no=invoice_no,
                        conflict_reason=f"发票 {invoice_no} 已被 {old_operator} 处理，现由 {new_operator} 重新处理",
                        old_status=old_inv_status,
                        new_status=new_inv_status,
                        old_operator=old_operator,
                        new_operator=new_operator,
                    )
                    conflicts.append({
                        "conflict_id": conflict_id,
                        "conflict_type": CONFLICT_TYPE_DUPLICATE_PROCESS,
                        "record_type": "invoice",
                        "record_no": invoice_no,
                        "conflict_reason": f"发票 {invoice_no} 已被 {old_operator} 处理，现由 {new_operator} 重新处理",
                        "old_amount": old_amount,
                        "new_amount": new_amount,
                        "old_status": old_inv_status,
                        "new_status": new_inv_status,
                        "old_operator": old_operator,
                        "new_operator": new_operator,
                    })

        else:
            for prev_batch_id in prev_batch_ids:
                with self.db._get_conn() as conn:
                    rows = conn.execute(
                        """SELECT id, payment_no, amount, match_status, status
                           FROM payments WHERE batch_id = ?""",
                        (prev_batch_id,)
                    ).fetchall()
                    for r in rows:
                        existing_records[r["payment_no"]] = {
                            "id": r["id"],
                            "amount": r["amount"],
                            "match_status": r["match_status"],
                            "status": r["status"],
                            "batch_id": prev_batch_id,
                        }

            with self.db._get_conn() as conn:
                new_rows = conn.execute(
                    """SELECT id, payment_no, amount, match_status, status
                       FROM payments WHERE batch_id = ?""",
                    (batch_id,)
                ).fetchall()

            for r in new_rows:
                payment_no = r["payment_no"]
                new_amount = r["amount"]
                new_match_status = r["match_status"]
                new_pay_status = r["status"]

                if payment_no not in existing_records:
                    conflict_id = self.db.insert_batch_conflict(
                        batch_id=batch_id,
                        conflict_type=CONFLICT_TYPE_NEW_RECORD,
                        record_type="payment",
                        record_no=payment_no,
                        conflict_reason=f"收款 {payment_no} 为本次导入新增记录，之前批次中不存在",
                        new_amount=new_amount,
                        new_status=new_match_status,
                        new_operator=new_operator,
                    )
                    conflicts.append({
                        "conflict_id": conflict_id,
                        "conflict_type": CONFLICT_TYPE_NEW_RECORD,
                        "record_type": "payment",
                        "record_no": payment_no,
                        "conflict_reason": f"收款 {payment_no} 为本次导入新增记录",
                        "old_amount": None,
                        "new_amount": new_amount,
                        "old_status": None,
                        "new_status": new_pay_status,
                        "old_operator": None,
                        "new_operator": new_operator,
                    })
                    continue

                existing = existing_records[payment_no]
                old_amount = existing["amount"]
                old_match_status = existing["match_status"]
                old_pay_status = existing["status"]
                old_operator = None

                with self.db._get_conn() as conn:
                    match_row = conn.execute(
                        """SELECT m.operator FROM matches m
                           JOIN payments p ON m.payment_id = p.id
                           WHERE p.id = ? AND m.status != 'revoked'
                           ORDER BY m.id DESC LIMIT 1""",
                        (existing["id"],)
                    ).fetchone()
                    if match_row:
                        old_operator = match_row["operator"]

                if abs(new_amount - old_amount) > 0.001:
                    conflict_id = self.db.insert_batch_conflict(
                        batch_id=batch_id,
                        conflict_type=CONFLICT_TYPE_AMOUNT_CHANGE,
                        record_type="payment",
                        record_no=payment_no,
                        conflict_reason=f"收款 {payment_no} 金额变更: 旧 {old_amount:.2f} -> 新 {new_amount:.2f}",
                        old_amount=old_amount,
                        new_amount=new_amount,
                        old_status=old_pay_status,
                        new_status=new_pay_status,
                    )
                    conflicts.append({
                        "conflict_id": conflict_id,
                        "conflict_type": CONFLICT_TYPE_AMOUNT_CHANGE,
                        "record_type": "payment",
                        "record_no": payment_no,
                        "conflict_reason": f"收款 {payment_no} 金额从 {old_amount:.2f} 变为 {new_amount:.2f}",
                        "old_amount": old_amount,
                        "new_amount": new_amount,
                        "old_status": old_pay_status,
                        "new_status": new_pay_status,
                        "old_operator": old_operator,
                        "new_operator": new_operator,
                    })

                if (old_pay_status != new_pay_status and old_pay_status is not None) or \
                   (old_match_status != new_match_status and old_match_status != MATCH_STATUS_UNMATCHED):
                    old_s = old_pay_status if old_pay_status != new_pay_status else old_match_status
                    new_s = new_pay_status if old_pay_status != new_pay_status else new_match_status
                    conflict_id = self.db.insert_batch_conflict(
                        batch_id=batch_id,
                        conflict_type=CONFLICT_TYPE_STATUS_CHANGE,
                        record_type="payment",
                        record_no=payment_no,
                        conflict_reason=f"收款 {payment_no} 状态冲突: 旧状态 {old_s}，新导入后为 {new_s}",
                        old_status=old_pay_status,
                        new_status=new_pay_status,
                        old_operator=old_operator,
                        new_operator=new_operator,
                    )
                    conflicts.append({
                        "conflict_id": conflict_id,
                        "conflict_type": CONFLICT_TYPE_STATUS_CHANGE,
                        "record_type": "payment",
                        "record_no": payment_no,
                        "conflict_reason": f"收款 {payment_no} 状态从 {old_s} 变为 {new_s}",
                        "old_amount": old_amount,
                        "new_amount": new_amount,
                        "old_status": old_pay_status,
                        "new_status": new_pay_status,
                        "old_operator": old_operator,
                        "new_operator": new_operator,
                    })

                if old_operator and new_operator and old_operator != new_operator and old_match_status == MATCH_STATUS_MATCHED:
                    conflict_id = self.db.insert_batch_conflict(
                        batch_id=batch_id,
                        conflict_type=CONFLICT_TYPE_DUPLICATE_PROCESS,
                        record_type="payment",
                        record_no=payment_no,
                        conflict_reason=f"收款 {payment_no} 已被 {old_operator} 处理，现由 {new_operator} 重新处理",
                        old_status=old_pay_status,
                        new_status=new_pay_status,
                        old_operator=old_operator,
                        new_operator=new_operator,
                    )
                    conflicts.append({
                        "conflict_id": conflict_id,
                        "conflict_type": CONFLICT_TYPE_DUPLICATE_PROCESS,
                        "record_type": "payment",
                        "record_no": payment_no,
                        "conflict_reason": f"收款 {payment_no} 已被 {old_operator} 处理，现由 {new_operator} 重新处理",
                        "old_amount": old_amount,
                        "new_amount": new_amount,
                        "old_status": old_pay_status,
                        "new_status": new_pay_status,
                        "old_operator": old_operator,
                        "new_operator": new_operator,
                    })

        for c in conflicts:
            if c["record_type"] == "invoice":
                label_map = STATUS_LABEL_INVOICE
            else:
                label_map = STATUS_LABEL_PAYMENT
            if c["old_status"] is not None:
                c["old_status"] = label_map.get(c["old_status"], c["old_status"])
            if c["new_status"] is not None:
                c["new_status"] = label_map.get(c["new_status"], c["new_status"])

        return conflicts

    def _validate_and_insert_row(self, batch_id: int, file_row_num: int,
                                 row: Dict, file_type: str,
                                 required_columns: List[str]) -> Dict:
        raw_data = json.dumps(row, ensure_ascii=False)

        for col in required_columns:
            if not row.get(col):
                return {
                    "success": False,
                    "error_type": ERROR_TYPE_MISSING_REQUIRED,
                    "error_message": f"缺少必填字段: {col}"
                }

        if file_type == "invoice":
            return self._validate_and_insert_invoice(
                batch_id, file_row_num, row, raw_data
            )
        else:
            return self._validate_and_insert_payment(
                batch_id, file_row_num, row, raw_data
            )

    def _validate_and_insert_invoice(self, batch_id: int, file_row_num: int,
                                     row: Dict, raw_data: str) -> Dict:
        invoice_no = row["invoice_no"].strip()
        invoice_date_str = row["invoice_date"].strip()
        customer = row["customer"].strip()
        amount_str = row["amount"].strip()

        if not customer:
            return {
                "success": False,
                "error_type": ERROR_TYPE_MISSING_CUSTOMER,
                "error_message": "客户名称为空"
            }

        try:
            amount = float(amount_str)
            if amount <= 0:
                return {
                    "success": False,
                    "error_type": ERROR_TYPE_INVALID_AMOUNT,
                    "error_message": f"金额非法: {amount_str}，必须为正数"
                }
        except (ValueError, TypeError):
            return {
                "success": False,
                "error_type": ERROR_TYPE_INVALID_AMOUNT,
                "error_message": f"金额格式错误: {amount_str}"
            }

        invoice_date = self._parse_date(invoice_date_str)
        if not invoice_date:
            self.db.insert_invoice(
                batch_id, file_row_num, invoice_no, None, customer,
                amount, raw_data, INVOICE_STATUS_INVALID,
                f"日期格式错误: {invoice_date_str}"
            )
            return {
                "success": False,
                "error_type": ERROR_TYPE_INVALID_DATE,
                "error_message": f"日期格式错误: {invoice_date_str}，请使用 YYYY-MM-DD 或 YYYY/MM/DD 格式"
            }

        status_raw = row.get("status", "正常")
        status = STATUS_MAP_INVOICE.get(status_raw, INVOICE_STATUS_NORMAL)
        self.db.insert_invoice(
            batch_id, file_row_num, invoice_no, invoice_date, customer,
            amount, raw_data, status
        )
        return {"success": True}

    def _validate_and_insert_payment(self, batch_id: int, file_row_num: int,
                                     row: Dict, raw_data: str) -> Dict:
        payment_no = row["payment_no"].strip()
        payment_date_str = row["payment_date"].strip()
        customer = row["customer"].strip()
        amount_str = row["amount"].strip()

        if not customer:
            return {
                "success": False,
                "error_type": ERROR_TYPE_MISSING_CUSTOMER,
                "error_message": "客户名称为空"
            }

        try:
            amount = float(amount_str)
            if amount <= 0:
                return {
                    "success": False,
                    "error_type": ERROR_TYPE_INVALID_AMOUNT,
                    "error_message": f"金额非法: {amount_str}，必须为正数"
                }
        except (ValueError, TypeError):
            return {
                "success": False,
                "error_type": ERROR_TYPE_INVALID_AMOUNT,
                "error_message": f"金额格式错误: {amount_str}"
            }

        payment_date = self._parse_date(payment_date_str)
        if not payment_date:
            self.db.insert_payment(
                batch_id, file_row_num, payment_no, None, customer,
                amount, raw_data, PAYMENT_STATUS_INVALID,
                f"日期格式错误: {payment_date_str}"
            )
            return {
                "success": False,
                "error_type": ERROR_TYPE_INVALID_DATE,
                "error_message": f"日期格式错误: {payment_date_str}，请使用 YYYY-MM-DD 或 YYYY/MM/DD 格式"
            }

        status_raw = row.get("status", "正常")
        status = STATUS_MAP_PAYMENT.get(status_raw, PAYMENT_STATUS_NORMAL)
        self.db.insert_payment(
            batch_id, file_row_num, payment_no, payment_date, customer,
            amount, raw_data, status
        )
        return {"success": True}

    @staticmethod
    def _parse_date(date_str: str) -> Optional[str]:
        date_formats = [
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%Y%m%d",
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
        ]

        for fmt in date_formats:
            try:
                parsed = datetime.strptime(date_str.strip(), fmt)
                return parsed.strftime("%Y-%m-%d")
            except ValueError:
                continue

        return None

    def preview_csv(self, file_path: str, file_type: str, rows: int = 5) -> Dict:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        required_columns = (
            self.config.invoice_required_columns
            if file_type == "invoice"
            else self.config.payment_required_columns
        )

        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            preview_rows = []
            for i, row in enumerate(reader):
                if i >= rows:
                    break
                preview_rows.append(row)

            missing_cols = [c for c in required_columns if c not in headers]

            return {
                "headers": headers,
                "required_columns": required_columns,
                "missing_columns": missing_cols,
                "preview_rows": preview_rows,
                "total_rows": sum(1 for _ in reader) + len(preview_rows)
            }
