# 主控制台手动调整 T：时间轴重置协议

本协议只处理一次“应用延迟”操作，不按现实时间持续覆盖播放 T。后端不拉流、不
解码，也不根据目标数值承诺媒体可用。正常 T 仍来自主控制台实际共同呈现的画面。
连接用途、租约和换主沿用 [frame-align-authority.md](frame-align-authority.md)。

## 点击、权限和版本

前端独立显示延迟输入区域，默认 30 秒；只有当前有效主 console 可操作。调试信息
显示开关不控制该区域。点击时一次计算并规范化安全整数微秒：
`Math.round((client_now_ms - delta_seconds * 1000) * 1000)`。
前端应校验有限数值，建议延迟范围 0..86400 秒；后端只接受 target_t_us，不接受
client_now_ms/delta_seconds，也不以请求到达时的服务器时间重新计算目标。

target_t_us 必须为正的 JS safe integer；服务端接受范围为接收时服务器墙钟的
过去 24 小时到未来 5 秒（后者用于小幅时钟偏差）。超出返回 TARGET_OUT_OF_RANGE。
这不是媒体安全范围；前端必须检查实际缓存范围并完成解码/呈现。不能呈现则失败，
不能为了满足协议而伪造 progress_t_us 或 presented_t_us。

只有原认证确定的同账号、同比赛、当前有效主 console 连接可以发起，且租约新鲜、
capability=true、未放弃、已完成接任确认。stage、备用 console、断线旧主、过期
租约均不能修改播放状态。身份字段必须与连接一致；新的请求必须基于当前 epoch
与 timeline_version。重置不是换主，合法操作不会让备用 console 抢主。

新增 `timeline_version`（非负整数），初始 0，仅接受新的手动重置时递增。
普通换主、scene/source_id 变化只改变 epoch，绝不改变 timeline_version 或解除
T 下限。普通 frame_align 一直禁止 T 倒退，只有重置事务能建立更低的新下限。

所有 frame_align、frame_align_status 在版本大于 0 后必须带正确 timeline_version。
FrameAlignStatus 增加可选整数字段，缺失解释为 0；因此旧消息只在版本 0 兼容。
request/ack 必须明确带版本。输出 auth_ok、align_authority、frame_align、
state_sync、冻结保活和 reset_result 都带版本（锚点还携带 reset 记录）。
旧 epoch、旧版本、缺失版本的迟到帧/状态/确认不能进入新时间轴。

## 1. 请求与接受结果

```json
{
  "type": "director_command",
  "action": "frame_align_reset",
  "payload": {
    "request_id": "reset_01",
    "connection_id": "console-connection-id",
    "account_id": "account-id",
    "match_id": "match-id",
    "authority_epoch": 1789900030000001,
    "timeline_version": 0,
    "target_t_us": 1789900000000000
  }
}
```

request_id 为 1..64 个 ASCII 字母、数字、下划线或连字符，建议 UUID。
所有字段必填；额外字段、布尔冒充整数、浮点、字符串数字等严格拒绝。

接受请求时，在 scope 锁内原子完成：timeline_version 和 epoch 增加；目标成为新
T 下限；锚点序号和输入帧序号重置；所有旧就绪标记清零；清除本范围旧候选资格、
租约播放进度及接任依据。保留当前主连接和它的租约接收时间，主必须继续正常上报
以续租。其状态输入 seq 仍按连接连续递增，不要因重置归零；帧输入 seq 可从 1 开始。
备用 console 需重新上报新版本的真实状态，连续满足 2 秒就绪条件后重新成为候选。

服务端先向每个页面发 align_authority，再向包括发起者在内的全部同域页面广播
frame_align_reset_result，随后完整 frozen 锚点。align_authority 的 src 仍为主连接，
携带新版本、新 epoch、t_floor_us=目标、reset=下面的事务记录。旧帧不能穿插污染
新锚点。接受结果如下：

```json
{
  "type": "director_cmd",
  "action": "frame_align_reset_result",
  "payload": {
    "request_id": "reset_01",
    "status": "preparing",
    "code": "PREPARING",
    "timeline_version": 1,
    "authority_epoch": 1789900030000002,
    "epoch": 1789900030000002,
    "owner_id": "console-connection-id",
    "src": "console-connection-id",
    "account_id": "account-id",
    "match_id": "match-id",
    "target_t_us": 1789900000000000,
    "server_time_ms": 1789900030000,
    "prepare_timeout_ms": 10000
  }
}
```

**此时只能显示“准备中”，不能显示“应用成功”。** 前端见更高 timeline_version 的
权威通知/快照后，才解除旧 ExternalClock lastInput/lastOutput、authorityFloor、
已提交 T、解码/恢复缓存等旧时间下限，建立新版本。仅 epoch 改变不允许清除下限。
主与舞台先遮住旧画面并重建解码，不能把冻结目标或旧 ready 当作新画面已呈现。

## 2. 解码状态与呈现确认

主准备期间每秒继续状态上报，保留现有字段并加 timeline_version=1。不可用时用
media_wait、media_ready=false、decode_ready=false、真实 progress_t_us；不能把
目标 T 当成实际进度。租约 5 秒失效仍会撤主。后端约每 750ms 保活冻结 T，不解除
ready、不推进 T，也不会因为媒体等待而换主。

完成首个共同帧呈现后，先发严格状态：

```json
{
  "type": "director_command",
  "action": "frame_align_status",
  "payload": {
    "connection_id": "console-connection-id",
    "account_id": "account-id",
    "match_id": "match-id",
    "authority_epoch": 1789900030000002,
    "timeline_version": 1,
    "seq": 22,
    "capability": true,
    "visibility": "visible",
    "progress_t_us": 1789900000000000,
    "media_ready": true,
    "decode_ready": true,
    "state": "running",
    "active_sides": ["A", "B"],
    "waiting_sides": []
  }
}
```

随后同一 WebSocket 发确认。必须至少有一路 active，媒体/解码可用，状态版本与
任期均为当前。确认 T 必须真实、不得低于 target_t_us，允许在目标后最多 1 秒内
选择实际共同帧（目标未必恰好落在帧时间点）；租约 progress_t_us 不得小于确认 T。

```json
{
  "type": "director_command",
  "action": "frame_align_reset_ack",
  "payload": {
    "request_id": "reset_01",
    "authority_epoch": 1789900030000002,
    "timeline_version": 1,
    "outcome": "presented",
    "presented_t_us": 1789900000000000
  }
}
```

服务端将实际确认 T 作为下限，输出 seq 递增，按 active_sides 设置 ready_a/b，
frozen=false，再向所有页面广播结果与锚点。前端收到对应版本、request_id 的
completed/OK 才显示“应用成功”；各舞台仍须本页实际解码/呈现就绪后才解除遮罩。
正常 frame_align 随后带当前 timeline_version、epoch、递增帧输入 seq 继续推进。

```json
{
  "type": "director_cmd",
  "action": "frame_align_reset_result",
  "payload": {
    "request_id": "reset_01",
    "status": "completed",
    "code": "OK",
    "timeline_version": 1,
    "authority_epoch": 1789900030000002,
    "epoch": 1789900030000002,
    "owner_id": "console-connection-id",
    "src": "console-connection-id",
    "account_id": "account-id",
    "match_id": "match-id",
    "target_t_us": 1789900000000000,
    "presented_t_us": 1789900000000000,
    "server_time_ms": 1789900030500,
    "prepare_timeout_ms": 10000
  }
}
```

服务端不解码，无法独立证明呈现；这个确认是已授权主端对实际共同呈现的声明。
前端必须负责媒体安全边界，不能用墙钟推进或填假进度通过验证。

## 3. 失败、超时与幂等

准备期限从接受开始计 10 秒，使用服务器 monotonic 时钟；续租和重复请求不延长。
未确认而过期进入 failed/PREPARE_TIMEOUT。主断开、撤权或租约过期进入
failed/OWNER_LOST，再走普通合格 console 接管，保留新时间轴及目标下限。
主可提前报告失败：

```json
{
  "type": "director_command",
  "action": "frame_align_reset_ack",
  "payload": {
    "request_id": "reset_01",
    "authority_epoch": 1789900030000002,
    "timeline_version": 1,
    "outcome": "failed",
    "reason": "media_unavailable"
  }
}
```

reason 只能是 media_unavailable 或 prepare_failed。失败广播结构与完成结果相同，
status=failed、code=MEDIA_UNAVAILABLE/PREPARE_FAILED/PREPARE_TIMEOUT/OWNER_LOST、
presented_t_us=null。失败不会回滚到旧时间轴，也不能用迟到确认复活事务。
原主需用新 request_id 重试；失败期间其普通 frame_align 仍拒绝。若已换主，
新主按新时间轴的 T 下限完成接任确认后，可通过普通实际呈现帧恢复播出。
UI 应结束“准备中”并显示失败，不能无限显示进行中。

重试仍须是当前存活主连接；同连接、同 request_id、完全相同请求：
返回已接受事务的最新结果，不再次重置；
同 ID 不同内容返回 REQUEST_ID_CONFLICT。准备中其他 ID 返回 RESET_BUSY。
未接受的请求不占 ID，可修正版本等条件后重试。每 scope 保留最近 128 个已接受
请求；更老请求即使缓存淘汰，原版本已过期也无法再次应用。断线后的新 connection_id
不重放旧操作，应先读取快照。重复 ack 在同一任期/版本返回终态，不能重复提交 T。

拒绝结果为下列结构，不修改当前播放；status=rejected 不代表已接受事务失败：

```json
{
  "type": "director_cmd",
  "action": "frame_align_reset_result",
  "payload": {
    "request_id": "reset_02",
    "status": "rejected",
    "code": "STALE_VERSION",
    "timeline_version": 1,
    "authority_epoch": 1789900030000002,
    "server_time_ms": 1789900030500
  }
}
```

| code | 含义 |
| --- | --- |
| NOT_AUTHORITY | stage、备用、已移除或非当前主连接 |
| LEASE_INVALID | 主租约失效、能力撤回或未完成接任 |
| INVALID_REQUEST | 严格类型/字段/格式不符或确认字段组合不合法 |
| SCOPE_MISMATCH | 请求身份与认证连接不符 |
| STALE_VERSION | epoch 或 timeline_version 不符 |
| TARGET_OUT_OF_RANGE | 超出过去 24 小时/未来 5 秒 |
| REQUEST_ID_CONFLICT | 已接受 ID 被用于不同内容 |
| RESET_BUSY | 另一重置正在准备 |
| NO_TRANSACTION | 确认没有对应当前事务 |
| NOT_PRESENTED | 未报告新版本就绪、实际进度不足或确认不在目标后 1 秒内 |

普通 WS 鉴权、座位/终场只读限制仍可能返回原协议 auth_error/error，前端也需处理。

## 4. 新加入、重连与快照

state_sync 外层有 timeline_version 和 reset；frame_align 内同样含版本与事务。
reset 保存最近一次事务（没有则 null），其 epoch/src 是该事务原发起任期/连接，
不是新的选主指令；**当前来源以 auth_ok/align_authority/锚点 src 为准**。后续换主
可能保留历史 failed/completed 记录，不能因此把旧主重新选回来。保活会重复结果，
前端按版本/request_id 幂等消费，不反复清缓存或弹成功提示。

下面是准备中的完整 state_sync 示例。config/soon 为既有配置/倒计时数据；准备
目标 t_us 只是冻结目标，ready=false，不代表媒体已呈现：

```json
{
  "type": "director_cmd",
  "action": "state_sync",
  "payload": {
    "scene": "match",
    "soon": {"target_ms": null, "started_at": null, "paused_at": null, "now_ms": 1789900030100},
    "config": {},
    "align_lease_required": true,
    "align_role": "follower",
    "connection_id": "new-stage-connection-id",
    "align_authority_src": "console-connection-id",
    "timeline_version": 1,
    "reset": {
      "request_id": "reset_01", "status": "preparing", "code": "PREPARING",
      "timeline_version": 1, "authority_epoch": 1789900030000002, "epoch": 1789900030000002,
      "owner_id": "console-connection-id", "src": "console-connection-id",
      "account_id": "account-id", "match_id": "match-id", "target_t_us": 1789900000000000,
      "server_time_ms": 1789900030000, "prepare_timeout_ms": 10000
    },
    "frame_align": {
      "src": "console-connection-id", "source_id": "console-connection-id",
      "account_id": "account-id", "match_id": "match-id", "scene": "match",
      "timeline_version": 1, "authority_epoch": 1789900030000002, "epoch": 1789900030000002,
      "seq": 0, "t_us": 1789900000000000, "rate": 1.0, "paused": false,
      "frozen": true, "stale": false, "ready_a": false, "ready_b": false,
      "active_sides": [], "waiting_sides": ["A", "B"], "reason": "reset_preparing",
      "effective_at_ms": 1789900030000, "server_time_ms": 1789900030100, "server_now_ms": 1789900030100,
      "reset": {
        "request_id": "reset_01", "status": "preparing", "code": "PREPARING",
        "timeline_version": 1, "authority_epoch": 1789900030000002, "epoch": 1789900030000002,
        "owner_id": "console-connection-id", "src": "console-connection-id",
        "account_id": "account-id", "match_id": "match-id", "target_t_us": 1789900000000000,
        "server_time_ms": 1789900030000, "prepare_timeout_ms": 10000
      }
    }
  }
}
```

实时 frame_align 广播的 payload 就是上例 frame_align 对象；准备期间更新 seq/
服务器时间但不推进 T。完成后该对象转为 completed、实际呈现 T、正确 ready 和
frozen=false。晚加入应先按 timeline_version 清理旧缓存，再遵守冻结/就绪状态。

## 兼容、部署和验证

前端尚需实现此协议，包括独立输入 UI、版本字段、缓存清理、媒体准备/呈现确认和
结果提示。本次仅后端实现；除删除临时交接提示词，不修改前端代码。所有参与页面
需升级，尤其舞台必须识别 timeline_version；后端无法强迫旧舞台解除旧单调下限。
版本 0 的原客户端继续可用，首次重置后缺失版本的帧/租约会被拒绝。

单进程/单实例限制不变，版本/事务/幂等记录都在内存；跨进程重启需重建客户端
WS 会话，不能声称持久事务或跨 worker 一致性。未来需共享存储和原子版本/事务。
测试包括前后重置、权限和范围、旧版本/序号、准备失败/超时/断开、重复请求、
多舞台结果相同、晚加入快照与新主接管；模拟测试不等于真实媒体或 OBS 实机验收。

2026-09-21 本地验证结果：

- `.venv/bin/pytest -q --tb=short`：365 passed，196.15 秒。
- `.venv/bin/pytest -q tests/test_frame_align_reset.py tests/test_console_authority.py tests/test_frame_align_lease.py tests/test_frame_align_authority.py tests/test_director_command.py --tb=short`：68 passed。
- 重置测试单独复跑：17 passed，含保留原媒体 scene/source_id 和实际 WebSocket 多舞台快照。
- 修改 Python 文件 Ruff/格式检查和 `git diff --check` 通过；全仓 Ruff 原有 7 项、
  Pyright 原有 11 项诊断未变。完整测试仅有已有依赖弃用警告。
