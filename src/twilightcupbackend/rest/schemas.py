"""REST 请求/响应模型（不含口令哈希等敏感字段）。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from ..controllers import resolve_pick_logo_url

if TYPE_CHECKING:
    from ..storage import Storage

from ..datatypes import (
    Account,
    AccountType,
    BracketSide,
    Fixture,
    FixtureStatus,
    Level,
    Mappool,
    MappoolDoc,
    Match,
    MatchStatus,
    ScoringMethod,
    Tournament,
    TournamentFormat,
    TournamentStanding,
    TournamentStatus,
)

# 登录端标识 → 需具备的账号角色（auth_controller 据此校验）
LoginEndpoint = Literal["admin", "referee", "director", "player"]


class LoginRequest(BaseModel):
    username: str = Field(description="登录用户名")
    password: str = Field(description="口令明文（服务端仅存哈希）")
    endpoint: LoginEndpoint | None = Field(
        default=None,
        description="登录端（可选，旧客户端不传）：校验账号 roles 含对应角色"
        "（admin→ADMIN/referee→REFEREE/director→DIRECTOR/player→PLAYER），"
        "无该角色则 403 ENDPOINT_FORBIDDEN 且不签发令牌；不传/null 时"
        "不做角色校验，行为与旧版完全一致。",
    )


class TokenResponse(BaseModel):
    access_token: str = Field(
        description="JWT 令牌，用于 REST Bearer 与 WebSocket /ws/{token}"
    )
    token_type: str = "bearer"
    account_id: str
    username: str = Field(description="登录名（唯一）")
    account_type: AccountType = Field(description="主角色（最高优先级）")
    roles: list[AccountType] = Field(description="角色集合")
    display_name: str


class AccountCreate(BaseModel):
    username: str = Field(description="登录用户名（唯一）")
    password: str = Field(description="口令明文")
    display_name: str = Field(description="展示名，如“选手A”")
    roles: list[AccountType] = Field(
        description="角色集合（可多选）：1=选手 2=裁判 3=导播 4=管理员"
    )
    speedrun_id: str | None = Field(
        default=None,
        description="speedrun.com 账号绑定（用户名或 8 位用户 id），可空",
    )


class AccountOut(BaseModel):
    id: str
    username: str
    roles: list[AccountType]
    display_name: str
    speedrun_id: str | None = None
    created_at: datetime

    @classmethod
    def from_account(cls, account: Account) -> AccountOut:
        return cls(
            id=account.id,
            username=account.username,
            roles=account.roles,
            display_name=account.display_name,
            speedrun_id=account.speedrun_id,
            created_at=account.created_at,
        )


class MatchCreate(BaseModel):
    name: str = Field(description="比赛名称")
    bo_format: int = Field(ge=1, description="赛制 BO 数，如 9 表示 BO9")
    win_threshold: int | None = Field(
        default=None, ge=1, description="取胜所需分数；省略时按 (bo//2)+1 推导（BO9→5）"
    )
    scoring_method: ScoringMethod = Field(
        description="单关项目成绩计算方式：1=最快 2=平均（仅对单关项目生效）"
    )
    start_countdown_delay: int = Field(
        default=5, ge=0, description="回合开始前的倒数秒数，默认 5"
    )
    ban_count: int = Field(default=1, ge=0, description="每方选图 ban 数，默认 1")
    protect_count: int = Field(
        default=1,
        ge=0,
        description="每方选图 protect 数（0=无 protect，如深度赛），默认 1",
    )
    ct_tag_count: int = Field(
        default=2,
        ge=0,
        le=4,
        description="CT 词条数上限（0=本场禁用词条），默认 2",
    )
    mappool: Mappool | None = Field(
        default=None, description="内联完整图池（与 mappool_id 二选一）"
    )
    mappool_id: str | None = Field(
        default=None, description="引用图池库中的图池 id（优先于内联 mappool）"
    )
    player_a: str = Field(description="选手 A 用户名")
    player_b: str = Field(description="选手 B 用户名")
    referee: str = Field(description="裁判用户名")
    director: str = Field(description="导播用户名")


class MatchUpdate(BaseModel):
    """局部更新已有会话（§7.1）：选手用户名 / 状态 / 名称。

    改选手或切到 RUNNING 时同样跑跨会话占用冲突校验（§5）。
    """

    player_a: str | None = Field(default=None, description="选手 A 用户名")
    player_b: str | None = Field(default=None, description="选手 B 用户名")
    status: MatchStatus | None = Field(default=None, description="目标状态枚举值")
    name: str | None = Field(default=None, description="比赛名称")


class MatchOut(BaseModel):
    id: str
    name: str
    bo_format: int
    win_threshold: int
    scoring_method: ScoringMethod
    start_countdown_delay: int
    ban_count: int
    protect_count: int
    ct_tag_count: int
    status: MatchStatus
    mappool: Mappool
    player_a_id: str
    player_b_id: str
    player_a_username: str = ""
    player_b_username: str = ""
    # 双方选手的 speedrun.com 账号绑定（导播 categoryinfo 场景高亮用；未绑定为 None）
    player_a_speedrun: str | None = None
    player_b_speedrun: str | None = None
    referee_id: str
    director_id: str
    winner: str | None
    tournament_id: str | None
    fixture_id: str | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    archived_at: datetime | None = None  # 归档时刻（null=未归档，仅列表整理用）

    @classmethod
    def from_match(
        cls, session: Match, db: Any = None, storage: Storage | None = None
    ) -> MatchOut:
        """构造 MatchOut。

        db 可选：传入时附带解析双方 username 与 speedrun 绑定（裁判/导播端展示用），
        并作为选图展示图按关卡回退的数据源（无 db 时仅签 pick 自有 logo）；
        storage 可选：传入时给 mappool 每个 Pick 的 logo 签 logo_url。
        """
        a_username = ""
        b_username = ""
        a_speedrun = None
        b_speedrun = None
        if db is not None:
            a = db.accounts.get(session.player_a_id)
            b = db.accounts.get(session.player_b_id)
            a_username = a.username if a is not None else ""
            b_username = b.username if b is not None else ""
            a_speedrun = a.speedrun_id if a is not None else None
            b_speedrun = b.speedrun_id if b is not None else None
        mappool = session.mappool
        if storage is not None:
            for pick in mappool.all_picks():
                pick.logo_url = resolve_pick_logo_url(pick, db, storage)
        return cls(
            id=session.id,
            name=session.name,
            bo_format=session.bo_format,
            win_threshold=session.win_threshold,
            scoring_method=session.scoring_method,
            start_countdown_delay=session.start_countdown_delay,
            ban_count=session.ban_count,
            protect_count=session.protect_count,
            ct_tag_count=session.ct_tag_count,
            status=session.status,
            mappool=mappool,
            player_a_id=session.player_a_id,
            player_b_id=session.player_b_id,
            player_a_username=a_username,
            player_b_username=b_username,
            player_a_speedrun=a_speedrun,
            player_b_speedrun=b_speedrun,
            referee_id=session.referee_id,
            director_id=session.director_id,
            winner=session.winner,
            tournament_id=session.tournament_id,
            fixture_id=session.fixture_id,
            created_at=session.created_at,
            started_at=session.started_at,
            ended_at=session.ended_at,
            archived_at=session.archived_at,
        )


class MappoolCreate(BaseModel):
    name: str = Field(description="图池库名，如「决赛图池 v1」")
    mappool: Mappool = Field(description="完整图池：类别→选图")


class MappoolUpdate(BaseModel):
    name: str | None = None
    mappool: Mappool | None = None


class MappoolOut(BaseModel):
    id: str
    name: str
    mappool: Mappool
    created_by: str
    created_at: datetime

    @classmethod
    def from_doc(
        cls,
        doc: MappoolDoc,
        storage: Storage | None = None,
        db: Any = None,
    ) -> MappoolOut:
        """构造输出；若传入 storage，给每个 Pick 签 logo_url（db 传入时支持按
        合集关卡回退 Level.logo，见 controllers.resolve_pick_logo_url）。"""
        mappool = doc.mappool
        if storage is not None:
            for pick in mappool.all_picks():
                pick.logo_url = resolve_pick_logo_url(pick, db, storage)
        return cls(
            id=doc.id,
            name=doc.name,
            mappool=mappool,
            created_by=doc.created_by,
            created_at=doc.created_at,
        )


class MatchSummary(BaseModel):
    """比赛摘要（列表用，不含图池等重字段；展示名由控制器解析后注入）。"""

    id: str
    name: str
    bo_format: int
    win_threshold: int
    status: MatchStatus
    player_a_name: str
    player_b_name: str
    referee_name: str
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None


# ---------------------------------------------------------------------------
# 赛程管理：赛事
# ---------------------------------------------------------------------------


class TournamentCreate(BaseModel):
    name: str = Field(description="赛事名称")
    format: TournamentFormat = Field(description="赛制：1=单败淘汰 2=双败淘汰 3=瑞士轮")
    swiss_rounds: int | None = Field(
        default=None, ge=1, description="瑞士轮轮数；仅瑞士轮用，省略按 ceil(log2(N))"
    )
    swiss_win_points: int = Field(default=1, ge=0, description="瑞士轮胜场积分")
    swiss_loss_points: int = Field(default=0, ge=0, description="瑞士轮负场积分")
    swiss_draw_points: int = Field(default=0, ge=0, description="瑞士轮平场积分")


class TournamentUpdate(BaseModel):
    """仅 DRAFT 状态赛事可改核心字段（控制器校验）；不允许改 format。"""

    name: str | None = None
    swiss_rounds: int | None = Field(default=None, ge=1)
    swiss_win_points: int | None = Field(default=None, ge=0)
    swiss_loss_points: int | None = Field(default=None, ge=0)
    swiss_draw_points: int | None = Field(default=None, ge=0)


class TournamentOut(BaseModel):
    id: str
    name: str
    format: TournamentFormat
    status: TournamentStatus
    participant_ids: list[str]
    seed_order: list[str]
    referee_ids: list[str]
    director_ids: list[str]
    swiss_rounds: int | None
    swiss_win_points: int
    swiss_loss_points: int
    swiss_draw_points: int
    bracket_generated_at: datetime | None
    current_round: int
    total_rounds: int | None
    winner_id: str | None
    final_standings: list[TournamentStanding] | None
    created_by: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    @classmethod
    def from_tournament(cls, t: Tournament) -> TournamentOut:
        return cls(
            id=t.id,
            name=t.name,
            format=t.format,
            status=t.status,
            participant_ids=t.participant_ids,
            seed_order=t.seed_order,
            referee_ids=t.referee_ids,
            director_ids=t.director_ids,
            swiss_rounds=t.swiss_rounds,
            swiss_win_points=t.swiss_win_points,
            swiss_loss_points=t.swiss_loss_points,
            swiss_draw_points=t.swiss_draw_points,
            bracket_generated_at=t.bracket_generated_at,
            current_round=t.current_round,
            total_rounds=t.total_rounds,
            winner_id=t.winner_id,
            final_standings=t.final_standings,
            created_by=t.created_by,
            created_at=t.created_at,
            started_at=t.started_at,
            completed_at=t.completed_at,
        )


class UsernamesBody(BaseModel):
    """批量按用户名增删成员（选手/裁判/导播通用）。"""

    usernames: list[str] = Field(description="账号用户名列表")


class SeedOrderBody(BaseModel):
    """设置种子序（生成赛程时决定对阵排位）。"""

    seed_order: list[str] = Field(
        description="种子序（账号 id 列表）；长度须等于参赛选手数"
    )


# ---------------------------------------------------------------------------
# 赛程管理：对阵 / 对阵表 / 排名输出
# ---------------------------------------------------------------------------


class FixtureOut(BaseModel):
    id: str
    tournament_id: str
    round_no: int
    bracket_side: BracketSide
    match_index: int
    player_a_id: str | None
    player_b_id: str | None
    player_a_name: str | None = None  # 展示名（BracketView.build 解析注入）
    player_b_name: str | None = None
    is_bye: bool
    advances_to: str | None
    advances_slot: str | None
    losers_drops_to: str | None
    losers_drop_slot: str | None
    depends_on: list[str]
    referee_id: str | None
    director_id: str | None
    match_id: str | None
    winner_id: str | None
    score_a: int | None = None  # 已结束对阵的累计比分（从 match_log 填）
    score_b: int | None = None
    status: FixtureStatus
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    @classmethod
    def from_fixture(cls, f: Fixture) -> FixtureOut:
        return cls(
            id=f.id,
            tournament_id=f.tournament_id,
            round_no=f.round_no,
            bracket_side=f.bracket_side,
            match_index=f.match_index,
            player_a_id=f.player_a_id,
            player_b_id=f.player_b_id,
            is_bye=f.is_bye,
            advances_to=f.advances_to,
            advances_slot=f.advances_slot,
            losers_drops_to=f.losers_drops_to,
            losers_drop_slot=f.losers_drop_slot,
            depends_on=f.depends_on,
            referee_id=f.referee_id,
            director_id=f.director_id,
            match_id=f.match_id,
            winner_id=f.winner_id,
            status=f.status,
            created_at=f.created_at,
            started_at=f.started_at,
            completed_at=f.completed_at,
        )


class BracketRound(BaseModel):
    round_no: int
    bracket_side: BracketSide
    fixtures: list[FixtureOut]


class BracketView(BaseModel):
    tournament_id: str
    format: TournamentFormat
    current_round: int
    total_rounds: int | None
    rounds: list[BracketRound]

    @classmethod
    def build(cls, db: Any, tournament_id: str) -> BracketView:
        """从 db 构建对阵视图：查 fixtures + 已结束对阵的比分（match_log）+ 选手名。"""
        t = db.tournaments.get(tournament_id)
        if t is None:
            raise KeyError(tournament_id)
        fixtures = db.fixtures.find_by_tournament(tournament_id)
        # 预读所有相关 match_log 的比分（按 match_id 索引）
        scores: dict[str, tuple[int, int]] = {}
        for f in fixtures:
            if f.match_id and f.status == FixtureStatus.COMPLETED and not f.is_bye:
                ml = db.match_logs.get_by_match(f.match_id)
                fr = getattr(ml, "final_result", None) if ml else None
                if isinstance(fr, dict) and "wins_a" in fr and "wins_b" in fr:
                    scores[f.match_id] = (int(fr["wins_a"]), int(fr["wins_b"]))
        # 预读所有选手的 display_name（按 account_id 索引，避免 N+1）
        name_ids = {
            aid for f in fixtures for aid in (f.player_a_id, f.player_b_id) if aid
        }
        names: dict[str, str] = {}
        for aid in name_ids:
            acc = db.accounts.get(aid)
            if acc is not None:
                names[aid] = acc.display_name
        rounds_map: dict[int, list[Fixture]] = {}
        for f in fixtures:
            rounds_map.setdefault(f.round_no, []).append(f)
        rounds = [
            BracketRound(
                round_no=r_no,
                bracket_side=sorted(fs, key=lambda x: x.match_index)[0].bracket_side,
                fixtures=[
                    cls._fixture_out(f, scores, names)
                    for f in sorted(fs, key=lambda x: x.match_index)
                ],
            )
            for r_no, fs in sorted(rounds_map.items())
        ]
        return cls(
            tournament_id=tournament_id,
            format=t.format,
            current_round=t.current_round,
            total_rounds=t.total_rounds,
            rounds=rounds,
        )

    @staticmethod
    def _fixture_out(
        f: Fixture, scores: dict[str, tuple[int, int]], names: dict[str, str]
    ) -> FixtureOut:
        out = FixtureOut.from_fixture(f)
        if f.match_id and f.match_id in scores:
            out.score_a, out.score_b = scores[f.match_id]
        out.player_a_name = names.get(f.player_a_id) if f.player_a_id else None
        out.player_b_name = names.get(f.player_b_id) if f.player_b_id else None
        return out


class FixtureAssignBody(BaseModel):
    """为对阵指派裁判 / 导播（用户名；须在赛事裁判组 / 导播组内）。"""

    referee: str | None = Field(default=None, description="裁判用户名")
    director: str | None = Field(default=None, description="导播用户名")


class FixtureCreateMatchBody(BaseModel):
    """为对阵生成比赛时传入的单场规则 + 图池（赛事不持有这些）。"""

    bo_format: int = Field(ge=1, description="本场 BO 数，如 9 表示 BO9")
    win_threshold: int | None = Field(
        default=None, ge=1, description="取胜所需分数；省略按 (bo//2)+1"
    )
    scoring_method: ScoringMethod = Field(description="单关成绩计算方式：1=最快 2=平均")
    start_countdown_delay: int = Field(
        default=5, ge=0, description="回合开始前倒数秒数"
    )
    ban_count: int = Field(default=1, ge=0, description="每方 ban 数")
    protect_count: int = Field(default=1, ge=0, description="每方 protect 数（0=无）")
    ct_tag_count: int = Field(
        default=2, ge=0, le=4, description="CT 词条数上限（0=禁用词条）"
    )
    mappool_id: str = Field(description="引用图池库 id（冻结为快照内嵌本场）")


class TournamentStandingOut(BaseModel):
    """赛事排名项（含展示名，由控制器注入）。"""

    account_id: str
    display_name: str
    rank: int
    points: int
    wins: int
    losses: int
    draws: int
    eliminated_round: int | None
    buchholz: float | None
    note: str | None


# ---------------------------------------------------------------------------
# 关卡管理
# ---------------------------------------------------------------------------


class LevelCreate(BaseModel):
    name: str = Field(description="关卡标识（唯一，如 Intro）")
    display_name: str = Field(default="", description="展示名；空则同 name")
    logo: str | None = Field(default=None, description="展示图 minio object key")


class LevelUpdate(BaseModel):
    display_name: str | None = None
    logo: str | None = None


class LevelOut(BaseModel):
    id: str
    name: str
    display_name: str
    logo: str | None
    logo_url: str | None
    created_at: datetime

    @classmethod
    def from_level(cls, level: Level, storage: Storage | None = None) -> LevelOut:
        return cls(
            id=level.id,
            name=level.name,
            display_name=level.display_name,
            logo=level.logo,
            logo_url=storage.public_url(level.logo) if storage else None,
            created_at=level.created_at,
        )
