# QDC507 Linux Headless Gateway

QDC507 的 Linux headless 短信与语音网关。宿主机通过 libusb 管理 AT/ADB，通过 UAC/ALSA
桥接 8 kHz PCM 音频；网页提供 REST、SSE 和双向音频 WebSocket，Telegram Bot 负责短信和命令，
Kurigram User session 只负责私人语音呼叫。

项目按普通 Python/uv 目录运行：不安装到系统 Python，不需要 wheel，也不提供内置 TLS。所有配置固定
读取项目根目录的 `config.toml`，运行数据保存在 `data/`。默认以 root 运行，方便访问 USB、ALSA 和
模块 voice runtime。

## 功能

- 收取、保存、转发和确认后发送 SMS；
- 网页拨号、接听、挂断、音频诊断和双向通话；
- Telegram `/call`、`/sendsms`、来电提示和私人通话桥；
- 模块手机号、运营商、无线制式、CSQ/dBm 信号状态；
- 只读 USB descriptor probe、直接 libusb ADB 和受控 QADBKEY 授权；
- SQLite 通话/SMS/事件历史以及只保存 scrypt hash 的 API Token；
- 按来源地址统计认证失败并临时封禁；
- systemd/journald 运行和可配置日志等级。

明确不做：使用 QDC507 内置网卡联网、自动修改 USBCFG/CFUN、未经确认发送短信或发起蜂窝电话。

## 1. Debian 13 全新安装

以下流程在 Debian 13 (trixie) VM 上使用 root service。先把整个 `2c7c:0125` USB 设备直通 VM；
Proxmox 建议按稳定物理 USB port 绑定，避免设备重枚举后地址变化。

安装系统依赖：

```sh
sudo apt-get update
sudo apt-get install -y \
  git curl ca-certificates build-essential pkg-config python3 python3-dev \
  libusb-1.0-0 libusb-1.0-0-dev libudev-dev \
  libasound2t64 libasound2-dev usbutils alsa-utils
```

按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/) 将可执行文件固定放到
systemd 示例使用的 `/usr/local/bin`：

```sh
curl -LsSf https://astral.sh/uv/install.sh \
  | sudo env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
/usr/local/bin/uv --version
```

clone 并创建项目环境：

```sh
sudo git clone <repository-url> /opt/qdc507-gateway
cd /opt/qdc507-gateway
sudo /usr/local/bin/uv sync --frozen
sudo cp config.example.toml config.toml
sudo mkdir -p -m 0700 data
sudo chmod 0600 config.toml
```

如果仓库尚未发布到远端，也可以把完整 Git 工作树复制到 `/opt/qdc507-gateway`；不要复制 `.venv`、
`config.toml`、`data/` 或 session 文件。

## 2. 配置

编辑 `/opt/qdc507-gateway/config.toml`：

```toml
[app]
data_dir = "data"
lock_path = "data/device.lock"

[server]
host = "127.0.0.1"
port = 8787

[logging]
level = "INFO" # CRITICAL, ERROR, WARNING, INFO, DEBUG

[security]
auth_max_failures = 10
auth_failure_window_seconds = 300
auth_block_seconds = 900

[calls]
incoming_frontend = "telegram" # web, telegram, auto

[telegram]
session = "data/telegram.session"
bot_session = "data/telegram-bot.session"
api_id = 123456
api_hash = "replace-me"
user_id = 123456789
bot_token = "123456789:replace-me"
allow_service_restart = false

[module]
voice_manifest = "resources/module-voice/manifest.json"
voice_resource_dir = "resources/module-voice"
```

相对路径都以 `config.toml` 所在目录为基准。`user_id` 是唯一允许操作 Bot、接收短信和接听电话的
个人账号。Kurigram User session 必须登录另一个账号，否则无法从网关账号呼叫这个个人账号。

检查配置不会输出 `api_hash`、`bot_token` 或任何 session 内容：

```sh
cd /opt/qdc507-gateway
sudo /usr/local/bin/uv run --frozen python gateway.py config-check
```

## 3. USB、ADB 和音频前置检查

```sh
lsusb -d 2c7c:0125
cd /opt/qdc507-gateway
sudo /usr/local/bin/uv run --frozen python gateway.py probe --json
aplay -l
arecord -l
```

Probe 必须看到 descriptor 匹配的 ADB `FF/42/01`、UAC 7/8/9 和音频 endpoint。它不会 claim、reset、
写 AT 或改变模块设置。

新模块的出厂 USBCFG 通常不包含本项目需要的完整 ADB + Audio 组合。确认 systemd 服务、宿主机
`adb`、MaVo 和 DJOneHub 都没有占用模块后，执行一次初始化：

```sh
sudo systemctl stop qdc507-gateway
cd /opt/qdc507-gateway
sudo /usr/local/bin/uv run --frozen python gateway.py module-setup --confirm
```

`module-setup` 会先读取 USBCFG。如果不是完整目标，它只写入一次
`2C7C:0125,diagnostic=1,nmea=1,at=1,modem=1,network=1,adb=1,audio=1`，再执行一次
`CFUN=1,1` 并等待同一物理 USB 设备重枚举。随后它检测 ADB root；只有 ADB 尚不可用时才执行
QADBKEY 授权，最后上传并自检 `[module]` 配置的 voice runtime。重复执行时，已经正确的 USBCFG
不会再次写入或重启模块。

这个初始化不会发送短信、拨号、配置 USB 网卡或通过模块联网。`network=1` 只是保持当前复合 USB
descriptor 的完整目标；命令不会在宿主机上启用该网络接口。输出不会包含 QADBKEY challenge、
response 或完整授权命令。成功后可重新启动服务：

```sh
sudo systemctl start qdc507-gateway
```

若只需要在 USBCFG 已正确的模块上重新执行 QADBKEY，仍可使用：

```sh
sudo /usr/local/bin/uv run --frozen python gateway.py adb-authorize --confirm
```

## 4. Telegram 与 API Token

创建 Kurigram User session：

```sh
cd /opt/qdc507-gateway
sudo /usr/local/bin/uv run --frozen python gateway.py telegram-login
sudo /usr/local/bin/uv run --frozen python gateway.py telegram-compat
```

`telegram-login` 会交互询问 User 账号手机号、验证码和两步验证密码。安装的发行包是 `kurigram`，
代码使用其兼容的 `pyrogram` import namespace；不安装停止维护的 Pyrogram distribution。

Bot 使用 `bot_token` 自动登录，不需要交互 session。启动后先由唯一用户向 Bot 发送 `/start`，这样
Bot 才能主动发送短信和来电通知。

创建 API Token：

```sh
sudo /usr/local/bin/uv run --frozen python gateway.py token
```

明文 Token 只显示这一次。SQLite 只保存 scrypt hash；任意未撤销 Token 都拥有全部 API 权限。
网页会把 Token 保存在当前站点的 `localStorage`。如需撤销：

```sh
sudo /usr/local/bin/uv run --frozen python gateway.py token-revoke <token-id>
```

## 5. 首次前台启动

先前台运行以发现配置或硬件问题：

```sh
cd /opt/qdc507-gateway
sudo /usr/local/bin/uv run --frozen python gateway.py serve
```

另一个终端验证：

```sh
curl -fsS http://127.0.0.1:8787/openapi.json | python3 -m json.tool >/dev/null
curl -H 'Authorization: Bearer <token>' \
  'http://127.0.0.1:8787/api/v1/module?refresh=true'
```

模块状态包含：

- `phone_number` / `subscriber`：`AT+CNUM` 返回的 SIM 自号码；运营商未写入时允许为 `null`；
- `operator`：`AT+COPS?` 返回的名称和无线制式；
- `signal`：`AT+CSQ` 的 RSSI、dBm、0–5 格；
- `network_measured_at` 和逐项 `network_errors`。

确认无误后按 `Ctrl+C` 停止前台服务。

## 6. systemd

仓库根目录只有一个部署模板 `qdc507-gateway.example.service`。默认路径已经是
`/opt/qdc507-gateway`，uv 路径已经是 `/usr/local/bin/uv`；使用其他路径时先修改这两项。

```sh
cd /opt/qdc507-gateway
sudo cp qdc507-gateway.example.service /etc/systemd/system/qdc507-gateway.service
sudo systemctl daemon-reload
sudo systemctl enable --now qdc507-gateway.service
sudo systemctl --no-pager --full status qdc507-gateway.service
sudo journalctl -u qdc507-gateway.service -n 100 --no-pager
```

示例没有 `User=`，因此以 root 运行。`UMask=0077` 保护 SQLite 和 Telegram session；每次启动前
自动执行 `config-check`。升级后使用：

```sh
cd /opt/qdc507-gateway
sudo systemctl stop qdc507-gateway.service
sudo git pull --ff-only
sudo /usr/local/bin/uv sync --frozen
sudo /usr/local/bin/uv run --frozen python gateway.py config-check
sudo systemctl start qdc507-gateway.service
```

## 7. Tailscale HTTPS 与监听地址

推荐保持 `server.host = "127.0.0.1"`，让 Tailscale Serve 在同一台 VM 终止 HTTPS。按照当前
[Tailscale Serve 文档](https://tailscale.com/docs/reference/tailscale-cli/serve)：

```sh
sudo tailscale serve --bg 8787
tailscale serve status
```

最终从 `https://<node>.<tailnet>.ts.net/web/` 访问。浏览器麦克风需要可信 HTTPS，Serve 会同时代理
HTTP、SSE 和 WebSocket。不要使用 `tailscale funnel`，除非明确希望向整个互联网公开。

监听选择：

- 同机 Tailscale Serve、Caddy 或 nginx：`127.0.0.1`；
- 另一台 LAN 反向代理：绑定 VM 的固定 LAN IP，并用防火墙只允许代理来源；
- 不建议 `0.0.0.0`，它会暴露到所有 VM 网卡。

## 8. Bot 命令与 User session 恢复

唯一用户可使用：

- `/status`：人类可读的模块、手机号、运营商、信号、Telegram、通话和音频状态；
- `/call <号码>`：先接通 Telegram，再由模块拨号；
- `/sendsms <号码>`：创建草稿，回复内容后通过“发送/取消”按钮确认；
- `/hangup`：挂断当前通话；
- `/userlogin <User账号手机号>`：User session 失效时发送新验证码；
- `/usercode <验证码>`：提交登录验证码；
- `/userpassword <两步验证密码>`：仅在账号启用两步验证时使用；
- `/restart`：仅当 `telegram.allow_service_restart = true` 时通过 systemd 重启服务。

Bot 和 User 客户端相互独立：User session 缺失或启动失败时，Bot 仍会在线并显示“需要登录”。在线
重登成功后会把旧 session 备份为 `telegram.session.bak`，再启动新的通话客户端。Bot 会尽力删除包含
手机号、验证码或密码的命令消息，但 Telegram 聊天不是最理想的密码输入通道；能够 SSH 时优先使用
`telegram-login`。登录流程 10 分钟后自动失效，通话或音频会话期间拒绝重登。

`/restart` 只适用于本 README 的 root systemd 部署。修改配置后必须先启用该选项并手动重启一次，
之后 Bot 才会注册此命令。

## 9. API 与网页

主要入口：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/web/` | 网页控制台 |
| GET | `/docs` | OpenAPI 文档 |
| GET | `/api/v1/status` | 网关、Telegram、音频和当前通话状态 |
| GET | `/api/v1/module` | 缓存的模块/网络状态 |
| GET | `/api/v1/module?refresh=true` | 安全空闲时重新读取 CNUM/COPS/CSQ |
| GET | `/api/v1/sms` | 短信列表 |
| POST | `/api/v1/sms/send` | 发送短信 |
| GET | `/api/v1/calls/current` | 当前通话 |
| POST | `/api/v1/calls/start` | 网页出站呼叫 |
| POST | `/api/v1/calls/{id}/answer` | 接听网页来电 |
| POST | `/api/v1/calls/{id}/hangup` | 挂断 |
| GET | `/api/v1/events` | SSE 事件流 |
| POST | `/api/v1/audio/diagnostic/start` | 不拨号的双向音频诊断 |
| WS | `/api/v1/calls/{id}/audio` | 8 kHz PCM16 mono 通话音频 |

音频 WebSocket 使用 20 ms、320-byte little-endian PCM16 帧。Bearer Token 不进入 URL 或 Cookie；
音频连接先用 Bearer Token 换取 30 秒、绑定会话 ID 的一次性票据。

认证失败按来源 IP 在内存中统计。默认 5 分钟内 10 次失败后封禁 15 分钟，响应为 HTTP 429 并带
`Retry-After`；服务重启会清空封禁。只有请求直接来自 loopback 反向代理时才信任合法的
`X-Forwarded-For`，避免普通远程客户端伪造来源。

## 10. 日志与排障

正常运行使用 `INFO`：

```sh
sudo journalctl -u qdc507-gateway.service -f -o short-iso
```

需要收集详细日志时把 `config.toml` 改为：

```toml
[logging]
level = "DEBUG"
```

然后：

```sh
sudo systemctl restart qdc507-gateway.service
sudo journalctl -u qdc507-gateway.service --since "10 minutes ago" \
  -o short-iso --no-pager > qdc507-debug.log
```

日志和 SQLite 事件不会记录 API Token、Bot Token、QADBKEY 密码、challenge response 或完整授权
命令。通话号码、状态和错误类型会进入日志用于排障；分享日志前仍应按需要遮盖电话号码。

常见检查：

```sh
systemctl is-active qdc507-gateway.service
lsusb -d 2c7c:0125
curl -fsS http://127.0.0.1:8787/openapi.json | grep '"version"'
sudo journalctl -u qdc507-gateway.service -n 200 --no-pager
```

- `phone_number = null`：SIM/运营商没有通过 CNUM 写入自号码，不代表注册失败；
- 有网页上行样本但对端听不到：检查模块 voice runtime、UAC playback 与天线/VoLTE；
- 下行 `nonzero_samples = 0`：模块没有输出蜂窝音频，先检查通话是否真正 active；
- User `login_required`、Bot `connected`：用 Bot 在线重登或 SSH 执行 `telegram-login`；
- HTTP 429：等待 `Retry-After`，或确认没有旧 Token 的客户端持续重试。

## 11. 删除 VM 前的全新部署验收

建议按以下顺序在新 VM 完整走一遍：

1. USB 直通后 `lsusb` 与 descriptor probe；
2. `uv sync --frozen`、复制 TOML、`config-check`；
3. 停止服务并执行一次 `module-setup --confirm`，完成 USBCFG、QADBKEY 和 voice runtime 自检；
4. `telegram-login`、`telegram-compat`、创建 API Token；
5. 前台启动并请求 `module?refresh=true`；
6. 安装并启动 systemd，检查 journald；
7. 配置 Tailscale Serve，从 HTTPS 网页连接；
8. 先做音频诊断，再分别测试网页和 Telegram 通话；
9. 测试 SMS 接收、Bot 草稿修改/取消，最后才显式确认发送；
10. 临时移走 User session，确认 Bot 仍在线并可执行 `/status` 和重登流程；
11. 用错误 Token 验证 401/429，再用正确 Token 验证封禁到期后的恢复；
12. 将日志等级恢复为 `INFO`。

默认 pytest 完全离线，不连接模块、不发送短信、不拨号、不写 USBCFG、不重启设备：

```sh
sudo /usr/local/bin/uv sync --frozen --extra dev
sudo /usr/local/bin/uv run pytest -q
sudo /usr/local/bin/uv run ruff check src tests gateway.py
```
