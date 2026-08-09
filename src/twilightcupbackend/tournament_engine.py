"""赛程引擎：按赛制生成对阵、推进轮次、产出排名。

每场实战对决仍由 ``MatchEngine`` 驱动；本引擎只做「编排」——生成对阵表、
单场结束时推进晋级链、产出最终排名。通过 ``Match.tournament_id`` /
``fixture_id`` 与对阵节点关联。``MatchEngine._end_match`` 末尾的钩子调用
``on_match_ended`` 触发推进。

M12 实现单败淘汰（SINGLE_ELIM）。双败 / 瑞士轮见 M13。
"""

from __future__ import annotations

import math
import random
from logging import Logger, getLogger
from typing import TYPE_CHECKING, Literal

from .controllers import DBController
from .datatypes import (
    BracketSide,
    Fixture,
    FixtureStatus,
    Mappool,
    Match,
    Tournament,
    TournamentFormat,
    TournamentStanding,
    TournamentStatus,
    now_ts,
)

if TYPE_CHECKING:
    from .connection_manager import ConnectionManager


def _seed_slots(bracket_size: int) -> list[int]:
    """标准单败 seed 播位：返回长度 ``bracket_size`` 的列表，第 i 项 = 槽位 i 应放的
    种子号（1-based）。递归构造，保证高种子分散（1 vs 末位、2 vs 倒数第二……）。

    例：bracket_size=8 → [1,8,4,5,2,7,3,6]（即 1v8 / 4v5 / 2v7 / 3v6）。
    """
    slots = [1]
    n = 1
    while n < bracket_size:
        n *= 2
        slots = [s for seed in slots for s in (seed, n + 1 - seed)]
    return slots


class TournamentEngine:
    """赛程编排引擎。"""

    def __init__(
        self,
        db: DBController,
        cm: ConnectionManager | None = None,
        logger: Logger | None = None,
    ) -> None:
        self.db = db
        self.cm = cm
        self.logger = logger or getLogger("TournamentEngine")

    # ---------------------------------------------------------------- 生成

    def generate(self, tournament: Tournament) -> list[Fixture]:
        """按赛制生成对阵表，返回首轮 fixtures。"""
        match tournament.format:
            case TournamentFormat.SINGLE_ELIM:
                return self._generate_single_elim(tournament)
            case TournamentFormat.DOUBLE_ELIM:
                return self._generate_double_elim(tournament)
            case TournamentFormat.SWISS:
                return self._generate_swiss_first_round(tournament)
            case _:
                raise ValueError(f"未知赛制：{tournament.format}")

    def _generate_single_elim(self, tournament: Tournament) -> list[Fixture]:
        participants = list(tournament.seed_order or tournament.participant_ids)
        if not tournament.seed_order and len(participants) > 1:
            random.shuffle(participants)
        n = len(participants)
        if n < 2:
            raise ValueError("单败赛制需至少 2 名选手")
        bracket_size = 1 << (n - 1).bit_length()  # ≥ n 的最小 2^k
        total_rounds = int(math.log2(bracket_size))
        seeded: list[str | None] = [
            participants[s - 1] if s <= n else None for s in _seed_slots(bracket_size)
        ]

        # 先建全部轮次的空壳节点（内存），再连线、填选手、推进 bye
        by_round: dict[int, list[Fixture]] = {
            r: [
                Fixture(
                    tournament_id=tournament.id,
                    round_no=r,
                    bracket_side=BracketSide.MAIN,
                    match_index=k,
                )
                for k in range(bracket_size // (2**r))
            ]
            for r in range(1, total_rounds + 1)
        }
        first = by_round[1]
        for k, f in enumerate(first):
            a, b = seeded[2 * k], seeded[2 * k + 1]
            f.player_a_id = a
            f.player_b_id = b
            if a is None or b is None:
                f.is_bye = True
                f.winner_id = a or b  # 有选手的一方自动晋级
                f.status = FixtureStatus.COMPLETED
                f.completed_at = now_ts()
            else:
                f.status = FixtureStatus.READY

        # 晋级链 + depends_on（每场指向下一轮对应节点的 A/B 槽）
        for r in range(1, total_rounds):
            nxt = by_round[r + 1]
            for k, f in enumerate(by_round[r]):
                target = nxt[k // 2]
                f.advances_to = target.id
                f.advances_slot = "A" if k % 2 == 0 else "B"
                target.depends_on.append(f.id)

        # 首轮 bye 的胜者递归填入下一轮槽位（双 bye 不会发生：seed 播位使非空分散）
        self._propagate_byes(by_round)

        # 持久化
        all_fixtures = [f for r in by_round for f in by_round[r]]
        for f in all_fixtures:
            self.db.fixtures.insert(f)

        tournament.total_rounds = total_rounds
        tournament.current_round = 1
        tournament.bracket_generated_at = now_ts()
        tournament.status = TournamentStatus.IN_PROGRESS
        self.db.tournaments.replace(tournament)
        return first

    def _propagate_byes(self, by_round: dict[int, list[Fixture]]) -> None:
        """把已 COMPLETED（首轮 bye 或后续自动推进）的 winner 填入晋级目标槽位，
        直到无变化。目标双方到位则置 READY。"""
        total_rounds = max(by_round)
        changed = True
        while changed:
            changed = False
            for r in range(1, total_rounds):
                for f in by_round[r]:
                    if (
                        f.status != FixtureStatus.COMPLETED
                        or f.winner_id is None
                        or f.advances_to is None
                    ):
                        continue
                    target = self._find_in_round(by_round, f.advances_to)
                    if target is None:
                        continue
                    slot = f.advances_slot
                    cur = target.player_a_id if slot == "A" else target.player_b_id
                    if cur is None:
                        if slot == "A":
                            target.player_a_id = f.winner_id
                        else:
                            target.player_b_id = f.winner_id
                        changed = True
                        if target.player_a_id and target.player_b_id:
                            target.status = FixtureStatus.READY

    @staticmethod
    def _find_in_round(
        by_round: dict[int, list[Fixture]], fixture_id: str
    ) -> Fixture | None:
        for rnd in by_round.values():
            for f in rnd:
                if f.id == fixture_id:
                    return f
        return None

    # ---------------------------------------------------- 单场结束 → 推进

    async def on_match_ended(self, match: Match, winner: Literal["A", "B"]) -> None:
        """``MatchEngine._end_match`` 钩子入口：标记 fixture 完成 + 按赛制推进。

        幂等：非赛事对决、fixture 不存在或已 COMPLETED 时直接返回。
        """
        if match.tournament_id is None or match.fixture_id is None:
            return
        fixture = self.db.fixtures.get(match.fixture_id)
        if fixture is None or fixture.status == FixtureStatus.COMPLETED:
            return
        if winner == "A":
            winner_id, loser_id = fixture.player_a_id, fixture.player_b_id
        else:
            winner_id, loser_id = fixture.player_b_id, fixture.player_a_id
        if winner_id is None:
            return  # 防御：实战比赛双方必已定，正常不会到这
        fixture.winner_id = winner_id
        fixture.status = FixtureStatus.COMPLETED
        fixture.completed_at = now_ts()
        self.db.fixtures.replace(fixture)

        tournament = self.db.tournaments.get(fixture.tournament_id)
        if tournament is None:
            return
        match tournament.format:
            case TournamentFormat.SINGLE_ELIM:
                self._advance_single_elim(fixture, winner_id, loser_id)
            case TournamentFormat.DOUBLE_ELIM:
                self._advance_double_elim(fixture, winner_id, loser_id)
            case TournamentFormat.SWISS:
                self._advance_swiss(fixture, winner_id, loser_id)
        await self._maybe_complete_tournament(tournament)

    def _advance_single_elim(
        self, fixture: Fixture, winner_id: str, loser_id: str | None
    ) -> None:
        """胜者填入下一轮目标槽位；目标双方到位则 READY。决赛无 advances_to。"""
        if fixture.advances_to is None:
            return
        target = self.db.fixtures.get(fixture.advances_to)
        if target is None:
            return
        if fixture.advances_slot == "A":
            target.player_a_id = winner_id
        else:
            target.player_b_id = winner_id
        if target.player_a_id and target.player_b_id:
            target.status = FixtureStatus.READY
        self.db.fixtures.replace(target)

    # ---------------------------------------------------------- 实例化比赛

    def materialize_match(self, fixture: Fixture, body, mappool: Mappool) -> Match:
        """把对阵节点实例化为 ``Match``（单场规则 + 图池来自 body）。"""
        if fixture.player_a_id is None or fixture.player_b_id is None:
            raise ValueError("双方选手未定，无法生成实战比赛")
        if fixture.referee_id is None:
            raise ValueError("未指派裁判，无法生成实战比赛")
        win_threshold = body.win_threshold or (body.bo_format // 2) + 1
        if win_threshold > body.bo_format:
            raise ValueError("取胜分数不能大于 BO 数")
        tournament = self.db.tournaments.get(fixture.tournament_id)
        tname = tournament.name if tournament else fixture.tournament_id
        match = Match(
            name=f"{tname} R{fixture.round_no} #{fixture.match_index}",
            bo_format=body.bo_format,
            win_threshold=win_threshold,
            scoring_method=body.scoring_method,
            start_countdown_delay=body.start_countdown_delay,
            ban_count=body.ban_count,
            protect_count=body.protect_count,
            ct_tag_count=getattr(body, "ct_tag_count", 2),
            mappool=mappool,
            player_a_id=fixture.player_a_id,
            player_b_id=fixture.player_b_id,
            referee_id=fixture.referee_id,
            director_id=fixture.director_id or "",
            tournament_id=fixture.tournament_id,
            fixture_id=fixture.id,
        )
        self.db.matches.insert(match)
        fixture.match_id = match.id
        fixture.status = FixtureStatus.RUNNING
        fixture.started_at = now_ts()
        self.db.fixtures.replace(fixture)
        return match

    # -------------------------------------------------------------- 指派

    def assign_officials(
        self,
        fixture: Fixture,
        referee_id: str | None = None,
        director_id: str | None = None,
    ) -> Fixture:
        if referee_id is not None:
            fixture.referee_id = referee_id
        if director_id is not None:
            fixture.director_id = director_id
        self.db.fixtures.replace(fixture)
        return fixture

    # -------------------------------------------------------------- 排名

    def compute_standings(self, tournament: Tournament) -> list[TournamentStanding]:
        """计算排名。单败：冠军 rank 1；其余按被淘汰轮次（越晚淘汰名次越高）。"""
        match tournament.format:
            case TournamentFormat.SINGLE_ELIM:
                return self._standings_single_elim(tournament)
            case TournamentFormat.DOUBLE_ELIM:
                return self._standings_double_elim(tournament)
            case TournamentFormat.SWISS:
                return self._standings_swiss(tournament)
        return []

    def _standings_single_elim(
        self, tournament: Tournament
    ) -> list[TournamentStanding]:
        fixtures = self.db.fixtures.find_by_tournament(tournament.id)
        total_rounds = tournament.total_rounds or 0
        eliminated: dict[str, int] = {}
        wins: dict[str, int] = dict.fromkeys(tournament.participant_ids, 0)
        champion: str | None = None
        for f in fixtures:
            if f.winner_id is None:
                continue
            wins[f.winner_id] = wins.get(f.winner_id, 0) + 1
            if f.advances_to is None:
                champion = f.winner_id  # 决赛
                continue
            loser = f.player_b_id if f.winner_id == f.player_a_id else f.player_a_id
            if loser and loser not in eliminated:
                eliminated[loser] = f.round_no

        standings: list[TournamentStanding] = []
        for pid in tournament.participant_ids:
            if pid == champion:
                standings.append(
                    TournamentStanding(
                        account_id=pid,
                        rank=1,
                        wins=wins.get(pid, 0),
                        eliminated_round=None,
                        note="冠军",
                    )
                )
            elif pid in eliminated:
                r = eliminated[pid]
                # round r 败者并列名次：2^(R-r) + 1
                rank = (2 ** (total_rounds - r)) + 1 if total_rounds else r + 1
                standings.append(
                    TournamentStanding(
                        account_id=pid,
                        rank=rank,
                        wins=wins.get(pid, 0),
                        eliminated_round=r,
                    )
                )
            else:
                # 尚未淘汰也未夺冠（赛事未完成 / bye 晋级中）
                standings.append(
                    TournamentStanding(
                        account_id=pid,
                        rank=0,
                        wins=wins.get(pid, 0),
                    )
                )
        standings.sort(key=lambda s: (s.rank == 0, s.rank, -s.wins))
        return standings

    # ---------------------------------------------------- 赛事结束判定

    async def _maybe_complete_tournament(self, tournament: Tournament) -> None:
        """所有需实战的 fixtures 完成 + 决出冠军 → 标记赛事 COMPLETED + 冻结排名。

        单败/双败看决赛/GF 是否有 winner；瑞士轮看是否打满 total_rounds。
        """
        if tournament.status == TournamentStatus.COMPLETED:
            return
        fixtures = self.db.fixtures.find_by_tournament(tournament.id)
        if any(not f.is_bye and f.status != FixtureStatus.COMPLETED for f in fixtures):
            return  # 仍有未完成的实战对阵
        if tournament.format == TournamentFormat.SWISS:
            if tournament.current_round < (tournament.total_rounds or 0):
                return  # 还有未生成的轮次
            standings = self.compute_standings(tournament)
            tournament.winner_id = standings[0].account_id if standings else None
        else:
            if tournament.format == TournamentFormat.DOUBLE_ELIM:
                final = next(
                    (f for f in fixtures if f.bracket_side == BracketSide.MAIN),
                    None,
                )
            else:
                final = next(
                    (f for f in fixtures if f.advances_to is None and not f.is_bye),
                    None,
                )
            if final is None or final.winner_id is None:
                return
            tournament.winner_id = final.winner_id
        tournament.status = TournamentStatus.COMPLETED
        tournament.completed_at = now_ts()
        tournament.final_standings = self.compute_standings(tournament)
        self.db.tournaments.replace(tournament)
        self.logger.info("赛事 %s 完成，冠军 %s。", tournament.id, tournament.winner_id)

    # ============================================================ 双败淘汰

    def _generate_double_elim(self, tournament: Tournament) -> list[Fixture]:
        """胜者组（单败构）+ 败者组（2(W-1) 轮）+ grand final。

        限制：参赛人数须为 2 的幂（4/8/16...），否则报错（bye 处理留后续）。
        """
        participants = list(tournament.seed_order or tournament.participant_ids)
        if not tournament.seed_order and len(participants) > 1:
            random.shuffle(participants)
        n = len(participants)
        if n < 2:
            raise ValueError("双败赛制需至少 2 名选手")
        if n & (n - 1) != 0:
            raise ValueError("双败赛制当前仅支持 2 的幂人数（4/8/16...）")
        bracket_size = n
        winners_rounds = int(math.log2(bracket_size))
        losers_rounds = 2 * (winners_rounds - 1)

        winners: dict[int, list[Fixture]] = {
            r: [
                Fixture(
                    tournament_id=tournament.id,
                    round_no=r,
                    bracket_side=BracketSide.WINNERS,
                    match_index=k,
                )
                for k in range(bracket_size // (2**r))
            ]
            for r in range(1, winners_rounds + 1)
        }
        losers: dict[int, list[Fixture]] = {
            lr: [
                Fixture(
                    tournament_id=tournament.id,
                    round_no=lr,
                    bracket_side=BracketSide.LOSERS,
                    match_index=k,
                )
                for k in range(bracket_size // (2 ** (math.ceil(lr / 2) + 1)))
            ]
            for lr in range(1, losers_rounds + 1)
        }
        gf = Fixture(
            tournament_id=tournament.id,
            round_no=1,
            bracket_side=BracketSide.MAIN,
            match_index=0,
        )

        # 胜者组首轮填选手（N=bracket_size，无 bye）
        slots = _seed_slots(bracket_size)
        seeded = [participants[s - 1] for s in slots]
        for k, f in enumerate(winners[1]):
            f.player_a_id = seeded[2 * k]
            f.player_b_id = seeded[2 * k + 1]
            f.status = FixtureStatus.READY

        # 胜者组晋级链 + 败者下落
        for r in range(1, winners_rounds + 1):
            for k, f in enumerate(winners[r]):
                if r < winners_rounds:
                    tgt = winners[r + 1][k // 2]
                    f.advances_to = tgt.id
                    f.advances_slot = "A" if k % 2 == 0 else "B"
                else:
                    f.advances_to = gf.id  # 胜者组决赛胜者 → GF slot A
                    f.advances_slot = "A"
                if r == 1:
                    # W_R1 败者两两进 L_R1（minor）
                    tgt = losers[1][k // 2]
                    slot = "A" if k % 2 == 0 else "B"
                else:
                    # W_R_r 败者进 L_R_(2r-2)（major：L 胜者填 A，W 败者填 B）
                    tgt = losers[2 * r - 2][k]
                    slot = "B"
                f.losers_drops_to = tgt.id
                f.losers_drop_slot = slot

        # 败者组晋级链
        for lr in range(1, losers_rounds + 1):
            for k, f in enumerate(losers[lr]):
                if lr < losers_rounds:
                    nxt = lr + 1
                    if nxt % 2 == 1:  # 下轮 minor：两两合并
                        tgt = losers[nxt][k // 2]
                        slot = "A" if k % 2 == 0 else "B"
                    else:  # 下轮 major：L 胜者填 A
                        tgt = losers[nxt][k]
                        slot = "A"
                    f.advances_to = tgt.id
                    f.advances_slot = slot
                else:
                    f.advances_to = gf.id  # 败者组冠军 → GF slot B
                    f.advances_slot = "B"

        all_fixtures = (
            [f for rnd in winners for f in winners[rnd]]
            + [f for rnd in losers for f in losers[rnd]]
            + [gf]
        )
        for f in all_fixtures:
            self.db.fixtures.insert(f)
        tournament.total_rounds = winners_rounds + losers_rounds + 1
        tournament.current_round = 1
        tournament.bracket_generated_at = now_ts()
        tournament.status = TournamentStatus.IN_PROGRESS
        self.db.tournaments.replace(tournament)
        return winners[1]

    def _advance_double_elim(
        self, fixture: Fixture, winner_id: str, loser_id: str | None
    ) -> None:
        """胜者 → advances_to；败者 → losers_drops_to（仅胜者组 fixture 有）。"""
        for target_id, slot, value in (
            (fixture.advances_to, fixture.advances_slot, winner_id),
            (fixture.losers_drops_to, fixture.losers_drop_slot, loser_id),
        ):
            if not target_id or value is None or slot is None:
                continue
            target = self.db.fixtures.get(target_id)
            if target is None:
                continue
            if slot == "A":
                target.player_a_id = value
            else:
                target.player_b_id = value
            if target.player_a_id and target.player_b_id:
                target.status = FixtureStatus.READY
            self.db.fixtures.replace(target)

    def _standings_double_elim(
        self, tournament: Tournament
    ) -> list[TournamentStanding]:
        fixtures = self.db.fixtures.find_by_tournament(tournament.id)
        wins = dict.fromkeys(tournament.participant_ids, 0)
        losses: dict[str, int] = dict.fromkeys(tournament.participant_ids, 0)
        champion: str | None = None
        for f in fixtures:
            if f.winner_id is None or f.is_bye:
                continue
            wins[f.winner_id] = wins.get(f.winner_id, 0) + 1
            loser = f.player_b_id if f.winner_id == f.player_a_id else f.player_a_id
            if loser:
                losses[loser] = losses.get(loser, 0) + 1
            if f.bracket_side == BracketSide.MAIN:
                champion = f.winner_id
        standings: list[TournamentStanding] = []
        for pid in tournament.participant_ids:
            if pid == champion:
                standings.append(
                    TournamentStanding(
                        account_id=pid,
                        rank=1,
                        wins=wins[pid],
                        losses=losses[pid],
                        note="冠军",
                    )
                )
            else:
                standings.append(
                    TournamentStanding(
                        account_id=pid, rank=0, wins=wins[pid], losses=losses[pid]
                    )
                )
        # 冠军 rank1，其余按胜场降序、负场升序近似排名
        standings.sort(key=lambda s: (s.rank == 0, s.rank, -s.wins, s.losses))
        for i, s in enumerate(standings):
            if s.rank == 0:
                s.rank = i + 1
        return standings

    # ============================================================ 瑞士轮

    def _generate_swiss_first_round(self, tournament: Tournament) -> list[Fixture]:
        participants = list(tournament.seed_order or tournament.participant_ids)
        if not tournament.seed_order and len(participants) > 1:
            random.shuffle(participants)
        n = len(participants)
        if n < 2:
            raise ValueError("瑞士轮需至少 2 名选手")
        rounds = tournament.swiss_rounds or max(1, math.ceil(math.log2(n)))
        fixtures: list[Fixture] = []
        idx = 0
        for i in range(0, n - 1, 2):
            f = Fixture(
                tournament_id=tournament.id,
                round_no=1,
                bracket_side=BracketSide.MAIN,
                match_index=idx,
                player_a_id=participants[i],
                player_b_id=participants[i + 1],
                status=FixtureStatus.READY,
            )
            self.db.fixtures.insert(f)
            fixtures.append(f)
            idx += 1
        if n % 2 == 1:  # 末位轮空
            bye_p = participants[-1]
            f = Fixture(
                tournament_id=tournament.id,
                round_no=1,
                bracket_side=BracketSide.MAIN,
                match_index=idx,
                player_a_id=bye_p,
                is_bye=True,
                winner_id=bye_p,
                status=FixtureStatus.COMPLETED,
                completed_at=now_ts(),
            )
            self.db.fixtures.insert(f)
            fixtures.append(f)
        tournament.total_rounds = rounds
        tournament.current_round = 1
        tournament.bracket_generated_at = now_ts()
        tournament.status = TournamentStatus.IN_PROGRESS
        self.db.tournaments.replace(tournament)
        return fixtures

    def pair_swiss_round(self, tournament: Tournament, round_no: int) -> list[Fixture]:
        """生成瑞士轮第 ``round_no`` 轮（荷兰式同分优先 + 避免重赛）。

        要求 ``round_no - 1`` 轮已全部完成。由 ``POST /next-round`` 显式触发。
        """
        if tournament.format != TournamentFormat.SWISS:
            raise ValueError("仅瑞士轮赛制可生成下一轮")
        if tournament.total_rounds is None or round_no > tournament.total_rounds:
            raise ValueError("超过总轮数")
        if round_no > 1:
            prev = self.db.fixtures.find_by_tournament_round(
                tournament.id, round_no - 1
            )
            if any(not f.is_bye and f.status != FixtureStatus.COMPLETED for f in prev):
                raise ValueError("上一轮尚未全部完成")
        scores = self._swiss_scores(tournament)
        history = self._opponent_history(tournament)
        ranked = sorted(tournament.participant_ids, key=lambda p: -scores.get(p, 0))
        pairs, bye_player = self._swiss_pair(ranked, history)
        fixtures: list[Fixture] = []
        for idx, (a, b) in enumerate(pairs):
            f = Fixture(
                tournament_id=tournament.id,
                round_no=round_no,
                bracket_side=BracketSide.MAIN,
                match_index=idx,
                player_a_id=a,
                player_b_id=b,
                status=FixtureStatus.READY,
            )
            self.db.fixtures.insert(f)
            fixtures.append(f)
        if bye_player:
            f = Fixture(
                tournament_id=tournament.id,
                round_no=round_no,
                bracket_side=BracketSide.MAIN,
                match_index=len(pairs),
                player_a_id=bye_player,
                is_bye=True,
                winner_id=bye_player,
                status=FixtureStatus.COMPLETED,
                completed_at=now_ts(),
            )
            self.db.fixtures.insert(f)
            fixtures.append(f)
        tournament.current_round = round_no
        self.db.tournaments.replace(tournament)
        return fixtures

    @staticmethod
    def _swiss_pair(
        ranked: list[str], history: dict[str, set[str]]
    ) -> tuple[list[tuple[str, str]], str | None]:
        """荷兰式配对：按积分顺序，每人找首个未交手对手；末位（最低分）bye。"""
        remaining = list(ranked)
        bye_player = None
        if len(remaining) % 2 == 1:
            bye_player = remaining.pop()
        pairs: list[tuple[str, str]] = []
        while remaining:
            p1 = remaining.pop(0)
            partner = None
            for i, p2 in enumerate(remaining):
                if p2 not in history.get(p1, set()):
                    partner = remaining.pop(i)
                    break
            if partner is None:
                partner = remaining.pop(0)  # 都交过手 → 允许重赛（罕见）
            pairs.append((p1, partner))
        return pairs, bye_player

    def _swiss_scores(self, tournament: Tournament) -> dict[str, int]:
        fixtures = self.db.fixtures.find_by_tournament(tournament.id)
        scores = dict.fromkeys(tournament.participant_ids, 0)
        for f in fixtures:
            if f.winner_id is None or f.status != FixtureStatus.COMPLETED:
                continue
            loser = f.player_b_id if f.winner_id == f.player_a_id else f.player_a_id
            scores[f.winner_id] = (
                scores.get(f.winner_id, 0) + tournament.swiss_win_points
            )
            if loser:
                scores[loser] = scores.get(loser, 0) + tournament.swiss_loss_points
        return scores

    def _opponent_history(self, tournament: Tournament) -> dict[str, set[str]]:
        fixtures = self.db.fixtures.find_by_tournament(tournament.id)
        history: dict[str, set[str]] = {}
        for f in fixtures:
            if f.is_bye or f.winner_id is None:
                continue
            a, b = f.player_a_id, f.player_b_id
            if a and b:
                history.setdefault(a, set()).add(b)
                history.setdefault(b, set()).add(a)
        return history

    def _advance_swiss(
        self, fixture: Fixture, winner_id: str, loser_id: str | None
    ) -> None:
        """瑞士轮单场结束不推进对阵（下轮由 next-round 触发）；积分实时计算。"""
        _ = (fixture, winner_id, loser_id)
        return None

    def _standings_swiss(self, tournament: Tournament) -> list[TournamentStanding]:
        scores = self._swiss_scores(tournament)
        fixtures = self.db.fixtures.find_by_tournament(tournament.id)
        opponents: dict[str, list[str]] = {p: [] for p in tournament.participant_ids}
        for f in fixtures:
            if f.is_bye or f.winner_id is None:
                continue
            a, b = f.player_a_id, f.player_b_id
            if a and b:
                opponents.setdefault(a, []).append(b)
                opponents.setdefault(b, []).append(a)
        standings: list[TournamentStanding] = []
        for pid in tournament.participant_ids:
            buchholz = sum(scores.get(o, 0) for o in opponents.get(pid, []))
            wins = sum(1 for f in fixtures if f.winner_id == pid and not f.is_bye)
            standings.append(
                TournamentStanding(
                    account_id=pid,
                    rank=0,
                    points=scores.get(pid, 0),
                    wins=wins,
                    buchholz=buchholz,
                )
            )
        # 积分 → Buchholz → 胜场
        standings.sort(key=lambda s: (-s.points, -(s.buchholz or 0), -s.wins))
        for i, s in enumerate(standings):
            s.rank = i + 1
        return standings
