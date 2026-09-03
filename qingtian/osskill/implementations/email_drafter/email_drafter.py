"""邮件起草器 — 根据要点生成正式商务邮件"""
from osskill.implementations.clawhub_adapter.clawhub_adapter import ClawHubSkill


class EmailDrafterSkill(ClawHubSkill):
    name = "email_drafter"
    display_name = "邮件起草器"
    description = "根据要点或关键词生成正式商务邮件，支持多种语气和场景"
    version = "1.0.0"
