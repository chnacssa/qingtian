#!/usr/bin/env python3
"""
镇岳审批通知桥接服务 v2
监听 localhost:6789，Plugin 拦截后 POST 通知到这里，
通过 bot-sys 飞书应用发 DM 给审批人。
不走 openclaw CLI，不走跨应用问题。
"""

import http.server
import hmac
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "6789"))

# bot-sys 飞书应用凭据（跟 openclaw.json 保持一致）
# 凭据从环境变量读取，禁止硬编码到源码（曾泄漏到仓库）
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

# P1 (2026-08-26 review #21): 原监听口完全无鉴权——同宿主机任何进程（含 untrusted
# Skill 子进程）可 POST 伪造审批 DM（"回复允许放行"钓鱼卡片），形成伪造通知→管理员
# 回复→越权放行链。现要求 X-Internal-Token == QINGTIAN_INTERNAL_IPC_TOKEN（与
# common/ipc_auth.py、gateway A2 内部通道同一约定），未配置时 fail-closed 拒绝全部
# POST（健康检查 GET 保留）。调用方（Plugin）需同步带上该头。
INTERNAL_TOKEN = os.environ.get("QINGTIAN_INTERNAL_IPC_TOKEN", "")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("notifier")

# token 缓存
_tenant_token = None
_token_expires_at = 0


def _get_tenant_token():
    """获取飞书 tenant_access_token"""
    global _tenant_token, _token_expires_at
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        log.error("FEISHU_APP_ID / FEISHU_APP_SECRET 未配置，无法发送审批通知")
        return ""
    if _tenant_token and time.time() < _token_expires_at - 60:
        return _tenant_token

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = json.dumps({
        "app_id": FEISHU_APP_ID,
        "app_secret": FEISHU_APP_SECRET,
    }).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    # #22: 无 timeout 时飞书 API 卡住则单线程 HTTPServer 永久阻塞
    resp = urllib.request.urlopen(req, timeout=10)
    body = json.loads(resp.read())
    _tenant_token = body.get("tenant_access_token", "")
    _token_expires_at = time.time() + body.get("expire", 7200)
    log.info("获取新 tenant_token 成功, 有效期 %d 秒", body.get("expire", 7200))
    return _tenant_token


def send_feishu_dm(open_id: str, agent_id: str, action: str, tool_name: str, reason: str):
    """通过 bot-sys 飞书应用发 DM"""
    token = _get_tenant_token()
    if not token:
        log.error("无法获取 tenant_token")
        return False

    content = {
        "text": (
            f"\uD83D\uDEE1\uFE0F 【镇岳审批请求】\n"
            f"Agent: {agent_id}\n"
            f"操作: {action}\n"
            f"工具: {tool_name}\n"
            f"原因: {reason}\n\n"
            f"回复【允许】放行，回复【拒绝】取消"
        )
    }
    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    body = json.dumps({
        "receive_id": open_id,
        "msg_type": "text",
        "content": json.dumps(content, ensure_ascii=False),
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        if result.get("code") == 0:
            log.info("DM 发送成功: open_id=%s", open_id)
            return True
        else:
            log.error("DM 发送失败: code=%s msg=%s", result.get("code"), result.get("msg"))
            return False
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        log.error("飞书 API 错误: %s %s", e.code, body)
        return False


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        # #21: 校验内部 token（fail-closed：未配置/不匹配一律拒绝）
        provided = self.headers.get("X-Internal-Token", "")
        if not INTERNAL_TOKEN or not hmac.compare_digest(provided, INTERNAL_TOKEN):
            log.warning(
                "拒绝无鉴权通知请求: remote=%s token_configured=%s",
                self.client_address[0], bool(INTERNAL_TOKEN),
            )
            self._respond(403, {"status": "error", "detail": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        data = json.loads(body) if body else {}

        open_id = data.get("open_id", "")
        agent_id = data.get("agent_id", "unknown")
        action = data.get("action", "unknown")
        tool_name = data.get("tool_name", "unknown")
        reason = data.get("reason", "需管理员审批")

        log.info(
            "收到通知: agent=%s action=%s open_id=%s",
            agent_id, action, open_id,
        )

        if not open_id:
            self._respond(400, {"status": "error", "detail": "missing open_id"})
            return

        ok = send_feishu_dm(open_id, agent_id, action, tool_name, reason)
        code = 200 if ok else 500
        status = "sent" if ok else "send_failed"
        self._respond(code, {"status": status, "result": status})

    def do_GET(self):
        self._respond(200, {"status": "ok", "version": "2.0"})

    def _respond(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, fmt, *args):
        log.info("HTTP %s", fmt % args)


def main():
    server = http.server.HTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
    log.info("镇岳审批通知桥接服务 v2 已启动，监听 127.0.0.1:%d", LISTEN_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("服务停止")
        server.server_close()


if __name__ == "__main__":
    main()
