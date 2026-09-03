"""汇川数据脱敏 — PII 清洗

按流动方向分级脱敏：
  - erp_to_ingest: PII only（企业系统→汇川入库）
  - private_to_shared: PII + 内部备注（私有→共享层晋升）
  - private_to_private: PII only（私有→私有授权穿透）

复用说明：
  - huichuan/ingest.py 调用 sanitize(level="erp_to_ingest")
  - huichuan/api.py promote 端点调用 sanitize(level="private_to_shared")
  - connector.py 调用 sanitize(level="erp_to_ingest")
"""

import logging
import re

logger = logging.getLogger("huichuan.sanitizer")

# ── PII 正则模式 ────────────────────────────────────────
# 使用 (?<!\d)...(?!\d) 替代 \b：
#   Python 3 Unicode 模式下 \b 在 CJK 字符和数字之间不识别为边界，
#   导致 "联系电话13812345678" 等无空格格式脱敏失效。
#   lookbehind/lookahead 绕开 Unicode 单词边界问题，直接检查相邻字符类型。

PII_PATTERNS: dict[str, tuple[str, str]] = {
    "phone":   (r'(?<!\d)1[3-9]\d{9}(?!\d)', '***'),
    "id_card": (r'(?<!\d)\d{17}[\dXx](?!\d)', '***'),
    "bank":    (r'(?<!\d)\d{16,19}(?!\d)', '***'),
    "email":   (r'(?<![a-zA-Z0-9@.\-])[\w.\-]+@[\w.\-]+\.\w+(?![a-zA-Z0-9@.\-])', '***'),
}

# 内部备注模式（以 #内部 开头的整行）
_INTERNAL_NOTE_RE = re.compile(r'^#内部.*$', re.MULTILINE)


def sanitize(text: str, level: str = "private_to_shared") -> str:
    """按流动方向脱敏。

    Args:
        text: 待脱敏文本
        level: 脱敏级别
            - "erp_to_ingest": PII only（电话/身份证/银行卡/邮箱）
            - "private_to_shared": PII + 内部备注行
            - "private_to_private": PII only

    Returns:
        脱敏后的文本

    Raises:
        无 — 异常时返回脱敏失败标记，绝不泄漏原文 PII
    """
    if not text:
        return text

    try:
        # 所有级别都脱敏 PII
        for _name, (pattern, replacement) in PII_PATTERNS.items():
            text = re.sub(pattern, replacement, text)

        # private_to_shared 额外去掉内部备注行
        if level == "private_to_shared":
            text = _INTERNAL_NOTE_RE.sub('', text)

        # TODO Phase 6/7: shared_to_shared, shared_to_private
        # (共享层数据已脱敏，当前无需额外处理)
    except Exception:
        logger.exception("脱敏异常，返回安全标记代替原文")
        return "[PII REDACTED: SANITIZE ERROR]"

    return text
