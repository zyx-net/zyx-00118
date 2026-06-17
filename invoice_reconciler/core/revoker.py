from typing import List, Dict, Optional
from .database import Database, MATCH_STATUS_MATCHED, MATCH_STATUS_REVOKED
from .workflow import WorkflowManager
from .config import Config


class Revoker:
    def __init__(self, db: Database, config: Config = None, workflow: WorkflowManager = None):
        self.db = db
        self.config = config
        self.workflow = workflow

    def revoke_match(self, match_id: int, operator: str,
                     remark: str = None) -> Dict:
        match = self.db.get_match_by_id(match_id)
        if not match:
            return {
                "success": False,
                "error_type": "not_found",
                "message": f"匹配记录不存在: {match_id}"
            }

        if not operator:
            return {
                "success": False,
                "error_type": "empty_operator",
                "message": "撤销操作必须指定操作者"
            }

        # 先锁后处理：锁检查放在状态检查之前，与 confirm_match 保持一致
        if self.workflow and self.config and self.config.enable_lock:
            can_operate, msg = self.workflow.can_operate_match(operator, match_id)
            if not can_operate:
                return {
                    "success": False,
                    "error_type": "lock_violation",
                    "message": msg
                }

        if match["status"] == MATCH_STATUS_REVOKED:
            return {
                "success": False,
                "error_type": "already_revoked",
                "message": f"匹配记录 {match['match_no']} 已经被撤销，不能重复撤销"
            }

        if match["status"] != MATCH_STATUS_MATCHED:
            return {
                "success": False,
                "error_type": "invalid_status",
                "message": (
                    f"只能撤销已确认的匹配，当前状态: {match['status']}。"
                    f"匹配编号: {match['match_no']}"
                )
            }

        try:
            self.db.revoke_match(match_id, operator, remark)

            if self.workflow and self.config and self.config.enable_lock:
                self.workflow.auto_unlock_on_revoke(match_id, operator)

            return {
                "success": True,
                "match_id": match_id,
                "match_no": match["match_no"],
                "invoice_no": match["invoice_no"],
                "payment_no": match["payment_no"],
                "message": f"已撤销匹配 {match['match_no']}"
            }
        except ValueError as e:
            return {
                "success": False,
                "error_type": "error",
                "message": str(e)
            }

    def revoke_by_match_no(self, match_no: str, operator: str,
                           remark: str = None) -> Dict:
        match = self.db.get_match_by_no(match_no)
        if not match:
            return {
                "success": False,
                "error_type": "not_found",
                "message": f"匹配编号不存在: {match_no}"
            }
        return self.revoke_match(match["id"], operator, remark)

    def batch_revoke(self, match_ids: List[int], operator: str,
                     remark: str = None) -> Dict:
        if not match_ids:
            return {
                "success": False,
                "error_type": "empty_list",
                "message": "撤销列表为空，请指定要撤销的匹配ID"
            }

        if not operator:
            return {
                "success": False,
                "error_type": "empty_operator",
                "message": "撤销操作必须指定操作者"
            }

        results = []
        success_count = 0
        failed_count = 0

        for match_id in match_ids:
            result = self.revoke_match(match_id, operator, remark)
            results.append(result)
            if result["success"]:
                success_count += 1
            else:
                failed_count += 1

        return {
            "success": True,
            "total": len(match_ids),
            "success_count": success_count,
            "failed_count": failed_count,
            "results": results,
            "message": f"批量撤销完成：成功 {success_count} 条，失败 {failed_count} 条"
        }

    def get_revokable_matches(self) -> List[Dict]:
        matches = self.db.get_matches_by_status(MATCH_STATUS_MATCHED)
        return matches

    def get_revoked_matches(self) -> List[Dict]:
        matches = self.db.get_matches_by_status(MATCH_STATUS_REVOKED)
        return matches

    def get_match_status_history(self, match_id: int = None,
                                 match_no: str = None) -> List[Dict]:
        if match_no and not match_id:
            match = self.db.get_match_by_no(match_no)
            if match:
                match_id = match["id"]

        if not match_id:
            return []

        return self.db.get_status_history(match_id=match_id)
