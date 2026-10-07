#!/usr/bin/env bash
# install-hermes-release.sh — 首次安装/重装发布执行器。幂等，可反复跑。
#
# 解决鸡生蛋：单元跑的是 STABLE_BIN 下的固定副本，而那份副本平时只在
# 「发布成功之后」才更新 —— 全新机器上它根本不存在，第一次触发必然失败。
# 这个脚本负责把它种下去，之后就由发布流程自己维护。
set -euo pipefail

STABLE_BIN="${STABLE_BIN:-$HOME/.local/lib/hermes-release}"
SRC="${SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
UNIT_DIR="${UNIT_DIR:-$HOME/.config/systemd/user}"

mkdir -p "$STABLE_BIN" "$UNIT_DIR"
for f in hermes-release.sh hermes-release-probes.sh hermes_patch_probe.py hermes-backup.sh; do
  [ -f "$SRC/$f" ] || { echo "缺少 $SRC/$f"; exit 1; }
  install -m 755 "$SRC/$f" "$STABLE_BIN/$f"
  echo "  种下 $f"
done
for f in hermes-release.service hermes-release.timer; do
  [ -f "$SRC/$f" ] && install -m 644 "$SRC/$f" "$UNIT_DIR/$f" && echo "  装单元 $f"
done
# webui 产物 token 是可选的：没有它发布照跑，只是退回生产机上原地 npm ci + build
# （2026-09-21 实测 9.5 分钟，把机械盘 IO 拖死）。有它才是快路径。
if [ -f "$HOME/.hermes/release.env" ]; then
  echo "  已有 $HOME/.hermes/release.env（webui 产物 token）"
else
  echo "  提示：还没有 $HOME/.hermes/release.env —— webui 会退回原地构建（慢）。"
  echo "        GitLab 项目 2829 → Settings → Repository → Deploy tokens，"
  echo "        scope 只勾 read_package_registry，然后："
  echo "          install -m 600 /dev/null ~/.hermes/release.env"
  echo "          printf 'HERMES_RELEASE_PKG_TOKEN=%s\\n' '<token>' > ~/.hermes/release.env"
fi
echo "已就绪。启用：systemctl --user daemon-reload && systemctl --user enable --now hermes-release.timer"
