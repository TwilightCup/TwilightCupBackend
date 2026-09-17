"""Server media clock: continuous encoded coverage, monotonic T, managed tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .media_hls import HlsObserver, MediaFetcher, Sample

SAFETY_US = 30_000_000
# Reserve the full speculative second allowed by the existing ExternalClock.
EXTRAPOLATION_US = 1_000_000


@dataclass
class Coverage:
    from_us: int | None = None
    to_us: int | None = None
    latest_us: int | None = None
    last_seq: int | None = None
    last_rt: int | None = None
    updated_at: float = 0.0
    generation: int = 0

    def break_input(self):
        if self.from_us is not None:
            self.generation += 1
        self.from_us = self.to_us = self.last_seq = self.last_rt = None

    def append(self, sample: Sample | None, now: float):
        if sample is None:
            self.break_input()
            return
        if self.last_seq == sample.seq and self.last_rt == sample.rt_us:
            return
        if self.last_seq is not None and (
            (sample.seq - self.last_seq) % 2**32 != 1
            or abs(sample.rt_us - (self.last_rt or 0)) > 500_000
        ):
            self.break_input()
        self.latest_us = sample.rt_us
        self.last_seq, self.last_rt = sample.seq, sample.rt_us
        if self.from_us is None:
            if not sample.key:
                return
            self.from_us = self.to_us = sample.rt_us
        self.to_us = max(self.to_us or sample.rt_us, sample.rt_us)
        self.updated_at = now
        # Bound metadata horizon (no samples or encoded bytes are retained).
        # This conservative window is shorter than the browser's cold harvest.
        self.from_us = max(self.from_us, self.to_us - 60_000_000)


class MediaClock:
    def __init__(self, floor: int = 0):
        self.t_us = floor
        self.last_mono: float | None = None
        self.frozen = True
        self.generations = None

    def tick(self, a: Coverage, b: Coverage, now: float) -> tuple[int, bool, str]:
        elapsed = 0 if self.last_mono is None else max(0.0, now - self.last_mono)
        self.last_mono = now
        reason = "playing"
        if any(c.from_us is None or c.to_us is None for c in (a, b)):
            reason = "waiting_coverage"
        elif any(now - c.updated_at > 5 for c in (a, b)):
            reason = "stale_media"
        else:
            assert a.from_us is not None and b.from_us is not None
            assert a.to_us is not None and b.to_us is not None
            start = max(a.from_us, b.from_us)
            safe = min(a.to_us, b.to_us) - SAFETY_US
            target = safe - EXTRAPOLATION_US
            generations = (a.generation, b.generation)
            if target < start or target < self.t_us:
                reason = "waiting_coverage"
            elif self.frozen or self.generations != generations:
                # Recovery is one common re-anchor, never accelerated playback.
                self.t_us = max(self.t_us, start, target - 2_000_000)
                self.generations = generations
            elif self.t_us + int(elapsed * 1_000_000) <= target:
                self.t_us += int(elapsed * 1_000_000)
            else:
                reason = "waiting_frontier"
        self.frozen = reason != "playing"
        return self.t_us, self.frozen, reason


class MediaAuthority:
    """One pair of readers and one 400ms clock pump for an account/match scope."""

    def __init__(self, key, urls, epoch, floor, settings, publish):
        self.key, self.urls, self.epoch = key, urls, epoch
        self.src = f"server:{epoch}"
        self.clock = MediaClock(floor)
        self.publish = publish
        self.coverage = (Coverage(), Coverage())
        self.observers = [
            HlsObserver(
                url,
                coverage,
                MediaFetcher(
                    settings.authority_hls_origins, settings.authority_hls_bearer
                ),
            )
            for url, coverage in zip(urls, self.coverage, strict=True)
        ]
        self.task: asyncio.Task | None = None
        self.closed = False

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        readers = (
            [asyncio.create_task(o.run()) for o in self.observers]
            if all(self.urls)
            else []
        )
        try:
            while True:
                await self.publish(self)
                await asyncio.sleep(0.4)
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            for observer in self.observers:
                await observer.fetcher.close()

    def cancel(self):
        if self.closed:
            return
        self.closed = True
        if self.task is not None:
            self.task.cancel()

    async def close(self):
        self.cancel()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)
        await asyncio.gather(*(o.fetcher.close() for o in self.observers))
