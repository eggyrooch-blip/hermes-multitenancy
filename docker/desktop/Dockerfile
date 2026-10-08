# syntax=docker/dockerfile:1
# Hermes 云电脑桌面镜像（mt-desktop-image）
#
# 上游官方 `-desktop` 镜像（Hermes runtime + TigerVNC + Xfce + Chromium）之上补齐
# PoC（2026-10-07）实测缺的三样，再按 CEO 当日追加换 core / 补 MT 依赖 / 预装 agent-browser，其余一概不动：
#   1. cua-driver —— 上游镜像不带，运行时由 uid 10000 装不进只读的 PM store；这里以 root
#      装进镜像层的 PM store（/opt/hermes/tools），再把属主交给运行用户。
#   2. at-spi2-core —— cua-driver 的 AX（无障碍树）能力依赖，缺了 doctor 的 ax_capability 不过。
#   3. fonts-noto-cjk —— Chromium 打开中文页全是豆腐块。
#
# 镜像不含任何租户数据 / 密钥 / profile：/opt/data 只保留上游从 /etc/skel 带入的 shell 点文件，
# 运行时由 gateway 挂 PROFILE_HOME。
# 构建入口：docker/desktop/build.sh（hermes-pre，rootful podman）。
#
# 基镜像 pin 到 digest：tag 会漂，digest 不会。换基线 = 改 build.sh 里的 CORE_TAG/BASE_DIGEST 一处。
ARG BASE_IMAGE=docker.io/nousresearch/hermes-agent
ARG BASE_TAG=v2026.9.24-desktop
ARG BASE_DIGEST=sha256:45379eb3eb2d11c38df9b0e529dc734775b42c40b57cfb1cae0448f0cb7bf647

FROM ${BASE_IMAGE}:${BASE_TAG}@${BASE_DIGEST}

# 运行用户。上游 stage2 默认 hermes = 10000:10000；gateway 起容器时传 HERMES_UID/HERMES_GID=10000，
# 与这里保持一致，否则 chown 过的 cua-driver 对运行用户又变成别人的文件。
ARG HERMES_UID=10000
ARG HERMES_GID=10000

# 上游镜像以 root 收尾（s6 stage2 需要 root 做 usermod/chown），这里显式写出以免基镜像改动后静默变化。
USER root

# 1 + 3：系统包。--no-install-recommends 防止拖进整套 GNOME；装完刷字体缓存并清 apt 列表。
RUN apt-get -o Acquire::Retries=3 update \
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
        at-spi2-core \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f \
    && test "$(fc-list :lang=zh | wc -l)" -ge 1 \
    && test -x /usr/libexec/at-spi-bus-launcher

# 3：core 换成 fork 的 hermes_agent-0.21.5 wheel，再补 MT worker 的 import 依赖（CEO 2026-10-07 定：
#    容器里的 core 必须等于生产要装的 core，否则 P0 验的不是 P1 上线的东西）。
# - wheel 在本项目 GitLab PyPI；simple index 要凭据，但 sha256 寻址的文件 URL 匿名可读（与 MT
#   pyproject 注释同口径），所以构建不经手任何凭据，下载后 sha256sum 校验。
# - 上游 venv 的解释器随 tag 变（v2026.9.24-desktop 是 3.13.5，main-desktop 已是 PM 的 3.14），wheel
#   元数据写 `<3.14`。不做整体依赖解析（会把上游 venv 已满足的包重装一遍）：先 `--no-deps` 装 core，
#   再用 wheel 自带的 Requires-Dist 按当前解释器算出 venv 里不满足的钉版，只装这几条 —— 既守 fork 的
#   锁定版本，又不动上游 venv 里已满足的包。uv 在 v2026.9.24-desktop 是 /usr/local/bin/uv，在
#   main-desktop 是 PM store 条目，两处都找。
# - 上游把 /opt/hermes 的源码树以 editable 装在 venv 里；uv 装 wheel 时会把那个 editable 卸掉，之后
#   import 只认 site-packages 里的 fork。/opt/hermes 的源码树本身留着（web_dist / ui-tui / PM store 的
#   HERMES_WEB_DIST、HERMES_TUI_DIR、HERMES_RUNTIME_DIR 都指向它），但它不能再当 cwd：`python -c` /
#   `python -m` 会把 cwd 排到 sys.path 最前，上游代码就又盖回来了 —— 所以下面把 WORKDIR 挪到 /opt/data，
#   校验用 `python -P`（不把 cwd 加进 sys.path，和 console script 看到的一致）。
# - MT worker 依赖列表只写在 MT_WORKER_PYTHON_DEPS 一处，隔壁实测 import 失败再往里加。
ARG CORE_VERSION=0.21.5
ARG CORE_WHEEL_SHA256=d0dde1182245c2dce1ed97bdf31266c0f70b3f85c35afb2fd9278b66ccd5ff46
ARG CORE_WHEEL_URL=https://gitlab.example.com/api/v4/projects/2765/packages/pypi/files/${CORE_WHEEL_SHA256}/hermes_agent-${CORE_VERSION}-py3-none-any.whl
ARG MT_WORKER_PYTHON_DEPS="tencentcloud-sdk-python-vod"
RUN set -eu; \
    uv="$(command -v uv || ls -d /opt/hermes/tools/uv-*/uv 2>/dev/null | head -n 1)"; test -x "${uv}"; \
    py=/opt/hermes/.venv/bin/python; \
    whl="/tmp/hermes_agent-${CORE_VERSION}-py3-none-any.whl"; \
    curl -fsSL --retry 3 -o "${whl}" "${CORE_WHEEL_URL}"; \
    echo "${CORE_WHEEL_SHA256}  ${whl}" | sha256sum -c -; \
    "${uv}" pip install --python "${py}" --no-deps "${whl}"; \
    unmet() { "${py}" -c 'import importlib.metadata as m, sys, zipfile; from packaging.requirements import Requirement as R; from packaging.version import Version as V; z = zipfile.ZipFile(sys.argv[1]); meta = z.read(next(n for n in z.namelist() if n.endswith("dist-info/METADATA"))).decode(); reqs = [R(l.split(":", 1)[1].strip()) for l in meta.splitlines() if l.startswith("Requires-Dist:")]; reqs = [r for r in reqs if not r.marker or r.marker.evaluate({"extra": ""})]; norm = lambda n: n.lower().replace("_", "-"); inst = {}; [inst.setdefault(norm(d.metadata["Name"]), d.version) for d in m.distributions()]; print("\n".join(str(r) for r in reqs if inst.get(norm(r.name)) is None or not r.specifier.contains(V(inst[norm(r.name)]), prereleases=True)), end="")' "${whl}"; }; \
    unmet > /tmp/core-unmet.txt; \
    echo "fork pins not satisfied by the upstream venv:"; cat /tmp/core-unmet.txt; \
    if [ -s /tmp/core-unmet.txt ]; then "${uv}" pip install --python "${py}" -r /tmp/core-unmet.txt; fi; \
    test -z "$(unmet)"; \
    "${uv}" pip install --python "${py}" ${MT_WORKER_PYTHON_DEPS}; \
    cd /tmp; \
    "${py}" -c "import importlib.metadata as m, hermes_cli, yaml, tencentcloud; assert m.version('hermes-agent')=='0.21.5'"; \
    HERMES_HOME=/tmp/hermes-selfcheck HERMES_DOCKER_EXEC_AS_ROOT=1 hermes --version | tee /tmp/hermes-version.txt; \
    grep -q "Hermes Agent v${CORE_VERSION} " /tmp/hermes-version.txt; \
    rm -rf "${whl}" /tmp/core-unmet.txt /tmp/hermes-version.txt /tmp/hermes-selfcheck /root/.cache/uv

# 2：cua-driver 进镜像层。
# - 基镜像（v2026.9.24）的 `hermes computer-use install` 走上游 trycua 安装脚本（不是 PM）：默认装到
#   $HOME/.local/bin + $HOME/.cua-driver，root 装完就在 /root 下，运行用户看不见；运行用户的 $HOME
#   又是 /opt/data（运行时挂载点）。所以把包目录钉到 /opt/hermes/cua-driver、入口符号链接放
#   /usr/local/bin（上游 resolve_cua_driver_cmd 的候选位置，且对所有用户在 PATH 上）。
# - /opt/hermes/bin/hermes 是上游 exec shim：root 调用默认降到 hermes 用户，这里需要 root 写
#   /opt/hermes 与 /usr/local/bin，所以 HERMES_DOCKER_EXEC_AS_ROOT=1。
# - 版本钉死：上游脚本默认装最新 prerelease（Linux 标 alpha）；0.21.0 是 Hermes 上游 PM 自己钉的版本，
#   PoC（2026-10-07）用它跑通 doctor / screen / 接管。换版本改 CUA_DRIVER_VERSION 一处。
# - root 装完属主是 root（PoC 六坑之一：运行用户读不到驱动记录）→ 包目录整体交给运行用户。
# - /opt/data 是上游声明的 VOLUME，构建期删除不会进入最终镜像；运行时只允许上游从 /etc/skel
#   带入的 .bashrc / .profile / .bash_logout，build.sh 会实际检查。
ARG CUA_DRIVER_VERSION=0.21.0
RUN HERMES_HOME=/tmp/hermes-cua-install \
    HERMES_DOCKER_EXEC_AS_ROOT=1 \
    CUA_DRIVER_RS_VERSION="${CUA_DRIVER_VERSION}" \
    CUA_DRIVER_RS_HOME=/opt/hermes/cua-driver \
    CUA_DRIVER_RS_INSTALL_DIR=/usr/local/bin \
    CUA_DRIVER_RS_NO_MODIFY_PATH=1 \
    hermes computer-use install \
    && test -x /usr/local/bin/cua-driver \
    && case "$(readlink -f /usr/local/bin/cua-driver)" in /opt/hermes/cua-driver/*) ;; *) echo "cua-driver 没装进 /opt/hermes/cua-driver: $(readlink -f /usr/local/bin/cua-driver)" >&2; exit 1 ;; esac \
    && /usr/local/bin/cua-driver --version | grep -F "${CUA_DRIVER_VERSION}" \
    && chown -R ${HERMES_UID}:${HERMES_GID} /opt/hermes/cua-driver \
    && chmod -R a+rX /opt/hermes/cua-driver \
    && rm -rf /tmp/hermes-cua-install

# 4：agent-browser 预装（CEO 2026-10-07 追加：staging 实测 MT worker 的 browser 工具集 check_fn 全 False，
#    agent.log「agent-browser CLI not found」，agent 只能退到 web_extract）。
# - v2026.9.24 没有 `hermes pm`（PM 是之后才进 main 的），core 的 browser_tool_install 按 PATH →
#   npx 懒解析找 agent-browser，上游给的装法就是 `npm install -g agent-browser`；装到 /usr/local
#   （root 属主、全员可读可执行），PATH 上直接命中，不再走 npx 临时拉包。
# - 版本钉 Hermes 上游 PM 自己钉的 0.26.0（PoC 实证）；换版本改 AGENT_BROWSER_VERSION 一处。
# - 不跑 `agent-browser install`（会下载 Playwright 自带浏览器）：浏览器走系统 Chromium，见下面的
#   AGENT_BROWSER_EXECUTABLE_PATH。
ARG AGENT_BROWSER_VERSION=0.26.0
RUN npm install -g --no-fund --no-audit "agent-browser@${AGENT_BROWSER_VERSION}" \
    && npm cache clean --force \
    && test -x /usr/local/bin/agent-browser \
    && agent-browser --version | grep -F "${AGENT_BROWSER_VERSION}" \
    && test -x /usr/bin/chromium

# podman 不被上游的 _running_in_docker 识别（只认 /.dockerenv 或 cgroup 里的 "docker"），
# 非 root 下 Chromium 不会自动加 --no-sandbox；显式给 agent-browser 传参。
ENV AGENT_BROWSER_ARGS=--no-sandbox,--disable-dev-shm-usage
# 浏览器固定走系统 Chromium（-desktop 镜像自带 /usr/bin/chromium）：不让 agent-browser 去下 Playwright
# 浏览器，也不依赖 stage2 开机扫描（沙箱单以 --entrypoint sleep 直接起，stage2 不跑）。显式设了这个值后
# stage2 自己的扫描会跳过（上游：用户已设则不覆盖）。
ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium

# 驱动路径写死成 override（core tools_config_cua.py 认 HERMES_CUA_DRIVER_CMD）：沙箱里每个 profile 的
# HERMES_HOME 各不相同，不能靠 $HOME 下的候选位置碰运气；override 也让运行时的
# `computer-use install --upgrade` 拒绝改动镜像钉死的版本。
ENV HERMES_CUA_DRIVER_CMD=/usr/local/bin/cua-driver

# 上游 WORKDIR 是 /opt/hermes（源码树）。core 已换成 site-packages 里的 fork wheel 后，这个 cwd 会让任何
# `python -c` / `python -m` 先 import 到上游旧代码（cwd 在 sys.path 最前）。挪到 HERMES_HOME，和运行用户
# 的家目录一致；console script `hermes` 本来就不受 cwd 影响。
WORKDIR /opt/data

# Bot Screen 的锁文件目录必须在切换到运行用户前创建并交给 uid 10000。
RUN mkdir -p /tmp/hermes-runtime \
    && chown 10000:10000 /tmp/hermes-runtime \
    && chmod 700 /tmp/hermes-runtime
ENV XDG_RUNTIME_DIR=/tmp/hermes-runtime

# 覆盖上游为 s6 stage2 设置的启动元数据：默认容器只保持存活，运行身份固定为沙箱 uid/gid。
USER 10000:10000
ENTRYPOINT []
CMD ["sleep", "infinity"]

# FROM 之前声明的 ARG 在 FROM 之后出了作用域，LABEL 里要再声明一次才拿得到值（无默认值 = 取全局值）。
ARG BASE_IMAGE
ARG BASE_TAG
ARG BASE_DIGEST
ARG CORE_TAG=v2026.9.24
ARG IMAGE_TAG=
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="hermes-multitenancy desktop" \
      org.opencontainers.image.description="Hermes cloud-desktop sandbox: upstream -desktop image + cua-driver + at-spi2-core + Noto CJK" \
      org.opencontainers.image.source="https://gitlab.example.com/sunke/hermes-multitenancy" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${IMAGE_TAG}" \
      org.opencontainers.image.base.name="${BASE_IMAGE}:${BASE_TAG}" \
      org.opencontainers.image.base.digest="${BASE_DIGEST}" \
      com.example.hermes.core_tag="${CORE_TAG}"
