"""汇川 — 错误定义"""


class AppError(Exception):
    """业务异常"""

    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


class KnowledgeNotFoundError(AppError):
    def __init__(self, knowledge_id: str):
        super().__init__("HUICHUAN_NOT_FOUND", f"知识条目不存在: {knowledge_id}", 404)


class VersionConflictError(AppError):
    def __init__(self, current_version: int):
        super().__init__(
            "VERSION_CONFLICT",
            f"版本冲突: 当前版本为 {current_version}，已被其他 Agent 修改，请重新获取后再提交",
            409,
        )


class VisibilityForbiddenError(AppError):
    def __init__(self, knowledge_id: str):
        super().__init__("VISIBILITY_FORBIDDEN", f"无权访问此知识条目: {knowledge_id}", 403)
