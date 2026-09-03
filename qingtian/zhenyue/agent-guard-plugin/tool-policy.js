/**
 * 镇岳 OpenClaw Plugin — 工具规则匹配引擎
 *
 * 此文件为 index.js 提供规则编译和匹配逻辑的独立测试入口。
 * 生产环境通过 index.js 集成到 OpenClaw 生命周期。
 */

const yaml = require("js-yaml");
const fs = require("fs");

// ── 规则编译 ─────────────────────────────────────────
function compileRules(rawRules) {
  const severityOrder = { block: 0, require_approval: 1, log_only: 2 };
  return rawRules
    .filter((r) => r.tool && r.match != null)
    .map((r) => ({
      ...r,
      _regex: new RegExp(r.match),
      _field: r.field || "command",
    }))
    .sort(
      (a, b) =>
        (severityOrder[a.severity] ?? 99) - (severityOrder[b.severity] ?? 99)
    );
}

// ── 规则匹配 ─────────────────────────────────────────
function matchRule(compiledRules, allowList, toolName, params) {
  // 快速通道：allowlist
  for (const entry of allowList) {
    if (entry.tool !== toolName) continue;
    if (!entry.match) return null;
    const fieldValue = String(params[entry._field] || params.command || "");
    if (entry.match.test(fieldValue)) return null;
  }

  // 工具名索引查找
  const candidates = compiledRules.filter((r) => r.tool === toolName);
  for (const rule of candidates) {
    const fieldValue = String(params[rule._field] || params.command || "");
    if (rule._regex && rule._regex.test(fieldValue)) {
      return rule;
    }
  }
  return null;
}

module.exports = { compileRules, matchRule };
