"""指令词注册表单元测试 — CommandResolver + extract_command

纯逻辑测试，不依赖数据库。通过 mock 模拟 DB 数据。
"""
import os
import sys
import json

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from osskill.command_resolver import (
    CommandResolver,
    CommandInfo,
    extract_command,
    get_resolver,
)


class TestCommandInfo:
    """CommandInfo dataclass 基本行为"""

    def test_minimal(self):
        ci = CommandInfo(word="询价", skill_name="procurement", action="inquiry_create")
        assert ci.word == "询价"
        assert ci.skill_name == "procurement"
        assert ci.action == "inquiry_create"
        assert ci.description == ""
        assert ci.examples == []

    def test_full(self):
        ci = CommandInfo(
            word="询价", skill_name="procurement", action="inquiry_create",
            description="创建询价单", examples=["!!询价!! 10台变压器"],
        )
        assert ci.description == "创建询价单"
        assert ci.examples == ["!!询价!! 10台变压器"]


class TestCommandResolver:
    """CommandResolver 核心功能"""

    @pytest.fixture
    def resolver(self):
        r = CommandResolver()
        r._registry = {
            "询价": CommandInfo("询价", "procurement", "inquiry_create",
                              "创建询价单找供应商报价", ["!!询价!! 10台变压器"]),
            "比价": CommandInfo("比价", "procurement", "inquiry_get_results",
                              "对比各家供应商报价", ["!!比价!! 上次电缆报价"]),
            "新建报价": CommandInfo("新建报价", "sales", "quotation_create",
                                "给客户做报价单", ["!!新建报价!! 给ABC公司报电缆价"]),
            "投标评分": CommandInfo("投标评分", "bidding", "score_bid",
                              "评审标书打分", ["!!投标评分!! 项目A的标书"]),
            "记录": CommandInfo("记录", "secretary", "record",
                           "记一下", ["!!记录!! 下午3点开会"]),
        }
        return r

    # ── resolve ────────────────────────────────────────

    def test_resolve_exact(self, resolver):
        """精确匹配已有指令词"""
        ci = resolver.resolve("询价")
        assert ci is not None
        assert ci.action == "inquiry_create"
        assert ci.skill_name == "procurement"

    def test_resolve_nonexistent(self, resolver):
        """不存在的指令词 → None"""
        assert resolver.resolve("不存在") is None

    def test_resolve_empty(self, resolver):
        """空字符串 → None"""
        assert resolver.resolve("") is None

    def test_resolve_partial(self, resolver):
        """部分匹配不命中（不是前缀匹配）"""
        assert resolver.resolve("询") is None
        assert resolver.resolve("价") is None

    def test_resolve_short_word(self, resolver):
        """短指令词精确匹配"""
        ci = resolver.resolve("记录")
        assert ci is not None
        assert ci.action == "record"

    # ── search（模糊匹配）───────────────────────────────

    def test_search_exact(self, resolver):
        """搜索完整指令词 → 命中"""
        results = resolver.search("询价")
        assert len(results) >= 1
        word, conf = results[0]
        assert word.word == "询价"
        assert conf == 1.0

    def test_search_in_text(self, resolver):
        """指令词嵌在自然语言中 → 命中"""
        results = resolver.search("帮我询价10台变压器")
        assert len(results) >= 1
        assert results[0][0].word == "询价"

    def test_search_no_match(self, resolver):
        """完全不匹配 → 空列表"""
        assert resolver.search("今天天气不错") == []

    def test_search_empty(self, resolver):
        """空文本 → 空列表"""
        assert resolver.search("") == []
        assert resolver.search(None) == []

    def test_search_multiple_matches(self, resolver):
        """同时匹配多个指令词 → 全部返回，按置信度排序"""
        results = resolver.search("询价和比价")
        assert len(results) >= 2
        words = [r[0].word for r in results]
        assert "询价" in words
        assert "比价" in words

    # ── list_all ────────────────────────────────────────

    def test_list_all(self, resolver):
        """列出所有注册指令"""
        all_cmds = resolver.list_all()
        assert len(all_cmds) == 5
        words = {c.word for c in all_cmds}
        assert words == {"询价", "比价", "新建报价", "投标评分", "记录"}

    def test_list_all_empty(self):
        """空注册表 → 空列表"""
        r = CommandResolver()
        assert r.list_all() == []

    # ── load ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_load_from_valid_data(self):
        """从有效数据加载"""
        r = CommandResolver()

        class MockPool:
            def acquire(self):
                return self
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def fetch(self, sql):
                return [
                    {"name": "procurement", "commands": json.dumps([
                        {"word": "询价", "action": "inquiry_create", "description": "创建询价单", "examples": ["!!询价!! 10台变压器"]},
                        {"word": "比价", "action": "inquiry_get_results", "description": "比价", "examples": []},
                    ])},
                    {"name": "sales", "commands": json.dumps([
                        {"word": "新建报价", "action": "quotation_create", "description": "报价", "examples": []},
                    ])},
                ]

        await r.load(pool=MockPool())
        assert len(r.list_all()) == 3
        assert r.resolve("询价").skill_name == "procurement"
        assert r.resolve("新建报价").skill_name == "sales"

    @pytest.mark.asyncio
    async def test_load_empty_commands(self):
        """commands 为空列表时不应报错"""
        r = CommandResolver()

        class MockPoolEmpty:
            def acquire(self):
                return self
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def fetch(self, sql):
                return [
                    {"name": "procurement", "commands": []},
                    {"name": "sales", "commands": "[]"},
                    {"name": "bidding", "commands": None},
                ]

        await r.load(pool=MockPoolEmpty())
        assert len(r.list_all()) == 0  # 全部空 → 无指令

    @pytest.mark.asyncio
    async def test_load_no_active_skills(self):
        """没有活跃 Skill → 空注册表"""
        r = CommandResolver()

        class MockPoolEmpty:
            def acquire(self):
                return self
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def fetch(self, sql):
                return []

        await r.load(pool=MockPoolEmpty())
        assert r.list_all() == []

    # ── reload ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_reload(self):
        """reload 委托 load，指针交换后旧数据不可见"""
        r = CommandResolver()
        # 先注入一些数据
        r._registry = {"旧指令": CommandInfo("旧指令", "old", "old_action")}

        class MockPoolReload:
            def acquire(self):
                return self
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def fetch(self, sql):
                return [
                    {"name": "procurement", "commands": json.dumps([
                        {"word": "询价", "action": "inquiry_create"},
                    ])},
                ]

        await r.reload(pool=MockPoolReload())
        assert r.resolve("旧指令") is None  # 旧数据已清除
        assert r.resolve("询价") is not None  # 新数据生效


class TestExtractCommand:
    """extract_command — 锚定解析"""

    @pytest.fixture(autouse=True)
    def setup_resolver(self):
        """每个测试前确保 resolver 有数据"""
        r = get_resolver()
        r._registry = {
            "询价": CommandInfo("询价", "procurement", "inquiry_create"),
            "取消": CommandInfo("取消", "system", "cancel"),
        }

    # ── 正向测试 ────────────────────────────────────────

    def test_command_at_start(self):
        """消息开头 !!command!! → 正常解析"""
        ci = extract_command("!!询价!! 10台变压器")
        assert ci is not None
        assert ci.word == "询价"
        assert ci.action == "inquiry_create"

    def test_command_after_at_secretary(self):
        """@秘书 后 !!command!! → 正常解析"""
        ci = extract_command("@秘书 !!询价!! 10台变压器")
        assert ci is not None
        assert ci.word == "询价"

    def test_command_fullwidth_exclamation(self):
        """全角感叹号统一识别"""
        ci = extract_command("！！询价！！ 10台变压器")
        assert ci is not None
        assert ci.word == "询价"

    def test_command_mixed_exclamation(self):
        """混打全半角感叹号"""
        ci = extract_command("！!询价！!")
        assert ci is not None
        assert ci.word == "询价"

    def test_command_after_at_with_space(self):
        """@秘书 与 !!command!! 之间有空格"""
        ci = extract_command("@秘书  !!询价!!")
        assert ci is not None
        assert ci.word == "询价"

    # ── 安全测试（锚定） ────────────────────────────────

    def test_command_mid_message_ignored(self):
        """消息体中间藏匿 !!command!! → 忽略（锚定安全）"""
        ci = extract_command("今天天气不错!!询价!!")
        assert ci is None

    def test_command_mid_sentence_ignored(self):
        """句子中间的 !!command!! → 忽略"""
        ci = extract_command("帮我!!询价!!一下")
        assert ci is None

    def test_command_embedded_in_word_ignored(self):
        """嵌在词里 → 忽略"""
        ci = extract_command("test!!询价!!test")
        assert ci is None

    def test_command_in_url_ignored(self):
        """URL 中的 !! 不应触发"""
        ci = extract_command("请访问 http://example.com/!!test!!")
        assert ci is None

    def test_command_dangling_exclamation(self):
        """单个 ! 不触发"""
        ci = extract_command("!询价!")
        assert ci is None

    # ── 边界测试 ────────────────────────────────────────

    def test_command_empty_text(self):
        """空文本 → None"""
        assert extract_command("") is None
        assert extract_command(None) is None

    def test_command_whitespace_only(self):
        """纯空白 → None"""
        assert extract_command("   ") is None

    def test_command_no_exclamation(self):
        """没有感叹号 → None"""
        assert extract_command("询价") is None

    def test_command_nonexistent_word(self):
        """指令词未注册 → None（走 resolve 返回 None）"""
        ci = extract_command("!!不存在的指令!!")
        assert ci is None

    def test_command_trailing_text(self):
        """!!command!! 后跟其他文本"""
        ci = extract_command("!!询价!! \n 10台变压器")
        assert ci is not None
        assert ci.word == "询价"

    def test_command_newline_before(self):
        """换行后的 !!command!! → 忽略（锚定要求开头）"""
        ci = extract_command("前面有文字\n!!询价!!")
        assert ci is None

    # ── 全角空格边界 ────────────────────────────────────

    def test_command_fullwidth_space_after_at(self):
        """@秘书后全角空格 -> Python s 匹配 u3000，正常命中"""
        ci = extract_command("@秘书\u3000!!询价!!")
        assert ci is not None
        assert ci.word == "询价"
