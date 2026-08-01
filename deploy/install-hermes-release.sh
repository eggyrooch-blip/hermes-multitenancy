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
echo "已就绪。启用：systemctl --user daemon-reload && systemctl --user enable --now hermes-release.timer"
