"""内存实时状态（无锁）：所有变更均在 asyncio 事件循环内完成。

参考 AShareGateway stores.py 的 PublicState 风格。``MatchStore`` 持有单场比赛的
实时状态与各座位的当前连接；``MatchRegistry`` 按比赛 id 聚合。
M4 仅含连接与基础状态；M6/M7 会补充回合、计时器、累计比分等。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import WebSocket

from .config import settings
from .datatypes import Match, MatchPhase, Seat
from .protocol import PreloadStatus, SrvLiveTime, SrvPickAnnounced
from .timer_service import CountdownTimer, CounterTimer


@dataclass(eq=False)
class Connection:
    """一条 WebSocket 连接及其归属（按对象身份判等/哈希，可入 set）。"""

    websocket: WebSocket
    account_id: str
    display_name: str
    seat: Seat
    match_id: str
    # 客户端能力声明（?cap= 逗号分隔；如 preload1 = 会上报预载状态）
    capabilities: frozenset[str] = frozenset()

    @property
    def read_only(self) -> bool:
        """导播只读围观。"""
        return self.seat == Seat.DIRECTOR


@dataclass(eq=False)
class SubsegmentSample:
    """一个分段采样点（MULTI 回合实时差距跟踪；纯内存态，不持久化）。

    采样窗口 = 每关「苏醒 → 触碰通关判定区」；t_ms 为采样方计时器
    （TwilightTimer）时间线上的总时间。(dx,dy,dz) 为采样间隔位移，
    全 0 = 静止（存档不建检测平面）。
    """

    level_index: int
    seq: int
    t_ms: int
    px: float
    py: float
    pz: float
    dx: float
    dy: float
    dz: float
    # 采样方上报的检测平面半径（米）；None = 旧版客户端未上报，
    # 服务端陈旧平面识别回退 match_fsm.SUBSEGMENT_LOOPBACK_RADIUS
    plane_radius: float | None = None


class MatchStore:
    """单场比赛的实时状态。"""

    def __init__(self, match: Match) -> None:
        self.match: Match = match
        self.connections: dict[Seat, Connection] = {}  # 选手/裁判（每座位单连接）
        self.directors: set[Connection] = set()  # 导播多连接并存（网页+OBS）
        self.phase: MatchPhase = MatchPhase.IDLE
        self.a_ready: bool = False
        self.b_ready: bool = False
        # M6/M7 将补充：当前回合、待选图、倒计时任务、累计比分
        self.pending_pick_code: str | None = None
        # 待选图的词条（与 pending_pick_code 同生命周期；见 backend-ct-pick-tags）
        self.pending_pick_tags: list[str] = []
        # 待选图裁判指定的重试次数（CT/EX 单关必填；其余类别 None 沿用图池预设）
        self.pending_pick_retry: int | None = None
        # 最近一次已播报的选图快照（select_pick 时广播并留存；PREP 重连握手重放，
        # tie-rematch 从 pick_snapshot 重建仅存不发；begin_prep 随 pending 一起清空）
        self.pick_announced: SrvPickAnnounced | None = None
        # 双方预载状态（内存态，不持久化；absent = 从未上报）
        self.preload_a: PreloadStatus = "absent"
        self.preload_b: PreloadStatus = "absent"
        self.wins_a: int = 0
        self.wins_b: int = 0
        self.round_counter: int = 0
        self.current_round_id: str | None = None
        # 分段采样（MULTI 实时差距跟踪；纯内存，回合结束即弃）：键 = (采样方,
        # level_index, seq)，dict 保持插入序 → 重连回放天然按时间顺序。
        self.subsegments: dict[tuple[Seat, int, int], SubsegmentSample] = {}
        # 采样平面穿越事件（同一平面可多次上报，settled-event 结算模型）：键 =
        # (穿越方, level_index, seq) → 穿越时刻列表（只留最新若干条，见
        # match_fsm.SUBSEGMENT_HIT_EVENTS_CAP）；结算取最后一条，结算后保留
        # 供后续再穿越追加（amend 修正）。
        self.subsegment_hits: dict[tuple[Seat, int, int], list[int]] = {}
        # 已结算广播的进度游标：穿越方 → 最高 (level_index, seq)。低于它的
        # 迟到乱序事件直接忽略，保证导播画面单调不回跳（曲折路线治理核心）。
        self.subsegment_frontier: dict[Seat, tuple[int, int]] = {}
        # 折返重访会话：选手失败折返重来期间（首次重开低键起 → 重新追平最远
        # 进度止），低于游标的穿越照常接受与结算广播——计时器坠落不清零，
        # 重穿时刻自带罚时成本，数值随时间单调增长，播出不会回跳；画面不再
        # 冻结到追平最远进度为止。
        self.subsegment_revisiting: set[Seat] = set()
        # 各席最近一次穿越事件时刻（重访会话的开启门槛基线；被丢弃的绕行
        # 回声同样刷新——持续绕行打不开会话）。
        self.subsegment_last_t: dict[Seat, int] = {}
        # 静默结算任务：键同 subsegment_hits。最后一次穿越后静默期（见
        # match_fsm.SUBSEGMENT_SETTLE_QUIET_S）无再穿越即结算广播；新事件到达
        # 会取消旧任务重新起算。
        self.subsegment_settle_tasks: dict[
            tuple[Seat, int, int], asyncio.Task[None]
        ] = {}
        # 选手实时计时（每秒上报）：按席暂存最近一条，裁判/导播晚连时握手补发。
        self.live_times: dict[Seat, SrvLiveTime] = {}
        # 裁判独立倒计时器（每比赛至多一个）
        self.counter_timer: CounterTimer | None = None
        # 比赛开始倒计时（auto 可被取消 / manual 不可）
        self.countdown_timer: CountdownTimer | None = None
        self.countdown_source: str | None = None
        # 预载门控超时兜底计时器（双方 ready 且门控未过时启动；见 match_fsm）
        self.preload_gate_timer: CountdownTimer | None = None
        # ban/pick 草稿状态（裁判端权威上报，后端转发给导播）
        self.draft_state: dict | None = None
        # 当前比赛系统消息语言（裁判 !lang 切换；内存态，重启回默认）
        self.locale: str = settings.default_locale

    @property
    def id(self) -> str:
        return self.match.id

    def same_key_connections(self, account_id: str, seat: Seat) -> list[Connection]:
        """同身份 key（account_id + seat + 本比赛）的既有连接，供 exclusive 接管顶掉。

        导播从集合中筛同账号（OBS 多源常多连接并存）；其余座位单槽，
        槽内连接同账号才属同 key（裁判改派后他人的连接不算，走静默替换旧语义）。
        """
        if seat == Seat.DIRECTOR:
            return [c for c in self.directors if c.account_id == account_id]
        conn = self.connections.get(seat)
        if conn is not None and conn.account_id == account_id:
            return [conn]
        return []

    def has_connection(self, conn: Connection) -> bool:
        """该连接是否仍登记于本比赛（被顶掉/清理后其在途消息据此忽略）。"""
        if conn.seat == Seat.DIRECTOR:
            return conn in self.directors
        return self.connections.get(conn.seat) is conn

    def seats_connected(self) -> set[Seat]:
        seats = set(self.connections)
        if self.directors:
            seats.add(Seat.DIRECTOR)
        return seats

    def reset_ready(self) -> None:
        self.a_ready = False
        self.b_ready = False

    def reset_subsegments(self) -> None:
        """清空本回合的回合级实时遥测（begin_prep / _begin_round 时调用）：
        分段采样、穿越事件、结算游标/任务与实时计时暂存。"""
        for task in self.subsegment_settle_tasks.values():
            task.cancel()
        self.subsegment_settle_tasks.clear()
        self.subsegments.clear()
        self.subsegment_hits.clear()
        self.subsegment_frontier.clear()
        self.subsegment_revisiting.clear()
        self.subsegment_last_t.clear()
        self.live_times.clear()


class MatchRegistry:
    """比赛实时状态注册表。"""

    def __init__(self) -> None:
        self._stores: dict[str, MatchStore] = {}

    def get(self, match_id: str) -> MatchStore | None:
        return self._stores.get(match_id)

    def get_or_create(self, match: Match) -> MatchStore:
        store = self._stores.get(match.id)
        if store is not None:
            # 刷新配置快照（管理员可能修改了元数据）
            store.match = match
            return store
        store = MatchStore(match)
        self._stores[match.id] = store
        return store

    def all(self) -> list[MatchStore]:
        return list(self._stores.values())
