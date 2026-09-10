# AT 命令与重启 API

所有接口使用现有 `Authorization: Bearer <bridge-api-token>`，复用失败限流。无需新 token；iOS 设备注册和通话协议不变。

## 自定义 AT 命令

```sh
curl -X POST "$ENDPOINT/api/v1/module/at" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"command":"AT+CSQ","timeout_ms":3000}'
```

`command` 是一行 ASCII AT 命令，最多 1024 字符，不允许 CR/LF、控制字符或二进制内容。`timeout_ms` 可选，默认 3000，范围 100–60000。返回模块响应，包含 `lines`、`urcs`、`terminal`、`ok`；HTTP 200 不代表 AT 最终结果一定为 OK，客户端应检查 `terminal`。

沿用既有持久配置保护：涉及 `AT+QCFG=`、`AT+CFUN`、`AT&W` 等命令时添加 `"confirm_persistent":true`；未提供返回 409。`QADBKEY` 只能使用专门的 `/api/v1/module/adb/authorize` 接口。

命令使用模块既有串行执行通道；通话或音频诊断期间返回 409。服务不记录自定义命令和响应正文，客户端应自行决定是否保存执行结果。命令可能改变模块设置，不应因 HTTP 超时而自动重放。

## 重启蜂窝模块

```sh
curl -X POST "$ENDPOINT/api/v1/module/restart" \
  -H "Authorization: Bearer $TOKEN"
```

执行固定的 `AT+CFUN=1,1`，复用模块 USB 重枚举恢复逻辑。返回 200：

```json
{"accepted":true,"target":"module","result":{"reenumerated":true}}
```

`result` 为实际模块执行结果，字段随响应而异。不会重启 Linux 主机或 bridge 进程；模块重新注册网络可能需要额外时间，随后查询 `/api/v1/module?refresh=true` 或订阅 SSE。通话或音频诊断期间返回 409，模块不可用返回 503。

## 重启 bridge 服务

```toml
[server]
allow_service_restart = true
systemd_unit = "djisimhub.service"
```

仅从 `config.toml` 读取。默认关闭；systemd unit 从配置读取，客户端不能提交 shell 命令或服务名称。进程需要具备重启该 unit 的权限。

```sh
curl -X POST "$ENDPOINT/api/v1/service/restart" \
  -H "Authorization: Bearer $TOKEN"
```

返回 202 后约 1 秒执行 `systemctl --no-block restart djisimhub.service`：

```json
{"accepted":true,"target":"service","unit":"djisimhub.service"}
```

202 表示已安排重启，不能证明 systemd 执行成功；失败写入 journald。重复请求在待执行期间合并，期间拒绝新通话、音频诊断和 AT 操作。通话或音频诊断期间返回 409，配置未启用返回 403。不会重启 Linux 主机。

连接会短暂中断。App 应等待数秒后重连 SSE，再查询 `/api/v1/status`，通过 `uptime_seconds` 判断服务是否已重启。短信、设备注册和 API token 保存在 SQLite，不因服务重启丢失。
