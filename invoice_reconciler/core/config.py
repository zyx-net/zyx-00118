import os
import yaml
from dataclasses import dataclass, field
from typing import List, Dict, Optional


@dataclass
class Config:
    amount_tolerance: float = 0.01
    date_window_days: int = 30
    invoice_required_columns: List[str] = field(
        default_factory=lambda: ["invoice_no", "invoice_date", "customer", "amount", "status"]
    )
    payment_required_columns: List[str] = field(
        default_factory=lambda: ["payment_no", "payment_date", "customer", "amount"]
    )
    export_format: str = "xlsx"
    db_path: str = "invoice_reconciler/data/reconciler.db"
    export_dir: str = "invoice_reconciler/exports"
    lock_timeout_seconds: int = 3600
    default_user_role: str = "reviewer"
    admin_users: List[str] = field(default_factory=lambda: ["admin"])
    enable_lock: bool = True

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "Config":
        if config_path is None:
            config_path = os.environ.get(
                "INVOICE_RECONCILER_CONFIG",
                "invoice_reconciler/data/config.yaml"
            )

        if not os.path.exists(config_path):
            config = cls()
            config.save(config_path)
            return config

        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        return cls(
            amount_tolerance=data.get("amount_tolerance", 0.01),
            date_window_days=data.get("date_window_days", 30),
            invoice_required_columns=data.get(
                "invoice_required_columns",
                ["invoice_no", "invoice_date", "customer", "amount", "status"]
            ),
            payment_required_columns=data.get(
                "payment_required_columns",
                ["payment_no", "payment_date", "customer", "amount"]
            ),
            export_format=data.get("export_format", "xlsx"),
            db_path=data.get("db_path", "invoice_reconciler/data/reconciler.db"),
            export_dir=data.get("export_dir", "invoice_reconciler/exports"),
            lock_timeout_seconds=data.get("lock_timeout_seconds", 3600),
            default_user_role=data.get("default_user_role", "reviewer"),
            admin_users=data.get("admin_users", ["admin"]),
            enable_lock=data.get("enable_lock", True),
        )

    def save(self, config_path: str) -> None:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        data = {
            "amount_tolerance": self.amount_tolerance,
            "date_window_days": self.date_window_days,
            "invoice_required_columns": self.invoice_required_columns,
            "payment_required_columns": self.payment_required_columns,
            "export_format": self.export_format,
            "db_path": self.db_path,
            "export_dir": self.export_dir,
            "lock_timeout_seconds": self.lock_timeout_seconds,
            "default_user_role": self.default_user_role,
            "admin_users": self.admin_users,
            "enable_lock": self.enable_lock,
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def validate(self) -> List[str]:
        errors = []
        if self.amount_tolerance < 0:
            errors.append("金额容差不能为负数")
        if self.date_window_days < 0:
            errors.append("日期窗口天数不能为负数")
        if self.export_format not in ["xlsx", "csv", "json"]:
            errors.append(f"不支持的导出格式: {self.export_format}，仅支持 xlsx、csv 和 json")
        if not self.invoice_required_columns:
            errors.append("发票必填列不能为空")
        if not self.payment_required_columns:
            errors.append("收款必填列不能为空")
        if self.lock_timeout_seconds < 0:
            errors.append("锁超时时间不能为负数")
        if self.default_user_role not in ["reviewer", "admin"]:
            errors.append(f"无效的默认用户角色: {self.default_user_role}")
        return errors

    def to_dict(self) -> Dict:
        return {
            "amount_tolerance": self.amount_tolerance,
            "date_window_days": self.date_window_days,
            "invoice_required_columns": self.invoice_required_columns,
            "payment_required_columns": self.payment_required_columns,
            "export_format": self.export_format,
            "db_path": self.db_path,
            "export_dir": self.export_dir,
            "lock_timeout_seconds": self.lock_timeout_seconds,
            "default_user_role": self.default_user_role,
            "admin_users": self.admin_users,
            "enable_lock": self.enable_lock,
        }
