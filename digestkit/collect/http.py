"""HTTP client for collectors: timeouts, per-host concurrency, no requests to private networks."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from collections import defaultdict
from urllib.parse import urljoin, urlsplit

import httpx

UA = os.environ.get("DIGEST_USER_AGENT", "Mozilla/5.0 (compatible; ai-digests/0.2; +https://github.com/dzorin350-cloud/ai-digests)")


class FetchError(Exception):
    pass


def _is_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


class Client:
    def __init__(self, timeout: float = 20.0, per_host: int = 2) -> None:
        self._sems: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(per_host))
        self._private: dict[str, bool] = {}
        self.allow_private = os.environ.get("DIGEST_ALLOW_PRIVATE") == "1"
        self._cache: dict[str, httpx.Response] = {}  # one run: the same feed used by two blocks is fetched once
        self.http = httpx.AsyncClient(
            timeout=timeout, follow_redirects=False,
            headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )

    async def _check(self, url: str) -> None:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise FetchError(f"bad url: {url}")
        if self.allow_private:
            return
        if p.hostname not in self._private:
            self._private[p.hostname] = await asyncio.to_thread(_is_private, p.hostname)
        if self._private[p.hostname]:
            raise FetchError(f"private address blocked: {p.hostname}")

    async def get(self, url: str, retries: int = 1) -> httpx.Response:
        if url in self._cache:
            return self._cache[url]
        for attempt in range(retries + 1):
            try:
                resp = await self._get(url)
                if resp.status_code == 200:
                    self._cache[url] = resp
                return resp
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt == retries:
                    raise FetchError(f"{type(e).__name__}: {url}") from e
                await asyncio.sleep(1.5 * (attempt + 1))
        raise FetchError("unreachable")

    async def _get(self, url: str) -> httpx.Response:
        for _ in range(6):
            await self._check(url)
            async with self._sems[urlsplit(url).hostname or ""]:
                resp = await self.http.get(url)
            if resp.is_redirect:
                url = urljoin(url, resp.headers.get("location", ""))
                continue
            return resp
        raise FetchError(f"too many redirects: {url}")

    async def aclose(self) -> None:
        await self.http.aclose()

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *a: object) -> None:
        await self.aclose()
