"""Skill 子进程入口点

子进程独立运行时执行的代码：
1. 解析启动参数（skill_name, agent_id, config JSON, version）
2. 加载 Skill 实现类
3. 实例化并调用 on_load
4. 通过 STDIO IPC 进入请求-响应循环
5. 收到 on_unload / on_data_purge 时触发对应钩子

命令行参数（JSON 字符串）:
    --skill-name    技能名称（必填）
    --agent-id      所属 Agent ID
    --config        底座配置 JSON
    --version       Skill 版本

注意：子进程用 STDERR 输出日志，STDOUT 专用于 IPC。
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from typing import Any

# ── 子进程一启动就配置日志（STDERR） ──

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("skill_runner")

# 进程启动时刻（monotonic），用于所有 elapsed 计算（定位外部信号时间点）
_PROC_START = time.monotonic()


def _install_signal_trace() -> None:
    """捕获外部信号（SIGINT/SIGTERM）打 trace，再委托原 handler——行为不变。

    背景：线上复现 "Skill runner cancelled + exit 0"，根因是子进程被外部信号终止
    （asyncio.run Runner 首个信号 → cancel 主任务 → 下方 except asyncio.CancelledError
    吞掉取消正常返回 → exit 0）。
    信号源不在 qingtian 进程树内（xihe/zhice 全排查过无内部取消路径），故在
    子进程侧安装信号捕获：记录信号类型、进程启动后经过秒数，并打印 asyncio 为
    该信号安装的 handler——借此判断生产 Python 3.12 Runner 是否也接管 SIGTERM
    （若接管，`systemctl restart qingtian`（systemd 默认 SIGTERM）即能解释 exit 0）。
    必须在 run_skill 协程内（事件循环已启动）调用，此时 asyncio.run 已装好 handler，
    我们覆盖后打 trace 再委托，行为不变、可观测性新增。
    """
    saved: dict[int, object] = {}

    def _install_one(signum: int, name: str) -> None:
        prev = signal.getsignal(signum)
        saved[signum] = prev
        logger.info(
            "[trace] skill_runner %s handler installed by asyncio=%s",
            name, prev if prev not in (signal.SIG_DFL, signal.SIG_IGN) else repr(prev),
        )

        def _handler(signum, frame, _name=name, _signum=signum):
            # 信号处理器内禁止走 logging（可能死锁日志锁），用 os.write 直接写 stderr
            elapsed = time.monotonic() - _PROC_START
            _msg = (
                f"[trace] skill_runner {_name} RECEIVED signum={_signum} "
                f"elapsed={elapsed:.1f}s pid={os.getpid()} "
                f"(外部信号源排查：重启/部署/systemctl/watchdog)\n"
            )
            try:
                os.write(2, _msg.encode("utf-8", "replace"))
            except Exception:
                pass
            target = saved.get(_signum)
            if target in (signal.SIG_DFL, signal.SIG_IGN, None):
                # asyncio 未接管 → 恢复默认并重发，保持原终止语义（可观测性不变行为）
                try:
                    signal.signal(_signum, signal.SIG_DFL)
                    os.kill(os.getpid(), _signum)
                except Exception:
                    raise KeyboardInterrupt(_name)
            else:
                # 委托 asyncio Runner 的 handler（SIGINT→cancel 主任务 → 212 行）
                target(_signum, frame)

        signal.signal(signum, _handler)

    try:
        _install_one(signal.SIGINT, "SIGINT")
        _install_one(signal.SIGTERM, "SIGTERM")
    except Exception as e:  # 平台差异（如 Windows SIGTERM 支持）降级：trace 缺失不影响子进程
        logger.warning("[trace] skill_runner signal trace install failed, degraded: %s", e)


def _apply_resource_limits(config: dict) -> None:
    """设置资源限制（POSIX），Windows 静默跳过"""
    memory_bytes = config.get("memory_limit_bytes", 512 * 1024 * 1024)
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        logger.info("RLIMIT_AS set to %d bytes", memory_bytes)
    except (ImportError, AttributeError, resource.error):
        pass  # Windows 或容器环境不支持


def _safe_segment(value: str) -> str:
    """路径段清洗（与 agent_runtime._safe_segment 同规）。

    P1 (2026-08-27 review #7): agent_id/skill_name 来自命令行参数（原始外部值），
    原实现直接 join → "../../" 可让 SKILL_HOME 逃逸数据根目录（父进程侧的
    _safe_segment 清洗被子进程重拼绕过——两处拼接只清洗了一处）。
    """
    if not value:
        return value
    parts = [p for p in value.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "_".join(parts)


def _setup_environment(config: dict, agent_id: str = "", skill_name: str = "") -> None:
    """子进程启动时设置环境限制，锁定工作目录

    Args:
        config: 底座配置
        agent_id: 所属 Agent ID（用于目录隔离）
        skill_name: Skill 名称（用于目录隔离）
    """
    data_root = config.get("skill_data_root", config.get("skill_data_dir", ""))
    if not data_root:
        return

    # 构建 Agent/Skill 层级隔离目录
    # P1 (2026-08-27 review #7): agent_id/skill_name 先过 _safe_segment 清洗
    # （与父进程同规），防 ../../ 逃逸数据根
    if agent_id and skill_name:
        skill_home = os.path.join(
            data_root, _safe_segment(agent_id), _safe_segment(skill_name),
        )
    else:
        skill_home = data_root

    os.makedirs(skill_home, exist_ok=True)
    os.makedirs(os.path.join(skill_home, "data"), exist_ok=True)
    os.makedirs(os.path.join(skill_home, "cache"), exist_ok=True)
    os.makedirs(os.path.join(skill_home, "tmp"), exist_ok=True)

    os.environ["SKILL_HOME"] = skill_home
    os.environ["SKILL_DATA"] = os.path.join(skill_home, "data")
    os.environ["SKILL_CACHE"] = os.path.join(skill_home, "cache")
    os.environ["SKILL_TMP"] = os.path.join(skill_home, "tmp")
    os.chdir(skill_home)
    logger.info("Environment locked to SKILL_HOME=%s", skill_home)


def _safe_path(path: str) -> str:
    """所有文件操作路径经过此函数，防止越权访问

    P1 阶段由 ctx.filesystem 调用。当前 P0 仅为基础设施预铺，
    子进程内无直接文件操作需要此检查。
    """
    skill_home = os.environ.get("SKILL_HOME", "")
    if not skill_home:
        return path
    real = os.path.realpath(os.path.join(skill_home, path))
    if not real.startswith(skill_home + os.sep) and real != skill_home:
        raise PermissionError(f"越权文件访问: {path}")
    return real


async def _connect_ipc_with_handshake(ipc) -> None:
    """P2 (R11): 建立与父进程的 IPC 连接并完成令牌握手。

    父进程 accept 后先发送随机 challenge，本进程须在 on_load 前以
    HMAC(challenge, QINGTIAN_IPC_AUTH_TOKEN) 应答，证明是本次启动的子进程
    （防同机其他进程冒充 Skill 抢占连接）。

    注：IPCClient.connect() 会同时启动 dispatch 消费消息，握手应答无法夹在中间，
    故此处手工连接 + 握手，通过后再注入 transport 并启动 dispatch（等价 connect()）。
    """
    from xihe.agent_runtime import _ipc_handshake_response

    port = int(os.environ.get("QINGTIAN_IPC_PORT", "0"))
    if not port:
        raise ConnectionError("QINGTIAN_IPC_PORT not set")
    token = os.environ.get("QINGTIAN_IPC_AUTH_TOKEN", "")
    if not token:
        raise ConnectionError("QINGTIAN_IPC_AUTH_TOKEN not set")

    reader = None
    writer = None
    for attempt in range(10):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            break
        except (ConnectionRefusedError, OSError):
            if attempt < 9:
                await asyncio.sleep(0.1)
    if reader is None or writer is None:
        raise ConnectionError(f"Failed to connect to parent on port {port}")

    try:
        # 读父进程 challenge
        line = await asyncio.wait_for(reader.readline(), timeout=30.0)
        if not line:
            raise ConnectionError("IPC handshake: connection closed by parent")
        try:
            msg = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ConnectionError(f"IPC handshake: invalid challenge: {e}")
        if not isinstance(msg, dict) or msg.get("type") != "ipc.handshake":
            raise ConnectionError("IPC handshake: unexpected challenge message")
        challenge = msg.get("challenge")
        if not isinstance(challenge, str) or not challenge:
            raise ConnectionError("IPC handshake: missing challenge")

        response = _ipc_handshake_response(token, challenge)
        reply = json.dumps(
            {"v": 1, "type": "ipc.handshake", "response": response},
            ensure_ascii=False, separators=(",", ":"),
        )
        writer.write((reply + "\n").encode("utf-8"))
        await writer.drain()
    except Exception:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        raise

    # 注入已连接 transport，并启动 dispatch（等价 IPCClient.connect()）
    ipc._transport._reader = reader
    ipc._transport._writer = writer
    ipc._recv_task = asyncio.create_task(ipc._dispatch(), name="ipc-dispatch")


async def run_skill(
    skill_name: str,
    agent_id: str,
    config: dict,
    version: str,
) -> None:
    """Skill 子进程主循环"""
    # 确保子进程可以找到 qingtian 模块
    # 69e460e 后商业 Skill 迁到仓库根 skills/：skill_runner 从 opensource/qingtian/xihe/
    # 运行时 _qingtian_root=opensource/qingtian（含 common/osskill），但 skills/ 在仓库根，
    # 需再上溯一层（同 main.py 4d9dc67 逻辑），否则子进程 `import skills.*` 报
    # `No module named 'osskill'` → 用户标书生成 503 无响应（大师 2026-08-10 实测）。
    # 幂等：仅当上溯后的父级含 skills/ 目录才采用；根目录运行时不改变既有行为。
    _runner_dir = os.path.dirname(os.path.abspath(__file__))
    _qingtian_root = os.path.dirname(_runner_dir)
    _repo_root = os.path.dirname(_qingtian_root)
    _parent = os.path.dirname(_repo_root)
    if os.path.isdir(os.path.join(_parent, "skills")):
        _repo_root = _parent
    for _p in (_qingtian_root, _repo_root):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    from common.ipc import IPCClient, Request, Response
    from osskill import Skill, SkillContext
    from osskill.loader import SkillLoader

    # 加载 Skill 类
    skill_cls = SkillLoader.load(skill_name)
    if skill_cls is None:
        logger.error("Skill '%s' not found", skill_name)
        # 通知父进程加载失败
        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": "startup_error",
            "error": {"code": -32002, "message": f"Skill '{skill_name}' not found"},
        })
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
        return

    # 实例化 Skill
    skill: Skill = skill_cls()

    # 构建上下文
    manifest = None
    permissions = []
    try:
        from osskill.loader import ManifestLoader
        # 69e460e 后商业 Skill 在仓库根 skills/{name}/skill.json，通用 Skill 仍在
        # osskill/implementations/{name}/skill.json → 两处都找（与 SkillLoader.load 一致）
        _candidates = (
            os.path.join(_repo_root, "skills", skill_name, "skill.json"),
            os.path.join(_qingtian_root, "osskill", "implementations", skill_name, "skill.json"),
        )
        manifest_path = next((p for p in _candidates if os.path.isfile(p)), "")
        if manifest_path:
            manifest = ManifestLoader.from_skill_json(manifest_path)
            if manifest:
                permissions = getattr(manifest, "permissions", []) or []
    except Exception:
        logger.warning("Could not load manifest for '%s', permissions default to empty", skill_name)

    # 建立 IPC 客户端
    ipc = IPCClient()
    # P2 (R11): 手工连接 + 握手（IPCClient.connect() 同时启动 dispatch，握手无法
    # 夹在中间），校验父进程令牌应答通过后再进入 on_load。
    await _connect_ipc_with_handshake(ipc)

    ctx = SkillContext(
        agent_id=agent_id,
        config=config,
        skill_name=skill_name,
        skill_version=version,
        permissions=permissions,
        _ipc_client=ipc,
    )

    # 调用 on_load
    try:
        await skill.on_load(ctx)
    except Exception as e:
        logger.exception("Skill.on_load failed: %s", e)
        error_msg = json.dumps({
            "jsonrpc": "2.0",
            "id": "startup_error",
            "error": {"code": -32002, "message": f"on_load failed: {e}"},
        })
        sys.stdout.write(error_msg + "\n")
        sys.stdout.flush()
        return

    # 通过 TCP 发送 ready_line（父进程在 TCP accept 后从 tcp_reader 读）
    ready_msg = json.dumps({
        "jsonrpc": "2.0",
        "id": "startup_ready",
        "result": {
            "skill_name": skill_name,
            "version": version,
            "name": getattr(skill, "name", ""),
            "display_name": getattr(skill, "display_name", ""),
        },
    })
    # 通过 IPC 自定义方法发送（不走标准请求-响应）
    ipc._transport._writer.write((ready_msg + "\n").encode("utf-8"))
    await ipc._transport._writer.drain()

    # 安装外部信号捕获（记录谁在什么时刻终止了子进程），必须在事件循环内调用
    _install_signal_trace()

    logger.info("Skill '%s' ready, entering IPC loop", skill_name)

    try:
        # IPC 请求-响应循环
        while True:
            msg = await ipc.recv()
            if not isinstance(msg, Request):
                continue

            result = None
            error = None

            try:
                if msg.is_notification():
                    # 通知 → 无响应
                    await _handle_method(skill, msg.method, msg.params)
                else:
                    result = await _handle_method(skill, msg.method, msg.params)
            except Exception as e:
                logger.exception("Method '%s' failed: %s", msg.method, e)
                error = {"code": -32002, "message": str(e)[:500]}

            if not msg.is_notification():
                response = Response(id=msg.id, result=result, error=error)
                await ipc._transport.send(response)
    except (EOFError, ConnectionError, BrokenPipeError):
        logger.info(
            "[trace] Parent connection closed elapsed=%.1fs",
            time.monotonic() - _PROC_START,
        )
    except asyncio.CancelledError as exc:
        # 关键观测：CancelledError 的 traceback 精确指向被取消时正在执行的 await
        # （即"哪个步骤/哪次 LLM 调用进行中被终止"），配合 _install_signal_trace 判定信号源
        _frames = []
        _tb = exc.__traceback__
        while _tb and len(_frames) < 5:
            _f = _tb.tb_frame
            _frames.append(f"{os.path.basename(_f.f_code.co_filename)}:{_tb.tb_lineno} {_f.f_code.co_name}")
            _tb = _tb.tb_next
        logger.warning(
            "[trace] Skill runner cancelled elapsed=%.1fs interrupted_at=%s",
            time.monotonic() - _PROC_START,
            " -> ".join(_frames) if _frames else "(no frames)",
        )
    finally:
        # 清理
        try:
            await skill.on_unload()
        except Exception:
            logger.exception("on_unload failed")
        await ipc.close()


async def _handle_method(skill, method: str, params: Any) -> Any:
    """路由 IPC 方法调用到 Skill 实例"""
    if method == "execute":
        return await skill.execute(params or {})
    elif method == "validate":
        return await skill.validate(params or {})
    elif method == "on_unload":
        await skill.on_unload()
        return None
    elif method == "on_data_purge":
        await skill.on_data_purge()
        return None
    elif method == "on_upgrade":
        await skill.on_upgrade(
            from_version=(params or {}).get("from_version", ""),
            to_version=(params or {}).get("to_version", ""),
        )
        return None
    elif method == "ping":
        return "pong"
    elif method == "get_metadata":
        return {
            "name": getattr(skill, "name", ""),
            "display_name": getattr(skill, "display_name", ""),
            "description": getattr(skill, "description", ""),
            "version": getattr(skill, "version", ""),
            "category": getattr(skill, "category", ""),
        }
    else:
        raise ValueError(f"Unknown method: {method}")


def _parse_args() -> dict:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="Skill Runner (child process)")
    parser.add_argument("--skill-name", required=True, help="Skill 名称")
    parser.add_argument("--agent-id", default="", help="所属 Agent ID")
    parser.add_argument("--config", default="{}", help="底座配置 JSON")
    parser.add_argument("--version", default="1.0.0", help="Skill 版本")
    parser.add_argument("--ipc-port", type=int, default=0, help="IPC TCP 端口")
    args = parser.parse_args()

    config = {}
    try:
        config = json.loads(args.config)
    except json.JSONDecodeError:
        logger.warning("Invalid config JSON, using empty config")

    return {
        "skill_name": args.skill_name,
        "agent_id": args.agent_id,
        "config": config,
        "version": args.version,
        "ipc_port": args.ipc_port,
    }


def main():
    """子进程入口"""
    logger.info("[trace] skill_runner start pid=%d", os.getpid())
    kwargs = _parse_args()
    _apply_resource_limits(kwargs.get("config", {}))
    _setup_environment(
        kwargs.get("config", {}),
        agent_id=kwargs.get("agent_id", ""),
        skill_name=kwargs.get("skill_name", ""),
    )

    # 注册存储配额
    try:
        from osskill.storage_quota import quota_from_skill_json, get_storage_quota
        skill_dir = os.environ.get("SKILL_HOME", "")
        if skill_dir:
            quota_bytes, level = quota_from_skill_json(skill_dir)
            if quota_bytes > 0:
                quota = get_storage_quota()
                quota.register(skill_dir, quota_bytes, level)
                logger.info("Storage quota registered: %d bytes (level=%s)", quota_bytes, level)
    except Exception:
        logger.warning("Failed to register storage quota, writes will use disk-usage fallback")

    # 从 --ipc-port 传递 TCP 端口（比 env 变量更可靠）
    ipc_port = kwargs.pop("ipc_port", 0)
    if ipc_port:
        os.environ["QINGTIAN_IPC_PORT"] = str(ipc_port)

    try:
        asyncio.run(run_skill(**kwargs))
    except KeyboardInterrupt:
        logger.warning("[trace] skill_runner KeyboardInterrupt elapsed=%.1fs", time.monotonic() - _PROC_START)
    finally:
        logger.info("[trace] skill_runner exit elapsed=%.1fs", time.monotonic() - _PROC_START)


if __name__ == "__main__":
    main()
