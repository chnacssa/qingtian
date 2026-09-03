#!/bin/bash
#=============================================================================
# ACSSA一键启动脚本 — 启动ACSSA 智能体操作系统 + 自动拉起全部 Agent
#
# 原理：
#   ACSSA启动后，羲和模块自动执行 reconciliation →
#   扫描 huanyu.agents 和 agent_processes 表 →
#   对每个已知 Agent 自动拉起（注入环境变量 + 启动进程 + 健康检查）
#
# 用法:
#   ./bootstrap.sh              # 启动ACSSA，等待就绪后输出 Agent 状态
#   ./bootstrap.sh --wait       # 额外等待所有 Agent 健康检查通过
#   ./bootstrap.sh --restart    # 先停旧进程再启动（完整重启）
#   ./bootstrap.sh --status     # 仅查看当前状态，不启动
#=============================================================================

set -euo pipefail

# ── 颜色与日志 ──────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[INFO]${NC}  $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo -e "${BLUE}─── $1 ───${NC}"; }

# ── 配置 ────────────────────────────────────────────────
QINGTIAN_HOME="${QINGTIAN_HOME:-/opt/qingtian}"
CONFIG="${QINGTIAN_HOME}/config.yaml"
MAIN_PY="${QINGTIAN_HOME}/main.py"
BASE_URL="${QINGTIAN_BASE_URL:-http://127.0.0.1:1996}"
HEALTH_URL="${BASE_URL}/health"
XIHE_STATS_URL="${BASE_URL}/v1/xihe/stats"
SERVICE_NAME="qingtian"

MODE="${1:-start}"

# ── 辅助函数 ────────────────────────────────────────────

wait_for_health() {
    local desc="$1" url="$2" max_retries="${3:-30}" delay="${4:-2}"
    log_info "等待 ${desc} 就绪..."
    for i in $(seq 1 "$max_retries"); do
        if curl -sf "$url" > /dev/null 2>&1; then
            log_info "${desc} 已就绪 (${i}次探测)"
            return 0
        fi
        sleep "$delay"
    done
    log_error "${desc} 启动超时 (${max_retries}次/${delay}s)"
    return 1
}

check_dependency() {
    local name="$1" check_cmd="$2"
    if eval "$check_cmd" > /dev/null 2>&1; then
        log_info "依赖检查: ${name} ✅"
        return 0
    else
        log_error "依赖检查: ${name} ❌ 未就绪"
        return 1
    fi
}

# ── 主流程 ──────────────────────────────────────────────

case "$MODE" in
    --status|-s)
        log_step "ACSSA运行状态"
        if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
            log_info "ACSSA服务: 运行中"
            HEALTH=$(curl -sf "$HEALTH_URL" 2>/dev/null || echo '{"status":"unknown"}')
            echo "$HEALTH" | python3 -m json.tool 2>/dev/null || echo "$HEALTH"

            if STATS=$(curl -sf "$XIHE_STATS_URL" 2>/dev/null); then
                echo ""
                log_info "羲和管控状态:"
                echo "$STATS" | python3 -m json.tool 2>/dev/null
            else
                log_warn "羲和未就绪 (可能正在启动)"
            fi
        else
            log_warn "ACSSA服务未运行"
            log_info "使用 systemctl start qingtian 启动"
            log_info "或直接运行 $0 启动"
        fi
        exit 0
        ;;

    --restart|-r)
        log_step "完整重启"
        log_info "停止ACSSA服务..."
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true

        # 等待进程完全退出
        sleep 3

        log_info "启动ACSSA服务..."
        # 继续到下面的启动流程
        ;;
esac

# ═══════════════════════════════════════════════════════════
#  启动流程
# ═══════════════════════════════════════════════════════════

echo ""
echo "=========================================="
echo "  ACSSA一键启动"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  主页: ${QINGTIAN_HOME}"
echo "=========================================="
echo ""

# ── ① 前置依赖检查 ─────────────────────────────────────
log_step "① 前置依赖检查"

check_dependency "PostgreSQL" "psql --version && pg_isready -q" || exit 1
check_dependency "Redis"      "redis-cli ping"               || log_warn "Redis 未响应，部分功能可能受限"
check_dependency "配置文件"   "test -f '${CONFIG}'"          || {
    log_error "配置文件 ${CONFIG} 不存在"
    log_info "请先创建配置文件，参考 ${QINGTIAN_HOME}/config.yaml.example"
    exit 1
}
check_dependency "ACSSA入口"   "test -f '${MAIN_PY}'"         || {
    log_error "ACSSA入口 ${MAIN_PY} 不存在"
    exit 1
}

# ── ② 启动ACSSA ──────────────────────────────────────────
log_step "② 启动ACSSA 智能体操作系统"

if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    log_warn "ACSSA服务已在运行中"
else
    # systemd 启动
    if systemctl start "$SERVICE_NAME" 2>/dev/null; then
        log_info "systemctl start ${SERVICE_NAME} 成功"
    else
        log_warn "systemctl 不可用，尝试直接启动..."
        # 降级：直接启动后台进程
        nohup python3 "$MAIN_PY" > "${QINGTIAN_HOME}/logs/boot.log" 2>&1 &
        log_info "后台进程 PID: $!"
    fi
fi

# ── ③ 等待ACSSA健康 ─────────────────────────────────────
log_step "③ 等待就绪"

wait_for_health "ACSSA 智能体操作系统" "$HEALTH_URL" 30 2 || {
    log_error "ACSSA启动失败，请检查日志: journalctl -u ${SERVICE_NAME} -n 50"
    exit 1
}

# 等待 羲和 reconciliation 完成（ACSSA健康后再等 5s 让 reconciliation 跑完）
sleep 3

# ── ④ 检查 羲和 与 Agent 状态 ──────────────────────────
log_step "④ 羲和 Agent 管控状态"

STATS=$(curl -sf "$XIHE_STATS_URL" 2>/dev/null || echo '{}')
MANAGED=$(echo "$STATS" | python3 -c "
import sys, json
try:
    s = json.load(sys.stdin)
    print(s.get('managed_agents', 'N/A'))
except:
    print('N/A')
" 2>/dev/null)

log_info "羲和已接管 Agent 数: ${MANAGED}"

if [ "$MANAGED" != "N/A" ] && [ "$MANAGED" -gt 0 ]; then
    log_info "Agent 列表:"
    curl -sf "$BASE_URL/v1/xihe/agents" 2>/dev/null | \
        python3 -c "
import sys, json
data = json.load(sys.stdin)
agents = data if isinstance(data, list) else data.get('agents', [])
for a in agents:
    aid = a.get('agent_id', a.get('id', '?'))
    st = a.get('status', '?')
    hb = a.get('last_heartbeat_at', '')[:19]
    print(f'  {aid:30s} status={st:12s} heartbeat={hb}')
" 2>/dev/null || log_info "  运行 curl -sf ${BASE_URL}/v1/xihe/agents 查看详情"
fi

# ── ⑤ 等待 Agent 健康（可选） ──────────────────────────
if [ "${MODE}" = "--wait" ] || [ "${MODE}" = "-w" ]; then
    log_step "⑤ 等待全部 Agent 健康"

    MAX_WAIT=60
    for i in $(seq 1 "$MAX_WAIT"); do
        STATS=$(curl -sf "$XIHE_STATS_URL" 2>/dev/null || echo '{}')
        UNHEALTHY=$(echo "$STATS" | python3 -c "
import sys, json
try:
    s = json.load(sys.stdin)
    unhealthy = sum(1 for a in s.get('agents', []) if a.get('status') != 'ready')
    print(unhealthy)
except:
    print(-1)
" 2>/dev/null)

        if [ "$UNHEALTHY" = "0" ]; then
            log_info "全部 Agent 已就绪!"
            break
        elif [ "$UNHEALTHY" = "-1" ]; then
            sleep 2
            continue
        fi
        if [ "$i" -eq "$MAX_WAIT" ]; then
            log_warn "等待超时，仍有 ${UNHEALTHY} 个 Agent 未就绪"
        fi
        sleep 2
    done

    echo ""
    echo "─── Agent 健康摘要 ───"
    curl -sf "$XIHE_STATS_URL" 2>/dev/null | python3 -m json.tool 2>/dev/null || true
fi

# ── 完成 ────────────────────────────────────────────────
echo ""
echo "=========================================="
echo "  ✅ ACSSA启动完成"
echo "  API 入口: ${BASE_URL}"
echo "  健康检查: ${HEALTH_URL}"
echo "  管控 Agent: ${MANAGED}"
echo "  日志查看: journalctl -u ${SERVICE_NAME} -f"
echo "=========================================="
