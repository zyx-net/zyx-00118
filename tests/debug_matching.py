import os
import tempfile
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from invoice_reconciler.core.config import Config
from invoice_reconciler.core.database import Database
from invoice_reconciler.core.workflow import WorkflowManager
from invoice_reconciler.core.importer import CSVImporter
from invoice_reconciler.core.matcher import MatchEngine

SAMPLE_INVOICES_CSV = """invoice_no,invoice_date,customer,amount,status
INV001,2024-01-15,北京科技有限公司,1000.00,正常
INV002,2024-01-16,上海贸易公司,2500.50,正常
INV003,2024-01-17,广州电子厂,3000.00,正常
INV004,2024-01-18,深圳软件公司,1500.00,正常
INV005,2024-01-19,杭州电商平台,800.00,正常
"""

SAMPLE_PAYMENTS_CSV = """payment_no,payment_date,customer,amount
PAY001,2024-01-16,北京科技有限公司,1000.00
PAY002,2024-01-17,上海贸易公司,2500.50
PAY003,2024-01-18,广州电子厂,3000.00
PAY004,2024-01-20,深圳软件公司,1499.99
PAY005,2024-01-21,杭州电商平台,800.00
"""

test_dir = tempfile.mkdtemp()
db_path = os.path.join(test_dir, "test.db")
export_dir = os.path.join(test_dir, "exports")

config = Config(
    db_path=db_path,
    export_dir=export_dir,
    lock_timeout_seconds=3600,
    admin_users=["admin"],
    enable_lock=True,
)

invoice_csv = os.path.join(test_dir, "invoices.csv")
payment_csv = os.path.join(test_dir, "payments.csv")

with open(invoice_csv, "w", encoding="utf-8") as f:
    f.write(SAMPLE_INVOICES_CSV)
with open(payment_csv, "w", encoding="utf-8") as f:
    f.write(SAMPLE_PAYMENTS_CSV)

db = Database(db_path)
workflow = WorkflowManager(config, db)
importer = CSVImporter(config, db)
matcher = MatchEngine(config, db, workflow)

inv_result = importer.import_invoices(invoice_csv, "init_user")
pay_result = importer.import_payments(payment_csv, "init_user")

print(f"Invoice batch: {inv_result}")
print(f"Payment batch: {pay_result}")

invoices = db.get_unmatched_invoices()
payments = db.get_unmatched_payments()

print(f"\nUnmatched invoices: {len(invoices)}")
for inv in invoices:
    print(f"  {inv['invoice_no']}: {inv['customer']}, {inv['amount']}, status={inv['status']}, match_status={inv['match_status']}")

print(f"\nUnmatched payments: {len(payments)}")
for pay in payments:
    print(f"  {pay['payment_no']}: {pay['customer']}, {pay['amount']}, status={pay['status']}, match_status={pay['match_status']}")

match_result = matcher.run_auto_matching("init_user")
print(f"\nAuto-match result: {match_result}")

all_matches = db.get_matches_by_status()
print(f"\nAll matches: {len(all_matches)}")
for m in all_matches:
    print(f"  {m['id']}: status={m['status']}, type={m['match_type']}")

import shutil
shutil.rmtree(test_dir, ignore_errors=True)
