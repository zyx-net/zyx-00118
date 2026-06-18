import sys, os, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from invoice_reconciler.core.config import Config
from invoice_reconciler.core.database import Database
from invoice_reconciler.core.importer import CSVImporter
from invoice_reconciler.core.change_tracker import ChangeTracker

test_dir = tempfile.mkdtemp()
try:
    db_path = os.path.join(test_dir, 'test.db')
    export_dir = os.path.join(test_dir, 'exports')
    os.makedirs(export_dir, exist_ok=True)

    config = Config(db_path=db_path, export_dir=export_dir, lock_timeout_seconds=3600,
                    admin_users=['admin'], enable_lock=True)
    db = Database(db_path)
    importer = CSVImporter(config, db)
    tracker = ChangeTracker(config, db)

    invoice_csv = os.path.join(test_dir, 'inv.csv')
    payment_csv = os.path.join(test_dir, 'pay.csv')
    with open(invoice_csv, 'w') as f:
        f.write('invoice_no,invoice_date,customer,amount,status\nINV001,2024-01-15,Test,1000.00,正常\nINV002,2024-01-16,Test2,2000.00,正常\n')
    with open(payment_csv, 'w') as f:
        f.write('payment_no,payment_date,customer,amount\nPAY001,2024-01-16,Test,1000.00\nPAY002,2024-01-17,Test2,2000.00\n')

    inv = importer.import_invoices(invoice_csv, 'test')
    pay = importer.import_payments(payment_csv, 'test')
    print('Invoice batch:', inv['batch_id'])
    print('Payment batch:', pay['batch_id'])

    updated_csv = os.path.join(test_dir, 'inv2.csv')
    with open(updated_csv, 'w') as f:
        f.write('invoice_no,invoice_date,customer,amount,status\nINV001,2024-01-15,Test,1000.00,正常\nINV002,2024-01-16,Test2,2000.00,作废\nINV003,2024-01-17,Test3,3000.00,正常\n')
    inv2 = importer.import_invoices(updated_csv, 'test')
    print('Second invoice batch:', inv2['batch_id'])
    print('Changes count from import:', inv2.get('change_count', 'N/A'))

    all_logs = db.get_change_logs(limit=100)
    print()
    print('Total change logs in DB:', len(all_logs))
    for log in all_logs:
        print(f'  Log #{log["id"]}: batch={log["batch_id"]}, type={log["change_type"]}, record={log["record_no"]}')

    for bid in [inv['batch_id'], pay['batch_id'], inv2['batch_id']]:
        result = tracker.get_filtered_changes(batch_id=bid, operator='test')
        print(f'Batch {bid} changes via tracker: {len(result["logs"])}')

    result_all = tracker.get_filtered_changes(operator='test')
    print()
    print('All batches changes via tracker:', len(result_all['logs']))

finally:
    shutil.rmtree(test_dir, ignore_errors=True)
