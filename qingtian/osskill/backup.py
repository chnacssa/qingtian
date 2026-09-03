"""备份与恢复 — 自动备份 Skill 包/许可证/配置 + restore CLI

备份结构（tar.gz）:
    backup/
    ├── skills/              # Skill 包文件
    │   ├── bidding/         # 每个 Skill 一个目录
    │   │   ├── skill.json
    │   │   └── main.py
    │   └── ...
    ├── licenses/            # License 文件
    │   ├── bidding.license
    │   └── ...
    ├── config.yaml           # 当前配置
    └── manifest.json         # 备份元数据（时间戳、版本、sha256）
"""

import json
import logging
import os
import shutil
import tarfile
import tempfile
import time
from datetime import datetime, timezone

from common.config import get as config_get
from common.crypto import sha256

logger = logging.getLogger("osskill.backup")

BACKUP_DIR = config_get("skill.backup_dir", "/opt/qingtian/backups/skills")
MAX_BACKUPS = 10  # 保留最近 10 个备份


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _manifest_path() -> str:
    return os.path.join(BACKUP_DIR, "manifest.json")


# ── 备份 ──


async def create_backup(
    skill_data_dir: str = "",
    config_path: str = "",
    output_dir: str = BACKUP_DIR,
) -> str:
    """创建完整备份

    Args:
        skill_data_dir: Skill 数据目录（包含 license 文件）
        config_path: config.yaml 路径
        output_dir: 备份输出目录

    Returns:
        备份文件路径
    """
    os.makedirs(output_dir, exist_ok=True)
    timestamp = _ts()
    backup_name = f"skill_backup_{timestamp}.tar.gz"
    backup_path = os.path.join(output_dir, backup_name)

    # 收集备份清单
    manifest = {
        "backup_name": backup_name,
        "created_at": timestamp,
        "created_at_epoch": int(time.time()),
        "sha256": "",
        "contents": {
            "skills": [],
            "licenses": [],
            "config": "",
        },
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        skills_dir = os.path.join(tmpdir, "skills")
        licenses_dir = os.path.join(tmpdir, "licenses")
        os.makedirs(skills_dir)
        os.makedirs(licenses_dir)

        # 1. 备份 Skill 包
        if skill_data_dir:
            for entry in os.listdir(skill_data_dir):
                entry_path = os.path.join(skill_data_dir, entry)
                if os.path.isdir(entry_path):
                    skill_json = os.path.join(entry_path, "skill.json")
                    if os.path.isfile(skill_json):
                        shutil.copytree(entry_path, os.path.join(skills_dir, entry))
                        manifest["contents"]["skills"].append(entry)

        # 2. 备份 License 文件
        if skill_data_dir:
            for fname in os.listdir(skill_data_dir):
                if fname.endswith(".license"):
                    shutil.copy2(
                        os.path.join(skill_data_dir, fname),
                        os.path.join(licenses_dir, fname),
                    )
                    manifest["contents"]["licenses"].append(fname)

        # 3. 备份配置
        if config_path and os.path.isfile(config_path):
            shutil.copy2(config_path, os.path.join(tmpdir, "config.yaml"))
            manifest["contents"]["config"] = "config.yaml"

        # 4. 写 manifest
        manifest_path = os.path.join(tmpdir, "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        # 5. 打包
        with tarfile.open(backup_path, "w:gz") as tar:
            tar.add(tmpdir, arcname="backup")

    # 计算备份文件 sha256
    with open(backup_path, "rb") as f:
        manifest["sha256"] = sha256(f.read())

    # 写 manifest 到备份目录
    _update_manifest(manifest)

    # 清理旧备份
    _cleanup_old_backups(output_dir)

    logger.info("Backup created: %s (%s)", backup_path, manifest["sha256"][:16])
    return backup_path


def _update_manifest(manifest: dict) -> None:
    """更新备份目录的 manifest 文件"""
    existing = []
    try:
        with open(_manifest_path()) as f:
            existing = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    if not isinstance(existing, list):
        existing = []

    existing.append(manifest)
    # 保留最近 10 条
    existing = existing[-MAX_BACKUPS:]

    with open(_manifest_path(), "w") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def _cleanup_old_backups(output_dir: str) -> None:
    """清理旧备份，只保留 MAX_BACKUPS 个"""
    backups = sorted([
        f for f in os.listdir(output_dir)
        if f.startswith("skill_backup_") and f.endswith(".tar.gz")
    ])
    while len(backups) > MAX_BACKUPS:
        old = backups.pop(0)
        try:
            os.remove(os.path.join(output_dir, old))
            logger.info("Removed old backup: %s", old)
        except OSError as e:
            logger.warning("Failed to remove old backup %s: %s", old, e)


# ── 恢复 ──


def restore_backup(
    backup_path: str,
    skill_data_dir: str = "",
    config_path: str = "",
) -> dict:
    """从备份恢复

    Args:
        backup_path: 备份文件路径
        skill_data_dir: Skill 数据恢复目标目录
        config_path: 配置文件恢复目标路径

    Returns:
        恢复摘要
    """
    if not os.path.isfile(backup_path):
        raise FileNotFoundError(f"Backup not found: {backup_path}")

    summary = {
        "backup": backup_path,
        "restored_skills": [],
        "restored_licenses": [],
        "restored_config": False,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        with tarfile.open(backup_path, "r:gz") as tar:
            tar.extractall(tmpdir, filter="data")

        base = os.path.join(tmpdir, "backup")

        # 1. 恢复 Skill 包
        skills_src = os.path.join(base, "skills")
        if os.path.isdir(skills_src) and skill_data_dir:
            for entry in os.listdir(skills_src):
                src = os.path.join(skills_src, entry)
                dst = os.path.join(skill_data_dir, entry)
                if os.path.isdir(src):
                    if os.path.exists(dst):
                        shutil.rmtree(dst)
                    shutil.copytree(src, dst)
                    summary["restored_skills"].append(entry)

        # 2. 恢复 License
        licenses_src = os.path.join(base, "licenses")
        if os.path.isdir(licenses_src) and skill_data_dir:
            for fname in os.listdir(licenses_src):
                if fname.endswith(".license"):
                    shutil.copy2(
                        os.path.join(licenses_src, fname),
                        os.path.join(skill_data_dir, fname),
                    )
                    summary["restored_licenses"].append(fname)

        # 3. 恢复配置
        config_src = os.path.join(base, "config.yaml")
        if os.path.isfile(config_src) and config_path:
            shutil.copy2(config_src, config_path)
            summary["restored_config"] = True

        # 4. 验证完整性
        manifest_src = os.path.join(base, "manifest.json")
        if os.path.isfile(manifest_src):
            with open(manifest_src) as f:
                manifest = json.load(f)
            summary["original_sha256"] = manifest.get("sha256", "")

    logger.info(
        "Restored from %s: %d skills, %d licenses",
        backup_path,
        len(summary["restored_skills"]),
        len(summary["restored_licenses"]),
    )
    return summary


# ── 备份列表 ──


def list_backups(output_dir: str = BACKUP_DIR) -> list[dict]:
    """列出所有可用备份"""
    backups = []
    try:
        with open(_manifest_path()) as f:
            backups = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # 补充实际文件存在性检查
    result = []
    for b in backups:
        name = b.get("backup_name", "")
        path = os.path.join(output_dir, name)
        b["exists"] = os.path.isfile(path)
        b["size_bytes"] = os.path.getsize(path) if b["exists"] else 0
        result.append(b)

    return result
