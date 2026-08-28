"""WebSocket 连接管理器：鉴权握手、消息分发、广播、断连清理。

参考 fakeway webservice.py 的 FastAPI WS 服务端模式（accept + recv 循环 +
按比赛广播 + finally 断连清理）。所有内存状态变更在事件循环内，无锁。
M4 实现：连接/鉴权、聊天中转、系统消息广播、导播只读；命令与状态机在 M5/M6 注入。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from logging import Logger, getLogger
from typing import TYPE_CHECKING, Any

import jwt
from fastapi import WebSocket
from pydantic import ValidationError

from . import i18n
from .auth import decode_token
from .config import Settings
from .controllers import DBController
from .datatypes import (
    Account,
    AccountType,
    ChatMessage,
    ChatSenderRole,
    MatchPhase,
    MatchStatus,
    Seat,
    now_ts,
)
from .protocol import (
    ClientAttemptSkip,
    ClientChat,
    ClientDirectorCommand,
    ClientDirectorSubscribe,
    ClientDraftSync,
    ClientForfeitSignal,
    ClientHeartbeat,
    ClientLevelTimeUpload,
    ClientLiveTime,
    ClientMessage,
    ClientPreloadReport,
    ClientProjectComplete,
    ClientReconnectResync,
    ClientRefereeEditVerdict,
    ClientRefereeEndMatch,
    ClientRefereeManualStart,
    ClientRefereeMarkPrep,
    ClientRefereeSelectPick,
    ClientRefereeTerminateRound,
    ClientRefereeVerdict,
    ClientSubsegmentHit,
    ClientSubsegmentSample,
    ServerMessage,
    SrvAuthError,
    SrvAuthOk,
    SrvChat,
    SrvDirectorCommand,
    SrvDisplaced,
    SrvDraftState,
    SrvError,
    SrvMatchStatus,
    SrvPhaseChange,
    SrvPreloadState,
    SrvReadyState,
    SrvSeatState,
    SrvSystem,
    parse_client_message,
)
from .stores import Connection, MatchRegistry, MatchStore

if TYPE_CHECKING:
    from .match_fsm import MatchEngine
    from .tournament_engine import TournamentEngine

# 命令路由器类型：处理 ``!`` 开头的聊天命令，返回是否已被处理。
CommandRouter = Callable[[Connection, str], Awaitable[bool]]

# 暂停（PAUSED）期间需拒绝的比赛类 WS 动作。chat/heartbeat/director_subscribe/
# draft_sync/reconnect_resync/director_command 不拦（聊天、保活、草稿同步、
# 只读快照、导播舞台操控仍可用）。
_PAUSED_BLOCKED_ACTIONS: tuple[type[ClientMessage], ...] = (
    ClientRefereeMarkPrep,
    ClientRefereeSelectPick,
    ClientRefereeManualStart,
    ClientRefereeVerdict,
    ClientRefereeEditVerdict,
    ClientRefereeTerminateRound,
    ClientLevelTimeUpload,
    ClientAttemptSkip,
    ClientProjectComplete,
    ClientForfeitSignal,
    ClientPreloadReport,
    ClientSubsegmentSample,
    ClientSubsegmentHit,
    ClientLiveTime,
)

# exclusive 接管（last-wins takeover）：新连接以 exclusive=1 要求独占其身份 key
# （account_id + seat + match）时，同 key 旧连接被顶掉所用的关闭码与原因。
# 4xxx 属应用自定义区间；前端凭 displaced 消息或 close 码 4001 判定「连接已转移」。
DISPLACED_CLOSE_CODE = 4001
DISPLACED_REASON = "superseded_by_new_connection"


def normalize_draft(state: dict[str, Any]) -> dict[str, Any]:
    """清洗裁判上报的草稿态，仅保留展示所需字段（见 backend-banpick-persist 契约）。

    丢弃 stage / rollA·B / 计时器标志等 UI 态；缺字段补空集合 / null，
    保证裁判端旧版漏字段也不会 500。
    """

    def norm_action(a: dict[str, Any]) -> dict[str, Any]:
        return {"by": a["by"], "code": str(a["code"]), "kind": a["kind"]}

    return {
        "actions": [norm_action(a) for a in state.get("actions", [])],
        "picks": [
            {"by": p["by"], "code": str(p["code"])} for p in state.get("picks", [])
        ],
        "bannedTags": [str(t) for t in state.get("bannedTags", [])],
        "tagBanBy": {
            "A": state.get("tagBanBy", {}).get("A"),
            "B": state.get("tagBanBy", {}).get("B"),
        },
    }


def _now_ms() -> int:
    """当前 UTC 毫秒时间戳（state_sync 回放 payload 用）。"""
    return int(now_ts().timestamp() * 1000)


@dataclass
class _DirectorState:
    """导播操控状态暂存（state_sync 回放用）。

    纯内存态、随进程重启丢失（与 director_cmd 纯推送语义一致）；倒计时
    时间线由服务端折算：start 记起点（自暂停恢复时扣除已暂停时长），
    pause 记暂停点，reset 清时间线但保留 target_ms。
    """

    scene: str | None = None  # 最近一次 switch_scene 的 payload.scene
    soon_target_ms: int | None = None
    soon_started_ms: int | None = None  # 折算后的起点（毫秒，已扣暂停）
    soon_paused_ms: int | None = None
    config: dict[str, Any] = field(default_factory=dict)  # config_update 覆盖合并


class ConnectionManager:
    def __init__(
        self,
        db: DBController,
        registry: MatchRegistry,
        settings: Settings,
        logger: Logger | None = None,
    ) -> None:
        self.db = db
        self.registry = registry
        self.settings = settings
        self.logger = logger or getLogger("ConnectionManager")
        # 后台任务（断连提示等）：保留引用避免被 GC，完成后自动丢弃。
        self._background_tasks: set[asyncio.Task[None]] = set()
        # M5 注入：处理 ``!`` 命令
        self.command_router: CommandRouter | None = None
        # M6 注入：比赛状态机
        self.match_engine: MatchEngine | None = None
        # M12 注入：赛程引擎（单场结束触发对阵推进）
        self.tournament_engine: TournamentEngine | None = None
        # 导播状态暂存：key=(account_id, match_id)。每条 director_command 到达
        # 时更新；DIRECTOR 连接 auth_ok 后按 key 回放（state_sync）。已结束/
        # 已删除比赛不清理（内存量极小）。
        self._director_state: dict[tuple[str, str], _DirectorState] = {}

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------

    async def connect(
        self,
        websocket: WebSocket,
        token: str,
        requested_seat: str | None = None,
        requested_match: str | None = None,
        capabilities: str | None = None,
        exclusive: bool = False,
    ) -> Connection | None:
        """鉴权并登记连接；失败时接受后发送 auth_error 并关闭。

        requested_seat 指定时，要求该账号被指派为此比赛的该座位（支持多角色账号
        以特定身份连接，如 admin 同时以裁判与导播身份各开一条连接）。
        requested_match 指定时（如裁判多标签页选某场），连到该比赛
        并校验账号是其成员；否则自动挑选该账号最新一场（兼容选手/导播）。
        capabilities 为 ``?cap=`` 逗号分隔的能力声明（如 ``preload1``）。
        exclusive 为真时要求独占身份 key（账号+座位+比赛）：同 key 既有连接
        （无论对方是否 exclusive）先收 displaced 再被 close(4001) 顶掉；
        key 含 match，故裁判不同场多标签、多角色多座位、导播不带 exclusive 的
        OBS 多源并存均不受影响。
        """
        account_id, error = self._authenticate(token)
        if error is not None or account_id is None:
            await websocket.accept()
            await self._send_model(
                websocket,
                SrvAuthError(msg=error or self.tr_default("error.auth_failed")),
            )
            await websocket.close()
            return None

        account = self.db.accounts.get(account_id)
        if account is None:
            await websocket.accept()
            await self._send_model(
                websocket, SrvAuthError(msg=self.tr_default("error.account_missing"))
            )
            await websocket.close()
            return None

        match, seat, resolve_err = self._resolve(
            account, requested_seat, requested_match
        )
        if match is None or seat is None:
            await websocket.accept()
            await self._send_model(
                websocket,
                SrvAuthError(
                    msg=resolve_err or self.tr_default("error.no_running_match_wait")
                ),
            )
            await websocket.close()
            return None

        await websocket.accept()
        store = self.registry.get_or_create(match)
        conn = Connection(
            websocket=websocket,
            account_id=account.id,
            display_name=account.display_name,
            seat=seat,
            match_id=match.id,
            capabilities=frozenset(
                c.strip() for c in (capabilities or "").split(",") if c.strip()
            ),
        )
        # exclusive 接管的临界区：注销同 key 旧连接 + 注册新连接之间无 await，
        # 保证原子完成；旧连接自注销一刻起，其在途消息一律被 handle 忽略。
        displaced: list[Connection] = []
        if exclusive:
            displaced = store.same_key_connections(account.id, seat)
            for old in displaced:
                self._remove_connection(store, old)
        # 同座位重连：导播允许多连接并存（网页+OBS 等）；选手/裁判替换旧连接
        legacy: Connection | None = None
        if seat == Seat.DIRECTOR:
            store.directors.add(conn)
        else:
            previous = store.connections.get(seat)
            store.connections[seat] = conn
            # 非 exclusive（或裁判改派后不同账号）的旧语义：静默替换旧连接
            if previous is not None and previous not in displaced:
                legacy = previous
        # 顶掉旧连接：displaced 先于关闭帧送达，前端凭其或 close 码 4001 判定
        for old in displaced:
            await self._displace(old)
        if legacy is not None:
            await self._safe_close(legacy.websocket)

        await self._send(
            conn,
            SrvAuthOk(
                account_id=account.id,
                display_name=account.display_name,
                seat=seat.name,
                match_id=match.id,
                match_name=match.name,
                player_a_name=self._display_name_of(match.player_a_id),
                player_b_name=self._display_name_of(match.player_b_id),
            ),
        )
        await self._send(
            conn, SrvReadyState(a_ready=store.a_ready, b_ready=store.b_ready)
        )
        await self._send(conn, SrvPhaseChange(phase=store.phase))
        # 补发当前 ban/pick 草稿状态（新连导播立即拿到进度）
        if store.draft_state is not None:
            await self._send(conn, SrvDraftState(state=store.draft_state))
        # PREP 阶段补发选图预览与预载状态快照（断线重连的选手端恢复预载）：
        # pick_announced 补发选手席（含专属 System 提示）与导播席（下方
        # DIRECTOR 分支，各阶段均补）；preload_state 对所有席位补发（重连端
        # 消除陈旧态）。其他阶段选手席不补（round_start 本就不重放，预载在
        # COUNTDOWN/IN_ROUND 已无意义）。
        if store.phase == MatchPhase.PREP:
            is_player = seat in (Seat.PLAYER_A, Seat.PLAYER_B)
            if is_player and store.pick_announced is not None:
                await self._send(conn, store.pick_announced)
                # 当前选图同步一条定向 System 提示（仅该选手可见、不落库；
                # 文案与 pick.selected 同构，取自冻结快照，含词条/重试）
                pick = store.pick_announced.pick
                tags_suffix = (
                    f" [{', '.join(pick.tags)}]" if pick.tags else ""
                )
                retry_suffix = (
                    f" x{pick.retry_count}"
                    if pick.retry_count is not None
                    else ""
                )
                await self._send(
                    conn,
                    SrvSystem(
                        text=self.tr(
                            store.id,
                            "pick.current_hint",
                            code=store.pick_announced.pick_code,
                            name=pick.name,
                            tags=tags_suffix,
                            retry=retry_suffix,
                        ),
                        kind="pick",
                        sender="System",
                    ),
                )
            await self._send(
                conn,
                SrvPreloadState(a_status=store.preload_a, b_status=store.preload_b),
            )
            # 连入时比赛已在 PREP（重连/中途加入）：没有 live 的 prep.started
            # 广播可看，定向补一条仅该选手可见的 System 提示（不广播、不落库）。
            # 该席已就绪则不提示（再 !ready 会取消就绪，提示反而误导）。
            already_ready = store.a_ready if seat == Seat.PLAYER_A else store.b_ready
            if is_player and not already_ready:
                await self._send(
                    conn,
                    SrvSystem(
                        text=self.tr(store.id, "prep.hint"),
                        kind="prep",
                        sender="System",
                    ),
                )
        # 初始化序列末尾给新连接补发全量在线状态（重连方消除陈旧离线态）
        for player_seat in (Seat.PLAYER_A, Seat.PLAYER_B):
            await self._send(
                conn,
                SrvSeatState(
                    seat=player_seat.name, online=player_seat in store.connections
                ),
            )
        # 回合中裁判/导播晚连/重连：补发双方最近一次实时计时（每秒上报、按席
        # 暂存；overlay 晚开也能立即对齐计时显示）。选手席不补（互不感知对手进度）。
        if (
            store.phase == MatchPhase.IN_ROUND
            and seat in (Seat.REFEREE, Seat.DIRECTOR)
            and store.live_times
        ):
            for live in store.live_times.values():
                await self._send(conn, live)
        # 座席在线状态广播（backend-seat-presence）：选手连入通知全员——
        # 系统提示连本人也发（system 消息 = Twilight 前缀，各端必须逐字一致，
        # 不排除任何在席连接）；seat_state 广播仍排除本人（上方已补发全量
        # 快照，避免重复 UI 事件）
        if seat in (Seat.PLAYER_A, Seat.PLAYER_B):
            await self.broadcast_match(
                store.id,
                SrvSeatState(seat=seat.name, online=True),
                exclude=conn,
            )
            await self.system_message(
                store.id,
                self.tr(
                    store.id,
                    "seat.online",
                    name=conn.display_name,
                    seat=seat.name,
                ),
                kind="seat",
            )
        # 导播状态回放：补发该 (account_id, match_id) 最近一次的场景/倒计时/
        # 直播配置，舞台晚于控制台打开也能对齐，无需控制台再点一次。
        # 仅 DIRECTOR 席、有暂存才发；选手/裁判永不收到。
        if seat == Seat.DIRECTOR:
            replay = self._director_state_payload(conn.account_id, store.id)
            if replay is not None:
                await self._send(
                    conn,
                    SrvDirectorCommand(action="state_sync", payload=replay),
                )
            # 补发当前选图预览（若已宣布）：导播 categoryinfo 场景晚开也能立即
            # 对齐当前项目。pick_announced 自 select_pick 起留存到下一次
            # begin_prep 才清空，覆盖 PREP/倒计时/回合中各阶段（选手席的
            # PREP 补发见上方分支，含专属 System 提示，口径不同勿合并）。
            if store.pick_announced is not None:
                await self._send(conn, store.pick_announced)
        self.logger.info("Seat %s connected to match %s.", seat.name, match.id)
        return conn

    async def disconnect(self, conn: Connection) -> None:
        store = self.registry.get(conn.match_id)
        if store is None:
            return
        was_current = store.connections.get(conn.seat) is conn
        self._remove_connection(store, conn)
        if was_current:
            await self._broadcast_seat_state(store, conn.seat)
            # 仅真正离线时发系统提示；被同座位新连接替换（顶号重连）不提示，
            # 与新连接侧的 online 广播一致，避免 false-offline 刷屏。
            # 提示派发为独立任务：disconnect 由端点 finally 触发，此时当前
            # websocket 任务可能正处于取消/收尾流程，同步等待第二次广播
            # 会在取消边界上阻塞（TestClient 场景可复现）。
            if conn.seat in (Seat.PLAYER_A, Seat.PLAYER_B):
                task = asyncio.create_task(
                    self._seat_offline_notice(
                        store.id, conn.display_name, conn.seat.name
                    )
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
        self.logger.info(
            "Seat %s disconnected from match %s.", conn.seat.name, conn.match_id
        )

    def _remove_connection(self, store: MatchStore, conn: Connection) -> None:
        """从比赛移除一条连接：导播从集合 discard，其余按座位删除。"""
        if conn.seat == Seat.DIRECTOR:
            store.directors.discard(conn)
        elif store.connections.get(conn.seat) is conn:
            del store.connections[conn.seat]

    async def _broadcast_seat_state(self, store: MatchStore, seat: Seat) -> None:
        """广播座席离线状态（仅选手座席；见 backend-seat-presence §2）。

        连入侧广播在 ``connect`` 内直接做（online 恒 true）；本方法只走
        断开/踢出路径，广播的是删除后的最终状态（座位无连接即 offline）。
        """
        if seat in (Seat.PLAYER_A, Seat.PLAYER_B):
            await self.broadcast_match(
                store.id,
                SrvSeatState(seat=seat.name, online=seat in store.connections),
            )

    async def kick_players(self, match_id: str) -> None:
        """关闭并移除该比赛的双方选手连接（比赛结束自动断连用；裁判/导播保留）。"""
        store = self.registry.get(match_id)
        if store is None:
            return
        for seat in (Seat.PLAYER_A, Seat.PLAYER_B):
            conn = store.connections.pop(seat, None)
            if conn is not None:
                await self._safe_close(conn.websocket)
                await self._broadcast_seat_state(store, seat)
                self.logger.info(
                    "Seat %s kicked from match %s (match ended).",
                    seat.name,
                    match_id,
                )

    async def pause_match(self, match_id: str) -> None:
        """暂停比赛的 WS 副作用：取消进行中的开始倒计时、广播系统消息与
        ``match_status``、踢出双方选手连接（释放占用）。

        由 REST ``POST /me/matches/{id}/pause`` 在置 ``status=PAUSED`` 后调用。
        """
        store = self.registry.get(match_id)
        if store is not None:
            # 取消可能正在进行的开始倒计时，避免暂停期间触发 _begin_round。
            # 若原本处于 COUNTDOWN，回退到 PREP，便于 resume 后裁判重新开始。
            if store.countdown_timer is not None:
                await store.countdown_timer.cancel()
                store.countdown_timer = None
                store.countdown_source = None
            # 预载门控超时计时器同理取消（暂停期不应在后台强制开局）。
            if store.preload_gate_timer is not None:
                await store.preload_gate_timer.cancel()
                store.preload_gate_timer = None
            if store.phase == MatchPhase.COUNTDOWN:
                store.phase = MatchPhase.PREP
        await self.system_message(
            match_id, self.tr(match_id, "pause.message"), kind="pause"
        )
        await self.broadcast_match(match_id, SrvMatchStatus(status=MatchStatus.PAUSED))
        await self.kick_players(match_id)

    async def resume_match(self, match_id: str) -> None:
        """恢复比赛的 WS 副作用：广播系统消息与 ``match_status``。

        由 REST ``POST /me/matches/{id}/resume`` 在置 ``status=RUNNING`` 后调用；
        选手随后自行重连（``find_running_for_player`` 重新命中本场）。
        """
        await self.system_message(
            match_id, self.tr(match_id, "resume.message"), kind="resume"
        )
        await self.broadcast_match(match_id, SrvMatchStatus(status=MatchStatus.RUNNING))

    # ------------------------------------------------------------------
    # 消息分发
    # ------------------------------------------------------------------

    async def handle(self, conn: Connection, raw: str) -> None:
        # 已被顶掉/清理的连接：在途消息一律忽略，不得再影响比赛状态
        store = self.registry.get(conn.match_id)
        if store is None or not store.has_connection(conn):
            return
        try:
            msg = parse_client_message(raw)
        except ValidationError:
            await self._send(
                conn,
                SrvError(code=400, msg=self.tr(conn.match_id, "error.bad_message")),
            )
            return

        if conn.read_only and not isinstance(
            msg, (ClientDirectorSubscribe, ClientHeartbeat, ClientDirectorCommand)
        ):
            await self._send(
                conn,
                SrvError(
                    code=403, msg=self.tr(conn.match_id, "error.director_readonly")
                ),
            )
            return

        await self._dispatch(conn, msg)

    async def _dispatch(self, conn: Connection, msg: ClientMessage) -> None:
        # 暂停期间拒绝比赛类动作（pause 由 REST 触发，resume 后恢复）。
        store = self.registry.get(conn.match_id)
        if (
            store is not None
            and store.match.status == MatchStatus.PAUSED
            and isinstance(msg, _PAUSED_BLOCKED_ACTIONS)
        ):
            await self._send(
                conn,
                SrvError(code=409, msg=self.tr(conn.match_id, "error.match_paused")),
            )
            return
        engine = self._require_engine(conn)
        match msg:
            case ClientChat(text=text):
                await self._on_chat(conn, text)
            case ClientDirectorSubscribe() | ClientHeartbeat():
                pass
            case ClientDirectorCommand(action=act, payload=pl):
                # 导播控制台 → 同账号其他导播连接（OBS 舞台）：原样转发，
                # 不落库、不回执 sender（瞬时操控指令，控制台无需回声）；
                # 同时更新状态暂存供新连接 state_sync 回放
                if await self._require_seat(conn, Seat.DIRECTOR):
                    self._update_director_state(conn.account_id, conn.match_id, act, pl)
                    await self.broadcast_to_other_directors(
                        conn, SrvDirectorCommand(action=act, payload=pl)
                    )
            case ClientDraftSync(state=st):
                # 裁判上报 ban/pick 草稿状态 → 存储 + 转发给全员（含导播）
                if await self._require_seat(conn, Seat.REFEREE):
                    store = self.registry.get(conn.match_id)
                    if store is not None:
                        store.draft_state = st
                        # 顺手落库草稿快照（方案 A：复用 draft_sync，零新增协议）
                        await self._persist_draft_snapshot(store, st)
                        await self.broadcast_match(
                            conn.match_id, SrvDraftState(state=st)
                        )
            case ClientRefereeMarkPrep():
                if await self._require_seat(conn, Seat.REFEREE):
                    await engine.begin_prep(conn.match_id)
            case ClientRefereeSelectPick(
                pick_code=pick_code, tags=tags, retry_count=retry
            ):
                if await self._require_seat(conn, Seat.REFEREE):
                    await engine.select_pick(conn.match_id, pick_code, tags, retry)
            case ClientRefereeManualStart():
                if await self._require_seat(conn, Seat.REFEREE):
                    await engine.manual_start(conn.match_id)
            case ClientRefereeVerdict(round_id=rid, verdict=v):
                if await self._require_seat(conn, Seat.REFEREE):
                    await engine.on_verdict(conn.match_id, rid, v)
            case ClientRefereeEditVerdict(round_id=rid, new_verdict=nv):
                if await self._require_seat(conn, Seat.REFEREE):
                    await engine.on_edit_verdict(conn.match_id, rid, nv)
            case ClientRefereeTerminateRound(round_id=rid, reason=r):
                if await self._require_seat(conn, Seat.REFEREE):
                    await engine.terminate_round(conn.match_id, rid, r)
            # 手动结束比赛：终态操作，不加入 _PAUSED_BLOCKED_ACTIONS
            # （暂停中被放弃的比赛也需能经裁判收尾）
            case ClientRefereeEndMatch():
                if await self._require_seat(conn, Seat.REFEREE):
                    await engine.end_match(conn.match_id)
            case ClientReconnectResync(round_id=rid):
                if await self._require_player(conn):
                    await engine.on_reconnect_resync(conn.match_id, conn.seat, rid)
            case ClientPreloadReport(status=st, detail=d):
                if await self._require_player(conn):
                    await engine.on_preload_report(conn.match_id, conn.seat, st, d)
            case ClientSubsegmentSample(
                round_id=rid,
                level_index=li,
                seq=sq,
                t_ms=t,
                px=px,
                py=py,
                pz=pz,
                dx=dx,
                dy=dy,
                dz=dz,
            ):
                if await self._require_player(conn):
                    await engine.on_subsegment_sample(
                        conn.match_id, conn.seat, rid, li, sq, t, px, py, pz, dx, dy, dz
                    )
            case ClientSubsegmentHit(round_id=rid, level_index=li, seq=sq, t_ms=t):
                if await self._require_player(conn):
                    await engine.on_subsegment_hit(
                        conn.match_id, conn.seat, rid, li, sq, t
                    )
            case ClientLiveTime(
                round_id=rid,
                level_index=li,
                total_ms=tm,
                segment_ms=sm,
                real_time_ms=rt,
            ):
                if await self._require_player(conn):
                    await engine.on_live_time(
                        conn.match_id, conn.seat, rid, li, tm, sm, rt
                    )
            case ClientLevelTimeUpload(
                round_id=rid,
                level_index=li,
                this_level_ms=t,
                total_ms=tm,
                invalid_reasons=ir,
            ):
                if await self._require_player(conn):
                    await engine.on_level_time_upload(
                        conn.match_id, conn.seat, rid, li, t, tm, ir
                    )
            case ClientAttemptSkip(round_id=rid, attempt_index=ai):
                if await self._require_player(conn):
                    await engine.on_attempt_skip(conn.match_id, conn.seat, rid, ai)
            case ClientProjectComplete(round_id=rid, final_total_ms=ft):
                if await self._require_player(conn):
                    await engine.on_project_complete(conn.match_id, conn.seat, rid, ft)
            case ClientForfeitSignal(round_id=rid, reason=r):
                if await self._require_player(conn):
                    await engine.on_forfeit(conn.match_id, conn.seat, rid, r)
            case _:
                await self._send(
                    conn,
                    SrvError(
                        code=501,
                        msg=self.tr(
                            conn.match_id, "error.unimplemented", type=msg.type
                        ),
                    ),
                )

    async def _require_seat(self, conn: Connection, seat: Seat) -> bool:
        if conn.seat != seat:
            await self._send(
                conn,
                SrvError(
                    code=403,
                    msg=self.tr(
                        conn.match_id,
                        "error.wrong_seat",
                        seat=i18n.seat_name(seat, self.locale_for(conn.match_id)),
                    ),
                ),
            )
            return False
        return True

    async def _require_player(self, conn: Connection) -> bool:
        if conn.seat not in (Seat.PLAYER_A, Seat.PLAYER_B):
            await self._send(
                conn,
                SrvError(code=403, msg=self.tr(conn.match_id, "error.players_only")),
            )
            return False
        return True

    def _require_engine(self, conn: Connection) -> MatchEngine:
        if self.match_engine is None:  # pragma: no cover - 由 main 注入
            raise RuntimeError("match_engine 未注入")
        return self.match_engine

    async def _persist_draft_snapshot(self, store: MatchStore, state: Any) -> None:
        """将裁判上报的草稿态清洗后 upsert 到 match_log（方案 A）。

        normalize 在本协议边界完成（仅保留展示字段）；落库借引擎的
        ``save_draft_snapshot``（按需创建 match_log）。失败仅记日志，不影响转发。
        """
        engine = self.match_engine
        if engine is None:
            return
        try:
            snapshot = normalize_draft(state) if isinstance(state, dict) else None
            engine.save_draft_snapshot(store, snapshot)
        except Exception:
            # 落库不应阻塞 draft_sync 转发，仅记日志
            self.logger.exception("持久化草稿快照失败 match_id=%s", store.id)

    async def _on_chat(self, conn: Connection, text: str) -> None:
        """聊天中转。

        输入（含 ``!`` 命令）一律先作为普通聊天消息广播留存（让发送方与各方都能看到
        所输入的命令），再交命令路由器执行。命令路由器的反馈以系统消息形式发出。
        """
        await self._relay_chat(conn, text)
        if text.startswith("!") and self.command_router is not None:
            await self.command_router(conn, text)

    async def _relay_chat(self, conn: Connection, text: str) -> None:
        sender_role = (
            ChatSenderRole.PLAYER
            if conn.seat in (Seat.PLAYER_A, Seat.PLAYER_B)
            else ChatSenderRole.REFEREE
        )
        chat = ChatMessage(
            match_id=conn.match_id,
            sender_role=sender_role,
            sender_id=conn.account_id,
            sender_name=conn.display_name,
            text=text,
        )
        self.db.chat_messages.insert(chat)
        await self.broadcast_match(
            conn.match_id,
            SrvChat(
                sender_id=conn.account_id,
                sender_name=conn.display_name,
                seat=conn.seat.name,
                text=text,
            ),
        )

    # ------------------------------------------------------------------
    # 广播与系统消息
    # ------------------------------------------------------------------

    async def broadcast_match(
        self,
        match_id: str,
        msg: ServerMessage,
        *,
        exclude: Connection | None = None,
    ) -> None:
        store = self.registry.get(match_id)
        if store is None:
            return
        payload = msg.model_dump_json()
        conns = list(store.connections.values()) + list(store.directors)
        dead: list[Connection] = []
        for conn in conns:
            if conn is exclude:
                continue
            try:
                await conn.websocket.send_text(payload)
            except Exception:
                dead.append(conn)
                self.logger.debug("广播发送失败，清理该连接。", exc_info=True)
        # 清理半开/已断连接，避免导播多连接场景下累积垃圾
        for conn in dead:
            self._remove_connection(store, conn)

    def _update_director_state(
        self, account_id: str, match_id: str, action: str, payload: dict[str, Any]
    ) -> None:
        """每条导播指令到达时更新 (account_id, match_id) 状态暂存（回放用）。

        倒计时时间线由服务端折算：start 记起点（自暂停恢复时前移已进行的
        有效时长，即暂停补偿；运行中重复 start 幂等不重置）；pause 仅在
        进行中生效；reset 清 started/paused 但保留 target_ms；config 为
        覆盖合并（八键可部分缺失）。payload 值不校验，与广播口径一致。
        """
        st = self._director_state.setdefault((account_id, match_id), _DirectorState())
        now = _now_ms()
        match action:
            case "switch_scene":
                st.scene = payload.get("scene")
            case "soon_set_target":
                st.soon_target_ms = payload.get("target_ms")
            case "soon_start":
                if st.soon_started_ms is not None and st.soon_paused_ms is not None:
                    # 暂停恢复：起点 = now - 暂停前的有效进行时长
                    ran = st.soon_paused_ms - st.soon_started_ms
                    st.soon_started_ms = now - max(ran, 0)
                elif st.soon_started_ms is None:
                    st.soon_started_ms = now  # 首次启动
                st.soon_paused_ms = None
            case "soon_pause":
                # 仅进行中可暂停（幂等：已暂停不重复记点）
                if st.soon_started_ms is not None and st.soon_paused_ms is None:
                    st.soon_paused_ms = now
            case "soon_reset":
                st.soon_started_ms = None
                st.soon_paused_ms = None  # target_ms 保留
            case "config_update":
                cfg = payload.get("config")
                if isinstance(cfg, dict):
                    st.config.update(cfg)

    def _director_state_payload(
        self, account_id: str, match_id: str
    ) -> dict[str, Any] | None:
        """构建 state_sync 回放 payload；该 key 无任何指令历史时返回 None。

        started_at/paused_at/now_ms 均为服务器毫秒时间戳，前端以 now_ms 做
        时钟偏移校正（elapsed = now_ms - started_at，已扣暂停）。
        """
        st = self._director_state.get((account_id, match_id))
        if st is None:
            return None
        return {
            "scene": st.scene,
            "soon": {
                "target_ms": st.soon_target_ms,
                "started_at": st.soon_started_ms,
                "paused_at": st.soon_paused_ms,
                "now_ms": _now_ms(),
            },
            "config": st.config,
        }

    async def broadcast_to_other_directors(
        self, sender: Connection, msg: ServerMessage
    ) -> None:
        """定向广播给同比赛内 sender 之外的同账号 DIRECTOR 连接。

        导播控制台与 OBS 舞台分属不同进程（localStorage 不互通），舞台操控
        指令经服务端转发：仅发 sender 之外的同账号导播连接（每个导播只控
        自己的舞台；改派后其他账号的残留连接不收）；发送失败的半开/已断
        连接照 ``broadcast_match`` 清理。
        """
        store = self.registry.get(sender.match_id)
        if store is None:
            return
        payload = msg.model_dump_json()
        dead: list[Connection] = []
        for conn in store.directors:
            if conn is sender or conn.account_id != sender.account_id:
                continue
            try:
                await conn.websocket.send_text(payload)
            except Exception:
                dead.append(conn)
                self.logger.debug("定向广播发送失败，清理该连接。", exc_info=True)
        for conn in dead:
            self._remove_connection(store, conn)

    async def send_to_seat(self, match_id: str, seat: Seat, msg: ServerMessage) -> None:
        store = self.registry.get(match_id)
        if store is None:
            return
        if seat == Seat.DIRECTOR:
            for conn in list(store.directors):
                await self._send(conn, msg)
            return
        conn = store.connections.get(seat)
        if conn is not None:
            await self._send(conn, msg)

    async def send(self, conn: Connection, msg: ServerMessage) -> None:
        """向单个连接发送消息。"""
        await self._send(conn, msg)

    async def _seat_offline_notice(
        self, match_id: str, display_name: str, seat: str
    ) -> None:
        """断连提示的独立任务：持久化并广播，失败只记日志。"""
        try:
            await self.system_message(
                match_id,
                self.tr(match_id, "seat.offline", name=display_name, seat=seat),
                kind="seat",
            )
        except Exception:
            self.logger.exception(
                "断连系统提示发送失败（match=%s, seat=%s）。", match_id, seat
            )

    async def system_message(
        self,
        match_id: str,
        text: str,
        kind: str = "info",
        *,
        exclude: Connection | None = None,
    ) -> None:
        """持久化（系统聊天日志）并广播系统消息。

        系统消息均为全场广播，展示前缀 "Twilight"（与 SrvSystem.sender 一致，
        客户端以 sender_name 渲染聊天前缀）；仅特定席位可见的错误回执走
        SrvError 定向发送，不落库、客户端沿用 "System" 前缀。
        """
        chat = ChatMessage(
            match_id=match_id,
            sender_role=ChatSenderRole.SYSTEM,
            sender_id=None,
            sender_name="Twilight",
            text=text,
            is_system=True,
        )
        self.db.chat_messages.insert(chat)
        await self.broadcast_match(
            match_id, SrvSystem(text=text, kind=kind), exclude=exclude
        )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def locale_for(self, match_id: str) -> str:
        """比赛当前语言（store 不存在时回默认语言）。"""
        store = self.registry.get(match_id)
        return store.locale if store is not None else self.settings.default_locale

    def tr(self, match_id: str, key: str, **kw: object) -> str:
        """按比赛语言渲染系统消息。"""
        return i18n.t(self.locale_for(match_id), key, **kw)

    def tr_default(self, key: str, **kw: object) -> str:
        """按默认语言渲染（握手期 store 尚未建立时用）。"""
        return i18n.t(self.settings.default_locale, key, **kw)

    def _authenticate(self, token: str) -> tuple[str | None, str | None]:
        try:
            claims = decode_token(token, self.settings)
        except jwt.PyJWTError:
            return None, self.tr_default("error.token_invalid_or_expired")
        sub = claims.get("sub")
        if not isinstance(sub, str):
            return None, self.tr_default("error.token_invalid")
        return sub, None

    def _resolve(
        self,
        account: Account,
        requested_seat: str | None,
        requested_match: str | None,
    ) -> tuple[Any, Seat | None, str | None]:
        """解析连接的目标比赛与座位。

        返回 ``(match, seat, 错误信息)``，每项均可为 None。

        - 显式 match（裁判/导播 ``?match=``）：按 id 取 + 成员校验 + 非 ENDED，
          座位由 _resolve_seat 定。
        - 选手路径（显式 PLAYER 席位，或无 seat 但账号含 PLAYER 角色）：
          解析为其唯一 RUNNING 场（即"当前活跃比赛"），无则报错等待
          ——保证选手同时只在一场。
        - 官方路径（裁判/导播，无显式 match）：兜底取最新非 ENDED 成员场。
        """
        account_id = account.id
        # 1) 显式比赛
        if requested_match:
            match = self.db.matches.get(requested_match)
            if match is None:
                return None, None, self.tr_default("error.match_missing")
            if not self._is_member(match, account_id):
                return None, None, self.tr_default("error.not_in_match")
            if match.status == MatchStatus.ENDED:
                return None, None, self.tr_default("error.match_ended")
            seat = self._resolve_seat(match, account_id, requested_seat)
            if seat is None:
                return None, None, self.tr_default("error.seat_not_assigned")
            return match, seat, None

        explicit_player = requested_seat in (Seat.PLAYER_A.name, Seat.PLAYER_B.name)
        is_player_seating = explicit_player or (
            requested_seat is None and AccountType.PLAYER in account.roles
        )
        # 2) 选手路径：必须有 RUNNING 场
        if is_player_seating:
            running = self.db.matches.find_running_for_player(account_id)
            if running:
                match = max(running, key=lambda s: s.created_at)
                seat = (
                    Seat.PLAYER_A if match.player_a_id == account_id else Seat.PLAYER_B
                )
                if requested_seat and Seat[requested_seat] != seat:
                    return (
                        None,
                        None,
                        self.tr_default("error.seat_mismatch", seat=requested_seat),
                    )
                return match, seat, None
            return None, None, self.tr_default("error.no_running_match_wait")

        # 3) 官方路径兜底（裁判/导播未指定 match）
        matches = self.db.matches.find_by_member(account_id)
        active = [s for s in matches if s.status != MatchStatus.ENDED]
        if not active:
            return None, None, None
        match = max(active, key=lambda s: s.created_at)
        seat = self._resolve_seat(match, account_id, requested_seat)
        if seat is None:
            return None, None, self.tr_default("error.not_in_match")
        return match, seat, None

    @staticmethod
    def _is_member(match: Any, account_id: str) -> bool:
        return account_id in {
            match.player_a_id,
            match.player_b_id,
            match.referee_id,
            match.director_id,
        }

    def _display_name_of(self, account_id: str | None) -> str | None:
        """account_id → display_name（查不到返回 None）。"""
        if not account_id:
            return None
        acc = self.db.accounts.get(account_id)
        return acc.display_name if acc else None

    @staticmethod
    def _resolve_seat(
        match, account_id: str, requested_seat: str | None = None
    ) -> Seat | None:  # type: ignore[no-untyped-def]
        seat_to_owner = {
            Seat.PLAYER_A: match.player_a_id,
            Seat.PLAYER_B: match.player_b_id,
            Seat.REFEREE: match.referee_id,
            Seat.DIRECTOR: match.director_id,
        }
        if requested_seat is not None:
            try:
                seat = Seat[requested_seat]
            except KeyError:
                return None
            return seat if seat_to_owner[seat] == account_id else None
        for seat, owner_id in seat_to_owner.items():
            if owner_id == account_id:
                return seat
        return None

    async def _send(self, conn: Connection, msg: ServerMessage) -> None:
        await self._send_model(conn.websocket, msg)

    async def _send_model(self, websocket: WebSocket, msg: ServerMessage) -> None:
        await websocket.send_text(msg.model_dump_json())

    async def _displace(self, old: Connection) -> None:
        """顶掉一条同 key 旧连接（exclusive 接管）。

        先发 displaced 通知再 close(4001)，保证通知先于关闭帧送达；对端可能
        已是 TCP 死连接，发送/关闭失败仅记日志（此时连接已注销，不占 key）。
        """
        self.logger.info(
            "Seat %s connection displaced by new exclusive connection on match %s.",
            old.seat.name,
            old.match_id,
        )
        try:
            await self._send_model(old.websocket, SrvDisplaced(reason=DISPLACED_REASON))
        except Exception:
            self.logger.debug(
                "发送 displaced 失败（连接可能已死），继续关闭。", exc_info=True
            )
        await self._safe_close(old.websocket, code=DISPLACED_CLOSE_CODE)

    async def _safe_close(self, websocket: WebSocket, code: int = 1000) -> None:
        try:
            await websocket.close(code=code)
        except Exception:
            self.logger.debug("关闭旧连接失败，忽略。", exc_info=True)
