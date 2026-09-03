#!/usr/bin/env python3
"""Skill 模板生成器 — `qingtian skill create`

快速生成 Skill 脚手架，含 skill.json / main.py / README.md / tests/ 目录。

用法:
    python scripts/create_skill_template.py my_skill --display-name "我的技能" \\
        --category tool --description "做什么的" --author "你的名字"

输出:
    ./my_skill/
      skill.json        — 元数据（已签名占位，可 later 用 generate-dev-certs.py 补签名）
      main.py           — Skill 入口（BaseProductSkill 或通用 Skill 模板）
      README.md         — 文档
      tests/
        __init__.py
        test_skill.py   — 基础测试
"""
import argparse
import json
import os
import sys
from pathlib import Path


SKILL_MAIN_TP = """\"\"\"{display_name} Skill — 自动生成\"\"\"
import logging
from typing import Any

from osskill.models import Skill, SkillContext

logger = logging.getLogger(__name__)


class {class_name}(Skill):
    \"\"\"{description}\"\"\"

    name = "{name}"
    version = "{version}"

    async def on_load(self, ctx: SkillContext):
        self.ctx = ctx
        logger.info("%s v%s loaded", self.name, self.version)

    async def on_unload(self, ctx: SkillContext):
        logger.info("%s unloaded", self.name)

    async def on_data_purge(self, ctx: SkillContext):
        \"\"\"个保法合规：卸载时清理用户数据。\"\"\"
        logger.info("%s data purged", self.name)

    async def execute(self, params: dict[str, Any] | None = None) -> dict:
        \"\"\"主要执行入口。params 来自 Agent 调用时的参数。\"\"\"
        logger.info("%s.execute called with params=%s", self.name, params)
        return {{"status": "ok", "result": "Hello from {name}"}}
"""

SKILL_TEST_TP = '''"""Tests for {name} Skill"""
import pytest
from {name} import {class_name}


@pytest.mark.asyncio
async def test_execute():
    skill = {class_name}()
    result = await skill.execute({{"test": True}})
    assert result["status"] == "ok"
    assert "Hello from {name}" in result["result"]
'''


def _camel(name: str) -> str:
    """snake_case → PascalCase"""
    return "".join(w.capitalize() for w in name.replace("-", "_").split("_"))


def _validate_name(name: str):
    if not name:
        raise ValueError("Skill 名称不能为空")
    if not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError("Skill 名称只允许字母、数字、下划线和连字符")
    if len(name) > 100:
        raise ValueError("Skill 名称不能超过 100 个字符")


def create_skill_template(
    name: str,
    display_name: str = "",
    description: str = "",
    category: str = "tool",
    author_name: str = "",
    author_contact: str = "",
    version: str = "1.0.0",
    output_dir: str = ".",
) -> Path:
    """生成 Skill 模板目录。"""
    _validate_name(name)
    display_name = display_name or name
    description = description or f"{display_name} Skill"
    class_name = _camel(name)

    base = Path(output_dir).resolve() / name
    base.mkdir(parents=True, exist_ok=True)

    # skill.json
    skill_json = {
        "name": name,
        "display_name": display_name,
        "version": version,
        "description": description,
        "category": category,
        "tags": [],
        "author": {
            "name": author_name or "",
            "contact": author_contact or "",
            "website": "",
        },
        "compliance": {
            "data_handling": "local",
        },
        "copyright": {
            "declaration": f"本人/本公司声明拥有本 Skill 的全部知识产权。",
            "license": "Apache-2.0",
        },
        "entry": {
            "class": f"{class_name}Skill",
            "file": f"{name}.py",
        },
        "permissions": ["llm", "skills"],
        "resources": {
            "cpu": "low",
            "memory_mb": 128,
            "api_calls_per_minute": 20,
        },
        "network": {
            "outbound": {
                "allowed": False,
                "allowed_domains": [],
            },
            "inbound": {
                "port_required": False,
                "port_range": [],
            },
        },
        "license_info": {
            "type": "free",
        },
        "compatibility": {
            "qingtian": ">=2.0.0",
            "python": ">=3.12",
            "platform": ["linux", "windows"],
        },
        "runtime": {
            "mode": "subprocess",
            "lifecycle": "resident",
            "startup_timeout_seconds": 30,
            "idle_timeout_seconds": 0,
        },
    }

    (base / "skill.json").write_text(
        json.dumps(skill_json, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # main.py
    (base / f"{name}.py").write_text(
        SKILL_MAIN_TP.format(
            name=name,
            display_name=display_name,
            description=description,
            class_name=class_name,
            version=version,
        ),
        encoding="utf-8",
    )

    # README.md
    readme = (
        f"# {display_name}\n\n"
        f"{description}\n\n"
        f"## 安装\n\n"
        f"```bash\n"
        f"osskill install {name} ./{name}\n"
        f"```\n\n"
        f"## 发布\n\n"
        f"1. 生成开发者密钥对：`python scripts/generate-dev-certs.py`\n"
        f"2. 打包：`tar czf {name}.tar.gz {name}/`\n"
        f"3. 计算 SHA256：`sha256sum {name}.tar.gz`\n"
        f"4. 提交到市场：通过 acssa.cn 开发者后台\n"
    )
    (base / "README.md").write_text(readme, encoding="utf-8")

    # tests/
    tests_dir = base / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_skill.py").write_text(
        SKILL_TEST_TP.format(name=name, class_name=class_name),
        encoding="utf-8",
    )

    print(f"[OK] Skill template created: {base}")
    print(f"  - skill.json")
    print(f"  - {name}.py")
    print(f"  - README.md")
    print(f"  - tests/")
    print()
    print("Next steps:")
    print(f"  1. cd {base} && python -m pytest tests/")
    print("  2. Edit main.py to implement your skill logic")
    print("  3. python scripts/generate-dev-certs.py to generate cert")
    print("  4. Package and submit to acssa.cn market")

    return base


def main():
    parser = argparse.ArgumentParser(description="创建 Skill 模板脚手架")
    parser.add_argument("name", help="Skill 名称（snake_case，如 bidding_parser）")
    parser.add_argument("--display-name", default="", help="展示名（如 招投标解析）")
    parser.add_argument("--description", default="", help="描述")
    parser.add_argument("--category", default="tool",
                        choices=["tool", "industry", "integration", "compliance", "data"])
    parser.add_argument("--author", default="", help="作者名")
    parser.add_argument("--contact", default="", help="联系方式")
    parser.add_argument("--version", default="1.0.0", help="版本号")
    parser.add_argument("--output-dir", default=".", help="输出目录")

    args = parser.parse_args()

    try:
        create_skill_template(
            name=args.name,
            display_name=args.display_name,
            description=args.description,
            category=args.category,
            author_name=args.author,
            author_contact=args.contact,
            version=args.version,
            output_dir=args.output_dir,
        )
    except ValueError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
