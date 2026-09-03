"""
ClawHub/Hermes 外部免费 Skill 适配器基类

每个工具一个子目录，继承此类，SKILL.md 自动从同目录加载。
执行时通过 ctx.llm.chat() 将 SKILL.md 作为 system prompt 传给 LLM。

设计原理：
  这些 Skill 来自 ClawHub/Hermes 等平台的 SKILL.md 指令型技能。
  本身无可执行代码，只有 LLM 指令。通过 ctx.llm 执行 = 一条代码，
  无需为每个工具写 Python 逻辑。

安全设计：
  - 仅需 "llm" 权限（无 system/filesystem/network）
  - 纯文本处理，不执行系统命令
  - LLM 输出经 skill.json output_schema 校验
"""

import inspect
import logging
import os

from osskill.models import Skill

logger = logging.getLogger("osskill.clawhub_adapter")

# ── 通用的输入/输出 Schema ─────────────────────────

COMMON_INPUT_SCHEMA = {
    "type": "object",
    "required": ["input"],
    "properties": {
        "input": {
            "type": "string",
            "description": "用户输入（任务描述或数据）",
        },
        "format": {
            "type": "string",
            "description": "输出格式偏好（如 markdown / json / text）",
        },
    },
}

COMMON_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "result": {"type": "string"},
        "error": {"type": "string"},
    },
}


class ClawHubSkill(Skill):
    """外部 SKILL.md 指令型 Skill 基类

    子类只需定义类元数据（name / display_name / description / version）。
    SKILL.md 自动从子类所在目录加载。

    用法:
        class ExcelGeneratorSkill(ClawHubSkill):
            name = "excel_generator"
            display_name = "Excel 生成器"
            description = "根据数据生成 Excel 报表"
    """

    # ── 元数据（子类覆盖） ──
    name = ""
    display_name = ""
    description = ""
    category = "tool"
    version = "1.0.0"

    permissions = ["llm"]

    input_schema = COMMON_INPUT_SCHEMA
    output_schema = COMMON_OUTPUT_SCHEMA

    def __init__(self):
        super().__init__()
        self._skill_md = ""

    async def on_load(self, ctx) -> None:
        """加载时自动读取同目录下的 SKILL.md"""
        await super().on_load(ctx)
        mod_path = inspect.getfile(self.__class__)
        skill_dir = os.path.dirname(mod_path)
        md_path = os.path.join(skill_dir, "SKILL.md")
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                self._skill_md = f.read().strip()
            if self._skill_md:
                logger.info(
                    "Loaded SKILL.md for %s (%d chars)",
                    self.name, len(self._skill_md),
                )
            else:
                logger.warning("SKILL.md for %s is empty", self.name)
        except FileNotFoundError:
            logger.warning("SKILL.md not found for %s at %s", self.name, md_path)

    async def execute(self, params: dict) -> dict:
        """将 SKILL.md 作为 system prompt，通过 LLM 执行"""
        user_input = params.get("input", "")
        fmt = params.get("format", "markdown")

        if not self._skill_md:
            return {"ok": False, "error": "SKILL.md 未加载，请检查安装完整性"}

        try:
            result = await self.ctx.llm.chat([
                {
                    "role": "system",
                    "content": (
                        "你是一个企业办公技能执行器。\n"
                        "严格按照以下 SKILL.md 指令完成任务。\n"
                        "输出格式偏好（如未指定 markdown 则用原文格式）。\n\n"
                        f"{self._skill_md}"
                    ),
                },
                {
                    "role": "user",
                    "content": user_input if user_input else "请执行此技能",
                },
            ])

            return {"ok": True, "result": result}
        except PermissionError as e:
            return {"ok": False, "error": f"权限不足: {e}"}
        except Exception as e:
            logger.warning(
                "LLM execution failed for %s: %s", self.name, e,
            )
            return {"ok": False, "error": f"执行失败: {e}"}

    async def validate(self, params: dict) -> list[str]:
        errors = []
        if "input" not in params:
            errors.append("缺少必填参数: input")
        return errors


# loader 兼容别名（约定 class = PascalCase(skill_name) + "Skill"）
ClawhubAdapterSkill = ClawHubSkill
