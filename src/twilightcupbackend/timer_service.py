"""计时器服务。

参考 AShareGateway timer.py 的 asyncio task 风格（start/run/cancel + 关闭标志）。
本期实现裁判独立倒计时器 ``CounterTimer``（独立计时，回合开始时由状态机
静默停止，§5.4）；比赛开始倒计时 ``CountdownTimer`` 在 M6 随状态机一并实现。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from logging import Logger, getLogger


def alert_seconds(total: int) -> set[int]:
    """计算应在哪些剩余秒数发出告警（整分钟 + 30/20/10 + 5..1，均 < total）。"""
    candidates: set[int] = set()
    m = 1
    while m * 60 < total:
        candidates.add(m * 60)
        m += 1
    for v in (30, 20, 10, 5, 4, 3, 2, 1):
        if v < total:
            candidates.add(v)
    return candidates


class CounterTimer:
    """裁判独立倒计时器。

    ``on_alert`` 在每个告警点（含归零 0）被调用；``on_done`` 在自然结束时调用。
    ``tick`` 默认 1 秒，测试时可调小。
    """

    def __init__(
        self,
        seconds: int,
        on_alert: Callable[[int], Awaitable[None]],
        on_done: Callable[[], Awaitable[None]],
        logger: Logger | None = None,
        tick: float = 1.0,
    ) -> None:
        self.total = seconds
        self._on_alert = on_alert
        self._on_done = on_done
        self._tick = tick
        self.logger = logger or getLogger("CounterTimer")
        self.task: asyncio.Task[None] | None = None
        self._alerts = alert_seconds(seconds)
        self._closed = False

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.task = loop.create_task(self._run())

    async def _run(self) -> None:
        remaining = self.total
        try:
            while remaining > 0 and not self._closed:
                await asyncio.sleep(self._tick)
                remaining -= 1
                if remaining in self._alerts:
                    await self._on_alert(remaining)
            if not self._closed:
                await self._on_alert(0)  # 归零
                await self._on_done()
        except asyncio.CancelledError:
            self.logger.debug("CounterTimer 被取消。")

    async def cancel(self) -> None:
        self._closed = True
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


class CountdownTimer:
    """比赛开始倒计时（逐秒提示，归零发令）。

    ``on_tick`` 在每剩余秒（不含初始延迟值）调用；``on_zero`` 在归零时调用。
    auto 倒计时可被取消（选手取消准备），manual 不可。
    """

    def __init__(
        self,
        seconds: int,
        source: str,
        on_tick: Callable[[int], Awaitable[None]],
        on_zero: Callable[[], Awaitable[None]],
        logger: Logger | None = None,
        tick: float = 1.0,
    ) -> None:
        self.total = seconds
        self.source = source
        self._on_tick = on_tick
        self._on_zero = on_zero
        self._tick = tick
        self.logger = logger or getLogger("CountdownTimer")
        self.task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.task = loop.create_task(self._run())

    async def _run(self) -> None:
        remaining = self.total
        try:
            while remaining > 0 and not self._closed:
                await asyncio.sleep(self._tick)
                remaining -= 1
                if remaining > 0:
                    await self._on_tick(remaining)
            if not self._closed:
                await self._on_zero()
        except asyncio.CancelledError:
            self.logger.debug("CountdownTimer(%s) 被取消。", self.source)

    async def cancel(self) -> None:
        self._closed = True
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
