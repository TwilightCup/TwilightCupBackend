# 控制台主 T 与舞台接收协议

后端不拉流、不解析媒体、不产生或推进 T。只有导播控制台可生产 T；后端按
`(account_id, match_id)` 选择唯一发布连接、校验、加序号、缓存和转发。
舞台、独立场景页及控制台内嵌预览均只接收权威锚点。

## 握手与控制台接管

导播通过 `/ws/{token}` 连接，可用 `match` 指定比赛。服务端先校验账号和席位，再解释 `align_client`。

| align_client | 握手后的行为 |
| --- | --- |
| `console` | 当前账号在本场最新注册的控制台立即成为 publisher。旧控制台收到 `displaced`，连接以 4001 关闭。 |
| `stage` 或省略 | 返回 follower，可同时打开多个舞台。舞台通过 `align_authority_src` 确定帧来源。 |
| 空字符串或其他值 | 服务端发送 `auth_error` 并以 1008 关闭。 |

导播的 `auth_ok` 包含 `connection_id`、`align_role`、`align_authority_src`、`authority_epoch`、
`timeline_version` 和 `align_lease_required=false`。控制台连接期间持有发布权，页面可见性、
媒体就绪状态和上报间隔均不触发换主。舞台应等待控制台重连或新控制台注册。

发布者变更后，服务端通过 `director_cmd/action=align_authority` 告知各连接自己的角色、
当前发布者 `src`、新任期、时间轴版本与已有 T 下限。有历史锚点时沿用最后 T，序号归零，
标记 `frozen=true`、`stale=true`，直到新控制台发布有效锚点。主控制台断开时 `src=null`。
首次连接且尚无 T 时，舞台应等待首帧。

## 可选状态上报

console 使用现有 director_command/action=frame_align_status；严格 payload
保持前端现有字段，不新增用途字段或客户端墙钟：

```json
{
  "type": "director_command",
  "action": "frame_align_status",
  "payload": {
    "connection_id": "auth_ok.connection_id",
    "account_id": "account-id",
    "match_id": "match-id",
    "authority_epoch": 1789649000000001,
    "seq": 21,
    "capability": true,
    "visibility": "visible",
    "progress_t_us": 1789648967000000,
    "media_ready": true,
    "decode_ready": true,
    "state": "running",
    "active_sides": ["A"],
    "waiting_sides": ["B"]
  }
}
```

状态上报用于诊断及重置确认时补充 A/B 就绪信息。身份、任期和时间轴版本须匹配当前连接；
`timeline_version` 缺省为 0，首次重置后须明确传入。状态序号在连接内递增，
与锚点序号独立。active_sides 和 waiting_sides 合起来恰好覆盖 A、B，各侧只出现一次。

## 锚点与晚加入回放

console 发送 director_command/action=frame_align，必须带当前 epoch 或
authority_epoch，以及递增输入 seq。服务端补全并转发给其他同范围导播连接：

```json
{
  "src": "publisher-connection-id",
  "source_id": "publisher-connection-id",
  "account_id": "account-id",
  "match_id": "match-id",
  "scene": "match",
  "authority_epoch": 1789649000000001,
  "epoch": 1789649000000001,
  "timeline_version": 0,
  "seq": 12,
  "t_us": 1789648967000000,
  "rate": 1.0,
  "paused": false,
  "frozen": false,
  "stale": false,
  "ready_a": true,
  "ready_b": false,
  "active_sides": ["A"],
  "waiting_sides": ["B"],
  "effective_at_ms": 1789649000000,
  "server_time_ms": 1789649000000,
  "server_now_ms": 1789649000000
}
```

T 为非负 safe integer 微秒，不能低于已采纳 T。服务器时间为毫秒并覆盖客户端
值。epoch/authority_epoch、server_now_ms/server_time_ms 为兼容别名。seq 同任期
单调递增。rate 限 0..1.08，paused/frozen/ready_a/ready_b 为布尔值。
src 由服务端根据连接填写。控制台可随帧提交 active_sides 和 waiting_sides，两者须共同覆盖 A、B。
客户端负责根据真实媒体进度发送锚点和暂停状态；服务端只在收到有效帧或连接、重置状态发生变化时更新锚点。

晚加入 state_sync.frame_align 回放当前完整锚点，保留 effective_at_ms/T/seq，
刷新 server_now_ms/server_time_ms；外层带 align_role、connection_id 和
align_lease_required。前端按 epoch/seq 丢弃过期锚点，根据 src=null、stale、
锚点超时或断线遮住舞台，收到有效新主锚点且实际解码就绪后恢复。

## 运行范围

帧发布者、锚点和重置事务保存在单 worker、单实例的内存中。进程重启后客户端须重新握手，
从新会话的角色、任期和快照恢复。

## 手动调整时间轴

主控制台可以通过独立重置事务调整 T（包括向后），普通 frame_align 仍禁止倒退。
所有锚点/快照增加 timeline_version；首次重置后客户端帧和状态必须明确携带该
版本。普通换主 epoch 不允许解除 T 下限。完整请求、确认、广播、快照、错误码
及前端状态机见 [frame-align-reset.md](frame-align-reset.md)。
