"""Synthetic fMP4/SEI fixtures and fail-closed HLS/network boundaries."""

import struct
from unittest.mock import AsyncMock

import pytest

from twilightcupbackend.media_authority import Coverage
from twilightcupbackend.media_hls import (
    HlsObserver,
    MediaError,
    MediaFetcher,
    PublicNetwork,
    parse_fragment,
    parse_init,
    playlist,
    sample_sei,
)


def box(kind, payload):
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def init_segment():
    avcc = bytes([1, 100, 0, 42, 255, 225]) + b"\x00\x02\x67\x64\x01\x00\x02\x68\x00"
    entry = box(b"avc1", bytes(78) + box(b"avcC", avcc))
    stbl = box(b"stbl", box(b"stsd", bytes(4) + struct.pack(">I", 1) + entry))
    mdia = box(b"mdia", box(b"hdlr", bytes(8) + b"vide") + box(b"minf", stbl))
    tkhd = box(b"tkhd", bytes(12) + struct.pack(">I", 1))
    return box(b"moov", box(b"trak", tkhd + mdia))


def avcc_sample(seq, rt, key=False):
    raw = bytes.fromhex("7e57c2ee0dd24b539b3593edf97a12c1")
    raw += struct.pack(">BBIqq", 1, 3 if key else 2, seq, seq * 100000, rt)
    rbsp = bytes([5, len(raw)]) + raw + b"\x80"
    escaped = bytearray()
    zeros = 0
    for value in rbsp:
        if zeros >= 2 and value <= 3:
            escaped.append(3)
            zeros = 0
        escaped.append(value)
        zeros = zeros + 1 if value == 0 else 0
    nal = b"\x06" + escaped
    vcl = b"\x65\x88" if key else b"\x41\x88"
    return struct.pack(">I", len(nal)) + nal + struct.pack(">I", len(vcl)) + vcl


def fragment(start_seq=0, start_rt=1_000_000_000, count=20):
    samples = [
        avcc_sample(start_seq + i, start_rt + i * 100000, i == 0) for i in range(count)
    ]
    tfhd = box(b"tfhd", struct.pack(">III", 0x20020, 1, 0))

    def moof(offset):
        run = struct.pack(">IIi", 0x201, count, offset)
        run += b"".join(struct.pack(">I", len(sample)) for sample in samples)
        return box(b"moof", box(b"traf", tfhd + box(b"trun", run)))

    header = moof(0)
    return moof(len(header) + 8) + box(b"mdat", b"".join(samples))


def test_mp4_metadata_from_real_sample_boundaries():
    track = parse_init(init_segment())
    samples = parse_fragment(fragment(), track)
    assert len(samples) == 20 and all(samples)
    assert samples[0] is not None and samples[1] is not None
    assert samples[-1] is not None
    assert samples[0].key and not samples[1].key
    assert samples[-1].rt_us == 1_001_900_000
    parsed = sample_sei(avcc_sample(7, 1_700_000_000_000_001, True), 4)
    assert parsed is not None and parsed.seq == 7
    with pytest.raises(MediaError):
        parse_fragment(fragment()[:-1], track)
    with pytest.raises(MediaError):
        parse_init(b"not mp4")
    assert sample_sei(struct.pack(">I", 2) + b"\x41\x88", 4) is None


def media_list(seq=0, gap=False, init="init.mp4", names=("a.mp4", "b.mp4")):
    return (
        "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:"
        + str(seq)
        + '\n#EXT-X-MAP:URI="'
        + init
        + '"\n'
        + ("#EXT-X-GAP\n" if gap else "")
        + "".join("#EXTINF:2,\n" + name + "\n" for name in names)
    ).encode()


async def test_hls_missing_segment_gap_restart_and_cleanup():
    base = "https://example.com/"
    content = {
        base + "index.m3u8": media_list(),
        base + "init.mp4": init_segment(),
        base + "a.mp4": fragment(),
        base + "b.mp4": fragment(20, 1_002_000_000),
    }

    async def fetch(url, limit=None):
        return content[url]

    fetcher = AsyncMock()
    fetcher.fetch.side_effect = fetch
    coverage = Coverage()
    observer = HlsObserver(base + "index.m3u8", coverage, fetcher)
    await observer.poll()
    assert observer.samples == 40 and coverage.to_us == 1_003_900_000
    await observer.poll()
    assert observer.samples == 40  # no duplicate append
    content[base + "index.m3u8"] = media_list(seq=3, gap=True, names=("c.mp4",))
    await observer.poll()
    assert coverage.to_us is None
    content[base + "index.m3u8"] = media_list(seq=0, names=("restart.mp4",))
    content[base + "restart.mp4"] = fragment(0, 1_100_000_000)
    await observer.poll()
    assert coverage.from_us == 1_100_000_000 and coverage.generation > 0


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/index.m3u8",
        "https://other.com/index.m3u8",
        "https://user:secret@example.com/index.m3u8",
        "file:///etc/passwd",
        "https://example.com:123/index.m3u8",
        "https://example.com/#secret",
    ],
)
async def test_origin_restrictions(url):
    fetcher = MediaFetcher(("https://example.com",))
    try:
        with pytest.raises(MediaError, match="url_blocked"):
            await fetcher.fetch(url)
    finally:
        await fetcher.close()


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "::1", "169.254.169.254", "10.0.0.1", "100.64.0.1"]
)
async def test_dns_cannot_rebind_to_private_address(monkeypatch, ip):
    import asyncio

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "getaddrinfo", AsyncMock(return_value=[(2, 1, 6, "", (ip, 443))])
    )
    with pytest.raises(MediaError, match="network_blocked"):
        await PublicNetwork().connect_tcp("example.com", 443)


def test_unsupported_media_fails_closed():
    for tag in ('#EXT-X-KEY:METHOD=AES-128,URI="key"', "#EXT-X-BYTERANGE:100@0"):
        with pytest.raises(MediaError):
            playlist("#EXTM3U\n" + tag, "https://example.com/")


async def test_fetch_limits_redirects_and_sanitizes_transport_errors(monkeypatch):
    from contextlib import asynccontextmanager

    fetcher = MediaFetcher(("https://example.com",), "test-only-secret")
    await fetcher.pool.aclose()

    class Response:
        status = 302

        async def aiter_stream(self):
            yield b"x" * 20

    response = Response()

    class Pool:
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            yield response

        async def aclose(self):
            pass

    monkeypatch.setattr(fetcher, "pool", Pool())
    try:
        with pytest.raises(MediaError, match="http_302"):
            await fetcher.fetch("https://example.com/?token=secret")
        response.status = 200
        with pytest.raises(MediaError, match="media_too_large"):
            await fetcher.fetch("https://example.com/", limit=10)

        @asynccontextmanager
        async def fail(*args, **kwargs):
            raise RuntimeError("https://example.com/?token=secret")
            yield  # pragma: no cover

        monkeypatch.setattr(fetcher.pool, "stream", fail)
        with pytest.raises(MediaError, match=r"^network_error$"):
            await fetcher.fetch("https://example.com/?token=secret")
    finally:
        await fetcher.close()
