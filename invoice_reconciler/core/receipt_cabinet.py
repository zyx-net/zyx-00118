import os
import json
import hashlib
from typing import List, Dict, Optional
from datetime import datetime
from .config import Config
from .database import Database
from .batch_workbench import BatchWorkbench


RECEIPT_STATUS_ACTIVE = "active"
RECEIPT_STATUS_RESUMED = "resumed"
RECEIPT_STATUS_ABANDONED = "abandoned"
RECEIPT_STATUS_SUPERSEDED = "superseded"

RECEIPT_STATUS_LABELS = {
    RECEIPT_STATUS_ACTIVE: "可用",
    RECEIPT_STATUS_RESUMED: "已续导",
    RECEIPT_STATUS_ABANDONED: "已放弃",
    RECEIPT_STATUS_SUPERSEDED: "已替代",
}

RECEIPT_EVENT_CREATE = "create"
RECEIPT_EVENT_READ = "read"
RECEIPT_EVENT_RESUME = "resume"
RECEIPT_EVENT_COMPARE = "compare"
RECEIPT_EVENT_ABANDON = "abandon"
RECEIPT_EVENT_SAVE_COPY = "save_copy"
RECEIPT_EVENT_REBIND = "rebind"
RECEIPT_EVENT_CLEANUP = "cleanup"
RECEIPT_EVENT_CONFLICT_DETECT = "conflict_detect"
RECEIPT_EVENT_BLOCK = "block"
RECEIPT_EVENT_CONFIRM = "confirm"
RECEIPT_EVENT_UNDO = "undo"
RECEIPT_EVENT_OVERRIDE = "override"

RECEIPT_EVENT_LABELS = {
    RECEIPT_EVENT_CREATE: "创建回执",
    RECEIPT_EVENT_READ: "读取回执",
    RECEIPT_EVENT_RESUME: "续导出",
    RECEIPT_EVENT_COMPARE: "回执对比",
    RECEIPT_EVENT_ABANDON: "放弃恢复",
    RECEIPT_EVENT_SAVE_COPY: "另存副本",
    RECEIPT_EVENT_REBIND: "重绑目标",
    RECEIPT_EVENT_CLEANUP: "清理失效",
    RECEIPT_EVENT_CONFLICT_DETECT: "冲突检测",
    RECEIPT_EVENT_BLOCK: "拦截续导",
    RECEIPT_EVENT_CONFIRM: "人工确认",
    RECEIPT_EVENT_UNDO: "撤销操作",
    RECEIPT_EVENT_OVERRIDE: "强制覆盖",
}

INTERCEPT_NEW_DATA = "new_data_imported"
INTERCEPT_FILE_MODIFIED = "file_modified"
INTERCEPT_DIR_NO_PERMISSION = "dir_no_permission"
INTERCEPT_SESSION_EXPIRED = "session_expired"
INTERCEPT_DIR_CHANGED = "dir_changed"
INTERCEPT_FILE_CONFLICT = "file_conflict"

INTERCEPT_LABELS = {
    INTERCEPT_NEW_DATA: "期间有新数据导入",
    INTERCEPT_FILE_MODIFIED: "同名文件已被修改",
    INTERCEPT_DIR_NO_PERMISSION: "导出目录无权限",
    INTERCEPT_SESSION_EXPIRED: "会话已失效",
    INTERCEPT_DIR_CHANGED: "工作目录变更",
    INTERCEPT_FILE_CONFLICT: "文件冲突",
}

HANDLE_SAVE_COPY = "save_copy"
HANDLE_ABANDON = "abandon"
HANDLE_REBIND = "rebind"
HANDLE_CLEANUP = "cleanup"

HANDLE_LABELS = {
    HANDLE_SAVE_COPY: "另存副本",
    HANDLE_ABANDON: "放弃恢复",
    HANDLE_REBIND: "重绑目标",
    HANDLE_CLEANUP: "清理失效记录",
}


class ExportReceiptCabinet:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._workbench = BatchWorkbench(config, db)

    def _compute_config_hash(self) -> str:
        config_dict = self.config.to_dict()
        raw = json.dumps(config_dict, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _generate_receipt_id(self) -> str:
        now = datetime.now()
        prefix = f"ER{now.strftime('%Y%m%d')}"
        existing = self.db.list_export_receipts(
            self._compute_config_hash(), include_inactive=True, limit=1000
        )
        today_count = sum(
            1 for r in existing
            if r.get("receipt_id", "").startswith(prefix)
        )
        seq = today_count + 1
        return f"{prefix}{seq:03d}"

    def _compute_record_fingerprints(self, log_ids: List[int] = None,
                                     batch_id: int = None) -> List[str]:
        from .change_tracker import ChangeTracker
        tracker = ChangeTracker(self.config, self.db)
        if batch_id:
            view = self._workbench.get_unified_change_view(
                batch_id=batch_id, operator="_receipt"
            )
            logs = view.get("logs", [])
        else:
            logs = []
        if log_ids:
            logs = [l for l in logs if l.get("id") in log_ids] if logs else []
        fingerprints = []
        for log in logs:
            raw = f"{log.get('record_type','')}|{log.get('record_no','')}|{log.get('change_type','')}|{log.get('batch_id','')}"
            fp = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
            fingerprints.append(fp)
        return fingerprints

    def _compute_file_hash(self, file_path: str) -> Optional[str]:
        if not file_path or not os.path.exists(file_path):
            return None
        try:
            hasher = hashlib.sha256()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()[:16]
        except (OSError, IOError):
            return None

    def _check_export_writable(self, export_dir: str) -> bool:
        if not export_dir:
            return False
        try:
            os.makedirs(export_dir, exist_ok=True)
            test_file = os.path.join(export_dir, ".receipt_write_test")
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
            return True
        except (OSError, PermissionError):
            return False

    def create_receipt(self, operator: str, target_file: str,
                       export_format: str = "json",
                       batch_id: int = None,
                       filter_snapshot: Dict = None,
                       summary_stats: Dict = None,
                       log_ids: List[int] = None,
                       subsequent_actions: List[str] = None,
                       session_id: str = None) -> Dict:
        config_hash = self._compute_config_hash()
        receipt_id = self._generate_receipt_id()

        fingerprints = self._compute_record_fingerprints(
            log_ids=log_ids, batch_id=batch_id
        )

        current_filters = self._workbench.get_filters()
        if filter_snapshot is None:
            filter_snapshot = current_filters or {}

        if summary_stats is None and batch_id:
            wb_summary = self._workbench.get_batch_workbench_summary(batch_id)
            if wb_summary.get("success") and wb_summary.get("batches"):
                summary_stats = wb_summary["batches"][0]
            else:
                summary_stats = {}
        elif summary_stats is None:
            summary_stats = {}

        if subsequent_actions is None:
            subsequent_actions = ["resume_export", "compare", "abandon"]

        file_hash = self._compute_file_hash(target_file)
        export_dir = self.config.export_dir
        working_dir = os.getcwd()

        receipt_data = {
            "status": RECEIPT_STATUS_ACTIVE,
            "operator": operator,
            "target_file": target_file,
            "export_format": export_format,
            "record_fingerprints": fingerprints,
            "filter_snapshot": filter_snapshot,
            "summary_stats": summary_stats,
            "file_hash": file_hash,
            "export_dir": export_dir,
            "working_dir": working_dir,
            "subsequent_actions": subsequent_actions,
            "session_id": session_id,
            "batch_id": batch_id,
            "hit_count": len(fingerprints),
            "exported_at": datetime.now().isoformat(),
        }

        self.db.save_export_receipt(receipt_id, config_hash, receipt_data)
        self.db.log_receipt_timeline_event(
            receipt_id, RECEIPT_EVENT_CREATE, operator,
            status="success",
            result_summary=f"创建回执 {receipt_id}",
            event_details={
                "target_file": target_file,
                "export_format": export_format,
                "hit_count": len(fingerprints),
                "batch_id": batch_id,
                "file_hash": file_hash,
            },
            event_label=RECEIPT_EVENT_LABELS[RECEIPT_EVENT_CREATE]
        )

        return {
            "success": True,
            "receipt_id": receipt_id,
            "config_hash": config_hash,
            "status": RECEIPT_STATUS_ACTIVE,
            "target_file": target_file,
            "export_format": export_format,
            "hit_count": len(fingerprints),
            "exported_at": receipt_data["exported_at"],
        }

    def read_receipt(self, receipt_id: str, operator: str = None) -> Dict:
        receipt = self.db.get_export_receipt(receipt_id)
        if not receipt:
            return {"success": False, "message": "回执不存在"}

        file_alive = True
        file_modified = False
        current_file_hash = None
        target_file = receipt.get("target_file", "")
        if target_file:
            current_file_hash = self._compute_file_hash(target_file)
            if not os.path.exists(target_file):
                file_alive = False
            elif receipt.get("file_hash") and current_file_hash != receipt.get("file_hash"):
                file_modified = True

        result = {
            "success": True,
            "receipt_id": receipt_id,
            "status": receipt.get("status"),
            "operator": receipt.get("operator"),
            "target_file": target_file,
            "export_format": receipt.get("export_format"),
            "hit_count": receipt.get("hit_count", 0),
            "filter_snapshot": receipt.get("filter_snapshot", {}),
            "summary_stats": receipt.get("summary_stats", {}),
            "exported_at": receipt.get("exported_at"),
            "created_at": receipt.get("created_at"),
            "batch_id": receipt.get("batch_id"),
            "subsequent_actions": receipt.get("subsequent_actions", []),
            "file_alive": file_alive,
            "file_modified": file_modified,
            "current_file_hash": current_file_hash,
            "original_file_hash": receipt.get("file_hash"),
            "working_dir": receipt.get("working_dir"),
            "export_dir": receipt.get("export_dir"),
        }

        self.db.log_receipt_timeline_event(
            receipt_id, RECEIPT_EVENT_READ, operator or "system",
            status="success",
            result_summary=f"读取回执 {receipt_id}",
            event_details={
                "file_alive": file_alive,
                "file_modified": file_modified,
            },
            event_label=RECEIPT_EVENT_LABELS[RECEIPT_EVENT_READ]
        )

        return result

    def check_interceptions(self, receipt_id: str) -> Dict:
        receipt = self.db.get_export_receipt(receipt_id)
        if not receipt:
            return {"success": False, "message": "回执不存在"}

        interceptions = []

        current_hash = self._compute_config_hash()
        if receipt.get("config_hash") and receipt["config_hash"] != current_hash:
            interceptions.append({
                "type": INTERCEPT_DIR_CHANGED,
                "label": INTERCEPT_LABELS[INTERCEPT_DIR_CHANGED],
                "detail": f"当前配置哈希 {current_hash} 与回执配置哈希 {receipt['config_hash']} 不一致",
                "severity": "critical",
            })

        batch_id = receipt.get("batch_id")
        if batch_id:
            batch = self.db.get_batch(batch_id)
            if batch:
                later_batches = [
                    b for b in self.db.get_batches()
                    if b["file_type"] == batch["file_type"] and b["id"] > batch_id
                ]
                if later_batches:
                    latest = later_batches[-1]
                    interceptions.append({
                        "type": INTERCEPT_NEW_DATA,
                        "label": INTERCEPT_LABELS[INTERCEPT_NEW_DATA],
                        "detail": (
                            f"批次 #{batch_id} 之后有 {len(later_batches)} 次新导入，"
                            f"最新批次 #{latest['id']} - {latest['file_name']}"
                        ),
                        "severity": "high",
                    })

        target_file = receipt.get("target_file", "")
        if target_file:
            current_file_hash = self._compute_file_hash(target_file)
            if receipt.get("file_hash") and current_file_hash and current_file_hash != receipt["file_hash"]:
                interceptions.append({
                    "type": INTERCEPT_FILE_MODIFIED,
                    "label": INTERCEPT_LABELS[INTERCEPT_FILE_MODIFIED],
                    "detail": f"目标文件 {target_file} 已被修改",
                    "severity": "high",
                })
            if os.path.exists(target_file):
                dir_path = os.path.dirname(target_file) or target_file
            else:
                dir_path = receipt.get("export_dir") or self.config.export_dir
            if not self._check_export_writable(dir_path):
                interceptions.append({
                    "type": INTERCEPT_DIR_NO_PERMISSION,
                    "label": INTERCEPT_LABELS[INTERCEPT_DIR_NO_PERMISSION],
                    "detail": f"导出目录 {dir_path} 不可写",
                    "severity": "critical",
                })

        if receipt.get("working_dir") and receipt["working_dir"] != os.getcwd():
            interceptions.append({
                "type": INTERCEPT_DIR_CHANGED,
                "label": INTERCEPT_LABELS[INTERCEPT_DIR_CHANGED],
                "detail": (
                    f"工作目录从 {receipt['working_dir']} 变更为 {os.getcwd()}"
                ),
                "severity": "medium",
            })

        session_id = receipt.get("session_id")
        if session_id:
            session_state = self.db.get_session_state(session_id)
            if not session_state:
                interceptions.append({
                    "type": INTERCEPT_SESSION_EXPIRED,
                    "label": INTERCEPT_LABELS[INTERCEPT_SESSION_EXPIRED],
                    "detail": f"会话 {session_id} 已失效",
                    "severity": "medium",
                })

        if target_file and os.path.exists(target_file):
            receipt_dir = receipt.get("export_dir") or self.config.export_dir
            if os.path.dirname(target_file) != receipt_dir:
                interceptions.append({
                    "type": INTERCEPT_FILE_CONFLICT,
                    "label": INTERCEPT_LABELS[INTERCEPT_FILE_CONFLICT],
                    "detail": f"目标文件路径与回执记录不一致",
                    "severity": "medium",
                })

        return {
            "success": True,
            "receipt_id": receipt_id,
            "interceptions": interceptions,
            "interception_count": len(interceptions),
            "has_critical": any(
                i["severity"] == "critical" for i in interceptions
            ),
            "can_resume": not any(
                i["severity"] == "critical" for i in interceptions
            ),
        }

    def get_handling_options(self, receipt_id: str) -> Dict:
        check = self.check_interceptions(receipt_id)
        if not check["success"]:
            return check

        options = []

        options.append({
            "action": HANDLE_SAVE_COPY,
            "label": HANDLE_LABELS[HANDLE_SAVE_COPY],
            "description": "将导出结果另存为新副本，避免覆盖已有文件",
            "available": True,
        })

        options.append({
            "action": HANDLE_ABANDON,
            "label": HANDLE_LABELS[HANDLE_ABANDON],
            "description": "放弃恢复此回执，标记为已放弃",
            "available": True,
            "dangerous": True,
        })

        has_target_issue = any(
            i["type"] in (INTERCEPT_DIR_NO_PERMISSION, INTERCEPT_FILE_CONFLICT, INTERCEPT_DIR_CHANGED)
            for i in check["interceptions"]
        )
        options.append({
            "action": HANDLE_REBIND,
            "label": HANDLE_LABELS[HANDLE_REBIND],
            "description": "重新绑定导出目标目录",
            "available": has_target_issue,
        })

        has_invalid = any(
            i["type"] in (INTERCEPT_SESSION_EXPIRED, INTERCEPT_NEW_DATA)
            for i in check["interceptions"]
        )
        options.append({
            "action": HANDLE_CLEANUP,
            "label": HANDLE_LABELS[HANDLE_CLEANUP],
            "description": "清理失效的回执记录",
            "available": has_invalid,
        })

        return {
            "success": True,
            "receipt_id": receipt_id,
            "interceptions": check["interceptions"],
            "handling_options": options,
            "can_force_resume": not check["has_critical"],
        }

    def handle_save_copy(self, receipt_id: str, operator: str,
                         new_target: str = None) -> Dict:
        receipt = self.db.get_export_receipt(receipt_id)
        if not receipt:
            return {"success": False, "message": "回执不存在"}

        config_hash = self._compute_config_hash()
        new_receipt_id = self._generate_receipt_id()

        original_target = receipt.get("target_file", "")
        if new_target is None:
            base, ext = os.path.splitext(original_target)
            new_target = f"{base}_copy_{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}"

        new_file_hash = self._compute_file_hash(new_target)

        new_data = {
            "status": RECEIPT_STATUS_ACTIVE,
            "operator": operator,
            "target_file": new_target,
            "export_format": receipt.get("export_format", "json"),
            "record_fingerprints": receipt.get("record_fingerprints", []),
            "filter_snapshot": receipt.get("filter_snapshot", {}),
            "summary_stats": receipt.get("summary_stats", {}),
            "file_hash": new_file_hash,
            "export_dir": receipt.get("export_dir"),
            "working_dir": os.getcwd(),
            "subsequent_actions": receipt.get("subsequent_actions", []),
            "session_id": receipt.get("session_id"),
            "batch_id": receipt.get("batch_id"),
            "hit_count": receipt.get("hit_count", 0),
            "exported_at": datetime.now().isoformat(),
        }

        self.db.save_export_receipt(new_receipt_id, config_hash, new_data)
        self.db.update_export_receipt_status(receipt_id, RECEIPT_STATUS_SUPERSEDED)

        self.db.log_receipt_timeline_event(
            receipt_id, RECEIPT_EVENT_SAVE_COPY, operator,
            status="success",
            result_summary=f"另存副本: {new_receipt_id}",
            event_details={
                "new_receipt_id": new_receipt_id,
                "new_target": new_target,
            },
            event_label=RECEIPT_EVENT_LABELS[RECEIPT_EVENT_SAVE_COPY]
        )
        self.db.log_receipt_timeline_event(
            new_receipt_id, RECEIPT_EVENT_CREATE, operator,
            status="success",
            result_summary=f"从 {receipt_id} 创建副本",
            event_details={"source_receipt_id": receipt_id},
            event_label="创建副本"
        )

        return {
            "success": True,
            "original_receipt_id": receipt_id,
            "new_receipt_id": new_receipt_id,
            "new_target": new_target,
        }

    def handle_abandon(self, receipt_id: str, operator: str) -> Dict:
        receipt = self.db.get_export_receipt(receipt_id)
        if not receipt:
            return {"success": False, "message": "回执不存在"}

        if receipt.get("status") == RECEIPT_STATUS_ABANDONED:
            return {"success": False, "message": "回执已被放弃"}

        self.db.update_export_receipt_status(receipt_id, RECEIPT_STATUS_ABANDONED)
        self.db.log_receipt_timeline_event(
            receipt_id, RECEIPT_EVENT_ABANDON, operator,
            status="success",
            result_summary=f"放弃恢复回执 {receipt_id}",
            event_label=RECEIPT_EVENT_LABELS[RECEIPT_EVENT_ABANDON]
        )

        return {
            "success": True,
            "receipt_id": receipt_id,
            "status": RECEIPT_STATUS_ABANDONED,
        }

    def handle_rebind(self, receipt_id: str, operator: str,
                      new_export_dir: str = None,
                      new_target_file: str = None) -> Dict:
        receipt = self.db.get_export_receipt(receipt_id)
        if not receipt:
            return {"success": False, "message": "回执不存在"}

        updates = {}
        if new_export_dir:
            if not self._check_export_writable(new_export_dir):
                return {"success": False, "message": f"目录 {new_export_dir} 不可写"}
            self.db.update_export_receipt_field(receipt_id, "export_dir", new_export_dir)
            updates["export_dir"] = new_export_dir

        if new_target_file:
            self.db.update_export_receipt_field(receipt_id, "target_file", new_target_file)
            new_hash = self._compute_file_hash(new_target_file)
            if new_hash:
                self.db.update_export_receipt_field(receipt_id, "file_hash", new_hash)
            updates["target_file"] = new_target_file
            updates["file_hash"] = new_hash

        self.db.log_receipt_timeline_event(
            receipt_id, RECEIPT_EVENT_REBIND, operator,
            status="success",
            result_summary=f"重绑目标: {updates}",
            event_details=updates,
            event_label=RECEIPT_EVENT_LABELS[RECEIPT_EVENT_REBIND]
        )

        return {
            "success": True,
            "receipt_id": receipt_id,
            "updates": updates,
        }

    def handle_cleanup(self, config_hash: str = None,
                       operator: str = None) -> Dict:
        if config_hash is None:
            config_hash = self._compute_config_hash()

        receipts = self.db.list_export_receipts(config_hash, include_inactive=True)
        removed = []
        invalid = []

        for r in receipts:
            receipt_id = r.get("receipt_id")
            reasons = []

            batch_id = r.get("batch_id")
            if batch_id:
                batch = self.db.get_batch(batch_id)
                if not batch:
                    reasons.append("引用的批次不存在")

            target_file = r.get("target_file", "")
            if target_file and not os.path.exists(target_file):
                if r.get("status") == RECEIPT_STATUS_ABANDONED:
                    reasons.append("目标文件不存在且已放弃")

            if reasons:
                invalid.append({
                    "receipt_id": receipt_id,
                    "reasons": reasons,
                })
                self.db.delete_export_receipt(receipt_id)
                removed.append(receipt_id)

        if removed:
            self.db.log_receipt_timeline_event(
                "cleanup", RECEIPT_EVENT_CLEANUP, operator or "system",
                status="success",
                result_summary=f"清理了 {len(removed)} 个失效回执",
                event_details={
                    "config_hash": config_hash,
                    "removed": removed,
                },
                event_label=RECEIPT_EVENT_LABELS[RECEIPT_EVENT_CLEANUP]
            )

        return {
            "success": True,
            "removed": removed,
            "removed_count": len(removed),
            "invalid_details": invalid,
        }

    def compare_receipt(self, receipt_id: str,
                        operator: str = None) -> Dict:
        receipt = self.db.get_export_receipt(receipt_id)
        if not receipt:
            return {"success": False, "message": "回执不存在"}

        comparisons = []

        batch_id = receipt.get("batch_id")
        if batch_id:
            current_view = self._workbench.get_unified_change_view(
                batch_id=batch_id, operator=operator or "_compare"
            )
            current_logs = current_view.get("logs", [])
            current_fingerprints = self._compute_record_fingerprints(
                batch_id=batch_id
            )
        else:
            current_fingerprints = []
            current_logs = []

        receipt_fps = set(receipt.get("record_fingerprints", []))
        current_fps = set(current_fingerprints)

        if receipt_fps != current_fps:
            added = current_fps - receipt_fps
            removed = receipt_fps - current_fps
            comparisons.append({
                "field": "record_fingerprints",
                "label": "记录指纹",
                "receipt_count": len(receipt_fps),
                "current_count": len(current_fps),
                "added_count": len(added),
                "removed_count": len(removed),
                "match": False,
                "detail": (
                    f"回执 {len(receipt_fps)} 条, 当前 {len(current_fps)} 条, "
                    f"新增 {len(added)}, 缺失 {len(removed)}"
                ),
            })

        current_filters = self._workbench.get_filters() or {}
        receipt_filters = receipt.get("filter_snapshot") or {}
        if current_filters != receipt_filters:
            comparisons.append({
                "field": "filter_snapshot",
                "label": "筛选条件",
                "receipt": receipt_filters,
                "current": current_filters,
                "match": False,
            })

        target_file = receipt.get("target_file", "")
        current_file_hash = self._compute_file_hash(target_file)
        receipt_file_hash = receipt.get("file_hash")
        if target_file:
            file_alive = os.path.exists(target_file)
            file_modified = (current_file_hash != receipt_file_hash) if receipt_file_hash and current_file_hash else False
            comparisons.append({
                "field": "target_file",
                "label": "目标文件",
                "target_file": target_file,
                "file_alive": file_alive,
                "file_modified": file_modified,
                "receipt_hash": receipt_file_hash,
                "current_hash": current_file_hash,
                "match": file_alive and not file_modified,
            })

        current_export_dir = self.config.export_dir
        receipt_export_dir = receipt.get("export_dir")
        if current_export_dir != receipt_export_dir:
            comparisons.append({
                "field": "export_dir",
                "label": "导出目录",
                "receipt": receipt_export_dir,
                "current": current_export_dir,
                "match": False,
            })

        current_working_dir = os.getcwd()
        receipt_working_dir = receipt.get("working_dir")
        if receipt_working_dir and current_working_dir != receipt_working_dir:
            comparisons.append({
                "field": "working_dir",
                "label": "工作目录",
                "receipt": receipt_working_dir,
                "current": current_working_dir,
                "match": False,
            })

        config_hash = self._compute_config_hash()
        receipt_config_hash = receipt.get("config_hash")
        if receipt_config_hash and config_hash != receipt_config_hash:
            comparisons.append({
                "field": "config_hash",
                "label": "配置哈希",
                "receipt": receipt_config_hash,
                "current": config_hash,
                "match": False,
            })

        all_match = all(c.get("match", True) for c in comparisons)

        self.db.log_receipt_timeline_event(
            receipt_id, RECEIPT_EVENT_COMPARE, operator or "system",
            status="success",
            result_summary=f"回执对比: {'一致' if all_match else '存在差异'}",
            event_details={
                "comparison_count": len(comparisons),
                "all_match": all_match,
                "mismatch_fields": [
                    c["field"] for c in comparisons if not c.get("match", True)
                ],
            },
            event_label=RECEIPT_EVENT_LABELS[RECEIPT_EVENT_COMPARE]
        )

        return {
            "success": True,
            "receipt_id": receipt_id,
            "comparisons": comparisons,
            "all_match": all_match,
            "mismatch_count": sum(
                1 for c in comparisons if not c.get("match", True)
            ),
        }

    def resume_with_receipt(self, receipt_id: str, operator: str,
                            force: bool = False) -> Dict:
        check = self.check_interceptions(receipt_id)
        if not check["success"]:
            return check

        if check["interceptions"] and not force:
            self.db.log_receipt_timeline_event(
                receipt_id, RECEIPT_EVENT_BLOCK, operator,
                status="failed",
                error_message=f"存在 {len(check['interceptions'])} 个拦截项",
                event_details={
                    "interceptions": [
                        {"type": i["type"], "severity": i["severity"]}
                        for i in check["interceptions"]
                    ],
                },
                event_label=RECEIPT_EVENT_LABELS[RECEIPT_EVENT_BLOCK]
            )
            return {
                "success": False,
                "message": f"存在 {len(check['interceptions'])} 个拦截项，无法续导",
                "interceptions": check["interceptions"],
                "can_force": check["can_resume"],
            }

        if check["has_critical"] and not force:
            return {
                "success": False,
                "message": "存在严重拦截项，无法强制续导",
                "interceptions": check["interceptions"],
                "can_force": False,
            }

        receipt = self.db.get_export_receipt(receipt_id)
        if not receipt:
            return {"success": False, "message": "回执不存在"}

        self.db.update_export_receipt_status(receipt_id, RECEIPT_STATUS_RESUMED)
        self.db.log_receipt_timeline_event(
            receipt_id, RECEIPT_EVENT_RESUME, operator,
            status="success",
            result_summary=f"续导出: {receipt_id}",
            event_details={
                "forced": force,
                "target_file": receipt.get("target_file"),
                "export_format": receipt.get("export_format"),
                "interception_count": len(check["interceptions"]),
            },
            event_label=RECEIPT_EVENT_LABELS[RECEIPT_EVENT_RESUME]
        )

        batch_id = receipt.get("batch_id")
        export_format = receipt.get("export_format", "json")
        target_file = receipt.get("target_file", "")

        export_result = None
        if batch_id:
            from .change_tracker import ChangeTracker
            tracker = ChangeTracker(self.config, self.db)
            try:
                export_result = tracker.export_change_logs(
                    batch_id, operator, format=export_format
                )
                self.db.log_receipt_timeline_event(
                    receipt_id, RECEIPT_EVENT_RESUME, operator,
                    status="success" if export_result.get("success") else "partial",
                    result_summary=f"导出{'成功' if export_result.get('success') else '部分成功'}",
                    event_details={
                        "file_path": export_result.get("file_path"),
                        "total_changes": export_result.get("total_changes"),
                    },
                    event_label="导出执行"
                )
            except Exception as e:
                export_result = {"success": False, "message": str(e)}
                self.db.log_receipt_timeline_event(
                    receipt_id, RECEIPT_EVENT_RESUME, operator,
                    status="failed",
                    error_message=str(e),
                    event_label="导出失败"
                )

        return {
            "success": True,
            "receipt_id": receipt_id,
            "forced": force,
            "export_result": export_result,
            "interceptions": check["interceptions"],
        }

    def list_receipts(self, config_hash: str = None,
                      include_inactive: bool = False) -> List[Dict]:
        if not config_hash:
            config_hash = self._compute_config_hash()
        return self.db.list_export_receipts(config_hash, include_inactive)

    def get_receipt(self, receipt_id: str) -> Dict:
        receipt = self.db.get_export_receipt(receipt_id)
        if not receipt:
            return {"success": False, "message": "回执不存在"}
        return {"success": True, "receipt": receipt}

    def get_timeline(self, receipt_id: str,
                     limit: int = 50) -> List[Dict]:
        return self.db.get_receipt_timeline(receipt_id, limit)

    def find_latest_receipt(self, config_hash: str = None) -> Optional[Dict]:
        if not config_hash:
            config_hash = self._compute_config_hash()
        receipts = self.db.list_export_receipts(config_hash, include_inactive=False, limit=1)
        return receipts[0] if receipts else None
