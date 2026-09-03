#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
需求澄清 Skill (Requirement Clairifier)
当检测到用户需求不明确时，主动向用户询问关键信息

功能：
1. 分析用户需求，检测缺失字段
2. 生成追问话术
3. 管理澄清会话状态（存储在 Redis）
4. 处理用户回复
5. 超时升级机制
"""

import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

# 尝试导入 Redis（如果不可用则使用内存存储）
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


@dataclass
class ClarificationQuestion:
    """单个澄清问题"""
    field: str
    question: str
    priority: int  # 1-5, 越高越优先
    answered: bool = False
    answer: str = ""


@dataclass
class ClarificationSession:
    """澄清会话状态"""
    session_id: str
    user_id: str
    agent_id: str
    server_type: str  # procurement / sales / management
    original_request: str
    context: Dict  # 业务上下文
    questions: List[ClarificationQuestion]
    status: str  # pending / awaiting_reply / completed / timeout / escalated
    created_at: str
    updated_at: str
    timeout_at: str
    reminder_count: int = 0

    def to_dict(self) -> Dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "server_type": self.server_type,
            "original_request": self.original_request,
            "context": self.context,
            "questions": [asdict(q) for q in self.questions],
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "timeout_at": self.timeout_at,
            "reminder_count": self.reminder_count
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ClarificationSession":
        questions = [ClarificationQuestion(**q) for q in data.get("questions", [])]
        return cls(
            session_id=data["session_id"],
            user_id=data["user_id"],
            agent_id=data["agent_id"],
            server_type=data["server_type"],
            original_request=data["original_request"],
            context=data.get("context", {}),
            questions=questions,
            status=data["status"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            timeout_at=data["timeout_at"],
            reminder_count=data.get("reminder_count", 0)
        )


class RequirementClairifier:
    """
    需求澄清 Skill 主类

    用法:
    clairifier = RequirementClairifier(server_type="procurement")
    result = clairifier.analyze(user_request, user_id, agent_id)
    if not result["is_complete"]:
        questions = result["missing_fields"]
        # 发送问题给用户
    """

    # 各业务类型的必填字段及其优先级
    REQUIRED_FIELDS = {
        "procurement": {
            "product_type": 5,
            "quantity": 5,
            "delivery_date": 4,
            "budget": 3,
            "specifications": 4,
            "quality_grade": 3,
            "payment_terms": 2,
            "delivery_address": 3
        },
        "sales": {
            "product_name": 5,
            "quantity": 5,
            "target_price": 4,
            "validity_period": 3,
            "payment_terms": 3,
            "delivery_terms": 2
        },
        "management": {
            "task_type": 5,
            "scope": 4,
            "deadline": 4,
            "priority": 3,
            "resources": 3,
            "constraints": 2
        }
    }

    # 追问话术模板
    QUESTION_TEMPLATES = {
        "procurement": {
            "product_type": "您需要采购的具体产品是什么？比如：钢材（螺纹钢III级/圆钢）、水泥（PC42.5/P042.5）、电缆（YJV-0.6/1kV-3x95+1x50）等",
            "quantity": "请问需要的数量是多少？能接受浮动的范围吗？",
            "delivery_date": "您期望的交货日期是什么时候？有最晚期限要求吗？",
            "budget": "您的预算范围是多少？或者有单价上限吗？",
            "specifications": "能提供更详细的技术参数吗？如：规格、型号、强度等级、执行标准等",
            "quality_grade": "对产品质量等级有什么要求？如：国标/行标/企标",
            "payment_terms": "您期望的付款方式是什么？如：先款后货、货到付款、月结30天等",
            "delivery_address": "送货地址是哪里？需要包含在报价里吗？"
        },
        "sales": {
            "product_name": "请告诉我您需要报价的产品名称和规格型号？",
            "quantity": "您需要的数量是多少？我帮您申请更优惠的价格",
            "target_price": "您的目标价位是多少？或者有预算限制吗？",
            "validity_period": "报价有效期有什么要求？通常我们能提供48小时有效报价",
            "payment_terms": "您期望的付款方式是什么？",
            "delivery_terms": "交货方式有什么要求？如：送到工地、买方自提等"
        },
        "management": {
            "task_type": "您需要的任务类型是什么？如：数据分析、报告生成、代码编写、系统巡检等",
            "scope": "任务的涉及范围是什么？有哪些部门或系统需要关注？",
            "deadline": "任务的截止时间是什么时候？",
            "priority": "任务的优先级是什么？高/中/低？是否有紧急程度说明",
            "resources": "任务需要哪些资源？如：人力、服务器、预算等",
            "constraints": "任务有什么约束条件？如：必须使用某技术、不能影响生产等"
        }
    }

    # 模糊词匹配（用于检测需求是否明确）
    AMBIGUOUS_PATTERNS = [
        r"随便|都可以|无所谓",
        r"大概|大约|估计|可能",
        r"一些|一点|若干",
        r"看看|了解|调研",
        r"尽快|早点|差不多",
        r"多少钱|价格|报价",  # 询价但无具体需求
        r"有没有|能不能|可以吗"  # 询问可能性
    ]

    def __init__(self, server_type: str = "procurement", redis_host: str = "localhost",
                 redis_port: int = 6379, redis_password: str = "",
                 redis_db: int = 0):
        """
        初始化需求澄清 Skill

        Args:
            server_type: 服务器类型 (procurement/sales/management)
            redis_host: Redis 主机
            redis_port: Redis 端口
            redis_password: Redis 密码
            redis_db: Redis 数据库编号
        """
        self.server_type = server_type
        self.config = self._load_config()
        self.redis_client = None

        if REDIS_AVAILABLE:
            try:
                self.redis_client = redis.Redis(
                    host=redis_host,
                    port=redis_port,
                    password=redis_password or None,
                    db=redis_db,
                    decode_responses=True,
                    socket_connect_timeout=5
                )
                self.redis_client.ping()
            except Exception:
                self.redis_client = None

    def _load_config(self) -> Dict:
        """加载技能配置"""
        return {
            "max_questions_per_round": 5,
            "timeout_hours": 24,
            "max_reminders": 3,
            "auto_escalate_on_timeout": True
        }

    def analyze(self, user_request: str, user_id: str = "", agent_id: str = "",
                context: Dict = None) -> Dict:
        """
        分析用户需求是否完整

        Args:
            user_request: 用户原始需求文本
            user_id: 用户ID
            agent_id: Agent ID
            context: 业务上下文

        Returns:
            Dict: {
                "is_complete": bool,          # 需求是否完整
                "missing_fields": List[Dict],  # 缺失字段列表
                "confidence": float,           # 需求完整度置信度 (0-1)
                "session_id": str,             # 澄清会话ID（如果需要澄清）
                "needs_clarification": bool    # 是否需要澄清
            }
        """
        context = context or {}

        # Step 1: 检查是否有模糊表述
        has_ambiguous = self._check_ambiguous(user_request)

        # Step 2: 提取已知字段
        extracted_fields = self._extract_fields(user_request)

        # Step 3: 检测缺失字段
        missing_fields = self._detect_missing_fields(extracted_fields)

        # Step 4: 计算置信度
        confidence = self._calculate_confidence(extracted_fields, missing_fields)

        # 判断是否需要澄清
        needs_clarification = len(missing_fields) > 0 or has_ambiguous or confidence < 0.7

        result = {
            "is_complete": not needs_clarification,
            "missing_fields": missing_fields,
            "confidence": confidence,
            "needs_clarification": needs_clarification,
            "has_ambiguous": has_ambiguous,
            "extracted_fields": extracted_fields
        }

        # 如果需要澄清，创建会话
        if needs_clarification:
            session = self._create_session(
                user_request=user_request,
                user_id=user_id,
                agent_id=agent_id,
                missing_fields=missing_fields,
                context=context
            )
            result["session_id"] = session.session_id
            result["questions"] = [asdict(q) for q in session.questions]

        return result

    def _check_ambiguous(self, text: str) -> bool:
        """检查文本中是否包含模糊表述"""
        for pattern in self.AMBIGUOUS_PATTERNS:
            if re.search(pattern, text):
                return True
        return False

    def _extract_fields(self, text: str) -> Dict[str, str]:
        """从文本中提取已知字段"""
        extracted = {}
        required = self.REQUIRED_FIELDS.get(self.server_type, {})

        for field in required.keys():
            # 简单的关键词匹配（实际应用中应该用 NLP 模型）
            if field in text.lower():
                extracted[field] = self._extract_field_value(text, field)

        return extracted

    def _extract_field_value(self, text: str, field: str) -> str:
        """提取字段值（简单实现，实际应该用更复杂的 NLP）"""
        # 这是一个占位实现
        return f"[从文本中提取的 {field}]"

    def _detect_missing_fields(self, extracted_fields: Dict) -> List[Dict]:
        """检测缺失的必填字段"""
        missing = []
        required = self.REQUIRED_FIELDS.get(self.server_type, {})

        for field, priority in required.items():
            if field not in extracted_fields or not extracted_fields[field]:
                missing.append({
                    "field": field,
                    "priority": priority,
                    "question": self.QUESTION_TEMPLATES.get(self.server_type, {}).get(
                        field, f"请提供{field}相关信息"
                    )
                })

        # 按优先级排序
        missing.sort(key=lambda x: x["priority"], reverse=True)
        return missing

    def _calculate_confidence(self, extracted: Dict, missing: List) -> float:
        """计算需求完整度置信度"""
        required = self.REQUIRED_FIELDS.get(self.server_type, {})
        total_weight = sum(required.values())

        if total_weight == 0:
            return 1.0

        # 计算已填字段的权重
        filled_weight = 0
        for field in extracted:
            if field in required:
                filled_weight += required[field]

        # 考虑缺失字段数量
        missing_penalty = len(missing) * 0.1

        confidence = (filled_weight / total_weight) - missing_penalty
        return max(0.0, min(1.0, confidence))

    def _create_session(self, user_request: str, user_id: str, agent_id: str,
                        missing_fields: List[Dict], context: Dict) -> ClarificationSession:
        """创建澄清会话"""
        now = datetime.utcnow()
        timeout = now + timedelta(hours=self.config["timeout_hours"])

        # 选择最高优先级的问题（不超过 max_questions_per_round）
        max_q = self.config["max_questions_per_round"]
        selected_fields = missing_fields[:max_q]

        questions = [
            ClarificationQuestion(
                field=f["field"],
                question=f["question"],
                priority=f["priority"]
            )
            for f in selected_fields
        ]

        session = ClarificationSession(
            session_id=str(uuid.uuid4()),
            user_id=user_id,
            agent_id=agent_id,
            server_type=self.server_type,
            original_request=user_request,
            context=context,
            questions=questions,
            status="awaiting_reply",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            timeout_at=timeout.isoformat()
        )

        # 保存到 Redis
        self._save_session(session)

        return session

    def _save_session(self, session: ClarificationSession):
        """保存会话到 Redis"""
        key = f"clarification:{session.session_id}"
        if self.redis_client:
            try:
                self.redis_client.setex(
                    key,
                    self.config["timeout_hours"] * 3600,
                    json.dumps(session.to_dict())
                )
            except Exception:
                pass  # Redis 不可用时静默失败

    def get_session(self, session_id: str) -> Optional[ClarificationSession]:
        """获取澄清会话"""
        key = f"clarification:{session_id}"
        if self.redis_client:
            try:
                data = self.redis_client.get(key)
                if data:
                    return ClarificationSession.from_dict(json.loads(data))
            except Exception:
                pass
        return None

    def process_answer(self, session_id: str, user_id: str, answer: str) -> Dict:
        """
        处理用户对某个问题的回复

        Args:
            session_id: 会话ID
            user_id: 用户ID（验证）
            answer: 用户回复内容

        Returns:
            Dict: 处理结果
        """
        session = self.get_session(session_id)
        if not session:
            return {"success": False, "error": "会话不存在或已过期"}

        if session.user_id != user_id:
            return {"success": False, "error": "用户ID不匹配"}

        if session.status != "awaiting_reply":
            return {"success": False, "error": f"会话状态不是等待回复: {session.status}"}

        # 更新会话状态
        session.updated_at = datetime.utcnow().isoformat()

        # 更新已回答的问题
        for q in session.questions:
            if not q.answered:
                q.answered = True
                q.answer = answer
                break

        # 检查是否还有未回答的问题
        # 不重写 session.questions：保留已回答问题（供 collected_info 完整返回），
        # 仅用 pending 判断会话状态。
        pending = [q for q in session.questions if not q.answered]
        if pending:
            session.status = "awaiting_reply"
        else:
            session.status = "completed"

        self._save_session(session)

        return {
            "success": True,
            "status": session.status,
            "pending_count": len(pending),
            "collected_info": {q.field: q.answer for q in session.questions if q.answered}
        }

    def check_timeout(self) -> List[ClarificationSession]:
        """检查超时的会话"""
        timeout_sessions = []
        now = datetime.utcnow()

        if self.redis_client:
            try:
                keys = self.redis_client.keys("clarification:*")
                for key in keys:
                    data = self.redis_client.get(key)
                    if data:
                        session = ClarificationSession.from_dict(json.loads(data))
                        if session.status == "awaiting_reply":
                            timeout_dt = datetime.fromisoformat(session.timeout_at)
                            if now > timeout_dt:
                                session.status = "timeout"
                                self._save_session(session)
                                timeout_sessions.append(session)
            except Exception:
                pass

        return timeout_sessions

    def escalate(self, session_id: str) -> Dict:
        """将会话升级到人工处理"""
        session = self.get_session(session_id)
        if not session:
            return {"success": False, "error": "会话不存在"}

        session.status = "escalated"
        self._save_session(session)

        return {
            "success": True,
            "message": "已升级到人工处理",
            "session": session.to_dict()
        }

    def generate_final_request(self, session_id: str) -> Optional[str]:
        """
        将原始需求和收集的信息合并为完整需求描述

        Args:
            session_id: 会话ID

        Returns:
            完整的用户需求描述
        """
        session = self.get_session(session_id)
        if not session:
            return None

        if session.status != "completed":
            return None

        # 构建完整需求描述
        parts = [session.original_request, "\n\n已确认信息："]
        for q in session.questions:
            parts.append(f"- {q.field}: {q.answer}")

        return "".join(parts)

    def format_questions_for_user(self, session: ClarificationSession) -> str:
        """
        格式化问题列表为用户友好的文本

        Returns:
            格式化后的问题文本
        """
        if not session.questions:
            return "您的问题已全部回答，谢谢！"

        lines = ["您好，为了更好地帮您处理需求，请补充以下信息：\n"]
        for i, q in enumerate(session.questions, 1):
            lines.append(f"{i}. {q.question}")

        lines.append("\n请回复告诉我，谢谢！")
        return "\n".join(lines)


def main():
    """CLI 入口（用于测试）"""
    import sys

    if len(sys.argv) < 2:
        print("用法: python clairifier.py <command> [args]")
        print("命令:")
        print("  analyze <text> - 分析需求是否完整")
        print("  session <session_id> - 获取会话状态")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "analyze":
        if len(sys.argv) < 3:
            print("用法: python clairifier.py analyze <text>")
            sys.exit(1)

        text = sys.argv[2]
        clairifier = RequirementClairifier()
        result = clairifier.analyze(text)

        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "session":
        if len(sys.argv) < 3:
            print("用法: python clairifier.py session <session_id>")
            sys.exit(1)

        session_id = sys.argv[2]
        clairifier = RequirementClairifier()
        session = clairifier.get_session(session_id)

        if session:
            print(json.dumps(session.to_dict(), ensure_ascii=False, indent=2))
        else:
            print("会话不存在")


if __name__ == "__main__":
    main()
