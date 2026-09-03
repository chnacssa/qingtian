# 贡献指南

欢迎为ACSSA 智能体操作系统做贡献！本文档说明如何提交代码、报告问题和参与社区。

## 行为准则

- 尊重他人，就事论事
- 欢迎不同观点，但保持专业
- 不接受歧视、骚扰或攻击性言论

## 如何贡献

### 报告 Bug

1. 在 GitHub Issues 搜索是否已有相同问题
2. 如果没有，新建 Issue，包含：
   - ACSSA版本号（`GET /version`）
   - 操作系统和 Python 版本
   - 复现步骤
   - 预期行为 vs 实际行为
   - 相关日志（如有）

### 提 Feature Request

1. 先在 Discussions 区讨论想法
2. 明确：解决什么问题、谁会用、为什么需要底座支持

### 提交代码

1. **Fork** 本仓库
2. 创建 feature 分支：`git checkout -b feature/your-feature`
3. 写代码 + 测试
4. 确保测试通过：`pytest tests/ -v`
5. 提交：`git commit -m "feat: 简短描述"`
6. Push 并创建 Pull Request

### Commit 规范

```
feat: 新功能
fix: 修复 bug
docs: 文档变更
refactor: 重构（不改功能）
test: 测试相关
chore: 构建/工具链
```

### 代码规范

- Python 3.12+，类型注解
- 公共函数/类必须写 docstring
- 新模块需在对应 `tests/` 目录下加测试
- 不引入新的外部依赖，除非有明确理由

### 测试

```bash
# 全部测试
pytest tests/ -v

# 单模块
pytest huanyu/tests/ -v

# 跳过集成测试（需要 PG + Redis）
pytest tests/ -v -m "not integration"
```

## CLA（贡献者许可协议）

提交代码即表示你同意：
- 你的贡献以 Apache 2.0 协议授权
- 你有权授予此授权（代码是你写的，或你有权提交）
- 对于企业员工：请确认你的雇主知晓并同意

## 开发环境

```bash
git clone https://github.com/chnacssa/qingtian.git
cd qingtian/qingtian
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.yaml.example config.yaml
# 编辑 config.yaml，配置 DEEPSEEK_API_KEY
python3 main.py
```

## 目录结构

```
qingtian/
├── common/       # 公共基础设施（配置/DB/LLM）
├── huanyu/       # 寰宇 — Agent 通信目录
├── yongheng/     # 永恒 — 记忆检索
├── xixing/       # 吸星 — 知识进化
├── huichuan/     # 汇川 — 知识管理
├── zhenyue/      # 镇岳 — 安全审计
├── zhice/        # 执策 — 任务编排
├── siku/         # 司库 — 账户计费
├── gateway/      # 网关 — 中间件
├── xihe/         # 羲和 — Agent 运行时
├── osskill/      # Skill 框架
│   └── implementations/  # 内建 Skill
├── tests/        # 测试
└── scripts/      # 脚本
```

## 代码审查

所有 PR 需至少一位 maintainer 审查通过。审查关注：
- 功能是否正确
- 测试覆盖是否充分
- 安全风险（SQL 注入/路径遍历/权限越界）
- 对现有 API 的兼容性

## 联系方式

- GitHub Issues: [github.com/chnacssa/qingtian/issues](https://github.com/chnacssa/qingtian/issues)
- 官网: [acssa.cn](https://acssa.cn)
- 邮箱: dev@qingtian.dev
