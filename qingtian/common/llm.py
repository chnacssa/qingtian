"""公共 LLM 调用 — 按部门主备模型 + JSON 模式 + 重试 + 超时保护

支持：
  - 全局默认 LLM（无部门或部门未配置时兜底）
  - 按部门配置主备双模型（primary → backup 自动 failover）
  - 环境变量引用（${VAR_NAME}）自动替换

config.yaml 格式:
  departments:
    finance:
      llm:
        primary:
          provider: zhipu
          base_url: https://open.bigmodel.cn/api/paas/v4
          api_key: ${FINANCE_ZHIPU_KEY}
          model: glm-5.3-flash
        backup:
          provider: deepseek
          base_url: https://api.deepseek.com/v1
          api_key: ${FINANCE_DEEPSEEK_KEY}
          model: deepseek-v4-flash

2026-08-26 波哥指示：全局主模型切 zhipu glm-5.3-flash（OpenAI 兼容端点，支持
1M 上下文 + 多模态 image_url），deepseek-v4-flash 全局备用。环境变量：
主 ZHIPU_API_KEY，备 DEEPSEEK_API_KEY。
2026-08-27 波哥定调：厂商顺序统一读全局 FIRST_LLM / SECOND_LLM 环境变量
（值=zhipu/deepseek，须配对应 key；都没配时智谱优先），不再硬编码绑死——
详见 common/config.py _LLM_PROVIDER_PROFILES；config.yaml 显式配置仍最优先。
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass

import httpx

from common.config import (
    get,
    default_llm_model,
    default_llm_base_url,
    default_llm_provider,
    default_llm_key_var,
    default_llm_backup_profile,
)

logger = logging.getLogger("common.llm")


# ── LLM 配置 ──────────────────────────────────────────


@dataclass
class LLMConfig:
    provider: str = "zhipu"
    api_key: str = ""
    base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    # 2026-08-27 波哥定调：默认随 FIRST_LLM/SECOND_LLM 全局厂商顺序（智谱优先），
    # 见 common/config.py _LLM_PROVIDER_PROFILES；构造处基本都显式传值。
    model: str = "glm-5.3-flash"

    def is_valid(self) -> bool:
        return bool(self.api_key and self.base_url)


# ── 三档模型路由（P2 模型资源层） ──────────────────────────
# 2026-08-26 全量切后：三档默认同落全局 glm-5.3-flash（config.yaml 可按档覆盖），
# 备用统一 deepseek-v4-flash。历史豆包/qwen 分档词表仅用于路由判定。
# task_type="simple"    → 便宜快：分类/抽取/格式转换/闲聊
# task_type="precise"   → 精确（脱敏/翻译/文档分析，现状默认）
# task_type="reasoning" → 推理型（可配）：复杂推理/多步规划/竞标评分
# "chat" 为历史别名，归一化为 "simple"（调用方不感知，向后兼容）

TASK_SIMPLE = "simple"
TASK_PRECISE = "precise"
TASK_REASONING = "reasoning"
TASK_CHAT = "chat"                       # 兼容别名 → simple
_TASK_ALIASES = {"chat": TASK_SIMPLE}
_ALL_TASK_TYPES = (TASK_SIMPLE, TASK_PRECISE, TASK_REASONING)


def _normalize_task_type(task_type: str) -> str:
    """归一化任务类型（"chat" → "simple"）。"""
    return _TASK_ALIASES.get(task_type, task_type)


def _resolve_env(value: str) -> str:
    """替换 ${VAR_NAME} 为环境变量值。未配置的变量 → 空字符串。

    2026-08-06 fix: 原实现对未配变量返回字面 "${VAR}"，导致 api_key 非空、
    LLMConfig.is_valid() 判有效、带着无效 key 硬调 LLM 全部 401 → invalid_json。
    改为空串后，缺 key 的部门配置被 is_valid() 跳过，自动 fallback 到全局默认
    （DEEPSEEK_API_KEY），根治 semantic_probe 等通用 LLM 调用路由失效。
    """
    if not isinstance(value, str):
        return value

    def _replace(m):
        var = m.group(1)
        return os.environ.get(var, "")

    return re.sub(r"\$\{(\w+)\}", _replace, value)


def get_llm_config(department: str | None = None) -> LLMConfig:
    """读取部门 LLM 配置（含主备），无部门时返回全局默认。

    查找顺序: 部门 primary → 部门 backup → 全局默认
    """
    if department:
        dept_path = f"departments.{department}.llm"
        primary = get(f"{dept_path}.primary")
        if primary:
            return LLMConfig(
                provider=primary.get("provider", default_llm_provider()),
                api_key=_resolve_env(primary.get("api_key", "")),
                base_url=primary.get("base_url", default_llm_base_url()),
                model=primary.get("model", default_llm_model()),
            )

    # 全局默认（2026-08-27 波哥定调：随 FIRST_LLM/SECOND_LLM 全局厂商顺序，
    # 不硬编码绑死一家；config.yaml common.llm.* 显式配置仍最优先）
    return LLMConfig(
        provider=get("common.llm.provider", default_llm_provider()),
        api_key=os.getenv(default_llm_key_var(), get("common.llm.api_key", "")),
        base_url=get("common.llm.base_url", default_llm_base_url()),
        model=get("common.llm.model", default_llm_model()),
    )


def get_backup_config(department: str | None = None) -> LLMConfig | None:
    """读取备份 LLM 配置：部门 backup 或全局 common.llm.backup。

    2026-08-06 fix: 原实现 department=None 直接返回 None，全局默认路径
    （semantic_probe 等通用 LLM 调用）无 backup。deepseek 短 prompt 偶发空 content
    时无 fallback → invalid_json。新增全局 backup 支持：
      common.llm.backup: {provider: qwen, base_url: ..., api_key: ${VAR}, model: qwen-turbo}
    """
    if department:
        backup = get(f"departments.{department}.llm.backup")
        if backup:
            cfg = LLMConfig(
                provider=backup.get("provider", "deepseek"),
                api_key=_resolve_env(backup.get("api_key", "")),
                base_url=backup.get("base_url", ""),
                model=backup.get("model", ""),
            )
            return cfg if cfg.is_valid() else None
    # 全局 backup（common.llm.backup），主 provider 不稳时兜底。
    # 2026-08-27 波哥定调：默认随 FIRST_LLM/SECOND_LLM 全局厂商顺序——
    # SECOND_LLM 指定且配了 key 优先，否则另一家（配了 key 才有内置备用）。
    backup = get("common.llm.backup")
    bp = default_llm_backup_profile()
    if backup:
        cfg = LLMConfig(
            provider=backup.get("provider", (bp or {}).get("provider", "deepseek")),
            api_key=_resolve_env(backup.get("api_key", "")),
            base_url=backup.get("base_url", (bp or {}).get("base_url", "")),
            model=backup.get("model", (bp or {}).get("model", "")),
        )
        if cfg.is_valid():
            return cfg
    if bp:
        return LLMConfig(
            provider=bp["provider"],
            api_key=os.getenv(bp["key_var"], ""),
            base_url=bp["base_url"],
            model=bp["model"],
        )
    return None


# ── 按任务类型的双模型路由 ──────────────────────────────


def _read_task_model_config(path_prefix: str) -> LLMConfig | None:
    """从 config.yaml 读取按任务类型配置的模型。"""
    cfg = get(path_prefix)
    if not cfg:
        return None
    return LLMConfig(
        provider=cfg.get("provider", ""),
        api_key=_resolve_env(cfg.get("api_key", "")),
        base_url=cfg.get("base_url", ""),
        model=cfg.get("model", ""),
    )


def get_task_model_config(task_type: str) -> tuple[LLMConfig | None, LLMConfig | None]:
    """获取指定任务类型的三档模型配置 (primary, backup)。

    Args:
        task_type: TASK_SIMPLE / TASK_PRECISE / TASK_REASONING（"chat" 自动归一化为 simple）

    Returns:
        (primary, backup): primary 为目标档模型，backup 为其他档中第一个有效配置
        （simple↔precise 互备；reasoning 的 backup 落到 precise/simple）。
        reasoning 未配置时优雅降级 precise；均未配置时回退全局默认。
    """
    task_type = _normalize_task_type(task_type)
    if task_type not in _ALL_TASK_TYPES:
        raise ValueError(f"Unknown task_type: {task_type}")

    def _read(t: str) -> LLMConfig | None:
        cfg = _read_task_model_config(f"common.llm.{t}")
        # legacy 兼容：simple 未配时回退 common.llm.chat
        if t == TASK_SIMPLE and not cfg:
            cfg = _read_task_model_config("common.llm.chat")
        if cfg and not cfg.is_valid():
            logger.warning("config for task_type='%s' is invalid (empty api_key or base_url)", t)
            return None
        return cfg

    primary = _read(task_type)

    # reasoning 未配置 → 优雅降级 precise（design §4.2 验收，不报错）
    if task_type == TASK_REASONING and not primary:
        logger.warning("common.llm.reasoning 未配置，降级 precise")
        primary = _read(TASK_PRECISE)

    # backup：其他档中第一个有效配置（故障容灾）
    backup = None
    for other in _ALL_TASK_TYPES:
        if other == task_type:
            continue
        c = _read(other)
        if c:
            backup = c
            break

    # 均未配置 → 降级到全局默认
    if not primary and not backup:
        global_cfg = get_llm_config(None)
        if global_cfg and global_cfg.is_valid():
            return global_cfg, None
        return None, None

    return primary, backup


# ── LLM 调用 ──────────────────────────────────────────


async def llm_call_json(
    prompt: str,
    caller: str,
    default: dict | None = None,
    timeout: int = 60,
    max_tokens: int = 2048,
    temperature: float = 0,
    model: str | None = None,
    department: str | None = None,
    system_prompt: str = "",
    token_hook: callable = None,
) -> dict | None:
    """LLM 调用 + 主备 failover + 重试 + JSON 解析。

    DeepSeek 缓存优化：system_prompt 放在 system 角色中，
    固定部分被缓存后，后续调用只计费 user 内容差异部分。

    策略: 部门 primary → 部门 backup → 全局默认
    每级重试 2 次。

    Args:
        prompt: 用户 prompt（可变内容）
        caller: 调用方标识（日志用）
        default: 失败返回值
        timeout: HTTP 超时（秒）
        max_tokens: 最大输出 token
        temperature: LLM 温度
        model: 覆盖模型名
        department: 部门名（用于读取部门专属 key）
        system_prompt: 系统 prompt（固定部分，放 system 角色以命中缓存）
        token_hook: 可选回调，每次成功调用后调用 fn(caller, usage_dict)

    Returns:
        解析后的 JSON dict，失败返回 default
    """
    primary = get_llm_config(department)
    backup = get_backup_config(department)   # 2026-08-06: 支持全局 common.llm.backup 兜底
    fallback = get_llm_config(None)

    configs = [c for c in (primary, backup, fallback) if c and c.is_valid()]
    # 去重（可能 primary == fallback）
    seen = set()
    unique_configs = []
    for c in configs:
        key = (c.api_key, c.base_url, c.model)
        if key not in seen:
            seen.add(key)
            unique_configs.append(c)

    if not unique_configs:
        logger.warning("%s: LLM API Key not configured", caller)
        return default

    model_name = model or unique_configs[0].model
    last_error = None

    for cfg in unique_configs:
        for attempt in range(2):
            try:
                result, usage = await _call_llm(
                    cfg=cfg,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    model_name=model_name if cfg == unique_configs[0] else cfg.model,
                    timeout=timeout,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                if result is not None:
                    if token_hook and usage:
                        await token_hook(caller, usage)
                    return result
            except httpx.TimeoutException:
                last_error = "timeout"
                if attempt == 0:
                    logger.warning("%s: LLM timeout, retrying same cfg...", caller)
                    await asyncio.sleep(2)
                    continue
                logger.warning("%s: LLM timeout after retry, trying next cfg...", caller)
                break
            except LLMContentBudgetError:
                # 推理预算耗尽：同 cfg 重试大概率同样失败，直接换下一个 cfg 不烧 token
                last_error = "reasoning_budget_exhausted"
                break
            except json.JSONDecodeError:
                last_error = "invalid_json"
                if attempt == 0:
                    logger.warning("%s: JSON parse failed, retrying...", caller)
                    await asyncio.sleep(1)
                    continue
                break
            except Exception as e:
                last_error = str(e)[:100]
                if attempt == 0:
                    logger.warning("%s: LLM call failed, retrying...", caller)
                    await asyncio.sleep(1)
                    continue
                break

    logger.error("%s: all LLM configs exhausted, last error: %s", caller, last_error)
    return default if default is not None else None


async def _call_llm(
    cfg: LLMConfig,
    prompt: str,
    model_name: str,
    timeout: int,
    max_tokens: int,
    temperature: float,
    system_prompt: str = "",
) -> tuple[dict | None, dict | None]:
    """单次 LLM HTTP 调用。system_prompt 放 system 角色以命中 DeepSeek 缓存。

    Returns:
        (result_dict, usage_dict) — result 为解析后的 JSON，usage 含 token 计数
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    # DeepSeek 要求 prompt 含 "json" 字才允许 json_object 格式，否则 400
    user_prompt = prompt
    if "json" not in prompt.lower():
        user_prompt = prompt + "\nReturn JSON."
    messages.append({"role": "user", "content": user_prompt})

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{cfg.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_name,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                # 2026-08-09：不再强制 response_format json_object。
                # 实证：deepseek-v4-flash 在 json_object 模式下返回空 content（finish=length）
                # → json.loads("") 失败 → invalid_json（大师实测：不带 json_object + max_tokens=800
                # 走 _extract_json_block 兜底 → 完整 JSON finish=stop）。该兜底支持纯文本/markdown 提取。
                # 不改动 llm_chat（走 _call_llm_messages，另一条路径）。
            },
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage")
        content = data["choices"][0]["message"]["content"]
        # 2026-08-13：DeepSeek reasoning 复杂 JSON 偶发 content 空 + finish=length
        #（推理 token 占满 max_tokens）→ 重试大概率同样超预算，抛 LLMContentBudgetError
        # 快速失败，避免无效重试烧 token（小智/波哥 2026-08-13）。
        if data["choices"][0].get("finish_reason") == "length" and not content:
            raise LLMContentBudgetError(
                "LLM 推理预算耗尽: finish=length 且 content 为空", "", 0)

        json_block = _extract_json_block(content)
        if json_block:
            result = json.loads(json_block)
            if not isinstance(result, dict):
                return None, usage
            return result, usage
        return json.loads(content), usage


# ── 认知原语（设计文档 P1 §3.1） ──────────────────────────
# reasoning 三种模式：cot / react / self_consistency，默认 None 零侵入。
# 链路独立性：cot 只在 llm_chat 内生效（追加提示后走 _call_llm_messages，返回纯文本）；
#             react 只由 llm_call_react 使用（走 llm_call_json，返回 JSON 决策 dict + tokens）。
#             二者入口/返回类型/内部函数完全不同，无共享状态。

REASONING_COT = "cot"
REASONING_REACT = "react"
REASONING_SELF_CONSISTENCY = "self_consistency"

_COT_PROMPT = "\n请逐步思考，先列出推理过程再给出最终答案。"


async def llm_call_react(goal: str, history: list[dict], tools_desc: str,
                         system_prompt: str = "", caller: str = "cognition") -> dict | None:
    """ReAct 决策调用：LLM 返回 {'thought','action','action_input','tokens'}。

    供 CognitionRunner 使用（common/cognition.py）。走 llm_call_json（TASK 主备 + 重试）。
    历史记录按"思考→行动→观察"顺序拼成 prompt。
    tokens: 本调用消耗的 token 数（从 usage 提取），供 runner 预算判定。
    """
    prompt = (
        f"你是智能体认知引擎。根据目标与历史决策下一步动作。\n\n"
        f"可用动作：\n{tools_desc}\n\n"
        f"历史步骤：\n{json.dumps(history, ensure_ascii=False, default=str)}\n\n"
        f"当前目标：{goal}\n\n"
        "请输出 JSON（不要 markdown 代码块）："
        '{"thought": "推理", "action": "动作名", "action_input": {"参数": "值"}}'
    )
    usage_box: dict = {}

    def _capture(caller_, usage):
        usage_box["total"] = (usage.get("total_tokens") or
                              usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))

    result = await llm_call_json(
        prompt=prompt, caller=caller,
        default=None, max_tokens=3072, temperature=0,
        system_prompt=system_prompt, token_hook=_capture,
    )
    if result is not None:
        result["tokens"] = usage_box.get("total", 0)
    return result


async def llm_call_react_consistency(goal: str, history: list[dict], tools_desc: str,
                                     system_prompt: str = "", caller: str = "cognition",
                                     n: int = 3) -> dict | None:
    """自洽性 ReAct 决策：n 次 `llm_call_react` 独立采样 + 逐字段多数投票。

    返回与 `llm_call_react` 同结构的决策 dict（`tokens` 为 n 次采样实际消耗之和，
    供 CognitionRunner 预算判定）。可作为 `CognitionRunner(llm_call=...)` 注入，
    实现 reasoning=self_consistency 模式（设计文档 §4.3）。
    """
    from common.cognition import sample_consistency

    total_tokens = 0

    async def _sample(i: int) -> dict | None:
        nonlocal total_tokens
        r = await llm_call_react(goal, history, tools_desc, system_prompt, caller)
        if r is None:
            return None
        total_tokens += int(r.get("tokens", 0) or 0)
        r.pop("tokens", None)  # 成本字段不参与字段投票
        return r

    decision = await sample_consistency(_sample, n)
    if not decision:
        return None
    decision["tokens"] = total_tokens
    return decision


# ── JSON 提取 ──────────────────────────────────────────


def _extract_json_block(text: str) -> str | None:
    """提取最外层 JSON 块（支持花括号计数和 markdown 代码围栏）。"""

    # Step 1: 优先提取 markdown 代码围栏内的内容
    fence_pos = text.find("```")
    if fence_pos >= 0:
        end_fence = text.find("```", fence_pos + 3)
        if end_fence >= 0:
            inner = text[fence_pos + 3 : end_fence]
            # 去掉可选的 json 标注行
            nl = inner.find("\n")
            if nl >= 0:
                first_line = inner[:nl].strip().lower()
                if first_line in ("json", "python", "py"):
                    inner = inner[nl + 1 :]
            # 从围栏内找完整 JSON 块
            result = _extract_json_block(inner)
            if result is not None:
                return result

    # Step 2: 花括号计数提取
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# ── messages 格式的 LLM 调用（双模型路由） ────────────────

# 简单档关键词：命中走 simple（便宜快）。历史 DOUBAO_KEYWORDS 并入。
DOUBAO_KEYWORDS = frozenset({
    "总结", "归纳", "简报", "纪要", "会前",
    "性格", "人格", "话术",
    "推荐", "聊天", "早报", "汇报", "记录",
})
SIMPLE_TASK_KEYWORDS = DOUBAO_KEYWORDS | frozenset({
    "分类", "抽取", "格式化", "翻译", "脱敏", "转换", "重命名", "排序",
})

# 推理意图词：命中且输入较长 → reasoning 档
REASONING_INTENT_WORDS = frozenset({
    "分析", "规划", "决策", "推理", "评估", "评分", "对比", "论证",
    "方案", "策略", "权衡", "预测", "推演", "多步", "计划", "测算",
})
# 意图词命中但输入过短时不上 reasoning，防短句误路由（只命中精确档）
REASONING_MIN_LEN = 40


def _routing_mode() -> str:
    """路由模式：none | rule | llm。非法值回退 rule。"""
    mode = get("common.llm.routing.mode", "rule")
    if mode not in ("none", "rule", "llm"):
        return "rule"
    return mode


def _collect_user_text(messages: list) -> str:
    """拼接全部 user 角色文本（用于分档检测，不检 system prompt）。"""
    parts = []
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
    return "\n".join(parts)


def _detect_task_type(messages: list) -> str:
    """三档规则路由（routing.mode 控制，显式 task_type 优先于本函数）。

    rule（默认）: 意图词 + 长度 → reasoning；简单词表 → simple；其余 → precise。
    none: 恒 precise（关闭自动分档，行为退化为现状）。
    llm: 轻判未实现（二期），日志告警后降级 rule。
    """
    mode = _routing_mode()
    if mode == "none":
        return TASK_PRECISE
    if mode == "llm":
        logger.warning("routing.mode=llm 轻判尚未实现，当前降级为规则分档（rule）")
    text = _collect_user_text(messages)
    if not text:
        return TASK_PRECISE
    # 简单词优先：DOUBAO 词表是已验证的便宜任务词汇（总结/汇报/纪要等），
    # 命中即输出形态为摘要/整理，即便同时含"分析"等推理意图词也不上贵档。
    if any(kw in text for kw in SIMPLE_TASK_KEYWORDS):
        return TASK_SIMPLE
    # 纯推理意图（无简单词）+ 足够长 → reasoning
    if len(text) >= REASONING_MIN_LEN and any(w in text for w in REASONING_INTENT_WORDS):
        return TASK_REASONING
    return TASK_PRECISE


async def _call_llm_messages(
    cfg: LLMConfig,
    messages: list,
    max_tokens: int = 4096,
    temperature: float = 0,
    timeout: int = 60,
    usage_box: dict | None = None,
) -> str:
    """messages 格式的单次 LLM HTTP 调用。

    Args:
        cfg: 模型配置
        messages: OpenAI 消息列表 [{"role":"...","content":"..."}]
        max_tokens: 最大输出 token
        temperature: 温度
        timeout: 超时秒数
        usage_box: 可选 dict；成功后写入 usage_box["usage"]=usage（成本记账用）。
                   为 None 时不写（默认，保持既有行为与签名兼容）。

    Returns:
        LLM 回复文本

    Raises:
        httpx.TimeoutException: 超时
        httpx.HTTPStatusError: HTTP 错误状态码
        ValueError: API 返回格式异常
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{cfg.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": cfg.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        err = data.get("error", {}).get("message", str(data.get("error", data))[:200])
        raise ValueError(f"LLM API returned no choices: {err}")

    if usage_box is not None:
        usage_box["usage"] = data.get("usage") or {}
    return choices[0]["message"]["content"]


async def llm_chat(
    messages: list,
    task_type: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0,
    timeout: int = 60,
    caller: str = "llm_chat",
    reasoning: str | None = None,
    context_budget: int = 0,
    output_filter: callable = None,
    track_cost: bool = False,
    cost_hook: callable = None,
) -> str:
    """messages 格式的 LLM 调用，带三档路由 + 主备故障容灾。

    路由逻辑（按优先级）:
      1. task_type 显式指定 → 按指定路由（"chat" 自动归一化为 simple）
      2. 否则三档规则检测（意图词+长度 → reasoning；简单词表 → simple；其余 → precise）

    故障容灾:
      primary 超时/异常 → 自动切 backup 模型 → 全局默认兜底

    Args:
        messages: OpenAI 格式的消息列表
        task_type: TASK_SIMPLE / TASK_PRECISE / TASK_REASONING（"chat" 兼容）
        max_tokens: 最大输出 token
        temperature: 温度
        timeout: 超时秒数
        caller: 调用方标识（日志用）
        reasoning: None | "cot"。cot 在最后一条 user 消息追加逐步思考提示；
                   返回仍是 str（不改结构）。react 走 llm_call_react，不走本函数。
        context_budget: 上下文预算（估算 token）。>0 时超预算自动裁剪历史（保 system+尾部）；
                        <=0 关闭（默认，零侵入）。
        output_filter: 可选过滤钩子 filter(text) -> (ok, reason)。不通过抛 OutputFilterError，
                       触发 failover/重试，不计成功。
        track_cost: True 时按档计价记账（metrics cost gauge + 每日预算守卫）。可由
                    common.llm.routing.track_cost 配置兜底。
        cost_hook: 可选回调，成功后调 fn(model, cost)（业务侧审计/告警注入）。

    Returns:
        LLM 回复文本

    Raises:
        RuntimeError: 所有模型都失败
        BudgetExceededError: track_cost 开启且当日预算已超限（调用前即拒）
    """
    if task_type is None:
        task_type = _detect_task_type(messages)
    # 配置兜底：未显式开 track_cost 时读 common.llm.routing.track_cost
    _cfg_track = get("common.llm.routing.track_cost", False)
    if not isinstance(_cfg_track, bool):
        _cfg_track = str(_cfg_track).lower() in ("1", "true", "yes")
    track_cost = track_cost or _cfg_track

    # cot 链路：追加思考提示到最后一条 user 消息（不改返回结构）
    if reasoning == REASONING_COT:
        messages = list(messages)
        if messages and messages[-1].get("role") == "user":
            content = messages[-1]["content"]
            # 多模态 content 为 list[dict]，不能字符串拼接 → 跳过追加
            if isinstance(content, str):
                messages[-1]["content"] = content + _COT_PROMPT
    # 预算守卫：当日已超限 → 直接拒绝（不进入重试）
    if track_cost:
        _budget_precheck()
    # 上下文工程：超预算裁剪历史（opt-in）
    if context_budget and context_budget > 0:
        messages = _prune_messages(messages, context_budget)
    # 成本记账：仅 track_cost 时透传 usage_box（默认不传 → 兼容既有 mock/调用方）
    usage_box: dict | None = {} if track_cost else None

    primary, backup = get_task_model_config(task_type)
    if not primary and not backup:
        raise RuntimeError(f"LLM not configured for task_type='{task_type}'")

    import time as _t
    _start = _t.monotonic()
    _model = "unknown"
    last_error = None
    # primary 未配置时直接走 backup（如 precise(DeepSeek)未配 → 降级 simple(DashScope)）
    configs = [cfg for cfg in (primary, backup) if cfg is not None]

    def _mk_call_kw():
        kw = dict(max_tokens=max_tokens, temperature=temperature, timeout=timeout)
        if usage_box is not None:
            kw["usage_box"] = usage_box
        return kw

    try:
        for idx, cfg in enumerate(configs):
            if cfg is None:
                continue
            try:
                _model = cfg.model or "unknown"
                text = await _call_llm_messages(cfg=cfg, messages=messages, **_mk_call_kw())
                if output_filter is not None:
                    ok, reason = output_filter(text)
                    if not ok:
                        raise OutputFilterError(reason)
                return text
            except httpx.TimeoutException:
                last_error = "timeout"
                if idx < len(configs) - 1:
                    logger.warning("%s: timeout (cfg=%d), trying next...", caller, idx)
                    continue
                logger.error("%s: all configs timed out", caller)
            except Exception as e:
                last_error = str(e)[:100]
                if idx < len(configs) - 1:
                    logger.warning("%s: failed (cfg=%d, %s), trying next...", caller, idx, last_error)
                    continue

        # 最后的兜底
        try:
            global_cfg = get_llm_config(None)
            if global_cfg and global_cfg.is_valid():
                already_used = {(cfg.api_key, cfg.base_url, cfg.model) for cfg in configs if cfg}
                key = (global_cfg.api_key, global_cfg.base_url, global_cfg.model)
                if key not in already_used:
                    logger.warning("%s: primary/backup both failed, falling back to global default", caller)
                    _model = global_cfg.model or "unknown"
                    result = await _call_llm_messages(
                        cfg=global_cfg, messages=messages, **_mk_call_kw())
                    if output_filter is not None:
                        ok, reason = output_filter(result)
                        if not ok:
                            raise OutputFilterError(reason)
                    return result
        except Exception as e:
            last_error = str(e)[:100]
            logger.warning("%s: global fallback also failed: %s", caller, last_error)

        raise RuntimeError(
            f"LLM call failed, task_type='{task_type}', last error: {last_error}"
        )
    finally:
        _elapsed = (_t.monotonic() - _start) * 1000
        # 用统一的 token 估算器（str 化处理多模态 content=list[dict] 情况）
        _tok = sum(_estimate_tokens(str(m.get("content", "")))
                   for m in messages if isinstance(m, dict))
        from common.metrics import record_llm_call
        record_llm_call(_model, _tok, _elapsed)
        # 成本记账（仅 track_cost 且拿到了 usage）
        if track_cost and usage_box and usage_box.get("usage"):
            try:
                cost = _compute_cost(task_type, usage_box["usage"])
                if cost > 0:
                    from common.metrics import record_llm_cost
                    record_llm_cost(_model, cost)
                    _budget_spend(cost)
                    if cost_hook:
                        await cost_hook(_model, cost)
            except Exception as e:
                logger.warning("%s: 成本记账失败: %s", caller, e)


# ── 向后兼容 ──────────────────────────────────────────


def get_llm_api_key() -> str:
    """向后兼容：全局 LLM Key。"""
    return get_llm_config(None).api_key


def get_llm_base_url() -> str:
    """向后兼容：全局 LLM URL。"""
    return get_llm_config(None).base_url


def get_llm_model() -> str:
    """向后兼容：全局 LLM 模型。"""
    return get_llm_config(None).model


# ── 上下文工程（P2 §四） ──────────────────────────────────


def _estimate_tokens(text: str) -> int:
    """中英混合 token 估算（保守偏大）。空串 0。"""
    if not text:
        return 0
    # 中文 ≈ 1字/token，英文 ≈ 4字符/token；统一按 2 字符/token 估算
    return max(1, (len(text) + 1) // 2)


_MARK = "\n…[截断]…\n"


def _truncate_text(text: str, head_ratio: float = 0.6, tail_ratio: float = 0.2) -> str:
    """超长文本保头尾截断。无节省空间时原样返回。"""
    head = max(1, int(len(text) * head_ratio))
    tail = int(len(text) * tail_ratio)          # 允许 0（不保留尾部）
    if head + tail + len(_MARK) >= len(text):
        return text
    tail_part = text[-tail:] if tail > 0 else ""  # text[-0:] 会取全文，需显式判空
    return text[:head] + _MARK + tail_part


def _prune_messages(messages: list, budget: int) -> list:
    """上下文预算裁剪（opt-in）。budget <= 0 不裁剪。

    超预算时：保留全部 system + 最后一条消息完整；中部历史按新→旧补回，先丢最旧；
    仍超则截断最长中部消息，再截断 system（保头 60% + 尾 20%，无节省则丢弃）。
    返回新列表，不修改入参。
    """
    if not messages or budget <= 0:
        return messages

    def _est(msgs: list) -> int:
        return sum(_estimate_tokens(str(m.get("content", ""))) for m in msgs)

    if _est(messages) <= budget:
        return messages

    system_msgs = [m for m in messages if m.get("role") == "system"]
    tail_msgs = [messages[-1]]
    middle = messages[len(system_msgs):-1] if len(messages) > len(system_msgs) + 1 else []
    kept = list(system_msgs) + list(tail_msgs)

    # 从新到旧补回中部历史，直到预算接近（半条放不下则截断塞入，再不行就停）
    budget_headroom = budget - _est(kept)
    for m in reversed(middle):
        t = _estimate_tokens(str(m.get("content", "")))
        if t <= budget_headroom:
            kept.insert(len(system_msgs), m)
            budget_headroom -= t
        else:
            truncated = _truncate_text(str(m.get("content", "")), 0.4, 0.0)
            if _estimate_tokens(truncated) < t and _estimate_tokens(truncated) <= budget_headroom:
                kept.insert(len(system_msgs), {**m, "content": truncated})
                budget_headroom -= _estimate_tokens(truncated)
            break

    # 仍超预算：反复截断/丢弃最长的消息，直到达标（保证终止）
    while _est(kept) > budget and kept:
        idx = max(range(len(kept)), key=lambda i: _estimate_tokens(str(kept[i].get("content", ""))))
        m = kept[idx]
        content = m.get("content")
        if not isinstance(content, str) or len(content) < 20:
            kept.pop(idx)
            continue
        truncated = _truncate_text(content)
        if truncated == content:
            kept.pop(idx)
        else:
            kept[idx] = {**m, "content": truncated}
    return kept


# ── 输出过滤（P2 §五，护栏内容侧） ──────────────────────────


class LLMContentBudgetError(json.JSONDecodeError):
    """LLM 推理预算耗尽：finish=length 且 content 为空（2026-08-13）。

    DeepSeek reasoning 模型复杂 JSON 下推理 token 占满 max_tokens → content 空，
    重试大概率同样超预算——调用方捕获本异常应快速失败，不做无谓重试烧 token。
    JSONDecodeError 子类：兼容所有 except json.JSONDecodeError 的调用方。
    """


class OutputFilterError(Exception):
    """输出未通过过滤（触发 failover/重试，不计成功）。"""


DEFAULT_SENSITIVE_WORDS = frozenset({
    "password", "api_key", "apikey", "secret", "token",
    "authorization", "bearer ", "私钥", "密钥", "口令",
})


def make_output_filter(sensitive_words: list[str] | None = None,
                       max_chars: int = 8000) -> callable:
    """构造输出过滤钩子 filter(text) -> (ok, reason)。

    - 敏感词：DEFAULT_SENSITIVE_WORDS 并集自定义（小写子串匹配）
    - 超长：len(text) > max_chars 拦截（max_chars <= 0 表示不限制长度）
    """
    words = frozenset(w.lower() for w in DEFAULT_SENSITIVE_WORDS | frozenset(sensitive_words or []))

    def _filter(text: str):
        low = str(text).lower()
        for w in words:
            if w in low:
                return False, f"输出命中敏感词: {w}"
        if max_chars > 0 and len(text) > max_chars:
            return False, f"输出超长 {len(text)} > {max_chars}"
        return True, ""

    return _filter


# ── 成本感知（P2 §六） ──────────────────────────────────


_DEFAULT_PRICING = {
    TASK_SIMPLE:    {"per_1k_in": 0.002, "per_1k_out": 0.006},
    TASK_PRECISE:   {"per_1k_in": 0.002, "per_1k_out": 0.008},
    TASK_REASONING: {"per_1k_in": 0.004, "per_1k_out": 0.016},
}


def get_pricing() -> dict:
    """按档计价（元/1k token）。读 common.llm.pricing，未配置用内置默认。"""
    cfg = get("common.llm.pricing", {}) or {}
    merged = {}
    for tier, default in _DEFAULT_PRICING.items():
        c = cfg.get(tier, {}) if isinstance(cfg, dict) else {}
        merged[tier] = {
            "per_1k_in": float(c.get("per_1k_in", default["per_1k_in"])),
            "per_1k_out": float(c.get("per_1k_out", default["per_1k_out"])),
        }
    return merged


def _compute_cost(tier: str, usage: dict) -> float:
    """按档计价：(in*per_1k_in + out*per_1k_out)/1000。tier 未知时按 precise。"""
    pricing = get_pricing()
    p = pricing.get(_normalize_task_type(tier), pricing[TASK_PRECISE])
    in_tok = int(usage.get("prompt_tokens", 0) or 0)
    out_tok = int(usage.get("completion_tokens", 0) or 0)
    return (in_tok * p["per_1k_in"] + out_tok * p["per_1k_out"]) / 1000.0


class BudgetExceededError(RuntimeError):
    """当日 LLM 成本预算超限（调用前拒绝）。"""


# 每日预算账本（进程内单机；asyncio 单线程无需锁）
_daily_spent = {"date": "", "total": 0.0}


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


def _daily_budget_limit() -> float:
    try:
        return float(get("common.llm.pricing.daily_budget", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _budget_precheck() -> None:
    """调用前守卫：当日已花 ≥ 预算 → 抛 BudgetExceededError（0 = 不限）。"""
    limit = _daily_budget_limit()
    if limit <= 0:
        return
    if _daily_spent["date"] != _today():
        _daily_spent["date"] = _today()
        _daily_spent["total"] = 0.0
    if _daily_spent["total"] >= limit:
        raise BudgetExceededError(
            f"当日 LLM 预算已超限（已花 {_daily_spent['total']:.3f} 元，预算 {limit:.3f} 元）")


def _budget_spend(amount: float) -> None:
    """调用成功后累计当日花费。"""
    limit = _daily_budget_limit()
    if limit <= 0 or amount <= 0:
        return
    if _daily_spent["date"] != _today():
        _daily_spent["date"] = _today()
        _daily_spent["total"] = 0.0
    _daily_spent["total"] += amount


def reset_daily_budget_for_test() -> None:
    """测试用：重置进程内预算账本。"""
    _daily_spent["date"] = ""
    _daily_spent["total"] = 0.0


# ── LLM 并行化（P2 §七） ──────────────────────────────────


async def llm_batch(prompts: list[str], caller: str,
                    task_type: str = TASK_PRECISE, concurrency: int = 4,
                    **llm_kw) -> list[str | None]:
    """并发执行一批独立 LLM 调用（信号量限流）。

    Args:
        prompts: 一批独立 prompt（相互无依赖）
        caller: 调用方标识（日志/指标）
        task_type: 批量档位
        concurrency: 并发上限（默认 4，防打爆 rate limit）
        **llm_kw: 透传 llm_chat 的其余参数（max_tokens/context_budget/output_filter 等）

    Returns:
        与输入顺序对齐的结果列表；单项失败返回 None（不整体失败）。
    """
    if not prompts:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(i: int, prompt: str):
        async with sem:
            try:
                return await llm_chat(
                    [{"role": "user", "content": prompt}],
                    task_type=task_type, caller=f"{caller}.{i}", **llm_kw)
            except Exception as e:
                logger.warning("%s: 批内第 %d 项失败: %s", caller, i, e)
                return None

    return await asyncio.gather(*(_one(i, p) for i, p in enumerate(prompts)))
