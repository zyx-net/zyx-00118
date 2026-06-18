# -*- coding: utf-8 -*-
"""CLI 实际流程验证：未授权人工改配不落库，锁历史和操作留痕符合预期

⚠️  严格模式：任何一步不符合预期立即以非0退出码终止，
   禁止"命令写错/命中0条/断言失败后还继续输出'已通过'"。
"""
import os
import sys
import subprocess
import sqlite3
import traceback

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.join(BASE, "tmp_cli_verify")
os.makedirs(TEST_DIR, exist_ok=True)

ENV = os.environ.copy()
ENV["PYTHONPATH"] = BASE
ENV["INVOICE_RECONCILER_CONFIG"] = os.path.join(TEST_DIR, "config.yaml")
ENV["PYTHONIOENCODING"] = "utf-8"

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

FAILURES = []


def fail(message, critical=True):
    """记录失败，关键错误直接终止"""
    FAILURES.append(message)
    print(flush=True)
    print("=" * 60, flush=True)
    print(f"  ❌ 验证失败: {message}", flush=True)
    print("=" * 60, flush=True)
    if critical:
        print("\n所有失败:", flush=True)
        for i, f in enumerate(FAILURES, 1):
            print(f"  {i}. {f}", flush=True)
        sys.exit(1)


def assert_success(result, label, expected_code=0):
    """断言命令执行成功（退出码为 expected_code）"""
    r, output = result
    if r.returncode != expected_code:
        fail(
            f"{label}: 预期退出码 {expected_code}，实际 {r.returncode}\n"
            f"命令: python -m invoice_reconciler.cli.main ...\n"
            f"输出:\n{output}"
        )
    return output


def assert_fail(result, label):
    """断言命令执行失败（退出码 != 0）"""
    r, output = result
    if r.returncode == 0:
        fail(
            f"{label}: 预期失败（退出码 != 0），实际成功（退出码 0）\n"
            f"输出:\n{output}"
        )
    return output


def assert_in_output(output, expected, label):
    """断言输出中包含预期内容"""
    if expected not in output:
        fail(
            f"{label}: 输出中未找到预期内容 '{expected}'\n"
            f"实际输出:\n{output}"
        )


def assert_not_in_output(output, not_expected, label):
    """断言输出中不包含指定内容"""
    if not_expected in output:
        fail(
            f"{label}: 输出中不应包含 '{not_expected}'，但实际包含\n"
            f"输出:\n{output}"
        )


def assert_count_greater(count, min_expected, label):
    """断言计数大于最小值，命中0条必须失败"""
    if count <= min_expected:
        fail(
            f"{label}: 预期计数 > {min_expected}，实际 {count}\n"
            f"命中 0 条或过少，验证脚本已收紧，禁止输出'已通过'！"
        )


def assert_equal(actual, expected, label):
    """断言相等"""
    if actual != expected:
        fail(
            f"{label}: 预期 {expected}，实际 {actual}"
        )


def run(args, label, expect_success=True, expect_fail=False):
    """运行 CLI 命令并立即检查退出码"""
    print("\n" + "=" * 60, flush=True)
    print(f"  {label}", flush=True)
    print(f">>> cli {' '.join(args)}", flush=True)
    print("=" * 60, flush=True)
    r = subprocess.run(
        [sys.executable, "-m", "invoice_reconciler.cli.main"] + args,
        env=ENV, capture_output=True, cwd=BASE
    )
    out = r.stdout.decode("utf-8", errors="replace")
    err = r.stderr.decode("utf-8", errors="replace")
    output = (out + err).strip()
    print(output if output else "(no output)", flush=True)
    if r.returncode != 0:
        print(f"  [exit code: {r.returncode}]", flush=True)

    if expect_fail:
        assert_fail((r, output), label)
    elif expect_success:
        assert_success((r, output), label)

    return r, output


def check_db(step_label, expected_bob_manual_count=0):
    """验证 DB 中的记录是否符合预期，不符合立即终止"""
    if not os.path.exists(DB_FILE):
        fail(f"[{step_label}] DB 文件不存在: {DB_FILE}")
        return

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    print(f"\n  >>> [{step_label}] DB 检查", flush=True)

    cur.execute(
        "SELECT id, match_no, status, operator, match_type FROM matches "
        "WHERE operator='bob' AND match_type='manual'"
    )
    bob_rows = [dict(r) for r in cur.fetchall()]
    print(f"    - bob 创建的 manual 匹配数: {len(bob_rows)} (预期: {expected_bob_manual_count})", flush=True)
    for m in bob_rows:
        print(f"      BAD {m}", flush=True)

    assert_equal(len(bob_rows), expected_bob_manual_count,
                 f"[{step_label}] bob 的 manual 匹配数")

    cur.execute(
        "SELECT id, match_id, action, operator, old_owner, new_owner, reason "
        "FROM lock_history ORDER BY id"
    )
    lock_rows = [dict(r) for r in cur.fetchall()]
    print(f"    · 锁历史总数: {len(lock_rows)}", flush=True)
    for h in lock_rows:
        print(f"      {h['action']:10s} op={h['operator']:10s} "
              f"{h['old_owner'] or '-':>8s} -> {h['new_owner'] or '-':8s} | {h['reason'] or ''}", flush=True)

    assert_count_greater(len(lock_rows), 0, f"[{step_label}] 锁历史记录数")

    alice_lock_count = sum(1 for h in lock_rows if h["operator"] == "alice")
    assert_count_greater(alice_lock_count, 1, f"[{step_label}] alice 的锁操作记录数")

    cur.execute(
        "SELECT id, match_id, old_status, new_status, operator, remark "
        "FROM status_history ORDER BY id DESC LIMIT 12"
    )
    st_rows = [dict(r) for r in cur.fetchall()]
    print(f"    - 最近 {len(st_rows)} 条状态历史:", flush=True)
    for h in reversed(st_rows):
        old_s = h["old_status"] or "-"
        new_s = h["new_status"] or "-"
        op = h["operator"] or "-"
        mk = h["match_id"] or 0
        rmk = (h["remark"] or "").strip()
        print(f"      match#{mk:2d} {old_s:10s} -> {new_s:10s} "
              f"op={op:10s} | {rmk}", flush=True)

    conn.close()
    return bob_rows, lock_rows


def main():
    try:
        if os.path.exists(DB_FILE):
            os.remove(DB_FILE)
            print(f"  已清理旧数据库: {DB_FILE}", flush=True)

        _, out = run(["import", "invoices", INV_CSV, "--operator", "importer"],
                     "Step 1: 导入发票")
        assert_in_output(out, "批次ID", "Step 1: 导入发票应输出批次ID")
        assert_in_output(out, "成功", "Step 1: 导入发票应成功")

        _, out = run(["import", "payments", PAY_CSV, "--operator", "importer"],
                     "Step 2: 导入收款")
        assert_in_output(out, "批次ID", "Step 2: 导入收款应输出批次ID")

        _, out = run(["match", "--operator", "matcher"], "Step 3: 自动匹配")
        assert_in_output(out, "自动精确匹配", "Step 3: 匹配应输出匹配结果")

        _, out = run(["status"], "Step 3b: 验证匹配结果")
        assert_in_output(out, "发票总数", "Step 3b: status 应输出发票总数")
        assert_in_output(out, "已匹配", "Step 3b: status 应输出已匹配")

        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM matches")
        match_count = cur.fetchone()[0]
        assert_count_greater(match_count, 0, "Step 3b: 匹配记录数")

        cur.execute("SELECT id, invoice_id, payment_id FROM matches "
                    "WHERE status = 'pending' ORDER BY id LIMIT 1")
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT id, invoice_id, payment_id FROM matches "
                        "WHERE status != 'revoked' ORDER BY id LIMIT 1")
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE matches SET status='pending' WHERE id=?", (row[0],))
                cur.execute("UPDATE invoices SET match_status='unmatched' WHERE id=?", (row[1],))
                cur.execute("UPDATE payments SET match_status='unmatched' WHERE id=?", (row[2],))
                conn.commit()
        assert row is not None, "Step 3c: 无法找到任何匹配记录，DB 为空"

        match_id, inv_id, pay_id = row[0], row[1], row[2]
        cur.execute("SELECT id FROM invoices WHERE match_status = 'unmatched' LIMIT 5")
        unmatched_invs = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT id FROM payments WHERE match_status = 'unmatched' LIMIT 5")
        unmatched_pays = [r[0] for r in cur.fetchall()]
        conn.close()

        print(f"\n>>> 选择: match_id={match_id}, inv_id={inv_id}, pay_id={pay_id}", flush=True)
        print(f">>> 未匹配发票: {unmatched_invs}, 未匹配收款: {unmatched_pays}", flush=True)

        assert_count_greater(len(unmatched_invs), 0, "未匹配发票数量")
        assert_count_greater(len(unmatched_pays), 0, "未匹配收款数量")

        other_inv = unmatched_invs[0] if unmatched_invs else inv_id
        other_pay = None
        for p in unmatched_pays:
            if p != pay_id:
                other_pay = p
                break
        if other_pay is None:
            other_pay = pay_id
        print(f">>> 改配目标: inv_id={other_inv}, pay_id={other_pay}", flush=True)

        _, out = run(["lock", "acquire", str(match_id), "--operator", "alice",
                      "--reason", "alice 认领，与客户核对中"],
                     f"Step 4: alice 先锁定 match_id={match_id}")
        assert_in_output(out, "OK", "Step 4: 锁定应成功")

        _, out = run(["confirm", "manual", str(other_inv), str(other_pay),
                      "--operator", "bob", "--remark", "bob 绕过锁改配"],
                     "Step 5: bob 未持锁人工改配", expect_fail=True)
        assert_in_output(out, "锁定", "Step 5: 应报锁定相关错误")

        check_db("Step 5 后 (bob 改配被拒后)", expected_bob_manual_count=0)

        _, out = run(["confirm", "manual", str(other_inv), str(other_pay),
                      "--operator", "alice", "--remark", "alice 持锁后合法改配"],
                     "Step 6: alice 持锁后改配")
        assert_in_output(out, "OK", "Step 6: alice 持锁改配应成功")
        assert_in_output(out, "匹配编号", "Step 6: 应输出匹配编号")

        check_db("Step 6 后 (alice 成功后)")

        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, match_no FROM matches WHERE operator='alice' "
            "AND match_type='manual' ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        assert row is not None, "Step 7: alice 创建的 manual 匹配不存在"
        new_match_id = row[0]
        new_match_no = row[1]
        conn.close()
        print(f"\n>>> alice 新创建的记录: match_id={new_match_id}, match_no={new_match_no}", flush=True)
        assert new_match_no is not None, "Step 7: 匹配编号不应为 None"

        _, out = run(["revoke", "by-no", new_match_no, "--operator", "charlie",
                      "--remark", "charlie 无锁就想撤销"],
                     "Step 8: charlie 无锁撤销", expect_fail=True)
        assert_in_output(out, "锁定", "Step 8: 应报锁定错误")

        check_db("Step 8 后 (charlie 被拒后)")

        _, out = run(["lock", "acquire", str(new_match_id), "--operator", "alice",
                      "--reason", "alice 撤销前先拿锁"],
                     f"Step 9: alice 撤销前拿锁 (new_match_id={new_match_id})")
        assert_in_output(out, "OK", "Step 9: 拿锁应成功")

        _, out = run(["revoke", "match", str(new_match_id), "--operator", "alice",
                      "--remark", "alice 持锁撤销自己的改配记录"],
                     "Step 10: alice 持锁撤销")
        assert_in_output(out, "OK", "Step 10: 撤销应成功")
        assert_in_output(out, "匹配编号", "Step 10: 应输出匹配编号")

        bob_rows, lock_rows = check_db("Step 10 后 (最终状态)")

        has_matched_to_revoked = any(
            h["old_status"] == "matched" and h["new_status"] == "revoked"
            for h in lock_rows if h.get("old_status") and h.get("new_status")
        )
        conn_tmp = sqlite3.connect(DB_FILE)
        cur_tmp = conn_tmp.cursor()
        cur_tmp.execute("SELECT old_status, new_status FROM status_history")
        sh_rows = cur_tmp.fetchall()
        conn_tmp.close()
        has_matched_to_revoked = has_matched_to_revoked or any(
            old == "matched" and new == "revoked" for old, new in sh_rows
        )
        assert has_matched_to_revoked, "Step 10: 状态历史中应有 matched -> revoked 变更"

        print("\n" + "=" * 60, flush=True)
        print("  Step 11: 运行全部测试套件", flush=True)
        print("=" * 60, flush=True)
        test_cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
        test_r = subprocess.run(test_cmd, env=ENV, cwd=BASE, capture_output=True)
        test_out = test_r.stdout.decode("utf-8", errors="replace")
        test_err = test_r.stderr.decode("utf-8", errors="replace")
        test_output = (test_out + test_err).strip()
        print(test_output, flush=True)

        if test_r.returncode != 0:
            fail(
                f"Step 11: 测试套件运行失败，退出码 {test_r.returncode}\n"
                f"请检查上方测试输出，修复后再运行。\n"
                f"⚠️  禁止测试失败后继续输出'已通过'！"
            )

        assert_in_output(test_output, "OK", "Step 11: 测试输出中应包含 OK")
        assert_not_in_output(test_output, "FAILED", "Step 11: 测试输出中不应包含 FAILED")
        assert_not_in_output(test_output, "ERROR", "Step 11: 测试输出中不应包含 ERROR")

        run_count = 0
        for line in test_output.split("\n"):
            if line.startswith("Ran") and "test" in line:
                try:
                    run_count = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
                break
        assert_count_greater(run_count, 0, "Step 11: 运行的测试数量")

        print("\n" + "=" * 60, flush=True)
        print("  ✅ 完整 CLI 流程验证全部通过", flush=True)
        print("=" * 60, flush=True)
        print("验证结果清单:", flush=True)
        print("  ✅ Step 5  bob 人工改配 -> 报锁定错误 [exit 1]", flush=True)
        print("  ✅ Step 5  后 DB 中 bob 的 manual 匹配数 = 0", flush=True)
        print("  ✅ Step 6  alice 持锁改配 -> OK", flush=True)
        print("  ✅ Step 8  charlie 无锁撤销 -> 报锁定错误 [exit 1]", flush=True)
        print("  ✅ Step 10 alice 持锁撤销 -> OK", flush=True)
        print("  ✅ 锁历史中有 alice 的多次 lock 记录", flush=True)
        print("  ✅ 状态历史中有 matched -> revoked 变更", flush=True)
        print(f"  ✅ Step 11 全部 {run_count} 个测试通过 (OK)", flush=True)
        print(f"\n总计 {run_count} 个测试，0 个失败，0 个错误", flush=True)

        sys.exit(0)

    except Exception as e:
        fail(
            f"验证脚本异常终止: {e}\n"
            f"堆栈:\n{traceback.format_exc()}\n"
            f"⚠️  验证脚本已收紧，异常即失败！"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
