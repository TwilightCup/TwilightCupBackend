# 控制台主 T 与舞台接收协议

后端不拉流、不解析媒体、不产生或推进 T。只有导播控制台可生产 T；后端按
`(account_id, match_id)` 选择唯一发布连接、校验、加序号、缓存和转发。
舞台、独立场景页及控制台内嵌预览均只接收权威锚点。

## 握手与升级

WebSocket `/ws/{token}` 新增 query `align_client=console|stage`：

- console：仅在原认证/授权得到 `Seat.DIRECTOR` 后，才有资格竞争主 T。
- stage 或缺失参数：只能接收，绝不进入候选集合，伪报 capability=true 也无效。
- 空串或其他非法值：返回 auth_error 并关闭（1008），不注册连接。

用途固定保存在 Connection，不从状态 payload 修改。不改变 token、seat、match
权限校验，不使用 exclusive；同账号同比赛可有多个 console 和任意多个 stage。
不要以该 query 作为新的账号权限：持有合法导播凭据的客户端可以声明 console。

所有页面握手初始为 follower，没有“最早 DIRECTOR 自动当主”的兼容路径。
auth_ok 包含 connection_id、align_role、align_authority_src、authority_epoch 和
align_lease_required=true。已有健康主时，新 console/stage 加入不会改变 epoch、
source 或播出状态；stage 离开不触发选举。重连获得新 connection_id，重新上报就绪。

**部署顺序**：先准备支持用途参数和现有租约协议的前端版本，再升级单进程后端，
随后统一刷新控制台、舞台和内嵌预览，使所有 WebSocket 带正确参数重新连接。
不支持该参数的旧页面只能接收，不能用旧舞台作兜底 publisher；混用期间可能等待
信号，直到新版 console 就绪。本次没有部署线上服务，也没有修改前端代码。

## 状态上报

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

所有字段必填，不允许额外字段或隐式类型转换。seq 是连接内递增状态序号，跨
任期不重置，与 frame_align 输入 seq 和服务端输出 seq 分开计数。数值为非负
JS safe integer。身份必须匹配认证连接，authority_epoch 必须匹配当前任期；
旧 epoch/seq 不续租。stage 发来的状态可以保持原格式，但不会更新可发布租约。

capability 表示具备生产主 T 的能力，media_ready/decode_ready 对 active_sides
声明当前媒体/解码条件。progress_t_us 是实际播放进度，不能根据墙钟伪造。
active_sides 与 waiting_sides 不重复、不相交，合起来必须恰好为 A/B；支持
单路降级候选。visibility 为 visible/hidden，blur 不撤权。state 为 running、
media_wait、paused 或 relinquish。建议每秒上报，角色/可用性变化时立即上报。

## 选举、撤权与接管

仅 console 可成为候选，且必须 capability/media_ready/decode_ready 为 true、
state=running、有 active 路、连续就绪至少 2 秒、最后状态距今不超过 5 秒。
优先可见且最近 5 秒报告实际进度增加的候选，其后按连接顺序排序。hidden 但
持续执行的 console 可参选，健康主不会被后来加入或切换可见性的 console 抢占。

主连接正常关闭立即从合格 console 中接任；仍保持连接但执行停止、网络黑洞等
按 5 秒租约超时撤权。250ms watchdog 检查，迟到续租不能抢在检查前复活旧任期。
没有合格 console 时立即无主，epoch 增加，先广播 src=null 角色通知，再广播
保留最后 T 的 frozen=true/stale=true 完整快照。尚无任何 T 时只通知无主，
不编造首个 T。全员挂起时只能冻结等待，stage 永不兜底。

选中新主后先增加 epoch 撤旧发布权，再向所有同范围页面发送
`director_cmd/action=align_authority`：src、epoch/authority_epoch、接收者自身
connection_id、role、account_id、match_id、lease_required=true、
lease_timeout_ms=5000、takeover_timeout_ms=3000、t_floor_us。
随后广播新 epoch、seq=0 的 waiting_publisher 冻结锚点，保留 T、sides 和扩展字段。
该快照不代表新主已经开始播出。

新主须在 3 秒内上报当前 epoch、真实解码就绪且 progress_t_us >= t_floor_us
的状态，确认后才能发布 frame_align。同一 WebSocket 上先状态、后锚点的顺序
会被串行处理。不能解码时可用 relinquish 或 capability=false 放弃；确认超时
尝试下一候选，新报告恢复资格后仍须满足稳定期。旧主的旧 epoch/seq 永远拒绝。
服务端只检查 T 下限，不保证媒体安全上限；实际解码和可播放边界由前端严格控制。

## 锚点与媒体等待保活

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
单调递增，包括冻结/保活消息。rate 限 0..1.08；paused/frozen/ready_a/b 为布尔值。
后端不追赶、不外推 T。src 由连接决定；scene/source_id 改变会更新任期。
active_sides/waiting_sides 来自已验证状态，其余扩展字段保留。

主租约仍活跃但报告 media_wait/paused 时保留角色，不反复换主。约每 750ms
（受 250ms 调度粒度影响）广播冻结锚点：T 不变，seq 增加，服务器时间更新，
frozen=true、stale=false，reason=media_wait/paused。即使没有可接受的新
frame_align，舞台也能确认主仍在线；这不是媒体恢复或播放就绪的证据。
租约超时则转为撤主/接管状态，停止旧主保活。一直声称 running 却没有锚点时，
5 秒后冻结为 publisher_silent/stale=true，不用保活伪装正常播出。

晚加入 state_sync.frame_align 回放当前完整锚点，保留 effective_at_ms/T/seq，
刷新 server_now_ms/server_time_ms；外层带 align_role、connection_id 和
align_lease_required。前端按 epoch/seq 丢弃过期锚点，根据 src=null、stale、
锚点超时或断线遮住舞台，收到有效新主锚点且实际解码就绪后恢复。

## 限制与验证

当前只保证单 worker、单实例。连接顺序、租约、锚点和配置为进程内状态；
跨 worker 需要 Redis/共享存储的有序注册、租约/CAS、持久 epoch 和 pub/sub。
epoch 以启动微秒墙钟为种子递增，不能保证墙钟回拨后的跨重启顺序；新 WebSocket
会话需重置客户端时钟会话。租约自身使用服务端 monotonic 接收时间。

后端无法证明浏览器自报的解码完成，也不能绕过浏览器后台限制或系统休眠。
测试覆盖握手用途、伪报能力、范围隔离、多控制台/舞台、同连接顺序确认、换主
下限和 fencing、冻结保活、租约失活。确定性测试和 TestClient WebSocket 联调
不等于跨机器 OBS 实机验收，部署后仍需实际多机验证。

## 手动调整时间轴

主控制台可以通过独立重置事务调整 T（包括向后），普通 frame_align 仍禁止倒退。
所有锚点/快照增加 timeline_version；首次重置后客户端帧和状态必须明确携带该
版本。普通换主 epoch 不允许解除 T 下限。完整请求、确认、广播、快照、错误码
及前端状态机见 [frame-align-reset.md](frame-align-reset.md)。
