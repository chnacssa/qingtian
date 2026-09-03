"""PriceListSkill — 销售 Agent 价目表管理能力。

Agent 调用示例:
  result = await skill.execute({"action": "list_price_lists", "enterprise_id": "ent-001"})
  result = await skill.execute({"action": "create_price_list", "enterprise_id": "ent-001", ...})
  result = await skill.execute({"action": "supersede_price_list", "id": "..."})
"""

from osskill.implementations._base import BaseProductSkill


class PriceListSkill(BaseProductSkill):
    """价目表管理 Skill"""

    CAPABILITIES = {
        "list_price_lists":     {"method": "GET",    "path": "/v1/product/price-lists",              "desc": "列出价目表"},
        "get_price_list":       {"method": "GET",    "path": "/v1/product/price-lists/{id}",         "desc": "价目表详情"},
        "create_price_list":    {"method": "POST",   "path": "/v1/product/price-lists",              "desc": "创建价目表"},
        "update_price_list":    {"method": "PUT",    "path": "/v1/product/price-lists/{id}",         "desc": "更新价目表"},
        "activate_price_list":  {"method": "POST",   "path": "/v1/product/price-lists/{id}/activate","desc": "激活价目表"},
        "supersede_price_list": {"method": "POST",   "path": "/v1/product/price-lists/{id}/supersede","desc": "版本升级"},
        "delete_price_list":    {"method": "DELETE", "path": "/v1/product/price-lists/{id}",         "desc": "删除价目表"},
        "list_items":           {"method": "GET",    "path": "/v1/product/price-lists/{id}/items",   "desc": "列出明细"},
        "add_item":             {"method": "POST",   "path": "/v1/product/price-lists/{id}/items",   "desc": "添加明细"},
        "batch_update_items":   {"method": "PUT",    "path": "/v1/product/price-lists/{id}/items/batch","desc": "批量更新明细"},
        "import_price_list":    {"method": "POST",   "path": "/v1/product/price-lists/{id}/import",  "desc": "XLSX 导入"},
    }
    name = "price_list"
    display_name = "报价单管理"
    description = "创建/更新/版本管理报价单，支持每日自动更新标记和 XLSX 导入"
    category = "product"
    version = "1.0.0"
    knowledge_deps = []
    tool_deps = []
    input_schema = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": list(CAPABILITIES.keys()),
                "description": "要执行的操作",
            },
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "data": {"type": "object"},
            "error": {"type": "string"},
        },
    }
