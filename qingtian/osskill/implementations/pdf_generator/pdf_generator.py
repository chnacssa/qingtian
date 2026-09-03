"""PDF 生成器 — HTML 转 PDF、合并拆分、加水印"""
from osskill.implementations.clawhub_adapter.clawhub_adapter import ClawHubSkill


class PdfGeneratorSkill(ClawHubSkill):
    name = "pdf_generator"
    display_name = "PDF 生成器"
    description = "HTML 到 PDF 转换、PDF 合并拆分、加页码水印、元信息编辑"
    version = "1.0.0"
