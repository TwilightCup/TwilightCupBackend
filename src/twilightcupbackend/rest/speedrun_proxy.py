"""speedrun.com 只读代理（导播 categoryinfo 场景与管理端映射选择器共用）。

浏览器 / OBS CEF 直连 speedrun.com 在部分网络环境不可达（或被浏览器扩展、
代理策略拦截），统一改走后端同源代理，并做两级缓存削峰（上游限流 100
请求/分）：进程内 TTL 缓存管「新鲜度」，MongoDB 持久化缓存（每个速通
项目一文档，按 fetched_at 保留 7 天）管「跨重启可用性」。前端拉取采用
SWR 双请求——``mode=cached`` 秒回持久化缓存先渲染，``mode=refresh``
并发拉上游成功后替换。仅代理以下白名单只读端点，需任一
ADMIN/REFEREE/DIRECTOR 角色（require_viewer）：

- GET /speedrun/game-meta                      → /games/{id}?embed=categories,levels
- GET /speedrun/variables?category_id&level_id → /categories|levels/{id}/variables
- GET /speedrun/leaderboard?category_id&level_id&top&var-*
- GET /speedrun/user?lookup=                   → /users?lookup=
- GET /speedrun/pb?user_id=                    → /users/{id}/personal-bests?game=

游戏白名单：HFF 主游戏（默认）与 Category Extensions 子游戏（No CP% /
Jumpless% 词条项目）。

上游 420 限流原状态码透传（前端按限流分支提示）；其余上游错误与网络失败
统一 502。auto/refresh 响应体为 speedrun.com 原文 JSON；cached 响应为
``{"fetched_at": ..., "data": ...}`` 信封（无缓存时双 null）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from logging import getLogger
from typing import Any, Literal

import httpx
from classy_fastapi import Routable, get
from fastapi import Depends, HTTPException, Query, Request

from ..auth import Account, require_viewer
from ..controllers import DBController
from ..databases import SpeedrunCaches
from ..datatypes import SpeedrunCacheDoc, SpeedrunCacheKind, now_ts

SR_BASE = "https://www.speedrun.com/api/v1"
HFF_GAME_ID = "k6qgnmdg"  # Human: Fall Flat（黄昏杯主项目）
EXT_GAME_ID = "o6gl20nd"  # Human Fall Flat Category Extensions（No CP%/Jumpless%）
_ALLOWED_GAMES = {HFF_GAME_ID, EXT_GAME_ID}
TTL_META_S = 600.0  # 游戏/变量/用户解析：变更极少
TTL_LEADERBOARD_S = 60.0  # 榜单：随 run 提交变动

FetchMode = Literal["auto", "cached", "refresh"]

_MODE_DESC = (
    "mode：auto（默认）走内存 TTL 缓存并在上游成功后写穿持久化；cached 只读"
    "持久化缓存秒回 {fetched_at, data} 信封（无缓存 200 双 null；最长可能旧至"
    "保留期 7 天）；refresh 绕过内存强制拉上游并比对写回（与缓存不同才更新"
    "文档，相同只更 fetched_at）。持久化缓存按速通项目区分存储。"
)

_LOG = getLogger(__name__)


def _game_id(game_id: str) -> str:
    """校验游戏 id 白名单（默认 HFF），非法返回 400。"""
    gid = (game_id or HFF_GAME_ID).strip()
    if gid not in _ALLOWED_GAMES:
        raise HTTPException(400, "不支持的游戏 id")
    return gid


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
    return _client


class _TtlCache:
    """进程内 TTL 缓存（monotonic 计时；条目上限防膨胀，超限整体清空）。"""

    def __init__(self, cap: int = 512) -> None:
        self._cap = cap
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._store.get(key)
        if hit is None:
            return None
        expires, payload = hit
        if expires < time.monotonic():
            del self._store[key]
            return None
        return payload

    def put(self, key: str, payload: Any, ttl: float) -> None:
        if len(self._store) >= self._cap:
            self._store.clear()
        self._store[key] = (time.monotonic() + ttl, payload)


def _cache_key(url: str, params: dict[str, str] | None) -> str:
    """上游完整 URL 作缓存 key（httpx 规范化；params 顺序即 key 的一部分）。"""
    return str(httpx.Request("GET", url, params=params).url)


def _doc_id(key: str) -> str:
    """缓存文档确定性主键：sha256(key) 前 32 位 hex（与 Document.id 形状一致）。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _envelope(doc: SpeedrunCacheDoc | None) -> dict[str, Any]:
    """cached 模式响应信封；无缓存时双 null（冷缓存是 SWR 的正常初态）。"""
    return {
        "fetched_at": doc.fetched_at.isoformat() if doc else None,
        "data": doc.data if doc else None,
    }


class _SpeedrunStore:
    """speedrun 上游数据存取：进程内 TTL + 单飞在途 + Mongo 持久层。

    三种读法（mode）：cached 只读 Mongo（零上游）；auto 内存命中即回
    （不碰 Mongo），miss 后与 refresh 同路；refresh 绕过内存强制上游，
    成功后回填内存并比对写回 Mongo。同 key 的并发请求经 ``_inflight``
    合并为一次上游调用（无锁：只在事件循环内变更）。
    """

    def __init__(self, repo: SpeedrunCaches) -> None:
        self._repo = repo
        self._mem = _TtlCache()
        self._inflight: dict[str, asyncio.Task[Any]] = {}

    async def get(
        self,
        mode: FetchMode,
        kind: SpeedrunCacheKind,
        url: str,
        ttl: float,
        params: dict[str, str] | None = None,
    ) -> Any:
        key = _cache_key(url, params)
        if mode == "cached":
            return _envelope(self._repo.get(_doc_id(key)))
        if mode == "auto":
            hit = self._mem.get(key)
            if hit is not None:
                return hit
        task = self._inflight.get(key)
        if task is None or task.done():  # done 兜底 done_callback 执行滞后
            task = asyncio.ensure_future(self._fetch_once(kind, key, url, ttl, params))
            self._inflight[key] = task
            task.add_done_callback(lambda _t, _key=key: self._inflight.pop(_key, None))
        # shield：防某个等待请求的客户端断连取消共享的上游 Task（殃及其他等待者）
        return await asyncio.shield(task)

    async def _fetch_once(
        self,
        kind: SpeedrunCacheKind,
        key: str,
        url: str,
        ttl: float,
        params: dict[str, str] | None,
    ) -> Any:
        """单次上游拉取（任务创建者独占执行）；失败转 HTTPException。"""
        try:
            resp = await _http().get(url, params=params)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"speedrun.com 请求失败：{exc}") from exc
        if resp.status_code == 420:
            raise HTTPException(420, "speedrun.com 限流（100 请求/分），请稍后再试")
        if resp.status_code >= 400:
            raise HTTPException(502, f"speedrun.com HTTP {resp.status_code}")
        try:
            payload: Any = resp.json()
        except ValueError as exc:
            raise HTTPException(502, "speedrun.com 响应不是有效 JSON") from exc
        self._mem.put(key, payload, ttl)  # refresh 路径也回填内存
        self._persist(kind, key, payload)
        return payload

    def _persist(self, kind: SpeedrunCacheKind, key: str, payload: Any) -> None:
        """比对写回：与缓存不同 → 整文档 upsert；相同 → 仅更新 fetched_at。

        Mongo 故障只告警不致命——持久化是尽力而为的加速层，不是正确性依赖。
        """
        try:
            doc_id = _doc_id(key)
            existing = self._repo.get(doc_id)
            if existing is not None and existing.data == payload:
                self._repo.update_fields(doc_id, {"fetched_at": now_ts()})
            else:
                self._repo.replace(
                    SpeedrunCacheDoc(
                        id=doc_id,
                        key=key,
                        kind=kind,
                        data=payload,
                        fetched_at=now_ts(),
                    )
                )
        except Exception:
            _LOG.warning("speedrun 缓存持久化失败 (key=%s)", key, exc_info=True)


class SpeedrunProxyController(Routable):
    def __init__(self, db: DBController) -> None:
        super().__init__(prefix="/speedrun", tags=["speedrun"])
        self._store = _SpeedrunStore(db.speedrun_cache)

    @get(
        "/game-meta",
        summary="游戏分类与关卡元数据（speedrun.com 代理）",
        description="透传 /games/{id}?embed=categories,levels；管理端映射选择器"
        "与 categoryinfo 场景自动解析的选项源。默认 HFF，game_id 可选 "
        "Category Extensions 子游戏。内存缓存 10 分钟。" + _MODE_DESC,
        responses={
            400: {"description": "游戏 id 不在白名单"},
            420: {"description": "speedrun.com 限流（原状态码透传）"},
            502: {"description": "上游请求失败"},
        },
    )
    async def game_meta(
        self,
        _: Account = Depends(require_viewer),
        game_id: str = Query(default=HFF_GAME_ID, description="游戏 id（默认 HFF）"),
        mode: FetchMode = Query(default="auto", description="读取模式（SWR）"),
    ) -> Any:
        return await self._store.get(
            mode,
            "game_meta",
            f"{SR_BASE}/games/{_game_id(game_id)}",
            TTL_META_S,
            {"embed": "categories,levels"},
        )

    @get(
        "/variables",
        summary="分类/关卡的子分类变量（speedrun.com 代理）",
        description="level_id 优先走 /levels/{id}/variables，否则 /categories/{id}"
        "/variables。内存缓存 10 分钟。" + _MODE_DESC,
        responses={
            400: {"description": "category_id 与 level_id 均缺失"},
            420: {"description": "speedrun.com 限流（原状态码透传）"},
            502: {"description": "上游请求失败"},
        },
    )
    async def variables(
        self,
        _: Account = Depends(require_viewer),
        category_id: str | None = Query(default=None, description="分类 id"),
        level_id: str | None = Query(default=None, description="关卡 id（优先）"),
        mode: FetchMode = Query(default="auto", description="读取模式（SWR）"),
    ) -> Any:
        if not category_id and not level_id:
            raise HTTPException(400, "category_id 与 level_id 至少传一个")
        url = (
            f"{SR_BASE}/levels/{level_id}/variables"
            if level_id
            else f"{SR_BASE}/categories/{category_id}/variables"
        )
        return await self._store.get(mode, "variables", url, TTL_META_S)

    @get(
        "/leaderboard",
        summary="项目排行榜（speedrun.com 代理）",
        description="单关传 level_id 走 IL 端点；var-{变量id}={值id} 为子分类过滤"
        "（原样转发）。内存缓存 60 秒。" + _MODE_DESC,
        responses={
            400: {"description": "游戏 id 不在白名单"},
            420: {"description": "speedrun.com 限流（原状态码透传）"},
            502: {"description": "上游请求失败"},
        },
    )
    async def leaderboard(
        self,
        request: Request,
        _: Account = Depends(require_viewer),
        category_id: str = Query(description="分类 id"),
        level_id: str | None = Query(default=None, description="单关 IL 的关卡 id"),
        top: int = Query(default=15, ge=1, le=50, description="名次数"),
        game_id: str = Query(default=HFF_GAME_ID, description="游戏 id（默认 HFF）"),
        mode: FetchMode = Query(default="auto", description="读取模式（SWR）"),
    ) -> Any:
        params = {"top": str(top), "embed": "players"}
        for key, value in request.query_params.items():
            if key.startswith("var-") and value:
                params[key] = value
        board = (
            f"level/{level_id}/{category_id}"
            if level_id
            else f"category/{category_id}"
        )
        return await self._store.get(
            mode,
            "leaderboard",
            f"{SR_BASE}/leaderboards/{_game_id(game_id)}/{board}",
            TTL_LEADERBOARD_S,
            params,
        )

    @get(
        "/user",
        summary="按用户名/ID 解析 speedrun.com 用户（代理）",
        description="透传 /users?lookup=；找不到时上游返回空 data。内存缓存 "
        "10 分钟。" + _MODE_DESC,
        responses={
            420: {"description": "speedrun.com 限流（原状态码透传）"},
            502: {"description": "上游请求失败"},
        },
    )
    async def user(
        self,
        _: Account = Depends(require_viewer),
        lookup: str = Query(description="用户名或 8 位用户 id"),
        mode: FetchMode = Query(default="auto", description="读取模式（SWR）"),
    ) -> Any:
        return await self._store.get(
            mode, "user", f"{SR_BASE}/users", TTL_META_S, {"lookup": lookup}
        )

    @get(
        "/pb",
        summary="用户的全部个人最好成绩（代理）",
        description="透传 /users/{id}/personal-bests?game={id}；前端按分类/关卡/"
        "子分类过滤出当前项目的 PB（名次可超出榜单 Top N）。game_id 默认 HFF。"
        "内存缓存 60 秒。" + _MODE_DESC,
        responses={
            400: {"description": "游戏 id 不在白名单"},
            420: {"description": "speedrun.com 限流（原状态码透传）"},
            502: {"description": "上游请求失败"},
        },
    )
    async def pb(
        self,
        _: Account = Depends(require_viewer),
        user_id: str = Query(description="speedrun.com 用户 id"),
        game_id: str = Query(default=HFF_GAME_ID, description="游戏 id（默认 HFF）"),
        mode: FetchMode = Query(default="auto", description="读取模式（SWR）"),
    ) -> Any:
        return await self._store.get(
            mode,
            "pb",
            f"{SR_BASE}/users/{user_id}/personal-bests",
            TTL_LEADERBOARD_S,
            {"game": _game_id(game_id)},
        )
