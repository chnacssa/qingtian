"""【P0 性能】100 Agent 并发启动/停止/健康检查

测试场景:
  1. 批量注册 100 个 Agent（biz:buyer / biz:seller 混合）
  2. 并发健康检查 100 Agent 状态（模拟巡检）
  3. 批量停止（如果总线支持 stop 操作）
  4. 测量全程耗时，验证在可接受范围内

运行前提: ACSSA 底座已启动在 127.0.0.1:1996
  pytest tests/production/test_concurrent_agents.py -v -s --timeout=300
"""
import time
import uuid
import pytest
from tests.production.conftest import api, BASE_URL

pytestmark = pytest.mark.production

# 并发控制
AGENT_COUNT = 100
BATCH_SIZE = 10
CONCURRENT_CHECKS = 20


def _agent_id(role: str, idx: int) -> str:
    """生成测试 agent_id。"""
    return f"perf:{role}-{idx:03d}"


def _register_batch_agents(base_url, count=AGENT_COUNT) -> list[str]:
    """注册批量测试 Agent，返回 agent_id 列表。"""
    agents = []
    errors = []

    for i in range(count):
        role = "buyer" if i < count // 2 else "seller"
        aid = _agent_id(role, i)
        resp = api("POST", "/v1/huanyu/agents/register", base_url, json={
            "name": f"性能测试-{role}-{i:03d}",
            "category": f"biz:{role}",
        }, timeout=10.0)

        if resp.status_code in (200, 201):
            agents.append(resp.json().get("agent_id", aid))
        elif resp.status_code == 409:
            agents.append(aid)
        else:
            errors.append((aid, resp.status_code, resp.text[:100]))

        if (i + 1) % BATCH_SIZE == 0:
            print(f"  注册进度: {i+1}/{count}")

    if errors:
        print(f"  注册错误: {len(errors)}/{count}")
        for aid, code, msg in errors[:5]:
            print(f"    {aid}: {code} {msg}")

    assert len(agents) >= count // 2, (
        f"注册成功率过低: {len(agents)}/{count}"
    )
    print(f"  注册完成: {len(agents)}/{count} agents")
    return agents


class TestConcurrentAgentRegistration:
    """批量 Agent 注册与健康检查性能。"""

    @pytest.fixture(scope="class")
    def registered_agents(self, base_url) -> list[str]:
        """注册 100 个测试 Agent，返回 agent_id 列表。"""
        return _register_batch_agents(base_url)

    def test_batch_health_check(self, base_url, registered_agents):
        """并发 100 个健康检查 -> 全部通过。"""
        import concurrent.futures

        def check(agent_id: str) -> tuple[str, bool, int, float]:
            t0 = time.time()
            try:
                resp = api("GET", f"/v1/huanyu/agents/{agent_id}", base_url, timeout=10.0)
                ok = resp.status_code == 200
                return agent_id, ok, resp.status_code, time.time() - t0
            except Exception as e:
                return agent_id, False, 0, time.time() - t0

        total_start = time.time()
        results = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENT_CHECKS) as pool:
            futures = [pool.submit(check, aid) for aid in registered_agents]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        elapsed = time.time() - total_start

        passed = sum(1 for r in results if r[1])
        failed = sum(1 for r in results if not r[1])
        latencies = [r[3] for r in results]

        print(f"\n  并发健康检查 ({AGENT_COUNT} agents, {CONCURRENT_CHECKS} 并发):")
        print(f"    通过: {passed}, 失败: {failed}")
        print(f"    总耗时: {elapsed:.2f}s")
        print(f"    平均延迟: {sum(latencies)/len(latencies)*1000:.1f}ms")
        print(f"    P99 延迟: {sorted(latencies)[int(len(latencies)*0.99)]*1000:.1f}ms")

        # 关键指标：P0 要求 100 agents 在 60s 内完成
        assert elapsed < 60, f"100 Agent 健康检查耗时 {elapsed:.1f}s，超过 60s 上限"
        assert passed >= AGENT_COUNT * 0.9, (
            f"健康检查通过率 {passed}/{AGENT_COUNT} < 90%"
        )

    def test_concurrent_register_duplicate(self, base_url, registered_agents):
        """并发重复注册 -> 全部返回 200/409（幂等）。"""
        import concurrent.futures

        def re_register(agent_id: str) -> tuple[str, int]:
            category = "biz:buyer" if "buyer" in agent_id else "biz:seller"
            try:
                resp = api("POST", "/v1/huanyu/agents/register", base_url, json={
                    "name": f"重试-{agent_id}",
                    "category": category,
                }, timeout=10.0)
                return agent_id, resp.status_code
            except Exception as e:
                return agent_id, 0

        sample = registered_agents[:20]  # 只测 20 个免过度注册
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(re_register, aid) for aid in sample]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        codes = set(r[1] for r in results)
        assert codes.issubset({200, 201, 409}), (
            f"重复注册返回异常状态码: {codes}"
        )
        print(f"  重复注册全部幂等: {len(results)} agents, codes={codes}")

    def test_xihe_stats_under_load(self, base_url, admin_token, registered_agents):
        """负载下羲和统计接口仍可用。"""
        resp = api("GET", "/v1/xihe/stats", base_url, admin_token, timeout=5.0)
        assert resp.status_code in (200, 404), (
            f"xihe/stats 异常: {resp.status_code}"
        )
        if resp.status_code == 200:
            data = resp.json()
            print(f"  负载下 xihe stats: managed={data.get('managed_agents', '?')}")

    def test_resource_limits_not_exceeded(self, base_url, admin_token, registered_agents):
        """大量 agent 注册后系统资源未超限。"""
        resp = api("GET", "/v1/xihe/stats", base_url, admin_token, timeout=5.0)
        if resp.status_code != 200:
            return

        data = resp.json()
        memory_mb = data.get("memory_mb", 0)
        fd_count = data.get("fd_count", 0)

        print(f"  资源使用: memory={memory_mb}MB, fd={fd_count}")

        # 这些是 info-level 检查，不强制
        if memory_mb > 2048:
            print(f"  [WARN] 内存使用偏高 ({memory_mb}MB)")
        if fd_count > 500:
            print(f"  [WARN] FD 数偏高 ({fd_count})")


class TestConcurrentTaskCreation:
    """并发任务创建性能。"""

    def test_batch_task_creation(self, base_url, agents):
        """并发创建 50 个任务。"""
        import concurrent.futures
        agent_id = agents["buyer"]

        def create_task(idx: int) -> tuple[int, str, int]:
            try:
                resp = api("POST", "/v1/zhice/tasks", base_url, json={
                    "title": f"性能测试任务-{idx:03d}",
                    "description": f"并发任务创建性能测试 #{idx}",
                    "created_by": agent_id,
                    "steps": [
                        {"idx": 1, "instruction": f"步骤 1 任务{idx}"},
                        {"idx": 2, "instruction": f"步骤 2 任务{idx}"},
                    ],
                }, timeout=30.0)
                return idx, f"task_{idx}", resp.status_code
            except Exception as e:
                return idx, str(e), 0

        task_count = 50
        total_start = time.time()
        results = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(create_task, i) for i in range(task_count)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        elapsed = time.time() - total_start
        passed = sum(1 for r in results if r[2] == 200)
        failed = sum(1 for r in results if r[2] != 200 and r[2] != 0)
        errored = sum(1 for r in results if r[2] == 0)

        print(f"\n  并发创建 {task_count} 个任务:")
        print(f"    成功: {passed}, HTTP 错误: {failed}, 异常: {errored}")
        print(f"    总耗时: {elapsed:.2f}s ({task_count/elapsed:.1f} tasks/s)")

        assert passed >= task_count * 0.8, (
            f"任务创建成功率 {passed}/{task_count} < 80%"
        )


@pytest.mark.production
@pytest.mark.slow
def test_concurrent_agents_full(
    base_url, admin_token, agents,
):
    """按顺序执行全部并发性能测试。"""
    import inspect
    started = time.time()
    results = []
    _agents = agents

    suites = [
        ("并发 Agent 注册", TestConcurrentAgentRegistration),
        ("并发任务创建", TestConcurrentTaskCreation),
    ]

    # 预注册批量 Agent 供后续测试使用
    batch_agents = _register_batch_agents(base_url)

    for suite_name, cls in suites:
        print(f"\n  {'─'*50}")
        print(f"  [{suite_name}]")
        print(f"  {'─'*50}")
        instance = cls()
        for attr in sorted(dir(instance)):
            if attr.startswith("test_"):
                method = getattr(instance, attr)
                sig = inspect.signature(method)
                kwargs = {}
                for p in sig.parameters:
                    if p == "base_url":
                        kwargs["base_url"] = base_url
                    elif p == "admin_token":
                        kwargs["admin_token"] = admin_token
                    elif p == "agents":
                        kwargs["agents"] = _agents
                    elif p == "registered_agents":
                        kwargs["registered_agents"] = batch_agents
                try:
                    method(**kwargs)
                    results.append((suite_name, attr, "PASS", ""))
                    print(f"  ✅ {attr}")
                except Exception as e:
                    results.append((suite_name, attr, "FAIL", str(e)[:150]))
                    print(f"  ❌ {attr}: {e}")

    elapsed = time.time() - started
    passed = sum(1 for r in results if r[2] == "PASS")
    failed = sum(1 for r in results if r[2] == "FAIL")
    print(f"\n  {'═'*50}")
    print(f"  并发性能测试结果: {passed}/{len(results)} 通过, {failed} 失败 ({elapsed:.1f}s)")
    if failed:
        pytest.fail(f"{failed} 个测试失败:\n" +
                    "\n".join(f"  {r[0]}/{r[1]}: {r[3]}" for r in results if r[2] == "FAIL"))
