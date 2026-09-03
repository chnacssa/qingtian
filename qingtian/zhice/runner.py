"""执策执行引擎 — Task 创建 / Step 分配 / 完结判定



GET /next 原子分配使用 FOR UPDATE SKIP LOCKED 保证并发安全。

"""

import asyncio

import json

import logging

import re

import uuid

from datetime import datetime, timezone

import httpx

from common.db import get_pool

from . import config as cfg

from . import status_machine as sm

from .models import AppError

from zhice.huichuan_client import search_knowledge, search_same_category_experience

# exec_type 合法值 = daemon 分发表（agent_daemon.py execute_step）全集 + manual
VALID_EXEC_TYPES = ("manual", "http", "skill", "script", "shell")


def _resolve_exec_type(step: dict) -> str:
    """归一化步骤 exec_type（P0-1 9-2：缺省/无效不再默认 shell）。

    - 合法值（大小写不敏感）原样采纳（manual/http/skill/script/shell），
      显式声明优先——不再对显式 shell 重跑 HTTP 前缀自动识别
    - 缺省时保留既有便利：instruction 以 HTTP 方法开头 → "http"
    - 其余（缺省非 HTTP 指令 / 非法值如 "bash"）→ "manual"：
      daemon 对 manual 仅确认不执行，杜绝"不写 exec_type 即得 shell"
      的 RCE 缺省面（原 :1555-1565 缺省/无效一律落 shell）
    """
    raw = str(step.get("exec_type") or "").strip().lower()
    if raw in VALID_EXEC_TYPES:
        return raw
    instr = str(step.get("instruction") or "").strip()
    if not raw and re.match(r"^(GET|POST|PUT|DELETE|PATCH)\s", instr):
        return "http"
    return "manual"

from zhice.config import get_knowledge_search_enabled, get_knowledge_search_max_results
from common.config import get as _cfg_get
from common.llm import llm_call_json, _extract_json_block
from huanyu.config import get_schema_name as _huanyu_schema
from yongheng.memory_service import write_memory
from yongheng.trajectory_service import add_action
from zhenyue.audit_service import write_audit
from .checker import check_all
from .dispatcher import ws_notify
from .ws import manager as zhice_ws
from .xixing_client import learn_workflow





# ── trace_id 轻量缓存（task_id → trace_id）──

# 用于在 step 级别操作中快速追溯上下文

# review(2026-08-16): 原 dict 无限增长（每个 task 一条、永不淘汰）。
# 改为有界缓存：超过上限淘汰最旧（task_id 单调递增 → 最早注册即最旧）。
# 命中失败回退 "unknown"，仅影响日志可追溯性，不影响主流程。

import collections

_TRACE_REGISTRY_MAX = 10_000

_trace_registry: "collections.OrderedDict[int, str]" = collections.OrderedDict()





def _get_trace(task_id: int) -> str:

    return _trace_registry.get(task_id, "unknown")





def _register_trace(task_id: int, trace_id: str):

    if task_id in _trace_registry:

        _trace_registry.move_to_end(task_id)

    _trace_registry[task_id] = trace_id

    # 超出上限：淘汰最旧的一半，防内存无限增长

    while len(_trace_registry) > _TRACE_REGISTRY_MAX:

        _trace_registry.popitem(last=False)



logger = logging.getLogger("zhice.runner")

SCHEMA = cfg.get_schema_name()



# 步骤输出/摘要最大长度（字符），防止超大 payload 写入数据库

MAX_STEP_SUMMARY_LENGTH = 5000

MAX_TASK_MEMORY_LENGTH = 10000

# LLM 分解 — Step 数量上下界

MIN_DECOMPOSE_STEPS = 1

MAX_DECOMPOSE_STEPS = 20





def _build_task_memory(task: dict, steps: list[dict], action: str) -> str:

    """构建 Task 记忆文本"""

    total = len(steps)

    done = sum(1 for s in steps if s["status"] in ("completed", "skipped"))

    failed = sum(1 for s in steps if s["status"] == "failed")

    return (

        f"Task #{task['task_id']} '{task['title']}' {action}: "

        f"{done}/{total} steps completed"

        + (f", {failed} failed" if failed else "")

    )





def _now():

    return datetime.now(timezone.utc)





def _safe_jsonb(val):

    """jsonb 反序列化兜底 — 兼容旧连接池返回字符串，含二次解码防双层编码。"""

    if isinstance(val, str):

        try:

            result = json.loads(val)
            if isinstance(result, str):
                result = json.loads(result)
            return result
        except (json.JSONDecodeError, TypeError):
            return None

    return val





# ── LLM 自动分解任务 ──────────────────────────────────────



DECOMPOSE_SYSTEM_PROMPT = """你是任务分解专家。将用户的工作指令拆解为可执行的步骤序列。

## 业务类型识别（先判断类别，再拆解）

根据标题/描述中的关键词判断任务类别：

- **投标类**（含"投标""标书""评分""打分""中标""废标""bid"）:
  步骤应为 exec_type="skill"，instruction="bidding:score_bid" 或 "bidding:generate_bid"

- **询价/采购类**（含"询价""采购""比价""报价"）:
  步骤为「发送询价→收集报价→比价」

- **文档生成类**（含"生成""写""文档""excel""word""ppt""pdf"）:
  步骤为 exec_type="skill"，instruction 指向对应文档生成 Skill

- **数据分析类**（含"分析""统计""报表"）:
  步骤为「读取数据→计算→生成报告」

## 规则

1. 每个步骤必须是原子操作（单一职责），不可再拆分

2. 步骤序号从 1 开始递增

3. 只能引用 step_index **小于**自己的步骤作为依赖（depends_on）

4. 每个步骤必须独立可验证——提供 acceptance_criteria 检查规则

5. 步骤数量: 简单任务 1-2 步，中等 3-5 步，复杂 6-10 步，最多 15 步

6. exec_type 决定如何执行此步骤:

   - "shell":   shell 命令（如 free -h, curl, psql, systemctl）

   - "http":    HTTP API 调用 — ⚠️ instruction 格式必须是: "METHOD /真实路径 -d '{...}'"

                只能使用以下真实端点:

                - /v1/huanyu/messages      — 发送消息到寰宇总线（跨 agent 通信）

                - /v1/huanyu/messages/inbox — 读取收件箱

                - /api/v1/skills/.../execute — 执行 Skill 操作（优先于消息总线）

                ❌ 严禁使用 /api/v1/inquiry 等不存在的端点

                ⚠️ 字段名必须精确: from_agent / to_agent（不是 from_agent_id / to_agent_id）

   - "script":  执行脚本文件（如 python3 /opt/scripts/check.py）

   - "manual":  需要人工判断的步骤（Agent 守护进程仅提示，不自动执行）

   - "skill":   调用平台 Skill 功能 — instruction="skill_name:action_name", params={...}

                示例: {"exec_type":"skill","instruction":"procurement:inquiry_create","params":{"product":"电缆","quantity":100}}

7. 优先选择 skill / shell / http / script，只在确实无法自动化时选 manual



## 检查规则类型

- output_contains: 检查输出中是否包含关键字 — ⚠️ 仅确知输出格式时用，日常 shell 不用

- file_exists: 检查文件是否存在（path=路径, required=true/false）

- api_health: 检查 API 健康状态（url=地址, expected_status=http状态码）— 用于 exec_type=http

- db_query: 检查数据库查询结果（sql=查询语句, expected_min=最小行数）

- run_script: 执行脚本并检查返回值（script=脚本路径, expected_exit_code=期望退出码）

- reasonableness: 检查输出数值是否在合理范围（field=result, min=下限, max=上限）



⚠️ 从简原则：日常 shell 命令(free/df/ps/curl 等)不设 output_contains，

只设 api_health(有 curl 时)或 run_script(有脚本时)。不确定输出格式时留空数组 []。

daemon 默认信任 exit_code=0 即为通过。



## 输出格式

8. quality_criteria（质量标准，可选，给 Agent 自检用，非引擎检查）：

   每步 0-3 条，格式: {"category": "must|should|nice", "rule_type": "类型", "description": "描述", "检查参数"}

   rule_type 必须是以下六种之一：api_health / file_exists / output_contains / db_query / run_script / manual_review

   如果某条标准无法对应任何类型，说明定得太虚，需要拆细再写

   must（一票否决）> should（质量基线）> nice（有余力再做）



仅返回 JSON 数组，不要额外文字:

[{"step_index":1,"title":"步骤标题","instruction":"...","exec_type":"shell|http|script|manual","assigned_agent":"sys-eng|ops-agent|manager 或空","depends_on":[],"acceptance_criteria":[...],"quality_criteria":[...],"timeout_minutes":5}]



assigned_agent 填写规则（决定哪个 daemon 来抢这一步）:

- shell 类检测命令 → "sys-eng" 或 "ops-agent"

- http 类 API 调用 → "ops-agent" 或 "manager"

- manual 类需人工 → "manager"

- 无法判断 → 留空（所有 daemon 都可以抢）



## 各行各业示例

系统巡检: [{"step_index":1,"title":"CPU","instruction":"nproc && uptime && top -bn1 | head -5","exec_type":"shell","assigned_agent":"sys-eng","depends_on":[],"acceptance_criteria":[],"timeout_minutes":3}]

采购询价: [{"step_index":1,"title":"发送询价","instruction":"POST /v1/huanyu/messages -d '{\"from_agent\":\"<你的采购Agent ID>\",\"to_agent\":\"biz:seller\",\"message_type\":\"inquiry\",\"payload\":{\"product\":\"YJV22-0.6/1 4×50 电缆\",\"quantity\":160,\"unit\":\"米\"}}'","exec_type":"http","assigned_agent":"ops-agent","depends_on":[],"timeout_minutes":5}]
采购询价(Skill直调): [{"step_index":1,"title":"创建询价","instruction":"procurement:inquiry_create","exec_type":"skill","params":{"product":"YJV22-0.6/1 4×50 电缆","quantity":160,"unit":"米"},"assigned_agent":"ops-agent","depends_on":[],"timeout_minutes":5}]

采购询价-需澄清: [{"step_index":1,"title":"补充信息","instruction":"需要补充: 向用户询问具体的物料规格、数量、期望到货时间等","exec_type":"manual","assigned_agent":"manager","depends_on":[],"timeout_minutes":10}]

销售报价: [{"step_index":1,"title":"回复报价","instruction":"POST /v1/huanyu/messages -d '{\"from_agent\":\"<你的销售Agent ID>\",\"to_agent\":\"<采购方Agent ID>\",\"message_type\":\"quote\",\"payload\":{\"product\":\"YJV22-0.6/1 4×50 电缆\",\"unit_price\":85.50,\"quantity\":160,\"delivery\":\"15天\",\"valid_until\":\"2026-08-01\"}}'","exec_type":"http","assigned_agent":"ops-agent","depends_on":[],"timeout_minutes":5}]

部署服务: [{"step_index":1,"title":"拉代码","instruction":"cd /opt/app && git pull origin master","exec_type":"shell","assigned_agent":"sys-eng","depends_on":[],"acceptance_criteria":[{"type":"file_exists","path":"/opt/app/main.py","required":true}],"timeout_minutes":5}]

信息安全: [{"step_index":1,"title":"审计日志","instruction":"仅提示——请联系安全管理员审核审计日志","exec_type":"manual","assigned_agent":"manager","depends_on":[],"timeout_minutes":10}]



"""



DECOMPOSE_REQUIRED_FIELDS = frozenset({"step_index", "title", "instruction", "exec_type"})





def _validate_decomposed_steps(steps: list[dict]) -> str | None:

    """校验 LLM 返回的 steps 结构合法性，返回错误信息或 None"""

    if not isinstance(steps, list) or len(steps) == 0:

        return "LLM 返回的 steps 为空"



    if len(steps) > MAX_DECOMPOSE_STEPS:

        return f"LLM 返回 {len(steps)} 个步骤，超过上限 {MAX_DECOMPOSE_STEPS}"



    seen_idx = set()

    for i, s in enumerate(steps):

        if not isinstance(s, dict):

            return f"steps[{i}] 不是对象"



        missing = DECOMPOSE_REQUIRED_FIELDS - set(s.keys())

        if missing:

            return f"steps[{i}] 缺少必填字段: {', '.join(sorted(missing))}"



        idx = s["step_index"]

        if not isinstance(idx, int) or idx < 1:

            return f"steps[{i}].step_index 无效: {idx}"

        if idx in seen_idx:

            return f"step_index={idx} 重复"

        seen_idx.add(idx)



        # depends_on 校验

        deps = s.get("depends_on") or []

        for d in deps:

            if not isinstance(d, int) or d >= idx:

                return f"steps[{i}] depends_on={d} 无效：只能引用小于自身的 step_index"



        # title/instruction 非空

        title = s.get("title", "")

        instruction = s.get("instruction", "")

        if not isinstance(title, str) or not title.strip():

            return f"steps[{i}].title 为空"

        if not isinstance(instruction, str) or not instruction.strip():

            return f"steps[{i}].instruction 为空"

        if len(title) > 256:

            return f"steps[{i}].title 超过 256 字符"

        if len(instruction) > 5000:

            return f"steps[{i}].instruction 超过 5000 字符"



        # acceptance_criteria type 校验

        for j, ac in enumerate(s.get("acceptance_criteria") or []):

            if not isinstance(ac, dict) or "type" not in ac:

                return f"steps[{i}].acceptance_criteria[{j}] 缺少 type"



    return None





async def _build_agent_context() -> str:

    """查询已注册 Agent 列表，注入 LLM 提示约束 from_agent/to_agent 取值"""

    try:

        pool = await get_pool()

        async with pool.acquire() as conn:

            rows = await conn.fetch(

                f"SELECT agent_id, name, category, server_host FROM {_huanyu_schema()}.agents "

                f"WHERE status = 'active' ORDER BY category, name"

            )

        if not rows:

            return ""

        # 过滤掉类别占位符 + 管理服残留 agent

        _my_role = _cfg_get("role", "management")

        agents = [

            f"  {r['agent_id']} ({r['name']}, category={r['category']})"

            for r in rows

            if not r['agent_id'].startswith('biz:')  # 排除类别占位符

            and not (_my_role != "management" and r.get('server_host') == 'management-server')

        ]

        return (

            "\n\n## 当前可用的 Agent（from_agent / to_agent 必须从以下列表选择）\n"

            + "\n".join(agents)

            + "\n\n禁止使用 biz:seller、procurement-001、sales-001、ops-agent、supplier 等不在列表中的 ID。"

            + "\n如果找不到匹配的 Agent，将 assigned_agent 留空。"

        )

    except Exception:

        return ""





async def llm_decompose_task(title: str, description: str) -> tuple[list[dict] | None, str | None]:

    """调 LLM 将任务标题+描述自动拆解为 Steps



    Returns:

        (steps, error) — 成功时 steps 非空 error=None，失败时 steps=None error 有值

    """

    api_key = cfg.get_llm_api_key()

    if not api_key:

        return None, "LLM API Key 未配置（DEEPSEEK_API_KEY）"



    base_url = cfg.get_llm_base_url()

    model = cfg.get_llm_decompose_model()

    timeout = cfg.get_llm_decompose_timeout()



    user_prompt = f"任务标题: {title}\n任务描述: {description}\n\n请将以上任务拆解为步骤序列（JSON 数组格式）。"



    # ── 注入已注册 Agent 列表，约束 from_agent/to_agent 取值 ──

    agent_context = await _build_agent_context()



    last_error = None

    raw = ""

    for attempt in range(2):  # 首次 + 1 次重试（第2次可能走缓存）

        try:

            async with httpx.AsyncClient() as client:

                if attempt > 0:

                    logger.info("LLM decompose retry %d/2 for task '%s'", attempt, title)

                resp = await client.post(

                    f"{base_url}/chat/completions",

                    headers={"Authorization": f"Bearer {api_key}"},

                    json={

                        "model": model,

                        "messages": [

                            {"role": "system", "content": DECOMPOSE_SYSTEM_PROMPT + agent_context},

                            {"role": "user", "content": user_prompt},

                        ],

                        "max_tokens": cfg.get_llm_decompose_max_tokens(),

                        "temperature": cfg.get_llm_decompose_temperature(),

                    },

                    timeout=timeout,

                )

                resp.raise_for_status()

                body = resp.json()

                raw = body["choices"][0]["message"]["content"].strip()

                break  # 成功，跳出重试

        except httpx.TimeoutException:

            last_error = f"LLM 调用超时（{timeout}秒）"

            if attempt == 0:

                logger.warning("LLM decompose timeout, retrying...")

                continue

        except Exception as e:

            last_error = str(e)

            break  # 非超时错误不重试



    if last_error:

        # 重试也失败了

        if "超时" in last_error:

            return None, last_error

        logger.exception("LLM decompose failed for task '%s': %s", title, last_error)

        return None, f"LLM 分解任务失败: {last_error}"



    if not raw:

        return None, last_error or "LLM 返回空内容"



    # 提取 JSON（处理可能的 markdown code 块 + 转义问题）

    if raw.startswith("```"):

        raw = raw.split("\n", 1)[-1]

        if raw.endswith("```"):

            raw = raw[:-3]

        raw = raw.strip()

        if raw.startswith("json"):

            raw = raw[4:].strip()



    # 修复 LLM 常见 JSON 转义问题

    raw = raw.replace("\\_", "_").replace("\\'", "'")

    try:

        steps = json.loads(raw)

    except json.JSONDecodeError:

        start = raw.find("[")

        end = raw.rfind("]")

        if start >= 0 and end > start:

            raw = raw[start:end + 1]

            try:

                steps = json.loads(raw)

            except json.JSONDecodeError as e:

                logger.error("LLM decompose JSON parse error after fix: %s, raw=%s", e, raw[:200])

                return None, f"LLM 返回非 JSON 格式: {e}"

        else:

            logger.error("LLM decompose JSON parse error: raw=%s", raw[:200])

            return None, f"LLM 返回非 JSON 格式"

    if isinstance(steps, dict):

        steps = steps.get("steps", [])



    err = _validate_decomposed_steps(steps)

    if err:

        return None, f"LLM 分解结果校验失败: {err}"



    logger.info(f"LLM decomposed task '{title}' into {len(steps)} steps")

    return steps, None





async def _llm_call_json(prompt: str, caller: str, default: dict,

                         timeout: int = 20, max_tokens: int = 300) -> dict | None:

    """LLM 调用 + JSON 解析，委托到底座统一双模型路由。"""

    return await llm_call_json(

        prompt=prompt, caller=caller, default=default,

        timeout=timeout, max_tokens=max_tokens, temperature=0,

    )





# ── 简单/复杂模式判断 ────────────────────────────────────



def _mode_from_steps(step_count: int) -> str:

    return "simple" if step_count == 1 else "complex"





# ── depends_on 校验 ──────────────────────────────────────



def validate_depends_on(steps: list[dict]) -> str | None:

    """校验 depends_on 合法性，返回错误信息或 None"""

    for s in steps:

        deps = s.get("depends_on") or []

        for d in deps:

            if d >= s["step_index"]:

                return (

                    f"step_index={s['step_index']} depends_on={d} 无效："

                    f"只能引用 step_index < 自身的步骤"

                )

    return None





# ── Workflow 解析 ─────────────────────────────────────────



async def _resolve_workflow(conn, workflow_id: int, workflow_version: int | None = None) -> dict:

    """从 workflow 表取出 definition，返回 {steps, acceptance_criteria, timeout_minutes}"""

    if workflow_version is not None:

        row = await conn.fetchrow(

            f"SELECT definition FROM {SCHEMA}.workflows "

            f"WHERE workflow_id = $1 AND version = $2",

            workflow_id, workflow_version,

        )

    else:

        row = await conn.fetchrow(

            f"SELECT definition FROM {SCHEMA}.workflows "

            f"WHERE workflow_id = $1 ORDER BY version DESC LIMIT 1",

            workflow_id,

        )

    if not row:

        raise AppError("NOT_FOUND", f"Workflow {workflow_id} 不存在", 404)



    definition = row["definition"]

    if isinstance(definition, str):

        definition = json.loads(definition)



    return {

        "steps": definition.get("steps", []),

        "acceptance_criteria": definition.get("acceptance_criteria"),

        "timeout_minutes": definition.get("timeout_minutes"),

    }





# ── 模版匹配 ────────────────────────────────────────────



CLARITY_MIN_CHARS = 20

CLARITY_LLM_CONFIDENCE = 0.5





async def _check_clarity(title: str, description: str) -> dict:

    """检测指令模糊度。



    短指令(<20字)或 LLM 判定不确定时，返回澄清问题而非分解执行。

    Returns:

        {"needs_clarification": bool, "score": float, "questions": [...]}

    """

    text = f"{title} {description}".strip()

    # 快速通道：足够长且明确 → 直接过

    if len(text) >= 60:

        return {"needs_clarification": False, "score": 1.0, "questions": []}



    # 极短 → 一定需要澄清

    if len(text) < CLARITY_MIN_CHARS:

        return {"needs_clarification": True, "score": 0.1, "questions": [

            "您的指令比较简短，能否补充一些细节？",

            "例如：具体要做什么、期望的结果、有没有时间或资源的限制？",

        ]}



    # 中等长度 → LLM 判断

    api_key = cfg.get_llm_api_key()

    if not api_key:

        return {"needs_clarification": len(text) < 40, "score": 0.5, "questions": []}



    try:

        base_url = cfg.get_llm_base_url()

        model = cfg.get_llm_decompose_model()

        prompt = (

            f"评估以下指令的明确程度，返回 JSON: {{\"clear\": true/false, \"confidence\": 0.0-1.0, "

            f"\"questions\": [\"澄清问题\"]}}\n\n"

            f"如果指令明确描述了要做的事、对象和环境，回复 clear=true。"

            f"如果模糊（缺对象、缺范围、目标不明确），回复 clear=false 并提供 2-3 个澄清问题。\n\n"

            f"指令: {text}"

        )

        async with httpx.AsyncClient(timeout=10) as client:

            resp = await client.post(

                f"{base_url}/chat/completions",

                headers={"Authorization": f"Bearer {api_key}"},

                json={"model": model, "messages": [{"role": "user", "content": prompt}],

                      "max_tokens": 200, "temperature": 0},

            )

            resp.raise_for_status()

            raw = resp.json()["choices"][0]["message"]["content"].strip()

            block = _extract_json_block(raw)
            if block:
                result = json.loads(block)
            else:
                result = json.loads(raw)

        clear = result.get("clear", True)

        confidence = result.get("confidence", 1.0)

        questions = result.get("questions", [])

        needs = not clear and confidence > CLARITY_LLM_CONFIDENCE

        logger.info("Clarity check: clear=%s conf=%.2f chars=%d", clear, confidence, len(text))

        return {"needs_clarification": needs, "score": confidence, "questions": questions}

    except Exception:

        logger.exception("Clarity LLM check failed, passing through")

        return {"needs_clarification": False, "score": 1.0, "questions": []}





def _jaccard(text: str, template_text: str) -> float:

    """Jaccard 相似度 — 基于重叠词。"""

    a = set(text)

    b = set(template_text)

    if not a and not b:

        return 0.0

    return len(a & b) / len(a | b)





MATCH_HIGH = 0.7

MATCH_MEDIUM = 0.4

# ── 业务类型关键词（模板匹配时的语义校验）──

_BUSINESS_KEYWORDS = {
    "bidding": ("投标", "标书", "评分", "打分", "中标", "废标", "bid"),
    "procurement": ("询价", "采购", "比价", "报价"),
    "document": ("生成", "写", "文档", "excel", "word", "ppt", "pdf"),
}

_BUSINESS_STEP_ACTIONS = {
    "bidding": ("bidding:", "bid_", "score", "generate"),
    "procurement": ("inquiry", "quote", "procurement"),
    "document": ("doc:", "generate", "word_", "excel_", "docx"),
}


def _template_mismatches_title(title: str, steps: list[dict] | None) -> bool:
    """模板步骤与标题业务类型不匹配 → 应跳过模板，重新 LLM 拆解。

    防止任务 255 错误模板（"写投标文件"→"发送询价"）被任务 256 通过
    Jaccard 相似度命中复用，导致投标需求再次被错误执行为询价流程。
    """
    if not steps or not title:
        return False
    title_lower = title.lower()
    expected = None
    for biz, keywords in _BUSINESS_KEYWORDS.items():
        if any(kw in title_lower for kw in keywords):
            expected = biz
            break
    if not expected:
        return False  # 无明确业务类型，不校验

    # 检查模板步骤的行动是否匹配业务类型
    step_text = " ".join(
        s.get("instruction", "") + " " + s.get("title", "")
        for s in steps
    ).lower()
    expected_actions = _BUSINESS_STEP_ACTIONS.get(expected, ())
    if any(act in step_text for act in expected_actions):
        return False  # 匹配，不拒绝

    logger.warning(
        "Template mismatch: title keywords → %s, but steps: %s",
        expected, step_text[:200],
    )
    return True  # 不匹配，拒绝模板





async def _match_workflow(title: str, description: str) -> tuple[int | None, list[dict] | None, str]:

    """匹配最佳 Workflow 模版，返回 (workflow_id, steps, source)。



    匹配策略：

      - 高匹配 (>0.7): 直接用模版 steps，不调 LLM

      - 中匹配 (0.4-0.7): 用模版作底 + LLM 微调

      - 无匹配: 返回 None，由调用方走 LLM 从零分解

    """

    text = f"{title} {description}"

    pool = await get_pool()

    async with pool.acquire() as conn:

        rows = await conn.fetch(

            f"SELECT workflow_id, name, description, definition, version "

            f"FROM {SCHEMA}.workflows ORDER BY updated_at DESC LIMIT 20"

        )



    best_id = None

    best_score = 0.0

    best_steps = None



    for row in rows:

        tpl_text = f"{row['name']} {row['description'] or ''}"

        score = _jaccard(text, tpl_text)

        if score > best_score:

            best_score = score

            best_id = row["workflow_id"]

            definition = row["definition"]

            if isinstance(definition, str):

                definition = json.loads(definition)

            best_steps = definition.get("steps", [])

            if best_steps and score >= MATCH_HIGH:

                break  # 够高了，不用继续



    if best_score >= MATCH_HIGH:

        if _template_mismatches_title(title, best_steps):
            logger.info("Template id=%s score=%.2f 业务类型不匹配，跳过", best_id, best_score)
        else:
            logger.info("Template matched: id=%s score=%.2f (high)", best_id, best_score)
            return best_id, best_steps, "template"

    elif best_score >= MATCH_MEDIUM:

        if _template_mismatches_title(title, best_steps):
            logger.info("Template id=%s score=%.2f (medium) 业务类型不匹配，跳过", best_id, best_score)
        else:
            logger.info("Template matched: id=%s score=%.2f (medium) — LLM refine", best_id, best_score)
            return best_id, best_steps, "hybrid"



    # 字符匹配不够 → 试 LLM 语义匹配

    if rows:

        sem_id, sem_steps = await _semantic_match_workflow(text, rows, best_steps)

        if sem_id:

            logger.info("Semantic template matched: id=%s", sem_id)

            return sem_id, sem_steps, "template"



    logger.info("No template matched (best=%.2f), falling back to LLM decompose", best_score)

    return None, None, "llm"





async def _semantic_match_workflow(text: str, rows, fallback_steps):

    """LLM 语义匹配: 判断任务是否属于某个已有 Workflow 的类型。"""

    candidates = "\n".join(f"#{r['workflow_id']}: {r['name']} — {r.get('description','')[:80]}" for r in rows[:10])

    prompt = (

        f"任务: {text[:200]}\n\n候选 Workflow:\n{candidates}\n\n"

        f"判断任务是否属于某个 Workflow 的类型。返回 JSON: {{\"workflow_id\": id 或 null, \"confidence\": 0.0-1.0}}\n"

        f"只有 confidence >= 0.7 才返回 id, 否则 workflow_id 为 null"

    )

    result = await _llm_call_json(prompt, "semantic_match", default=None,

                                   timeout=15, max_tokens=cfg.get_llm_max_tokens())

    if result and result.get("workflow_id"):

        wf_id = result["workflow_id"]

        if result.get("confidence", 0) >= 0.7:

            for r in rows:

                if r["workflow_id"] == wf_id:

                    definition = r["definition"]

                    if isinstance(definition, str):

                        definition = json.loads(definition)

                    return wf_id, definition.get("steps", [])

    # LLM 失败 → Jaccard fallback

    return _jaccard_fallback(text, rows, fallback_steps)





def _jaccard_fallback(text: str, rows, fallback_steps):

    """Jaccard 兜底匹配——LLM 不可用时的备选方案。"""

    if not rows:

        return None, None

    try:

        best = max(rows, key=lambda r: _jaccard(text, f"{r['name']} {r.get('description','')}"))

        if _jaccard(text, f"{best['name']} {best.get('description','')}") >= 0.5:

            definition = best["definition"]

            if isinstance(definition, str):

                definition = json.loads(definition)

            return best["workflow_id"], definition.get("steps", [])

    except ValueError:

        pass

    return None, None





# ── 知识预检索 ────────────────────────────────────────────



async def _inject_knowledge(task: dict, steps: list[dict], agent_id: str) -> list[dict]:

    """知识预检索：搜汇川 → 注入步骤 instruction"""

    if not get_knowledge_search_enabled():

        return []



    query = f"{task.get('title', '')} {task.get('description', '')}"

    knowledge = await search_knowledge(query, agent_category="")

    if not knowledge:

        return []



    same_cat = await search_same_category_experience(agent_id, query)

    knowledge.extend(same_cat)



    max_results = get_knowledge_search_max_results()

    for step in steps:

        prefix = "📚 企业知识库参考资料：\n"

        for i, k in enumerate(knowledge[:max_results], 1):

            prefix += f"{i}. [{k.get('title', '')}]({k.get('source', '')})"

            if k.get("summary"):

                prefix += f" — {k['summary'][:100]}"

            prefix += "\n"

        prefix += "\n请优先参考以上企业知识库内容。\n---\n"

        step["instruction"] = prefix + step["instruction"]

        if "criteria" not in step.get("quality_criteria", {}):

            if isinstance(step.get("quality_criteria"), dict):

                step["quality_criteria"]["criteria"] = []

        if isinstance(step.get("quality_criteria"), dict):

            step["quality_criteria"]["criteria"].append(

                f"参考知识库关键词：{query[:50]}"

            )



    return knowledge





# ── 任务创建 ──────────────────────────────────────────────



async def create_task(

    title: str,

    description: str,

    priority: str,

    created_by: str,

    steps: list[dict],

    acceptance_criteria: list[dict] | None = None,

    expected_outputs: list[str] | None = None,

    timeout_minutes: int | None = None,

    workflow_id: int | None = None,

    workflow_version: int | None = None,

    # v2 参数（全部可选）

    quality_criteria: list[dict] | None = None,

    auto_quality_confirm: bool = False,

    skip_clarity: bool = False,  # 秘书 probe 已高置信度识别意图时跳过模糊检测

) -> dict:

    """创建 Task + 所有 Steps，引擎自动判断简单/复杂模式



    四种模式（优先级从高到低）：

      1. workflow_id 存在 → 展开 workflow definition（steps 可选覆盖）

      2. steps 非空 → 使用调用方提供的 steps

      3. 两者皆空 → 模糊指令先澄清 → 匹配已有模版（高匹配直接用 / 中匹配微调 / 无匹配 LLM 分解）

      4. 模版匹配+LLM 混合

    """

    # 生成 trace_id 贯穿全链路

    trace_id = uuid.uuid4().hex[:16]

    matched_wf_id = None

    matched_source = "manual"

    if not steps and workflow_id is None:

        # 模糊指令检测 — 太短的描述先澄清再执行
        # 秘书 probe 已高置信度路由时跳过（skip_clarity=True）
        if skip_clarity:
            clarity = {"needs_clarification": False, "score": 1.0, "questions": []}
        else:
            clarity = await _check_clarity(title, description)

        if clarity.get("needs_clarification"):

            return {"success": False, "needs_clarification": True,

                    "score": clarity["score"], "questions": clarity["questions"]}



        matched_wf_id, matched_steps, source = await _match_workflow(title, description)

        if source == "template":

            steps = matched_steps

            workflow_id = matched_wf_id

            matched_source = "template"

        elif source == "hybrid":

            # 用模版作底 + LLM 微调

            steps, err = await llm_decompose_task(title, description)

            if err:

                # LLM 失败了，直接用模版

                steps = matched_steps

                workflow_id = matched_wf_id

                matched_source = "template_fallback"

            else:

                # 继承模板的 depends_on 链（LLM 生成的可能没依赖关系）

                _tpl_by_title = {s.get("title", ""): s for s in matched_steps}

                for s in steps:

                    tpl_s = _tpl_by_title.get(s.get("title", ""))

                    if tpl_s and tpl_s.get("depends_on"):

                        s["depends_on"] = tpl_s["depends_on"]

                matched_source = "hybrid"

                workflow_id = matched_wf_id

        else:

            steps, err = await llm_decompose_task(title, description)

            if err:

                return {"success": False, "error": f"自动分解失败: {err}"}

            if not steps:

                return {"success": False, "error": "自动分解失败：LLM 返回空步骤"}

            matched_source = "llm"



    pool = await get_pool()

    async with pool.acquire() as conn:

        async with conn.transaction():

            # ── 展开 Workflow ──

            if workflow_id is not None:

                await conn.execute(

                    f"UPDATE {SCHEMA}.workflows SET last_used_at = NOW(), "

                    f"use_count = use_count + 1, updated_at = NOW() "

                    f"WHERE workflow_id = $1", workflow_id,

                )

                wf = await _resolve_workflow(conn, workflow_id, workflow_version)

                wf_steps = wf["steps"]



                if not steps:

                    steps = wf_steps

                else:

                    wf_by_idx = {s["step_index"]: s for s in wf_steps}

                    for s in steps:

                        idx = s.get("step_index", 1)

                        wf_by_idx[idx] = s

                    steps = sorted(wf_by_idx.values(), key=lambda s: s.get("step_index", 1))



                if acceptance_criteria is None:

                    acceptance_criteria = wf["acceptance_criteria"]

                if timeout_minutes is None:

                    timeout_minutes = wf["timeout_minutes"]



            err = validate_depends_on(steps)

            if err:

                return {"success": False, "error": err}



            # 如果有 workflow，注入踩坑经验到步骤指令

            if workflow_id:

                wf_row = await conn.fetchrow(

                    f"SELECT definition FROM {SCHEMA}.workflows WHERE workflow_id = $1 ORDER BY version DESC LIMIT 1",

                    workflow_id,

                )

                if wf_row:

                    wf_def = wf_row["definition"]

                    if isinstance(wf_def, str):

                        wf_def = json.loads(wf_def)

                    if not wf_def:

                        wf_def = {}

                    gotchas = wf_def.get("_gotchas", [])

                    if gotchas:

                        for s in steps:

                            idx = s.get("step_index", 0)

                            # 2026-08-28 P1 修复：原变量名 title 遮蔽函数参数，
                            # 导致 INSERT tasks 的任务标题被改成最后一个 step 的标题
                            step_title = s.get("title", "")

                            matched = [g for g in gotchas if g.get("step_index") == idx or g.get("title") == step_title]

                            if matched:

                                warn = "⚠️ 踩坑经验（来自历史执行记录）:\n" + "\n".join(

                                    f"  - 问题: {g['error']}\n    修复: {g['fix']}" for g in matched

                                )

                                s["instruction"] = warn + "\n\n" + s["instruction"]



            mode = _mode_from_steps(len(steps))



            task = await conn.fetchrow(

                f"INSERT INTO {SCHEMA}.tasks "

                f"(title, description, priority, status, created_by, participants, "

                f"acceptance_criteria, expected_outputs, timeout_minutes, "

                f"workflow_id, workflow_version) "

                f"VALUES ($1,$2,$3,'pending',$4,$5,$6,$7,$8,$9,$10) RETURNING *",

                title, description, priority, created_by,

                [created_by],

                json.dumps(acceptance_criteria, ensure_ascii=False) if acceptance_criteria else None,

                json.dumps(expected_outputs, ensure_ascii=False) if expected_outputs else None,

                timeout_minutes,

                workflow_id, workflow_version,

            )

            task = dict(task)

            task_id = task["task_id"]



            # 知识预检索：注入到步骤上下文

            try:

                if get_knowledge_search_enabled() and steps:

                    await _inject_knowledge(task, steps, created_by)

            except Exception:

                logger.warning("知识预检索跳过（失败不阻塞任务创建）")



            step_records = []

            for s in steps:

                default_timeout = timeout_minutes or cfg.get_default_step_timeout()

                # P0-1 (9-2): exec_type 归一化——缺省/无效落 manual（原默认 shell）

                exec_type = _resolve_exec_type(s)

                row = await conn.fetchrow(

                    f"INSERT INTO {SCHEMA}.steps "

                    f"(task_id, step_index, title, instruction, exec_type, depends_on, "

                    f"acceptance_criteria, timeout_minutes, auto_retry, "

                    f"assigned_agent, "

                    f"quality_criteria, max_iterations, risk_level, confirmation_required, params) "

                    f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) RETURNING *",

                    task_id,

                    s["step_index"],

                    s["title"],

                    s["instruction"],

                    exec_type,

                    s.get("depends_on") or [],

                    json.dumps(s.get("acceptance_criteria"), ensure_ascii=False) if s.get("acceptance_criteria") else None,

                    s.get("timeout_minutes", default_timeout),

                    s.get("auto_retry", 0),

                    s.get("assigned_agent"),

                    json.dumps(s.get("quality_criteria"), ensure_ascii=False) if s.get("quality_criteria") else None,

                    s.get("max_iterations", 3),

                    s.get("risk_level", "low"),

                    s.get("confirmation_required", False),

                    json.dumps(s.get("params"), ensure_ascii=False) if s.get("params") else None,

                )

                step_records.append(dict(row))



            # Task → running

            await conn.execute(

                f"UPDATE {SCHEMA}.tasks SET status = 'running', started_at = NOW(), "

                f"updated_at = NOW() WHERE task_id = $1",

                task_id,

            )

            task["status"] = "running"



    _register_trace(task_id, trace_id)

    logger.info(f"[trace={trace_id}] Task created: {task_id} ({mode}, source={matched_source}) by {created_by} — {len(steps)} steps")

    return {

        "success": True,

        "trace_id": trace_id,

        "task_id": task_id,

        "mode": mode,

        "task": task,

        "steps": step_records,

        "source": matched_source,

        "matched_workflow_id": matched_wf_id,

    }





# ── GET /next — 原子分配 ─────────────────────────────────



async def get_next_step(task_id: int, caller_agent_id: str = "") -> dict:

    """找到下一个可执行 Step 并原子分配（pending → assigned）



    使用 FOR UPDATE SKIP LOCKED 保证并发下不会重复分配。

    """

    if not caller_agent_id:

        return {"found": False, "error": "agent_id is required", "task_status": "", "progress": "", "upcoming_steps": []}



    pool = await get_pool()

    async with pool.acquire() as conn:

        task = await sm.get_task(conn, task_id)

        if not task:

            return {"found": False, "error": "task not found", "task_status": "", "progress": "", "upcoming_steps": []}



        if task["status"] not in ("running", "pending"):

            return {"found": False, "error": f"task status is '{task['status']}'",

                    "task_status": task["status"], "progress": "", "upcoming_steps": []}



        async with conn.transaction():

            # 原子分配：找到依赖已满足的 pending Step

            row = await conn.fetchrow(

                f"UPDATE {SCHEMA}.steps SET status = 'assigned', "

                f"assigned_agent = $2, assigned_at = NOW(), updated_at = NOW() "

                f"WHERE step_id = ("

                f"  SELECT step_id FROM {SCHEMA}.steps "

                f"  WHERE task_id = $1 AND status = 'pending' "

                f"    AND (depends_on IS NULL OR NOT (depends_on && ARRAY("

                f"      SELECT step_index FROM {SCHEMA}.steps "

                f"      WHERE task_id = $1 AND status NOT IN ('completed', 'skipped')"

                f"    ))) "

                f"    AND (confirmation_required = false OR confirmed_by IS NOT NULL) "  # v2: 跳过未确认的高风险 Step

                f"  ORDER BY step_index LIMIT 1 "

                f"  FOR UPDATE SKIP LOCKED"

                f") RETURNING *",

                task_id, caller_agent_id,

            )



            if not row:

                # 检查是否全部完成

                steps = await sm.get_task_steps(conn, task_id)

                pending = [s for s in steps if s["status"] == "pending"]

                blocked_by_deps = [

                    s for s in pending

                    if s.get("depends_on") and any(

                        d in [

                            s2["step_index"] for s2 in steps

                            if s2["status"] not in ("completed", "skipped")

                        ]

                        for d in s["depends_on"]

                    )

                ]

                upcoming = [

                    {"step_index": s["step_index"], "title": s["title"], "status": s["status"]}

                    for s in steps if s["status"] not in ("completed", "skipped", "failed", "cancelled")

                ]



                return {

                    "found": False,

                    "task_id": task_id,

                    "task_status": task["status"],

                    "current_step": None,

                    "reason": "no_pending_steps" if not pending else "dependencies_not_met",

                    "blocked_steps": len(blocked_by_deps),

                    "progress": _progress_str(steps),

                    "upcoming_steps": upcoming[:5],

                }



            step = dict(row)

            retries_left = step.get("auto_retry", 0)



            steps = await sm.get_task_steps(conn, task_id)

            upcoming = [

                {"step_index": s["step_index"], "title": s["title"], "status": s["status"]}

                for s in steps

                if s["step_index"] > step["step_index"]

                and s["status"] not in ("completed", "skipped", "failed", "cancelled")

            ]



            # WS 主动推送给 Agent: 你被分配了 Step

            try:

                await zhice_ws.send_to(caller_agent_id, {

                    "type": "zhice:assigned",

                    "task_id": task_id,

                    "step_id": step["step_id"],

                    "step_index": step["step_index"],

                    "title": step["title"],

                    "instruction": step["instruction"],

                    "exec_type": step.get("exec_type", "shell"),

                    "retries_left": retries_left,

                    # v2 字段

                    "quality_criteria": _safe_jsonb(step.get("quality_criteria")),

                    "max_iterations": step.get("max_iterations", 3),

                })

            except Exception:

                pass



            return {

                "found": True,

                "task_id": task_id,

                "task_status": task["status"],

                "current_step": {

                    "step_id": step["step_id"],

                    "step_index": step["step_index"],

                    "title": step["title"],

                    "status": step["status"],

                    "instruction": step["instruction"],

                    "exec_type": step.get("exec_type", "shell"),

                    "acceptance_criteria": _safe_jsonb(step.get("acceptance_criteria")),

                    "auto_retry": step.get("auto_retry", 0),

                    "retries_left": retries_left,

                    "timeout_minutes": step.get("timeout_minutes"),

                    # v2 字段

                    "quality_criteria": _safe_jsonb(step.get("quality_criteria")),

                    "max_iterations": step.get("max_iterations", 3),

                    "risk_level": step.get("risk_level", "low"),

                    "confirmation_required": step.get("confirmation_required", False),

                },

                "context": _build_context(task, steps, dict(step)),

                "progress": _progress_str(steps),

                "upcoming_steps": upcoming[:5],

            }





def _progress_str(steps: list[dict]) -> str:

    total = len(steps)

    done = sum(1 for s in steps if s["status"] in ("completed", "skipped"))

    return f"{done}/{total} completed"





def _build_context(task: dict, steps: list[dict], current_step: dict) -> dict:

    """构建上下文：已完成步骤摘要 + 任务全局信息（v2 Phase 1）"""

    completed = [

        {"index": s["step_index"], "title": s["title"],

         "summary": (s.get("summary") or "")[:200]}

        for s in steps if s["status"] in ("completed", "skipped")

    ]

    return {

        "task_title": task.get("title", ""),

        "task_description": (task.get("description") or "")[:500],

        "completed_steps": completed,

        "total_steps": len(steps),

        "completed_count": len(completed),

    }





# ── Task 自动完结判定 ────────────────────────────────────



async def try_complete_task(task_id: int, conn=None) -> dict | None:

    """检查是否所有 Step 均为终态，是则触发 Task 完结



    如果传入 conn 则复用调用方事务，否则自行获取连接。



    Returns:

        {"action": "completed"|"failed", "task_id": int} or None

    """

    _close = conn is None

    if conn is None:

        pool = await get_pool()

        conn = await pool.acquire()

    try:

        async with conn.transaction():

            task = await sm.get_task(conn, task_id)

            if not task or task["status"] not in ("running", "pending"):

                return None



            if not await sm.all_steps_terminal(conn, task_id):

                return None



            steps = await sm.get_task_steps(conn, task_id)

            has_failed = any(s["status"] == "failed" for s in steps)



            if has_failed:

                # 级联取消依赖失败步骤的下游步骤

                failed_indices = {s["step_index"] for s in steps if s["status"] == "failed"}

                for s in steps:

                    if s["status"] == "pending" and s.get("depends_on"):

                        if any(d in failed_indices for d in s["depends_on"]):

                            await sm.step_skip(conn, s["step_id"])

                result = await sm.task_fail(conn, task_id, "存在失败的步骤")

                action = "failed"

                participants = task.get("participants") or []

                for pid in set([task["created_by"]] + participants):

                    await ws_notify(pid, "task_failed", {

                        "task_id": task_id,

                        "title": task["title"],

                        "reason": "存在失败的步骤",

                    })

            else:

                acceptance = task.get("acceptance_criteria")

                if acceptance:

                    check_inputs = {}

                    for s in steps:

                        if s.get("outputs"):

                            check_inputs.update(s["outputs"])

                    check_result = check_all(acceptance, check_inputs)

                    if not check_result["passed"]:

                        result = await sm.task_fail(conn, task_id, "Task 级验收不通过")

                        action = "failed"

                        return {"action": action, "task_id": task_id, "check_result": check_result}



                result = await sm.task_complete(conn, task_id, "所有步骤已完成")

                action = "completed"



            await sm.task_update_progress(conn, task_id)



            logger.info(f"Task {task_id} auto-{action}")



            # 事务完成后 fire-and-forget hooks（失败不回滚 task_complete）

            asyncio.create_task(_post_complete_hooks(task, steps, action, task_id))



            return {"action": action, "task_id": task_id}

    finally:

        if _close and conn:

            await conn.close()





async def _qc_analyze(conn, task_id: int, steps: list[dict]):

    """Task 完成时写入 quality stats（Phase C）"""

    try:

        total_iterations = 0

        has_qc = False

        for s in steps:

            it_log = _safe_jsonb(s.get("iteration_log"))

            if it_log:

                total_iterations += len(it_log)

            if s.get("quality_criteria"):

                has_qc = True



        if not has_qc and total_iterations == 0:

            return



        recheck_fails = await conn.fetchval(

            f"SELECT COUNT(*) FROM {SCHEMA}.verifications "

            f"WHERE task_id = $1 AND check_mode = 'engine_recheck' AND result = 'failed'",

            task_id,

        )



        # 收集 failure_patterns 摘要

        failure_patterns = None

        if total_iterations > 0:

            patterns = []

            for s in steps:

                it_log = _safe_jsonb(s.get("iteration_log"))

                if not it_log:

                    continue

                for rlog in it_log:

                    result = rlog.get("self_check_result", "")

                    if result and not result.startswith("全部通过"):

                        patterns.append({"step": s.get("step_index"), "detail": result[:150]})

            if patterns:

                failure_patterns = patterns[:10]  # 最多 10 条



        workflow_id = None

        if steps:

            # P2 (R11): zhice.steps 表无 workflow_id 列（schema 仅 task_id），
            # steps[0].get("workflow_id") 恒为 None → task_quality_stats.workflow_id
            # 永远写 NULL。workflow_id 实际在 zhice.tasks 上，改按 task_id 直查。
            workflow_id = await conn.fetchval(
                f"SELECT workflow_id FROM {SCHEMA}.tasks WHERE task_id = $1", task_id,
            )



        await conn.execute(

            f"INSERT INTO {SCHEMA}.task_quality_stats "

            f"(task_id, workflow_id, has_quality_criteria, iteration_count, "

            f"engine_recheck_fails, failure_patterns) "

            f"VALUES ($1, $2, $3, $4, $5, $6)",

            task_id, workflow_id, has_qc, total_iterations,

            recheck_fails or 0,

            json.dumps(failure_patterns) if failure_patterns else None,

        )

        logger.debug("Task %s quality stats written: iter=%d recheck=%d", task_id, total_iterations, recheck_fails or 0)

    except Exception:

        logger.exception("_qc_analyze failed for task %s", task_id)





async def _post_complete_hooks(task: dict, steps: list[dict], action: str, task_id: int):

    """Task 完结后的 fire-and-forget: WS通知 + 永恒 + 吸星 + 自动提炼 + 质量统计。"""

    pool = await get_pool()

    async with pool.acquire() as conn:

        if action == "completed":

            participants = task.get("participants") or []

            for pid in set([task["created_by"]] + participants):

                try:

                    await ws_notify(pid, "task_completed", {

                        "task_id": task_id, "title": task["title"],

                    })

                except Exception:

                    pass

        try:

            await _yongheng_integration(conn, task, steps, action)

        except Exception:

            pass

        if action == "completed":

            try:

                await _qc_analyze(conn, task_id, steps)  # ← Phase C: 质量统计

            except Exception:

                pass

            try:

                await _xixing_learn(conn, task, steps)

            except Exception:

                pass

            if not task.get("workflow_id"):

                try:

                    await _auto_extract_workflow(conn, task, steps)

                except Exception:

                    pass





async def _step_trajectory(conn, step: dict, agent_id: str, action: str):

    """Step 状态变更时写永恒 trajectory"""

    try:

        namespace = f"task:{step.get('task_id', '')}"

        await add_action(conn, namespace, {

            "agent_id": agent_id,

            "action": f"step_{action}",

            "detail": f"Step {step.get('step_index', '?')}: {step.get('title', '')} → {action}",

            "result": "",

        })

    except Exception:

        pass





async def _step_audit(conn, step: dict, agent_id: str, action: str):

    """Step 状态变更时写镇岳 audit_log"""

    try:

        await write_audit(conn, {

            "agent_id": agent_id,

            "action": f"zhice.step.{action}",

            "target_id": str(step.get("step_id", "")),

            "target_type": "step",

            "detail": {

                "task_id": step.get("task_id"),

                "step_index": step.get("step_index"),

                "title": step.get("title"),

                "from_status": step.get("status"),

                "to_action": action,

            },

            "severity": "low",

        })

    except Exception:

        pass





async def step_hooks(step: dict, agent_id: str, action: str):

    """Step 状态变更时统一触发所有 hook（yongheng trajectory + zhenyue audit）"""

    pool = await get_pool()

    async with pool.acquire() as conn:

        await _step_trajectory(conn, step, agent_id, action)

        await _step_audit(conn, step, agent_id, action)





async def _yongheng_integration(conn, task: dict, steps: list[dict], action: str):

    """Task 完结时写永恒 memory + trajectory"""

    try:




        namespace = f"task:{task['task_id']}"

        content = _build_task_memory(task, steps, action)[:MAX_TASK_MEMORY_LENGTH]

        await write_memory(conn, namespace, content, mem_type="episodic",

                           source="zhice", metadata={

                               "task_id": task["task_id"],

                               "status": action,

                               "title": task["title"],

                           })



        for s in steps:

            await add_action(conn, namespace, {

                "agent_id": s.get("assigned_agent", ""),

                "action": f"step_{s['status']}",

                "detail": f"Step {s['step_index']}: {s['title']} → {s['status']}",

                "result": (s.get("summary") or "")[:MAX_STEP_SUMMARY_LENGTH],

            })



        logger.info(f"[yongheng] Task {task['task_id']} memory + trajectory written")

    except Exception:

        logger.exception("[yongheng] write failed, ignoring")





async def _xixing_learn(conn, task: dict, steps: list[dict]):

    """Task 完成时通过吸星 API 提交 Workflow 骨架到知识进化管道



    走 POST /v1/xixing/agent/{agent_id}/learn（文档 §4.4），

    替代直接写 yongheng 表。

    """

    try:

        ok = await learn_workflow(

            agent_id=task["created_by"],

            task_title=task["title"],

            steps=steps,

            task_id=task["task_id"],

            acceptance_criteria=task.get("acceptance_criteria"),

        )

        if ok:

            logger.info(f"[xixing] Task {task['task_id']} workflow skeleton learned")

        else:

            logger.warning(f"[xixing] Task {task['task_id']} learn API returned non-ok")

    except Exception:

        logger.exception("[xixing] learn failed, ignoring")





async def _cluster_similar_tasks(conn, task: dict) -> list[int] | None:

    """用 LLM 聚类: 在已完成任务中找语义相似度 ≥0.7 的任务, 返回同类 task_id 列表。"""

    rows = await conn.fetch(

        f"SELECT task_id, title, description FROM {SCHEMA}.tasks "

        f"WHERE status = 'completed' AND workflow_id IS NULL AND task_id != $1 "

        f"ORDER BY completed_at DESC LIMIT 30",

        task["task_id"],

    )

    if len(rows) < 2:

        return None



    # 构建 prompt
    candidates = "\n".join(f"- #{r['task_id']}: {r['title']}" for r in rows)

    prompt = (
        f"判断以下任务是否属于同一业务类型（采购、部署、巡检、维护等）。\n"
        f"当前任务: {task['title']} — {task.get('description','')[:100]}\n\n"
        f"候选任务列表:\n{candidates}\n\n"
        f"请严格按照 JSON 格式输出（不要包含 markdown 代码围栏或额外说明文字）：\n"
        f'{{"category": "类型名", "task_ids": [整数列表], '
        f'"confidence": 0.0-1.0}}\n'
        f"只要 confidence >= 0.7 且 task_ids 至少 3 个(含当前)才返回, "
        f'否则返回 {{"task_ids": []}}'
    )

    result = await _llm_call_json(prompt, "cluster_similar_tasks", default=None,

                                   timeout=20, max_tokens=cfg.get_llm_max_tokens())

    if result is None:
        logger.warning("cluster_similar_tasks: LLM returned None or invalid JSON, falling back to Jaccard")

    tids = None

    if result:

        tids = result.get("task_ids", [])

        if result.get("confidence", 0) >= 0.7 and len(tids) >= 3:

            logger.info("Semantic cluster found: category=%s count=%d", result.get("category", "?"), len(tids))

            return tids



    # LLM 失败或 key 失效 → fallback: Jaccard 标题前缀匹配

    if not tids:

        cur_title = task["title"]

        similar = [r["task_id"] for r in rows if _jaccard(cur_title, r["title"]) >= 0.5]

        similar.append(task["task_id"])

        if len(similar) >= 3:

            logger.info("Jaccard fallback cluster: %d similar tasks", len(similar))

            return similar

    return None





async def _auto_extract_workflow(conn, task: dict, steps: list[dict]):

    """语义聚类自动提炼: 同类任务 ≥3 个 → 创建 Workflow 模板。"""

    try:

        similar_ids = await _cluster_similar_tasks(conn, task)

        if not similar_ids:

            return



        # 选代表性 Task 的标题作为模板名

        cat_name = f"同类任务模板-{len(similar_ids)}个"

        existing = await conn.fetchval(

            f"SELECT COUNT(*) FROM {SCHEMA}.workflows WHERE name = $1", cat_name,

        )

        if existing:

            return



        definition = {

            "steps": [

                {"step_index": s["step_index"], "title": s["title"],

                 "instruction": s["instruction"], "depends_on": s.get("depends_on"),

                 "timeout_minutes": s.get("timeout_minutes"),

                 "acceptance_criteria": s.get("acceptance_criteria"),

                 "quality_criteria": s.get("quality_criteria")}   # ← Phase B 保留 quality_criteria

                for s in steps

            ],

            "acceptance_criteria": task.get("acceptance_criteria"),

            "quality_criteria": task.get("quality_criteria"),     # ← Phase B 保留 quality_criteria

            "timeout_minutes": task.get("timeout_minutes"),

            "_clustered_from": similar_ids,

        }

        await conn.execute(

            f"INSERT INTO {SCHEMA}.workflows (name, description, version, definition, created_by, source_task_id) "

            f"VALUES ($1, $2, 1, $3, $4, $5)",

            cat_name, f"语义聚类: {len(similar_ids)} 个同类任务", json.dumps(definition, ensure_ascii=False),

            task["created_by"], task["task_id"],

        )

        # 批量回填 workflow_id
        # P2 (R11): 子查询可能因同名多版本（workflows.name 不唯一，UNIQUE(name,version)）
        # 返回多行而抛 "more than one row returned by a subquery"；原实现整段被外层
        # except 吞掉 → 回填静默失败。逐条 try/except + trace 日志，单条失败不吞整体，
        # 继续回填后续行；子查询 ORDER BY version DESC LIMIT 1 取最新版本，规避多行报错。
        for tid in similar_ids:
            try:
                await conn.execute(
                    f"UPDATE {SCHEMA}.tasks SET workflow_id = "
                    f"(SELECT workflow_id FROM {SCHEMA}.workflows WHERE name = $1 "
                    f"ORDER BY version DESC LIMIT 1) "
                    f"WHERE task_id = $2", cat_name, tid,
                )
            except Exception:
                logger.exception("[trace] auto_extract_workflow: 回填 workflow_id fail, task_id=%s", tid)
                continue

        logger.info("Auto-extracted workflow '%s' from %d semantically similar tasks", cat_name, len(similar_ids))

    except Exception:

        logger.exception("auto_extract_workflow failed")





# ── Workflow 进化提炼 ──────────────────────────────────



async def refine_workflow(workflow_id: int, min_completed: int = 2) -> dict:

    """从该 Workflow 的所有已完成 Task 中聚合数据，LLM 分析优化后生成新版本。



    Args:

        workflow_id: 要优化的模版 ID

        min_completed: 最少需要多少个已完成 Task 才触发优化



    Returns:

        {"refined": True, "new_version": int, "changes":[...], "suggested_timeouts":{...}}

        或 {"refined": False, "reason": "..."}

    """

    pool = await get_pool()

    async with pool.acquire() as conn:

        # 查已有模版

        wf = await conn.fetchrow(

            f"SELECT * FROM {SCHEMA}.workflows WHERE workflow_id = $1", workflow_id,

        )

        if not wf:

            return {"refined": False, "reason": "Workflow 不存在"}



        # 查该模版下所有已完成 Task

        tasks = await conn.fetch(

            f"SELECT t.* FROM {SCHEMA}.tasks t "

            f"WHERE t.workflow_id = $1 AND t.status = 'completed' "

            f"ORDER BY t.completed_at DESC LIMIT 50",

            workflow_id,

        )

        if len(tasks) < min_completed:

            return {"refined": False, "reason": f"已完成 Task 不足 ({len(tasks)} < {min_completed})"}



        # 收集每个 Step 的执行数据

        task_ids = [t["task_id"] for t in tasks]

        step_stats = {}  # step_index → [{timeout_minutes, failed, duration}]

        for tid in task_ids:

            steps = await sm.get_task_steps(conn, tid)

            for s in steps:

                idx = s["step_index"]

                if idx not in step_stats:

                    step_stats[idx] = []

                duration = None

                if s.get("started_at") and s.get("completed_at"):

                    duration = (s["completed_at"] - s["started_at"]).total_seconds() / 60

                step_stats[idx].append({

                    "title": s["title"],

                    "timeout_minutes": s.get("timeout_minutes", 30),

                    "failed": s["status"] == "failed",

                    "duration": duration,

                })



        # 构建 LLM 优化提示

        current_def = wf["definition"]

        if isinstance(current_def, str):

            current_def = json.loads(current_def)



        summary_lines = []

        total_failures = 0

        total_steps = 0

        all_stable = True

        for idx in sorted(step_stats.keys()):

            stats = step_stats[idx]

            durations = [s["duration"] for s in stats if s["duration"] is not None]

            failures = sum(1 for s in stats if s["failed"])

            avg_dur = sum(durations) / len(durations) if durations else 0

            timeout = stats[0]["timeout_minutes"]

            total_failures += failures

            total_steps += len(stats)

            # 判定稳定: 失败率 < 5% 且 平均耗时 < 超时 80%

            step_stable = (failures / len(stats)) < 0.05 and (avg_dur < timeout * 0.8)

            if not step_stable:

                all_stable = False

            summary_lines.append(

                f"Step {idx} '{stats[0]['title']}': "

                f"执行 {len(stats)} 次, 失败 {failures} 次 ({failures/len(stats)*100:.0f}%), "

                f"平均耗时 {avg_dur:.1f}min, 当前超时 {timeout}min"

                + (" ⚠️" if not step_stable else " ✅")

            )



        # 收集踩坑经验（失败→重试→成功的步骤）

        gotchas = []

        for tid in task_ids:

            steps = await sm.get_task_steps(conn, tid)

            for s in steps:

                if s["status"] == "completed" and s.get("outputs") and isinstance(s["outputs"], dict):

                    outs = s["outputs"]

                    if isinstance(outs, str):

                        try: outs = json.loads(outs)

                        except Exception: continue

                    if outs.get("retry_from_failure") and outs.get("gotcha_verified"):

                        gotchas.append({

                            "step_index": s["step_index"],

                            "title": s["title"],

                            "error": outs.get("failure_reason", "")[:200],

                            "fix": outs.get("failure_fix", "")[:200],

                        })



        # ── Phase B: 收集 quality_criteria 自检失败模式 ──

        qc_failures = {}

        for tid in task_ids:

            q_steps = await sm.get_task_steps(conn, tid)

            for qs in q_steps:

                it_log = _safe_jsonb(qs.get("iteration_log"))

                if not it_log:

                    continue

                for round_log in it_log:

                    fail_info = round_log.get("self_check_result", "")

                    if fail_info and not fail_info.startswith("全部通过"):

                        qc_failures[fail_info[:120]] = qc_failures.get(fail_info[:120], 0) + 1



        # 收敛判定: 所有步骤都稳定 + 之前已优化过 → 定版

        prev_refined = False

        if isinstance(current_def, dict) and current_def.get("_refined_at"):

            prev_refined = True



        if all_stable and prev_refined and total_failures / total_steps < 0.05:

            return {

                "refined": False,

                "reason": f"已收敛：{len(tasks)} 次执行，失败率 {total_failures/total_steps*100:.1f}%，无需进一步优化",

                "stable": True,

            }



        prompt_parts = [

            f"以下是 Workflow '{wf['name']}' 的 {len(tasks)} 次执行数据：\n\n",

            "\n".join(summary_lines),

            f"\n\n当前 Workflow 定义：\n{json.dumps(current_def, ensure_ascii=False, indent=2)}",

        ]



        # Phase B: 追加 quality_criteria 自检失败统计

        if qc_failures:

            qc_lines = "\n".join(

                f"- {desc}: {count}次"

                for desc, count in sorted(qc_failures.items(), key=lambda x: -x[1])[:5]

            )

            prompt_parts.append(

                f"\n\nquality_criteria 自检失败统计：\n{qc_lines}\n"

                "如果某条 quality_criteria 持续失败，请评估是否需要："

                "(1) 降级为 should/nice，(2) 拆细为更具体的检查项，(3) 新增补位规则"

            )



        prompt_parts.append(

            "\n\n请分析哪些步骤需要优化（超时延长、增加检查规则、拆分慢步骤等），"

            "只返回一个 JSON: {\"changes\": [\"建议1\", \"建议2\"], "

            "\"suggested_timeouts\": {step_index: timeout_minutes}, "

            "\"summary\": \"一句话总结\", "

            "\"qc_suggestions\": [{\"description\": \"标准描述\", "

            "\"action\": \"add|downgrade|upgrade|split\"}]}"

        )



        prompt = "".join(prompt_parts)



    # 调 LLM

    api_key = cfg.get_llm_api_key()

    if not api_key:

        return {"refined": False, "reason": "LLM API Key 未配置"}



    try:

        base_url = cfg.get_llm_base_url()

        model = cfg.get_llm_decompose_model()

        async with httpx.AsyncClient(timeout=30) as client:

            resp = await client.post(

                f"{base_url}/chat/completions",

                headers={"Authorization": f"Bearer {api_key}"},

                json={

                    "model": model,

                    "messages": [{"role": "user", "content": prompt}],

                    "max_tokens": 2000,

                    "temperature": 0.1,

                },

            )

            resp.raise_for_status()

            raw = resp.json()["choices"][0]["message"]["content"].strip()

            if raw.startswith("```"):

                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

                if raw.startswith("json"):

                    raw = raw[4:].strip()

        analysis = json.loads(raw)

    except Exception:

        logger.exception("LLM refine failed for workflow %s", workflow_id)

        return {"refined": False, "reason": "LLM 调用失败"}



    # LLM 也判断无优化空间 → 不生成新版本

    if not analysis.get("changes"):

        return {

            "refined": False,

            "reason": analysis.get("summary", "Workflow 已达最优，无需优化"),

            "stable": True,

        }



    # 生成新版本

    pool2 = await get_pool()

    async with pool2.acquire() as conn:

        new_ver = wf["version"] + 1

        new_def = dict(current_def)

        new_def["_refined_at"] = _now().isoformat()

        new_def["_refined_from"] = len(tasks)

        new_def["_changes"] = analysis.get("changes", [])

        if gotchas:

            new_def["_gotchas"] = gotchas



        row = await conn.fetchrow(

            f"INSERT INTO {SCHEMA}.workflows "

            f"(name, description, version, definition, created_by, source_task_id) "

            f"VALUES ($1, $2, $3, $4, $5, $6) RETURNING workflow_id, version",

            wf["name"], wf["description"], new_ver,

            json.dumps(new_def, ensure_ascii=False),

            wf["created_by"], wf.get("source_task_id"),

        )



    logger.info(f"Workflow {workflow_id} refined → v{new_ver}: {analysis.get('summary', '')}")

    return {

        "refined": True,

        "workflow_id": workflow_id,

        "new_version": new_ver,

        "source_tasks": len(tasks),

        "changes": analysis.get("changes", []),

        "suggested_timeouts": analysis.get("suggested_timeouts", {}),

        "summary": analysis.get("summary", ""),

    }

