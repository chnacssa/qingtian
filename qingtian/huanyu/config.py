"""
寰宇配置 — 从 qingtian/config.yaml 读取 huanyu 相关配置
"""

import os

from common.config import get


def get_schema_name() -> str:
    return get("huanyu.schema_name", "huanyu")


def get_redis_url() -> str:
    # QINGTIAN_REDIS_URL 环境变量优先（Docker compose 一键部署传 redis://redis:6379；裸机无需设置）
    return os.getenv("QINGTIAN_REDIS_URL", get("huanyu.redis_url", "redis://localhost:6379"))


def get_redis_password() -> str:
    return os.getenv("HUANYU_REDIS_PASSWORD", get("huanyu.redis_password", ""))


def get_peer_id() -> str:
    return get("host", "unknown")


def get_peer_name() -> str:
    return get("host", "unknown")


def get_peer_port() -> int:
    return get("service.port", 1996)


def get_organization() -> str:
    """QACP 组织标识，用于 AIN 生成"""
    return get("organization", "acssa")


def get_country() -> str:
    """ISO 3166-1 alpha-2 国家码"""
    return get("country", "cn")


def get_city() -> str:
    """城市码（小写字母）"""
    return get("city", "hf")


def get_msg_sign_key() -> str:
    """消息签名密钥，优先读环境变量"""
    return os.getenv("HUANYU_SIGN_KEY", get("huanyu.sign_key", ""))


def get_max_counters() -> int:
    return get("huanyu.max_counters", 5)


def get_negotiation_expire_days() -> int:
    return get("huanyu.negotiation_expire_days", 7)


def get_heartbeat_interval() -> int:
    return get("huanyu.heartbeat_interval_seconds", 300)


def get_heartbeat_timeout() -> int:
    """连续多少次无心跳后标记 inactive"""
    return get("huanyu.heartbeat_miss_threshold", 3)


def get_hub_endpoint() -> str:
    """管理底座 HTTP 地址。

    B5 (R11): 不再自动发现/默认写死 WG 网段 Hub（10.0.100.1 泄露内网拓扑）。
    必须显式配置 huanyu.hub_endpoint 或环境变量 QINGTIAN_HUB_HOST，否则返回空。
    """
    endpoint = get("huanyu.hub_endpoint", "")
    if endpoint:
        return endpoint
    wg_hub = os.environ.get("QINGTIAN_HUB_HOST", "")
    if not wg_hub:
        return ""
    hub_port = os.environ.get("QINGTIAN_HUB_PORT", "1996")
    return f"http://{wg_hub}:{hub_port}"


def get_hub_relay_enabled() -> bool:
    """是否通过管理服 Hub 中转跨底座消息（解决容器无法直连其他 WG IP 的问题）"""
    return get("huanyu.hub_relay", False)


def get_global_root_url() -> str:
    """Global Root 解析器 URL"""
    return get("huanyu.global_root_url", "https://ain.acssa.cn")


def get_resolver_scope() -> str:
    """本解析器授权的国家范围（ISO 3166-1 alpha-2 逗号分隔）"""
    return get("huanyu.resolver_scope", get_country().upper())


def get_ain_key_layer2() -> str:
    """AIN 内部层 AES-256-GCM 密钥（32 字节），跨底座共享"""
    return os.getenv("AIN_LAYER2_KEY", get("huanyu.ain_key_layer2", ""))


def get_ain_key_layer3() -> str:
    """AIN 私有层 AES-256-GCM 密钥（32 字节），仅锚点持有"""
    return os.getenv("AIN_LAYER3_KEY", get("huanyu.ain_key_layer3", ""))


def get_org_id() -> str:
    """本底座企业码。跨企业未接入时返回 ''（全部走原有内部/WG 路由）。"""
    return get("huanyu.organization_id", "")


def get_cross_org_enabled() -> bool:
    """跨企业通讯开关：配置了企业码且未显式关闭才启用。"""
    return bool(get_org_id()) and get("huanyu.cross_org", True)


def get_hub_ws_url() -> str:
    """企业端连 Hub 的 wss 地址（hub_endpoint http(s)→ws(s)）。"""
    hub = get_hub_endpoint()
    if not hub:
        return ""
    return hub.replace("http://", "ws://").replace("https://", "wss://")


def get_org_sign_key() -> str:
    """企业 Ed25519 签名私钥（hex），来自本地密钥文件/环境变量，不落 git。"""
    return os.getenv("HUANYU_ORG_SIGN_KEY", get("huanyu.org_sign_key", ""))


def get_org_static_priv() -> bytes | None:
    """企业 X25519 长期静态私钥（离线消息解密用），hex 配置。"""
    v = os.getenv("HUANYU_ORG_STATIC_PRIV", get("huanyu.org_static_priv", ""))
    return bytes.fromhex(v) if v else None


def get_org_token() -> str:
    """企业连 Hub 的认证 token（Hub 签发：org_id.expiry.sig），不落 git。"""
    return os.getenv("HUANYU_ORG_TOKEN", get("huanyu.org_token", ""))


def get_base_ip_map() -> dict:
    """底座权威 IP 映射（server_host → WG IP）。

    B5 (R11): 不再源码硬编码内网拓扑，改由部署方显式注入：
      1. 环境变量 QINGTIAN_BASE_IP_MAP（JSON 对象）
      2. config.yaml huanyu.base_ip_by_host
    未配置返回空 dict（归属校验自动跳过）。
    """
    import json as _json
    env_raw = os.environ.get("QINGTIAN_BASE_IP_MAP", "")
    if env_raw.strip():
        try:
            parsed = _json.loads(env_raw)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            logger = getattr(__import__("logging"), "getLogger")("huanyu.config")
            logger.warning("QINGTIAN_BASE_IP_MAP 不是合法 JSON 对象，已忽略")
    cfg = get("huanyu.base_ip_by_host", {})
    return cfg if isinstance(cfg, dict) else {}
