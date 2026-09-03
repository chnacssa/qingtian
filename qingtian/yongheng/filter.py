"""消息过滤 —— 内容质量检查。"""

import re

# 配置阈值
MIN_CONTENT_LENGTH = 10      # 最少字符数（去空白后）
MIN_WORD_COUNT = 3           # 最少词数（非纯结构体）
MAX_CONTENT_LENGTH = 50000   # 最长字符数
MAX_REPETITION_RATIO = 0.5   # 单字符最高占比
MAX_STRUCT_RATIO = 0.6       # JSON/HTML/XML 标记符号最高占比

# 纯结构体模式：内容几乎全是 JSON/HTML/XML/日志格式
_STRUCT_PATTERN = re.compile(
    r'^[\s\[\]\{\}"\':,\.<>/=\-|#*\d\s]+$',
    re.UNICODE,
)


def _repetition_ratio(content: str) -> float:
    """计算最高频字符在内容中的占比。重复字符过多 = 垃圾。"""
    if not content:
        return 1.0
    counts = {}
    for ch in content:
        counts[ch] = counts.get(ch, 0) + 1
    return max(counts.values()) / len(content)


def _word_count(content: str) -> int:
    """估算词数。中文按字符数/2，英文按空格分词。"""
    # 简单启发式：CJK 字符按 2 字一词，其余按空格分词
    cjk = sum(1 for ch in content if '一' <= ch <= '鿿')
    non_cjk = content
    for ch in content:
        if '一' <= ch <= '鿿':
            non_cjk = non_cjk.replace(ch, ' ')
    words = [w for w in non_cjk.split() if len(w) > 1]
    return len(words) + cjk // 2


def should_store(content: str) -> bool:
    """判断消息是否值得作为记忆存储。

    过滤规则：
    1. 空内容 / 纯空白 → 拒绝
    2. 过短（<10 字符）→ 拒绝
    3. 过长（>50000 字符）→ 拒绝
    4. 单字符重复过半 → 拒绝（如 "AAAAAA..."）
    5. 纯结构体 → 拒绝（如纯 JSON / HTML 骨架无文本）
    6. 词数不足（<3 词）→ 拒绝（如 "好的" "收到"）
    """
    stripped = content.strip() if content else ""

    if not stripped:
        return False

    if len(stripped) < MIN_CONTENT_LENGTH:
        return False

    if len(stripped) > MAX_CONTENT_LENGTH:
        return False

    if _repetition_ratio(stripped) > MAX_REPETITION_RATIO:
        return False

    if _STRUCT_PATTERN.match(stripped):
        return False

    if _word_count(stripped) < MIN_WORD_COUNT:
        return False

    return True
