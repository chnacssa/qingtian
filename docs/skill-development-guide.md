# Skill 开发指南 — 30 分钟写第一个 Skill

> ACSSA Skill 是运行在底座之上的可扩展能力模块。每个 Skill 就是一个 Python 类，Agent 启动时自动加载。

---

## 一、最小 Skill（5 分钟）

### 1.1 目录结构

```
skills/packages/my-first-skill/
├── __init__.py
├── skill.json
└── hello.py
```

### 1.2 skill.json

```json
{
  "name": "my-first-skill",
  "display_name": "我的第一个 Skill",
  "version": "1.0.0",
  "description": "一个简单的 Hello World Skill",
  "category": "demo",
  "tags": ["demo"],
  "entry": {
    "class": "HelloSkill",
    "file": "hello.py"
  },
  "permissions": ["llm"],
  "lifecycle": "on_demand",
  "license_info": { "type": "free" }
}
```

### 1.3 hello.py

```python
from osskill.models import Skill

class HelloSkill(Skill):
    name = "my-first-skill"
    display_name = "我的第一个 Skill"
    category = "demo"
    permissions = ["llm"]
    lifecycle = "on_demand"

    async def execute(self, params: dict) -> dict:
        name = params.get("name", "世界")
        return {"ok": True, "message": f"你好，{name}！"}
```

### 1.4 测试

```python
# Agent 调用
ctx.call_skill("my-first-skill", "execute", {"name": "ACSSA"})
# → {"ok": True, "message": "你好，ACSSA！"}
```

---

## 二、Skill 生命周期

```
on_load()  → execute() → on_unload()
   ↑                        ↑
 启动时调用              进程终止时调用
```

| 生命周期 | 说明 | 何时用 |
|---------|------|--------|
| `on_demand` | 按需加载，用完释放 | 工具类 Skill（文档处理） |
| `resident` | 常驻内存，后台任务 | 服务类 Skill（工作秘书） |

---

## 三、权限体系

| 权限 | 说明 | 适用场景 |
|------|------|---------|
| `llm` | 调用 LLM | 文本生成/分析/翻译 |
| `network` | 网络访问 | API 调用/文件下载 |
| `filesystem` | 文件读写 | 文档处理/数据导入 |
| `skills` | 调用其他 Skill | 编排/协作场景 |

**原则**：只声明你真正需要的权限。SAST 扫描会根据权限做不同的安全检查。

---

## 四、调用底座 API

Skill 通过 `ctx.api` 访问底座服务，不需要直接连数据库：

```python
async def execute(self, params: dict) -> dict:
    # 搜索记忆
    memories = await self.ctx.api.post("/v1/yongheng/memories/search", {
        "namespace": f"agent:{self.ctx.agent_id}",
        "query": params.get("query", ""),
        "method": "hybrid",
        "top_k": 5,
    })

    # 调用 LLM
    answer = await self.ctx.llm.chat([
        {"role": "system", "content": "你是问答助理。"},
        {"role": "user", "content": params["question"]},
    ])

    return {"ok": True, "answer": answer}
```

**常用 API**：

| API | 用途 |
|-----|------|
| `POST /v1/yongheng/memories` | 写入记忆 |
| `POST /v1/yongheng/memories/search` | 搜索记忆 |
| `GET /v1/yongheng/trajectory` | 读取操作轨迹 |
| `GET /v1/huanyu/reminders/pending` | 待处理提醒 |
| `POST /v1/huanyu/admin-messages` | 推送管理消息 |
| `POST /v1/xixing/agent/{id}/learn` | 提交知识到吸星 |

---

## 五、参考 Skill

| Skill | 路径 | 学什么 |
|-------|------|--------|
| **工作秘书** | `osskill/implementations/work_secretary/` | resident 生命周期、后台任务、多模块架构、NL 路由、降级策略 |
| **监管适配器** | `osskill/implementations/regulatory_adapter/` | 外部 SKILL.md 导入、审计日志、路径安全 |

---

## 六、上架到 Skill 市场

```
1. 确保 skill.json 字段完整
2. 写 README.md（功能说明 + 使用示例）
3. 提交到 acssa.cn → 自动 SAST 扫描
4. 人工审核 → 上架
```

### skill.json 完整字段

```json
{
  "name": "my-skill",
  "display_name": "显示名称",
  "version": "1.0.0",
  "description": "一句话描述",
  "category": "productivity",
  "tags": ["标签1", "标签2"],
  "author": {
    "type": "individual",
    "name": "你的名字",
    "contact": "email@example.com"
  },
  "copyright": {
    "declaration": "© 2026 你的名字",
    "license": "Apache-2.0"
  },
  "entry": { "class": "MySkill", "file": "main.py" },
  "permissions": ["llm"],
  "lifecycle": "on_demand",
  "license_info": { "type": "free", "price": 0 },
  "dependencies": { "qingtian": ">=2.0.0", "skills": {} }
}
```

---

## 七、常见问题

**Q: Skill 可以调用其他 Skill 吗？**
A: 可以。声明 `permissions: ["skills"]`，然后用 `ctx.call_skill(name, method, params)`。

**Q: Skill 能直接操作数据库吗？**
A: 不能，也不应该。Skill 通过 `ctx.api` 访问底座服务，不直连 DB。

**Q: 免费 Skill 和付费 Skill 的区别？**
A: 功能上无区别。付费 Skill 多了 License 校验和 Ed25519 签名。开发时不需要关心这些——底座自动处理。
