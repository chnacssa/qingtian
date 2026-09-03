# -*- coding: utf-8 -*-
"""2026-08-28 吸星 review P0-1：网关 /v1/xixing/ 白名单收窄回归测试。

原 `/v1/xixing/` 整段前缀公开 → 14 端点裸奔（knowledge/export 外泄、
sources/collect SSRF 采集链等）。收窄后仅放行 agent 子树 3 条内部无 token
调用（learn / report-pitfall / insights），其余走网关 Bearer。
"""


class TestXixingWhitelistNarrowed:
    def _call(self, path, method="GET"):
        import sys
        import os
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        if root not in sys.path:
            sys.path.insert(0, root)
        from gateway.middleware import _is_path_public
        return _is_path_public(path, method)

    # ── 放行：内部调用方在用的 agent 子树 3 条 ─────────────

    def test_agent_learn_post_allowed(self):
        assert self._call("/v1/xixing/agent/zhice-001/learn", "POST")

    def test_agent_report_pitfall_post_allowed(self):
        assert self._call("/v1/xixing/agent/zhice-001/report-pitfall", "POST")

    def test_agent_insights_get_allowed(self):
        assert self._call("/v1/xixing/agent/zhice-001/insights", "GET")

    def test_agent_id_with_encoded_chars(self):
        # xixing_client quote 后的 agent_id（含 %2F 等编码字符）仍应放行
        assert self._call("/v1/xixing/agent/a%2Fb/learn", "POST")

    # ── 拒绝：敏感读/写端点不再公开 ────────────────────────

    def test_knowledge_export_blocked(self):
        assert not self._call("/v1/xixing/knowledge/export", "POST")

    def test_knowledge_query_blocked(self):
        assert not self._call("/v1/xixing/knowledge/query", "POST")

    def test_sources_create_blocked(self):
        assert not self._call("/v1/xixing/sources", "POST")

    def test_collect_blocked(self):
        assert not self._call("/v1/xixing/collect", "POST")

    def test_process_blocked(self):
        assert not self._call("/v1/xixing/process", "POST")

    def test_proposals_blocked(self):
        assert not self._call("/v1/xixing/proposals", "GET")

    def test_evolve_blocked(self):
        assert not self._call("/v1/xixing/evolve", "POST")

    def test_prefix_root_blocked(self):
        assert not self._call("/v1/xixing")
        assert not self._call("/v1/xixing/")

    # ── 方法感知：agent 子树只放行对应方法 ─────────────────

    def test_agent_learn_get_blocked(self):
        assert not self._call("/v1/xixing/agent/a/learn", "GET")

    def test_agent_insights_post_blocked(self):
        assert not self._call("/v1/xixing/agent/a/insights", "POST")

    # ── 其他前缀不受影响 ──────────────────────────────────

    def test_other_prefixes_unchanged(self):
        assert self._call("/v1/zhice/tasks", "POST")
        assert self._call("/v1/yongheng/memories/search", "POST")
        assert self._call("/health")
