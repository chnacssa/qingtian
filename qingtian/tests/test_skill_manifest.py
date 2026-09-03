"""SkillManifest 解析器测试"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from common.skill_manifest import (
    EventDecl,
    RouteDecl,
    SkillManifest,
    _parse_manifest,
    load_manifest,
    validate_manifest,
)


# ═══════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def minimal_skill_json() -> dict:
    """最小可用 skill.json"""
    return {
        "name": "test-skill",
        "display_name": "测试 Skill",
        "version": "1.0.0",
        "description": "用于测试的最小 Skill",
        "category": "test",
        "tags": ["test"],
        "permissions": ["network"],
        "author": {
            "type": "enterprise",
            "name": "ACSSA团队",
            "contact": "dev@qingtian.dev",
        },
        "entry": {
            "class": "TestSkill",
            "file": "test_skill.py",
        },
        "runtime": {
            "mode": "subprocess",
            "lifecycle": "on_demand",
        },
    }


@pytest.fixture
def full_skill_json() -> dict:
    """完整 skill.json（包括所有可选字段）"""
    return {
        "name": "workflow",
        "display_name": "审批工作流引擎",
        "version": "1.0.0",
        "description": "企业审批工作流",
        "category": "engine",
        "tags": ["workflow", "approval"],
        "icon": "assets/icon.png",
        "permissions": ["network", "database", "llm"],
        "author": {
            "type": "enterprise",
            "name": "ACSSA团队",
            "contact": "dev@qingtian.dev",
            "website": "https://acssa.cn",
        },
        "compliance": {
            "data_handling": "local",
            "gdpr": False,
            "audit_log": True,
        },
        "copyright": {
            "declaration": "© 2026 ACSSA团队。保留所有权利。",
            "license": "Apache-2.0",
        },
        "license_info": {
            "type": "subscription",
            "retail_price_yuan": 9999,
            "trial_days": 30,
            "refund_days": 7,
        },
        "certificate": "ed25519_sig_hex",
        "entry": {
            "class": "WorkflowSkill",
            "file": "skill.py",
        },
        "compatibility": {
            "qingtian": ">=2.0.0",
            "python": ">=3.12",
        },
        "runtime": {
            "mode": "embedded",
            "lifecycle": "resident",
            "startup_timeout_seconds": 30,
        },
        "resources": {
            "cpu": "low",
            "memory_mb": 128,
            "disk_mb": 50,
            "api_calls_per_minute": 100,
            "max_concurrent_requests": 10,
        },
        "network": {
            "outbound": {"allowed": False, "allowed_domains": []},
            "inbound": {"port_required": False, "port_range": []},
        },
        "routes": [
            {
                "path": "/v1/test/orders",
                "method": "POST",
                "handler": "create_order",
                "auth": "token",
                "rate_limit": 60,
            },
            {
                "path": "/v1/test/orders/{order_id}",
                "method": "GET",
                "handler": "get_order",
                "auth": "token",
            },
        ],
        "events": {
            "emits": [
                {
                    "event": "workflow:order_created",
                    "description": "审批单已创建",
                    "payload": {"order_id": "uuid", "amount": "number"},
                }
            ],
            "subscribes": [
                {
                    "event": "payment:completed",
                    "description": "收款成功通知",
                    "handler": "on_payment_completed",
                }
            ],
        },
        "database": {
            "schema": "qingtian",
            "tables": ["wf_orders", "wf_node_history"],
            "init_sql": "schema.sql",
            "requires_pool": True,
        },
        "config": {
            "sweep_interval": {
                "type": "int",
                "default": 300,
                "description": "扫描间隔",
            }
        },
        "secrets": {
            "QINGTIAN_CERT_PRIVATE_KEY": {
                "description": "Ed25519 签名私钥",
                "required": True,
            }
        },
        "lifecycle": {
            "on_install": "ensure_schema",
            "on_startup": "init_engine",
            "on_shutdown": "stop_engine",
            "on_health_check": "health_check",
        },
        "background_tasks": [
            {
                "name": "cert_sweeper",
                "interval_seconds": 300,
                "handler": "sweep_expired_certs",
                "description": "扫描过期证书",
            }
        ],
        "dependencies": {
            "skills": {
                "zhenyue": {
                    "version": ">=1.0.0",
                    "required": True,
                    "description": "安全审计",
                }
            }
        },
        "health_check": {
            "endpoint": "/v1/workflow/health",
            "interval_seconds": 30,
            "timeout_seconds": 5,
        },
        "upgrade": {
            "strategy": "graceful",
            "max_downtime_seconds": 10,
            "rollback_enabled": True,
        },
        "monitoring": {
            "metrics_port": 0,
            "export_prometheus": False,
            "log_level": "info",
        },
        "data_dirs": [
            {
                "path": "/etc/qingtian/workflow/flows",
                "description": "流程定义文件",
                "create_if_missing": True,
            }
        ],
    }


@pytest.fixture
def temp_skill_dir(minimal_skill_json: dict) -> str:
    """创建包含 skill.json 的临时目录"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "skill.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(minimal_skill_json, f, ensure_ascii=False)
        yield d


# ═══════════════════════════════════════════════════════════
# _parse_manifest 测试
# ═══════════════════════════════════════════════════════════


class TestParseManifest:
    def test_parse_minimal(self, minimal_skill_json: dict):
        """最小配置解析"""
        m = _parse_manifest(minimal_skill_json)
        assert m.name == "test-skill"
        assert m.display_name == "测试 Skill"
        assert m.version == "1.0.0"
        assert m.entry.class_name == "TestSkill"
        assert m.entry.file == "test_skill.py"
        assert m.runtime["mode"] == "subprocess"

    def test_parse_full(self, full_skill_json: dict):
        """完整配置解析所有字段"""
        m = _parse_manifest(full_skill_json)

        # 基本信息
        assert m.name == "workflow"
        assert m.category == "engine"
        assert m.tags == ["workflow", "approval"]
        assert m.icon == "assets/icon.png"

        # 作者
        assert m.author.name == "ACSSA团队"
        assert m.author.website == "https://acssa.cn"

        # 合规
        assert m.compliance.data_handling == "local"
        assert m.compliance.gdpr is False

        # 许可
        assert m.license_info.type == "subscription"
        assert m.license_info.retail_price_yuan == 9999

        # 证书
        assert m.certificate == "ed25519_sig_hex"

        # 兼容性
        assert m.compatibility["qingtian"] == ">=2.0.0"

        # 运行时
        assert m.runtime["mode"] == "embedded"
        assert m.runtime["lifecycle"] == "resident"

        # 资源
        assert m.resources.memory_mb == 128
        assert m.resources.cpu == "low"

        # 网络
        assert m.network.outbound_allowed is False

        # 路由
        assert len(m.routes) == 2
        assert m.routes[0].path == "/v1/test/orders"
        assert m.routes[0].method == "POST"
        assert m.routes[1].rate_limit == 0  # 未设置 → 默认

        # 事件
        assert len(m.events["emits"]) == 1
        assert m.events["emits"][0].event == "workflow:order_created"
        assert len(m.events["subscribes"]) == 1
        assert m.events["subscribes"][0].handler == "on_payment_completed"

        # 数据库
        assert len(m.database.tables) == 2
        assert m.database.init_sql == "schema.sql"

        # 配置
        assert "sweep_interval" in m.config
        assert m.config["sweep_interval"].type == "int"

        # 密钥
        assert "QINGTIAN_CERT_PRIVATE_KEY" in m.secrets
        assert m.secrets["QINGTIAN_CERT_PRIVATE_KEY"].required is True

        # 生命周期
        assert m.lifecycle.on_startup == "init_engine"
        assert m.lifecycle.on_shutdown == "stop_engine"

        # 后台任务
        assert len(m.background_tasks) == 1
        assert m.background_tasks[0].interval_seconds == 300

        # 依赖
        assert "skills" in m.dependencies
        assert "zhenyue" in m.dependencies["skills"]

        # 健康检查
        assert m.health_check.endpoint == "/v1/workflow/health"

        # 升级
        assert m.upgrade.strategy == "graceful"

        # 监控
        assert m.monitoring.log_level == "info"

        # 数据目录
        assert len(m.data_dirs) == 1

    def test_parse_empty(self):
        """空字典解析 — 应返回默认值"""
        m = _parse_manifest({})
        assert m.name == ""
        assert m.version == "1.0.0"
        assert len(m.routes) == 0
        assert len(m.background_tasks) == 0
        assert m.resources.memory_mb == 128

    def test_parse_permissions(self):
        """permissions 字段解析"""
        m = _parse_manifest({"permissions": ["network", "llm", "system"]})
        assert "system" in m.permissions
        assert len(m.permissions) == 3

    def test_parse_no_author(self):
        """没有 author 字段 — 默认值"""
        m = _parse_manifest({"name": "test"})
        assert m.author.type == "enterprise"
        assert m.author.name == ""


# ═══════════════════════════════════════════════════════════
# load_manifest 测试
# ═══════════════════════════════════════════════════════════


class TestLoadManifest:
    def test_load_from_directory(self, temp_skill_dir: str):
        """从目录加载 skill.json"""
        m = load_manifest(temp_skill_dir)
        assert m is not None
        assert m.name == "test-skill"

    def test_load_from_file_path(self, temp_skill_dir: str):
        """从完整文件路径加载"""
        path = os.path.join(temp_skill_dir, "skill.json")
        m = load_manifest(path)
        assert m is not None
        assert m.name == "test-skill"

    def test_load_nonexistent_path(self):
        """不存在路径返回 None"""
        m = load_manifest("/nonexistent/path/skill.json")
        assert m is None

    def test_load_invalid_json(self):
        """无效 JSON 返回 None"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "skill.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{invalid json}")
            m = load_manifest(path)
            assert m is None


# ═══════════════════════════════════════════════════════════
# validate_manifest 测试
# ═══════════════════════════════════════════════════════════


class TestValidateManifest:
    def test_validate_valid_subprocess(self, minimal_skill_json: dict):
        """subprocess 模式 — 基础校验通过"""
        m = _parse_manifest(minimal_skill_json)
        errors = validate_manifest(m)
        assert errors == []

    def test_validate_missing_name(self):
        """缺少 name"""
        m = _parse_manifest({"entry": {"class": "X", "file": "x.py"}})
        errors = validate_manifest(m)
        assert "name is required" in errors

    def test_validate_missing_entry_class(self):
        """缺少 entry.class"""
        m = _parse_manifest({"name": "test", "entry": {"file": "x.py"}})
        errors = validate_manifest(m)
        assert "entry.class is required" in errors

    def test_validate_missing_entry_file(self):
        """缺少 entry.file"""
        m = _parse_manifest({"name": "test", "entry": {"class": "X"}})
        errors = validate_manifest(m)
        assert "entry.file is required" in errors

    def test_validate_embedded_no_cert(self):
        """embedded 模式要求已签名"""
        m = _parse_manifest({
            "name": "test",
            "entry": {"class": "X", "file": "x.py"},
            "runtime": {"mode": "embedded"},
        })
        errors = validate_manifest(m)
        assert any("certificate" in e for e in errors)

    def test_validate_embedded_with_cert_verified(self):
        """embedded 模式 + 已签名 + 无 system 权限 = 通过"""
        m = _parse_manifest({
            "name": "test",
            "entry": {"class": "X", "file": "x.py"},
            "runtime": {"mode": "embedded"},
            "permissions": ["network"],
        })
        m._cert_verified = True
        errors = validate_manifest(m)
        assert errors == []

    def test_validate_embedded_system_permission(self):
        """embedded 模式禁止 system 权限"""
        m = _parse_manifest({
            "name": "test",
            "entry": {"class": "X", "file": "x.py"},
            "runtime": {"mode": "embedded"},
            "permissions": ["system"],
        })
        m._cert_verified = True
        errors = validate_manifest(m)
        assert any("system" in e and "forbids" in e for e in errors)

    def test_validate_invalid_http_method(self):
        """无效 HTTP method"""
        m = _parse_manifest({
            "name": "test",
            "entry": {"class": "X", "file": "x.py"},
            "routes": [{"path": "/test", "method": "OPTIONS"}],
        })
        errors = validate_manifest(m)
        assert any("invalid HTTP method" in e for e in errors)

    def test_validate_event_naming(self, full_skill_json: dict):
        """事件命名规范检查"""
        m = _parse_manifest(full_skill_json)
        errors = validate_manifest(m)
        # 完整配置应该通过
        assert all("event" not in e for e in errors)

    def test_validate_workflow_skill_json(self):
        """加载实际 workflow 的 skill.json 并校验"""
        # 从项目路径加载
        base = Path(__file__).resolve().parent.parent
        workflow_skill_path = str(base / "osskill" / "implementations" / "workflow" / "skill.json")
        m = load_manifest(workflow_skill_path)
        assert m is not None
        assert m.name == "workflow"
        # 标记为已验证（实际环境中由 Ed25519 验证）
        m._cert_verified = True
        errors = validate_manifest(m)
        assert errors == []

    def test_validate_work_secretary_skill_json(self):
        """加载实际 work_secretary 的 skill.json 并校验"""
        base = Path(__file__).resolve().parent.parent
        ws_path = str(base / "osskill" / "implementations" / "work_secretary" / "skill.json")
        m = load_manifest(ws_path)
        assert m is not None
        assert m.name == "work_secretary"
        errors = validate_manifest(m)
        assert errors == []
