"""Word 文档生成器 — 根据大纲生成 Word 文档、合同、标书"""
from osskill.implementations.clawhub_adapter.clawhub_adapter import ClawHubSkill


class WordGeneratorSkill(ClawHubSkill):
    name = "word_generator"
    display_name = "Word 文档生成器"
    description = "根据大纲或要点生成 Word 文档、合同、标书、函件"
    version = "1.0.0"
