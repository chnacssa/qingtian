"""镇岳 — 配置自动备份。

写配置前自动备份到 /opt/qingtian/backups/ 目录。
保留最近 MAX_VERSIONS 个版本，自动清理旧版本。
"""

import logging
import os
import shutil
from datetime import datetime
from typing import Optional

logger = logging.getLogger("zhenyue.backup")

BACKUP_BASE = "/opt/qingtian/backups"
MAX_VERSIONS = 30
PROTECTED_PATHS = ["/opt/qingtian/config.yaml", "/opt/qingtian/tool-rules.yaml"]


def backup_file(path: str) -> str:
    """写配置前自动备份。

    Args:
        path: 要备份的文件路径

    Returns:
        备份文件的目标路径

    Raises:
        FileNotFoundError: 如果源文件不存在
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Source file not found: {path}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = os.path.basename(path)

    dest_dir = os.path.join(BACKUP_BASE, name)
    os.makedirs(dest_dir, exist_ok=True)

    backup_filename = f"{ts}_{name}"
    backup_path = os.path.join(dest_dir, backup_filename)

    shutil.copy2(path, backup_path)
    logger.info("Backup created: %s -> %s", path, backup_path)

    # 更新 latest 软链（不支持软链的平台跳过）
    link_path = os.path.join(dest_dir, "latest")
    try:
        if os.path.islink(link_path) or os.path.exists(link_path):
            os.unlink(link_path)
        os.symlink(backup_filename, link_path)
    except (OSError, NotImplementedError) as e:
        logger.debug("Symlink creation skipped (%s)", e)

    # 清理旧版本
    _cleanup_old(name)

    return backup_path


def _cleanup_old(name: str):
    """保留最近 MAX_VERSIONS 个版本，删除超出的旧版本。"""
    dest = os.path.join(BACKUP_BASE, name)
    if not os.path.isdir(dest):
        return

    suffix = f"_{name}"
    files = sorted(
        [f for f in os.listdir(dest) if f.endswith(suffix) and os.path.isfile(os.path.join(dest, f))]
    )

    while len(files) > MAX_VERSIONS:
        old = files.pop(0)
        old_path = os.path.join(dest, old)
        try:
            os.remove(old_path)
            logger.debug("Removed old backup: %s", old_path)
        except OSError as e:
            logger.warning("Failed to remove old backup %s: %s", old_path, e)


def list_backups(name: str) -> list[dict]:
    """列出某文件的所有备份版本，按时间倒序。

    Args:
        name: 文件名（如 'config.yaml'）

    Returns:
        [{"filename": str, "path": str, "timestamp": str}, ...]
    """
    dest = os.path.join(BACKUP_BASE, name)
    if not os.path.isdir(dest):
        return []

    suffix = f"_{name}"
    files = sorted(
        [f for f in os.listdir(dest) if f.endswith(suffix) and os.path.isfile(os.path.join(dest, f))],
        reverse=True,
    )

    result = []
    for f in files:
        result.append({
            "filename": f,
            "path": os.path.join(dest, f),
            "timestamp": f[:-(len(name) + 1)],  # 去掉 "_{name}" 后缀得到完整时间戳
        })
    return result
