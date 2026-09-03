"""
BaseSkill 基类单元测试
"""

import pytest
from osskill.models import Skill, SkillContext

# 向后兼容别名
BaseSkill = Skill


class TestBaseSkill:
    def test_abstract_cannot_instantiate(self):
        """BaseSkill 是抽象类，不能直接实例化"""
        with pytest.raises(TypeError):
            BaseSkill()

    def test_concrete_skill_minimal(self):
        """最小实现：只实现 execute"""
        class MySkill(BaseSkill):
            name = "my_skill"
            display_name = "My Skill"
            description = "A test skill"

            async def execute(self, params: dict) -> dict:
                return {"result": "ok"}

        skill = MySkill()
        assert skill.name == "my_skill"
        assert skill.display_name == "My Skill"
        assert skill.version == "1.0.0"  # 默认值
        assert skill.input_schema == {"type": "object", "properties": {}}
        assert skill.output_schema == {"type": "object", "properties": {}}
        assert skill.knowledge_deps == []
        assert skill.tool_deps == []
        assert skill.model_deps == ""

    def test_custom_metadata(self):
        """子类可以覆盖所有元数据字段"""
        class CustomSkill(BaseSkill):
            name = "custom_skill"
            display_name = "自定义技能"
            description = "一个自定义测试技能"
            category = "bidding"
            version = "2.1.0"
            knowledge_deps = ["工程材料价格", "定额库"]
            tool_deps = ["calculator", "web_search"]
            model_deps = "deepseek-v4-pro"

            async def execute(self, params: dict) -> dict:
                return {"custom": True}

        skill = CustomSkill()
        assert skill.category == "bidding"
        assert skill.version == "2.1.0"
        assert "工程材料价格" in skill.knowledge_deps
        assert "calculator" in skill.tool_deps
        assert skill.model_deps == "deepseek-v4-pro"

    def test_input_schema_validation(self):
        """validate 方法检查 required 字段"""
        class ValidatedSkill(BaseSkill):
            name = "validated"
            display_name = "Validated"
            description = "test"
            input_schema = {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "age": {"type": "integer"},
                },
                "required": ["name", "age"],
            }

            async def execute(self, params: dict) -> dict:
                return params

        skill = ValidatedSkill()

    @pytest.mark.asyncio
    async def test_validate_missing_required(self):
        """缺少 required 字段时返回错误列表"""

        class TestSkill(BaseSkill):
            name = "test"
            display_name = "Test"
            description = "test"
            input_schema = {
                "type": "object",
                "required": ["name", "score"],
            }

            async def execute(self, params: dict) -> dict:
                return params

        skill = TestSkill()
        errors = await skill.validate({"name": "foo"})
        assert len(errors) == 1
        assert "score" in errors[0]

    @pytest.mark.asyncio
    async def test_validate_all_present(self):
        """所有 required 字段都存在时无错误"""

        class TestSkill(BaseSkill):
            name = "test"
            display_name = "Test"
            description = "test"
            input_schema = {
                "type": "object",
                "required": ["name", "score"],
            }

            async def execute(self, params: dict) -> dict:
                return params

        skill = TestSkill()
        errors = await skill.validate({"name": "foo", "score": 95})
        assert errors == []

    @pytest.mark.asyncio
    async def test_validate_no_required_schema(self):
        """input_schema 没有 required 字段时 validate 通过"""
        class LooseSkill(BaseSkill):
            name = "loose"
            display_name = "Loose"
            description = "test"

            async def execute(self, params: dict) -> dict:
                return params

        skill = LooseSkill()
        errors = await skill.validate({"anything": "goes"})
        assert errors == []


class TestSkillContext:
    def test_minimal_context(self):
        ctx = SkillContext(agent_id="agent-001")
        assert ctx.agent_id == "agent-001"
        assert ctx.config == {}

    def test_context_with_config(self):
        ctx = SkillContext(agent_id="agent-002", config={"model": "deepseek-v4"})
        assert ctx.config["model"] == "deepseek-v4"

    def test_context_default_config(self):
        ctx = SkillContext(agent_id="agent-003", config=None)
        assert ctx.config == {}
