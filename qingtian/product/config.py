"""
产品目录模块 — 配置适配层

通过环境变量配置，不依赖 common.config。

    QINGTIAN_PRODUCT_SCHEMA   schema 名称，默认 "product"
    QINGTIAN_API_URL          ACSSA 底座 API 地址，默认 "http://127.0.0.1:1996"
"""

import os


def get_schema_name() -> str:
    return os.environ.get("QINGTIAN_PRODUCT_SCHEMA", "product")


def get_internal_file_url(file_id: str) -> str:
    """构建内部文件下载 URL（本服务进程内 file_service 下载）。"""
    base = os.environ.get("QINGTIAN_API_URL", "http://127.0.0.1:1996")
    return f"{base.rstrip('/')}/v1/files/{file_id}/download"
