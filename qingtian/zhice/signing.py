"""执策 Ed25519 签名验证 — 调镇岳公钥 API 验签

文档 §3.4.3：Agent submit 时用 Ed25519 私钥对 check_results 签名，
引擎通过镇岳 API 获取公钥验签，结果存入 verifications.signature。

签名算法：
  signature = Ed25519_Sign(private_key, SHA256(canonical_json({
      "step_id": step_id, "task_id": task_id, ...check_results
  })))
  -- 签名消息绑定 step_id/task_id，防跨步骤/跨任务重放（R11 P1）
"""
import json
import logging
import httpx
import nacl.exceptions
from nacl.signing import SigningKey, VerifyKey

from . import config as cfg

logger = logging.getLogger("zhice.signing")

# 镇岳 API 地址
_BASE_URL = cfg.get_zhenyue_base_url()


def _canonical_json(data: dict) -> bytes:
    """生成规范 JSON 字节（排序 keys，紧凑格式），用于签名和验签。"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _signed_message(step_id: int, task_id: int, check_results: dict) -> bytes:
    """签名消息体：绑定 step/task 防跨步骤重放。

    R11 (P1): 原实现只对 check_results 签名——两个 Step 上报相同 check_results
    即可重放签名。现叠加 step_id/task_id（后置，保证覆盖同名键）。
    """
    payload = dict(check_results or {})
    payload["step_id"] = step_id
    payload["task_id"] = task_id
    return _canonical_json(payload)


def sign_check_results(private_key_hex: str, check_results: dict, step_id: int, task_id: int) -> str:
    """Agent 端签名：用 Ed25519 私钥对 (step_id, task_id, check_results) 签名。

    Args:
        private_key_hex: Ed25519 私钥（32 bytes hex = 64 hex chars）
        check_results: Agent 上报的 check_results dict
        step_id: 目标 Step ID（绑定进签名消息）
        task_id: 目标 Task ID（绑定进签名消息）

    Returns:
        签名（64 bytes hex = 128 hex chars）
    """
    try:
        sk_bytes = bytes.fromhex(private_key_hex)
    except ValueError:
        raise ValueError("invalid Ed25519 private key hex")

    sk = SigningKey(sk_bytes)
    message = _signed_message(step_id, task_id, check_results)
    signature = sk.sign(message).signature
    return signature.hex()


async def verify_signature(
    agent_id: str, step_id: int, task_id: int,
    check_results: dict, signature_hex: str,
) -> tuple[bool, str]:
    """引擎验签：通过镇岳 API 获取公钥，验证 Agent 对签名消息的签名。

    R11 (P1) fail-closed 语义：
      - 签名消息绑定 step_id/task_id → 防跨步骤/跨任务重放
      - Agent 已注册公钥但缺签名 → 拒绝（有签名能力的 Agent 必须签名）
      - Agent 无活跃公钥（404）→ 无签名能力，unsigned 向后兼容放行；
        此时若带了签名（无公钥可验）→ 拒绝

    Args:
        agent_id: Agent ID
        step_id: 目标 Step ID
        task_id: 目标 Task ID
        check_results: 原始 check_results dict
        signature_hex: Agent 提交的签名（128 hex chars）

    Returns:
        (valid: bool, error: str) — valid=True 时 error 为空
    """
    if signature_hex and len(signature_hex) != 128:
        return False, "signature 格式错误（需 64 bytes hex = 128 chars）"

    # 从镇岳获取公钥（同时判定 Agent 是否有签名能力）
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{_BASE_URL}/v1/zhenyue/agents/{agent_id}/public-key",
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 404:
                # 无活跃公钥：无签名能力 → unsigned 兼容；带了签名却无公钥 → 拒
                if signature_hex:
                    return False, f"Agent {agent_id} 无活跃公钥（需先调用镇岳生成密钥对）"
                return True, ""
            resp.raise_for_status()
            key_data = resp.json()
    except httpx.TimeoutException:
        logger.warning("Timeout fetching public key for %s", agent_id)
        return False, "获取公钥超时"
    except Exception as e:
        logger.exception("Failed to fetch public key for %s", agent_id)
        return False, f"获取公钥失败: {e}"

    public_key_hex = key_data.get("public_key")
    if not public_key_hex:
        if signature_hex:
            return False, f"Agent {agent_id} 无活跃公钥（需先调用镇岳生成密钥对）"
        return True, ""

    # Agent 已注册公钥 → 签名必须存在且有效（fail-closed，不再可选空转）
    if not signature_hex:
        return False, f"缺少签名：Agent {agent_id} 已注册公钥，submit 必须携带 Ed25519 签名"

    # Ed25519 验签
    try:
        pk_bytes = bytes.fromhex(public_key_hex)
        if len(pk_bytes) != 32:
            return False, f"公钥长度异常: {len(pk_bytes)} bytes（预期 32）"
        verify_key = VerifyKey(pk_bytes)
        message = _signed_message(step_id, task_id, check_results)
        verify_key.verify(message, bytes.fromhex(signature_hex))
        return True, ""
    except nacl.exceptions.BadSignatureError:
        logger.warning("Signature verification failed for agent=%s", agent_id)
        return False, "签名验证失败：签名与 step/task/check_results 不匹配"
    except Exception as e:
        logger.exception("Signature verification error for agent=%s", agent_id)
        return False, f"验签错误: {e}"
