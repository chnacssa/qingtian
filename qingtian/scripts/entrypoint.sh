#!/bin/bash
# ACSSA Docker 入口点 — 确保配置就绪后启动主服务
set -e

CONFIG="${QINGTIAN_CONFIG:-/app/qingtian/config.yaml}"
EXAMPLE="${CONFIG}.example"

# 如果 config.yaml 不存在，从 example 复制
if [ ! -f "$CONFIG" ]; then
    if [ -f "$EXAMPLE" ]; then
        cp "$EXAMPLE" "$CONFIG"
        echo "[entrypoint] 已从 $(basename "$EXAMPLE") 生成默认配置文件"
        echo "[entrypoint] 请编辑 $CONFIG 配置 DEEPSEEK_API_KEY 等必填项"
    else
        echo "[entrypoint] 错误: 找不到配置文件模板 $EXAMPLE"
        exit 1
    fi
fi

# 检查必填环境变量
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
    echo -e "\033[1;33m⚠️  DEEPSEEK_API_KEY 未设置，LLM 功能将不可用\033[0m"
fi

exec python3 main.py
