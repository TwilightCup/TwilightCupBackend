"""日志查询控制器（admin/裁判/导播）：比赛日志、聊天日志、回合明细。"""

from __future__ import annotations

from classy_fastapi import Routable, get
from fastapi import Depends, HTTPException, status

from ..auth import require_viewer
from ..controllers import DBController
from ..datatypes import Account, ChatMessage, MatchLog, RoundRecord


class LogController(Routable):
    def __init__(self, db: DBController) -> None:
        super().__init__(prefix="/logs", tags=["logs"])
        self.db = db

    @get(
        "/matches/{match_id}/match_log",
        response_model=MatchLog,
        summary="比赛日志",
        description="整场比赛的实时日志视图：初始信息(赛制/图池/选手/延迟)、"
        "全部回合 id（含不计分的重赛原回合）、最终结果。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员/裁判/导播权限"},
            404: {"description": "比赛日志不存在（尚无回合开始）"},
        },
    )
    def match_log(
        self,
        match_id: str,
        _: Account = Depends(require_viewer),
    ) -> MatchLog:
        log = self.db.match_logs.get_by_match(match_id)
        if log is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "比赛日志不存在")
        return log

    @get(
        "/matches/{match_id}/chat",
        response_model=list[ChatMessage],
        summary="聊天日志",
        description="该比赛累计的全部聊天与系统消息（跨回合不清空）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员/裁判/导播权限"},
        },
    )
    def chat(
        self,
        match_id: str,
        _: Account = Depends(require_viewer),
    ) -> list[ChatMessage]:
        return self.db.chat_messages.find_by_match(match_id)

    @get(
        "/matches/{match_id}/rounds/{round_no}",
        response_model=RoundRecord,
        summary="回合明细",
        description="指定回合的完整数据：选图快照、双方每次尝试/每关用时、最终成绩、胜负判定。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员/裁判/导播权限"},
            404: {"description": "回合不存在"},
        },
    )
    def round_detail(
        self,
        match_id: str,
        round_no: int,
        _: Account = Depends(require_viewer),
    ) -> RoundRecord:
        for record in self.db.rounds.find_by_match(match_id):
            if record.round_no == round_no:
                return record
        raise HTTPException(status.HTTP_404_NOT_FOUND, "回合不存在")
