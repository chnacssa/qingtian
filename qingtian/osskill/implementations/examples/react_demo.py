"""ReAct 循环演示 Skill（设计文档 P1 §4.5，opensource 通用骨架）

演示 CognitionRunner 多步执行与失败复盘，供社区参考。
不涉及任何商业谈判逻辑。
"""

from osskill.models import CognizantSkill


class ReactDemoSkill(CognizantSkill):
    name = "react_demo"
    display_name = "ReAct 循环演示"
    description = "演示 CognitionRunner 多步执行与失败复盘"
    version = "1.0.0"

    def tools(self):
        return {
            "查库存": self._tool_stock,
            "算报价": self._tool_price,
        }

    async def _tool_stock(self, params):
        """查产品库存。参数: {"product": "产品名"}，返回现货数量（米）。"""
        stock = {"电缆YJV22-4x95": 1200, "电缆YJV22-4x50": 800}
        return {"ok": True, "stock": stock.get(params.get("product"), 0)}

    async def _tool_price(self, params):
        """查产品单价。参数: {"product": "产品名"}，返回元/米。"""
        base = {"电缆YJV22-4x95": 86.5, "电缆YJV22-4x50": 52.0}
        return {"ok": True, "price": base.get(params.get("product"), 0)}

    async def execute(self, params):
        goal = params.get("goal", "")
        result = await self.react(goal)
        if result["success"]:
            return result
        # 失败复盘路径：execute 层兜底（演示 on_execution_failure 接入）
        return await self.on_execution_failure(params, result.get("error", ""),
                                               result.get("steps", []))
