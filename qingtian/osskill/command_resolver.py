"""指令词注册表 — 全量加载到内存，O(1) 解析。

锚定安全：仅消息开头或 @秘书 后的 !!command!! 生效。
"""
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from common.db import get_pool
from .database import SCHEMA as _SCHEMA

logger = logging.getLogger("osskill.command_resolver")


@dataclass
class CommandInfo:
    word: str
    skill_name: str
    action: str
    description: str = ""
    examples: list[str] = field(default_factory=list)


# 锚定正则：消息开头 or @秘书 后方的 !!指令词!!
_COMMAND_PATTERN = re.compile(r"(?:^|@秘书\s*)!!(\w+)!!")


class CommandResolver:
    """指令词注册表。

    全量加载到内存 dict，resolve O(1)。
    reload() 使用指针交换保证原子性。
    """

    def __init__(self):
        self._registry: dict[str, CommandInfo] = {}

    def resolve(self, word: str) -> Optional[CommandInfo]:
        """O(1) 精确匹配。"""
        return self._registry.get(word)

    def search(self, text: str) -> list[tuple[CommandInfo, float]]:
        """模糊匹配（NL 理解用）。"""
        if not text:
            return []
        results: list[tuple[CommandInfo, float]] = []
        for word, info in self._registry.items():
            if word == text:
                results.append((info, 1.0))
            elif text.startswith(word):
                results.append((info, 0.95))
            elif word in text:
                results.append((info, 0.8))
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def list_all(self) -> list[CommandInfo]:
        return list(self._registry.values())

    async def load(self, pool=None):
        """全量加载 — 从 DB 读取 skill_definitions 的 commands 列。"""
        if pool is None:
            pool = await get_pool()
        new_registry: dict[str, CommandInfo] = {}
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT name, commands FROM {_SCHEMA}.skill_definitions "
                "WHERE status = 'active'"
            )
            for row in rows:
                skill_name = row["name"]
                raw = row.get("commands")
                if isinstance(raw, str):
                    cmds = json.loads(raw) if raw.strip() else []
                elif isinstance(raw, (list, tuple)):
                    cmds = raw
                else:
                    cmds = []
                for cmd in cmds:
                    new_registry[cmd["word"]] = CommandInfo(
                        word=cmd["word"],
                        skill_name=skill_name,
                        action=cmd["action"],
                        description=cmd.get("description", ""),
                        examples=cmd.get("examples", []),
                    )
        # 指针交换（原子操作）
        self._registry = new_registry
        logger.info("CommandResolver loaded %d commands", len(new_registry))

    async def reload(self, pool=None):
        await self.load(pool=pool)


# 全局单例
_resolver: CommandResolver | None = None


def get_resolver() -> CommandResolver:
    global _resolver
    if _resolver is None:
        _resolver = CommandResolver()
    return _resolver


def extract_command(text: str) -> Optional[CommandInfo]:
    """从消息中提取 !!command!!（锚定安全版本）。

    只匹配消息开头或 @秘书 后的 !!指令词!!，中间藏匿的指令忽略。
    全角半角感叹号统一识别。
    """
    if not text:
        return None
    text = text.replace("！", "!")
    m = _COMMAND_PATTERN.search(text.strip())
    if not m:
        return None
    word = m.group(1)
    return get_resolver().resolve(word)
