---
name: zhice-gateway
description: "执策任务网关 — 消息拦截，自动通过执策引擎创建和管理 Agent 任务"
metadata:
  openclaw:
    kind: gateway
    emoji: "🧩"
    events:
      - "inbound_claim"
      - "message_received"
---

# 执策 Gateway 插件

拦截 Agent 消息，自动通过执策引擎创建和管理任务。

## 配置

```json
{
  "plugins": {
    "entries": {
      "zhice-gateway": {
        "enabled": true,
        "config": {
          "zhiceEndpoint": "http://localhost:1996/v1/zhice",
          "minInstructionLength": 40,
          "enforceStepByStep": true,
          "excludePatterns": ["^(好的|好|收到|了解了|明白|ok|OK|好的|行|可以|嗯|哦|哈哈|666|777|PONG)"],
          "enabledAgents": ["manager", "sys-eng", "ops-agent"]
        }
      }
    }
  }
}
```
