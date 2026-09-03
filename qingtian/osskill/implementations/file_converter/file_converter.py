"""文件格式转换器 — 文档格式互转（docx ↔ pdf ↔ md ↔ html）"""
from osskill.implementations.clawhub_adapter.clawhub_adapter import ClawHubSkill


class FileConverterSkill(ClawHubSkill):
    name = "file_converter"
    display_name = "文件格式转换器"
    description = "文档格式互转：Markdown、HTML、纯文本、CSV 之间的内容转换"
    version = "1.0.0"
