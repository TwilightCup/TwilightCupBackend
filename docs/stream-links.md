# 比赛直播链接持久化

直播链接嵌入 MongoDB 比赛文档 `stream_links`，作用域仅为 `match_id`。
数据库是四个链接字段唯一权威；不持久化播放器外观、延迟、重拉流计数或 T。
本功能不拉流、不探测链接，不改变播放代理、对齐或计时。

## REST

原生路径为 `GET /me/matches/{match_id}/stream-links` 和同路径 `PUT`。
使用本页账号自己的 Bearer JWT，Vite 的 `/api` 前缀只属于前端代理。

完整响应示例：

```json
{
  "match_id": "match-id",
  "version": 1,
  "hlsA": "https://example.test/live?signature=abc",
  "hlsB": "",
  "embedA": "",
  "embedB": "123456",
  "updated_at_ms": 1790000000000,
  "updated_by": "director-id"
}
```

旧比赛缺少子对象时默认 version=0、四字段空串、更新时间和账号为 null，
无需全量回填。新比赛默认为空，不复制别场或浏览器缓存。
version 是非负 JS safe integer，用于排序；updated_at_ms 是服务器 UTC epoch 毫秒。

PUT 必须提交 `expected_version` 和全部四个字符串，拒绝额外字段。
空串表示清除；内容先去首尾空白，每项输入最多 4096 字符，拒绝控制字符。
hls 接受绝对 HTTP/HTTPS URL，不要求文件后缀；embed 还接受正整数字符串房间号。
其他协议及含 token 的本系统 Bilibili/YouTube 播放代理地址拒绝。
保留签名查询的原始大小写和转义，不重写为代理 URL，不发起网络请求。

单条 MongoDB `find_one_and_update` 同时检查版本、指定导播、比赛状态、归档标记
和 A/B 席位身份，成功才将版本加一；缺字段的 version=0 同样使用 CAS，不 upsert。
并发竞争失败返回 409，需要 GET 后由用户决定如何保存，不能自动覆盖重试。
版本匹配且内容完全相同，返回原版本及元数据，不广播。
从空 version=0 保存空内容同样幂等；已有配置主动清空保留非零的新版本。

| 身份 | GET | PUT |
| --- | --- | --- |
| 当前指定导播且含 DIRECTOR 角色 | 允许 | CREATED/RUNNING/PAUSED 且未归档允许 |
| 当前指定裁判且含 REFEREE 角色 | 允许 | 拒绝 |
| ADMIN | 审计读取 | 不因 ADMIN 自动获写权 |
| 选手、其他导播、已改派的旧导播 | 拒绝 | 拒绝 |

同账号兼任裁判和导播，仍须符合指定导播与 DIRECTOR 两个写入条件。
stage 使用指定导播账号读取 REST；REST 不接受用途参数，也没有独立 stage 角色。
指定导播凭据允许 REST PUT，stage 只读由页面职责及旧 WS 用途检查共同实现。
ENDED/归档只读；更换导播继承链接。替换选手时，在同一比赛文档原子清空该侧
hls/embed 并增加版本；另一侧保留，updated_by=null 表示系统清理。
普通比赛 replace 路径保护 stream_links，避免旧状态快照覆盖新链接。
物理删除比赛即删除链接，不存在独立 collection 的遗留记录。

错误使用 `{"detail":{"code":"...","message":"..."}}`，不回显链接或 JWT：

| HTTP | code |
| --- | --- |
| 401 | unauthorized |
| 403 | stream_links_forbidden |
| 404 | match_not_found |
| 409 | stream_links_version_conflict（通常含 current_version）或 match_read_only |
| 422 | stream_links_invalid |
| 503 | stream_links_unavailable |

服务端异常仍可能返回既有通用 500；保存失败不伪成功、不通知成功。

## WebSocket、兼容和恢复

新增顶层消息 `{"type":"stream_links_update","payload":完整响应}`。
当前指定裁判及指定导播的 console/stage 在握手后收到数据库快照，version=0 也发。
REST/旧 WS 保存及选手替换后通知上述在线连接，包含发起者；不通知玩家或无关账号。
每次发送重新查数据库归属及账号角色，不信任握手时缓存的指定账号。
发送失败不撤销已写数据，GET 或重连恢复。PUT 响应才是发起者的保存确认。

旧 `director_command/config_update` 的四字段只有当前指定导播的
`align_client=console` 连接可写，按同一持久化服务部分合并，保留未提供字段。
旧请求没有 expected_version，按数据库成功提交顺序后写覆盖，不提供编辑冲突保护。
链接保存失败返回原 WS `error`（数字 HTTP code，msg 为稳定错误标识），
不更新/转发未保存的配置。stage/未声明用途者的链接写入返回 403；
混合请求中的非链接配置仍可按原路径转发。
既有 WS 座席/终场只读守卫仍可能先返回原权限错误，不改变这些通用守卫。

导播连接还收到兼容 `director_cmd/config_update` 的规范四字段；
新接入的 `state_sync.config` 以数据库四字段覆盖旧内存，只保留原有非链接配置。
旧裁判忽略新增消息，需前端升级才具备跨机器读取。后端不会导入 localStorage。
新前端只在 version=0 时提示导播显式导入；禁止挂载时自动上传旧缓存/URL。
version>0 即使四字段为空也以服务器为准。新前端用 PUT 保存后，不再经旧 WS
重复写链接。迁移期间旧 console 仍可能无版本覆盖新版配置，需要统一升级后另行撤除入口。

通知无绝对顺序保证，前端按 match_id+version 接受较新配置，同版本幂等，
忽略旧 GET/WS 返回。播放器各自用本页 token 构造代理；第三方签名可能过期，
能保存不等于能播放。后端不负责前端 URL/缓存优先级或播放器重挂策略。

## 进程与验证边界

数据库单文档 CAS 跨进程有效；WS registry 是进程内的，实时通知仅保证单 worker。
多 worker 实时更新尚需共享 pub/sub，不能声明已经实现。重连、前台恢复必须 GET 兜底。
授权角色按请求和发送时读取；比赛归属/状态与写入 CAS 在同文档内原子检查。
账号角色在另一个文档，不声称具备跨集合事务或瞬时账号撤权的一致性保证。

专项测试覆盖持久化、权限、输入校验、比赛状态与归属竞争、旧 WS、重连和通知失败。
真实 Mongo 集成用例默认跳过，可用临时数据库执行：

```sh
STREAM_LINKS_TEST_MONGO_URI=mongodb://127.0.0.1:27017 uv run pytest tests/test_stream_links_mongo.py
```

用例只创建/删除随机 `twc_stream_test_*` 数据库，验证 version=0 和后续版本的并发 CAS。
自动化测试不等于真实三台机器、播放器或生产部署验收；本次不部署线上服务。
