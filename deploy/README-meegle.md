# meegle binary pin — 部署 RUNBOOK

让飞书项目（meegle）连接器读取**直跑二进制**而非 `npx -y`，把 Run Broker 冷
`/connectors` 从 ~11s 降到 ~1s，避免 Connectors 面板回退到「检测失败」。

## 根因（一句话）

`credential_hub._meegle_invocation()` 没找到直跑 `meegle` 时回退 `npx -y @lark-project/meegle`，
npx 每次重新解析包（实测 10–32s）。装个直跑二进制到 `~/.local/bin`（在 gateway unit
PATH 上、且在 `_meegle_search_path()` 里），reader 自动改用它，**零代码改动**。

## 两个部件

1. **`ensure-meegle.sh`** — 幂等安装脚本：`~/.local/bin/meegle` 不存在才装，存在即秒退；
   安装失败也 `exit 0`（绝不阻塞网关启动）。
2. **`hermes-gateway-meegle.conf`** — gateway drop-in，两件事：
   - `Environment=HERMES_MEEGLE_BIN=%h/.local/bin/meegle` —— 让 `_meegle_invocation()` 最高优先级
     直指该二进制，**不依赖 unit PATH 是否含 `~/.local/bin`**（否则重建改了 PATH 会静默回退 npx）。
   - `ExecStartPre=-`（前导 `-` = 非致命）每次网关启动前跑脚本自愈那个二进制存在。

## 安装（hermes-1，hermes 用户）

```bash
REPO=/home/hermes/code/hermes-multitenancy            # 实际 checkout 路径
chmod +x "$REPO/deploy/ensure-meegle.sh"
# 1) 现在就装好二进制（首次；之后由 drop-in 自愈）
"$REPO/deploy/ensure-meegle.sh"
# 2) 装 drop-in（@REPO@ 占位替换为真实路径）
mkdir -p ~/.config/systemd/user/hermes-gateway.service.d
sed "s#@REPO@#$REPO#g" "$REPO/deploy/hermes-gateway-meegle.conf" \
  > ~/.config/systemd/user/hermes-gateway.service.d/45-meegle-bin.conf
systemctl --user daemon-reload
# 不需要重启网关：二进制装好后 reader 下次调用即生效（call 时 shutil.which 解析）。
# drop-in 只在“下次网关启动”生效，用于重建/被清理后的自愈。
```

## 验证

```bash
ls -l ~/.local/bin/meegle                              # 软链存在
# 冷 /connectors 应 ~1s（而非 ~11s）：
URL=$(grep ^HERMES_RUN_BROKER_URL= /home/hermes/code/hermes-web-ui/.env | cut -d= -f2-)
KEY=$(grep ^HERMES_RUN_BROKER_KEY= /home/hermes/code/hermes-web-ui/.env | cut -d= -f2-)
curl -s -o /dev/null -w "%{time_total}s\n" -H "Authorization: Bearer $KEY" \
  "$URL/api/run-broker/connectors?profile_name=sunke&fresh=1"
```

## 注意

- 这是 ops 二进制，不在 git 里——所以靠本 drop-in 的 `ExecStartPre` 自愈，并把本步骤
  写进每次 prod provisioning/重建流程。
- 改安装路径：同时改脚本的 `HERMES_MEEGLE_PREFIX` 与 drop-in 的 `HERMES_MEEGLE_BIN`
  （二者必须指向同一 `bin/meegle`）。
- 与 [[meegle-node-resolve-robust]] 同源：meegle 修复必须是**提交代码/脚本**，不是
  脆弱的 plist/手改主机（codex review 定的规矩）。
