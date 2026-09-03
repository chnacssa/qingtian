"""检测管线 — SKILL.md 类外部 Skill 的 SAST + 沙箱 + 断言检测

与 osskill/sast.py（Python 类 Skill 权限检查）互补：
  - sast.py → 扫描 .py 文件的权限一致性
  - detection/ → 扫描 SKILL.md 文件的安全合规性

管线流程：
  SAST 扫描 → 沙箱执行 → 功能断言 → 检测报告
"""
