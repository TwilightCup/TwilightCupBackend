"""赛事管理控制器（管理员）：创建/查询/修改赛事 + 成员池（选手/裁判组/导播组）管理。

赛事属某赛季（须 ACTIVE），配置赛制、规则模板、图池快照。
赛程生成/推进/排名等端点在 M12（TournamentEngine 接入后）补充。
成员管理仅 DRAFT 状态可用（赛程生成后不得再改成员，以免破坏对阵表）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from classy_fastapi import Routable, delete, get, patch, post
from fastapi import Depends, HTTPException, status

from ..auth import require_admin
from ..controllers import DBController
from ..datatypes import (
    DEFAULT_TOURNAMENT_ID,
    Account,
    AccountType,
    Fixture,
    Tournament,
    TournamentFormat,
    TournamentStatus,
)
from .schemas import (
    BracketView,
    FixtureAssignBody,
    FixtureCreateMatchBody,
    FixtureOut,
    MatchOut,
    SeedOrderBody,
    TournamentCreate,
    TournamentOut,
    TournamentStandingOut,
    TournamentUpdate,
    UsernamesBody,
)

if TYPE_CHECKING:
    from ..connection_manager import ConnectionManager
    from ..storage import Storage
    from ..tournament_engine import TournamentEngine

_RESP_AUTH: dict[int | str, dict[str, Any]] = {
    401: {"description": "未携带有效令牌"},
    403: {"description": "需要管理员权限"},
}


class TournamentController(Routable):
    def __init__(
        self,
        db: DBController,
        cm: ConnectionManager | None = None,
        storage: Storage | None = None,
    ) -> None:
        super().__init__(prefix="/admin/tournaments", tags=["tournaments"])
        self.db = db
        self.cm = cm
        self.storage = storage

    # ------------------------------------------------------------------ CRUD

    @post(
        "",
        response_model=TournamentOut,
        status_code=status.HTTP_201_CREATED,
        summary="创建赛事",
        description="创建赛事（编排容器：赛制 + 参赛池 + 裁判组 + 导播组 + "
        "瑞士轮积分）。单场规则（BO/图池等）在生成每场比赛时指定，赛事不持有。",
        responses=_RESP_AUTH,
    )
    def create(
        self,
        body: TournamentCreate,
        admin: Account = Depends(require_admin),
    ) -> TournamentOut:
        tournament = Tournament(
            name=body.name,
            format=body.format,
            swiss_rounds=body.swiss_rounds,
            swiss_win_points=body.swiss_win_points,
            swiss_loss_points=body.swiss_loss_points,
            swiss_draw_points=body.swiss_draw_points,
            created_by=admin.id,
        )
        self.db.tournaments.insert(tournament)
        return TournamentOut.from_tournament(tournament)

    @get(
        "",
        response_model=list[TournamentOut],
        summary="赛事列表",
        description="返回全部赛事（按创建时间倒序）。",
        responses=_RESP_AUTH,
    )
    def list_all(
        self,
        _: Account = Depends(require_admin),
    ) -> list[TournamentOut]:
        tournaments = self.db.tournaments.find()
        tournaments.sort(key=lambda t: t.created_at, reverse=True)
        return [TournamentOut.from_tournament(t) for t in tournaments]

    @get(
        "/{tournament_id}",
        response_model=TournamentOut,
        summary="查询赛事详情",
        responses={**_RESP_AUTH, 404: {"description": "赛事不存在"}},
    )
    def get_one(
        self,
        tournament_id: str,
        _: Account = Depends(require_admin),
    ) -> TournamentOut:
        return TournamentOut.from_tournament(self._require(tournament_id))

    @patch(
        "/{tournament_id}",
        response_model=TournamentOut,
        summary="修改赛事核心字段",
        description="按字段局部更新（仅 DRAFT 状态）。不允许改 format；"
        "改 mappool_id 会同步刷新图池快照。",
        responses={
            **_RESP_AUTH,
            404: {"description": "赛事/图池不存在"},
            400: {"description": "非 DRAFT 状态 / 默认赛事 / 分数非法"},
        },
    )
    def update(
        self,
        tournament_id: str,
        body: TournamentUpdate,
        _: Account = Depends(require_admin),
    ) -> TournamentOut:
        t = self._require(tournament_id)
        self._reject_default(t)
        if t.status != TournamentStatus.DRAFT:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "仅 DRAFT 状态赛事可修改核心字段"
            )
        if body.name is not None:
            t.name = body.name
        if body.swiss_rounds is not None:
            t.swiss_rounds = body.swiss_rounds
        if body.swiss_win_points is not None:
            t.swiss_win_points = body.swiss_win_points
        if body.swiss_loss_points is not None:
            t.swiss_loss_points = body.swiss_loss_points
        if body.swiss_draw_points is not None:
            t.swiss_draw_points = body.swiss_draw_points
        self.db.tournaments.replace(t)
        return TournamentOut.from_tournament(t)

    @delete(
        "/{tournament_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="删除赛事",
        description="仅 DRAFT 状态赛事可删除（已生成赛程的不可删，需先取消）；"
        "默认赛事（孤立比赛容器）不可删除。",
        responses={
            **_RESP_AUTH,
            404: {"description": "赛事不存在"},
            400: {"description": "非 DRAFT 状态 / 默认赛事"},
        },
    )
    def remove(
        self,
        tournament_id: str,
        _: Account = Depends(require_admin),
    ) -> None:
        t = self._require(tournament_id)
        self._reject_default(t)
        if t.status != TournamentStatus.DRAFT:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "仅 DRAFT 状态赛事可删除")
        self.db.tournaments.delete(tournament_id)

    # -------------------------------------------------------- 成员池管理

    @post(
        "/{tournament_id}/participants",
        response_model=TournamentOut,
        summary="加入参赛选手",
        description="按用户名批量追加选手到参赛池（校验 PLAYER 角色）；已存在则跳过。"
        "仅 DRAFT 状态可用。",
        responses={
            **_RESP_AUTH,
            404: {"description": "赛事不存在"},
            400: {"description": "非 DRAFT 状态 / 默认赛事 / 账号不存在或角色不符"},
        },
    )
    def add_participants(
        self,
        tournament_id: str,
        body: UsernamesBody,
        _: Account = Depends(require_admin),
    ) -> TournamentOut:
        t = self._require_draft(tournament_id)
        self._reject_default(t)
        for aid in self._resolve_usernames(body.usernames, AccountType.PLAYER, "选手"):
            if aid not in t.participant_ids:
                t.participant_ids.append(aid)
        self.db.tournaments.replace(t)
        return TournamentOut.from_tournament(t)

    @post(
        "/{tournament_id}/participants/remove",
        response_model=TournamentOut,
        summary="移除参赛选手",
        description="按用户名批量从参赛池移除（同步清理种子序）。仅 DRAFT 状态可用。",
        responses={
            **_RESP_AUTH,
            404: {"description": "赛事不存在"},
            400: {"description": "非 DRAFT 状态 / 默认赛事 / 账号不存在或角色不符"},
        },
    )
    def remove_participants(
        self,
        tournament_id: str,
        body: UsernamesBody,
        _: Account = Depends(require_admin),
    ) -> TournamentOut:
        t = self._require_draft(tournament_id)
        self._reject_default(t)
        rm = set(self._resolve_usernames(body.usernames, AccountType.PLAYER, "选手"))
        t.participant_ids = [p for p in t.participant_ids if p not in rm]
        t.seed_order = [s for s in t.seed_order if s not in rm]
        self.db.tournaments.replace(t)
        return TournamentOut.from_tournament(t)

    @post(
        "/{tournament_id}/referees",
        response_model=TournamentOut,
        summary="加入裁判组",
        description="按用户名批量追加裁判到裁判组（候选池，校验 REFEREE 角色）。"
        "仅 DRAFT 状态可用。",
        responses={
            **_RESP_AUTH,
            404: {"description": "赛事不存在"},
            400: {"description": "非 DRAFT 状态 / 默认赛事 / 账号不存在或角色不符"},
        },
    )
    def add_referees(
        self,
        tournament_id: str,
        body: UsernamesBody,
        _: Account = Depends(require_admin),
    ) -> TournamentOut:
        t = self._require_draft(tournament_id)
        self._reject_default(t)
        for aid in self._resolve_usernames(body.usernames, AccountType.REFEREE, "裁判"):
            if aid not in t.referee_ids:
                t.referee_ids.append(aid)
        self.db.tournaments.replace(t)
        return TournamentOut.from_tournament(t)

    @post(
        "/{tournament_id}/directors",
        response_model=TournamentOut,
        summary="加入导播组",
        description="按用户名批量追加导播到导播组（候选池，校验 DIRECTOR 角色）。"
        "仅 DRAFT 状态可用。",
        responses={
            **_RESP_AUTH,
            404: {"description": "赛事不存在"},
            400: {"description": "非 DRAFT 状态 / 默认赛事 / 账号不存在或角色不符"},
        },
    )
    def add_directors(
        self,
        tournament_id: str,
        body: UsernamesBody,
        _: Account = Depends(require_admin),
    ) -> TournamentOut:
        t = self._require_draft(tournament_id)
        self._reject_default(t)
        for aid in self._resolve_usernames(
            body.usernames, AccountType.DIRECTOR, "导播"
        ):
            if aid not in t.director_ids:
                t.director_ids.append(aid)
        self.db.tournaments.replace(t)
        return TournamentOut.from_tournament(t)

    @post(
        "/{tournament_id}/seeds",
        response_model=TournamentOut,
        summary="设置种子序",
        description="设置参赛选手的种子排位（生成赛程时决定对阵播位）。"
        "长度须等于参赛选手数、且每个 id 都在池中、无重复。仅 DRAFT 状态可用。",
        responses={
            **_RESP_AUTH,
            404: {"description": "赛事不存在"},
            400: {
                "description": "非 DRAFT 状态 / 默认赛事 / 长度不符 / 非池内 id / 重复"
            },
        },
    )
    def set_seeds(
        self,
        tournament_id: str,
        body: SeedOrderBody,
        _: Account = Depends(require_admin),
    ) -> TournamentOut:
        t = self._require_draft(tournament_id)
        self._reject_default(t)
        if len(body.seed_order) != len(t.participant_ids):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "种子序长度须等于参赛选手数"
            )
        pset = set(t.participant_ids)
        for sid in body.seed_order:
            if sid not in pset:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"种子 {sid} 不在参赛选手池中"
                )
        if len(set(body.seed_order)) != len(body.seed_order):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "种子序存在重复")
        t.seed_order = list(body.seed_order)
        self.db.tournaments.replace(t)
        return TournamentOut.from_tournament(t)

    # ============================================================ 赛程操作

    @post(
        "/{tournament_id}/generate-bracket",
        response_model=BracketView,
        summary="生成对阵表",
        description="按赛制生成对阵表（M12 仅单败淘汰；双败/瑞士轮在 M13）。"
        "要求 DRAFT 状态、至少 2 名选手；生成后赛事进入 IN_PROGRESS。",
        responses={
            **_RESP_AUTH,
            404: {"description": "赛事不存在"},
            400: {"description": "状态不符 / 默认赛事 / 选手不足 / 该赛制暂未实现"},
            503: {"description": "赛程引擎未就绪"},
        },
    )
    def generate_bracket(
        self,
        tournament_id: str,
        _: Account = Depends(require_admin),
    ) -> BracketView:
        t = self._require(tournament_id)
        self._reject_default(t)
        if t.status != TournamentStatus.DRAFT:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "仅 DRAFT 状态赛事可生成赛程"
            )
        if len(t.participant_ids) < 2:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少需 2 名参赛选手")
        try:
            self._engine().generate(t)
        except NotImplementedError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return self._bracket_view(tournament_id)

    @post(
        "/{tournament_id}/next-round",
        response_model=BracketView,
        summary="生成下一轮（瑞士轮专用）",
        description="瑞士轮按当前积分荷兰式配对生成下一轮（避免重赛）；"
        "淘汰赛返回 400。要求上一轮已全部完成。",
        responses={
            **_RESP_AUTH,
            404: {"description": "赛事不存在"},
            400: {"description": "非瑞士轮 / 默认赛事 / 上一轮未完成 / 超过总轮数"},
        },
    )
    def start_next_round(
        self,
        tournament_id: str,
        _: Account = Depends(require_admin),
    ) -> BracketView:
        t = self._require(tournament_id)
        self._reject_default(t)
        if t.format != TournamentFormat.SWISS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "仅瑞士轮赛制支持生成下一轮"
            )
        try:
            self._engine().pair_swiss_round(t, t.current_round + 1)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return self._bracket_view(tournament_id)

    @get(
        "/{tournament_id}/bracket",
        response_model=BracketView,
        summary="查看对阵表",
        responses={**_RESP_AUTH, 404: {"description": "赛事不存在"}},
    )
    def get_bracket(
        self,
        tournament_id: str,
        _: Account = Depends(require_admin),
    ) -> BracketView:
        self._require(tournament_id)
        return self._bracket_view(tournament_id)

    @get(
        "/{tournament_id}/standings",
        response_model=list[TournamentStandingOut],
        summary="查看排名",
        description="返回当前排名（单败：冠军 rank 1，其余按被淘汰轮次）。"
        "display_name 由控制器注入。",
        responses={**_RESP_AUTH, 404: {"description": "赛事不存在"}},
    )
    def get_standings(
        self,
        tournament_id: str,
        _: Account = Depends(require_admin),
    ) -> list[TournamentStandingOut]:
        t = self._require(tournament_id)
        standings = self._engine().compute_standings(t)

        def name_of(acc_id: str) -> str:
            a = self.db.accounts.get(acc_id)
            return a.display_name if a else "—"

        return [
            TournamentStandingOut(**s.model_dump(), display_name=name_of(s.account_id))
            for s in standings
        ]

    @get(
        "/{tournament_id}/fixtures/{fixture_id}",
        response_model=FixtureOut,
        summary="查询单个对阵",
        responses={**_RESP_AUTH, 404: {"description": "赛事/对阵不存在"}},
    )
    def get_fixture(
        self,
        tournament_id: str,
        fixture_id: str,
        _: Account = Depends(require_admin),
    ) -> FixtureOut:
        return FixtureOut.from_fixture(self._require_fixture(tournament_id, fixture_id))

    @post(
        "/{tournament_id}/fixtures/{fixture_id}/assign",
        response_model=FixtureOut,
        summary="为对阵指派裁判/导播",
        description="从赛事裁判组/导播组里为该对阵指派裁判与（可选）导播（按用户名）。",
        responses={
            **_RESP_AUTH,
            404: {"description": "赛事/对阵不存在"},
            400: {"description": "账号不存在 / 角色不符 / 不在赛事组内"},
        },
    )
    def assign_officials(
        self,
        tournament_id: str,
        fixture_id: str,
        body: FixtureAssignBody,
        _: Account = Depends(require_admin),
    ) -> FixtureOut:
        t = self._require(tournament_id)
        fixture = self._require_fixture(tournament_id, fixture_id)
        ref_id: str | None = None
        dir_id: str | None = None
        if body.referee is not None:
            ref = self._resolve_one(body.referee, AccountType.REFEREE, "裁判")
            if ref.id not in t.referee_ids:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "该裁判不在赛事裁判组")
            ref_id = ref.id
        if body.director is not None:
            dri = self._resolve_one(body.director, AccountType.DIRECTOR, "导播")
            if dri.id not in t.director_ids:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "该导播不在赛事导播组")
            dir_id = dri.id
        fixture = self._engine().assign_officials(fixture, ref_id, dir_id)
        return FixtureOut.from_fixture(fixture)

    @post(
        "/{tournament_id}/fixtures/{fixture_id}/create-match",
        response_model=MatchOut,
        status_code=status.HTTP_201_CREATED,
        summary="为对阵生成实战比赛",
        description="把对阵节点实例化为 Match（须已指派裁判、双方选手已定）。"
        "生成比赛带 tournament_id/fixture_id，单场打完自动推进对阵。",
        responses={
            **_RESP_AUTH,
            404: {"description": "赛事/对阵不存在"},
            400: {"description": "未指派裁判 / 双方未定 / 已生成过比赛"},
        },
    )
    def create_match_for_fixture(
        self,
        tournament_id: str,
        fixture_id: str,
        body: FixtureCreateMatchBody,
        _: Account = Depends(require_admin),
    ) -> MatchOut:
        self._require(tournament_id)
        fixture = self._require_fixture(tournament_id, fixture_id)
        if fixture.match_id is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "该对阵已生成实战比赛")
        mappool_doc = self.db.mappools.get(body.mappool_id)
        if mappool_doc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "图池不存在")
        try:
            match = self._engine().materialize_match(fixture, body, mappool_doc.mappool)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return MatchOut.from_match(match, None, self.storage)

    # ------------------------------------------------------------ 辅助

    def _require(self, tournament_id: str) -> Tournament:
        t = self.db.tournaments.get(tournament_id)
        if t is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "赛事不存在")
        return t

    def _require_draft(self, tournament_id: str) -> Tournament:
        t = self._require(tournament_id)
        if t.status != TournamentStatus.DRAFT:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "仅 DRAFT 状态赛事可修改成员"
            )
        return t

    @staticmethod
    def _reject_default(t: Tournament) -> None:
        """默认赛事是孤立比赛的永久容器：一切变更操作（改字段/删/成员/赛程）禁入。"""
        if t.id == DEFAULT_TOURNAMENT_ID:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "默认赛事不允许该操作")

    def _resolve_usernames(
        self, usernames: list[str], expected: AccountType, label: str
    ) -> list[str]:
        """把用户名列表解析为 Account.id 列表，校验角色。"""
        ids: list[str] = []
        for uname in usernames:
            acc = self.db.accounts.get_by_username(uname)
            if acc is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"{label}账号不存在：{uname}"
                )
            if expected not in acc.roles:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"{label}账号不含所需角色（期望 {expected.name}）：{uname}",
                )
            ids.append(acc.id)
        return ids

    def _engine(self) -> TournamentEngine:
        if self.cm is None or self.cm.tournament_engine is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "赛程引擎未就绪")
        return self.cm.tournament_engine

    def _require_fixture(self, tournament_id: str, fixture_id: str) -> Fixture:
        fixture = self.db.fixtures.get(fixture_id)
        if fixture is None or fixture.tournament_id != tournament_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "对阵不存在")
        return fixture

    def _resolve_one(self, username: str, expected: AccountType, label: str) -> Account:
        acc = self.db.accounts.get_by_username(username)
        if acc is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"{label}账号不存在：{username}"
            )
        if expected not in acc.roles:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{label}账号不含所需角色（期望 {expected.name}）：{username}",
            )
        return acc

    def _bracket_view(self, tournament_id: str) -> BracketView:
        try:
            return BracketView.build(self.db, tournament_id)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "赛事不存在") from None
