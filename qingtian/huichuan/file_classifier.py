"""汇川文件分类器 — MIME 魔数检测

Phase 1 模块。用魔数（magic bytes）识别文件真实格式，不信任扩展名。
主要用于 file_classifier.classify() → FileCategory，供 ingest 管道判断处理策略。

与 _extract_text() (ingest.py) 的关系：
  - _extract_text 按扩展名分发文本提取（已有功能，不改动）
  - file_classifier 按魔数分类文件（新增安全检测层）
  两者功能不同，不重叠不冲突。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("huichuan.file_classifier")

# ── 魔数签名表 ──────────────────────────────────────────
# (魔数字节, 格式名, MIME, 分类)
_MAGIC_SIGNATURES: list[tuple[bytes, str, str, str]] = [
    (b"%PDF",              "pdf",  "application/pdf",                                                    "document"),
    (b"\x89PNG\r\n\x1a\n", "png",  "image/png",                                                         "image"),
    (b"\xff\xd8\xff",      "jpg",  "image/jpeg",                                                        "image"),
    (b"GIF8",              "gif",  "image/gif",                                                          "image"),
    (b"RIFF",              "webp", "image/webp",                                                         "image"),
    (b"PK",                "zip",  "application/zip",                                                    "archive"),
]

# ZIP 容器内子类型（通过扩展名辅助判定）
_ZIP_SUBTYPES: dict[str, tuple[str, str, str]] = {
    ".docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "document"),
    ".xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",       "spreadsheet"),
    ".pptx": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "presentation"),
}

# 扩展名兜底表（仅魔数未命中时使用）
_EXT_FALLBACK: dict[str, tuple[str, str, str]] = {
    ".txt":  ("txt",  "text/plain",           "text"),
    ".md":   ("md",   "text/markdown",        "text"),
    ".json": ("json", "application/json",     "text"),
    ".csv":  ("csv",  "text/csv",             "text"),
}

# 可处理分类白名单（当前管道能处理的分类）
_PROCESSABLE_CATEGORIES = frozenset({"text", "document", "spreadsheet"})


class FileCategory:
    """文件分类结果。

    Attributes:
        fmt: 格式名（pdf/docx/xlsx/png/jpg/zip/txt/…）
        mime: MIME 类型
        category: 大类（text/document/spreadsheet/image/archive/binary）
        processable: 当前管道是否能处理
    """
    def __init__(self, fmt: str, mime: str, category: str):
        self.fmt = fmt
        self.mime = mime
        self.category = category
        self.processable = category in _PROCESSABLE_CATEGORIES

    @property
    def future_processable(self) -> bool:
        """未来多模态能力上线后可处理（当前仅 image 类）。"""
        return self.category == "image"

    def __repr__(self) -> str:
        return f"FileCategory({self.fmt}, {self.mime}, cat={self.category})"


def classify(data: bytes, filename: str = "") -> FileCategory:
    """检测文件格式。魔数优先，扩展名兜底。

    Args:
        data: 文件原始字节（至少需前 8 字节，完整文件更可靠）
        filename: 原始文件名（用于扩展名兜底和 ZIP 子类型判断）

    Returns:
        FileCategory 实例，含格式名/MIME/分类/可处理性
    """
    if not data:
        return FileCategory("empty", "application/x-empty", "binary")

    for sig, fmt, mime, category in _MAGIC_SIGNATURES:
        if len(data) >= len(sig) and data[:len(sig)] == sig:
            if fmt == "zip":
                return _classify_zip(filename)
            return FileCategory(fmt, mime, category)

    # 魔数未命中 → 扩展名兜底
    ext = _get_ext(filename)
    if ext in _EXT_FALLBACK:
        fmt, mime, category = _EXT_FALLBACK[ext]
        return FileCategory(fmt, mime, category)

    return FileCategory("unknown", "application/octet-stream", "binary")


def _classify_zip(filename: str) -> FileCategory:
    """ZIP 容器 — 通过扩展名判断 Office 子类型。"""
    ext = _get_ext(filename)
    if ext in _ZIP_SUBTYPES:
        fmt, mime, category = _ZIP_SUBTYPES[ext]
        return FileCategory(fmt, mime, category)
    return FileCategory("zip", "application/zip", "archive")


def _get_ext(filename: str) -> str:
    """提取小写扩展名（含点号）。"""
    idx = filename.rfind(".")
    return filename[idx:].lower() if idx >= 0 else ""
