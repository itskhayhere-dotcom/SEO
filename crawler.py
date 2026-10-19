"""Polite async fetcher: robots.txt, per-host pacing, Retry-After backoff, size caps, SSRF guard."""
from __future__ import annotations
import asyncio, ipaddress, random, socket, time
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
import httpx
from . import config

@dataclass
class Resp:
    url: str
    final_url: str = ""
    status: int = 0
    headers: dict = field(default_factory=dict)
    text: str = ""
    chain: list = field(default_factory=list)      # [(url, status), ...] every hop
    elapsed_ms: int = 0                            # time to response headers of the final hop (TTFB proxy)
    error: str | None = None
    robots_blocked: bool = False
    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 400

class Fetcher:
    def __init__(self):
        self.client = httpx.AsyncClient(
            headers={"User-Agent": config.USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                     "Accept-Language": "en"},
            timeout=httpx.Timeout(20, connect=10), follow_redirects=False)
        self._robots: dict[str, tuple[float, RobotFileParser | None, float | None]] = {}
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._sem = asyncio.Semaphore(config.CONCURRENCY)
        self._pub: dict[str, bool] = {}

    async def close(self):
        await self.client.aclose()

    async def _public(self, host: str) -> bool:
        """SSRF guard: refuse private/loopback/link-local hosts (a redirect must not reach your LAN)."""
        if config.ALLOW_PRIVATE:
            return True
        if host not in self._pub:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
                self._pub[host] = bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)
            except Exception:
                self._pub[host] = False
        return self._pub[host]

    async def _pace(self, host: str, delay: float):
        async with self._locks[host]:
            wait = self._next.get(host, 0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next[host] = time.monotonic() + delay + random.uniform(0, delay * 0.25)

    async def robots_for(self, url: str):
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        hit = self._robots.get(origin)
        if hit and time.time() - hit[0] < 3600:
            return hit[1], hit[2]
        rp, delay = None, None
        try:
            await self._pace(p.hostname or "", config.CRAWL_DELAY)
            r = await self.client.get(origin + "/robots.txt", follow_redirects=True)
            if r.status_code == 200:
                rp = RobotFileParser(); rp.parse(r.text.splitlines())
                delay = rp.crawl_delay(config.AGENT_TOKEN)
            elif r.status_code >= 500:           # RFC 9309: server error => treat as "disallow all" for now
                rp = RobotFileParser(); rp.parse(["User-agent: *", "Disallow: /"])
        except Exception:
            pass
        self._robots[origin] = (time.time(), rp, float(delay) if delay else None)
        return rp, self._robots[origin][2]

    async def get(self, url: str, *, method: str = "GET", respect_robots: bool = True, delay: float | None = None,
                  max_bytes: int | None = None, max_hops: int = 8) -> Resp:
        max_bytes = max_bytes or config.MAX_BYTES
        out = Resp(url=url, final_url=url)
        cur, seen, retries = url, {url}, 0
        while len(out.chain) <= max_hops:
            host = urlsplit(cur).hostname or ""
            if urlsplit(cur).scheme not in ("http", "https") or not host:
                out.error = "bad_url"; return out
            if not await self._public(host):
                out.error = "blocked_or_unresolvable_host"; return out
            crawl_delay = None
            if respect_robots:
                rp, crawl_delay = await self.robots_for(cur)
                if rp is not None and not rp.can_fetch(config.AGENT_TOKEN, cur):
                    out.error, out.robots_blocked = "robots_disallow", True; return out
            await self._pace(host, max(delay if delay is not None else config.CRAWL_DELAY, crawl_delay or 0))
            t0 = time.monotonic()
            try:
                async with self._sem, self.client.stream(method, cur) as r:
                    out.elapsed_ms = int((time.monotonic() - t0) * 1000)
                    status, headers = r.status_code, dict(r.headers)
                    body = b""
                    ctype = headers.get("content-type", "")
                    if method == "GET" and status < 400 and (not ctype or any(k in ctype for k in ("html", "xml", "text", "json"))):
                        async for chunk in r.aiter_bytes():
                            body += chunk
                            if len(body) >= max_bytes:
                                break
                    enc = r.charset_encoding or "utf-8"
            except Exception as e:
                out.error = type(e).__name__; return out
            if status in (429, 503) and retries < 2:
                retries += 1
                try:
                    wait = min(float(headers.get("retry-after", 5)), 60)
                except ValueError:
                    wait = 10
                await asyncio.sleep(wait); continue
            out.chain.append((cur, status))
            out.status, out.headers, out.final_url = status, headers, cur
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                cur = urljoin(cur, headers["location"])
                if cur in seen:
                    out.error = "redirect_loop"; return out
                seen.add(cur); continue
            out.text = body.decode(enc, "replace")
            return out
        out.error = "too_many_redirects"
        return out

_shared: Fetcher | None = None
def shared() -> Fetcher:
    """One fetcher for the whole process so per-host pacing holds across concurrent jobs."""
    global _shared
    if _shared is None:
        _shared = Fetcher()
    return _shared
