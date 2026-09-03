"""回归测试 — 通道绑定 resolve 路由不被 /agents/{agent_id} 动态段抢占。

背景（2026-08-08 大师实测 404）：`GET /agents/resolve` 单段路径会被
main.py 先 include 的 api_compliance `/agents/{agent_id}` 动态段匹配吞掉，
resolve 永远不可达 → 动态绑定未生效。修复方案：resolve 改用三段路径
`/agents/identity/resolve`（一段 {agent_id} 模式匹配不到三段路径）。

本测试不启动 app（无 DB/依赖），以源码级断言守住契约，防回归。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_HUANYU_DIR = Path(__file__).resolve().parents[1]
_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "zhice" / "agent-gateway-plugin"


def _api_source() -> str:
    return (_HUANYU_DIR / "api.py").read_text(encoding="utf-8")


def _compliance_source() -> str:
    return (_HUANYU_DIR / "api_compliance.py").read_text(encoding="utf-8")


def _plugin_source() -> str:
    return (_PLUGIN_DIR / "index.js").read_text(encoding="utf-8")


def test_resolve_route_is_three_segment():
    """resolve 端点必须用三段路径，避开单段 /agents/{agent_id} 动态段。"""
    src = _api_source()
    assert '@router.get("/agents/identity/resolve")' in src, \
        "resolve 端点必须为 /agents/identity/resolve（三段），单段 /agents/resolve 会被 compliance 动态段吞掉"


def test_no_bare_resolve_route():
    """禁止再出现单段 /agents/resolve 路由。"""
    src = _api_source()
    assert '@router.get("/agents/resolve")' not in src, \
        "单段 /agents/resolve 会与 api_compliance /agents/{agent_id} 冲突，禁止"


def test_plugin_calls_three_segment_resolve():
    """插件 resolve 必须调三段路径。"""
    src = _plugin_source()
    assert "/v1/huanyu/agents/identity/resolve" in src, \
        "插件必须调 /v1/huanyu/agents/identity/resolve"
    assert "/v1/huanyu/agents/resolve?" not in src, \
        "插件不得残留单段 resolve URL"


def test_compliance_still_has_dynamic_segment():
    """api_compliance 的 /agents/{agent_id} 动态段仍存在（说明冲突源未消失，依赖三段路径规避）。"""
    assert '@compliance_router.get("/agents/{agent_id}")' in _compliance_source()
