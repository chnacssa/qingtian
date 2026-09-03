"""
网关配置
"""

from common.config import get


def get_gateway_port() -> int:
    return get("service.port", 1996)


def get_yongheng_url() -> str:
    return get("yongheng.url", "http://localhost:1995")


def get_role() -> str:
    return get("role", "company")


def get_host() -> str:
    return get("host", "localhost")
