# iOS PushKit / CallKit 通话对接

来电使用独立的 PushKit VoIP token，经 APNs 唤起 App，由 App 向 CallKit 报告来电。接听、挂断及音频走 bridge 的 REST / SSE / WebSocket。App 通话核心位于 `calls/core.py`、`calls/controller.py`。本次不实现 iOS App。

## 配置与设备注册

```toml
[server]
enabled = true
host = "127.0.0.1"
port = 8787
public_base_url = "https://djihub.fubuki.app"

[calls]
incoming_frontend = "app"
```

`app` 将蜂窝来电交给独立客户端，同时向已注册的 VoIP 设备发送来电邀请。`auto` 选择 `app`；客户端通话需要启用 HTTP 服务；`web` 保留原浏览器音频接听流程。没有 VoIP 设备时，app 来电仍可通过事件流发现；无人接听 60 秒后结束。

沿用 `[apns]` 的 `enabled`、`sandbox`、`key_path`、`key_id`、`team_id`、`bundle_id`，不用另外配置密钥。Apple 签名密钥需支持目标环境和 `<bundle_id>.voip` topic。普通通知 token 与 PushKit token 不可互换。

**共用原设备接口：**

```http
PUT /api/v1/push/devices/{installation_id}
Authorization: Bearer <bridge-api-token>
Content-Type: application/json

{"device_token":"<普通通知十六进制token>","voip_token":"<PushKit十六进制token>"}
```

也可以只提交一个字段。规则：

- 省略字段：该通道保持原值；传 `null`：注销该通道；非空字符串：注册或更新。
- 至少提交一个 token 字段；UUID 或 token 格式无效返回 422。支持仅 VoIP 注册。
- 普通通知与 VoIP 独立保存版本、处理失效，不互相覆盖。更新 VoIP token 不取消短信任务。
- `DELETE /api/v1/push/devices/{installation_id}` 同时注销两个通道，重复删除返回 204。
- 成功 PUT 返回 200 和 `installation_id`、`environment`、`enabled`，沿用短信接口。
- `installation_id` 是同一次 App 安装持久化的 UUID。它用于选择通话参与设备，不是新的认证凭据；所有 REST 请求仍使用同一个 bridge Bearer token。

## 来电推送

服务器使用 `apns-push-type: voip`、`apns-topic: <bundle_id>.voip`、优先级 10、`apns-expiration: 0`：

```json
{
  "aps": {},
  "type": "call.incoming",
  "call_id": "877d6f83-bf98-43e9-9ed1-9fb8a20a907d",
  "caller": "+123456789",
  "timestamp": "2026-09-09T12:00:00+00:00",
  "expires_at": "2026-09-09T12:01:00+00:00"
}
```

`caller` 可能为 null，此时 App 应显示未知来电。所有时间为 UTC。`call_id` 是可直接作为 CallKit UUID 使用的稳定通话 ID。重复 modem 来电事件不会重复创建通话或重复触发已成功发送的 VoIP 邀请。

VoIP 不走短信队列，也不使用短信 ACK。APNs 接受后不主动重发；短暂网络错误、429 或 5xx 最多尝试 3 次，重试前分别等待 2、4 秒且重新检查通话和 token 是否仍有效。有 `Retry-After` 的拒绝不延迟重放来电；过期、被接听或已结束的邀请取消。无法撤回已提交或在途的推送。

410 与 token 错误只停用对应 VoIP 注册；凭据/topic 错误暂停 VoIP 邀请，修复后重启恢复。短信通道不受这个暂停状态影响。状态位于 `GET /api/v1/status` 的 `apns.voip`（`active_devices`、`paused`、脱敏 `last_error`）。

## 接听与音频时序

1. 在 PushKit 回调中及时用 `call_id` 向 CallKit 报告来电，不等待 bridge 请求完成。App 必须实现 PushKit/CallKit 对应能力及麦克风权限。
2. 建立 `GET /api/v1/events` SSE 连接，并查询 `GET /api/v1/calls/{call_id}` 校验当前状态；重连时重新查询。推送只是来电邀请，查询结果是状态依据。
3. 用户接听，App 在 `CXAnswerCallAction` 中调用下面的 `answer`，成功后 fulfill action。服务器原子分配接听归属，返回 `waiting_client`，此时还没执行蜂窝接听。
4. 在 CallKit `provider(_:didActivate:)` 激活音频后，获取音频票据并连接 WebSocket。服务器无需等待首个麦克风 PCM 帧，直接准备音频硬件、接听蜂窝侧，再返回 WebSocket `ready`。
5. 开始双向 PCM。收到 `call.state` 的 `active` 表示蜂窝侧已接通；挂断时停止音频并更新 CallKit。

```http
POST /api/v1/calls/{call_id}/answer
Authorization: Bearer <bridge-api-token>
Content-Type: application/json

{"installation_id":"<当前安装UUID>"}
```

返回 200 和完整通话记录（见下方）；`owner_installation_id` 标识获得通话的设备。其他设备接听返回 409。同一设备重复接听幂等，不延长连接期限。接听成功后有 **15 秒**建立音频连接，超时结束通话。未注册设备或缺少 installation ID 无法接听 app 来电。

```http
POST /api/v1/calls/{call_id}/audio-ticket
Authorization: Bearer <bridge-api-token>
Content-Type: application/json

{"installation_id":"<接听设备UUID>"}
```

仅归属设备可获取票据，返回：

```json
{"ticket":"<一次性票据>","expires_in":30.0,"subprotocol":"qdc507.audio.v1"}
```

连接 `wss://djihub.fubuki.app/api/v1/calls/{call_id}/audio`，WebSocket 子协议同时携带 `qdc507.audio.v1` 和 `ticket.<一次性票据>`。服务器只协商返回 `qdc507.audio.v1`；票据绑定通话和接听设备，校验和消费后不能再次使用，不能连接另一条通话。

WebSocket 首个服务端 JSON 为 `ready`，包含音频格式：PCM16 little-endian、8 kHz、单声道、20 ms/帧（320 字节）。二进制消息为原始 PCM，可包含最多 10 个完整帧；App 负责原生音频采样率与 8 kHz 间的转换。可发送 `{"type":"ping"}` 获取 `pong`。音频通过 WebSocket，不通过 APNs。

同一通话只允许一个音频连接；第二个连接被拒绝且不会结束第一个。音频连接断开会结束通话，本版不提供通话中无缝音频重连；查询状态后向 CallKit 报告结束。

## 通话状态、其他设备及挂断

`GET /api/v1/calls/current` 返回当前客户端通话或 null；`GET /api/v1/calls/{call_id}` 返回指定通话，包括已结束记录，不存在返回 404。`GET /api/v1/calls` 是历史列表。

记录和 SSE 的 `call.state` 包括：`id`、`direction`、`state`、`cellular_number`、`frontend`、`owner_installation_id`、`started_at`、`connected_at`、`ended_at`、`expires_at`、`last_error`。状态为：

- `ringing_cellular`：正在响铃。
- `waiting_client`：已认领，等待音频；呼出时表示已创建呼叫，等待音频后拨号。
- `waiting_cellular`：已拨号，等待远端接通。
- `active`：已接通。
- `ended` / `failed`：终态；`last_error` 包含远端挂断、超时、连接断开或服务器重启等原因。

其他设备发现 `owner_installation_id` 为别的安装实例时，应向 CallKit 报告 `answeredElsewhere` 并结束本机来电界面，不发送蜂窝挂断请求。远端挂断等变化通过 SSE 传递；不发送额外 VoIP 推送用于取消来电。SSE 中断后必须重新 GET 状态；bridge 重启会将残留客户端通话标为 ended，不重放旧来电。

```http
POST /api/v1/calls/{call_id}/hangup
Authorization: Bearer <bridge-api-token>
Content-Type: application/json

{"installation_id":"<当前安装UUID>"}
```

尚无人接听时，任意已注册设备可以拒接，拒接结束整个通话；已分配归属后只有对应 installation ID 可通过客户端接口挂断，其他设备返回 409。短信的全局 ACK 与这个通话归属规则相互独立。通话已经结束时挂断返回 200；对另一个活动通话使用旧 ID 返回 409。

## 呼出

在 `CXStartCallAction` 中调用：

```http
POST /api/v1/calls/start
Authorization: Bearer <bridge-api-token>
Content-Type: application/json

{"frontend":"app","number":"+123456789","installation_id":"<当前安装UUID>"}
```

返回 `waiting_client` 和新的通话 UUID，归属立即绑定发起设备。随后配合 CallKit 激活音频，获取票据并建立音频 WebSocket；服务器在音频准备好后拨号，事件进入 `waiting_cellular`，远端接听后进入 `active`。本机仅支持一条活动通话。保持/转接/DTMF 暂无 API，App 不应启用对应 CallKit 操作；静音可由 App 本地处理。

呼出时 `CXStartCallAction` 已有本地 CallKit UUID，App 需要保存它与服务器返回 `id` 的映射；后续 bridge 请求使用服务器 ID，CallKit 操作使用本地 UUID。来电则可直接使用推送中的 `call_id`，无需另建 UUID。

## 部署验证范围

自动化测试覆盖双 token 独立更新、VoIP 请求参数与取消、抢接互斥、归属票据、音频不等待首帧、历史状态及音频生命周期。实际 PushKit 唤醒、CallKit 操作顺序、音频路由及公网双向语音需要配合 iOS App 和真实来电进行验收。

参考：[Apple PushKit 来电处理](https://developer.apple.com/documentation/pushkit/responding-to-voip-notifications-from-pushkit)、[CallKit 音频激活](https://developer.apple.com/documentation/callkit/cxproviderdelegate/provider%28_%3Adidactivate%3A%29)。

## 连续性与客户端发送要求

客户端持续发送 PCM16/8 kHz 单声道音频，建议每 20 ms 发一帧；允许批量发送最多 200 ms，但长时间攒包会增加延迟。麦克风静音时也发送对应时长的零采样，避免把静音检测误当成网络断流。重采样后不足 160 个采样点的尾部必须保留到下一批，不要按每次麦克风回调独立截断。

bridge 的播放缓冲先积累 3 帧（60 ms），缓冲耗尽后重新积累并逐步增加目标，最多 6 帧（120 ms）。最多保存 20 帧（400 ms），超过上限仍丢弃最旧音频，避免延迟无限增长。ALSA 由独立线程按单调时钟的绝对 20 ms 截止时间连续供给，避免写入立即返回时过快消耗队列；欠载后重写原帧，短写只补写剩余采样。该处理不改变 WebSocket 音频协议，也无法恢复客户端未发送的音频。

`audio.state` 的停止记录包含本次统计。`client_to_cellular` 中 `underruns` 是运行中缓冲耗尽次数，`startup_silence_periods` 与 `rebuffer_silence_periods` 分别记录启动和运行中等待补充音频的 20 ms 周期。`dropped` 是溢出丢帧；`max_interarrival_ms`、`gaps_over_40ms`、`audio_received_ms` 和 `last_frame_ms` 帮助检查供给节奏。ALSA 的 `playback_recoveries` 记录欠载重试，`write_failures` 记录未恢复的写入失败。
