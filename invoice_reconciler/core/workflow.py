from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
from .config import Config
from .database import (
    Database,
    USER_ROLE_REVIEWER,
    USER_ROLE_ADMIN,
    USER_STATUS_ACTIVE,
    LOCK_ACTION_LOCK,
    LOCK_ACTION_UNLOCK,
    LOCK_ACTION_TRANSFER,
    LOCK_ACTION_TAKEOVER,
    LOCK_ACTION_FORCE_UNLOCK,
    MATCH_STATUS_PENDING,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_REVOKED,
)


LOCK_STATUS_LABELS = {
    LOCK_ACTION_LOCK: "锁定",
    LOCK_ACTION_UNLOCK: "解锁",
    LOCK_ACTION_TRANSFER: "转交",
    LOCK_ACTION_TAKEOVER: "接管",
    LOCK_ACTION_FORCE_UNLOCK: "强制解锁",
}

USER_ROLE_LABELS = {
    USER_ROLE_REVIEWER: "复核员",
    USER_ROLE_ADMIN: "管理员",
}


class WorkflowManager:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._init_default_users()

    def _init_default_users(self):
        for admin_user in self.config.admin_users:
            existing = self.db.get_user(admin_user)
            if not existing:
                self.db.create_user(admin_user, USER_ROLE_ADMIN)
            elif existing.get("role") != USER_ROLE_ADMIN:
                self.db.update_user_role(admin_user, USER_ROLE_ADMIN)

    def restore_locks_on_startup(self) -> Dict:
        if not self.config.enable_lock:
            return {
                "restored": False,
                "reason": "锁功能已禁用",
                "total_locks": 0,
                "expired_locks": 0,
                "auto_expired_count": 0,
            }

        now = datetime.now()
        timeout_seconds = self.config.lock_timeout_seconds

        all_locks = self.db.list_all_locks(include_expired=True)
        total_locks = len(all_locks)
        expired_count = 0
        auto_expired_count = 0

        for lock in all_locks:
            lock_id = lock["id"]
            match_id = lock["match_id"]
            current_lock = self.db.get_match_lock(match_id)

            if not current_lock:
                continue

            locked_at_str = current_lock.get("locked_at")
            if not locked_at_str:
                continue

            try:
                locked_at = datetime.strptime(locked_at_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue

            configured_expires_at = locked_at + timedelta(seconds=timeout_seconds) if timeout_seconds > 0 else None
            db_expires_at_str = current_lock.get("lock_expires_at")

            if timeout_seconds > 0:
                if not db_expires_at_str:
                    with self.db._get_conn() as conn:
                        conn.execute(
                            "UPDATE match_locks SET lock_expires_at = ? WHERE id = ?",
                            (configured_expires_at.strftime("%Y-%m-%d %H:%M:%S"), lock_id)
                        )
                    db_expires_at_str = configured_expires_at.strftime("%Y-%m-%d %H:%M:%S")

                try:
                    db_expires_at = datetime.strptime(db_expires_at_str, "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    continue

                if db_expires_at < configured_expires_at:
                    with self.db._get_conn() as conn:
                        conn.execute(
                            "UPDATE match_locks SET lock_expires_at = ? WHERE id = ?",
                            (configured_expires_at.strftime("%Y-%m-%d %H:%M:%S"), lock_id)
                        )

            existing_expires = current_lock.get("lock_expires_at")
            if existing_expires:
                try:
                    expire_time = datetime.strptime(existing_expires, "%Y-%m-%d %H:%M:%S")
                    if expire_time <= now:
                        expired_count += 1
                except (ValueError, TypeError):
                    pass

        return {
            "restored": True,
            "reason": "按配置恢复锁状态完成",
            "total_locks": total_locks,
            "expired_locks": expired_count,
            "auto_expired_count": auto_expired_count,
            "config_timeout": timeout_seconds,
        }

    def get_user_role(self, username: str) -> str:
        user = self.db.get_user(username)
        if user:
            return user.get("role", self.config.default_user_role)
        if username in self.config.admin_users:
            return USER_ROLE_ADMIN
        return self.config.default_user_role

    def is_admin(self, username: str) -> bool:
        return self.get_user_role(username) == USER_ROLE_ADMIN

    def is_reviewer(self, username: str) -> bool:
        return self.get_user_role(username) == USER_ROLE_REVIEWER

    def ensure_user(self, username: str, role: str = None) -> Dict:
        user = self.db.get_user(username)
        if user:
            return user
        if role is None:
            role = USER_ROLE_ADMIN if username in self.config.admin_users else self.config.default_user_role
        user_id = self.db.create_user(username, role)
        return {"id": user_id, "username": username, "role": role}

    def can_lock(self, username: str, match_id: int) -> Tuple[bool, str]:
        if not self.config.enable_lock:
            return True, "锁功能已禁用"

        match = self.db.get_match_by_id(match_id)
        if not match:
            return False, "匹配记录不存在"

        if match["status"] == MATCH_STATUS_REVOKED:
            return False, "已撤销的记录无法锁定"

        lock = self.db.get_match_lock(match_id)
        if not lock:
            return True, "可以锁定"

        if lock["lock_owner"] == username:
            return True, "您已锁定该记录"

        if lock.get("lock_expires_at"):
            try:
                expire_time = datetime.strptime(lock["lock_expires_at"], "%Y-%m-%d %H:%M:%S")
                if expire_time <= datetime.now():
                    return True, "锁已过期，可以接管"
            except (ValueError, TypeError):
                pass

        return False, f"记录已被 {lock['lock_owner']} 锁定"

    def can_unlock(self, username: str, match_id: int) -> Tuple[bool, str]:
        if not self.config.enable_lock:
            return True, "锁功能已禁用"

        lock = self.db.get_match_lock(match_id)
        if not lock:
            return False, "该记录未被锁定"

        if lock["lock_owner"] == username:
            return True, "可以解锁自己锁定的记录"

        if self.is_admin(username):
            return True, "管理员可以解锁任何记录"

        return False, "只能解锁自己锁定的记录"

    def can_transfer(self, username: str, match_id: int) -> Tuple[bool, str]:
        if not self.config.enable_lock:
            return False, "锁功能已禁用"

        lock = self.db.get_match_lock(match_id)
        if not lock:
            return False, "该记录未被锁定"

        if lock["lock_owner"] == username:
            return True, "可以转交自己锁定的记录"

        if self.is_admin(username):
            return True, "管理员可以转交任何记录"

        return False, "只能转交自己锁定的记录"

    def can_takeover(self, username: str, match_id: int) -> Tuple[bool, str]:
        if not self.config.enable_lock:
            return True, "锁功能已禁用"

        lock = self.db.get_match_lock(match_id)
        if not lock:
            return True, "记录未锁定，可以直接锁定"

        if lock["lock_owner"] == username:
            return True, "您已锁定该记录"

        if lock.get("lock_expires_at"):
            try:
                expire_time = datetime.strptime(lock["lock_expires_at"], "%Y-%m-%d %H:%M:%S")
                if expire_time <= datetime.now():
                    return True, "锁已过期，可以接管"
            except (ValueError, TypeError):
                pass

        if self.is_admin(username):
            return True, "管理员可以强制接管"

        return False, "锁未过期，无法接管"

    def can_operate_match(self, username: str, match_id: int) -> Tuple[bool, str]:
        if not self.config.enable_lock:
            return True, "锁功能已禁用"

        if self.is_admin(username):
            return True, "管理员权限"

        lock = self.db.get_match_lock(match_id)
        if not lock:
            return False, "该记录未被锁定，请先执行 'lock acquire' 锁定后再操作"

        if lock["lock_owner"] == username:
            if lock.get("lock_expires_at"):
                try:
                    expire_time = datetime.strptime(lock["lock_expires_at"], "%Y-%m-%d %H:%M:%S")
                    if expire_time <= datetime.now():
                        return False, "您持有的锁已过期，请重新执行 'lock takeover' 接管后再操作"
                except (ValueError, TypeError):
                    pass
            return True, "您是该记录的责任人"

        if lock.get("lock_expires_at"):
            try:
                expire_time = datetime.strptime(lock["lock_expires_at"], "%Y-%m-%d %H:%M:%S")
                if expire_time <= datetime.now():
                    return False, f"该记录由 {lock['lock_owner']} 锁定（锁已过期），请先执行 'lock takeover' 接管后再操作"
            except (ValueError, TypeError):
                pass

        return False, f"该记录由 {lock['lock_owner']} 锁定，您无法操作。如为紧急情况请联系管理员强制解锁"

    def acquire_lock(self, match_id: int, username: str,
                     reason: str = None) -> Dict:
        if not self.config.enable_lock:
            return {
                "success": True,
                "skipped": True,
                "message": "锁功能已禁用"
            }

        can_lock, msg = self.can_lock(username, match_id)
        if not can_lock:
            raise ValueError(msg)

        self.ensure_user(username)

        expire_seconds = self.config.lock_timeout_seconds if self.config.lock_timeout_seconds > 0 else None

        result = self.db.acquire_lock(
            match_id, username, reason, expire_seconds
        )
        return result

    def release_lock(self, match_id: int, username: str,
                     reason: str = None) -> Dict:
        if not self.config.enable_lock:
            return {
                "success": True,
                "skipped": True,
                "message": "锁功能已禁用"
            }

        can_unlock, msg = self.can_unlock(username, match_id)
        if not can_unlock:
            raise ValueError(msg)

        result = self.db.release_lock(match_id, username, reason)
        return result

    def transfer_lock(self, match_id: int, from_user: str, to_user: str,
                      reason: str = None) -> Dict:
        if not self.config.enable_lock:
            return {
                "success": True,
                "skipped": True,
                "message": "锁功能已禁用"
            }

        can_transfer, msg = self.can_transfer(from_user, match_id)
        if not can_transfer:
            raise ValueError(msg)

        self.ensure_user(to_user)

        result = self.db.transfer_lock(match_id, from_user, to_user, reason)
        return result

    def takeover_lock(self, match_id: int, username: str,
                      reason: str = None) -> Dict:
        if not self.config.enable_lock:
            return {
                "success": True,
                "skipped": True,
                "message": "锁功能已禁用"
            }

        can_takeover, msg = self.can_takeover(username, match_id)
        if not can_takeover:
            raise ValueError(msg)

        self.ensure_user(username)

        result = self.db.takeover_lock(match_id, username, reason)
        return result

    def force_unlock(self, match_id: int, username: str,
                     reason: str = None) -> Dict:
        if not self.is_admin(username):
            raise ValueError("只有管理员可以强制解锁")

        result = self.db.force_unlock(match_id, username, reason)
        return result

    def batch_force_unlock(self, username: str, reason: str = None,
                           owner: str = None) -> Dict:
        if not self.is_admin(username):
            raise ValueError("只有管理员可以批量解锁")

        result = self.db.batch_force_unlock(username, reason, owner)
        return result

    def get_locks_by_owner(self, owner: str) -> List[Dict]:
        return self.db.get_locks_by_owner(owner)

    def list_all_locks(self, include_expired: bool = False) -> List[Dict]:
        return self.db.list_all_locks(include_expired)

    def get_lock_history(self, match_id: int = None,
                         operator: str = None,
                         limit: int = 100) -> List[Dict]:
        history = self.db.get_lock_history(match_id, operator, limit)
        for h in history:
            h["action_label"] = LOCK_STATUS_LABELS.get(h["action"], h["action"])
        return history

    def get_expired_locks(self) -> List[Dict]:
        return self.db.get_expired_locks()

    def get_user_locks_summary(self, username: str) -> Dict:
        locks = self.db.get_locks_by_owner(username)
        now = datetime.now()
        active_count = 0
        expired_count = 0
        for lock in locks:
            if lock.get("lock_expires_at"):
                try:
                    expire_time = datetime.strptime(
                        lock["lock_expires_at"], "%Y-%m-%d %H:%M:%S"
                    )
                    if expire_time <= now:
                        expired_count += 1
                    else:
                        active_count += 1
                except (ValueError, TypeError):
                    active_count += 1
            else:
                active_count += 1

        return {
            "username": username,
            "role": self.get_user_role(username),
            "role_label": USER_ROLE_LABELS.get(
                self.get_user_role(username), self.get_user_role(username)
            ),
            "total_locks": len(locks),
            "active_locks": active_count,
            "expired_locks": expired_count,
        }

    def list_users(self) -> List[Dict]:
        users = self.db.list_users()
        for u in users:
            u["role_label"] = USER_ROLE_LABELS.get(u.get("role"), u.get("role"))
        return users

    def update_user_role(self, admin_user: str, target_user: str,
                         role: str) -> Dict:
        if not self.is_admin(admin_user):
            raise ValueError("只有管理员可以修改用户角色")

        if role not in [USER_ROLE_REVIEWER, USER_ROLE_ADMIN]:
            raise ValueError(f"无效的角色: {role}")

        self.ensure_user(target_user, role)
        self.db.update_user_role(target_user, role)

        return {
            "success": True,
            "username": target_user,
            "new_role": role,
            "new_role_label": USER_ROLE_LABELS.get(role, role),
            "message": f"用户 {target_user} 角色已更新为 {USER_ROLE_LABELS.get(role, role)}"
        }

    def auto_lock_on_confirm(self, match_id: int, username: str) -> None:
        if not self.config.enable_lock:
            return

        lock = self.db.get_match_lock(match_id)
        if not lock:
            try:
                self.db.acquire_lock(
                    match_id, username,
                    "确认匹配时自动锁定",
                    self.config.lock_timeout_seconds
                )
            except ValueError:
                pass

    def auto_unlock_on_revoke(self, match_id: int, username: str) -> None:
        if not self.config.enable_lock:
            return

        lock = self.db.get_match_lock(match_id)
        if lock and lock["lock_owner"] == username:
            try:
                self.db.release_lock(match_id, username, "撤销匹配时自动解锁")
            except ValueError:
                pass

    def check_permission(self, username: str, action: str,
                         match_id: int = None) -> Tuple[bool, str]:
        admin_actions = [
            "force_unlock", "batch_unlock", "update_role",
            "view_all_locks", "manage_users"
        ]
        if action in admin_actions:
            if self.is_admin(username):
                return True, "管理员权限"
            return False, "需要管理员权限"

        reviewer_actions = [
            "lock", "unlock", "transfer", "confirm", "reject",
            "view_my_locks"
        ]
        if action in reviewer_actions:
            return True, "复核员权限"

        if match_id and action in ["confirm", "reject", "revoke"]:
            return self.can_operate_match(username, match_id)

        return True, "默认允许"
