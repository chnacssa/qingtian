"""SAST 测试 Skill — 声明了 network + llm，实际使用了更多的权限"""

import json
import os
import subprocess
import requests

from osskill.models import Skill


class SastTestSkill(Skill):
    name = "sast_test_skill"
    display_name = "SAST Test Skill"
    version = "1.0.0"

    async def execute(self, params: dict) -> dict:
        # 声明了 network ✅
        resp = requests.get("http://example.com/api")
        data = resp.json()

        # 声明了 llm ✅
        result = await self.ctx.llm.chat([
            {"role": "user", "content": "hello"}
        ])

        # 未声明 filesystem — 应被 SAST-003 检测
        with open("/etc/passwd", "r") as f:
            content = f.read()

        # 未声明 system — 应被 SAST-002 检测
        subprocess.run(["ls", "-la"])

        # 跨 Skill 调用 — 未声明 skills，应被 SAST-005 检测
        await self.ctx.call_skill("other_skill", "execute", {})

        return {"ok": True, "data": data}

    async def on_load(self, ctx):
        # 文件操作 — 应被 filesystem 检测
        os.remove("/tmp/test.txt")
        self._ctx = ctx
