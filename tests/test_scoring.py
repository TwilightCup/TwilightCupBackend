"""M7 计分纯函数测试。"""

from __future__ import annotations

from twilightcupbackend.datatypes import (
    Attempt,
    AttemptStatus,
    LevelTime,
    PlayerRoundState,
    ScoringMethod,
)
from twilightcupbackend.scoring import compare, multi_score, single_score


def _state() -> PlayerRoundState:
    return PlayerRoundState(account_id="A")


def test_multi_score_sum() -> None:
    s = _state()
    s.completed_levels = [
        LevelTime(level_index=0, time_ms=1000, total_ms=1000),
        LevelTime(level_index=1, time_ms=2000, total_ms=3000),
    ]
    assert multi_score(s) == 3000


def test_multi_score_final_total() -> None:
    s = _state()
    s.final_total_ms = 9999
    assert multi_score(s) == 9999


def test_multi_forfeit_none() -> None:
    s = _state()
    s.forfeited = True
    assert multi_score(s) is None


def test_single_fastest() -> None:
    s = _state()
    s.attempts = [
        Attempt(index=0, status=AttemptStatus.VALID, time_ms=5000),
        Attempt(index=1, status=AttemptStatus.VALID, time_ms=3000),
        Attempt(index=2, status=AttemptStatus.SKIPPED, time_ms=None),
    ]
    assert single_score(s, ScoringMethod.FASTEST) == 3000


def test_single_average() -> None:
    s = _state()
    s.attempts = [
        Attempt(index=0, status=AttemptStatus.VALID, time_ms=5000),
        Attempt(index=1, status=AttemptStatus.VALID, time_ms=3000),
    ]
    assert single_score(s, ScoringMethod.AVERAGE) == 4000


def test_single_zero_valid_none() -> None:
    s = _state()
    s.attempts = [Attempt(index=0, status=AttemptStatus.SKIPPED, time_ms=None)]
    assert single_score(s, ScoringMethod.FASTEST) is None


# ---------------------------------------------------------------------------
# INVALID（通关但带无效标记）排除计分（INVALID_ATTEMPT_REQ §4.4）
# ---------------------------------------------------------------------------


def test_single_fastest_excludes_invalid() -> None:
    """FASTEST：无效尝试恰好最快 → 成绩取次快（有效最小值）。"""
    s = _state()
    s.attempts = [
        Attempt(index=0, status=AttemptStatus.VALID, time_ms=60_000),
        Attempt(
            index=1,
            status=AttemptStatus.INVALID,
            time_ms=50_000,
            invalid_reasons=["CheckpointSkip"],
        ),
    ]
    assert single_score(s, ScoringMethod.FASTEST) == 60_000


def test_single_average_excludes_invalid() -> None:
    """AVERAGE：平均只除以有效尝试数。"""
    s = _state()
    s.attempts = [
        Attempt(index=0, status=AttemptStatus.VALID, time_ms=60_000),
        Attempt(
            index=1,
            status=AttemptStatus.INVALID,
            time_ms=50_000,
            invalid_reasons=["!CheatCode"],
        ),
    ]
    assert single_score(s, ScoringMethod.AVERAGE) == 60_000


def test_single_all_invalid_none() -> None:
    """全 INVALID（+退出）→ 0 有效成绩 → 弃权路径（None）。"""
    s = _state()
    s.attempts = [
        Attempt(
            index=0,
            status=AttemptStatus.INVALID,
            time_ms=50_000,
            invalid_reasons=["CheckpointSkip"],
        )
    ]
    assert single_score(s, ScoringMethod.FASTEST) is None
    assert single_score(s, ScoringMethod.AVERAGE) is None


def test_compare() -> None:
    assert compare(100, 200) == "A"
    assert compare(200, 100) == "B"
    assert compare(100, 100) == "TIE"
    assert compare(None, 100) == "B"
    assert compare(100, None) == "A"
    assert compare(None, None) == "TIE"
