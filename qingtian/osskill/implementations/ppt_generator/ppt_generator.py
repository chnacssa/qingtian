"""PPT 生成器 — 从大纲生成演示文稿内容"""
from osskill.implementations.clawhub_adapter.clawhub_adapter import ClawHubSkill


class PptGeneratorSkill(ClawHubSkill):
    name = "ppt_generator"
    display_name = "PPT 生成器"
    description = "从大纲或要点生成演示文稿内容、幻灯片结构、演讲备注"
    version = "1.0.0"
