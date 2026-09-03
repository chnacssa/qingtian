"""CSV 分析器 — CSV 数据清洗、统计、可视化建议"""
from osskill.implementations.clawhub_adapter.clawhub_adapter import ClawHubSkill


class CsvAnalyzerSkill(ClawHubSkill):
    name = "csv_analyzer"
    display_name = "CSV 分析器"
    description = "对 CSV 数据进行清洗、统计分析、异常检测、可视化建议"
    version = "1.0.0"
