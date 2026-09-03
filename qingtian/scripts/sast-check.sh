#!/bin/bash
# SAST 权限一致性检查 — 本地运行脚本
# 用法: scripts/sast-check.sh [path]
# 默认扫描 osskill/implementations/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_PATH="${1:-$BASE_DIR/osskill/implementations/}"

echo "=========================================="
echo " SAST 权限一致性检查"
echo " 扫描路径: $SCAN_PATH"
echo "=========================================="

cd "$BASE_DIR"

# 1. 运行 SAST 扫描
echo ""
echo "--- 1/3: SAST 静态分析 ---"
python -m osskill.sast "$SCAN_PATH" || true

# 2. 检查敏感 API
echo ""
echo "--- 2/3: 敏感 API 检查 ---"
BANNED_PATTERNS=(
    "subprocess\.call"
    "subprocess\.Popen"
    "os\.system"
    "eval("
    "exec("
    "__import__"
    "pickle\.loads"
)
sensitive_found=0
for pattern in "${BANNED_PATTERNS[@]}"; do
    matches=$(grep -rn "$pattern" "$SCAN_PATH" \
        --include="*.py" \
        --exclude-dir="__pycache__" \
        --exclude="*_test.py" \
        --exclude="test_*.py" 2>/dev/null || true)
    if [ -n "$matches" ]; then
        echo "🔴 发现敏感 API: $pattern"
        echo "$matches"
        sensitive_found=1
    fi
done
if [ $sensitive_found -eq 0 ]; then
    echo "✅ 未发现禁止的敏感 API 调用"
fi

# 3. 检查 skill.json permissions 声明
echo ""
echo "--- 3/3: skill.json permissions 完整性 ---"
missing_perm=0
for skill_json in "$SCAN_PATH"/*/skill.json; do
    if [ -f "$skill_json" ]; then
        perm_count=$(python3 -c "
import json
try:
    with open('$skill_json') as f:
        data = json.load(f)
    perms = data.get('permissions', [])
    print(len(perms))
except Exception:
    print('error')
" 2>/dev/null)
        if [ "$perm_count" = "error" ]; then
            echo "⚠️  解析失败: $skill_json"
        elif [ "$perm_count" -eq 0 ]; then
            echo "🟡 权限声明为空: $(basename $(dirname $skill_json))"
            missing_perm=1
        fi
    fi
done
if [ $missing_perm -eq 0 ]; then
    echo "✅ 所有 Skill 已声明 permissions"
fi

echo ""
echo "=========================================="
echo " 检查完成"
echo "=========================================="
