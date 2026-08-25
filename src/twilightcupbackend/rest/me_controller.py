"""当前账号相关：列出本人参与的比赛、查询比赛详情、开始/暂停/恢复比赛（裁判）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from classy_fastapi import Routable, get, patch, post
from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth import get_current_account, hash_password, verify_password
from ..controllers import DBController, player_running_conflict
from ..datatypes import (
    DEFAULT_TOURNAMENT_ID,
    Account,
    AccountType,
    MatchStatus,
    Tournament,
    TournamentStatus,
    now_ts,
)
from ..storage import Storage
from .schemas import (
    AccountOut,
    BracketView,
    MappoolOut,
    MatchOut,
    MatchSummary,
    TournamentOut,
)

if TYPE_CHECKING:
    from ..connection_manager import ConnectionManager


class MeController(Routable):
    def __init__(
        self,
        db: DBController,
        cm: ConnectionManager | None = None,
        storage: Storage | None = None,
    ) -> None:
        super().__init__(prefix="/me", tags=["me"])
        self.db = db
        self.cm = cm
        self.storage = storage

    @staticmethod
    def _is_referee_or_admin(match, account: Account) -> bool:  # type: ignore[no-untyped-def]
        return match.referee_id == account.id or AccountType.ADMIN in account.roles

    @staticmethod
    def _is_member(match, account_id: str) -> bool:  # type: ignore[no-untyped-def]
        return account_id in {
            match.player_a_id,
            match.player_b_id,
            match.referee_id,
            match.director_id,
        }

    @get(
        "/matches",
        response_model=list[MatchSummary],
        summary="我参与的比赛",
        description="返回当前账号作为选手/裁判/导播参与的比赛；非结束的优先，创建时间倒序。"
        "已归档（archived_at 非空）的比赛不再下发。",
    )
    def matches(
        self, account: Account = Depends(get_current_account)
    ) -> list[MatchSummary]:
        matches = self.db.matches.find_by_member(account.id)
        # 已归档比赛为管理端列表整理收纳，成员端无需再看到
        matches = [s for s in matches if s.archived_at is None]
        matches.sort(
            key=lambda s: (s.status == MatchStatus.ENDED, -s.created_at.timestamp())
        )

        def name_of(acc_id: str) -> str:
            a = self.db.accounts.get(acc_id)
            return a.display_name if a else "—"

        return [
            MatchSummary(
                id=s.id,
                name=s.name,
                bo_format=s.bo_format,
                win_threshold=s.win_threshold,
                status=s.status,
                player_a_name=name_of(s.player_a_id),
                player_b_name=name_of(s.player_b_id),
                referee_name=name_of(s.referee_id),
                created_at=s.created_at,
                started_at=s.started_at,
                ended_at=s.ended_at,
            )
            for s in matches
        ]

    @get(
        "/matches/{match_id}",
        response_model=MatchOut,
        summary="查询比赛详情（含图池）",
        description="返回当前账号参与的某个比赛的完整信息（含结构化图池，"
        "供裁判端 ban/pick 载入）。仅比赛成员可访问。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "非该比赛成员"},
            404: {"description": "比赛不存在"},
        },
    )
    def match_detail(
        self,
        match_id: str,
        account: Account = Depends(get_current_account),
    ) -> MatchOut:
        match = self.db.matches.get(match_id)
        if match is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "比赛不存在")
        if not self._is_member(match, account.id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "非该比赛成员")
        return MatchOut.from_match(match, self.db, self.storage)

    @post(
        "/matches/{match_id}/start",
        response_model=MatchOut,
        summary="开始比赛（裁判激活）",
        description="裁判把比赛从 CREATED 激活为 RUNNING（写 started_at），"
        "随后选手可连入房间摇点。仅该场比赛的裁判（或管理员）可调用；"
        "已 RUNNING 幂等返回；PAUSED 须走 resume；激活时校验双方选手不在另一场 "
        "RUNNING 比赛中（冲突 409）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "非该场裁判/管理员"},
            404: {"description": "比赛不存在"},
            409: {"description": "状态非法 / 选手正在另一场进行中的比赛"},
        },
    )
    def start_match(
        self,
        match_id: str,
        account: Account = Depends(get_current_account),
    ) -> MatchOut:
        match = self.db.matches.get(match_id)
        if match is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "比赛不存在")
        if not self._is_referee_or_admin(match, account):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "仅该场比赛的裁判可开始比赛")
        # 合法转换：CREATED → RUNNING；RUNNING 幂等；PAUSED/ENDED 非法（→ 409）。
        if match.status in (MatchStatus.PAUSED, MatchStatus.ENDED):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "比赛已暂停或已结束"
                if match.status == MatchStatus.PAUSED
                else "比赛已结束",
            )
        if match.status == MatchStatus.CREATED:
            conflict = player_running_conflict(
                self.db, match.player_a_id, match.player_b_id, self_id=match.id
            )
            if conflict:
                raise HTTPException(status.HTTP_409_CONFLICT, conflict)
            match.status = MatchStatus.RUNNING
            match.started_at = now_ts()
            self.db.matches.replace(match)
        return MatchOut.from_match(match, None, self.storage)

    @post(
        "/matches/{match_id}/pause",
        response_model=MatchOut,
        summary="暂停比赛（保留进度，释放选手）",
        description="裁判把进行中的比赛 RUNNING→PAUSED：保留回合/比分/草稿数据，"
        "释放两名选手占用（可立刻去打另一场）；仅该场裁判或管理员可调用；"
        "暂停后该场所有比赛类 WS 动作被拒绝，直到 resume。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "非该场裁判/管理员"},
            404: {"description": "比赛不存在"},
            409: {"description": "当前非 RUNNING（无法暂停）"},
        },
    )
    async def pause_match(
        self,
        match_id: str,
        account: Account = Depends(get_current_account),
    ) -> MatchOut:
        match = self.db.matches.get(match_id)
        if match is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "比赛不存在")
        if not self._is_referee_or_admin(match, account):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "仅该场比赛的裁判可暂停比赛")
        if match.status != MatchStatus.RUNNING:
            raise HTTPException(status.HTTP_409_CONFLICT, "仅 RUNNING 比赛可暂停")
        match.status = MatchStatus.PAUSED
        match.paused_at = now_ts()
        self.db.matches.replace(match)
        if self.cm is not None:
            await self.cm.pause_match(match_id)
        return MatchOut.from_match(match, None, self.storage)

    @post(
        "/matches/{match_id}/resume",
        response_model=MatchOut,
        summary="恢复比赛（PAUSED → RUNNING）",
        description="裁判把暂停中的比赛恢复为 RUNNING，原样续上历史回合数据。"
        "恢复前校验双方选手不在另一场 RUNNING 比赛中（冲突 409）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "非该场裁判/管理员"},
            404: {"description": "比赛不存在"},
            409: {"description": "当前非 PAUSED / 选手在另一场进行中的比赛"},
        },
    )
    async def resume_match(
        self,
        match_id: str,
        account: Account = Depends(get_current_account),
    ) -> MatchOut:
        match = self.db.matches.get(match_id)
        if match is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "比赛不存在")
        if not self._is_referee_or_admin(match, account):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "仅该场比赛的裁判可恢复比赛")
        if match.status != MatchStatus.PAUSED:
            raise HTTPException(status.HTTP_409_CONFLICT, "仅 PAUSED 比赛可恢复")
        conflict = player_running_conflict(
            self.db, match.player_a_id, match.player_b_id, self_id=match.id
        )
        if conflict:
            raise HTTPException(status.HTTP_409_CONFLICT, conflict)
        match.status = MatchStatus.RUNNING
        self.db.matches.replace(match)
        if self.cm is not None:
            await self.cm.resume_match(match_id)
        return MatchOut.from_match(match, None, self.storage)

    # -------------------------------------------------------- 赛程管理

    def _my_tournaments(self, account_id: str) -> list[Tournament]:
        """当前账号作为选手/裁判/导播参与的赛事（按 id 去重）。"""
        merged: dict[str, Tournament] = {}
        for t in (
            *self.db.tournaments.find_by_participant(account_id),
            *self.db.tournaments.find_by_referee(account_id),
            *self.db.tournaments.find_by_director(account_id),
        ):
            merged[t.id] = t
        items = list(merged.values())
        items.sort(
            key=lambda t: (
                t.status != TournamentStatus.IN_PROGRESS,
                -t.created_at.timestamp(),
            )
        )
        return items

    @get(
        "/tournaments",
        response_model=list[TournamentOut],
        summary="我参与的赛事",
        description="返回当前账号作为选手/裁判/导播参与的赛事；进行中优先，创建时间倒序。",
    )
    def tournaments(
        self, account: Account = Depends(get_current_account)
    ) -> list[TournamentOut]:
        return [
            TournamentOut.from_tournament(t) for t in self._my_tournaments(account.id)
        ]

    def _is_default_tournament_member(self, account: Account) -> bool:
        """默认赛事（孤立比赛容器）无成员池，按「参与过其名下任意一场比赛
        （选手/裁判/导播任一角色）」判定成员资格。"""
        return any(
            m.tournament_id == DEFAULT_TOURNAMENT_ID
            for m in self.db.matches.find_by_member(account.id)
        )

    def _require_tournament_member(self, tournament_id: str, account: Account) -> None:
        """门控：账号须是该赛事的参赛选手/裁判/导播/管理员。

        默认赛事成员池恒空，改按比赛参与关系判定（见 _is_default_tournament_member）。
        """
        t = self.db.tournaments.get(tournament_id)
        if t is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "赛事不存在")
        is_member = (
            account.id in t.participant_ids
            or account.id in t.referee_ids
            or account.id in t.director_ids
            or AccountType.ADMIN in account.roles
            or (
                t.id == DEFAULT_TOURNAMENT_ID
                and self._is_default_tournament_member(account)
            )
        )
        if not is_member:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "非该赛事成员")

    @get(
        "/tournaments/{tournament_id}",
        response_model=TournamentOut,
        summary="查看赛事详情（赛事成员）",
        description="赛事成员（选手/裁判/导播/管理员）可读赛事基本信息（名称/状态等）。"
        "与列表端点不同，默认赛事（孤立比赛容器）不在 /me/tournaments 列表里，"
        "但参与过其名下任意比赛的账号可经本端点读取——导播舞台 Coming Soon "
        "场景显示赛事名/状态用。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "非该赛事成员"},
            404: {"description": "赛事不存在"},
        },
    )
    def tournament_detail(
        self,
        tournament_id: str,
        account: Account = Depends(get_current_account),
    ) -> TournamentOut:
        self._require_tournament_member(tournament_id, account)
        t = self.db.tournaments.get(tournament_id)
        if t is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "赛事不存在")
        return TournamentOut.from_tournament(t)

    @get(
        "/tournaments/{tournament_id}/bracket",
        response_model=BracketView,
        summary="查看赛事对阵树（赛事成员）",
        description="选手/裁判/导播（赛事成员）可读对阵树，含已结束对阵的比分。"
        "门控：须为该赛事参赛/裁判/导播/管理员；默认赛事按「参与过其名下"
        "任意比赛」判定（成员池恒空）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "非该赛事成员"},
            404: {"description": "赛事不存在"},
        },
    )
    def tournament_bracket(
        self,
        tournament_id: str,
        account: Account = Depends(get_current_account),
    ) -> BracketView:
        self._require_tournament_member(tournament_id, account)
        try:
            return BracketView.build(self.db, tournament_id)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "赛事不存在") from None

    @get(
        "/tournaments/{tournament_id}/mappool",
        response_model=MappoolOut,
        summary="查看赛事图池（赛事成员）",
        description="赛事成员（选手/裁判/导播/管理员）可读该赛事任一已生成比赛的图池"
        "（含 logo_url）；优先取第一场已生成比赛的图池。默认赛事按「参与过"
        "其名下任意比赛」判定成员，但其无 fixture，恒 404 无图池。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "非该赛事成员"},
            404: {"description": "赛事不存在 / 尚无已生成比赛"},
        },
    )
    def tournament_mappool(
        self,
        tournament_id: str,
        account: Account = Depends(get_current_account),
    ) -> MappoolOut:
        self._require_tournament_member(tournament_id, account)
        # 找该赛事下第一场已生成比赛（有 match_id 的 fixture → match）
        fixtures = self.db.fixtures.find_by_tournament(tournament_id)
        match = None
        for f in fixtures:
            if f.match_id:
                m = self.db.matches.get(f.match_id)
                if m is not None:
                    match = m
                    break
        if match is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "该赛事尚无已生成比赛（无图池可读）"
            )
        from ..datatypes import MappoolDoc

        doc = MappoolDoc(
            name=f"{match.name} 图池",
            mappool=match.mappool,
            created_by="system",
        )
        return MappoolOut.from_doc(doc, self.storage, self.db)

    # -------------------------------------------------------- 账号自助

    @patch(
        "",
        response_model=AccountOut,
        summary="修改我的展示名",
        description="当前登录账号修改自己的展示名（非敏感，无需旧密码校验）。",
        responses={
            401: {"description": "未携带有效令牌"},
            400: {"description": "展示名为空"},
        },
    )
    def update_display_name(
        self,
        body: DisplayNameUpdate,
        account: Account = Depends(get_current_account),
    ) -> AccountOut:
        name = body.display_name.strip()
        if not name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "展示名不能为空")
        account.display_name = name
        self.db.accounts.replace(account)
        return AccountOut.from_account(account)

    @post(
        "/password",
        summary="修改我的口令",
        description="当前登录账号修改自己的口令；须校验旧口令，防止借用登录态误改。",
        responses={
            401: {"description": "未携带有效令牌"},
            400: {"description": "旧口令错误 / 新口令非法"},
        },
    )
    def change_password(
        self,
        body: PasswordChange,
        account: Account = Depends(get_current_account),
    ) -> dict[str, bool]:
        if not verify_password(body.old_password, account.password_hash):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "旧口令错误")
        if len(body.new_password) < 4:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "新口令过短（至少 4 位）")
        account.password_hash = hash_password(body.new_password)
        self.db.accounts.replace(account)
        return {"ok": True}


class DisplayNameUpdate(BaseModel):
    display_name: str = Field(description="新的展示名")


class PasswordChange(BaseModel):
    old_password: str = Field(description="旧口令（明文，服务端校验）")
    new_password: str = Field(description="新口令（明文，至少 4 位）")
