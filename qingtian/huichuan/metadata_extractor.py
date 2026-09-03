"""汇川元数据提取器 — 图片/视频/CAD → file_registry.metadata

Phase 4 模块。对当前管道无法处理的格式（图片、视频、CAD 等），
提取基本元数据存入 file_registry.metadata，标记 future_processable。

不作为独立的类，而是提供纯函数分发表，供 ingest.py 兜底分支调用。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("huichuan.metadata_extractor")


def extract_image_meta(data: bytes, filename: str) -> dict:
    """图片 → 提取尺寸/格式/色彩模式。"""
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(data))
        meta: dict = {
            "width": img.width,
            "height": img.height,
            "format": img.format,
            "mode": img.mode,
        }
        img.close()
        return meta
    except Exception as e:
        logger.debug("Image meta extract failed: %s", e)
        return {}


def extract_fallback(data: bytes, filename: str) -> dict:
    """未知格式 → 标记暂不可处理。"""
    return {"future_processable": True, "unknown_format": True}


# 分类 → 提取函数 分发表
EXTRACTORS: dict[str, callable] = {
    "image": extract_image_meta,
}
