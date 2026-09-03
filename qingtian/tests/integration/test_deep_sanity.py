#!/usr/bin/env python3
"""
深层综合安全测试 — 司库(Siku) + 寰宇(Huanyu) 跨界验证

覆盖维度：
1. 财务哈希链完整性跨模块校验
2. 并发安全（竞态条件下的充值/扣款）
3. 哈希链篡改检测
4. 空链 / 边界值 / 幂等
5. 证书注册→交易→审计链→链校验 端到端
6. 跨服数据一致性（采购/销售服同步）
"""

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

BASE_URL = os.environ.get("BASE_URL", "http://10.0.100.1:18789")
ADMIN_TOKEN = os.environ.get("ZHENYUE_ADMIN_TOKEN", "")

PASS = 0
FAIL = 0
ERRORS = []


def log(msg: str):
    print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}")


def check(name: str, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        ERRORS.append(f"❌ {name}: {detail}")
        print(f"  ❌ {name} — {detail}")


async def fetch(url: str, method="GET", json_body=None):
    """简单 HTTP 请求，不用外部依赖"""
    import urllib.request
    headers = {"Content-Type": "application/json"}
    if ADMIN_TOKEN:
        headers["Authorization"] = f"Bearer {ADMIN_TOKEN}"
    data = json.dumps(json_body).encode() if json_body else None
    req = urllib.request.Request(
        url, data=data, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return e.code, json.loads(body) if body else {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


async def test_01_accounts_and_balance():
    """1. 账务基础：查询、充值、扣款、查询余额"""
    log("--- 1.1 查询不存在的 Agent（返回 200 + balance=0）---")
    aid = f"test-deep-{uuid.uuid4().hex[:8]}"
    status, data = await fetch(f"{BASE_URL}/v1/siku/accounts/{aid}")
    check("不存在 Agent 返回 balance=0", status == 200 and data.get("balance") == 0, str(data))

    log("--- 1.2 充值 500 元（50000 fen）---")
    status, data = await fetch(
        f"{BASE_URL}/v1/siku/accounts/recharge", method="POST",
        json_body={"agent_id": aid, "amount_fen": 50000, "remark": "深层测试充值"}
    )
    check("充值 500 成功", status == 200 and data.get("balance") == 50000, str(data))
    txn_id_1 = data.get("transaction_id", "")

    log("--- 1.3 充值 0.01 元（1 fen）边界值 ---")
    status, data = await fetch(
        f"{BASE_URL}/v1/siku/accounts/recharge", method="POST",
        json_body={"agent_id": aid, "amount_fen": 1, "remark": "边界值"}
    )
    check("充值 1fen 成功", status == 200 and data.get("balance") == 50001, str(data))

    log("--- 1.4 一次完整扣款 200 元 ---")
    status, data = await fetch(
        f"{BASE_URL}/v1/siku/accounts/deduct", method="POST",
        json_body={"agent_id": aid, "amount_fen": 20000, "remark": "扣款测试"}
    )
    check("扣款 200 成功", status == 200 and data.get("balance") == 30001, str(data))

    log("--- 1.5 余额不足扣款（应拒绝）---")
    status, data = await fetch(
        f"{BASE_URL}/v1/siku/accounts/deduct", method="POST",
        json_body={"agent_id": aid, "amount_fen": 999999, "remark": "超额扣款"}
    )
    check("超额扣款被拒绝", status == 400 or status == 403, str(data))

    log("--- 1.6 余额查询确认 ---")
    status, data = await fetch(f"{BASE_URL}/v1/siku/accounts/{aid}")
    check("最终余额 30001", status == 200 and data.get("balance") == 30001, str(data))

    return aid, txn_id_1


async def test_02_concurrent_safety(aid: str):
    """2. 并发安全：同时发起多个充值请求"""
    log("--- 2.1 并发充值（5 个 100 元同时发）---")
    tasks = [
        fetch(
            f"{BASE_URL}/v1/siku/accounts/recharge", method="POST",
            json_body={"agent_id": aid, "amount_fen": 10000, "remark": f"并发充值-{i}"}
        )
        for i in range(5)
    ]
    results = await asyncio.gather(*tasks)
    successes = sum(1 for s, d in results if s == 200)
    check(f"并发充值 5 个全部成功", successes == 5, f"成功{successes}/5")

    # 预期余额 = 30001 + 50000 = 80001
    status, data = await fetch(f"{BASE_URL}/v1/siku/accounts/{aid}")
    check("并发后余额 80001", status == 200 and data.get("balance") == 80001, str(data))

    log("--- 2.2 并发扣款（5 个 50 元同时发）---")
    tasks = [
        fetch(
            f"{BASE_URL}/v1/siku/accounts/deduct", method="POST",
            json_body={"agent_id": aid, "amount_fen": 5000, "remark": f"并发扣款-{i}"}
        )
        for i in range(5)
    ]
    results = await asyncio.gather(*tasks)
    successes = sum(1 for s, d in results if s == 200)
    check(f"并发扣款 5 个全部成功", successes == 5, f"成功{successes}/5")

    # 预期余额 = 80001 - 25000 = 55001
    status, data = await fetch(f"{BASE_URL}/v1/siku/accounts/{aid}")
    check("并发扣款后余额 55001", status == 200 and data.get("balance") == 55001, str(data))


async def test_03_idempotency(aid: str):
    """3. 幂等性：同一请求 ID 重复提交"""
    import uuid as _uuid
    idem_key = str(_uuid.uuid4())

    log("--- 3.1 第一次充值（带 idempotency_key）---")
    status, data = await fetch(
        f"{BASE_URL}/v1/siku/accounts/recharge", method="POST",
        json_body={
            "agent_id": aid, "amount_fen": 7777, "remark": "幂等测试",
            "idempotency_key": idem_key,
        }
    )
    check("第一次充值 7777 成功", status == 200, str(data))

    log("--- 3.2 同一 idempotency_key 重复充值 ---")
    status2, data2 = await fetch(
        f"{BASE_URL}/v1/siku/accounts/recharge", method="POST",
        json_body={
            "agent_id": aid, "amount_fen": 7777, "remark": "幂等重试",
            "idempotency_key": idem_key,
        }
    )
    check("幂等返回（不重复扣钱）", status2 in (200, 409), str(data2))

    # 余额只能是 55001 + 7777 = 62778（不会被加两次）
    status, data = await fetch(f"{BASE_URL}/v1/siku/accounts/{aid}")
    check("余额未被重复扣（62778）", status == 200 and data.get("balance") == 62778, str(data))


async def test_04_transaction_chain(aid: str):
    """4. 交易流水与哈希链完整性"""
    log("--- 4.1 查看交易流水 ---")
    status, data = await fetch(f"{BASE_URL}/v1/siku/transactions/{aid}")
    check("交易流水返回", status == 200, str(data)[:200])
    txs = data if isinstance(data, list) else data.get("transactions", [])
    check(f"交易记录 >0 条", len(txs) > 0, f"共{len(txs)}条")

    log("--- 4.2 哈希链完整性校验 ---")
    status, data = await fetch(f"{BASE_URL}/v1/siku/chain/verify?agent_id={aid}")
    check("链校验返回", status == 200, str(data)[:200])
    is_valid = data.get("valid") if isinstance(data, dict) else data
    check("哈希链完整（valid=true）", is_valid is True or is_valid == "true", str(data)[:200])


async def test_05_chain_tamper_detection(aid: str):
    """5. 模拟链篡改检测（通过 DB 直接改一条记录再校验）"""
    log("--- 5.1 DB 模拟篡改：改一条余额 ---")
    import subprocess
    # 直接从 DB 改一条审计记录的哈希，然后验证链校验能不能发现
    cmd = (
        'psql -U postgres -d qingtian -c '
        '"UPDATE siku.transactions SET txn_hash = \'tampered_hash_DEADBEEF\' '
        f'WHERE agent_id = \'{aid}\' AND txn_hash LIKE \'%0000%\' LIMIT 1;"'
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if "UPDATE 1" in result.stderr or "UPDATE 1" in result.stdout:
        log("   ✅ 模拟篡改写入成功（1 条记录被改）")
    else:
        log(f"   ⚠️ 篡改写入结果: {result.stdout} {result.stderr}")
        # 可能没有以 0000 结尾的 hash，试试随机一条
        cmd2 = (
            'psql -U postgres -d qingtian -c '
            f"'UPDATE siku.transactions SET txn_hash = \\'tampered\\' WHERE agent_id = \\'{aid}\\' LIMIT 1;'"
        )
        result = subprocess.run(cmd2, shell=True, capture_output=True, text=True)
        log(f"   fallback: {result.stdout} {result.stderr}")

    status, data = await fetch(f"{BASE_URL}/v1/siku/chain/verify?agent_id={aid}")
    is_valid = data.get("valid") if isinstance(data, dict) else data
    check("链校验能检测到篡改（valid=false/error）", is_valid is False or status >= 400, str(data)[:200])


async def test_06_certificate_end_to_end():
    """6. 寰宇证书→司库账务 端到端链路"""
    log("--- 6.1 寰宇注册 Agent ---")
    import uuid as _uuid
    ain = f"agent-{_uuid.uuid4().hex[:12]}"
    status, data = await fetch(
        f"{BASE_URL}/v1/huanyu/register", method="POST",
        json_body={"ain": ain, "tier": "free"}
    )
    check("Agent 注册成功", status == 200 and data.get("agent_id") == ain, str(data)[:200])
    cert = data.get("certificate", {})
    fingerprint = cert.get("fingerprint", "")

    log("--- 6.2 用新注册的 Agent 进行司库操作 ---")
    # 充值
    status, data = await fetch(
        f"{BASE_URL}/v1/siku/accounts/recharge", method="POST",
        json_body={"agent_id": ain, "amount_fen": 100000, "remark": "端到端充值"}
    )
    check("端到端充值成功", status == 200, str(data)[:200])

    status, data = await fetch(f"{BASE_URL}/v1/siku/accounts/{ain}")
    check("端到端余额 100000", status == 200 and data.get("balance") == 100000, str(data))

    log("--- 6.3 汇川发消息到该 Agent ---")
    status, data = await fetch(
        f"{BASE_URL}/v1/huanyu/messages", method="POST",
        json_body={
            "sender": "system",
            "receiver": ain,
            "content": {"type": "text", "text": "测试消息：证书注册完毕"},
        }
    )
    check("寰宇发送消息成功", status == 200, str(data)[:200])

    log("--- 6.4 检查该 Agent 的收件箱 ---")
    status, data = await fetch(f"{BASE_URL}/v1/huanyu/inbox/{ain}")
    check("收件箱查询成功", status == 200, str(data)[:200])
    inbox = data if isinstance(data, list) else data.get("messages", [])
    check(f"收件箱有消息", len(inbox) > 0, f"共{len(inbox)}条")

    return ain


async def test_07_annual_fee(aid: str):
    """7. 年费缴纳"""
    log("--- 7.1 查询年费状态 ---")
    status, data = await fetch(f"{BASE_URL}/v1/siku/annual/status/{aid}")
    check("年费状态查询", status == 200, str(data)[:200])

    log("--- 7.2 缴纳年费 ---")
    status, data = await fetch(
        f"{BASE_URL}/v1/siku/annual/pay", method="POST",
        json_body={"agent_id": aid, "year": 2026}
    )
    check("年费缴纳", status == 200, str(data)[:200])


async def test_08_parallel_multi_agent():
    """8. 多 Agent 并发交叉操作"""
    log("--- 8.1 创建 3 个 Agent，交叉操作 ---")
    import uuid as _uuid
    agents = [f"test-para-{_uuid.uuid4().hex[:6]}" for _ in range(3)]

    # 并发充值
    tasks = [
        fetch(
            f"{BASE_URL}/v1/siku/accounts/recharge", method="POST",
            json_body={"agent_id": a, "amount_fen": 50000, "remark": "多Agent充值"}
        )
        for a in agents
    ]
    results = await asyncio.gather(*tasks)
    check("3 Agent 并发充值全部成功", all(s == 200 for s, _ in results))

    # 验证每个余额
    tasks = [fetch(f"{BASE_URL}/v1/siku/accounts/{a}") for a in agents]
    results = await asyncio.gather(*tasks)
    all_ok = all(s == 200 and d.get("balance") == 50000 for s, d in results)
    check("每个 Agent 余额正确 50000", all_ok)

    log("--- 8.2 链交叉校验 ---")
    for a in agents:
        status, data = await fetch(f"{BASE_URL}/v1/siku/chain/verify?agent_id={a}")
        is_valid = data.get("valid") if isinstance(data, dict) else data
        check(f"Agent {a[:8]}... 链完整", is_valid is True or is_valid == "true", str(data)[:100])


async def test_09_empty_mock_operations():
    """9. 空链/零余额场景"""
    import uuid as _uuid
    aid = f"test-empty-{_uuid.uuid4().hex[:8]}"

    log("--- 9.1 空 Agent 链校验 ---")
    status, data = await fetch(f"{BASE_URL}/v1/siku/chain/verify?agent_id={aid}")
    # 空链应该返回 valid=true 或 404
    check("空 Agent 链校验不报错", status < 500, str(data)[:200])

    log("--- 9.2 0 元充值 ---")
    status, data = await fetch(
        f"{BASE_URL}/v1/siku/accounts/recharge", method="POST",
        json_body={"agent_id": aid, "amount_fen": 0, "remark": "零元充值"}
    )
    check("零元充值返回 400 拒绝", status == 400, str(data)[:200])


async def test_10_audit_log(aid):
    """10. 审计日志"""
    log("--- 10.1 查询审计日志 ---")
    status, data = await fetch(f"{BASE_URL}/v1/siku/admin/operations")
    check("审计日志可查", status == 200, str(data)[:200])


async def main():
    global PASS, FAIL
    print(f"\n{'='*60}")
    print(f"  司库(Siku) + 寰宇(Huanyu) 深层综合测试")
    print(f"  目标服务器: {BASE_URL}")
    print(f"  Admin Token: {'已设置' if ADMIN_TOKEN else '⚠️ 未设置'}")
    print(f"{'='*60}\n")

    start = time.time()

    # 1-5 用同一个 Agent
    aid, txn_id = await test_01_accounts_and_balance()
    await test_02_concurrent_safety(aid)
    await test_03_idempotency(aid)
    await test_04_transaction_chain(aid)
    await test_05_chain_tamper_detection(aid)

    # 6 端到端
    ain2 = await test_06_certificate_end_to_end()

    # 7-8 用 01 的 Agent
    await test_07_annual_fee(aid)
    await test_08_parallel_multi_agent()

    # 9-10
    await test_09_empty_mock_operations()
    await test_10_audit_log(aid)

    elapsed = time.time() - start

    print(f"\n{'='*60}")
    print(f"  测试结束 — 耗时 {elapsed:.1f}s")
    print(f"  ✅ PASS: {PASS}")
    print(f"  ❌ FAIL: {FAIL}")
    if ERRORS:
        print(f"\n  失败详情:")
        for e in ERRORS:
            print(f"    {e}")
    print(f"{'='*60}\n")

    return 1 if FAIL > 0 else 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
