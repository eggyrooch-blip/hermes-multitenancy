"""User-facing LiteLLM billing failure copy.

Lives in its own leaf module because both the router layer (which picks the
card BODY on stream failure) and the card layer (which picks the HEADER from
that body) need these strings, and card must not import router.
"""

BUDGET_EXCEEDED_NOTICE = (
    "⚠️ 你本月的 AI 使用额度已用完，这次请求没有执行。"
    "额度每月 1 日自动恢复；如需提前恢复或调整额度，请联系 IT。"
)
RATE_LIMIT_NOTICE = "⚠️ AI 网关当前请求过于频繁，这次没有执行成功。请稍等几分钟再试。"
