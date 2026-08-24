"""全部领域模型与枚举。

设计要点：
- 文档主键统一用字符串 UUID（``id``），便于 JSON/WS 序列化，规避 ObjectId codec。
- 时间字段统一用带时区的 ``datetime``（UTC），由 orjson 原生序列化为 ISO 字符串。
- ``CollectionConfig`` 为关卡合集插件定义的不透明配置，服务端原样存储与下发，不作解释。
- 枚举采用 ``IntEnum`` + ``from_str`` match/case（参考 AShareGateway datatypes.py）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now_ts() -> datetime:
    """当前 UTC 时间戳。"""
    return datetime.now(UTC)


def _new_id() -> str:
    """生成文档主键（32 位十六进制 UUID）。"""
    return uuid4().hex


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class AccountType(IntEnum):
    """账号类型。"""

    PLAYER = 1  # 选手
    REFEREE = 2  # 裁判
    DIRECTOR = 3  # 导播
    ADMIN = 4  # 管理员（赛事方）

    @classmethod
    def from_str(cls, value: str) -> AccountType:
        match value:
            case "选手" | "player":
                return cls.PLAYER
            case "裁判" | "referee":
                return cls.REFEREE
            case "导播" | "director":
                return cls.DIRECTOR
            case "管理员" | "admin":
                return cls.ADMIN
            case _:
                return cls.PLAYER


class PickType(IntEnum):
    """选图项目类型。"""

    MULTI = 1  # 多关项目
    SINGLE = 2  # 单关项目

    @classmethod
    def from_str(cls, value: str) -> PickType:
        match value:
            case "多关" | "multi":
                return cls.MULTI
            case "单关" | "single":
                return cls.SINGLE
            case _:
                return cls.MULTI


class ScoringMethod(IntEnum):
    """单关项目成绩计算方式。"""

    FASTEST = 1  # 最快时间
    AVERAGE = 2  # 平均时间

    @classmethod
    def from_str(cls, value: str) -> ScoringMethod:
        match value:
            case "最快" | "fastest":
                return cls.FASTEST
            case "平均" | "average":
                return cls.AVERAGE
            case _:
                return cls.FASTEST


class MatchStatus(IntEnum):
    """比赛比赛状态（持久化层）。"""

    CREATED = 0  # 已创建未开始
    RUNNING = 1  # 进行中
    ENDED = 2  # 已结束
    PAUSED = 3  # 暂停（保留回合数据，释放选手占用）


class MatchPhase(IntEnum):
    """比赛生命周期阶段（内存实时状态机）。"""

    IDLE = 0  # 空闲，未选图
    PREP = 1  # 准备阶段
    COUNTDOWN = 2  # 开始倒计时中
    IN_ROUND = 3  # 回合进行中
    ROUND_JUDGING = 4  # 回合待判定
    ROUND_END = 5  # 回合结束（瞬态）
    MATCH_END = 6  # 比赛结束（终态）


class PlayerStatus(IntEnum):
    """选手单回合状态。"""

    IN_GAME = 1  # 游戏中
    COMPLETED = 2  # 已完成
    FORFEITED = 3  # 已弃权


class AttemptStatus(IntEnum):
    """单关项目单次尝试状态。"""

    VALID = 1  # 有效成绩
    SKIPPED = 2  # 跳过（N/A）
    UNFINISHED = 3  # 未完成（N/A）
    INVALID = 4  # 通关但带无效标记——time_ms 保留作证据，不参与计分


class RoundSource(IntEnum):
    """回合来源。"""

    NORMAL = 1  # 正常回合
    REMATCH = 2  # 重赛回合


class RoundVerdict(IntEnum):
    """回合胜负判定。"""

    A_WIN = 1  # 选手 A 胜
    B_WIN = 2  # 选手 B 胜
    TIE_REMATCH = 3  # 平局 → 重赛
    A_DISCONNECT_LOSS = 4  # 选手 A 断连判负
    B_DISCONNECT_LOSS = 5  # 选手 B 断连判负

    @property
    def is_a_win(self) -> bool:
        return self in (RoundVerdict.A_WIN, RoundVerdict.B_DISCONNECT_LOSS)

    @property
    def is_b_win(self) -> bool:
        return self in (RoundVerdict.B_WIN, RoundVerdict.A_DISCONNECT_LOSS)


class ChatSenderRole(IntEnum):
    """聊天发送者角色。"""

    PLAYER = 1  # 选手
    REFEREE = 2  # 裁判
    SYSTEM = 3  # 系统


class Seat(IntEnum):
    """连接在比赛中的座位（用于消息路由与就绪状态）。"""

    PLAYER_A = 1  # 选手 A
    PLAYER_B = 2  # 选手 B
    REFEREE = 3  # 裁判
    DIRECTOR = 4  # 导播（只读）

    @property
    def name_zh(self) -> str:
        match self:
            case Seat.PLAYER_A:
                return "选手A"
            case Seat.PLAYER_B:
                return "选手B"
            case Seat.REFEREE:
                return "裁判"
            case Seat.DIRECTOR:
                return "导播"

    @property
    def name_en(self) -> str:
        match self:
            case Seat.PLAYER_A:
                return "Player A"
            case Seat.PLAYER_B:
                return "Player B"
            case Seat.REFEREE:
                return "Referee"
            case Seat.DIRECTOR:
                return "Director"


class TournamentStatus(IntEnum):
    """赛事状态。"""

    DRAFT = 0  # 配置中（增减选手/裁判/导播、设种子）
    READY = 1  # 配置完成，待生成赛程
    IN_PROGRESS = 2  # 赛程已生成，进行中
    COMPLETED = 3  # 已完成（决出排名）
    CANCELLED = 4  # 已取消


class TournamentFormat(IntEnum):
    """赛事赛制。"""

    SINGLE_ELIM = 1  # 单败淘汰
    DOUBLE_ELIM = 2  # 双败淘汰
    SWISS = 3  # 瑞士轮

    @classmethod
    def from_str(cls, value: str) -> TournamentFormat:
        match value:
            case "单败" | "single_elim" | "single-elim":
                return cls.SINGLE_ELIM
            case "双败" | "double_elim" | "double-elim":
                return cls.DOUBLE_ELIM
            case "瑞士" | "swiss":
                return cls.SWISS
            case _:
                return cls.SINGLE_ELIM


class FixtureStatus(IntEnum):
    """对阵节点状态。"""

    PENDING = 0  # 双方未定或未就绪
    READY = 1  # 双方已定、可生成实战比赛
    RUNNING = 2  # 实战比赛进行中
    COMPLETED = 3  # 已结束（胜者已定）
    SKIPPED = 4  # 跳过（轮空自动晋级 / 弃权）


class BracketSide(IntEnum):
    """对阵所在半区（双败淘汰用；单败/瑞士统一为 MAIN）。"""

    MAIN = 0  # 主区
    WINNERS = 1  # 胜者组
    LOSERS = 2  # 败者组


# ---------------------------------------------------------------------------
# 基础模型
# ---------------------------------------------------------------------------

_model_config = ConfigDict(arbitrary_types_allowed=True)


class Document(BaseModel):
    """带字符串 UUID 主键的顶层文档基类（主键映射 Mongo 的 _id）。"""

    model_config = _model_config

    id: str = Field(default_factory=_new_id)


class Account(Document):
    """账号（可拥有多个角色）。"""

    username: str  # 登录名（唯一）
    password_hash: str  # 口令哈希
    roles: list[AccountType] = Field(default_factory=list)  # 角色集合
    display_name: str  # 展示名
    created_at: datetime = Field(default_factory=now_ts)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_type(cls, data: Any) -> Any:
        """兼容旧文档：仅有 ``type`` 字段时派生 ``roles``；旧 ADMIN 补裁判+导播。"""
        if isinstance(data, dict) and "roles" not in data and "type" in data:
            legacy = data["type"]
            try:
                t = AccountType(int(legacy))
            except ValueError, TypeError:
                t = AccountType.from_str(str(legacy))
            roles = [t]
            if t == AccountType.ADMIN:
                roles += [AccountType.REFEREE, AccountType.DIRECTOR]
            data = {**data, "roles": roles}
        return data

    @property
    def primary_role(self) -> AccountType:
        """主角色（最高优先级 ADMIN>REFEREE>DIRECTOR>PLAYER），用于 JWT/展示。"""
        for t in (
            AccountType.ADMIN,
            AccountType.REFEREE,
            AccountType.DIRECTOR,
            AccountType.PLAYER,
        ):
            if t in self.roles:
                return t
        return AccountType.PLAYER


class CollectionConfig(BaseModel):
    """关卡合集配置（插件定义格式，服务端原样存储，下发前把关卡 id 展开为名字）。"""

    model_config = _model_config

    # 持久层存关卡 id：多关 {"levels": [level_id, ...]}；单关 {"level": level_id}
    raw: dict[str, Any]


class Level(Document):
    """关卡（游戏内的一张图；关卡库统一管理，图池选图按 id 引用）。"""

    name: str  # 关卡标识（唯一，如 "Intro"）
    display_name: str  # 展示名（默认同 name）
    logo: str | None = None  # 展示图 minio object key
    logo_url: str | None = None  # 公开 URL（输出层拼，不持久化）
    created_at: datetime = Field(default_factory=now_ts)


class Pick(BaseModel):
    """选图（= 一个项目 / 一个回合）。"""

    model_config = _model_config

    code: str  # 编号，图池内唯一，如 ML1
    name: str  # 项目名称（展示用）
    type: PickType  # 多关 / 单关
    retry_count: int | None = None  # 重试次数，仅单关有效
    collection: CollectionConfig  # 关卡合集配置
    tag: str | None = None  # 附加词条，如 "单关 + 全存档点"
    category: str | None = None  # 所属类别（展示分组）
    logo: str | None = None  # 展示图 minio object key
    logo_url: str | None = None  # 预签名 URL（输出层签发，不持久化）
    # CT 词条集合（裁判 referee_select_pick 提交，仅 CT 类别可有非空值）。
    # 图池中的 Pick 恒为空；回合 pick 快照存内存 pending pick 上，随 round_start 发出
    # 后冻结进 pick_snapshot（见 backend-ct-pick-tags §2.2）。无词条时为空数组。
    tags: list[str] = Field(default_factory=list)
    # 本场比赛的单关计分方式快照（"fastest"/"average"，随 round_start 下发；
    # 见 backend-round-start-single-scoring §2.2）。图池选图恒为 None
    # ——计分方式属于赛制（Match.scoring_method），不属于图池。
    single_scoring: str | None = None


class Category(BaseModel):
    """图池类别（仅展示分组，无程序逻辑）。"""

    model_config = _model_config

    name: str  # 类别名，如 ML / IL / CP
    picks: list[Pick] = Field(default_factory=list)


class Mappool(BaseModel):
    """图池：一场比赛所有可选项目的集合。"""

    model_config = _model_config

    categories: list[Category] = Field(default_factory=list)

    def get_pick(self, code: str) -> Pick | None:
        """按编号取选图。"""
        for category in self.categories:
            for pick in category.picks:
                if pick.code == code:
                    return pick
        return None

    def all_picks(self) -> list[Pick]:
        return [pick for c in self.categories for pick in c.picks]


class Match(Document):
    """比赛比赛（由管理员创建并配置）。"""

    name: str  # 比赛名称
    bo_format: int  # 赛制 BO 数，如 BO9
    win_threshold: int  # 取胜所需分数，如 BO9 → 5
    scoring_method: ScoringMethod  # 单关成绩计算方式（最快/平均）
    start_countdown_delay: int  # 开始倒计时延迟秒数，默认 5
    ban_count: int = 1  # 每方选图 ban 数（创建时由管理员设定）
    protect_count: int = 1  # 每方选图 protect 数（0 表示无 protect，如深度赛）
    ct_tag_count: int = 2  # CT 词条数上限（0=禁用；backend-ct-pick-tags §2.5）
    mappool: Mappool  # 完整图池
    player_a_id: str  # 选手 A 账号 id
    player_b_id: str  # 选手 B 账号 id
    referee_id: str  # 裁判账号 id
    director_id: str  # 导播账号 id
    status: MatchStatus = MatchStatus.CREATED
    winner: Literal["A", "B"] | None = None  # 最终胜方
    created_at: datetime = Field(default_factory=now_ts)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    paused_at: datetime | None = None  # 最近一次暂停时刻（审计/展示用）
    # 归档时刻（NULL=未归档）。纯管理端列表整理标记，与状态机正交：
    # 不影响 MatchStatus / match_log / fixture 推进 / 选手占用等任何逻辑。
    archived_at: datetime | None = None
    # 赛事归属（孤立比赛统一挂默认赛事 DEFAULT_TOURNAMENT_ID）。赛程引擎据此
    # 关联回 Fixture；存量 None 由启动 seed（ensure_default_tournament）回填。
    tournament_id: str | None = None
    fixture_id: str | None = None


# ---------------------------------------------------------------------------
# 回合与计时
# ---------------------------------------------------------------------------


class LevelTime(BaseModel):
    """单关用时上报记录。"""

    model_config = _model_config

    level_index: int  # 关卡序号（插件侧定义）
    time_ms: int  # 本关用时（毫秒）
    total_ms: int | None = None  # 当前总时长（仅多关项目）
    # 完成时刻活跃的无效原因（informational——多关仲裁归裁判人工，不影响总分）
    invalid_reasons: list[str] = Field(default_factory=list)


class Attempt(BaseModel):
    """单关项目的一次尝试。"""

    model_config = _model_config

    index: int  # 尝试序号
    status: AttemptStatus = AttemptStatus.UNFINISHED
    time_ms: int | None = None  # 成绩（毫秒）；INVALID 时保留作证据，N/A 时为 None
    invalid_reasons: list[str] = Field(default_factory=list)  # INVALID 时的证据（元素可带 "!" 前缀）


class PlayerRoundState(BaseModel):
    """单回合中某选手的实时状态（内存）。"""

    model_config = _model_config

    account_id: str
    status: PlayerStatus = PlayerStatus.IN_GAME
    current_level_index: int = 0  # 当前进度（多关：当前关卡；单关：当前尝试）
    completed_levels: list[LevelTime] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)
    final_total_ms: int | None = None  # 多关最终总时长
    forfeited: bool = False


class RoundRecord(Document):
    """一回合的持久化记录。"""

    match_id: str
    round_no: int  # 回合序号（比赛内递增）
    pick_code: str
    pick_snapshot: Pick  # 该回合选图快照
    collection_snapshot: CollectionConfig
    source: RoundSource = RoundSource.NORMAL  # 来源（正常/重赛）
    counted: bool = True  # 是否计入比分（重赛的原回合为 False）
    superseded_by: str | None = None  # 被哪个重赛回合取代
    state_a: PlayerRoundState
    state_b: PlayerRoundState
    verdict: RoundVerdict | None = None
    score_a_ms: int | None = None  # 本回合 A 最终成绩（毫秒）
    score_b_ms: int | None = None
    created_at: datetime = Field(default_factory=now_ts)
    ended_at: datetime | None = None


# ---------------------------------------------------------------------------
# 聊天与系统事件
# ---------------------------------------------------------------------------


class ChatMessage(Document):
    """一条聊天消息（用户消息或系统消息）。"""

    match_id: str
    sender_role: ChatSenderRole
    sender_id: str | None  # 系统消息为 None
    sender_name: str  # 展示名（系统消息为 "Twilight"，见 protocol.SrvSystem.sender）
    text: str
    is_system: bool = False
    ts: datetime = Field(default_factory=now_ts)


class SystemEvent(Document):
    """系统事件（阶段切换、判罚、弃权、断连等，用于审计）。"""

    match_id: str
    ts: datetime = Field(default_factory=now_ts)
    kind: str  # 事件类型标签
    payload: dict[str, Any] = Field(default_factory=dict)


class MatchLog(Document):
    """整场比赛日志视图文档（实时更新）。"""

    match_id: str
    initial_info: dict[str, Any]  # 初始信息（赛制、图池、选手、延迟配置等）
    round_ids: list[str] = Field(default_factory=list)  # 含不计分的重赛原回合
    final_result: dict[str, Any] | None = None
    # ban/pick/protect 草稿快照（裁判 draft_sync 上报后落库，可空）。
    # 结构见 backend-banpick-persist 契约：{actions, picks, bannedTags, tagBanBy}。
    draft_snapshot: dict[str, Any] | None = None
    updated_at: datetime = Field(default_factory=now_ts)


class MappoolDoc(Document):
    """图池库文档：可复用的图池定义，创建比赛时按 id 引用并内嵌进比赛。"""

    name: str  # 图池库名，如「决赛图池 v1」
    mappool: Mappool  # 复用既有 Mappool 结构（类别 → 选图）
    created_by: str  # 创建者账号 id
    created_at: datetime = Field(default_factory=now_ts)


# ---------------------------------------------------------------------------
# 赛程管理：赛季 / 赛事 / 对阵 / 排名
# ---------------------------------------------------------------------------


class TournamentStanding(BaseModel):
    """赛事单项排名（嵌入 ``Tournament.final_standings``，不单独建集合）。"""

    model_config = _model_config

    account_id: str
    rank: int
    points: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    eliminated_round: int | None = None  # 淘汰赛第几轮出局（None=冠军）
    buchholz: float | None = None  # 瑞士轮破平：对手积分和
    note: str | None = None  # 备注，如「冠军」「亚军」


# 默认赛事（孤立比赛容器）的固定主键：所有不经赛程直接创建的比赛都挂它名下。
# 不允许删除/修改/生成赛程，也永不结束（孤立比赛无 fixture，结束钩子不推进）。
DEFAULT_TOURNAMENT_ID = "default"


class Tournament(Document):
    """赛事（编排容器：赛制、参赛池、裁判组、导播组、瑞士轮积分）。

    每场实战对决由 ``Match`` 承载（含 BO/图池等单场规则，创建比赛时指定）；
    本类只负责赛程编排与排名聚合，不持有单场规则。
    """

    name: str
    format: TournamentFormat
    status: TournamentStatus = TournamentStatus.DRAFT

    # 成员池（均为 Account.id）
    participant_ids: list[str] = Field(default_factory=list)  # 参赛选手池
    seed_order: list[str] = Field(default_factory=list)  # 种子序；空=随机
    referee_ids: list[str] = Field(default_factory=list)  # 裁判候选池（组）
    director_ids: list[str] = Field(default_factory=list)  # 导播候选池（组）

    # 瑞士轮专用（淘汰赛不用）
    swiss_rounds: int | None = None  # None → ceil(log2(N))
    swiss_win_points: int = 1
    swiss_loss_points: int = 0
    swiss_draw_points: int = 0

    # 赛程元数据（生成赛程后填充）
    bracket_generated_at: datetime | None = None
    current_round: int = 0
    total_rounds: int | None = None
    winner_id: str | None = None  # 淘汰赛冠军；瑞士轮无单一冠军
    final_standings: list[TournamentStanding] | None = None  # COMPLETED 时冻结

    created_by: str
    created_at: datetime = Field(default_factory=now_ts)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class Fixture(Document):
    """赛程对阵节点（独立集合；引用一个 ``Match`` 实战）。

    与 ``Match`` 的区别：Fixture 是赛程表里的对阵结构（谁 vs 谁、哪一轮、
    晋级链），``Match`` 是该对阵的实际比赛运行载体（WS 连入）。
    """

    tournament_id: str
    round_no: int  # 赛事内轮次序（从 1 起）
    bracket_side: BracketSide = BracketSide.MAIN
    match_index: int  # 同轮同半区内的序号

    # 对阵双方（后续轮节点可能 TBD，待前置场结束填充）
    player_a_id: str | None = None
    player_b_id: str | None = None
    is_bye: bool = False  # 轮空（一方为 None，对方自动晋级）

    # 晋级链（生成赛程时确定，推进时据此填目标节点）
    advances_to: str | None = None  # 胜者晋级目标 Fixture.id
    advances_slot: Literal["A", "B"] | None = None  # 填入目标的哪个槽
    losers_drops_to: str | None = None  # 双败：败者掉入败者组目标 Fixture.id
    losers_drop_slot: Literal["A", "B"] | None = None
    depends_on: list[str] = Field(default_factory=list)  # 双方来源 Fixture.id

    # 指派与实战
    referee_id: str | None = None
    director_id: str | None = None
    match_id: str | None = None  # 关联 Match.id（未生成实战时为 None）
    winner_id: str | None = None

    status: FixtureStatus = FixtureStatus.PENDING
    created_at: datetime = Field(default_factory=now_ts)
    started_at: datetime | None = None
    completed_at: datetime | None = None
