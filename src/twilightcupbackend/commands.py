"""``!`` 聊天命令解析与执行。

由 ConnectionManager 在聊天消息以 ``!`` 开头时调用（见 ``_on_chat``）。
本期实现：``!ready``（选手切换准备）、``!roll``（1-100 随机）、
``!timer [seconds]`` / ``!timer reset``（裁判独立倒计时器）、
``!lang [id]``（裁判切换比赛系统消息语言）。
所有命令均以系统消息形式广播反馈（§5.3/5.4/5.5）；文案键见 locales/*.txt。
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from . import i18n
from .datatypes import MatchStatus, Seat
from .protocol import SrvCounterAlert, SrvCounterState, SrvError, SrvReadyState
from .timer_service import CounterTimer

if TYPE_CHECKING:
    from .connection_manager import ConnectionManager
    from .stores import Connection, MatchStore


class CommandHandler:
    """处理 ``!`` 开头的聊天命令。作为 ConnectionManager.command_router 注入。"""

    def __init__(self, cm: ConnectionManager) -> None:
        self.cm = cm

    async def __call__(self, conn: Connection, text: str) -> bool:
        parts = text[1:].split(None, 1)
        if not parts or not parts[0]:
            await self.cm.system_message(conn.match_id, self.cm.tr(conn.match_id, "command.empty"))
            return True
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        match name:
            case "ready":
                await self._ready(conn)
            case "roll":
                await self._roll(conn)
            case "timer":
                await self._timer(conn, arg)
            case "lang":
                await self._lang(conn, arg)
            case _:
                await self.cm.system_message(
                    conn.match_id,
                    self.cm.tr(conn.match_id, "command.unknown", name=name),
                    kind="command_error",
                )
        return True

    # ------------------------------------------------------------------

    async def _ready(self, conn: Connection) -> None:
        if conn.seat not in (Seat.PLAYER_A, Seat.PLAYER_B):
            await self.cm.send(
                conn,
                SrvError(code=403, msg=self.cm.tr(conn.match_id, "ready.only_players")),
            )
            return
        store = self.cm.registry.get(conn.match_id)
        if store is None:
            return
        if store.match.status == MatchStatus.PAUSED:
            await self.cm.send(
                conn,
                SrvError(code=409, msg=self.cm.tr(conn.match_id, "error.match_paused")),
            )
            return
        locale = store.locale
        if conn.seat == Seat.PLAYER_A:
            store.a_ready = not store.a_ready
            ready, seat = store.a_ready, Seat.PLAYER_A
        else:
            store.b_ready = not store.b_ready
            ready, seat = store.b_ready, Seat.PLAYER_B
        await self.cm.broadcast_match(
            conn.match_id, SrvReadyState(a_ready=store.a_ready, b_ready=store.b_ready)
        )
        await self.cm.system_message(
            conn.match_id,
            self.cm.tr(
                conn.match_id,
                "ready.on" if ready else "ready.off",
                player=i18n.seat_name(seat, locale),
            ),
            kind="ready",
        )
        # 双方就绪/取消就绪后通知状态机（触发/中断自动开始倒计时）
        if self.cm.match_engine is not None:
            await self.cm.match_engine.on_ready_changed(conn.match_id)

    async def _roll(self, conn: Connection) -> None:
        value = random.randint(1, 100)
        match_id = conn.match_id
        await self.cm.system_message(
            match_id,
            self.cm.tr(
                match_id,
                "roll.result",
                player=i18n.seat_name(conn.seat, self.cm.locale_for(match_id)),
                value=value,
            ),
            kind="roll",
        )

    async def _timer(self, conn: Connection, arg: str) -> None:
        if conn.seat != Seat.REFEREE:
            await self.cm.send(
                conn,
                SrvError(code=403, msg=self.cm.tr(conn.match_id, "timer.only_referee")),
            )
            return
        store = self.cm.registry.get(conn.match_id)
        if store is None:
            return
        if store.match.status == MatchStatus.PAUSED:
            await self.cm.send(
                conn,
                SrvError(code=409, msg=self.cm.tr(conn.match_id, "error.match_paused")),
            )
            return

        if arg.lower() == "reset":
            await self._timer_reset(conn, store)
            return

        try:
            seconds = int(arg)
        except ValueError:
            await self.cm.send(
                conn,
                SrvError(code=400, msg=self.cm.tr(conn.match_id, "timer.invalid")),
            )
            return
        if seconds <= 0:
            await self.cm.send(
                conn,
                SrvError(code=400, msg=self.cm.tr(conn.match_id, "timer.positive")),
            )
            return
        if store.counter_timer is not None:
            # 已有计时器在跑时直接静默覆盖（旧计时器不发任何输出）
            await store.counter_timer.cancel()
            store.counter_timer = None

        await self._timer_start(conn, store, seconds)

    async def _timer_start(
        self, conn: Connection, store: MatchStore, seconds: int
    ) -> None:
        match_id = conn.match_id

        async def on_alert(remaining: int) -> None:
            await self.cm.broadcast_match(
                match_id, SrvCounterAlert(remaining_secs=remaining)
            )
            await self.cm.system_message(
                match_id,
                self.cm.tr(
                    match_id,
                    "timer.remaining",
                    m=remaining // 60,
                    s=remaining % 60,
                ),
                kind="counter",
            )

        async def on_done() -> None:
            store.counter_timer = None
            await self.cm.broadcast_match(
                match_id, SrvCounterState(remaining_secs=None)
            )
            await self.cm.system_message(
                match_id, self.cm.tr(match_id, "timer.ended"), kind="counter"
            )

        timer = CounterTimer(seconds, on_alert, on_done)
        store.counter_timer = timer
        timer.start()
        await self.cm.broadcast_match(match_id, SrvCounterState(remaining_secs=seconds))
        await self.cm.system_message(
            match_id,
            self.cm.tr(match_id, "timer.started", seconds=seconds),
            kind="counter",
        )

    async def _timer_reset(self, conn: Connection, store: MatchStore) -> None:
        timer = store.counter_timer
        if timer is None:
            await self.cm.send(
                conn,
                SrvError(code=400, msg=self.cm.tr(conn.match_id, "timer.no_active")),
            )
            return
        await timer.cancel()
        store.counter_timer = None
        await self.cm.broadcast_match(
            conn.match_id, SrvCounterState(remaining_secs=None)
        )
        await self.cm.system_message(
            conn.match_id, self.cm.tr(conn.match_id, "timer.reset_done"), kind="counter"
        )

    async def _lang(self, conn: Connection, arg: str) -> None:
        """裁判切换比赛系统消息语言（``!lang {id}``，无参列出可用语言）。"""
        if conn.seat != Seat.REFEREE:
            await self.cm.send(
                conn,
                SrvError(code=403, msg=self.cm.tr(conn.match_id, "lang.only_referee")),
            )
            return
        store = self.cm.registry.get(conn.match_id)
        if store is None:
            return
        languages = ", ".join(i18n.catalog.languages())
        if not arg:
            await self.cm.system_message(
                conn.match_id,
                self.cm.tr(conn.match_id, "lang.available", list=languages),
            )
            return
        if not i18n.catalog.has(arg):
            await self.cm.send(
                conn,
                SrvError(
                    code=400,
                    msg=self.cm.tr(conn.match_id, "lang.unknown", id=arg, list=languages),
                ),
            )
            return
        store.locale = arg
        # 切换确认用新语言渲染
        await self.cm.system_message(
            conn.match_id, self.cm.tr(conn.match_id, "lang.changed", id=arg)
        )
