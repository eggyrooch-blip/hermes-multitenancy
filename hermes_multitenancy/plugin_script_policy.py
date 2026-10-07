"""Host policy for executing files distributed by installed Plugins/Skills."""

from __future__ import annotations


PLUGIN_SCRIPT_RUNTIME_GUIDANCE = "\n".join(
    [
        "Installed Plugin/Skill script execution:",
        "- A file distributed by an AiDock-installed Plugin/Skill (installed from AiDock: its SKILL.md lives in the shared, read-only AiDock skill roots, or is the Codex runtime's materialized copy of an AiDock Plugin under codex-home) must run through the registered `lark_cli` tool with `mode=\"script\"`; resolve the file relative to its SKILL.md and preserve remaining argv.",
        "- Any other installed Skill (for example one installed directly into this profile's skills directory) runs as its SKILL.md instructs, through terminal/execute_code in the sandbox.",
        "- If `lark_cli` mode=\"script\" answers that a file is not an AiDock-distributed script, run it per its SKILL.md via terminal instead.",
    ]
)

PLUGIN_SCRIPT_SOUL_RULE = (
    "- AiDock 分发安装的 Skill/Plugin（从 AiDock 安装：SKILL.md 位于共享只读 AiDock skill 目录，"
    "或是 Codex 运行时在 codex-home 下物化的 AiDock Plugin 副本）要求运行其分发文件时，"
    "必须把文件相对其 SKILL.md 解析成实际安装路径，并调用 `lark_cli` 的 `mode=\"script\"`"
    "（其余参数原样放入 argv）；其他已安装 Skill（例如直接装在本 profile skills 目录下的）照 SKILL.md 执行，"
    "可以使用 terminal/execute_code；`lark_cli` 提示 not an AiDock-distributed script 时改走 terminal。"
)

# SOUL.md lines written by earlier releases. `_ensure_soul_guidance` only
# appends, so these must be removed explicitly or the old ban keeps winning.
RETIRED_PLUGIN_SCRIPT_SOUL_RULES = (
    "- 已安装 Skill/Plugin 要求用任何解释器或直接执行方式运行其分发文件时（不限文件类型或所在目录），"
    "必须把文件相对其 SKILL.md 解析成实际安装路径，并调用 `lark_cli` 的 `mode=\"script\"`"
    "（其余参数原样放入 argv）；不得改用 terminal/execute_code。",
)
