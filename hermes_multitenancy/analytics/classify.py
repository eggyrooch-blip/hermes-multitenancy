from __future__ import annotations

import re

from .audit import Turn

SCENARIOS = [
    "Feishu/Lark office automation",
    "Code/data/file operations",
    "Knowledge/search/research",
    "Image/multimodal generation/analysis",
    "Automation/reminder/cron",
    "Skill/profile management",
    "General chat/Q&A",
]

FAILURE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("auth/permission", re.compile(r"授权|未授权|没有权限|unauthorized|forbidden|permission|denied|token expired|401|403", re.I)),
    ("file/path", re.compile(r"路径|cannot open|no such file|file not found|read-only|只读|文件不存在", re.I)),
    ("model/provider", re.compile(r"模型|provider|backend|streaming exhausted|llm|model", re.I)),
    ("timeout", re.compile(r"超时|timeout|timed out", re.I)),
    ("quota/rate-limit", re.compile(r"余额|quota|rate.?limit|limitexceeded|requestlimit", re.I)),
    ("feishu/lark-api", re.compile(r"飞书|lark|lark-cli|open\.feishu|image_key|message_id", re.I)),
]

EXPLICIT_FAILURE_RE = re.compile(
    r"失败|报错|错误|无法|不能|没有权限|未授权|需要授权|不可用|超时|"
    r"未完成|not ok|token expired|"
    r"timeout|timed out|unauthorized|forbidden|permission|denied|not available|"
    r"provider rejected|failed|error|cannot open|no such file|file not found|"
    r"read.?only|文件不存在|401|403|500",
    re.I,
)

SUCCESS_SIGNAL_RE = re.compile(r"已完成|完成|成功|done|ok|✅|发送成功|创建成功|写入成功|整理完成", re.I)


def classify_failure(text: str) -> str | None:
    if not text or not EXPLICIT_FAILURE_RE.search(text):
        return None
    for name, pattern in FAILURE_PATTERNS:
        if pattern.search(text):
            return name
    return "other explicit issue"


def classify_scenario(turn: Turn) -> str:
    # Classify demand from the user's request and tool choices. Final assistant
    # text often contains status words like "image uploaded" or "file sent",
    # which describes execution rather than the original demand.
    text = turn.text.lower()
    tools = turn.tools
    skills = " ".join(sorted(turn.skills)).lower()
    commands = " ".join(turn.lark_commands).lower()
    terminal_themes = " ".join(sorted(turn.terminal_themes)).lower()

    if tools & {"image_generate", "vision_analyze"} or re.search(r"图片|截图|生图|二维码|视觉|image|png|jpg|照片", text):
        return "Image/multimodal generation/analysis"
    if (
        "lark_cli" in tools
        or tools & {"feishu_doc_read", "feishu_drive_add_comment"}
        or "lark-" in skills
        or commands
        or re.search(r"飞书|lark|表格|sheet|docx|文档|日历|群|消息|审批|base|多维表|云文档", text, re.I)
    ):
        return "Feishu/Lark office automation"
    if (
        tools & {"terminal", "execute_code", "write_file", "read_file", "search_files", "patch", "process"}
        or terminal_themes
        or re.search(
            r"代码|脚本|csv|excel|json|日志|文件|报错|trace|debug|修复|部署|上线|sql|"
            r"python|node|curl|internal api|内部.?api|接口",
            text,
            re.I,
        )
    ):
        return "Code/data/file operations"
    if tools & {"cronjob", "todo"} or re.search(r"提醒|定时|每天|每周|cron|任务|todo", text, re.I):
        return "Automation/reminder/cron"
    if tools & {"web_search", "web_extract", "session_search", "memory"} or re.search(
        r"搜索|查一下|总结|分析|调研|资料|网页|链接", text, re.I
    ):
        return "Knowledge/search/research"
    if tools & {"skill_view", "skill_manage", "skills_list", "delegate_task", "clarify"} or re.search(
        r"skill|技能|profile|授权|登录|配置", text, re.I
    ):
        return "Skill/profile management"
    return "General chat/Q&A"


def annotate_turn(turn: Turn) -> Turn:
    turn.explicit_failure = bool(classify_failure(turn.final_content))
    turn.failure_category = classify_failure(turn.final_content)
    turn.success_signal = bool(SUCCESS_SIGNAL_RE.search(turn.final_content or "")) and not turn.explicit_failure
    turn.scenario = classify_scenario(turn)
    return turn
