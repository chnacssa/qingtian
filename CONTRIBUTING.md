# Contributing to ACSSA

**[简体中文](#简体中文)** | **English**

Thank you for your interest in contributing! This guide covers the basics.

## Ways to contribute

- **Bug reports** — open an issue with reproduction steps and logs
- **Feature discussions** — open a discussion/issue describing the use case first, before writing code
- **Code contributions** — bug fixes and module improvements (see "Good First Issues" on the issue tracker)
- **Docs & translations** — README/quickstart fixes, new translations, examples

## Development setup

```bash
git clone https://github.com/chnacssa/qingtian.git
cd qingtian/qingtian
pip install -r requirements.txt
# PostgreSQL 16+ and Redis 7+ running locally
pytest tests/ -v          # full suite (needs PG + Redis)
pytest huanyu/tests/ -v   # single module
```

## Pull request guidelines

1. Keep PRs **small and focused** — one fix or feature per PR
2. Run the relevant test suite before submitting; new features need tests
3. Follow the existing code style (type hints where present, `[trace]`-prefixed logging in critical paths)
4. In your PR description, include the CLA confirmation line (see [CLA.md](CLA.md)):

   ```
   我已阅读并同意 CLA（贡献者许可协议）。
   ```

## Reporting security issues

Please **do not** open public issues for security vulnerabilities — see [SECURITY.md](SECURITY.md) for the disclosure process.

---

# 简体中文

感谢参与贡献！

## 贡献方式

- **Bug 报告**：提 issue，附复现步骤与日志
- **功能讨论**：先提 discussion/issue 说明使用场景，再写代码
- **代码贡献**：bug 修复与模块改进（issue 区有 Good First Issues 标签）
- **文档与翻译**：README / quickstart 修订、新增语言翻译、示例

## 开发环境

```bash
git clone https://github.com/chnacssa/qingtian.git
cd qingtian/qingtian
pip install -r requirements.txt
# 本地需 PostgreSQL 16+ 与 Redis 7+
pytest tests/ -v          # 全量（需 PG + Redis）
pytest huanyu/tests/ -v   # 单模块
```

## PR 规范

1. **小而聚焦**——一个 PR 只做一件事
2. 提交前跑相关测试；新功能必须带测试
3. 遵循现有代码风格（已有类型注解处保持、关键路径日志带 `[trace]` 前缀）
4. PR 描述中包含 CLA 确认行（见 [CLA.md](CLA.md)）：`我已阅读并同意 CLA（贡献者许可协议）。`

## 安全问题

漏洞请勿公开提 issue，走 [SECURITY.md](SECURITY.md) 披露流程。
