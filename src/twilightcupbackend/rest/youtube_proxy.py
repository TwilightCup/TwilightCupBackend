"""YouTube 直播流同源代理（导播选手画面专用）。

浏览器 / OBS CEF 直接 iframe 嵌入 YouTube 直播（即使使用 embed 页）在
Chrome 142+ 的 Local Network Access 防护下仍会触发“公共页面连接本地网络被
阻止”。因此参考 B站直播的处理方式：后端用 yt-dlp 解析 YouTube 直播 HLS
视频流地址，再以同源 `/api/youtube/live/*` 代理 HLS 播放列表与视频分片，
前端用 hls.js 播放同源 HLS，彻底绕开 iframe 和本地设备探测。

需要 DIRECTOR/REFEREE/ADMIN 角色。代理流是 hls.js 发起的 GET，无法携带
Authorization 头，因此鉴权令牌通过 URL ``token`` 参数传递。
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from collections.abc import AsyncIterator
from typing import Any

import httpx
from classy_fastapi import Routable, get
from fastapi import HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse

from ..auth import Account, AccountType, decode_token, get_db, get_settings

try:
    import yt_dlp
except ImportError:  # 未安装时由依赖锁定保证生产可用，测试环境可缺失
    yt_dlp = None  # type: ignore[assignment]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0"
)
YT_REFERER = "https://www.youtube.com/"
YT_ORIGIN = "https://www.youtube.com"

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_client: httpx.AsyncClient | None = None
_hls_cache: dict[str, tuple[float, str]] = {}
_HLS_TTL_S = 120.0


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Referer": YT_REFERER},
        )
    return _client


async def _require_viewer_query(request: Request, token: str) -> Account:
    """从 URL query 读取 JWT 并校验 DIRECTOR/REFEREE/ADMIN。"""
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少令牌")
    settings = get_settings(request)
    db = get_db(request)
    try:
        claims = decode_token(token, settings)
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效或已过期") from exc
    sub = claims.get("sub")
    if not isinstance(sub, str):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效")
    account = db.accounts.get(sub)
    if account is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不存在")
    if not set(account.roles) & {
        AccountType.ADMIN,
        AccountType.REFEREE,
        AccountType.DIRECTOR,
    }:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "权限不足")
    return account


def _extract_hls_url_sync(video_id: str) -> str:
    """同步调 yt-dlp 解析最高清的 video-only HLS 直播流地址。"""
    if yt_dlp is None:
        raise HTTPException(503, "服务端缺少 yt-dlp，无法解析 YouTube 直播流")
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "nocheckcertificate": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False
        )
    if not info:
        raise HTTPException(502, "YouTube 直播解析失败")
    if info.get("live_status") not in ("is_live", True):
        raise HTTPException(404, "该 YouTube 视频当前未开播")
    candidates = [
        f
        for f in (info.get("formats") or [])
        if f.get("protocol") == "m3u8_native" and f.get("vcodec") not in (None, "none")
    ]
    if not candidates:
        raise HTTPException(502, "YouTube 未返回可播放的 HLS 视频流")
    # 优先高分辨率 / 高码率
    candidates.sort(
        key=lambda f: (
            f.get("height") or 0,
            f.get("tbr") or 0,
            f.get("width") or 0,
        ),
        reverse=True,
    )
    return str(candidates[0]["url"])


async def _get_hls_url(video_id: str) -> str:
    now = time.monotonic()
    cached = _hls_cache.get(video_id)
    if cached and now - cached[0] < _HLS_TTL_S:
        return cached[1]
    url = await asyncio.to_thread(_extract_hls_url_sync, video_id)
    _hls_cache[video_id] = (now, url)
    return url


def _allowed_upstream(url: str) -> bool:
    """只允许代理 googlevideo 的 HLS 清单 / 分片，避免 SSRF。"""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return host == "googlevideo.com" or host.endswith(".googlevideo.com")


def _proxy_url(target: str, token: str) -> str:
    token_q = urllib.parse.quote(token, safe="")
    url_q = urllib.parse.quote(target, safe="")
    return f"/api/youtube/live/file?token={token_q}&url={url_q}"


def _rewrite_m3u8(text: str, token: str) -> str:
    """把 HLS 清单里的绝对分片/子清单地址换成同源代理地址。"""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if stripped.startswith("#"):
            # 处理 #EXT-X-KEY / #EXT-X-MAP 等 URI="..." 形式的标签
            line = re.sub(
                r'URI="([^"]+)"',
                lambda m: f'URI="{_proxy_url(m.group(1), token)}"',
                line,
            )
            lines.append(line)
            continue
        lines.append(_proxy_url(stripped, token))
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


async def _fetch_m3u8(url: str, token: str) -> Response:
    try:
        resp = await _http().get(
            url, headers={"Referer": YT_REFERER, "Origin": YT_ORIGIN}
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"YouTube HLS 清单获取失败：{exc}") from exc
    if resp.status_code >= 400:
        raise HTTPException(502, f"YouTube HLS 清单 HTTP {resp.status_code}")
    text = resp.text
    rewritten = _rewrite_m3u8(text, token)
    return Response(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


class YouTubeProxyController(Routable):
    def __init__(self) -> None:
        super().__init__(prefix="/youtube/live", tags=["youtube"])

    @get(
        "/stream",
        summary="YouTube 直播 HLS 同源代理流",
        description="后端用 yt-dlp 解析 YouTube 直播 HLS 视频流地址，返回重写为"
        "同源 `/api/youtube/live/file` 的播放列表；前端用 hls.js 播放。"
        "需 ?token= 传导播/裁判/管理 JWT。",
        responses={
            401: {"description": "缺少或无效令牌"},
            403: {"description": "权限不足"},
            404: {"description": "视频未开播"},
            502: {"description": "YouTube 解析/清单错误"},
            503: {"description": "服务端缺少 yt-dlp"},
        },
    )
    async def stream(
        self,
        request: Request,
        video_id: str = Query(description="YouTube 视频 ID"),
        token: str = Query(description="导播/裁判/管理 JWT"),
    ) -> Response:
        await _require_viewer_query(request, token)
        if not VIDEO_ID_RE.fullmatch(video_id or ""):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "无效的 YouTube 视频 ID")
        hls_url = await _get_hls_url(video_id)
        if not _allowed_upstream(hls_url):
            raise HTTPException(502, "YouTube 返回了非预期流地址")
        return await _fetch_m3u8(hls_url, token)

    @get(
        "/file",
        summary="YouTube HLS 分片/子清单同源代理",
        description="代理 YouTube HLS 清单或视频分片；清单会再次重写为同源地址，"
        "分片原样转发。需 ?token= 传导播/裁判/管理 JWT。",
        responses={
            401: {"description": "缺少或无效令牌"},
            403: {"description": "权限不足"},
            502: {"description": "上游请求失败"},
        },
    )
    async def file(
        self,
        request: Request,
        url: str = Query(description="YouTube googlevideo 上游 URL"),
        token: str = Query(description="导播/裁判/管理 JWT"),
    ) -> Response:
        await _require_viewer_query(request, token)
        if not url or not _allowed_upstream(url):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "无效的上游地址")
        try:
            resp = await _http().get(
            url, headers={"Referer": YT_REFERER, "Origin": YT_ORIGIN}
        )
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"YouTube 分片获取失败：{exc}") from exc
        if resp.status_code >= 400:
            raise HTTPException(502, f"YouTube 分片 HTTP {resp.status_code}")

        ct = (resp.headers.get("content-type") or "").lower()
        if "mpegurl" in ct or resp.content.lstrip().startswith(b"#EXTM3U"):
            return await _fetch_m3u8(url, token)

        async def body() -> AsyncIterator[bytes]:
            yield resp.content

        return StreamingResponse(
            body(),
            status_code=200,
            headers={
                "Cache-Control": "no-store",
                "Content-Type": (
                    resp.headers.get("content-type") or "application/octet-stream"
                ),
            },
        )
