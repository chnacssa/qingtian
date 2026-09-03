"""Excel 生成器 — 根据数据描述生成 Excel 报表"""
from osskill.implementations.clawhub_adapter.clawhub_adapter import ClawHubSkill


class ExcelGeneratorSkill(ClawHubSkill):
    name = "excel_generator"
    display_name = "Excel 生成器"
    description = "根据数据描述生成 Excel 报表、数据透视表模板、图表"
    version = "1.0.0"
