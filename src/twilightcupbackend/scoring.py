"""成绩计算（纯函数）。

- 多关项目：所有关卡用时之和（取选手上报的最终总时长，缺省则累加已完成关卡）。
- 单关项目：有效尝试（非 N/A）的最快或平均；0 有效 → 视为弃权（返回 None）。
- 弃权方成绩记 None；比较时 None 为最差；双方均 None → 平局。
"""

from __future__ import annotations

from typing import Literal

from .datatypes import (
    AttemptStatus,
    PlayerRoundState,
    ScoringMethod,
)


def multi_score(state: PlayerRoundState) -> int | None:
    if state.forfeited:
        return None
    if state.final_total_ms is not None:
        return state.final_total_ms
    return sum(lt.time_ms for lt in state.completed_levels)


def single_score(state: PlayerRoundState, method: ScoringMethod) -> int | None:
    if state.forfeited:
        return None
    valid = [
        a.time_ms
        for a in state.attempts
        if a.status == AttemptStatus.VALID and a.time_ms is not None
    ]
    if not valid:
        return None  # 0 次有效成绩 → 弃权
    if method == ScoringMethod.FASTEST:
        return min(valid)
    return sum(valid) // len(valid)


def compare(score_a: int | None, score_b: int | None) -> Literal["A", "B", "TIE"]:
    """比较双方成绩，低时长为胜；均 None 为平局。"""
    if score_a is None and score_b is None:
        return "TIE"
    if score_a is None:
        return "B"
    if score_b is None:
        return "A"
    if score_a < score_b:
        return "A"
    if score_b < score_a:
        return "B"
    return "TIE"
