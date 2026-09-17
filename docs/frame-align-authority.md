# 服务端媒体观测与权威 frame_align

适配 TwilightCupFrontend `627c34a` 及 `8304f73` 的纯 follower 模型。
首个 T 和后续 T 均由后端实际启动的任务产生，不需要独立部署 publisher，
也不需要任何浏览器发送 `frame_align`、`presentedRt` 或先播放视频。

## 如何启动

导播通过已有 `director_command / config_update` 保存双路配置：

```json
{
  "type": "director_command",
  "action": "config_update",
  "payload": {"config": {
    "hlsA": "https://bsrserver.org.cn:1936/test/index.m3u8",
    "hlsB": "https://bsrserver.org.cn:1936/test2/index.m3u8"
  }}
}
```

不新增前端接口。首次升级时，旧配置若仅存在浏览器 localStorage，需在现有
导播设置中保存一次，让后端获得 URL。此后配置写入 MongoDB 的
`director_authorities` 集合，后续导播 auth 会自动恢复，重启后也无需重发配置。
只有同账号同比赛的 DIRECTOR 连接可启动/更新该范围；Stage 同样使用此座位。

连接管理器按 `(account_id, match_id)` 启动一个 `MediaAuthority`，包含两条
`HlsObserver.run()` 和一条 400ms 时钟任务。多个页面共享这一任务组。
未配置双路、覆盖不足、媒体拒绝访问时保持 waiting/frozen，不用服务器墙钟造 T。

## 观测与时钟

- `media_hls.py`：轮询 master/media m3u8，串行读取最近约 60 秒的完整分片，
  增量读取后续分片。解析 fMP4 的视频 track、avcC 参数集、moof/traf/trun/mdat
  样本边界，以及 H.264 SEI UUID `7e57c2ee0dd24b539b3593edf97a12c1`。
- `media_authority.py::Coverage`：区分 latest_us 与 continuous_to_us；连续序列
  必须从实际 IDR 开始，存在初始化参数集、逐样本 SEI、连续 seq 和有界时间变化。
  缺 SEI、样本序列跳跃、HLS GAP/漏段/不连续/源重启会打断覆盖，再等新 IDR。
  SEI 表示编码元数据，**不代表浏览器已解码或已呈现**。
- `MediaClock.tick()`：用 `time.monotonic()` 推进。安全上限始终为
  `min(A.continuousTo, B.continuousTo) - 30秒`，且 T 位于公共连续覆盖内。
  首次通常选择前沿后 33 秒，为分片更新预留 2 秒、前端外推预留 1 秒。
  正常以 rate=1 推进，运行锚点继续预留完整 1 秒外推余量。
- 覆盖失效、超过 5 秒没有新样本、追到安全边界时冻结；恢复时共同重新锚定到
  安全覆盖，T 不回退。若重启流的时间落后于旧 T，旧 T 仅作为 frozen 下限保留，
  在新覆盖追上前绝不声明可播放。比赛暂停时 paused=true，恢复后共同对齐。
- `ConnectionManager._publish_media()`：每约 400ms 产生新 seq、T、effective 时间，
  同范围所有 Director/Stage 都收到同一完整锚点，没有页面 publisher 排除项。
  首次先发 align_authority，再发 frame_align。auth 后立即通过 state_sync
  补当前完整锚点，不等下次媒体事件；初始化尚未完成时回放完整 frozen/waiting 态。
- 改 hlsA/B 会取消旧采集、关闭连接池、增加 epoch，再启动新任务。
  视觉 `switch_scene` 不会创建另一条时间线；锚点 scene 固定为 shared-playback。
  全部同范围页面离开、比赛结束或应用关闭时取消采集，释放 HTTP 资源。
  重连使用持久配置重启任务；其它账号/比赛不受影响。

没有像素解码或长时间视频缓存。内存仅保留当前有界分片和小量覆盖元数据。
目前支持明文（未加密）H.264 AVC fMP4 HLS，含 master playlist；只读取完整分片，
不重复读取 LL-HLS parts。不支持的 HEVC、MPEG-TS、加密及 byte-range 媒体会明确
waiting/frozen 并附诊断 reason，不能将“扫描到 UUID”冒充连续可解码覆盖。

## 字段契约

```json
{
  "src": "server:1789649000001",
  "source_id": "server:1789649000001",
  "scene": "shared-playback",
  "account_id": "account-id",
  "match_id": "match-id",
  "authority_epoch": 1789649000001,
  "epoch": 1789649000001,
  "seq": 1284,
  "t_us": 1789648967000000,
  "rate": 1.0,
  "paused": false,
  "frozen": false,
  "stale": false,
  "state": "playing",
  "reason": "playing",
  "ready_a": false,
  "ready_b": false,
  "effective_at_ms": 1789649000000,
  "server_time_ms": 1789649000000,
  "server_now_ms": 1789649000000,
  "coverage": [
    {"from_us": 1789648940000000, "continuous_to_us": 1789649000000000,
     "latest_us": 1789649000000000},
    {"from_us": 1789648940000000, "continuous_to_us": 1789649000000000,
     "latest_us": 1789649000000000}
  ]
}
```

- t_us 是 SEI 的 epoch 微秒，服务器时钟字段为 epoch 毫秒。
- authority_epoch/epoch、server_time_ms/server_now_ms 是兼容别名；现有前端
  实际消费 epoch 与 server_now_ms，用服务器时间差计算回放年龄。
- seq 同 epoch 内单调增加；epoch 更大时可重新从 0 开始（0 为启动快照）。
  同 epoch 内 scene/source_id 固定。冻结时 rate=0，正常 rate=1，不做 2x。
- ready_a/b 保留兼容字段，但后端没有浏览器呈现证据，恒 false；前端自身的
  解码/呈现门控决定可上屏状态。coverage 只表示服务端观测到的编码覆盖。
  当前 DirectorView 的旧就绪徽标优先读取 ready_a/b，可能持续显示“攒缓冲中”；
  如要准确显示该徽标，前端应使用本页 alignEngine.presented/sync 状态。
  这是显示逻辑调整，不需要新增后端接口，也不阻止接收 T 或解码呈现。
- 无有效 T 时 t_us=0、state=waiting、frozen=true。旧 ExternalClock 会拒绝 0，
  保持尚未附着的 waiting 状态；已有 T 则以 frozen 锚点停止前进。
- state_sync.frame_align 保留 effective_at_ms/T/seq，刷新 server_now_ms，
  同时保留外层 align_authority_src。正常周期消息会实际更新 T 和 effective 时间。
- 已配置媒体的 scope 完全拒绝浏览器 frame_align，不允许重新竞选。
  为兼容旧前端，仅从未配置媒体的 scope 保留原连接绑定、5 秒 lease 的旧发布协议。
  新版完整链路不依赖该兼容入口。

## 持久状态与部署

MongoDB 每个 scope 记录 config、epoch 和最大已发布 t_us。分配新任期前先使
持久 epoch 至少大于旧内存/墙钟毫秒下限，再原子递增；重启甚至墙钟回拨也不会
退回 epoch=1。每条正常锚点发送前持久化 T 下限，写入失败则只广播冻结状态。
已有页不会因后端进程重启永久拒收新 epoch，T 也不会重置。
不能删除/回滚这个集合后声称仍保留跨重启 fencing；数据库恢复策略须保留该状态。

当前仍限定 **单 worker、单实例**。虽然 Mongo 分配递增 epoch，采集任务、租约和
广播没有跨实例协调，不能把这一实现称为分布式时钟。多实例仍需共享 lease/CAS、
唯一任务所有者、fencing 和 pub/sub；只增加 worker 数或粘性会话不够。

部署需要：

1. 保持 uvicorn 一个 worker；持久 MongoDB 可读写 director_authorities。
2. `AUTHORITY_HLS_ORIGINS` 为精确 HTTPS origin 白名单，默认仅测试媒体的
   `https://bsrserver.org.cn:1936`。主列表、变体、init、分片全部校验。
3. 若媒体需要鉴权，通过部署 secret 配置 `AUTHORITY_HLS_BEARER`；不内置密钥，
   不读取前端源码作为生产凭据。匿名媒体服务可留空。
4. 网络允许访问媒体白名单；DNS 每次建连解析后检查所有地址必须为公网地址，
   实际连接固定到已检查 IP，TLS 验证原 hostname，阻止 DNS rebinding。
   禁止 HTTP、userinfo、非白名单 origin、重定向、代理环境变量和本机/内网地址。
5. `AUTHORITY_MAX_SCOPES` 默认 8，每 scope 两路串行观测。每次请求总超时 12 秒，
   playlist/init 上限 1MiB、完整分片 16MiB，并限制 box/sample 数量。
   日志及 reason 只包含静态错误码，不含 URL 查询凭据或请求头。

本次不包含部署操作；无需另行部署 publisher，也无需修改前端或 SEIInjector。

## 验证

确定性测试在 `test_media_authority.py`、`test_media_hls.py`：无浏览器 T 的三页
启动、持续更新、晚加入、GAP/停流/重启恢复、跨重启 epoch/T 下限、scope 隔离、
任务取消与池关闭、fMP4/SEI 边界、URL/DNS/重定向/体积限制。

真实流回归为可选 `tests/test_media_live.py`。从 secret 环境加载
AUTHORITY_HLS_BEARER，设置 AUTHORITY_LIVE_TEST=1 后运行：

```sh
.venv/bin/pytest -q -s --tb=short tests/test_media_live.py
```

2026-09-17 本地验证两条 test/test2 流：独立采集各 30 分片、3600/3600 样本命中
SEI、各 30 个 IDR、约 60 秒连续覆盖。完整 WebSocket 联调在无页面 publisher
情况下通过：三页锚点相同，12 条 playing 锚点推进约 4.43 秒，第四页立即回放。
该轮包含等待覆盖重建，耗时约 39.7 秒。这证明本地真实媒体采集与后端广播链路，
不代表已部署到线上，也不代表已验证浏览器解码呈现。
