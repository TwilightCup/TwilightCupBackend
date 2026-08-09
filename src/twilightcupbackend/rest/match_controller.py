"""比赛管理控制器（管理员）：创建/查询/强制结束比赛。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from classy_fastapi import Routable, get, patch, post
from fastapi import Depends, HTTPException, status

from ..auth import require_admin
from ..controllers import DBController, player_running_conflict
from ..datatypes import (
    Account,
    AccountType,
    Mappool,
    Match,
    MatchPhase,
    MatchStatus,
    now_ts,
)
from ..protocol import SrvPhaseChange
from .schemas import MatchCreate, MatchOut, MatchUpdate

if TYPE_CHECKING:
    from ..connection_manager import ConnectionManager
    from ..storage import Storage


class MatchController(Routable):
    def __init__(
        self,
        db: DBController,
        cm: ConnectionManager | None = None,
        storage: Storage | None = None,
    ) -> None:
        super().__init__(prefix="/admin/matches", tags=["matches"])
        self.db = db
        self.cm = cm
        self.storage = storage

    @post(
        "",
        response_model=MatchOut,
        status_code=status.HTTP_201_CREATED,
        summary="创建比赛",
        description="管理员创建一场比赛：指定赛制(BO)、单关计分方式、完整图池、"
        "指派双方选手/裁判/导播账号、开始倒计时延迟秒数。角色账号以用户名指定，"
        "服务端校验账号类型并解析为 id。win_threshold 省略时按 (bo//2)+1 推导。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            400: {"description": "角色账号不存在/类型不符/双方选手相同/分数非法"},
        },
    )
    def create(
        self,
        body: MatchCreate,
        _: Account = Depends(require_admin),
    ) -> MatchOut:
        player_a = self._resolve(body.player_a, AccountType.PLAYER, "选手A")
        player_b = self._resolve(body.player_b, AccountType.PLAYER, "选手B")
        referee = self._resolve(body.referee, AccountType.REFEREE, "裁判")
        director = self._resolve(body.director, AccountType.DIRECTOR, "导播")
        # 允许同一账号身兼多职（选手A/B/裁判/导播可指向同一账号），
        # 以支持单人调试或一个端以多种身份各开一条 ?seat= 连接。

        win_threshold = body.win_threshold or (body.bo_format // 2) + 1
        if win_threshold > body.bo_format:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "取胜分数不能大于 BO 数")

        mappool = self._resolve_mappool(body)

        match = Match(
            name=body.name,
            bo_format=body.bo_format,
            win_threshold=win_threshold,
            scoring_method=body.scoring_method,
            start_countdown_delay=body.start_countdown_delay,
            ban_count=body.ban_count,
            protect_count=body.protect_count,
            ct_tag_count=body.ct_tag_count,
            mappool=mappool,
            player_a_id=player_a.id,
            player_b_id=player_b.id,
            referee_id=referee.id,
            director_id=director.id,
        )
        # 指定选手不得已在另一场 RUNNING 会话中（§6）
        conflict = player_running_conflict(
            self.db, match.player_a_id, match.player_b_id, self_id=match.id
        )
        if conflict:
            raise HTTPException(status.HTTP_409_CONFLICT, conflict)
        self.db.matches.insert(match)
        return MatchOut.from_match(match, None, self.storage)

    @get(
        "",
        response_model=list[MatchOut],
        summary="比赛列表",
        description="返回全部比赛。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
        },
    )
    def list(
        self,
        _: Account = Depends(require_admin),
    ) -> list[MatchOut]:
        return [
            MatchOut.from_match(s, None, self.storage) for s in self.db.matches.find()
        ]

    @get(
        "/{match_id}",
        response_model=MatchOut,
        summary="查询比赛详情",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            404: {"description": "比赛不存在"},
        },
    )
    def get_one(
        self,
        match_id: str,
        _: Account = Depends(require_admin),
    ) -> MatchOut:
        match = self.db.matches.get(match_id)
        if match is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "比赛不存在")
        return MatchOut.from_match(match, None, self.storage)

    @patch(
        "/{match_id}",
        response_model=MatchOut,
        summary="局部更新会话（选手/状态/名称）",
        description="便于「把选手分到已存在的会话」或管理员手动改状态（§7.1）。"
        "选手以用户名指定（校验 PLAYER 角色）；改选手或切到 RUNNING 时同样跑跨会话占用"
        "冲突校验（§5）。status 切换须合法（→RUNNING 须 CREATED/PAUSED；→PAUSED 须 "
        "RUNNING；→ENDED 须 RUNNING/PAUSED），否则 409。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            404: {"description": "会话/账号不存在"},
            400: {"description": "账号角色不符"},
            409: {"description": "状态切换非法 / 选手在另一场进行中的比赛"},
        },
    )
    def update(
        self,
        match_id: str,
        body: MatchUpdate,
        _: Account = Depends(require_admin),
    ) -> MatchOut:
        match = self.db.matches.get(match_id)
        if match is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "比赛不存在")
        if body.name is not None:
            match.name = body.name
        if body.player_a is not None:
            match.player_a_id = self._resolve(
                body.player_a, AccountType.PLAYER, "选手A"
            ).id
        if body.player_b is not None:
            match.player_b_id = self._resolve(
                body.player_b, AccountType.PLAYER, "选手B"
            ).id
        if body.status is not None and body.status != match.status:
            self._transition(match, body.status)
        # 应用后若处于 RUNNING，校验最终双方选手无跨会话占用冲突。
        if match.status == MatchStatus.RUNNING:
            conflict = player_running_conflict(
                self.db, match.player_a_id, match.player_b_id, self_id=match.id
            )
            if conflict:
                raise HTTPException(status.HTTP_409_CONFLICT, conflict)
        self.db.matches.replace(match)
        return MatchOut.from_match(match, None, self.storage)

    @staticmethod
    def _transition(match: Match, target: MatchStatus) -> None:
        """校验状态切换合法性（§2 状态机），非法则 409。

        合法：→RUNNING 须 CREATED/PAUSED；→PAUSED 须 RUNNING；→ENDED 须
        RUNNING/PAUSED；→CREATED 不允许（不可回退）。
        """
        cur = match.status
        ok = (
            (
                target == MatchStatus.RUNNING
                and cur in (MatchStatus.CREATED, MatchStatus.PAUSED)
            )
            or (target == MatchStatus.PAUSED and cur == MatchStatus.RUNNING)
            or (
                target == MatchStatus.ENDED
                and cur in (MatchStatus.RUNNING, MatchStatus.PAUSED)
            )
        )
        if not ok:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"非法状态切换：{cur.name} → {target.name}",
            )
        if target == MatchStatus.RUNNING and cur == MatchStatus.CREATED:
            match.started_at = match.started_at or now_ts()
        if target == MatchStatus.PAUSED:
            match.paused_at = now_ts()
        if target == MatchStatus.ENDED:
            match.ended_at = now_ts()
        match.status = target

    def _resolve(self, username: str, expected: AccountType, label: str) -> Account:
        account = self.db.accounts.get_by_username(username)
        if account is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"{label}账号不存在：{username}"
            )
        if expected not in account.roles:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{label}账号不含所需角色（期望 {expected.name}）",
            )
        return account

    def _resolve_mappool(self, body: MatchCreate) -> Mappool:
        """优先按 mappool_id 引用图池库；否则用内联 mappool；
        二者皆空则 400。"""
        if body.mappool_id:
            saved = self.db.mappools.get(body.mappool_id)
            if saved is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "图池不存在")
            return saved.mappool
        if body.mappool is not None:
            return body.mappool
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "需提供 mappool_id 或 mappool")

    @post(
        "/{match_id}/end",
        summary="强制结束比赛（管理员）",
        description="把进行中（RUNNING/CREATED）的比赛标记为 ENDED，"
        "断开双方选手并广播结束。能从比分推导胜方（达到取胜分数或领先）时走"
        "完整结束流程：记录 winner/final_result 并推进赛程；比分持平或无实时"
        "会话时仅标记 ENDED + 释放选手。用于裁判弃赛/异常导致卡住时释放选手"
        "（否则该选手的下一场 begin_prep 被单场规则挡）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            404: {"description": "比赛不存在"},
            400: {"description": "比赛已结束"},
        },
    )
    async def force_end(
        self,
        match_id: str,
        _: Account = Depends(require_admin),
    ) -> dict[str, bool]:
        match = self.db.matches.get(match_id)
        if match is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "比赛不存在")
        if match.status == MatchStatus.ENDED:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "比赛已结束")
        cm = self.cm
        engine = cm.match_engine if cm is not None else None
        # 有实时会话且能从比分推导胜方 → 与裁判手动结束同一完整流程
        # （_end_match 内部会置 ENDED、写 winner/final_result、踢选手、推进赛程）
        if cm is not None and engine is not None and await engine.force_end_full(
            match_id
        ):
            return {"ok": True}
        match.status = MatchStatus.ENDED
        match.ended_at = now_ts()
        self.db.matches.replace(match)
        if cm is not None:
            store = cm.registry.get(match_id)
            if store is not None:
                store.phase = MatchPhase.MATCH_END
            await cm.system_message(
                match_id, cm.tr(match_id, "match.admin_ended"), kind="match_end"
            )
            await cm.broadcast_match(
                match_id, SrvPhaseChange(phase=MatchPhase.MATCH_END)
            )
            await cm.kick_players(match_id)
        return {"ok": True}
