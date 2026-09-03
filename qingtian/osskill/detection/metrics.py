"""检测管线 — 指标上报（对接 common.metrics）

委托给 common.metrics 的 counter/histogram。
破军已在 main.py 注册 /metrics 端点暴露 Prometheus 格式。
指标名约定:
  qingtian_skill_detection_total{skill,result}        检测次数(pass/fail)
  qingtian_skill_detection_duration_ms{skill}         检测耗时
  qingtian_skill_install_total{skill,action}          安装/卸载次数
"""

import logging

from common.metrics import (
    record_skill_detection as _record_detection,
    record_skill_install as _record_install,
)

logger = logging.getLogger(__name__)


async def report_detection(skill: str, passed: bool, duration_ms: int):
    """上报 Skill 检测结果指标"""
    _record_detection(
        skill=skill,
        result="pass" if passed else "fail",
        duration_ms=float(duration_ms),
    )


async def report_install(skill: str):
    """上报 Skill 安装事件"""
    _record_install(skill=skill, action="install")
