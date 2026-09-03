"""司库模块冒烟导入测试。

2026-09-02 事故复盘（5028c75b）：database.py 模块级 f-string 注释 {message_id}
未转义 → import 即 NameError。但 api/finance_agent 均不在模块级 import database
（main.py 启动事件里 ensure_schema 才惰性引入），导致 109 个测试全绿而线上启动
必崩——测试链碰不到这个模块。本测试钉死"司库全部模块可导入"，同类模块级
语法/名字错误从此在 CI 就炸。
"""


def test_siku_modules_importable():
    import importlib

    for mod in ("siku.config", "siku.database", "siku.api", "siku.finance_agent"):
        importlib.import_module(mod)
