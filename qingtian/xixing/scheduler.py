"""
吸星 — 内置定时调度（替代 crontab）

仅在 management 角色服务器上激活。
使用 asyncio create_task + 简单循环，避免引入第三方调度库。
所有时间基于 config.xixing.timezone（默认 Asia/Shanghai，即北京时间 UTC+8）。

任务（北京时间）：
  - 采集→吸收→注入流水线：每天 00:30
  - 踩坑自动处理：每天 01:00
  - 质量门清理：每日 02:00（清理 90 天前未注入条目）
  - 经验蒸馏：每周一 03:00
  - 竞品扫描：每周日 06:00

触发机制：>= 目标时间 + last_run_at 记录，避免精确分钟匹配在服务重启后丢失任务。
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from common.config import is_management

logger = logging.getLogger("xixing.scheduler")

_tasks: list[asyncio.Task] = []
_running = False
_timezone: ZoneInfo | None = None

# 上次执行日期，防止同一天重复触发
_last_run: dict[str, str] = {}


def _track_task(task: asyncio.Task) -> None:
    """登记 fire-and-forget 任务：完成后自动从 _tasks 移除并消费异常。

    review(2026-08-16): 原实现只 append 不清理，长跑进程 _tasks 引用持续累积；
    且任务异常无人 await → 'Task exception was never retrieved'。done 回调兜底。
    """
    _tasks.append(task)

    def _done(t: asyncio.Task):
        try:
            if t in _tasks:
                _tasks.remove(t)
            t.exception()  # 消费异常，避免未检索告警
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.error("Scheduled task %r crashed", getattr(t, "get_name", lambda: "?")(),
                         exc_info=True)

    task.add_done_callback(_done)


def _now():
    """返回配置时区下的当前时间。"""
    global _timezone
    if _timezone is None:
        from . import config as cfg
        tz_name = cfg.get_timezone()
        try:
            _timezone = ZoneInfo(tz_name)
        except Exception:
            _timezone = ZoneInfo("Asia/Shanghai")
    return datetime.now(_timezone)


def _should_run(task: str, hour: int, minute: int, dow: int | None = None) -> bool:
    """检查任务是否应在当前分钟触发。

    规则：now >= 目标时间 且今天尚未执行 且 DOW 匹配（如指定）。
    即使服务在目标时刻之后重启，也能在下一个 scheduler tick 补执行。
    """
    now = _now()
    today = now.strftime("%Y-%m-%d")

    if _last_run.get(task) == today:
        return False

    if dow is not None and now.weekday() != dow:
        return False

    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= target


# ── 任务实现 ──────────────────────────────────────────


async def _collect_pipeline_job():
    """采集 → 吸收 → 注入永恒 全自动流水线。"""
    from .crawler import run_collect
    from .api import run_ingest, run_ingest_to_yongheng
    from common.db import get_pool

    logger.info("Scheduled: collect pipeline started")

    # Step 1: 采集
    try:
        collect_result = await run_collect()
        logger.info(
            f"Collect: {collect_result['sources_collected']}/{collect_result['sources_total']} success, "
            f"{collect_result['sources_failed']} failed"
        )
    except Exception as e:
        logger.error(f"Collect failed: {e}")
        return

    if collect_result["sources_collected"] == 0:
        logger.info("Pipeline ends: no successful collections")
        return

    # Step 2+3: 吸收 → 注入（共用同一个 conn）
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            ingest_result = await run_ingest(conn)
            logger.info(
                f"Ingest: {ingest_result['passed']} passed, "
                f"{ingest_result['rejected']} rejected, "
                f"{ingest_result['injected']} stored"
            )

            yh_result = await run_ingest_to_yongheng(conn)
            logger.info(
                f"Inject to yongheng: {len(yh_result['stored'])} stored, "
                f"{len(yh_result['failed'])} failed"
            )
    except Exception as e:
        logger.error(f"Ingest/inject pipeline failed: {e}")


async def _xizhenji_job():
    """踩坑自动处理：审计日志检测 + high/critical 注入永恒。"""
    from .xizhenji import detect_from_audit_log
    from yongheng.memory_service import write_memory
    from . import config as cfg
    from common.db import get_pool

    logger.info("Scheduled: xizhenji started")

    # Step 1: 从镇岳审计日志检测异常
    try:
        captured = await detect_from_audit_log(days=1)
        logger.info(f"Xizhenji: {captured} pitfalls captured from audit log")
    except Exception as e:
        logger.error(f"Xizhenji detect failed: {e}")
        return

    # Step 2: 将 unresolved high/critical 踩坑注入永恒
    try:
        pool = await get_pool()
        SCHEMA = cfg.get_schema_name()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT id, title, description, root_cause, solution, severity
                    FROM {SCHEMA}.xizhenji
                    WHERE resolved = FALSE AND severity IN ('high', 'critical')
                    AND injected_to_yongheng = FALSE
                    ORDER BY created_at DESC LIMIT 20"""
            )

            injected = 0
            for row in rows:
                content_parts = [f"[踩坑] {row['title']}"]
                if row["description"]:
                    content_parts.append(row["description"])
                if row["root_cause"]:
                    content_parts.append(f"根因: {row['root_cause']}")
                if row["solution"]:
                    content_parts.append(f"方案: {row['solution']}")

                try:
                    await write_memory(
                        conn,
                        namespace=cfg.get_global_namespace(),
                        content="\n".join(content_parts),
                        mem_type="experience",
                        source=f"xixing:xizhenji:{row['id']}",
                        metadata={
                            "pitfall_id": row["id"],
                            "severity": row["severity"],
                        },
                    )
                    await conn.execute(
                        f"UPDATE {SCHEMA}.xizhenji SET injected_to_yongheng = TRUE WHERE id = $1",
                        row["id"],
                    )
                    injected += 1
                except Exception as e:
                    logger.warning(f"Xizhenji inject failed for #{row['id']}: {e}")

            if injected:
                logger.info(f"Xizhenji: {injected} pitfalls injected to yongheng")
    except Exception as e:
        logger.error(f"Xizhenji inject failed: {e}")


async def _scan_job():
    from .scanner import run_scan, _analyze_gaps, _generate_proposals
    from yongheng.memory_service import write_memory
    from . import config as cfg
    from common.db import get_pool

    logger.info("Scheduled: scan started")

    # Step 1: 执行竞品扫描
    try:
        result = await run_scan()
        logger.info(f"Scheduled: scan done — {result['total_scanned']} skills, top {len(result['results'])}")
    except Exception as e:
        logger.error(f"Scheduled: scan failed: {e}")
        return

    # Step 2: 将 actionable 洞察注入永恒
    try:
        pool = await get_pool()
        SCHEMA = cfg.get_schema_name()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT id, skill_name, function_cluster, score, description, url
                    FROM {SCHEMA}.scan_results
                    WHERE actionable = TRUE AND injected_to_yongheng = FALSE
                    ORDER BY score DESC LIMIT 20"""
            )

            injected = 0
            for row in rows:
                content = (
                    f"[竞品洞察] {row['skill_name']}（{row['function_cluster']}）\n"
                    f"差异度: {row['score']:.0%}\n"
                    f"{row['description']}\n"
                    f"参考: {row['url']}"
                )
                try:
                    await write_memory(
                        conn,
                        namespace=cfg.get_global_namespace(),
                        content=content,
                        mem_type="knowledge",
                        source=f"xixing:scanner:{row['id']}",
                        metadata={
                            "scan_result_id": row["id"],
                            "skill_name": row["skill_name"],
                            "function_cluster": row["function_cluster"],
                            "differentiation_score": row["score"],
                        },
                    )
                    await conn.execute(
                        f"UPDATE {SCHEMA}.scan_results SET injected_to_yongheng = TRUE WHERE id = $1",
                        row["id"],
                    )
                    injected += 1
                except Exception as e:
                    logger.warning(f"Scanner inject failed for #{row['id']}: {e}")

            if injected:
                logger.info(f"Scanner: {injected} insights injected to yongheng")
    except Exception as e:
        logger.error(f"Scanner inject failed: {e}")

    # Step 3: 自评
    try:
        from .self_assess import self_assess
        self_scores = await self_assess()
        logger.info(f"Self-assessment: {len(self_scores)} dimensions scored, "
                    f"avg={sum(s['score'] for s in self_scores.values())/max(len(self_scores),1):.2f}")
    except Exception as e:
        logger.error(f"Self-assessment failed: {e}")
        return

    # Step 4: 差距分析
    try:
        gaps = await _analyze_gaps(self_scores, result["results"])
        if gaps:
            logger.info(f"Gap analysis: {len(gaps)} gaps found, top: "
                       f"{gaps[0]['dim_name']} (+{gaps[0]['gap']:.0%})")
        else:
            logger.info("Gap analysis: no significant gaps found")
    except Exception as e:
        logger.error(f"Gap analysis failed: {e}")
        return

    # Step 5: LLM 提案生成
    proposals = []
    if gaps:
        try:
            proposals = await _generate_proposals(gaps)
            logger.info(f"Proposals: {len(proposals)} generated")
        except Exception as e:
            logger.error(f"Proposal generation failed: {e}")

    # Step 6: 提案注入永恒
    if proposals:
        try:
            pool = await get_pool()
            xcfg_schema = cfg.get_schema_name()
            async with pool.acquire() as conn:
                # Dedup: check existing proposals for same dimension
                existing_dims = set()
                existing = await conn.fetch(
                    f"""SELECT DISTINCT metadata->>'dimension' as dim
                        FROM {xcfg_schema}.scan_results
                        WHERE action_taken = 'proposal_generated'"""
                )
                for row in existing:
                    if row["dim"]:
                        existing_dims.add(row["dim"])

                injected = 0
                for proposal in proposals:
                    dim = proposal.get("dimension", "")
                    if dim in existing_dims:
                        continue  # Already generated proposal for this dimension

                    content = (
                        f"[改进提案] {proposal.get('title', '未命名')}\n"
                        f"维度: {proposal.get('dimension', '')}\n"
                        f"当前: {proposal.get('current_state', '')}\n"
                        f"目标: {proposal.get('target_state', '')}\n"
                        f"实施步骤:\n" +
                        "\n".join(f"  {i+1}. {s}" for i, s in enumerate(proposal.get('implementation_steps', []))) +
                        f"\n预估投入: {proposal.get('estimated_effort', 'unknown')}\n"
                        f"预期收益: {proposal.get('expected_impact', '')}"
                    )
                    try:
                        await write_memory(
                            conn,
                            namespace=cfg.get_global_namespace(),
                            content=content,
                            mem_type="proposal",
                            source="xixing:scanner:proposal",
                            metadata={
                                "dimension": dim,
                                "gap": proposal.get("gap", 0),
                                "estimated_effort": proposal.get("estimated_effort", ""),
                                "references": proposal.get("references", []),
                            },
                        )
                        injected += 1
                    except Exception as e:
                        logger.warning(f"Proposal inject failed for {dim}: {e}")

                # Mark as done
                await conn.execute(
                    f"UPDATE {xcfg_schema}.scan_results SET action_taken = 'proposal_generated' "
                    f"WHERE action_taken = '' AND actionable = TRUE"
                )

                if injected:
                    logger.info(f"Proposals: {injected} injected to yongheng")
        except Exception as e:
            logger.error(f"Proposal injection failed: {e}")


async def _distill_job():
    from .distiller import run_distillation
    logger.info("Scheduled: distillation started")
    try:
        result = await run_distillation()
        logger.info(f"Scheduled: distillation done — {result['produced_count']} produced, status={result['status']}")
    except Exception as e:
        logger.error(f"Scheduled: distillation failed: {e}")


async def _daily_buffer_job():
    """拉取 bus buffer → 粗过滤 → 写入 Yongheng → 清空 buffer（Phase 1）"""
    from common.bus import bus
    from common.db import get_pool
    from . import config as cfg

    pool = await get_pool()
    agent_ids = bus.get_buffer_agents()

    if not agent_ids:
        logger.info("Daily buffer: no agents with buffered events")
        return

    total_injected = 0
    for agent_id in agent_ids:
        snapshot = bus.buffer_snapshot(agent_id)
        if not snapshot:
            continue

        filtered = _coarse_filter(snapshot)
        if not filtered:
            bus.buffer_clear(agent_id)
            continue

        injected = 0
        try:
            from yongheng.memory_service import write_memory
            from yongheng.config import get_schema_name as yh_schema

            async with pool.acquire() as conn:
                for event in filtered:
                    content = event.get("content", "").strip()
                    if len(content) < 5:
                        continue

                    # 幂等：同事件指纹已注入过则跳过（崩溃重启后 buffer 未清会重放）
                    # P2 (R11): 原用 seq_id 幂等，buffer_clear 重置计数导致跨轮 seq 复用误判，
                    # 改用事件唯一性指纹（_event_fingerprint）。
                    event_fp = _event_fingerprint(agent_id, event)
                    dup = await conn.fetchval(
                        f"SELECT 1 FROM {yh_schema()}.memories "
                        "WHERE namespace = $1 AND metadata->>'event_fp' = $2 LIMIT 1",
                        f"agent:{agent_id}", event_fp,
                    )
                    if dup:
                        continue

                    event_type = event.get("type", "lifecycle:unknown").replace("lifecycle:", "", 1)
                    mem_type = "trajectory" if event_type in ("llm_input", "llm_output", "tool_result") else "episodic"

                    await write_memory(
                        conn,
                        namespace=f"agent:{agent_id}",
                        content=content[:2000],
                        mem_type=mem_type,
                        source=f"bus:buffer:{event_type}",
                        metadata={
                            "agent_id": agent_id,
                            "event_type": event_type,
                            "session_id": event.get("session_id", ""),
                            "tool_name": event.get("tool_name", ""),
                            "seq_id": event.get("seq_id"),
                            "event_fp": event_fp,
                            "source": "bus_buffer",
                        },
                    )
                    injected += 1

                # 全部写入成功后立即清 buffer（尽量贴近写入，缩小重复窗口）
                bus.buffer_clear(agent_id)
            logger.info("Daily buffer: %s → %d events injected to Yongheng, buffer cleared", agent_id, injected)
            total_injected += injected

        except Exception as e:
            logger.error("Daily buffer: %s inject failed: %s", agent_id, e)
            continue

    logger.info("Daily buffer done: %d agents processed, %d events injected", len(agent_ids), total_injected)


def _event_fingerprint(agent_id: str, event: dict) -> str:
    """计算事件幂等指纹（agent + 稳定字段），用于注入去重。

    P2 (R11): 原幂等键用 buffer 的 seq_id，而 common.bus.buffer_clear 会重置
    _seq_counters，导致跨轮 seq 复用 → 新事件被误判为已注入而丢弃。
    改用事件唯一性（timestamp/type/content/session_id/tool_name）做键，
    与 seq 复用无关；崩溃重放时同一事件字典指纹相同，可正确跳过。
    """
    payload = json.dumps(
        {
            "ts": event.get("timestamp", ""),
            "type": event.get("type", ""),
            "content": event.get("content", ""),
            "session_id": event.get("session_id", ""),
            "tool_name": event.get("tool_name", ""),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(f"{agent_id}\x00{payload}".encode("utf-8")).hexdigest()


def _coarse_filter(events: list[dict]) -> list[dict]:
    """粗过滤：去噪、去重、短内容过滤"""
    seen = set()
    filtered = []
    for ev in events:
        dedup_key = (ev.get("content", ""), ev.get("session_id", ""), ev.get("type", ""))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        content = ev.get("content", "").strip()
        if len(content) < 5:
            continue

        filtered.append(ev)
    return filtered


async def _run_bus_distillation():
    """从 Yongheng 拉取昨日粗过滤数据 → LLM 精炼 → 分流（Phase 2）

    配置阈值：
      - min_events_for_experience: 同类事件 >= N 条才生成经验（默认 3）
      - max_experiences_per_round: 每轮最多产出经验数（默认 20）
    """
    from . import config as cfg
    from common.config import get as config_get
    from common.db import get_pool
    from common.bus import bus

    min_events = config_get("data_sources.bus_distillation.min_events_for_experience", 3)
    max_exp = config_get("data_sources.bus_distillation.max_experiences_per_round", 20)
    # 2026-08-27 全量切智谱：默认 glm-5.3-flash（原 deepseek-v4-pro 是 ds 侧模型名，
    # zhice base_url 已切智谱后发该模型名会直接报错）
    from common.config import default_llm_model
    model = config_get("data_sources.bus_distillation.llm_model", default_llm_model())

    pool = await get_pool()
    categories = await _get_agent_categories(pool)

    total_experiences = 0
    for category, agent_ids in categories.items():
        if not agent_ids:
            continue

        raw_memories = await _fetch_yesterday_memories(pool, agent_ids)
        if len(raw_memories) < min_events:
            logger.debug("Distillation: %s only %d events, skip", category, len(raw_memories))
            continue

        try:
            refined = await _llm_refine(raw_memories, category, model)
        except Exception as e:
            logger.error("Distillation: LLM refine failed for %s: %s", category, e)
            continue

        if not refined:
            continue

        for item in refined[:max_exp]:
            try:
                if item.get("type") == "personal" and item.get("agent_id"):
                    await _write_yongheng(pool, item)
                elif item.get("type") == "public":
                    await _write_huichuan(pool, item)
                elif item.get("type") == "skill_proposal":
                    await _write_skill_proposal(pool, item)
            except Exception as e:
                logger.warning("Distillation: write failed for %s: %s", item.get("type", "?"), e)

        total_experiences += len(refined)
        logger.info("Distillation: %s → %d experiences refined", category, len(refined))

        for agent_id in agent_ids:
            try:
                await bus.publish(agent_id, {
                    "type": "experience_ready",
                    "source": "xixing",
                    "payload": {"category": category, "count": len(refined)},
                })
            except Exception:
                pass

    logger.info("Distillation done: %d total experiences from %d categories", total_experiences, len(categories))


async def _get_agent_categories(pool) -> dict[str, list[str]]:
    """按 category 分组所有 active agent"""
    from huanyu.config import get_schema_name as hy_schema
    categories = {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT agent_id, category FROM {hy_schema()}.agents WHERE status = 'active'"
        )
    for row in rows:
        cat = row["category"] or "unknown"
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(row["agent_id"])
    return categories


async def _fetch_yesterday_memories(pool, agent_ids: list[str]) -> list[dict]:
    """从 Yongheng 拉取昨日粗过滤数据"""
    from yongheng.config import get_schema_name as yh_schema
    memories = []
    async with pool.acquire() as conn:
        for agent_id in agent_ids:
            try:
                rows = await conn.fetch(
                    f"""SELECT content, memory_type, metadata, timestamp
                       FROM {yh_schema()}.memories
                       WHERE namespace = $1
                         AND timestamp >= NOW() - INTERVAL '1 day'
                       ORDER BY timestamp""",
                    f"agent:{agent_id}",
                )
                for row in rows:
                    memories.append({
                        "agent_id": agent_id,
                        "content": row["content"],
                        "memory_type": row["memory_type"],
                        "metadata": row.get("metadata") or {},
                        "timestamp": row["timestamp"].isoformat() if row["timestamp"] else "",
                    })
            except Exception:
                continue
    return memories


async def _llm_refine(memories: list[dict], category: str, model: str) -> list[dict]:
    """LLM 精炼事件流 → 结构化经验"""
    summaries = []
    for m in memories[:50]:
        summaries.append(f"- [{m.get('memory_type', 'event')}] {m.get('content', '')[:200]}")

    prompt = (
        f"以下是一组 {category} 类别 Agent 的昨日操作记录。请分析这些记录，找出可复用的经验、常见模式和技能提案。\n\n"
        f"操作记录:\n" + "\n".join(summaries) + "\n\n"
        f"返回 JSON 数组，每项格式:\n"
        f'{{"type": "personal|public|skill_proposal", "agent_id": "仅 personal 时填写", '
        f'"title": "经验标题", "summary": "经验总结（50-200字）", '
        f'"patterns": ["模式1", "模式2"], "severity": "info|warning|critical", '
        f'"applicability": ["适用场景1"], "confidence": 0.0-1.0}}\n\n'
        f"规则:\n"
        f"- personal: 仅适用于单个 Agent 的个性化经验\n"
        f"- public: 通用性强的经验，适合入库 Huichuan 知识库\n"
        f"- skill_proposal: 高频可复用的能力模式，适合提案为 Skill\n"
        f"- confidence < 0.6 的不要返回（质量门槛）"
    )

    api_key = None
    base_url = None
    try:
        from zhice.config import get_llm_api_key, get_llm_base_url
        api_key = get_llm_api_key()
        base_url = get_llm_base_url()
    except Exception:
        pass

    if not api_key:
        logger.warning("Distillation: no LLM API key, skip LLM refine")
        return []

    try:
        import httpx
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    # 2026-08-27: glm思考强制开启计入max_tokens,2000→4096
                    "max_tokens": 4096,
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                if raw.startswith("json"):
                    raw = raw[4:].strip()
        import json as _json
        items = _json.loads(raw)
        if isinstance(items, dict):
            items = items.get("experiences", [])
        return [it for it in items if isinstance(it, dict) and it.get("confidence", 0) >= 0.6]
    except Exception as e:
        logger.error("Distillation LLM refine failed: %s", e)
        return []


async def _write_yongheng(pool, item: dict):
    """写入 Agent 专属 Yongheng namespace"""
    from yongheng.memory_service import write_memory
    async with pool.acquire() as conn:
        await write_memory(
            conn,
            namespace=f"agent:{item['agent_id']}",
            content=f"[经验] {item['title']}\n{item['summary']}",
            mem_type="experience",
            source="xixing:distillation",
            metadata={
                "patterns": item.get("patterns", []),
                "severity": item.get("severity", "info"),
                "confidence": item.get("confidence", 0),
                "source": "bus_distillation",
            },
        )


async def _write_huichuan(pool, item: dict):
    """写入 Huichuan 知识库（公共经验）"""
    from huichuan.config import get_schema_name as hc_schema
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                f"""INSERT INTO {hc_schema()}.knowledge_entries
                   (title, content, category, source, metadata, status)
                   VALUES ($1, $2, $3, $4, $5, 'active')""",
                item["title"],
                item["summary"],
                item.get("applicability", ["general"])[0],
                "xixing:distillation:public",
                json.dumps({
                    "patterns": item.get("patterns", []),
                    "severity": item.get("severity", "info"),
                    "applicability": item.get("applicability", []),
                    "source": "bus_distillation",
                }),
            )
        except Exception:
            pass


async def _write_skill_proposal(pool, item: dict):
    """写入 Skill 提案表"""
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """INSERT INTO skills.skill_definitions
                   (name, description, category, status, proposed_at, metadata)
                   VALUES ($1, $2, $3, 'proposed', NOW(), $4)
                   ON CONFLICT (name) DO NOTHING""",
                item["title"],
                item["summary"][:500],
                "distilled",
                json.dumps({
                    "patterns": item.get("patterns", []),
                    "applicability": item.get("applicability", []),
                    "source": "bus_distillation",
                    "confidence": item.get("confidence", 0),
                }),
            )
        except Exception:
            pass


async def _cleanup_job():
    """清理 90 天前未注入永恒的过期 knowledge_items。"""
    from common.db import get_pool
    from . import config as cfg

    SCHEMA = cfg.get_schema_name()
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"""DELETE FROM {SCHEMA}.knowledge_items
                WHERE injected_to_yongheng = FALSE
                AND created_at < NOW() - INTERVAL '90 days'""",
        )
        deleted = int(result.split()[-1]) if result else 0
        if deleted > 0:
            logger.info(f"Scheduled: cleanup removed {deleted} expired knowledge_items")


# ── 从属服务器：知识同步 ────────────────────────────


async def _pull_knowledge_job():
    """从属服务器：从管理服务器拉取知识 → 注入本地永恒。"""
    from . import config as cfg
    from yongheng.memory_service import write_memory
    from common.db import get_pool
    import httpx

    mgmt_url = cfg.get_sync_management_url().rstrip("/")
    export_url = f"{mgmt_url}/v1/xixing/knowledge/export?limit=100"

    logger.info(f"Subordinate sync: pulling from {mgmt_url}")

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(export_url)
            if resp.status_code != 200:
                logger.error(f"Subordinate sync: HTTP {resp.status_code} from {mgmt_url}")
                return
            data = resp.json()
    except Exception as e:
        logger.error(f"Subordinate sync: failed to reach {mgmt_url}: {e}")
        return

    items = data.get("items", [])
    if not items:
        logger.info("Subordinate sync: no new knowledge to pull")
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        stored = 0
        for item in items:
            try:
                await write_memory(
                    conn,
                    namespace=cfg.get_global_namespace(),
                    content=item["content"],
                    mem_type="knowledge",
                    source=f"xixing:sync:{item['source_id']}",
                    metadata={
                        "xixing_knowledge_id": item["xixing_knowledge_id"],
                        "xixing_category": item["category"],
                        "source_name": item.get("source_name", ""),
                        "source_url": item.get("source_url", ""),
                        "synced_from": mgmt_url,
                    },
                )
                stored += 1
            except Exception as e:
                logger.warning(f"Subordinate sync: write failed for #{item.get('xixing_knowledge_id')}: {e}")

    logger.info(f"Subordinate sync: {stored}/{len(items)} knowledge items pulled from {mgmt_url}")


# ── 从属服务器：能力差距检测 ──────────────────────────


async def _capability_sync_job():
    """从属服务器：从管理服务器拉取能力评分 → 对比本地 → 差距告警。"""
    from . import config as cfg
    from .self_assess import self_assess
    from yongheng.memory_service import write_memory
    from common.db import get_pool
    import httpx

    mgmt_url = cfg.get_sync_management_url().rstrip("/")
    cap_url = f"{mgmt_url}/v1/xixing/capabilities"

    logger.info(f"Capability sync: fetching from {mgmt_url}")

    # 1. 拉取管理端能力评分
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(cap_url)
            if resp.status_code != 200:
                logger.error(f"Capability sync: HTTP {resp.status_code} from {mgmt_url}")
                return
            data = resp.json()
    except Exception as e:
        logger.error(f"Capability sync: failed to reach {mgmt_url}: {e}")
        return

    mgmt_dims = data.get("dimensions", {})
    if not mgmt_dims:
        logger.info("Capability sync: management returned no dimensions")
        return

    # 2. 本地自评
    try:
        local_scores = await self_assess()
    except Exception as e:
        logger.error(f"Capability sync: local self_assess failed: {e}")
        return

    # 3. 逐维度对比（仅对比从属服务器关心的通用能力维度）
    gap_threshold = cfg.get_capability_gap_threshold()
    shared_dims = set(cfg.get_capability_shared_dimensions())
    drift_dims = []
    for dim_id, dim_info in mgmt_dims.items():
        if dim_id not in shared_dims:
            continue  # 管理专属能力，不触发从属告警
        mgmt_score = dim_info["score"]
        local_info = local_scores.get(dim_id, {})
        local_score = local_info.get("score", 0.0)
        gap = round(mgmt_score - local_score, 2)
        if gap > gap_threshold:
            drift_dims.append({
                "dimension": dim_id,
                "dim_name": dim_info["name"],
                "mgmt_score": mgmt_score,
                "local_score": local_score,
                "gap": gap,
            })

    if not drift_dims:
        logger.info("Capability sync: no drift detected, all dimensions aligned")
        return

    drift_dims.sort(key=lambda x: x["gap"], reverse=True)
    logger.info(f"Capability sync: {len(drift_dims)} drift(s) detected, "
                f"top: {drift_dims[0]['dim_name']} (+{drift_dims[0]['gap']:.0%})")

    # 4. 写入 yongheng（带去重）
    dedup_days = cfg.get_capability_dedup_days()
    from yongheng.config import get_schema_name as yh_schema
    pool = await get_pool()
    async with pool.acquire() as conn:
        injected = 0
        for d in drift_dims:
            # 去重：同维度 dedup_days 内不重复生成
            existing = await conn.fetchval(
                f"""SELECT 1 FROM {yh_schema()}.memories
                   WHERE memory_type = 'capability_drift'
                     AND metadata->>'dimension' = $1
                     AND timestamp > NOW() - make_interval(days => $2)
                   LIMIT 1""",
                d["dimension"],
                dedup_days,
            )
            if existing:
                logger.debug(f"Capability sync: skipping {d['dimension']} (recently reported)")
                continue

            content = (
                f"[能力差距告警] {d['dim_name']}\n"
                f"管理服务器: {d['mgmt_score']:.0%}\n"
                f"本地: {d['local_score']:.0%}\n"
                f"差距: +{d['gap']:.0%}\n"
                f"建议: 从管理服务器同步 {d['dim_name']} 相关代码/配置更新"
            )
            try:
                await write_memory(
                    conn,
                    namespace=cfg.get_global_namespace(),
                    content=content,
                    mem_type="capability_drift",
                    source=f"xixing:capability_sync:{d['dimension']}",
                    metadata={
                        "dimension": d["dimension"],
                        "dim_name": d["dim_name"],
                        "mgmt_score": d["mgmt_score"],
                        "local_score": d["local_score"],
                        "gap": d["gap"],
                        "mgmt_url": mgmt_url,
                    },
                )
                injected += 1
            except Exception as e:
                logger.warning(f"Capability sync: write failed for {d['dimension']}: {e}")

        if injected:
            logger.info(f"Capability sync: {injected} drift alert(s) injected to yongheng")


# ── 从属服务器：自动部署 ──────────────────────────────


async def _auto_deploy_job():
    """从属服务器：检测到能力差距后自动 git pull + 重启 + 健康检查。"""
    from . import config as cfg
    from . import deploy
    from yongheng.memory_service import write_memory
    from common.db import get_pool

    if not cfg.get_auto_deploy_enabled():
        return

    # 检查冷却期
    last_deploy = deploy.get_last_deploy_time()
    if last_deploy is not None:
        cooldown = cfg.get_deploy_cooldown_minutes()
        elapsed = (datetime.now(timezone.utc) - last_deploy).total_seconds() / 60
        if elapsed < cooldown:
            logger.debug(f"Auto-deploy: cooldown ({elapsed:.0f}m < {cooldown}m)")
            return

    # 检查连续失败上限
    max_fail = cfg.get_deploy_max_failures()
    if deploy.get_consecutive_failures() >= max_fail:
        logger.error(f"Auto-deploy: blocked ({deploy.get_consecutive_failures()} consecutive failures >= {max_fail})")
        return

    # 检查是否有最近的未处理 drift（最近 2 个同步周期内）
    dedup_days = cfg.get_capability_dedup_days()
    from yongheng.config import get_schema_name as yh_schema
    pool = await get_pool()
    async with pool.acquire() as conn:
        drift_count = await conn.fetchval(
            f"""SELECT COUNT(*) FROM {yh_schema()}.memories
               WHERE memory_type = 'capability_drift'
                 AND timestamp > NOW() - make_interval(days => $1)""",
            dedup_days,
        )
    if not drift_count:
        logger.debug("Auto-deploy: no recent drifts, skipping")
        return

    # 构建 URL
    mgmt_url = cfg.get_sync_management_url().rstrip("/")
    health_url = f"http://localhost:1996/v1/xixing/health"

    logger.info(f"Auto-deploy: triggered by {drift_count} drift(s) in {dedup_days}d window")

    # 执行部署
    result = await deploy.auto_deploy(
        mgmt_url=mgmt_url,
        restart_cmd=cfg.get_deploy_restart_command(),
        health_url=health_url,
        health_timeout=cfg.get_deploy_health_timeout_seconds(),
    )

    # 记录部署日志
    status = result["status"]
    log_content = (
        f"[自动部署] {status}\n"
        f"部署前: {result['pre_head'][:8] if result['pre_head'] else 'N/A'}\n"
        f"部署后: {result['post_head'][:8] if result['post_head'] else 'N/A'}\n"
        f"时间: {result['deployed_at']}\n"
        + (f"错误: {result['error']}\n" if result["error"] else "")
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await write_memory(
                conn,
                namespace=cfg.get_global_namespace(),
                content=log_content,
                mem_type="deploy_log",
                source=f"xixing:auto_deploy:{status}",
                metadata={
                    "status": status,
                    "pre_head": result["pre_head"],
                    "post_head": result["post_head"],
                    "error": result.get("error", ""),
                    "deployed_at": result["deployed_at"],
                    "consecutive_failures": deploy.get_consecutive_failures(),
                },
            )
        except Exception as e:
            logger.warning(f"Auto-deploy: log write failed: {e}")

    # 连续失败达上限时告警
    if deploy.get_consecutive_failures() >= max_fail:
        logger.critical(
            f"Auto-deploy: {deploy.get_consecutive_failures()} consecutive failures reached max ({max_fail}), "
            f"auto-deploy suspended until manual intervention"
        )


def _in_sync_window() -> bool:
    """检查当前时间是否在从属同步窗口内（默认凌晨 0-6 点）。"""
    from . import config as cfg
    now = _now()
    start = cfg.get_sync_time_window_start_hour()
    end = cfg.get_sync_time_window_end_hour()
    if start <= end:
        return start <= now.hour < end
    else:
        # 跨日窗口（如 22 点-6 点）
        return now.hour >= start or now.hour < end


async def _subordinate_scheduler_loop():
    """从属服务器调度循环：在时间窗口内拉取知识 → 能力差距检测 → 自动部署。

    默认仅在凌晨 0-6 点执行，避免影响白天正常工作。
    """
    global _running
    _running = True

    from . import config as cfg
    interval = max(cfg.get_sync_interval_minutes(), 10)  # 最少 10 分钟
    cap_sync = cfg.get_capability_sync_enabled()
    auto_deploy = cfg.get_auto_deploy_enabled()
    win_start = cfg.get_sync_time_window_start_hour()
    win_end = cfg.get_sync_time_window_end_hour()

    flags = []
    if cap_sync:
        flags.append("capability_sync")
    if auto_deploy:
        flags.append("auto_deploy")
    flag_str = " + ".join(flags) if flags else "knowledge only"

    logger.info(
        f"Subordinate scheduler: every {interval} min from {cfg.get_sync_management_url()} "
        f"({flag_str}), window={win_start:02d}:00-{win_end:02d}:00"
    )

    # 启动时如果在窗口内则立即执行一轮
    if _in_sync_window():
        await _pull_knowledge_job()
        if cap_sync:
            await _capability_sync_job()
        if auto_deploy:
            await _auto_deploy_job()
    else:
        logger.info(f"Subordinate scheduler: outside sync window ({win_start:02d}:00-{win_end:02d}:00), waiting...")

    while _running:
        await asyncio.sleep(interval * 60)
        if _running and _in_sync_window():
            await _pull_knowledge_job()
            if cap_sync:
                await _capability_sync_job()
            if auto_deploy:
                await _auto_deploy_job()


async def start():
    """启动调度器（角色感知）。

    - management: 全量任务（采集/吸收/注入/踩坑/蒸馏/扫描/清理）
    - procurement/sales: 仅知识同步（需 xixing.sync.enabled=true）
    """
    from . import config as cfg

    if not cfg.get_scheduler_enabled():
        logger.info("Scheduler disabled (config)")
        return

    global _running
    if _running:
        return

    if is_management():
        logger.info(
            f"Starting xixing scheduler (management, tz={_now().tzinfo}): "
            f"collect@00:30, xizhenji@01:00, cleanup@02:00, distill@Mon 03:00, scan@Sun 06:00"
        )
        task = asyncio.create_task(_mgmt_scheduler_loop())
    elif cfg.get_sync_enabled():
        logger.info(
            f"Starting xixing scheduler (subordinate): "
            f"pull knowledge every {cfg.get_sync_interval_minutes()} min from {cfg.get_sync_management_url()}"
        )
        task = asyncio.create_task(_subordinate_scheduler_loop())
    else:
        logger.info("Scheduler disabled (not management, sync not enabled)")
        return

    _tasks.append(task)


async def stop():
    """停止调度器。"""
    global _running
    _running = False
    for task in _tasks:
        task.cancel()
    _tasks.clear()
    logger.info("Scheduler stopped")


# ── Skill 维护任务 ──────────────────────────────────────


async def _skill_expiry_job():
    """将 proposed 超过 30 天未审核的自动标记为 rejected"""
    from common.db import get_pool
    pool = await get_pool()
    try:
        result = await pool.execute("""
            UPDATE skills.skill_definitions
            SET status = 'rejected',
                rejection_reason = '30天未审核自动过期',
                updated_at = NOW()
            WHERE status = 'proposed'
              AND proposed_at < NOW() - INTERVAL '30 days'
        """)
        import re
        match = re.search(r"UPDATE (\d+)", result)
        count = int(match.group(1)) if match else 0
        logger.info("Skill expiry: auto-rejected %d stale proposals", count)
    except Exception:
        logger.warning("Skill expiry job skipped (management server not deployed yet)")


async def _skill_evolve_job():
    """每天增量分析一次 Skill 提案，与周蒸馏解耦"""
    from .config import get_skill_proposals_enabled

    if not get_skill_proposals_enabled():
        return
    from .distiller import _generate_skill_proposals
    from common.db import get_pool

    pool = await get_pool()
    try:
        result = await _generate_skill_proposals(pool)
        if result:
            logger.info("Daily evolve: %d proposal(s) generated", len(result))
    except Exception as e:
        logger.error("Daily evolve failed: %s", e)


# ── 调度主循环 ────────────────────────────────────────

# Management 任务注册表: (task_name, hour, minute, dow_or_none, job_fn)
# 注意：所有 job_fn 必须在之前定义（_skill_expiry_job 等）
_MGMT_SCHEDULE: list[tuple[str, int, int, int | None, callable]] = [
    ("collect_pipeline", 0, 30, None, _collect_pipeline_job),
    ("xizhenji",        1,  0, None, _xizhenji_job),
    ("daily_buffer",    2,  0, None, _daily_buffer_job),          # ← new: 拉取 bus buffer → 粗过滤 → Yongheng
    ("cleanup",         2,  0, None, _cleanup_job),
    ("distill",         3,  0,    0, _distill_job),
    ("bus_distillation", 4, 0, None, _run_bus_distillation),      # ← new: Yongheng → LLM 精炼 → 分流
    ("scan",            6,  0,    6, _scan_job),
    ("skill_expiry",    2,  0, None, _skill_expiry_job),
    ("skill_evolve",    6,  0, None, _skill_evolve_job),
]


async def _mgmt_scheduler_loop():
    """Management 调度循环：按时间点触发。"""
    global _running
    _running = True

    while _running:
        try:
            for task_name, hour, minute, dow, job_fn in _MGMT_SCHEDULE:
                if _should_run(task_name, hour, minute, dow):
                    _last_run[task_name] = _now().strftime("%Y-%m-%d")
                    _track_task(asyncio.create_task(job_fn()))
        except Exception as e:
            logger.error("Scheduler loop error: %s", e)

        await asyncio.sleep(60)
