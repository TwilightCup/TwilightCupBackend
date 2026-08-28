# WebSocket 协议

选手端 / 裁判端 / 导播端与服务端的实时通信协议。本文档由 `scripts/gen_ws_docs.py` 从 `src/twilightcupbackend/protocol.py` 自动生成（字段表与代码同步，描述集中维护于生成脚本）。

## 连接与鉴权

- 端点 `ws://<host>/ws/{token}`，token 为登录返回的 JWT。
- 可选 ?seat=NAME（PLAYER_A/PLAYER_B/REFEREE/DIRECTOR）指定座位身份。
- 可选 ?match=ID 连到指定比赛（裁判/导播多标签页选场）。
- 可选 ?cap=（逗号分隔的能力声明，如 `preload1`=会上报预载状态）；预载开局门控只对声明了能力的席位生效。
- 可选 ?exclusive=1 要求独占身份 key（账号+座位+比赛）：同 key 既有连接先收 `displaced` 再被 close(4001) 顶掉，新连接照常 auth_ok + 快照；被顶掉连接的在途消息一律忽略。key 含 match，故裁判不同场多标签、多角色多座位互不影响；导播 OBS 多源不带 exclusive 仍并存（裁判端/选手端用，导播各场景页不用）。
- 鉴权成功后先发 `auth_ok`，再推 `ready_state`、`phase_change`；PREP 阶段选手席补发 `pick_announced`（有待选图时），各席位补发 `preload_state` 快照；选手席另收仅其可见的 System 前缀定向提示：当前选图（有选图时）与未就绪时的 prep 提示。选手连入时向全员（含本人）广播 `seat.online` 系统消息（广播 system 消息 = Twilight 前缀，各端逐字一致）。
- 导播连接只读：除 `director_subscribe`/`heartbeat`/`director_command` 外入站一律拒绝；`director_command` 仅定向转发给同账号其他导播连接（OBS 舞台），不影响比赛状态。
- 多角色账号可开多条连接（不同 seat 各一条）；同 seat 重连替换旧连接（不带 exclusive 时为静默替换，关闭码 1000）。
- 回合中发 `reconnect_resync` 取快照后幂等补传。
- 不带 `seat` 时按比赛指派取首个匹配（选手 A/B 由此确定）。
- 编码 JSON，带 `type` 判别字段（下表 type 列即其字面量）。

## 枚举取值

**seat 座位**

| 名称 | 值 |
| --- | --- |
| `PLAYER_A` | 1 |
| `PLAYER_B` | 2 |
| `REFEREE` | 3 |
| `DIRECTOR` | 4 |

**phase 比赛阶段**

| 名称 | 值 |
| --- | --- |
| `IDLE` | 0 |
| `PREP` | 1 |
| `COUNTDOWN` | 2 |
| `IN_ROUND` | 3 |
| `ROUND_JUDGING` | 4 |
| `ROUND_END` | 5 |
| `MATCH_END` | 6 |

**player_status 选手状态**

| 名称 | 值 |
| --- | --- |
| `IN_GAME` | 1 |
| `COMPLETED` | 2 |
| `FORFEITED` | 3 |

**attempt_status 尝试状态**

| 名称 | 值 |
| --- | --- |
| `VALID` | 1 |
| `SKIPPED` | 2 |
| `UNFINISHED` | 3 |
| `INVALID` | 4 |

**verdict 回合判定**

| 名称 | 值 |
| --- | --- |
| `A_WIN` | 1 |
| `B_WIN` | 2 |
| `TIE_REMATCH` | 3 |
| `A_DISCONNECT_LOSS` | 4 |
| `B_DISCONNECT_LOSS` | 5 |

**pick_type 项目类型**

| 名称 | 值 |
| --- | --- |
| `MULTI` | 1 |
| `SINGLE` | 2 |

**scoring_method 单关计分**

| 名称 | 值 |
| --- | --- |
| `FASTEST` | 1 |
| `AVERAGE` | 2 |

**account_type 账号角色（account.roles 取值，一个账号可含多个）**

| 名称 | 值 |
| --- | --- |
| `PLAYER` | 1 |
| `REFEREE` | 2 |
| `DIRECTOR` | 3 |
| `ADMIN` | 4 |

## 客户端 → 服务端

### `ClientChat`

- type：'chat'

- 选手/裁判发送的聊天文本（以 ``!`` 开头会被当作命令解析：``!ready`` 仅选手、``!roll`` 所有人、``!timer [秒]|reset`` 与 ``!lang [id]`` 仅裁判——切换比赛系统消息语言，见 docs/locales.md）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `text` | str | 是 | — |  |

### `ClientReadyToggle`

- type：'ready_toggle'

- 预留消息：实际准备切换走 ClientChat !ready 命令（仅选手）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |

### `ClientLevelTimeUpload`

- type：'level_time_upload'

- 每关完成时上报用时（断线重连后用于幂等补传）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `level_index` | int | 是 | — |  |
| `this_level_ms` | int | 是 | — |  |
| `total_ms` | int | None | 否 | None |  |
| `invalid_reasons` | list[str] | None | 否 | None |  |

### `ClientAttemptSkip`

- type：'attempt_skip'

- 单关项目跳过某次尝试，记为 N/A。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `attempt_index` | int | 是 | — |  |

### `ClientProjectComplete`

- type：'project_complete'

- 本回合项目全部完成。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `final_total_ms` | int | None | 否 | None |  |

### `ClientForfeitSignal`

- type：'forfeit_signal'

- 弃权信号（多关退出 / 单关退出且 0 次有效成绩）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `reason` | 'multi_exit' | 'single_exit_0_valid' | 是 | — |  |

### `ClientReconnectResync`

- type：'reconnect_resync'

- 断线重连后请求本回合权威快照。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |

### `ClientPreloadReport`

- type：'preload_report'

- 选手端预载状态上报（仅 PLAYER_A/PLAYER_B，PREP 阶段有意义；场景级预载仅 MULTI 合集，SINGLE 报 ``na``）。``failed`` 不阻塞开局（round_start 时选手端回退标准加载），仅触发 kind=preload 告警；旧版客户端连接不带 ``cap=preload1`` 不上报，门控豁免。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `status` | 'in_progress' | 'done' | 'failed' | 'na' | 是 | — |  |
| `detail` | str | None | 否 | None |  |

### `ClientSubsegmentSample`

- type：'subsegment_sample'

- 选手端分段采样上报（仅 MULTI 回合、PLAYER 席位，每秒一次）：每关「角色从装死苏醒」到「触碰通关判定区」窗口内采样位置与运动向量，t_ms 为该选手计时器（TwilightTimer）时间线上的当前总时间（与官方计分同一时钟）；位移全 0 = 该秒近乎静止（照存不建检测平面）。纯内存回合级数据，不落库、回合结束清空。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `level_index` | int | 是 | — |  |
| `seq` | int | 是 | — |  |
| `t_ms` | int | 是 | — |  |
| `px` | float | 是 | — |  |
| `py` | float | 是 | — |  |
| `pz` | float | 是 | — |  |
| `dx` | float | 是 | — |  |
| `dy` | float | 是 | — |  |
| `dz` | float | 是 | — |  |

### `ClientSubsegmentHit`

- type：'subsegment_hit'

- 选手穿越对手采样平面时上报（仅 MULTI 回合）；同一平面可多次上报（擦边往复/曲折路线绕回均如实上报，客户端同平面防抖）。t_ms 为穿越时刻自己计时器时间线上的总时间。服务端按 settled-event 模型结算：某平面最后一次穿越后静默期（约 0.5s）无再穿越才广播，有效时刻取最后一次穿越；低于已结算进度游标的迟到乱序事件直接忽略（画面不回跳）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `level_index` | int | 是 | — |  |
| `seq` | int | 是 | — |  |
| `t_ms` | int | 是 | — |  |

### `ClientLiveTime`

- type：'live_time'

- 选手端实时计时上报（每秒一次，随 subsegment 采样节拍）：total_ms/segment_ms 取自其注册的真实计时器（TwilightTimer）的 RoundTotalMs/CurrentSegmentMs，level_index 为当前所在合集关卡。仅中转裁判/导播，选手间互不转发。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `level_index` | int | 是 | — |  |
| `total_ms` | int | 是 | — |  |
| `segment_ms` | int | 是 | — |  |

### `ClientRefereeMarkPrep`

- type：'referee_mark_prep'

- 裁判标记进入回合准备阶段。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |

### `ClientRefereeSelectPick`

- type：'referee_select_pick'

- 裁判从图池选定本回合选图；CT 类别可随消息提交词条（0-ct_tag_count 个，服务端校验枚举/互斥/数量）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `pick_code` | str | 是 | — |  |
| `tags` | list[str] | 否 | <list> |  |
| `retry_count` | int | None | 否 | None |  |

### `ClientRefereeManualStart`

- type：'referee_manual_start'

- 裁判手动发起开始（触发不可中断的倒计时）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |

### `ClientRefereeVerdict`

- type：'referee_verdict'

- 裁判判定本回合胜负。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `verdict` | RoundVerdict | 是 | — |  |

### `ClientRefereeEditVerdict`

- type：'referee_edit_verdict'

- 裁判事后修改本回合判定（实时同步导播端）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `new_verdict` | RoundVerdict | 是 | — |  |

### `ClientRefereeTerminateRound`

- type：'referee_terminate_round'

- 裁判强制终止当前回合（异常处置）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `reason` | str | 是 | — |  |

### `ClientRefereeEndMatch`

- type：'referee_end_match'

- 裁判手动结束比赛（胜方按比分自动判定；需已达到取胜分数）。常规流程下达到取胜分数时判定落定即自动结束，本消息用于兜底（改判后比分重回阈值、异常卡住的场次）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |

### `ClientCounterStart`

- type：'counter_start'

- 裁判启动独立倒计时器（由 ``!timer [秒]`` 触发）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `seconds` | int | 是 | — |  |

### `ClientCounterReset`

- type：'counter_reset'

- 裁判停止当前倒计时器（由 ``!timer reset`` 触发）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |

### `ClientDirectorSubscribe`

- type：'director_subscribe'

- 导播订阅（占位，导播连接天然只读）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |

### `ClientDirectorCommand`

- type：'director_command'

- 导播控制台发往同账号其他导播连接（OBS 舞台）的操控指令：场景切换（``switch_scene``，payload ``{"scene": ...}``）、Coming Soon 倒计时操控（``soon_start``/``soon_pause``/``soon_reset``/``soon_set_target``，set_target payload ``{"target_ms": ...}``）与直播配置实时下发（``config_update``，payload ``{"config": {...}}``，八个字符串键rtmpA/rtmpB/hlsA/hlsB/pbA/pbB/histA/histB，可部分缺失，服务端不校验、原样透传）。服务端以 ``director_cmd`` 原样定向转发，不落库、不回执发送方。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `action` | 'switch_scene' | 'soon_start' | 'soon_pause' | 'soon_reset' | 'soon_set_target' | 'config_update' | 是 | — |  |
| `payload` | dict[str, Any] | 否 | <dict> |  |

### `ClientHeartbeat`

- type：'heartbeat'

- 心跳保活（导播亦可用）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |

### `ClientDraftSync`

- type：'draft_sync'

- 裁判上报 ban/pick 草稿（前端权威，后端存储+转发）

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `state` | dict[str, Any] | 是 | — |  |

## 服务端 → 客户端

### `SrvAuthOk`

- type：'auth_ok'

- 连接鉴权成功，告知座位与比赛。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `account_id` | str | 是 | — |  |
| `display_name` | str | 是 | — |  |
| `seat` | str | 是 | — |  |
| `match_id` | str | 是 | — |  |
| `match_name` | str | None | 否 | None |  |
| `player_a_name` | str | None | 否 | None |  |
| `player_b_name` | str | None | 否 | None |  |

### `SrvAuthError`

- type：'auth_error'

- 连接鉴权失败（令牌无效/未参与比赛等）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `msg` | str | 是 | — |  |

### `SrvChat`

- type：'chat'

- 广播一条聊天消息（含发送者自己的回声）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `sender_id` | str | None | 是 | — |  |
| `sender_name` | str | 是 | — |  |
| `seat` | str | 是 | — |  |
| `text` | str | 是 | — |  |
| `ts` | datetime | 否 | <now_ts> |  |

### `SrvSystem`

- type：'system'

- 系统消息：全场广播（命令回执、倒计时提示、回合信息等）或单席位定向提示。sender 为聊天展示前缀：广播 ``Twilight``（与落库 ChatMessage.sender_name 一致，全员逐字相同）；定向提示 ``System``（仅目标席位收到、不落库，如重连回 PREP 的补发提示）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `text` | str | 是 | — |  |
| `kind` | str | 否 | 'info' |  |
| `sender` | 'Twilight' | 'System' | 否 | 'Twilight' | 展示前缀：全场广播为 Twilight，单席位定向提示为 System |
| `ts` | datetime | 否 | <now_ts> |  |

### `SrvReadyState`

- type：'ready_state'

- 双方准备状态变更。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `a_ready` | bool | 是 | — |  |
| `b_ready` | bool | 是 | — |  |

### `SrvPreloadState`

- type：'preload_state'

- 双方预载状态广播（上报/重置时；取值 absent|in_progress|done|failed|na，absent=从未上报）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `a_status` | 'absent' | 'in_progress' | 'done' | 'failed' | 'na' | 是 | — |  |
| `b_status` | 'absent' | 'in_progress' | 'done' | 'failed' | 'na' | 是 | — |  |

### `SrvSeatState`

- type：'seat_state'

- 座席连接状态（选手连入/断开广播；新连接初始化序列亦补发全量）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `seat` | str | 是 | — |  |
| `online` | bool | 是 | — |  |

### `SrvPhaseChange`

- type：'phase_change'

- 比赛阶段切换。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `phase` | MatchPhase | 是 | — |  |
| `round_id` | str | None | 否 | None |  |

### `SrvCountdownTick`

- type：'countdown_tick'

- 开始倒计时逐秒提示。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `remaining_secs` | int | 是 | — |  |
| `source` | 'auto' | 'manual' | 是 | — |  |

### `SrvCountdownAbort`

- type：'countdown_abort'

- 开始倒计时被中断（auto 倒计时下选手取消准备）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `reason` | str | 是 | — |  |

### `SrvPickAnnounced`

- type：'pick_announced'

- 选图确定即向全体成员提前下发合集（预览性质；``round_start`` 仍是唯一权威，PREP 期间改图导致两者不同属正常流程，选手端自行作废旧预载）。裁判重新应用选图会重发，以最新一次为准；pick 与 collection 与 ``round_start`` 同构（含词条/重试/计分方式、关卡 id 已展开为显示名）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `pick_code` | str | 是 | — |  |
| `pick` | Pick | 是 | — |  |
| `collection` | CollectionConfig | 是 | — |  |

### `SrvRoundStart`

- type：'round_start'

- 回合开始，向选手下发选图与关卡合集配置。pick.single_scoring 为本场单关计分方式快照（"fastest"/"average"，来自 Match.scoring_method；缺席或 null 时客户端按 fastest 处理，MULTI 回合忽略；见 backend-round-start-single-scoring）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `pick` | Pick | 是 | — |  |
| `collection` | CollectionConfig | 是 | — |  |

### `SrvRoundStartedBroadcast`

- type：'round_started_broadcast'

- 回合开始广播（含项目编号与名称；tags 为 CT 词条）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `pick_code` | str | 是 | — |  |
| `pick_name` | str | 是 | — |  |
| `tags` | list[str] | 否 | <list> |  |

### `SrvPlayerStatus`

- type：'player_status'

- 选手单回合实时状态（重连快照亦复用此消息）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `seat` | str | 是 | — |  |
| `account_id` | str | 是 | — |  |
| `status` | PlayerStatus | 是 | — |  |
| `current_level_index` | int | 是 | — |  |
| `completed_levels` | list[LevelTime] | 否 | <list> |  |
| `attempts` | list[Attempt] | 否 | <list> |  |

### `SrvLevelTimeUpdate`

- type：'level_time_update'

- 某选手单关用时更新（裁判/导播）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `seat` | str | 是 | — |  |
| `account_id` | str | 是 | — |  |
| `level_index` | int | 是 | — |  |
| `this_level_ms` | int | 是 | — |  |
| `total_ms` | int | None | 否 | None |  |
| `invalid_reasons` | list[str] | None | 否 | None |  |

### `SrvSubsegmentSample`

- type：'subsegment_sample'

- 转发对手的采样点给对侧选手（其客户端据此建检测平面）；仅发对方 seat（裁判/导播不收），选手断线重连后按原序补放。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `seat` | str | 是 | — |  |
| `round_id` | str | 是 | — |  |
| `level_index` | int | 是 | — |  |
| `seq` | int | 是 | — |  |
| `t_ms` | int | 是 | — |  |
| `px` | float | 是 | — |  |
| `py` | float | 是 | — |  |
| `pz` | float | 是 | — |  |
| `dx` | float | 是 | — |  |
| `dy` | float | 是 | — |  |
| `dz` | float | 是 | — |  |

### `SrvSubsegmentGap`

- type：'subsegment_gap'

- 实时时间差广播（双方选手 + 裁判 + 导播；overlay 用）：平面穿越结算（静默期无再穿越）后发出，``gap_ms = hit_ms - sample_ms``，>0 = 穿越方落后，可为负。同一 (hit_seat, level_index, seq) 可能再次收到（结算后再次穿越的修正/amend）——前端按键覆盖取最新。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `level_index` | int | 是 | — |  |
| `seq` | int | 是 | — |  |
| `seat` | str | 是 | — |  |
| `sample_ms` | int | 是 | — |  |
| `hit_seat` | str | 是 | — |  |
| `hit_ms` | int | 是 | — |  |
| `gap_ms` | int | 是 | — |  |

### `SrvLiveTime`

- type：'live_time'

- 选手实时计时中转（每秒；仅裁判与导播席，overlay 计时显示用）：服务端按席暂存最近一条，IN_ROUND 期间裁判/导播晚连时握手补发双方。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `seat` | str | 是 | — |  |
| `round_id` | str | 是 | — |  |
| `level_index` | int | 是 | — |  |
| `total_ms` | int | 是 | — |  |
| `segment_ms` | int | 是 | — |  |

### `SrvRoundResult`

- type：'round_result'

- 本回合结算（判定与双方成绩）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `verdict` | RoundVerdict | 是 | — |  |
| `score_a_ms` | int | None | 否 | None |  |
| `score_b_ms` | int | None | 否 | None |  |
| `detail` | dict[str, object] | 否 | <dict> |  |

### `SrvCumulativeScore`

- type：'cumulative_score'

- 累计比分。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `wins_a` | int | 是 | — |  |
| `wins_b` | int | 是 | — |  |
| `threshold` | int | 是 | — |  |

### `SrvMatchEnd`

- type：'match_end'

- 比赛结束，宣告胜方（判定落定且比分达到取胜分数时自动触发）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `winner` | 'A' | 'B' | 是 | — |  |

### `SrvCounterState`

- type：'counter_state'

- 独立倒计时器状态（剩余秒数或 None）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `remaining_secs` | int | None | 否 | None |  |

### `SrvCounterAlert`

- type：'counter_alert'

- 独立倒计时器告警（整分钟/30·20·10/5..1/0）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `remaining_secs` | int | 是 | — |  |

### `SrvVerdictEdit`

- type：'verdict_edit'

- 判定被修改的广播（导播端）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `round_id` | str | 是 | — |  |
| `old_verdict` | RoundVerdict | 是 | — |  |
| `new_verdict` | RoundVerdict | 是 | — |  |

### `SrvDraftState`

- type：'draft_state'

- 广播 ban/pick 草稿给全员（含导播）；state 原样转发自裁判端。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `state` | dict[str, Any] | 是 | — |  |

### `SrvDirectorCommand`

- type：'director_cmd'

- 定向转发导播控制台操控指令（action/payload 原样来自 ``director_command``）：仅发发送方之外的同账号 DIRECTOR 连接（OBS 舞台），每个导播只控自己的舞台；选手/裁判与其他账号导播均不收。另含服务端主动下发的 ``state_sync``：DIRECTOR 连接 ``auth_ok`` 后若有状态暂存，补发最近的场景/倒计时/直播配置（payload ``{"scene"/"soon"/"config"}``，soon 内时间戳均为服务器毫秒、附 ``now_ms`` 供时钟校正）。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `action` | str | 是 | — |  |
| `payload` | dict[str, Any] | 否 | <dict> |  |

### `SrvMatchStatus`

- type：'match_status'

- 比赛状态变更广播（pause/resume），导播/裁判多标签同步。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `status` | MatchStatus | 是 | — |  |

### `SrvDisplaced`

- type：'displaced'

- 本连接被同身份（账号+座位+比赛）且带 ``exclusive=1`` 的新连接顶掉：先于 close(4001) 送达。被顶掉 ≠ 鉴权失败（token 仍有效），前端应停止自动重连并提示「已在其他窗口打开」。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `reason` | str | 是 | — |  |

### `SrvError`

- type：'error'

- 错误回执（命令非法/权限不足/比赛已暂停等）。仅发给触发方（特定连接/席位，不广播、不落库）；客户端展示沿用 ``System`` 前缀，与全场广播的 ``system`` 消息（``Twilight``）区分。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `code` | int | 是 | — |  |
| `msg` | str | 是 | — |  |
