# 服务端权威 frame_align 协议

适配 TwilightCupFrontend `627c34a`。权威状态由 ConnectionManager 按
`(account_id, match_id)` 独立管理；Director 和 Stage 均使用 DIRECTOR seat，
其多个 WebSocket 共存，账号/比赛来自鉴权解析，不能由 payload 改写。

## 消息与单位

输入继续使用 `director_command` / `action=frame_align`；输出为
`director_cmd` / `action=frame_align`。普通导播命令不改变行为。

```json
{
  "src": "publisher-session-id",
  "source_id": "publisher-session-id",
  "account_id": "account-id",
  "match_id": "match-id",
  "scene": "match",
  "authority_epoch": 3,
  "epoch": 3,
  "seq": 1284,
  "t_us": 1234567890000,
  "rate": 1.0,
  "paused": false,
  "frozen": false,
  "stale": false,
  "ready_a": true,
  "ready_b": true,
  "effective_at_ms": 1760000000000,
  "server_time_ms": 1760000000000,
  "server_now_ms": 1760000000000
}
```

- `authority_epoch == epoch`：前端 ExternalClock 实际读取 `epoch`。
- `server_time_ms == server_now_ms`：前端实际读取 `server_now_ms`。
  这两个字段与 `effective_at_ms` 都由服务端生成，忽略客户端同名值。
- `t_us` 保持微秒，必须是 0..2^53-1 的整数，bool/字符串/浮点数均拒绝。
  0 仅兼容旧客户端的未就绪哨兵；627c34a 不接受 0 作为可播放时钟。
  已有正 T 后拒绝更小 T，包括回退到 0。
- `rate` 默认 1.0，范围 0..1.08；`paused/frozen` 默认 false，必须为 bool。
  暂停/冻结时外推速率为 0；不做 2x 或服务端追赶。
- `src` 是 publisher 会话 id；缺少时允许 `source_id` 作为兼容输入。
  `source_id` 默认等于 `src`，也可表示该 publisher 的媒体源 scope。
  `scene` 默认使用最近 `switch_scene`，无场景时为 `""`。
- 保留 `ready_a/ready_b`，缺省 false；冻结失联时均置 false。
  未知扩展字段继续保留，但上述权威字段始终由服务端覆盖。

## 生命周期与 fencing

1. 首条有效消息选择 publisher，epoch 从 1 开始；绑定实际连接对象和 src。
   其它页面都是 follower，不能复制 src 冒充 publisher。发送方继续不收自己的
   普通回声，兼容旧客户端；其它同账号同比赛 DIRECTOR 连接收到相同完整锚点。
2. 服务器分配每个 epoch 内从 1 递增的输出 seq，冻结也占一个 seq。
   可选输入 seq 是 publisher 自己的独立序列；一旦使用，就必须持续递增，
   不能省略以绕过检查。输入 epoch/authority_epoch 若存在必须等于当前 epoch，
   两者同时存在时都校验。新 scope 的已知 epoch 为 0。
3. source 静默超过 5000ms 后可接任；每 250ms 的 watchdog 在无入站消息时也会
   发布 `frozen=true, stale=true`。断线、exclusive 顶替、广播失败清理会立即冻结，
   但其它 publisher 仍须等原 lease 的 5 秒到期才能接任。
4. 接任增加 epoch、重置输出 seq；先发 `align_authority {src, epoch,
   authority_epoch}` 再发新锚点。被取代连接不能换 src 再竞选；已退休 src 不能
   再使用。重连 publisher 应使用新的会话 src。旧连接/旧 epoch 消息不污染新任期。
5. 同一 publisher 在超时后恢复，或改变 scene/source_id，也开启新 epoch。
   已接受的 T 不允许回退。冻结 T 使用旧锚点最多 1000ms 的外推值，匹配
   627c34a ExternalClock 的上限，不凭空假定整个 5 秒都有视频覆盖。
6. auth_ok 后的 `state_sync.frame_align` 回放完整当前锚点，同时保留外层
   `align_authority_src`。回放保持 t_us/effective_at_ms/epoch/seq，刷新服务器
   当前时间，使前端用两个服务器时间的差计算锚点年龄。发送前检查超时；
   已失联只补 frozen 当前态。首次没有有效 publisher 时不伪造 anchor。

同 scope 的命令、回放和超时广播串行化；异步断线通知会检查是否已被新状态取代。
旧客户端无需新增字段即可输入，但不带 epoch/seq 的旧协议本身不能区分同一连接
内所有重发；服务端仍执行连接 fencing、退休 src 检查和 T 不回退检查。

## 前端使用与尚未闭合的生产链路

627c34a 的 director store 已将扩展字段传入 ExternalClock，校验账号/比赛；
先 selectAuthority，再 accept anchor。epoch/seq 丢弃旧状态，
`server_now_ms - effective_at_ms` 修正回放年龄，paused/frozen 停止外推，
本地软追赶保持在前端。`stale` 是诊断字段，该版前端依赖 frozen 和锚点年龄，
不直接读取 stale。网络传输延迟仍不等于服务器已知的锚点年龄。

**627c34a 已删除 Stage 的 frame_align 发布逻辑，所有页面均为 follower。**
本协议保留已有 publisher 输入能力，但仓库内没有视频 RT/缓冲前沿采集服务；
全量使用该版前端、又无独立 publisher 时不会产生首个 T，页面会保持 waiting。
上线前必须接入一个能提供有效视频时间的 publisher（通过既有鉴权 DIRECTOR
WebSocket 发布，并遵守上述 lease/seq 协议）。服务端不能用当前墙钟冒充视频 T。
本次未修改前端或 SEIInjector，也未声称实现媒体采集或独立时钟生产者。

## 部署边界

状态、锁和 watchdog 属于单个 ConnectionManager / asyncio 进程。
当前 main.py、Dockerfile 的 uvicorn 启动方式默认单 worker；必须保持单进程单实例。
MongoDB 比赛存储并不共享此时钟状态，多个 worker/容器不能保证相同 authority。

进程重启会丢失 anchor 和 epoch（重新从 1 开始）。旧页面若保留 ExternalClock
的旧 epoch，需要重建前端时钟实例；后端无法仅靠内存实现跨重启 fencing。
生产多实例/跨重启方案需要 Redis 或其它共享持久存储：原子 lease/CAS 选举、
持久递增 fencing epoch、seq/anchor 原子更新、按账号+比赛的 pub/sub，并处理
订阅与快照的顺序；只做粘性会话不够。这些能力本次未实现。
