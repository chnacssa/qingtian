"""
底座 OS 主配置模块
读取 /opt/qingtian/config.yaml，提供全局配置访问

支持 config.local.yaml 覆盖：同目录下的 config.local.yaml 会深度合并到主配置之上，
用于每台服务器独立设置 role/host/peer_id 等差异项，rsync 时排除此文件。
"""

import os, yaml
from typing import Any, Dict, Optional

_CONFIG_PATH = os.environ.get("QINGTIAN_CONFIG", "/opt/qingtian/config.yaml")
_LOCAL_CONFIG_PATH = os.path.join(os.path.dirname(_CONFIG_PATH), "config.local.yaml")
_config_cache: Optional[Dict] = None


def _deep_merge(base: dict, overlay: dict) -> dict:
    """深度合并两个 dict：overlay 的值覆盖 base，嵌套 dict 递归合并"""
    result = dict(base)
    for key, val in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _load(force: bool = False) -> Dict:
    global _config_cache
    if _config_cache is None or force:
        cfg = {}
        try:
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except FileNotFoundError:
            cfg = {}

        # 加载本地覆盖配置（每台服务器独立，rsync 排除）
        # config.local.yaml 格式：{ override: { role: ..., host: ..., ... } }
        try:
            with open(_LOCAL_CONFIG_PATH, encoding="utf-8") as f:
                local = yaml.safe_load(f) or {}
        except FileNotFoundError:
            local = {}
        cfg = _deep_merge(cfg, local.get("override", {}))

        _config_cache = cfg
    return _config_cache


def get(key: str, default: Any = None) -> Any:
    """获取配置值，支持点路径: 'xixing.collect_enabled'"""
    cfg = _load()
    parts = key.split(".")
    for p in parts:
        if isinstance(cfg, dict):
            cfg = cfg.get(p)
        else:
            return default
    return cfg if cfg is not None else default


def get_role() -> str:
    return get("role", "company")


def get_host() -> str:
    import socket
    return get("host", socket.gethostname())


def is_management() -> bool:
    return get_role() == "management"


def collect_enabled() -> bool:
    return is_management()


def scan_enabled() -> bool:
    return is_management()


def get_global_namespace() -> str:
    return get("xixing.global_namespace", "global")


def reload() -> bool:
    try:
        _load(force=True)
        return True
    except Exception:
        return False


# ── 全局 LLM 厂商顺序（波哥 2026-08-27 定调）──────────────────
# FIRST_LLM / SECOND_LLM 两个环境变量统一所有组件的 LLM 厂商选择，不硬编码
# 绑死一家，不管用户用什么模型都按这个顺序读：
#   ① FIRST_LLM 指定厂商（值=zhipu/deepseek，大小写不敏感，须配对应 key）
#   ② SECOND_LLM 兜底（FIRST 指的厂商没配 key 时用）
#   ③ 都没配 → 智谱优先（仅 DEEPSEEK_API_KEY 时走 deepseek）
# 各模块 config.yaml key（common.llm.model / xixing.classifier.model 等）显式
# 配置仍最优先——本节只替换原硬编码默认值，供 common/llm、xixing、yongheng、
# huichuan、zhice、setup 统一引用（Skill 层独立部署不 import common，
# skills/bidding/domain/config.py 有同语义副本 _llm_profile）。

_LLM_PROVIDER_PROFILES: Dict[str, Dict[str, str]] = {
    # provider 名 → 档案（模型/端点/key 环境变量三者同源，
    # 不会拿 A 家 key 打 B 家端点）
    "zhipu": {
        "provider": "zhipu",
        "model": "glm-5.3-flash",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "key_var": "ZHIPU_API_KEY",
    },
    "deepseek": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com/v1",
        "key_var": "DEEPSEEK_API_KEY",
    },
}


def _active_llm_profile() -> Dict[str, str]:
    """当前生效的主 LLM 厂商档案（FIRST_LLM > SECOND_LLM > 智谱优先自动推导）。"""
    for var in ("FIRST_LLM", "SECOND_LLM"):
        profile = _LLM_PROVIDER_PROFILES.get((os.environ.get(var) or "").strip().lower())
        if profile and os.environ.get(profile["key_var"]):
            return profile
    if os.environ.get("ZHIPU_API_KEY"):
        return _LLM_PROVIDER_PROFILES["zhipu"]
    if os.environ.get("DEEPSEEK_API_KEY"):
        return _LLM_PROVIDER_PROFILES["deepseek"]
    return _LLM_PROVIDER_PROFILES["zhipu"]


def default_llm_model() -> str:
    """全局默认 LLM 模型名（随 FIRST_LLM/SECOND_LLM，智谱优先）。"""
    return _active_llm_profile()["model"]


def default_llm_base_url() -> str:
    """全局默认 LLM 端点（与 default_llm_model 同档案）。"""
    return _active_llm_profile()["base_url"]


def default_llm_provider() -> str:
    """全局默认 LLM 厂商名（与 default_llm_model 同档案）。"""
    return _active_llm_profile()["provider"]


def default_llm_key_var() -> str:
    """当前主 LLM 档案的 key 环境变量名（ZHIPU_API_KEY / DEEPSEEK_API_KEY）。"""
    return _active_llm_profile()["key_var"]


def default_llm_backup_profile() -> Optional[Dict[str, str]]:
    """备用 LLM 厂商档案：SECOND_LLM 指定且 ≠ 主厂商且配了 key 优先；
    否则另一家（配了 key 才返回；两家 key 都没配 → None）。"""
    active = _active_llm_profile()
    second = _LLM_PROVIDER_PROFILES.get((os.environ.get("SECOND_LLM") or "").strip().lower())
    if second and second is not active and os.environ.get(second["key_var"]):
        return second
    for profile in _LLM_PROVIDER_PROFILES.values():
        if profile is not active and os.environ.get(profile["key_var"]):
            return profile
    return None
