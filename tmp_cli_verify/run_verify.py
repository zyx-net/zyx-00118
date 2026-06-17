# -*- coding: utf-8 -*-
"""CLI 实际流程验证：未授权人工改配不落库，锁历史和操作留痕符合预期"""
import os
import sys
import subprocess
import sqlite3
import glob

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.join(BASE, "tmp_cli_verify")
os.makedirs(TEST_DIR, exist_ok=True)

ENV = os.environ.copy()
ENV["PYTHONPATH"] = BASE
ENV["INVOICE_RECONCILER_CONFIG"] = os.path.join(TEST_DIR, "config.yaml")
ENV["PYTHONIOENCODING"] = "utf-8"

# 准备配置
import yaml
with open(os.path.join(BASE, "invoice_reconciler/data/config.yaml"), "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
cfg["db_path"] = os.path.join(TEST_DIR, "reconciler.db").replace("/", "\\")
cfg["export_dir"] = os.path.join(TEST_DIR, "exports").replace("/", "\\")
with open(os.path.join(TEST_DIR, "config.yaml"), "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)

INV_CSV = os.path.join(BASE, "invoice_reconciler/data/sample_invoices.csv")
PAY_CSV = os.path.join(BASE, "invoice_reconciler/data/sample_payments.csv")
DB_FILE = cfg["db_path"]


def run(args, label):
    print("\n" + "=" * 60)
    print(f"  {label}")
    print(f">>> cli {' '.join(args)}")
    print("=" * 60)
    r = subprocess.run(
        [sys.executable, "-m", "invoice_reconciler.cli.main"] + args,
        env=ENV, capture_output=True, cwd=BASE
    )
    out = r.stdout.decode("utf-8", errors="replace")
    err = r.stderr.decode("utf-8", errors="replace")
    output = (out + err).strip()
    print(output if output else "(no output)")
    if r.returncode != 0:
        print(f"  [exit code: {r.returncode}]")
    return r, output


def check_db(step_label):
    """验证 DB 中的记录是否符合预期"""
    if not os.path.exists(DB_FILE):
        print(f"  [{step_label}] DB 不存在")
        return
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    print(f"\n  >>> [{step_label}] DB 检查")

    cur.execute(
        "SELECT id, match_no, status, operator, match_type FROM matches "
        "WHERE operator='bob' AND match_type='manual'"
    )
    bob_rows = [dict(r) for r in cur.fetchall()]
    print(f"    - bob 创建的 manual 匹配数: {len(bob_rows)} (预期: 0)")
    for m in bob_rows:
        print(f"      BAD {m}")

    cur.execute(
        "SELECT id, match_id, action, operator, old_owner, new_owner, reason "
        "FROM lock_history ORDER BY id"
    )
    lock_rows = [dict(r) for r in cur.fetchall()]
    print(f"    · 锁历史总数: {len(lock_rows)}")
    for h in lock_rows:
        print(f"      {h['action']:10s} op={h['operator']:10s} "
              f"{h['old_owner'] or '-':>8s} -> {h['new_owner'] or '-':8s} | {h['reason'] or ''}")

    cur.execute(
        "SELECT id, match_id, old_status, new_status, operator, remark "
        "FROM status_history ORDER BY id DESC LIMIT 12"
    )
    st_rows = [dict(r) for r in cur.fetchall()]
    print(f"    - 最近 12 条状态历史:")
    for h in reversed(st_rows):
        old_s = h["old_status"] or "-"
        new_s = h["new_status"] or "-"
        op = h["operator"] or "-"
        mk = h["match_id"] or 0
        rmk = (h["remark"] or "").strip()
        print(f"      match#{mk:2d} {old_s:10s} -> {new_s:10s} "
              f"op={op:10s} | {rmk}")
    conn.close()
    return bob_rows, lock_rows


# 1. 导入
run(["import", "invoices", INV_CSV, "--operator", "importer"], "Step 1: 导入发票")
run(["import", "payments", PAY_CSV, "--operator", "importer"], "Step 2: 导入收款")

# 2. 匹配
run(["match", "--operator", "matcher"], "Step 3: 自动匹配")

# 3. 从 DB 找一条 PENDING 状态的记录（选待确认记录，这样 inv/pay 都是 unmatched 状态，
#    manual_match 才不会被"发票已匹配"拦截）和未匹配的 inv/pay
conn = sqlite3.connect(DB_FILE)
cur = conn.cursor()
# 优先找 pending
cur.execute("SELECT id, invoice_id, payment_id FROM matches "
            "WHERE status = 'pending' ORDER BY id LIMIT 1")
row = cur.fetchone()
if not row:
    # 没有 pending 就找任意非 revoked 的记录，并手动改成 pending + inv/pay=unmatched
    cur.execute("SELECT id, invoice_id, payment_id FROM matches "
                "WHERE status != 'revoked' ORDER BY id LIMIT 1")
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE matches SET status='pending' WHERE id=?", (row[0],))
        cur.execute("UPDATE invoices SET match_status='unmatched' WHERE id=?", (row[1],))
        cur.execute("UPDATE payments SET match_status='unmatched' WHERE id=?", (row[2],))
        conn.commit()
match_id, inv_id, pay_id = row[0], row[1], row[2]
cur.execute("SELECT id FROM invoices WHERE match_status = 'unmatched' LIMIT 5")
unmatched_invs = [r[0] for r in cur.fetchall()]
cur.execute("SELECT id FROM payments WHERE match_status = 'unmatched' LIMIT 5")
unmatched_pays = [r[0] for r in cur.fetchall()]
conn.close()
print(f"\n>>> 选择: match_id={match_id}, inv_id={inv_id}, pay_id={pay_id}")
print(f">>> 未匹配发票: {unmatched_invs}, 未匹配收款: {unmatched_pays}")

other_inv = unmatched_invs[0] if unmatched_invs else inv_id
# 选一个和 target 的 pay_id 不同的未匹配收款（同一个 inv 换一个 pay）
other_pay = None
for p in unmatched_pays:
    if p != pay_id:
        other_pay = p
        break
if other_pay is None:
    other_pay = pay_id

# 4. alice 先锁定这条记录
run(["lock", "acquire", str(match_id), "--operator", "alice",
     "--reason", "alice 认领，与客户核对中"],
    "Step 4: alice 先锁定 match_id={}".format(match_id))

# 5. X bob 未持锁，用同 inv_id + other_pay 尝试人工改配（应该失败）
run(["confirm", "manual", str(inv_id), str(other_pay),
     "--operator", "bob", "--remark", "bob 绕过锁改配"],
    "Step 5: bob 未持锁人工改配（应当失败，报未获得锁权限）")

check_db("Step 5 后 (bob 改配被拒后)")

# 6. OK alice 持锁，同 inv_id 人工改配（应该成功）
run(["confirm", "manual", str(inv_id), str(other_pay),
     "--operator", "alice", "--remark", "alice 持锁后合法改配"],
    "Step 6: alice 持锁后改配（应当成功）")

check_db("Step 6 后 (alice 成功后)")

# 7. 从 DB 找到 alice 刚创建的 manual 匹配
conn = sqlite3.connect(DB_FILE)
cur = conn.cursor()
cur.execute(
    "SELECT id, match_no FROM matches WHERE operator='alice' "
    "AND match_type='manual' ORDER BY id DESC LIMIT 1"
)
row = cur.fetchone()
new_match_id = row[0] if row else match_id
new_match_no = row[1] if row else None
conn.close()
print(f"\n>>> alice 新创建的记录: match_id={new_match_id}, match_no={new_match_no}")

# 8. X charlie 无锁直接撤销 alice 的记录（应该失败）
run(["revoke", "by-no", new_match_no, "--operator", "charlie",
     "--remark", "charlie 无锁就想撤销"],
    "Step 8: charlie 无锁撤销（应当失败，报锁定错误）")

check_db("Step 8 后 (charlie 被拒后)")

# 9. OK alice 重新拿锁后撤销（应该成功）
run(["lock", "acquire", str(new_match_id), "--operator", "alice",
     "--reason", "alice 撤销前先拿锁"],
    "Step 9: alice 撤销前拿锁 (new_match_id={})".format(new_match_id))
run(["revoke", "match", str(new_match_id), "--operator", "alice",
     "--remark", "alice 持锁撤销自己的改配记录"],
    "Step 10: alice 持锁撤销（应当成功）")

check_db("Step 10 后 (最终状态)")

# 11. 运行完整测试套件
print("\n" + "=" * 60)
print("  Step 11: 运行全部 67 个测试")
print("=" * 60)
ret = os.system(f'cd /d "{BASE}" && python -m unittest discover -s tests')
if ret != 0:
    ret = os.system(f'cd "{BASE}" && python -m unittest discover -s tests')

# 最终清单
print("\n" + "=" * 60)
print("  完整 CLI 流程验证完成")
print("=" * 60)
print("预期结果清单 (请对照上方输出检查):")
print("  OK Step 5  bob 人工改配 -> 报 '未获得锁权限' [exit 1]")
print("  OK Step 5  后 DB 中 bob 的 manual 匹配数 = 0")
print("  OK Step 6  alice 持锁改配 -> OK")
print("  OK Step 8  charlie 无锁撤销 -> 报锁定错误")
print("  OK Step 10 alice 持锁撤销 -> OK")
print("  OK 锁历史中有 alice 的多次 lock 记录")
print("  OK 状态历史中有 matched -> revoked 变更")
print("  OK Step 11 全部 67 个测试通过 (OK)")
