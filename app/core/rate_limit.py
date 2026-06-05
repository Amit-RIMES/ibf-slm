import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone

WINDOW_MINUTES = 15
MAX_ATTEMPTS = 5


class LoginRateLimiter:
    def __init__(self) -> None:
        self._failures: dict[str, list[datetime]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def _recent(self, ip: str) -> list[datetime]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)
        self._failures[ip] = [t for t in self._failures[ip] if t > cutoff]
        return self._failures[ip]

    async def is_limited(self, ip: str) -> tuple[bool, int]:
        """Return (limited, seconds_remaining). Seconds is 0 when not limited."""
        async with self._lock:
            attempts = self._recent(ip)
            if len(attempts) >= MAX_ATTEMPTS:
                oldest = attempts[0]
                unlock_at = oldest + timedelta(minutes=WINDOW_MINUTES)
                remaining = (unlock_at - datetime.now(timezone.utc)).seconds + 1
                return True, remaining
            return False, 0

    async def record_failure(self, ip: str) -> None:
        async with self._lock:
            self._recent(ip)  # prune stale
            self._failures[ip].append(datetime.now(timezone.utc))

    async def clear(self, ip: str) -> None:
        async with self._lock:
            self._failures.pop(ip, None)


login_limiter = LoginRateLimiter()
