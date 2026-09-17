# 主 T 连接选举与后台租约

后端不拉流、不解析媒体、不产生或推进 T。前端主页面计算并发布 T，后端负责
选择唯一发布连接、校验、加序号、缓存和转发。同一 `(account_id, match_id)`
的 Director / Stage（均为 DIRECTOR 座位）共享一个主页面。

## 旧客户端生命周期

- 以服务端注册 WebSocket 的顺序为准，最先连接的页面成为 publisher；其他页面
  为 follower。不是抢先发送 frame_align 的页面获胜，也不信任客户端 src 自选主。
- auth_ok 增加 connection_id、align_role（publisher/follower）、align_authority_src、
  authority_epoch。connection_id 为服务端生成的连接标识，主页面标识作为锚点 src。
- 主连接断开或发送失败移除后，立即选择剩余连接中最早的一条，递增 epoch，
  向各页发送 director_cmd/action=align_authority。payload 包含 src、epoch、
  authority_epoch、connection_id（接收页自身）、role、account_id、match_id。
  重连得到新 connection_id，排在现有连接之后。
- 已有 T 时，换主还会广播新 epoch、seq=0、frozen=true、stale=true 的完整锚点，
  T 保持最后一次接受的值。新主随后继续发布，不能回退到更小的 T。
  尚未收到任何 T 时，只通知角色，不编造启动 T。
- 主连接超过 5 秒没有有效更新，只冻结最后的 T，不把主权交给仍连接的其他页。
  原主恢复发布即可解冻；WebSocket 真正断开才换主。
- 其他页面发布 T、旧连接迟到消息、错误账号/比赛、旧 epoch/seq 均被忽略。
  普通配置、场景和倒计时指令仍可由任意同账号导播页面发送。

## frame_align 契约

前端发送 director_command/action=frame_align。旧消息可只带 t_us 和 ready_a/b；
现代 publisher 应附当前 epoch 和自身单调递增 seq。服务端输出 director_cmd，
向同范围的其他导播连接转发完整锚点，不向发布者回送正常 T。

```json
{
  "src": "server-assigned-connection-id",
  "source_id": "server-assigned-connection-id",
  "account_id": "account-id",
  "match_id": "match-id",
  "scene": "match",
  "authority_epoch": 1789649000000001,
  "epoch": 1789649000000001,
  "seq": 12,
  "t_us": 1789648967000000,
  "rate": 1.0,
  "paused": false,
  "frozen": false,
  "stale": false,
  "ready_a": true,
  "ready_b": true,
  "effective_at_ms": 1789649000000,
  "server_time_ms": 1789649000000,
  "server_now_ms": 1789649000000
}
```

- t_us 为非负 JS safe integer 微秒；服务端时间字段为毫秒，覆盖客户端输入。
- authority_epoch/epoch、server_time_ms/server_now_ms 为兼容别名，externalClock.ts
  使用 epoch 和 server_now_ms。同 epoch 内输出 seq 递增，和 publisher 输入 seq
  分开计数，因为服务端冻结也会增加输出 seq。
- rate 限于 0..1.08，默认 1.0；paused/frozen/ready_a/b 必须为布尔值。
  后端不实现追赶。客户端必须尊重 frozen/paused；软追赶由前端处理。
- src 永远由认证连接决定。source_id 可由主页面指定，默认等于 src。
  scene/source_id 变化时更新 epoch，并先通知角色和 epoch，再广播新锚点。
  未知扩展字段保留转发。
- state_sync.frame_align 包含完整当前锚点，保留 T、seq、effective_at_ms，刷新
  server_time_ms/server_now_ms；外层附 align_authority_src、align_role、connection_id。
  没有任何已发布 T 时不发送虚构 frame_align。

## 前端需要的配合

连接顺序协议已由前端接入；新增租约状态仍需前端接入。本次未修改前端代码：

1. auth_ok 的 align_role 决定是否启动本地 T 发布；follower 只消费后端锚点。
2. align_authority 的 role 动态启停发布，使用服务端 src/epoch 更新 ExternalClock
   的 authority。接任时从冻结锚点 T 下限继续，不允许先回退到本页旧 T。
3. follower 按 epoch/seq 丢弃过期锚点，消费毫秒服务器时间与微秒 T；晚加入先用
   state_sync。不要自行超时抢主，也不要等待后端产生首个 T。
4. 每次建立新 WebSocket/auth connection_id 时重建时钟会话，避免跨服务端重启
   残留 fencing 状态。旧消息仍可接受，租约模式下必须发送当前 epoch 和 seq。

## 新版后台租约协议

前端通过现有 director_command 新增 action=frame_align_status。payload 由
FrameAlignStatus 严格校验，不允许额外字段、隐式类型转换或客户端墙钟：

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

所有字段必填。seq 是该连接状态上报的递增序号，跨 epoch 也不重置；与
frame_align 输入 seq 及服务端输出 seq 分开计数。数值为非负 JS safe integer。
身份字段必须匹配当前认证连接，authority_epoch 必须等于当前任期。格式错误返回
error/400；范围不符、旧 epoch/seq 忽略，不续租。authority_epoch 改变后，前端
用新任期重新上报。必须在实际执行状态采样时产生消息，不能重播旧采样充当活跃。

capability 表示该页面实现了生产主 T 的能力；media_ready/decode_ready 表示
active_sides 当前具备媒体/解码条件；progress_t_us 必须是最近实际播放进度，
不能用墙钟推算。降级为单路仍可成为候选。active_sides 与 waiting_sides 不重复、
不相交，合起来必须恰好是 A/B 两路。两路均等待时 active_sides=[]。
visibility 只能是 visible/hidden，不使用 blur。state 取值：

- running：执行正常，准备接任或正在播放。
- media_wait：线程仍执行，但媒体或解码不可用；冻结并保留当前主角色。
- paused：主动暂停；冻结并保留当前主角色。
- relinquish：主动放弃主角色；capability=false 也立即放弃。

建议每 1 秒上报，角色/可用性改变时立即上报。所有超时与稳定期使用服务端
接收时的 monotonic 时钟，不接受客户端时间戳，不受系统墙钟调整影响。

### 启用与候选排序

某 `(account_id, match_id)` 收到首条合法状态消息后，撤销旧的无租约主角色，
递增 epoch，广播 align_authority 并启用租约模式；本进程内该范围不退回旧模式。
未发状态的旧连接不能继续占主，也不能参与此范围的新选举。没有任何状态上报的
旧客户端范围保持上文的连接顺序模式，避免未升级前端失去 T。

候选必须声明 capability、媒体和解码可用、running、有至少一路 active，
连续具备条件至少 2 秒且最后状态接收距今不超过 5 秒。优先选可见且最近 5 秒
报告实际进度增加的页面，再按连接顺序排序。没有可见候选时，hidden 页面仍
正常参选。健康主页面不会因为另一个窗口显示/隐藏就被抢占，无 blur 撤权。

主租约超过 5 秒失效，即使 WebSocket 仍连接也重新选举。250ms watchdog 检查，
下次状态处理也会先处理已过期任期，迟到续租不能抢在 watchdog 前复活旧主。
无可用候选则撤主并冻结，不反复增加 epoch。全员挂起时只能等待；前端 Worker
唤醒、后台节流和系统休眠均不由后端绕过。

### 接管确认与 fencing

选中新主时先撤旧发布权、增加 epoch，然后发角色通知，再发完整冻结锚点。
align_authority 新增 lease_required、lease_timeout_ms=5000、
takeover_timeout_ms=3000、t_floor_us（无已发布 T 时为 null）。auth_ok 与
state_sync 增加 align_lease_required；具体身份字段沿用连接顺序协议。

新主必须在 3 秒内发送当前 epoch 的状态：capability/media_ready/decode_ready
为 true、state=running、有 active 路，且 progress_t_us >= t_floor_us。
确认之前 frame_align 被拒绝。不能解码时应发送 relinquish 或 capability=false；
超时未确认则自动尝试下一位，新报告恢复资格后须重新满足 2 秒稳定期。
服务端不会替新主推进 T 或拉取媒体填补缺帧。

租约模式下 frame_align 必须带当前 epoch/authority_epoch 和递增输入 seq；
T 不得低于历史最大已接受值。旧主恢复后旧 epoch 的状态和锚点均拒绝。
接受锚点时 active_sides/waiting_sides 来自已验证的状态上报；其余扩展字段保留。
冻结、接管及 state_sync 保留完整扩展字段和原 T，不根据经过时间生成新的 T。
新主可以通过后续合法状态更新双路/降级模式。

存活主页面报告 media_wait/paused 时保留主角色，更新冻结原因和 sides；因此
比赛双路故障不会引起不断换主。冻结 reason 包括 media_wait、paused、
publisher_silent、waiting_publisher、lease_expired、publisher_declined、
takeover_timeout；选举日志含账号、比赛、epoch、source 和触发原因。
状态声称正常却未发布锚点超过 5 秒时仍会冻结 T；状态心跳持续则不判为进程挂起。
服务端只能验证格式、接收时间、作用域和序号，不能证明浏览器实际完成了解码。

## 部署与限制

选举、连接顺序、配置缓存和锚点均为进程内状态，限定单 worker、单实例。
跨 worker 需要 Redis/共享存储的有序连接注册、租约/CAS、持久 epoch 和 pub/sub；
此版本没有实现跨进程一致性。epoch 以进程启动时的微秒墙钟为种子并递增，
不保证系统时钟回拨后的跨重启顺序；T/配置也不持久化。

已移除 HLS 采集任务、媒体解析、服务端时钟、媒体鉴权环境配置及相关依赖。
config_update 只缓存/转发配置，不访问媒体 URL。旧 director_authorities 数据库
记录不再使用，未执行删除数据库数据。未修改 SEIInjector，也未部署线上服务。

测试覆盖连接先后选举、顺序接任、重连排队、范围隔离、发送失败接任、超时冻结、
锚点校验、晚加入回放、服务器时间字段与旧协议兼容。
