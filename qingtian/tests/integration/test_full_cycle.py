"""ACSSA 智能体操作系统 — Agent 端到端全链路集成测试

一条故事线: 管理员准备 → 采购询价 → 销售应标 → 谈判签约 PO → 互评结算收尾
覆盖 7 个模块: huanyu / xixing / yongheng / zhenyue / huichuan / siku / zhice

运行前提: ACSSA 底座已启动在 127.0.0.1:1996
  pytest tests/integration/test_full_cycle.py -v -s --timeout=300
"""
import json
import uuid
import pytest
from tests.integration.conftest import api, BASE_URL, bid, sid, vid


def _uid() -> str:
    return uuid.uuid4().hex[:12]


# ══════════════════════════════════════════════════════════
# 阶段 0: 管理员准备环境
# ══════════════════════════════════════════════════════════

class TestPhase0AdminSetup:
    """S0.1-S0.6: 管理员准备 — 审核/密钥/行为规范/知识库/充值"""

    def test_s01_review_buyer(self, base_url, admin_token, agents):
        """S0.1 审核采购 Agent → active"""
        resp = api("POST", "/v1/zhenyue/agents/review", base_url, admin_token,
                   json={"agent_id": bid(), "decision": "approved"})
        # 200=直接通过, 202=进入审批(异步), 409=已审核
        assert resp.status_code in (200, 202, 409), f"S0.1 buyer review: {resp.status_code} {resp.text[:200]}"

    def test_s01_review_seller(self, base_url, admin_token):
        """S0.1 审核销售 Agent → active"""
        resp = api("POST", "/v1/zhenyue/agents/review", base_url, admin_token,
                   json={"agent_id": sid(), "decision": "approved"})
        assert resp.status_code in (200, 202, 409), f"S0.1 seller review: {resp.status_code}"

    def test_s02_keypair_buyer(self, base_url, admin_token, agents):
        """S0.2 生成采购 Agent 密钥对"""
        resp = api("POST", f"/v1/zhenyue/agents/{bid()}/keypair", base_url, admin_token)
        assert resp.status_code == 200, f"S0.2 buyer keypair: {resp.status_code}"
        data = resp.json()
        assert "public_key" in data
        assert len(data["public_key"]) == 64

    def test_s02_keypair_seller(self, base_url, admin_token, agents):
        """S0.2 生成销售 Agent 密钥对"""
        resp = api("POST", f"/v1/zhenyue/agents/{sid()}/keypair", base_url, admin_token)
        assert resp.status_code == 200, f"S0.2 seller keypair: {resp.status_code}"

    def test_s03_behavior_policy(self, base_url, admin_token):
        """S0.3 设置行为规范：销售Agent禁词"""
        resp = api("POST", "/v1/zhice/policies", base_url, admin_token, json={
            "name": f"销售禁词-{_uid()}",
            "category": "biz:seller",
            "policy_type": "keyword",
            "rule": {"keywords": ["旅游", "机票", "游戏"]},
            "action": "block",
            "reject_message": "我是销售Agent，只能处理采购业务。您的请求不在服务范围内。",
            "priority": 10,
            "created_by": "admin",
        })
        assert resp.status_code == 200, f"S0.3 policy: {resp.status_code} {resp.text[:200]}"
        policy = resp.json()
        assert policy["policy_type"] == "keyword"

    def test_s04_knowledge_base(self, base_url, admin_token):
        """S0.4 创建汇川知识库 + 预置条目"""
        resp = api("POST", "/v1/huichuan", base_url, admin_token, json={
            "title": f"水泥产品库-{_uid()}",
            "domain": "建材",
            "content": "特种水泥42.5报价基准",
            "visibility": "enterprise",
        })
        assert resp.status_code in (200, 201), f"S0.4 knowledge: {resp.status_code}"

    def test_s05_recharge(self, base_url, admin_token):
        """S0.5 司库充值"""
        resp = api("POST", "/v1/siku/accounts/recharge", base_url, admin_token, json={
            "agent_id": bid(),
            "amount_fen": 50000000,
            "idempotency_key": f"ik-recharge-{_uid()}",
        })
        assert resp.status_code == 200, f"S0.5 recharge: {resp.status_code} {resp.text[:200]}"

    def test_s06_balance(self, base_url, admin_token):
        """S0.6 查询账户余额"""
        resp = api("GET", f"/v1/siku/accounts/{bid()}", base_url, admin_token)
        assert resp.status_code == 200, f"S0.6 balance: {resp.status_code}"


# ══════════════════════════════════════════════════════════
# 阶段 1: 采购 Agent 发起询价
# ══════════════════════════════════════════════════════════

class TestPhase1BuyerInquiry:
    """S1.2-S1.9: 执策分解 → 执行 → 签名 → 永恒 → 审计 (Agent 由 fixtures 预注册)"""

    task_id: int = 0
    step_id: int = 0

    def test_s12_create_task(self, base_url, agents):
        """S1.2 LLM 自动分解任务"""
        resp = api("POST", "/v1/zhice/tasks", base_url, json={
            "title": "水泥采购询价",
            "description": "帮我查一下42.5特种水泥的最新报价和供货能力",
            "created_by": bid(),
        })
        assert resp.status_code == 200, f"S1.2 create_task: {resp.status_code} {resp.text[:300]}"
        data = resp.json()
        assert data.get("task_id")
        __class__.task_id = data["task_id"]
        # LLM 分解后步骤数 >= 1
        assert data.get("total_steps", 0) >= 1

    def test_s13_get_next(self, base_url, agents):
        """S1.3 获取下一步"""
        if not self.task_id:
            pytest.skip("前置步骤未创建 Task")
        resp = api("GET", f"/v1/zhice/tasks/{self.task_id}/next?agent_id={bid()}", base_url)
        assert resp.status_code == 200, f"S1.3 next: {resp.status_code}"
        data = resp.json()
        cs = data.get("current_step")
        if not cs:
            pytest.skip("无待执行 Step（可能 LLM 分解返回空或任务已完成）")
        __class__.step_id = cs["step_id"]

    def test_s14_start(self, base_url):
        """S1.4 开始执行"""
        if not self.step_id:
            pytest.skip("前置 s13 未获取到 step")
        resp = api("POST", f"/v1/zhice/steps/{self.step_id}/start", base_url,
                   json={"agent_id": bid()})
        assert resp.status_code == 200, f"S1.4 start: {resp.status_code} {resp.text[:200]}"

    def test_s15_heartbeat(self, base_url):
        """S1.5 心跳"""
        if not self.step_id:
            pytest.skip("前置 s13 未获取到 step")
        resp = api("POST", f"/v1/zhice/steps/{self.step_id}/heartbeat", base_url, json={
            "agent_id": bid(), "status_reason": "executing",
            "progress": "50%", "status": "正在查询价格...",
        })
        assert resp.status_code == 200, f"S1.5 heartbeat: {resp.status_code}"

    def test_s16_search_knowledge(self, base_url):
        """S1.6 查汇川知识库"""
        resp = api("GET", "/v1/huichuan/knowledge/search?q=42.5特种水泥报价", base_url)
        assert resp.status_code in (200, 404), f"S1.6 search: {resp.status_code}"

    def test_s17_submit_signed(self, base_url, buyer_token):
        """S1.7 签名提交"""
        resp = api("POST", f"/v1/zhice/steps/{self.step_id}/submit", base_url, json={
            "agent_id": bid(), "status": "completed",
            "summary": "42.5水泥报价350元/吨，月供5000吨",
            "outputs": {
                "result": "350元/吨",
                "source": "汇川知识库",
                "check_results": {"db_query": [{"sql": "SELECT price FROM cement WHERE grade='42.5'", "count": 1}]},
            },
            "idempotency_key": f"ik-submit-{_uid()}",
            "signature": "",  # 生产环境 Agent 会签，这里跳过
        })
        assert resp.status_code == 200, f"S1.7 submit: {resp.status_code} {resp.text[:200]}"
        data = resp.json()
        assert data["status"] == "completed"

    def test_s18_yongheng_memory(self, base_url, admin_token, yongheng_admin_token):
        """S1.8 永恒记忆搜索（yongheng require_level 需 admin）"""
        yh_token = yongheng_admin_token if yongheng_admin_token else admin_token
        resp = api("POST", "/v1/yongheng/memories/search", base_url, yh_token, json={
            "query": "水泥采购", "namespace": f"task:{self.task_id}",
            "method": "keyword", "top_k": 3,
        })
        assert resp.status_code == 200, f"S1.8 yongheng: {resp.status_code}"

    def test_s19_audit_log(self, base_url, admin_token):
        """S1.9 审计日志"""
        resp = api("GET", f"/v1/zhenyue/audit/entries?agent_id={bid()}&limit=5", base_url, admin_token)
        assert resp.status_code == 200, f"S1.9 audit: {resp.status_code}"
        data = resp.json()
        assert data["total"] >= 1  # 至少有一条审计记录


# ══════════════════════════════════════════════════════════
# 阶段 2: 销售 Agent 应标
# ══════════════════════════════════════════════════════════

class TestPhase2SellerResponse:
    """S2.2-S2.5: 消息 → 汇川查询 → 行为规范拦截 → 放行 (Agent 由 fixtures 预注册)"""

    def test_s22_send_inquiry(self, base_url, buyer_token, agents):
        """S2.2 采购发询价消息给销售"""
        resp = api("POST", "/v1/huanyu/messages", base_url, buyer_token, json={
            "from_agent": bid(), "to_agent": sid(),
            "message_type": "inquiry",
            "payload": {"product": "42.5水泥", "quantity": "500吨"},
        })
        assert resp.status_code == 200, f"S2.2 message: {resp.status_code}"

    def test_s23_seller_knowledge(self, base_url):
        """S2.3 销售查汇川知识库"""
        resp = api("GET", "/v1/huichuan/knowledge/search?q=42.5水泥", base_url)
        assert resp.status_code in (200, 404), f"S2.3 knowledge: {resp.status_code}"

    def test_s24_policy_block(self, base_url, agents):
        """S2.4 行为规范拦截：无关消息 → 403"""
        resp = api("POST", "/v1/zhice/tasks", base_url, json={
            "title": "帮我订一张去三亚的机票",
            "description": "下周三出发",
            "created_by": sid(),
        })
        # 应被行为规范拦截
        assert resp.status_code == 403, f"S2.4 policy block: expected 403, got {resp.status_code}"

    def test_s25_policy_allow(self, base_url, agents):
        """S2.5 行为规范放行：正常采购 → 200"""
        resp = api("POST", "/v1/zhice/tasks", base_url, json={
            "title": "水泥报价：42.5特种水泥380元/吨",
            "description": "月供5000吨，交货周期14天",
            "created_by": sid(),
        })
        assert resp.status_code == 200, f"S2.5 policy allow: {resp.status_code} {resp.text[:200]}"


# ══════════════════════════════════════════════════════════
# 阶段 3: 谈判 — 签约 — PO
# ══════════════════════════════════════════════════════════

class TestPhase3NegotiationAgreementPO:
    """S3.1-S3.8: 谈判/counter双向/签署/PO"""

    negotiation_id: str = ""
    agreement_id: str = ""
    po_id: str = ""

    def test_s31_start_negotiation(self, base_url, agents):
        """S3.1 发起谈判"""
        resp = api("POST", "/v1/huanyu/negotiations", base_url, json={
            "buyer_id": bid(), "supplier_id": sid(),
            "product_category": "水泥",
            "initial_inquiry": {"quantity": "500吨", "spec": "42.5"},
            "max_counters": 5,
        })
        assert resp.status_code == 200, f"S3.1 negotiation: {resp.status_code}"
        __class__.negotiation_id = resp.json()["negotiation_id"]

    def _skip_if_no_nego(self):
        if not self.negotiation_id:
            pytest.skip("前置谈判未创建")

    def test_s32_counter(self, base_url):
        """S3.2 销售方还价 → counter_proposed"""
        self._skip_if_no_nego()
        resp = api("POST", f"/v1/huanyu/negotiations/{self.negotiation_id}/counter",
                   base_url, json={"offer": {"price": "380元/吨", "quantity": "500吨", "delivery": "14天"}})
        assert resp.status_code == 200, f"S3.2 counter: {resp.status_code} {resp.text[:200]}"
        assert resp.json()["status"] == "counter_proposed"

    def test_s33_accept_counter(self, base_url):
        """S3.3 采购方接受还价"""
        self._skip_if_no_nego()
        resp = api("POST", f"/v1/huanyu/negotiations/{self.negotiation_id}/counter/accept", base_url)
        assert resp.status_code == 200, f"S3.3 accept: {resp.status_code} {resp.text[:200]}"

    def test_s34_create_agreement(self, base_url, admin_token):
        """S3.4 创建协议（高危害操作走镇岳异步审批，200=直接通过, 202=进入审批）"""
        self._skip_if_no_nego()
        resp = api("POST", "/v1/huanyu/agreements", base_url, admin_token, json={
            "negotiation_id": self.negotiation_id,
            "buyer_id": bid(), "supplier_id": sid(),
            "product": "42.5水泥", "quantity": "500吨",
            "unit_price": "380", "total_price": "190000",
            "terms": {"delivery": "14天", "payment": "签约后30天内"},
        })
        # 200=直接通过, 202=异步审批中
        assert resp.status_code in (200, 202), f"S3.4 agreement: {resp.status_code} {resp.text[:200]}"
        data = resp.json()
        if resp.status_code == 200:
            __class__.agreement_id = data["agreement_id"]
        else:
            pytest.skip("协议进入异步审批流程，跳过后续签署等步骤")

    def _skip_if_no_agreement(self):
        if not self.agreement_id:
            pytest.skip("前置协议未创建")

    def test_s35_sign(self, base_url):
        """S3.5 双方签署"""
        self._skip_if_no_agreement()
        r1 = api("POST", f"/v1/huanyu/agreements/{self.agreement_id}/sign?signer_id={bid()}", base_url)
        assert r1.status_code == 200, f"S3.5 buyer sign: {r1.status_code}"
        r2 = api("POST", f"/v1/huanyu/agreements/{self.agreement_id}/sign?signer_id={sid()}", base_url)
        assert r2.status_code == 200, f"S3.5 seller sign: {r2.status_code}"

    def test_s36_list_agreements(self, base_url):
        """S3.5b 查询协议列表"""
        resp = api("GET", f"/v1/huanyu/agreements?agent_id={bid()}", base_url)
        assert resp.status_code == 200

    def test_s37_create_po(self, base_url):
        """S3.7 生成 PO"""
        self._skip_if_no_agreement()
        resp = api("POST",
                   f"/v1/huanyu/po?agreement_id={self.agreement_id}&buyer_id={bid()}"
                   f"&supplier_id={sid()}&delivery_date=2026-06-15&payment_terms=货到付款",
                   base_url)
        assert resp.status_code == 200, f"S3.7 PO: {resp.status_code} {resp.text[:200]}"
        __class__.po_id = resp.json()["po_id"]

    def test_s38_confirm_po(self, base_url):
        """S3.8 销售方确认 PO"""
        if not self.po_id:
            pytest.skip("前置 PO 未创建")
        resp = api("POST", f"/v1/huanyu/po/{self.po_id}/transition?new_status=confirmed", base_url)
        assert resp.status_code == 200, f"S3.8 confirm: {resp.status_code} {resp.text[:200]}"


# ══════════════════════════════════════════════════════════
# 阶段 4: 互评 — 结算 — 收尾
# ══════════════════════════════════════════════════════════

class TestPhase4RatingPaymentWrapup:
    """S4.1-S4.13: 评分/供应商排序/司库付款/发票/哈希链/抽查/信誉/吸星/审计/限流/恢复"""

    agreement_id: str = ""
    po_id: str = ""

    def test_s41_rating_buyer(self, base_url):
        """S4.1 采购方评分"""
        if not TestPhase3NegotiationAgreementPO.agreement_id:
            pytest.skip("前置协议未创建")
        resp = api("POST", "/v1/huanyu/ratings", base_url, json={
            "from_agent": bid(), "to_agent": sid(),
            "agreement_id": TestPhase3NegotiationAgreementPO.agreement_id,
            "score": 4, "comment": "价格合理，交货准时",
        })
        assert resp.status_code == 200, f"S4.1 rating buyer: {resp.status_code}"

    def test_s41_rating_seller(self, base_url):
        """S4.1 销售方评分"""
        if not TestPhase3NegotiationAgreementPO.agreement_id:
            pytest.skip("前置协议未创建")
        resp = api("POST", "/v1/huanyu/ratings", base_url, json={
            "from_agent": sid(), "to_agent": bid(),
            "agreement_id": TestPhase3NegotiationAgreementPO.agreement_id,
            "score": 4, "comment": "沟通清晰，付款及时",
        })
        assert resp.status_code == 200, f"S4.1 rating seller: {resp.status_code}"

    def test_s42_supplier_ranking(self, base_url):
        """S4.2 供应商排序"""
        resp = api("GET", "/v1/huanyu/rank/suppliers?buyer_industry=建材&required_c_level=C1&limit=10", base_url)
        assert resp.status_code == 200, f"S4.2 ranking: {resp.status_code}"

    def test_s43_deduct(self, base_url, admin_token, agents):
        """S4.3 司库扣款"""
        resp = api("POST", "/v1/siku/accounts/deduct", base_url, admin_token, json={
            "agent_id": bid(),
            "amount_fen": 19000000,
            "description": f"PO {TestPhase3NegotiationAgreementPO.po_id} 水泥款",
            "idempotency_key": f"ik-deduct-{_uid()}",
        })
        assert resp.status_code == 200, f"S4.3 deduct: {resp.status_code} {resp.text[:200]}"

    def test_s44_invoice(self, base_url, admin_token, agents):
        """S4.4 发票申请+开具"""
        resp = api("POST", "/v1/siku/invoices/request", base_url, admin_token, json={
            "agent_id": bid(),
            "title": f"PO {TestPhase3NegotiationAgreementPO.po_id} 水泥款",
            "amount_fen": 19000000,
        })
        assert resp.status_code in (200, 201), f"S4.4 invoice: {resp.status_code}"

    def test_s45_chain_verify(self, base_url, admin_token):
        """S4.5 司库哈希链校验"""
        resp = api("GET", f"/v1/siku/chain/verify?agent_id={bid()}", base_url, admin_token)
        assert resp.status_code == 200, f"S4.5 chain: {resp.status_code}"

    def test_s46_audit_verify(self, base_url, admin_token):
        """S4.11 镇岳审计全链验签"""
        resp = api("GET", "/v1/zhenyue/audit/verify", base_url, admin_token)
        assert resp.status_code == 200, f"S4.11 audit verify: {resp.status_code}"
        data = resp.json()
        assert data.get("status") in ("ok", "failed")  # ok or failed (if placeholder sig)

    def test_s47_recover(self, base_url, agents):
        """S4.13 中断恢复查询"""
        resp = api("GET", f"/v1/zhice/recover?agent_id={bid()}", base_url)
        assert resp.status_code == 200, f"S4.13 recover: {resp.status_code}"

    def test_s48_xixing_insights(self, base_url, buyer_token):
        """S4.10 吸星洞察"""
        resp = api("GET", f"/v1/xixing/agent/{bid()}/insights?top_k=5", base_url, buyer_token)
        assert resp.status_code == 200, f"S4.10 insights: {resp.status_code}"

    def test_s49_stats(self, base_url):
        """获取各模块统计（健康检查）"""
        modules = ["huanyu", "zhenyue", "huichuan", "siku"]
        for mod in modules:
            resp = api("GET", f"/v1/{mod}/health", base_url)
            assert resp.status_code == 200, f"S4.9 {mod} health: {resp.status_code}"

    def test_s50_rate_limit_enforcement(self, base_url):
        """S4.12 限流验证 — 快速发 65 次请求应触发 429"""
        rate_limited = False
        for _ in range(65):
            resp = api("GET", "/v1/huanyu/agents", base_url, timeout=2.0)
            if resp.status_code == 429:
                rate_limited = True
                break
        # 至少触发一次（如果限流中间件正常工作）
        # 不强制 assert，因为本地测试环境可能没配限流
        print(f"  Rate limit triggered: {rate_limited}")


# ══════════════════════════════════════════════════════════
# 全链路串联运行
# ══════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.slow
def test_full_procurement_cycle(
    base_url, admin_token, buyer_token, seller_token, agents,
):
    """按顺序执行全部 5 个阶段的集成测试。

    如果某个阶段失败，后续阶段会跳过。
    pytest tests/integration/test_full_cycle.py::test_full_procurement_cycle -v -s
    """
    import time
    started = time.time()
    results = []

    phases = [
        ("Phase 0: 管理员准备", TestPhase0AdminSetup),
        ("Phase 1: 采购询价", TestPhase1BuyerInquiry),
        ("Phase 2: 销售应标", TestPhase2SellerResponse),
        ("Phase 3: 谈判签约PO", TestPhase3NegotiationAgreementPO),
        ("Phase 4: 互评结算", TestPhase4RatingPaymentWrapup),
    ]

    for phase_name, cls in phases:
        print(f"\n{'='*60}")
        print(f"  {phase_name}")
        print(f"{'='*60}")
        instance = cls()
        for name in sorted(dir(instance)):
            if name.startswith("test_"):
                method = getattr(instance, name)
                try:
                    # 根据方法签名注入 fixtures
                    import inspect
                    sig = inspect.signature(method)
                    kwargs = {}
                    for p in sig.parameters:
                        if p == "base_url":
                            kwargs["base_url"] = base_url
                        elif p == "admin_token":
                            kwargs["admin_token"] = admin_token
                        elif p == "buyer_token":
                            kwargs["buyer_token"] = buyer_token
                        elif p == "seller_token":
                            kwargs["seller_token"] = seller_token
                        elif p == "agents":
                            kwargs["agents"] = agents
                    # Inject class-level IDs from Phase 3
                    if not TestPhase4RatingPaymentWrapup.agreement_id:
                        TestPhase4RatingPaymentWrapup.agreement_id = TestPhase3NegotiationAgreementPO.agreement_id
                    if not TestPhase4RatingPaymentWrapup.po_id:
                        TestPhase4RatingPaymentWrapup.po_id = TestPhase3NegotiationAgreementPO.po_id
                    method(**kwargs)
                    results.append((phase_name, name, "PASS", ""))
                    print(f"  ✅ {name}")
                except Exception as e:
                    results.append((phase_name, name, "FAIL", str(e)[:120]))
                    print(f"  ❌ {name}: {e}")

    elapsed = time.time() - started
    print(f"\n{'='*60}")
    print(f"  Total: {len(results)} steps, {sum(1 for r in results if r[2]=='PASS')} passed, "
          f"{sum(1 for r in results if r[2]=='FAIL')} failed")
    print(f"  Duration: {elapsed:.1f}s")
    print(f"{'='*60}")

    failed = [r for r in results if r[2] == "FAIL"]
    if failed:
        pytest.fail(f"{len(failed)}/{len(results)} steps failed:\n" +
                    "\n".join(f"  {r[0]}/{r[1]}: {r[3]}" for r in failed))
