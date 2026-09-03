"""osskill 测试目录 — pytest 共享 sys.path 配置。

69e460e 后商业 Skill（bidding/procurement/sales/work_secretary/workflow）迁到
仓库根 skills/，本目录下测试（work_secretary/sales/pairing 等）import 走
`skills.*` 新路径；pytest 从 opensource/qingtian 运行时 cwd 在 sys.path 但
仓库根不在 → `import skills.*` 失败。同 tests/conftest.py 补仓库根（幂等）。
"""

import os
import sys

_qingtian_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_repo_root = os.path.dirname(_qingtian_root)
_parent = os.path.dirname(_repo_root)
if os.path.isdir(os.path.join(_parent, "skills")):
    _repo_root = _parent
for _p in (_qingtian_root, _repo_root):
    if _p not in sys.path:
        sys.path.insert(0, _p)
