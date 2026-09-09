# iOS 短信推送对接

Bridge 使用 Apple Push Notification service (APNs) 直接发送普通 alert 通知，不经过第三方推送平台。当前为单用户系统，所有已注册普通通知 token 的设备接收新收到的完整短信。PushKit / CallKit 通话另见 [iOS 通话对接](ios-calls.md)。

## 服务器配置

在现有 `config.toml` 的对应表中合并以下字段，不要重复声明已有表：

```toml
[server]
enabled = true
host = "127.0.0.1"
port = 8787
public_base_url = "https://djihub.fubuki.app"

[apns]
enabled = false
sandbox = true
# key_path = "secrets/AuthKey.p8"
# key_id = "APPLE_KEY_ID"
# team_id = "APPLE_TEAM_ID"
# bundle_id = "your.app.bundle"

[telegram]
sms_forwarding_enabled = false
```

`public_base_url` 是客户端入口信息，不会自动创建 DNS、TLS 证书或 Cloudflare 隧道，也不影响监听地址。预期 cloudflared 将 `https://djihub.fubuki.app` 转发到 `http://127.0.0.1:8787`。隧道中不要加入要求交互式网页登录的规则，除非 App 另外实现对应认证。

后续把 `.p8` 私钥放在服务器配置目录下的 `secrets/AuthKey.p8`（或使用绝对路径），限制文件权限为 0600，然后填入 Apple Key ID、Team ID 和 App Bundle ID，设置 `enabled=true`，重启服务。私钥只放在 bridge 服务器，不能打包到 App；仓库忽略 `*.p8`。启用时配置检查会验证 P-256 私钥可用于 ES256 签名，真实 Apple 授权关系仍须通过 APNs 联调验证。

`sandbox=true` 使用 `api.sandbox.push.apple.com`；`false` 使用 `api.push.apple.com`。App 的签名环境与 APNs token 必须匹配服务器环境（例如开发签名与 TestFlight 的环境不同），密钥也必须支持所选环境和 topic。环境或 Bundle ID 变更需重启，旧注册被停用，App 必须重新注册。

配置支持环境变量覆盖：`QDC507_PUBLIC_BASE_URL`、`QDC507_TELEGRAM_SMS_FORWARDING_ENABLED`、`QDC507_APNS_ENABLED`、`QDC507_APNS_SANDBOX`、`QDC507_APNS_KEY_PATH`、`QDC507_APNS_KEY_ID`、`QDC507_APNS_TEAM_ID`、`QDC507_APNS_BUNDLE_ID`。

代码默认保留 Telegram 短信转发，部署配置明确关闭它。APNs 关闭期间短信仍保存，不产生 APNs 新任务，不在随后启用时补推历史短信。

## App 注册流程

1. 用户填写 endpoint（预计 `https://djihub.fubuki.app`）和 bridge 的 Bearer API token。沿用现有单一 API token，不创建新的登录或用户体系。
2. App 生成 UUID 作为 `installation_id` 并持久化；请求通知权限，调用 iOS 的 APNs 注册能力。
3. 在获取 device token 的回调中，将二进制 token 转为无空格十六进制字符串，调用下面的 PUT 接口。不要把 bridge API token 当作 device token。
4. 每次启动获取当前 device token 并重复注册；token 变化时沿用原 installation ID 更新。切换 endpoint 前向旧 bridge 注销，再向新 bridge 注册。
5. 用户关闭推送时调用 DELETE。卸载 App 不一定能执行注销；服务器也会处理 Apple 的失效反馈。

所有接口都需要 `Authorization: Bearer <bridge-api-token>`。失败鉴权沿用 401 和基于来源地址的 429 限流。API token 授予现有单用户完整 API 权限，不是仅推送权限。设备注册不代表已获得系统通知权限，也不代表手机已收到通知。

### 注册或更新

```http
PUT /api/v1/push/devices/56862d57-3a92-44c4-ab23-a864bc4b06cf
Authorization: Bearer <bridge-api-token>
Content-Type: application/json

{"device_token":"<hex-device-token>"}
```

```json
{
  "installation_id": "56862d57-3a92-44c4-ab23-a864bc4b06cf",
  "environment": "sandbox",
  "enabled": false
}
```

成功始终返回 200。UUID 无效、token 非十六进制、长度不是偶数或超出 2–512 字符返回 422。token 统一小写，不固定要求 64 字符。重复注册是幂等的；同一环境和 Bundle ID 下，同一个 token 仅保留一个注册目标。`enabled=false` 时仍可注册，但不会推送；之后补齐 Bundle ID 需要重新注册。

同一 PUT 也接受 `voip_token`，普通通知和 PushKit token 独立更新。至少提供一个字段；省略字段表示保持原值，传 `null` 注销该通道。DELETE 设备会同时注销两个通道。

```sh
curl --fail-with-body -X PUT \
  -H "Authorization: Bearer $BRIDGE_API_TOKEN" \
  -H 'Content-Type: application/json' \
  --data "{\"device_token\":\"$APNS_DEVICE_TOKEN\"}" \
  "https://djihub.fubuki.app/api/v1/push/devices/$INSTALLATION_ID"
```

### 注销

```sh
curl --fail-with-body -X DELETE \
  -H "Authorization: Bearer $BRIDGE_API_TOKEN" \
  "https://djihub.fubuki.app/api/v1/push/devices/$INSTALLATION_ID"
```

返回 204，无响应正文；重复删除仍为 204。删除同时取消待发送任务，但无法撤回已提交 Apple 或正在发送的请求。

### 获取短信

Bridge 仅保留最新 **100 条短信**（收件和发件合计），按短信 UTC 时间倒序，同一时间优先保留后入库的记录。启动和每次保存短信时都会清理超出部分，包括正文、原始 PDU 和关联的未 ACK 推送任务。PDU 去重摘要保留，避免旧短信再次入库。长期历史由 App 保存；App 应及时同步，不能依赖 bridge 作为归档。被清理的短信详情返回 404，对应任务的迟到 ACK 仍返回 204。保留上限优先于推送的 24 小时重试期限。

保留 `GET /api/v1/sms?limit=50` 列表接口。通知点击后可按 ID 获取完整内容：

```sh
curl --fail-with-body -H "Authorization: Bearer $BRIDGE_API_TOKEN" \
  "https://djihub.fubuki.app/api/v1/sms/$SMS_ID"
```

成功返回 200：

```json
{
  "id": "sms-<sha256>",
  "sender": "+123456789",
  "body": "完整短信正文",
  "timestamp": "2026-09-05T12:00:00+00:00",
  "is_read": 0
}
```

不存在返回 404；读取不会自动标记已读，也不返回原始 PDU。

## 客户端 ACK（必需）

APNs 返回 200 后，bridge 继续保留任务，直到任意客户端 ACK。App 或 Notification Service Extension 应先可靠保存短信（正文截断时可先获取完整短信），然后使用 payload 中的 `sms_id` 全局确认：

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $BRIDGE_API_TOKEN" \
  "https://djihub.fubuki.app/api/v1/sms/$SMS_ID/ack"
```

不需要请求正文。成功返回 204；重复确认、未知短信、已过期或已删除任务同样返回 204。缺少或错误 Bearer token 按现有规则返回 401/429。任意客户端 ACK 后，删除该短信在所有设备上的待发送和重试任务；不影响其他短信，也不将短信标为已读。系统沿用单一管理员 Bearer token，ACK 不需要 installation ID 或 notification ID。

`sms_id` 用于全局 ACK 和本地内容去重。收到重复通知时仍需再次 ACK（之前的 ACK 可能没有到达服务器）。ACK 请求失败时客户端应保存待确认记录，并在后续获得网络执行机会时重试。重发和 ACK 并发时，已经在途或交给 Apple 的通知仍可能到达。


**兼容性变化：旧 App 若不发送 ACK，将重复收到通知直到任务过期。** 本次升级只改变仍在队列中的任务和新任务，之前已按 APNs 200 删除的任务不会补建。

## APNs 通知格式

```json
{
  "aps": {
    "alert": {"title": "+123456789", "body": "您的验证码是 123456"},
    "sound": "default",
    "mutable-content": 1
  },
  "type": "sms.received",
  "sms_id": "sms-<sha256>",
  "notification_id": "877d6f83-bf98-43e9-9ed1-9fb8a20a907d",
  "installation_id": "56862d57-3a92-44c4-ab23-a864bc4b06cf",
  "timestamp": "2026-09-05T12:00:00+00:00",
  "body_truncated": false
}
```

`aps.mutable-content=1` 配合 alert 通知触发 App 的 `UNNotificationServiceExtension`；iOS 工程需要包含该扩展，它不是通用的后台唤醒开关。

短信 `timestamp` 统一使用 UTC ISO 8601（`+00:00`，与 `Z` 等价），按短信中心 PDU 的当地时间与偏移换算，App 可在展示时转换到设备时区。升级时从原始 PDU 一次性修正旧记录，不重新推送；无有效原始时间的记录保留已有接收时间。

按实际 UTF-8 JSON 字节数限制在 4096 字节内；超长正文以省略号截断并设置 `body_truncated=true`。完整正文保留在 bridge。App 应以 `sms_id` 识别同一短信，并在用户打开 App 时通过 API 同步短信；APNs 不是可靠的完整短信存储。

状态中的 `queued` 包括等待客户端 ACK 和等待再次发送的任务。

后台通过持久化队列发送，不阻塞短信接收。只有完整拼接、去重后的入站短信会创建任务；发送短信不会触发入站通知。每个任务使用稳定 `apns-id`，同一短信使用稳定 `apns-collapse-id`。网络超时后重试仍可能出现重复，APNs 的 200 只表示 Apple 接受了请求。

APNs 200 后等待 ACK：初始等待约 60 秒，后续按发送尝试次数指数增长（连续成功提交但无 ACK 时为 60、120、240 秒……），最高约 1 小时，额外增加 0–1 秒抖动。收到 ACK 才完成任务。网络错误、429、5xx 仍从约 2 秒起指数退避，并尊重 `Retry-After`；两种情况共用尝试次数。任务从创建起 24 小时后过期。重启恢复未完成任务。410 按失效时间和注册版本处理，旧请求不会撤销新的 token 注册。`BadDeviceToken` / `DeviceTokenNotForTopic` 停用对应注册；凭据或 topic 配置错误暂停 worker，修复配置并重启后恢复。其他不可重试请求错误结束该任务并记录脱敏错误。

## 状态与验收

`GET /api/v1/status` 新增 `public_base_url` 和：

```json
{
  "apns": {
    "enabled": false,
    "environment": "sandbox",
    "paused": false,
    "last_error": null,
    "active_devices": 0,
    "queued": 0
  }
}
```

日志只记录已知错误类型或 HTTP 状态，不记录私钥、JWT、完整 device token 或推送正文。设备 token 必须可读地保存在权限受限的 SQLite 中才能向 Apple 发送请求。

本次部署验收：服务正常、modem 连接、localhost API 可用、注册和删除持久化正常。公网验收等待 cloudflared 配置；真机验收等待 Apple 标识和密钥配置，再由 iOS 注册真实 token，并接收一条实际入站短信。不要用测试短信写入生产短信库来代替真实接收验收。

Apple 参考：[Token authentication](https://developer.apple.com/documentation/usernotifications/establishing-a-token-based-connection-to-apns)、[APNs 请求](https://developer.apple.com/documentation/usernotifications/sending-notification-requests-to-apns)、[错误响应](https://developer.apple.com/documentation/usernotifications/handling-notification-responses-from-apns)。
