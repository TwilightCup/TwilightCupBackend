"""WebSocket 消息协议（客户端与服务端共同实现的契约）。

所有消息为带 ``type`` 判别字段的 pydantic v2 模型，经 orjson 编解码。
入站用 ``parse_client_message``（判别联合），出站直接 ``model.model_dump_json()``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .datatypes import (
    Attempt,
    CollectionConfig,
    LevelTime,
    MatchPhase,
    MatchStatus,
    Pick,
    PlayerStatus,
    RoundVerdict,
    now_ts,
)

_cfg = ConfigDict(extra="forbid")

# 预载状态取值（合集提前下发与预载门控）：
# in_progress 预载已开始 / done 完成 / failed 失败（回退标准加载）
# / na 不适用（SINGLE 合集）；preload_state 额外含 absent（从未上报，初始值）。
PreloadReportStatus = Literal["in_progress", "done", "failed", "na"]
PreloadStatus = Literal["absent", "in_progress", "done", "failed", "na"]

# 预载门控能力标识（WS 连接 ``?cap=`` 参数声明；门控只对声明了能力的席位生效，
# 未声明的旧客户端席位视为 na 豁免）
PRELOAD_CAP = "preload1"

# ===========================================================================
# 客户端 -> 服务端
# ===========================================================================


class ClientChat(BaseModel):
    model_config = _cfg
    type: Literal["chat"] = "chat"
    text: str


class ClientReadyToggle(BaseModel):
    model_config = _cfg
    type: Literal["ready_toggle"] = "ready_toggle"


class ClientLevelTimeUpload(BaseModel):
    model_config = _cfg
    type: Literal["level_time_upload"] = "level_time_upload"
    round_id: str
    level_index: int
    this_level_ms: int
    total_ms: int | None = None
    # 完成时刻活跃的无效原因；缺省/空 = 有效。元素 "<Reason>"，
    # 不可原谅原因带 "!" 前缀（如 "!CheatCode"）。INVALID_ATTEMPT_REQ §3.2
    invalid_reasons: list[str] | None = None


class ClientAttemptSkip(BaseModel):
    model_config = _cfg
    type: Literal["attempt_skip"] = "attempt_skip"
    round_id: str
    attempt_index: int


class ClientProjectComplete(BaseModel):
    model_config = _cfg
    type: Literal["project_complete"] = "project_complete"
    round_id: str
    final_total_ms: int | None = None


class ClientForfeitSignal(BaseModel):
    model_config = _cfg
    type: Literal["forfeit_signal"] = "forfeit_signal"
    round_id: str
    reason: Literal["multi_exit", "single_exit_0_valid"]


class ClientReconnectResync(BaseModel):
    model_config = _cfg
    type: Literal["reconnect_resync"] = "reconnect_resync"
    round_id: str


class ClientPreloadReport(BaseModel):
    """选手端预载状态上报（仅 PLAYER_A/PLAYER_B 席位；PREP 阶段有意义）。

    场景级预载仅 MULTI 合集，SINGLE 合集选手端报 ``na``；旧版客户端不上报
    （连接不带 ``cap=preload1`` 能力，门控豁免）。
    """

    model_config = _cfg
    type: Literal["preload_report"] = "preload_report"
    status: PreloadReportStatus
    detail: str | None = None  # 失败原因等，仅日志/告警用


class ClientSubsegmentSample(BaseModel):
    """选手端分段采样上报（仅 MULTI 回合、PLAYER 席位；每秒一次）。

    采样窗口 = 每关「角色从装死苏醒」到「触碰通关判定区」；t_ms 为该选手
    计时器（TwilightTimer，经 ITimerProvider 注册）时间线上的当前总时间
    （RoundTotalMs）——与官方计分同一条时钟。
    (dx,dy,dz) 为采样间隔位移，全 0 = 该秒近乎静止（照存不建检测平面）。
    """

    model_config = _cfg
    type: Literal["subsegment_sample"] = "subsegment_sample"
    round_id: str
    level_index: int
    seq: int  # 关内递增，从 0 起
    t_ms: int
    px: float
    py: float
    pz: float
    dx: float
    dy: float
    dz: float
    plane_radius: float | None = Field(
        default=None,
        description="本端检测平面半径（米，Subsegment.PlaneRadius）；服务端用于陈旧平面"
        "回路识别，旧版客户端可缺省",
    )


class ClientSubsegmentHit(BaseModel):
    """选手穿越对手采样平面时上报（仅 MULTI 回合；同一平面可多次上报）。

    服务端按 settled-event 模型结算：某平面最后一次穿越后静默期（约 0.5s）
    内无再穿越才广播 ``subsegment_gap``，有效时刻取最后一次穿越（擦边往返
    的早触发被真实穿越顶掉）。低于已结算进度游标的迟到乱序事件忽略；但
    距该席最近一次穿越 ≥3s 的低键穿越视为「失败折返重来」的真实重访，
    重开该键按当前时刻广播（计时器坠落不清零，数值自带罚时成本）。
    """

    model_config = _cfg
    type: Literal["subsegment_hit"] = "subsegment_hit"
    round_id: str
    level_index: int
    seq: int
    t_ms: int  # 命中时刻该选手计时器时间线上的总时间（RoundTotalMs）


class ClientLiveTime(BaseModel):
    """选手端实时计时上报（每秒一次，随 subsegment 采样节拍；当前回合有效）。

    total_ms/segment_ms 取自选手端注册的真实计时器（TwilightTimer）的
    RoundTotalMs/CurrentSegmentMs；real_time_ms 可选，为提供方 Real Time
    现实/墙钟计时（TwilightTimer 实现 IRealtimeTimerProvider 时附带）；
    level_index 为其当前所在合集关卡。仅中转裁判/导播。
    """

    model_config = _cfg
    type: Literal["live_time"] = "live_time"
    round_id: str
    level_index: int
    total_ms: int
    segment_ms: int
    real_time_ms: int | None = None


class ClientRefereeMarkPrep(BaseModel):
    model_config = _cfg
    type: Literal["referee_mark_prep"] = "referee_mark_prep"


class ClientRefereeSelectPick(BaseModel):
    """裁判选图；CT/EX/CP 类别可随消息提交词条（见 backend-ct-pick-tags §2.1）。

    retry_count：CT/EX 单关的重试次数（改由裁判选图时指定，必填 ≥1）；
    其余类别沿用图池预设，传入会被拒绝。
    """

    model_config = _cfg
    type: Literal["referee_select_pick"] = "referee_select_pick"
    pick_code: str
    tags: list[str] = Field(default_factory=list)  # 仅词条类别可非空；上限 ct_tag_count
    retry_count: int | None = None


class ClientRefereeManualStart(BaseModel):
    model_config = _cfg
    type: Literal["referee_manual_start"] = "referee_manual_start"


class ClientRefereeVerdict(BaseModel):
    model_config = _cfg
    type: Literal["referee_verdict"] = "referee_verdict"
    round_id: str
    verdict: RoundVerdict


class ClientRefereeEditVerdict(BaseModel):
    model_config = _cfg
    type: Literal["referee_edit_verdict"] = "referee_edit_verdict"
    round_id: str
    new_verdict: RoundVerdict


class ClientRefereeTerminateRound(BaseModel):
    model_config = _cfg
    type: Literal["referee_terminate_round"] = "referee_terminate_round"
    round_id: str
    reason: str


class ClientRefereeEndMatch(BaseModel):
    """裁判手动结束比赛（胜方按比分自动判定；需已达到取胜分数）。

    常规流程下达到取胜分数时判定落定即自动结束，本消息用于兜底
    （改判后比分重回阈值、异常卡住的场次）。
    """

    model_config = _cfg
    type: Literal["referee_end_match"] = "referee_end_match"


class ClientCounterStart(BaseModel):
    """裁判启动独立倒计时器（由 ``!timer [seconds]`` 触发）。"""

    model_config = _cfg
    type: Literal["counter_start"] = "counter_start"
    seconds: int


class ClientCounterReset(BaseModel):
    model_config = _cfg
    type: Literal["counter_reset"] = "counter_reset"


class ClientDirectorSubscribe(BaseModel):
    model_config = _cfg
    type: Literal["director_subscribe"] = "director_subscribe"


class ClientDirectorCommand(BaseModel):
    """导播控制台发往同账号其他导播连接（OBS 舞台）的操控指令。

    控制台（Chrome）与舞台（OBS 内置 CEF）分属不同进程，localStorage +
    StorageEvent 不互通，故场景切换与 Coming Soon 倒计时操控经服务端定向
    转发（不落库、不广播选手/裁判）。payload 按 action 不同含义：
    switch_scene: {"scene": "soon"}（SceneKey 字符串）；
    soon_start: {}（从 paused 恢复或首次启动）；soon_pause: {}（暂停倒计时）；
    soon_reset: {}（重置为 idle）；
    soon_set_target: {"target_ms": 300000}（改目标毫秒数）；
    config_update: {"config": {...}}（直播配置实时下发：rtmpA/rtmpB/hlsA/
    hlsB/pbA/pbB/histA/histB 八个字符串键，可部分缺失；结构由前端约定，
    服务端不校验、原样透传，与其余 action 的宽松口径一致）。
    """

    model_config = _cfg
    type: Literal["director_command"] = "director_command"
    # 指令类别
    action: Literal[
        "switch_scene",
        "soon_start",
        "soon_pause",
        "soon_reset",
        "soon_set_target",
        "config_update",
    ]
    # 指令载荷（见类 docstring）
    payload: dict[str, Any] = Field(default_factory=dict)


class ClientHeartbeat(BaseModel):
    model_config = _cfg
    type: Literal["heartbeat"] = "heartbeat"


class ClientDraftSync(BaseModel):
    """裁判上报 ban/pick 草稿（前端权威，后端仅存储+转发，不解析内部结构）。"""

    model_config = _cfg
    type: Literal["draft_sync"] = "draft_sync"
    state: dict[str, Any]


ClientMessage = Annotated[
    ClientChat
    | ClientReadyToggle
    | ClientLevelTimeUpload
    | ClientAttemptSkip
    | ClientProjectComplete
    | ClientForfeitSignal
    | ClientReconnectResync
    | ClientPreloadReport
    | ClientSubsegmentSample
    | ClientSubsegmentHit
    | ClientLiveTime
    | ClientRefereeMarkPrep
    | ClientRefereeSelectPick
    | ClientRefereeManualStart
    | ClientRefereeVerdict
    | ClientRefereeEditVerdict
    | ClientRefereeTerminateRound
    | ClientRefereeEndMatch
    | ClientCounterStart
    | ClientCounterReset
    | ClientDirectorSubscribe
    | ClientDirectorCommand
    | ClientHeartbeat
    | ClientDraftSync,
    Field(discriminator="type"),
]

_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


def parse_client_message(raw: str | bytes) -> ClientMessage:
    """解析入站客户端消息。"""
    return _client_adapter.validate_json(raw)


# ===========================================================================
# 服务端 -> 客户端
# ===========================================================================


class SrvAuthOk(BaseModel):
    model_config = _cfg
    type: Literal["auth_ok"] = "auth_ok"
    account_id: str
    display_name: str
    seat: str  # PLAYER_A / PLAYER_B / REFEREE / DIRECTOR
    match_id: str
    match_name: str | None = None
    player_a_name: str | None = None  # 选手 A 展示名（导播/裁判连入即拿）
    player_b_name: str | None = None  # 选手 B 展示名


class SrvAuthError(BaseModel):
    model_config = _cfg
    type: Literal["auth_error"] = "auth_error"
    msg: str


class SrvChat(BaseModel):
    model_config = _cfg
    type: Literal["chat"] = "chat"
    sender_id: str | None
    sender_name: str
    seat: str
    text: str
    ts: datetime = Field(default_factory=now_ts)


class SrvSystem(BaseModel):
    """系统消息：全场广播（sender=Twilight）或单席位定向提示（sender=System）。

    广播走 ConnectionManager.system_message（全员逐字一致并落库）；定向
    提示（如重连回 PREP 的补发提示）仅发该连接、不落库。客户端以 sender
    为聊天展示前缀；错误回执仍走 SrvError（不带 sender，客户端沿用
    "System" 前缀）。
    """

    model_config = _cfg
    type: Literal["system"] = "system"
    text: str
    kind: str = "info"
    sender: Literal["Twilight", "System"] = Field(
        default="Twilight",
        description="展示前缀：全场广播为 Twilight，单席位定向提示为 System",
    )
    ts: datetime = Field(default_factory=now_ts)


class SrvReadyState(BaseModel):
    model_config = _cfg
    type: Literal["ready_state"] = "ready_state"
    a_ready: bool
    b_ready: bool


class SrvPreloadState(BaseModel):
    """双方预载状态广播（状态变更与重置时；同 ready_state 的广播模式）。"""

    model_config = _cfg
    type: Literal["preload_state"] = "preload_state"
    a_status: PreloadStatus
    b_status: PreloadStatus


class SrvSeatState(BaseModel):
    """座席连接状态（选手连入/断开时广播；见 backend-seat-presence）。"""

    model_config = _cfg
    type: Literal["seat_state"] = "seat_state"
    seat: str  # PLAYER_A / PLAYER_B
    online: bool


class SrvPhaseChange(BaseModel):
    model_config = _cfg
    type: Literal["phase_change"] = "phase_change"
    phase: MatchPhase
    round_id: str | None = None


class SrvCountdownTick(BaseModel):
    model_config = _cfg
    type: Literal["countdown_tick"] = "countdown_tick"
    remaining_secs: int
    source: Literal["auto", "manual"]


class SrvCountdownAbort(BaseModel):
    model_config = _cfg
    type: Literal["countdown_abort"] = "countdown_abort"
    reason: str


class SrvPickAnnounced(BaseModel):
    """选图确定即向全体成员提前下发合集（预览；round_start 仍是唯一权威）。

    裁判每次重新应用选图都会重发，选手端以最新一次为准；
    PREP 阶段选手断线重连、以及晚连的导播席（各阶段，categoryinfo 场景
    对齐当前项目用）都在握手序列补发（见 connection_manager.connect）。
    """

    model_config = _cfg
    type: Literal["pick_announced"] = "pick_announced"
    pick_code: str
    pick: Pick  # 完整 Pick（与 round_start.pick 同构，含词条/重试/计分方式/展示图 URL）
    collection: CollectionConfig  # 与 round_start.collection 同构（已展开为显示名）


class SrvRoundStart(BaseModel):
    model_config = _cfg
    type: Literal["round_start"] = "round_start"
    round_id: str
    pick: Pick
    collection: CollectionConfig


class SrvRoundStartedBroadcast(BaseModel):
    model_config = _cfg
    type: Literal["round_started_broadcast"] = "round_started_broadcast"
    round_id: str
    pick_code: str
    pick_name: str
    tags: list[str] = Field(default_factory=list)  # CT 词条（导播展示用）


class SrvPlayerStatus(BaseModel):
    model_config = _cfg
    type: Literal["player_status"] = "player_status"
    seat: str
    account_id: str
    status: PlayerStatus
    current_level_index: int
    completed_levels: list[LevelTime] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)


class SrvLevelTimeUpdate(BaseModel):
    model_config = _cfg
    type: Literal["level_time_update"] = "level_time_update"
    seat: str
    account_id: str
    level_index: int
    this_level_ms: int
    total_ms: int | None = None
    invalid_reasons: list[str] | None = None  # 同 ClientLevelTimeUpload（裁判端可见）


class SrvSubsegmentSample(BaseModel):
    """转发对手的采样点给对侧选手（其客户端据此建检测平面）。

    仅发对方 seat（裁判/导播不收，overlay 消费 subsegment_gap）；
    选手断线重连后由 reconnect_resync 触发按原序补放。
    """

    model_config = _cfg
    type: Literal["subsegment_sample"] = "subsegment_sample"
    seat: str  # 采样归属（PLAYER_A / PLAYER_B）
    round_id: str
    level_index: int
    seq: int
    t_ms: int
    px: float
    py: float
    pz: float
    dx: float
    dy: float
    dz: float


class SrvSubsegmentGap(BaseModel):
    """实时时间差广播（双方选手 + 裁判 + 导播；overlay 用）。

    gap_ms = hit_ms - sample_ms，>0 = 穿越方落后，可为负。仅在平面穿越
    结算（静默期无再穿越）后发出；同键或更低键可能再次收到——结算后再次
    穿越的修正（amend）或失败折返重来的重访（重穿时刻自带罚时成本）——
    前端展示取最新一条，进度类 UI 需自持最大 seq（服务端游标不回退）。
    """

    model_config = _cfg
    type: Literal["subsegment_gap"] = "subsegment_gap"
    round_id: str
    level_index: int
    seq: int
    seat: str  # 采样归属
    sample_ms: int
    hit_seat: str  # 穿越方
    hit_ms: int
    gap_ms: int


class SrvLiveTime(BaseModel):
    """选手实时计时中转（每秒；仅裁判与导播席，overlay 计时显示用）。

    服务端按席暂存最近一条：IN_ROUND 期间裁判/导播晚连时握手补发双方。
    real_time_ms 为可选字段：选手端提供方支持 Real Time 时携带。
    """

    model_config = _cfg
    type: Literal["live_time"] = "live_time"
    seat: str
    round_id: str
    level_index: int
    total_ms: int
    segment_ms: int
    real_time_ms: int | None = None


class SrvRoundResult(BaseModel):
    model_config = _cfg
    type: Literal["round_result"] = "round_result"
    round_id: str
    verdict: RoundVerdict
    score_a_ms: int | None = None
    score_b_ms: int | None = None
    detail: dict[str, object] = Field(default_factory=dict)


class SrvCumulativeScore(BaseModel):
    model_config = _cfg
    type: Literal["cumulative_score"] = "cumulative_score"
    wins_a: int
    wins_b: int
    threshold: int


class SrvMatchEnd(BaseModel):
    model_config = _cfg
    type: Literal["match_end"] = "match_end"
    winner: Literal["A", "B"]


class SrvCounterState(BaseModel):
    model_config = _cfg
    type: Literal["counter_state"] = "counter_state"
    remaining_secs: int | None = None


class SrvCounterAlert(BaseModel):
    model_config = _cfg
    type: Literal["counter_alert"] = "counter_alert"
    remaining_secs: int


class SrvVerdictEdit(BaseModel):
    model_config = _cfg
    type: Literal["verdict_edit"] = "verdict_edit"
    round_id: str
    old_verdict: RoundVerdict
    new_verdict: RoundVerdict


class SrvDraftState(BaseModel):
    """广播 ban/pick 草稿状态给全员（含导播）；state 原样转发自裁判端上报。"""

    model_config = _cfg
    type: Literal["draft_state"] = "draft_state"
    state: dict[str, Any]


class SrvDirectorCommand(BaseModel):
    """定向转发给同账号其他 DIRECTOR 连接（OBS 舞台）的操控指令。

    action/payload 原样转发自 ClientDirectorCommand；仅发 sender 之外的
    同账号导播连接（每个导播只控自己的舞台），选手/裁判不收，发送方不回执。
    另有服务端主动下发的 action="state_sync"：DIRECTOR 连接 auth_ok 后若
    该 (account_id, match_id) 有状态暂存，补发 payload={"scene": ...,
    "soon": {"target_ms"/"started_at"/"paused_at"/"now_ms"（服务器毫秒）},
    "config": {...}}，消除舞台晚开收不到状态的问题。
    """

    model_config = _cfg
    type: Literal["director_cmd"] = "director_cmd"
    action: str  # 同 ClientDirectorCommand.action
    payload: dict[str, Any] = Field(default_factory=dict)


class SrvMatchStatus(BaseModel):
    """比赛级状态变更广播（pause/resume），便于导播端、裁判多标签实时同步。"""

    model_config = _cfg
    type: Literal["match_status"] = "match_status"
    status: MatchStatus


class SrvDisplaced(BaseModel):
    """本连接被同身份（账号+座位+比赛）新连接以 exclusive=1 顶掉。

    先于 close(4001) 送达；被顶掉 ≠ 鉴权失败（token 仍有效，勿登出/重连），
    前端应停止自动重连并提示「已在其他窗口打开」。见 exclusive takeover 契约。
    """

    model_config = _cfg
    type: Literal["displaced"] = "displaced"
    reason: str  # 目前仅 "superseded_by_new_connection"


class SrvError(BaseModel):
    model_config = _cfg
    type: Literal["error"] = "error"
    code: int
    msg: str


ServerMessage = (
    SrvAuthOk
    | SrvAuthError
    | SrvChat
    | SrvSystem
    | SrvReadyState
    | SrvPreloadState
    | SrvSeatState
    | SrvPhaseChange
    | SrvCountdownTick
    | SrvCountdownAbort
    | SrvPickAnnounced
    | SrvRoundStart
    | SrvRoundStartedBroadcast
    | SrvPlayerStatus
    | SrvLevelTimeUpdate
    | SrvSubsegmentSample
    | SrvSubsegmentGap
    | SrvLiveTime
    | SrvRoundResult
    | SrvCumulativeScore
    | SrvMatchEnd
    | SrvCounterState
    | SrvCounterAlert
    | SrvVerdictEdit
    | SrvDraftState
    | SrvDirectorCommand
    | SrvMatchStatus
    | SrvDisplaced
    | SrvError
)
