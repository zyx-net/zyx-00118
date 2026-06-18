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
- **工单锁定**：操作者处理记录时自动/手动加锁，防止并发冲突
- **锁转交**：支持将锁定的记录转交给其他操作者处理
- **超时接管**：过期的锁可被其他复核员接管，并记录完整操作日志
- **角色权限**：普通复核员只能处理自己锁定的记录，管理员可强制解锁/批量解锁
- **重启恢复**：锁状态持久化到数据库，程序重启后自动恢复
- **操作审计**：完整的锁历史和状态历史，支持追溯
- **导出增强**：JSON/CSV/快照导出包含责任人、锁历史、接管原因、确认证据
- **批次工作台**：批次摘要视图、进度百分比、未完成项提醒、一键导出进度
- **按处理人过滤**：支持按操作者筛选匹配记录，方便个人工作区
- **重启恢复上下文**：自动恢复上次打开的批次和筛选条件，避免重新找上下文
- **导入冲突检测**：同一批次重新导入时检测新增记录、状态冲突、金额变更、重复处理
- **冲突信息导出**：冲突和差异信息自动带入 JSON/CSV 导出，方便追溯
- **批次变更追踪**：重新导入时自动记录 5 类变更（新增记录/状态变更/金额变更/关键字段变更/重复处理），支持按变更类型、影响类型、处理状态过滤
- **影响分析**：自动识别变更对已确认/待确认/已撤销数据的影响程度（严重/影响已确认/影响待确认/影响已撤销/警告/无影响）
- **变更日志导出**：JSON 和 CSV 双格式稳定输出变更前后摘要、操作者、时间、关联批次、处理状态，支持交接追溯
- **导出上下文持久化**：程序重启后恢复上次导出上下文，`resume-export` 可一键继续导出不丢上下文
- **导出回执与单一事实来源**：每次 `batch export-changes` 自动生成回执，记录真实筛选快照、记录指纹、目标文件和摘要；`receipt show/compare/resume` 只认这份真实导出上下文，杜绝全量视角回退；跨重启、同配置接手、文件被改、目录无权限、工作目录迁移等场景要么安全续导，要么明确拦截

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

### 7. 人工确认匹配（⚠️ 必须先锁定再操作）

> **重要**：普通复核员（非 admin）**必须先 `lock acquire` 拿到自己的锁**才能确认/拒绝/改配/撤销；管理员可跳过。

```bash
# 查看待确认列表，记下要处理的匹配ID（第一列）
python -m invoice_reconciler.cli.main confirm list --status pending

# ⭐ 第一步：先锁定该条记录（必须与 confirm 的 --operator 是同一个人）
python -m invoice_reconciler.cli.main lock acquire 2 --operator 张三 --reason "与客户核对中"

# 查看匹配详情
python -m invoice_reconciler.cli.main confirm show 2

# 第二步：确认匹配（同一位操作员，持锁才能成功）
python -m invoice_reconciler.cli.main confirm approve 2 --operator 张三 --remark "核对无误"

# 处理完释放锁（可选，超时会自动过期）
python -m invoice_reconciler.cli.main lock release 2 --operator 张三

# --- 多候选场景 ---

# 查看多候选收款列表（显示收款ID，与 --select-payment 参数对应）
python -m invoice_reconciler.cli.main confirm candidates --invoice-id 4

# ⭐ 先锁定
python -m invoice_reconciler.cli.main lock acquire 3 --operator 张三 --reason "多候选人工改配"

# 多候选时选择特定收款（--select-payment 传入的是收款ID，即 candidates 列表中的"收款ID"列）
python -m invoice_reconciler.cli.main confirm approve 3 --operator 张三 --select-payment 6 --remark "选择第二笔收款"

# --- 拒绝匹配 ---
python -m invoice_reconciler.cli.main lock acquire 4 --operator 张三 --reason "核对不符需拒绝"
python -m invoice_reconciler.cli.main confirm reject 4 --operator 张三 --remark "客户名称不符，需核查"

# --- 人工强制匹配（指定发票ID和收款ID）---
python -m invoice_reconciler.cli.main lock acquire 6 --operator 张三 --reason "人工改配"
python -m invoice_reconciler.cli.main confirm manual 6 13 --operator 张三 --remark "手工匹配"
```

### 8. 撤销匹配（⚠️ 必须先锁定再操作）

```bash
# 查看可撤销的匹配
python -m invoice_reconciler.cli.main revoke list

# ⭐ 先锁定（普通复核员必须持锁，管理员可跳过）
python -m invoice_reconciler.cli.main lock acquire 1 --operator 张三 --reason "需撤销并重新核对"

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

### 15. 批次工作台 - 从导入到导出完整可跑命令链

> **批次工作台专为方便交接设计**：程序重启自动恢复上次打开的批次和筛选条件，
> 重新导入自动检测冲突，一键导出完整进度（含差异和冲突信息）。

```bash
# ========= 步骤 1：导入数据（发票 + 收款）
# 导入发票台账
python -m invoice_reconciler.cli.main import invoices invoice_reconciler/data/sample_invoices.csv --operator 张三
# 导入收款流水
python -m invoice_reconciler.cli.main import payments invoice_reconciler/data/sample_payments.csv --operator 张三

# ========= 步骤 2：查看所有批次列表
python -m invoice_reconciler.cli.main batch list

# ========= 步骤 3：选择当前处理批次（程序重启后自动恢复）
# 选择批次 #1（根据上面 list 的 ID）
python -m invoice_reconciler.cli.main batch select 1 --operator 张三

# ========= 步骤 4：设置筛选条件（程序重启后自动恢复）
# 按处理人过滤（只看张三处理的）
python -m invoice_reconciler.cli.main batch filter --operator 张三
# 按状态过滤（只看待确认的）
python -m invoice_reconciler.cli.main batch filter --status pending

# ========= 步骤 5：查看批次工作台摘要（进度条 + 待办提醒）
python -m invoice_reconciler.cli.main batch summary --batch-id 1

# ========= 步骤 6：运行自动匹配
python -m invoice_reconciler.cli.main match --operator 张三

# ========= 步骤 7：查看未完成项提醒
python -m invoice_reconciler.cli.main batch reminders

# ========= 步骤 8：人工确认匹配（先锁定再操作）
# 先查看待确认列表
python -m invoice_reconciler.cli.main confirm list --status pending
# 锁定记录
python -m invoice_reconciler.cli.main lock acquire 1 --operator 张三 --reason "核对中"
# 确认匹配
python -m invoice_reconciler.cli.main confirm approve 1 --operator 张三 --remark "核对无误"

# ========= 步骤 9：重新导入更新版文件（自动检测冲突）
# 模拟业务场景：财务发来更新版发票台账，包含新增、状态变更、金额变更、重复处理等场景
# 复制一份示例文件并修改其中几行来模拟更新
# 然后执行导入，系统会自动检测以下 4 类冲突：
#   1) new_record: 新增记录（原批次不存在）
#   2) status_change: 状态变更（如"正常"变"作废"）
#   3) amount_change: 金额变更（如 1500.00 变 1600.00）
#   4) duplicate_process: 重复处理（已处理过的记录被新操作人重导）
python -m invoice_reconciler.cli.main import invoices invoice_reconciler/data/sample_invoices.csv --operator 李四

# ========= 步骤 10：查看冲突明细
python -m invoice_reconciler.cli.main batch conflicts
# 按类型过滤冲突
python -m invoice_reconciler.cli.main batch conflicts --conflict-type status_change

# ========= 步骤 11：一键导出批次进度（含冲突信息）
# JSON 格式（结构化数据，方便程序处理）
python -m invoice_reconciler.cli.main batch export-progress 1 --operator 张三 --format json
# Excel 格式（带格式报表，方便交接）
python -m invoice_reconciler.cli.main batch export-progress 1 --operator 张三 --format xlsx
# CSV 格式
python -m invoice_reconciler.cli.main batch export-progress 1 --operator 张三 --format csv

# ========= 步骤 12：撤销某个已确认的匹配（测试撤销后再次导出）
python -m invoice_reconciler.cli.main lock acquire 1 --operator 张三 --reason "发现错误需撤销"
python -m invoice_reconciler.cli.main revoke match 1 --operator 张三 --remark "客户名称有误"

# ========= 步骤 13：再次导出进度（验证撤销后导出结果一致）
python -m invoice_reconciler.cli.main batch export-progress 1 --operator 张三 --format json

# ========= 步骤 14：模拟程序重启 - 自动恢复上下文
# 退出程序后重新运行任意命令，会自动显示上次的批次和筛选条件
python -m invoice_reconciler.cli.main batch summary

# ========= 步骤 15：手动恢复会话状态
python -m invoice_reconciler.cli.main batch restore

# ========= 步骤 16：清除会话状态（不恢复）
python -m invoice_reconciler.cli.main batch clear-state
```

### 15b. 变更追踪 - 重新导入后的完整交接链路

> **变更追踪专为交接设计**：同一批次文件重新导入后，不仅能看冲突，
> 还能直接看出新增了哪些记录、哪些状态变了、哪些金额或关键字段变了，
> 以及这些变化会不会影响已确认/待确认/已撤销的数据。
> 所有变更可导出为 JSON/CSV，程序重启后上下文不丢。

```bash
# ========= 前置准备：已有 v1 发票和收款，且做过匹配 =========
# 导入发票 v1
python -m invoice_reconciler.cli.main import invoices \
    invoice_reconciler/data/sample_invoices.csv --operator zhangsan
# 导入收款 v1
python -m invoice_reconciler.cli.main import payments \
    invoice_reconciler/data/sample_payments.csv --operator zhangsan
# 自动匹配
python -m invoice_reconciler.cli.main match --operator zhangsan

# ========= 步骤 1：重新导入更新版发票文件（自动触发变更追踪） =========
# 使用 sample_invoices_updated.csv（更新版），与 v1 相比有 4 类变化：
#   INV002 状态 normal→void、INV004 金额 12500→13000、
#   INV005 客户名变更、INV016 新增记录
# 重新导入时会自动检测 4 类变更，并评估对匹配结果的影响
#   - new_record: 新增记录（原批次没有）
#   - status_change: 状态变更（如"正常"变"作废"）
#   - amount_change: 金额变更
#   - key_field_change: 关键字段变更（客户、日期等）
#   - duplicate_process: 重复处理（不同操作者重新导入）
# 影响类型：critical(严重) / affects_confirmed(影响已确认) /
#          affects_pending(影响待确认) / affects_revoked(影响已撤销) /
#          warning(警告) / none(无影响)
python -m invoice_reconciler.cli.main import invoices \
    invoice_reconciler/data/sample_invoices_updated.csv --operator lisi

# ========= 步骤 2：默认查看所有批次的变更（不带 batch_id） =========
# 适合先全局看一遍所有变更，再决定深入哪个批次
python -m invoice_reconciler.cli.main batch changes
# 输出包含：变更统计表、影响分析表、处理状态表、变更明细（前20条）

# ========= 步骤 3：按变更类型过滤筛查 =========
# 只看金额变更
python -m invoice_reconciler.cli.main batch changes --change-type amount_change
# 只看状态变更
python -m invoice_reconciler.cli.main batch changes --change-type status_change
# 只看新增记录
python -m invoice_reconciler.cli.main batch changes --change-type new_record

# ========= 步骤 4：按影响程度过滤 =========
# 只看严重影响的变更（可能影响已确认匹配）
python -m invoice_reconciler.cli.main batch changes --impact-type critical
# 只看影响已确认匹配的变更
python -m invoice_reconciler.cli.main batch changes --impact-type affects_confirmed

# ========= 步骤 5：缩小到具体批次查看 =========
# 先看批次列表找批次ID（按导入顺序，发票v1=1、收款v1=2、发票v2=3）
python -m invoice_reconciler.cli.main batch list
# 查看指定批次的所有变更（请将 3 替换为 batch list 中你要查看的批次ID）
python -m invoice_reconciler.cli.main batch changes --batch-id 3

# ========= 步骤 6：按记录编号精准查找 =========
python -m invoice_reconciler.cli.main batch changes --record-no INV005

# ========= 步骤 7：更新变更处理状态（交接留痕） =========
# 先看变更列表第一列的日志ID，比如 ID=3
# 标记为已查看
python -m invoice_reconciler.cli.main batch change-status 3 \
    --status reviewed --operator lisi --remark "已核对，无影响"
# 标记为已解决
python -m invoice_reconciler.cli.main batch change-status 3 \
    --status resolved --operator lisi --remark "已调整匹配，问题解决"
# 标记为忽略
python -m invoice_reconciler.cli.main batch change-status 3 \
    --status ignored --operator lisi --remark "数据差异在可接受范围内，忽略"
# 查看已处理的变更
python -m invoice_reconciler.cli.main batch changes --status reviewed

# ========= 步骤 8：导出变更日志（JSON 格式，适合程序处理） =========
# 导出指定批次的所有变更日志，包含：
#   - 变更前后摘要
#   - 操作者、检测时间
#   - 关联批次、处理状态
#   - 影响分析详情
# （请将 3 替换为 batch list 中你要导出的批次ID）
python -m invoice_reconciler.cli.main batch export-changes 3 \
    --operator lisi --format json

# ========= 步骤 9：导出变更日志（CSV 格式，适合 Excel 打开） =========
# 会生成两个文件：变更摘要.csv + 变更明细.csv
python -m invoice_reconciler.cli.main batch export-changes 3 \
    --operator lisi --format csv

# ========= 步骤 10：模拟程序重启 - 恢复导出上下文 =========
# 退出后重新运行，会自动显示上次导出和查看的批次
# 使用 resume-export 一键用上次的上下文继续导出，不用重新记参数
python -m invoice_reconciler.cli.main batch resume-export --operator lisi
# 输出会显示：使用上次导出上下文（批次/类型/格式/上次导出时间）

# ========= 步骤 11：模拟程序重启 - 恢复查看上下文 =========
# 重新打开 batch changes 会自动恢复上次的查看范围和过滤条件
# （上次是全部批次 / 指定批次 / 过滤了哪些类型）
python -m invoice_reconciler.cli.main batch changes
# 顶部会显示 [会话恢复] 上次查看变更: xxx
```

**变更日志导出字段说明（JSON / CSV 一致）：**

| 字段 | 说明 |
|------|------|
| 日志ID | 变更日志唯一标识 |
| 批次ID | 所属导入批次 |
| 来源文件 | 原始文件名 |
| 变更类型 | new_record / status_change / amount_change / key_field_change / duplicate_process |
| 记录类型 | 发票 / 收款 |
| 记录编号 | 发票号或收款号 |
| 变更字段 | 发生变化的字段名（如 amount、status、customer） |
| 原值 | 变更前的值 |
| 新值 | 变更后的值 |
| 变更摘要 | 中文变更说明 |
| 变更前摘要 | 变更前记录完整摘要 |
| 变更后摘要 | 变更后记录完整摘要 |
| 影响类型 | critical / affects_confirmed / affects_pending / affects_revoked / warning / none |
| 影响详情 | 影响分析的文字说明 |
| 影响的匹配ID | 受影响的匹配记录ID列表 |
| 处理状态 | pending / reviewed / resolved / ignored |
| 操作者 | 触发本次导入的人 |
| 检测时间 | 变更被检测到的时间 |
| 处理时间 | 状态最后更新的时间 |
| 处理人 | 最后更新状态的人 |
| 备注 | 处理备注 |

**导出内容说明：**
- **批次摘要 Sheet**：批次信息、进度百分比、各状态计数（待确认/已确认/异常/已撤销）、冲突数
- **匹配明细 Sheet**: 所有匹配记录（带处理人、状态、操作时间、匹配证据）
- **批次冲突 Sheet**: 所有冲突记录（含冲突类型：新增记录、状态变更、金额变更、重复处理；差异原因写入）
- **未匹配发票 Sheet**: 本批次未匹配的发票明细（发票号、日期、客户、金额、匹配状态、来源文件）
- **未匹配收款 Sheet**: 本批次未匹配的收款明细（收款号、日期、客户、金额、匹配状态、来源文件）
- **JSON 格式**：包含 6 个顶级字段的结构化数据：
  ```json
  {
    "batch_info": { "batch_id": 1, "file_type": "发票", "file_name": "...", ... },
    "progress": { "pending_matches": 5, "confirmed_matches": 2, "unmatched_invoices": 9, ... },
    "matches": [ { "匹配ID": 1, "发票号": "INV001", "收款号": "PAY001", ... } ],
    "conflicts": [ { "冲突类型": "status_change", "冲突原因": "...", ... } ],
    "unmatched_invoices": [ { "发票号": "INV003", "客户": "...", "发票金额": "8000.00", ... } ],
    "unmatched_payments": [ { "收款号": "PAY005", "客户": "...", "收款金额": "5000.00", ... } ]
  }
  ```

### 15c. 导出回执与单一事实来源

> **核心设计理念**：回执不是平行命令，而是 `batch export-changes` 主流程的一部分。
> 每次导出自动落下真实的筛选快照、记录指纹、目标文件和摘要，后续 `receipt show/compare/resume`
> 只认这份真实导出上下文，**不会回退成整批全量视角**。
> 回执是导出状态的**单一事实来源**，替代了之前导出状态、回执状态、恢复提示各存一份的分叉逻辑。

#### 回执包含什么（单一事实来源）

每次 `batch export-changes` 成功后自动创建回执，包含以下完整上下文：

| 字段 | 说明 |
|------|------|
| receipt_no | 回执编号，如 E202606180001，全局唯一 |
| batch_id | 导出的批次ID |
| operator | 导出操作者 |
| export_format | 导出格式（json/csv/xlsx） |
| target_file | 实际导出的目标文件绝对路径 |
| file_hash | 导出文件的 SHA256 哈希，用于检测文件篡改 |
| filter_snapshot | 导出时实际命中的筛选条件快照（change_type/impact_type/status/record_no 等） |
| log_ids | 本次导出实际命中的变更日志ID列表（精确到每一条） |
| record_fingerprints | 基于 log_ids 计算的记录指纹列表（每条记录 = 类型+编号+变更类型+批次ID 的哈希） |
| summary_stats | 导出摘要：total_records、exported_records、by_change_type 等统计 |
| created_at | 导出时间戳 |

#### 命令速查

```bash
# ── 1. 导出（自动创建回执） ──
# 筛选导出 status_change 类型的变更（只导出1条也可以）
python -m invoice_reconciler.cli.main batch export-changes 3 \
    --operator lisi --format json --change-type status_change

# 导出成功后会输出：
#   ✅ 变更日志已导出: <文件路径>
#   🧾 已创建导出回执: E202606180001 (命中 1 条记录)
#   后续可用: receipt show / receipt compare / receipt resume

# ── 2. 查看回执（显示真实导出上下文，不是全量视角） ──
python -m invoice_reconciler.cli.main receipt show E202606180001
# 输出包含：回执编号、批次、筛选快照、命中 log_ids、目标文件、摘要统计

# ── 3. 对比回执（检测导出后是否有变化） ──
python -m invoice_reconciler.cli.main receipt compare E202606180001
# 对比项：
#   - 目标文件是否被修改（file_hash 校验）
#   - 记录指纹是否一致（基于 log_ids 重新计算，只对比真实导出范围）
#   - 筛选快照是否匹配
# 所有项匹配输出 ✅，有差异输出 ❌ 并标注具体差异

# ── 4. 续导回执（用完全相同的筛选条件 + log_ids 重新导出） ──
python -m invoice_reconciler.cli.main receipt resume E202606180001 --operator lisi
# 续导前自动执行拦截检测：
#   - 文件是否被修改（可通过 --force 跳过）
#   - 导出目录是否可写
#   - 工作目录是否迁移（找不到文件时拦截）
# 续导成功后自动更新回执中的 target_file、file_hash、record_fingerprints

# ── 5. 列出所有回执 ──
python -m invoice_reconciler.cli.main receipt list
# 显示回执编号、批次、操作者、导出时间、命中记录数

# ── 6. batch resume-export（优先使用最新回执） ──
python -m invoice_reconciler.cli.main batch resume-export --operator lisi
# 自动查找最新回执作为导出上下文，带拦截检测
# 也支持 --force 强制跳过拦截
```

#### 拦截检测场景说明

系统在 `receipt resume` 和 `batch resume-export` 时会执行以下检测，
**要么安全续导，要么明确拦截**：

| 场景 | 行为 | 处理方式 |
|------|------|----------|
| 跨程序重启 | ✅ 安全续导 | 回执存在数据库中，重启后直接使用 |
| 同配置重新接手 | ✅ 安全续导 | 同一配置下回执上下文一致 |
| 导出文件被修改 | ❌ 拦截 | 检测到 file_hash 不匹配，提示文件已被修改 |
| 导出目录无写入权限 | ❌ 拦截 | 检查目录可写性，提示权限不足 |
| 工作目录迁移（原文件找不到） | ❌ 拦截 | 目标文件路径不存在，提示工作目录可能已迁移 |
| 数据库中对应 log_ids 的记录已被删除 | ❌ 拦截 | 提示导出范围的记录已不存在 |
| 想用旧的筛选条件但数据已变 | ⚠️ 按真实 log_ids 导出 | 不会回退到全量，始终只导回执中记录的范围 |

> **强制续导**：如果确认文件修改不影响、或想换个目录重新导出，
> 可加 `--force` 参数跳过文件哈希校验，但 log_ids 和筛选快照仍会被严格遵守。

#### 完整验证链路（可实际复制运行）

```bash
# ═══════════════════════════════════════════════════════════
#  完整链路：导出1条 → 关掉再开 → 查看 → 对比 → 续导 → 异常拦截
# ═══════════════════════════════════════════════════════════

# ── 0. 环境准备（如果没有数据，先跑一遍导入+匹配+重导） ──
Remove-Item -Recurse -Force invoice_reconciler\data\reconciler.db -ErrorAction SilentlyContinue
python -m invoice_reconciler.cli.main import invoices \
    invoice_reconciler/data/sample_invoices.csv --operator zhangsan
python -m invoice_reconciler.cli.main import payments \
    invoice_reconciler/data/sample_payments.csv --operator zhangsan
python -m invoice_reconciler.cli.main match --operator zhangsan
python -m invoice_reconciler.cli.main import invoices \
    invoice_reconciler/data/sample_invoices_updated.csv --operator lisi
# 记下最后一次导入的批次ID（通常是 3），下文用 <BATCH_ID>

# ── 1. 筛选导出：只导 status_change 类型（应只命中1-2条） ──
python -m invoice_reconciler.cli.main batch export-changes <BATCH_ID> \
    --operator lisi --format json --change-type status_change
# ✅ 输出包含回执编号，如 E202606180001，记下它下文用 <RECEIPT_NO>

# ── 2. 模拟程序关掉再开（实际关不关都一样，DB 持久化） ──
# 直接执行下一步即可验证跨重启

# ── 3. 查看回执（确认只记住了真实导出的筛选范围） ──
python -m invoice_reconciler.cli.main receipt show <RECEIPT_NO>
# 验证点：filter_snapshot 中有 change-type=status_change，
#         log_ids 数量很少（1-2条），不是整批全量

# ── 4. 对比回执（导出后还没动过，应该全部匹配） ──
python -m invoice_reconciler.cli.main receipt compare <RECEIPT_NO>
# 预期：所有对比项显示 ✅ MATCH

# ── 5. 续导回执（用相同筛选条件重新导出） ──
python -m invoice_reconciler.cli.main receipt resume <RECEIPT_NO> --operator lisi
# 预期：拦截检测通过，重新导出成功，回执自动更新 file_hash

# ── 6. 异常拦截：修改导出文件后尝试续导 ──
# 先找到回执中 target_file 指向的 JSON 文件，随便改一个字符
# 然后尝试续导：
python -m invoice_reconciler.cli.main receipt resume <RECEIPT_NO> --operator lisi
# 预期：❌ 拦截，提示 "导出文件已被修改"

# ── 7. 强制续导（跳过文件哈希校验） ──
python -m invoice_reconciler.cli.main receipt resume <RECEIPT_NO> --operator lisi --force
# 预期：跳过文件哈希检查，续导成功（但 log_ids 和筛选条件仍严格遵守）

# ── 8. batch resume-export 也走回执逻辑 ──
python -m invoice_reconciler.cli.main batch resume-export --operator lisi
# 预期：自动找到最新回执，使用其上下文导出，而不是旧的 session state
```

#### 与旧版分叉逻辑的对比

| 维度 | 旧版（分叉逻辑） | 新版（单一事实来源） |
|------|------------------|----------------------|
| 导出状态存储 | workbench session state + 回执 + 恢复提示，各存一份 | 只存在回执（export_receipts 表） |
| 续导上下文 | 可能回退到整批全量（3条） | 始终使用回执中的 log_ids + filter_snapshot，精确到条 |
| 对比范围 | 用 workbench 当前筛选条件，可能和导出时不一致 | 只用回执中记录的筛选快照，保证对比的是同一范围 |
| 文件篡改检测 | 无 | SHA256 哈希校验，修改即拦截 |
| 跨重启可靠性 | 依赖 session state，可能丢失 | 回执持久化在 DB，100% 可恢复 |
| 命令入口 | receipt 是平行命令，和 export-changes 脱节 | export-changes 自动生成回执，receipt 是回执的操作入口 |

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
lock_timeout_seconds: 3600      # 锁超时时间（秒），过期后可被他人接管
default_user_role: reviewer     # 默认用户角色：reviewer 或 admin
admin_users:                    # 管理员用户列表
  - admin
enable_lock: true               # 是否启用工单锁定功能
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
│   ├── reviewer.py          # 复核快照与回放校验
│   └── workflow.py          # 工单锁定与角色权限
├── data/
│   ├── config.yaml          # 配置文件
│   ├── sample_invoices.csv  # 示例发票数据
│   └── sample_payments.csv  # 示例收款数据
├── exports/                 # 报告导出目录
└── tests/
    ├── test_review_snapshot.py  # 快照与回放测试
    ├── test_cli_fixes.py        # CLI 集成测试
    └── test_workflow.py         # 工单流测试
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

### Q: 变更追踪和冲突检测有什么区别？
A: **冲突检测**是识别同一记录被重复处理、状态或金额不一致的问题列表；
**变更追踪**是完整的变更日志，包括 5 类变更（新增记录/状态变更/金额变更/关键字段变更/重复处理）、
影响分析（对已确认/待确认/已撤销数据的影响程度）、处理状态（待处理/已查看/已解决/已忽略），
支持按类型过滤、导出为 JSON/CSV、程序重启后恢复上下文，更适合交接和追溯。

### Q: 怎么看重新导入后有哪些变化？
A: 使用 `batch changes` 命令：不带参数默认查看所有批次的全部变更，
用 `--batch-id` 缩小到指定批次，用 `--change-type` 按类型过滤，
用 `--impact-type` 按影响程度过滤。具体示例见"批次工作台 → 变更追踪"章节。

### Q: 程序重启后导出会丢上下文吗？
A: 不会。每次 `batch export-changes` 成功后会自动创建导出回执，记录完整的导出上下文（筛选快照、log_ids、目标文件、记录指纹、摘要统计）。回执持久化在 SQLite 中，用 `batch resume-export` 或 `receipt resume <回执编号>` 可以一键继续导出，保证续导范围和导出时完全一致，不会回退到整批全量。

### Q: 导出回执和 batch resume-export 有什么关系？
A: 回执是导出状态的**单一事实来源**。`batch export-changes` 自动生成回执，`batch resume-export` 优先使用最新回执的上下文，`receipt show/compare/resume` 直接操作回执。旧的 workbench session state 逻辑保留作回退兼容，但优先走回执。

### Q: 回执对比时说"记录指纹不匹配"是什么意思？
A: 说明回执记录的导出范围内，某些变更记录的内容已经变了（比如状态又更新了、金额又改了）。这意味着数据库中的数据和导出时已经不一致，需要决定是否重新导出。对比是**只针对回执中 log_ids 记录的真实导出范围**，不会拿整批全量来比。

### Q: 修改了导出文件后续导被拦截了怎么办？
A: 如果只是文件被改了但数据库数据没变，加 `--force` 参数可以跳过文件哈希校验强制续导。但 `--force` 不会跳过 log_ids 和筛选快照的约束，续导仍会严格按回执记录的范围导出，不会回退到全量。如果想重新筛选导出，请直接用 `batch export-changes` 重新导出，会自动生成新的回执。

### Q: 程序重启后快照和历史会丢失吗？
A: 不会。所有快照、状态历史和匹配数据都保存在 SQLite 数据库中，程序重启后可以继续操作。

### Q: `--select-payment` 参数应该传什么值？
A: 传**收款ID**（payment_id）。可以通过 `confirm candidates --invoice-id <发票ID>` 查看候选列表，列表中的"收款ID"列就是要传的值。注意不要和匹配ID（match_id）混淆，匹配ID是 `confirm approve` 的第一个参数。

### Q: 匹配ID和收款ID有什么区别？
A: 匹配ID（match_id）是 `matches` 表的主键，对应一条匹配记录；收款ID（payment_id）是 `payments` 表的主键，对应一笔收款。多候选场景下，一条待确认匹配（有匹配ID）可以选择不同的收款（用收款ID切换）。

### Q: 什么是工单锁定？
A: 当操作者开始处理某条匹配记录时，系统会自动或手动给该记录加锁，锁定后只有持有人（或管理员）才能操作。锁有超时机制，过期后可被他人接管。

### Q: 普通复核员和管理员有什么区别？
A: 普通复核员只能操作自己锁定的记录，不能强制解锁或批量解锁。管理员可以操作任何记录、强制解锁、批量解锁、设置用户角色。

### Q: 程序重启后锁状态会丢失吗？
A: 不会。锁状态保存在 SQLite 数据库中，程序重启后自动恢复。过期锁在下次查询时会被识别为已过期，可被他人接管。

### Q: 导出中是否包含锁信息？
A: 是。JSON/CSV/快照导出都包含当前责任人、锁历史、接管原因和最后确认证据。

## 从导入到回放校验的完整命令链

以下命令链**严格遵循先锁再处理**，可以复制粘贴从零开始实际跑通。匹配 ID 是自增的，请按 `status` 输出的真实 ID 替换示例中的数字。

```bash
# ── 0. 清理旧数据库（可选，从零开始） ──
# Windows (PowerShell):
Remove-Item -Recurse -Force invoice_reconciler\data\reconciler.db -ErrorAction SilentlyContinue
# Linux / macOS:
# rm -f invoice_reconciler/data/reconciler.db

# ═══════════════════════════════════════════
#  第一阶段：导入 → 自动匹配
# ═══════════════════════════════════════════

# 1. 导入发票台账（15 条示例，11 条成功）
python -m invoice_reconciler.cli.main import invoices \
    invoice_reconciler/data/sample_invoices.csv --operator zhangsan

# 2. 导入收款流水（17 条示例，15 条成功）
python -m invoice_reconciler.cli.main import payments \
    invoice_reconciler/data/sample_payments.csv --operator zhangsan

# 3. 运行自动匹配（会输出 "待人工确认的匹配ID: Mxxxx..." 列表）
python -m invoice_reconciler.cli.main match --operator zhangsan

# 4. 查看状态，找到第一条 "待确认匹配" 的 ID（第一列数字，示例中是 8）
python -m invoice_reconciler.cli.main status
#    记下待确认列表第一个 ID，例如 9（INV005，模糊匹配，金额 3200 vs 3199.99）
#    下文用 <PENDING_ID> 代表这个值，请自行替换

# 5. 创建导入后快照（用于后续回放对比）
python -m invoice_reconciler.cli.main review snapshot create \
    --operator zhangsan --description "自动匹配完成"

# ═══════════════════════════════════════════
#  第二阶段：先锁再处理（核心约束演示）
# ═══════════════════════════════════════════

# 6. ❌ 演示失败：不锁定直接 confirm（普通复核员 lisi 应被拒绝）
python -m invoice_reconciler.cli.main confirm approve <PENDING_ID> \
    --operator lisi --remark "未锁定就确认(会失败)"
#    预期输出：[!!] 该记录未被锁定，请先执行 'lock acquire' 锁定后再操作

# 7. ✅ 正确流程：lisi 先 lock acquire 拿到锁
python -m invoice_reconciler.cli.main lock acquire <PENDING_ID> \
    --operator lisi --reason "lisi 与客户电话核对中"

# 8. 查看 lisi 持有的锁
python -m invoice_reconciler.cli.main lock list --owner lisi

# 9. ❌ 锁冲突：zhangsan 同时想锁定同一条记录（应被拒绝，报 "已被 lisi 锁定"）
python -m invoice_reconciler.cli.main lock acquire <PENDING_ID> \
    --operator zhangsan --reason "zhangsan 也想处理"

# 10. ✅ 持锁后 confirm approve（同一位操作员 lisi，成功）
python -m invoice_reconciler.cli.main confirm approve <PENDING_ID> \
    --operator lisi --remark "电话与客户核对一致，确认该笔匹配"

# 11. 查看详情（含确认人、确认时间、状态历史、锁信息）
python -m invoice_reconciler.cli.main confirm show <PENDING_ID>

# 12. 完成处理后释放锁
python -m invoice_reconciler.cli.main lock release <PENDING_ID> --operator lisi

# ═══════════════════════════════════════════
#  第三阶段：接管过期锁 → 撤销重做（历史不冲掉）
# ═══════════════════════════════════════════

# 13. 再找另一条待确认记录，zhangsan 锁定（<PENDING_ID_2>）
python -m invoice_reconciler.cli.main lock acquire <PENDING_ID_2> \
    --operator zhangsan --reason "zhangsan 锁定后长期不处理"

# 14. ⚡ 管理员强制模拟过期（实际是等 lock_timeout_seconds 秒；或用 DB 直接改时间）
#     然后 lisi 接管这条过期锁
python -m invoice_reconciler.cli.main lock takeover <PENDING_ID_2> \
    --operator lisi --reason "原锁长期未处理，已过期，lisi 接管"

# 15. lisi 接管后撤销（锁历史中的 zhangsan→lisi 接管链条不会丢）
python -m invoice_reconciler.cli.main revoke match <PENDING_ID_2> \
    --operator lisi --remark "接管后发现匹配有误，撤销"

# 16. 查看锁操作历史（应同时看到 zhangsan 的锁定 + lisi 的 takeover + revoke）
python -m invoice_reconciler.cli.main lock history --match-id <PENDING_ID_2>

# 17. 撤销后重做：重新人工匹配新记录（旧的锁历史/状态历史保留在原记录上）
python -m invoice_reconciler.cli.main list-unmatched --type invoices
python -m invoice_reconciler.cli.main confirm manual 6 13 --operator admin \
    --remark "撤销后重新人工匹配，管理员可跳过锁"

# ═══════════════════════════════════════════
#  第四阶段：导出 + 快照 + 回放
# ═══════════════════════════════════════════

# 18. 创建确认后快照
python -m invoice_reconciler.cli.main review snapshot create \
    --operator zhangsan --description "第一轮人工确认完成"
#    记下输出的快照编号，如 R202606180001，下文用 <SNAPSHOT_NO>

# 19. 导出 JSON 完整报告（含：责任人 / 锁历史 / 接管原因 / 最后确认证据）
python -m invoice_reconciler.cli.main export full --operator lisi --format json
#    打开导出的 JSON，任意已匹配记录可见字段：
#    - 当前责任人 / 锁定原因 / 锁定时间 / 锁到期时间 / 是否锁定
#    - 锁历史（完整操作链：锁定→接管→解锁→...）
#    - 接管原因（提取 takeover 的 reason）
#    - 最后确认证据（确认人+备注+时间+类型+得分+证据）

# 20. 导出 CSV 完整报告（同样包含上述锁字段）
python -m invoice_reconciler.cli.main export full --operator lisi --format csv

# 21. 导出快照（JSON / CSV，带稳定编号，同一快照可多次导出编号不变）
python -m invoice_reconciler.cli.main export snapshot \
    --snapshot-no <SNAPSHOT_NO> --operator zhangsan --format json
python -m invoice_reconciler.cli.main export snapshot \
    --snapshot-no <SNAPSHOT_NO> --operator zhangsan --format csv

# 22. 回放校验（检测当前状态与历史快照是否一致，输出差异详情）
python -m invoice_reconciler.cli.main review replay --snapshot-no <SNAPSHOT_NO>

# ═══════════════════════════════════════════
#  第五阶段：管理员操作（强制解锁 / 批量解锁）
# ═══════════════════════════════════════════

# 23. 先造几条被 zhangsan 锁定的记录
python -m invoice_reconciler.cli.main lock acquire <ANOTHER_ID_1> \
    --operator zhangsan --reason "处理中"
python -m invoice_reconciler.cli.main lock acquire <ANOTHER_ID_2> \
    --operator zhangsan --reason "处理中"

# 24. 管理员 admin 强制解锁单条记录
python -m invoice_reconciler.cli.main lock force-unlock <ANOTHER_ID_1> \
    --operator admin --reason "张三请假，管理员强制释放"

# 25. 管理员批量解锁 zhangsan 持有的所有锁
python -m invoice_reconciler.cli.main lock batch-unlock \
    --operator admin --reason "批量释放张三的锁（请假交接）" --owner zhangsan

# 26. 再次 import 同一旧批次（不会冲掉现有锁和历史）
python -m invoice_reconciler.cli.main import invoices \
    invoice_reconciler/data/sample_invoices.csv --operator zhangsan
#    预期输出："文件已导入，批次ID: x，跳过重复导入"

# 27. 最终快照 + 完整报告
python -m invoice_reconciler.cli.main review snapshot create \
    --operator admin --description "最终复核完成"
python -m invoice_reconciler.cli.main export full --operator admin --format json

# 28. 跑测试套件验证（36个测试，覆盖批次统计、撤销后重导、冲突导出、重启恢复、未匹配项导出等回归场景）
python -m unittest tests.test_batch_workbench -v
```

## 示例完整流程（日常使用，严格先锁再操作）

> 运行前先清理：`Remove-Item -Force invoice_reconciler\data\reconciler.db`（Windows）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 查看/修改配置
python -m invoice_reconciler.cli.main config show
# python -m invoice_reconciler.cli.main config set --amount-tolerance 0.01 --date-window 30 --export-format json

# 3. 导入发票和收款
python -m invoice_reconciler.cli.main import invoices invoice_reconciler/data/sample_invoices.csv --operator zhangsan
python -m invoice_reconciler.cli.main import payments invoice_reconciler/data/sample_payments.csv --operator zhangsan

# 4. 查看导入错误
python -m invoice_reconciler.cli.main import errors

# 5. 运行自动匹配
python -m invoice_reconciler.cli.main match --operator zhangsan

# 6. 查看状态并记录待确认列表的第一个 ID（第一列，如 8/9/10...）
python -m invoice_reconciler.cli.main status
# 假设第一个待确认 ID 是 <PID>

# 7. ⭐ 日常流程：先 lock，再操作，再释放
#    把下面的 <PID> 替换成你自己 status 里看到的数字
python -m invoice_reconciler.cli.main lock acquire <PID> --operator lisi --reason "日常复核：与客户邮件核对"
python -m invoice_reconciler.cli.main confirm show <PID>
python -m invoice_reconciler.cli.main confirm approve <PID> --operator lisi --remark "核对无误"
python -m invoice_reconciler.cli.main lock release <PID> --operator lisi

# 8. 处理多候选（先锁 → 再选择收款 → 确认）
python -m invoice_reconciler.cli.main confirm candidates --invoice-id 4
# 假设候选列表中要选择的收款 ID 是 <PAY_ID>，对应匹配 ID 是 <PID_2>
python -m invoice_reconciler.cli.main lock acquire <PID_2> --operator lisi --reason "多候选改配"
python -m invoice_reconciler.cli.main confirm approve <PID_2> --operator lisi \
    --select-payment <PAY_ID> --remark "选择与合同一致的那笔收款"
python -m invoice_reconciler.cli.main lock release <PID_2> --operator lisi

# 9. 人工匹配未匹配项（先锁，或用 admin 直接跳过）
python -m invoice_reconciler.cli.main list-unmatched --type invoices
python -m invoice_reconciler.cli.main list-unmatched --type payments
# 管理员 admin 可跳过锁直接操作
python -m invoice_reconciler.cli.main confirm manual 6 13 --operator admin --remark "手工匹配西安咨询公司"

# 10. 撤销错误匹配（普通复核员需先锁自己的记录）
python -m invoice_reconciler.cli.main revoke list
python -m invoice_reconciler.cli.main lock acquire 1 --operator zhangsan --reason "发现错误要撤销"
python -m invoice_reconciler.cli.main revoke match 1 --operator zhangsan --remark "日期不符，需重新核对"

# 11. 导出报告（JSON/CSV 均含 责任人/锁历史/接管原因/确认证据）
python -m invoice_reconciler.cli.main export full --operator lisi --format json
python -m invoice_reconciler.cli.main export full --operator lisi --format csv

# 12. 测试断点恢复：重复导入（去重，不会冲掉锁和历史）
python -m invoice_reconciler.cli.main import invoices invoice_reconciler/data/sample_invoices.csv --operator lisi
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

## 工单锁定管理

### 锁操作命令

```bash
# 锁定匹配记录（开始处理时手动加锁）
python -m invoice_reconciler.cli.main lock acquire 1 --operator 张三 --reason "开始复核"

# 解锁（持有人完成处理后解锁）
python -m invoice_reconciler.cli.main lock release 1 --operator 张三 --reason "处理完成"

# 转交锁（将记录转交给其他人处理）
python -m invoice_reconciler.cli.main lock transfer 1 李四 --operator 张三 --reason "转交李四继续处理"

# 接管过期锁（锁超时后其他人可接管）
python -m invoice_reconciler.cli.main lock takeover 1 --operator 李四 --reason "原锁已过期，接管处理"

# 强制解锁（仅管理员）
python -m invoice_reconciler.cli.main lock force-unlock 1 --operator admin --reason "管理员强制解锁"

# 批量解锁（仅管理员，可指定持有者）
python -m invoice_reconciler.cli.main lock batch-unlock --operator admin --reason "批量释放" --owner 张三

# 列出锁定记录
python -m invoice_reconciler.cli.main lock list

# 列出所有锁（包括已过期）
python -m invoice_reconciler.cli.main lock list --all

# 按持有人过滤
python -m invoice_reconciler.cli.main lock list --owner 张三

# 查看锁操作历史
python -m invoice_reconciler.cli.main lock history

# 按匹配ID查看历史
python -m invoice_reconciler.cli.main lock history --match-id 1

# 按操作人过滤历史
python -m invoice_reconciler.cli.main lock history --operator 张三
```

### 用户角色管理

```bash
# 列出所有用户（仅管理员）
python -m invoice_reconciler.cli.main user list --operator admin

# 设置用户角色（仅管理员）
python -m invoice_reconciler.cli.main user set-role 张三 admin --operator admin

# 查看用户信息和锁统计
python -m invoice_reconciler.cli.main user info --username 张三
```

### 锁操作类型说明

| 操作类型 | 说明 | 权限要求 |
|----------|------|----------|
| lock | 锁定记录 | 任何用户（记录未被锁定） |
| unlock | 解锁记录 | 锁持有人 |
| transfer | 转交锁 | 锁持有人 |
| takeover | 接管过期锁 | 任何用户（锁已过期） |
| force_unlock | 强制解锁 | 仅管理员 |
| batch_unlock | 批量解锁 | 仅管理员 |

### 角色权限说明

| 操作 | 复核员 (reviewer) | 管理员 (admin) |
|------|-------------------|----------------|
| 锁定未锁定记录 | ✅ | ✅ |
| 解锁自己的记录 | ✅ | ✅ |
| 转交自己的锁 | ✅ | ✅ |
| 接管过期锁 | ✅ | ✅ |
| 操作他人锁定的记录 | ❌ | ✅ |
| 强制解锁 | ❌ | ✅ |
| 批量解锁 | ❌ | ✅ |
| 设置用户角色 | ❌ | ✅ |

## 含工单流的完整验证命令链（从零开始可跑通）

以下命令链覆盖**导入→匹配→锁冲突→转交→接管→撤销→重做→导出→回放→重启恢复**全链路，可实际复制运行。
关键：所有普通复核员操作**必须先 lock acquire**，示例中 `<PID>`、`<PID_2>` 请替换为 `status` 输出的真实待确认 ID。

```bash
# ── 0. 环境准备（清理旧库 + 验证依赖） ──
Remove-Item -Recurse -Force invoice_reconciler\data\reconciler.db -ErrorAction SilentlyContinue
python -c "import yaml, click, openpyxl; print('依赖检查通过')"

# ── 1. 导入 ──
python -m invoice_reconciler.cli.main import invoices \
    invoice_reconciler/data/sample_invoices.csv --operator importer
python -m invoice_reconciler.cli.main import payments \
    invoice_reconciler/data/sample_payments.csv --operator importer

# ── 2. 自动匹配 ──
python -m invoice_reconciler.cli.main match --operator matcher
#    输出 "待人工确认的匹配ID: M..., M..."

# ── 3. 查看状态，记录待确认的前 2 个 ID（<PID> 和 <PID_2>） ──
python -m invoice_reconciler.cli.main status
#    例如：8 (INV004 多候选)、9 (INV005 模糊)

# ── 4. ⭐ 严格先锁再确认（普通复核员 reviewer_a） ──
#    ❌ 先演示：不锁直接确认 → 被拒绝（核心规则验证）
python -m invoice_reconciler.cli.main confirm approve <PID> \
    --operator reviewer_a --remark "XXX"
#    预期：[!!] 该记录未被锁定，请先执行 'lock acquire' 锁定后再操作

#    ✅ 正确：先 lock，再 confirm
python -m invoice_reconciler.cli.main lock acquire <PID> \
    --operator reviewer_a --reason "与客户核对中"
python -m invoice_reconciler.cli.main confirm show <PID>
python -m invoice_reconciler.cli.main confirm approve <PID> \
    --operator reviewer_a --remark "与客户邮件核对一致"
python -m invoice_reconciler.cli.main lock release <PID> --operator reviewer_a

# ── 5. 锁冲突：同一条记录不能两人同时锁 ──
python -m invoice_reconciler.cli.main lock acquire <PID_2> \
    --operator reviewer_a --reason "reviewer_a 先抢到"
python -m invoice_reconciler.cli.main lock acquire <PID_2> \
    --operator reviewer_b --reason "reviewer_b 也想抢"
#    预期：[!!] 记录已被 reviewer_a 锁定

# ── 6. 转交锁：reviewer_a → reviewer_b ──
python -m invoice_reconciler.cli.main lock transfer <PID_2> reviewer_b \
    --operator reviewer_a --reason "reviewer_a 临时有事，转交 reviewer_b 继续"
python -m invoice_reconciler.cli.main lock list --owner reviewer_b
python -m invoice_reconciler.cli.main lock history --match-id <PID_2>
#    历史可见：锁定(reviewer_a) → 转交(reviewer_a→reviewer_b)

# ── 7. 接管过期锁（模拟：reviewer_b 锁定后不处理） ──
#    方式A：等配置 lock_timeout_seconds（默认 3600s）后自动过期
#    方式B：管理员直接强制解锁再重新分配
python -m invoice_reconciler.cli.main lock force-unlock <PID_2> \
    --operator admin --reason "reviewer_b 请假，管理员强制释放"
python -m invoice_reconciler.cli.main lock takeover <PID_2> \
    --operator reviewer_c --reason "原锁被管理员释放，reviewer_c 接管重做"

# ── 8. 接管后撤销：旧的锁历史/状态历史不会被冲掉 ──
python -m invoice_reconciler.cli.main revoke match <PID_2> \
    --operator reviewer_c --remark "接管后发现该笔对不上，撤销"
python -m invoice_reconciler.cli.main lock history --match-id <PID_2>
#    历史仍保留 reviewer_a → reviewer_b → admin(force_unlock) → reviewer_c(takeover) 完整链条

# ── 9. 撤销后重做：新创建的匹配记录与旧记录 ID 不同，历史完全独立 ──
python -m invoice_reconciler.cli.main list-unmatched --type invoices
#    选一个未匹配发票和收款 ID，比如发票 6 和收款 13
python -m invoice_reconciler.cli.main confirm manual 6 13 \
    --operator admin --remark "撤销后重新人工匹配，admin 可跳过锁"

# ── 10. 跨重启恢复：模拟关闭程序再打开（实际关不关闭都一样，DB 持久化） ──
#     锁状态、锁历史、状态历史全部存在 SQLite，重启后自动恢复
python -m invoice_reconciler.cli.main lock list --all
#     启动会打印：[锁状态恢复] 共 N 条锁，其中已过期 X 条。配置超时: 3600 秒

# ── 11. 创建快照（包含当时所有锁信息和确认证据） ──
python -m invoice_reconciler.cli.main review snapshot create \
    --operator reviewer_c --description "第一轮复核 + 接管撤销完成"
#    记下快照编号 <SNAP>，如 R202606180001

# ── 12. 导出：JSON / CSV 均含 责任人/锁历史/接管原因/最后确认证据 ──
python -m invoice_reconciler.cli.main export full --operator reviewer_c --format json
python -m invoice_reconciler.cli.main export full --operator reviewer_c --format csv
python -m invoice_reconciler.cli.main export snapshot \
    --snapshot-no <SNAP> --operator reviewer_c --format json

# ── 13. 回放校验 ──
python -m invoice_reconciler.cli.main review replay --snapshot-no <SNAP>

# ── 14. 管理员操作：批量解锁所有用户的所有锁 ──
python -m invoice_reconciler.cli.main lock list --all
python -m invoice_reconciler.cli.main lock batch-unlock \
    --operator admin --reason "对账周期结束，批量释放所有锁"
python -m invoice_reconciler.cli.main lock list --all

# ── 15. 导入旧批次：跳过重复导入，不冲掉锁和历史 ──
python -m invoice_reconciler.cli.main import invoices \
    invoice_reconciler/data/sample_invoices.csv --operator importer
#    输出：文件已导入，批次ID: x，跳过重复导入

# ── 15.5 批次工作台：查看进度、筛选、重启恢复 ──
#    查看批次摘要（含进度百分比、各状态计数、冲突数）
python -m invoice_reconciler.cli.main batch summary
#    选择批次并保存（下次重启自动恢复）
python -m invoice_reconciler.cli.main batch select 1 --operator reviewer_c
#    设置筛选条件（只看 reviewer_c 处理的已确认匹配）
python -m invoice_reconciler.cli.main batch filter --operator reviewer_c --status matched
#    一键导出批次进度 JSON（含 matches、conflicts、progress 全部字段）
python -m invoice_reconciler.cli.main batch export-progress 1 --operator reviewer_c --format json
#    模拟重启：手动恢复会话状态，显示上次批次+筛选条件
python -m invoice_reconciler.cli.main batch restore

# ── 16. 运行测试套件（36个测试，覆盖导入更新、冲突导出、撤销后重导、重启恢复、未匹配项导出等回归场景） ──
python -m unittest tests.test_batch_workbench -v
```

> **运行结果自检清单**：运行后依次核对
> 1. 第4步不锁直接 confirm 被拒 ✅
> 2. 第5步 reviewer_a/reviewer_b 锁冲突被拒 ✅
> 3. 第8步撤销后 `lock history` 仍能看到最早的 reviewer_a 操作 ✅
> 4. 第12步导出 JSON 中能看到"锁历史"/"接管原因"/"最后确认证据"字段 ✅
> 5. 第13步 replay 输出包含"一致"或"差异"字样 ✅
> 6. 第15.5步 batch summary 中的 progress_percent ∈ [0, 100]，各状态计数≥0 ✅
> 7. 第15.5步 batch restore 正确返回 has_state=true，last_batch_id=上次选择的批次 ✅
> 8. 第16步所有 36 个测试显示 `OK` ✅
