import sqlite3
import os
import json
import hashlib
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any
from contextlib import contextmanager


MATCH_STATUS_MATCHED = "matched"
MATCH_STATUS_PENDING = "pending"
MATCH_STATUS_EXCEPTION = "exception"
MATCH_STATUS_UNMATCHED = "unmatched"
MATCH_STATUS_REVOKED = "revoked"

INVOICE_STATUS_NORMAL = "normal"
INVOICE_STATUS_INVALID = "invalid"
PAYMENT_STATUS_NORMAL = "normal"
PAYMENT_STATUS_INVALID = "invalid"

USER_ROLE_REVIEWER = "reviewer"
USER_ROLE_ADMIN = "admin"

USER_STATUS_ACTIVE = "active"
USER_STATUS_INACTIVE = "inactive"

LOCK_ACTION_LOCK = "lock"
LOCK_ACTION_UNLOCK = "unlock"
LOCK_ACTION_TRANSFER = "transfer"
LOCK_ACTION_TAKEOVER = "takeover"
LOCK_ACTION_FORCE_UNLOCK = "force_unlock"
LOCK_ACTION_AUTO_EXPIRE = "auto_expire"


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_schema()

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS import_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    total_rows INTEGER DEFAULT 0,
                    success_rows INTEGER DEFAULT 0,
                    failed_rows INTEGER DEFAULT 0,
                    operator TEXT,
                    imported_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    UNIQUE(file_hash, file_type)
                );

                CREATE TABLE IF NOT EXISTS invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER,
                    file_row_num INTEGER,
                    invoice_no TEXT NOT NULL,
                    invoice_date DATE,
                    customer TEXT,
                    amount REAL,
                    status TEXT DEFAULT 'normal',
                    raw_data TEXT,
                    imported_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    match_status TEXT DEFAULT 'unmatched',
                    error_message TEXT,
                    FOREIGN KEY (batch_id) REFERENCES import_batches(id),
                    UNIQUE(invoice_no, batch_id)
                );

                CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(customer);
                CREATE INDEX IF NOT EXISTS idx_invoices_amount ON invoices(amount);
                CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(invoice_date);
                CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(match_status);

                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER,
                    file_row_num INTEGER,
                    payment_no TEXT NOT NULL,
                    payment_date DATE,
                    customer TEXT,
                    amount REAL,
                    status TEXT DEFAULT 'normal',
                    raw_data TEXT,
                    imported_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    match_status TEXT DEFAULT 'unmatched',
                    error_message TEXT,
                    FOREIGN KEY (batch_id) REFERENCES import_batches(id),
                    UNIQUE(payment_no, batch_id)
                );

                CREATE INDEX IF NOT EXISTS idx_payments_customer ON payments(customer);
                CREATE INDEX IF NOT EXISTS idx_payments_amount ON payments(amount);
                CREATE INDEX IF NOT EXISTS idx_payments_date ON payments(payment_date);
                CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(match_status);

                CREATE TABLE IF NOT EXISTS matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id INTEGER NOT NULL,
                    payment_id INTEGER NOT NULL,
                    match_type TEXT NOT NULL,
                    match_score REAL,
                    match_evidence TEXT,
                    status TEXT DEFAULT 'pending',
                    operator TEXT,
                    operator_remark TEXT,
                    confirmed_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    match_no TEXT UNIQUE,
                    FOREIGN KEY (invoice_id) REFERENCES invoices(id),
                    FOREIGN KEY (payment_id) REFERENCES payments(id)
                );

                CREATE INDEX IF NOT EXISTS idx_matches_invoice ON matches(invoice_id);
                CREATE INDEX IF NOT EXISTS idx_matches_payment ON matches(payment_id);
                CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status);
                CREATE INDEX IF NOT EXISTS idx_matches_no ON matches(match_no);

                CREATE TABLE IF NOT EXISTS match_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id INTEGER NOT NULL,
                    payment_id INTEGER NOT NULL,
                    match_score REAL,
                    match_reason TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (invoice_id) REFERENCES invoices(id),
                    FOREIGN KEY (payment_id) REFERENCES payments(id),
                    UNIQUE(invoice_id, payment_id)
                );

                CREATE TABLE IF NOT EXISTS status_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id INTEGER,
                    invoice_id INTEGER,
                    payment_id INTEGER,
                    old_status TEXT,
                    new_status TEXT,
                    operator TEXT,
                    remark TEXT,
                    changed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (match_id) REFERENCES matches(id)
                );

                CREATE INDEX IF NOT EXISTS idx_status_history_match ON status_history(match_id);

                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER,
                    file_type TEXT,
                    file_row_num INTEGER,
                    error_type TEXT,
                    error_message TEXT,
                    raw_data TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (batch_id) REFERENCES import_batches(id)
                );

                CREATE INDEX IF NOT EXISTS idx_errors_batch ON errors(batch_id);

                CREATE TABLE IF NOT EXISTS review_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_no TEXT UNIQUE NOT NULL,
                    snapshot_type TEXT NOT NULL,
                    description TEXT,
                    operator TEXT,
                    total_matches INTEGER DEFAULT 0,
                    matched_count INTEGER DEFAULT 0,
                    pending_count INTEGER DEFAULT 0,
                    exception_count INTEGER DEFAULT 0,
                    revoked_count INTEGER DEFAULT 0,
                    total_invoice_amount REAL DEFAULT 0,
                    total_payment_amount REAL DEFAULT 0,
                    matched_amount REAL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_no ON review_snapshots(snapshot_no);
                CREATE INDEX IF NOT EXISTS idx_snapshots_created ON review_snapshots(created_at);

                CREATE TABLE IF NOT EXISTS snapshot_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER NOT NULL,
                    match_id INTEGER,
                    match_no TEXT,
                    match_type TEXT,
                    match_score REAL,
                    match_evidence TEXT,
                    status TEXT,
                    invoice_id INTEGER,
                    invoice_no TEXT,
                    invoice_date TEXT,
                    inv_customer TEXT,
                    inv_amount REAL,
                    inv_batch_id INTEGER,
                    payment_id INTEGER,
                    payment_no TEXT,
                    payment_date TEXT,
                    pay_customer TEXT,
                    pay_amount REAL,
                    pay_batch_id INTEGER,
                    operator TEXT,
                    operator_remark TEXT,
                    confirmed_at TEXT,
                    created_at TEXT,
                    status_history TEXT,
                    candidate_payments TEXT,
                    current_owner TEXT,
                    lock_reason TEXT,
                    locked_at TEXT,
                    lock_expires_at TEXT,
                    lock_history TEXT,
                    last_confirm_evidence TEXT,
                    FOREIGN KEY (snapshot_id) REFERENCES review_snapshots(id)
                );

                CREATE INDEX IF NOT EXISTS idx_snapshot_items_snapshot ON snapshot_items(snapshot_id);
                CREATE INDEX IF NOT EXISTS idx_snapshot_items_match ON snapshot_items(match_no);

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    role TEXT NOT NULL DEFAULT 'reviewer',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_login_at DATETIME
                );

                CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
                CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

                CREATE TABLE IF NOT EXISTS match_locks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id INTEGER UNIQUE NOT NULL,
                    lock_owner TEXT NOT NULL,
                    lock_reason TEXT,
                    locked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    lock_expires_at DATETIME,
                    FOREIGN KEY (match_id) REFERENCES matches(id)
                );

                CREATE INDEX IF NOT EXISTS idx_match_locks_owner ON match_locks(lock_owner);
                CREATE INDEX IF NOT EXISTS idx_match_locks_expires ON match_locks(lock_expires_at);

                CREATE TABLE IF NOT EXISTS lock_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    reason TEXT,
                    old_owner TEXT,
                    new_owner TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (match_id) REFERENCES matches(id)
                );

                CREATE INDEX IF NOT EXISTS idx_lock_history_match ON lock_history(match_id);
                CREATE INDEX IF NOT EXISTS idx_lock_history_action ON lock_history(action);
                CREATE INDEX IF NOT EXISTS idx_lock_history_operator ON lock_history(operator);

                CREATE TABLE IF NOT EXISTS batch_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL,
                    conflict_type TEXT NOT NULL,
                    record_type TEXT NOT NULL,
                    record_no TEXT NOT NULL,
                    old_status TEXT,
                    new_status TEXT,
                    old_operator TEXT,
                    new_operator TEXT,
                    old_amount REAL,
                    new_amount REAL,
                    conflict_reason TEXT NOT NULL,
                    detected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (batch_id) REFERENCES import_batches(id)
                );

                CREATE INDEX IF NOT EXISTS idx_batch_conflicts_batch ON batch_conflicts(batch_id);
                CREATE INDEX IF NOT EXISTS idx_batch_conflicts_type ON batch_conflicts(conflict_type);
                CREATE INDEX IF NOT EXISTS idx_batch_conflicts_record ON batch_conflicts(record_no);

                CREATE TABLE IF NOT EXISTS session_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL,
                    value TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_session_state_key ON session_state(key);

                CREATE TABLE IF NOT EXISTS batch_change_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL,
                    change_type TEXT NOT NULL,
                    record_type TEXT NOT NULL,
                    record_no TEXT NOT NULL,
                    field_name TEXT,
                    old_value TEXT,
                    new_value TEXT,
                    change_summary TEXT NOT NULL,
                    before_summary TEXT,
                    after_summary TEXT,
                    impact_type TEXT,
                    impact_details TEXT,
                    impacted_match_ids TEXT,
                    operator TEXT,
                    detected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    processing_status TEXT DEFAULT 'pending',
                    processed_at DATETIME,
                    processed_by TEXT,
                    remark TEXT,
                    conflict_reason TEXT,
                    FOREIGN KEY (batch_id) REFERENCES import_batches(id)
                );

                CREATE INDEX IF NOT EXISTS idx_change_logs_batch ON batch_change_logs(batch_id);
                CREATE INDEX IF NOT EXISTS idx_change_logs_type ON batch_change_logs(change_type);
                CREATE INDEX IF NOT EXISTS idx_change_logs_record ON batch_change_logs(record_no);
                CREATE INDEX IF NOT EXISTS idx_change_logs_impact ON batch_change_logs(impact_type);
                CREATE INDEX IF NOT EXISTS idx_change_logs_status ON batch_change_logs(processing_status);

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_type TEXT NOT NULL,
                    action_category TEXT NOT NULL,
                    batch_id INTEGER,
                    record_type TEXT,
                    record_no TEXT,
                    operator TEXT,
                    action_summary TEXT NOT NULL,
                    action_details TEXT,
                    status TEXT DEFAULT 'success',
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (batch_id) REFERENCES import_batches(id)
                );

                CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action_type);
                CREATE INDEX IF NOT EXISTS idx_audit_logs_batch ON audit_logs(batch_id);
                CREATE INDEX IF NOT EXISTS idx_audit_logs_operator ON audit_logs(operator);
                CREATE INDEX IF NOT EXISTS idx_audit_logs_category ON audit_logs(action_category);
                CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at);

                CREATE TABLE IF NOT EXISTS handover_packages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    package_id TEXT UNIQUE NOT NULL,
                    config_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    operator TEXT,
                    description TEXT,
                    package_data TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME
                );

                CREATE INDEX IF NOT EXISTS idx_handover_packages_id ON handover_packages(package_id);
                CREATE INDEX IF NOT EXISTS idx_handover_packages_config ON handover_packages(config_hash);
                CREATE INDEX IF NOT EXISTS idx_handover_packages_status ON handover_packages(status);

                CREATE TABLE IF NOT EXISTS handover_undos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    undo_id TEXT UNIQUE NOT NULL,
                    package_id TEXT NOT NULL,
                    previous_state TEXT NOT NULL,
                    operator TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (package_id) REFERENCES handover_packages(package_id)
                );

                CREATE INDEX IF NOT EXISTS idx_handover_undos_package ON handover_undos(package_id);
                CREATE INDEX IF NOT EXISTS idx_handover_undos_id ON handover_undos(undo_id);

                CREATE TABLE IF NOT EXISTS handover_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    event_type TEXT NOT NULL,
                    package_id TEXT,
                    operator TEXT,
                    status TEXT NOT NULL DEFAULT 'success',
                    result_summary TEXT,
                    event_details TEXT,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (package_id) REFERENCES handover_packages(package_id)
                );

                CREATE INDEX IF NOT EXISTS idx_handover_events_id ON handover_events(event_id);
                CREATE INDEX IF NOT EXISTS idx_handover_events_package ON handover_events(package_id);
                CREATE INDEX IF NOT EXISTS idx_handover_events_type ON handover_events(event_type);
                CREATE INDEX IF NOT EXISTS idx_handover_events_created ON handover_events(created_at);
            """)

    @staticmethod
    def calculate_file_hash(file_path: str) -> str:
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def check_file_imported(self, file_path: str, file_type: str) -> Optional[Dict]:
        file_hash = self.calculate_file_hash(file_path)
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM import_batches WHERE file_hash = ? AND file_type = ?",
                (file_hash, file_type)
            ).fetchone()
            return dict(row) if row else None

    def create_batch(self, file_type: str, file_path: str, file_name: str,
                     total_rows: int, operator: str = None) -> int:
        file_hash = self.calculate_file_hash(file_path)
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO import_batches
                   (file_type, file_path, file_hash, file_name, total_rows, operator)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (file_type, file_path, file_hash, file_name, total_rows, operator)
            )
            if cursor.lastrowid == 0:
                row = conn.execute(
                    "SELECT id FROM import_batches WHERE file_hash = ? AND file_type = ?",
                    (file_hash, file_type)
                ).fetchone()
                return row["id"]
            return cursor.lastrowid

    def update_batch_stats(self, batch_id: int, success_rows: int, failed_rows: int) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE import_batches SET success_rows = ?, failed_rows = ? WHERE id = ?",
                (success_rows, failed_rows, batch_id)
            )

    def insert_invoice(self, batch_id: int, file_row_num: int, invoice_no: str,
                       invoice_date: str, customer: str, amount: float,
                       raw_data: str, status: str = INVOICE_STATUS_NORMAL,
                       error_message: str = None) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO invoices
                   (batch_id, file_row_num, invoice_no, invoice_date, customer, amount,
                    status, raw_data, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, file_row_num, invoice_no, invoice_date, customer, amount,
                 status, raw_data, error_message)
            )
            if cursor.lastrowid == 0:
                row = conn.execute(
                    "SELECT id FROM invoices WHERE invoice_no = ? AND batch_id = ?",
                    (invoice_no, batch_id)
                ).fetchone()
                return row["id"]
            return cursor.lastrowid

    def insert_payment(self, batch_id: int, file_row_num: int, payment_no: str,
                       payment_date: str, customer: str, amount: float,
                       raw_data: str, status: str = PAYMENT_STATUS_NORMAL,
                       error_message: str = None) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO payments
                   (batch_id, file_row_num, payment_no, payment_date, customer, amount,
                    status, raw_data, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, file_row_num, payment_no, payment_date, customer, amount,
                 status, raw_data, error_message)
            )
            if cursor.lastrowid == 0:
                row = conn.execute(
                    "SELECT id FROM payments WHERE payment_no = ? AND batch_id = ?",
                    (payment_no, batch_id)
                ).fetchone()
                return row["id"]
            return cursor.lastrowid

    def insert_error(self, batch_id: int, file_type: str, file_row_num: int,
                     error_type: str, error_message: str, raw_data: str = None) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO errors
                   (batch_id, file_type, file_row_num, error_type, error_message, raw_data)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (batch_id, file_type, file_row_num, error_type, error_message, raw_data)
            )

    def get_unmatched_invoices(self) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT i.*, b.file_name, b.imported_at as batch_imported_at
                   FROM invoices i
                   LEFT JOIN import_batches b ON i.batch_id = b.id
                   WHERE i.match_status = 'unmatched' AND i.status = 'normal'
                   ORDER BY i.invoice_date""",
            ).fetchall()
            return [dict(r) for r in rows]

    def get_unmatched_invoices_by_batch(self, batch_id: int) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT i.*, b.file_name, b.imported_at as batch_imported_at
                   FROM invoices i
                   LEFT JOIN import_batches b ON i.batch_id = b.id
                   WHERE i.batch_id = ? AND i.match_status = 'unmatched' AND i.status = 'normal'
                   ORDER BY i.invoice_date""",
                (batch_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_unmatched_payments_by_batch(self, batch_id: int) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT p.*, b.file_name, b.imported_at as batch_imported_at
                   FROM payments p
                   LEFT JOIN import_batches b ON p.batch_id = b.id
                   WHERE p.batch_id = ? AND p.match_status = 'unmatched' AND p.status = 'normal'
                   ORDER BY p.payment_date""",
                (batch_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_unmatched_payments(self) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT p.*, b.file_name, b.imported_at as batch_imported_at
                   FROM payments p
                   LEFT JOIN import_batches b ON p.batch_id = b.id
                   WHERE p.match_status = 'unmatched' AND p.status = 'normal'
                   ORDER BY p.payment_date""",
            ).fetchall()
            return [dict(r) for r in rows]

    def insert_match_candidate(self, invoice_id: int, payment_id: int,
                               match_score: float, match_reason: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO match_candidates
                   (invoice_id, payment_id, match_score, match_reason)
                   VALUES (?, ?, ?, ?)""",
                (invoice_id, payment_id, match_score, match_reason)
            )

    def clear_match_candidates(self) -> None:
        with self._get_conn() as conn:
            conn.execute("DELETE FROM match_candidates")

    def get_match_candidates(self, invoice_id: int = None) -> List[Dict]:
        with self._get_conn() as conn:
            if invoice_id:
                rows = conn.execute(
                    """SELECT mc.*, i.invoice_no, i.invoice_date, i.customer as inv_customer,
                              i.amount as inv_amount, p.payment_no, p.payment_date,
                              p.customer as pay_customer, p.amount as pay_amount
                       FROM match_candidates mc
                       JOIN invoices i ON mc.invoice_id = i.id
                       JOIN payments p ON mc.payment_id = p.id
                       WHERE mc.invoice_id = ?
                       ORDER BY mc.match_score DESC""",
                    (invoice_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT mc.*, i.invoice_no, i.invoice_date, i.customer as inv_customer,
                              i.amount as inv_amount, p.payment_no, p.payment_date,
                              p.customer as pay_customer, p.amount as pay_amount
                       FROM match_candidates mc
                       JOIN invoices i ON mc.invoice_id = i.id
                       JOIN payments p ON mc.payment_id = p.id
                       ORDER BY mc.invoice_id, mc.match_score DESC""",
                ).fetchall()
            return [dict(r) for r in rows]

    def create_match(self, invoice_id: int, payment_id: int, match_type: str,
                     match_score: float, match_evidence: str, status: str = MATCH_STATUS_PENDING,
                     operator: str = None, operator_remark: str = None) -> str:
        match_no = self._generate_match_no()
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO matches
                   (invoice_id, payment_id, match_type, match_score, match_evidence,
                    status, operator, operator_remark, confirmed_at, match_no)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (invoice_id, payment_id, match_type, match_score, match_evidence,
                 status, operator, operator_remark,
                 datetime.now() if status == MATCH_STATUS_MATCHED else None,
                 match_no)
            )

            if status == MATCH_STATUS_MATCHED:
                self._update_invoice_match_status(conn, invoice_id, MATCH_STATUS_MATCHED)
                self._update_payment_match_status(conn, payment_id, MATCH_STATUS_MATCHED)
                self._record_status_history(conn, None, invoice_id, payment_id,
                                            MATCH_STATUS_UNMATCHED, MATCH_STATUS_MATCHED,
                                            operator, operator_remark)

            return match_no

    def confirm_match(self, match_id: int, operator: str, remark: str = None) -> None:
        with self._get_conn() as conn:
            match = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
            if not match:
                raise ValueError(f"匹配记录不存在: {match_id}")

            conn.execute(
                """UPDATE matches
                   SET status = ?, operator = ?, operator_remark = ?, confirmed_at = ?
                   WHERE id = ?""",
                (MATCH_STATUS_MATCHED, operator, remark, datetime.now(), match_id)
            )

            self._update_invoice_match_status(conn, match["invoice_id"], MATCH_STATUS_MATCHED)
            self._update_payment_match_status(conn, match["payment_id"], MATCH_STATUS_MATCHED)
            self._record_status_history(conn, match_id, match["invoice_id"], match["payment_id"],
                                        match["status"], MATCH_STATUS_MATCHED,
                                        operator, remark)

            conn.execute(
                "DELETE FROM match_candidates WHERE invoice_id = ? OR payment_id = ?",
                (match["invoice_id"], match["payment_id"])
            )

    def reject_match(self, match_id: int, operator: str, remark: str = None) -> None:
        with self._get_conn() as conn:
            match = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
            if not match:
                raise ValueError(f"匹配记录不存在: {match_id}")

            conn.execute(
                """UPDATE matches
                   SET status = ?, operator = ?, operator_remark = ?, confirmed_at = ?
                   WHERE id = ?""",
                (MATCH_STATUS_EXCEPTION, operator, remark, datetime.now(), match_id)
            )

            self._update_invoice_match_status(conn, match["invoice_id"], MATCH_STATUS_UNMATCHED)
            self._update_payment_match_status(conn, match["payment_id"], MATCH_STATUS_UNMATCHED)
            self._record_status_history(conn, match_id, match["invoice_id"], match["payment_id"],
                                        match["status"], MATCH_STATUS_EXCEPTION,
                                        operator, remark)

    def revoke_match(self, match_id: int, operator: str, remark: str = None) -> None:
        with self._get_conn() as conn:
            match = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
            if not match:
                raise ValueError(f"匹配记录不存在: {match_id}")

            if match["status"] != MATCH_STATUS_MATCHED:
                raise ValueError(f"只能撤销已确认的匹配，当前状态: {match['status']}")

            conn.execute(
                """UPDATE matches
                   SET status = ?, operator_remark = COALESCE(operator_remark, '') || ?
                   WHERE id = ?""",
                (MATCH_STATUS_REVOKED,
                 f" [撤销备注: {remark}]" if remark else "",
                 match_id)
            )

            self._update_invoice_match_status(conn, match["invoice_id"], MATCH_STATUS_UNMATCHED)
            self._update_payment_match_status(conn, match["payment_id"], MATCH_STATUS_UNMATCHED)
            self._record_status_history(conn, match_id, match["invoice_id"], match["payment_id"],
                                        MATCH_STATUS_MATCHED, MATCH_STATUS_REVOKED,
                                        operator, remark)

    def get_matches_by_status(self, status: str = None) -> List[Dict]:
        with self._get_conn() as conn:
            sql = """
                SELECT m.*, i.invoice_no, i.invoice_date, i.customer as inv_customer,
                       i.amount as inv_amount, i.file_row_num as inv_row_num,
                       p.payment_no, p.payment_date, p.customer as pay_customer,
                       p.amount as pay_amount, p.file_row_num as pay_row_num,
                       ib.file_name as inv_file, pb.file_name as pay_file
                FROM matches m
                JOIN invoices i ON m.invoice_id = i.id
                JOIN payments p ON m.payment_id = p.id
                LEFT JOIN import_batches ib ON i.batch_id = ib.id
                LEFT JOIN import_batches pb ON p.batch_id = pb.id
            """
            params = ()
            if status:
                sql += " WHERE m.status = ?"
                params = (status,)
            sql += " ORDER BY m.created_at DESC"

            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_match_by_id(self, match_id: int) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                """SELECT m.*, i.invoice_no, i.invoice_date, i.customer as inv_customer,
                          i.amount as inv_amount, p.payment_no, p.payment_date,
                          p.customer as pay_customer, p.amount as pay_amount
                   FROM matches m
                   JOIN invoices i ON m.invoice_id = i.id
                   JOIN payments p ON m.payment_id = p.id
                   WHERE m.id = ?""",
                (match_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_match_by_no(self, match_no: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                """SELECT m.*, i.invoice_no, i.invoice_date, i.customer as inv_customer,
                          i.amount as inv_amount, p.payment_no, p.payment_date,
                          p.customer as pay_customer, p.amount as pay_amount
                   FROM matches m
                   JOIN invoices i ON m.invoice_id = i.id
                   JOIN payments p ON m.payment_id = p.id
                   WHERE m.match_no = ?""",
                (match_no,)
            ).fetchone()
            return dict(row) if row else None

    def get_status_history(self, match_id: int = None, invoice_id: int = None,
                           payment_id: int = None) -> List[Dict]:
        with self._get_conn() as conn:
            sql = "SELECT * FROM status_history WHERE 1=1"
            params = []
            if match_id:
                sql += " AND match_id = ?"
                params.append(match_id)
            if invoice_id:
                sql += " AND invoice_id = ?"
                params.append(invoice_id)
            if payment_id:
                sql += " AND payment_id = ?"
                params.append(payment_id)
            sql += " ORDER BY changed_at DESC"

            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def get_errors(self, batch_id: int = None) -> List[Dict]:
        with self._get_conn() as conn:
            if batch_id:
                rows = conn.execute(
                    "SELECT * FROM errors WHERE batch_id = ? ORDER BY created_at",
                    (batch_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM errors ORDER BY created_at"
                ).fetchall()
            return [dict(r) for r in rows]

    def get_invoice_by_id(self, invoice_id: int) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_payment_by_id(self, payment_id: int) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM payments WHERE id = ?", (payment_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_batches(self) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM import_batches ORDER BY imported_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_batch(self, batch_id: int) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM import_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_statistics(self) -> Dict:
        with self._get_conn() as conn:
            stats = {}
            stats["total_invoices"] = conn.execute(
                "SELECT COUNT(*) FROM invoices"
            ).fetchone()[0]
            stats["total_payments"] = conn.execute(
                "SELECT COUNT(*) FROM payments"
            ).fetchone()[0]
            stats["matched_invoices"] = conn.execute(
                "SELECT COUNT(*) FROM invoices WHERE match_status = 'matched'"
            ).fetchone()[0]
            stats["matched_payments"] = conn.execute(
                "SELECT COUNT(*) FROM payments WHERE match_status = 'matched'"
            ).fetchone()[0]
            stats["unmatched_invoices"] = conn.execute(
                "SELECT COUNT(*) FROM invoices WHERE match_status = 'unmatched' AND status = 'normal'"
            ).fetchone()[0]
            stats["unmatched_payments"] = conn.execute(
                "SELECT COUNT(*) FROM payments WHERE match_status = 'unmatched' AND status = 'normal'"
            ).fetchone()[0]
            stats["pending_matches"] = conn.execute(
                "SELECT COUNT(*) FROM matches WHERE status = 'pending'"
            ).fetchone()[0]
            stats["confirmed_matches"] = conn.execute(
                "SELECT COUNT(*) FROM matches WHERE status = 'matched'"
            ).fetchone()[0]
            stats["exception_matches"] = conn.execute(
                "SELECT COUNT(*) FROM matches WHERE status = 'exception'"
            ).fetchone()[0]
            stats["revoked_matches"] = conn.execute(
                "SELECT COUNT(*) FROM matches WHERE status = 'revoked'"
            ).fetchone()[0]
            stats["total_errors"] = conn.execute(
                "SELECT COUNT(*) FROM errors"
            ).fetchone()[0]
            stats["invalid_invoices"] = conn.execute(
                "SELECT COUNT(*) FROM invoices WHERE status = 'invalid'"
            ).fetchone()[0]
            stats["invalid_payments"] = conn.execute(
                "SELECT COUNT(*) FROM payments WHERE status = 'invalid'"
            ).fetchone()[0]
            return stats

    def _generate_match_no(self) -> str:
        now = datetime.now()
        prefix = f"M{now.strftime('%Y%m%d')}"
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM matches WHERE match_no LIKE ?",
                (prefix + "%",)
            ).fetchone()
            seq = row[0] + 1
            return f"{prefix}{seq:04d}"

    def _update_invoice_match_status(self, conn, invoice_id: int, status: str) -> None:
        conn.execute(
            "UPDATE invoices SET match_status = ? WHERE id = ?",
            (status, invoice_id)
        )

    def _update_payment_match_status(self, conn, payment_id: int, status: str) -> None:
        conn.execute(
            "UPDATE payments SET match_status = ? WHERE id = ?",
            (status, payment_id)
        )

    def _record_status_history(self, conn, match_id: Optional[int], invoice_id: int,
                               payment_id: int, old_status: str, new_status: str,
                               operator: str, remark: str) -> None:
        conn.execute(
            """INSERT INTO status_history
               (match_id, invoice_id, payment_id, old_status, new_status, operator, remark)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (match_id, invoice_id, payment_id, old_status, new_status, operator, remark)
        )

    def create_review_snapshot(self, snapshot_type: str, description: str = None,
                               operator: str = None) -> Dict:
        snapshot_no = self._generate_snapshot_no()
        matches = self.get_matches_by_status()

        matched_count = sum(1 for m in matches if m["status"] == MATCH_STATUS_MATCHED)
        pending_count = sum(1 for m in matches if m["status"] == MATCH_STATUS_PENDING)
        exception_count = sum(1 for m in matches if m["status"] == MATCH_STATUS_EXCEPTION)
        revoked_count = sum(1 for m in matches if m["status"] == MATCH_STATUS_REVOKED)

        matched_amount = sum(m["inv_amount"] for m in matches if m["status"] == MATCH_STATUS_MATCHED)

        stats = self.get_statistics()
        total_inv_amt = self._get_total_invoice_amount()
        total_pay_amt = self._get_total_payment_amount()

        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO review_snapshots
                   (snapshot_no, snapshot_type, description, operator,
                    total_matches, matched_count, pending_count,
                    exception_count, revoked_count,
                    total_invoice_amount, total_payment_amount, matched_amount)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (snapshot_no, snapshot_type, description, operator,
                 len(matches), matched_count, pending_count,
                 exception_count, revoked_count,
                 total_inv_amt, total_pay_amt, matched_amount)
            )
            snapshot_id = cursor.lastrowid

            for m in matches:
                history = self.get_status_history(match_id=m["id"])
                history_json = json.dumps(history, ensure_ascii=False) if history else "[]"

                candidates = self.get_match_candidates(invoice_id=m["invoice_id"])
                candidates_json = json.dumps(candidates, ensure_ascii=False) if candidates else "[]"

                lock = self.get_match_lock(m["id"])
                current_owner = lock["lock_owner"] if lock else None
                lock_reason = lock["lock_reason"] if lock else None
                locked_at = lock["locked_at"] if lock else None
                lock_expires_at = lock["lock_expires_at"] if lock else None

                lock_history = self.get_lock_history(match_id=m["id"])
                lock_history_json = json.dumps(lock_history, ensure_ascii=False) if lock_history else "[]"

                last_confirm_evidence = None
                if m["status"] == MATCH_STATUS_MATCHED:
                    last_confirm_evidence = json.dumps({
                        "operator": m["operator"],
                        "remark": m["operator_remark"],
                        "confirmed_at": m["confirmed_at"],
                        "match_type": m["match_type"],
                        "match_score": m["match_score"],
                        "match_evidence": m["match_evidence"],
                    }, ensure_ascii=False)

                conn.execute(
                    """INSERT INTO snapshot_items
                       (snapshot_id, match_id, match_no, match_type, match_score,
                        match_evidence, status, invoice_id, invoice_no, invoice_date,
                        inv_customer, inv_amount, inv_batch_id,
                        payment_id, payment_no, payment_date, pay_customer,
                        pay_amount, pay_batch_id, operator, operator_remark,
                        confirmed_at, created_at, status_history, candidate_payments,
                        current_owner, lock_reason, locked_at, lock_expires_at,
                        lock_history, last_confirm_evidence)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?)""",
                    (snapshot_id, m["id"], m["match_no"], m["match_type"], m["match_score"],
                     m["match_evidence"], m["status"], m["invoice_id"], m["invoice_no"],
                     m["invoice_date"], m["inv_customer"], m["inv_amount"], m.get("batch_id"),
                     m["payment_id"], m["payment_no"], m["payment_date"], m["pay_customer"],
                     m["pay_amount"], None, m["operator"], m["operator_remark"],
                     m["confirmed_at"], m["created_at"], history_json, candidates_json,
                     current_owner, lock_reason, locked_at, lock_expires_at,
                     lock_history_json, last_confirm_evidence)
                )

        return self.get_snapshot_by_no(snapshot_no)

    def _generate_snapshot_no(self) -> str:
        now = datetime.now()
        prefix = f"R{now.strftime('%Y%m%d')}"
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM review_snapshots WHERE snapshot_no LIKE ?",
                (prefix + "%",)
            ).fetchone()
            seq = row[0] + 1
            return f"{prefix}{seq:04d}"

    def _get_total_invoice_amount(self) -> float:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM invoices WHERE status = 'normal'"
            ).fetchone()
            return row[0]

    def _get_total_payment_amount(self) -> float:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'normal'"
            ).fetchone()
            return row[0]

    def get_snapshot_by_no(self, snapshot_no: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM review_snapshots WHERE snapshot_no = ?",
                (snapshot_no,)
            ).fetchone()
            return dict(row) if row else None

    def get_snapshot_by_id(self, snapshot_id: int) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM review_snapshots WHERE id = ?",
                (snapshot_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_snapshots(self, limit: int = 50) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM review_snapshots ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_snapshot_items(self, snapshot_id: int) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM snapshot_items WHERE snapshot_id = ? ORDER BY id",
                (snapshot_id,)
            ).fetchall()
            items = [dict(r) for r in rows]
            for item in items:
                if item.get("status_history"):
                    try:
                        item["status_history"] = json.loads(item["status_history"])
                    except (json.JSONDecodeError, TypeError):
                        item["status_history"] = []
                if item.get("candidate_payments"):
                    try:
                        item["candidate_payments"] = json.loads(item["candidate_payments"])
                    except (json.JSONDecodeError, TypeError):
                        item["candidate_payments"] = []
                if item.get("lock_history"):
                    try:
                        item["lock_history"] = json.loads(item["lock_history"])
                    except (json.JSONDecodeError, TypeError):
                        item["lock_history"] = []
                if item.get("last_confirm_evidence"):
                    try:
                        item["last_confirm_evidence"] = json.loads(item["last_confirm_evidence"])
                    except (json.JSONDecodeError, TypeError):
                        item["last_confirm_evidence"] = None
            return items

    def check_invoice_conflicts(self, invoice_no: str = None) -> List[Dict]:
        with self._get_conn() as conn:
            if invoice_no:
                rows = conn.execute(
                    """SELECT m.*, i.invoice_no, i.invoice_date, i.customer as inv_customer,
                              i.amount as inv_amount, p.payment_no, p.payment_date,
                              p.customer as pay_customer, p.amount as pay_amount
                       FROM matches m
                       JOIN invoices i ON m.invoice_id = i.id
                       LEFT JOIN payments p ON m.payment_id = p.id
                       WHERE i.invoice_no = ?
                       ORDER BY m.created_at""",
                    (invoice_no,)
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT m.*, i.invoice_no, i.invoice_date, i.customer as inv_customer,
                              i.amount as inv_amount, p.payment_no, p.payment_date,
                              p.customer as pay_customer, p.amount as pay_amount
                       FROM matches m
                       JOIN invoices i ON m.invoice_id = i.id
                       LEFT JOIN payments p ON m.payment_id = p.id
                       ORDER BY i.invoice_no, m.created_at""",
                ).fetchall()

            invoice_matches = {}
            for r in rows:
                inv_no = r["invoice_no"]
                if inv_no not in invoice_matches:
                    invoice_matches[inv_no] = []
                invoice_matches[inv_no].append(dict(r))

            conflicts = []
            for inv_no, matches in invoice_matches.items():
                operators = set()
                active_count = 0
                for m in matches:
                    if m["operator"]:
                        operators.add(m["operator"])
                    if m["status"] != MATCH_STATUS_REVOKED:
                        active_count += 1
                if len(matches) > 1 and len(operators) > 1:
                    conflicts.append({
                        "invoice_no": inv_no,
                        "match_count": len(matches),
                        "active_count": active_count,
                        "operators": list(operators),
                        "matches": matches
                    })
            return conflicts

    def get_user(self, username: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?",
                (username,)
            ).fetchone()
            return dict(row) if row else None

    def create_user(self, username: str, role: str = USER_ROLE_REVIEWER,
                    status: str = USER_STATUS_ACTIVE) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO users (username, role, status)
                   VALUES (?, ?, ?)""",
                (username, role, status)
            )
            if cursor.lastrowid == 0:
                row = conn.execute(
                    "SELECT id FROM users WHERE username = ?",
                    (username,)
                ).fetchone()
                return row["id"]
            return cursor.lastrowid

    def update_user_role(self, username: str, role: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE users SET role = ? WHERE username = ?",
                (role, username)
            )

    def list_users(self) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY username"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_match_lock(self, match_id: int) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM match_locks WHERE match_id = ?",
                (match_id,)
            ).fetchone()
            return dict(row) if row else None

    def is_lock_expired(self, match_id: int) -> bool:
        lock = self.get_match_lock(match_id)
        if not lock:
            return False
        if not lock.get("lock_expires_at"):
            return False
        try:
            expire_time = datetime.strptime(
                lock["lock_expires_at"], "%Y-%m-%d %H:%M:%S"
            )
            return expire_time <= datetime.now()
        except (ValueError, TypeError):
            return False

    def get_locks_by_owner(self, owner: str) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT ml.*, m.match_no, m.status as match_status,
                          i.invoice_no, p.payment_no
                   FROM match_locks ml
                   JOIN matches m ON ml.match_id = m.id
                   JOIN invoices i ON m.invoice_id = i.id
                   JOIN payments p ON m.payment_id = p.id
                   WHERE ml.lock_owner = ?
                   ORDER BY ml.locked_at DESC""",
                (owner,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_expired_locks(self, now: datetime = None) -> List[Dict]:
        if now is None:
            now = datetime.now()
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT ml.*, m.match_no, m.status as match_status,
                          i.invoice_no, p.payment_no
                   FROM match_locks ml
                   JOIN matches m ON ml.match_id = m.id
                   JOIN invoices i ON m.invoice_id = i.id
                   JOIN payments p ON m.payment_id = p.id
                   WHERE ml.lock_expires_at IS NOT NULL AND ml.lock_expires_at <= ?
                   ORDER BY ml.lock_expires_at""",
                (now.strftime("%Y-%m-%d %H:%M:%S"),)
            ).fetchall()
            return [dict(r) for r in rows]

    def list_all_locks(self, include_expired: bool = False) -> List[Dict]:
        with self._get_conn() as conn:
            if include_expired:
                rows = conn.execute(
                    """SELECT ml.*, m.match_no, m.status as match_status,
                              i.invoice_no, p.payment_no
                       FROM match_locks ml
                       JOIN matches m ON ml.match_id = m.id
                       JOIN invoices i ON m.invoice_id = i.id
                       JOIN payments p ON m.payment_id = p.id
                       ORDER BY ml.locked_at DESC"""
                ).fetchall()
            else:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                rows = conn.execute(
                    """SELECT ml.*, m.match_no, m.status as match_status,
                              i.invoice_no, p.payment_no
                       FROM match_locks ml
                       JOIN matches m ON ml.match_id = m.id
                       JOIN invoices i ON m.invoice_id = i.id
                       JOIN payments p ON m.payment_id = p.id
                       WHERE ml.lock_expires_at IS NULL OR ml.lock_expires_at > ?
                       ORDER BY ml.locked_at DESC""",
                    (now,)
                ).fetchall()
            return [dict(r) for r in rows]

    def acquire_lock(self, match_id: int, owner: str, reason: str = None,
                     expire_seconds: int = None) -> Dict:
        match = self.get_match_by_id(match_id)
        if not match:
            raise ValueError(f"匹配记录不存在: {match_id}")

        now = datetime.now()
        expires_at = None
        if expire_seconds and expire_seconds > 0:
            from datetime import timedelta
            expires_at = now + timedelta(seconds=expire_seconds)
            expires_at_str = expires_at.strftime("%Y-%m-%d %H:%M:%S")
        else:
            expires_at_str = None

        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM match_locks WHERE match_id = ?",
                (match_id,)
            ).fetchone()

            if existing:
                existing_expires = existing["lock_expires_at"]
                if existing_expires:
                    expire_time = datetime.strptime(existing_expires, "%Y-%m-%d %H:%M:%S")
                    if expire_time > now:
                        if existing["lock_owner"] == owner:
                            return {
                                "success": True,
                                "already_locked": True,
                                "match_id": match_id,
                                "match_no": match["match_no"],
                                "owner": owner,
                                "message": "您已锁定该记录"
                            }
                        raise ValueError(
                            f"记录已被 {existing['lock_owner']} 锁定，"
                            f"锁定时间: {existing['locked_at']}"
                        )

            if existing:
                conn.execute(
                    """UPDATE match_locks
                       SET lock_owner = ?, lock_reason = ?, locked_at = ?,
                           lock_expires_at = ?
                       WHERE match_id = ?""",
                    (owner, reason, now.strftime("%Y-%m-%d %H:%M:%S"),
                     expires_at_str, match_id)
                )
            else:
                conn.execute(
                    """INSERT INTO match_locks
                       (match_id, lock_owner, lock_reason, locked_at, lock_expires_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (match_id, owner, reason,
                     now.strftime("%Y-%m-%d %H:%M:%S"), expires_at_str)
                )

            old_owner = existing["lock_owner"] if existing else None
            self._record_lock_history(
                conn, match_id, LOCK_ACTION_LOCK, owner, reason,
                old_owner, owner
            )

            return {
                "success": True,
                "already_locked": False,
                "match_id": match_id,
                "match_no": match["match_no"],
                "owner": owner,
                "locked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "expires_at": expires_at_str,
                "message": "锁定成功"
            }

    def release_lock(self, match_id: int, operator: str, reason: str = None) -> Dict:
        match = self.get_match_by_id(match_id)
        if not match:
            raise ValueError(f"匹配记录不存在: {match_id}")

        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM match_locks WHERE match_id = ?",
                (match_id,)
            ).fetchone()

            if not existing:
                return {
                    "success": False,
                    "match_id": match_id,
                    "match_no": match["match_no"],
                    "message": "该记录未被锁定"
                }

            old_owner = existing["lock_owner"]

            conn.execute("DELETE FROM match_locks WHERE match_id = ?", (match_id,))

            self._record_lock_history(
                conn, match_id, LOCK_ACTION_UNLOCK, operator, reason,
                old_owner, None
            )

            return {
                "success": True,
                "match_id": match_id,
                "match_no": match["match_no"],
                "old_owner": old_owner,
                "message": "解锁成功"
            }

    def transfer_lock(self, match_id: int, from_owner: str, to_owner: str,
                      reason: str = None) -> Dict:
        match = self.get_match_by_id(match_id)
        if not match:
            raise ValueError(f"匹配记录不存在: {match_id}")

        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM match_locks WHERE match_id = ?",
                (match_id,)
            ).fetchone()

            if not existing:
                raise ValueError("该记录未被锁定，无法转交")

            if existing["lock_owner"] != from_owner:
                raise ValueError(
                    f"当前锁定人是 {existing['lock_owner']}，"
                    f"不是 {from_owner}，无法转交"
                )

            conn.execute(
                "UPDATE match_locks SET lock_owner = ? WHERE match_id = ?",
                (to_owner, match_id)
            )

            self._record_lock_history(
                conn, match_id, LOCK_ACTION_TRANSFER, from_owner, reason,
                from_owner, to_owner
            )

            return {
                "success": True,
                "match_id": match_id,
                "match_no": match["match_no"],
                "old_owner": from_owner,
                "new_owner": to_owner,
                "message": "转交成功"
            }

    def takeover_lock(self, match_id: int, operator: str,
                      reason: str = None) -> Dict:
        match = self.get_match_by_id(match_id)
        if not match:
            raise ValueError(f"匹配记录不存在: {match_id}")

        now = datetime.now()

        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM match_locks WHERE match_id = ?",
                (match_id,)
            ).fetchone()

            if not existing:
                conn.execute(
                    """INSERT INTO match_locks
                       (match_id, lock_owner, lock_reason, locked_at)
                       VALUES (?, ?, ?, ?)""",
                    (match_id, operator, reason or "接管未锁定记录",
                     now.strftime("%Y-%m-%d %H:%M:%S"))
                )
                self._record_lock_history(
                    conn, match_id, LOCK_ACTION_TAKEOVER, operator, reason,
                    None, operator
                )
                return {
                    "success": True,
                    "match_id": match_id,
                    "match_no": match["match_no"],
                    "old_owner": None,
                    "new_owner": operator,
                    "was_expired": False,
                    "message": "接管成功（原记录未锁定）"
                }

            old_owner = existing["lock_owner"]
            is_expired = False

            if existing["lock_expires_at"]:
                expire_time = datetime.strptime(
                    existing["lock_expires_at"], "%Y-%m-%d %H:%M:%S"
                )
                if expire_time <= now:
                    is_expired = True

            if not is_expired and old_owner == operator:
                return {
                    "success": True,
                    "already_locked": True,
                    "match_id": match_id,
                    "match_no": match["match_no"],
                    "owner": operator,
                    "message": "您已锁定该记录"
                }

            conn.execute(
                """UPDATE match_locks
                   SET lock_owner = ?, lock_reason = ?, locked_at = ?,
                       lock_expires_at = NULL
                   WHERE match_id = ?""",
                (operator, reason, now.strftime("%Y-%m-%d %H:%M:%S"), match_id)
            )

            self._record_lock_history(
                conn, match_id, LOCK_ACTION_TAKEOVER, operator, reason,
                old_owner, operator
            )

            return {
                "success": True,
                "match_id": match_id,
                "match_no": match["match_no"],
                "old_owner": old_owner,
                "new_owner": operator,
                "was_expired": is_expired,
                "message": f"接管成功（原锁定人: {old_owner}）"
            }

    def force_unlock(self, match_id: int, operator: str,
                     reason: str = None) -> Dict:
        match = self.get_match_by_id(match_id)
        if not match:
            raise ValueError(f"匹配记录不存在: {match_id}")

        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM match_locks WHERE match_id = ?",
                (match_id,)
            ).fetchone()

            if not existing:
                return {
                    "success": False,
                    "match_id": match_id,
                    "match_no": match["match_no"],
                    "message": "该记录未被锁定"
                }

            old_owner = existing["lock_owner"]

            conn.execute("DELETE FROM match_locks WHERE match_id = ?", (match_id,))

            self._record_lock_history(
                conn, match_id, LOCK_ACTION_FORCE_UNLOCK, operator, reason,
                old_owner, None
            )

            return {
                "success": True,
                "match_id": match_id,
                "match_no": match["match_no"],
                "old_owner": old_owner,
                "message": "强制解锁成功"
            }

    def batch_force_unlock(self, operator: str, reason: str = None,
                           owner: str = None) -> Dict:
        with self._get_conn() as conn:
            if owner:
                rows = conn.execute(
                    "SELECT * FROM match_locks WHERE lock_owner = ?",
                    (owner,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM match_locks").fetchall()

            if not rows:
                return {
                    "success": True,
                    "count": 0,
                    "unlocked_count": 0,
                    "message": "没有需要解锁的记录"
                }

            count = 0
            for row in rows:
                conn.execute(
                    "DELETE FROM match_locks WHERE match_id = ?",
                    (row["match_id"],)
                )
                self._record_lock_history(
                    conn, row["match_id"], LOCK_ACTION_FORCE_UNLOCK,
                    operator, reason, row["lock_owner"], None
                )
                count += 1

            return {
                "success": True,
                "count": count,
                "unlocked_count": count,
                "message": f"批量解锁 {count} 条记录"
            }

    def get_lock_history(self, match_id: int = None,
                         operator: str = None,
                         limit: int = 100) -> List[Dict]:
        with self._get_conn() as conn:
            sql = "SELECT * FROM lock_history WHERE 1=1"
            params = []
            if match_id:
                sql += " AND match_id = ?"
                params.append(match_id)
            if operator:
                sql += " AND operator = ?"
                params.append(operator)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def _record_lock_history(self, conn, match_id: int, action: str,
                             operator: str, reason: str,
                             old_owner: str, new_owner: str) -> None:
        conn.execute(
            """INSERT INTO lock_history
               (match_id, action, operator, reason, old_owner, new_owner)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (match_id, action, operator, reason, old_owner, new_owner)
        )

    def is_match_locked_by_other(self, match_id: int, operator: str) -> bool:
        lock = self.get_match_lock(match_id)
        if not lock:
            return False
        if lock["lock_owner"] == operator:
            return False
        if lock.get("lock_expires_at"):
            expire_time = datetime.strptime(
                lock["lock_expires_at"], "%Y-%m-%d %H:%M:%S"
            )
            if expire_time <= datetime.now():
                return False
        return True

    def get_matches_with_lock_info(self, status: str = None,
                                    owner: str = None) -> List[Dict]:
        matches = self.get_matches_by_status(status)
        if not matches:
            return []

        result = []
        for m in matches:
            lock = self.get_match_lock(m["id"])
            m_with_lock = dict(m)
            if lock:
                m_with_lock["current_owner"] = lock["lock_owner"]
                m_with_lock["lock_reason"] = lock["lock_reason"]
                m_with_lock["locked_at"] = lock["locked_at"]
                m_with_lock["lock_expires_at"] = lock["lock_expires_at"]
                m_with_lock["is_locked"] = True
                m_with_lock["is_lock_expired"] = False
                if lock.get("lock_expires_at"):
                    expire_time = datetime.strptime(
                        lock["lock_expires_at"], "%Y-%m-%d %H:%M:%S"
                    )
                    m_with_lock["is_lock_expired"] = expire_time <= datetime.now()
            else:
                m_with_lock["current_owner"] = None
                m_with_lock["lock_reason"] = None
                m_with_lock["locked_at"] = None
                m_with_lock["lock_expires_at"] = None
                m_with_lock["is_locked"] = False
                m_with_lock["is_lock_expired"] = False

            if owner and m_with_lock.get("current_owner") != owner:
                continue

            result.append(m_with_lock)

        return result

    def get_match_with_lock_by_id(self, match_id: int) -> Optional[Dict]:
        match = self.get_match_by_id(match_id)
        if not match:
            return None

        lock = self.get_match_lock(match_id)
        if lock:
            match["current_owner"] = lock["lock_owner"]
            match["lock_reason"] = lock["lock_reason"]
            match["locked_at"] = lock["locked_at"]
            match["lock_expires_at"] = lock["lock_expires_at"]
            match["is_locked"] = True
            if lock.get("lock_expires_at"):
                expire_time = datetime.strptime(
                    lock["lock_expires_at"], "%Y-%m-%d %H:%M:%S"
                )
                match["is_lock_expired"] = expire_time <= datetime.now()
            else:
                match["is_lock_expired"] = False
        else:
            match["current_owner"] = None
            match["lock_reason"] = None
            match["locked_at"] = None
            match["lock_expires_at"] = None
            match["is_locked"] = False
            match["is_lock_expired"] = False

        return match

    def insert_batch_conflict(self, batch_id: int, conflict_type: str, record_type: str,
                              record_no: str, conflict_reason: str,
                              old_status: str = None, new_status: str = None,
                              old_operator: str = None, new_operator: str = None,
                              old_amount: float = None, new_amount: float = None) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO batch_conflicts
                   (batch_id, conflict_type, record_type, record_no,
                    conflict_reason, old_status, new_status,
                    old_operator, new_operator, old_amount, new_amount)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, conflict_type, record_type, record_no,
                 conflict_reason, old_status, new_status,
                 old_operator, new_operator, old_amount, new_amount)
            )
            return cursor.lastrowid

    def get_batch_conflicts(self, batch_id: int = None, conflict_type: str = None) -> List[Dict]:
        with self._get_conn() as conn:
            sql = """SELECT bc.*, b.file_name, b.file_type, b.imported_at
                     FROM batch_conflicts bc
                     JOIN import_batches b ON bc.batch_id = b.id
                     WHERE 1=1"""
            params = []
            if batch_id:
                sql += " AND bc.batch_id = ?"
                params.append(batch_id)
            if conflict_type:
                sql += " AND bc.conflict_type = ?"
                params.append(conflict_type)
            sql += " ORDER BY bc.detected_at DESC"

            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def set_session_state(self, key: str, value: Any) -> None:
        import json
        value_str = json.dumps(value, ensure_ascii=False)
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO session_state (key, value)
                   VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = CURRENT_TIMESTAMP""",
                (key, value_str)
            )

    def get_session_state(self, key: str, default: Any = None) -> Any:
        import json
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM session_state WHERE key = ?",
                (key,)
            ).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                return row["value"]

    def clear_session_state(self, key: str = None) -> None:
        with self._get_conn() as conn:
            if key:
                conn.execute("DELETE FROM session_state WHERE key = ?", (key,))
            else:
                conn.execute("DELETE FROM session_state")

    def insert_batch_change_log(self, batch_id: int, change_type: str, record_type: str,
                                record_no: str, change_summary: str,
                                field_name: str = None, old_value: str = None,
                                new_value: str = None, before_summary: str = None,
                                after_summary: str = None, impact_type: str = None,
                                impact_details: str = None, impacted_match_ids: List[int] = None,
                                operator: str = None, processing_status: str = 'pending',
                                remark: str = None) -> int:
        import json
        impacted_ids_str = json.dumps(impacted_match_ids) if impacted_match_ids else None
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO batch_change_logs
                   (batch_id, change_type, record_type, record_no, field_name,
                    old_value, new_value, change_summary, before_summary, after_summary,
                    impact_type, impact_details, impacted_match_ids, operator,
                    processing_status, remark)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, change_type, record_type, record_no, field_name,
                 old_value, new_value, change_summary, before_summary, after_summary,
                 impact_type, impact_details, impacted_ids_str, operator,
                 processing_status, remark)
            )
            return cursor.lastrowid

    def get_batch_change_logs(self, batch_id: int = None, change_type: str = None,
                              impact_type: str = None, processing_status: str = None,
                              record_no: str = None) -> List[Dict]:
        import json
        with self._get_conn() as conn:
            sql = """SELECT cl.*, b.file_name, b.file_type, b.imported_at as batch_imported_at
                     FROM batch_change_logs cl
                     JOIN import_batches b ON cl.batch_id = b.id
                     WHERE 1=1"""
            params = []
            if batch_id:
                sql += " AND cl.batch_id = ?"
                params.append(batch_id)
            if change_type:
                sql += " AND cl.change_type = ?"
                params.append(change_type)
            if impact_type:
                sql += " AND cl.impact_type = ?"
                params.append(impact_type)
            if processing_status:
                sql += " AND cl.processing_status = ?"
                params.append(processing_status)
            if record_no:
                sql += " AND cl.record_no = ?"
                params.append(record_no)
            sql += " ORDER BY cl.detected_at DESC"

            rows = conn.execute(sql, tuple(params)).fetchall()
            result = []
            for row in rows:
                row_dict = dict(row)
                if row_dict.get("impacted_match_ids"):
                    try:
                        row_dict["impacted_match_ids"] = json.loads(row_dict["impacted_match_ids"])
                    except (json.JSONDecodeError, TypeError):
                        row_dict["impacted_match_ids"] = []
                result.append(row_dict)
            return result

    def update_change_log_status(self, log_id: int, processing_status: str,
                                 processed_by: str = None, remark: str = None) -> None:
        from datetime import datetime
        with self._get_conn() as conn:
            sql = """UPDATE batch_change_logs
                        SET processing_status = ?, processed_at = ?, processed_by = ?"""
            params = [processing_status, datetime.now().isoformat(), processed_by]
            if remark:
                sql += ", remark = COALESCE(remark, '') || ?"
                params.append(f" {remark}")
            sql += " WHERE id = ?"
            params.append(log_id)
            conn.execute(sql, tuple(params))

    def get_batch_summary(self, batch_id: int = None) -> List[Dict]:
        with self._get_conn() as conn:
            batches_sql = "SELECT * FROM import_batches"
            params = ()
            if batch_id:
                batches_sql += " WHERE id = ?"
                params = (batch_id,)
            batches_sql += " ORDER BY imported_at DESC"

            batch_rows = conn.execute(batches_sql, params).fetchall()
            if not batch_rows:
                return []

            result = []
            for b in batch_rows:
                batch = dict(b)
                batch_id_val = batch["id"]
                file_type = batch["file_type"]

                inv_stats = conn.execute(
                    """SELECT
                        COUNT(DISTINCT CASE WHEN match_status = 'matched' THEN id END) as matched_invoices,
                        COUNT(DISTINCT CASE WHEN match_status = 'unmatched' AND status = 'normal' THEN id END) as unmatched_invoices
                       FROM invoices WHERE batch_id = ?""",
                    (batch_id_val,)
                ).fetchone()

                pay_stats = conn.execute(
                    """SELECT
                        COUNT(DISTINCT CASE WHEN match_status = 'matched' THEN id END) as matched_payments,
                        COUNT(DISTINCT CASE WHEN match_status = 'unmatched' AND status = 'normal' THEN id END) as unmatched_payments
                       FROM payments WHERE batch_id = ?""",
                    (batch_id_val,)
                ).fetchone()

                match_stats = conn.execute(
                    """SELECT
                        COUNT(DISTINCT CASE WHEN m.status = 'pending' THEN m.id END) as pending_matches,
                        COUNT(DISTINCT CASE WHEN m.status = 'matched' THEN m.id END) as confirmed_matches,
                        COUNT(DISTINCT CASE WHEN m.status = 'exception' THEN m.id END) as exception_matches,
                        COUNT(DISTINCT CASE WHEN m.status = 'revoked' THEN m.id END) as revoked_matches
                       FROM matches m
                       JOIN invoices i ON m.invoice_id = i.id
                       JOIN payments p ON m.payment_id = p.id
                       WHERE i.batch_id = ? OR p.batch_id = ?""",
                    (batch_id_val, batch_id_val)
                ).fetchone()

                conflict_count = conn.execute(
                    "SELECT COUNT(*) FROM batch_conflicts WHERE batch_id = ?",
                    (batch_id_val,)
                ).fetchone()[0]

                summary = {
                    "batch_id": batch_id_val,
                    "file_type": file_type,
                    "file_name": batch["file_name"],
                    "total_rows": batch["total_rows"],
                    "success_rows": batch["success_rows"],
                    "failed_rows": batch["failed_rows"],
                    "operator": batch["operator"],
                    "imported_at": batch["imported_at"],
                    "matched_invoices": inv_stats["matched_invoices"] or 0,
                    "unmatched_invoices": inv_stats["unmatched_invoices"] or 0,
                    "matched_payments": pay_stats["matched_payments"] or 0,
                    "unmatched_payments": pay_stats["unmatched_payments"] or 0,
                    "pending_matches": match_stats["pending_matches"] or 0,
                    "confirmed_matches": match_stats["confirmed_matches"] or 0,
                    "exception_matches": match_stats["exception_matches"] or 0,
                    "revoked_matches": match_stats["revoked_matches"] or 0,
                    "conflict_count": conflict_count or 0,
                }
                result.append(summary)

            return result

    def get_matches_by_batch(self, batch_id: int, status: str = None,
                             operator: str = None) -> List[Dict]:
        with self._get_conn() as conn:
            sql = """
                SELECT DISTINCT m.*,
                       i.invoice_no, i.invoice_date, i.customer as inv_customer,
                       i.amount as inv_amount, i.batch_id as inv_batch_id,
                       p.payment_no, p.payment_date, p.customer as pay_customer,
                       p.amount as pay_amount, p.batch_id as pay_batch_id,
                       ib.file_name as inv_file, pb.file_name as pay_file
                FROM matches m
                JOIN invoices i ON m.invoice_id = i.id
                JOIN payments p ON m.payment_id = p.id
                LEFT JOIN import_batches ib ON i.batch_id = ib.id
                LEFT JOIN import_batches pb ON p.batch_id = pb.id
                WHERE (i.batch_id = ? OR p.batch_id = ?)
            """
            params = [batch_id, batch_id]
            if status:
                sql += " AND m.status = ?"
                params.append(status)
            if operator:
                sql += " AND m.operator = ?"
                params.append(operator)
            sql += " ORDER BY m.created_at DESC"

            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def insert_audit_log(self, action_type: str, action_category: str,
                         action_summary: str, batch_id: int = None,
                         record_type: str = None, record_no: str = None,
                         operator: str = None, action_details: str = None,
                         status: str = 'success', error_message: str = None) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO audit_logs
                   (action_type, action_category, action_summary, batch_id,
                    record_type, record_no, operator, action_details,
                    status, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (action_type, action_category, action_summary, batch_id,
                 record_type, record_no, operator, action_details,
                 status, error_message)
            )
            return cursor.lastrowid

    def get_audit_logs(self, batch_id: int = None, action_category: str = None,
                       action_type: str = None, operator: str = None,
                       limit: int = 200) -> List[Dict]:
        with self._get_conn() as conn:
            sql = "SELECT * FROM audit_logs WHERE 1=1"
            params = []
            if batch_id:
                sql += " AND batch_id = ?"
                params.append(batch_id)
            if action_category:
                sql += " AND action_category = ?"
                params.append(action_category)
            if action_type:
                sql += " AND action_type = ?"
                params.append(action_type)
            if operator:
                sql += " AND operator = ?"
                params.append(operator)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def insert_batch_change_log(self, batch_id: int, change_type: str, record_type: str,
                                record_no: str, change_summary: str,
                                field_name: str = None, old_value: str = None,
                                new_value: str = None, before_summary: str = None,
                                after_summary: str = None, impact_type: str = None,
                                impact_details: str = None, impacted_match_ids: List[int] = None,
                                operator: str = None, processing_status: str = 'pending',
                                remark: str = None, conflict_reason: str = None) -> int:
        import json
        impacted_ids_str = json.dumps(impacted_match_ids) if impacted_match_ids else None
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO batch_change_logs
                   (batch_id, change_type, record_type, record_no, field_name,
                    old_value, new_value, change_summary, before_summary, after_summary,
                    impact_type, impact_details, impacted_match_ids, operator,
                    processing_status, remark, conflict_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, change_type, record_type, record_no, field_name,
                 old_value, new_value, change_summary, before_summary, after_summary,
                 impact_type, impact_details, impacted_ids_str, operator,
                 processing_status, remark, conflict_reason)
            )
            return cursor.lastrowid

    def get_record_change_history(self, record_type: str, record_no: str) -> List[Dict]:
        import json
        with self._get_conn() as conn:
            sql = """SELECT cl.*, b.file_name, b.file_type, b.imported_at as batch_imported_at
                     FROM batch_change_logs cl
                     JOIN import_batches b ON cl.batch_id = b.id
                     WHERE cl.record_type = ? AND cl.record_no = ?
                     ORDER BY cl.detected_at ASC"""
            rows = conn.execute(sql, (record_type, record_no)).fetchall()

            result = []
            for row in rows:
                row_dict = dict(row)
                if row_dict.get("impacted_match_ids"):
                    try:
                        row_dict["impacted_match_ids"] = json.loads(row_dict["impacted_match_ids"])
                    except (json.JSONDecodeError, TypeError):
                        row_dict["impacted_match_ids"] = []
                result.append(row_dict)
            return result

    def get_record_revoked_status(self, record_type: str, record_no: str) -> Dict:
        result = {
            "has_been_revoked": False,
            "revoked_matches": [],
            "reimported_after_revoke": False,
        }
        with self._get_conn() as conn:
            if record_type == "invoice":
                match_rows = conn.execute(
                    """SELECT m.*, i.invoice_no as rec_no
                       FROM matches m
                       JOIN invoices i ON m.invoice_id = i.id
                       WHERE i.invoice_no = ? AND m.status = 'revoked'
                       ORDER BY m.confirmed_at DESC""",
                    (record_no,)
                ).fetchall()
            else:
                match_rows = conn.execute(
                    """SELECT m.*, p.payment_no as rec_no
                       FROM matches m
                       JOIN payments p ON m.payment_id = p.id
                       WHERE p.payment_no = ? AND m.status = 'revoked'
                       ORDER BY m.confirmed_at DESC""",
                    (record_no,)
                ).fetchall()

            if match_rows:
                result["has_been_revoked"] = True
                result["revoked_matches"] = [dict(r) for r in match_rows]

                last_revoked_at = None
                for r in result["revoked_matches"]:
                    if r.get("confirmed_at"):
                        if last_revoked_at is None or r["confirmed_at"] > last_revoked_at:
                            last_revoked_at = r["confirmed_at"]

                if last_revoked_at and result["revoked_matches"]:
                    if record_type == "invoice":
                        change_rows = conn.execute(
                            """SELECT detected_at FROM batch_change_logs cl
                               JOIN invoices i ON 1=1
                               WHERE cl.record_no = ? AND cl.record_type = 'invoice'
                                 AND cl.detected_at > ?
                               LIMIT 1""",
                            (record_no, last_revoked_at)
                        ).fetchall()
                    else:
                        change_rows = conn.execute(
                            """SELECT detected_at FROM batch_change_logs cl
                               WHERE cl.record_no = ? AND cl.record_type = 'payment'
                                 AND cl.detected_at > ?
                               LIMIT 1""",
                            (record_no, last_revoked_at)
                        ).fetchall()
                    if change_rows:
                        result["reimported_after_revoke"] = True

        return result

    def get_concurrent_field_conflicts(self, batch_id: int, record_type: str,
                                       record_no: str, field_name: str) -> List[Dict]:
        with self._get_conn() as conn:
            sql = """SELECT cl.*, b.file_name, b.operator as batch_operator
                     FROM batch_change_logs cl
                     JOIN import_batches b ON cl.batch_id = b.id
                     WHERE cl.record_type = ? AND cl.record_no = ?
                       AND cl.field_name = ? AND cl.batch_id <= ?
                     ORDER BY cl.detected_at ASC"""
            rows = conn.execute(sql, (record_type, record_no, field_name, batch_id)).fetchall()
            return [dict(r) for r in rows]

    def save_handover_package(self, package_id: str, config_hash: str,
                              package_data: Dict) -> None:
        data_str = json.dumps(package_data, ensure_ascii=False, default=str)
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO handover_packages (package_id, config_hash, status, operator, description, package_data, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(package_id) DO UPDATE SET
                   config_hash = excluded.config_hash,
                   status = excluded.status,
                   operator = excluded.operator,
                   description = excluded.description,
                   package_data = excluded.package_data,
                   updated_at = CURRENT_TIMESTAMP""",
                (package_id, config_hash,
                 package_data.get("status", "active"),
                 package_data.get("operator"),
                 package_data.get("description"),
                 data_str)
            )

    def get_handover_package(self, package_id: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM handover_packages WHERE package_id = ?",
                (package_id,)
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            if result.get("package_data"):
                try:
                    result["package_data"] = json.loads(result["package_data"])
                except (json.JSONDecodeError, TypeError):
                    pass
            return result

    def list_handover_packages(self, config_hash: str,
                               include_discarded: bool = False) -> List[Dict]:
        with self._get_conn() as conn:
            sql = "SELECT * FROM handover_packages WHERE config_hash = ?"
            params = [config_hash]
            if not include_discarded:
                sql += " AND status != 'discarded'"
            sql += " ORDER BY created_at DESC"
            rows = conn.execute(sql, tuple(params)).fetchall()
            result = []
            for row in rows:
                row_dict = dict(row)
                if row_dict.get("package_data"):
                    try:
                        row_dict["package_data"] = json.loads(row_dict["package_data"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append(row_dict)
            return result

    def delete_handover_package(self, package_id: str) -> None:
        with self._get_conn() as conn:
            conn.execute("DELETE FROM handover_events WHERE package_id = ?", (package_id,))
            conn.execute("DELETE FROM handover_undos WHERE package_id = ?", (package_id,))
            conn.execute("DELETE FROM handover_packages WHERE package_id = ?", (package_id,))

    def save_handover_undo(self, undo_id: str, package_id: str,
                           previous_state: Dict, operator: str) -> None:
        state_str = json.dumps(previous_state, ensure_ascii=False, default=str)
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO handover_undos (undo_id, package_id, previous_state, operator)
                   VALUES (?, ?, ?, ?)""",
                (undo_id, package_id, state_str, operator)
            )

    def get_handover_undo(self, undo_id: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM handover_undos WHERE undo_id = ?",
                (undo_id,)
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            if result.get("previous_state"):
                try:
                    result["previous_state"] = json.loads(result["previous_state"])
                except (json.JSONDecodeError, TypeError):
                    pass
            return result

    def list_handover_undos(self, package_id: str) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM handover_undos WHERE package_id = ? ORDER BY created_at DESC",
                (package_id,)
            ).fetchall()
            result = []
            for row in rows:
                row_dict = dict(row)
                if row_dict.get("previous_state"):
                    try:
                        row_dict["previous_state"] = json.loads(row_dict["previous_state"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append(row_dict)
            return result

    def log_handover_event(self, event_type: str, package_id: Optional[str],
                           operator: str, details: str) -> None:
        event_id = self._generate_handover_event_id()
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO audit_logs (action_type, action_category, operator, action_summary, action_details)
                   VALUES (?, 'handover', ?, ?, ?)""",
                (event_type, operator,
                 f"交接包 {package_id or '-'}: {event_type}",
                 details)
            )
            conn.execute(
                """INSERT INTO handover_events
                   (event_id, event_type, package_id, operator, status, result_summary, event_details)
                   VALUES (?, ?, ?, ?, 'success', ?, ?)""",
                (event_id, event_type, package_id, operator,
                 f"交接包 {package_id or '-'}: {event_type}",
                 details)
            )

    def _generate_handover_event_id(self) -> str:
        now = datetime.now()
        prefix = f"HE{now.strftime('%Y%m%d%H%M%S')}"
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM handover_events WHERE event_id LIKE ?",
                (prefix + "%",)
            ).fetchone()
            seq = row[0] + 1
            return f"{prefix}{seq:03d}"

    def insert_handover_event(self, event_type: str, package_id: str = None,
                              operator: str = None, status: str = "success",
                              result_summary: str = None, event_details: Dict = None,
                              error_message: str = None) -> str:
        event_id = self._generate_handover_event_id()
        details_str = json.dumps(event_details, ensure_ascii=False, default=str) if event_details else None
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO handover_events
                   (event_id, event_type, package_id, operator, status,
                    result_summary, event_details, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, event_type, package_id, operator, status,
                 result_summary, details_str, error_message)
            )
        return event_id

    def get_handover_event(self, event_id: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM handover_events WHERE event_id = ?",
                (event_id,)
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            if result.get("event_details"):
                try:
                    result["event_details"] = json.loads(result["event_details"])
                except (json.JSONDecodeError, TypeError):
                    pass
            return result

    def list_handover_events(self, package_id: str = None,
                             event_type: str = None,
                             limit: int = 100) -> List[Dict]:
        with self._get_conn() as conn:
            sql = "SELECT * FROM handover_events WHERE 1=1"
            params = []
            if package_id:
                sql += " AND package_id = ?"
                params.append(package_id)
            if event_type:
                sql += " AND event_type = ?"
                params.append(event_type)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, tuple(params)).fetchall()
            result = []
            for row in rows:
                row_dict = dict(row)
                if row_dict.get("event_details"):
                    try:
                        row_dict["event_details"] = json.loads(row_dict["event_details"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append(row_dict)
            return result
