"""会议纪要 — 会议记录转结构化摘要和待办事项"""
from osskill.implementations.clawhub_adapter.clawhub_adapter import ClawHubSkill


class MeetingSummarySkill(ClawHubSkill):
    name = "meeting_summary"
    display_name = "会议纪要"
    description = "将会议记录或讨论内容转为结构化会议纪要和待办事项"
    version = "1.0.0"
