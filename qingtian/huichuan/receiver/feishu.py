"""汇川飞书文件接收器 — Phase 2

飞书消息 webhook → download_resource → 文件存储 → ingest_text 入库。

详细流程:
  1. 飞书 event webhook → message.file 事件
  2. download_resource(file_key, file_type) → bytes
  3. 保存到 /opt/qingtian/huichuan/storage/{yyyy}/{mm}/{uuid}.{ext}
  4. pdfplumber/docx/openpyxl 提取文本
  5. huichuan.ingest.ingest_text() 入库
  6. 回复飞书消息: "文件已收到，预计 X 分钟后可搜索"

文件大小策略:
  - ≤ 50MB: 直接提取文本 → LLM 编译入库（ingest_file 内部按 MAX_CHUNK_CHARS 截断）
  - > 50MB: 拒绝（超飞书 download_resource 上限）
"""

import asyncio
import hashlib
import logging
import os
import re
import uuid
from datetime import date, datetime, timezone

from huichuan.ingest import ingest_file

logger = logging.getLogger("huichuan.receiver.feishu")

# 飞书 download_resource API 限制
FEISHU_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
# 汇川直接处理阈值
DIRECT_INGEST_MAX_SIZE = 10 * 1024 * 1024  # 10MB

# 支持的飞书文件类型
SUPPORTED_FILETYPES = {"pdf", "docx", "xlsx", "txt", "md"}


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


async def handle_feishu_file_event(
    conn,
    event: dict,
    feishu_client,
    storage_base: str = "/opt/qingtian/huichuan/storage",
    schema: str = "huichuan",
) -> dict:
    """处理飞书文件消息事件。

    Args:
        conn: asyncpg connection
        event: 飞书事件 payload（含 file_key, file_type, file_name, file_size）
        feishu_client: 飞书 SDK client（需有 download_resource 方法）
        storage_base: Layer 1 存储根目录
        schema: 数据库 schema 名

    Returns:
        {"action": "ingested"|"queued"|"skipped"|"error", ...}
    """
    file_key = event.get("file_key", "")
    file_type = event.get("file_type", "").lower()
    file_name = event.get("file_name", "unknown")
    file_size = event.get("file_size", 0)

    if file_type not in SUPPORTED_FILETYPES:
        return {
            "action": "skipped",
            "reason": f"unsupported file type: {file_type}",
            "file_name": file_name,
        }

    if file_size > FEISHU_MAX_FILE_SIZE:
        return {
            "action": "skipped",
            "reason": f"file too large: {file_size} > {FEISHU_MAX_FILE_SIZE}",
            "file_name": file_name,
        }

    # 下载文件
    try:
        file_bytes = await feishu_client.download_resource(file_key, file_type)
    except Exception as e:
        logger.exception("Failed to download feishu file %s", file_key)
        return {
            "action": "error",
            "reason": f"download failed: {e}",
            "file_name": file_name,
        }

    # 校验完整性
    actual_size = len(file_bytes)
    sha256_hash = hashlib.sha256(file_bytes).hexdigest()

    # 保存到 Layer 1（I/O 走线程池避免阻塞事件循环）
    # 注意: ingest_file 内部也会保存文件到 Layer 1，此处只做飞书侧备份登记，
    # 文件注册表以 ingest_file 的 storage_path 为准
    today = date.today()
    storage_dir = os.path.join(storage_base, str(today.year), f"{today.month:02d}")
    await asyncio.to_thread(os.makedirs, storage_dir, exist_ok=True)

    file_id = uuid.uuid4().hex
    clean_name = _safe_filename(file_name)
    backup_path = os.path.join(storage_dir, f"{file_id}_{clean_name}")

    # P1 (R?): 防御性包含校验 —— file_name 来自飞书事件，理论可含 ../ 穿越；
    # 清洗后再验 resolved 路径仍落在 storage_dir 内，双保险。
    if not _os_path_contained(backup_path, storage_dir):
        logger.warning("file_name 含路径穿越被拒: %s", file_name)
        return {"action": "skipped", "reason": "invalid file name", "file_name": file_name}

    await asyncio.to_thread(_write_file, backup_path, file_bytes)

    # 10-50MB: 直接提取文本并入库（ingest_file 内部会按 MAX_CHUNK_CHARS 截断）。
    # P1 (R?): 原实现此处返回 "queued" 但从未有异步队列消费 → 该区间文件永不入库。
    # 修复：取消假排队，≤50MB 一律同步摄入。
    # 直接用 bytes + filename 调用 ingest_file
    result = await ingest_file(
        conn,
        file_bytes,
        clean_name,
        source="feishu",
        storage_base=storage_base,
        schema=schema,
    )

    return {
        "action": "ingested",
        "file_name": file_name,
        "file_size": actual_size,
        "storage_path": result.get("storage_path", backup_path),
        "sha256": sha256_hash,
        "entries": result.get("entries", 0),
        "knowledge_ids": result.get("knowledge_ids", []),
        "images_registered": result.get("images_registered", 0),
        "xlsx_sheets": result.get("xlsx_sheets", 0),
    }


def _write_file(path: str, data: bytes) -> None:
    """同步写文件。"""
    with open(path, "wb") as f:
        f.write(data)


def _safe_filename(name: str) -> str:
    """清洗飞书事件下发的 file_name，去除路径分隔符与穿越片段。

    只保留中英文、数字、点、横线、下划线，其余一律替换为下划线。
    """
    base = os.path.basename(name.replace("\\", "/"))
    base = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", base).strip(" .")
    return base or "file"


def _os_path_contained(path: str, parent_dir: str) -> bool:
    """校验解析后的绝对路径仍位于父目录内（防 ../ 穿越）。"""
    try:
        resolved = os.path.realpath(path)
    except Exception:
        return False
    return resolved.startswith(os.path.realpath(parent_dir) + os.sep)
