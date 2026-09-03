#!/bin/bash
# ACSSA 底座 — Agent 崩溃恢复脚本
# 部署路径：/root/.openclaw/workspace/skills/qingtian/crash-recover.sh
#
# 用法（从 Agent 目录执行）：
#   cd /root/.openclaw/workspace/agents/<agent-name>
#   ../../skills/qingtian/crash-recover.sh
#
# 或显式指定 Agent 目录：
#   crash-recover.sh --agent-dir /root/.openclaw/workspace/agents/<agent-name>
set -e

SKILLS_DIR="/root/.openclaw/workspace/skills/qingtian"
ENV_FILE=""

# 解析 --agent-dir 参数
AGENT_DIR_ARG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent-dir)
            AGENT_DIR_ARG="$2"
            shift 2
            ;;
        *)
            echo "未知参数: $1"
            echo "用法: crash-recover.sh [--agent-dir <path>]"
            exit 1
            ;;
    esac
done

# 1. 定位 qingtian.env
if [ -n "$AGENT_DIR_ARG" ]; then
    ENV_FILE="${AGENT_DIR_ARG}/qingtian.env"
elif [ -f "./qingtian.env" ]; then
    # 当前目录（常见：cd 到 Agent 目录后执行）
    ENV_FILE="./qingtian.env"
else
    # fallback：从 CWD 路径推断（如果 CWD 在 skills/ 下）
    CWD="$(pwd)"
    if echo "$CWD" | grep -q '/agents/'; then
        AGENT_DIR="$(echo "$CWD" | sed 's|/skills/qingtian.*||; s|/workspace.*||')"
        for f in "$CWD"/*/qingtian.env "$(dirname "$CWD")/agents"/*/qingtian.env; do
            if [ -f "$f" ]; then
                ENV_FILE="$f"
                break
            fi
        done
    fi
fi

if [ -z "$ENV_FILE" ] || [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: 找不到 qingtian.env"
    echo "  请从 Agent 目录执行: cd ~/.openclaw/workspace/agents/<name> && ../../skills/qingtian/crash-recover.sh"
    echo "  或显式指定: crash-recover.sh --agent-dir ~/.openclaw/workspace/agents/<name>"
    exit 1
fi

source "$ENV_FILE" 2>/dev/null || {
    echo "ERROR: 无法加载 $ENV_FILE"
    exit 1
}

echo "=== Agent ${AGENT_ID} 崩溃恢复 ==="
echo "底座: ${QINGTIAN_HOST}"
echo "Env:  ${ENV_FILE}"
echo ""

# 2. 健康检查
if ! python3 "$SKILLS_DIR/qingtian.py" health > /dev/null 2>&1; then
    echo "ERROR: 底座 ${QINGTIAN_HOST} 不可达"
    exit 1
fi
echo "✅ 底座可达"

# 3. 幂等注册
python3 "$SKILLS_DIR/qingtian.py" register > /dev/null 2>&1
echo "✅ 已注册"

# 4. 获取恢复数据
RECOVER_DATA=$(python3 "$SKILLS_DIR/qingtian.py" recover "$AGENT_ID" 2>&1) || {
    echo "ERROR: 恢复请求失败"
    echo "$RECOVER_DATA"
    exit 1
}

# 5. 检查是否返回错误
if echo "$RECOVER_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(1 if d.get('error') else 0)" 2>/dev/null; then
    echo "⚠️  恢复请求返回异常"
    echo "$RECOVER_DATA"
else
    TOTAL=$(echo "$RECOVER_DATA" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_recovered',0))" 2>/dev/null || echo "0")
    echo "✅ 恢复 ${TOTAL} 条记忆"

    # 6. 打印最后会话摘要
    echo "$RECOVER_DATA" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ls = d.get('last_session')
if ls:
    print()
    print('=== 上次会话摘要 ===')
    print(ls.get('content','')[:300])
" 2>/dev/null || true

    # 7. 列出最近记忆
    echo ""
    echo "=== 最近 5 条记忆 ==="
    echo "$RECOVER_DATA" | python3 -c "
import sys, json
for m in json.load(sys.stdin).get('recent_memories',[])[:5]:
    print(f\"  [{m.get('memory_type','')}] {m.get('content','')[:120]}\")
" 2>/dev/null || true

    # 8. 打印上次工作状态
    echo "$RECOVER_DATA" | python3 -c "
import sys, json
d = json.load(sys.stdin)
p = d.get('profile') or {}
state = p.get('state', {})
if state:
    print()
    print('=== 上次工作状态 ===')
    for k,v in state.items():
        print(f'  {k}: {v}')
" 2>/dev/null || true
fi

# 9. 启动恢复会话
python3 "$SKILLS_DIR/qingtian.py" session-start "崩溃恢复完成 — Agent 重新上线，继续之前工作"
echo ""
echo "=== 恢复完成 ==="
