"""Bounded H.264/fMP4 metadata observation; never decodes pixels or logs URLs."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
import time
from dataclasses import dataclass
from typing import cast
from urllib.parse import urljoin, urlsplit

import httpcore


class MediaError(ValueError):
    """Sanitized diagnostic code, safe to expose without source credentials."""


class PublicNetwork(httpcore.AsyncNetworkBackend):
    """Resolve once, reject non-public addresses, connect to that exact IP.

    httpcore retains the original hostname for TLS SNI/certificate verification.
    This avoids a check-then-resolve DNS rebinding window and ignores HTTP proxies.
    """

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        async with asyncio.timeout(timeout or 10):
            addresses = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
            ips = [str(item[4][0]) for item in addresses]
            if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
                raise MediaError("network_blocked")
            backend = cast(httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend())
            return await backend.connect_tcp(
                ips[0], port, timeout, local_address, socket_options
            )

    async def connect_unix_socket(self, *args, **kwargs):
        raise MediaError("network_blocked")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def origin(url: str) -> str:
    try:
        p = urlsplit(url)
        if (
            p.scheme != "https"
            or not p.hostname
            or p.username is not None
            or p.password is not None
            or p.fragment
            or len(url) > 8192
            or any(ord(c) < 33 for c in url)
        ):
            raise ValueError
        host = p.hostname.lower()
        return f"https://{host}:{p.port or 443}"
    except ValueError:
        raise MediaError("url_blocked") from None


class MediaFetcher:
    def __init__(self, allowed_origins: tuple[str, ...], bearer: str = ""):
        self.allowed = {origin(u) for u in allowed_origins}
        self.bearer = bearer
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=PublicNetwork(),
            max_connections=2,
            max_keepalive_connections=2,
        )

    def validate(self, url: str) -> None:
        if origin(url) not in self.allowed:
            raise MediaError("url_blocked")

    async def fetch(self, url: str, limit: int = 16 * 1024 * 1024) -> bytes:
        self.validate(url)
        headers = {"Accept-Encoding": "identity", "Cache-Control": "no-cache"}
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"
        try:
            async with asyncio.timeout(12):
                async with self.pool.stream(
                    "GET",
                    url,
                    headers=list(headers.items()),
                    extensions={
                        "timeout": {"connect": 5, "read": 8, "write": 5, "pool": 5}
                    },
                ) as response:
                    # Redirects are deliberately not followed; no credential forwarding.
                    if response.status != 200:
                        raise MediaError(f"http_{response.status}")
                    body = bytearray()
                    async for data in response.aiter_stream():
                        body.extend(data)
                        if len(body) > limit:
                            raise MediaError("media_too_large")
                    return bytes(body)
        except MediaError:
            raise
        except Exception:
            raise MediaError("network_error") from None

    async def close(self):
        await self.pool.aclose()


@dataclass(frozen=True)
class Sample:
    rt_us: int
    seq: int
    key: bool


@dataclass(frozen=True)
class Box:
    kind: bytes
    start: int
    body: int
    end: int


def uint(b: bytes, p: int, size: int = 4) -> int:
    if p < 0 or p + size > len(b):
        raise MediaError("truncated_mp4")
    return int.from_bytes(b[p : p + size], "big")


def boxes(b: bytes, start: int = 0, end: int | None = None) -> list[Box]:
    end = len(b) if end is None else end
    result = []
    while start < end:
        if start + 8 > end:
            raise MediaError("truncated_mp4")
        size, header = uint(b, start), 8
        if size == 1:
            size, header = uint(b, start + 8, 8), 16
        elif size == 0:
            size = end - start
        if size < header or start + size > end:
            raise MediaError("truncated_mp4")
        result.append(
            Box(b[start + 4 : start + 8], start, start + header, start + size)
        )
        if len(result) > 20000:
            raise MediaError("mp4_limit")
        start += size
    return result


def child(b: bytes, parent: Box, kind: bytes) -> Box:
    for box in boxes(b, parent.body, parent.end):
        if box.kind == kind:
            return box
    raise MediaError("unsupported_mp4")


@dataclass(frozen=True)
class VideoTrack:
    id: int
    nal_size: int
    default_size: int = 0
    default_flags: int = 0


def parse_init(b: bytes) -> VideoTrack:
    moov = next((x for x in boxes(b) if x.kind == b"moov"), None)
    if moov is None:
        raise MediaError("missing_init")
    for trak in boxes(b, moov.body, moov.end):
        if trak.kind != b"trak":
            continue
        mdia = child(b, trak, b"mdia")
        if b[child(b, mdia, b"hdlr").body + 8 :][:4] != b"vide":
            continue
        stbl = child(b, child(b, mdia, b"minf"), b"stbl")
        stsd = child(b, stbl, b"stsd")
        entries = boxes(b, stsd.body + 8, stsd.end)
        if len(entries) != 1 or entries[0].kind not in (b"avc1", b"avc3"):
            raise MediaError("unsupported_codec")
        entry = entries[0]
        avcc = next(
            (x for x in boxes(b, entry.body + 78, entry.end) if x.kind == b"avcC"), None
        )
        if avcc is None or avcc.end - avcc.body < 7:
            raise MediaError("missing_avcc")
        config = b[avcc.body : avcc.end]
        # Parameter sets are required before claiming an IDR-decodable run.
        pos = 6
        sps = config[5] & 31
        for _ in range(sps):
            length = uint(config, pos, 2)
            pos += 2 + length
            if pos > len(config):
                raise MediaError("truncated_avcc")
        pps = uint(config, pos, 1)
        if not sps or not pps:
            raise MediaError("missing_parameter_sets")
        pos += 1
        for _ in range(pps):
            length = uint(config, pos, 2)
            pos += 2 + length
            if pos > len(config):
                raise MediaError("truncated_avcc")
        tkhd = child(b, trak, b"tkhd")
        track_id = uint(b, tkhd.body + (20 if b[tkhd.body] == 1 else 12))
        size = flags = 0
        for mvex in boxes(b, moov.body, moov.end):
            if mvex.kind == b"mvex":
                for trex in boxes(b, mvex.body, mvex.end):
                    if trex.kind == b"trex" and uint(b, trex.body + 4) == track_id:
                        size, flags = uint(b, trex.body + 16), uint(b, trex.body + 20)
        return VideoTrack(track_id, (config[4] & 3) + 1, size, flags)
    raise MediaError("missing_video")


_UUID = bytes.fromhex("7e57c2ee0dd24b539b3593edf97a12c1")


def sample_sei(payload: bytes, nal_size: int) -> Sample | None:
    pos, info, idr, vcl = 0, None, False, False
    while pos < len(payload):
        length = uint(payload, pos, nal_size)
        pos += nal_size
        if not length or pos + length > len(payload):
            raise MediaError("truncated_nal")
        nal = payload[pos : pos + length]
        pos += length
        kind = nal[0] & 31
        idr |= kind == 5
        vcl |= kind in (1, 5)
        if kind != 6:
            continue
        rbsp = re.sub(b"\x00\x00\x03", b"\x00\x00", nal[1:])
        q = 0
        while q < len(rbsp) and rbsp[q:] != b"\x80":
            values = []
            for _ in range(2):
                value = 0
                while q < len(rbsp) and rbsp[q] == 255:
                    value += 255
                    q += 1
                value += uint(rbsp, q, 1)
                q += 1
                values.append(value)
            typ, size = values
            if q + size > len(rbsp):
                raise MediaError("truncated_sei")
            raw = rbsp[q : q + size]
            q += size
            if typ == 5 and len(raw) >= 38 and raw[:16] == _UUID and raw[16] == 1:
                rt = int.from_bytes(raw[30:38], "big", signed=True)
                if not 0 < rt <= 2**53 - 1 or info is not None:
                    return None
                info = Sample(rt, uint(raw, 18), False)
    return Sample(info.rt_us, info.seq, idr) if info and vcl else None


def parse_fragment(b: bytes, track: VideoTrack) -> list[Sample | None]:
    top = boxes(b)
    mdats = [x for x in top if x.kind == b"mdat"]
    result: list[Sample | None] = []
    for moof in top:
        if moof.kind != b"moof":
            continue
        for traf in boxes(b, moof.body, moof.end):
            if traf.kind != b"traf":
                continue
            tfhd = child(b, traf, b"tfhd")
            tf = b[tfhd.body : tfhd.end]
            if uint(tf, 4) != track.id:
                continue
            flags, p = uint(tf, 0) & 0xFFFFFF, 8
            base = moof.start
            if flags & 1:
                base, p = uint(tf, p, 8), p + 8
            if flags & 2:
                p += 4
            if flags & 8:
                p += 4
            size, sample_flags = track.default_size, track.default_flags
            if flags & 16:
                size, p = uint(tf, p), p + 4
            if flags & 32:
                sample_flags = uint(tf, p)
            sample_pos = None
            for trun in boxes(b, traf.body, traf.end):
                if trun.kind != b"trun":
                    continue
                tr = b[trun.body : trun.end]
                flags, count, p = uint(tr, 0) & 0xFFFFFF, uint(tr, 4), 8
                if count > 10000 or len(result) + count > 20000:
                    raise MediaError("sample_limit")
                if flags & 1:
                    offset = uint(tr, p)
                    sample_pos, p = (
                        base + (offset if offset < 2**31 else offset - 2**32),
                        p + 4,
                    )
                first_flags = sample_flags
                if flags & 4:
                    first_flags, p = uint(tr, p), p + 4
                if sample_pos is None:
                    raise MediaError("missing_data_offset")
                for i in range(count):
                    if flags & 256:
                        p += 4
                    sz, fl = size, sample_flags
                    if flags & 512:
                        sz, p = uint(tr, p), p + 4
                    if flags & 1024:
                        fl, p = uint(tr, p), p + 4
                    if flags & 2048:
                        p += 4
                    if i == 0 and flags & 4:
                        fl = first_flags
                    if (
                        p > len(tr)
                        or not sz
                        or not any(
                            x.body <= sample_pos < sample_pos + sz <= x.end
                            for x in mdats
                        )
                    ):
                        raise MediaError("invalid_sample_bounds")
                    info = sample_sei(b[sample_pos : sample_pos + sz], track.nal_size)
                    # Require both an actual IDR and the container's sync flag.
                    if info and info.key and fl & 0x10000:
                        info = None
                    result.append(info)
                    sample_pos += sz
    if not result:
        raise MediaError("missing_video_samples")
    return result


@dataclass(frozen=True)
class Segment:
    url: str
    init: str
    sequence: int
    discontinuity: int
    duration: float
    gap: bool


def playlist(text: str, base: str) -> tuple[list[Segment], str | None]:
    if not text.lstrip().startswith("#EXTM3U"):
        raise MediaError("invalid_playlist")
    segments = []
    seq = disc = 0
    init, duration, gap, variant = "", None, False, False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY:") and "METHOD=NONE" not in line:
            raise MediaError("encrypted_media_unsupported")
        if (
            line.startswith(("#EXT-X-BYTERANGE:", "#EXT-X-MAP:"))
            and "BYTERANGE" in line
        ):
            raise MediaError("byterange_unsupported")
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            disc = int(line.split(":", 1)[1])
        elif line == "#EXT-X-DISCONTINUITY":
            disc += 1
        elif line == "#EXT-X-GAP":
            gap = True
        elif line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            if not match:
                raise MediaError("missing_init")
            init = urljoin(base, match[1])
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",")[0])
            if not 0 < duration <= 30:
                raise MediaError("segment_duration_unsupported")
        elif line.startswith("#EXT-X-STREAM-INF:"):
            variant = True
        elif line and not line.startswith("#"):
            if variant:
                return [], urljoin(base, line)
            if duration is not None:
                if not init:
                    raise MediaError("fmp4_init_required")
                segments.append(
                    Segment(urljoin(base, line), init, seq, disc, duration, gap)
                )
                seq += 1
                duration, gap = None, False
    return segments, None


class HlsObserver:
    def __init__(self, url, coverage, fetcher):
        self.url, self.coverage, self.fetcher = url, coverage, fetcher
        self.last_sequence = None
        self.last_disc = None
        self.init_url = None
        self.track = None
        self.last_segment_url = None
        self.reason = "waiting_media"
        self.segments = 0
        self.samples = 0
        self.sei_samples = 0
        self.keys = 0

    async def poll(self):
        url = self.url
        for _ in range(3):
            raw = await self.fetcher.fetch(url, 1024 * 1024)
            segments, variant = playlist(raw.decode("utf-8"), url)
            if variant is None:
                break
            url = variant
        else:
            raise MediaError("playlist_depth")
        if not segments:
            raise MediaError("empty_playlist")
        # Muxer reset can reuse media sequence numbers. New init OR a changed
        # last URI at the same high-water sequence breaks the generation.
        tail = segments[-1]
        if self.last_sequence is not None and (
            tail.sequence < self.last_sequence
            or (
                tail.sequence == self.last_sequence
                and self.last_segment_url != tail.url
            )
        ):
            self.last_sequence = None
            self.init_url = None
            self.coverage.break_input()
        if self.last_sequence is None:
            seconds, start = 0.0, len(segments)
            while start and seconds < 60:
                start -= 1
                seconds += segments[start].duration
            segments = segments[start:]
        for seg in segments:
            if self.last_sequence is not None and seg.sequence <= self.last_sequence:
                continue
            if (
                self.last_sequence is not None
                and seg.sequence != self.last_sequence + 1
            ) or (self.last_disc is not None and seg.discontinuity != self.last_disc):
                self.coverage.break_input()
            if seg.gap:
                self.coverage.break_input()
                self.last_sequence, self.last_segment_url = seg.sequence, seg.url
                continue
            if seg.init != self.init_url:
                self.coverage.break_input()
                self.track = parse_init(await self.fetcher.fetch(seg.init, 1024 * 1024))
                self.init_url = seg.init
            raw = await self.fetcher.fetch(seg.url)
            assert self.track is not None
            samples = await asyncio.to_thread(parse_fragment, raw, self.track)
            now = time.monotonic()
            for sample in samples:
                self.coverage.append(sample, now)
                self.samples += 1
                self.sei_samples += sample is not None
                self.keys += bool(sample and sample.key)
            self.segments += 1
            self.last_sequence, self.last_segment_url = seg.sequence, seg.url
            self.last_disc = seg.discontinuity
            self.reason = "observing"

    async def run(self):
        try:
            while True:
                try:
                    await self.poll()
                except Exception as exc:
                    self.reason = (
                        str(exc) if isinstance(exc, MediaError) else "invalid_media"
                    )
                    self.coverage.break_input()
                await asyncio.sleep(0.5)
        finally:
            await self.fetcher.close()
