"""内存实时状态（无锁）：所有变更均在 asyncio 事件循环内完成。

参考 AShareGateway stores.py 的 PublicState 风格。``MatchStore`` 持有单场比赛的
实时状态与各座位的当前连接；``MatchRegistry`` 按比赛 id 聚合。
M4 仅含连接与基础状态；M6/M7 会补充回合、计时器、累计比分等。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import WebSocket

from .config import settings
from .datatypes import Match, MatchPhase, Seat
from .timer_service import CountdownTimer, CounterTimer


@dataclass(eq=False)
class Connection:
    """一条 WebSocket 连接及其归属（按对象身份判等/哈希，可入 set）。"""

    websocket: WebSocket
    account_id: str
    display_name: str
    seat: Seat
    match_id: str

    @property
    def read_only(self) -> bool:
        """导播只读围观。"""
        return self.seat == Seat.DIRECTOR


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
        self.wins_a: int = 0
        self.wins_b: int = 0
        self.round_counter: int = 0
        self.current_round_id: str | None = None
        # 裁判独立倒计时器（每比赛至多一个）
        self.counter_timer: CounterTimer | None = None
        # 比赛开始倒计时（auto 可被取消 / manual 不可）
        self.countdown_timer: CountdownTimer | None = None
        self.countdown_source: str | None = None
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
