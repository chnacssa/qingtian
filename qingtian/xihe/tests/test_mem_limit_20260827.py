# -*- coding: utf-8 -*-
"""per-skill 内存上限（2026-08-27）回归测试：

线上实锤（安徽变压器标书 10:01:25）：bidding skill 下载/排版大量证照图时
OpenBLAS "Memory allocation failed"（RLIMIT_AS 536MB），进程崩→自动重启→重跑。
根因双料：① XiheConfig.memory_limit_bytes 从未到达子进程（main.py 启动常驻
Skill 不传 config，_spawn 收到空 dict，skill_runner 落硬编码 512MiB 兜底）；
② bidding 是 OCR/PyMuPDF/docx 嵌图内存密集型，全局 512MiB 本身就不够。

修复：XiheConfig 新增 per_skill_memory_limit_bytes（bidding 2GiB），_spawn 显式
注入，巡检 check_memory_pressure 按同口径有效限额判定（防 2GiB 提额后仍按
512MiB 误降级 CPU 权重）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from xihe.config import XiheConfig
from xihe.agent_runtime import _effective_memory_limit


def test_per_skill_memory_default():
    """默认 per-skill 映射：bidding/bid_prep 2GiB；全局默认仍 512MiB。

    bid_prep 2GiB（2026-08-29 小智实锤）：LibreOffice 转 153MB zip 内 .doc 在
    512MiB 下 std::bad_alloc 全崩；2GiB 后 49 文件零失败。
    """
    cfg = XiheConfig()
    assert cfg.memory_limit_bytes == 512 * 1024 * 1024
    assert cfg.per_skill_memory_limit_bytes == {
        "bidding": 2 * 1024 * 1024 * 1024,
        "bid_prep": 2 * 1024 * 1024 * 1024,
    }


def test_effective_memory_limit_per_skill_override():
    """bidding/bid_prep 取 per-skill 2GiB；其他 Skill 取全局。"""
    cfg = XiheConfig()
    assert _effective_memory_limit("bidding", cfg) == 2 * 1024 * 1024 * 1024
    assert _effective_memory_limit("bid_prep", cfg) == 2 * 1024 * 1024 * 1024
    assert _effective_memory_limit("sales", cfg) == 512 * 1024 * 1024
    assert _effective_memory_limit("workflow", cfg) == 512 * 1024 * 1024


def test_effective_memory_limit_config_override():
    """config.yaml xihe.per_skill_memory_limit_bytes 可整体覆盖默认映射。"""
    cfg = XiheConfig(per_skill_memory_limit_bytes={"bidding": 1024 * 1024 * 1024})
    assert _effective_memory_limit("bidding", cfg) == 1024 * 1024 * 1024


def test_effective_memory_limit_missing_attr_fallback():
    """cfg 没有该属性（旧配置对象）也不崩，退全局值。"""

    class _Legacy:
        memory_limit_bytes = 256 * 1024 * 1024

    assert _effective_memory_limit("bidding", _Legacy()) == 256 * 1024 * 1024


def test_main_py_no_hardcoded_per_skill_mem():
    """main.py 不得硬编码 per-skill 内存字面量（2026-08-29 小智 19:25 实锤：
    `{"bidding": 2GiB}` 字面量显式传参挤掉 config.py 默认并集，bid_prep 等
    新 skill 只改 config.py 不生效、运行时仍 512MiB）。必须以 XiheConfig
    默认映射为底 + config.yaml override 合并。"""
    from pathlib import Path
    main_py = Path(__file__).resolve().parents[2] / "main.py"
    src = main_py.read_text(encoding="utf-8")
    assert '_per_skill_mem = {"bidding"' not in src, \
        "main.py 硬编码字面量会覆盖 config.py 默认并集（bid_prep 512MiB 事故复发）"
    assert "_per_skill_mem = dict(XiheConfig()" in src, \
        "必须以 XiheConfig 默认映射为底再合并 override"
