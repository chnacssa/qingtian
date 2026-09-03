# -*- coding: utf-8 -*-
"""execute_api bidding action 归一化（含空 action 兜底）测试 — 2026-08-27 小智 13:56 实锤。

线上事故：门户直调 /skills/bidding/execute，body.params 键 `action` 在但值空、
payload_user 空、user_id 靠 body——空 action 漏过原 `if act and act not in ...`
前置（只兜非白名单不兜空）→ bidding.execute 秒回 `未知操作: `，标书没生成、
无 call_llm。修复后空 action 与非白名单同走 _normalize_bidding_action 按原文
关键词归一化（生成 vs 评分）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from osskill.execute_api import _normalize_bidding_action


class TestNormalizeEmptyAction:
    """空 action → 用原文线索归一化（本次修复核心）。"""

    def test_empty_action_raw_text_top_level(self):
        # 13:56 实锤形态：action 键在值空 + payload 存在；_raw_text 在顶层
        params = {"action": "", "payload": {"text": "帮我写投标文件"},
                  "_raw_text": "帮我生成这份标书"}
        assert _normalize_bidding_action(params) == "generate_bid"

    def test_empty_action_raw_text_in_payload(self):
        # 原文线索只在嵌套 payload 内（params 顶层无 _raw_text/query）
        params = {"action": "", "payload": {"_raw_text": "对这份标书打分"}}
        assert _normalize_bidding_action(params) == "evaluate_bid"

    def test_empty_action_text_in_payload(self):
        params = {"action": "", "payload": {"text": "写一份安徽项目的标书"}}
        assert _normalize_bidding_action(params) == "generate_bid"

    def test_empty_action_query_top_level(self):
        params = {"action": "", "query": "修改标书第三章"}
        assert _normalize_bidding_action(params) == "revise_bid"

    def test_empty_action_fallback_body_action(self):
        # params 内无任何原文 → 兜底 body.action
        params = {"action": "", "payload": {}}
        assert _normalize_bidding_action(params, body_action="评分这份标书") == "evaluate_bid"

    def test_empty_action_no_clue_defaults_evaluate(self):
        # 全无线索 → _bidding_action_hint("") 默认评分（兼容旧行为）
        assert _normalize_bidding_action({"action": ""}) == "evaluate_bid"


class TestNormalizeNonWhitelist:
    """非白名单 action（LLM 幻觉）→ 归一化（2026-08-06 既有行为保持）。"""

    def test_hallucinated_action_normalized(self):
        params = {"action": "write_bid_document", "_raw_text": "帮我写标书"}
        assert _normalize_bidding_action(params) == "generate_bid"

    def test_non_whitelist_no_clue_uses_body_action(self):
        params = {"action": "magic_action"}
        assert _normalize_bidding_action(params, body_action="生成投标文件") == "generate_bid"


class TestWhitelistPassthrough:
    """白名单 action 原样返回（health/search_files 等非生成类不受影响）。"""

    def test_health_passthrough(self):
        assert _normalize_bidding_action({"action": "health"}) == "health"

    def test_generate_bid_passthrough(self):
        assert _normalize_bidding_action(
            {"action": "generate_bid", "_raw_text": "评分"}) == "generate_bid"

    def test_search_files_passthrough(self):
        assert _normalize_bidding_action({"action": "search_files"}) == "search_files"

    def test_none_params(self):
        # params 为 None/空 dict → 全兜底链走默认评分
        assert _normalize_bidding_action(None) == "evaluate_bid"
        assert _normalize_bidding_action({}) == "evaluate_bid"
