"""
产品目录模块 — 消息附件辅助函数

提供在消息中携带文件/图片引用的标准化 payload 格式。

使用流程：
  1. Agent 通过 file_service 上传文件，获得 file_id
  2. Agent 调用 send_product_file() 发送消息带 file_id 引用
  3. 对方 Agent 收到消息后，从 payload 提取 file_id 到 file_service 下载

解耦说明：
  - 不直接导入 huanyu.messaging，通过 MessageSender 协议注入
  - 默认实现 HuanyuMessageSender 在运行时延迟导入 huanyu

payload 格式规范：
  message_type = "file" 时:
    payload = {
        "type_hint": "product_catalog" | "price_list" | "document" | "image" | "other",
        "file_id": "abc123...",
        "filename": "catalog.xlsx",
        "file_size": 12345,
        "file_sha256": "def456...",
        "enterprise_id": "ent-001",
        "description": "2026Q3 updated catalog",
    }

  message_type = "image" 时:
    payload = {
        "type_hint": "product_image",
        "image_id": "uuid...",           # product_images 表 ID
        "product_id": "uuid...",
        "product_name": "SF6断路器LW36-126",
        "file_id": "abc123...",
        "filename": "product_photo.jpg",
        "enterprise_id": "ent-001",
    }
"""

import httpx
import logging
import os
from typing import Protocol, runtime_checkable

logger = logging.getLogger("product.messaging")


# ── 消息发送接口 ─────────────────────────────────────


@runtime_checkable
class MessageSender(Protocol):
    """消息发送接口 — 与底层消息系统解耦。"""

    async def send_message(
        self,
        from_agent: str,
        to_agent: str,
        message_type: str,
        payload: dict,
    ) -> dict: ...


class ApiMessageSender:
    """通过 REST API 发送消息，不直接调用 huanyu.messaging。"""

    async def send_message(
        self,
        from_agent: str,
        to_agent: str,
        message_type: str,
        payload: dict,
    ) -> dict:
        base_url = os.environ.get("QINGTIAN_API_URL", "http://127.0.0.1:1996")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/v1/huanyu/messages",
                json={
                    "from_agent": from_agent,
                    "to_agent": to_agent,
                    "message_type": message_type,
                    "payload": payload,
                },
            )
            resp.raise_for_status()
            return resp.json()


_sender: MessageSender = ApiMessageSender()


def set_sender(sender: MessageSender) -> None:
    """替换默认消息发送器（测试 mock 或切换底层时使用）。"""
    global _sender
    _sender = sender


# ── 公开辅助函数 ────────────────────────────────────


async def send_product_file(
    from_agent: str,
    to_agent: str,
    enterprise_id: str,
    file_id: str,
    filename: str,
    type_hint: str = "other",
    file_size: int = 0,
    file_sha256: str = "",
    description: str = "",
    sender: MessageSender | None = None,
) -> dict:
    """发送带文件引用的消息。

    Args:
        from_agent: 发送方 agent_id
        to_agent: 接收方 agent_id
        enterprise_id: 企业 ID
        file_id: file_service 返回的文件 ID
        filename: 原始文件名
        type_hint: 文件类型提示 (product_catalog/price_list/document/image/other)
        file_size: 文件大小（字节）
        file_sha256: 文件 SHA256
        description: 描述信息
        sender: 可选的消息发送器，默认使用模块级 _sender

    Returns:
        send_message 的返回结果
    """
    actual = sender or _sender
    payload = {
        "type_hint": type_hint,
        "file_id": file_id,
        "filename": filename,
        "file_size": file_size,
        "file_sha256": file_sha256,
        "enterprise_id": enterprise_id,
        "description": description,
    }

    result = await actual.send_message(
        from_agent=from_agent,
        to_agent=to_agent,
        message_type="file",
        payload=payload,
    )
    logger.info(
        "send_product_file: %s → %s (%s: %s)",
        from_agent[:8], to_agent[:8], type_hint, filename,
    )
    return result


async def send_product_image(
    from_agent: str,
    to_agent: str,
    enterprise_id: str,
    image_id: str,
    product_id: str,
    product_name: str,
    file_id: str,
    filename: str,
    sender: MessageSender | None = None,
) -> dict:
    """发送产品图片消息。

    Args:
        from_agent: 发送方 agent_id
        to_agent: 接收方 agent_id
        enterprise_id: 企业 ID
        image_id: product_images 表 image_id
        product_id: 关联 product_id
        product_name: 产品名称
        file_id: file_service 返回的文件 ID
        filename: 原始文件名
        sender: 可选的消息发送器，默认使用模块级 _sender

    Returns:
        send_message 的返回结果
    """
    actual = sender or _sender
    payload = {
        "type_hint": "product_image",
        "image_id": image_id,
        "product_id": product_id,
        "product_name": product_name,
        "file_id": file_id,
        "filename": filename,
        "enterprise_id": enterprise_id,
    }

    result = await actual.send_message(
        from_agent=from_agent,
        to_agent=to_agent,
        message_type="image",
        payload=payload,
    )
    logger.info(
        "send_product_image: %s → %s (%s)",
        from_agent[:8], to_agent[:8], product_name,
    )
    return result
