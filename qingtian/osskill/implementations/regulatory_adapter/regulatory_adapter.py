"""
监管适配器 — 将外部 SKILL.md 纳入羲和+镇岳安全监管体系

不做格式转换（SKILL.md 原样执行），只做安全接入：
  - 羲和层：子进程隔离 + 内存限制 + 权限门控
  - 镇岳层：HMAC 签名验证 + 哈希链审计

用法：
  提交到 acssa.cn 的外部 Skill = skill.json + SKILL.md
  无需写 Python，适配器自动包装执行。
"""

import logging
import os

from osskill.models import Skill, SkillContext

logger = logging.getLogger("osskill.regulatory_adapter")


class RegulatoryAdapter(Skill):
    """监管适配器 — 将外部 SKILL.md 指令型 Skill 纳入ACSSA安全体系

    与裸跑在 OpenClaw/Claude Code/Hermes 上的区别：
      裸跑 → 无隔离、无权限、无审计
      适配器 → 羲和子进程 + L1权限 + 镇岳审计日志
    """

    name = "regulatory-adapter"
    display_name = "监管适配器"
    description = "将外部 SKILL.md 格式的指令型 Skill 纳入ACSSA羲和+镇岳安全监管体系执行"
    category = "system"
    version = "1.0.0"

    permissions = ["llm", "filesystem", "network"]  # 代码使用 ctx.filesystem.read + ctx.api.post

    input_schema = {
        "type": "object",
        "required": ["input", "skill_path"],
        "properties": {
            "input": {
                "type": "string",
                "description": "用户输入（传给 Skill 的任务描述或数据）",
            },
            "skill_path": {
                "type": "string",
                "description": "SKILL.md 文件路径（相对于 Skill data 目录）",
            },
        },
    }

    output_schema = {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "result": {"type": "string"},
            "error": {"type": "string"},
        },
    }

    async def execute(self, params: dict) -> dict:
        """执行外部 Skill 指令

        1. 校验 skill_path 白名单
        2. 加载 SKILL.md（走 filesystem 代理，防路径遍历）
        3. 作为 system prompt 传给 LLM
        4. 写入镇岳审计日志
        5. 返回 LLM 执行结果
        """
        user_input = params.get("input", "")
        skill_path = params.get("skill_path", "")

        if not skill_path:
            return {"ok": False, "error": "缺少 skill_path 参数"}
        if not self._is_safe_path(skill_path):
            return {"ok": False, "error": f"Skill 路径不在允许目录内: {skill_path}"}

        # 加载 SKILL.md（走 ctx.filesystem 代理，羲和子进程隔离 + 路径校验）
        try:
            skill_md = self.ctx.filesystem.read(skill_path)
        except (FileNotFoundError, PermissionError) as e:
            return {"ok": False, "error": f"无法加载 Skill 文件: {e}"}
        except OSError as e:
            return {"ok": False, "error": f"读取 Skill 文件失败: {e}"}

        if not skill_md.strip():
            return {"ok": False, "error": "SKILL.md 内容为空"}

        skill_name = self._extract_name(skill_md, skill_path)

        # 通过底座 LLM 代理层执行（受羲和权限门控保护）
        try:
            result = await self.ctx.llm.chat([
                {
                    "role": "system",
                    "content": (
                        "你是一个技能执行器。严格按照以下 SKILL.md 指令完成任务。\n\n"
                        f"{skill_md}"
                    ),
                },
                {"role": "user", "content": user_input if user_input else "请执行此技能"},
            ])

            # 审计日志：记录外部 Skill 执行
            await self._audit_log(skill_name, skill_path, "completed")

            return {"ok": True, "result": result}
        except PermissionError as e:
            await self._audit_log(skill_name, skill_path, f"permission_denied: {e}")
            return {"ok": False, "error": f"权限不足: {e}"}
        except Exception as e:
            logger.warning("LLM execution failed for %s: %s", skill_name, e)
            await self._audit_log(skill_name, skill_path, f"failed: {e}")
            return {"ok": False, "error": f"执行失败: {e}"}

    @staticmethod
    def _is_safe_path(skill_path: str) -> bool:
        """校验路径是否在允许目录内（防路径遍历）"""
        import os
        skill_home = os.environ.get("SKILL_HOME", "")
        allowed = [os.path.realpath(p) for p in [skill_home, os.path.expanduser("~")] if p]
        real = os.path.realpath(skill_path)
        return any(real.startswith(prefix + os.sep) or real == prefix for prefix in allowed)

    @staticmethod
    def _extract_name(skill_md: str, fallback: str) -> str:
        """从 SKILL.md 提取 name 字段，降级用路径"""
        for line in skill_md.splitlines()[:10]:
            if line.startswith("name:") or line.startswith("# "):
                return line.split(":", 1)[-1].strip().lstrip("#").strip()[:100]
        return fallback.rsplit("/", 1)[-1] if "/" in fallback else fallback

    async def _audit_log(self, skill_name: str, skill_path: str, status: str) -> None:
        """写入镇岳审计日志（POST /v1/zhenyue/audit/logs，AuditEntryRequest 形状）"""
        severity = "medium" if status.startswith(("failed", "permission_denied")) else "low"
        try:
            await self.ctx.api.post("/v1/zhenyue/audit/logs", {
                "agent_id": self.ctx.agent_id,
                "action": "external_skill.execute",
                "target_type": "external_skill",
                "target_id": skill_name,
                "severity": severity,
                "detail": {
                    "skill_path": skill_path,
                    "status": status,
                },
            })
        except Exception:
            logger.debug("Audit log write failed for %s", skill_name)


# loader 兼容别名（约定 class = PascalCase(skill_name) + "Skill"）
RegulatoryAdapterSkill = RegulatoryAdapter
