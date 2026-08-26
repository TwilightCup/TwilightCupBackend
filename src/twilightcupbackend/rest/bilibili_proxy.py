"""B站直播流同源代理（导播选手画面专用）。

浏览器 / OBS CEF 直接 iframe 嵌入 B站直播会遇到两类问题：

- 主站直播间页带 ``X-Frame-Options: SAMEORIGIN``，无法嵌入；
- ``blanc`` 嵌入页在 Chrome 142+ 的 Local Network Access 防护下会触发
  “公共页面连接本地网络被阻止”，播放器区域黑屏/无交互。

因此本模块参考 BililiveRecorder 的取流方式：后端调 ``getRoomPlayInfo``
拿到 HTTP-FLV 地址，再由本服务以 B站要求的 Referer/Origin/UA 拉流并原样
转发给前端。前端用 mpegts.js 播放同源 FLV（OBS 内置 Chromium 支持 MSE），
彻底绕开 iframe 和 P2P/本地网络探测。

需要 DIRECTOR/REFEREE/ADMIN 角色。流地址是 mpegts.js 发起的 GET，无法带
Authorization 头，因此鉴权令牌通过 URL ``token`` 参数传递。
"""

from __future__ import annotations

import hashlib
import random
import time
import urllib.parse
from typing import Any, AsyncIterator

import httpx
from classy_fastapi import Routable, get
from fastapi import HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from ..auth import Account, AccountType, decode_token, get_db, get_settings

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0"
)
LIVE_REFERER = "https://live.bilibili.com/"
LIVE_ORIGIN = "https://live.bilibili.com"
LIVE_API_BASE = "https://api.live.bilibili.com"
WBI_URL = "https://api.bilibili.com/x/web-interface/nav"

# C# BililiveRecorder Wbi.cs 的 64 位混淆表（0..63 索引映射到 img+sub）
_WBI_KEY_MAP = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
_WBI_TTL_S = 4 * 60 * 60
_wbi_key: str | None = None
_wbi_expires = 0.0

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Referer": LIVE_REFERER},
        )
    return _client


def _api_headers() -> dict[str, str]:
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN",
        "Origin": LIVE_ORIGIN,
        "Referer": LIVE_REFERER,
        "User-Agent": USER_AGENT,
    }


async def _get_wbi_key() -> str:
    """获取并缓存 B站 WBI 混淆 key（4 小时一换）。"""
    global _wbi_key, _wbi_expires
    if _wbi_key and time.monotonic() < _wbi_expires:
        return _wbi_key
    resp = await _http().get(WBI_URL, headers=_api_headers())
    resp.raise_for_status()
    obj = resp.json()
    try:
        wbi_img = obj["data"]["wbi_img"]
        img_url: str = wbi_img["img_url"]
        sub_url: str = wbi_img["sub_url"]
    except (KeyError, TypeError) as exc:
        raise HTTPException(502, "B站 WBI key 解析失败") from exc

    def name_from_url(url: str) -> str:
        slash = url.rfind("/")
        dot = url.find(".", slash)
        if slash == -1 or dot == -1:
            raise ValueError("invalid wbi image url")
        return url[slash + 1 : dot]

    full = name_from_url(img_url) + name_from_url(sub_url)
    _wbi_key = "".join(full[i] for i in _WBI_KEY_MAP[:32])
    _wbi_expires = time.monotonic() + _WBI_TTL_S
    return _wbi_key


def _wbi_sign(params: dict[str, object]) -> dict[str, str]:
    """对查询参数做 WBI 签名（与 BililiveRecorder HttpApiClient 同逻辑）。"""
    signed = dict(params)
    signed["wts"] = str(int(time.time()))
    filtered = {
        str(k): "".join(ch for ch in str(v) if ch not in "!'()*")
        for k, v in signed.items()
    }
    query = urllib.parse.urlencode(sorted(filtered.items()))
    key = _wbi_key
    if not key:
        raise RuntimeError("wbi key not ready")
    sign = hashlib.md5((query + key).encode("utf-8")).hexdigest()
    signed["w_rid"] = sign
    return {str(k): str(v) for k, v in signed.items()}


async def _get_room_play_info(room_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """请求 B站 getRoomPlayInfo，返回 (data, raw)；非 0 code 抛 502。"""
    await _get_wbi_key()
    params: dict[str, object] = {
        "room_id": room_id,
        "no_playurl": 0,
        "mask": 1,
        "qn": 10000,
        "platform": "web",
        "protocol": "0,1",
        "format": "0,1,2",
        "codec": "0,1,2",
        "dolby": 5,
        "panorama": 1,
        "hdr_type": "0,1",
        "web_location": "444.8",
    }
    signed = _wbi_sign(params)
    url = f"{LIVE_API_BASE}/xlive/web-room/v2/index/getRoomPlayInfo"
    resp = await _http().get(url, params=signed, headers=_api_headers())
    if resp.status_code >= 400:
        raise HTTPException(502, f"B站直播接口 HTTP {resp.status_code}")
    try:
        obj = resp.json()
    except ValueError as exc:
        raise HTTPException(502, "B站直播接口响应不是有效 JSON") from exc
    if obj.get("code") != 0:
        raise HTTPException(502, f"B站直播接口错误：{obj.get('code')} {obj.get('message')}")
    data = obj.get("data") or {}
    return data, obj


async def _pick_flv_url(room_id: int) -> tuple[str, dict[str, Any]]:
    """按 BililiveRecorder 策略选择 http_stream/flv/avc 的流地址。"""
    data, _ = await _get_room_play_info(room_id)
    if data.get("live_status") != 1:
        raise HTTPException(404, "直播间未开播")
    playurl_info = data.get("playurl_info") or {}
    playurl = playurl_info.get("playurl") or {}
    streams = playurl.get("stream") or []
    if not streams:
        raise HTTPException(502, "B站未返回可用直播流")

    best: dict[str, Any] | None = None
    for stream in streams:
        if stream.get("protocol_name") != "http_stream":
            continue
        for fmt in stream.get("format") or []:
            if fmt.get("format_name") != "flv":
                continue
            for codec in fmt.get("codec") or []:
                if codec.get("codec_name") != "avc":
                    continue
                best = codec
                break
            if best:
                break
        if best:
            break

    if not best:
        # 退而求其次：任意 http_stream/flv
        for stream in streams:
            if stream.get("protocol_name") != "http_stream":
                continue
            for fmt in stream.get("format") or []:
                if fmt.get("format_name") != "flv":
                    continue
                codecs = fmt.get("codec") or []
                if codecs:
                    best = codecs[0]
                    break
            if best:
                break

    if not best:
        raise HTTPException(502, "B站未返回可播放的 FLV 流")

    url_infos = best.get("url_info") or []
    if not url_infos:
        raise HTTPException(502, "B站直播流缺少 url_info")
    # 参考 BililiveRecorder：优先排除 mcdn 节点
    candidates = [u for u in url_infos if ".mcdn." not in (u.get("host") or "")]
    if not candidates:
        candidates = url_infos
    url_info = candidates[random.randrange(len(candidates))]
    full_url = url_info.get("host", "") + best.get("base_url", "") + url_info.get("extra", "")
    return full_url, best


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


class BilibiliProxyController(Routable):
    def __init__(self) -> None:
        super().__init__(prefix="/bilibili/live", tags=["bilibili"])

    @get(
        "/stream",
        summary="B站直播 FLV 同源代理流",
        description="后端以 B站要求的 Referer/Origin/UA 拉取 HTTP-FLV 并转发；"
        "前端用 mpegts.js 播放同源流。需 ?token= 传导播/裁判/管理 JWT。",
        responses={
            401: {"description": "缺少或无效令牌"},
            403: {"description": "权限不足"},
            404: {"description": "直播间未开播"},
            502: {"description": "B站接口/流地址错误"},
        },
    )
    async def stream(
        self,
        request: Request,
        room_id: int = Query(description="B站直播间号"),
        token: str = Query(description="导播/裁判/管理 JWT"),
    ) -> StreamingResponse:
        await _require_viewer_query(request, token)
        flv_url, _ = await _pick_flv_url(room_id)

        upstream_headers = {
            "Accept": "*/*",
            "Origin": LIVE_ORIGIN,
            "Referer": LIVE_REFERER,
            "User-Agent": USER_AGENT,
        }
        range_header = request.headers.get("range")
        if range_header:
            upstream_headers["Range"] = range_header

        upstream_req = _http().build_request("GET", flv_url, headers=upstream_headers)
        try:
            upstream_resp = await _http().send(upstream_req, stream=True)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"B站直播流连接失败：{exc}") from exc
        if upstream_resp.status_code >= 400:
            await upstream_resp.aclose()
            raise HTTPException(502, f"B站直播流 HTTP {upstream_resp.status_code}")

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    yield chunk
            finally:
                await upstream_resp.aclose()

        headers = {
            "Cache-Control": "no-store",
            "Content-Type": "video/x-flv",
        }
        if range_header:
            headers["Accept-Ranges"] = "bytes"
        return StreamingResponse(body(), status_code=200, headers=headers)
