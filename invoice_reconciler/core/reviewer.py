import json
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from .database import (
    Database,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_PENDING,
    MATCH_STATUS_EXCEPTION,
    MATCH_STATUS_REVOKED,
)
from .exporter import STATUS_LABELS, MATCH_TYPE_LABELS
from .workflow import LOCK_STATUS_LABELS


SNAPSHOT_TYPE_MANUAL = "manual"
SNAPSHOT_TYPE_POST_IMPORT = "post_import"
SNAPSHOT_TYPE_POST_MATCH = "post_match"
SNAPSHOT_TYPE_POST_CONFIRM = "post_confirm"
SNAPSHOT_TYPE_POST_REVOKE = "post_revoke"
SNAPSHOT_TYPE_REPLAY = "replay"

SNAPSHOT_TYPE_LABELS = {
    SNAPSHOT_TYPE_MANUAL: "手动生成",
    SNAPSHOT_TYPE_POST_IMPORT: "导入后",
    SNAPSHOT_TYPE_POST_MATCH: "匹配后",
    SNAPSHOT_TYPE_POST_CONFIRM: "确认后",
    SNAPSHOT_TYPE_POST_REVOKE: "撤销后",
    SNAPSHOT_TYPE_REPLAY: "回放校验",
}


class ReviewSnapshot:
    def __init__(self, db: Database):
        self.db = db

    def create_snapshot(self, snapshot_type: str = SNAPSHOT_TYPE_MANUAL,
                        description: str = None, operator: str = None) -> Dict:
        snapshot = self.db.create_review_snapshot(snapshot_type, description, operator)
        return snapshot

    def list_snapshots(self, limit: int = 50) -> List[Dict]:
        snapshots = self.db.list_snapshots(limit)
        for s in snapshots:
            s["type_label"] = SNAPSHOT_TYPE_LABELS.get(
                s["snapshot_type"], s["snapshot_type"]
            )
        return snapshots

    def get_snapshot(self, snapshot_no: str = None, snapshot_id: int = None) -> Optional[Dict]:
        if snapshot_no:
            snapshot = self.db.get_snapshot_by_no(snapshot_no)
        elif snapshot_id:
            snapshot = self.db.get_snapshot_by_id(snapshot_id)
        else:
            return None

        if not snapshot:
            return None

        snapshot["type_label"] = SNAPSHOT_TYPE_LABELS.get(
            snapshot["snapshot_type"], snapshot["snapshot_type"]
        )
        snapshot["items"] = self.db.get_snapshot_items(snapshot["id"])
        return snapshot

    def replay_verify(self, snapshot_no: str = None, snapshot_id: int = None) -> Dict:
        snapshot = self.get_snapshot(snapshot_no, snapshot_id)
        if not snapshot:
            raise ValueError("快照不存在")

        current_matches = self.db.get_matches_by_status()
        current_match_map = {m["match_no"]: m for m in current_matches}

        snapshot_items = snapshot["items"]
        snapshot_match_map = {item["match_no"]: item for item in snapshot_items}

        all_match_nos = set(current_match_map.keys()) | set(snapshot_match_map.keys())

        differences = []
        matched_count = 0
        new_in_current = 0
        missing_in_current = 0
        status_changed = 0

        for match_no in sorted(all_match_nos):
            current = current_match_map.get(match_no)
            snapshot_item = snapshot_match_map.get(match_no)

            if current and snapshot_item:
                current_status = current["status"]
                snapshot_status = snapshot_item["status"]
                if current_status == snapshot_status:
                    matched_count += 1
                else:
                    status_changed += 1
                    differences.append({
                        "match_no": match_no,
                        "type": "status_changed",
                        "snapshot_status": snapshot_status,
                        "snapshot_status_label": STATUS_LABELS.get(snapshot_status, snapshot_status),
                        "current_status": current_status,
                        "current_status_label": STATUS_LABELS.get(current_status, current_status),
                        "snapshot_operator": snapshot_item["operator"],
                        "current_operator": current["operator"],
                        "invoice_no": current["invoice_no"],
                        "payment_no": current["payment_no"],
                    })
            elif current and not snapshot_item:
                new_in_current += 1
                differences.append({
                    "match_no": match_no,
                    "type": "new_in_current",
                    "current_status": current["status"],
                    "current_status_label": STATUS_LABELS.get(current["status"], current["status"]),
                    "current_operator": current["operator"],
                    "invoice_no": current["invoice_no"],
                    "payment_no": current["payment_no"],
                })
            elif not current and snapshot_item:
                missing_in_current += 1
                differences.append({
                    "match_no": match_no,
                    "type": "missing_in_current",
                    "snapshot_status": snapshot_item["status"],
                    "snapshot_status_label": STATUS_LABELS.get(snapshot_item["status"], snapshot_item["status"]),
                    "snapshot_operator": snapshot_item["operator"],
                    "invoice_no": snapshot_item["invoice_no"],
                    "payment_no": snapshot_item["payment_no"],
                })

        return {
            "snapshot_no": snapshot["snapshot_no"],
            "snapshot_type": snapshot["snapshot_type"],
            "snapshot_type_label": snapshot["type_label"],
            "snapshot_created_at": snapshot["created_at"],
            "snapshot_total": len(snapshot_items),
            "current_total": len(current_matches),
            "matched": matched_count,
            "status_changed": status_changed,
            "new_in_current": new_in_current,
            "missing_in_current": missing_in_current,
            "differences": differences,
            "is_consistent": len(differences) == 0,
        }

    def check_conflicts(self, invoice_no: str = None) -> List[Dict]:
        conflicts = self.db.check_invoice_conflicts(invoice_no)
        for conflict in conflicts:
            for m in conflict["matches"]:
                m["status_label"] = STATUS_LABELS.get(m["status"], m["status"])
                m["match_type_label"] = MATCH_TYPE_LABELS.get(m["match_type"], m["match_type"])
        return conflicts

    def get_snapshot_for_export(self, snapshot_no: str = None, snapshot_id: int = None) -> Dict:
        snapshot = self.get_snapshot(snapshot_no, snapshot_id)
        if not snapshot:
            raise ValueError("快照不存在")

        export_items = []
        for item in snapshot["items"]:
            history = item.get("status_history", [])
            history_str = "; ".join([
                f"{h['changed_at']}: {STATUS_LABELS.get(h['old_status'], h['old_status'])} -> "
                f"{STATUS_LABELS.get(h['new_status'], h['new_status'])} "
                f"(操作人: {h['operator'] or '系统'}, 备注: {h['remark'] or '-'})"
                for h in history
            ])

            candidates = item.get("candidate_payments", [])
            candidate_str = "; ".join([
                f"{c['payment_no']}(金额:{c['pay_amount']},得分:{c['match_score']:.1f})"
                for c in candidates
            ])

            lock_history = item.get("lock_history", [])
            lock_history_str = "; ".join([
                f"{lh['created_at']}: {LOCK_STATUS_LABELS.get(lh['action'], lh['action'])} "
                f"(操作人: {lh['operator']}, 原持有人: {lh['old_owner'] or '-'}, "
                f"新持有人: {lh['new_owner'] or '-'}, 原因: {lh['reason'] or '-'})"
                for lh in lock_history
            ])

            last_confirm_evidence = item.get("last_confirm_evidence")
            if isinstance(last_confirm_evidence, dict):
                last_confirm_evidence_str = (
                    f"确认人: {last_confirm_evidence.get('operator', '-')}, "
                    f"备注: {last_confirm_evidence.get('remark', '-')}, "
                    f"确认时间: {last_confirm_evidence.get('confirmed_at', '-')}, "
                    f"匹配类型: {MATCH_TYPE_LABELS.get(last_confirm_evidence.get('match_type'), last_confirm_evidence.get('match_type', '-'))}, "
                    f"得分: {last_confirm_evidence.get('match_score', '-')}, "
                    f"证据: {last_confirm_evidence.get('match_evidence', '-')}"
                )
            else:
                last_confirm_evidence_str = ""

            takeover_reason = ""
            if lock_history:
                for lh in lock_history:
                    if lh.get("action") == "takeover" and lh.get("reason"):
                        takeover_reason = lh["reason"]
                        break

            export_items.append({
                "匹配编号": item["match_no"],
                "匹配类型": MATCH_TYPE_LABELS.get(item["match_type"], item["match_type"]),
                "当前状态": STATUS_LABELS.get(item["status"], item["status"]),
                "匹配得分": item["match_score"],
                "匹配证据": item["match_evidence"],
                "发票号": item["invoice_no"],
                "发票日期": item["invoice_date"],
                "发票客户": item["inv_customer"],
                "发票金额": item["inv_amount"],
                "收款号": item["payment_no"],
                "收款日期": item["payment_date"],
                "收款客户": item["pay_customer"],
                "收款金额": item["pay_amount"],
                "金额差异": abs(item["inv_amount"] - item["pay_amount"]) if item["inv_amount"] and item["pay_amount"] else None,
                "操作者": item["operator"] or "",
                "人工备注": item["operator_remark"] or "",
                "确认时间": item["confirmed_at"] or "",
                "创建时间": item["created_at"] or "",
                "候选收款证据": candidate_str,
                "状态历史": history_str,
                "当前责任人": item.get("current_owner") or "",
                "锁定原因": item.get("lock_reason") or "",
                "锁定时间": item.get("locked_at") or "",
                "锁到期时间": item.get("lock_expires_at") or "",
                "锁历史": lock_history_str,
                "接管原因": takeover_reason,
                "最后确认证据": last_confirm_evidence_str,
                "快照编号": snapshot["snapshot_no"],
                "快照生成时间": snapshot["created_at"],
            })

        return {
            "snapshot_info": {
                "快照编号": snapshot["snapshot_no"],
                "快照类型": snapshot["type_label"],
                "描述": snapshot["description"] or "",
                "操作者": snapshot["operator"] or "",
                "总匹配数": snapshot["total_matches"],
                "已匹配数": snapshot["matched_count"],
                "待确认数": snapshot["pending_count"],
                "异常数": snapshot["exception_count"],
                "已撤销数": snapshot["revoked_count"],
                "发票总金额": snapshot["total_invoice_amount"],
                "收款总金额": snapshot["total_payment_amount"],
                "已匹配金额": snapshot["matched_amount"],
                "生成时间": snapshot["created_at"],
            },
            "items": export_items,
        }
