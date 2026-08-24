"""speedrun.com 只读代理（导播 categoryinfo 场景与管理端映射选择器共用）。

浏览器 / OBS CEF 直连 speedrun.com 在部分网络环境不可达（或被浏览器扩展、
代理策略拦截），统一改走后端同源代理，并做进程内 TTL 缓存削峰（上游限流
100 请求/分）。仅代理以下白名单只读端点，需任一 ADMIN/REFEREE/DIRECTOR
角色（require_viewer）：

- GET /speedrun/game-meta                      → /games/{id}?embed=categories,levels
- GET /speedrun/variables?category_id&level_id → /categories|levels/{id}/variables
- GET /speedrun/leaderboard?category_id&level_id&top&var-*
- GET /speedrun/user?lookup=                   → /users?lookup=
- GET /speedrun/pb?user_id=                    → /users/{id}/personal-bests?game=

游戏白名单：HFF 主游戏（默认）与 Category Extensions 子游戏（No CP% /
Jumpless% 词条项目）。

上游 420 限流原状态码透传（前端按限流分支提示）；其余上游错误与网络失败
统一 502；响应体为 speedrun.com 原文 JSON。
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from classy_fastapi import Routable, get
from fastapi import Depends, HTTPException, Query, Request

from ..auth import Account, require_viewer

SR_BASE = "https://www.speedrun.com/api/v1"
HFF_GAME_ID = "k6qgnmdg"  # Human: Fall Flat（黄昏杯主项目）
EXT_GAME_ID = "o6gl20nd"  # Human Fall Flat Category Extensions（No CP%/Jumpless%）
_ALLOWED_GAMES = {HFF_GAME_ID, EXT_GAME_ID}
TTL_META_S = 600.0  # 游戏/变量/用户解析：变更极少
TTL_LEADERBOARD_S = 60.0  # 榜单：随 run 提交变动


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


_cache = _TtlCache()


async def _proxy_json(
    url: str, ttl: float, params: dict[str, str] | None = None
) -> Any:
    """拉取上游 JSON（带 TTL 缓存）；失败转 HTTPException。"""
    key = str(httpx.Request("GET", url, params=params).url)
    hit = _cache.get(key)
    if hit is not None:
        return hit
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
    _cache.put(key, payload, ttl)
    return payload


class SpeedrunProxyController(Routable):
    def __init__(self) -> None:
        super().__init__(prefix="/speedrun", tags=["speedrun"])

    @get(
        "/game-meta",
        summary="游戏分类与关卡元数据（speedrun.com 代理）",
        description="透传 /games/{id}?embed=categories,levels；管理端映射选择器"
        "与 categoryinfo 场景自动解析的选项源。默认 HFF，game_id 可选 "
        "Category Extensions 子游戏。缓存 10 分钟。",
        responses={
            400: {"description": "游戏 id 不在白名单"},
            502: {"description": "上游请求失败"},
        },
    )
    async def game_meta(
        self,
        _: Account = Depends(require_viewer),
        game_id: str = Query(default=HFF_GAME_ID, description="游戏 id（默认 HFF）"),
    ) -> Any:
        return await _proxy_json(
            f"{SR_BASE}/games/{_game_id(game_id)}",
            TTL_META_S,
            {"embed": "categories,levels"},
        )

    @get(
        "/variables",
        summary="分类/关卡的子分类变量（speedrun.com 代理）",
        description="level_id 优先走 /levels/{id}/variables，否则 /categories/{id}"
        "/variables。缓存 10 分钟。",
        responses={
            400: {"description": "category_id 与 level_id 均缺失"},
            502: {"description": "上游请求失败"},
        },
    )
    async def variables(
        self,
        _: Account = Depends(require_viewer),
        category_id: str | None = Query(default=None, description="分类 id"),
        level_id: str | None = Query(default=None, description="关卡 id（优先）"),
    ) -> Any:
        if not category_id and not level_id:
            raise HTTPException(400, "category_id 与 level_id 至少传一个")
        url = (
            f"{SR_BASE}/levels/{level_id}/variables"
            if level_id
            else f"{SR_BASE}/categories/{category_id}/variables"
        )
        return await _proxy_json(url, TTL_META_S)

    @get(
        "/leaderboard",
        summary="项目排行榜（speedrun.com 代理）",
        description="单关传 level_id 走 IL 端点；var-{变量id}={值id} 为子分类过滤"
        "（原样转发）。缓存 60 秒。",
        responses={502: {"description": "上游请求失败"}},
    )
    async def leaderboard(
        self,
        request: Request,
        _: Account = Depends(require_viewer),
        category_id: str = Query(description="分类 id"),
        level_id: str | None = Query(default=None, description="单关 IL 的关卡 id"),
        top: int = Query(default=15, ge=1, le=50, description="名次数"),
        game_id: str = Query(default=HFF_GAME_ID, description="游戏 id（默认 HFF）"),
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
        return await _proxy_json(
            f"{SR_BASE}/leaderboards/{_game_id(game_id)}/{board}",
            TTL_LEADERBOARD_S,
            params,
        )

    @get(
        "/user",
        summary="按用户名/ID 解析 speedrun.com 用户（代理）",
        description="透传 /users?lookup=；找不到时上游返回空 data。缓存 10 分钟。",
        responses={502: {"description": "上游请求失败"}},
    )
    async def user(
        self,
        _: Account = Depends(require_viewer),
        lookup: str = Query(description="用户名或 8 位用户 id"),
    ) -> Any:
        return await _proxy_json(f"{SR_BASE}/users", TTL_META_S, {"lookup": lookup})

    @get(
        "/pb",
        summary="用户的全部个人最好成绩（代理）",
        description="透传 /users/{id}/personal-bests?game={id}；前端按分类/关卡/"
        "子分类过滤出当前项目的 PB（名次可超出榜单 Top N）。game_id 默认 HFF。"
        "缓存 60 秒。",
        responses={
            400: {"description": "游戏 id 不在白名单"},
            502: {"description": "上游请求失败"},
        },
    )
    async def pb(
        self,
        _: Account = Depends(require_viewer),
        user_id: str = Query(description="speedrun.com 用户 id"),
        game_id: str = Query(default=HFF_GAME_ID, description="游戏 id（默认 HFF）"),
    ) -> Any:
        return await _proxy_json(
            f"{SR_BASE}/users/{user_id}/personal-bests",
            TTL_LEADERBOARD_S,
            {"game": _game_id(game_id)},
        )
