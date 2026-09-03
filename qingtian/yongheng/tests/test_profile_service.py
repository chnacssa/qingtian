"""
profile_service.py 单元测试
画像读写 / learned 整理 / Levenshtein 去重
"""

import json

import pytest

from yongheng.profile_service import (
    get_profile,
    update_profile,
    _levenshtein,
    _deduplicate_learned,
)


class TestLevenshtein:
    def test_same_strings_zero(self):
        assert _levenshtein("hello", "hello") == 0

    def test_one_edit(self):
        assert _levenshtein("hello", "helo") == 1

    def test_empty_string(self):
        assert _levenshtein("", "abc") == 3

    def test_completely_different(self):
        assert _levenshtein("abc", "xyz") == 3

    def test_chinese_characters(self):
        assert _levenshtein("你好世界", "你好") == 2


class TestDeduplicateLearned:
    def test_no_duplicates(self):
        items = [
            {"preference": "喜欢简洁的回复", "confirmations": 2},
            {"preference": "需要使用英文术语", "confirmations": 1},
        ]
        result = _deduplicate_learned(items)
        assert len(result) == 2

    def test_similar_items_merged(self):
        items = [
            {"preference": "喜欢简洁的回复格式", "confirmations": 1},
            {"preference": "喜欢简洁的回复", "confirmations": 3},
        ]
        result = _deduplicate_learned(items)
        assert len(result) == 1
        assert result[0]["confirmations"] == 3

    def test_different_items_kept(self):
        items = [
            {"preference": "喜欢简洁的回复", "confirmations": 2},
            {"preference": "需要使用专业数据分析工具", "confirmations": 1},
        ]
        result = _deduplicate_learned(items)
        assert len(result) == 2

    def test_empty_list(self):
        assert _deduplicate_learned([]) == []


class TestGetProfile:
    @pytest.mark.asyncio
    async def test_no_existing_profile(self, mock_conn):
        mock_conn.fetchrow.return_value = None
        mock_conn.fetch.return_value = []

        result = await get_profile(mock_conn, "agent:test")
        assert result["namespace"] == "agent:test"
        assert result["agent_id"] == "test"
        assert result["traits"] == {}
        assert result["learned"] == []

    @pytest.mark.asyncio
    async def test_existing_profile(self, mock_conn):
        mock_conn.fetchrow.return_value = {
            "namespace": "agent:test",
            "traits": {"role": "developer"},
            "learned": [{"preference": "use python"}],
            "state": {"online": True},
            "updated_at": None,
        }
        mock_conn.fetch.return_value = []

        result = await get_profile(mock_conn, "agent:test")
        assert result["traits"] == {"role": "developer"}
        assert len(result["learned"]) == 1

    @pytest.mark.asyncio
    async def test_json_fields_parsed(self, mock_conn):
        mock_conn.fetchrow.return_value = {
            "namespace": "agent:test",
            "traits": json.dumps({"role": "dev"}),
            "learned": json.dumps([{"preference": "x"}]),
            "state": json.dumps({"mode": "auto"}),
            "updated_at": None,
        }
        mock_conn.fetch.return_value = []

        result = await get_profile(mock_conn, "agent:test")
        assert result["traits"] == {"role": "dev"}
        assert result["state"] == {"mode": "auto"}


class TestUpdateProfile:
    @pytest.mark.asyncio
    async def test_update_traits(self, mock_conn):
        mock_conn.fetchrow.return_value = {
            "namespace": "agent:test",
            "traits": {"role": "dev"},
            "learned": [],
            "state": {},
            "updated_at": None,
        }
        mock_conn.fetch.return_value = []

        result = await update_profile(mock_conn, "agent:test", traits={"role": "admin"})
        assert result["traits"] == {"role": "dev"}  # from fetchrow mock

    @pytest.mark.asyncio
    async def test_add_learned_fills_defaults(self, mock_conn):
        existing = {
            "namespace": "agent:test",
            "traits": {},
            "learned": [],
            "state": {},
            "updated_at": None,
        }
        mock_conn.fetchrow.return_value = existing
        mock_conn.fetch.return_value = []

        await update_profile(mock_conn, "agent:test",
                            learned_add=[{"preference": "use vim"}])
        update_calls = [c for c in mock_conn.execute.call_args_list
                      if "learned" in str(c.args[0])]
        assert len(update_calls) >= 1

    @pytest.mark.asyncio
    async def test_new_profile_created(self, mock_conn):
        mock_conn.fetchrow.return_value = None
        mock_conn.fetch.return_value = []

        await update_profile(mock_conn, "agent:new", traits={"role": "tester"})
        insert_calls = [c for c in mock_conn.execute.call_args_list
                      if "INSERT INTO" in str(c.args[0])]
        assert len(insert_calls) >= 1
