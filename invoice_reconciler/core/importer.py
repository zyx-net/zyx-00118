import csv
import os
import json
from datetime import datetime
from typing import List, Dict, Tuple, Optional
from .config import Config
from .database import Database, INVOICE_STATUS_INVALID, PAYMENT_STATUS_INVALID


ERROR_TYPE_MISSING_COLUMN = "missing_column"
ERROR_TYPE_MISSING_CUSTOMER = "missing_customer"
ERROR_TYPE_INVALID_AMOUNT = "invalid_amount"
ERROR_TYPE_INVALID_DATE = "invalid_date"
ERROR_TYPE_MISSING_REQUIRED = "missing_required"
ERROR_TYPE_DUPLICATE = "duplicate"


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

            return {
                "success": True,
                "skipped": False,
                "batch_id": batch_id,
                "total_rows": total_rows,
                "success_rows": success_count,
                "failed_rows": failed_count,
                "message": f"导入完成：成功 {success_count} 条，失败 {failed_count} 条"
            }

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

        self.db.insert_invoice(
            batch_id, file_row_num, invoice_no, invoice_date, customer,
            amount, raw_data
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

        self.db.insert_payment(
            batch_id, file_row_num, payment_no, payment_date, customer,
            amount, raw_data
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
