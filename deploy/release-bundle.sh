#!/usr/bin/env bash
# release-bundle — 把一个 release-* 标签变成对外可交付的自包含安装包
#
# 输入:标签名(清单 annotation 钉死 multitenancy/webui 双 SHA)。
# 输出:hermes-bundle-<tag>/ 目录(可选 tar.gz),内含:
#   webui/                             webui 仓在清单 SHA 的源码(git archive)
#   webui/vendor/hermes-multitenancy/  mt 仓在清单 SHA 的源码
#   webui/Dockerfile.bundle            webui 自己的 Dockerfile + 钉入 mt 插件的叠加层
#   docker-compose.yml  .env.example  VERSION  README.md
# 外部机器只需要 Docker:解包 → docker compose up -d。
# 回滚 = 用上一版 bundle 重新 up(镜像 tag 即版本,互不覆盖)。
#
# 用法:
#   release-bundle.sh <release-tag> [--repo-mt <path>] [--repo-webui <path>]
#                     [--out <dir>] [--no-tar]
set -euo pipefail

log() { printf '[release-bundle] %s\n' "$*"; }
die() { printf '[release-bundle] FATAL: %s\n' "$*" >&2; exit 1; }

TAG="${1:-}"; [ -n "$TAG" ] || die "用法:release-bundle.sh <release-tag> [--out <dir>] …"
shift
REPO_MT="${RELEASE_CUT_REPO_MT:-$HOME/code/hermes-multitenancy}"
REPO_WEBUI="${RELEASE_CUT_REPO_WEBUI:-$HOME/code/hermes-web-ui}"
OUT=""
MAKE_TAR=1
while [ $# -gt 0 ]; do
  case "$1" in
    --repo-mt)    REPO_MT="$2"; shift 2 ;;
    --repo-webui) REPO_WEBUI="$2"; shift 2 ;;
    --out)        OUT="$2"; shift 2 ;;
    --no-tar)     MAKE_TAR=0; shift ;;
    *) die "未知参数:$1" ;;
  esac
done
[ -n "$OUT" ] || OUT="./hermes-bundle-$TAG"

git -C "$REPO_MT" fetch -q --tags --prune origin 2>/dev/null || true
git -C "$REPO_WEBUI" fetch -q --tags --prune origin 2>/dev/null || true

# ── 读清单,用发布器同款解析 + 校验 ──────────────────────────────────
BODY=$(git -C "$REPO_MT" tag -l --format='%(contents)' "$TAG")
[ -n "$BODY" ] || die "标签不存在或无 annotation:$TAG"
MT_SHA=$(printf '%s\n' "$BODY" | sed -n 's/^multitenancy:[[:space:]]*//p' | head -1)
WEBUI_SHA=$(printf '%s\n' "$BODY" | sed -n 's/^webui:[[:space:]]*//p' | head -1)
{ [ ${#MT_SHA} -eq 40 ] && [ ${#WEBUI_SHA} -eq 40 ]; } \
  || die "$TAG 的清单残缺(mt='${MT_SHA}' webui='${WEBUI_SHA}')—— 拒绝打包残缺清单"
case "$MT_SHA$WEBUI_SHA" in *[!0-9a-f]*) die "清单 SHA 不是纯 hex" ;; esac
git -C "$REPO_MT" cat-file -e "$MT_SHA^{commit}" 2>/dev/null \
  || die "multitenancy SHA 本地不可达:$MT_SHA"
git -C "$REPO_WEBUI" cat-file -e "$WEBUI_SHA^{commit}" 2>/dev/null \
  || die "webui SHA 本地不可达:$WEBUI_SHA"

# ── base 镜像钉 digest(先于任何落盘,拒绝时零残留)────────────────────
# bundle 宣称双 SHA 钉死,基础镜像不能漂 latest;从清单 SHA 的 Dockerfile 里取 ARG
BASE_REF=$(git -C "$REPO_WEBUI" show "$WEBUI_SHA:Dockerfile" 2>/dev/null \
  | sed -n 's/^ARG BASE_IMAGE=//p' | head -1)
[ -n "$BASE_REF" ] || die "webui@${WEBUI_SHA:0:12} 的 Dockerfile 里找不到 ARG BASE_IMAGE= —— 无法钉基础镜像"
if [ -n "${RELEASE_BUNDLE_BASE_DIGEST:-}" ]; then
  BASE_PINNED="$RELEASE_BUNDLE_BASE_DIGEST"   # 测试/离线注入口
else
  BASE_PINNED=$(docker image inspect --format '{{index .RepoDigests 0}}' "$BASE_REF" 2>/dev/null || true)
  [ -n "$BASE_PINNED" ] || die "拿不到 $BASE_REF 的 digest(先 docker pull $BASE_REF),或用 RELEASE_BUNDLE_BASE_DIGEST 显式注入"
fi
# 不可变引用硬校验:tag 之类可变引用一律拒绝,无论来自 docker 还是注入口
printf '%s' "$BASE_PINNED" | grep -Eq '^[^@ ]+@sha256:[0-9a-f]{64}$' \
  || die "base 镜像引用不是不可变 digest(repo@sha256:<64hex>):$BASE_PINNED"
log "base 镜像钉死:$BASE_PINNED"

[ -e "$OUT" ] && die "输出目录已存在:$OUT —— 不覆盖既有产物,先挪走"
mkdir -p "$OUT/webui/vendor/hermes-multitenancy"

log "导出源码(webui@${WEBUI_SHA:0:12} + mt@${MT_SHA:0:12})…"
git -C "$REPO_WEBUI" archive "$WEBUI_SHA" | tar -x -C "$OUT/webui"
git -C "$REPO_MT" archive "$MT_SHA" | tar -x -C "$OUT/webui/vendor/hermes-multitenancy"

# ── Dockerfile.bundle:webui 原 Dockerfile + ARG 默认值改写为 digest + 叠加层 ──
[ -f "$OUT/webui/Dockerfile" ] || die "webui 源码里没有 Dockerfile —— 打包前提不成立"
# 默认值直接钉死,这样绕过 compose 手动 docker build 也拿不到漂移的 latest
sed "s|^ARG BASE_IMAGE=.*|ARG BASE_IMAGE=$BASE_PINNED|" \
  "$OUT/webui/Dockerfile" > "$OUT/webui/Dockerfile.bundle"
cat >> "$OUT/webui/Dockerfile.bundle" <<'EOF'

# ── release-bundle 叠加层(生成物,勿手改):按清单钉入 multitenancy 插件 ──
# base 镜像的 venv 由 uv 创建、不带 pip,必须用镜像自带的 uv 装
COPY vendor/hermes-multitenancy /opt/src/hermes-multitenancy
RUN uv pip install --python /opt/hermes/.venv/bin/python --no-cache /opt/src/hermes-multitenancy \
 && /opt/hermes/.venv/bin/python -c "import hermes_multitenancy._import_smoke"
EOF

# ── compose:与 webui 仓自带 compose 同语义,仅换 build 目标与镜像名 ──
# ponytail: 手写镜像而非 sed 原文件;webui compose 若演进需同步这里(S4 冒烟会兜住)
cat > "$OUT/docker-compose.yml" <<EOF
services:
  hermes-webui:
    build:
      context: ./webui
      dockerfile: Dockerfile.bundle
      args:
        BASE_IMAGE: $BASE_PINNED
    image: hermes-bundle:$TAG
    container_name: \${WEBUI_CONTAINER_NAME:-hermes-webui}
    ports:
      - "\${PORT:-6060}:\${PORT:-6060}"
      - "\${PREVIEW_FRONTEND_PORT:-8651}:8651"
      - "\${XAI_OAUTH_PORT:-56121}:56121"
    volumes:
      - \${HERMES_DATA_DIR:-./hermes_data}:/home/agent/.hermes
      - \${HERMES_DATA_DIR:-./hermes_data}/hermes-web-ui:/home/agent/.hermes-web-ui
    environment:
      - PORT=\${PORT:-6060}
      - HERMES_HOME=/home/agent/.hermes
      - HERMES_BIN=/opt/hermes/.venv/bin/hermes
      - HERMES_WEB_UI_MANAGED_GATEWAY=1
      - HERMES_WEB_UI_XAI_CALLBACK_BIND_HOST=0.0.0.0
      - PATH=/opt/hermes/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
      - HERMES_ALLOW_ROOT_GATEWAY=1
    restart: unless-stopped
    stdin_open: true
    tty: true
EOF

cat > "$OUT/.env.example" <<'EOF'
# 复制为 .env 后按需修改
PORT=6060
HERMES_DATA_DIR=./hermes_data
WEBUI_CONTAINER_NAME=hermes-webui
EOF

cat > "$OUT/VERSION" <<EOF
tag=$TAG
multitenancy=$MT_SHA
webui=$WEBUI_SHA
base_image=$BASE_PINNED
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
builder=release-bundle
EOF

cat > "$OUT/README.md" <<EOF
# Hermes 一体化安装包 $TAG

单容器交付:WebUI + Hermes agent core + multitenancy 插件,版本由 \`VERSION\`
里的双仓 SHA 钉死。宿主机只需要 Docker(含 compose 插件)。

## 安装

\`\`\`bash
cp .env.example .env   # 按需改端口/数据目录
docker compose up -d --build
\`\`\`

## 验证

\`\`\`bash
curl -fsS http://localhost:6060/ >/dev/null && echo WEBUI-OK
docker exec hermes-webui /opt/hermes/.venv/bin/python -c "import hermes_multitenancy._import_smoke" && echo MT-OK
\`\`\`

## 回滚

镜像 tag 即版本(hermes-bundle:$TAG),互不覆盖。回滚 = 到上一版 bundle 目录:

\`\`\`bash
docker compose down          # 在当前版本目录
cd ../hermes-bundle-<上一版tag> && docker compose up -d
\`\`\`

数据在 \`HERMES_DATA_DIR\`(默认 ./hermes_data),换版本不动数据;跨版本共享数据
请在 .env 里把两个版本指到同一目录。

## 内容清单

- \`webui/\` — hermes-web-ui @ \`${WEBUI_SHA:0:12}\`
- \`webui/vendor/hermes-multitenancy/\` — hermes-multitenancy @ \`${MT_SHA:0:12}\`
- \`webui/Dockerfile.bundle\` — webui 官方 Dockerfile + 插件钉入层(生成物)
EOF

if [ "$MAKE_TAR" = "1" ]; then
  TAR_PATH="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT").tar.gz"
  tar -czf "$TAR_PATH" -C "$(dirname "$OUT")" "$(basename "$OUT")"
  log "✔ 交付包:$TAR_PATH"
fi
log "✔ bundle 目录:$OUT(安装/回滚说明见其中 README.md)"
