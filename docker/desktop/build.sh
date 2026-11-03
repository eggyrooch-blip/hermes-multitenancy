#!/usr/bin/env bash
# 构建（并可选推送）Hermes 云电脑桌面镜像。预期在 hermes-pre（rootful podman 4.4.1）上执行：
#   rsync -a docker/desktop/ hermes-pre:/root/mt-desktop-image/ && ssh hermes-pre bash /root/mt-desktop-image/build.sh
#
# 产物 tag：<CORE_TAG>-<yyyymmdd>，另打浮动 latest。
# 推送只在 DESKTOP_REGISTRY 非空时发生；凭据只从环境变量读、经 stdin 交给 podman login，
# 不落文件、不进命令行（ps / history 看不到）。
#
# 环境变量（全部可选）：
#   CORE_TAG          上游核心 tag，基镜像 = ${CORE_TAG}-desktop            默认 v2026.9.24
#   BASE_DIGEST       基镜像 manifest digest（pin）                            默认见下
#   DATE_TAG          日期段                                                  默认 今天 yyyymmdd
#   IMAGE_NAME        镜像名（不含 registry）                                  默认 hermes-multitenancy/desktop
#   DESKTOP_REGISTRY  推送目标 registry 主机（含可选端口 / 路径前缀）；为空 = 只构建不推送
#   REGISTRY_USER     podman login 用户名（配 REGISTRY_TOKEN 用）
#   REGISTRY_TOKEN    podman login 口令 / token；为空则假定已登录
#   GIT_SHA           写进 org.opencontainers.image.revision 的源码 sha      默认 unknown
#   PODMAN            容器 CLI                                                默认 podman
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { echo "build.sh: $*" >&2; exit 1; }

if [[ ${CORE_TAG+x} == x && ${BASE_DIGEST+x} != x ]]; then
    die "覆盖 CORE_TAG 时必须同时显式设置 BASE_DIGEST（两者必须成对）"
fi

CORE_TAG="${CORE_TAG:-v2026.9.24}"
BASE_DIGEST="${BASE_DIGEST:-sha256:45379eb3eb2d11c38df9b0e529dc734775b42c40b57cfb1cae0448f0cb7bf647}"
DATE_TAG="${DATE_TAG:-$(date +%Y%m%d)}"
IMAGE_NAME="${IMAGE_NAME:-hermes-multitenancy/desktop}"
DESKTOP_REGISTRY="${DESKTOP_REGISTRY:-}"
REGISTRY_USER="${REGISTRY_USER:-}"
REGISTRY_TOKEN="${REGISTRY_TOKEN:-}"
GIT_SHA="${GIT_SHA:-unknown}"
PODMAN="${PODMAN:-podman}"

[[ "${CORE_TAG}" =~ ^v[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}$ ]] || die "CORE_TAG 必须形如 v2026.9.24，收到 '${CORE_TAG}'"
[[ "${BASE_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]] || die "BASE_DIGEST 必须是 sha256:<64 hex>，收到 '${BASE_DIGEST}'"
[[ "${DATE_TAG}" =~ ^[0-9]{8}$ ]] || die "DATE_TAG 必须是 yyyymmdd，收到 '${DATE_TAG}'"
[[ -f "${here}/Dockerfile" ]] || die "找不到 ${here}/Dockerfile"
command -v "${PODMAN}" >/dev/null 2>&1 || die "找不到 ${PODMAN}"
if [[ -n "${REGISTRY_TOKEN}" && -z "${REGISTRY_USER}" ]]; then
    die "给了 REGISTRY_TOKEN 就必须给 REGISTRY_USER"
fi

IMAGE_TAG="${CORE_TAG}-${DATE_TAG}"
BASE_TAG="${CORE_TAG}-desktop"
LOCAL_REF="localhost/${IMAGE_NAME}"

echo "==> base   docker.io/nousresearch/hermes-agent:${BASE_TAG}@${BASE_DIGEST}"
echo "==> target ${LOCAL_REF}:${IMAGE_TAG}  (+ :latest)"

"${PODMAN}" build --pull \
    --file "${here}/Dockerfile" \
    --build-arg "BASE_TAG=${BASE_TAG}" \
    --build-arg "BASE_DIGEST=${BASE_DIGEST}" \
    --build-arg "CORE_TAG=${CORE_TAG}" \
    --build-arg "IMAGE_TAG=${IMAGE_TAG}" \
    --build-arg "GIT_SHA=${GIT_SHA}" \
    --tag "${LOCAL_REF}:${IMAGE_TAG}" \
    --tag "${LOCAL_REF}:latest" \
    "${here}"

echo "==> built"
"${PODMAN}" images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' --filter "reference=${LOCAL_REF}"

echo "==> check /opt/data contains only upstream /etc/skel dotfiles"
profile_files="$("${PODMAN}" run --rm --entrypoint sh "${LOCAL_REF}:${IMAGE_TAG}" -c 'ls -A /opt/data')"
printf '%s\n' "${profile_files}"
while IFS= read -r profile_file; do
    case "${profile_file}" in
        ""|.bashrc|.profile|.bash_logout) ;;
        *) die "/opt/data 含非预期文件: ${profile_file}" ;;
    esac
done <<< "${profile_files}"

if [[ -z "${DESKTOP_REGISTRY}" ]]; then
    echo "==> push skipped: DESKTOP_REGISTRY 未设置（本项目 GitLab 容器仓 2026-10-07 尚未开通）。镜像留在本机 ${LOCAL_REF}:${IMAGE_TAG}"
    exit 0
fi

REMOTE_REF="${DESKTOP_REGISTRY}/${IMAGE_NAME}"
registry_host="${DESKTOP_REGISTRY%%/*}"
REGISTRY_AUTH_FILE="$(mktemp)"
chmod 600 "${REGISTRY_AUTH_FILE}"
printf '{}' > "${REGISTRY_AUTH_FILE}"
trap 'rm -f "${REGISTRY_AUTH_FILE}"' EXIT

if [[ -n "${REGISTRY_TOKEN}" ]]; then
    echo "==> podman login ${registry_host} as ${REGISTRY_USER} (token via stdin)"
    printf '%s' "${REGISTRY_TOKEN}" | "${PODMAN}" login --authfile "${REGISTRY_AUTH_FILE}" --username "${REGISTRY_USER}" --password-stdin "${registry_host}"
else
    echo "==> REGISTRY_TOKEN 未设置，尝试匿名推送"
fi

for tag in "${IMAGE_TAG}" latest; do
    "${PODMAN}" tag "${LOCAL_REF}:${tag}" "${REMOTE_REF}:${tag}"
    echo "==> push ${REMOTE_REF}:${tag}"
    "${PODMAN}" push --authfile "${REGISTRY_AUTH_FILE}" "${REMOTE_REF}:${tag}"
done

echo "==> pushed ${REMOTE_REF}:${IMAGE_TAG} and ${REMOTE_REF}:latest"
