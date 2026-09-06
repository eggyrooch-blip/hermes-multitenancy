"""Host policy for executing files distributed by installed Plugins/Skills."""

from __future__ import annotations


PLUGIN_SCRIPT_RUNTIME_GUIDANCE = "\n".join(
    [
        "Installed Plugin/Skill script execution:",
        "- Any file distributed by an installed Plugin/Skill must run through the registered `lark_cli` tool with `mode=\"script\"`; resolve the file relative to its SKILL.md and preserve remaining argv.",
        "- Never run a distributed file through shell, terminal, execute_code, or an interpreter directly.",
        "- If `lark_cli` is unavailable, report the capability as unavailable; do not fall back to direct execution.",
    ]
)

PLUGIN_SCRIPT_SOUL_RULE = (
    "- 已安装 Skill/Plugin 要求用任何解释器或直接执行方式运行其分发文件时（不限文件类型或所在目录），"
    "必须把文件相对其 SKILL.md 解析成实际安装路径，并调用 `lark_cli` 的 `mode=\"script\"`"
    "（其余参数原样放入 argv）；不得改用 terminal/execute_code。"
)
