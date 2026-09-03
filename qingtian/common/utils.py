"""
ACSSA 智能体操作系统公共工具函数
"""

import os, json
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def utcnow() -> str:
    """兼容别名"""
    return utc_now()


def parse_ts(ts: str) -> datetime:
    """解析 ISO 时间戳"""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def load_json(path: str, default=None):
    if not os.path.isfile(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data):
    dirname = os.path.dirname(path)
    if dirname:  # 无目录部分（如 "file.json"）跳过 makedirs，避免 FileNotFoundError
        os.makedirs(dirname, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
