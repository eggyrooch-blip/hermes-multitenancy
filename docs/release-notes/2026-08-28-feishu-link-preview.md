# 全租户飞书链接解析

- 新增 `POST /api/run-broker/link-previews`，批量上限 10、单 URL 上限 2048、总预算 3 秒。
- URL 只做本地规范化和类型路由；绝不请求租户 HTML。唯一例外是精确 host `open.feishu.cn`/`open.larksuite.com` 的 `/document/...` 公开页面：匿名读取最多 1 MB HTML、2 秒超时、不跟随重定向，并优先提取 `og:title`、其次 `<title>`。
- 支持 Wiki、Doc/Docx、Sheets、Base/Bitable、Slides、Drive 文件/文件夹的真实标题；任务、日历、审批、会议、妙记、消息、Meegle 及未知飞书页均有明确通用类型。
- 开放平台公开文档页返回真实网页标题与“开放平台文档”；失败、超限或无标题安全降级为通用预览。带 URL 用户信息或非 443 端口的链接在外呼前拒绝。
- 合法域名不限 `keep.feishu.cn`，覆盖任意 `*.feishu.cn` 和 `*.larksuite.com`；无跨租户权限时不借用其他身份。
- 本机聚焦测试通过；官方公开页只读真探针返回“获取文件元数据 - 服务端 API - 飞书开放平台”。没有凭据改动、飞书写入、员工消息或真实跨租户 UAT。
- 本机候选 sidecar 手工重启约束：与 WebUI 共用 master key，`HERMES_HOME=/Users/hermes/.hermes`，`HERMES_USE_SANDBOX=1`。三项恢复后，当前 actor/profile 的 Base preview 读回 HTTP 200/resolved 与真实标题，对话 run 读回 HTTP 200/done/0 error/OK。
- 两处既有 session-search 测试显式固定临时 `HERMES_HOME`，只消除宿主 profile 插件发现污染，不改变 session-search 生产逻辑。
