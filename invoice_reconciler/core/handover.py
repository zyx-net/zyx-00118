import os
import json
import hashlib
from typing import List, Dict, Optional, Any
from datetime import datetime
from .config import Config
from .database import Database
from .batch_workbench import (
    BatchWorkbench,
    SESSION_KEY_LAST_BATCH,
    SESSION_KEY_FILTER_OPERATOR,
    SESSION_KEY_LAST_EXPORT,
    SESSION_KEY_LAST_CHANGE_VIEW,
)


HANDOVER_STATUS_ACTIVE = "active"
HANDOVER_STATUS_DISCARDED = "discarded"
HANDOVER_STATUS_RESTORED = "restored"

HANDOVER_EVENT_CREATE = "create"
HANDOVER_EVENT_RESTORE = "restore"
HANDOVER_EVENT_UNDO = "undo"
HANDOVER_EVENT_DISCARD = "discard"
HANDOVER_EVENT_SAVE_COPY = "save_copy"
HANDOVER_EVENT_CLEANUP = "cleanup"

HANDOVER_CONFLICT_REIMPORT = "data_reimported"
HANDOVER_CONFLICT_CONFIG_MISMATCH = "config_mismatch"
HANDOVER_CONFLICT_EXPORT_NOT_WRITABLE = "export_not_writable"

HANDOVER_CONFLICT_LABELS = {
    HANDOVER_CONFLICT_REIMPORT: "数据已重新导入",
    HANDOVER_CONFLICT_CONFIG_MISMATCH: "工作目录配置变更",
    HANDOVER_CONFLICT_EXPORT_NOT_WRITABLE: "导出目录不可写",
}


class BatchHandover:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._workbench = BatchWorkbench(config, db)

    def _compute_config_hash(self) -> str:
        config_dict = self.config.to_dict()
        raw = json.dumps(config_dict, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _generate_package_id(self) -> str:
        now = datetime.now()
        prefix = f"HP{now.strftime('%Y%m%d')}"
        existing = self.db.list_handover_packages(self._compute_config_hash(), include_discarded=True)
        today_count = sum(
            1 for p in existing
            if p.get("package_id", "").startswith(prefix)
        )
        seq = today_count + 1
        return f"{prefix}{seq:03d}"

    def _collect_current_state(self) -> Dict:
        last_batch = self._workbench.get_last_selected_batch()
        filters = self._workbench.get_filters()
        change_view = self._workbench.get_last_change_view_context()
        export_ctx = self._workbench.get_last_export_context()

        stats = {}
        batch_id = None
        if last_batch and last_batch.get("batch_id"):
            batch_id = last_batch["batch_id"]
            summary = self._workbench.get_batch_workbench_summary(batch_id)
            if summary.get("success") and summary.get("batches"):
                stats = summary["batches"][0]

        return {
            "last_selected_batch": last_batch,
            "filters": filters,
            "change_view_context": change_view,
            "export_context": export_ctx,
            "stats": stats,
        }

    def _apply_state(self, state: Dict, operator: str) -> Dict:
        applied = []

        batch_info = state.get("last_selected_batch")
        if batch_info and batch_info.get("batch_id"):
            self._workbench.save_last_selected_batch(batch_info["batch_id"], operator)
            applied.append("last_selected_batch")

        filters = state.get("filters")
        if filters:
            self._workbench.save_filters(
                operator=filters.get("operator"),
                status=filters.get("status"),
                impact_filter=filters.get("impact_filter"),
                with_conflicts_only=filters.get("with_conflicts_only"),
            )
            applied.append("filters")
        else:
            self.db.clear_session_state(SESSION_KEY_FILTER_OPERATOR)

        change_view = state.get("change_view_context")
        if change_view:
            self._workbench.save_change_view_context(
                batch_id=change_view.get("batch_id"),
                change_type=change_view.get("change_type"),
                impact_type=change_view.get("impact_type"),
                processing_status=change_view.get("processing_status"),
                record_no=change_view.get("record_no"),
                affect_filter=change_view.get("affect_filter"),
                with_conflicts_only=change_view.get("with_conflicts_only"),
                operator=operator,
            )
            applied.append("change_view_context")
        else:
            self.db.clear_session_state(SESSION_KEY_LAST_CHANGE_VIEW)

        export_ctx = state.get("export_context")
        if export_ctx:
            self._workbench.save_export_context(
                batch_id=export_ctx.get("batch_id"),
                export_type=export_ctx.get("export_type"),
                format=export_ctx.get("format"),
                operator=operator,
                filters=export_ctx.get("filters"),
                extra=export_ctx.get("extra"),
            )
            applied.append("export_context")
        else:
            self.db.clear_session_state(SESSION_KEY_LAST_EXPORT)

        return {"applied": applied}

    def _check_export_writable(self, export_dir: str) -> bool:
        if not export_dir:
            return False
        try:
            os.makedirs(export_dir, exist_ok=True)
            test_file = os.path.join(export_dir, ".handover_write_test")
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
            return True
        except (OSError, PermissionError):
            return False

    def _check_conflicts(self, package_id: str) -> List[Dict]:
        conflicts = []
        package = self.db.get_handover_package(package_id)
        if not package:
            return [{"conflict_type": "package_not_found", "message": "交接包不存在"}]

        package_data = self._extract_package_state(package)

        current_hash = self._compute_config_hash()
        package_hash = package.get("config_hash", "")
        if package_hash and package_hash != current_hash:
            conflicts.append({
                "conflict_type": HANDOVER_CONFLICT_CONFIG_MISMATCH,
                "message": HANDOVER_CONFLICT_LABELS[HANDOVER_CONFLICT_CONFIG_MISMATCH],
                "detail": f"当前配置哈希 {current_hash} 与交接包配置哈希 {package_hash} 不一致",
            })

        batch_info = package_data.get("last_selected_batch")
        if batch_info and batch_info.get("batch_id"):
            batch = self.db.get_batch(batch_info["batch_id"])
            if batch:
                batch_id = batch["id"]
                reimport_batches = [
                    b for b in self.db.get_batches()
                    if b["file_type"] == batch["file_type"]
                    and b["id"] > batch_id
                ]
                if reimport_batches:
                    latest = reimport_batches[-1]
                    conflicts.append({
                        "conflict_type": HANDOVER_CONFLICT_REIMPORT,
                        "message": HANDOVER_CONFLICT_LABELS[HANDOVER_CONFLICT_REIMPORT],
                        "detail": (
                            f"批次 #{batch_id} 之后有新的导入 "
                            f"（批次 #{latest['id']}，文件 {latest['file_name']}），"
                            f"数据可能已变更"
                        ),
                    })

        export_ctx = package_data.get("export_context")
        if export_ctx:
            export_dir = export_ctx.get("export_dir") or self.config.export_dir
            if not self._check_export_writable(export_dir):
                conflicts.append({
                    "conflict_type": HANDOVER_CONFLICT_EXPORT_NOT_WRITABLE,
                    "message": HANDOVER_CONFLICT_LABELS[HANDOVER_CONFLICT_EXPORT_NOT_WRITABLE],
                    "detail": f"导出目录 {export_dir} 不可写",
                })

        return conflicts

    def create_package(self, operator: str, batch_id: int = None,
                       description: str = None) -> Dict:
        current_state = self._collect_current_state()
        current_batch_id = None
        current_batch = current_state.get("last_selected_batch")
        if current_batch and isinstance(current_batch, dict):
            current_batch_id = current_batch.get("batch_id")
        if batch_id and current_batch_id != batch_id:
            self._workbench.save_last_selected_batch(batch_id, operator)
            current_state = self._collect_current_state()

        config_hash = self._compute_config_hash()
        package_id = self._generate_package_id()

        package_data = {
            "last_selected_batch": current_state.get("last_selected_batch"),
            "filters": current_state.get("filters"),
            "change_view_context": current_state.get("change_view_context"),
            "export_context": current_state.get("export_context"),
            "stats": current_state.get("stats"),
        }

        package_record = {
            "package_id": package_id,
            "config_hash": config_hash,
            "status": HANDOVER_STATUS_ACTIVE,
            "operator": operator,
            "description": description,
            "package_data": package_data,
            "created_at": datetime.now().isoformat(),
        }

        self.db.save_handover_package(package_id, config_hash, package_record)
        self.db.log_handover_event(
            HANDOVER_EVENT_CREATE, package_id, operator,
            json.dumps({"description": description}, ensure_ascii=False)
        )

        return {
            "success": True,
            "package_id": package_id,
            "config_hash": config_hash,
            "status": HANDOVER_STATUS_ACTIVE,
            "created_at": package_record["created_at"],
        }

    def _extract_package_state(self, package: Dict) -> Dict:
        package_data = package.get("package_data", {})
        if isinstance(package_data, str):
            try:
                package_data = json.loads(package_data)
            except (json.JSONDecodeError, TypeError):
                package_data = {}
        if isinstance(package_data, dict) and "package_data" in package_data:
            inner = package_data.get("package_data", {})
            if isinstance(inner, dict):
                return inner
        return package_data

    def restore_package(self, package_id: str, operator: str,
                        force: bool = False) -> Dict:
        package = self.db.get_handover_package(package_id)
        if not package:
            return {"success": False, "message": "交接包不存在"}

        package_data = self._extract_package_state(package)

        if package.get("status") == HANDOVER_STATUS_DISCARDED:
            return {"success": False, "message": "该交接包已被废弃，无法恢复"}

        conflicts = self._check_conflicts(package_id)
        if conflicts and not force:
            return {
                "success": False,
                "message": "存在冲突，请确认后强制恢复",
                "conflicts": conflicts,
            }

        current_state = self._collect_current_state()
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
        undo_id = f"UNDO_{package_id}_{timestamp}"
        self.db.save_handover_undo(undo_id, package_id, current_state, operator)

        apply_result = self._apply_state(package_data, operator)

        self.db.save_handover_package(package_id, package.get("config_hash", ""), {
            **package,
            "status": HANDOVER_STATUS_RESTORED,
        })
        self.db.log_handover_event(
            HANDOVER_EVENT_RESTORE, package_id, operator,
            json.dumps({
                "undo_id": undo_id,
                "conflicts": conflicts,
                "applied": apply_result.get("applied", []),
                "forced": force,
            }, ensure_ascii=False)
        )

        return {
            "success": True,
            "package_id": package_id,
            "undo_id": undo_id,
            "conflicts": conflicts,
            "applied": apply_result.get("applied", []),
        }

    def preview_package(self, package_id: str) -> Dict:
        package = self.db.get_handover_package(package_id)
        if not package:
            return {"success": False, "message": "交接包不存在"}

        package_data = self._extract_package_state(package)

        conflicts = self._check_conflicts(package_id)

        batch_info = package_data.get("last_selected_batch")
        filters = package_data.get("filters")
        change_view = package_data.get("change_view_context")
        export_ctx = package_data.get("export_context")
        stats = package_data.get("stats")

        has_filters = bool(filters and any(
            v is not None and v is not False
            for v in filters.values()
        ))

        preview = {
            "package_id": package_id,
            "status": package.get("status"),
            "operator": package.get("operator"),
            "description": package.get("description"),
            "created_at": package.get("created_at"),
            "config_hash": package.get("config_hash"),
            "will_restore_batch": batch_info is not None,
            "batch_info": batch_info,
            "will_restore_filters": has_filters,
            "filters": filters,
            "will_restore_change_view": change_view is not None,
            "change_view_context": change_view,
            "will_restore_export": export_ctx is not None,
            "export_context": export_ctx,
            "stats": stats,
            "conflicts": conflicts,
            "has_conflicts": len(conflicts) > 0,
        }

        return {"success": True, "preview": preview}

    def diff_package(self, package_id: str) -> Dict:
        package = self.db.get_handover_package(package_id)
        if not package:
            return {"success": False, "message": "交接包不存在"}

        package_data = self._extract_package_state(package)

        current_state = self._collect_current_state()
        changes = []

        current_batch = current_state.get("last_selected_batch")
        package_batch = package_data.get("last_selected_batch")
        if current_batch != package_batch:
            changes.append({
                "field": "last_selected_batch",
                "before": current_batch,
                "after": package_batch,
            })

        current_filters = current_state.get("filters", {})
        package_filters = package_data.get("filters", {})
        if current_filters != package_filters:
            changes.append({
                "field": "filters",
                "before": current_filters,
                "after": package_filters,
            })

        current_cv = current_state.get("change_view_context")
        package_cv = package_data.get("change_view_context")
        if current_cv != package_cv:
            changes.append({
                "field": "change_view_context",
                "before": current_cv,
                "after": package_cv,
            })

        current_export = current_state.get("export_context")
        package_export = package_data.get("export_context")
        if current_export != package_export:
            changes.append({
                "field": "export_context",
                "before": current_export,
                "after": package_export,
            })

        current_stats = current_state.get("stats", {})
        package_stats = package_data.get("stats", {})
        if current_stats != package_stats:
            changes.append({
                "field": "stats",
                "before": current_stats,
                "after": package_stats,
            })

        return {
            "success": True,
            "package_id": package_id,
            "changes": changes,
            "change_count": len(changes),
            "no_change": len(changes) == 0,
        }

    def undo_restore(self, undo_id: str, operator: str) -> Dict:
        undo = self.db.get_handover_undo(undo_id)
        if not undo:
            return {"success": False, "message": "撤销记录不存在"}

        previous_state = undo.get("previous_state", {})
        if isinstance(previous_state, str):
            try:
                previous_state = json.loads(previous_state)
            except (json.JSONDecodeError, TypeError):
                previous_state = {}

        apply_result = self._apply_state(previous_state, operator)

        package_id = undo.get("package_id")
        self.db.log_handover_event(
            HANDOVER_EVENT_UNDO, package_id, operator,
            json.dumps({
                "undo_id": undo_id,
                "applied": apply_result.get("applied", []),
            }, ensure_ascii=False)
        )

        return {
            "success": True,
            "undo_id": undo_id,
            "package_id": package_id,
            "applied": apply_result.get("applied", []),
        }

    def save_as_copy(self, package_id: str, operator: str,
                     description: str = None) -> Dict:
        package = self.db.get_handover_package(package_id)
        if not package:
            return {"success": False, "message": "交接包不存在"}

        package_data = self._extract_package_state(package)

        config_hash = self._compute_config_hash()
        new_package_id = self._generate_package_id()

        new_description = description or f"复制自 {package_id}"
        if package.get("description"):
            new_description = f"复制自 {package_id}: {package['description']}"

        new_package = {
            "package_id": new_package_id,
            "config_hash": config_hash,
            "status": HANDOVER_STATUS_ACTIVE,
            "operator": operator,
            "description": new_description,
            "package_data": package_data,
            "created_at": datetime.now().isoformat(),
            "source_package_id": package_id,
        }

        self.db.save_handover_package(new_package_id, config_hash, new_package)
        self.db.log_handover_event(
            HANDOVER_EVENT_SAVE_COPY, package_id, operator,
            json.dumps({
                "new_package_id": new_package_id,
                "description": new_description,
            }, ensure_ascii=False)
        )

        return {
            "success": True,
            "original_package_id": package_id,
            "new_package_id": new_package_id,
            "description": new_description,
        }

    def discard_package(self, package_id: str, operator: str) -> Dict:
        package = self.db.get_handover_package(package_id)
        if not package:
            return {"success": False, "message": "交接包不存在"}

        if package.get("status") == HANDOVER_STATUS_DISCARDED:
            return {"success": False, "message": "该交接包已被废弃"}

        updated_package = {**package, "status": HANDOVER_STATUS_DISCARDED}
        self.db.save_handover_package(
            package_id, package.get("config_hash", ""), updated_package
        )
        self.db.log_handover_event(
            HANDOVER_EVENT_DISCARD, package_id, operator,
            json.dumps({"previous_status": package.get("status")}, ensure_ascii=False)
        )

        return {
            "success": True,
            "package_id": package_id,
            "status": HANDOVER_STATUS_DISCARDED,
        }

    def cleanup_invalid(self, config_hash: str, operator: str) -> Dict:
        packages = self.db.list_handover_packages(config_hash, include_discarded=True)
        removed = []
        invalid = []

        for pkg in packages:
            package_id = pkg.get("package_id")
            package_data = self._extract_package_state(pkg)

            is_invalid = False
            reasons = []

            batch_info = package_data.get("last_selected_batch")
            if batch_info and batch_info.get("batch_id"):
                batch = self.db.get_batch(batch_info["batch_id"])
                if not batch:
                    is_invalid = True
                    reasons.append("引用的批次不存在")

            export_ctx = package_data.get("export_context")
            if export_ctx and export_ctx.get("export_path"):
                if not os.path.exists(export_ctx["export_path"]):
                    is_invalid = True
                    reasons.append("导出文件不存在")

            if is_invalid:
                invalid.append({
                    "package_id": package_id,
                    "reasons": reasons,
                })
                self.db.delete_handover_package(package_id)
                removed.append(package_id)

        if removed:
            self.db.log_handover_event(
                HANDOVER_EVENT_CLEANUP, None, operator,
                json.dumps({
                    "config_hash": config_hash,
                    "removed_count": len(removed),
                    "removed_packages": removed,
                }, ensure_ascii=False)
            )

        return {
            "success": True,
            "config_hash": config_hash,
            "removed": removed,
            "removed_count": len(removed),
            "invalid_details": invalid,
        }

    def list_packages(self, config_hash: str = None,
                      include_discarded: bool = False) -> List[Dict]:
        if not config_hash:
            config_hash = self._compute_config_hash()
        return self.db.list_handover_packages(config_hash, include_discarded)

    def get_package(self, package_id: str) -> Dict:
        package = self.db.get_handover_package(package_id)
        if not package:
            return {"success": False, "message": "交接包不存在"}
        return {"success": True, "package": package}

    def get_timeline(self, package_id: str = None,
                     limit: int = 50) -> List[Dict]:
        all_events = []
        if package_id:
            packages = [self.db.get_handover_package(package_id)]
            packages = [p for p in packages if p is not None]
        else:
            config_hash = self._compute_config_hash()
            packages = self.db.list_handover_packages(config_hash, include_discarded=True)

        for pkg in packages:
            pid = pkg.get("package_id")
            undos = self.db.list_handover_undos(pid)
            for u in undos:
                all_events.append({
                    "event_type": HANDOVER_EVENT_RESTORE,
                    "package_id": pid,
                    "operator": u.get("operator"),
                    "timestamp": u.get("created_at"),
                    "undo_id": u.get("undo_id"),
                })

        for pkg in packages:
            pid = pkg.get("package_id")
            all_events.append({
                "event_type": HANDOVER_EVENT_CREATE,
                "package_id": pid,
                "operator": pkg.get("operator"),
                "timestamp": pkg.get("created_at"),
                "description": pkg.get("description"),
            })
            status = pkg.get("status")
            if status == HANDOVER_STATUS_DISCARDED:
                all_events.append({
                    "event_type": HANDOVER_EVENT_DISCARD,
                    "package_id": pid,
                    "timestamp": pkg.get("updated_at") or pkg.get("created_at"),
                })
            if status == HANDOVER_STATUS_RESTORED:
                all_events.append({
                    "event_type": HANDOVER_EVENT_RESTORE,
                    "package_id": pid,
                    "timestamp": pkg.get("updated_at") or pkg.get("created_at"),
                })

        all_events.sort(key=lambda e: e.get("timestamp") or "", reverse=True)

        return all_events[:limit]


HANDOVER_EVENT_EXPORT_PACKAGE = "export_package"
HANDOVER_EVENT_RESUME_EXPORT = "resume_export"
HANDOVER_EVENT_VIEW_SUMMARY = "view_summary"
HANDOVER_EVENT_CONFLICT_RESOLVE = "conflict_resolve"
HANDOVER_EVENT_SESSION_EXPIRE = "session_expire"
HANDOVER_EVENT_EXPORT_START = "export_start"
HANDOVER_EVENT_EXPORT_COMPLETE = "export_complete"
HANDOVER_EVENT_EXPORT_FAILED = "export_failed"

HANDOVER_EVENT_LABELS = {
    HANDOVER_EVENT_CREATE: "创建交接包",
    HANDOVER_EVENT_RESTORE: "恢复交接包",
    HANDOVER_EVENT_UNDO: "撤销恢复",
    HANDOVER_EVENT_DISCARD: "废弃交接包",
    HANDOVER_EVENT_SAVE_COPY: "另存副本",
    HANDOVER_EVENT_CLEANUP: "清理失效会话",
    HANDOVER_EVENT_EXPORT_PACKAGE: "导出交接包",
    HANDOVER_EVENT_RESUME_EXPORT: "续导出",
    HANDOVER_EVENT_VIEW_SUMMARY: "查看摘要",
    HANDOVER_EVENT_CONFLICT_RESOLVE: "冲突解决",
    HANDOVER_EVENT_SESSION_EXPIRE: "会话过期",
    HANDOVER_EVENT_EXPORT_START: "开始导出",
    HANDOVER_EVENT_EXPORT_COMPLETE: "导出完成",
    HANDOVER_EVENT_EXPORT_FAILED: "导出失败",
}

HANDOVER_EXPORT_STATUS_PENDING = "pending"
HANDOVER_EXPORT_STATUS_PARTIAL = "partial"
HANDOVER_EXPORT_STATUS_COMPLETE = "complete"
HANDOVER_EXPORT_STATUS_FAILED = "failed"

HANDOVER_EXPORT_STATUS_LABELS = {
    HANDOVER_EXPORT_STATUS_PENDING: "待导出",
    HANDOVER_EXPORT_STATUS_PARTIAL: "部分导出",
    HANDOVER_EXPORT_STATUS_COMPLETE: "导出完成",
    HANDOVER_EXPORT_STATUS_FAILED: "导出失败",
}

HANDOVER_ACTION_RESUME_EXPORT = "resume_export"
HANDOVER_ACTION_VIEW_FILTERS = "view_filters"
HANDOVER_ACTION_VIEW_SUMMARY = "view_summary"
HANDOVER_ACTION_EXPORT_JSON = "export_json"
HANDOVER_ACTION_EXPORT_CSV = "export_csv"
HANDOVER_ACTION_CHECK_FILES = "check_files"
HANDOVER_ACTION_DISCARD_SESSION = "discard_session"

HANDOVER_ACTION_LABELS = {
    HANDOVER_ACTION_RESUME_EXPORT: "继续导出",
    HANDOVER_ACTION_VIEW_FILTERS: "查看筛选条件",
    HANDOVER_ACTION_VIEW_SUMMARY: "查看导出摘要",
    HANDOVER_ACTION_EXPORT_JSON: "导出 JSON",
    HANDOVER_ACTION_EXPORT_CSV: "导出 CSV",
    HANDOVER_ACTION_CHECK_FILES: "检查导出文件",
    HANDOVER_ACTION_DISCARD_SESSION: "废弃会话",
}


class HandoverPlaybackCenter:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._handover = BatchHandover(config, db)
        self._workbench = BatchWorkbench(config, db)

    def _record_event(self, event_type: str, package_id: str = None,
                      operator: str = None, status: str = "success",
                      result_summary: str = None,
                      event_details: Dict = None,
                      error_message: str = None) -> str:
        return self.db.insert_handover_event(
            event_type=event_type,
            package_id=package_id,
            operator=operator,
            status=status,
            result_summary=result_summary,
            event_details=event_details,
            error_message=error_message,
        )

    def _check_export_file_status(self, export_path: str = None) -> Dict:
        if not export_path:
            export_ctx = self._workbench.get_last_export_context()
            if export_ctx and export_ctx.get("export_path"):
                export_path = export_ctx["export_path"]
            else:
                export_path = self.config.export_dir

        result = {
            "export_path": export_path,
            "exists": False,
            "is_dir": False,
            "is_writable": False,
            "file_count": 0,
            "files": [],
            "total_size": 0,
        }

        if os.path.exists(export_path):
            result["exists"] = True
            if os.path.isdir(export_path):
                result["is_dir"] = True
                try:
                    files = os.listdir(export_path)
                    result["file_count"] = len(files)
                    result["files"] = files[:20]
                    total_size = 0
                    for f in files:
                        fp = os.path.join(export_path, f)
                        if os.path.isfile(fp):
                            total_size += os.path.getsize(fp)
                    result["total_size"] = total_size
                except OSError:
                    pass
            elif os.path.isfile(export_path):
                result["file_count"] = 1
                result["files"] = [os.path.basename(export_path)]
                result["total_size"] = os.path.getsize(export_path)

            try:
                test_file = os.path.join(
                    export_path if result["is_dir"] else os.path.dirname(export_path),
                    ".handover_write_test"
                )
                with open(test_file, "w") as f:
                    f.write("test")
                os.remove(test_file)
                result["is_writable"] = True
            except (OSError, PermissionError):
                result["is_writable"] = False
        else:
            parent_dir = os.path.dirname(export_path) or export_path
            if parent_dir and os.path.exists(parent_dir):
                try:
                    test_file = os.path.join(parent_dir, ".handover_write_test")
                    with open(test_file, "w") as f:
                        f.write("test")
                    os.remove(test_file)
                    result["is_writable"] = True
                except (OSError, PermissionError):
                    result["is_writable"] = False

        return result

    def _collect_export_summary(self, package_id: str) -> Dict:
        package = self.db.get_handover_package(package_id)
        if not package:
            return {}

        package_data = self._handover._extract_package_state(package) or {}

        export_ctx = package_data.get("export_context") or {}
        change_view = package_data.get("change_view_context") or {}
        filters = package_data.get("filters") or {}
        batch_info = package_data.get("last_selected_batch") or {}
        stats = package_data.get("stats") or {}

        summary = {
            "package_id": package_id,
            "batch_id": batch_info.get("batch_id") if batch_info else None,
            "batch_file_name": batch_info.get("file_name") if batch_info else None,
            "filters": self._format_filter_summary(filters, change_view),
            "export_type": export_ctx.get("export_type"),
            "export_format": export_ctx.get("format"),
            "export_path": export_ctx.get("export_path"),
            "exported_at": export_ctx.get("exported_at"),
            "hit_count": export_ctx.get("extra", {}).get("hit_count"),
            "stats": stats,
        }

        has_filters = any([
            filters.get("operator"),
            filters.get("status"),
            filters.get("impact_filter"),
            filters.get("with_conflicts_only"),
            change_view.get("change_type"),
            change_view.get("impact_type"),
            change_view.get("processing_status"),
            change_view.get("record_no"),
            change_view.get("affect_filter"),
            change_view.get("with_conflicts_only"),
        ])
        summary["has_active_filters"] = has_filters
        summary["is_full_view"] = not has_filters

        return summary

    def _format_filter_summary(self, filters: Dict, change_view: Dict) -> Dict:
        result = {
            "operator": filters.get("operator"),
            "status": filters.get("status"),
            "impact_filter": filters.get("impact_filter"),
            "with_conflicts_only": filters.get("with_conflicts_only"),
            "change_type": change_view.get("change_type"),
            "impact_type": change_view.get("impact_type"),
            "processing_status": change_view.get("processing_status"),
            "record_no": change_view.get("record_no"),
            "affect_filter": change_view.get("affect_filter"),
            "change_view_conflicts_only": change_view.get("with_conflicts_only"),
        }
        return result

    def get_session_summary(self, package_id: str, operator: str = None) -> Dict:
        package = self.db.get_handover_package(package_id)
        if not package:
            self._record_event(
                HANDOVER_EVENT_VIEW_SUMMARY, package_id, operator,
                status="failed", error_message="交接包不存在"
            )
            return {"success": False, "message": "交接包不存在"}

        summary = self._collect_export_summary(package_id)
        conflicts = self._handover._check_conflicts(package_id)
        file_status = self._check_export_file_status(
            summary.get("export_path")
        )

        available_actions = self._get_available_actions(
            package_id, summary, conflicts, file_status
        )

        result = {
            "success": True,
            "package_id": package_id,
            "package_status": package.get("status"),
            "operator": package.get("operator"),
            "description": package.get("description"),
            "created_at": package.get("created_at"),
            "summary": summary,
            "conflicts": conflicts,
            "has_conflicts": len(conflicts) > 0,
            "file_status": file_status,
            "available_actions": available_actions,
        }

        self._record_event(
            HANDOVER_EVENT_VIEW_SUMMARY, package_id, operator,
            status="success",
            result_summary=f"查看会话摘要：{package_id}",
            event_details={
                "has_conflicts": len(conflicts) > 0,
                "has_active_filters": summary.get("has_active_filters"),
                "available_actions_count": len(available_actions),
                "file_exists": file_status.get("exists"),
            }
        )

        return result

    def _get_available_actions(self, package_id: str, summary: Dict,
                                conflicts: List[Dict],
                                file_status: Dict) -> List[Dict]:
        actions = []

        actions.append({
            "action_id": HANDOVER_ACTION_VIEW_FILTERS,
            "label": HANDOVER_ACTION_LABELS[HANDOVER_ACTION_VIEW_FILTERS],
            "enabled": True,
            "description": "查看交接包中的筛选条件",
        })

        actions.append({
            "action_id": HANDOVER_ACTION_VIEW_SUMMARY,
            "label": HANDOVER_ACTION_LABELS[HANDOVER_ACTION_VIEW_SUMMARY],
            "enabled": True,
            "description": "查看导出摘要信息",
        })

        if summary.get("is_full_view"):
            actions.append({
                "action_id": HANDOVER_ACTION_RESUME_EXPORT,
                "label": "⚠ 恢复后为全量视图，请先设置筛选条件",
                "enabled": False,
                "description": "当前交接包中无筛选条件，恢复后为全量视图",
                "warning": "no_filters",
            })
        else:
            can_export = not conflicts or all(
                c.get("conflict_type") != HANDOVER_CONFLICT_EXPORT_NOT_WRITABLE
                for c in conflicts
            )
            actions.append({
                "action_id": HANDOVER_ACTION_RESUME_EXPORT,
                "label": HANDOVER_ACTION_LABELS[HANDOVER_ACTION_RESUME_EXPORT],
                "enabled": can_export,
                "description": "使用交接包中的筛选条件继续导出",
                "requires_confirmation": True,
            })

        actions.append({
            "action_id": HANDOVER_ACTION_EXPORT_JSON,
            "label": HANDOVER_ACTION_LABELS[HANDOVER_ACTION_EXPORT_JSON],
            "enabled": file_status.get("is_writable", False),
            "description": "以 JSON 格式导出筛选结果",
        })

        actions.append({
            "action_id": HANDOVER_ACTION_EXPORT_CSV,
            "label": HANDOVER_ACTION_LABELS[HANDOVER_ACTION_EXPORT_CSV],
            "enabled": file_status.get("is_writable", False),
            "description": "以 CSV 格式导出筛选结果",
        })

        actions.append({
            "action_id": HANDOVER_ACTION_CHECK_FILES,
            "label": HANDOVER_ACTION_LABELS[HANDOVER_ACTION_CHECK_FILES],
            "enabled": True,
            "description": "检查目标导出目录状态",
        })

        actions.append({
            "action_id": HANDOVER_ACTION_DISCARD_SESSION,
            "label": HANDOVER_ACTION_LABELS[HANDOVER_ACTION_DISCARD_SESSION],
            "enabled": True,
            "description": "废弃当前交接会话",
            "dangerous": True,
        })

        return actions

    def resume_export(self, package_id: str, operator: str,
                      force: bool = False) -> Dict:
        summary_result = self.get_session_summary(package_id, operator)
        if not summary_result["success"]:
            return summary_result

        summary = summary_result["summary"]
        conflicts = summary_result["conflicts"]
        file_status = summary_result["file_status"]

        if summary.get("is_full_view") and not force:
            self._record_event(
                HANDOVER_EVENT_RESUME_EXPORT, package_id, operator,
                status="failed",
                error_message="无筛选条件，恢复后为全量视图",
                event_details={"reason": "no_filters"}
            )
            return {
                "success": False,
                "message": "交接包中无筛选条件，恢复后将回到全量视图。请使用 --force 强制恢复，或先设置筛选条件后重新创建交接包。",
                "warning": "full_view_fallback",
                "summary": summary,
            }

        if conflicts and not force:
            self._record_event(
                HANDOVER_EVENT_RESUME_EXPORT, package_id, operator,
                status="failed",
                error_message=f"存在 {len(conflicts)} 个冲突",
                event_details={"conflict_count": len(conflicts)}
            )
            return {
                "success": False,
                "message": f"存在 {len(conflicts)} 个冲突，请确认后使用 --force 强制恢复。",
                "conflicts": conflicts,
                "summary": summary,
            }

        if not file_status.get("is_writable") and not force:
            self._record_event(
                HANDOVER_EVENT_RESUME_EXPORT, package_id, operator,
                status="failed",
                error_message="导出目录不可写",
                event_details={"export_path": file_status.get("export_path")}
            )
            return {
                "success": False,
                "message": f"导出目录 {file_status.get('export_path')} 不可写。",
                "file_status": file_status,
                "summary": summary,
            }

        self._record_event(
            HANDOVER_EVENT_EXPORT_START, package_id, operator,
            status="success",
            result_summary="开始续导出",
            event_details={
                "forced": force,
                "export_format": summary.get("export_format"),
                "has_filters": summary.get("has_active_filters"),
            }
        )

        restore_result = self._handover.restore_package(
            package_id, operator, force=force
        )

        if not restore_result["success"]:
            self._record_event(
                HANDOVER_EVENT_EXPORT_FAILED, package_id, operator,
                status="failed",
                error_message=restore_result.get("message", "恢复失败"),
                event_details=restore_result
            )
            return restore_result

        batch_id = summary.get("batch_id")
        export_format = summary.get("export_format") or "json"

        if batch_id:
            from .change_tracker import ChangeTracker
            tracker = ChangeTracker(self.config, self.db)
            try:
                export_result = tracker.export_change_logs(
                    batch_id, operator, format=export_format
                )
                if export_result.get("success"):
                    self._record_event(
                        HANDOVER_EVENT_EXPORT_COMPLETE, package_id, operator,
                        status="success",
                        result_summary=(
                            f"导出完成：{export_result.get('total_changes', 0)} 条记录"
                        ),
                        event_details={
                            "file_path": export_result.get("file_path"),
                            "format": export_format,
                            "total_changes": export_result.get("total_changes"),
                        }
                    )
                else:
                    self._record_event(
                        HANDOVER_EVENT_EXPORT_FAILED, package_id, operator,
                        status="failed",
                        error_message=export_result.get("message", "导出失败"),
                        event_details=export_result
                    )
            except Exception as e:
                export_result = {"success": False, "message": str(e)}
                self._record_event(
                    HANDOVER_EVENT_EXPORT_FAILED, package_id, operator,
                    status="failed", error_message=str(e)
                )
        else:
            export_result = {
                "success": False,
                "message": "交接包中无批次信息，无法导出"
            }

        result = {
            "success": True,
            "package_id": package_id,
            "restored": restore_result.get("success", False),
            "undo_id": restore_result.get("undo_id"),
            "applied": restore_result.get("applied", []),
            "export_result": export_result,
            "summary": summary,
            "conflicts": conflicts,
            "file_status": file_status,
        }

        self._record_event(
            HANDOVER_EVENT_RESUME_EXPORT, package_id, operator,
            status="success" if export_result.get("success") else "partial",
            result_summary=(
                f"续导出完成：成功={export_result.get('success', False)}"
            ),
            event_details={
                "export_success": export_result.get("success"),
                "export_file": export_result.get("file_path"),
                "conflict_count": len(conflicts),
                "forced": force,
            }
        )

        return result

    def export_handover_package(self, package_id: str, operator: str,
                                target_dir: str = None,
                                format: str = "json") -> Dict:
        if target_dir is None:
            target_dir = self.config.export_dir

        package = self.db.get_handover_package(package_id)
        if not package:
            self._record_event(
                HANDOVER_EVENT_EXPORT_PACKAGE, package_id, operator,
                status="failed", error_message="交接包不存在"
            )
            return {"success": False, "message": "交接包不存在"}

        package_data = self._handover._extract_package_state(package)
        summary = self._collect_export_summary(package_id)

        os.makedirs(target_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        package_dir_name = f"handover_{package_id}_{timestamp}"
        package_dir = os.path.join(target_dir, package_dir_name)
        os.makedirs(package_dir, exist_ok=True)

        meta_file = os.path.join(package_dir, "交接包元数据.json")
        meta_data = {
            "package_id": package_id,
            "config_hash": package.get("config_hash"),
            "status": package.get("status"),
            "operator": package.get("operator"),
            "description": package.get("description"),
            "created_at": package.get("created_at"),
            "exported_at": datetime.now().isoformat(),
            "exported_by": operator,
            "summary": summary,
        }
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta_data, f, ensure_ascii=False, indent=2, default=str)

        state_file = os.path.join(package_dir, "会话状态.json")
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(package_data, f, ensure_ascii=False, indent=2, default=str)

        filters_file = os.path.join(package_dir, "筛选条件.json")
        filters_data = {
            "batch_filters": package_data.get("filters", {}),
            "change_view_filters": package_data.get("change_view_context", {}),
            "has_active_filters": summary.get("has_active_filters"),
            "is_full_view": summary.get("is_full_view"),
        }
        with open(filters_file, "w", encoding="utf-8") as f:
            json.dump(filters_data, f, ensure_ascii=False, indent=2, default=str)

        batch_id = summary.get("batch_id")
        export_files = []
        if batch_id:
            from .change_tracker import ChangeTracker
            tracker = ChangeTracker(self.config, self.db)
            try:
                export_result = tracker.export_change_logs(
                    batch_id, operator, format=format
                )
                if export_result.get("success"):
                    src_path = export_result["file_path"]
                    if os.path.isdir(src_path):
                        for fname in os.listdir(src_path):
                            src = os.path.join(src_path, fname)
                            dst = os.path.join(package_dir, fname)
                            if os.path.isfile(src):
                                import shutil
                                shutil.copy2(src, dst)
                                export_files.append(fname)
                    else:
                        import shutil
                        base_name = os.path.basename(src_path)
                        dst = os.path.join(package_dir, base_name)
                        shutil.copy2(src_path, dst)
                        export_files.append(base_name)
            except Exception:
                pass

        readme_file = os.path.join(package_dir, "README.md")
        readme_content = f"""# 交接包 {package_id}

## 基本信息
- 创建人: {package.get('operator', '-')}
- 创建时间: {package.get('created_at', '-')}
- 描述: {package.get('description', '-')}
- 导出时间: {datetime.now().isoformat()}
- 导出人: {operator}

## 文件说明
- `交接包元数据.json` - 交接包基本信息和摘要
- `会话状态.json` - 完整的会话状态数据（用于恢复）
- `筛选条件.json` - 当前筛选条件详情

## 使用方法
1. 使用同一份配置文件启动 CLI
2. 执行 `handover preview {package_id}` 查看交接包内容
3. 执行 `handover restore {package_id}` 恢复会话
4. 确认筛选条件后执行导出

## 筛选状态
- {'已应用筛选条件' if summary.get('has_active_filters') else '全量视图（无筛选）'}

"""
        with open(readme_file, "w", encoding="utf-8") as f:
            f.write(readme_content)

        result = {
            "success": True,
            "package_id": package_id,
            "export_path": package_dir,
            "files": export_files + [
                "交接包元数据.json",
                "会话状态.json",
                "筛选条件.json",
                "README.md",
            ],
            "format": format,
            "has_active_filters": summary.get("has_active_filters"),
        }

        self._record_event(
            HANDOVER_EVENT_EXPORT_PACKAGE, package_id, operator,
            status="success",
            result_summary=f"交接包已导出到 {package_dir}",
            event_details={
                "export_path": package_dir,
                "file_count": len(result["files"]),
                "has_active_filters": summary.get("has_active_filters"),
            }
        )

        return result

    def get_detailed_timeline(self, package_id: str = None,
                               limit: int = 50) -> List[Dict]:
        events = self.db.list_handover_events(
            package_id=package_id, limit=limit
        )

        result = []
        for event in events:
            result.append({
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "event_type_label": HANDOVER_EVENT_LABELS.get(
                    event["event_type"], event["event_type"]
                ),
                "package_id": event.get("package_id"),
                "operator": event.get("operator"),
                "status": event.get("status"),
                "result_summary": event.get("result_summary"),
                "event_details": event.get("event_details"),
                "error_message": event.get("error_message"),
                "timestamp": event.get("created_at"),
            })

        return result

    def cleanup_expired_sessions(self, operator: str = None,
                                 config_hash: str = None,
                                 max_age_days: int = 30) -> Dict:
        if config_hash is None:
            config_hash = self._handover._compute_config_hash()

        packages = self.db.list_handover_packages(
            config_hash, include_discarded=True
        )

        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=max_age_days)

        removed = []
        expired = []

        for pkg in packages:
            pkg_id = pkg.get("package_id")
            created_str = pkg.get("created_at")
            if not created_str:
                continue

            try:
                created = datetime.fromisoformat(created_str)
            except (ValueError, TypeError):
                continue

            if created < cutoff:
                is_invalid = False
                reasons = []

                package_data = self._handover._extract_package_state(pkg)
                batch_info = package_data.get("last_selected_batch")
                if batch_info and batch_info.get("batch_id"):
                    batch = self.db.get_batch(batch_info["batch_id"])
                    if not batch:
                        is_invalid = True
                        reasons.append("引用的批次不存在")

                export_ctx = package_data.get("export_context")
                if export_ctx and export_ctx.get("export_path"):
                    if not os.path.exists(export_ctx["export_path"]):
                        is_invalid = True
                        reasons.append("导出文件不存在")

                if is_invalid or pkg.get("status") == HANDOVER_STATUS_DISCARDED:
                    expired.append({
                        "package_id": pkg_id,
                        "status": pkg.get("status"),
                        "created_at": created_str,
                        "age_days": (datetime.now() - created).days,
                        "reasons": reasons if reasons else ["已过期"],
                    })
                    self.db.delete_handover_package(pkg_id)
                    removed.append(pkg_id)

        if removed:
            self._record_event(
                HANDOVER_EVENT_CLEANUP, None, operator,
                status="success",
                result_summary=f"清理了 {len(removed)} 个失效会话",
                event_details={
                    "config_hash": config_hash,
                    "removed_count": len(removed),
                    "removed_packages": removed,
                    "max_age_days": max_age_days,
                }
            )
            self.db.log_handover_event(
                HANDOVER_EVENT_CLEANUP, None, operator or "system",
                json.dumps({
                    "config_hash": config_hash,
                    "removed_count": len(removed),
                    "removed_packages": removed,
                    "max_age_days": max_age_days,
                }, ensure_ascii=False)
            )

        return {
            "success": True,
            "config_hash": config_hash,
            "removed_count": len(removed),
            "removed_packages": removed,
            "expired_details": expired,
        }

    def check_duplicate_import_conflict(self, package_id: str) -> Dict:
        package = self.db.get_handover_package(package_id)
        if not package:
            return {"success": False, "message": "交接包不存在"}

        package_data = self._handover._extract_package_state(package)
        batch_info = package_data.get("last_selected_batch")

        if not batch_info or not batch_info.get("batch_id"):
            return {
                "success": True,
                "has_conflict": False,
                "message": "交接包中无批次信息",
            }

        batch_id = batch_info["batch_id"]
        batch = self.db.get_batch(batch_id)
        if not batch:
            return {
                "success": True,
                "has_conflict": True,
                "conflict_type": "batch_not_found",
                "message": f"批次 #{batch_id} 不存在",
            }

        file_type = batch["file_type"]
        later_batches = [
            b for b in self.db.get_batches()
            if b["file_type"] == file_type and b["id"] > batch_id
        ]

        if later_batches:
            latest = later_batches[-1]
            return {
                "success": True,
                "has_conflict": True,
                "conflict_type": HANDOVER_CONFLICT_REIMPORT,
                "message": f"存在后续导入（批次 #{latest['id']}）",
                "detail": (
                    f"原始批次 #{batch_id} 之后有 {len(later_batches)} 次"
                    f"{'发票' if file_type == 'invoice' else '收款'}导入，"
                    f"最新批次: #{latest['id']} - {latest['file_name']}"
                ),
                "later_batch_count": len(later_batches),
                "latest_batch": latest,
            }

        return {
            "success": True,
            "has_conflict": False,
            "message": "无重复导入冲突",
        }

    def verify_config_isolation(self, package_id: str) -> Dict:
        package = self.db.get_handover_package(package_id)
        if not package:
            return {"success": False, "message": "交接包不存在"}

        current_hash = self._handover._compute_config_hash()
        package_hash = package.get("config_hash", "")

        is_match = current_hash == package_hash

        return {
            "success": True,
            "package_id": package_id,
            "current_config_hash": current_hash,
            "package_config_hash": package_hash,
            "is_match": is_match,
            "has_conflict": not is_match,
            "message": "配置一致" if is_match else "配置不一致",
            "detail": (
                "当前配置与交接包创建时的配置一致"
                if is_match
                else f"当前配置哈希 {current_hash} 与交接包配置哈希 {package_hash} 不一致"
            ),
        }
