"""报表系统 — 各岗位智能体报表实时查询。

每个岗位（投标/销售/采购）实现自己的 query_report()，
接收 enterprise_id + period_type，返回结构化数据。

不使用预生成，即查即算。
"""
