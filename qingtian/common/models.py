"""
ACSSA 智能体操作系统共享数据模型
各板块复用的 Pydantic 模型
"""

from pydantic import BaseModel
from typing import Optional, List, Dict, Any


class GenericResponse(BaseModel):
    status: str = "ok"
    message: Optional[str] = None
    data: Any = None


# 阿尔兹记忆接口基础模型（兼容旧版引用）
class MemoryBase(BaseModel):
    namespace: str
    memory_type: str = "episodic"
    content: str
    source: str = "assistant"
    protected: bool = False
    metadata: Dict[str, Any] = {}
