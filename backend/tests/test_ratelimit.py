"""SEC's 10 requests/second is per IP: spacing must hold across processes, not just inside one."""

import multiprocessing as mp
import time

import httpx

from catalystedge.clock import FrozenClock
from catalystedge.core.http import HttpClient
from catalystedge.core.kv import InMemoryKV
from catalystedge.core.ratelimit import MAX_AHEAD_S, FileSlots
from catalystedge.sources import SOURCES


def test_file_slots_space_calls_and_reset_stale_slots(tmp_path):
    now = [1000.0]
    slots = FileSlots(tmp_path, clock=lambda: now[0])
    assert slots.reserve("sec", 0.15) == 0.0
    assert abs(slots.reserve("sec", 0.15) - 0.15) < 1e-9
    assert abs(slots.reserve("sec", 0.15) - 0.30) < 1e-9
    assert slots.reserve("finnhub", 1.2) == 0.0                      # groups are independent
    now[0] += 10
    assert slots.reserve("sec", 0.15) == 0.0                         # slot in the past: go now
    (tmp_path / "sec.slot").write_text(repr(now[0] + MAX_AHEAD_S + 500))
    assert slots.reserve("sec", 0.15) == 0.0                         # stale far-future slot is reset


def _worker(directory, n, out):
    slots = FileSlots(directory)
    for _ in range(n):
        wait = slots.reserve("sec", 0.05)
        if wait > 0:
            time.sleep(wait)
        out.put(time.time())


def test_three_processes_share_one_budget(tmp_path):
    """The failure from this morning: three download processes, each spacing only itself."""
    q = mp.get_context("fork").Queue()
    procs = [mp.get_context("fork").Process(target=_worker, args=(str(tmp_path), 8, q)) for _ in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    stamps = sorted(q.get() for _ in range(24))
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert min(gaps) > 0.04                       # never two requests closer than the interval (small jitter)
    assert (stamps[-1] - stamps[0]) >= 0.05 * 23 * 0.95


def test_sec_sources_use_shared_slots_others_do_not(tmp_path):
    assert SOURCES["sec_edgar"].shared_spacing and SOURCES["sec_feed"].shared_spacing
    assert not SOURCES["finnhub_news"].shared_spacing
    calls, sleeps = [], []

    class Slots:
        def reserve(self, group, interval):
            calls.append((group, interval))
            return 0.1

    clock = FrozenClock(__import__("datetime").datetime(2026, 9, 24, tzinfo=__import__("datetime").UTC))
    http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok")), kv=InMemoryKV(clock),
                      clock=clock, sleep=sleeps.append, slots=Slots())
    http.get_text("sec_edgar", "https://www.sec.gov/a", headers={"User-Agent": "t t@example.com"})
    http.get_text("sec_feed", "https://www.sec.gov/b", headers={"User-Agent": "t t@example.com"})
    assert calls == [("sec", 0.15), ("sec", 0.15)] and sleeps == [0.1, 0.1]   # one group for both SEC sources


def test_redis_slots_share_spacing(tmp_path):
    """Same guarantee through Redis (local Docker's redis, Upstash in the cloud)."""
    import shutil
    import socket
    import subprocess

    import pytest

    if not shutil.which("redis-server"):
        pytest.skip("redis-server not installed")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen(["redis-server", "--port", str(port), "--save", "", "--appendonly", "no"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        from catalystedge.core.ratelimit import RedisSlots

        for _ in range(50):
            try:
                a = RedisSlots(f"redis://127.0.0.1:{port}/0")
                a.reserve("warmup", 0.0)
                break
            except Exception:
                time.sleep(0.1)
        b = RedisSlots(f"redis://127.0.0.1:{port}/0")          # a second client, as another process would be
        waits = [a.reserve("sec", 0.2), b.reserve("sec", 0.2), a.reserve("sec", 0.2)]
        assert waits[0] < 0.05 and 0.15 < waits[1] < 0.25 and 0.35 < waits[2] < 0.45
    finally:
        proc.terminate()
        proc.wait(5)
