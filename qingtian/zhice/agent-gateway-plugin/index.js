/**
 * 执策 Gateway Plugin v3.1 — OpenClaw v7.x 兼容
 *
 * OpenClaw v7 类型化 hooks（兼容 v6）:
 *   api.on("message_received", handler) — 消息入口
 *   api.on("before_tool_call", handler) — 工具调用门控（镇岳守卫用）
 *
 * v7 主要变化（2026.3.22 SDK）:
 *   - 插件入口推荐 definePluginEntry（module.exports 仍兼容，有迁移警告）
 *   - handler 签名从 (ctx) → (event, context)，本插件做了双向适配
 *   - config.yaml 独立于 OpenClaw 插件配置面板，插件启动时显式读取
 *
 * 业务逻辑（同 v3.0）:
 *   无 zhice_task_id → 创建 Task → 拉第一步 → 注入 Agent
 *   有 zhice_task_id → 自动拉下一步 → 闭环
 *   中断命令检测 → 取消当前 Task → 回到待命
 *   !!command!! 锚定 → 跨 Skill 路由
 */

// ── 文件系统工具（config.yaml 读取用）────────────

const fs = require('fs');
const path_ = require('path');

// ── 配置 ────────────────────────────────────────────
let config = {
  zhiceEndpoint: "http://127.0.0.1:1996/v1/zhice",
  apiToken: "",
  // 内部通道令牌：网关 A2 保护端点（/v1/huanyu/messages 等）凭 loopback+此头豁免
  // （2026-08-25 R11 A2 合入后无 token 调用全被 401，询价提示推送被拦）。
  // 留空时回退读环境变量 QINGTIAN_INTERNAL_IPC_TOKEN（与网关进程同值）。
  internalToken: "",
  // 多通道交付直发（波哥 2026-08-08 19:30）：飞书 OpenAPI 发引导消息，绕开 ctx.reply 发不出问题
  // 凭证来自 openclaw.json channels.feishu.accounts.<bot-id>；环境变量 FEISHU_APP_ID/FEISHU_APP_SECRET 兜底
  feishuAppId: "",
  feishuAppSecret: "",
  secretaryEnabled: true,
  secretaryEndpoint: "http://127.0.0.1:1996/api/v1/skills/work_secretary",
  excludePatterns: [
    "^(你好|hi|hello|在吗|在不在|早上好|晚上好|晚安|谢谢|再见|拜拜|ok|好的|嗯|哦|知道了)[\\s。.，,！!？?]*$",
  ],
  minInstructionLength: 10,
  enabledAgents: [],
  enforceStepByStep: true,
  interruptPatterns: [
    "^(取消|停止|中止|中断|停|stop|cancel|abort|reset|halt)([\\s。.，,！!？?]|$)",
    "^(别做了|不要做了|别干了|算了|不弄了)([\\s。.，,！!？?]|$)",
    "^(回到待命|重置|重新开始|restart|standby)",
    "!!取消!!",
  ],
  // 审批人白名单，空 = 不启用身份校验（向后兼容）
  approverIds: [],
  // 任务超时（分钟），超过此时间未完成自动取消，0 = 不启用
  taskTimeoutMinutes: 30,  // 30分钟未完成自动取消，防止僵尸任务阻塞通道
  // 消息追踪开关（生产环境关闭）
  traceEnabled: false,
  // agent 执行前拦截验证关键词（波哥 2026-08-20）：非空时，命中该关键词的消息被
  // before_dispatch（{handled:true} 丢弃）或 before_agent_run（{decision:block} 阻断）
  // 拦下，用于验证 7.1-2 官方钩子拦截契约；置空 = 钩子仅按内置规则拦截。
  // 注：原 before_route_inbound_message（#81061）被小智实测推翻（7.1-2 核心不认，never fires）
  beforeInterceptTestKeyword: "",
  // 三条业务线拦截总开关（波哥 2026-08-20）：投标/采购/销售消息在 before_dispatch 拦截 →
  // 直接走 bus 管线（probe→skillExecute HTTP）生成，返回 {handled:true} → agent 不跑 → 不推文件；
  // 其余消息放行 message_received 正常处理。false = 关闭三线拦截（仅 dump，agent 照跑）。回滚用。
  interceptBusinessLines: true,
  // 旧开关（superseded by interceptBusinessLines）：投标硬指令"生成标书/文件ID"现已并入
  // _businessLine，此键保留仅为向后兼容，代码不再读取。
  interceptBidCommands: true,
  // 通道身份 → 规范 agent 名 归一映射（修复：飞书 from.open_id 与 OpenClaw agent 名两个命名空间错配致 403）
  // 账号绑定时登记，示例: {"ou_772330": "bidding-feishu-2"}
  identityAliases: {},
};

let compiledExcludes = [];

function _trace(msg) {
  if (config.traceEnabled) console.log(`[trace] ${msg}`);
}

// ── 网关认证头（Bearer + 内部通道令牌）──────────
// A2 保护端点（/v1/huanyu/messages POST 等）二选一放行：有效 zhenyue Bearer 或
// loopback + X-Internal-Token（内部通道豁免）。插件无 zhenyue token，走后者。
function _gwAuthHeaders() {
  const h = {};
  if (config.apiToken) h["Authorization"] = `Bearer ${config.apiToken}`;
  const it = config.internalToken || process.env.QINGTIAN_INTERNAL_IPC_TOKEN || "";
  if (it) h["X-Internal-Token"] = it;
  return h;
}

// ── 原始用户身份规范化（2026-08-26 双草稿割裂修复）──────────
// 采购等 skill 的草稿按 user_id 定位（user_id or agent_id 兜底）。同一用户的请求
// 若身份形态漂移（ou_xxx 裸形态 / feishu:ou_xxx 前缀形态 / 规范 agent 名），
// 会落多张草稿互相看不到（线上实锤：bde6a450@feishu:ou_69c9 与
// 1b76bf17@procurement-feishu 同秒并存，回显旧单、用户新单丢失）。
// execute 请求统一带规范化 user_id（feishu:ou_xxx 形态，与既有草稿数据一致），
// 无论走哪条闸、agent_id 是什么形态，草稿恒按同一 user 定位。
function _canonicalUserId(rawId) {
  const s = String(rawId || "").trim();
  if (!s) return "";
  if (s.startsWith("feishu:")) return s;
  if (s.startsWith("ou_")) return `feishu:${s}`;
  return s;
}
let compiledInterrupts = [];

// ── 身份归一：通道身份(open_id) → 规范 OpenClaw agent 名 ──
// 同一实体在两个命名空间：飞书消息 from.open_id(=feishu:ou_xxx) vs OpenClaw agent 名(bidding-feishu-2)。
// 文件 owner 存 agent 名，execute 若带通道身份 → 下载精确比较 403。
// 解析顺序：identityAliases(静态映射) → 本地缓存(5min) → huanyu agent_channel_bindings(账号绑定流程动态维护)。
const _CHANNEL_ID_PREFIXES = ["feishu:", "dingtalk:", "wechat:", "slack:", "discord:"];
const _IDENTITY_CACHE_TTL_MS = 300000; // 5 分钟，避免每消息查库
const _identityCache = new Map(); // raw → { agentId, ts }

function _stripChannelPrefix(raw) {
  for (const p of _CHANNEL_ID_PREFIXES) {
    if (raw.toLowerCase().startsWith(p)) {
      return { channel: p.slice(0, -1), bare: raw.slice(p.length) };
    }
  }
  return { channel: "", bare: raw };
}

async function resolveCanonicalAgentId(raw) {
  if (!raw) return raw;
  const aliases = config.identityAliases || {};
  // 1) 原始值直接命中（键可为 "feishu:ou_xxx" 或 "ou_xxx"）
  if (aliases[raw]) return aliases[raw];
  const { channel, bare } = _stripChannelPrefix(raw);
  // 2) 剥离通道前缀再查（feishu:ou_xxx → ou_xxx）
  if (bare !== raw && aliases[bare]) return aliases[bare];
  // 3) 本地缓存
  const cached = _identityCache.get(raw);
  if (cached && Date.now() - cached.ts < _IDENTITY_CACHE_TTL_MS) return cached.agentId;
  // 4) HTTP 查 huanyu resolve（账号绑定流程写入的动态映射；失败/未命中保持原值）
  let resolved = raw;
  try {
    const base = config.zhiceEndpoint.replace(/\/v1\/zhice.*/, "");
    const q = new URLSearchParams({ channel_id: bare, channel });
    // 三段路径避开 api_compliance `/agents/{agent_id}` 动态段抢占（大师 2026-08-08 实测 404）
    const resp = await fetch(`${base}/v1/huanyu/agents/identity/resolve?${q.toString()}`);
    if (resp.ok) {
      const data = await resp.json();
      if (data && data.agent_id) resolved = data.agent_id;
    }
  } catch (_e) { /* 静默 */ }
  _identityCache.set(raw, { agentId: resolved, ts: Date.now() });
  if (resolved !== raw) console.log(`[zhice-gateway] identity resolve "${raw}" → "${resolved}"`);
  return resolved;
}

function loadConfig(pluginConfig) {
  if (pluginConfig) {
    config = { ...config, ...pluginConfig };
  }
  compiledExcludes = (config.excludePatterns || []).map(
    (p) => new RegExp(p, "i")
  );
  compiledInterrupts = (config.interruptPatterns || []).map(
    (p) => new RegExp(p, "i")
  );
  console.log(
    `[zhice-gateway v3.0] endpoint=${config.zhiceEndpoint}, ` +
    `secretary=${config.secretaryEnabled}, ` +
    `enforce=${config.enforceStepByStep}, ` +
    `excludes=${compiledExcludes.length}, ` +
    `interrupts=${compiledInterrupts.length}`
  );
}

// ── 闲聊判断 ──────────────────────────────────────────

function isCasualChat(text) {
  const trimmed = text.trim();
  if (!trimmed) return true;
  // 2026-08-31（波哥实锤"确认询价老是稍后尝试+block"）：短业务指令不是闲聊——
  // 长度闸（minInstructionLength=15）把 4 字确认短语（确认询价/确认下单）当闲聊
  // 放行 agent，skill 永远不执行、无签名 → 兜底闸拦截 → 用户永远"稍后重试"。
  // 业务线命中优先于长度闸（"下单"/"报价"等 2-4 字业务词同理受益）。
  if (_businessLine(trimmed)) return false;
  if (trimmed.length < config.minInstructionLength) return true;
  for (const re of compiledExcludes) {
    if (re.test(trimmed)) return true;
  }
  return false;
}

function isEnabledFor(agentId) {
  if (!config.enabledAgents || config.enabledAgents.length === 0) return true;
  return config.enabledAgents.includes(agentId);
}

// ── 执策 API ──────────────────────────────────────────

async function zhicePost(path, body) {
  try {
    const headers = { "Content-Type": "application/json", ..._gwAuthHeaders() };
    const resp = await fetch(`${config.zhiceEndpoint}${path}`, {
      method: "POST", headers, body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const errText = await resp.text().catch(() => "");
      console.error(`[zhice-gateway] POST ${path}: ${resp.status} ${errText.slice(0, 200)}`);
      return null;
    }
    return await resp.json();
  } catch (err) {
    console.error(`[zhice-gateway] POST ${path}: ${err.message}`);
    return null;
  }
}

async function zhiceGet(path) {
  try {
    const headers = { "Content-Type": "application/json", ..._gwAuthHeaders() };
    const resp = await fetch(`${config.zhiceEndpoint}${path}`, { headers });
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    return null;
  }
}

// ── 秘书 API ─────────────────────────────────────────

async function secretaryProbe(text) {
  if (!text) return null;
  // 最多 3 次尝试（1 次主调用 + 2 次重试），间隔 2s。冷启动/模型预热阶段
  // LLM 可能瞬时不可用，重试窗口期内大概率恢复，避免静默降级到执策导致任务断裂。
  const MAX_TRIES = 3;
  const RETRY_DELAY_MS = 2000;
  let lastError = null;
  for (let attempt = 1; attempt <= MAX_TRIES; attempt++) {
    try {
      const headers = { "Content-Type": "application/json", ..._gwAuthHeaders() };
      const resp = await fetch(`${config.secretaryEndpoint}/probe`, {
        method: "POST", headers, body: JSON.stringify({ action: text }),
      });
      if (resp.ok) {
        const result = await resp.json();
        if (attempt > 1) {
          console.log(`[zhice-gateway] Secretary probe recovered on attempt ${attempt}`);
        }
        return result;
      }
      lastError = `HTTP ${resp.status}`;
    } catch (err) {
      lastError = err.message;
    }
    if (attempt < MAX_TRIES) {
      console.log(`[zhice-gateway] Secretary probe attempt ${attempt} failed (${lastError}), retrying in ${RETRY_DELAY_MS}ms...`);
      await new Promise(r => setTimeout(r, RETRY_DELAY_MS));
    }
  }
  console.error(`[zhice-gateway] Secretary probe failed after ${MAX_TRIES} attempts: ${lastError}`);
  return null;
}

async function secretaryExecute(text, agentId) {
  try {
    const headers = { "Content-Type": "application/json", ..._gwAuthHeaders() };
    const resp = await fetch(`${config.secretaryEndpoint}/execute`, {
      method: "POST", headers,
      body: JSON.stringify({ agent_id: agentId, params: { action: text } }),
    });
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.error(`[zhice-gateway] Secretary execute failed: ${err.message}`);
    return null;
  }
}

function extractSecretaryReply(result) {
  if (!result) return null;
  if (result.passthrough === true) return null;
  // 文本类回复
  if (typeof result.text === "string" && result.text) return result.text;
  // 2026-08-30（小智 B 方向）：execute_api 幂等 in-flight 命中等场景返回
  // {"ok":true,"duplicate":true,"reply":"⏳ ..."}——reply 是给用户的明确文案，
  // 优先透传，否则会被下面 ok=true 分支吞掉渲染成通用"任务已提交"。
  if (typeof result.reply === "string" && result.reply) return result.reply;
  if (typeof result.data === "string" && result.data) return result.data;
  if (result.data && typeof result.data.text === "string" && result.data.text) return result.data.text;
  if (result.data && typeof result.data.content === "string" && result.data.content) return result.data.content;
  if (result.result && typeof result.result === "string" && result.result) return result.result;
  // 结构化 Skill 回复（如 procurement 返回 {"ok": true} 无 text 字段）
  // ok=true 表示 Skill 已接收处理，生成系统确认消息
  if (result.ok === true) {
    const data = result.data && typeof result.data === "object" ? result.data : result;
    // 2026-08-18 修复（问题4-3）：标书生成但 gen_status=needs_review（评审未达标/含待确认项）
    // → 不回复"✅ 已生成"，如实说明需登录系统查看完善后再交付，避免"假成功"误导。
    const genStatus = data.gen_status
      || (data.delivery && data.delivery.quality && data.delivery.quality.gen_status)
      || "";
    if (genStatus === "needs_review") {
      const missing = (data.delivery && data.delivery.quality && data.delivery.quality.missing) || [];
      const hint = (Array.isArray(missing) && missing.length) ? `（缺：${missing.slice(0, 5).join("、")}）` : "";
      return `⚠️ 标书已生成，但存在待确认项${hint}，请登录投标系统【我的标书】查看完善后再交付`;
    }
    const action = result.intent || result.action || "";
    const summary = result.summary || result.message || "任务已提交，正在处理中...";
    return `✅ ${summary}\n\n${action ? `⏳ ${action} 正在执行，请稍候...` : ""}`;
  }
  // 业务失败（ok=false 但 error 有文案）：把 skill 返回的真实原因透传给用户
  // （如"请先上传招标文件"），避免用户看到误导性的"服务暂时不可用"。
  // 只有真服务失败（null / 无回复 / 网络错）才走调用方的"服务暂时不可用"。
  if (result.ok === false && typeof result.error === "string" && result.error) {
    return `⚠️ ${result.error}`;
  }
  return null;
}

// ── 交付环节：skill 返回文件 → 引导用户去企业门户【我的标书】下载 ─────────
// 波哥 2026-08-19 确认：交付走门户任务栏下载，不主动下发文件到聊天。
// 大师 2026-08-08 曾加过"发 huanyu file 消息 → OpenClaw 调飞书 API 下发文件"
// （当时用户收不到文件，应急改飞书直发），与门户下载设计冲突，2026-08-19 已删除。
// 这里只用 result 的交付元数据（bid_generator 已补 file_id/file_name/download_url）
// 组引导文本（按通道直发 / ctx.reply 兜底），文件本体留在汇川/门户任务栏。
function extractSkillFile(result) {
  if (!result) return null;
  const data = result.data && typeof result.data === "object" ? result.data : result;
  const fileId = data.file_id || data.fileId || "";
  const fileName = data.file_name || data.filename || data.title || "";
  const downloadUrl = data.download_url || data.downloadUrl || "";
  if (!fileId && !downloadUrl) return null;
  return { file_id: fileId, file_name: fileName, download_url: downloadUrl };
}

async function deliverSkillFile(agentId, result, rawOpenId, channel) {
  const file = extractSkillFile(result);
  if (!file) return null;
  const data = result.data && typeof result.data === "object" ? result.data : result;
  // 2026-08-18 修复（问题4-3）：gen_status=needs_review 时交付物是待确认草稿，
  // 措辞不称"已生成"，如实提示完善后再交付，避免误导。
  const genStatus = data.gen_status
    || (data.delivery && data.delivery.quality && data.delivery.quality.gen_status)
    || "";
  const draft = genStatus === "needs_review";
  // 2026-08-19（波哥确认）：不再发 message_type:"file" 消息（此前 OpenClaw 消费后
  // 调飞书 API 把文件推给用户，与"门户任务栏下载"设计冲突）。文件仍留在汇川/门户
  // 任务栏（download_url 已上传），只发引导文本让用户去【我的标书】下载。
  _trace(`deliverSkillFile guide only: file_id="${file.file_id}" file_name="${file.file_name}" agent=${agentId}`);
  // 多通道公共消息层（波哥 2026-08-08 19:30 拍板）：skill 只负责生成 + 返回交付元数据，
  // 引导消息由公共层按通道直发（飞书 OpenAPI，绕开 ctx.reply 发不出问题）；文件大走网页版下载。
  const sizeText = await _probeFileSize(file.download_url);
  // 一步交付（波哥 2026-08-27）：草稿版同条消息带缺料/待确认清单（quality.missing 与
  // bidding _completion_notice 同源），用户一步知道补什么；不再只说"含待确认项"。
  const qMissing = (data.delivery && data.delivery.quality && Array.isArray(data.delivery.quality.missing))
    ? data.delivery.quality.missing : [];
  const missHint = qMissing.length ? `，待补充：${qMissing.slice(0, 5).join("；")}` : "";
  const guide = draft
    ? `⚠️ 标书草稿已生成：《${file.file_name || "（未命名）"}》${sizeText}，含待确认项${missHint}，请登录投标系统【我的标书】下载完善后再交付。`
    : `📄 你的标书已生成：《${file.file_name || "（未命名）"}》${sizeText}，请登录投标系统网页版，在【我的标书】中下载。`;
  if (await _deliverGuideByChannel(guide, rawOpenId, channel)) {
    _trace(`deliverSkillFile guide sent via channel api open_id="${rawOpenId}" channel="${channel || "-"}"`);
    return null;  // 通道直发成功，不重复走 ctx.reply
  }
  // 直发不可用（无身份/无凭据/API 失败/非飞书通道）→ 兜底：随 skill 回复走 ctx.reply
  _trace(`deliverSkillFile guide fallback ctx.reply channel="${channel || "-"}"`);
  return `\n\n${guide}`;
}

// 探测 download_url 文件大小（HEAD），失败/不可达时降级不显示
async function _probeFileSize(downloadUrl) {
  if (!downloadUrl) return "";
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 3000);
    const resp = await fetch(downloadUrl, { method: "HEAD", signal: ctl.signal });
    clearTimeout(timer);
    const len = Number(resp.headers.get("content-length") || 0);
    if (resp.ok && len > 0) return `（${_formatSize(len)}）`;
  } catch (_) { /* 静默降级，不影响引导文本 */ }
  return "";
}

function _formatSize(bytes) {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)}KB`;
  return `${bytes}B`;
}

// ── 多通道交付直发层（波哥 2026-08-08 19:30）──
// 识别会话通道 → 调对应通道 OpenAPI 发引导消息；飞书已实现，微信预留接口，其他预留
// 返回 true = 已直发成功（调用方不再走 ctx.reply）；false = 直发不可用（调用方兜底）
// 2026-08-30（小智 23:45 报回复丢失）：失败原因写入 _lastDirectSendErr——凭据缺失
// 此前完全静默，排障无从下手。
let _lastDirectSendErr = "";
async function _deliverGuideByChannel(text, rawOpenId, channel) {
  _lastDirectSendErr = "";
  const openId = _bareChannelId(rawOpenId);
  const ch = (channel || "").toLowerCase();
  const isFeishu = /feishu|lark/.test(ch) || (openId && openId.startsWith("ou_"));
  const isWeixin = /weixin|wechat|wx/.test(ch);
  if (!openId) { _lastDirectSendErr = "empty-open-id"; return false; }
  if (isFeishu) return await _feishuSendText(openId, text);
  if (isWeixin) {
    // 微信通道预留：接入后按微信 API 实现（先留接口，返回 false 走 ctx.reply 兜底）
    console.log(`[zhice-gateway] deliver weixin channel not implemented, fallback. open_id="${openId}"`);
    _lastDirectSendErr = "weixin-not-implemented";
    return false;
  }
  _lastDirectSendErr = `channel-unrecognized(ch="${ch}")`;
  return false;
}

// 剥离通道前缀（feishu:ou_xxx → ou_xxx），无前缀原样返回
function _bareChannelId(raw) {
  if (!raw) return "";
  const { bare } = _stripChannelPrefix(String(raw));
  return bare;
}

// ── 飞书直发（OpenAPI，大师 2026-08-08 19:30 实测 code=0 可达用户）──
let _feishuToken = "";
let _feishuTokenExp = 0;

async function _feishuGetTenantToken() {
  const appId = config.feishuAppId || process.env.FEISHU_APP_ID || "";
  const appSecret = config.feishuAppSecret || process.env.FEISHU_APP_SECRET || "";
  if (!appId || !appSecret) { _lastDirectSendErr = "no-credentials(config.feishuAppId/FEISHU_APP_ID 均缺)"; return ""; }
  if (_feishuToken && Date.now() < _feishuTokenExp - 60000) return _feishuToken;
  try {
    const resp = await fetch("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ app_id: appId, app_secret: appSecret }),
    });
    const data = await resp.json();
    if (data.code === 0 && data.tenant_access_token) {
      _feishuToken = data.tenant_access_token;
      _feishuTokenExp = Date.now() + (data.expire || 7200) * 1000;
      return _feishuToken;
    }
    _lastDirectSendErr = `token-failed(code=${data.code} msg=${data.msg})`;
    console.error(`[zhice-gateway] feishu token failed: code=${data.code} msg=${data.msg}`);
  } catch (err) {
    _lastDirectSendErr = `token-error(${err.message})`;
    console.error(`[zhice-gateway] feishu token error: ${err.message}`);
  }
  return "";
}

async function _feishuSendText(openId, text) {
  if (!openId) return false;
  const token = await _feishuGetTenantToken();
  if (!token) return false;
  try {
    const resp = await fetch("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${token}`,
      },
      body: JSON.stringify({
        receive_id: openId,
        msg_type: "text",
        content: JSON.stringify({ text }),
      }),
    });
    const data = await resp.json();
    if (data.code === 0) {
      _trace(`feishu_send_text open_id="${openId}" len=${text.length} code=0`);
      return true;
    }
    _lastDirectSendErr = `send-failed(code=${data.code} msg=${data.msg})`;
    console.error(`[zhice-gateway] feishu send failed: code=${data.code} msg=${data.msg}`);
  } catch (err) {
    _lastDirectSendErr = `send-error(${err.message})`;
    console.error(`[zhice-gateway] feishu send error: ${err.message}`);
  }
  return false;
}

// ── !!command!! 检测（锚定安全）────────────────────────

function extractCommand(text) {
  if (!text) return null;
  // 全半角归一
  let t = text.replace(/！/g, '!');
  // 锚定匹配：消息开头或 @秘书 后
  const m = t.match(/(?:^|@秘书\s*)!!([\w\u4e00-\u9fff]+)!!/);
  return m ? m[1] : null;
}

// ── 通用 Skill 执行（!!command!! 跨 Skill 路由用）────

async function skillExecute(skillName, text, agentId, extractedParams, targetAction, senderId = "") {
  try {
    // #1 防御：空 agent_id 不允许发出（避免子进程 --agent-id="" 落通用目录/拉不起）
    const safeAgentId = agentId || "gateway:unidentified";
    if (!agentId) console.warn(`[zhice-gateway] skillExecute(${skillName}) agent_id empty → fallback "${safeAgentId}"`);
    // 2026-08-26 双草稿割裂修复：execute 统一带规范化 user_id（原始通道身份），
    // skill 侧草稿按 user_id 定位（user_id or agent_id 兜底），不受 agent_id 形态影响。
    const userId = _canonicalUserId(senderId);
    const headers = { "Content-Type": "application/json", ..._gwAuthHeaders() };
    const baseUrl = config.secretaryEndpoint.replace(/\/work_secretary\/?$/, "");
    // probe 提供了 targetAction → 传标准 {action, payload} 格式
    // 仅有结构化参数 → 展开给 Skill
    // 都没有 → 传 query 通用语义
    let params;
    if (targetAction) {
      // LLM 提取的参数 + 兜底：关键字段为空时用原始文本填充
      const payload = { ...(extractedParams || {}), _raw_text: text };
      if (!payload.product && !payload.product_name) {
        payload.product = text;  // inquiry_create 要求 product 不能为空
      }
      params = { action: targetAction, payload };
    } else if (extractedParams && Object.keys(extractedParams).length > 0) {
      params = { ...extractedParams, _raw_text: text };
    } else {
      params = { action: "execute", payload: { query: text, _raw_text: text } };
    }
    _trace(`skillExecute send skill=${skillName} body_agent="${safeAgentId}" raw_agent="${agentId}" user="${userId}" action="${(params.action||"")}" payload_keys=[${params.payload ? Object.keys(params.payload).join(",") : ""}]`);
    const resp = await fetch(`${baseUrl}/${encodeURIComponent(skillName)}/execute`, {
      method: "POST", headers,
      body: JSON.stringify({ agent_id: safeAgentId, ...(userId ? { user_id: userId } : {}), params }),
    });
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.error(`[zhice-gateway] skillExecute ${skillName} failed: ${err.message}`);
    return null;
  }
}

// ── 进度播报（长任务每 10s 汇报进展，通用层，所有执策任务自动受益）──

const _progressTimers = {};
const PROGRESS_INTERVAL = 10000;  // 10 秒

// 进度消息幂等键：按 (agentId, taskId, message) 派生，同文本重复投递 → 同键 → 下游去重
// （2026-08-11 大师实锤：进度消息重复投递叠加语义路由误判 → 投标生成死循环）。
function _progressIdemKey(agentId, taskId, message) {
  let s = `progress:${agentId}:${taskId}:${message}`;
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  }
  return "prog_" + (h >>> 0).toString(16).padStart(8, "0");
}

async function sendProgressToUser(agentId, taskId, message) {
  try {
    const headers = { "Content-Type": "application/json", ..._gwAuthHeaders() };
    await fetch(`${config.zhiceEndpoint.replace(/\/v1\/zhice.*$/, "")}/v1/huanyu/messages`, {
      method: "POST", headers,
      body: JSON.stringify({
        from_agent: "agent:sys-eng",
        to_agent: agentId,
        message_type: "info",
        payload: { text: message, task_id: taskId, type: "progress", kind: "progress", source: "work" },
        idempotency_key: _progressIdemKey(agentId, taskId, message),
      }),
    });
  } catch (err) {
    // 静默失败，进度播报是辅助功能
  }
}

async function checkTaskProgress(taskId, agentId) {
  try {
    const task = await zhiceGet(`/tasks/${taskId}?agent_id=${encodeURIComponent(agentId)}`);
    if (!task) return null;
    const done = task.completed_steps || 0;
    const total = task.total_steps || 0;
    const status = task.status || "";
    const mode = task.mode || "";
    // 取当前步骤的 heartbeat_progress 作为进度细节（Skill 侧通过心跳上报）
    let progressDetail = "";
    const steps = task.steps || [];
    for (const s of steps) {
      if (s.status === "in_progress") {
        const out = s.outputs || {};
        const hp = out.heartbeat_progress || "";
        if (hp) progressDetail = hp;
        break;
      }
    }
    return { done, total, status, mode, progressDetail };
  } catch (err) {
    return null;
  }
}

function startProgressReport(taskId, agentId, ctx) {
  if (_progressTimers[taskId]) return;
  let lastDone = 0;
  let lastDetail = "";
  let tick = 0;

  _progressTimers[taskId] = setInterval(async () => {
    const prog = await checkTaskProgress(taskId, agentId);
    if (!prog) { stopProgressReport(taskId); return; }

    // 任务完成 → 停止
    if (prog.status === "completed" || prog.status === "failed" || prog.status === "cancelled") {
      stopProgressReport(taskId);
      return;
    }

    tick++;
    const stepChanged = prog.done > lastDone;
    const detailChanged = prog.progressDetail && prog.progressDetail !== lastDetail;

    // 播报条件：步骤推进 / 进度细节变化 / 每隔 6 轮（~60s）兜底保活
    if (stepChanged || detailChanged || tick % 6 === 0) {
      if (stepChanged) lastDone = prog.done;
      if (detailChanged) lastDetail = prog.progressDetail;

      const pct = prog.total ? Math.round((prog.done / prog.total) * 100) : 0;
      const base = `⏳ 正在处理中...（${prog.done}/${prog.total} 步, ${pct}%）`;
      const detail = prog.progressDetail ? `\n   📋 ${prog.progressDetail}` : "";
      const msg = base + detail;
      await sendProgressToUser(agentId, taskId, msg);
    }
  }, PROGRESS_INTERVAL);
}

function stopProgressReport(taskId) {
  if (_progressTimers[taskId]) {
    clearInterval(_progressTimers[taskId]);
    delete _progressTimers[taskId];
  }
}


// ── 秘书处理路径（拆出独立函数提升可读性）────────────

// ── 新 agent 自动注册到寰宇目录 ──
const _registeredCache = new Set();

async function _ensureAgentRegistered(agentId, text) {
  if (!agentId || _registeredCache.has(agentId)) return;
  try {
    const base = config.zhiceEndpoint.replace(/\/v1\/zhice.*/, "");
    // 先查是否已注册
    const check = await fetch(`${base}/v1/huanyu/agents/${encodeURIComponent(agentId)}`);
    if (check.ok) { _registeredCache.add(agentId); return; }
    // 未注册 → 自动注册
    const cat = text && (text.includes("投标") || text.includes("标书") || text.includes("评分"))
      ? "biz:buyer" : "biz:buyer";
    await fetch(`${base}/v1/huanyu/agents/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: agentId, category: cat, agent_id: agentId }),
    });
    _registeredCache.add(agentId);
    console.log(`[zhice-gateway] auto-registered agent: ${agentId} (${cat})`);
  } catch (_) { /* 静默失败，不阻塞消息处理 */ }
}


// ── 记忆恢复双通道（找回记忆，2026-08-16 波哥定调）────────────
// work_secretary recover_memory 返回 {data:{text, _memory_recover:true}}，
// 识别标记后：① 写 ${agent_workspace}/MEMORY.md（持久化）② 注入 ctx.message.text 放行 agent（本回合恢复）。
function _isMemoryRecover(result) {
  return !!(result && result.data && result.data._memory_recover === true);
}

// 解析 agent workspace：OPENCLAW_CONFIG 环境变量优先，兜底 /root/.openclaw/openclaw.json
// （restore.py 同款：agents.list[].workspace 按 id 匹配）
function _resolveAgentWorkspace(agentId) {
  try {
    const cfgPath = process.env.OPENCLAW_CONFIG || "/root/.openclaw/openclaw.json";
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
    const agents = Array.isArray(cfg.agents) ? cfg.agents : (cfg.agents && cfg.agents.list) || [];
    const a = agents.find(x => String(x.id) === String(agentId));
    return (a && a.workspace) ? a.workspace : null;
  } catch (e) {
    _trace(`memory-recover: resolve workspace failed: ${e.message}`);
    return null;
  }
}

// 追加「记忆恢复」块到 MEMORY.md（带日期头，整体限长 64KB 防无限膨胀；失败仅 trace 不阻塞）
async function _persistRecoveredMemory(agentId, memoryText) {
  try {
    const ws = _resolveAgentWorkspace(agentId);
    if (!ws) {
      _trace(`memory-recover: no workspace for ${agentId}, skip MEMORY.md write`);
      return false;
    }
    const memPath = path_.join(ws, "MEMORY.md");
    const stamp = new Date().toISOString().slice(0, 16);
    const block = `\n## [${stamp}] 记忆恢复（用户触发「找回记忆」）\n\n${memoryText}\n`;
    let existing = "";
    try { existing = fs.readFileSync(memPath, "utf8"); } catch (_) { /* 文件不存在视为空 */ }
    const combined = (existing + block).slice(-64 * 1024);
    fs.writeFileSync(memPath, combined, "utf8");
    _trace(`memory-recover: wrote ${block.length} chars to ${memPath}`);
    return true;
  } catch (e) {
    _trace(`memory-recover: MEMORY.md write failed: ${e.message}`);
    return false;
  }
}


async function handleSecretaryPath(text, agentId, ctx, senderId = "") {
  if (!config.secretaryEnabled) return false;

  // !!command!! 锚定检测 — 先 probe 再路由到正确 Skill
  const cmdWord = extractCommand(text);
  if (cmdWord) {
    console.log(`[zhice-gateway] !!command!! detected: "${cmdWord}"`);
    // 先 probe 工作秘书，确认是否路由到其他 Skill
    const probe = await secretaryProbe(text);
    if (probe && probe.target_skill && probe.target_skill !== "work_secretary") {
      // 跨 Skill 路由：调目标 Skill 的 execute
      console.log(`[zhice-gateway] !!command!! routing to ${probe.target_skill}:${probe.target_action}`);
      const targetResult = await skillExecute(probe.target_skill, text, agentId, probe.params, probe.target_action, senderId);
      const reply = extractSecretaryReply(targetResult);
      const deliverNote = await deliverSkillFile(agentId, targetResult, ctx.__rawOpenId, ctx.__channel);
      const finalReply = deliverNote ? (reply || "") + deliverNote : reply;
      if (finalReply) {
        ctx.reply(finalReply);
        // 方案X：!!command!! 显式锚定触发的采购下单/续答同样放行 agent（与常规 probe 路径一致）
        if (probe.target_skill === "procurement" && probe.target_action === "po_complete") {
          ctx.__skillHandledOrder = true;
          _markPassthrough([agentId, senderId, ctx?.__rawOpenId], text);
          ctx.message.text = _PASSTHROUGH_ANCHOR;
          console.log(`[zhice-gateway] !!command!! → procurement:po_complete replied → PASS-THROUGH to agent (方案X, anchored)`);
          return false;
        }
        ctx.skip();
        console.log(`[zhice-gateway] !!command!! → ${probe.target_skill} replied, skipping LLM`);
        return { block: true };
      }
      // skillExecute 失败：block 消息 + 告知用户，与常规 probe 路径一致，不降级到 zhice
      console.log(`[zhice-gateway] !!command!! → ${probe.target_skill} FAILED, blocking (no zhice fallback)`);
      ctx.reply(`⚠️ 系统识别到您想使用「${probe.target_skill}」功能，但该服务暂时不可用。\n请稍后重试，或联系管理员。`);
      ctx.skip();
      return { block: true };
    }
    // 工作秘书自身处理
    const execResult = await secretaryExecute(text, agentId);
    const reply = extractSecretaryReply(execResult);
    if (reply) {
      // 记忆恢复双通道：写 MEMORY.md + 注入 ctx 放行 agent（不 skip）
      if (_isMemoryRecover(execResult)) {
        _trace(`[zhice-gateway] memory:recover → 写 MEMORY.md + 注入 ctx.message.text (agent=${agentId})`);
        await _persistRecoveredMemory(agentId, reply);
        ctx.message.text = `【找回记忆】以下是恢复的历史记忆（最近24小时），请据此恢复工作状态；如需更早的历史记忆，请引导用户使用「查一下」检索个人记忆库（秘书会从永恒全量检索，不受时间限制）。\n\n${reply}`;
        ctx.reply("🧠 已为您找回最近记忆，正在恢复工作上下文…");
        return false;
      }
      ctx.reply(reply);
      ctx.skip();
      console.log(`[zhice-gateway] !!command!! resolved by secretary, skipping LLM`);
      return { block: true };
    }
    return false;
  }

  const probe = await secretaryProbe(text);
  if (probe === null) {
    console.log(`[zhice-gateway] Secretary probe error, falling back to zhice`);
    return false;  // 探针失败 → 交执策
  }
  if (probe.passthrough === false) {
    // LLM 语义路由：命中到其他 Skill → 带参数调目标 Skill
    if (probe.target_skill && probe.target_skill !== "work_secretary") {
      console.log(`[zhice-gateway] LLM semantic route → ${probe.target_skill}:${probe.target_action || 'execute'}`);
      const targetResult = await skillExecute(probe.target_skill, text, agentId, probe.params, probe.target_action, senderId);
      const reply = extractSecretaryReply(targetResult);
      const deliverNote = await deliverSkillFile(agentId, targetResult, ctx.__rawOpenId, ctx.__channel);
      const finalReply = deliverNote ? (reply || "") + deliverNote : reply;
      if (finalReply) {
        ctx.reply(finalReply);
        // 方案X（2026-08-13 波哥定调）：采购下单/补齐/续答（po_complete）是既有订单草稿的
        // 确定性执行，skill 已落库并回复确认。实测 block 拦不住 agent run（PO 照常落库、产物
        // 反被吞），故不 block——设 __skillHandledOrder 标记让 handler 跳过执策防重复分解，
        // 消息交还 agent 正常处理（网关拦截只保留自由外采意图）。
        if (probe.target_skill === "procurement" && probe.target_action === "po_complete") {
          ctx.__skillHandledOrder = true;
          _markPassthrough([agentId, senderId, ctx?.__rawOpenId], text);
          ctx.message.text = _PASSTHROUGH_ANCHOR;
          console.log(`[zhice-gateway] Semantic route → procurement:po_complete replied → PASS-THROUGH to agent (方案X, anchored)`);
          return false;
        }
        ctx.skip();
        console.log(`[zhice-gateway] Semantic route → ${probe.target_skill} replied, skipping LLM`);
        return { block: true };
      }
      console.log(`[zhice-gateway] Semantic route to ${probe.target_skill} failed`);
      // 探针已识别意图但Skill执行失败 → 直接告诉用户,不放agent去网上乱搜
      ctx.reply(`⚠️ 系统识别到您想${probe.target_action || '处理'}「${probe.target_skill}」，但服务暂时不可用。\n请稍后重试，或联系管理员。`);
      ctx.skip();
      return { block: true };
    }

    console.log(`[zhice-gateway] Secretary can handle, executing...`);
    const execResult = await secretaryExecute(text, agentId);
    const reply = extractSecretaryReply(execResult);
    if (reply) {
      // 记忆恢复双通道：写 MEMORY.md + 注入 ctx 放行 agent（不 skip）
      if (_isMemoryRecover(execResult)) {
        _trace(`[zhice-gateway] memory:recover → 写 MEMORY.md + 注入 ctx.message.text (agent=${agentId})`);
        await _persistRecoveredMemory(agentId, reply);
        ctx.message.text = `【找回记忆】以下是恢复的历史记忆（最近24小时），请据此恢复工作状态；如需更早的历史记忆，请引导用户使用「查一下」检索个人记忆库（秘书会从永恒全量检索，不受时间限制）。\n\n${reply}`;
        ctx.reply("🧠 已为您找回最近记忆，正在恢复工作上下文…");
        return false;
      }
      // 直接回复，跳过 LLM
      ctx.reply(reply);
      ctx.skip();
      console.log(`[zhice-gateway] Secretary replied, skipping LLM`);
      return { block: true };
    }
    console.log(`[zhice-gateway] Secretary passthrough or no reply, falling back to zhice`);
    return false;
  }
  console.log(`[zhice-gateway] Secretary probe passthrough, falling back to zhice`);
  // 采购意图最后防线（2026-08-13 波哥定调）：采购意图唯一判定——补齐订单才往下执行
  // （走 skill 续答），其他采购/投标意图一律拦截，绝不放行到 OpenClaw LLM/web 工具自由外采。
  // 补条款/续答消息（账期/抽检/交货/补齐等）豁免，放行走 skill 续答。
  if (!_isOrderFill(text) && _isProcureIntent(text)) {
    ctx.reply("采购/投标需求请直接说明：要买什么、数量多少、规格型号，或询什么价。我会转采购专员受控处理，不直接外网采集。");
    ctx.skip();
    console.log(`[zhice-gateway][trace] procure-intent block: "${text.slice(0,60)}" → blocked`);
    return { block: true };
  }
  return false;
}

// ── 采购/投标意图识别：probe passthrough 后的最后防线 ──
// 2026-08-13 波哥定调：采购意图唯一判定——补齐订单才往下执行(走 skill 续答)，
// 其他采购/投标意图一律拦。词表覆盖 P1.5 关键词兜底可能漏掉的采购意图
// （行情/价格/供应商/电力物资等），阻断 OpenClaw 自由外采（如扫电缆宝）。
function _isProcureIntent(text) {
  if (!text) return false;
  const PROCURE_KEYWORDS = [
    "下单", "下订单", "要买", "采购", "询价", "报价", "比价", "行情",
    "价格", "多少钱", "单价", "供应商", "招标", "投标", "中标", "标书",
    "评标", "电缆", "变压器", "铜价", "铝价", "物资", "开关柜", "母线",
  ];
  return PROCURE_KEYWORDS.some(kw => text.includes(kw));
}

// ── 补条款/续答豁免：订单补全上下文，放行走 skill 续答 ──
function _isOrderFill(text) {
  if (!text) return false;
  const FILL_HINTS = [
    "补齐", "不做要求", "不指定", "货到付款", "月结", "账期", "抽检",
    "交货", "物流", "发货", "税率", "质保",
  ];
  return FILL_HINTS.some(h => text.includes(h));
}

// ── 系统/进度消息识别：非用户指令，跳过语义路由（防投标生成死循环）──
// 2026-08-11 大师实锤：投标 skill 进度广播（"⏳ 正在生成投标文件…"/"✅ 生成完成"）
// 被语义路由误判成新 generate_bid → 自我死循环（id 85-89）。识别特征：
//  ① 以状态 emoji 开头（_send_progress_msg 统一前缀 "⏳ "，绝不可能是用户指令）；
//  ② 自环：from==agentId（skill 广播给自己）且含投标/标书/评审特征词。
function _isProgressNotice(text, fromId, agentId) {
  if (!text) return false;
  const t = text.trim();
  if (/^[⏳✅⚠️📋📦⬇️📎🔔⏸️🔄]/.test(t)) return true;
  if (fromId && agentId && fromId === agentId && /(投标|标书|评审|评分)/.test(t)) return true;
  // 2026-08-27 回环实锤（小智 15:37 二次 generate_bid + MemoryError）：完成/交付消息经
  // OpenClaw 回声成 inbound，"⏳ "前缀被剥（状态 emoji 落在句中）→ 上两分支 miss →
  // _businessLine 命中"投标"→ 硬路径直接 generate_bid（不 probe，probe 端进度防护被
  // 绕过）→ 二次生成。补两分支：
  //  ③ 交付引导模板措辞（deliverSkillFile/deliverSkillFile 草稿版/bidding 完成播报）——
  //     系统模板专属，用户自然语言不会逐字出现；
  //  ④ 状态 emoji 出现在任意位置 + 投标语境词——系统播报都带状态 emoji，用户输入
  //     极少 emoji+投标词组合（用户问"投标文件生成完成了吗？"无 emoji，不受影响）。
  if (/(你的标书已生成|标书草稿已生成|投标文件已(生成|完成|发送|交付)|标书已.*交付)/.test(t)) return true;
  // 十五轮（2026-08-27 bid185/186 回环实锤，小智网关日志抓到 _handleBusinessViaBus bidding:generate_bid）：
  // P0 完成播报改新文案（"投标文件生成完成 ✅ 标书编号：N，请到【我的标书】下载" /
  // "完成 ⚠️ 标书编号：N（含待确认项，待补充：…）"）——无"已"字，上方旧模板锚 miss；
  // OpenClaw 本地回声若剥 emoji（十三轮已实证"⏳ "前缀被剥），分支④也 miss →
  // _businessLine 命中"投标/标书" → before_dispatch 硬路径/语义路由二次 generate_bid。
  // 补纯文字锚（系统模板专属措辞，用户自然语言不会逐字出现）：
  if (/(投标文件生成完成(?![吗呢啊吧了没没有])|请到【我的标书】下载|含待确认项[，,]待补充)/.test(t)) return true;
  if (/[⏳✅⚠⬇⏸]/.test(t) && /(投标|标书|评审|评分)/.test(t)) return true;
  // 十八轮（2026-08-28 小智直调 API 复现回环：第一单 docx 渲染中同秒第二单进来挤爆内存）：
  // 触发源不是完成播报（当时还没发）而是**周期进度消息**（_progress_loop 每 10s 一条
  // "⏳ 正在×××..."），回声剥前缀后上四分支全 miss → _businessLine 命中"投标" →
  // 硬路径二次 generate_bid。锚进度文案的系统模板措辞（bid_generator _p() 全集 +
  // _start_progress 初始文案 + 互斥闸拒绝文案）：
  //  - "正在核对资格/检索素材库/参考历史反馈/分章生成/进行 AI 评审/校验投标文件/
  //    生成投标文件（Word）/上传投标文件到汇川/生成投标文件（Word）/评审无进展"——
  //    用户自然语言不会以这些动词短语逐字开头；
  //  - "开始生成投标文件「"带全角书名号文件名——用户口令极少用「」；
  //  - "投标文件正在生成中，请等待"——互斥闸拒绝文案。
  if (/^(正在(核对资格|检索素材库|参考历史反馈|分章生成投标文件|进行 AI 评审|校验投标文件完整度|生成投标文件（Word）|上传投标文件到汇川)|评审无进展[，,]|开始生成投标文件「|该企业投标文件正在生成中[，,]请等待)/.test(t)) return true;
  return false;
}

// ── 中断命令 ──────────────────────────────────────────

function isInterruptCommand(text) {
  const trimmed = text.trim();
  for (const re of compiledInterrupts) {
    if (re.test(trimmed)) return true;
  }
  return false;
}

async function cancelCurrentTask(agentId) {
  const recData = await zhiceGet(`/recover?agent_id=${encodeURIComponent(agentId)}`);
  if (!recData || !recData.unfinished || recData.unfinished.length === 0) return false;

  let cancelled = false;
  for (const item of recData.unfinished) {
    const taskId = item.task_id;
    if (!taskId) continue;
    const res = await zhicePost(`/tasks/${taskId}/cancel`, {});
    if (res) {
      console.log(`[zhice-gateway] Cancelled task #${taskId} for ${agentId}`);
      cancelled = true;
    }
  }
  return cancelled;
}

// ── 未完成任务检查 ─────────────────────────────────────

async function checkUnfinishedTask(agentId) {
  const data = await zhiceGet(`/recover?agent_id=${encodeURIComponent(agentId)}`);
  if (!data || !data.unfinished || data.unfinished.length === 0) return null;
  const first = data.unfinished[0];
  // 超时自动取消
  if (config.taskTimeoutMinutes > 0 && first.created_at) {
    const elapsed = (Date.now() - new Date(first.created_at).getTime()) / 60000;
    if (elapsed > config.taskTimeoutMinutes) {
      console.log(`[zhice-gateway] Task #${first.task_id} timed out (${elapsed.toFixed(1)}m), auto-cancelling`);
      await zhicePost(`/tasks/${first.task_id}/cancel`, { reason: "timeout" });
      return null;
    }
  }
  console.log(`[zhice-gateway] ${agentId} has unfinished step: task=${first.task_id}`);
  return first.task_id;
}

// ── 创建 Task + 拉第一步 ──────────────────────────────

async function createAndFetchNext(agentId, messageText, skipClarity = false) {
  const body = {
    title: messageText.slice(0, 80).replace(/\n/g, " "),
    description: messageText.slice(0, 500),
    created_by: agentId,
    priority: "P2",
    skip_clarity: skipClarity,
  };

  const taskData = await zhicePost("/tasks", body);
  if (!taskData) {
    console.error("[zhice-gateway] Task creation failed (no response)");
    return { clarification: null };
  }

  // ── needs_clarification：模糊指令 → 返回追问让用户补充 ──
  if (taskData.status === "needs_clarification") {
    return { clarification: taskData };
  }

  if (!taskData.task_id) {
    console.error("[zhice-gateway] Task creation failed (no task_id):",
                  JSON.stringify(taskData).slice(0, 200));
    return { clarification: null };
  }

  console.log(
    `[zhice-gateway] Task #${taskData.task_id} created for ${agentId} ` +
    `(mode=${taskData.mode}, steps=${taskData.total_steps})`
  );

  const nextData = await zhiceGet(
    `/tasks/${taskData.task_id}/next?agent_id=${encodeURIComponent(agentId)}`
  );

  if (!nextData || !nextData.current_step) {
    console.warn("[zhice-gateway] /next returned no step");
    return null;
  }

  return {
    taskId: taskData.task_id,
    stepId: nextData.current_step.step_id,
    stepIndex: nextData.current_step.step_index,
    totalSteps: taskData.total_steps,
    instruction: nextData.current_step.instruction,
    execType: nextData.current_step.exec_type || "shell",
    progress: nextData.progress,
  };
}

// ── 构建注入文本 ─────────────────────────────────────

function buildStepContext(stepData) {
  return [
    `---`,
    `【执策任务 #${stepData.taskId}】第 ${stepData.stepIndex}/${stepData.totalSteps} 步（${stepData.progress}）`,
    ``,
    `📋 请严格完成以下任务，不要偏离：`,
    ``,
    `> ${stepData.instruction}`,
    ``,
    ``,
    `完成后执行提交（用你的真实 agent_id 替换 <你的ID>，用 uuidgen 生成 idempotency_key）：`,
    ``,
    `  curl -X POST ${config.zhiceEndpoint}/steps/${stepData.stepId}/start -H "Content-Type: application/json" -d '{"agent_id":"<你的ID>"}'`,
    ``,
    `  curl -X POST ${config.zhiceEndpoint}/steps/${stepData.stepId}/submit \\`,
    `    -H "Content-Type: application/json" \\`,
    `    -d '{"agent_id":"<你的ID>","status":"completed","summary":"完成摘要","outputs":{},"idempotency_key":"$(uuidgen)"}'`,
    `---`,
  ].join("\n");
}

function buildUnfinishedBlock(taskId, originalMessage) {
  return [
    `⚠️ 你有一个未完成的执策任务 #${taskId}，请先完成后再处理新请求。`,
    ``,
    `查询当前步骤: curl ${config.zhiceEndpoint}/tasks/${taskId}/next?agent_id=<你的ID>`,
    ``,
    `---`,
    `待处理的新消息（已暂存）：`,
    `> ${originalMessage.slice(0, 200)}`,
  ].join("\n");
}

// ── 解析 curl 风格 HTTP 指令 ─────────────────────

function parseHttpInstruction(instruction) {
  // 格式: "POST /path -d '{"key":"val"}'"
  const methodMatch = instruction.match(/^(GET|POST|PUT|DELETE|PATCH)\s+/);
  if (!methodMatch) return null;
  const method = methodMatch[1];
  const rest = instruction.slice(method.length).trim();
  const pathMatch = rest.match(/^(\/\S+)/);
  if (!pathMatch) return null;
  const path = pathMatch[1];

  let body = null;
  const bodyMatch = rest.match(/-d\s+'([^']*)'/);
  if (bodyMatch) {
    try { body = JSON.parse(bodyMatch[1]); } catch (_) { body = bodyMatch[1]; }
  }
  return { method, path, body };
}

function tryParseJSON(str) {
  try { return JSON.parse(str); } catch (_) { return str; }
}

// ── 执行 HTTP 步骤（插件直接调 fetch）───────────

async function executeHttpStep(instruction) {
  const parsed = parseHttpInstruction(instruction);
  if (!parsed) return { error: "cannot_parse", raw: instruction };

  const baseUrl = config.zhiceEndpoint.replace(/\/v1\/zhice\/?$/, "");
  const url = `${baseUrl}${parsed.path}`;

  const headers = { "Content-Type": "application/json", ..._gwAuthHeaders() };

  try {
    const resp = await fetch(url, {
      method: parsed.method,
      headers,
      body: parsed.body ? JSON.stringify(parsed.body) : undefined,
    });
    const raw = await resp.text().catch(() => "");
    return {
      ok: resp.ok,
      status: resp.status,
      body: raw ? tryParseJSON(raw) : null,
      raw: raw.slice(0, 2000),
    };
  } catch (err) {
    return { error: err.message, raw: instruction };
  }
}

// ── 执行 Skill 步骤（插件直接调 Skill API）──────────

async function executeSkillStep(instruction, params, agentId) {
  const colonIdx = instruction.indexOf(":");
  if (colonIdx === -1) return { error: "cannot_parse", raw: instruction };

  const skillName = instruction.slice(0, colonIdx).trim();
  const action = instruction.slice(colonIdx + 1).trim();
  if (!skillName || !action) return { error: "cannot_parse", raw: instruction };

  // #1 防御：空 agent_id 不允许发出（避免子进程 --agent-id="" 落通用目录/拉不起）
  const safeAgentId = agentId || "gateway:unidentified";
  if (!agentId) console.warn(`[zhice-gateway] executeSkillStep(${skillName}:${action}) agent_id empty → fallback "${safeAgentId}"`);

  const baseUrl = config.zhiceEndpoint.replace(/\/v1\/zhice\/?$/, "");
  const url = `${baseUrl}/api/v1/skills/${encodeURIComponent(skillName)}/execute`;

  const headers = { "Content-Type": "application/json", ..._gwAuthHeaders() };

  try {
    const resp = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify({ agent_id: safeAgentId, action, params: params || {} }),
    });
    const raw = await resp.text().catch(() => "");
    return {
      ok: resp.ok,
      status: resp.status,
      body: raw ? tryParseJSON(raw) : null,
      raw: raw.slice(0, 2000),
    };
  } catch (err) {
    return { error: err.message, raw: instruction };
  }
}

// ── 提交步骤完成 ────────────────────────────────

async function startStep(stepId, agentId) {
  return zhicePost(`/steps/${stepId}/start`, { agent_id: agentId });
}

async function submitStep(taskId, stepId, agentId, status, summary, outputs) {
  return zhicePost(`/steps/${stepId}/submit`, {
    agent_id: agentId,
    status: status,
    summary: summary,
    outputs: outputs || {},
    idempotency_key: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
  });
}

// ═══════════════════════════════════════════════════════
// v7 事件归一化：将 v7 event 转为 v6 ctx 风格
// ═══════════════════════════════════════════════════════

function normalizeEvent(event) {
  // v7 event = { from, content, timestamp?, metadata? }
  // 包装为 ctx { from, message: {text, content, metadata}, reply, skip }
  const ctx = {
    from: event?.from || event?.sender || '',
    message: {
      text: event?.content || event?.text || '',
      content: event?.content || event?.text || '',
      metadata: event?.metadata || {},
    },
    metadata: event?.metadata || {},
    // v7 message_received 可 return { block: true } 拦截
    reply: null,   // 由外部注入
    skip: null,    // 由外部注入
  };
  return ctx;
}

// before_dispatch / before_agent_run 事件文本提取（多候选结构兜底；事件结构与 message_received 未必一致）
function _beforeRouteText(event) {
  if (!event) return "";
  if (typeof event === "string") return event;
  for (const key of ["text", "content", "rawContent"]) {
    if (typeof event[key] === "string" && event[key]) return event[key];
  }
  const m = event.message || event.body || event.raw || {};
  if (typeof m === "string") return m;
  for (const key of ["text", "content", "rawContent", "message"]) {
    if (typeof m[key] === "string" && m[key]) return m[key];
  }
  return "";
}

// 投标生成指令判定（完整拦截主闸）：命中即拦在 agent 执行前，杜绝 agent 主线把
// 生成结果文件自动下发到飞书（双通道根因）。指令来自飞书快捷按钮/用户输入，
// 如"生成标书"/"文件ID：6192d037..."。只作用于入站用户消息，不影响 agent 出站回复。
function _isBidCommand(text) {
  if (!text) return false;
  return /生成标书|文件id[:：]|文件标识[:：]/i.test(text);
}

// ── 三条业务线意图识别（波哥 2026-08-20）：投标/采购/销售 → before_dispatch 走 bus，其余放行 ──
// 文件推送的三个来源：投标标书、销售报价/日报、采购询价。关键词快判（快、无 LLM 延迟），
// 归属歧义（如"报价"采购 vs 销售）交由 secretaryProbe 语义路由消歧。

function _isSalesIntent(text) {
  if (!text) return false;
  const SALES_KEYWORDS = [
    "报价", "日报", "销售", "客户", "产品目录", "价目表", "底价", "开单",
    "成交", "谈判", "竞争对手", "竞品", "培训", "知识库", "询价单", "订货",
    "对外报价", "价格表", "价差",
  ];
  return SALES_KEYWORDS.some(kw => text.includes(kw));
}

// 统一入口：返回 "bidding" | "procurement" | "sales" | ""（空 = 非三线，放行）
// 顺序：标书语境词 > 生成标书硬指令 > sales > procurement（"报价"等歧义词 sales 优先，靠 probe 纠正）
// 二十三轮（2026-08-28 小智 2c52850b stdout 实锤）：「文件ID：xxx」形态识别——
// _businessLine 判 bidding 且不含任何显式投标词（BID_RE 六词），即仅凭 _isBidCommand
// 的 文件id[:：]/文件标识[:：] 命中（"生成标书"含"标书"必走 BID_RE，不会到这里）。
// 此类消息在飞书有两义：快捷按钮裸文件ID续作生成标书 / 用户键入的技术规范整理
// （bid_prep）→ 不能直接硬路径，须交 probe 消歧（tags 确定性层命中 bid_prep 则让位）。
function _bidFileIdOnly(text) {
  if (!text) return false;
  return _isBidCommand(text) && !/标书|投标|招标|中标|评标|开标/.test(text);
}

function _businessLine(text) {
  if (!text) return "";
  if (/标书|投标|招标|中标|评标|开标/.test(text)) return "bidding";
  if (_isBidCommand(text)) return "bidding";
  if (_isSalesIntent(text)) return "sales";
  if (_isProcureIntent(text)) return "procurement";
  return "";
}

// ── before_dispatch 处理去重（防 message_received 二次处理三线消息）──
// before_dispatch 处理成功 → 记 senderId+content 签名（TTL 30s）；message_received 顶部命中
// 即 return {block:true}（不再 skillExecute / 不再建任务）。同用户同文本 30s 内重发会误短路，
// 罕见、可接受。
const _handledSigs = new Map(); // sig → ts
// 2026-08-24 深夜补强：签名文本归一化——before_dispatch（event 字段）与 message_received
// （ctx 字段）文本来源不同，@提及前缀/空白/大小写任一差异即静默 miss（14:06 复测实锤：
// 短路未命中且无日志 → probe 路径 ~7s 后二次 skillExecute）。归一后再拼签名。
function _sigNorm(text) {
  return String(text || "")
    .replace(/@[\u4e00-\u9fa5A-Za-z0-9_-]+/g, " ")  // 去 @提及（飞书 @机器人前缀）
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase()
    .slice(0, 120);
}
function _msgSig(senderId, text) {
  return `${senderId || ""}|${_sigNorm(text)}`;
}
function _markHandled(senderId, text) {
  const sig = _msgSig(senderId, text);
  _handledSigs.set(sig, Date.now());
  if (_handledSigs.size > 500) {
    const now = Date.now();
    for (const [k, ts] of _handledSigs) {
      if (now - ts > 30000) _handledSigs.delete(k);
    }
  }
}
function _consumeHandled(senderId, text) {
  const sig = _msgSig(senderId, text);
  const hit = _handledSigs.has(sig);
  if (hit) _handledSigs.delete(sig);
  return hit;
}
// 2026-08-24：多 key 消费——写入侧（before_dispatch）同时记 senderId（原始 open_id 形态）
// 与 agentId（sessionKey/规范名形态）双签名，此处逐 key 尝试，任一命中即短路，防身份
// 归一化差异导致签名永久 miss。
function _consumeHandledAny(senderIds, text) {
  for (const id of senderIds) {
    if (id && _consumeHandled(id, text)) return true;
  }
  return false;
}

// ── 方案X 放行签名（2026-08-30）──
// 背景：po_complete（下单/补齐/确认询价）走方案X pass-through——skill 已落库并回复，
// 消息交还 agent 正常处理（2026-08-13 定调）。但 8-20 加的 before_agent_run 兜底闸
// 只看 _businessLine(text)，不认识挂在 ctx 上的 __skillHandledOrder（事件里看不到）
// → "确认下单/我要买xxx"命中采购线 → {decision:block} → OpenClaw 给用户回
// "your message can not be send: blocked by zhice-gateway"。
// 修复：方案X 放行时记短期签名（TTL 90s），兜底闸命中签名则豁免（agent run 正常跑，
// 正是方案X的预期行为）。
const _PASSTHROUGH_TTL_MS = 90000;
const _passthroughSigs = new Map(); // sig → ts
// 方案X 锚文本（2026-08-31 问题A：agent 收到原始"下单"文本后用工具重做采购流程，
// 10 分钟 20 次 LLM 工具循环）。skill 已处理的消息交还 agent 时改写为锚——
// 告知已处理、勿调工具、简短确认即可。锚文本刻意不含任何业务线关键词
// （_businessLine 不命中 → 兜底闸天然放行，无需依赖签名豁免）。
const _PASSTHROUGH_ANCHOR =
  "【系统提示】该消息对应的业务已由后台系统处理完毕（回执见上方消息）。你无需调用任何工具，也无需重复执行任何操作，请用一两句话向用户自然确认即可。";
// ID 形态变体（2026-08-31 小智 07:15 日志实锤：before_dispatch 打的是 sessionKey
// 形态 "procurement-feishu"，message_received 归一化后是 "feishu:ou_xxx"，两侧
// 形态不一致 → 豁免永久 miss。写入/检查两侧统一按变体展开：原值 / 去前缀 /
// "feishu:"+裸值，交集概率大幅提高。）
function _idVariants(id) {
  const out = new Set();
  const s = String(id || "");
  if (!s) return out;
  out.add(s);
  const m = s.match(/^([a-z]+):(.+)$/i);
  if (m) out.add(m[2]);
  else if (s.startsWith("ou_")) out.add(`feishu:${s}`);
  return out;
}
function _markPassthrough(agentIds, text) {
  const norm = _sigNorm(text);
  const now = Date.now();
  for (const id of (Array.isArray(agentIds) ? agentIds : [agentIds])) {
    for (const v of _idVariants(id)) {
      _passthroughSigs.set(`pass|${v}|${norm}`, now);
    }
  }
  if (_passthroughSigs.size > 200) {
    for (const [k, ts] of _passthroughSigs) {
      if (now - ts > _PASSTHROUGH_TTL_MS) _passthroughSigs.delete(k);
    }
  }
}
function _isPassthrough(ids, text) {
  const norm = _sigNorm(text);
  if (!norm) return false;
  const now = Date.now();
  const variants = new Set();
  for (const id of ids) for (const v of _idVariants(id)) variants.add(v);
  // 2026-08-30 二次block加固：before_agent_run 的 event.prompt 可能是拼装后的长文本
  // （系统指令+上下文+用户原文），纯等值比对静默 miss → 豁免失效。放宽为包含关系
  // （同 agent + 90s 窗口内，任一方向包含即命中；sigText ≥6 字防短串误命中）。
  for (const [sig, ts] of _passthroughSigs) {
    if (now - ts > _PASSTHROUGH_TTL_MS) { _passthroughSigs.delete(sig); continue; }
    const parts = sig.split("|");
    const sigId = parts[1] || "";
    const sigText = parts.slice(2).join("|");
    const idOk = variants.has(sigId);
    // 事件无任何可用 ID 候选时降级为纯文本匹配（后果仅 90s 窗口内放行 agent run，
    // 同文误中概率极低；比"必 miss→用户永远 blocked"好）
    const noIdAtAll = variants.size === 0;
    if (!idOk && !noIdAtAll) continue;
    if (norm === sigText
      // 门槛 ≥2 即可：确认类短语"确认询价/确认下单"仅 4 字（小智 22:10 日志实锤
      // [Queued user message...] 包装文本内嵌原文被 ≥6 门槛挡掉）；误中后果仅为
      // 放行 agent run（90s 窗口内同 agent 同文），风险可接受。
      || (sigText.length >= 2 && (norm.includes(sigText) || sigText.includes(norm)))) {
      _passthroughSigs.delete(sig);
      if (!idOk) console.log(`[zhice-gateway] PASSTHROUGH-EXEMPT text-only（事件无 ID 候选）text="${String(text).slice(0, 60)}"`);
      return true;
    }
  }
  return false;
}

// ── 三线 bus 处理：probe→skillExecute → 回复文本（含门户下载引导，不直发避免重复）──
// 供 before_dispatch 复用 message_received 的秘书路径，但回复以 {handled:true,text} 返回。
async function _handleBusinessViaBus(text, agentId, skillName, action, params, senderId = "") {
  const result = await skillExecute(skillName, text, agentId, params, action, senderId);
  const reply = extractSecretaryReply(result);
  const guide = await buildSkillFileGuide(result);
  const finalReply = reply ? (guide ? `${reply}\n\n${guide}` : reply) : guide;
  if (finalReply) return finalReply;
  console.warn(`[zhice-gateway] _handleBusinessViaBus ${skillName}:${action} → no reply text`);
  return `⚠️ 系统识别到您想使用「${skillName}」功能，但该服务暂时不可用。\n请稍后重试，或联系管理员。`;
}

// 从 skill 结果提取门户下载引导文本（非发送版，纯文本；deliverSkillFile 是发送版）
async function buildSkillFileGuide(result) {
  if (!result) return "";
  const data = result.data && typeof result.data === "object" ? result.data : result;
  const genStatus = data.gen_status
    || (data.delivery && data.delivery.quality && data.delivery.quality.gen_status)
    || "";
  const file = extractSkillFile(result);
  if (!file) return "";
  const sizeText = await _probeFileSize(file.download_url);
  return genStatus === "needs_review"
    ? `⚠️ 标书草稿已生成：《${file.file_name || "（未命名）"}》${sizeText}，含待确认项，请登录投标系统【我的标书】下载完善后再交付。`
    : `📄 你的标书已生成：《${file.file_name || "（未命名）"}》${sizeText}，请登录投标系统网页版，在【我的标书】中下载。`;
}

// ═══════════════════════════════════════════════════════
// 插件入口 — 兼容 v6 + v7
// ═══════════════════════════════════════════════════════

// 构建戳（2026-08-31 小智 07:15 报"更新代码后仍跑旧版"）：每次发布必须递增，
// init 日志即版本凭证——日志无此戳/戳不对 = 跑的不是最新代码。
const PLUGIN_BUILD = "2026-08-31.3";

function registerPlugin(api) {
  console.log(`[zhice-gateway v3.1 build=${PLUGIN_BUILD}] Initializing — OpenClaw v7.x`);

  // ── 显式读取 config.yaml（修复问题：OpenClaw plugin config 不含 filesystem yaml）──
  let fileConfig = {};
  try {
    const configPath = path_.join(__dirname, 'config.yaml');
    if (fs.existsSync(configPath)) {
      const raw = fs.readFileSync(configPath, 'utf8');
      // 不依赖 js-yaml：简单 key: value 解析（config.yaml 结构扁平）
      for (const line of raw.split('\n')) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#')) continue;
        const colonIdx = trimmed.indexOf(':');
        if (colonIdx === -1) continue;
        const key = trimmed.slice(0, colonIdx).trim();
        let val = trimmed.slice(colonIdx + 1).trim();
        if (val.startsWith('"') && val.endsWith('"')) val = val.slice(1, -1);
        else if (val.startsWith("'") && val.endsWith("'")) val = val.slice(1, -1);
        else if (val === 'true') val = true;
        else if (val === 'false') val = false;
        else if (val === 'null') val = null;
        else if (/^\d+(\.\d+)?$/.test(val)) val = Number(val);
        // 数组/对象：尝试 JSON 解析（支持 identityAliases: {"ou_xxx":"agent"} 内联 JSON）；解析失败则跳过
        if (typeof val === 'string' && val && (val.startsWith('[') || val.startsWith('{'))) {
          try { val = JSON.parse(val); } catch (_e) { val = null; }
        }
        if (key && val !== null) fileConfig[key] = val;
      }
      console.log('[zhice-gateway] Loaded config.yaml:', Object.keys(fileConfig).join(', '));
    }
  } catch (err) {
    console.warn('[zhice-gateway] Failed to read config.yaml:', err.message);
  }
  // 合并：fileConfig 为基础，api.config 覆盖（OpenClaw 面板配置优先）
  loadConfig({ ...fileConfig, ...api.config });

  // ── before_dispatch / before_agent_run → agent 执行前拦截（波哥 2026-08-20 三线主闸）──
  // 背景：zhice 在 message_received（路由后）处理，ctx.skip/block 拦不住 OpenClaw agent run，
  // agent 主线把 skill 结果文件自动下发（双通道根因）。原设计 before_route_inbound_message
  // （GitHub #81061）被小智 7.1-2 实测推翻（核心 isPluginHookName 不认，永不触发）。改用官方
  // PLUGIN_HOOK_NAMES 里早于 agent 执行的钩子（小智附清单 + 测试二实测契约成功）：
  //   - before_dispatch：派发前拦截，{handled:true, text} 丢弃消息（不进 agent），text 回用户；
  //   - before_agent_run：LLM 前 gate，{decision:"block", message} 阻断 agent run。
  // 波哥定调（2026-08-20）：只拦投标/采购/销售三条线（文件推送来源），拦截后直接走我们系统
  // （bus 管线 probe→skillExecute HTTP 真实生成），其余消息放行 message_received 正常走：
  //   ① 投标（生成标书/文件ID/标书语境词）→ 直接 skillExecute(bidding:generate_bid) 硬保险；
  //   ② 采购/销售（_isProcureIntent/_isSalesIntent 关键词）→ secretaryProbe 语义路由消歧归属后执行；
  //   ③ 处理成功 → 记 _handledSigs → message_received 顶部短路（绝不二次 skillExecute/建任务）；
  //   ④ before_agent_run 兜底闸（主闸漏网时 {decision:block} 防文件下发）；
  //   ⑤ 审批/中断/闲聊/系统/进度消息 → 放行或静默吞，不误拦既有功能。
  const _interceptKw = config.beforeInterceptTestKeyword || "";
  const _interceptBusiness = config.interceptBusinessLines !== false;
  const _BUSINESS_SKILLS = ["bidding", "procurement", "sales"];
  try {
    api.on("before_dispatch", async (event) => {
      if (config.traceEnabled) {
        console.log("[zhice-gateway] before_dispatch FIRED");
        try {
          console.log("[zhice-gateway] before_dispatch event keys:", Object.keys(event || {}).join(", "));
          console.log("[zhice-gateway] before_dispatch event:", JSON.stringify(event, null, 2).slice(0, 2500));
        } catch (e) { console.log("[zhice-gateway] before_dispatch dump err:", e.message); }
      }
      const text = _beforeRouteText(event);
      const senderId = event?.senderId || "";
      // sessionKey "agent:bidding-feishu-2:main" → "bidding-feishu-2"（目标 agent 直接可取），
      // 取不到再退回 resolveCanonicalAgentId(senderId)（通道身份 → 规范 agent 名）
      let agentId = "";
      if (typeof event?.sessionKey === "string") {
        const m = event.sessionKey.match(/^agent:([^:]+?)(?::|$)/);
        if (m && m[1]) agentId = m[1];
      }
      if (!agentId) {
        try { agentId = await resolveCanonicalAgentId(senderId); } catch (_) {}
      }
      if (!agentId) agentId = "gateway:unidentified";

      // ① 自定义验证关键词（小智测试用）
      if (_interceptKw && text.includes(_interceptKw)) {
        console.log(`[zhice-gateway] before_dispatch KEYWORD HIT kw="${_interceptKw}" text="${text.slice(0, 60)}" → {handled:true} (discarded, no agent)`);
        return { handled: true, text: "【网关拦截验证】消息已被 before_dispatch 丢弃（未进 agent）。验证测试，正常消息不受影响。" };
      }

      // ② 空/系统/进度消息 → 静默吞（防 agent 重跑 skill 自我死循环）
      if (!text) return { handled: true };
      if (_isProgressNotice(text, senderId, agentId)) {
        _trace(`before_dispatch 进度消息静默 agent=${agentId} text="${text.slice(0, 50)}"`);
        return { handled: true };
      }
      if (/^(inquiry_id|quote_id|counter_id|negotiation_id|message_id)[:\s]/.test(text) || text.startsWith('{"') || text.startsWith('[{"')) {
        _trace(`before_dispatch 系统消息静默 agent=${agentId}`);
        return { handled: true };
      }

      // ③ 审批回复 / 中断命令 / 闲聊 → 放行 message_received 原逻辑（不误拦既有功能）
      const approvalMatch = text.match(/^(允许|拒绝|批准|同意|approve|reject|deny)\s*(\w*)$/i);
      if (approvalMatch && text.trim().length <= 10) return undefined;
      if (isInterruptCommand(text)) return undefined;
      if (isCasualChat(text)) return undefined;

      // ④ 三线拦截 → 直接走我们的 bus（probe→skillExecute），agent 不跑 → 不推文件
      const line = _businessLine(text);
      if (_interceptBusiness && line) {
        console.log(`[zhice-gateway] before_dispatch BUSINESS-LINE HIT line="${line}" text="${text.slice(0, 60)}" → bus handle + {handled:true}`);
        // 2026-08-24 双次执行根因修复：签名必须在 await 执行**之前**写入——生成标书耗时数十秒，
        // 完成后再写签名，message_received（~3s 后到达）短路检查时签名尚不存在 → 语义路由
        // 二次 skillExecute → 标书生成两份。双 key（senderId 原始形态 + agentId 规范形态）
        // 对齐消费侧 _consumeHandledAny 的双 key 匹配。失败路径不删签名（错误已有回复，勿重试）。
        _markHandled(senderId, text);
        _markHandled(agentId, text);
        try {
          let reply;
          // 二十三轮（2026-08-28）：投标分支拆两形态——显式投标词 → 硬路径原样不动
          // （100% 保险，杜绝双通道）；仅凭「文件ID：/文件标识：」判线 → 先 probe 消歧。
          // 误伤实锤：用户「文件ID：xxx。批量整理技术规范」被 _isBidCommand 判 bidding
          // → 硬路径 generate_bid 抢走 bid_prep → bidding 定位 156MB zip 报「文件过大」。
          const fileIdOnly = line === "bidding" && _bidFileIdOnly(text);
          if (fileIdOnly) {
            console.log(`[zhice-gateway] before_dispatch 文件ID形态无投标词 text="${text.slice(0, 60)}" → probe 消歧（bid_prep/投标续作）`);
          }
          if (line === "bidding" && !fileIdOnly) {
            // 投标硬路径：直接 generate_bid，不 probe（100% 保险，杜绝双通道）
            reply = await _handleBusinessViaBus(text, agentId, "bidding", "generate_bid", {}, senderId);
          } else {
            // probe 语义路由消歧：采购/销售归属（"报价/询价"）+ 文件ID形态（bid_prep/投标续作）
            const probe = await secretaryProbe(text);
            // 守卫含 bid_prep：文件ID形态下 tags 层（≥0.7 确定性）命中 bid_prep 必须放行。
            // 此前三线守卫会丢弃 bid_prep 回落硬路径——正是本次误伤的另一半根因。
            const routable = probe && probe.target_skill &&
              (_BUSINESS_SKILLS.includes(probe.target_skill) || probe.target_skill === "bid_prep");
            if (routable) {
              // 方案X豁免补口（2026-08-30 二次block）：po_complete 经 before_dispatch bus
              // 执行后，OpenClaw 仍可能跑 agent（handled:true 拦不住，与 block 拦不住同源）
              // → before_agent_run 兜底闸拦截 → 用户收到 blocked 报错。初始"下单"消息走
              // 本路径且被 _handledSigs 短路，永远到不了 message_received 的方案X分支，
              // 必须在此对齐打放行签名（先签名后 await，同 2026-08-24 双次执行教训）。
              if (probe.target_skill === "procurement" && probe.target_action === "po_complete") {
                _markPassthrough([agentId, senderId], text);
                console.log(`[zhice-gateway] before_dispatch po_complete → mark PASSTHROUGH agent=${agentId} sender=${senderId}`);
              }
              reply = await _handleBusinessViaBus(text, agentId, probe.target_skill, probe.target_action, probe.params, senderId);
            } else if (line === "bidding") {
              // 文件ID形态 probe 未命中/放行（快捷按钮裸「文件ID：xxx」续作等）→
              // 回落投标硬路径，行为与修复前完全一致（仅多一次 probe 延迟）
              console.warn(`[zhice-gateway] probe 未命中（文件ID形态）→ 回落投标硬路径`);
              reply = await _handleBusinessViaBus(text, agentId, "bidding", "generate_bid", {}, senderId);
            } else {
              console.warn(`[zhice-gateway] probe 未命中三线（kw line=${line}）→ 按关键词线执行`);
              reply = await _handleBusinessViaBus(text, agentId, line, undefined, {}, senderId);
            }
          }
          return { handled: true, text: reply };
        } catch (err) {
          console.error(`[zhice-gateway] before_dispatch bus handle failed: ${err.message}`);
          return { handled: true, text: "⚠️ 系统服务暂时不可用，请稍后重试或联系管理员。" };
        }
      }

      // ⑤ 非三线业务/闲聊等 → 放行 message_received 正常处理（执策/审批/其他 skill 原逻辑不动）
      return undefined;
    });
    console.log("[zhice-gateway] before_dispatch hook registered");
  } catch (err) {
    console.warn("[zhice-gateway] before_dispatch register failed:", err.message);
  }
  try {
    api.on("before_agent_run", async (event) => {
      if (config.traceEnabled) {
        console.log("[zhice-gateway] before_agent_run FIRED");
        try {
          console.log("[zhice-gateway] before_agent_run event keys:", Object.keys(event || {}).join(", "));
          console.log("[zhice-gateway] before_agent_run event:", JSON.stringify(event, null, 2).slice(0, 2500));
        } catch (e) { console.log("[zhice-gateway] before_agent_run dump err:", e.message); }
      }
      const text = (typeof event?.prompt === "string" ? event.prompt : "") || _beforeRouteText(event);
      // 方案X 豁免（2026-08-30）：po_complete 已由 skill 处理并显式放行 agent run，
      // 兜底闸不拦（否则用户收到 "blocked by zhice-gateway" 报错）。事件结构与
      // message_received 的 ctx 不同，agent 标识取多候选（含 sessionKey 解析——
      // before_dispatch 两侧 agentId 均取自 "agent:xxx:main" 形态，须对齐）。
      let _ptSessId = "";
      if (typeof event?.sessionKey === "string") {
        const _m = event.sessionKey.match(/^agent:([^:]+?)(?::|$)/);
        if (_m && _m[1]) _ptSessId = _m[1];
      }
      const _ptIds = [event?.agentId, event?.agent_id, _ptSessId, event?.sessionId,
        event?.senderId, event?.from?.id, event?.from?.userId, event?.from?.open_id,
        event?.user?.id, event?.sender?.id].filter(Boolean);
      if (_isPassthrough(_ptIds, text)) {
        console.log(`[zhice-gateway] before_agent_run PASSTHROUGH-EXEMPT text="${String(text).slice(0, 60)}" → 放行（方案X po_complete）`);
        return undefined;
      }
      // 兜底闸（2026-08-20）：主闸 before_dispatch 若漏网（文本结构差异/识别偏差等），此处仍阻断，
      // 防止 agent 主线把业务结果文件自动下发。阻断消息仅阻止 agent run，不重复回复用户
      // （业务已由 bus skillExecute 生成并回引导文本）。
      if (_interceptBusiness && _businessLine(text)) {
        const _blkLine = _businessLine(text);
        console.log(`[zhice-gateway] before_agent_run BUSINESS-LINE BLOCK line=${_blkLine} text="${text.slice(0, 60)}" ids=[${_ptIds.join(",")}] passSigs=${_passthroughSigs.size} → {decision:block} (backstop, no LLM/file)`);
        // 2026-08-31 问题B：排队消息（前一轮 turn 未结束）跳过插件管线，skill 从未
        // 处理、无签名 → block 后用户只看到 OpenClaw 天书报错。采购线（po_complete
        // 确认类）此时直发友好提示，告知未被执行、稍后重发。投标/销售线维持静默
        // （那些场景业务通常已由主闸处理并回复，多一条提示反而误导）。
        if (_blkLine === "procurement") {
          const _ou = [event?.senderId, event?.from?.id, event?.from?.open_id, event?.from?.userId]
            .map(String).find(v => v.includes("ou_")) || "";
          if (_ou) {
            _deliverGuideByChannel(
              `⏳ 您的消息「${String(text).replace(/\s+/g, " ").slice(0, 24)}」暂未执行（上一条消息仍在处理中），请稍候片刻后重发即可。`,
              _ou, "feishu"
            ).then(sent => {
              console.log(sent
                ? `[zhice-gateway] backstop 友好提示已直发 open_id="${_ou.slice(0, 20)}..."`
                : `[zhice-gateway] backstop 友好提示直发失败 reason="${_lastDirectSendErr}"（用户将只看到 block 报错）`);
            }).catch(e => console.log(`[zhice-gateway] backstop 友好提示直发异常: ${e.message}`));
          }
        }
        return { decision: "block", message: "业务指令由网关接管（bus 处理、门户交付），agent 不执行。", pluginId: "zhice-gateway" };
      }
      if (_interceptKw && text.includes(_interceptKw)) {
        console.log(`[zhice-gateway] before_agent_run KEYWORD HIT kw="${_interceptKw}" text="${text.slice(0, 60)}" → return {decision:block} (agent run blocked)`);
        return { decision: "block", message: "【网关拦截验证】agent 已被阻断（未跑 LLM）。验证测试，正常消息不受影响。", pluginId: "zhice-gateway" };
      }
      return undefined;
    });
    console.log("[zhice-gateway] before_agent_run hook registered");
  } catch (err) {
    console.warn("[zhice-gateway] before_agent_run register failed:", err.message);
  }

  // ── message_received → 唯一入口 ──
  // v7 handler 签名: (event, context) — event={from,content,timestamp?,metadata?}
  // v6 handler 签名: (ctx) — ctx.message.text, ctx.reply(), ctx.skip()
  // 本 handler 双向适配：对 event 做 normalize 使 ctx 风格兼容
  api.on("message_received", async (first, second) => {
    // ── 归一化：v6 (ctx) vs v7 (event, context) ──
    const ctx = (second !== undefined) ? normalizeEvent(first) : first;
    // second 是 v7 context = { channelId, accountId, conversationId }，暂不直接使用
    try {
      // ── ctx 结构调试（首次 + trace 模式）──
      if (!ctx.__dump_done) {
        ctx.__dump_done = true;
        console.log("[zhice-gateway] ctx keys:", Object.keys(ctx || {}).join(", "));
        // 安全打印前 5 层
        _trace("ctx raw: " + JSON.stringify(ctx, (k, v) => k === "__dump_done" ? undefined : v, 2).slice(0, 3000));
      }

      // ── 兼容多通道的 agentId / text 提取 ──
      // 不同通道（飞书、web、slack）ctx 结构不同，逐一尝试
      const from = ctx?.from || ctx?.sender || ctx?.user || ctx?.event?.from || {};
      const message = ctx?.message || ctx?.event?.message || ctx?.event || {};
      let agentId = (typeof from === 'string' ? from : from?.id || from?.userId || from?.open_id)
                    || ctx?.senderId
                    || ctx?.to?.id
                    || ctx?.session?.id
                    || "";
      // 多通道交付层（波哥 2026-08-08 19:30）：归一化前保留原始通道身份（飞书 open_id 等），
      // 供 deliverSkillFile 按通道直发引导消息（open_id 会被 resolveCanonicalAgentId 归一化成 agent 名）
      const rawOpenId = (typeof from === 'string' ? from : from?.open_id || from?.id || from?.userId) || "";
      const channel = (second && typeof second === 'object' && second.channelId)
                      || (typeof from === 'object' && (from.channelId || from.channel || "")) || "";
      ctx.__rawOpenId = rawOpenId;
      ctx.__channel = channel;
      // 身份归一：通道身份(open_id) → 规范 agent 名（修复 owner=bidding-feishu-2 但 execute 带 feishu:ou_xxx 的 403）
      agentId = await resolveCanonicalAgentId(agentId);
      // #1 根因防御：空 agent_id 会拉起 --agent-id="" 的通用 Skill 子进程、数据落共享目录、IPC 起不来。
      // 兜底为非空哨兵，保证下游（秘书/直通 skill/执策）始终带身份，同时在日志里留痕便于追查。
      if (!agentId) {
        agentId = "gateway:unidentified";
        console.warn(`[zhice-gateway] message has no resolvable agent_id → fallback "${agentId}" (trace for debugging)`);
      }
      const text = message?.text || message?.content || message?.rawContent || ctx?.content || ctx?.text || "";
      const meta = ctx?.metadata || {};

      // ── 三线短路（波哥 2026-08-20）：before_dispatch 已处理的投标/采购/销售消息绝不再处理 ──
      // before_dispatch 处理成功时写入 _handledSigs（senderId+text 签名，TTL 30s）；此处命中即
      // return {block:true}——不再 skillExecute、不再建任务（否则标书会生成两次）。
      if (_consumeHandledAny([rawOpenId, agentId], text)) {
        _trace(`message_received 三线短路（before_dispatch 已处理）agent=${agentId} text="${(text || "").slice(0, 50)}"`);
        // 2026-08-31 问题A配套：before_dispatch 处理过的 po_complete 消息，agent 仍会
        // 收到原始文本（handled:true 拦不住 agent run）→ 工具重做循环。此处消费放行签名
        // 并锚改写——agent 看到的是"已处理"提示而非原始下单文本（锚无业务关键词，兜底闸
        // 天然放行，签名豁免不再必要）。
        if (_businessLine(text) === "procurement" && _isPassthrough([rawOpenId, agentId], text)) {
          ctx.message.text = _PASSTHROUGH_ANCHOR;
          console.log(`[zhice-gateway] 三线短路 po_complete → 锚改写 message.text（防 agent 工具循环）`);
        }
        return { block: true };
      }
      // 三线消息短路 MISS 必留痕（此前静默 miss：二次 skillExecute 无日志可查，14:06 复测
      // 定位卡在这）。打出 agent/raw/归一文本，残留身份/文本差异一眼可见；后续由
      // execute_api 文本幂等兜底拦二次执行。
      if (config.interceptBusinessLines !== false && _businessLine(text)) {
        console.log(`[zhice-gateway] short-circuit MISS agent=${agentId} raw="${String(rawOpenId || "").slice(0, 24)}" sig_norm="${_sigNorm(text).slice(0, 50)}" text="${(text || "").slice(0, 60)}" → 秘书路径继续（execute_api 幂等兜底）`);
      }

      // ── 保障下游 ctx.message / ctx.reply / ctx.skip 可用 ──
      if (!ctx.message) ctx.message = {};
      // 吞回复根因修复（2026-08-13 方案X）：v7 下 ctx.reply 原为 no-op（存在但不发送），
      // 仅 `if (!ctx.reply)` 才会装直发 fallback，导致语义路由「replied」后用户收不到任何消息。
      // 改为无条件包装：多通道直发层（_deliverGuideByChannel → 飞书 OpenAPI）优先，直发不可用
      // （非飞书/无身份/无凭据/API 失败）才降级原 reply（v6 真实现）或日志兜底。
      const _origReply = typeof ctx.reply === "function" ? ctx.reply : null;
      ctx.reply = async (msg) => {
        const text = String(msg || "");
        const sent = await _deliverGuideByChannel(text, ctx.__rawOpenId, ctx.__channel).catch(() => false);
        if (sent) {
          _trace(`ctx.reply direct-sent open_id="${ctx.__rawOpenId || ""}" ch="${ctx.__channel || ""}" len=${text.length}`);
          return;
        }
        if (_origReply) {
          try {
            await _origReply(msg);
            return;
          } catch (e) {
            console.log(`[zhice-gateway] ctx.reply orig failed (${e.message}), fallback to log`);
          }
        }
        console.log(`[zhice-gateway] reply fallback (direct send unavailable reason="${_lastDirectSendErr || "unknown"}" open_id="${ctx.__rawOpenId || ""}" ch="${ctx.__channel || ""}"): ${text.slice(0, 100)}`);
      };
      if (!ctx.skip) ctx.skip = () => {};

      // 身份提取明细 trace：每次消息记录 from 各候选字段 + 最终 agentId（定位身份错取/泄露）
      _trace(`msg from=${typeof from === 'string' ? '"'+from+'"' : 'id:"'+(from?.id||'')+'" userId:"'+(from?.userId||'')+'" open_id:"'+(from?.open_id||'')+'"'} → agent="${agentId}"`);
      _trace(`message_received agent=${agentId} text="${(text||"").slice(0, 80)}" len=${(text||"").length} task=${meta.zhice_task_id || "none"}`);

      // ── 新 agent 自动注册到寰宇目录 ──
      _ensureAgentRegistered(agentId, text).catch(() => {});

      // 上下文压缩已交由 OpenClaw v7 内置 compaction 处理
      // Yongheng 记忆提供持久备份 — 新会话恢复近两天记忆即可

      // ── 路径 A0：审批回复检测（优先）──
      // 匹配: "允许" / "拒绝"（纯文字，无 ID）/ "允许 ABC123"（带 ID）
      const approvalMatch = text.match(/^(允许|拒绝|批准|同意|approve|reject|deny)\s*(\w*)$/i);
      if (approvalMatch && text.trim().length <= 10 && config.approverIds.length > 0 && !config.approverIds.includes(agentId)) {
        console.log(`[zhice-gateway] Approval ignored: ${agentId} not in approver list`);
      } else if (approvalMatch && text.trim().length <= 10) {
        _trace(`路径A0 审批检测 agent=${agentId} text="${text}"`);
        const isApprove = /^(允许|批准|同意|approve)/i.test(approvalMatch[1]);
        const decision = isApprove ? "approved" : "rejected";
        const explicitId = approvalMatch[2] || "";
        const apiBase = config.zhiceEndpoint.replace("/v1/zhice", "");

        // 有显式 ID → 直接 resolve；无 ID → 查最近 pending 审批
        let resolveUrl;
        if (explicitId.length >= 6) {
          resolveUrl = `${apiBase}/v1/zhenyue/approvals/${encodeURIComponent(explicitId)}/resolve?decision=${decision}&approver=${encodeURIComponent(agentId)}`;
        } else {
          resolveUrl = `${apiBase}/v1/zhenyue/approvals/resolve-latest?decision=${decision}&approver=${encodeURIComponent(agentId)}`;
        }

        try {
          const resp = await fetch(resolveUrl, { method: "POST" });
          const result = resp.ok ? await resp.json().catch(() => ({})) : {};
          const label = isApprove ? "已通过" : "已拒绝";
          const actionDesc = result.action || "未知操作";
          const agentDesc = result.agent_id || "";

          // 直接回复审批结果，跳过 LLM
          const hint = isApprove
            ? `✅ 审批已通过: ${actionDesc}${agentDesc ? ` (${agentDesc})` : ""}\nAgent 重新执行命令即可，5分钟内无需再次审批。`
            : `❌ 审批已拒绝: ${actionDesc}`;
          ctx.reply(hint);
          ctx.skip();
          console.log(`[zhice-gateway] Approval ${decision} by ${agentId}: ${actionDesc}`);
        } catch (err) {
          console.error(`[zhice-gateway] Approval resolve failed: ${err.message}`);
          ctx.reply(`⚠️ 审批处理失败: ${err.message}`);
          ctx.skip();
        }
        return { block: true };
      }

      // ── 路径 B：Agent 回复执策步骤 → 自动拉下一步 ──
      if (meta.zhice_task_id) {
        _trace(`路径B 步骤延续 task=${meta.zhice_task_id} agent=${agentId}`);

        const nextData = await zhiceGet(
          `/tasks/${meta.zhice_task_id}/next?agent_id=${encodeURIComponent(agentId)}`
        );

        if (!nextData || !nextData.current_step) {
          console.log(`[zhice-gateway] Task #${meta.zhice_task_id} — no more steps, completed!`);
          stopProgressReport(meta.zhice_task_id);
          return { block: true };
        }

        const nextStep = buildStepContext({
          taskId: meta.zhice_task_id,
          stepId: nextData.current_step.step_id,
          stepIndex: nextData.current_step.step_index,
          totalSteps: nextData.total_steps || nextData.current_step.step_index,
          instruction: nextData.current_step.instruction,
          progress: nextData.progress,
        });

        // 修改消息文本，注入下一步上下文
        const prepended = `${nextStep}\n\n---\n${text}`;
        ctx.message.text = prepended;
        ctx.message.metadata = {
          ...meta,
          zhice_step_id: nextData.current_step.step_id,
          zhice_step_index: nextData.current_step.step_index,
          zhice_auto_next: true,
        };
        console.log(
          `[zhice-gateway] → Task #${meta.zhice_task_id} Step ${nextData.current_step.step_index} auto-injected`
        );
        startProgressReport(meta.zhice_task_id, agentId, ctx);
        return;
      }

      // ── 路径 A：新消息 → 创建 Task + 拉 Step 1 ──
      if (!isEnabledFor(agentId)) return;
      if (!text) return;

      // ── 路径 A0.5：系统/进度/通知消息（非用户指令）→ 直接跳过，不路由任何 skill ──
      // 2026-08-11 大师实锤死循环：投标 skill 进度广播（⏳ 正在生成投标文件…/✅ 生成完成）
      // 被语义路由误判成新 generate_bid → 又触发新一轮生成 → 死循环（id 85-89）。
      // 进度消息由系统自身产生，绝不是用户指令，必须在秘书路由/关键词拦截之前拦截。
      if (_isProgressNotice(text, from, agentId)) {
        _trace(`路径A0.5 系统/进度消息跳过 agent=${agentId} text="${(text||"").slice(0, 50)}"`);
        return;
      }

      if (isCasualChat(text)) {
        _trace(`路径闲聊 放行 agent=${agentId} len=${text.length}`);
        return;
      }

      if (isInterruptCommand(text)) {
        _trace(`路径中断 agent=${agentId} text="${text}"`);
        const cancelled = await cancelCurrentTask(agentId);
        if (cancelled) {
          // 直接回复取消消息，跳过 LLM
          ctx.reply("⏹ 当前任务已取消。已回到待命状态，请发送新的指令。");
          ctx.skip();
          console.log(`[zhice-gateway] ${agentId} interrupted — task cancelled, standby`);
        }
        return { block: true };
      }

      // ── 路径 S：秘书模式 — 探针→执行，复杂操作→执策 ──
      // 系统间消息（inbox投递）跳过秘书，直接走执策/InboxScanner
      const _isSystemMsg = /^(inquiry_id|quote_id|counter_id|negotiation_id|message_id)[:\s]/.test(text) ||
                           text.startsWith('{"') || text.startsWith('[{');
      if (_isSystemMsg) {
        _trace(`路径S 系统消息跳过秘书 agent=${agentId} text=${text.slice(0, 60)}`);
      } else if (await handleSecretaryPath(text, agentId, ctx, rawOpenId)) {
        _trace(`路径S 秘书处理 agent=${agentId}`); return { block: true };
      }
      // 方案X（2026-08-13 波哥定调）：采购下单/补齐/续答已由 skill 落库并回复确认，
      // 放行 agent 原始处理（不 block），但跳过执策——避免询价任务模板（发送询价→收集
      // 报价→比价）对同一采购消息重复建任务。
      if (ctx.__skillHandledOrder) {
        _trace(`路径S 采购下单已由 skill 处理→放行 agent agent=${agentId}`);
        return;
      }
      _trace(`路径S 秘书passthrough → 交执策 agent=${agentId}`);

      console.log(`[zhice-gateway] ${agentId}: "${text.slice(0, 60)}..." (${text.length} chars)`);

      const unfinishedTaskId = await checkUnfinishedTask(agentId);
      if (unfinishedTaskId) {
        ctx.reply(`⚠️ 你有一个未完成的任务 #${unfinishedTaskId}。\n发送"取消"可中断它，30分钟未完成系统会自动取消。`);
        ctx.skip();
        console.log(`[zhice-gateway] ${agentId} blocked — must finish task #${unfinishedTaskId} first`);
        return { block: true };
      }

      _trace(`路径A 新消息→创建任务 agent=${agentId} text="${text.slice(0, 60)}"`);
      // 秘书 probe 已高置信度识别意图 → 跳过执策的模糊检测
      const probeSaved = meta.zhice_probe_result;
      const skipClarity = !!(probeSaved && probeSaved.target_skill && probeSaved.confidence >= 0.7);
      const stepData = await createAndFetchNext(agentId, text, skipClarity);
      if (!stepData) {
        _trace(`路径A 任务创建失败 agent=${agentId} (无响应)`);
        return;
      }

      // ── needs_clarification：追问返回给用户 ──
      if (stepData.clarification) {
        const q = stepData.clarification;
        const questions = q.questions || [];
        const replyText = "❓ 我需要补充一些信息来理解你的需求：\n" +
          (questions.length ? questions.map((x, i) => `${i + 1}. ${x}`).join("\n")
                           : "请描述得更具体一些，比如需要做什么、达到什么效果。");
        ctx.reply(replyText);
        ctx.skip();
        _trace(`路径A 需要澄清 agent=${agentId} questions=${questions.length}`);
        return { block: true };
      }

      // ── HTTP 步骤：插件直接执行，跳过 Agent ──
      if (stepData.execType === "http") {
        _trace(`路径A http步骤→直接执行 agent=${agentId} step=${stepData.stepIndex}`);
        console.log(`[zhice-gateway] Executing HTTP step #${stepData.taskId}/${stepData.stepIndex}: ${(stepData.instruction||"").slice(0,100)}`);

        const result = await executeHttpStep(stepData.instruction);
        if (result.error && result.error !== "cannot_parse") {
          console.error(`[zhice-gateway] HTTP step failed: ${result.error}`);
        }

        // 标记 step 为 in_progress（否则 submit 会 409）
        await startStep(stepData.stepId, agentId);

        // 提交步骤
        const submitResult = await submitStep(
          stepData.taskId, stepData.stepId, agentId,
          result.ok ? "completed" : "failed",
          result.ok ? "HTTP 步骤自动执行成功" : `HTTP 步骤执行失败: ${result.error || result.raw?.slice(0,200)}`,
          result
        );

        // 检查是否有下一步
        const nextData2 = await zhiceGet(
          `/tasks/${stepData.taskId}/next?agent_id=${encodeURIComponent(agentId)}`
        );

        if (nextData2 && nextData2.current_step) {
          // 有后续步骤 → 注入到 Agent 上下文
          const nextStepText = buildStepContext({
            taskId: stepData.taskId,
            stepId: nextData2.current_step.step_id,
            stepIndex: nextData2.current_step.step_index,
            totalSteps: stepData.totalSteps,
            instruction: nextData2.current_step.instruction,
            progress: nextData2.progress,
          });

          const resultSummary = result.ok
            ? `✅ 上一步（步骤 ${stepData.stepIndex}/${stepData.totalSteps}）已自动执行完成。`
            : `⚠️ 上一步（步骤 ${stepData.stepIndex}/${stepData.totalSteps}）执行异常: ${result.error || `HTTP ${result.status}`}`;

          ctx.message.text = `${resultSummary}\n\n---\n${nextStepText}`;
          ctx.message.metadata = {
            ...meta,
            zhice_task_id: stepData.taskId,
            zhice_step_id: nextData2.current_step.step_id,
            zhice_step_index: nextData2.current_step.step_index,
            zhice_auto_next: true,
          };
          console.log(`[zhice-gateway] → Task #${stepData.taskId} Step ${nextData2.current_step.step_index} auto-injected after http exec`);
        } else {
          // 任务完成
          let replyText = result.ok
            ? `✅ 任务已处理完成。\n\n服务器返回: ${JSON.stringify(result.body || result.raw).slice(0, 500)}`
            : `⚠️ 任务处理遇到问题: ${result.error || "HTTP " + result.status}`;
          ctx.reply(replyText);
          ctx.skip();
          _trace(`路径A http步骤完成 agent=${agentId} task=${stepData.taskId}`);
        }
        return { block: true };
      }

      // ── Skill 步骤：插件直接调 Skill API，跳过 Agent ──
      if (stepData.execType === "skill") {
        _trace(`路径A skill步骤→直接执行 agent=${agentId} step=${stepData.stepIndex}`);
        console.log(`[zhice-gateway] Executing Skill step #${stepData.taskId}/${stepData.stepIndex}: ${(stepData.instruction||"").slice(0,100)}`);

        // 标记 in_progress 再执行（否则 submit 会 409）
        await startStep(stepData.stepId, agentId);

        const result = await executeSkillStep(stepData.instruction, stepData.params, agentId);
        if (result.error && result.error !== "cannot_parse") {
          console.error(`[zhice-gateway] Skill step failed: ${result.error}`);
        }

        // 提交步骤
        const submitResult = await submitStep(
          stepData.taskId, stepData.stepId, agentId,
          result.ok ? "completed" : "failed",
          result.ok ? "Skill 步骤自动执行成功" : `Skill 步骤执行失败: ${result.error || result.raw?.slice(0,200)}`,
          result
        );

        // 检查是否有下一步
        const nextData2 = await zhiceGet(
          `/tasks/${stepData.taskId}/next?agent_id=${encodeURIComponent(agentId)}`
        );

        if (nextData2 && nextData2.current_step) {
          const nextStepText = buildStepContext({
            taskId: stepData.taskId,
            stepId: nextData2.current_step.step_id,
            stepIndex: nextData2.current_step.step_index,
            totalSteps: stepData.totalSteps,
            instruction: nextData2.current_step.instruction,
            progress: nextData2.progress,
          });

          const resultSummary = result.ok
            ? `✅ 上一步（步骤 ${stepData.stepIndex}/${stepData.totalSteps}）已自动执行完成。`
            : `⚠️ 上一步（步骤 ${stepData.stepIndex}/${stepData.totalSteps}）执行异常: ${result.error || `HTTP ${result.status}`}`;

          ctx.message.text = `${resultSummary}\n\n---\n${nextStepText}`;
          ctx.message.metadata = {
            ...meta,
            zhice_task_id: stepData.taskId,
            zhice_step_id: nextData2.current_step.step_id,
            zhice_step_index: nextData2.current_step.step_index,
            zhice_auto_next: true,
          };
          console.log(`[zhice-gateway] → Task #${stepData.taskId} Step ${nextData2.current_step.step_index} auto-injected after skill exec`);
        } else {
          // 任务完成
          let replyText = result.ok
            ? `✅ 任务已处理完成。\n\nSkill 返回: ${JSON.stringify(result.body).slice(0, 500)}`
            : `⚠️ 任务处理遇到问题: ${result.error || "skill error"}`;
          ctx.reply(replyText);
          ctx.skip();
          _trace(`路径A skill步骤完成 agent=${agentId} task=${stepData.taskId}`);
        }
        return;
      }

      // 修改消息文本，注入步骤上下文（非 HTTP/Skill 步骤）
      const stepCtx = buildStepContext(stepData);
      ctx.message.text = stepCtx;
      ctx.message.metadata = {
        ...meta,
        zhice_task_id: stepData.taskId,
        zhice_step_id: stepData.stepId,
        zhice_step_index: stepData.stepIndex,
        zhice_auto_registered: true,
      };
      console.log(
        `[zhice-gateway] → Task #${stepData.taskId} Step ${stepData.stepIndex}/${stepData.totalSteps} injected`
      );
      // 长任务启动进度播报（每 20s 汇报进展）
      startProgressReport(stepData.taskId, agentId, ctx);
    } catch (err) {
      console.error(`[zhice-gateway] Unhandled hook error: ${err.message}`, err.stack || "");
    }
  });

  // ── HTTP Route: /zhice/config ──
  api.registerHttpRoute({
    path: "/plugin/zhice/config",
    auth: "gateway",
    handler: async (_req, res) => {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({
        endpoint: config.zhiceEndpoint,
        minInstructionLength: config.minInstructionLength,
        enforceStepByStep: config.enforceStepByStep,
        excludePatterns: config.excludePatterns,
        enabledAgents: config.enabledAgents,
      }));
      return true;
    },
  });

  // ── 启动时扫描 OpenClaw agent，未注册的补登记到 huanyu + zhenyue ──
  // registerPlugin 为同步函数，裸 await 会让整个文件解析失败（zhice-gateway 加载崩溃线上事故），
  // 故用 async IIFE 包住，fire-and-forget 异步执行、不阻塞插件注册。
  (async () => {
    try {
      const base = config.zhiceEndpoint.replace(/\/v1\/zhice.*/, "");
      const resp = await fetch(`${base}/v1/huanyu/agents`);
      const existing = new Set(((await resp.json())?.agents || []).map(a => a.agent_id));
      // OpenClaw 侧已知的 agent（通过 _agent_ids 或注入）
      const scanList = [];
      if (typeof api?.listAgents === "function") {
        const oa = await api.listAgents();
        if (Array.isArray(oa)) scanList.push(...oa.map(a => a.id || a.agent_id || a.name));
      }
      for (const aid of scanList) {
        if (!aid || existing.has(aid)) continue;
        try {
          await fetch(`${base}/v1/huanyu/agents/register`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: aid, category: "biz:buyer", agent_id: aid }),
          });
          console.log(`[zhice-gateway] startup scan: registered agent ${aid}`);
        } catch (_) {}
      }
    } catch (_) { /* 静默，不阻塞启动 */ }
  })();

  console.log("[zhice-gateway v3.1] Ready — OpenClaw v7.x message_received + registerHttpRoute");
};

// 导出插件入口（兼容 v7 和 v6）
// v7: 如 OpenClaw 支持 definePluginEntry，优选新 API
// v6/v7 兼容: module.exports = function(api) 在 v7 仍有效（有迁移警告）
module.exports = registerPlugin;

// 测试钩子（node 直连单测用，宿主不受影响——纯函数无副作用）
// 二十三轮（2026-08-28）文件ID形态路由：test_bid_fileid_routing.js
registerPlugin._isBidCommand = _isBidCommand;
registerPlugin._businessLine = _businessLine;
registerPlugin._bidFileIdOnly = _bidFileIdOnly;
