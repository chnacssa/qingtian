"""SAST 测试 — 只声明 L1 权限（无声明），代码也不做任何事"""
from osskill.models import Skill


class L1TestSkill(Skill):
    name = "l1_test_skill"
    display_name = "L1 Test Skill"
    version = "1.0.0"

    async def execute(self, params: dict) -> dict:
        return {"ok": True}
