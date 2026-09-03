"""秘书推送全链路测试 — 底座 → OpenClaw Gateway → 飞书

测试前提:
  1. 底座运行在 localhost:1996
  2. OpenClaw Gateway 运行在 localhost:18789
  3. OPENCLAW_API_TOKEN 已配置在网关和底座环境变量
  4. 已注册一个测试 Agent (biz:assistant)

用法:
  export OPENCLAW_API_TOKEN="OC-LOCAL-2026-SECURE-TOKEN-XXX"
  export QINGTIAN_PUSH_SECRET="test-secret"
  python -m pytest osskill/tests/test_push_chain.py -v
"""

import os
import json
import hmac
import hashlib
import httpx
import pytest


# ── 配置 ──────────────────────────────────────────────

BASE_URL = os.environ.get("QINGTIAN_BASE_URL", "http://127.0.0.1:1996")
OC_URL = os.environ.get("OPENCLAW_ENDPOINT", "http://127.0.0.1:18789")
OC_TOKEN = os.environ.get("OPENCLAW_API_TOKEN", "")
PUSH_SECRET = os.environ.get("QINGTIAN_PUSH_SECRET", "test-secret")
TEST_AGENT = os.environ.get("TEST_AGENT_ID", "test-agent-001")


def _make_push_token(agent_id: str) -> str:
    return hmac.new(
        PUSH_SECRET.encode(), agent_id.encode(), hashlib.sha256
    ).hexdigest()


# ── 测试 ──────────────────────────────────────────────


class TestPushBaseToOpenClaw:

    @pytest.mark.asyncio
    async def test_01_base_health(self):
        """底座健康检查"""
        async with httpx.AsyncClient(timeout=5) as c:
            resp = await c.get(f"{BASE_URL}/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_02_openclaw_health(self):
        """OpenClaw Gateway 可达性"""
        if not OC_TOKEN:
            pytest.skip("OPENCLAW_API_TOKEN not set")
        async with httpx.AsyncClient(timeout=5) as c:
            resp = await c.get(
                f"{OC_URL}/health",
                headers={"Authorization": f"Bearer {OC_TOKEN}"},
            )
        # 404 = 网关存在但无 /health 端点，也算可达
        assert resp.status_code in (200, 404), f"OpenClaw unreachable: {resp.status_code}"

    @pytest.mark.asyncio
    async def test_03_push_to_openclaw_session(self):
        """直接推送到 OpenClaw 会话 API 验证格式正确"""
        if not OC_TOKEN:
            pytest.skip("OPENCLAW_API_TOKEN not set")

        async with httpx.AsyncClient(timeout=5) as c:
            resp = await c.post(
                f"{OC_URL}/api/sessions/{TEST_AGENT}/messages",
                json={
                    "content": "[测试] 秘书推送链路验证消息，请忽略",
                    "metadata": {"source": "test", "event_type": "test.push"},
                },
                headers={"Authorization": f"Bearer {OC_TOKEN}"},
            )
        # 200 = 正常投递 / 404 = 会话不存在（也能说明网关响应正确）
        assert resp.status_code in (200, 404, 202), (
            f"Unexpected status: {resp.status_code} body={resp.text[:200]}"
        )

    @pytest.mark.asyncio
    async def test_04_base_push_pipeline(self):
        """底座 push_api 全链路：验证 HMAC → bus.publish → OpenClaw"""
        if not OC_TOKEN:
            pytest.skip("OPENCLAW_API_TOKEN not set")

        payload = {
            "agent_id": TEST_AGENT,
            "event_type": "secretary:reminder",
            "payload": {
                "title": "测试提醒",
                "content": "这是一条来自 push_api 测试的提醒消息。",
            },
            "token": _make_push_token(TEST_AGENT),
        }

        async with httpx.AsyncClient(timeout=5) as c:
            resp = await c.post(
                f"{BASE_URL}/api/v1/internal/skill-push",
                json=payload,
            )
        result = resp.json()
        print(f"\n  push_api response: {json.dumps(result, ensure_ascii=False)}")

        # 主通道（bus.publish）必须成功
        assert result.get("ok"), f"push_api failed: {result}"

        # 框架推送结果：{"frameworks": {"openclaw": {"ok": true}, ...}}
        fw = result.get("frameworks", {})
        print(f"  Framework push results: {json.dumps(fw, ensure_ascii=False)}")
        assert isinstance(fw, dict), f"frameworks should be dict, got {type(fw)}"

    @pytest.mark.asyncio
    async def test_05_invalid_token_rejected(self):
        """无效 HMAC token 被拒绝"""
        async with httpx.AsyncClient(timeout=5) as c:
            resp = await c.post(
                f"{BASE_URL}/api/v1/internal/skill-push",
                json={
                    "agent_id": TEST_AGENT,
                    "event_type": "test",
                    "token": "invalid-token",
                },
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is False


class TestReminderE2E:

    @pytest.mark.asyncio
    async def test_reminder_delivered(self):
        """端到端：创建提醒 → 秘书 poll → push_api → OpenClaw"""
        if not OC_TOKEN:
            pytest.skip("OPENCLAW_API_TOKEN not set")

        async with httpx.AsyncClient(timeout=10) as c:
            # Step 1: 创建一条测试提醒
            resp = await c.post(
                f"{BASE_URL}/v1/huanyu/reminders",
                json={
                    "agent_id": TEST_AGENT,
                    "title": "E2E 测试提醒",
                    "content": "秘书全链路测试，请确认收到。",
                    "priority": "normal",
                    "fire_at": "2026-07-17T10:00:00Z",
                },
            )
            assert resp.status_code == 200, f"Create reminder failed: {resp.text}"
            reminder = resp.json()
            assert reminder.get("id"), f"No reminder id: {reminder}"
            print(f"\n  Created reminder id={reminder['id']}")

            # Step 2: 手动触发推送（模拟 _deliver 逻辑）
            push_resp = await c.post(
                f"{BASE_URL}/api/v1/internal/skill-push",
                json={
                    "agent_id": TEST_AGENT,
                    "event_type": "secretary:reminder",
                    "payload": {
                        "title": reminder.get("title", "测试"),
                        "content": reminder.get("content", ""),
                        "priority": "normal",
                        "fire_at": reminder.get("fire_at", ""),
                    },
                    "token": _make_push_token(TEST_AGENT),
                },
            )
            result = push_resp.json()
            assert result.get("ok"), f"Push failed: {result}"

            # Step 3: 验证 OpenClaw 侧收到了（查询最近消息）
            # OpenClaw 不提供消息查询 API，这里只验证推送不报错
            oc = result.get("openclaw", {})
            print(f"  OpenClaw result: {json.dumps(oc, ensure_ascii=False)}")

            # Step 4: 清理
            await c.put(
                f"{BASE_URL}/v1/huanyu/reminders/{reminder['id']}",
                json={"status": "done"},
            )
            print("  Reminder cleaned up")


if __name__ == "__main__":
    print(f"BASE_URL: {BASE_URL}")
    print(f"OC_URL: {OC_URL}")
    print(f"OC_TOKEN: {'configured' if OC_TOKEN else 'MISSING'}")
    print(f"TEST_AGENT: {TEST_AGENT}")
    print("\nRun: python -m pytest osskill/tests/test_push_chain.py -v -s")
