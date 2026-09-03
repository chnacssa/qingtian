"""ProductCatalogSkill — 销售 Agent 产品目录管理能力。

Agent 调用示例:
  result = await skill.execute({"action": "list_products", "enterprise_id": "ent-001"})
  result = await skill.execute({"action": "create_product", "enterprise_id": "ent-001", ...})
  result = await skill.execute({"action": "import_products", "enterprise_id": "ent-001", "file_id": "abc..."})
"""

from osskill.implementations._base import BaseProductSkill


class ProductCatalogSkill(BaseProductSkill):
    """产品目录管理 Skill"""

    CAPABILITIES = {
        "list_products":    {"method": "GET",    "path": "/v1/product/catalog",         "desc": "列出产品"},
        "get_product":      {"method": "GET",    "path": "/v1/product/catalog/{id}",    "desc": "产品详情"},
        "create_product":   {"method": "POST",   "path": "/v1/product/catalog",         "desc": "创建产品"},
        "update_product":   {"method": "PUT",    "path": "/v1/product/catalog/{id}",    "desc": "更新产品"},
        "delete_product":   {"method": "DELETE", "path": "/v1/product/catalog/{id}",    "desc": "删除产品"},
        "search_products":  {"method": "GET",    "path": "/v1/product/catalog",         "desc": "搜索产品"},
        "import_products":  {"method": "POST",   "path": "/v1/product/catalog/import",  "desc": "XLSX 导入"},
    }
    name = "product_catalog"
    display_name = "产品目录管理"
    description = "管理企业产品目录，创建/查询/更新产品信息，支持 XLSX 批量导入"
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
