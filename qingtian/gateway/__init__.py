# ACSSA统一网关

# 导入 Adapter 模块触发自注册（无依赖，无副作用）
try:
    from . import adapters  # noqa: F401 — triggers adapter self-registration
except ImportError:
    pass  # adapters/ 目录可能尚未部署
