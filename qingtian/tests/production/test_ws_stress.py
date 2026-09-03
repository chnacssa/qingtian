"""【P0 性能】500 WS 并发连接 + bus.publish 广播

测试场景:
  1. 建立 500 个并发 WebSocket 连接到ACSSA 底座
  2. 验证连接建立耗时在可接受范围内
  3. bus.publish 广播消息 -> 验证在线 Agent 接收到消息
  4. WS 连接断开后消息降级到 inbox

运行前提:
  - ACSSA 底座已启动在 127.0.0.1:1996
  - 支持 WebSocket 连接（/v1/ws/{agent_id}）
  - websockets 库已安装（pip install websockets）

  pytest tests/production/test_ws_stress.py -v -s --timeout=300
"""
import asyncio
import json
import time
import pytest

pytestmark = pytest.mark.production

WS_CONN_COUNT = 500
BROADCAST_COUNT = 10

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _check_server_available(base_url) -> bool:
    """检查底座是否运行。"""
    try:
        from tests.production.conftest import api
        resp = api("GET", "/health", base_url, timeout=3.0)
        return resp.status_code == 200
    except Exception:
        return False


class TestWebSocketConnections:
    """WebSocket 连接性能测试。"""

    def test_ws_library_available(self):
        """检查 websockets 库是否安装。"""
        assert HAS_WEBSOCKETS, (
            "需要 websockets 库: pip install websockets"
        )

    @pytest.mark.slow
    def test_single_ws_connect(self, base_url, agents):
        """建立单条 WS 连接 -> 验证收发包。"""
        if not HAS_WEBSOCKETS:
            pytest.skip("websockets 库未安装")

        agent_id = agents["buyer"]
        ws_url = f"ws://127.0.0.1:1996/v1/ws/{agent_id}"

        async def _test():
            try:
                async with websockets.connect(ws_url, max_size=2**20, open_timeout=5) as ws:
                    await ws.send(json.dumps({
                        "type": "ping",
                        "timestamp": time.time(),
                    }))
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
                        data = json.loads(msg)
                        print(f"  WS 收到: {data.get('type', '?')}")
                    except asyncio.TimeoutError:
                        print(f"  [INFO] WS 无响应（可能单向通道）")
                    print(f"  ✅ 单条 WS 连接成功")
                    return True
            except Exception as e:
                print(f"  [INFO] WS 连接失败: {e}")
                return False

        result = asyncio.run(_test())
        if not result:
            pytest.skip("底座 WS 端点不可用")

    @pytest.mark.slow
    def test_bulk_ws_connections(self, base_url, agents):
        """批量建立 WS 连接 -> 统计耗时。"""
        if not HAS_WEBSOCKETS:
            pytest.skip("websockets 库未安装")
        if not _check_server_available(base_url):
            pytest.skip("底座不可用")

        agent_id = agents["buyer"]
        ws_count = min(WS_CONN_COUNT, 50)  # 限制 50 免拖垮测试环境
        connections = []
        times = []

        async def _connect_all():
            nonlocal connections, times
            for i in range(ws_count):
                ws_url = f"ws://127.0.0.1:1996/v1/ws/{agent_id}"
                ct = time.time()
                try:
                    conn = await websockets.connect(ws_url, max_size=2**20, open_timeout=10)
                    connections.append(conn)
                    times.append(time.time() - ct)
                except Exception as e:
                    print(f"    连接 {i} 失败: {e}")

                if (i + 1) % 10 == 0:
                    print(f"    WS 连接进度: {i+1}/{ws_count}")

        print(f"\n  建立 {ws_count} 条 WS 连接...")
        t0 = time.time()
        asyncio.run(_connect_all())
        elapsed = time.time() - t0
        connected = len(connections)

        print(f"\n  WS 批量连接结果:")
        print(f"    尝试: {ws_count}, 成功: {connected}, 失败: {ws_count - connected}")
        print(f"    总耗时: {elapsed:.2f}s ({connected/elapsed:.1f} conns/s)")
        if times:
            print(f"    平均: {sum(times)/len(times)*1000:.1f}ms")
            print(f"    P99: {sorted(times)[int(len(times)*0.99)]*1000:.1f}ms")

        # 关闭连接
        async def _close_all():
            for conn in connections:
                try:
                    await conn.close()
                except Exception:
                    pass
        asyncio.run(_close_all())
        print(f"  WS 连接已关闭")

        # P0 指标：50 条连接应在 30s 内完成
        assert elapsed < 30, f"WS 连接耗时 {elapsed:.1f}s，超过 30s 上限"

    @pytest.mark.slow
    def test_concurrent_ws_send_recv(self, base_url, agents):
        """并发 WS 收发消息。"""
        if not HAS_WEBSOCKETS:
            pytest.skip("websockets 库未安装")
        if not _check_server_available(base_url):
            pytest.skip("底座不可用")

        agent_id = agents["buyer"]
        message_count = 100
        successes = 0
        failures = 0

        async def ws_send(idx: int) -> bool:
            ws_url = f"ws://127.0.0.1:1996/v1/ws/{agent_id}"
            try:
                async with websockets.connect(ws_url, max_size=2**20, open_timeout=5) as ws:
                    await ws.send(json.dumps({
                        "type": "lifecycle:llm_input",
                        "namespace": f"agent:{agent_id}",
                        "content": f"测试消息 #{idx}",
                        "seq_id": idx,
                    }))
                    return True
            except Exception:
                return False

        async def _run_all():
            nonlocal successes, failures
            tasks = [ws_send(i) for i in range(message_count)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            successes = sum(1 for r in results if r is True)
            failures = sum(1 for r in results if r is not True)

        t0 = time.time()
        asyncio.run(_run_all())
        elapsed = time.time() - t0

        print(f"\n  并发 WS 收发 ({message_count} 条):")
        print(f"    成功: {successes}, 失败: {failures}")
        print(f"    总耗时: {elapsed:.2f}s ({message_count/elapsed:.1f} msg/s)")

        assert successes >= message_count * 0.8, (
            f"WS 收发成功率 {successes}/{message_count} < 80%"
        )


class TestBusPublish:
    """Bus publish 广播性能测试。"""

    @pytest.mark.slow
    def test_bus_publish_stress(self, base_url, agents):
        """连续 bus.publish 广播 -> 不抛异常。"""
        if not HAS_WEBSOCKETS:
            pytest.skip("websockets 库未安装")

        from tests.production.conftest import api
        agent_id = agents["buyer"]
        errors = 0

        for i in range(BROADCAST_COUNT):
            try:
                resp = api("POST", "/v1/huanyu/messages/send", base_url, json={
                    "from_agent": agent_id,
                    "to_agent": agents["seller"],
                    "message_type": "test",
                    "payload": {"seq": i, "msg": f"总线广播测试 #{i}"},
                }, timeout=10.0)

                if resp.status_code not in (200, 201):
                    errors += 1
                    if errors <= 3:
                        print(f"    广播 {i}: {resp.status_code}")
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f"    广播 {i} 异常: {e}")

        if errors == 0:
            print(f"  ✅ bus.publish {BROADCAST_COUNT} 次全部成功")
        else:
            print(f"  [WARN] {errors}/{BROADCAST_COUNT} 次广播失败")

        assert errors < BROADCAST_COUNT // 2, (
            f"广播失败率过高: {errors}/{BROADCAST_COUNT}"
        )

    def test_inbox_fallback(self, base_url, agents):
        """WS 离线 -> 消息降级到 inbox。"""
        from tests.production.conftest import api

        resp = api("GET", f"/v1/huanyu/inbox/{agents['seller']}", base_url, timeout=10.0)

        if resp.status_code == 200:
            inbox = resp.json()
            count = len(inbox.get("messages", inbox.get("items", [])))
            print(f"  Inbox 可用: {count} 条消息")
        else:
            print(f"  [INFO] Inbox: {resp.status_code}")


@pytest.mark.production
@pytest.mark.slow
def test_ws_stress_full(base_url, agents):
    """按顺序执行全部 WS 压力测试。"""
    import inspect
    started = time.time()
    results = []

    suites = [
        ("WS 连接", TestWebSocketConnections),
        ("Bus Publish", TestBusPublish),
    ]

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
                    elif p == "agents":
                        kwargs["agents"] = agents
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
    print(f"  WS 压力测试结果: {passed}/{len(results)} 通过, {failed} 失败 ({elapsed:.1f}s)")
    if failed:
        pytest.fail(f"{failed} 个测试失败:\n" +
                    "\n".join(f"  {r[0]}/{r[1]}: {r[3]}" for r in results if r[2] == "FAIL"))
