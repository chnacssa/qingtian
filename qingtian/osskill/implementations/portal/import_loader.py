"""模板下载 build_template 加载器。

目录结构调整后（skills 目录承载实际技能代码），portal 模板下载接口（/v1/sales/templates、/v1/procurement/templates）
原相对导入 `from ..{sales,procurement}.import_export import build_template` 指向的
osskill/implementations/{sales,procurement}/import_export.py 已不存在（旧结构残留）。

实际 import_export.py 位于 /opt/qingtian/skills/{sales,procurement}/import_export.py（纯函数模块，仅依赖 io/logging/typing/openpyxl）。
本模块按该绝对路径加载并缓存 build_template，避免每次请求重复加载，且不依赖包名解析（跨 skill 薄封装，保持 portal 独立）。
"""

import importlib.util
import os
from functools import lru_cache
from pathlib import Path


def _skills_root() -> Path:
    """P2 (R11): skills 目录配置化——QINGTIAN_SKILLS_DIR 优先，缺省 /opt/qingtian/skills 兜底。

    原硬编码 /opt/qingtian/skills，非该路径部署时模板下载必 500；
    改为环境变量可配，默认值保留原路径保证向后兼容。
    """
    return Path(os.environ.get("QINGTIAN_SKILLS_DIR") or "/opt/qingtian/skills")


@lru_cache(maxsize=8)
def _load_import_export(skill: str):
    """按 skill 名（sales/procurement）从 skills 目录加载 import_export 模块。"""
    mod_path = _skills_root() / skill / "import_export.py"
    if not mod_path.exists():
        raise ImportError(f"import_export 模块不存在: {mod_path}")
    spec = importlib.util.spec_from_file_location(f"portal_impl_{skill}_import_export", str(mod_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {mod_path} 创建模块 spec")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_build_template(skill: str):
    """返回指定 skill 的 build_template 可调用对象（函数，非模块）。"""
    mod = _load_import_export(skill)
    build_template = getattr(mod, "build_template", None)
    if build_template is None:
        raise AttributeError(f"{skill} 的 import_export 模块缺少 build_template")
    return build_template
