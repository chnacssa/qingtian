"""执策行为规范测试 — policy_check + CRUD"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ══════════════════════════════════════════════════════════
# _check_keyword / _check_pattern / _check_scope_substring
# ══════════════════════════════════════════════════════════

from zhice.policy_service import _check_keyword, _check_pattern, _check_scope_substring, _collect_text


class TestKeyword:
    def test_hit_exact(self):
        assert _check_keyword("帮我订一张去三亚的机票", {"keywords": ["机票", "旅游"]})

    def test_hit_case_insensitive(self):
        assert _check_keyword("HOTEL booking please", {"keywords": ["hotel", "flight"]})

    def test_no_hit(self):
        assert not _check_keyword("帮我查水泥报价", {"keywords": ["机票", "旅游"]})

    def test_empty_keywords(self):
        assert not _check_keyword("anything", {"keywords": []})


class TestPattern:
    def test_hit(self):
        assert _check_pattern("帮我写一篇毕业论文", {"patterns": [r"帮.*写.*论文", r"代.*考试"]})

    def test_case_insensitive(self):
        assert _check_pattern("HELP ME WRITE A THESIS", {"patterns": [r"write.*thesis"]})

    def test_no_hit(self):
        assert not _check_pattern("帮我查报价", {"patterns": [r"帮你写.*论文"]})

    def test_invalid_pattern_skipped(self):
        assert not _check_pattern("anything", {"patterns": ["***invalid[[[regex"]})


class TestScopeSubstring:
    def test_allow_hit(self):
        allowed, denied = _check_scope_substring(
            "帮我查一下特种水泥42.5号的最新报价",
            {"allow": ["采购询价", "报价", "供应商洽谈"]},
        )
        assert allowed
        assert not denied

    def test_deny_hit(self):
        allowed, denied = _check_scope_substring(
            "帮我订一张去三亚的机票",
            {"allow": ["采购询价"], "deny": ["机票", "旅游"]},
        )
        assert not allowed
        assert denied

    def test_not_in_scope(self):
        allowed, denied = _check_scope_substring(
            "帮我写个 Python 爬虫",
            {"allow": ["采购询价", "合同签署"]},
        )
        assert not allowed
        assert not denied


class TestCollectText:
    def test_collects_all_fields(self):
        text = _collect_text({
            "title": "部署服务",
            "description": "部署到生产环境",
            "steps": [
                {"instruction": "拉代码"},
                {"instruction": "重启服务"},
            ],
        })
        assert "部署服务" in text
        assert "部署到生产环境" in text
        assert "拉代码" in text
        assert "重启服务" in text


# ══════════════════════════════════════════════════════════
# policy_check — 集成测试
# ══════════════════════════════════════════════════════════

class TestPolicyCheck:

    @pytest.mark.asyncio
    async def test_scope_deny_warn_then_block(self):
        """scope#1 deny旅游→warn，scope#2 deny机票→block → block 应生效"""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[
            {"name": "scope1-旅游warn", "policy_type": "scope",
             "rule": {"deny": ["旅游"]}, "action": "warn", "priority": 5, "reject_message": ""},
            {"name": "scope2-机票block", "policy_type": "scope",
             "rule": {"deny": ["机票"]}, "action": "block", "priority": 10, "reject_message": "禁止订票"},
        ])
        conn.fetchval = AsyncMock(return_value="biz:seller")
        pool = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.acquire.return_value = ctx

        with patch("zhice.policy_service.get_pool", AsyncMock(return_value=pool)):
            result = await __import__("zhice.policy_service", fromlist=[""]).policy_check(
                "biz:seller-01", {"title": "订机票去旅游", "description": "", "steps": []},
            )
        assert not result["allowed"]
        assert "scope2" in result["matched_policy"]
        assert result["matched_policy"] == "scope2-机票block"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_no_policies_allows(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchval = AsyncMock(return_value=None)
        pool = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.acquire.return_value = ctx

        with patch("zhice.policy_service.get_pool", AsyncMock(return_value=pool)):
            result = await __import__("zhice.policy_service", fromlist=[""]).policy_check(
                "agent1", {"title": "test", "description": "test", "steps": []},
            )
            assert result["allowed"]

    @pytest.mark.asyncio
    async def test_keyword_block(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{
            "name": "销售禁词", "policy_type": "keyword",
            "rule": {"keywords": ["机票", "旅游"]},
            "action": "block", "reject_message": "不能处理旅游相关请求",
            "priority": 10,
        }])
        conn.fetchval = AsyncMock(return_value="biz:seller")
        pool = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.acquire.return_value = ctx

        with patch("zhice.policy_service.get_pool", AsyncMock(return_value=pool)):
            result = await __import__("zhice.policy_service", fromlist=[""]).policy_check(
                "biz:seller-01", {"title": "订机票", "description": "去三亚", "steps": []},
            )
        assert not result["allowed"]
        assert result["action"] == "block"
        assert "旅游" in result["message"]

    @pytest.mark.asyncio
    async def test_scope_allows(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{
            "name": "销售范围", "policy_type": "scope",
            "rule": {"allow": ["采购询价", "报价", "供应商"]},
            "action": "block", "reject_message": "不在服务范围",
            "priority": 10,
        }])
        conn.fetchval = AsyncMock(return_value="biz:seller")
        pool = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.acquire.return_value = ctx

        with patch("zhice.policy_service.get_pool", AsyncMock(return_value=pool)):
            result = await __import__("zhice.policy_service", fromlist=[""]).policy_check(
                "biz:seller-01", {"title": "询价", "description": "查水泥报价", "steps": []},
            )
        assert result["allowed"]

    @pytest.mark.asyncio
    async def test_scope_llm_fallback_rejects(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{
            "name": "销售范围", "policy_type": "scope",
            "rule": {"allow": ["采购询价"]},
            "action": "block", "reject_message": "不在服务范围",
            "priority": 10,
        }])
        conn.fetchval = AsyncMock(return_value="biz:seller")
        pool = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.acquire.return_value = ctx

        mock_llm_resp = MagicMock()
        mock_llm_resp.raise_for_status = MagicMock()
        mock_llm_resp.json.return_value = {"choices": [{"message": {"content": "NO"}}]}

        with patch("zhice.policy_service.get_pool", AsyncMock(return_value=pool)):
            with patch("zhice.policy_service.cfg.get_llm_api_key", return_value="test-key"):
                with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                    mock_post.return_value = mock_llm_resp
                    result = await __import__("zhice.policy_service", fromlist=[""]).policy_check(
                        "biz:seller-01", {"title": "帮我写爬虫", "description": "爬取数据", "steps": []},
                    )
        assert not result["allowed"]  # 有 key 且 LLM 判定 NO → 拦截

    @pytest.mark.asyncio
    async def test_scope_no_llm_key_degrades_to_allow(self):
        """P2 (R11): 无 LLM key（配置缺失）→ scope allow 降级放行，不再全拦"""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{
            "name": "销售范围", "policy_type": "scope",
            "rule": {"allow": ["采购询价"]},
            "action": "block", "reject_message": "不在服务范围",
            "priority": 10,
        }])
        conn.fetchval = AsyncMock(return_value="biz:seller")
        pool = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.acquire.return_value = ctx

        with patch("zhice.policy_service.get_pool", AsyncMock(return_value=pool)):
            with patch("zhice.policy_service.cfg.get_llm_api_key", return_value=""):
                with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                    result = await __import__("zhice.policy_service", fromlist=[""]).policy_check(
                        "biz:seller-01", {"title": "帮我写爬虫", "description": "爬取数据", "steps": []},
                    )
        assert result["allowed"]  # 无 key → 降级放行
        mock_post.assert_not_called()  # 没有 key 不应发起 LLM 调用

    @pytest.mark.asyncio
    async def test_scope_llm_error_fail_closed(self):
        """P2 (R11): 有 key 但 LLM 调用异常 → fail-closed 拦截（防御瞬时错误）"""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{
            "name": "销售范围", "policy_type": "scope",
            "rule": {"allow": ["采购询价"]},
            "action": "block", "reject_message": "不在服务范围",
            "priority": 10,
        }])
        conn.fetchval = AsyncMock(return_value="biz:seller")
        pool = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.acquire.return_value = ctx

        with patch("zhice.policy_service.get_pool", AsyncMock(return_value=pool)):
            with patch("zhice.policy_service.cfg.get_llm_api_key", return_value="test-key"):
                with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                    mock_post.side_effect = Exception("LLM timeout")
                    result = await __import__("zhice.policy_service", fromlist=[""]).policy_check(
                        "biz:seller-01", {"title": "帮我写爬虫", "description": "爬取数据", "steps": []},
                    )
        assert not result["allowed"]  # LLM 异常 → fail-closed 拦截

    @pytest.mark.asyncio
    async def test_warn_does_not_block(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{
            "name": "观察规则", "policy_type": "keyword",
            "rule": {"keywords": ["旅游"]},
            "action": "warn", "reject_message": "",
            "priority": 5,
        }])
        conn.fetchval = AsyncMock(return_value="biz:seller")
        pool = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.acquire.return_value = ctx

        with patch("zhice.policy_service.get_pool", AsyncMock(return_value=pool)):
            result = await __import__("zhice.policy_service", fromlist=[""]).policy_check(
                "biz:seller-01", {"title": "旅游咨询", "description": "", "steps": []},
            )
        assert result["allowed"]  # warn 不拦截

    @pytest.mark.asyncio
    async def test_block_overrides_warn(self):
        """warn 通过了但是后面有 block → block 生效"""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[
            {"name": "低优warn", "policy_type": "keyword", "rule": {"keywords": ["旅游"]},
             "action": "warn", "priority": 1, "reject_message": ""},
            {"name": "高优block", "policy_type": "keyword", "rule": {"keywords": ["机票"]},
             "action": "block", "priority": 10, "reject_message": "禁止旅游相关"},
        ])
        conn.fetchval = AsyncMock(return_value="biz:seller")
        pool = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.acquire.return_value = ctx

        with patch("zhice.policy_service.get_pool", AsyncMock(return_value=pool)):
            result = await __import__("zhice.policy_service", fromlist=[""]).policy_check(
                "biz:seller-01", {"title": "订机票旅游", "description": "", "steps": []},
            )
        assert not result["allowed"]
        assert "高优block" in result["matched_policy"]
