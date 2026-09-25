"""Request spacing shared by every process, for providers whose limit is per IP or per key (SEC: 10 req/s).

Each call reserves the next free slot for its rate group and sleeps until it:
  RedisSlots  one atomic Lua script on the shared Redis (local Docker's redis, Upstash in the cloud), so
              the API, the worker, beat and any script on any container share one budget
  FileSlots   a lock file per group on this machine (no Redis, e.g. a standalone download script), so
              parallel processes on one computer still share it
A slot more than MAX_AHEAD_S in the future is treated as stale (e.g. a crashed process) and reset.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Protocol

MAX_AHEAD_S = 60.0


class SlotStore(Protocol):
    def reserve(self, group: str, interval_s: float) -> float:
        """Reserve the next slot; returns how many seconds to wait before sending."""


class FileSlots:
    def __init__(self, directory: str | Path | None = None, clock=time.time):
        self.dir = Path(directory or os.environ.get("CATALYSTEDGE_RATE_DIR")
                        or Path(tempfile.gettempdir()) / "catalystedge-rate")
        self.clock = clock
        self._lock = threading.Lock()

    def reserve(self, group: str, interval_s: float) -> float:
        try:
            import fcntl
        except ImportError:          # not POSIX: this process only
            fcntl = None
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self.dir / f"{group}.slot", "a+") as fh:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                raw = fh.read().strip()
                now = self.clock()
                prev = float(raw) if raw else 0.0
                slot = max(now, prev)
                if slot - now > MAX_AHEAD_S:
                    slot = now
                fh.seek(0)
                fh.truncate()
                fh.write(repr(slot + interval_s))
                fh.flush()
                return slot - now
            finally:
                if fcntl:
                    fcntl.flock(fh, fcntl.LOCK_UN)


_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local prev = tonumber(redis.call('GET', KEYS[1]) or '0')
local slot = math.max(now, prev)
if slot - now > tonumber(ARGV[2]) then slot = now end
redis.call('SET', KEYS[1], tostring(slot + tonumber(ARGV[1])), 'EX', 120)
return tostring(slot - now)
"""


class RedisSlots:
    def __init__(self, url: str, prefix: str = "ce:rate:"):
        import redis

        self._r = redis.Redis.from_url(url)
        self._script = self._r.register_script(_LUA)
        self._prefix = prefix

    def reserve(self, group: str, interval_s: float) -> float:
        return float(self._script(keys=[self._prefix + group], args=[interval_s, MAX_AHEAD_S]))
