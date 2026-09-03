/**
 * 镇岳 OpenClaw Plugin — 第一层拦截：工具调用门控
 *
 * 接入方式：OpenClaw api.on("before_tool_call", handler)
 * 在大模型决定调用工具后、工具实际执行前被调用。
 *
 * 部署：将此目录放到 OpenClaw Gateway 的 plugins 目录下。
 */

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const yaml = require("js-yaml");

// ── 配置 ────────────────────────────────────────────
const CONFIG_PATH = path.join(__dirname, "config.yaml");
const RULES_PATH = process.env.ZHENYUE_TOOL_RULES_PATH || CONFIG_PATH;

let config = {};
let compiledRules = [];
let allowList = [];

function loadConfig() {
  try {
    const raw = fs.readFileSync(RULES_PATH, "utf8");
    config = yaml.load(raw) || {};
    compiledRules = compileRules(config.rules || []);
    allowList = (config.allowlist || []).map((item) => ({
      ...item,
      tool: item.tool,
      _field: item.field || "command",
      match: item.match ? new RegExp(item.match) : null,
    }));
    console.log(
      `[zhenyue-plugin] Loaded ${compiledRules.length} rules, ${allowList.length} allowlist entries`
    );
  } catch (err) {
    console.error(`[zhenyue-plugin] Failed to load config: ${err.message}`);
    compiledRules = [];
    allowList = [];
  }
}

// ── 规则编译 ─────────────────────────────────────────
function compileRules(rawRules) {
  const severityOrder = { block: 0, require_approval: 1, log_only: 2 };
  const rules = rawRules
    .filter((r) => r.tool && r.match != null)
    .map((r) => ({
      ...r,
      _regex: r.match ? new RegExp(r.match) : null,
      _field: r.field || "command",
    }));

  rules.sort(
    (a, b) =>
      (severityOrder[a.severity] ?? 99) - (severityOrder[b.severity] ?? 99)
  );
  return rules;
}

// ── 匹配引擎 ─────────────────────────────────────────
function matchRule(toolName, params) {
  // 快速通道：allowlist
  for (const entry of allowList) {
    if (entry.tool !== toolName) continue;
    if (!entry.match) return null; // 完全豁免此工具
    const fieldValue = String(params[entry._field] || params.command || "");
    if (entry.match.test(fieldValue)) return null; // 豁免此匹配
  }

  // 工具名索引查找
  const candidates = compiledRules.filter((r) => r.tool === toolName);
  if (candidates.length === 0) return null;

  for (const rule of candidates) {
    const fieldValue = String(params[rule._field] || params.command || "");
    if (rule._regex && rule._regex.test(fieldValue)) {
      return rule;
    }
  }
  return null;
}

// ── 审计钩子 ─────────────────────────────────────────
async function writeAuditLog(entry) {
  const auditEndpoint =
    config.audit?.endpoint || "http://127.0.0.1:1996/v1/zhenyue/audit/entry";
  try {
    await fetch(auditEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        agent_id: entry.agent_id || "sys-eng",
        agent_role: entry.agent_role || "agent",
        action: `tool:${entry.tool_name}`,
        target_type: "tool_call",
        target_id: entry.tool_name,
        severity: entry.severity || "low",
        detail: {
          source_layer: "plugin",
          tool_name: entry.tool_name,
          tool_params: entry.tool_params,
          decision: entry.decision,
          session_key: entry.session_key || "",
        },
        approval_status: entry.decision === "blocked" ? "denied" : "auto",
      }),
    });
  } catch (err) {
    console.error(`[zhenyue-plugin] Audit write failed: ${err.message}`);
  }
}

// ── 文件热加载 ──────────────────────────────────────
let watchInitialized = false;
function initWatcher() {
  if (watchInitialized) return;
  watchInitialized = true;

  try {
    fs.watch(path.dirname(RULES_PATH), (eventType, filename) => {
      if (filename === path.basename(RULES_PATH)) {
        console.log(`[zhenyue-plugin] Rules file changed, reloading...`);
        loadConfig();
      }
    });
  } catch (err) {
    console.warn(
      `[zhenyue-plugin] fs.watch not available, rules reload via API only`
    );
  }
}

// ── 插件入口 ─────────────────────────────────────────
module.exports = function zhenyueGuardPlugin(api) {
  console.log("[zhenyue-plugin] Initializing Zhenyue Guard Plugin v2.1");

  loadConfig();
  initWatcher();

  // v7.1: before_tool_call handler — 必须显式 allow() 或 deny()
  // 兼容 v6.11: event.toolName/params; 兼容 v7.1: event.tool?.name/params
  api.on("before_tool_call", async (event) => {
    const tool = event.tool || event;
    const toolName = tool.name || event.toolName;
    const params = tool.params || event.params || {};
    const agentId = event.agentId || (event.sessionKey || "").split(":")[1] || "sys-eng";
    const sessionKey = event.sessionKey || "";

    const matchedRule = matchRule(toolName, params);
    if (!matchedRule) {
      event.allow?.(); // 7.1: 显式放行
      return; // 无匹配 → 放行
    }

    const decision = matchedRule.severity;

    // ── log_only：仅审计，放行 ──
    if (decision === "log_only") {
      await writeAuditLog({
        tool_name: toolName,
        tool_params: params,
        decision: "allowed",
        severity: "low",
        agent_id: agentId,
        session_key: sessionKey,
      });
      event.allow?.(); // 7.1: 显式放行
      return;
    }

    // ── block：直接拦截 ──
    if (decision === "block") {
      await writeAuditLog({
        tool_name: toolName,
        tool_params: params,
        decision: "blocked",
        severity: "critical",
        agent_id: agentId,
        session_key: sessionKey,
      });
      return {
        block: true,
        blockReason: matchedRule.reason || `禁止危险操作: ${toolName}`,
      };
    }

    // ── require_approval：使用 SDK 原生 requireApproval 机制 ──
    // SDK 自动: 创建审批 → 投递到审批通道 → 处理 /approve 命令 → 回调 onResolution
    // 不需要手工 HTTP 调镇岳 API、不需要匹配回复文字、不需要 sendApprovalDM
    if (decision === "require_approval") {
      const sev = matchedRule.approval_severity || "high";
      const reason = matchedRule.reason || "危险操作";
      const paramSummary = JSON.stringify(params).slice(0, 120);

      await writeAuditLog({
        tool_name: toolName, tool_params: params,
        decision: "approval_pending", severity: sev,
        agent_id: agentId, session_key: sessionKey,
      });

      return {
        requireApproval: {
          title: `${toolName} [${sev}]`,
          description: [
            `操作: ${toolName}`,
            `参数: ${paramSummary}`,
            `执行者: ${agentId}`,
            `风险: ${reason}`,
          ].join(" | ").slice(0, 256),
          severity: sev === "critical" ? "critical" : "warning",
          timeoutMs: sev === "critical" ? 300_000 : 600_000,
          timeoutBehavior: "deny",
          onResolution(decision) {
            console.log(`[zhenyue-plugin] 审批决议: ${toolName} → ${decision} (${agentId})`);
          },
        },
      };
    }
  });

  // ── API 端点：动态规则管理（运行时） ──
  api.registerHttpRoute({
    path: "/plugin/zhenyue/rules",
    auth: "gateway",
    handler: async (_req, res) => {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(compiledRules.map((r) => ({
        tool: r.tool,
        match: r.match,
        field: r._field,
        severity: r.severity,
        approval_severity: r.approval_severity || "high",
        reason: r.reason || "",
      }))));
      return true;
    },
  });

  api.registerHttpRoute({
    path: "/plugin/zhenyue/rules/reload",
    auth: "gateway",
    handler: async (_req, res) => {
      loadConfig();
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ status: "ok", count: compiledRules.length }));
      return true;
    },
  });

  console.log(
    `[zhenyue-plugin] Ready — ${compiledRules.length} rules loaded`
  );
};
