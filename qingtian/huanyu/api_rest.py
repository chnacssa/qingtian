"""
寰宇 — REST API 路由（向后兼容入口）

已拆分为三个文件：
  api_compliance.py  — 合规路由（社区版全开）
  api_business.py    — 业务路由（企业版）
  api_federation.py  — 联邦路由（企业版）

本文件保留为向后兼容层，从新文件导入。
"""

from huanyu.api_compliance import compliance_router as router
from huanyu.api_federation import peer_router
from huanyu.api_business import business_router
