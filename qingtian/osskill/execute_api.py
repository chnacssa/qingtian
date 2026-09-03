"""
Skill 执行 API — execute + probe HTTP 端点

供 Gateway / OpenClaw pre_llm_hook 调用，实现秘书人设统一 + 能接则接不能则放行。

路由前缀: /api/v1/skills
注入方式: 由 main.py 将 XiheRuntime 注入到函数属性上（与 osskill/api.py 一致）
"""

import asyncio
import json
import logging
import os
import re
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from common.db import get_pool
from osskill.command_resolver import extract_command as resolve_command
from .database import SCHEMA, list_active_skill_routes

logger = logging.getLogger("osskill.execute_api")

router = APIRouter(prefix="/api/v1/skills", tags=["技能执行"])


# ── 执行重试策略（P2 R11）──────────────────────────────

_EXECUTE_MAX_ATTEMPTS = 3
"""execute 最大尝试次数（含首次）。"""

_EXECUTE_BACKOFF_BASE_S = 1.0
"""execute 重试退避基数（秒），按 1s/2s/4s 指数退避。"""


def _is_retryable_exec_error(exc: Exception) -> bool:
    """判定 execute 异常是否可重试（仅传输级瞬态错误）。

    - 可重试：IPC 连接/超时/句柄关闭/传输中断（xihe ProcessError、ConnectionError、
      OSError、asyncio.TimeoutError、EOFError）。
    - 不可重试：业务/校验错误。skill.execute 抛出的任何异常在子进程侧都会被
      包装成 code -32002 响应、父进程侧抛 common.ipc.IPCError——这类错误重试
      会重放非幂等副作用，必须立即透出。
    """
    from common.ipc import IPCError
    if isinstance(exc, IPCError):
        return False
    from xihe.errors import ProcessError
    return isinstance(exc, (ProcessError, ConnectionError, OSError,
                            asyncio.TimeoutError, EOFError))


# ── Pydantic 模型 ──────────────────────────────────


class ExecuteRequest(BaseModel):
    agent_id: str = ""
    params: dict = {}
    # 支持 Gateway 透传的额外字段（自动合并到 params）
    action: str = ""
    user_id: str = ""
    chat_type: str = ""
    message_id: str = ""


class ProbeRequest(BaseModel):
    action: str
    agent_id: str = ""


# ── 已知 action 列表（用于本地 probe 匹配） ──────────

_KNOWN_ACTIONS = frozenset({
    "query", "brief", "confirm", "record",
    "meeting", "meeting:summary", "meeting:transcribe",
    "translate", "doc:analyze", "doc:generate",
    "dnd:set", "dnd:list", "dnd:clear",
    "handover", "handover:do", "handover:recover",
    "memory:recover",
})

# bidding skill 合法 action 白名单（对应 bidding.py execute 分流的全部 action）
# 语义路由/网关/OpenClaw 传非白名单 action（如 LLM 幻觉 write_bid_document）时，归一化回关键词判断
_BIDDING_VALID_ACTIONS = frozenset({
    # 评分
    "score_bid", "evaluate_bid", "score", "打分", "评分",
    # 生成
    "generate_bid", "write_bid", "create_bid", "create_bid_document",
    "generate", "生成", "生成标书", "写标书",
    # 修订
    "revise_bid", "修订标书", "修改标书",
    "finalize_feedback", "确认满意", "满意",
    # 任务/素材/档案/看板
    "get_task", "list_tasks", "create_asset", "list_assets",
    "update_asset", "delete_asset", "list_records", "submit_feedback",
    "submit_experience", "score_history", "dashboard", "anomaly_scan",
    "experiences", "reminders", "health", "batch_import",
    "search_files", "download_file",
})

# 纯校验器 action（validate_* 系列）：返回 {ok, errors} 校验结果，不是对话入口，
# 无自然语言回复。LLM 语义路由出这类 action（如把补条款续答解析成 validate_rfq）必然
# skillExecute 拿不到回复 → 用户收不到消息（2026-08-13 小智实测）。弃用 → 落关键词兜底。
_VALIDATE_ACTIONS = frozenset({
    "validate_rfq", "validate_po", "validate_contract",
    "validate_negotiation", "validate_evaluation",
})

# 中文关键词 → 已知 action 映射
_CN_KEYWORDS = {
    "记一下": "record",
    "记录": "record",
    "帮我记": "record",
    "查一下": "query",
    "帮我查": "query",
    "搜索": "query",
    "回答": "query",
    "日报": "brief",
    "简报": "brief",
    "早报": "brief",
    "汇报": "brief",
    "确认": "confirm",
    "会议": "meeting",
    "纪要": "meeting:summary",
    "翻译": "translate",
    "文档": "doc:analyze",
    "分析": "doc:analyze",
    "生成": "doc:generate",
    "免打扰": "dnd:set",
    "静音": "dnd:set",
    "交接": "handover",
    "休假": "handover:do",
    "找回记忆": "memory:recover",
    "恢复记忆": "memory:recover",
    "失忆": "memory:recover",
    "恢复": "handover:recover",
}


def _match_action(action: str) -> str | None:
    """本地匹配 action 文本到已知 action。

    优先级:
      1. 精确匹配已知 action
      2. 前缀匹配（例如 "query 今天天气" → "query"）
      3. 中文关键词匹配
    """
    text = action.strip().lower()

    # 精确匹配
    if text in _KNOWN_ACTIONS:
        return text

    # 前缀匹配（action 文本以已知 action 开头）
    for known in sorted(_KNOWN_ACTIONS, key=len, reverse=True):
        if text.startswith(known + " ") or text.startswith(known + "："):
            return known

    # 中文关键词匹配
    for kw, mapped in _CN_KEYWORDS.items():
        if kw in action:
            return mapped

    return None


# ── 外部 Skill 路由缓存（供 probe 跨 Skill 匹配） ─────

_SKILL_ROUTE_CACHE: list[dict] | None = None
_SKILL_ROUTE_CACHE_TS: float = 0
_SKILL_ROUTE_CACHE_TTL = 60  # 秒


async def _load_skill_routes() -> list[dict]:
    """加载所有活跃 Skill 的路由数据（含缓存）。"""
    global _SKILL_ROUTE_CACHE, _SKILL_ROUTE_CACHE_TS
    now = time.monotonic()
    if _SKILL_ROUTE_CACHE is not None and now - _SKILL_ROUTE_CACHE_TS < _SKILL_ROUTE_CACHE_TTL:
        return _SKILL_ROUTE_CACHE
    try:
        skills = await list_active_skill_routes()
        _SKILL_ROUTE_CACHE = skills
        _SKILL_ROUTE_CACHE_TS = now
    except Exception as e:
        logger.warning("load_skill_routes failed: %s", e)
        _SKILL_ROUTE_CACHE = _SKILL_ROUTE_CACHE or []
    return _SKILL_ROUTE_CACHE


# 通用的 action 词根 → 中文映射（用于跨 Skill 匹配）
_ACTION_CN_ROOTS = {
    "import": "导入", "export": "导出", "create": "创建", "add": "添加",
    "update": "更新", "delete": "删除", "remove": "移除", "list": "列表",
    "get": "查看", "query": "查询", "search": "搜索", "train": "训练",
    "negotiation": "谈判", "inquiry": "询价", "quote": "报价",
    "approve": "审批", "cancel": "取消", "close": "关闭",
    "generate": "生成", "analyze": "分析", "recommend": "推荐",
    "bid": "投标", "contract": "合同", "purchase": "采购",
    "pricing": "定价", "customer": "客户",
}


def _match_external_skill(text: str) -> dict | None:
    """跨 Skill 匹配：检查消息文本是否匹配到其他活跃 Skill。

    Returns:
        {"matched_by": ..., "matched_skill": ..., "matched_action": ..., "confidence": float}
        或 None
    """
    cache = _SKILL_ROUTE_CACHE
    if not cache:
        return None

    text_lower = text.lower()
    candidates: list[dict] = []

    for skill in cache:
        name = skill.get("name", "")
        display_name = skill.get("display_name", "")
        desc = skill.get("description", "")
        tags = skill.get("tags") or []
        actions = skill.get("actions") or []

        # 1. tags 匹配（最高置信度）
        for tag in tags:
            tag_lower = tag.lower().strip()
            if len(tag_lower) > 1 and tag_lower in text_lower:
                candidates.append({
                    "matched_by": "tag",
                    "matched_skill": name,
                    "keyword": tag,
                    "confidence": 0.8,
                })
                break
        else:
            # 2. display_name 匹配（如 "销售" in "帮我销售导入"）
            short_name = ""
            for suffix in ["智能体", "助手", "系统"]:
                if suffix in display_name:
                    short_name = display_name.split(suffix)[0].strip().lower()
                    break
            if short_name and short_name in text_lower:
                candidates.append({
                    "matched_by": "display_name",
                    "matched_skill": name,
                    "keyword": short_name,
                    "confidence": 0.7,
                })
                continue

            # 3. action 中文词根匹配（如 "导入" in "导入文件"）
            # 高频业务关键词加权："询价/采购/投标" > "报价/合同/客户"
            _KEYWORD_BOOST = {"询价": 0.25, "采购": 0.2, "投标": 0.2, "比价": 0.2, "谈判": 0.15}
            for action in actions:
                parts = action.replace("-", "_").split("_")
                for p in parts:
                    cn = _ACTION_CN_ROOTS.get(p)
                    if cn and cn in text:
                        base_conf = 0.6
                        boost = _KEYWORD_BOOST.get(cn, 0)
                        candidates.append({
                            "matched_by": "action",
                            "matched_skill": name,
                            "keyword": cn,
                            "confidence": min(base_conf + boost, 0.95),
                            "matched_action": action,
                        })
                        break
                else:
                    continue
                break
            else:
                # 4. description 关键词匹配（低置信度）
                desc_lower = desc.lower()
                kw_candidates = [s.strip() for s in desc_lower.replace("—", ",").replace("，", ",").split(",") if 1 < len(s.strip()) < 10]
                for kw in kw_candidates:
                    if kw in text_lower:
                        candidates.append({
                            "matched_by": "description",
                            "matched_skill": name,
                            "keyword": kw,
                            "confidence": 0.4,
                        })
                        break

    if not candidates:
        return None

    # 按置信度降序
    candidates.sort(key=lambda x: -x["confidence"])
    best = candidates[0]

    # 如果最佳匹配置信度太低就不拦截
    if best["confidence"] < 0.5:
        return None

    # 标记多候选模糊
    if len(candidates) > 1 and candidates[1]["confidence"] >= 0.5 and (best["confidence"] - candidates[1]["confidence"]) < 0.2:
        best["ambiguous"] = True
        best["candidates"] = [{"matched_skill": c["matched_skill"], "confidence": c["confidence"]} for c in candidates[:3]]

    return best


# ── LLM 语义路由（自然语言 fallback）──────────────────


async def _llm_semantic_probe(text: str) -> dict | None:
    """LLM 语义分析：意图分类 + 参数提取。

    当代码匹配（!!command!!/关键词/词根）全部失败时，
    用轻量 LLM 分析用户自然语言，匹配到最合适的 Skill + Action，
    同时提取结构化参数。

    Returns:
        {"matched_skill": ..., "matched_action": ..., "confidence": float, "params": dict}
        或 None（LLM 也无法确定时放行给执策）
    """
    try:
        from common.llm import llm_call_json
    except ImportError:
        return None

    # 构建可用 Skill 列表（优先 DB，DB 空时回退到本地 skill.json）
    await _load_skill_routes()
    cache = _SKILL_ROUTE_CACHE

    skills_desc = []
    if cache:
        for skill in cache:
            name = skill.get("name", "")
            display_name = skill.get("display_name", "")
            desc = skill.get("description", "")
            actions = skill.get("actions") or []
            tags = skill.get("tags") or []
            if name in ("work_secretary", "clawhub_adapter", "regulatory_adapter"):
                continue
            skills_desc.append({
                "skill": name,
                "display": display_name,
                "desc": desc,
                "actions": actions,
                # B 方案（2026-08-28）：tags 进 LLM 消歧——P0.8 确定性层 ambiguous 放行后，
                # LLM 兜底时也应看见运营方声明的路由意图（原 skills_short 丢弃 tags）
                "tags": tags,
            })

    # DB 无数据 → 从本地 skill.json 文件构建 Skill 列表
    if not skills_desc:
        impl_dir = os.path.join(os.path.dirname(__file__), "implementations")
        if os.path.isdir(impl_dir):
            for entry in sorted(os.listdir(impl_dir)):
                entry_path = os.path.join(impl_dir, entry)
                if not os.path.isdir(entry_path):
                    continue
                sj = os.path.join(entry_path, "skill.json")
                if not os.path.isfile(sj):
                    continue
                try:
                    with open(sj, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    name = data.get("name", entry)
                    if name in ("work_secretary", "clawhub_adapter", "regulatory_adapter"):
                        continue
                    skills_desc.append({
                        "skill": name,
                        "display": data.get("display_name", name),
                        "desc": data.get("description", "")[:100],
                        "actions": [c.get("action", "") for c in data.get("commands", [])],
                        "tags": data.get("tags") or [],
                    })
                except Exception:
                    continue

    if not skills_desc:
        return None

    system_prompt = (
        "你是擎天系统的语义路由器。根据用户输入，判断用户意图应该路由到哪个 Skill 的哪个 Action，"
        "并提取结构化参数。\n\n"
        "核心区分：\n"
        "- procurement = 采购方发起询价/比价/采购，action: inquiry_create；用户直接下单（要买某产品、带数量/价格）→ action: po_complete\n"
        "- sales = 销售方被动接收报价请求后回应报价，action: query_quote/validate_quote；销售用户回复『接单/拒单』（含单价底线/账期/物流/支付约束）→ action: order_decide；设置自主成交委托开关 → action: set_auto_delegate\n"
        "- bidding = 标书处理，action 需区分：\n"
        "  * generate_bid = 写/生成/制作投标文件、投标响应、标书撰写（用户要『生成投标文件/帮我写投标/制作标书/响应招标文件』）\n"
        "  * revise_bid = 按用户意见修订已生成标书的指定章节并重新交付（用户『修标书/修改标书/修订标书/某章节补充XX』，常带标书编号）\n"
        "  * evaluate_bid = 投标评分/标书分析/评审打分（用户『评标/打分/评分/分析这份标书』）\n\n"
        "路由规则（按优先级）：\n"
        "1. 用户查某产品（含型号）当前市场价/现价/多少钱 → sales:query_quote\n"
        "2. 用户直接下单/要买某产品（下订单/下单/我要买/现在要买，含数量/价格）→ procurement:po_complete\n"
        "3. 用户说'我要采购/询价/比价/招标'（无价格查询、非直接下单意图）→ procurement:inquiry_create\n"
        "4. 用户说'我有个报价/报价是多少/客户要报价'（销售侧报价）→ sales:query_quote\n"
        "5. 投标文件生成类（写/生成/制作投标文件、标书撰写、响应招标文件、起草标书）→ bidding:generate_bid\n"
        "6. 标书修订类（修标书/修改标书/修订标书/改标书/修订某章节/给标书某章补充内容，带或提到标书编号）→ bidding:revise_bid\n"
        "7. 标书评分类（评标/打分/评分/分析投标文件/评审标书）→ bidding:evaluate_bid\n"
        "8. 文档生成(excel/word/pdf/ppt) → 对应 generator Skill\n"
        "9. 会议纪要/翻译/简报 → work_secretary Skill\n"
        "10. 销售用户回复『接单/拒单/同意接单』，可能带约束（单价不能低于X、账期Y天、物流谁承担、支付方式）→ sales:order_decide\n"
        "11. 销售用户设置自主成交委托（开启/关闭/设置自主成交）→ sales:set_auto_delegate\n"
        "12. 买卖用户回复履约回访（履约情况/已交付/延期/质量异常/售后/回访反馈）→ 本端采购用 procurement:fulfillment_reply，销售用 sales:fulfillment_reply\n\n"
        "参数提取（procurement=inquiry_create 时）：\n"
        "- product: 产品名称和规格（如'YJV22 4×95 电缆'）\n"
        "- spec: 规格参数（如'4×95'）\n"
        "- quantity: 数量\n"
        "参数提取（bidding=revise_bid 时）：\n"
        "- record_id: 标书编号（数字，如 ID：123 / 标书编号 456）\n"
        "- user_feedback: 用户修订意见原文（第几章节、补充/修改什么内容）\n\n"
        "只返回 JSON，不要任何解释文字。\n"
        "如果无法确定，返回 {\"passthrough\": true}"
    )

    # 精简 prompt：skills_desc 太长容易让 LLM 输出超出 max_tokens
    skills_short = []
    for s in skills_desc:
        skills_short.append({
            "skill": s["skill"],
            "display": s["display"],
            "actions": s["actions"][:3],  # 只取前 3 个 action
            "tags": (s.get("tags") or [])[:8],  # B 方案：tags 进 LLM 消歧上下文
        })

    prompt = (
        f"Skills: {json.dumps(skills_short, ensure_ascii=False)}\n"
        f"User: {text}\n"
        f"Return JSON only, no markdown, no explanation:"
    )

    result = await llm_call_json(
        prompt=prompt,
        caller="osskill.semantic_probe",
        system_prompt=system_prompt,
        timeout=60,
        max_tokens=2500,
        temperature=0,
    )

    # 一次失败 → 用更短的 prompt 重试
    if not result:
        retry_prompt = (
            f"User: {text}\n"
            f"Match to one of: {', '.join(s['display'] + '(' + s['skill'] + ')' for s in skills_short)}\n"
            f'Return: {{"skill":"...", "action":"...", "params":{{}}}}\n'
            f"JSON only, no markdown:"
        )
        result = await llm_call_json(
            prompt=retry_prompt,
            caller="osskill.semantic_probe_retry",
            system_prompt="Return only JSON. No markdown, no explanation.",
            timeout=60,
            max_tokens=2500,
            temperature=0,
        )

    # 二次失败 → 第三重试（长超时 30s，兜重启冷启动阶段 LLM 未就绪）
    if not result:
        logger.warning("semantic_probe LLM 两次失败(主+短重试)，尝试第三重试(长超时)")
        result = await llm_call_json(
            prompt=retry_prompt,
            caller="osskill.semantic_probe_retry2",
            system_prompt="Return only JSON. No markdown, no explanation.",
            timeout=60,
            max_tokens=2500,
            temperature=0,
        )

    if not result or result.get("passthrough"):
        if not result:
            logger.warning("semantic_probe LLM 三次全失败，降级放行执策")
        return None

    matched_skill = result.get("skill") or result.get("matched_skill", "")
    matched_action = result.get("action") or result.get("matched_action", "")
    if not matched_skill:
        return None

    return {
        "matched_by": "llm_semantic",
        "matched_skill": matched_skill,
        "matched_action": matched_action,
        "confidence": float(result.get("confidence", 0.7)),
        "params": result.get("params", {}),
    }


# ── Execute 端点 ──────────────────────────────────

# ── 2026-08-24 幂等兜底：同一 message_id 只执行一次 ──
# 线上事故：网关 before_dispatch 与 message_received 双路径对同一条用户消息各调一次
# execute → 标书生成两份（小智 12:30 实锤）。网关侧已修（签名提前写入），此处为最后一道
# 兜底：同 key 已完成 → 返回缓存结果；处理中 → 忽略重复请求。失败路径清在途标记可重试。
# 同日二次兜底（小智 14:06 复测：bus skillExecute 请求体不带 message_id → key 空直跳过）：
# message_id 缺失时降级用 params 内原始文本（_raw_text/query，去空白 md5）当 key，短窗
# 口（120s）防双路径 ~7s 级重复；用户隔几分钟有意重发同文本不受影响。
_IDEM_INFLIGHT: dict[str, float] = {}
_IDEM_RESULTS: dict[str, tuple[float, object]] = {}
_IDEM_TTL = 600.0
_IDEM_FALLBACK_TTL = 120.0
# 2026-08-29 变压器标书实锤（任务 202→203）：execute 失败（LLM 预算爆）后调用方
# **同秒重发同一消息**——失败路径只清在途标记不记失败 → 幂等层视为新请求 → 新
# bid_records 又起一单（又遇 glm 500）。修：同 key 失败后冷却窗内再来直接 429，
# 不再起新执行（调用方风暴重试被确定性挡住；用户稍后有意重试不受影响——新消息
# = 新 message_id/新文本 = 新 key）。
_IDEM_FAILURES: dict[str, tuple[float, str]] = {}
_IDEM_FAIL_COOLDOWN_S = 120.0


def _idem_fallback_key(skill_name: str, agent_id: str, user_id: str, params: dict) -> str:
    """message_id 缺失时的文本幂等 key：skill|用户身份|md5(归一化原始文本)。

    2026-08-26：主身份由 agent_id 改为 user_id（agent 兜底）——agent_id 是共享
    岗位身份（procurement-feishu）时不同用户发同文本会互相吃缓存（A 的下单结果
    回给 B）；同一用户经不同闸（before_dispatch 主闸 / message_received 漏网
    二次执行）携带不同 agent_id 时 key 也不同，双跑防护失效（线上实锤：同秒双
    execute 各落一张草稿）。统一按用户身份判重，双闸同用户同文本只跑一次。
    """
    p = params if isinstance(params, dict) else {}
    txt = ""
    if isinstance(p.get("payload"), dict):
        txt = str(p["payload"].get("_raw_text") or p["payload"].get("query") or "")
    txt = txt or str(p.get("_raw_text") or p.get("query") or "")
    txt = re.sub(r"\s+", "", txt)
    if len(txt) < 2:
        return ""
    import hashlib
    h = hashlib.md5(txt.encode("utf-8")).hexdigest()[:16]
    return f"{skill_name}|u:{user_id or agent_id}|txt:{h}"


def _idem_cleanup() -> None:
    now = time.time()
    if len(_IDEM_INFLIGHT) > 500:
        for k, ts in list(_IDEM_INFLIGHT.items()):
            if now - ts > _IDEM_TTL:
                _IDEM_INFLIGHT.pop(k, None)
    if len(_IDEM_RESULTS) > 200:
        for k, (ts, _) in list(_IDEM_RESULTS.items()):
            if now - ts > _IDEM_TTL:
                _IDEM_RESULTS.pop(k, None)
    if _IDEM_FAILURES:
        for k, (ts, _) in list(_IDEM_FAILURES.items()):
            if now - ts > _IDEM_FAIL_COOLDOWN_S:
                _IDEM_FAILURES.pop(k, None)


@router.post("/{skill_name}/execute")
async def api_execute_skill(skill_name: str, body: ExecuteRequest, request: Request):
    """执行 Skill（通过 XiheRuntime 子进程 IPC）。

    如果 skill 未运行，自动尝试启动。
    返回 skill.execute(params) 的结果（含 passthrough 字段）。
    """
    runtime = getattr(api_execute_skill, "_runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "RUNTIME_NOT_READY", "message": "XiheRuntime 未初始化"},
        )

    # agent_id 优先级：body > X-Agent-ID header > auth context > 拒绝
    # 空 agent_id 会拉起 --agent-id="" 的通用子进程、数据落到共享目录，静默出错（投标 skill 线上事故根因 #1）
    agent_id = (
        body.agent_id
        or request.headers.get("X-Agent-ID", "")
        or getattr(request.state, "agent_id", "")
    )
    logger.info("[trace] execute_api agent_id resolve body=%r header=%r state=%r → agent=%r",
                body.agent_id, request.headers.get("X-Agent-ID", ""),
                getattr(request.state, "agent_id", ""), agent_id)
    if not agent_id:
        raise HTTPException(
            status_code=400,
            detail={"code": "AGENT_ID_MISSING", "message": "缺少 agent_id，无法为 Skill 拉起用户隔离实例（请由网关注入可信 agent_id）"},
        )

    # 幂等兜底（2026-08-24）：同 message_id 已完成 → 返回缓存结果；处理中 → 忽略重复。
    # message_id 缺失（bus skillExecute 不透传）→ 降级文本 key（短窗口）。
    # 在途标记在真正执行前写入，前面的 license 校验等失败路径不会留下脏标记。
    # 2026-08-26：key 统一携带 user_id（认证上下文，兜底 body.user_id），防共享岗位
    # agent_id 下跨用户串缓存、及同用户跨闸双跑（见 _idem_fallback_key 注释）。
    _idem_uid = getattr(request.state, "agent_id", "") or body.user_id or ""
    idem_ttl = _IDEM_TTL
    if body.message_id:
        idem_key = f"{skill_name}|{agent_id}|u:{_idem_uid}|{body.message_id}"
    else:
        idem_key = _idem_fallback_key(skill_name, agent_id, _idem_uid, body.params)
        idem_ttl = _IDEM_FALLBACK_TTL
    if idem_key:
        _idem_cleanup()
        _now = time.time()
        _cached = _IDEM_RESULTS.get(idem_key)
        if _cached and _now - _cached[0] < idem_ttl:
            logger.info("[trace] execute_api idempotent hit (cached result) key=%s", idem_key)
            return _cached[1]
        if _IDEM_INFLIGHT.get(idem_key, 0) and _now - _IDEM_INFLIGHT[idem_key] < idem_ttl:
            logger.warning("[trace] execute_api idempotent hit (in-flight, skip duplicate) key=%s", idem_key)
            # 2026-08-30：in-flight 命中不再静默，明确告知用户在处理中（小智 B 方向）。
            # 注意：这里绝不释放锁（A 方向否决——锁是双路径 7s 间隔重复执行的
            # 最后一道防线，缩短 TTL/提前释放会复活"两份草稿"事故）；锁本身
            # 已有 TTL 兜底且成功/失败两条路径都会 pop。
            return {
                "ok": True,
                "duplicate": True,
                "reply": "⏳ 您这条消息正在处理中（大模型执行约需 30-60 秒），请稍候，无需重发。",
            }
        _fail = _IDEM_FAILURES.get(idem_key)
        if _fail and _now - _fail[0] < _IDEM_FAIL_COOLDOWN_S:
            logger.warning(
                "[trace] execute_api idempotent hit (recently failed, reject) key=%s ago=%.1fs err=%s",
                idem_key, _now - _fail[0], _fail[1][:120],
            )
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "RECENTLY_FAILED",
                    "message": (
                        f"同一消息刚执行失败（{_fail[1][:120]}），"
                        f"{int(_IDEM_FAIL_COOLDOWN_S - (_now - _fail[0]))}s 内请勿重发；"
                        "稍后如需重新生成请发送新消息"
                    ),
                },
            )

    # 将顶层字段合并到 params（兼容 Gateway 直传格式）
    extra = {}
    extra["agent_id"] = agent_id
    if body.action:
        extra["action"] = body.action
    # R6-5: user_id 优先从认证上下文取，防止请求体伪造
    auth_agent_id = getattr(request.state, "agent_id", "")
    if auth_agent_id:
        extra["user_id"] = auth_agent_id
    elif body.user_id:
        logger.warning("user_id from request body without auth context (R6-5)")
        extra["user_id"] = body.user_id
    if body.chat_type:
        extra["chat_type"] = body.chat_type
    if body.message_id:
        extra["message_id"] = body.message_id
    if extra:
        merged = dict(body.params or {})
        merged.update(extra)
        # 兼容 {action, payload} 包裹约定：agent_id/user_id 同步注入嵌套 payload 子字典。
        # 否则 bidding 等从 payload 读身份的 skill 拿到空 → step6 上传汇川 agent 空
        # （2026-08-07 大师报：子进程 agent 正确拉起但 execute payload 无 agent_id）。
        _p = merged.get("payload")
        if isinstance(_p, dict):
            _p.setdefault("agent_id", agent_id)
            if auth_agent_id:
                _p.setdefault("user_id", auth_agent_id)
        body.params = merged
        _pj = merged.get("payload")
        logger.info(
            "[trace] execute_api merged agent=%s user_id=%s action=%s params_keys=%s payload_agent=%s payload_user=%s",
            agent_id, extra.get("user_id", ""), extra.get("action", ""),
            list(merged.keys()),
            _pj.get("agent_id", "") if isinstance(_pj, dict) else "",
            _pj.get("user_id", "") if isinstance(_pj, dict) else "",
        )

    # bidding action 白名单校验 + 关键词 hint 归一化兜底（2026-08-06 fix）
    # 线上事故：OpenClaw/网关侧传了不在白名单的 action（如 LLM 幻觉 write_bid_document），
    # 导致 bidding.execute 走"未知操作"短路，未进入真实生成流程（0.2s 秒回 200）。
    # 无论 action 来自 LLM probe 还是调用方，非白名单一律用原文关键词归一化（生成 vs 评分）。
    # 2026-08-27 空白名单漏洞：门户直调带空 action（键在值空）漏过原 `if act and` 前置
    # → bidding 秒回"未知操作"标书不生成（小智 13:56 实锤）。空 action 与非白名单
    # 同样走归一化兜底（_raw_text 可能在 params 顶层或嵌套 payload 内）。
    if skill_name == "bidding" and isinstance(body.params, dict):
        body.params["action"] = _normalize_bidding_action(body.params, body.action or "")

    # 收费 Skill 到期拦截：subscriptions 或本地 license 任一到期 → 402 拒绝（防白漂）
    # 总开关：QINGTIAN_DISABLE_LICENSE_CHECK=1 临时放行（运维用，license 机制对齐后移除）
    _lic_disabled = os.environ.get("QINGTIAN_DISABLE_LICENSE_CHECK", "").lower() in ("1", "true", "yes")
    if not _lic_disabled:
        try:
            has_access = await runtime.check_skill_access(skill_name, agent_id)
        except Exception as e:
            # P1 (R11): 原 AttributeError 分支 fail-open 放行（任何 AttributeError
            # 都跳过收费检查）——xihe runtime 已实现 check_skill_access 且内部
            # fail-closed，此处统一异常即拒绝（收费安全优先）。
            logger.warning("check_skill_access error skill=%s agent=%s: %s", skill_name, agent_id, e)
            has_access = False
        if not has_access:
            raise HTTPException(
                status_code=402,
                detail={"code": "LICENSE_EXPIRED",
                        "message": f"Skill '{skill_name}' 订阅/许可已到期，请续费后使用"},
            )

    # 获取已有句柄，或启动后等待就绪
    handle = None
    try:
        handle = await runtime.get_handle(skill_name, agent_id)
    except Exception:
        pass

    if handle is None:
        # 未运行 → 自动启动
        try:
            handle = await runtime.launch_skill(skill_name, agent_id=agent_id)
        except Exception as e:
            raise HTTPException(
                status_code=503,
                detail={"code": "LAUNCH_FAILED", "message": f"启动 skill 失败: {e}"[:500]},
            )
        # 等待子进程就绪（IPC 建立需要时间，最多等 30s）
        for attempt in range(15):
            try:
                ok = await handle.ping()
                if ok:
                    break
            except Exception:
                pass
            await asyncio.sleep(2)
        else:
            raise HTTPException(
                status_code=503,
                detail={"code": "SKILL_NOT_READY",
                        "message": f"Skill '{skill_name}' 启动超时，请稍后重试"},
            )

    # 执行（带重试兜底）
    # P2 (R11)：原实现无脑重试 3 次——业务/校验错误（skill.execute 抛出的异常
    # 经 IPC 包装为 common.ipc.IPCError）也会被重放，非幂等副作用（生成投标文件/
    # 扣费/发消息）被重复执行。现仅对传输级瞬态错误（连接/超时/句柄关闭等）重试，
    # 业务错误立即透出；重试带指数退避 + 上限，且在句柄被强制卸载后重新获取/拉起。
    last_error = None
    attempts = 0
    if idem_key:
        _IDEM_INFLIGHT[idem_key] = time.time()
    while attempts < _EXECUTE_MAX_ATTEMPTS:
        try:
            result = await handle.execute(body.params)
            if idem_key:
                _IDEM_RESULTS[idem_key] = (time.time(), result)
                _IDEM_INFLIGHT.pop(idem_key, None)
                _IDEM_FAILURES.pop(idem_key, None)
            return result
        except Exception as e:
            last_error = e
            attempts += 1
            if attempts >= _EXECUTE_MAX_ATTEMPTS or not _is_retryable_exec_error(e):
                break
            backoff = _EXECUTE_BACKOFF_BASE_S * (2 ** (attempts - 1))
            logger.warning(
                "[trace] execute_api retry skill=%s agent=%s attempt=%d/%d err=%s",
                skill_name, agent_id, attempts, _EXECUTE_MAX_ATTEMPTS, str(e)[:200],
            )
            await asyncio.sleep(backoff)
            # ProcessError 触发 on-demand 强制卸载后句柄已失效 → 重新获取/拉起
            try:
                handle = await runtime.get_handle(skill_name, agent_id)
            except Exception:
                handle = None
            if handle is None:
                try:
                    handle = await runtime.launch_skill(skill_name, agent_id=agent_id)
                except Exception as le:
                    logger.warning("[trace] execute_api retry launch failed: %s", str(le)[:200])
                    break
    if idem_key:
        _IDEM_INFLIGHT.pop(idem_key, None)
        # 失败冷却（2026-08-29 任务 202→203 实锤）：记录失败 → 同 key 冷却窗内
        # 重发直接 429，挡住调用方对 500 的风暴重试再起新执行。
        _IDEM_FAILURES[idem_key] = (time.time(), str(last_error)[:200])
        logger.warning(
            "[trace] execute_api idempotent fail recorded key=%s err=%s",
            idem_key, str(last_error)[:200],
        )
    raise HTTPException(
        status_code=500,
        detail={"code": "EXECUTE_FAILED", "message": f"Skill 执行失败: {last_error}"[:500]},
    )


# ── 系统/进度消息识别（防语义路由死循环）──────────

# 2026-08-11 大师实锤死循环根因：投标 skill 生成/评审时广播进度消息
# （"⏳ 正在生成投标文件（Word）…"/"⏳ AI 评审第 4 轮…"/"✅ 投标文件生成完成"）
# 含"投标/标书/生成/评审"关键词 → 被 semantic_probe 关键词兜底误判成新
# generate_bid/evaluate_bid → 又触发新一轮生成 → 又广播 → 自我死循环（id 85-89）。
# 特征：以状态 emoji 开头（进度/完成/警告/通知，绝不会是用户指令），或进度播报专属措辞。
# 主信号 = emoji 开头（_send_progress_msg 统一前缀 "⏳ "）；措辞仅作兜底且收窄到
# 投标生成/评审专属，避免误伤真实用户指令（如"正在生成报价单"）。
_PROGRESS_NOTICE_RE = re.compile(
    r"^(⏳|✅|⚠️|📋|📦|⬇️|📎|🔔|⏸️|🔄)\s|"
    r"(评审第\s*\d+\s*轮|生成完成|生成失败|投标文件已|标书已.*交付)"
)


def _looks_like_progress_notice(text: str) -> bool:
    """是否系统/进度/通知类消息（非用户指令）。命中 → 路由应跳过。"""
    if not text:
        return False
    return bool(_PROGRESS_NOTICE_RE.search(text.strip()))


# ── Probe 端点 ────────────────────────────────────


@router.post("/{skill_name}/probe")
async def api_probe_skill(skill_name: str, body: ProbeRequest):
    """轻量探测：检查 Skill 是否能处理该 intent。

    不启动子进程、不调 LLM，纯本地规则匹配，<10ms 响应。
    给 OpenClaw pre_llm_hook 调用，决定是否拦截消息走秘书。
    """
    if not body.action:
        return {
            "ok": True,
            "passthrough": True,
            "intent": "",
            "confidence": 0.0,
        }

    # 🔒 P0 前置防护：系统/进度/通知类消息（非用户指令）→ 直接 passthrough，不路由任何 skill。
    # 2026-08-11 大师实锤：投标 skill 进度广播被 semantic_probe 关键词兜底误判成新
    # generate_bid 死循环。进度消息含"投标/标书/生成/评审"关键词必然命中 bidding 路由，
    # 必须在此拦截，绝不进入语义路由/关键词兜底。
    if _looks_like_progress_notice(body.action):
        logger.info("semantic_probe 系统/进度消息跳过路由: %r", body.action[:60])
        return {
            "ok": True,
            "passthrough": True,
            "intent": "",
            "confidence": 0.0,
        }

    # 🔒 P0: !!command!! 锚定匹配（最高优先级，O(1) 确定性路由）
    cmd = resolve_command(body.action)
    if cmd:
        if cmd.skill_name == skill_name:
            return {
                "ok": True,
                "passthrough": False,
                "intent": cmd.action,
                "confidence": 1.0,
            }
        return {
            "ok": True,
            "passthrough": False,
            "intent": f"{cmd.skill_name}:{cmd.action}",
            "confidence": 1.0,
            "target_skill": cmd.skill_name,
            "target_action": cmd.action,
        }

    # P0.5: 直接下单意图确定性路由（2026-08-13 小智实测：LLM semantic probe 偶发把
    # "下单"路由到 validate_po——非下单入口，缺 po_id/line_items 校验必然 {ok:false}，
    # 采购下单无回复。下单是用户直接指令，不该交给概率判断，提升为 LLM 之前的确定性路由。）
    if _direct_order_match(body.action):
        logger.info("semantic_probe 直接下单意图命中: %r", body.action[:60])
        return {
            "ok": True,
            "passthrough": False,
            "intent": "procurement:po_complete",
            "confidence": 1.0,
            "target_skill": "procurement",
            "target_action": "po_complete",
        }

    # P0.5.5: 补条款/续答意图确定性路由（2026-08-13 小智实测：补条款消息无"下单/补齐"强词，
    # 绕过 P0.5 落 LLM 语义路由，偶发吐校验器/幻觉 action（如 validate_rfq）→ skillExecute
    # 无自然语言回复 → 用户收不到消息。续答是既有订单草稿的补充，确定性路由
    # procurement:po_complete 续答草稿，不经 LLM。仅 procurement skill 活跃时路由
    # （sales 等部署不误路由）；履约回复/投标强词优先，不误路由。）
    if await _order_fill_match(body.action):
        logger.info("semantic_probe 补条款/续答意图命中: %r", body.action[:60])
        return {
            "ok": True,
            "passthrough": False,
            "intent": "procurement:po_complete",
            "confidence": 1.0,
            "target_skill": "procurement",
            "target_action": "po_complete",
        }

    # P0.5.6: 确认询价确定性路由（2026-08-15）：用户回复「确认询价/发起询价/开始询价」等
    # 确认短语，是对已登记草稿的确认指令 → po_complete。此前落 LLM 语义路由（规则3
    # "询价→inquiry_create"）或关键词兜底（"询价"→inquiry_create）都被劫持到 inquiry_create
    # → _complete_order 确认分支不执行 → 用户收不到"已按你的确认发起询价"。确认是直接指令，
    # 提升为 LLM 之前的确定性路由（同 P0.5 哲学，防小智实测 LLM 概率判断误路由致采购无回复）。
    # 纯确认短句（≤12字）由 _complete_order 内部确认分支发起询价；带参数长消息走正常下单流程。
    if await _confirm_inquiry_match(body.action):
        logger.info("semantic_probe 确认询价意图命中: %r", body.action[:60])
        return {
            "ok": True,
            "passthrough": False,
            "intent": "procurement:po_complete",
            "confidence": 1.0,
            "target_skill": "procurement",
            "target_action": "po_complete",
        }

    # P0.6: 询价强词+价格词确定性路由（2026-08-14 大师实锤）
    # "询价：XX多少钱"同时含询价强词(询价)+价格词(多少钱)，_KEYWORD_ROUTES 价格词规则
    # 优先级更高会劫持到 sales:query_quote → 采购服本地无销售目录 → 误报"产品目录中
    # 未找到需人工核价"（sales.py:1033）。询价强词优先走 procurement:inquiry_create，
    # 且置于 LLM 之前（防 LLM 语义路由按 355 行"查价格→query_quote"劫持）。仅本端注册
    # procurement 时路由（销售服不误路由），履约/投标强词排除。
    inquiry_route = await _inquiry_strong_route(body.action)
    if inquiry_route:
        logger.info("semantic_probe 询价强词+价格词命中: %r → %s", body.action[:60], inquiry_route)
        return {
            "ok": True,
            "passthrough": False,
            "intent": inquiry_route,
            "confidence": 0.9,
            "target_skill": "procurement",
            "target_action": "inquiry_create",
        }

    # P0.7: 修标书确定性路由（2026-08-14）：『修标书 ID：xxx 第N章补充XX』是对已生成
    # 标书的修订指令（需 record_id + user_feedback），不靠 LLM 概率判断 → 提升为
    # 确定性路由，携带参数供 bidding._revise_bid 直接使用。
    bid_revise = _bid_revise_match(body.action)
    if bid_revise:
        logger.info("semantic_probe 修标书意图命中: %r → %s:%s",
                    body.action[:60], bid_revise["skill"], bid_revise["action"])
        return {
            "ok": True,
            "passthrough": False,
            "intent": f"{bid_revise['skill']}:{bid_revise['action']}",
            "confidence": 1.0,
            "target_skill": bid_revise["skill"],
            "target_action": bid_revise["action"],
            "params": bid_revise.get("params") or {},
        }

    # P0.8: Skill tags/display_name 确定性路由（2026-08-28 波哥拍板 B 方案）。
    # 背景：_match_external_skill 原是死代码（全文件零调用）——DB tags 从未被任何
    # 运行路径消费，LLM prompt 构建时 tags 又被丢弃（skills_short 只留 skill/display/
    # actions）。bid_prep 线上实锤：DB tags 改中文仍路由不到，裸句"技术规范书整理"
    # 被 LLM 语义路由判给 bidding:generate_bid。tags 是运营方主动声明的路由意图，
    # 确定性优先于 LLM 概率判断；置于 P0.7 之后（带参数提取的确定性路由优先）。
    # 门槛 ≥0.7：tag(0.8)/display_name(0.7)/强词根(≥0.7) 过线；裸词根(0.6)/
    # description(0.4) 不过线落 LLM 消歧——防"生成/查询"泛词根误拦。
    # 多候选模糊(ambiguous，如 tag"技术规范" vs 词根"投标"同 0.8) → 放行 LLM 消歧。
    await _load_skill_routes()
    tag_match = _match_external_skill(body.action)
    if (tag_match and not tag_match.get("ambiguous")
            and tag_match.get("confidence", 0) >= 0.7):
        ext_skill = tag_match["matched_skill"]
        logger.info("semantic_probe tags路由命中: %r → %s (by=%s kw=%s conf=%.2f)",
                    body.action[:60], ext_skill, tag_match.get("matched_by"),
                    tag_match.get("keyword"), tag_match["confidence"])
        if ext_skill == skill_name:
            return {
                "ok": True,
                "passthrough": False,
                "intent": tag_match.get("matched_action") or "execute",
                "confidence": tag_match["confidence"],
            }
        return {
            "ok": True,
            "passthrough": False,
            "intent": f"{ext_skill}:{tag_match.get('matched_action') or 'execute'}",
            "confidence": tag_match["confidence"],
            "target_skill": ext_skill,
            "target_action": tag_match.get("matched_action") or "",
        }

    # P1: LLM 语义路由 — 理解用户意图 + 匹配 Skill 能力 + 提取参数
    try:
        llm_match = await _llm_semantic_probe(body.action)
    except Exception as e:
        logger.warning("LLM semantic probe failed: %s", e)
        llm_match = None

    if llm_match and llm_match.get("confidence", 0) >= 0.6:
        llm_skill = llm_match.get("matched_skill", "")
        llm_action = llm_match.get("matched_action", "")
        if not await _llm_route_usable(llm_skill, llm_action):
            # 校验器/幻觉 action 弃用 → 落 P1.5 关键词兜底（防 skillExecute 无回复/失败致回复丢）
            logger.warning("semantic_probe 弃用 LLM 路由 %s:%s（校验器/幻觉 action），落关键词兜底",
                           llm_skill, llm_action)
            llm_match = None
        else:
            result = {
                "ok": True,
                "passthrough": False,
                "intent": f"{llm_skill}:{llm_action or 'execute'}",
                "confidence": llm_match["confidence"],
                "target_skill": llm_skill,
                "target_action": llm_action,
            }
            params = llm_match.get("params")
            if params:
                result["params"] = params
            return result

    # P1.5: 关键词兜底 — LLM 失败/不确定时，用强信号关键词做确定性路由
    # 防 LLM 冷启动/invalid_json 导致投标/采购等明确需求被误放行到执策。
    keyword_match = await _keyword_fallback(body.action)
    if keyword_match:
        logger.info("semantic_probe LLM 未命中，关键词兜底命中: %s", keyword_match)
        return {
            "ok": True,
            "passthrough": False,
            "intent": f"{keyword_match['skill']}:{keyword_match.get('action', '')}",
            "confidence": 0.85,
            "target_skill": keyword_match["skill"],
            "target_action": keyword_match.get("action", ""),
        }

    # LLM + 关键词均无法确定 → 不猜测，放行给执策
    return {
        "ok": True,
        "passthrough": True,
        "intent": body.action,
        "confidence": 0.0,
    }


# ── 关键词兜底路由 ──────────────────────────────────

# 直接下单强信号（2026-08-13 提升为 LLM 之前的确定性路由，见 api_probe_skill P0.5）
# "下单/下订单/我要买/现在要买/采购单/直接采购"=初始订单，"补齐"=续答缺失项。
_ORDER_KEYWORDS = ("下单", "下订单", "我要买", "现在要买", "采购单", "直接采购", "补齐")


def _direct_order_match(text: str) -> bool:
    """命中直接下单意图 → 确定性路由 procurement:po_complete（不经 LLM）。"""
    return bool(text) and any(kw in text for kw in _ORDER_KEYWORDS)


# 补条款/续答强信号（2026-08-13 提升为 LLM 之前的确定性路由，见 api_probe_skill P0.5.5）。
# 与网关 _isOrderFill 豁免词表同源：订单补全上下文，放行走 skill 续答（po_complete）。
_ORDER_FILL_KEYWORDS = (
    "供应商是", "交货日期", "交货地点", "货到付款", "月结", "账期", "抽检",
    "物流", "发货", "质保", "税率", "不指定", "无指定", "不做要求",
)

# 确认询价强信号（2026-08-15 提升为 LLM 之前的确定性路由，见 api_probe_skill P0.5.6）。
# 与 procurement.py _CONFIRM_INQUIRY_RE 同源：用户回复「确认询价/发起询价/开始询价」等
# 确认短句，是对已登记草稿的确认指令 → po_complete（_complete_order 内部 ≤12 字纯确认
# 分支 _confirm_then_inquiry 发起询价；带参数长消息走正常下单流程）。此前落 LLM 语义路由
# （规则3"询价→inquiry_create"）或关键词兜底（"询价"→inquiry_create）都被劫持到
# inquiry_create → _complete_order 确认分支不执行 → 用户收不到"已按你的确认发起询价"。
_CONFIRM_INQUIRY_PHRASES = (
    "确认询价", "确认下单询价", "确认并询价", "确认发出询价", "确认后询价",
    "发起询价", "开始询价", "发出询价", "就去询价", "可以询价",
)

# 履约回复强信号（与 _KEYWORD_ROUTES __fulfillment__ 同源）：如"已交付,物流正常"含"物流"，
# 若先命中续答路由会误路由采购 po_complete，必须排除让履约路由优先。
_FULFILLMENT_STRONG = ("履约", "回访", "售后", "交付情况", "已交付", "延期")
# 投标强信号：如"标书交货日期怎么填"含"交货日期"，排除让 bidding 路由优先。
_BIDDING_STRONG = ("投标", "标书", "招标", "评分", "打分", "评标", "中标", "废标")


async def _order_fill_match(text: str) -> bool:
    """命中补条款/续答意图 → 确定性路由 procurement:po_complete（不经 LLM）。

    仅当 procurement skill 活跃时路由（_load_skill_routes 有 60s 缓存，开销低），
    sales 等部署不误路由；路由数据异常时保守放行（落后续 LLM/关键词判断）。
    履约回复/投标强词优先排除，不误路由。
    """
    if not text or not any(kw in text for kw in _ORDER_FILL_KEYWORDS):
        return False
    if any(kw in text for kw in _FULFILLMENT_STRONG) or any(kw in text for kw in _BIDDING_STRONG):
        return False
    try:
        routes = await _load_skill_routes()
        names = {r.get("name", "") for r in routes}
    except Exception:
        return False
    return "procurement" in names


async def _confirm_inquiry_match(text: str) -> bool:
    """命中确认询价意图 → 确定性路由 procurement:po_complete（不经 LLM）。

    2026-08-15：用户回复「确认询价/发起询价/开始询价」等确认短语，是对已登记草稿的
    确认指令。此前落 LLM 语义路由（规则3"询价→inquiry_create"）或关键词兜底（"询价"
    →inquiry_create）都被劫持到 inquiry_create → _complete_order 确认分支不执行 →
    用户收不到"已按你的确认发起询价"（甚至拿到旧成交单通知混淆）。确认是直接指令，
    提升为 LLM 之前的确定性路由（同 P0.5 下单哲学）。仅当 procurement skill 活跃时路由
    （sales 等部署不误路由）；履约回复/投标强词优先排除，不误路由。
    """
    if not text or len(text.strip()) < 2:
        return False
    low = text.lower()
    if not any(p in low for p in _CONFIRM_INQUIRY_PHRASES):
        return False
    if any(kw in text for kw in _FULFILLMENT_STRONG) or any(kw in text for kw in _BIDDING_STRONG):
        return False
    try:
        routes = await _load_skill_routes()
        names = {r.get("name", "") for r in routes}
    except Exception:
        return False
    return "procurement" in names


# 询价/报价/比价/采购/招标强词（与 _KEYWORD_ROUTES 856 行同源）：与价格词组合时优先采购询价
_INQUIRY_STRONG = ("询价", "报价", "比价", "采购", "招标")
# 价格词（与 _KEYWORD_ROUTES 850 行同源）
_PRICE_KEYWORDS = ("价格", "多少钱", "市场价", "现价", "单价")


async def _inquiry_strong_route(text: str) -> str | None:
    """询价强词 + 价格词组合 → 部署感知路由为 procurement:inquiry_create。

    2026-08-14 大师实锤："询价：YJV22...多少钱"同时含询价强词(询价)+价格词(多少钱)，
    _KEYWORD_ROUTES 850 行价格词规则优先级更高，把消息劫持到 sales:query_quote →
    采购服本地无销售目录（9 家销售数据在销售服）→ 误报"产品目录中未找到需人工核价"
    （sales.py:1033）。修复：本端注册 procurement 时询价强词+价格词 → inquiry_create
    （正确的采购→寰宇→销售服市场价回流）；未注册（销售服）→ None（落价格词规则
    query_quote 查自家目录，语义正确）。履约回复/投标强词优先排除，不误路由。
    """
    if not text or len(text) < 2:
        return None
    if not any(kw in text for kw in _INQUIRY_STRONG):
        return None
    if not any(kw in text for kw in _PRICE_KEYWORDS):
        return None
    if any(kw in text for kw in _FULFILLMENT_STRONG) or any(kw in text for kw in _BIDDING_STRONG):
        return None
    try:
        routes = await _load_skill_routes()
        names = {r.get("name", "") for r in routes}
    except Exception:
        return None  # 路由数据异常 → 保守落原逻辑（价格词规则），不误路由
    if "procurement" in names:
        return "procurement:inquiry_create"
    return None


# 修标书确定性路由（2026-08-14）：用户飞书直接说『修标书 ID：xxx 第N章补充XX』。
# 修订是对已生成标书的直接指令（需 record_id + 反馈），不靠 LLM 概率判断，
# 提升为 LLM 之前的确定性路由（同 P0.5 下单 / P0.6 询价 哲学）。
_BID_REVISE_STRONG = ("修标书", "修改标书", "修订标书", "改标书", "修订", "修正")
# 弱信号：无"修标书"强词，但含标书编号 + 修订动作词（如"标书ID123第3章补充安全措施"）
_BID_REVISE_WEAK_ACTION = ("补充", "修改", "调整", "更新", "改", "不满意")
# P3-1：兼容 ID：123 / 编号 789 / 标书 456 / ID为123 / ID号123 形态
_BID_RECORD_ID_RE = re.compile(r"(?:ID|标书编号|标书|编号)\s*(?:[:：]|[为号]\s*)*\s*(\d+)", re.IGNORECASE)


def _clean_bid_revise_feedback(text: str) -> str:
    """从修订指令中剥掉路由强词与编号，保留实质修订意见（第几章节/补充什么内容）。"""
    t = text
    for kw in _BID_REVISE_STRONG:
        t = t.replace(kw, "")
    t = _BID_RECORD_ID_RE.sub("", t)
    # 强词先于编号剥除时，"修改标书 456" 里的 456 失去前缀残留，剥掉前置裸编号
    t = re.sub(r"^\s*\d+[，,。；;：:\s]*", "", t)
    t = re.sub(r"^[，,。；;：:\s]+", "", t)
    t = re.sub(r"[，,。；;：:\s]+$", "", t)
    return t.strip()


def _bid_revise_match(text: str) -> dict | None:
    """命中『修标书』意图 → 确定性路由 bidding:revise_bid（不经 LLM）。

    强词（修标书/修改标书/修订标书/改标书/修订/修正）命中且须带投标上下文
    （标书/投标/招标），防『修订采购方案』等非投标意图被劫持绕过 LLM；
    弱信号（含标书编号 + 补充/修改/调整等修订词）也命中。提取 record_id
    （ID：123 / 编号 456）与 user_feedback（修订意见原文）。
    返回 {"skill","action","params"} 或 None。
    """
    if not text or len(text) < 4:
        return None
    m_id = _BID_RECORD_ID_RE.search(text)
    has_bid_ctx = any(w in text for w in ("标书", "投标", "招标"))
    strong = any(kw in text for kw in _BID_REVISE_STRONG) and has_bid_ctx
    weak = bool(m_id) and any(kw in text for kw in _BID_REVISE_WEAK_ACTION)
    if not (strong or weak):
        return None
    params: dict = {}
    if m_id:
        params["record_id"] = int(m_id.group(1))
    feedback = _clean_bid_revise_feedback(text)
    if feedback:
        params["user_feedback"] = feedback
    return {"skill": "bidding", "action": "revise_bid", "params": params}


def _is_validator_action(action: str) -> bool:
    """纯校验器 action（validate_*）→ 返回 {ok, errors}，非对话入口，无自然语言回复。"""
    return action in _VALIDATE_ACTIONS


async def _llm_route_usable(llm_skill: str, llm_action: str) -> bool:
    """LLM 语义路由结果是否可作对话路由采用。

    弃用条件（→ 落 P1.5 关键词兜底，防 skillExecute 无回复/失败导致用户收不到消息）：
      1. 纯校验器 action（validate_*）——返回 {ok, errors}，无自然语言回复（2026-08-13 小智实测）；
      2. action 不在该 skill 合法 action 列表（bidding 用白名单，其余用
         skill_definitions.input_schema enum，见 _load_skill_routes.actions）——
         LLM 幻觉不存在的 action（如 write_bid_document）。
    例外：skill 不在活跃路由 / actions 为空（未 sync input_schema）时不做存在性拦截，
    避免误杀（校验器拦截仍生效）。
    """
    if not llm_skill or not llm_action:
        return False
    if _is_validator_action(llm_action):
        logger.info("semantic_probe LLM 路由 %s:%s 是纯校验器 action，弃用", llm_skill, llm_action)
        return False
    if llm_skill == "bidding":
        return llm_action in _BIDDING_VALID_ACTIONS
    try:
        routes = await _load_skill_routes()
    except Exception:
        return True
    for r in routes:
        if r.get("name") == llm_skill:
            actions = r.get("actions") or []
            if not actions:
                return True  # 未 sync enum → 不做存在性拦截
            return llm_action in actions
    return True  # skill 未在活跃路由（网关可能单独注册）→ 不拦


_KEYWORD_ROUTES = [
    # (关键词列表, skill, action)
    # 规则：关键词必须 ≥2 字，且至少命中一个即路由。按声明顺序匹配，先命中先得。
    # bidding 的 action 由 _bidding_action_hint 判别（生成 vs 评分）。
    (("投标", "标书", "评分", "打分", "评标", "中标", "废标"), "bidding", ""),
    # 履约回访回复：买卖双方都可能回复履约/回访/售后/交付情况。__fulfillment__ 由探针
    # 按本端已注册 skill 解析（procurement/sales 二选一），action=fulfillment_reply。
    (("履约", "回访", "售后", "交付情况", "已交付", "延期"), "__fulfillment__", "fulfillment_reply"),
    # 销售接单闸门回复 + 自主成交委托开关（2026-08-09，顺序敏感：必须在 query_quote 之前，
    # "接单，单价不能低于5000" 含"单价"，会被下一条价格查询吞掉）。
    (("接单", "拒单", "接这个单", "同意接单"), "sales", "order_decide"),
    (("开启自主成交", "关闭自主成交", "委托自主成交", "设置自主成交", "开启自动接单", "关闭自动接单"), "sales", "set_auto_delegate"),
    # 价格查询意图优先于采购询价（顺序敏感）：
    # "询价，YJV22 4×70 电缆现在的价格是多少" 含"价格/是多少" → sales:query_quote；
    # 纯采购意图"询价 10 台变压器"无价格词 → 落到下一条 procurement。
    (("价格", "多少钱", "市场价", "现价", "单价"), "sales", "query_quote"),
    # 直接下单意图 → po_complete（有状态草稿补齐；"下单/下订单/我要买/现在要买/采购单/直接采购"=初始订单，"补齐"=续答缺失项）。
    (_ORDER_KEYWORDS, "procurement", "po_complete"),
    # 补条款/续答特征 → po_complete（与 P0.5.5 确定性路由同源；LLM 结果被弃用/LLM 未命中时的兜底续答草稿）
    (("供应商是", "交货日期", "交货地点", "货到付款", "月结", "账期", "抽检",
      "物流", "发货", "质保", "税率", "不指定", "无指定", "不做要求"), "procurement", "po_complete"),
    # 确认询价 → po_complete（2026-08-15 与 P0.5.6 确定性路由同源；须先于下一条"询价→
    # inquiry_create"，防 LLM 失败/不确定时确认短语被兜底劫持到 inquiry_create）
    (("确认询价", "确认下单询价", "确认并询价", "确认发出询价", "确认后询价",
      "发起询价", "开始询价", "发出询价", "就去询价", "可以询价"), "procurement", "po_complete"),
    (("询价", "报价", "比价", "采购", "招标"), "procurement", "inquiry_create"),
    (("生成文档", "写文档", "生成 word", "生成 excel", "生成 ppt", "生成 pdf"), "word_generator", ""),
    (("翻译", "翻译成"), "translator", ""),
    (("会议纪要", "会议记录", "转写"), "work_secretary", "meeting:transcribe"),
    # 找回记忆（2026-08-16）：用户触发「找回记忆/恢复记忆/失忆」→ work_secretary memory:recover，
    # 确定性路由（防 LLM 语义路由漏识别），携带 _memory_recover 标记走双通道（注入 + 写 MEMORY.md）。
    (("找回记忆", "恢复记忆", "失忆"), "work_secretary", "memory:recover"),
]

# 生成意图强信号：写/生成/制作/起草/响应/编写 + 投标相关词
_GENERATE_BID_HINTS = ("写", "生成", "制作", "起草", "响应", "编写", "撰写", "做一份")
# 修订意图强信号（2026-08-14）：修标书/修改标书/修订标书/改标书 → revise_bid
# 放在生成之前判断，避免"修标书"含"标书"被生成词"写/制作"误吞
_REVISE_BID_HINTS = ("修标书", "修改标书", "修订标书", "改标书")
# 通用修订词须带投标上下文（标书/投标/招标）才判修订，防"修订采购方案"等非投标意图误判（P1-②）
_REVISE_BID_GENERIC = ("修订", "修正", "补充", "调整", "不满意")
# 评分意图强信号
_EVALUATE_BID_HINTS = ("评分", "打分", "评标", "评审", "评标打分", "分析")


def _normalize_bidding_action(params: dict, body_action: str = "") -> str:
    """bidding action 归一化：非白名单或空 action 一律按原文关键词兜底。

    白名单内原样返回（含 health/search_files 等非生成类，不受影响）；
    空 action（门户直调键在值空，2026-08-27 小智 13:56 实锤）与非白名单
    action（LLM 幻觉，2026-08-06）同走 _bidding_action_hint 归一化。
    原文线索优先级：params 顶层 _raw_text/query > 嵌套 payload 内
    _raw_text/query/text > body.action。
    """
    params = params or {}
    act = params.get("action", "")
    if act in _BIDDING_VALID_ACTIONS:
        return act
    raw = str(params.get("_raw_text") or params.get("query") or "")
    _pl = params.get("payload")
    if not raw and isinstance(_pl, dict):
        raw = str(_pl.get("_raw_text") or _pl.get("query") or _pl.get("text") or "")
    if not raw:
        raw = body_action or ""
    hint = _bidding_action_hint(raw)
    logger.warning(
        "[trace] execute_api bidding action '%s' %s, normalize to '%s' (raw=%s)",
        act, "not in whitelist" if act else "empty", hint, raw[:60],
    )
    return hint


def _bidding_action_hint(text: str) -> str:
    """判别 bidding 意图：修订 / 生成投标文件 / 投标评分。

    修订强词优先（修标书/修改标书/修订标书/改标书，2026-08-14）；生成强词其次
    （写/生成/制作/起草/响应/编写/撰写/做一份，先于通用修订词防"生成+补充/调整"
    被误判为修订，199cfd1 回归根因）；通用修订词（修订/修正/补充/调整/不满意）
    须带投标上下文才判修订；评分类最后；都不匹配时默认评分（兼容旧行为）。
    """
    if not text:
        return "evaluate_bid"
    for kw in _REVISE_BID_HINTS:
        if kw in text:
            return "revise_bid"
    if any(kw in text for kw in _GENERATE_BID_HINTS):
        # 生成强词（写/生成/制作/起草/响应/编写/撰写/做一份）优先于通用修订词，
        # 防"生成投标文件，技术方案补充XX"被"补充"误判为修订——revise_bid 无
        # record_id 会失败 → 标书不生成（199cfd1 回归根因）
        return "generate_bid"
    if any(w in text for w in ("标书", "投标", "招标")):
        for kw in _REVISE_BID_GENERIC:
            if kw in text:
                return "revise_bid"
    for kw in _EVALUATE_BID_HINTS:
        if kw in text:
            return "evaluate_bid"
    return "evaluate_bid"


async def _resolve_fulfillment_skill() -> str:
    """履约回访回复路由到本端已注册的买卖 Skill。

    履约回复（履约/回访/已交付等）买卖双方都可能说，探针无 agent 上下文时按本端
    已注册 skill 判定：采购服（procurement）→ procurement，销售服（sales）→ sales。
    """
    try:
        routes = await list_active_skill_routes()
        names = {r.get("name", "") for r in routes}
    except Exception:
        names = set()
    if "procurement" in names:
        return "procurement"
    return "sales"


async def _keyword_fallback(text: str) -> dict | None:
    """强信号关键词匹配 — LLM 不稳定时的确定性兜底。

    只匹配高置信度关键词（如"投标""标书"→bidding），不搞模糊匹配。
    返回 {"skill": ..., "action": ...} 或 None。
    """
    if not text or len(text) < 2:
        return None
    for keywords, skill, action in _KEYWORD_ROUTES:
        for kw in keywords:
            if kw in text:
                if skill == "bidding":
                    action = _bidding_action_hint(text)
                elif skill == "__fulfillment__":
                    skill = await _resolve_fulfillment_skill()
                return {"skill": skill, "action": action}
    return None
