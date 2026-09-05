# DJI2Telegram

DJI2Telegram 是 QDC507 的 Linux headless 短信与语音网关。宿主机通过 libusb 管理 AT/ADB，通过 UAC/ALSA
桥接 8 kHz PCM 音频；网页提供 REST、SSE 和双向音频 WebSocket，Telegram Bot 负责短信和命令，
Kurigram User session 只负责私人语音呼叫。

项目按普通 Python/uv 目录运行：不安装到系统 Python，不需要 wheel，也不提供内置 TLS。所有配置固定
读取项目根目录的 `config.toml`，运行数据保存在 `data/`。默认以 root 运行，方便访问 USB、ALSA 和
模块 voice runtime。

> [!WARNING]
> **不要使用 Debian `cloud-amd64` 内核。** DJI2Telegram 需要 USB host、libusb 和 USB Audio/UAC；
> Debian cloud 内核可能裁掉这些支持，表现为 Proxmox 已配置 USB 直通，但 VM 内 `lsusb` 看不到
> QDC507，甚至看不到 USB root hub。必须启动通用的 `linux-image-amd64` 内核。

先检查当前内核：

```sh
uname -r
grep -E 'CONFIG_USB_SUPPORT|CONFIG_USB=|CONFIG_USB_XHCI_HCD|CONFIG_SND_USB_AUDIO' \
  /boot/config-$(uname -r)
```

如果 `uname -r` 包含 `-cloud-amd64`，或配置显示 `# CONFIG_USB_SUPPORT is not set`，安装通用内核：

```sh
sudo apt-get update
sudo apt-get install -y linux-image-amd64
sudo update-grub
```

在 Proxmox Console 重启 VM，进入 GRUB 的 `Advanced options for Debian GNU/Linux`，选择名称中包含
`amd64`、但**不包含** `cloud-amd64` 的最新内核。通用内核安装后，GRUB 仍可能优先启动同版本 cloud
内核，因此不能只安装后直接假定切换成功。启动后必须确认：

```sh
uname -r                         # 必须以 -amd64 结尾，不能包含 -cloud-amd64
lsusb -d 2c7c:0125
aplay -l
arecord -l
```

只有确认通用内核、USB 和 UAC 均正常后，才列出并删除 cloud 元包以及与通用内核同版本的 cloud
image，再次更新 GRUB：

```sh
dpkg -l 'linux-image-*cloud-amd64' | grep '^ii'
# 根据上一行填写实际包名；下面的版本号仅为示例：
sudo apt-get remove linux-image-cloud-amd64 linux-image-6.12.105+deb13-cloud-amd64
sudo update-grub
```

可以暂时保留一个更旧的 cloud image 作为紧急回退。在通用内核成功启动前，不要删除当前 cloud 内核，
也不要执行 `apt autoremove`。如果通用内核下仍只有 USB root hub 而没有 `2c7c:0125`，再检查
Proxmox VM 的 USB passthrough；这时才是直通问题。

## 功能

- 收取、保存、转发和确认后发送 SMS；
- 网页拨号、挂断出站通话、音频诊断和双向通话；
- Telegram `/call`、`/sendsms`、来电提示和私人通话桥；
- 模块手机号、运营商、无线制式、CSQ/dBm 信号状态；
- 只读 USB descriptor probe、直接 libusb ADB 和受控 QADBKEY 授权；
- SQLite 通话/SMS/事件历史以及只保存 scrypt hash 的 API Token；
- 按来源地址统计认证失败并临时封禁；
- systemd/journald 运行和可配置日志等级。

## 1. Debian 13 全新安装

以下流程在 Debian 13 (trixie) VM 上使用 root service，项目默认放在 `/root/DJI2Telegram`。先把整个
`2c7c:0125` USB 设备直通 VM；
Proxmox 建议按物理 USB port 绑定，避免设备重枚举后地址变化。

先进入 root shell；后续所有项目命令都在这个 shell 中执行：

```sh
sudo -i
```

安装系统依赖：

```sh
sudo apt-get update
sudo apt-get install -y \
  git curl ca-certificates build-essential pkg-config python3 python3-dev linux-image-amd64 \
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
sudo git clone https://github.com/NimaQu/DJI2Telegram.git /root/DJI2Telegram
cd /root/DJI2Telegram
sudo /usr/local/bin/uv sync --frozen
sudo cp config.example.toml config.toml
sudo mkdir -p -m 0700 data
sudo chmod 0600 config.toml
```

## 2. 配置

编辑 `/root/DJI2Telegram/config.toml`：

```toml
[app]
data_dir = "data"
lock_path = "data/device.lock"

[server]
enabled = true
host = "127.0.0.1"
port = 8787

[logging]
level = "INFO" # CRITICAL, ERROR, WARNING, INFO, DEBUG

[security]
auth_max_failures = 10
auth_failure_window_seconds = 300
auth_block_seconds = 900

[calls]
incoming_frontend = "telegram" # telegram, auto；web 仅供未来的独立 API 客户端

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
cd /root/DJI2Telegram
sudo /usr/local/bin/uv run --frozen python gateway.py config-check
```

## 3. USB、ADB 和音频前置检查

USB VID/PID 可在 `config.toml` 中配置；省略时仍使用 `2c7c:0125`。
对于 `lsusb` 显示为 `2ca3:4006` 的 EG25G-QDC507，添加：

```toml
[usb]
vendor_id = 0x2ca3
product_id = 0x4006
```

也可通过 `QDC507_USB_VENDOR_ID` / `QDC507_USB_PRODUCT_ID` 覆盖，值如 `0x2ca3`。
配置用于 probe、AT/ADB 连接、热插拔、状态显示和宿主机 ALSA 声卡识别。
`module-setup` 使用相同 VID/PID 启用完整接口组合，不会强制改为 `2c7c:0125`。
下方 `lsusb -d` 的 ID 也应替换为实际配置值。
设置 ID 只解决设备选择，不保证其他型号或固件兼容。已提供的 `2ca3:4006` 描述有
5 个厂商接口，尚无 ADB/UAC；完整语音功能仍需初始化并验证固件、ADB 和音频。


```sh
lsusb -d 2c7c:0125
cd /root/DJI2Telegram
sudo /usr/local/bin/uv run --frozen python gateway.py probe --json
aplay -l
arecord -l
```

Probe 必须看到 descriptor 匹配的 ADB `FF/42/01`、UAC 7/8/9 和音频 endpoint。它不会 claim、reset、
写 AT 或改变模块设置。

新模块的出厂 USBCFG 通常不包含本项目需要的完整 ADB + Audio 组合。确认 systemd 服务、宿主机
`adb`、MaVo 和 DJOneHub 都没有占用模块后，执行一次初始化：

```sh
sudo systemctl stop dji2telegram
cd /root/DJI2Telegram
sudo /usr/local/bin/uv run --frozen python gateway.py module-setup --confirm
```

`module-setup` 会先读取 USBCFG。如果不是完整目标，它只写入一次
配置中的 VID/PID 和 `diagnostic=1,nmea=1,at=1,modem=1,network=1,adb=1,audio=1`，再执行一次
`CFUN=1,1` 并等待同一物理 USB 设备重枚举。随后它检测 ADB root；只有 ADB 尚不可用时才执行
QADBKEY 授权，最后上传并自检 `[module]` 配置的 voice runtime。重复执行时，已经正确的 USBCFG
不会再次写入或重启模块。

这个初始化不会发送短信、拨号、配置 USB 网卡或通过模块联网。`network=1` 只是保持当前复合 USB
descriptor 的完整目标；命令不会在宿主机上启用该网络接口。输出不会包含 QADBKEY challenge、
response 或完整授权命令。成功后可重新启动服务：

```sh
sudo systemctl start dji2telegram
```

若只需要在 USBCFG 已正确的模块上重新执行 QADBKEY，仍可使用：

```sh
sudo /usr/local/bin/uv run --frozen python gateway.py adb-authorize --confirm
```

## 4. Telegram 与 API Token

创建 Kurigram User session：

```sh
cd /root/DJI2Telegram
sudo /usr/local/bin/uv run --frozen python gateway.py telegram-login
```

`telegram-login` 会交互询问 User 账号手机号、验证码和两步验证密码。安装的发行包是 `kurigram`，
代码使用其兼容的 `pyrogram` import namespace；不安装停止维护的 Pyrogram distribution。

Bot 使用 `bot_token` 自动登录，不需要交互 session。启动后先由唯一用户向 Bot 发送 `/start`，这样
Bot 才能主动发送短信和来电通知。

> [!IMPORTANT]
> **新建或重新登录 User session 后，必须先建立一次 User peer。** 等日志出现
> `telegram.connected` 后，用配置中 `telegram.user_id` 对应的个人账号，给“网关 User 账号”发送一条
> 新的普通私聊消息。这里不是给 Bot 发消息；Bot session 与 User session 的 peer 缓存完全独立。

Kurigram 通过数字 `user_id` 发起私人通话时还需要该用户的 `access_hash`。全新 session 即使登录成功，
也可能尚未缓存个人账号的完整 peer；这时 `/call` 会立即失败，日志显示
`telegram.call.failed ... PeerIdInvalid`，模块不会开始蜂窝拨号。发送上述私聊消息后通常无需重启即可
重试。如果仍然失败，用网关 User 账号的官方 Telegram 客户端添加个人账号为联系人、互相发送一条
消息，然后重启 `dji2telegram.service`。每次删除 session 或在全新 VM 重新登录后都应重复此步骤。

创建 API Token：

```sh
sudo /usr/local/bin/uv run --frozen python gateway.py token
```

明文 Token 只显示这一次，SQLite 只保存一个 scrypt hash。再次执行 `token` 会立即替换旧 Token，
旧值不能再用于新的 REST、SSE 或音频票据请求。网页会把 Token 保存在当前站点的 `localStorage`。
如果只使用 Telegram、不需要 Web/API，可以彻底关闭 HTTP 监听：

```toml
[server]
enabled = false
```

重启服务后，模块、短信、电话和 Telegram 仍会运行，但不会监听 `server.host/server.port`，网页控制台、
REST、SSE、WebSocket 和 OpenAPI 都无法访问。关闭时 `calls.incoming_frontend` 不能设为 `web`；使用
`telegram` 或 `auto`。重新设为 `true` 并重启即可恢复，已有 API Token 会保留。也可以用环境变量
`QDC507_SERVER_ENABLED=false` 临时覆盖。

仅删除 Token 不会关闭 HTTP 监听，但会撤销所有 Bearer API 访问：

```sh
sudo /usr/local/bin/uv run --frozen python gateway.py token-delete
```

删除后所有 Bearer 认证请求都会返回 HTTP 401；重新执行 `token` 即可恢复 API 访问。

## 5. 首次前台启动

先前台运行以发现配置或硬件问题：

```sh
cd /root/DJI2Telegram
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

仓库根目录只有一个部署模板 `dji2telegram.example.service`。默认路径已经是
`/root/DJI2Telegram`，uv 路径已经是 `/usr/local/bin/uv`；使用其他路径时先修改这两项。

```sh
cd /root/DJI2Telegram
sudo cp dji2telegram.example.service /etc/systemd/system/dji2telegram.service
sudo systemctl daemon-reload
sudo systemctl enable --now dji2telegram.service
sudo systemctl --no-pager --full status dji2telegram.service
sudo journalctl -u dji2telegram.service -n 100 --no-pager
```

示例没有 `User=`，因此以 root 运行。`UMask=0077` 保护 SQLite 和 Telegram session；每次启动前
自动执行 `config-check`。升级后使用：

```sh
cd /root/DJI2Telegram
sudo systemctl stop dji2telegram.service
sudo git pull --ff-only
sudo /usr/local/bin/uv sync --frozen
sudo /usr/local/bin/uv run --frozen python gateway.py config-check
sudo systemctl start dji2telegram.service
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
- `/sendat`：回复任意 ASCII AT 命令，先通过“执行/取消”按钮确认，再返回模块结果；
- `/restartmodule`（别名 `/rebootmodule`）：通过按钮确认后发送 `AT+CFUN=1,1` 重启 QDC507 模块；
- `/hangup`：挂断当前通话；
- `/userlogin`：交互式恢复 User session；Bot 会依次询问手机号、验证码，并仅在需要时询问两步验证密码；
- `/cancel`：取消当前短信草稿、AT 命令或 User 登录流程；
- `/restart`：仅当 `telegram.allow_service_restart = true` 时通过 systemd 重启服务。

Bot 和 User 客户端相互独立：User session 缺失或启动失败时，Bot 仍会在线并显示“需要登录”。在线
重登成功后会把旧 session 备份为 `telegram.session.bak`，再启动新的通话客户端。交互过程中直接回复
Bot 即可，不需要手动输入 `/usercode` 或 `/userpassword`。Bot 会尽力删除包含手机号、验证码或密码的
回复消息，但 Telegram 聊天不是最理想的密码输入通道；能够 SSH 时优先使用
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
| POST | `/api/v1/calls/start` | 网页或独立客户端发起出站呼叫 |
| POST | `/api/v1/calls/{id}/answer` | 独立 API 客户端接听蜂窝来电 |
| POST | `/api/v1/calls/{id}/hangup` | 挂断 |
| GET | `/api/v1/events` | SSE 事件流 |
| POST | `/api/v1/audio/diagnostic/start` | 不拨号的双向音频诊断 |
| WS | `/api/v1/calls/{id}/audio` | 8 kHz PCM16 mono 通话音频 |

音频 WebSocket 使用 20 ms、320-byte little-endian PCM16 帧。Bearer Token 不进入 URL 或 Cookie；
音频连接先用 Bearer Token 换取 30 秒、绑定会话 ID 的一次性票据。

内置网页不显示接听按钮，也不会为蜂窝来电连接音频或调用 `answer`。未来开发独立 App 时，可将
`calls.incoming_frontend` 设为 `web`，通过 SSE 或 `/api/v1/calls/current` 获取来电，申请
`audio-ticket` 并建立音频 WebSocket，确认音频就绪后再调用 `/api/v1/calls/{id}/answer`。通话控制
保持 RESTful；实时 PCM 不适合通过普通 REST 传输，因此继续使用带一次性票据的 WebSocket。

认证失败按来源 IP 在内存中统计。默认 5 分钟内 10 次失败后封禁 15 分钟，响应为 HTTP 429 并带
`Retry-After`；服务重启会清空封禁。只有请求直接来自 loopback 反向代理时才信任合法的
`X-Forwarded-For`，避免普通远程客户端伪造来源。

## 10. 日志与排障

正常运行使用 `INFO`：

```sh
sudo journalctl -u dji2telegram.service -f -o short-iso
```

需要收集详细日志时把 `config.toml` 改为：

```toml
[logging]
level = "DEBUG"
```

然后：

```sh
sudo systemctl restart dji2telegram.service
sudo journalctl -u dji2telegram.service --since "10 minutes ago" \
  -o short-iso --no-pager > dji2telegram-debug.log
```

日志和 SQLite 事件不会记录 API Token、Bot Token、QADBKEY 密码、challenge response 或完整授权
命令。通话号码、状态和错误类型会进入日志用于排障；分享日志前仍应按需要遮盖电话号码。

常见检查：

```sh
systemctl is-active dji2telegram.service
lsusb -d 2c7c:0125
curl -fsS http://127.0.0.1:8787/openapi.json | grep '"version"'
sudo journalctl -u dji2telegram.service -n 200 --no-pager
```

- `phone_number = null`：SIM/运营商没有通过 CNUM 写入自号码，不代表注册失败；
- 有网页上行样本但对端听不到：检查模块 voice runtime、UAC playback 与天线/VoLTE；
- 下行 `nonzero_samples = 0`：模块没有输出蜂窝音频，先检查通话是否真正 active；
- User `login_required`、Bot `connected`：用 Bot 在线重登或 SSH 执行 `telegram-login`；
- `telegram.call.failed ... PeerIdInvalid`：新 User session 尚未认识个人账号；由个人账号给网关 User
  账号发送一条新的普通私聊消息，注意不是发送给 Bot；
- HTTP 429：等待 `Retry-After`，或确认没有旧 Token 的客户端持续重试。

默认 pytest 完全离线，不连接模块、不发送短信、不拨号、不写 USBCFG、不重启设备：

```sh
sudo /usr/local/bin/uv sync --frozen --extra dev
sudo /usr/local/bin/uv run pytest -q
sudo /usr/local/bin/uv run ruff check src tests gateway.py
```
