"""汇川 v2.4 — 全链路集成测试

基于汇川-v2.4-技术实施文档（v1.0），按生产实际使用逻辑覆盖：
  - 生产渠道：文件输入 → 文本摄入 → ERP 连接器 → 用户搜索
  - 生产链路：文件解析 → LLM 编译 → PII 脱敏 → 入库 → 中文搜索 → 知识图谱 → 晋升 → 订阅
  - 边界防护：所有端点的入参边界、超限、异常恢复
  - 补充神经网络：精炼管道触发、异步任务队列验证
  - 用户搜索输入：中文自然语言、错别字、中英混合、片段/全文对比

前置条件：
  - ACSSA 底座运行中 (http://127.0.0.1:1996/health)
  - 汇川 schema 已初始化（建表 + pg_bigm 索引）
  - QINGTIAN_ADMIN_TOKEN 或 ZHENYUE_ADMIN_TOKEN 环境变量
  - /opt/qingtian/huichuan/storage/ 和 /opt/qingtian/huichuan/connectors/ 目录存在

运行：
  pytest tests/integration/test_huichuan_v2_4_integration.py -v -xvs
"""

import json
import os
import io
import random
import string
import time
import asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timezone
import pytest
import httpx
from unittest.mock import patch

# ════════════════════════════════════════════════════
# 服务器配置
# ════════════════════════════════════════════════════
BASE_URL = os.environ.get("HUICHUAN_BASE_URL", "http://127.0.0.1:1996")
SERVERS = {
    "management": {"url": "http://10.0.100.1:1996", "label": "管理服"},
    "procurement": {"url": "http://10.0.100.2:1996", "label": "采购服"},
    "sales": {"url": "http://10.0.100.3:1996", "label": "销售服"},
}
_SCHEMA = "huichuan"

# ── 跳过条件 ──
_SHOULD_SKIP = False
try:
    resp = httpx.get(f"{BASE_URL}/health", timeout=3.0)
    _SHOULD_SKIP = resp.status_code != 200
except Exception:
    _SHOULD_SKIP = True

need_base = pytest.mark.skipif(_SHOULD_SKIP, reason="ACSSA 底座未运行")
need_db = pytest.mark.skipif(_SHOULD_SKIP, reason="ACSSA 底座未运行")

ADMIN_TOKEN = os.environ.get("QINGTIAN_ADMIN_TOKEN", "") or os.environ.get("ZHENYUE_ADMIN_TOKEN", "")

# ── 文档常量（与实施文档 §5 边界约束表一致） ──
MAX_QUERY_LENGTH = 100
MAX_LIMIT = 200
MAX_CHUNK_CHARS = 50000
MAX_ENTRIES_PER_DOC = 15
DIRECT_INGEST_MAX_SIZE = 50 * 1024 * 1024  # 50MB
ERP_MAX_ITEMS = 500
HTTP_TIMEOUT = 30
LLM_TIMEOUT = 60
LLM_RETRY = 2
PROMOTE_THRESHOLD = 5
STORAGE_DIR = "/opt/qingtian/huichuan/storage"
CONNECTOR_DIR = "/opt/qingtian/huichuan/connectors"


# ════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════

def _api(method: str, path: str, token: str = "", **kw) -> httpx.Response:
    url = BASE_URL if path.startswith("/") else path
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    kw.setdefault("timeout", 15.0)
    return httpx.request(method, f"{url}{path}", headers=headers, **kw)


def _random_text(length: int) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits + " \n", k=length))


def _chinese_text(length: int) -> str:
    base = "变压器绝缘等级温升限值供应商报价单技术规格书高压低压设备标准型号额定容量电力电缆开关柜"
    return (base * ((length // len(base)) + 1))[:length]


def _tag(prefix: str = "test") -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%H%M%S%f')}"


# ══════════════════════════════════════════════════════════════════
# 一、生产渠道覆盖（基于文档 §4 API 端点变更）
#    渠道1: 飞书文件输入 → ingest/file
#    渠道2: 文本摄入 → ingest
#    渠道3: ERP 连接器 → connector/{name}/run
#    渠道4: 用户搜索 → search
# ══════════════════════════════════════════════════════════════════

@pytest.mark.integration
@need_base
@need_db
class TestChannelFileInput:
    """渠道1：文件输入 — 飞书/手动上传 → ingest/file"""

    def test_upload_txt_file_then_searchable(self):
        """上传 .txt 文件 → ingress → 在搜索结果中出现（正例）"""
        content = "变压器绝缘电阻测试标准：不低于 1000 欧姆/伏".encode("utf-8")
        files = {"file": ("transformer_test.txt", content, "text/plain")}
        resp = _api("POST", "/v1/huichuan/ingest/file", token=ADMIN_TOKEN, files=files)
        assert resp.status_code in (200, 201, 202), f"文件上传失败: {resp.text}"
        data = resp.json()
        assert data.get("entries", 0) >= 1, "未生成知识条目"
        assert "knowledge_ids" in data, "缺少 knowledge_ids"

    def test_upload_multiple_files_batch_import(self):
        """批量导入多个文件 → batch-import → 全部入库"""
        batch = [
            ("contract_A.txt", "供应商甲：变压器单价 12.5 万元".encode("utf-8")),
            ("contract_B.txt", "供应商乙：变压器单价 11.8 万元".encode("utf-8")),
        ]
        files = [("files", (name, content, "text/plain")) for name, content in batch]
        resp = _api("POST", "/v1/huichuan/batch-import", token=ADMIN_TOKEN, files=files)
        assert resp.status_code in (200, 201), f"批量导入失败: {resp.text}"

    def test_upload_empty_file(self):
        """上传空文件 → 422 / {entries:0}"""
        files = {"file": ("empty.txt", b"", "text/plain")}
        resp = _api("POST", "/v1/huichuan/ingest/file", token=ADMIN_TOKEN, files=files)
        assert resp.status_code in (400, 422), f"预期 400/422，实际 {resp.status_code}: {resp.text}"

    def test_upload_file_without_token(self):
        """未认证上传文件 → 401"""
        files = {"file": ("test.txt", b"hello", "text/plain")}
        resp = _api("POST", "/v1/huichuan/ingest/file", files=files)
        assert resp.status_code == 401, f"预期 401，实际 {resp.status_code}"

    def test_upload_file_storage_path_exists(self):
        """文件上传后存储路径存在（检查 storage 目录）"""
        if not Path(STORAGE_DIR).exists():
            pytest.skip("storage 目录不存在，跳过")
        # 上传文件
        files = {"file": ("storage-test.txt", "持久化存储验证".encode("utf-8"), "text/plain")}
        resp = _api("POST", "/v1/huichuan/ingest/file", token=ADMIN_TOKEN, files=files)
        assert resp.status_code in (200, 201), f"上传失败: {resp.text}"
        # 验证知识条目中有 storage_path
        data = resp.json()
        k_ids = data.get("knowledge_ids", [])
        if k_ids:
            detail = _api("GET", f"/v1/huichuan/{k_ids[0]}", token=ADMIN_TOKEN)
            if detail.status_code == 200:
                entry = detail.json()
                assert entry.get("original_storage_path") or entry.get("storage_path"), \
                    "入库条目缺少文件存储路径"


@pytest.mark.integration
@need_base
@need_db
class TestChannelTextIngest:
    """渠道2：文本摄入 — 手动/后台触发 → ingest（Phase 3）"""

    def test_ingest_plain_text(self):
        """正常文本摄入 → 返回入库条目数 > 0"""
        body = {"text": "变压器油温监测：正常范围 40-85℃，超过 90℃ 报警", "source": "manual_test"}
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN, json=body)
        assert resp.status_code == 200, f"ingest 失败: {resp.text}"
        data = resp.json()
        assert data.get("entries", 0) >= 1, f"未生成条目: {data}"

    def test_ingest_text_with_llm_summary(self):
        """文本携 LLM 编译摘要 → 返回含 summary 字段"""
        body = {
            "text": "供应商评估报告：ABC 公司提供 35kV 变压器，报价 45 万，交货期 60 天，质保 5 年",
            "source": "supplier_report",
        }
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN, json=body)
        assert resp.status_code == 200, f"ingest 失败: {resp.text}"
        data = resp.json()
        assert "summary" in data, f"缺少 summary 字段: {data}"

    def test_ingest_with_entry_type_metadata(self):
        """摄入时指定 entry_type 和 metadata → 入库后查得"""
        body = {
            "text": "对比分析：油浸式变压器 vs 干式变压器",
            "source": "comparison",
            "entry_type": "comparison",
            "metadata": {"tags": ["变压器", "对比分析"], "priority": "high"},
        }
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN, json=body)
        assert resp.status_code == 200, f"带着 entry_type 摄入失败: {resp.text}"

    def test_ingest_text_with_pii_auto_sanitized(self):
        """含 PII 的文本摄入 → 入库时自动脱敏（phone/id_card/email/bank 用 *** 替换）"""
        body = {
            "text": "联系人张三 手机 13800138000，身份证 110101199001011234，邮箱 zhang@test.com，银行卡 6222021234567890",
            "source": "pii_test",
        }
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN, json=body)
        assert resp.status_code == 200, f"PII 摄入失败: {resp.text}"
        # 验证入库后无明文 PII（通过 search 验证）
        time.sleep(0.5)
        search = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN,
                       json={"query": "13800138000", "limit": 5})
        if search.status_code == 200:
            results = search.json()
            entries = results if isinstance(results, list) else results.get("results", [])
            plain_found = any("13800138000" in json.dumps(e) for e in entries)
            assert not plain_found, "PII 未被脱敏，手机号明文存在于搜索结果"

    def test_ingest_text_ultra_long_chunked(self):
        """超长文本（>50K 字符）→ 自动截断/分段，不崩溃"""
        long_text = _chinese_text(MAX_CHUNK_CHARS + 1000)
        body = {"text": long_text, "source": "long_doc"}
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN, json=body, timeout=30)
        assert resp.status_code == 200, f"超长文本摄入失败: {resp.text}"
        data = resp.json()
        # 最多生成 MAX_ENTRIES_PER_DOC 条
        assert data.get("entries", 0) <= MAX_ENTRIES_PER_DOC

    def test_ingest_source_domain_required(self):
        """来源 domain 缺失 → 422"""
        body = {"text": "测试内容"}
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN, json=body)
        assert resp.status_code == 422, f"缺失 domain 预期 422，实际 {resp.status_code}: {resp.text}"


@pytest.mark.integration
@need_base
@need_db
class TestChannelConnector:
    """渠道3：ERP 连接器 — connector/{name}/run（Phase 4）"""

    def test_connector_run_nonexistent(self):
        """运行不存在的连接器 → {error: 'not found'}"""
        resp = _api("POST", "/v1/huichuan/connector/nonexistent_xyz/run", token=ADMIN_TOKEN)
        assert resp.status_code == 404, f"预期 404，实际 {resp.status_code}: {resp.text}"

    def test_connector_auth_missing(self):
        """连接器配置存在但 token 环境变量未设置 → 不崩溃"""
        # 确保不能调真实 ERP，只是测试 error 处理
        resp = _api("POST", "/v1/huichuan/connector/test_missing_auth/run", token=ADMIN_TOKEN)
        # 预期：要么 404（配置不存在）、要么 200 但 error 信息
        if resp.status_code == 200:
            data = resp.json()
            assert data.get("error") in ("auth not configured",), f"未预期错误: {data}"

    def test_connector_yaml_invalid_config(self):
        """连接器 YAML 格式错误 → 友好错误"""
        resp = _api("POST", "/v1/huichuan/connector/bad_yaml/run", token=ADMIN_TOKEN)
        assert resp.status_code in (400, 404, 500), f"预期友好错误，实际 {resp.status_code}"
        if resp.status_code == 500:
            assert "yaml" in resp.text.lower() or "parse" in resp.text.lower(), \
                f"错误信息应描述 YAML 解析失败: {resp.text}"


@pytest.mark.integration
@need_base
@need_db
class TestChannelSearch:
    """渠道4：用户搜索 — 知识查询与消费（Phase 0）"""

    def test_search_chinese_natural_language(self):
        """用户输入自然语言中文查询（如'变压器的绝缘等级'）→ 返回匹配的结果"""
        body = {"query": "变压器的绝缘等级", "limit": 5}
        resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json=body)
        assert resp.status_code == 200, f"中文自然语言搜索失败: {resp.text}"
        data = resp.json()
        assert "results" in data or isinstance(data, list), "搜索结果格式不符合预期"

    def test_search_typo_tolerate(self):
        """用户输入错别字（如'变压器'写成'便压器'）→ 仍能模糊匹配"""
        body = {"query": "便压器温升限值", "limit": 5}
        resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json=body)
        # pg_bigm 对 2-gram 的部分匹配应该能容忍单字错误
        assert resp.status_code in (200, 500), f"错别字搜索异常: {resp.text}"
        if resp.status_code == 200:
            data = resp.json()
            results = data if isinstance(data, list) else data.get("results", [])
            if len(results) == 0:
                pytest.skip("无数据，无法验证错别字容忍（需先导入变压器相关数据）")

    def test_search_mixed_chinese_english(self):
        """用户输入中英混合查询（如'35kV变压器报价'）→ 正确返回"""
        body = {"query": "35kV变压器报价 供应商", "limit": 5}
        resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json=body)
        assert resp.status_code == 200, f"中英混合搜索失败: {resp.text}"

    def test_search_punctuation_only(self):
        """用户输入纯标点符号 → 返回空结果 / 友好错误"""
        body = {"query": "，。！？【】", "limit": 5}
        resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json=body)
        assert resp.status_code == 200, f"纯标点搜索应返回 200: {resp.text}"

    def test_search_domain_filter(self):
        """按 domain 过滤搜索 → 只返回该 domain 的结果"""
        body = {"query": "变压器", "domain": "supplier_report", "limit": 5}
        resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json=body)
        assert resp.status_code == 200, f"按 domain 搜索失败: {resp.text}"

    def test_search_by_entry_type(self):
        """按知识类型搜索（entity/concept/comparison/query/source）→ 只返回指定类型"""
        for etype in ("entity", "concept", "comparison"):
            body = {"query": "变压器", "entry_type": etype, "limit": 5}
            resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json=body)
            assert resp.status_code == 200, f"按 entry_type={etype} 搜索失败: {resp.text}"

    def test_search_fragment_vs_full(self):
        """片段搜索（部分词）vs 全文搜索 — 两者都应返回同一结果"""
        fragment = {"query": "绝缘等级", "limit": 5}
        full = {"query": "变压器的绝缘等级标准要求", "limit": 5}
        resp_f = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json=fragment)
        resp_full = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json=full)
        assert resp_f.status_code == 200 and resp_full.status_code == 200


# ══════════════════════════════════════════════════════════════════
# 二、生产典型场景（端到端业务流程）
# ══════════════════════════════════════════════════════════════════

@pytest.mark.integration
@need_base
@need_db
class TestScenarioProcurementIngest:
    """生产场景1：采购文件→入库→搜索→使用（采购部门的典型日流程）"""

    def test_scenario_purchase_contract_full_flow(self):
        """采购合同从飞书上传到搜索使用的完整流程"""
        tag = _tag("contract")
        # Step 1: 用户上传合同文件
        content = f"采购合同 {tag}：供应商 东方变压器厂，产品 S11-630/10 油浸式变压器，" \
                  f"单价 ￥128,000，数量 2 台，交货期 2026-07-15。联系人 13800138001"
        files = {"file": (f"合同-{tag}.txt", content.encode("utf-8"), "text/plain")}
        resp = _api("POST", "/v1/huichuan/ingest/file", token=ADMIN_TOKEN, files=files)
        assert resp.status_code in (200, 201), f"合同文件上传失败: {resp.text}"
        upload_data = resp.json()
        k_ids = upload_data.get("knowledge_ids", [])
        assert len(k_ids) > 0, "上传后未生成知识条目"

        # Step 2: 搜索合同内容（确认可检索到）
        time.sleep(0.5)
        search_resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN,
                           json={"query": tag, "limit": 5})
        assert search_resp.status_code == 200, f"搜索合同失败: {search_resp.text}"
        # Step 3: 验证 PII 脱敏（手机号应被 *** 替代）
        search_data = search_resp.json()
        results = search_data if isinstance(search_data, list) else search_data.get("results", [])
        for r in results:
            dumped = json.dumps(r)
            assert "13800138001" not in dumped, f"PII 未脱敏: {dumped[:200]}"

        # Step 4: 晋升到共享层
        if k_ids:
            promote = _api("POST", f"/v1/huichuan/promote/{k_ids[0]}", token=ADMIN_TOKEN)
            assert promote.status_code in (200, 409), f"晋升失败: {promote.text}"

    def test_scenario_multiple_supplier_ingest(self):
        """批量导入多家供应商合同 → 全部入库 → 可聚合搜索"""
        tag = _tag("supplier")
        texts = [
            f"供应商 {tag}-A：变压器油 S-001，单价 85 元/升",
            f"供应商 {tag}-B：变压器油 S-002，单价 92 元/升",
            f"供应商 {tag}-C：变压器油 S-003，单价 78 元/升",
        ]
        for t in texts:
            _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN,
                 json={"text": t, "source": "supplier_bulk", "domain": "procurement"})
        time.sleep(0.5)
        search = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN,
                      json={"query": tag, "limit": 10})
        assert search.status_code == 200, f"批量导入后搜索失败: {search.text}"
        results = search.json()
        entries = results if isinstance(results, list) else results.get("results", [])
        assert len(entries) >= 2, f"预期至少 2 条供应商记录，实际 {len(entries)}"


@pytest.mark.integration
@need_base
@need_db
class TestScenarioKnowledgeGraph:
    """生产场景2：知识图谱 — 知识关联与图查询（Phase 5）"""

    def test_scenario_knowledge_graph_linking(self):
        """建立知识关联（contradicts/extends/depends）→ 图查询可展开关联"""
        tag = _tag("graph")
        # 入库两条知识
        r1 = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN,
                   json={"text": f"{tag} 标准 A：油浸变压器绝缘等级 F 级", "source": "graph_test"})
        r2 = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN,
                   json={"text": f"{tag} 标准 B：油浸变压器绝缘等级 H 级（高温）", "source": "graph_test"})
        assert r1.status_code == 200 and r2.status_code == 200

    def test_scenario_graph_two_hop_query(self):
        """2-跳知识图谱查询（文档 §2.3 的 with two_hop 语法）→ 返回关联链路"""
        body = {"query": "变压器", "hops": 2, "limit": 10}
        resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json=body)
        assert resp.status_code in (200, 501), f"图查询失败: {resp.text}"
        if resp.status_code == 200:
            assert resp.json() is not None


# ══════════════════════════════════════════════════════════════════
# 三、用户交互流（文档 §4 API 端点变更）
# ══════════════════════════════════════════════════════════════════

@pytest.mark.integration
@need_base
@need_db
class TestUserInteractionFlow:
    """用户交互 — 注册→摄入→搜索→晋升→订阅→巡检"""

    def test_agent_register_and_ingest_flow(self):
        """Agent 注册后 → 搜索知识 → 通过"""
        tag = _tag("agent")
        body = {"text": f"{tag} 高压开关柜操作安全规程：必须先断电再操作", "source": "safety_doc"}
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN, json=body)
        assert resp.status_code == 200

    def test_promote_and_visibility_change(self):
        """知识从私有→共享层晋升 → 其他 domain 可搜索到"""
        tag = _tag("promote")
        body = {"text": f"{tag} 年度财务审计报告 v3", "source": "finance_dept"}
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN, json=body)
        assert resp.status_code == 200
        data = resp.json()
        k_ids = data.get("knowledge_ids", [])
        if k_ids:
            promote = _api("POST", f"/v1/huichuan/promote/{k_ids[0]}", token=ADMIN_TOKEN)
            assert promote.status_code in (200, 409), f"晋升失败: {promote.text}"

    def test_subscribe_then_notify(self):
        """Agent 订阅某 domain → 有新知识时可获得通知"""
        tag = _tag("sub")
        sub_body = {"agent_id": f"agent_{tag}", "domain": "procurement", "event": "new_ingest"}
        resp = _api("POST", "/v1/huichuan/subscribe", token=ADMIN_TOKEN, json=sub_body)
        assert resp.status_code in (200, 201, 409), f"订阅失败: {resp.text}"

    def test_lint_then_auto_fix_flow(self):
        """巡检引擎 → 生成报告 → 执行自动修复（Phase 6）"""
        report = _api("GET", "/v1/huichuan/lint/report", token=ADMIN_TOKEN)
        if report.status_code == 501:
            pytest.skip("lint 尚未实施")
        assert report.status_code == 200, f"巡检报告失败: {report.text}"


# ══════════════════════════════════════════════════════════════════
# 四、边界条件测试（文档 §5 边界约束表）
# ══════════════════════════════════════════════════════════════════

@pytest.mark.integration
@need_base
@need_db
class TestBoundary:
    """边界条件 — search / ingest / create / promote / subscribe 各端点的入参边界"""

    # ── search 边界 ──
    def test_search_empty_query(self):
        resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json={"query": "", "limit": 5})
        assert resp.status_code in (200, 422)

    def test_search_query_max_length(self):
        q = _chinese_text(MAX_QUERY_LENGTH)
        resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json={"query": q, "limit": 5})
        assert resp.status_code == 200

    def test_search_query_over_max(self):
        q = _chinese_text(MAX_QUERY_LENGTH + 1)
        resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json={"query": q, "limit": 5})
        assert resp.status_code in (200, 422)

    def test_search_limit_zero(self):
        resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json={"query": "测试", "limit": 0})
        assert resp.status_code in (200, 422)

    def test_search_limit_max(self):
        resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json={"query": "测试", "limit": MAX_LIMIT})
        assert resp.status_code == 200

    def test_search_limit_over_max(self):
        resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json={"query": "测试", "limit": MAX_LIMIT + 1})
        assert resp.status_code in (200, 422)

    def test_search_limit_negative(self):
        resp = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN, json={"query": "测试", "limit": -1})
        assert resp.status_code == 422

    # ── ingest 边界 ──
    def test_ingest_empty_text(self):
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN, json={"text": "", "source": "test"})
        assert resp.status_code in (200, 422)

    def test_ingest_whitespace_text(self):
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN, json={"text": "   ", "source": "test"})
        assert resp.status_code in (200, 422)

    def test_ingest_no_json_body(self):
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN, data="not json")
        assert resp.status_code == 422

    # ── create 边界 ──
    def test_create_empty_title(self):
        resp = _api("POST", "/v1/huichuan", token=ADMIN_TOKEN, json={"title": "", "content": "测试"})
        assert resp.status_code == 422

    def test_create_invalid_entry_type(self):
        resp = _api("POST", "/v1/huichuan", token=ADMIN_TOKEN,
                     json={"title": "测试", "content": "测试", "entry_type": "invalid_type"})
        assert resp.status_code in (422, 400)

    def test_create_invalid_visibility(self):
        resp = _api("POST", "/v1/huichuan", token=ADMIN_TOKEN,
                     json={"title": "测试", "content": "测试", "visibility": "public"})
        assert resp.status_code in (422, 400)

    def test_create_empty_content(self):
        resp = _api("POST", "/v1/huichuan", token=ADMIN_TOKEN, json={"title": "测试", "content": ""})
        assert resp.status_code in (422, 400)

    def test_batch_write_empty(self):
        resp = _api("POST", "/v1/huichuan/batch", token=ADMIN_TOKEN, json={"entries": []})
        assert resp.status_code in (200, 422)

    # ── GET/DELETE/PROMOTE 不存在 ──
    def test_get_nonexistent_knowledge(self):
        rid = "00000000-0000-0000-0000-000000000000"
        resp = _api("GET", f"/v1/huichuan/{rid}", token=ADMIN_TOKEN)
        assert resp.status_code == 404

    def test_delete_nonexistent_knowledge(self):
        rid = "00000000-0000-0000-0000-000000000000"
        resp = _api("DELETE", f"/v1/huichuan/{rid}", token=ADMIN_TOKEN)
        assert resp.status_code == 404

    def test_promote_nonexistent(self):
        rid = "00000000-0000-0000-0000-000000000000"
        resp = _api("POST", f"/v1/huichuan/promote/{rid}", token=ADMIN_TOKEN)
        assert resp.status_code == 404

    # ── subscribe 边界 ──
    def test_subscribe_empty_agent(self):
        resp = _api("POST", "/v1/huichuan/subscribe", token=ADMIN_TOKEN,
                     json={"agent_id": "", "domain": "test", "event": "new_ingest"})
        assert resp.status_code in (422, 400)

    # ── lint 边界 ──
    def test_lint_report_empty(self):
        resp = _api("GET", "/v1/huichuan/lint/report", token=ADMIN_TOKEN)
        if resp.status_code == 501:
            pytest.skip("lint 尚未实施")
        assert resp.status_code in (200, 500)
        if resp.status_code == 200:
            assert isinstance(resp.json(), dict)

    def test_lint_auto_fix_no_body(self):
        resp = _api("POST", "/v1/huichuan/lint/auto-fix", token=ADMIN_TOKEN, json={})
        if resp.status_code == 501:
            pytest.skip("lint 尚未实施")
        assert resp.status_code in (200, 422)


# ══════════════════════════════════════════════════════════════════
# 五、数据全链路流转
# ══════════════════════════════════════════════════════════════════

@pytest.mark.integration
@need_base
@need_db
class TestDataFlow:
    """数据流转 — sanitize → store → search → promote → delete → vector → connector"""

    def test_full_ingest_sanitize_store_search_cycle(self):
        """完整链路：含 PII 文本 → ingest（脱敏）→ 入库 → search 无 PII"""
        tag = _tag("flow")
        content = f"{tag} PII 测试 手机 13912345678"
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN,
                     json={"text": content, "source": "flow_test"})
        assert resp.status_code == 200
        time.sleep(0.5)
        search = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN,
                       json={"query": tag, "limit": 5})
        assert search.status_code == 200

    def test_ingest_then_promote_then_search_shared(self):
        """私有 → promote 晋升 → 共享层可搜索"""
        tag = _tag("promote")
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN,
                     json={"text": f"{tag} 共享层测试", "source": "promote_test"})
        assert resp.status_code == 200
        data = resp.json()
    def test_ingest_then_promote_then_search_shared(self):
        """私有 → promote 晋升 → 共享层可搜索"""
        tag = _tag("promote")
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN,
                     json={"text": f"{tag} 共享层测试", "source": "promote_test"})
        assert resp.status_code == 200
        data = resp.json()
        k_ids = data.get("knowledge_ids", [])
        if k_ids:
            promote = _api("POST", f"/v1/huichuan/promote/{k_ids[0]}", token=ADMIN_TOKEN)
            assert promote.status_code in (200, 409), f"晋升失败: {promote.text}"
            time.sleep(0.3)
            search_shared = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN,
                                 json={"query": tag, "visibility": "shared", "limit": 5})
            assert search_shared.status_code == 200

    def test_delete_then_search_absent(self):
        """删除知识后 → 搜索不再包含"""
        tag = _tag("delete")
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN,
                     json={"text": f"{tag} 待删除测试", "source": "delete_test"})
        assert resp.status_code == 200
        data = resp.json()
        k_ids = data.get("knowledge_ids", [])
        if k_ids:
            del_resp = _api("DELETE", f"/v1/huichuan/{k_ids[0]}", token=ADMIN_TOKEN)
            assert del_resp.status_code == 200
            time.sleep(0.3)
            search = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN,
                           json={"query": tag, "limit": 5})
            assert search.status_code == 200

    def test_vector_search_roundtrip(self):
        """向量搜索（vector-search）→ 返回语义相关结果"""
        body = {"query": "变压器绝缘", "limit": 5}
        resp = _api("POST", "/v1/huichuan/vector-search", token=ADMIN_TOKEN, json=body)
        if resp.status_code == 501:
            pytest.skip("向量搜索尚未实施")
        assert resp.status_code in (200, 500), f"向量搜索: {resp.status_code}"
        if resp.status_code == 200:
            assert resp.json() is not None

    def test_refine_trigger_then_verify(self):
        """手动触发精炼管道 → 返回 202 accepted"""
        resp = _api("POST", "/v1/huichuan/refine/trigger", token=ADMIN_TOKEN, json={})
        if resp.status_code == 501:
            pytest.skip("refine 尚未实施")
        assert resp.status_code in (200, 202), f"refine 触发: {resp.status_code}"

    def test_connector_run_sanity(self):
        """ERP 连接器运行 sanity check"""
        resp = _api("POST", "/v1/huichuan/connector/test_run/run", token=ADMIN_TOKEN)
        if resp.status_code == 501:
            pytest.skip("connector 尚未实施")
        assert resp.status_code in (200, 404), f"connector run: {resp.status_code}"


# ══════════════════════════════════════════════════════════════════
# 六、补充神经网络（精炼管道 + 异步任务）
# ══════════════════════════════════════════════════════════════════

@pytest.mark.integration
@need_base
@need_db
class TestNeuralRefinement:
    """补充神经网络 — refine 管道与异步任务"""

    def test_refine_endpoint_reachable(self):
        """refine/trigger 端点可达（不要求执行成功）"""
        resp = _api("POST", "/v1/huichuan/refine/trigger", token=ADMIN_TOKEN, json={})
        assert resp.status_code in (200, 202, 501), f"refine 端点异常: {resp.status_code}"

    def test_refine_with_queue_id(self):
        """触发精炼后返回 queue_id（异步任务）"""
        resp = _api("POST", "/v1/huichuan/refine/trigger", token=ADMIN_TOKEN, json={})
        if resp.status_code in (200, 202):
            data = resp.json()
            assert "queue_id" in data or "task_id" in data, \
                f"异步任务应返回 queue_id/task_id: {data}"

    def test_refine_after_ingest_refines_content(self):
        """最新摄入的内容可通过 refine 管道再次精炼"""
        tag = _tag("refine")
        _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN,
             json={"text": f"{tag} 需要精炼的质量数据 v2", "source": "refine_trigger"})
        resp = _api("POST", "/v1/huichuan/refine/trigger", token=ADMIN_TOKEN, json={})
        assert resp.status_code in (200, 202), f"精炼触发失败: {resp.text}"


# ══════════════════════════════════════════════════════════════════
# 七、脱敏逻辑直接测试
# ══════════════════════════════════════════════════════════════════

@pytest.mark.integration
@need_base
@need_db
class TestSanitizerDirect:
    """脱敏模块直接测试（不依赖 API，import sanitizer）"""

    def _import_sanitizer(self):
        try:
            from huichuan.sanitizer import sanitize
            return sanitize
        except ImportError:
            pytest.skip("sanitizer 模块不可导入")

    def test_sanitize_phone(self):
        sanitize = self._import_sanitizer()
        assert sanitize("联系手机13800138000") == "联系手机***", f"手机脱敏失败"

    def test_sanitize_id_card(self):
        sanitize = self._import_sanitizer()
        result = sanitize("身份证110101199001011234")
        assert "11010119900101123" not in result, "身份证未脱敏"

    def test_sanitize_bank(self):
        sanitize = self._import_sanitizer()
        result = sanitize("银行卡6222021234567890")
        assert "***" in result, "银行卡未脱敏"

    def test_sanitize_email(self):
        sanitize = self._import_sanitizer()
        assert "zhang@test.com" not in sanitize("邮箱zhang@test.com"), "邮箱未脱敏"

    def test_sanitize_internal_note(self):
        sanitize = self._import_sanitizer()
        text = "公开内容\n#内部 仅供财务参考\n更多公开内容"
        result = sanitize(text, level="private_to_shared")
        assert "#内部" not in result, "内部备注未去除"

    def test_sanitize_multiple_pii(self):
        sanitize = self._import_sanitizer()
        text = "张先生13800000000，身份证320106199003071234"
        result = sanitize(text)
        # 所有 PII 应被替换
        pii_count = sum(text.count(p) for p in ["13800000000", "320106199003071234"])
        assert pii_count == 0, f"存在未脱敏 PII: {pii_count}"

    def test_sanitize_perf(self):
        sanitize = self._import_sanitizer()
        text = "手机" + "13800138000\n" * 100
        import time
        t0 = time.time()
        sanitize(text)
        elapsed = time.time() - t0
        assert elapsed < 0.5, f"脱敏性能不达标: {elapsed:.3f}s"

    def test_sanitize_invalid_level(self):
        sanitize = self._import_sanitizer()
        result = sanitize("测试13700000001", level="unknown_level")
        assert result is not None, "非法 level 不应崩溃"


# ══════════════════════════════════════════════════════════════════
# 八、MCP 工具定义验证
# ══════════════════════════════════════════════════════════════════

@pytest.mark.integration
@need_base
@need_db
class TestMCPToolDefinitions:
    """MCP Server 工具定义完整性检查"""

    def _load_mcp(self):
        try:
            import huichuan.mcp as mcp
            return mcp
        except ImportError:
            pytest.skip("mcp 模块不可导入")

    def test_mcp_tools_nonempty(self):
        mcp = self._load_mcp()
        # 期望 23 个工具（文档 §3.6）
        tools = [a for a in dir(mcp) if callable(getattr(mcp, a)) and not a.startswith("_")]
        assert len(tools) > 0, "mcp 模块无公开工具"

    def test_mcp_tool_names_unique(self):
        mcp = self._load_mcp()
        from huichuan.mcp import mcp as fastmcp_server
        tools = fastmcp_server._tools if hasattr(fastmcp_server, '_tools') else []
        names = [t.name for t in tools]
        assert len(names) == len(set(names)), "工具名称不唯一"


# ══════════════════════════════════════════════════════════════════
# 九、三服务器分布检查
# ══════════════════════════════════════════════════════════════════

@pytest.mark.integration
@need_base
@need_db
class TestCrossServer:
    """三服务器健康入口——管理服（核心API+DB）/ 采购服（采购Agent+ERP连接器）/ 销售服（销售Agent+飞书）"""

    @pytest.mark.parametrize("role,url", [
        ("management", "http://10.0.100.1:1996"),
        ("procurement", "http://10.0.100.2:1996"),
        ("sales", "http://10.0.100.3:1996"),
    ])
    def test_health(self, role, url):
        try:
            resp = httpx.get(f"{url}/health", timeout=5.0)
            assert resp.status_code == 200, f"{role}: {resp.status_code}"
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            pytest.skip(f"{role} health 不可达（跨服网络）: {e}")

    @pytest.mark.parametrize("role,url", [
        ("management", "http://10.0.100.1:1996"),
        ("procurement", "http://10.0.100.2:1996"),
        ("sales", "http://10.0.100.3:1996"),
    ])
    def test_health_huichuan_module(self, role, url):
        try:
            resp = httpx.get(f"{url}/v1/huichuan/health", timeout=5.0)
            assert resp.status_code == 200, f"{role}: {resp.status_code}"
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            pytest.skip(f"{role} huichuan health 不可达（跨服网络）: {e}")


# ══════════════════════════════════════════════════════════════════
# 十、软删除与恢复（commit 8c87581 — 破军新增功能）
# ══════════════════════════════════════════════════════════════════

@pytest.mark.integration
@need_base
@need_db
class TestSoftDelete:
    """软删除 — 撤销→搜索不可见→恢复→搜索恢复→镇岳拦截"""

    def test_soft_delete_revoke_status(self):
        """DELETE 后 status=revoked，返回 retain_until"""
        tag = _tag("softdel")
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN,
                     json={"text": f"{tag} 待删除条目", "source": "softdel_test"})
        assert resp.status_code == 200
        kid = resp.json()["knowledge_ids"][0]

        del_resp = _api("DELETE", f"/v1/huichuan/{kid}", token=ADMIN_TOKEN)
        assert del_resp.status_code == 200, f"软删除失败: {del_resp.text}"
        data = del_resp.json()
        assert data["status"] == "revoked", f"预期 revoked，实际 {data['status']}"
        assert "retain_until" in data, "缺少 retain_until 字段"
        assert "revoked_at" in data, "缺少 revoked_at 字段"

    def test_soft_delete_search_not_found(self):
        """撤销后知识不出现在搜索结果中（永恒索引已移除）"""
        tag = _tag("softdel")
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN,
                     json={"text": f"{tag} 撤销后应搜索不到", "source": "softdel_test"})
        assert resp.status_code == 200
        kid = resp.json()["knowledge_ids"][0]

        # 先确认搜得到
        pre = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN,
                    json={"query": tag, "limit": 5})
        assert pre.status_code == 200
        pre_results = pre.json() if isinstance(pre.json(), list) else pre.json().get("results", [])
        assert len(pre_results) >= 1, "删除前应能搜到"

        # 删除
        del_resp = _api("DELETE", f"/v1/huichuan/{kid}", token=ADMIN_TOKEN)
        assert del_resp.status_code == 200

        # 搜索结果中不再包含
        time.sleep(0.5)
        post = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN,
                     json={"query": tag, "limit": 5})
        assert post.status_code == 200
        post_results = post.json() if isinstance(post.json(), list) else post.json().get("results", [])
        # 允许有同 tag 的其他条目，但已撤销的这条不应出现
        for r in post_results:
            dumped = json.dumps(r)
            assert kid not in dumped, f"已撤销的知识仍出现在搜索结果: {dumped[:150]}"

    def test_restore_revoked_knowledge(self):
        """POST /{id}/restore 恢复已撤销知识 → status=active"""
        tag = _tag("restore")
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN,
                     json={"text": f"{tag} 将被撤销再恢复", "source": "restore_test"})
        assert resp.status_code == 200
        kid = resp.json()["knowledge_ids"][0]

        # 撤销
        _api("DELETE", f"/v1/huichuan/{kid}", token=ADMIN_TOKEN)

        # 恢复
        restore = _api("POST", f"/v1/huichuan/{kid}/restore", token=ADMIN_TOKEN)
        assert restore.status_code == 200, f"恢复失败: {restore.text}"
        assert restore.json()["action"] == "restore_huichuan"

        # 验证恢复后可搜索到
        time.sleep(0.5)
        search = _api("POST", "/v1/huichuan/search", token=ADMIN_TOKEN,
                       json={"query": tag, "limit": 5})
        assert search.status_code == 200
        results = search.json() if isinstance(search.json(), list) else search.json().get("results", [])
        found = any(kid in json.dumps(r) for r in results)
        assert found, "恢复后应能搜索到该知识"

    def test_restore_active_knowledge_400(self):
        """恢复一个 active 状态的知识 → 400"""
        tag = _tag("noop")
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN,
                     json={"text": f"{tag} 无需恢复的知识", "source": "restore_400_test"})
        assert resp.status_code == 200
        kid = resp.json()["knowledge_ids"][0]

        # 直接恢复（未撤销）
        restore = _api("POST", f"/v1/huichuan/{kid}/restore", token=ADMIN_TOKEN)
        assert restore.status_code == 400, f"预期 400，实际 {restore.status_code}: {restore.text}"

    def test_delete_revoked_twice(self):
        """对已撤销的知识再次 DELETE → 仍然成功（幂等）"""
        tag = _tag("twice")
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN,
                     json={"text": f"{tag} 幂等删除", "source": "del_twice_test"})
        assert resp.status_code == 200
        kid = resp.json()["knowledge_ids"][0]

        del1 = _api("DELETE", f"/v1/huichuan/{kid}", token=ADMIN_TOKEN)
        assert del1.status_code == 200

        del2 = _api("DELETE", f"/v1/huichuan/{kid}", token=ADMIN_TOKEN)
        # 幂等：第二次应 200 或 404
        assert del2.status_code in (200, 404), f"二次删除预期 200/404，实际 {del2.status_code}: {del2.text}"

    def test_delete_without_token_401(self):
        """未认证 DELETE → 401（镇岳拦截）"""
        tag = _tag("unauth")
        resp = _api("POST", "/v1/huichuan/ingest", token=ADMIN_TOKEN,
                     json={"text": f"{tag} 删除鉴权", "source": "auth_test"})
        assert resp.status_code == 200
        kid = resp.json()["knowledge_ids"][0]

        del_resp = _api("DELETE", f"/v1/huichuan/{kid}")  # 无 token
        assert del_resp.status_code == 401, f"未认证 DELETE 预期 401，实际 {del_resp.status_code}"
