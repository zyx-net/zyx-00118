import json
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from .config import Config
from .database import (
    Database, MATCH_STATUS_MATCHED, MATCH_STATUS_PENDING, MATCH_STATUS_EXCEPTION
)
from .workflow import WorkflowManager


MATCH_TYPE_AUTO_EXACT = "auto_exact"
MATCH_TYPE_AUTO_FUZZY = "auto_fuzzy"
MATCH_TYPE_AUTO_MULTI_CANDIDATE = "auto_multi_candidate"
MATCH_TYPE_MANUAL = "manual"


class MatchEngine:
    def __init__(self, config: Config, db: Database, workflow: WorkflowManager = None):
        self.config = config
        self.db = db
        self.workflow = workflow

    def run_auto_matching(self, operator: str = None) -> Dict:
        self.db.clear_match_candidates()

        invoices = self.db.get_unmatched_invoices()
        payments = self.db.get_unmatched_payments()

        results = {
            "total_invoices": len(invoices),
            "total_payments": len(payments),
            "exact_matches": 0,
            "fuzzy_matches": 0,
            "multi_candidate_invoices": 0,
            "created_matches": 0,
            "pending_confirmations": 0,
            "auto_exact_match_nos": [],
            "pending_match_ids": []
        }

        used_payment_ids = set()

        for invoice in invoices:
            candidates = self._find_payment_candidates(invoice, payments, used_payment_ids)

            if not candidates:
                continue

            if len(candidates) == 1:
                candidate = candidates[0]
                if candidate["is_exact"]:
                    match_no = self.db.create_match(
                        invoice["id"], candidate["payment_id"],
                        MATCH_TYPE_AUTO_EXACT, candidate["score"],
                        candidate["evidence"], MATCH_STATUS_MATCHED,
                        operator, "自动精确匹配"
                    )
                    results["exact_matches"] += 1
                    results["created_matches"] += 1
                    results["auto_exact_match_nos"].append(match_no)
                    used_payment_ids.add(candidate["payment_id"])
                else:
                    match_no = self.db.create_match(
                        invoice["id"], candidate["payment_id"],
                        MATCH_TYPE_AUTO_FUZZY, candidate["score"],
                        candidate["evidence"], MATCH_STATUS_PENDING,
                        None, "自动模糊匹配，需人工确认"
                    )
                    results["fuzzy_matches"] += 1
                    results["created_matches"] += 1
                    results["pending_confirmations"] += 1
                    results["pending_match_ids"].append(match_no)
                    used_payment_ids.add(candidate["payment_id"])
            else:
                for candidate in candidates:
                    self.db.insert_match_candidate(
                        invoice["id"], candidate["payment_id"],
                        candidate["score"], candidate["evidence"]
                    )
                match_no = self.db.create_match(
                    invoice["id"], candidates[0]["payment_id"],
                    MATCH_TYPE_AUTO_MULTI_CANDIDATE, candidates[0]["score"],
                    f"存在 {len(candidates)} 个候选收款，需人工确认",
                    MATCH_STATUS_PENDING, None,
                    f"多候选匹配，共 {len(candidates)} 个候选"
                )
                results["multi_candidate_invoices"] += 1
                results["created_matches"] += 1
                results["pending_confirmations"] += 1
                results["pending_match_ids"].append(match_no)

        return results

    def _find_payment_candidates(self, invoice: Dict, payments: List[Dict],
                                 used_payment_ids: set) -> List[Dict]:
        candidates = []
        invoice_date = datetime.strptime(invoice["invoice_date"], "%Y-%m-%d")
        date_window = timedelta(days=self.config.date_window_days)

        for payment in payments:
            if payment["id"] in used_payment_ids:
                continue

            score, evidence, is_exact = self._calculate_match_score(
                invoice, payment, invoice_date, date_window
            )

            if score > 0:
                candidates.append({
                    "payment_id": payment["id"],
                    "payment_no": payment["payment_no"],
                    "score": score,
                    "evidence": evidence,
                    "is_exact": is_exact
                })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:5]

    def _calculate_match_score(self, invoice: Dict, payment: Dict,
                               invoice_date: datetime,
                               date_window: timedelta) -> Tuple[float, str, bool]:
        score = 0.0
        evidence_parts = []
        is_exact = True

        inv_customer = (invoice["customer"] or "").strip().lower()
        pay_customer = (payment["customer"] or "").strip().lower()

        if inv_customer and pay_customer:
            if inv_customer == pay_customer:
                score += 50
                evidence_parts.append("客户完全匹配")
            elif inv_customer in pay_customer or pay_customer in inv_customer:
                score += 30
                evidence_parts.append("客户模糊匹配")
                is_exact = False
            else:
                return 0, "", False
        else:
            return 0, "", False

        amount_diff = abs(invoice["amount"] - payment["amount"])
        if amount_diff <= self.config.amount_tolerance:
            score += 40
            evidence_parts.append(f"金额精确匹配 (差异: {amount_diff:.2f})")
        elif amount_diff <= max(invoice["amount"], payment["amount"]) * 0.01:
            score += 25
            evidence_parts.append(f"金额接近 (差异: {amount_diff:.2f})")
            is_exact = False
        else:
            return 0, "", False

        payment_date = datetime.strptime(payment["payment_date"], "%Y-%m-%d")
        date_diff = abs((payment_date - invoice_date).days)

        if date_diff <= date_window.days:
            score += 10 - min(date_diff, 10)
            evidence_parts.append(f"日期差异 {date_diff} 天 (窗口内)")
            if date_diff > 3:
                is_exact = False
        else:
            return 0, "", False

        evidence = "; ".join(evidence_parts) + f" (总分: {score:.1f})"
        return score, evidence, is_exact

    def confirm_match(self, match_id: int, operator: str,
                      remark: str = None, selected_payment_id: int = None) -> Dict:
        match = self.db.get_match_by_id(match_id)
        if not match:
            raise ValueError(f"匹配记录不存在: {match_id}")

        if match["status"] != MATCH_STATUS_PENDING:
            raise ValueError(
                f"只能确认待确认状态的匹配，当前状态: {match['status']}"
            )

        if self.workflow and self.config.enable_lock:
            can_operate, msg = self.workflow.can_operate_match(operator, match_id)
            if not can_operate:
                raise ValueError(msg)

        if selected_payment_id and selected_payment_id != match["payment_id"]:
            candidates = self.db.get_match_candidates(match["invoice_id"])
            valid_candidate = any(
                c["payment_id"] == selected_payment_id for c in candidates
            )
            if not valid_candidate:
                raise ValueError(
                    f"选择的收款 ID {selected_payment_id} 不是有效的候选"
                )

            from .database import Database
            with self.db._get_conn() as conn:
                conn.execute(
                    "UPDATE matches SET payment_id = ? WHERE id = ?",
                    (selected_payment_id, match_id)
                )

        self.db.confirm_match(match_id, operator, remark)

        if self.workflow and self.config.enable_lock:
            self.workflow.auto_lock_on_confirm(match_id, operator)

        return {
            "success": True,
            "match_id": match_id,
            "match_no": match["match_no"],
            "message": "匹配已确认"
        }

    def reject_match(self, match_id: int, operator: str,
                     remark: str = None) -> Dict:
        if self.workflow and self.config.enable_lock:
            can_operate, msg = self.workflow.can_operate_match(operator, match_id)
            if not can_operate:
                raise ValueError(msg)

        self.db.reject_match(match_id, operator, remark)
        return {
            "success": True,
            "match_id": match_id,
            "message": "匹配已拒绝"
        }

    def manual_match(self, invoice_id: int, payment_id: int, operator: str,
                     remark: str = None) -> Dict:
        invoice = self.db.get_invoice_by_id(invoice_id)
        if not invoice:
            raise ValueError(f"发票记录不存在: {invoice_id}")

        payment = self.db.get_payment_by_id(payment_id)
        if not payment:
            raise ValueError(f"收款记录不存在: {payment_id}")

        if invoice["match_status"] == MATCH_STATUS_MATCHED:
            raise ValueError(f"发票已匹配，无法再次匹配")

        if payment["match_status"] == MATCH_STATUS_MATCHED:
            raise ValueError(f"收款已匹配，无法再次匹配")

        invoice_date = datetime.strptime(invoice["invoice_date"], "%Y-%m-%d")
        date_window = timedelta(days=self.config.date_window_days)
        score, evidence, _ = self._calculate_match_score(
            invoice, payment, invoice_date, date_window
        )

        if score == 0:
            evidence = "人工强制匹配"
            score = 100.0

        match_no = self.db.create_match(
            invoice_id, payment_id, MATCH_TYPE_MANUAL, score,
            evidence, MATCH_STATUS_MATCHED, operator, remark
        )

        if self.workflow and self.config.enable_lock:
            self.workflow.auto_lock_on_confirm(
                self._get_match_id_by_no(match_no), operator
            )

        return {
            "success": True,
            "match_no": match_no,
            "message": "人工匹配完成"
        }

    def _get_match_id_by_no(self, match_no: str) -> Optional[int]:
        match = self.db.get_match_by_no(match_no)
        return match["id"] if match else None

    def get_match_summary(self) -> Dict:
        matches = self.db.get_matches_by_status()
        stats = self.db.get_statistics()

        matched = [m for m in matches if m["status"] == MATCH_STATUS_MATCHED]
        pending = [m for m in matches if m["status"] == MATCH_STATUS_PENDING]
        exception = [m for m in matches if m["status"] == MATCH_STATUS_EXCEPTION]

        matched_total = sum(m["inv_amount"] for m in matched)
        pending_total = sum(m["inv_amount"] for m in pending)

        return {
            "statistics": stats,
            "matched": matched,
            "pending": pending,
            "exception": exception,
            "unmatched_invoices": self.db.get_unmatched_invoices(),
            "unmatched_payments": self.db.get_unmatched_payments(),
            "matched_amount_total": matched_total,
            "pending_amount_total": pending_total
        }

    def get_multi_candidate_invoices(self) -> List[Dict]:
        result = []
        candidates = self.db.get_match_candidates()

        invoice_map = {}
        for c in candidates:
            inv_id = c["invoice_id"]
            if inv_id not in invoice_map:
                invoice_map[inv_id] = {
                    "invoice_id": inv_id,
                    "invoice_no": c["invoice_no"],
                    "invoice_date": c["invoice_date"],
                    "inv_customer": c["inv_customer"],
                    "inv_amount": c["inv_amount"],
                    "candidates": []
                }
            invoice_map[inv_id]["candidates"].append({
                "payment_id": c["payment_id"],
                "payment_no": c["payment_no"],
                "payment_date": c["payment_date"],
                "pay_customer": c["pay_customer"],
                "pay_amount": c["pay_amount"],
                "match_score": c["match_score"],
                "match_reason": c["match_reason"]
            })

        return list(invoice_map.values())
