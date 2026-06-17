# 发票与收款核对 CLI 工具

离线发票与收款核对工具，支持配置管理、两类 CSV 导入、自动匹配、人工确认、撤销、断点恢复和差异报告。

## 功能特性

- **配置管理**：金额容差、日期窗口、必填列、导出格式
- **两类 CSV 导入**：发票台账、收款流水，支持去重和行号追踪
- **自动匹配**：精确匹配、模糊匹配、多候选匹配
- **人工确认**：确认/拒绝匹配，保存操作者、备注、时间戳
- **撤销功能**：按 ID/编号撤销，批量撤销，防止空撤销
- **断点恢复**：基于 SQLite 的持久化存储，程序退出后可继续
- **差异报告**：含稳定编号、匹配证据、状态变化、人工备注
- **去重机制**：重复导入同一文件不会产生重复记录
- **复核快照**：按批次生成复核快照，带稳定编号，支持多次导出不改号
- **回放校验**：对比当前状态与历史快照，检测差异
- **冲突检测**：识别同一发票被不同操作者重复处理的冲突
- **多格式导出**：支持 xlsx、csv、json 三种格式导出

## 安装

```bash
pip install -r requirements.txt
pip install -e .
```

或直接使用模块方式运行：
```bash
pip install -r requirements.txt
```

## 快速开始 - 完整命令链

### 1. 查看/修改配置

```bash
# 查看当前配置
python -m invoice_reconciler.cli.main config show

# 修改配置（可选）
python -m invoice_reconciler.cli.main config set --amount-tolerance 0.01 --date-window 30 --export-format xlsx
```

### 2. 预览 CSV 文件

```bash
# 预览发票
python -m invoice_reconciler.cli.main import invoices invoice_reconciler/data/sample_invoices.csv --preview

# 预览收款
python -m invoice_reconciler.cli.main import payments invoice_reconciler/data/sample_payments.csv --preview
```

### 3. 导入数据

```bash
# 导入发票台账
python -m invoice_reconciler.cli.main import invoices invoice_reconciler/data/sample_invoices.csv --operator 张三

# 导入收款流水
python -m invoice_reconciler.cli.main import payments invoice_reconciler/data/sample_payments.csv --operator 张三
```

### 4. 查看导入批次和错误

```bash
# 查看导入批次
python -m invoice_reconciler.cli.main import batches

# 查看导入错误
python -m invoice_reconciler.cli.main import errors
```

### 5. 运行自动匹配

```bash
python -m invoice_reconciler.cli.main match --operator 张三
```

### 6. 查看匹配状态

```bash
# 查看整体状态
python -m invoice_reconciler.cli.main status

# 查看待确认匹配
python -m invoice_reconciler.cli.main confirm list --status pending

# 查看多候选匹配
python -m invoice_reconciler.cli.main confirm candidates
```

### 7. 人工确认匹配

```bash
# 查看匹配详情
python -m invoice_reconciler.cli.main confirm show 1

# 确认匹配
python -m invoice_reconciler.cli.main confirm approve 2 --operator 张三 --remark "核对无误"

# 多候选时选择特定收款
python -m invoice_reconciler.cli.main confirm approve 3 --operator 张三 --select-payment 6 --remark "选择第二笔收款"

# 拒绝匹配
python -m invoice_reconciler.cli.main confirm reject 4 --operator 张三 --remark "客户名称不符，需核查"

# 人工强制匹配（指定发票ID和收款ID）
python -m invoice_reconciler.cli.main confirm manual 6 13 --operator 张三 --remark "手工匹配"
```

### 8. 撤销匹配

```bash
# 查看可撤销的匹配
python -m invoice_reconciler.cli.main revoke list

# 按ID撤销
python -m invoice_reconciler.cli.main revoke match 1 --operator 张三 --remark "匹配错误，需重新核对"

# 按匹配编号撤销
python -m invoice_reconciler.cli.main revoke by-no M202606180001 --operator 张三 --remark "操作失误"

# 查看状态历史
python -m invoice_reconciler.cli.main revoke history --match-id 1
```

### 9. 导出报告

```bash
# 导出完整报告（默认格式）
python -m invoice_reconciler.cli.main export full --operator 张三

# 导出完整报告（指定 JSON 格式）
python -m invoice_reconciler.cli.main export full --operator 张三 --format json

# 导出完整报告（指定 CSV 格式）
python -m invoice_reconciler.cli.main export full --operator 张三 --format csv

# 导出差异报告
python -m invoice_reconciler.cli.main export diff --operator 张三 --format json
```

### 10. 复核快照管理

```bash
# 创建复核快照
python -m invoice_reconciler.cli.main review snapshot create --operator 张三 --description "第一轮核对完成"

# 列出所有快照
python -m invoice_reconciler.cli.main review snapshot list

# 查看快照详情
python -m invoice_reconciler.cli.main review snapshot show --snapshot-no R202606180001

# 导出快照（JSON 格式）
python -m invoice_reconciler.cli.main export snapshot --snapshot-no R202606180001 --operator 张三 --format json

# 导出快照（CSV 格式）
python -m invoice_reconciler.cli.main export snapshot --snapshot-no R202606180001 --operator 张三 --format csv
```

### 11. 回放校验

```bash
# 对比当前状态与历史快照
python -m invoice_reconciler.cli.main review replay --snapshot-no R202606180001

# 查看校验结果，确认状态是否一致
```

### 12. 冲突检测

```bash
# 检测所有冲突
python -m invoice_reconciler.cli.main review conflicts

# 检测指定发票的冲突
python -m invoice_reconciler.cli.main review conflicts --invoice-no INV001
```

### 13. 查看未匹配项

```bash
# 查看所有未匹配项
python -m invoice_reconciler.cli.main list-unmatched

# 仅查看未匹配发票
python -m invoice_reconciler.cli.main list-unmatched --type invoices

# 仅查看未匹配收款
python -m invoice_reconciler.cli.main list-unmatched --type payments
```

### 14. 断点恢复 - 重复导入测试

```bash
# 再次导入同一文件（应提示跳过）
python -m invoice_reconciler.cli.main import invoices invoice_reconciler/data/sample_invoices.csv --operator 李四

# 再次运行匹配（应从上次状态继续）
python -m invoice_reconciler.cli.main match --operator 李四
```

## 配置说明

配置文件默认路径：`invoice_reconciler/data/config.yaml`

```yaml
amount_tolerance: 0.01          # 金额容差（元），小于等于此值视为匹配
date_window_days: 30            # 日期窗口（天），在此范围内视为可能匹配
invoice_required_columns:       # 发票必填列
  - invoice_no
  - invoice_date
  - customer
  - amount
  - status
payment_required_columns:       # 收款必填列
  - payment_no
  - payment_date
  - customer
  - amount
export_format: xlsx             # 导出格式：xlsx、csv 或 json
db_path: invoice_reconciler/data/reconciler.db  # 数据库路径
export_dir: invoice_reconciler/exports          # 导出目录
```

## CSV 文件格式

### 发票台账 (sample_invoices.csv)

| 列名 | 说明 | 必填 |
|------|------|------|
| invoice_no | 发票编号 | 是 |
| invoice_date | 发票日期 | 是 |
| customer | 客户名称 | 是 |
| amount | 发票金额 | 是 |
| status | 发票状态 | 是 |

### 收款流水 (sample_payments.csv)

| 列名 | 说明 | 必填 |
|------|------|------|
| payment_no | 收款编号 | 是 |
| payment_date | 收款日期 | 是 |
| customer | 客户名称 | 是 |
| amount | 收款金额 | 是 |

支持的日期格式：
- YYYY-MM-DD
- YYYY/MM/DD
- YYYYMMDD
- YYYY-MM-DD HH:MM:SS
- YYYY/MM/DD HH:MM:SS

## 匹配规则

### 评分机制（总分100分）

| 条件 | 得分 |
|------|------|
| 客户完全匹配 | 50分 |
| 客户模糊匹配（包含关系） | 30分 |
| 金额精确匹配（差异≤容差） | 40分 |
| 金额接近（差异≤1%） | 25分 |
| 日期在窗口内（0天） | 10分 |
| 日期在窗口内（每天减1分，最低0分） | 10~0分 |

### 匹配类型

1. **自动精确匹配**（得分≥97分）：系统自动确认
2. **自动模糊匹配**（得分≥70分）：需人工确认
3. **多候选匹配**（同一发票对应≥2笔候选收款）：需人工选择
4. **人工匹配**：用户手动指定的匹配

## 状态说明

| 状态 | 说明 |
|------|------|
| unmatched | 未匹配 |
| pending | 待人工确认 |
| matched | 已确认匹配 |
| exception | 异常（被拒绝的匹配） |
| revoked | 已撤销 |

## 报告说明

### 完整报告（Excel 多 Sheet）

1. **概览**：统计数据、配置参数
2. **已匹配**：所有已确认的匹配，含匹配证据、状态历史
3. **待确认**：等待人工确认的匹配
4. **异常**：被拒绝的匹配
5. **未匹配发票**：无法匹配的发票
6. **未匹配收款**：无法匹配的收款
7. **已撤销**：已撤销的匹配记录
8. **导入错误**：数据验证失败的记录
9. **状态历史**：所有状态变更记录

### 差异报告

重点展示未匹配项和异常项，包含：
- 差异金额统计
- 未匹配发票清单
- 未匹配收款清单
- 异常匹配清单
- 导入错误清单

## 项目结构

```
invoice_reconciler/
├── __init__.py
├── cli/
│   ├── __init__.py
│   └── main.py              # CLI 主入口
├── core/
│   ├── __init__.py
│   ├── config.py            # 配置管理
│   ├── database.py          # 数据库操作
│   ├── importer.py          # CSV 导入
│   ├── matcher.py           # 匹配引擎
│   ├── revoker.py           # 撤销操作
│   ├── exporter.py          # 报告导出
│   └── reviewer.py          # 复核快照与回放校验
├── data/
│   ├── config.yaml          # 配置文件
│   ├── sample_invoices.csv  # 示例发票数据
│   └── sample_payments.csv  # 示例收款数据
└── exports/                 # 报告导出目录
```

## 常见问题

### Q: 重新导入同一文件会怎样？
A: 系统通过文件哈希检测，重复导入会被自动跳过，不会产生重复记录。

### Q: 程序退出后再运行，数据会丢失吗？
A: 不会。所有数据保存在 SQLite 数据库中，可断点继续操作。

### Q: 可以撤销已撤销的匹配吗？
A: 不可以。撤销操作是单向的，已撤销的匹配不能再次撤销。如需重新匹配，可使用人工匹配功能。

### Q: 如何修改已确认的匹配？
A: 先撤销该匹配，然后重新进行匹配或人工匹配。

### Q: 支持哪些导出格式？
A: 支持 xlsx（Excel）、csv 和 json 三种格式，可通过配置或命令行参数指定。

### Q: 什么是复核快照？
A: 复核快照是某一时刻所有匹配状态的完整存档，带稳定编号。同一快照可多次导出，编号始终不变。

### Q: 回放校验有什么用？
A: 回放校验可以对比当前数据状态与历史快照，检测是否有状态变更、新增或缺失的记录，用于审计和对账。

### Q: 什么是冲突检测？
A: 冲突检测用于识别同一张发票被不同操作者重复处理的情况，防止重复核销。

### Q: 程序重启后快照和历史会丢失吗？
A: 不会。所有快照、状态历史和匹配数据都保存在 SQLite 数据库中，程序重启后可以继续操作。

## 从导入到回放校验的完整命令链

以下是一个完整的对账复核流程示例：

```bash
# 1. 导入数据
python -m invoice_reconciler.cli.main import invoices invoice_reconciler/data/sample_invoices.csv --operator 张三
python -m invoice_reconciler.cli.main import payments invoice_reconciler/data/sample_payments.csv --operator 张三

# 2. 自动匹配
python -m invoice_reconciler.cli.main match --operator 张三

# 3. 创建导入后快照（用于后续回放对比）
python -m invoice_reconciler.cli.main review snapshot create --operator 张三 --description "自动匹配完成"

# 4. 人工确认待匹配项
python -m invoice_reconciler.cli.main confirm list --status pending
python -m invoice_reconciler.cli.main confirm approve 2 --operator 张三 --remark "核对无误"

# 5. 创建确认后快照
python -m invoice_reconciler.cli.main review snapshot create --operator 张三 --description "第一轮人工确认完成"

# 6. 导出快照（JSON 格式，带稳定编号）
python -m invoice_reconciler.cli.main export snapshot --snapshot-no R202606180001 --operator 张三 --format json

# 7. 导出快照（CSV 格式，同一编号重复导出）
python -m invoice_reconciler.cli.main export snapshot --snapshot-no R202606180001 --operator 张三 --format csv

# 8. 撤销某条匹配
python -m invoice_reconciler.cli.main revoke match 1 --operator 李四 --remark "匹配错误"

# 9. 回放校验（检测与快照的差异）
python -m invoice_reconciler.cli.main review replay --snapshot-no R202606180001

# 10. 重做确认
python -m invoice_reconciler.cli.main confirm approve 2 --operator 李四 --remark "重新确认"

# 11. 冲突检测（检查同一发票是否被多人处理）
python -m invoice_reconciler.cli.main review conflicts

# 12. 创建最终快照并导出
python -m invoice_reconciler.cli.main review snapshot create --operator 张三 --description "最终复核完成"
python -m invoice_reconciler.cli.main export full --operator 张三 --format json
```

## 示例完整流程

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 查看配置
python -m invoice_reconciler.cli.main config show

# 3. 导入发票和收款
python -m invoice_reconciler.cli.main import invoices invoice_reconciler/data/sample_invoices.csv --operator 张三
python -m invoice_reconciler.cli.main import payments invoice_reconciler/data/sample_payments.csv --operator 张三

# 4. 查看导入错误
python -m invoice_reconciler.cli.main import errors

# 5. 运行自动匹配
python -m invoice_reconciler.cli.main match --operator 张三

# 6. 查看状态
python -m invoice_reconciler.cli.main status

# 7. 查看待确认匹配
python -m invoice_reconciler.cli.main confirm list --status pending

# 8. 查看多候选
python -m invoice_reconciler.cli.main confirm candidates

# 9. 确认匹配
python -m invoice_reconciler.cli.main confirm show 2
python -m invoice_reconciler.cli.main confirm approve 2 --operator 张三 --remark "核对无误"

# 10. 处理多候选
python -m invoice_reconciler.cli.main confirm candidates --invoice-id 4
python -m invoice_reconciler.cli.main confirm approve 3 --operator 张三 --select-payment 6 --remark "选择第二笔收款"

# 11. 人工匹配未匹配项
python -m invoice_reconciler.cli.main list-unmatched --type invoices
python -m invoice_reconciler.cli.main list-unmatched --type payments
python -m invoice_reconciler.cli.main confirm manual 6 13 --operator 张三 --remark "手工匹配西安咨询公司"

# 12. 撤销错误匹配
python -m invoice_reconciler.cli.main revoke list
python -m invoice_reconciler.cli.main revoke match 1 --operator 张三 --remark "日期不符，需重新核对"

# 13. 导出报告
python -m invoice_reconciler.cli.main export full --operator 张三
python -m invoice_reconciler.cli.main export diff --operator 张三

# 14. 测试断点恢复（重复导入）
python -m invoice_reconciler.cli.main import invoices invoice_reconciler/data/sample_invoices.csv --operator 李四
python -m invoice_reconciler.cli.main status
```

## 错误处理说明

系统会记录以下类型的错误，并在报告中展示：

| 错误类型 | 说明 | 处理方式 |
|----------|------|----------|
| missing_customer | 客户名称为空 | 记录错误，不入库 |
| invalid_amount | 金额格式错误或非正数 | 记录错误，标记为无效 |
| invalid_date | 日期格式错误 | 记录错误，标记为无效 |
| missing_required | 缺少必填字段 | 记录错误，不入库 |

所有错误都会记录文件行号，方便用户定位原始数据问题。
