"""根测试目录 — pytest 共享 fixture（空 — integration 测试各自独立）"""
import os
import sys

# 69e460e 后商业 Skill 迁到仓库根 skills/：pytest 从 opensource/qingtian 运行时
# cwd 在 sys.path 但仓库根不在，`import skills.*` 失败（tests/workflow 收集即挂）。
# 同 main.py 4d9dc67 的上溯逻辑：父级含 skills/ 目录则补仓库根（幂等）。
_qingtian_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_repo_root = os.path.dirname(_qingtian_root)
_parent = os.path.dirname(_repo_root)
if os.path.isdir(os.path.join(_parent, "skills")):
    _repo_root = _parent
for _p in (_qingtian_root, _repo_root):
    if _p not in sys.path:
        sys.path.insert(0, _p)
