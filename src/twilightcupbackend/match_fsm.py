"""比赛生命周期状态机（MatchEngine）。

驱动 ``IDLE → PREP → COUNTDOWN → IN_ROUND → ROUND_JUDGING → ROUND_END`` 阶段切换，
管理 auto/manual 开始倒计时（auto 可被取消准备中断、manual 不可）。
所有内存状态变更在事件循环内，无锁。
回合内的计时上报/计分/判定在 M7 由本引擎扩展（``on_level_time`` 等方法）。
"""

from __future__ import annotations

from logging import Logger, getLogger
from typing import TYPE_CHECKING, Literal

from . import i18n, scoring
from .controllers import player_running_conflict
from .datatypes import (
    Attempt,
    AttemptStatus,
    CollectionConfig,
    LevelTime,
    Match,
    MatchLog,
    MatchPhase,
    MatchStatus,
    Pick,
    PickType,
    PlayerRoundState,
    PlayerStatus,
    RoundRecord,
    RoundSource,
    RoundVerdict,
    ScoringMethod,
    Seat,
    SystemEvent,
    now_ts,
)
from .protocol import (
    PRELOAD_CAP,
    PreloadReportStatus,
    PreloadStatus,
    SrvCountdownAbort,
    SrvCountdownTick,
    SrvCumulativeScore,
    SrvError,
    SrvLevelTimeUpdate,
    SrvMatchEnd,
    SrvPhaseChange,
    SrvPickAnnounced,
    SrvPlayerStatus,
    SrvPreloadState,
    SrvReadyState,
    SrvRoundResult,
    SrvRoundStart,
    SrvRoundStartedBroadcast,
    SrvVerdictEdit,
)
from .timer_service import CountdownTimer

if TYPE_CHECKING:
    from .connection_manager import ConnectionManager
    from .controllers import DBController
    from .stores import MatchStore

# CT 词条合法值（backend-ct-pick-tags §2.1）：单关（type=SINGLE）额外允许 Achievement
CT_TAG_VALUES = frozenset(
    {"Glitchless", "Pinch", "Checkpoint", "Jumpless", "No Checkpoint", "No EC"}
)
CT_TAG_SINGLE_ONLY = frozenset({"Achievement"})
CT_TAG_CONFLICTS = frozenset({"Checkpoint", "No Checkpoint"})
CT_CATEGORY = "CT"
# 可携带词条的类别：CT（裁判选定）、EX（裁判选定，不受词条 ban 约束——仅前端约束，
# 后端不区分）、CP（前端自动传入 Checkpoint）
TAGGED_CATEGORIES = frozenset({"CT", "EX", "CP"})
# 重试次数改由裁判选图时指定的类别（单关必填）
REFEREE_RETRY_CATEGORIES = frozenset({"CT", "EX"})


def _validate_pick_tags(
    pick: Pick, tags: list[str], limit: int
) -> tuple[str, dict[str, object]] | None:
    """校验裁判随选图提交的词条；违规返回 (消息键, 参数)，否则 None。

    规则（backend-ct-pick-tags §2.1，扩展到 CT/EX/CP）：仅这三类别可携带、
    数量 ≤ 本场 ct_tag_count、枚举内（单关额外允许 Achievement）、
    Checkpoint 与 No Checkpoint 互斥。
    """
    if not tags:
        return None
    if pick.category not in TAGGED_CATEGORIES:
        return "pick.tags_category", {"category": pick.category, "code": pick.code}
    if len(tags) > limit:
        return "pick.tags_too_many", {"limit": limit, "tags": tags}
    allowed = (
        CT_TAG_VALUES | CT_TAG_SINGLE_ONLY
        if pick.type == PickType.SINGLE
        else CT_TAG_VALUES
    )
    for tag in tags:
        if tag not in allowed:
            return "pick.tags_invalid", {"tags": tag}
    if CT_TAG_CONFLICTS.issubset(tags):
        return "pick.tags_conflict", {}
    return None


def _validate_pick_retry(
    pick: Pick, retry_count: int | None
) -> tuple[str, dict[str, object]] | None:
    """校验裁判随选图提交的重试次数；违规返回 (消息键, 参数)，否则 None。

    规则：CT/EX 单关必填且 ≥1（图池不再预设）；其余类别沿用图池预设，传入即拒绝。
    """
    is_referee_retry = (
        pick.type == PickType.SINGLE and pick.category in REFEREE_RETRY_CATEGORIES
    )
    if is_referee_retry:
        if retry_count is None:
            return "pick.retry_required", {"code": pick.code, "category": pick.category}
        if retry_count < 1:
            return "pick.retry_min", {"value": retry_count}
        return None
    if retry_count is not None:
        return "pick.retry_not_allowed", {
            "code": pick.code,
            "category": pick.category,
        }
    return None


class MatchEngine:
    def __init__(
        self,
        cm: ConnectionManager,
        db: DBController,
        logger: Logger | None = None,
    ) -> None:
        self.cm = cm
        self.db = db
        self.logger = logger or getLogger("MatchEngine")

    # ------------------------------------------------------------------
    # 日志与系统事件
    # ------------------------------------------------------------------

    def _ensure_match_log(self, store: MatchStore) -> MatchLog:
        log = self.db.match_logs.get_by_match(store.id)
        if log is not None:
            return log
        match = store.match
        log = MatchLog(
            match_id=store.id,
            initial_info={
                "name": match.name,
                "bo_format": match.bo_format,
                "win_threshold": match.win_threshold,
                "scoring_method": match.scoring_method.name,
                "start_countdown_delay": match.start_countdown_delay,
                "ban_count": match.ban_count,
                "protect_count": match.protect_count,
                "ct_tag_count": match.ct_tag_count,
                "player_a_id": match.player_a_id,
                "player_b_id": match.player_b_id,
                "referee_id": match.referee_id,
                "director_id": match.director_id,
                "mappool": [p.code for p in match.mappool.all_picks()],
            },
        )
        self.db.match_logs.insert(log)
        return log

    def _log_event(self, match_id: str, kind: str, payload: dict[str, object]) -> None:
        self.db.system_events.insert(
            SystemEvent(match_id=match_id, kind=kind, payload=payload)
        )

    def _username_of(self, account_id: str | None) -> str:
        """account_id → 登录用户名（score 消息标识选手用；查不到返回空串）。"""
        if not account_id:
            return ""
        acc = self.db.accounts.get(account_id)
        return acc.username if acc else ""

    def save_draft_snapshot(self, store: MatchStore, snapshot: dict | None) -> None:
        """持久化 ban/pick/protect 草稿快照到 match_log（见 backend-banpick-persist）。

        开赛前草稿可能先于首个回合到达，此时 match_log 尚不存在 → 由
        ``_ensure_match_log`` 按需创建（含 initial_info）。snapshot 为 None 时
        写空（不展示）；未变化则跳过写库。
        """
        log = self._ensure_match_log(store)
        if log.draft_snapshot == snapshot:
            return
        log.draft_snapshot = snapshot
        self.db.match_logs.replace(log)

    # ------------------------------------------------------------------
    # 准备阶段
    # ------------------------------------------------------------------

    async def begin_prep(self, match_id: str) -> None:
        store = self.cm.registry.get(match_id)
        if store is None:
            return
        if store.phase not in (MatchPhase.IDLE, MatchPhase.ROUND_END):
            await self.cm.system_message(
                match_id,
                self.cm.tr(match_id, "prep.phase_error", phase=store.phase.name),
                kind="error",
            )
            return
        # 比分已达取胜分数：胜负已定（自动结束流程应已把比赛收尾），
        # 不开新回合；改判把比分改回阈值以下后本守卫自动放行
        if self._decided_winner(store) is not None:
            await self.cm.send_to_seat(
                match_id,
                Seat.REFEREE,
                SrvError(code=400, msg=self.cm.tr(match_id, "prep.score_decided")),
            )
            return
        # 首次进入准备 = 激活比赛（CREATED → RUNNING）：
        # 强制双方选手不在另一场 RUNNING 比赛中
        if store.match.status == MatchStatus.CREATED:
            conflict = self._players_conflict(store.match)
            if conflict:
                await self.cm.send_to_seat(
                    match_id, Seat.REFEREE, SrvError(code=400, msg=conflict)
                )
                await self.cm.system_message(match_id, conflict, kind="error")
                return
            store.match.status = MatchStatus.RUNNING
            store.match.started_at = now_ts()
            self.db.matches.replace(store.match)
        store.phase = MatchPhase.PREP
        store.reset_ready()
        store.pending_pick_code = None
        store.pending_pick_tags = []
        store.pending_pick_retry = None
        store.pick_announced = None
        await self.cm.broadcast_match(match_id, SrvPhaseChange(phase=MatchPhase.PREP))
        await self.cm.system_message(
            match_id,
            self.cm.tr(match_id, "prep.started"),
            kind="prep",
        )
        await self.cm.broadcast_match(
            match_id,
            SrvReadyState(a_ready=store.a_ready, b_ready=store.b_ready),
        )
        await self._reset_preload(store)

    def _players_conflict(self, match: Match) -> str | None:
        """单场强制：本场选手若已在另一场 RUNNING 比赛中，返回冲突文案；否则 None。

        用于 begin_prep 激活时拦截，保证「同一选手同时只在一场活跃比赛」。
        委托共享 ``player_running_conflict``（与 REST start/resume/create/PATCH 一致）。
        """
        return player_running_conflict(
            self.db,
            match.player_a_id,
            match.player_b_id,
            self_id=match.id,
        )

    async def select_pick(
        self,
        match_id: str,
        pick_code: str,
        tags: list[str] | None = None,
        retry_count: int | None = None,
    ) -> None:
        store = self.cm.registry.get(match_id)
        if store is None:
            return
        # 倒计时/回合进行中拒绝改图（预载门控 R3.2：防倒计时中改图与预载赛跑）；
        # PREP/IDLE/ROUND_END 保持开放（词条校验本身与阶段无关）
        if store.phase in (MatchPhase.COUNTDOWN, MatchPhase.IN_ROUND):
            await self.cm.send_to_seat(
                match_id,
                Seat.REFEREE,
                SrvError(
                    code=400,
                    msg=self.cm.tr(
                        match_id, "pick.phase_error", phase=store.phase.name
                    ),
                ),
            )
            return
        pick = store.match.mappool.get_pick(pick_code)
        if pick is None:
            await self.cm.send_to_seat(
                match_id,
                Seat.REFEREE,
                SrvError(
                    code=400,
                    msg=self.cm.tr(match_id, "pick.not_in_mappool", code=pick_code),
                ),
            )
            return
        tags = tags or []
        # 词条 / 重试次数校验（backend-ct-pick-tags §2.1）：客户端已拦截但不可信任
        error = _validate_pick_tags(pick, tags, store.match.ct_tag_count)
        if error is None:
            error = _validate_pick_retry(pick, retry_count)
        if error is not None:
            key, kw = error
            await self.cm.send_to_seat(
                match_id,
                Seat.REFEREE,
                SrvError(code=400, msg=self.cm.tr(match_id, key, **kw)),
            )
            return
        store.pending_pick_code = pick_code
        store.pending_pick_tags = tags
        store.pending_pick_retry = retry_count
        # 改图（含重新应用）旧预载必然作废 → 重置并广播，再提前下发新合集
        await self._reset_preload(store)
        suffix = f" [{', '.join(tags)}]" if tags else ""
        # 重试后缀用生效值：CT/EX 单关为裁判指定值，其余单关沿用图池预设
        # （与 _pending_pick_enriched / pick_announced 的合并口径一致；
        # 多关 retry 恒 None 不显示）
        effective_retry = retry_count if retry_count is not None else pick.retry_count
        retry_suffix = f" x{effective_retry}" if effective_retry is not None else ""
        await self.cm.system_message(
            match_id,
            self.cm.tr(
                match_id,
                "pick.selected",
                code=pick.code,
                name=pick.name,
                tags=suffix,
                retry=retry_suffix,
            ),
            kind="pick",
        )
        # 合集提前下发（预览；round_start 仍是唯一权威）。全体成员（两选手席 +
        # 裁判 + 导播），选手端以最新一次为准（丢弃旧预载）；快照留存供
        # PREP 断线重连握手重放。
        announced = SrvPickAnnounced(
            pick_code=pick.code,
            pick=self._pending_pick_enriched(store, pick),
            collection=self._expand_collection(pick.collection),
        )
        store.pick_announced = announced
        await self.cm.broadcast_match(match_id, announced)

    # ------------------------------------------------------------------
    # 就绪 → 倒计时
    # ------------------------------------------------------------------

    async def on_ready_changed(self, match_id: str) -> None:
        """``!ready`` 切换后调用：双就绪走预载门控的自动开始检查，否则中断倒计时。"""
        store = self.cm.registry.get(match_id)
        if store is None:
            return
        both = store.a_ready and store.b_ready
        if store.phase == MatchPhase.PREP and both:
            await self._auto_start_check(store)
        elif (
            store.phase == MatchPhase.COUNTDOWN
            and store.countdown_source == "auto"
            and not both
        ):
            await self._abort_countdown(store)

    async def manual_start(self, match_id: str) -> None:
        store = self.cm.registry.get(match_id)
        if store is None:
            return
        if store.phase != MatchPhase.PREP:
            await self.cm.send_to_seat(
                match_id,
                Seat.REFEREE,
                SrvError(code=400, msg=self.cm.tr(match_id, "start.only_prep")),
            )
            return
        # 手动开始无条件放行（现状）；存在预载未完/等待中席位时向全体播提示
        if self._preload_incomplete(store):
            await self.cm.system_message(
                store.id, self.cm.tr(store.id, "preload.manual_skip"), kind="preload"
            )
        await self._start_countdown(store, "manual")

    async def _start_countdown(self, store: MatchStore, source: str) -> None:
        if store.phase == MatchPhase.COUNTDOWN:
            return  # 已在倒计时
        # 门控等待的超时计时器一并撤销（门控通过或手动强制两条路径都到这里）
        await self._cancel_preload_gate_timer(store)
        if store.pending_pick_code is None:
            await self.cm.send_to_seat(
                store.id,
                Seat.REFEREE,
                SrvError(code=400, msg=self.cm.tr(store.id, "start.no_pick")),
            )
            return
        delay = store.match.start_countdown_delay
        store.phase = MatchPhase.COUNTDOWN
        store.countdown_source = source
        await self.cm.broadcast_match(
            store.id, SrvPhaseChange(phase=MatchPhase.COUNTDOWN)
        )
        intro_key = (
            "countdown.intro_auto" if source == "auto" else "countdown.intro_manual"
        )
        await self.cm.system_message(
            store.id, self.cm.tr(store.id, intro_key, delay=delay), kind="countdown"
        )

        match_id = store.id

        async def on_tick(remaining: int) -> None:
            await self.cm.broadcast_match(
                match_id,
                SrvCountdownTick(remaining_secs=remaining, source=source),  # type: ignore[arg-type]
            )
            await self.cm.system_message(match_id, str(remaining), kind="countdown")

        async def on_zero() -> None:
            await self._on_countdown_zero(store)

        timer = CountdownTimer(delay, source, on_tick, on_zero)
        store.countdown_timer = timer
        timer.start()

    async def _abort_countdown(self, store: MatchStore) -> None:
        if store.countdown_timer is not None:
            await store.countdown_timer.cancel()
        store.countdown_timer = None
        store.countdown_source = None
        store.phase = MatchPhase.PREP
        await self.cm.system_message(
            store.id, self.cm.tr(store.id, "countdown.cancelled"), kind="countdown"
        )
        await self.cm.broadcast_match(
            store.id, SrvCountdownAbort(reason="player_unready")
        )
        await self.cm.broadcast_match(store.id, SrvPhaseChange(phase=MatchPhase.PREP))
        # 倒计时中止回 PREP：预载状态随之重置（选手重新 ready 后重走门控）
        await self._reset_preload(store)

    async def _on_countdown_zero(self, store: MatchStore) -> None:
        store.countdown_timer = None
        store.countdown_source = None
        await self._begin_round(store)

    # ------------------------------------------------------------------
    # 预载状态与开局门控（合集提前下发与预载门控 R2）
    # ------------------------------------------------------------------

    async def on_preload_report(
        self,
        match_id: str,
        seat: Seat,
        status: PreloadReportStatus,
        detail: str | None,
    ) -> None:
        """选手端预载状态上报：更新并广播，失败告警，然后复查自动开始。"""
        store = self.cm.registry.get(match_id)
        if store is None:
            return
        if store.phase != MatchPhase.PREP:
            # 非 PREP 上报无意义（进入回合时状态已清场），静默忽略
            return
        if seat == Seat.PLAYER_A:
            store.preload_a = status
        else:
            store.preload_b = status
        await self.cm.broadcast_match(
            store.id,
            SrvPreloadState(a_status=store.preload_a, b_status=store.preload_b),
        )
        if status == "failed":
            # 不阻塞开局（选手端 round_start 时回退标准加载路径），仅告警
            suffix = f": {detail}" if detail else ""
            await self.cm.system_message(
                store.id,
                self.cm.tr(
                    store.id,
                    "preload.failed",
                    seat=i18n.seat_name(seat, self.cm.locale_for(store.id)),
                    detail=suffix,
                ),
                kind="preload",
            )
            self._log_event(
                store.id, "preload_failed", {"seat": seat.name, "detail": detail}
            )
        # 最后一份上报可能早于对手 ready：满足条件的瞬间立即复查，避免卡住
        await self._auto_start_check(store)

    async def _auto_start_check(self, store: MatchStore) -> None:
        """双方就绪后的自动开始入口（ready 切换与 preload_report 共用）。

        自动倒计时开始条件 = 双方 ready 且双方预载门控通过；门控不满足时
        维持 PREP 并挂超时兜底计时器。
        """
        if store.phase != MatchPhase.PREP or not (store.a_ready and store.b_ready):
            return
        if store.pending_pick_code is None:
            # 与 _start_countdown 的拒绝一致：提示裁判先选图（保留现状行为）
            await self.cm.send_to_seat(
                store.id,
                Seat.REFEREE,
                SrvError(code=400, msg=self.cm.tr(store.id, "start.no_pick")),
            )
            return
        if self._preload_gate_ok(store):
            await self._start_countdown(store, "auto")
        else:
            self._ensure_preload_gate_timer(store)

    def _preload_gate_ok(self, store: MatchStore) -> bool:
        """双方席位门控均通过才放行。"""
        return all(
            self._seat_gate_ok(store, seat, status)
            for seat, status in (
                (Seat.PLAYER_A, store.preload_a),
                (Seat.PLAYER_B, store.preload_b),
            )
        )

    @staticmethod
    def _seat_gate_ok(store: MatchStore, seat: Seat, status: PreloadStatus) -> bool:
        """单席位门控：done/na/failed 通过（failed 收到时已告警，不阻塞）；
        in_progress 等待；absent 仅在该席位连接声明了预载能力（新版插件）时等待，
        旧客户端/断线席位视为豁免（在途的非 absent 状态不受断线影响）。
        """
        if status in ("done", "na", "failed"):
            return True
        if status == "in_progress":
            return False
        conn = store.connections.get(seat)
        return conn is None or PRELOAD_CAP not in conn.capabilities

    def _preload_incomplete(self, store: MatchStore) -> bool:
        """是否存在明确的预载未完信号（手动开始跳过提示的触发条件）。

        任一席位 in_progress/failed，或门控等待计时器仍在走（双方 ready 但
        有能力席位尚未上报）。纯 absent 且未在等待的常规手动开始不提示。
        """
        return (
            store.preload_gate_timer is not None
            or store.preload_a in ("in_progress", "failed")
            or store.preload_b in ("in_progress", "failed")
        )

    def _ensure_preload_gate_timer(self, store: MatchStore) -> None:
        """门控等待的超时兜底计时器；已计时则不重置（起点=首次进入等待的时刻）。

        超时后强制开始（选手端 round_start 时回退标准加载）。配置 ≤0 视为
        关闭兜底（只等门控通过或裁判手动开始）。
        """
        if store.preload_gate_timer is not None:
            return
        timeout = self.cm.settings.preload_gate_timeout
        if timeout <= 0:
            return

        async def on_tick(remaining: int) -> None:  # 静默走秒，不广播
            pass

        async def on_zero() -> None:
            store.preload_gate_timer = None
            # 防御性复查：取消路径之外的局面变化（如等待中断连）兜住
            if (
                store.phase != MatchPhase.PREP
                or not (store.a_ready and store.b_ready)
                or store.pending_pick_code is None
            ):
                return
            await self.cm.system_message(
                store.id, self.cm.tr(store.id, "preload.timeout_force"), kind="preload"
            )
            self._log_event(
                store.id,
                "preload_gate_timeout",
                {"a": store.preload_a, "b": store.preload_b},
            )
            await self._start_countdown(store, "auto")

        timer = CountdownTimer(timeout, "preload_gate", on_tick, on_zero)
        store.preload_gate_timer = timer
        timer.start()

    async def _cancel_preload_gate_timer(self, store: MatchStore) -> None:
        if store.preload_gate_timer is not None:
            await store.preload_gate_timer.cancel()
            store.preload_gate_timer = None

    async def _reset_preload(self, store: MatchStore) -> None:
        """重置双方预载状态并广播（进 PREP / 改图 / 倒计时中止 / 回合开始）。"""
        await self._cancel_preload_gate_timer(store)
        store.preload_a = "absent"
        store.preload_b = "absent"
        await self.cm.broadcast_match(
            store.id,
            SrvPreloadState(a_status=store.preload_a, b_status=store.preload_b),
        )

    # ------------------------------------------------------------------
    # 回合开始（回合内数据流由 M7 扩展）
    # ------------------------------------------------------------------

    def _expand_collection(self, collection: CollectionConfig) -> CollectionConfig:
        """把 raw 里的关卡 id 展开为关卡名（插件契约：下发的是名字）。

        多关 ``{"levels": [id, ...]}`` → ``{"levels": [名, ...]}``；
        单关 ``{"level": id}`` 同理。查不到的 id 原样保留（关卡库被删不致下发崩）。
        """
        raw = dict(collection.raw)
        if isinstance(raw.get("levels"), list):
            raw["levels"] = [self._level_name(x) for x in raw["levels"]]
        if isinstance(raw.get("level"), str):
            raw["level"] = self._level_name(raw["level"])
        return CollectionConfig(raw=raw)

    def _level_name(self, level_id: str) -> str:
        """level_id → 关卡名；查不到（或本身已是名字）原样返回。"""
        lv = self.db.levels.get(level_id)
        return lv.name if lv is not None else level_id

    def _pending_pick_enriched(self, store: MatchStore, pick: Pick) -> Pick:
        """按 pending 裁剪合并出下发的 pick（pick_announced 与 round_start 共用）。

        回合 pick 快照冻结本回合词条与裁判指定的重试次数（图池原件不动；
        见 backend-ct-pick-tags §2.2）。未指定重试（ML/IL 单关）沿用图池预设。
        single_scoring 随快照下发本场单关计分方式（LEADERBOARD_REQ §4.2-B；
        Match.scoring_method 建赛时定、必填，恒非 None）。
        """
        return pick.model_copy(
            update={
                "tags": list(store.pending_pick_tags),
                "retry_count": (
                    store.pending_pick_retry
                    if store.pending_pick_retry is not None
                    else pick.retry_count
                ),
                "single_scoring": store.match.scoring_method.name.lower(),
            }
        )

    async def _begin_round(self, store: MatchStore) -> None:
        assert store.pending_pick_code is not None
        pick = store.match.mappool.get_pick(store.pending_pick_code)
        if pick is None:
            await self.cm.system_message(
                store.id, self.cm.tr(store.id, "round.invalid_pick"), kind="error"
            )
            store.phase = MatchPhase.PREP
            await self.cm.broadcast_match(
                store.id, SrvPhaseChange(phase=MatchPhase.PREP)
            )
            return
        # 回合开始后预载状态不再有意义，清场
        await self._reset_preload(store)

        pick = self._pending_pick_enriched(store, pick)

        store.round_counter += 1
        record = RoundRecord(
            match_id=store.id,
            round_no=store.round_counter,
            pick_code=pick.code,
            pick_snapshot=pick,
            collection_snapshot=pick.collection,  # 持久存关卡 id（审计可追溯）
            state_a=PlayerRoundState(account_id=store.match.player_a_id),
            state_b=PlayerRoundState(account_id=store.match.player_b_id),
        )
        self.db.rounds.insert(record)
        store.current_round_id = record.id
        store.phase = MatchPhase.IN_ROUND
        match_log = self._ensure_match_log(store)
        match_log.round_ids.append(record.id)
        self.db.match_logs.replace(match_log)

        # 下发给插件的 collection.raw 用关卡名（插件契约）；查不到的 id 原样保留
        collection_out = self._expand_collection(pick.collection)
        await self.cm.send_to_seat(
            store.id,
            Seat.PLAYER_A,
            SrvRoundStart(round_id=record.id, pick=pick, collection=collection_out),
        )
        await self.cm.send_to_seat(
            store.id,
            Seat.PLAYER_B,
            SrvRoundStart(round_id=record.id, pick=pick, collection=collection_out),
        )
        await self.cm.broadcast_match(
            store.id, SrvPhaseChange(phase=MatchPhase.IN_ROUND, round_id=record.id)
        )
        await self.cm.system_message(
            store.id,
            self.cm.tr(store.id, "round.started", code=pick.code, name=pick.name),
            kind="round_start",
        )
        await self.cm.broadcast_match(
            store.id,
            SrvRoundStartedBroadcast(
                round_id=record.id,
                pick_code=pick.code,
                pick_name=pick.name,
                tags=list(pick.tags),
            ),
        )

    # ------------------------------------------------------------------
    # 回合进行中：计时上报 / 完成 / 弃权
    # ------------------------------------------------------------------

    @staticmethod
    def _state_of(record: RoundRecord, seat: Seat) -> PlayerRoundState:
        return record.state_a if seat == Seat.PLAYER_A else record.state_b

    async def _require_active_round(
        self, store: MatchStore, round_id: str
    ) -> RoundRecord | None:
        if store.phase != MatchPhase.IN_ROUND or store.current_round_id != round_id:
            return None  # 迟到的上报，忽略
        return self.db.rounds.get(round_id)

    async def on_level_time_upload(
        self,
        match_id: str,
        seat: Seat,
        round_id: str,
        level_index: int,
        this_level_ms: int,
        total_ms: int | None,
        invalid_reasons: list[str] | None = None,
    ) -> None:
        store = self.cm.registry.get(match_id)
        if store is None:
            return
        record = await self._require_active_round(store, round_id)
        if record is None:
            return
        state = self._state_of(record, seat)
        if record.pick_snapshot.type == PickType.MULTI:
            # 幂等 upsert（按 level_index），支持断线重连后补传去重；
            # invalid_reasons 透传（informational，多关仲裁归裁判人工）
            new_lt = LevelTime(
                level_index=level_index,
                time_ms=this_level_ms,
                total_ms=total_ms,
                invalid_reasons=invalid_reasons or [],
            )
            levels = state.completed_levels
            for i, existing in enumerate(levels):
                if existing.level_index == level_index:
                    levels[i] = new_lt
                    break
            else:
                levels.append(new_lt)
            state.current_level_index = max(state.current_level_index, level_index + 1)
        else:  # 单关：本次尝试成绩（按 level_index 作为尝试序号幂等 upsert）
            # 带无效标记完成的尝试 → INVALID：time_ms 保留作证据（裁判仲裁），
            # 计分经 scoring.single_score 的 status==VALID 过滤自动排除
            # （INVALID_ATTEMPT_REQ §4.3）
            new_attempt = Attempt(
                index=level_index,
                status=AttemptStatus.INVALID
                if invalid_reasons
                else AttemptStatus.VALID,
                time_ms=this_level_ms,  # 保留（证据）
                invalid_reasons=invalid_reasons or [],
            )
            attempts = state.attempts
            for i, existing in enumerate(attempts):
                if existing.index == level_index:
                    attempts[i] = new_attempt
                    break
            else:
                attempts.append(new_attempt)
            state.current_level_index = max(
                state.current_level_index, len(state.attempts)
            )
        self.db.rounds.replace(record)
        await self.cm.broadcast_match(
            match_id,
            SrvLevelTimeUpdate(
                seat=seat.name,
                account_id=state.account_id,
                level_index=level_index,
                this_level_ms=this_level_ms,
                total_ms=total_ms,
                invalid_reasons=invalid_reasons,
            ),
        )
        await self._broadcast_status(match_id, seat, state)

    async def on_attempt_skip(
        self, match_id: str, seat: Seat, round_id: str, attempt_index: int
    ) -> None:
        store = self.cm.registry.get(match_id)
        if store is None:
            return
        record = await self._require_active_round(store, round_id)
        if record is None:
            return
        # 仅单关回合有「跳过尝试」语义（MULTI 选手端不发该消息，稳妥过滤）
        if record.pick_snapshot.type != PickType.SINGLE:
            return
        state = self._state_of(record, seat)
        # upsert（与 on_level_time_upload 幂等写法对齐）：从未上报过的尝试
        # 也要落一条 SKIPPED 记录，否则跳过的尝试凭空消失（attempt-skip-record）
        for attempt in state.attempts:
            if attempt.index == attempt_index:
                attempt.status = AttemptStatus.SKIPPED
                attempt.time_ms = None
                break
        else:
            state.attempts.append(
                Attempt(index=attempt_index, status=AttemptStatus.SKIPPED)
            )
        self.db.rounds.replace(record)
        await self._broadcast_status(match_id, seat, state)

    async def on_project_complete(
        self,
        match_id: str,
        seat: Seat,
        round_id: str,
        final_total_ms: int | None,
    ) -> None:
        store = self.cm.registry.get(match_id)
        if store is None:
            return
        record = await self._require_active_round(store, round_id)
        if record is None:
            return
        state = self._state_of(record, seat)
        if record.pick_snapshot.type == PickType.MULTI and final_total_ms is not None:
            state.final_total_ms = final_total_ms
        else:  # 单关：未完成的剩余尝试标记为 N/A
            for attempt in state.attempts:
                if attempt.status == AttemptStatus.UNFINISHED:
                    attempt.status = AttemptStatus.SKIPPED
        state.status = PlayerStatus.COMPLETED
        self.db.rounds.replace(record)
        await self._broadcast_status(match_id, seat, state)
        await self._maybe_to_judging(store, record)

    async def on_forfeit(
        self, match_id: str, seat: Seat, round_id: str, reason: str
    ) -> None:
        store = self.cm.registry.get(match_id)
        if store is None:
            return
        # 弃权即便对方仍在进行也可推进判定，但需当前回合匹配
        if store.current_round_id != round_id:
            return
        record = self.db.rounds.get(round_id)
        if record is None:
            return
        state = self._state_of(record, seat)
        state.forfeited = True
        state.status = PlayerStatus.FORFEITED
        self.db.rounds.replace(record)
        await self._broadcast_status(match_id, seat, state)
        await self.cm.system_message(
            match_id,
            self.cm.tr(
                match_id,
                "forfeit.done",
                player=i18n.seat_name(seat, self.cm.locale_for(match_id)),
                reason=reason,
            ),
            kind="forfeit",
        )
        self._log_event(match_id, "forfeit", {"seat": seat.name, "reason": reason})
        await self._maybe_to_judging(store, record)

    async def _broadcast_status(
        self, match_id: str, seat: Seat, state: PlayerRoundState
    ) -> None:
        await self.cm.broadcast_match(
            match_id,
            SrvPlayerStatus(
                seat=seat.name,
                account_id=state.account_id,
                status=state.status,
                current_level_index=state.current_level_index,
                completed_levels=state.completed_levels,
                attempts=state.attempts,
            ),
        )

    @staticmethod
    def _both_terminal(record: RoundRecord) -> bool:
        return (
            record.state_a.status != PlayerStatus.IN_GAME
            and record.state_b.status != PlayerStatus.IN_GAME
        )

    async def _maybe_to_judging(self, store: MatchStore, record: RoundRecord) -> None:
        if store.phase == MatchPhase.IN_ROUND and self._both_terminal(record):
            store.phase = MatchPhase.ROUND_JUDGING
            await self.cm.broadcast_match(
                store.id,
                SrvPhaseChange(phase=MatchPhase.ROUND_JUDGING, round_id=record.id),
            )
            await self.cm.system_message(
                store.id,
                self.cm.tr(store.id, "judging.await"),
                kind="judging",
            )

    async def on_reconnect_resync(
        self, match_id: str, seat: Seat, round_id: str
    ) -> None:
        """选手断线重连后请求本回合权威快照（双方状态），客户端据此补传缺失上报。"""
        store = self.cm.registry.get(match_id)
        if store is None:
            return
        record = self.db.rounds.get(round_id)
        if record is None:
            await self.cm.send_to_seat(
                match_id,
                seat,
                SrvError(code=404, msg=self.cm.tr(match_id, "round.not_found")),
            )
            return
        own = self._state_of(record, seat)
        opp_seat = Seat.PLAYER_B if seat == Seat.PLAYER_A else Seat.PLAYER_A
        opp = self._state_of(record, opp_seat)
        await self.cm.send_to_seat(
            match_id,
            seat,
            SrvPlayerStatus(
                seat=seat.name,
                account_id=own.account_id,
                status=own.status,
                current_level_index=own.current_level_index,
                completed_levels=own.completed_levels,
                attempts=own.attempts,
            ),
        )
        await self.cm.send_to_seat(
            match_id,
            seat,
            SrvPlayerStatus(
                seat=opp_seat.name,
                account_id=opp.account_id,
                status=opp.status,
                current_level_index=opp.current_level_index,
                completed_levels=opp.completed_levels,
                attempts=opp.attempts,
            ),
        )

    async def terminate_round(self, match_id: str, round_id: str, reason: str) -> None:
        """裁判强制终止当前回合（选手崩溃/断连不可判断，§10.1）。

        进入待判定态，由裁判随后下达断连判负或重赛。
        """
        store = self.cm.registry.get(match_id)
        if store is None:
            return
        if store.current_round_id != round_id:
            await self.cm.send_to_seat(
                match_id,
                Seat.REFEREE,
                SrvError(code=400, msg=self.cm.tr(match_id, "round.not_current")),
            )
            return
        store.phase = MatchPhase.ROUND_JUDGING
        await self.cm.system_message(
            match_id,
            self.cm.tr(match_id, "round.terminated", reason=reason),
            kind="terminate",
        )
        self._log_event(match_id, "terminate", {"round_id": round_id, "reason": reason})
        await self.cm.broadcast_match(
            match_id,
            SrvPhaseChange(phase=MatchPhase.ROUND_JUDGING, round_id=round_id),
        )

    async def end_match(self, match_id: str) -> None:
        """裁判手动结束比赛（backend-manual-match-end）。

        胜方自动按比分判定（达到取胜分数的一方）；未决出胜方时拒绝。
        常规流程下达到取胜分数时 ``_apply_verdict`` 已自动结束，本入口
        主要兜底改判把比分改回阈值以下再改回达阈、或暂停/异常卡住的场。
        结束后广播 match_end/MATCH_END、踢出选手、通知赛程引擎。
        """
        store = self.cm.registry.get(match_id)
        if store is None:
            return
        if (
            store.match.status == MatchStatus.ENDED
            or store.phase == MatchPhase.MATCH_END
        ):
            await self.cm.send_to_seat(
                match_id,
                Seat.REFEREE,
                SrvError(code=400, msg=self.cm.tr(match_id, "match.already_ended")),
            )
            return
        winner = self._decided_winner(store)
        if winner is None:
            await self.cm.send_to_seat(
                match_id,
                Seat.REFEREE,
                SrvError(code=400, msg=self.cm.tr(match_id, "match.no_winner")),
            )
            return
        await self._end_match(store, winner)

    @staticmethod
    def _decided_winner(store: MatchStore) -> Literal["A", "B"] | None:
        """胜负已定的一方（达到取胜分数）；未决出时 None。"""
        threshold = store.match.win_threshold
        if store.wins_a >= threshold:
            return "A"
        if store.wins_b >= threshold:
            return "B"
        return None

    async def force_end_full(self, match_id: str) -> bool:
        """管理端强制结束的完整流程：按比分推导胜方后走 ``_end_match``。

        推导顺序：达到取胜分数的一方 → 领先一方；比分持平则无法推导，
        返回 False（调用方回退到仅标记 ENDED + 释放选手的最小流程）。
        """
        store = self.cm.registry.get(match_id)
        if store is None:
            return False
        winner = self._decided_winner(store)
        if winner is None:
            if store.wins_a > store.wins_b:
                winner = "A"
            elif store.wins_b > store.wins_a:
                winner = "B"
            else:
                return False
        await self._end_match(store, winner)
        return True

    # ------------------------------------------------------------------
    # 判定 / 计分 / 重赛 / 比赛结束
    # ------------------------------------------------------------------

    def _compute_scores(self, record: RoundRecord) -> tuple[int | None, int | None]:
        method = record.pick_snapshot.type
        match = self.cm.registry.get(record.match_id)
        scoring_method = match.match.scoring_method if match else ScoringMethod.FASTEST
        if method == PickType.MULTI:
            return scoring.multi_score(record.state_a), scoring.multi_score(
                record.state_b
            )
        return scoring.single_score(
            record.state_a, scoring_method
        ), scoring.single_score(record.state_b, scoring_method)

    async def on_verdict(
        self, match_id: str, round_id: str, verdict: RoundVerdict
    ) -> None:
        store = self.cm.registry.get(match_id)
        if store is None:
            return
        if (
            store.phase != MatchPhase.ROUND_JUDGING
            or store.current_round_id != round_id
        ):
            await self.cm.send_to_seat(
                match_id,
                Seat.REFEREE,
                SrvError(code=400, msg=self.cm.tr(match_id, "verdict.not_now")),
            )
            return
        record = self.db.rounds.get(round_id)
        if record is None:
            return
        await self._apply_verdict(store, record, verdict)

    async def _apply_verdict(
        self, store: MatchStore, record: RoundRecord, verdict: RoundVerdict
    ) -> None:
        record.verdict = verdict
        record.ended_at = now_ts()
        score_a, score_b = self._compute_scores(record)
        record.score_a_ms = score_a
        record.score_b_ms = score_b
        self._log_event(
            store.id,
            "verdict",
            {"round_id": record.id, "verdict": verdict.name},
        )

        if verdict == RoundVerdict.TIE_REMATCH:
            record.counted = False
            self.db.rounds.replace(record)
            await self.cm.broadcast_match(
                store.id,
                SrvRoundResult(
                    round_id=record.id,
                    verdict=verdict,
                    score_a_ms=score_a,
                    score_b_ms=score_b,
                ),
            )
            await self._rematch(store, record)
            return

        record.counted = True
        if verdict in (RoundVerdict.A_WIN, RoundVerdict.B_DISCONNECT_LOSS):
            store.wins_a += 1
        else:
            store.wins_b += 1
        self.db.rounds.replace(record)
        await self.cm.broadcast_match(
            store.id,
            SrvRoundResult(
                round_id=record.id,
                verdict=verdict,
                score_a_ms=score_a,
                score_b_ms=score_b,
            ),
        )
        await self.cm.broadcast_match(
            store.id,
            SrvCumulativeScore(
                wins_a=store.wins_a,
                wins_b=store.wins_b,
                threshold=store.match.win_threshold,
            ),
        )
        # 比分同步一条全场系统消息（Twilight 前缀，各端逐字一致；标识用
        # 登录用户名而非展示名）
        await self.cm.system_message(
            store.id,
            self.cm.tr(
                store.id,
                "score.update",
                player_a=self._username_of(store.match.player_a_id),
                a=store.wins_a,
                b=store.wins_b,
                player_b=self._username_of(store.match.player_b_id),
            ),
            kind="score",
        )
        # 比分达到取胜分数 → 立即自动结束（广播 match_end/系统消息、踢选手、
        # 推进赛程）；未达阈值才进入 ROUND_END 等待下一回合。
        winner = self._decided_winner(store)
        if winner is not None:
            await self._end_match(store, winner)
            return
        store.phase = MatchPhase.ROUND_END
        store.reset_ready()
        await self.cm.broadcast_match(
            store.id, SrvPhaseChange(phase=MatchPhase.ROUND_END)
        )

    async def _end_match(self, store: MatchStore, winner: Literal["A", "B"]) -> None:
        store.phase = MatchPhase.MATCH_END
        match = store.match
        match.status = MatchStatus.ENDED
        match.winner = winner
        match.ended_at = now_ts()
        self.db.matches.replace(match)
        match_log = self._ensure_match_log(store)
        match_log.final_result = {
            "winner": winner,
            "wins_a": store.wins_a,
            "wins_b": store.wins_b,
        }
        self.db.match_logs.replace(match_log)
        self._log_event(store.id, "match_end", {"winner": winner})
        # 系统消息入聊天审计（与 REST force_end 的 system 消息对齐）
        await self.cm.system_message(
            store.id,
            self.cm.tr(store.id, "match.ended", winner=winner),
            kind="match_end",
        )
        await self.cm.broadcast_match(store.id, SrvMatchEnd(winner=winner))
        await self.cm.broadcast_match(
            store.id, SrvPhaseChange(phase=MatchPhase.MATCH_END)
        )
        # 比赛结束：自动断开双方选手（选手零操作离场；裁判/导播保留以便多场）
        await self.cm.kick_players(store.id)
        # M12：通知赛程引擎推进对阵（仅赛事对决生效；异常隔离不影响单场）
        await self._notify_tournament_engine(store.match, winner)

    async def _notify_tournament_engine(
        self, match: Match, winner: Literal["A", "B"]
    ) -> None:
        """单场结束后通知赛程引擎推进对阵（仅赛事对决生效）。"""
        engine = self.cm.tournament_engine
        if engine is None or match.tournament_id is None:
            return
        try:
            await engine.on_match_ended(match, winner)
        except Exception:
            self.logger.exception("赛程引擎推进失败（match %s），已忽略。", match.id)

    async def _rematch(self, store: MatchStore, old_record: RoundRecord) -> None:
        store.round_counter += 1
        pick = old_record.pick_snapshot
        new_record = RoundRecord(
            match_id=store.id,
            round_no=store.round_counter,
            pick_code=pick.code,
            pick_snapshot=pick,
            collection_snapshot=pick.collection,
            source=RoundSource.REMATCH,
            state_a=PlayerRoundState(account_id=store.match.player_a_id),
            state_b=PlayerRoundState(account_id=store.match.player_b_id),
        )
        old_record.superseded_by = new_record.id
        self.db.rounds.replace(old_record)
        self.db.rounds.insert(new_record)
        store.current_round_id = new_record.id
        store.pending_pick_code = pick.code
        # 重赛沿用原回合词条集合与重试次数（backend-ct-pick-tags §2.3），裁判无需重选
        store.pending_pick_tags = list(pick.tags)
        store.pending_pick_retry = pick.retry_count
        # 不重发 pick_announced（选手端沿用上一回合预载）；仅留存快照供
        # PREP 断线重连握手重放。pick_snapshot 已是下发形态，直接复用。
        store.pick_announced = SrvPickAnnounced(
            pick_code=pick.code,
            pick=pick,
            collection=self._expand_collection(pick.collection),
        )
        store.phase = MatchPhase.PREP
        store.reset_ready()
        await self.cm.broadcast_match(
            store.id, SrvPhaseChange(phase=MatchPhase.PREP, round_id=new_record.id)
        )
        await self.cm.system_message(
            store.id, self.cm.tr(store.id, "rematch.announce"), kind="rematch"
        )
        await self.cm.broadcast_match(
            store.id, SrvReadyState(a_ready=store.a_ready, b_ready=store.b_ready)
        )

    async def on_edit_verdict(
        self, match_id: str, round_id: str, new_verdict: RoundVerdict
    ) -> None:
        record = self.db.rounds.get(round_id)
        if record is None:
            await self.cm.send_to_seat(
                match_id,
                Seat.REFEREE,
                SrvError(code=404, msg=self.cm.tr(match_id, "round.not_found")),
            )
            return
        old = record.verdict
        if old is None:
            await self.cm.send_to_seat(
                match_id,
                Seat.REFEREE,
                SrvError(code=400, msg=self.cm.tr(match_id, "verdict.not_verdicted")),
            )
            return
        record.verdict = new_verdict
        record.counted = new_verdict != RoundVerdict.TIE_REMATCH
        if record.score_a_ms is None and record.score_b_ms is None:
            score_a, score_b = self._compute_scores(record)
            record.score_a_ms = score_a
            record.score_b_ms = score_b
        self.db.rounds.replace(record)
        await self._recompute_score(match_id)
        store = self.cm.registry.get(match_id)
        wins_a = store.wins_a if store else 0
        wins_b = store.wins_b if store else 0
        await self.cm.broadcast_match(
            match_id,
            SrvVerdictEdit(round_id=round_id, old_verdict=old, new_verdict=new_verdict),
        )
        await self.cm.broadcast_match(
            match_id,
            SrvCumulativeScore(
                wins_a=wins_a,
                wins_b=wins_b,
                threshold=store.match.win_threshold if store else 1,
            ),
        )
        # 改判后比分变化 → 同步全场系统消息（与 _apply_verdict 一致）
        if store is not None:
            await self.cm.system_message(
                match_id,
                self.cm.tr(
                    match_id,
                    "score.update",
                    player_a=self._username_of(store.match.player_a_id),
                    a=wins_a,
                    b=wins_b,
                    player_b=self._username_of(store.match.player_b_id),
                ),
                kind="score",
            )

    async def _recompute_score(self, match_id: str) -> None:
        store = self.cm.registry.get(match_id)
        if store is None:
            return
        wins_a = 0
        wins_b = 0
        for record in self.db.rounds.find_by_match(match_id):
            if not record.counted or record.verdict is None:
                continue
            if record.verdict in (RoundVerdict.A_WIN, RoundVerdict.B_DISCONNECT_LOSS):
                wins_a += 1
            elif record.verdict in (RoundVerdict.B_WIN, RoundVerdict.A_DISCONNECT_LOSS):
                wins_b += 1
        store.wins_a = wins_a
        store.wins_b = wins_b
