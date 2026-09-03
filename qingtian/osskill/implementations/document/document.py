"""DocumentSkill — 销售 Agent 企业文档管理能力。

Agent 调用示例:
  result = await skill.execute({"action": "list_documents", "enterprise_id": "ent-001"})
  result = await skill.execute({"action": "upload_document", "enterprise_id": "ent-001", ...})
"""

from osskill.implementations._base import BaseProductSkill


class DocumentSkill(BaseProductSkill):
    """企业文档管理 Skill"""

    CAPABILITIES = {
        "list_documents":      {"method": "GET",    "path": "/v1/product/documents",              "desc": "列出文档"},
        "get_document":        {"method": "GET",    "path": "/v1/product/documents/{id}",         "desc": "文档详情"},
        "upload_document":     {"method": "POST",   "path": "/v1/product/documents",              "desc": "上传文档"},
        "update_document":     {"method": "PUT",    "path": "/v1/product/documents/{id}",         "desc": "更新文档"},
        "delete_document":     {"method": "DELETE", "path": "/v1/product/documents/{id}",         "desc": "删除文档"},
    }
    name = "document"
    display_name = "企业文档管理"
    description = "管理企业公共文档（资质、证书、样本、合同模板）的上传/查询/删除"
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
