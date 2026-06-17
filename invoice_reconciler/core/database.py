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
                    FOREIGN KEY (snapshot_id) REFERENCES review_snapshots(id)
                );

                CREATE INDEX IF NOT EXISTS idx_snapshot_items_snapshot ON snapshot_items(snapshot_id);
                CREATE INDEX IF NOT EXISTS idx_snapshot_items_match ON snapshot_items(match_no);
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

                conn.execute(
                    """INSERT INTO snapshot_items
                       (snapshot_id, match_id, match_no, match_type, match_score,
                        match_evidence, status, invoice_id, invoice_no, invoice_date,
                        inv_customer, inv_amount, inv_batch_id,
                        payment_id, payment_no, payment_date, pay_customer,
                        pay_amount, pay_batch_id, operator, operator_remark,
                        confirmed_at, created_at, status_history, candidate_payments)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (snapshot_id, m["id"], m["match_no"], m["match_type"], m["match_score"],
                     m["match_evidence"], m["status"], m["invoice_id"], m["invoice_no"],
                     m["invoice_date"], m["inv_customer"], m["inv_amount"], m.get("batch_id"),
                     m["payment_id"], m["payment_no"], m["payment_date"], m["pay_customer"],
                     m["pay_amount"], None, m["operator"], m["operator_remark"],
                     m["confirmed_at"], m["created_at"], history_json, candidates_json)
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
