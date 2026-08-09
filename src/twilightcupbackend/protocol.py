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
    model_config = _cfg
    type: Literal["system"] = "system"
    text: str
    kind: str = "info"
    ts: datetime = Field(default_factory=now_ts)


class SrvReadyState(BaseModel):
    model_config = _cfg
    type: Literal["ready_state"] = "ready_state"
    a_ready: bool
    b_ready: bool


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


class SrvMatchStatus(BaseModel):
    """比赛级状态变更广播（pause/resume），便于导播端、裁判多标签实时同步。"""

    model_config = _cfg
    type: Literal["match_status"] = "match_status"
    status: MatchStatus


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
    | SrvSeatState
    | SrvPhaseChange
    | SrvCountdownTick
    | SrvCountdownAbort
    | SrvRoundStart
    | SrvRoundStartedBroadcast
    | SrvPlayerStatus
    | SrvLevelTimeUpdate
    | SrvRoundResult
    | SrvCumulativeScore
    | SrvMatchEnd
    | SrvCounterState
    | SrvCounterAlert
    | SrvVerdictEdit
    | SrvDraftState
    | SrvMatchStatus
    | SrvError
)
