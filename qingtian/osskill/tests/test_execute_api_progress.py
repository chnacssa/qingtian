"""execute_api 系统/进度消息防护测试 — 防投标生成死循环（2026-08-11 大师实锤）

纯逻辑测试，不依赖数据库。验证：
- 进度/通知类消息（⏳ 前缀、评审第 N 轮、生成完成/失败）→ 识别为非指令；
- 真实用户指令（生成/写标书/评标请求）→ 不受误伤。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from osskill.execute_api import _looks_like_progress_notice


@pytest.mark.parametrize("text", [
    "⏳ 正在生成投标文件（Word）...",       # 生成进度
    "⏳ 正在校验投标文件完整度...",          # 校验进度
    "⏳ AI 评审第 4 轮...",                  # 评审进度
    "✅ 投标文件生成完成",                    # 完成通知
    "⚠️ 生成失败: 标书生成...",               # 失败通知
    "📋 进度播报",                            # 进度播报
    "生成完成",                               # 完成措辞
    "生成失败: 标书生成异常",                 # 失败措辞
])
def test_progress_notice_detected(text):
    """进度/通知类消息 → 识别为非指令（不得路由成新 generate_bid）。"""
    assert _looks_like_progress_notice(text) is True


@pytest.mark.parametrize("text", [
    "帮我生成投标文件",          # 真实用户指令
    "写一份标书",               # 真实用户指令
    "帮我评标打分",             # 真实用户指令
    "生成报价单",               # 其他技能指令，不应误伤
    "客户回访情况怎么样",        # 日常询问
    "",                          # 空文本
])
def test_real_instructions_not_blocked(text):
    """真实用户指令 → 不误判为进度消息。"""
    assert _looks_like_progress_notice(text) is False
