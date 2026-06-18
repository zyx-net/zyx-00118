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
        undo_id = f"UNDO_{package_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
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
