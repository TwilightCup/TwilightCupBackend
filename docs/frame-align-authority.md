# 按连接顺序选举主 T

后端不拉流、不解析媒体、不产生或推进 T。前端主页面计算并发布 T，后端负责
选择唯一发布连接、校验、加序号、缓存和转发。同一 `(account_id, match_id)`
的 Director / Stage（均为 DIRECTOR 座位）共享一个主页面。

## 生命周期

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

当前纯 follower 前端需要恢复 publisher 分支，本次没有修改前端仓库：

1. auth_ok 的 align_role 决定是否启动本地 T 发布；follower 只消费后端锚点。
2. align_authority 的 role 动态启停发布，使用服务端 src/epoch 更新 ExternalClock
   的 authority。接任时从冻结锚点 T 下限继续，不允许先回退到本页旧 T。
3. follower 按 epoch/seq 丢弃过期锚点，消费毫秒服务器时间与微秒 T；晚加入先用
   state_sync。不要自行超时抢主，也不要等待后端产生首个 T。
4. 每次建立新 WebSocket/auth connection_id 时重建时钟会话，避免跨服务端重启
   残留 fencing 状态。旧消息仍可接受，但老前端必须适配服务端角色才能可靠换主。

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
