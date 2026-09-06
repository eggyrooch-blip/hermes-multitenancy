---
project: "hermes"
vault_dir: "hermes"
repo: "hermes-multitenancy"
lang: "python"
managed_by: "ftask-env"
probed:
  remote: "git@gitlab.example.com:sunke/hermes-multitenancy.git"
  default_branch: "main"
  ci: ".gitlab-ci.yml"
  lang_detected: "python"
  probed_at: "2026-08-14"
environments:
  local:
    purpose: "本地开发"
    run: "见 deploy/hermes-release.sh；WebUI 本地默认 http://127.0.0.1:8648"
    verify: "curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8648/api/health"
    test_binding:
      lark_profile: "TODO"
      bot: "TODO"
      browser: "interceptor"
  pre:
    purpose: "预发/联调（2026-08-04 从零搭建）"
    url: "https://hermes-pre.example.com"
    host: "root@192.0.2.188"
    deploy: "见 vault 笔记「部署 — Hermes PRE 环境 192.0.2.188 从零搭建 2026-08-04」"
    version_probe: "TODO"
    test_binding:
      lark_profile: "TODO"
      bot: "TODO"
      browser: "interceptor"
  production:
    purpose: "生产（hermes-1）"
    url: "TODO"
    host: "root@192.0.2.133"
    deploy: "ftask ship + deploy/hermes-release.sh（探针 deploy/hermes-release-probes.sh）"
    version_probe: "TODO"
    test_binding:
      lark_profile: "TODO"
      bot: "TODO"
      browser: "interceptor"
---
# 环境地图（真源）

> 排障 / ship / 备份先看本文件。声明字段在上方 frontmatter；本正文写 runbook、红线、变更记录（人写区，ftask 不动）。

## 红线
1. 禁止把口令/token 写进本文件（`ftask env verify` 会扫）。

## 变更记录
