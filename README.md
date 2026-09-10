# djisimhub

djisimhub 是 QDC507 的 Linux 短信与语音网关，通过 USB 连接模块，为 iOS App 和网页客户端提供 REST、SSE 与双向 PCM 音频。

- 短信：PDU 去重和分段拼接，仅保留最新 100 条；APNs 推送与客户端全局 ACK。
- 通话：PushKit 来电邀请、CallKit 接听归属、呼出、挂断和 WebSocket 音频。
- 维护：自定义 AT 命令、模块重启和 systemd 服务重启。
- 鉴权：单用户 Bearer API token、失败限流；设备注册同时管理短信和 VoIP token。

对接文档：[短信](docs/ios-apns.md)、[iOS 通话](docs/ios-calls.md)、[AT 与重启](docs/maintenance-api.md)。

## 安装教程

以下以 Debian 13（trixie）为例，项目安装到 `/root/djisimhub`，服务以 root 运行。配置读取项目根目录的 `config.toml`，运行数据默认保存在 `data/`。

### 1. 检查内核

```sh
uname -r
grep -E 'CONFIG_USB_SUPPORT|CONFIG_USB=|CONFIG_USB_XHCI_HCD|CONFIG_SND_USB_AUDIO' \
  /boot/config-$(uname -r)
```

**不要使用 Debian `cloud-amd64` 内核**：它可能缺少 USB host 或 USB Audio 支持。如果当前是 cloud 内核，或缺少上述支持，先安装通用内核：

```sh
sudo apt-get update
sudo apt-get install -y linux-image-amd64
sudo update-grub
sudo reboot
```

重启时在 GRUB 的 `Advanced options for Debian GNU/Linux` 中选择不含 `cloud-amd64` 的通用内核，再用 `uname -r` 确认。安装通用内核不代表已经切换成功；确认前不要删除当前内核。

PVE/KVM 用户应把整个模块按**物理 USB 端口**直通，避免初始化后 USB ID 从 `2ca3:4006` 变为 `2c7c:0125` 导致设备丢失。

### 2. 安装环境

进入 root shell，后续命令均在此 shell 中执行：

```sh
sudo -i
apt-get update
apt-get install -y \
  git curl ca-certificates build-essential pkg-config python3 python3-dev \
  libusb-1.0-0 libusb-1.0-0-dev libudev-dev \
  libasound2t64 libasound2-dev usbutils alsa-utils
```

安装 uv 并创建项目环境：

```sh
curl -LsSf https://astral.sh/uv/install.sh \
  | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
git clone https://github.com/NimaQu/djisimhub.git /root/djisimhub
cd /root/djisimhub
/usr/local/bin/uv sync --frozen
cp config.example.toml config.toml
mkdir -p -m 0700 data
chmod 0600 config.toml
```

此时先保留示例配置，供模块初始化读取语音资源路径。

### 3. 初始化模块

确认没有其他服务或程序（如 adb、MaVo、DJOneHub）占用模块，然后执行：

```sh
cd /root/djisimhub
/usr/local/bin/uv run --frozen python gateway.py module-setup --confirm
```

命令会自动识别原始或已转换的模块，备份原设置，完成 USB、ADB、IMS 和语音运行时初始化及自检；需要时重启模块，等待重新枚举。备份保存在 `data/module-backups/`，已就绪的模块不会重复写入或重启。

如果按 USB ID 直通导致模块转换后消失，将直通改为物理端口或 `2c7c:0125`，再运行同一命令。已有部署重新初始化前，先执行 `systemctl stop djisimhub.service`。

### 4. 配置与启动

编辑 `config.toml` 中已有配置表：

```toml
[server]
enabled = true
host = "127.0.0.1"
port = 8787
public_base_url = "https://gateway.example.com"
allow_service_restart = true
systemd_unit = "djisimhub.service"

[calls]
incoming_frontend = "app"
```

保留 `[app]`、`[module]` 和 `[apns]` 设置。相对路径以配置文件目录为基准；现有 `QDC507_*` 环境变量仍可覆盖对应配置。API 路径和 iOS 通话协议保持不变。

如运营商要求手动 APN，修改已有 `[network]` 配置：

```toml
[network]
apn = "connect" # 替换为运营商要求的值
pdp_type = "IP" # IP / IPV6 / IPV4V6
```

省略 `apn` 会保留模块现有设置；空字符串表示请求订阅默认 APN。修改后手动应用，服务启动不会自动应用 APN：

```sh
/usr/local/bin/uv run --frozen python gateway.py network-setup --confirm
```

已有服务需先停止，且不要在通话中执行。最后检查配置：

```sh
/usr/local/bin/uv run --frozen python gateway.py config-check
```

### 5. 前台验证

```sh
uv run --frozen djisimhub serve
```

在另一个终端检查 `http://127.0.0.1:8787/openapi.json`，确认日志出现 `module.connected`。停止前台进程后安装 systemd 服务。

### 6. systemd 部署

```sh
cd /root/djisimhub
cp djisimhub.example.service /etc/systemd/system/djisimhub.service
systemctl daemon-reload
systemctl enable --now djisimhub.service
systemctl --no-pager --full status djisimhub.service
```

模板默认以 root 运行，项目路径为 `/root/djisimhub`，uv 路径为 `/usr/local/bin/uv`；使用其他路径时先修改模板。服务每次启动前自动检查配置，修改配置后执行：

```sh
systemctl restart djisimhub.service
```

### 7. 获取 API Token

需要使用网页或 API 时执行：

```sh
cd /root/djisimhub
/usr/local/bin/uv run --frozen python gateway.py token
```

明文 Token 仅显示一次，请保存。再次执行会替换旧 Token，数据库只保存 scrypt hash。验证 API：

```sh
curl -H 'Authorization: Bearer <token>' \
  'http://127.0.0.1:8787/api/v1/module?refresh=true'
```

需要撤销 Token 时执行 `uv run --frozen python gateway.py token-delete`；此操作不会关闭 HTTP 服务。

### 8. 配置 HTTPS 域名

远程访问网页/API 需要准备一个可信的 **HTTPS 域名**，浏览器通话也需要 HTTPS 才能使用麦克风。项目不内置 TLS，建议使用 **cloudflared（Cloudflare Tunnel）**。

按 [Cloudflare Tunnel 官方教程](https://developers.cloudflare.com/tunnel/setup/) 在同一台主机安装并运行 cloudflared，创建隧道，将自己的域名（如 `gateway.example.com`）映射到 `http://127.0.0.1:8787`。保持 `server.host = "127.0.0.1"`，并将 `server.public_base_url` 设为 `https://gateway.example.com`。

完成后访问 `https://gateway.example.com/web/`，输入 API Token。代理需支持 SSE 和 WebSocket；iOS/API 客户端入口不要加入交互式网页登录规则，除非客户端已实现对应认证。

## API 文档

网页入口为 `/web/`，交互式 API 文档为 `/docs`，完整 OpenAPI 定义为 `/openapi.json`。REST API 使用 `Authorization: Bearer <token>` 认证。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/status` | 网关、音频和当前通话状态 |
| GET | `/api/v1/module` | 缓存的模块和网络状态；加 `?refresh=true` 在空闲时刷新 |
| GET | `/api/v1/sms` | 短信列表 |
| GET | `/api/v1/sms/{id}` | 单条短信 |
| POST | `/api/v1/sms/send` | 发送短信 |
| GET | `/api/v1/calls/current` | 当前通话 |
| POST | `/api/v1/calls/start` | 发起出站呼叫 |
| POST | `/api/v1/calls/{id}/answer` | 独立 API 客户端接听来电 |
| POST | `/api/v1/calls/{id}/hangup` | 挂断 |
| GET | `/api/v1/events` | SSE 事件流 |
| POST | `/api/v1/audio/diagnostic/start` | 不拨号的双向音频诊断 |
| WS | `/api/v1/calls/{id}/audio` | 双向通话音频 |
| PUT | `/api/v1/push/devices/{installation_id}` | 注册或更新 iOS 推送设备 |
| DELETE | `/api/v1/push/devices/{installation_id}` | 注销 iOS 推送设备 |

音频使用 8 kHz、单声道、little-endian PCM16，每帧 20 ms（320 字节）。连接 WebSocket 前用 Bearer Token 换取绑定会话的 30 秒一次性音频票据，避免将 Token 放进 URL。

内置网页支持出站通话，不提供来电接听按钮。独立客户端可设置 `calls.incoming_frontend = "web"`，获取来电后先建立音频连接，再调用 `answer`。

模块状态包括手机号、运营商、CSQ/dBm 和 `radio_metrics` 中的 LTE 测量值；缺失值为 `null`，SIM 未写入自号码不代表注册失败。SINR 字段依据见 [实机核实记录](docs/sinr-verification.md)。认证连续失败会返回 HTTP 429，按 `Retry-After` 等待后重试。

## 日志

```sh
# 实时查看
journalctl -u djisimhub.service -f -o short-iso

# 最近 100 条
journalctl -u djisimhub.service -n 100 --no-pager
```

默认日志等级为 `INFO`。排障时将 `config.toml` 中 `[logging].level` 改为 `"DEBUG"`，重启服务后导出：

```sh
systemctl restart djisimhub.service
journalctl -u djisimhub.service --since "10 minutes ago" \
  -o short-iso --no-pager > djisimhub-debug.log
```

排障后恢复 `INFO`。分享日志前遮盖电话号码等个人信息。
