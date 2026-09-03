## ACSSA 底座命令

你在ACSSA 底座中运行，以下 Python 命令可用。**会话开始和结束由 hooks 自动处理**，你只需要在关键决策点主动调用以下命令：

### 自动执行（hooks，你不需要管）
| 时机 | 自动化内容 |
|------|-----------|
| Agent 启动 | 健康检查 → 注册 → session-start（浮现记忆 + 加载偏好 + 恢复状态） |
| Agent 退出 | session-end（写入总结 + 更新状态） |

### 你需要主动调用的命令

| 命令 | 用途 | 示例 |
|------|------|------|
| `qingtian.py recall "关键词"` | 搜索历史经验 | `qingtian.py recall "供应商评估标准"` |
| `qingtian.py learn` (stdin) | 提交决策经验 | `echo "选择了B供应商因为..." \| qingtian.py learn` |
| `qingtian.py remember` (stdin) | 写入长期记忆 | `echo "供应商A交货周期30天" \| qingtian.py remember` |
| `qingtian.py pitfall "标题" "描述"` | 上报踩坑 | `qingtian.py pitfall "OOM" "并发过高导致" high` |
| `qingtian.py insights` | 查看进化统计 | `qingtian.py insights` |
| `qingtian.py profile` | 查看当前画像 | `qingtian.py profile` |
| `qingtian.py recover` | 崩溃恢复 | `qingtian.py recover` (通常由 crash-recover.sh 调用) |

### 调用规则

- **产生决策后** (选定方案/确认变更/发现风险) → 立即 `learn` 提交经验
  ```bash
  echo "决策：选定B供应商，理由：价格低15%且交货周期短" | qingtian.py learn
  ```
- **学到通用规则** (如 "X类供应商普遍Y特征") → `remember` 写语义记忆
  ```bash
  QINGTIAN_MEM_TYPE=semantic qingtian.py remember --file /tmp/insight.txt
  ```
- **遇到报错/意外行为** → 立即 `pitfall`
  ```bash
  qingtian.py pitfall "Redis连接超时" "采购底座无法连接管理底座Redis" high
  ```
- **开始复杂任务前** → 先 `recall` 查历史经验
- **会话结束前** → 将摘要写入 `/tmp/qingtian_session_summary.txt`，hooks 会自动提交
  ```bash
  cat > /tmp/qingtian_session_summary.txt << 'EOF'
  本日完成：
  1. 对比三家供应商报价
  2. 选定供应商A并发起合同
  3. 发现供应商B产能风险并上报
  EOF
  ```
