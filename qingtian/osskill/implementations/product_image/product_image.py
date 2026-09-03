"""ProductImageSkill — 销售 Agent 产品图片管理能力。

Agent 调用示例:
  result = await skill.execute({"action": "list_images", "product_id": "uuid-..."})
  result = await skill.execute({"action": "upload_image", "product_id": "uuid-...", ...})
"""

from osskill.implementations._base import BaseProductSkill


class ProductImageSkill(BaseProductSkill):
    """产品图片管理 Skill"""

    CAPABILITIES = {
        "list_images":        {"method": "GET",    "path": "/v1/product/{product_id}/images",               "desc": "列出图片"},
        "upload_image":       {"method": "POST",   "path": "/v1/product/{product_id}/images",               "desc": "上传图片"},
        "set_primary_image":  {"method": "PUT",    "path": "/v1/product/{product_id}/images/{img_id}/primary","desc": "设为主图"},
        "delete_image":       {"method": "DELETE", "path": "/v1/product/{product_id}/images/{img_id}",      "desc": "删除图片"},
    }
    name = "product_image"
    display_name = "产品图片管理"
    description = "管理产品图片，支持上传/设主图/删除"
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
